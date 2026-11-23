"""Which agent run may go now: a lease in the database (`agent_run_lease`, migration 024, #213).

Every agent run, by hand or scheduled, takes the lease before it does anything else and gives it
back at its end (`vending.agent.run`). Two runs never overlap, in any mode, from any machine on the
same database.

Before each paid model request, the harness checks the lease in the same transaction that reserves the
request's budget (`hold`, through `vending.harness.Harness`'s `hold`). It renews the lease there, under
the row's lock, and refuses unless the lease then lasts longer than a model request can take. A takeover
(`take`) locks the same row, so the two never interleave: a run that was taken over reserves nothing and
sends nothing, and a run that reserved keeps its lease until its request has ended.

It is a row, not a session lock, so a dropped or killed database connection never frees it while
the run goes on. The run renews it every `HEARTBEAT` seconds, on a fresh connection each time, and
checks it still holds it before each model request and before a purchase request is sent. A run
that stops renewing (its process died or stalled, or it lost the database) loses it at `expires_at`,
`TTL` after the last renewal. The next run takes it over (`take`), and records the old run as failed
if the old run never recorded its own ending. No renewal goes past `hard_until`: the run's deadline
plus `GRACE`. So even a stuck run that still renews lets go then.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

import psycopg

from vending import agent_runs

# How long a lease lasts after it is taken or renewed. Longer than a model request (5 minutes) and an
# MCP request (2 minutes), so a run waiting on one keeps it even if a renewal is late.
TTL = timedelta(minutes=10)
HEARTBEAT = 60.0        # seconds between renewals while a run goes on
# How long past the run's deadline the lease may last: a model request already sent finishes first,
# and the run records its ending.
GRACE = timedelta(minutes=10)
EXPIRED = ("Stopped: its run lease expired at {at} without being renewed (its process stopped or "
           "lost the database), so the next run took it over.")


LOST = "this run no longer holds the run lease (it expired or another run took it over)"
NEAR_END = "this run's lease ends before a model request could finish (the run is at its deadline)"


@dataclass(frozen=True)
class Lease:
    token: uuid.UUID
    took_over: int | None = None    # the agent run whose expired lease this one took, if it had one


@dataclass(frozen=True)
class Busy:
    run_id: int | None              # the run holding it, once that run has an id


@dataclass(frozen=True)
class Refused:
    detail: str                     # why `authorize` said no


def take(conn: psycopg.Connection, *, deadline: float,
         authorize: Callable[[psycopg.Connection], str | None] | None = None) -> Lease | Busy | Refused:
    """Take the lease, in one transaction with the row locked. `authorize(conn)`, if given, runs in that
    transaction once the lease is known to be free, and returns why not, or None to go on."""
    from vending.harness import EASTERN

    with conn.transaction():
        token, holder, expires, expired = conn.execute(
            "select token, run_id, expires_at, expires_at <= now() from agent_run_lease where one for update").fetchone()
        if token is not None and not expired:
            return Busy(holder)
        # Whose scheduled time this lease is for: none, unless `authorize` (the scheduler's) says so below.
        conn.execute("update agent_run_lease set schedule_due = null where one")
        if authorize is not None and (why := authorize(conn)) is not None:
            return Refused(why)
        mine = uuid.uuid4()
        conn.execute("update agent_run_lease set token = %s, run_id = null, taken_at = now(), renewed_at = now(),"
                     " hard_until = now() + %s, expires_at = least(now() + %s, now() + %s) where one",
                     (mine, timedelta(seconds=deadline) + GRACE, TTL, timedelta(seconds=deadline) + GRACE))
        took_over = None
        if token is not None and holder is not None:
            at = f"{expires.astimezone(EASTERN):%a %d %b %H:%M} ET"
            # A run that submitted stays `submitted`, with the confirmation first (`agent_runs.SUBMITTED`).
            found = conn.execute(
                "update agent_runs set finished_at = now(),"
                " status = case when request_id is not null then 'submitted' else 'failed' end,"
                " detail = case when request_id is not null then %s || request_id || %s || %s else %s end"
                " where id = %s and status = 'running' returning id",
                (agent_runs.SUBMITTED, agent_runs.AFTERWARDS, EXPIRED.format(at=at), EXPIRED.format(at=at),
                 holder)).fetchone()
            took_over = holder if found else None
    return Lease(mine, took_over)


def attach(conn: psycopg.Connection, token: uuid.UUID, run_id: int) -> bool:
    """Name the holder's run, so a run that finds the lease taken can say which. False if it isn't held. A lease
    taken for a scheduled time (`schedule_due`) names the run on that `agent_schedule` row too, so the row links
    its run even if the worker never records the ending."""
    with conn.transaction():
        row = conn.execute("update agent_run_lease set run_id = %s where token = %s returning schedule_due",
                           (run_id, token)).fetchone()
        if row is not None and row[0] is not None:
            conn.execute("update agent_schedule set run_id = %s where due_at = %s and run_id is null", (run_id, row[0]))
    return row is not None


def renew(conn: psycopg.Connection, token: uuid.UUID) -> bool:
    """Push the expiry on, never past `hard_until`. False when this run no longer holds the lease: it was
    taken over, or it expired (an expired lease is never renewed, since another run may be taking it)."""
    return conn.execute("update agent_run_lease set renewed_at = now(), expires_at = least(now() + %s, hard_until)"
                        " where token = %s and expires_at > now() returning one", (TTL, token)).fetchone() is not None


def hold(conn: psycopg.Connection, token: uuid.UUID, margin: timedelta) -> str | None:
    """For the harness, inside the transaction that reserves a paid request's budget: renew the lease under
    its row lock, and return why the request may not go (None when it may). It may go only while this run
    holds an unexpired lease that, renewed, lasts at least `margin` more: longer than the request can take."""
    row = conn.execute("update agent_run_lease set renewed_at = clock_timestamp(),"
                       " expires_at = least(clock_timestamp() + %s, hard_until)"
                       " where token = %s and expires_at > clock_timestamp()"
                       " returning expires_at >= clock_timestamp() + %s", (TTL, token, margin)).fetchone()
    if row is None:
        return LOST
    return None if row[0] else NEAR_END


def give_back(conn: psycopg.Connection, token: uuid.UUID) -> None:
    """Free the lease, if this run still holds it."""
    conn.execute("update agent_run_lease set token = null, run_id = null, taken_at = null, renewed_at = null,"
                 " expires_at = null, hard_until = null, schedule_due = null where token = %s", (token,))
