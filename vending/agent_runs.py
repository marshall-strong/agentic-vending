"""The record of each agent run, for the dashboard. See `db/migrations/009_agent_runs.sql`.

The agent loop (#39) calls `start` when a run begins and `finish` once when it
ends, whatever happened:

    run_id = agent_runs.start(conn, mode="dry-run", model="claude-sonnet-5-5")
    ...
    agent_runs.finish(conn, run_id, "skipped", plan=plan, update=update,
                      notes=notes, detail="nothing sold out", cost_usd=Decimal("0.42"))

These are bookkeeping, like `action_log`: they don't go through the write
gate, so a halted run can still record that it was halted.
"""

from __future__ import annotations

import json
from decimal import Decimal

import psycopg
from psycopg.types.json import Jsonb

def _dumps(value: object) -> str:
    """JSON with money as exact text: a Decimal price is sent as "2.50", never 2.5."""
    def default(v: object) -> str:
        if isinstance(v, Decimal):
            return str(v)
        raise TypeError(f"can't store {type(v).__name__} in a plan")
    return json.dumps(value, default=default)


MODES = ("live", "dry-run", "replay")
ENDINGS = ("submitted", "skipped", "halted", "failed")


def start(conn: psycopg.Connection, *, mode: str, model: str | None = None) -> int:
    """Record a run as `running` and return its id."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}, not {mode!r}")
    with conn.transaction():
        return conn.execute(
            "insert into agent_runs (mode, model) values (%s, %s) returning id", (mode, model)
        ).fetchone()[0]


def finish(conn: psycopg.Connection, run_id: int, status: str, *, plan: dict | None = None,
           update: str | None = None, notes: str | None = None, detail: str | None = None,
           cost_usd: Decimal | None = None, request_id: str | None = None) -> None:
    """Record how a run ended. Only a `running` run can finish, and only once."""
    if status not in ENDINGS:
        raise ValueError(f"status must be one of {', '.join(ENDINGS)}, not {status!r}")
    if cost_usd is not None and not isinstance(cost_usd, Decimal):
        raise TypeError("cost_usd must be a Decimal, so money is never a float")
    with conn.transaction():
        row = conn.execute(
            "update agent_runs set finished_at = now(), status = %s, plan = %s, team_update = %s,"
            " notes = %s, detail = %s, cost_usd = %s, request_id = %s"
            " where id = %s and status = 'running' returning id",
            (status, None if plan is None else Jsonb(plan, dumps=_dumps), update, notes, detail, cost_usd,
             request_id, run_id),
        ).fetchone()
    if row is None:
        raise ValueError(f"agent run {run_id} doesn't exist or has already finished")
