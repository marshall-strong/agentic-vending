"""The record of every purchase request we tried to send, and how to settle one left open.

    python -m vending.submissions list
    python -m vending.submissions check                                         # ask the bridge first
    python -m vending.submissions resolve 7 --request-id pr_abc123 --by kevin   # the bridge filed it
    python -m vending.submissions resolve 7 --not-filed --by kevin              # it never did

`submit_desired_state` commits a `sending` row here before it POSTs, and
settles it after, on the same connection. A row left `sending` (a crash in
between) or `unknown` (sent, but no usable answer) may or may not exist on the
bridge, and both the tool and `preview_desired_state` refuse
(`submission_unresolved`) until a person settles it.

`check` settles what the bridge can answer by itself, and a live agent run does
the same before it starts. It looks up an open row only by a request ID the row
already holds (in `request_id`, or as `id` in the stored `response`): found
means `accepted`; a `404` means `not_filed`, but only when the row is older than
`SAFE_WAIT` and another of our requests reads fine on the same route, so a
broken route can't pass for a missing request. Anything else leaves the row
open. Most open rows hold no ID (a timeout leaves none), and the bridge can't
find a request by anything else, so those still need a person.

To settle one, find out whether the bridge filed it. If the request ID is known
(the server log has a `LOST ANSWER` line, or the row's `response` holds it),
the bridge's Swagger page has `GET /api/v1/purchase-requests/{id}`. If it isn't,
**ask the instructors first**: the bridge has no route that lists our requests,
and a resend of a request that was filed would order twice. The request would
carry the lines in the row's `body`.

Resolving with a request ID also records the request in `purchase_requests`,
with the visit fee the plan quoted, so the request sync reads it back from then
on. `--by` is required: every change is logged in `action_log` with your name.
This never migrates, and the halt switch doesn't block it, because it's a
person correcting our records.

It does wait for a submit in flight. A submit holds the halt row's lock from
planning until it has settled its row, and `resolve` takes the same lock, so a
person's answer and the bridge's can't overwrite each other. `settle` also
refuses a row that is no longer `sending` (`AlreadySettled`), in case anything
else changed it.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from vending import db
from vending.config import is_halted
from vending.halt import WAIT_NOTICE_MS
from vending.pricing import Refusal
from vending.purchase_requests import record
from vending.write_gate import log

OPEN = ("sending", "unknown")
RESOLVE_HINT = ("a person must run python -m vending.submissions check; if it can't tell, find out whether the "
                "bridge filed it (ask the instructors if the request ID is unknown), then run: "
                "python -m vending.submissions resolve {row} ...")
# A `404` counts as "never filed" only for a row at least this old, so a request the bridge is
# still writing can't be taken for a missing one.
SAFE_WAIT = timedelta(minutes=30)
AUTO_BY = "auto-check"     # `resolved_by` for a row `check` settled from the bridge's own answer


class AlreadySettled(RuntimeError):
    """`settle` found its row no longer `sending`: something else settled it while the request was in flight."""

    def __init__(self, row: int, status: str, by: str | None):
        super().__init__(
            f"submission {row} was already settled as {status}{f' by {by}' if by else ''} while its request was "
            "in flight, so the bridge's answer was not recorded over it")
        self.row, self.status, self.by = row, status, by


def save_sending(side: psycopg.Connection, plan_hash: str, body: dict,
                 directive: str | None, visit_fee_cents: int | None = None) -> int:
    """Commit a `sending` row on `side`, an autocommit connection of its own, so a crash after this can't lose it.

    `visit_fee_cents` is the fee the plan quoted for an off-cycle request (None
    otherwise). It is saved here, before the POST, so even a request settled
    later by `resolve` carries the fee that was agreed when it was sent.
    """
    if not side.autocommit:
        # In a transaction the row would only commit after the POST, which is what it guards against.
        raise ValueError("save_sending needs an autocommit connection")
    (row,) = side.execute(
        "insert into purchase_submissions (plan_hash, body, directive, visit_fee_cents)"
        " values (%s, %s, %s, %s) returning id",
        (plan_hash, Jsonb(body), directive, visit_fee_cents),
    ).fetchone()
    return row


def settle(side: psycopg.Connection, row: int, status: str, *, request_id: str | None = None,
           response: object = None, detail: str | None = None, submitted_at: datetime | None = None) -> None:
    """Record how a submission ended, on the connection that saved it.

    `accepted` also records the request in `purchase_requests`, in the same
    transaction, so the ID can't be stored in one place and not the other.

    Only a row still `sending` is settled. Anything else raises `AlreadySettled`
    and leaves the row as it is, so a person's `resolve` is never overwritten.
    """
    with side.transaction():
        current, by, body, plan_hash, fee = side.execute(
            "select status, resolved_by, body, plan_hash, visit_fee_cents from purchase_submissions"
            " where id = %s for update", (row,)).fetchone()
        if current != "sending":
            raise AlreadySettled(row, current, by)
        side.execute(
            "update purchase_submissions set status = %s, request_id = %s, response = %s, detail = %s"
            " where id = %s and status = 'sending'",
            (status, request_id, None if response is None else Jsonb(response), detail, row),
        )
        if status == "accepted":
            record(side, request_id, submitted_at or datetime.now(timezone.utc), bool(body.get("off_cycle")),
                   body, plan_hash=plan_hash, visit_fee_cents=fee)


def open_rows(conn: psycopg.Connection) -> list[tuple]:
    """(id, at, status, line count, detail) for every submission a person still has to settle."""
    return conn.execute(
        "select id, at, status, jsonb_array_length(body->'lines'), detail from purchase_submissions"
        " where status = any(%s) order by id",
        (list(OPEN),),
    ).fetchall()


def unresolved(conn: psycopg.Connection) -> Refusal | None:
    """The refusal both preview and submit raise while any submission is still open, naming every one."""
    rows = open_rows(conn)
    if not rows:
        return None
    named = ", ".join(f"{row} ({status} since {at:%Y-%m-%d %H:%M} UTC)" for row, at, status, _, _ in rows)
    return Refusal("submission_unresolved", (
        f"submission{'s' if len(rows) > 1 else ''} {named} may already exist on the bridge, so nothing can be "
        f"submitted; {RESOLVE_HINT.format(row=rows[0][0])}"))


def resolve(conn: psycopg.Connection, row: int, request_id: str | None, who: str, *,
            lock_timeout_ms: int | None = None) -> str:
    """Settle one open row as filed under `request_id`, or as never filed. Returns what was done.

    Waits for a submit in flight to finish first. With `lock_timeout_ms`, it gives
    up after that long and raises `psycopg.errors.LockNotAvailable`, so a caller
    can say it's waiting.
    """
    if not who.strip():
        raise ValueError("--by needs your name")
    with conn.transaction():
        if lock_timeout_ms is not None:
            conn.execute(f"set local lock_timeout = {int(lock_timeout_ms)}")
        # The lock every write takes; a submit holds it until its row is settled.
        is_halted(conn, lock=True)
        found = conn.execute(
            "select at, status, body, plan_hash, visit_fee_cents from purchase_submissions where id = %s for update",
            (row,)).fetchone()
        if found is None:
            raise ValueError(f"There is no submission {row}.")
        at, status, body, plan_hash, fee = found
        if status not in OPEN:
            raise ValueError(f"Submission {row} is {status}, not sending or unknown; nothing to settle.")
        if request_id:
            request_id = request_id.strip()
            conn.execute("update purchase_submissions set status = 'accepted', request_id = %s, resolved_by = %s,"
                         " resolved_at = now() where id = %s", (request_id, who.strip(), row))
            record(conn, request_id, at, bool(body.get("off_cycle")), body, plan_hash=plan_hash, visit_fee_cents=fee)
            done = f"submission {row} recorded as filed under {request_id}, pending, by {who.strip()}"
        else:
            conn.execute("update purchase_submissions set status = 'not_filed', resolved_by = %s, resolved_at = now()"
                         " where id = %s", (who.strip(), row))
            done = f"submission {row} marked never filed by {who.strip()}; the plan may be submitted again"
        log(conn, "write", "submissions.resolve", {"row": row, "request_id": request_id, "was": status}, "done", done)
    return done[0].upper() + done[1:]


@dataclass
class Checked:
    row: int
    status: str        # `accepted` or `not_filed` if settled; otherwise the row's status, still open
    why: str


def known_request_id(request_id: str | None, response: Any) -> str | None:
    """The bridge's ID for a row, if the row holds one: its `request_id`, or `id` in the answer it kept."""
    found = request_id or (response.get("id") if isinstance(response, dict) else None)
    return found.strip() if isinstance(found, str) and found.strip() else None


def check(conn: psycopg.Connection, bridge: Callable[[], Any], now: datetime | None = None, *,
          wait: timedelta = SAFE_WAIT, lock_timeout_ms: int | None = None) -> list[Checked]:
    """Settle each open row the bridge can answer for by itself. Returns one `Checked` per open row.

    It never sends anything, and it settles only on the bridge's own answer: when in doubt the
    row stays open. The bridge is read first, outside any lock; then each row is settled through
    `resolve`, which takes the halt lock and refuses a row someone settled meanwhile.
    `bridge` is called only if some open row holds a request ID.
    """
    from vending.bridge import BridgeError, BridgeNotFound

    now = now or datetime.now(timezone.utc)
    rows = conn.execute("select id, at, status, request_id, response from purchase_submissions"
                        " where status = any(%s) order by id", (list(OPEN),)).fetchall()
    client = None
    results = []
    for row, at, status, request_id, response in rows:
        found = known_request_id(request_id, response)
        if found is None:
            results.append(Checked(row, status, "it holds no request ID, and the bridge can't find a request "
                                                "by anything else"))
            continue
        try:
            client = client or bridge()
            answer = client.purchase_request(found)
        except BridgeNotFound:
            settle_as, why = _absent(conn, client, found, at, now, wait)
        except BridgeError as error:
            settle_as, why = None, f"the bridge couldn't be asked about {found} ({type(error).__name__})"
        except Exception as error:      # no bridge address or key set, say: the row just stays open
            settle_as, why = None, f"the bridge couldn't be asked ({type(error).__name__})"
        else:
            if isinstance(answer, dict) and answer.get("id") == found:
                settle_as, why = found, f"the bridge has request {found} ({answer.get('status') or 'no status'})"
            else:
                settle_as, why = None, f"the bridge's answer for {found} doesn't name that request"
        if settle_as is None:
            results.append(Checked(row, status, why))
            continue
        try:
            resolve(conn, row, settle_as or None, AUTO_BY, lock_timeout_ms=lock_timeout_ms)
        except (ValueError, psycopg.errors.LockNotAvailable) as error:
            results.append(Checked(row, status, f"{why}, but it wasn't settled ({type(error).__name__})"))
            continue
        results.append(Checked(row, "accepted" if settle_as else "not_filed", why))
    return results


def _absent(conn: psycopg.Connection, client: Any, request_id: str, at: datetime, now: datetime,
            wait: timedelta) -> tuple[str | None, str]:
    """After a `404` for `request_id`: ("", why) to settle as never filed, or (None, why) to leave it open."""
    from vending.bridge import BridgeError

    if now - at < wait:
        return None, f"the bridge has no {request_id} yet, but the row is under {wait} old"
    control = conn.execute("select request_id from purchase_requests where request_id <> %s"
                           " order by submitted_at desc limit 1", (request_id,)).fetchone()
    if control is None:
        return None, f"the bridge has no {request_id}, but no other request of ours shows the route works"
    try:
        seen = client.purchase_request(control[0])
    except BridgeError as error:
        return None, f"the bridge has no {request_id}, and reading {control[0]} failed too ({type(error).__name__})"
    if not (isinstance(seen, dict) and seen.get("id") == control[0]):
        return None, f"the bridge has no {request_id}, and its answer for {control[0]} doesn't name that request"
    return "", f"the bridge has no {request_id}, though it returns our request {control[0]}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.submissions", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list", help="submissions still waiting for a person to check the bridge")
    sub.add_parser("check", help="settle what the bridge can answer by itself, from a request ID a row holds")
    resolver = sub.add_parser("resolve", help="settle one submission")
    resolver.add_argument("row", type=int, help="the submission number, from `list`")
    outcome = resolver.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--request-id", help="the bridge filed it, under this request ID")
    outcome.add_argument("--not-filed", action="store_true", help="the bridge never filed it")
    resolver.add_argument("--by", required=True, help="your name, for the log")
    args = parser.parse_args(argv)

    with db.connect() as conn:
        if args.action == "list":
            rows = open_rows(conn)
            if not rows:
                print("Nothing to settle: every submission was accepted, refused or never filed.")
            for row, at, status, lines, detail in rows:
                print(f"{row}  {at:%Y-%m-%d %H:%M}  {status:8}  {lines} lines  {detail or ''}".rstrip())
            return 0
        if args.action == "check":
            from vending.bridge import Bridge

            results = check(conn, Bridge, lock_timeout_ms=WAIT_NOTICE_MS)
            if not results:
                print("Nothing to settle: every submission was accepted, refused or never filed.")
            for c in results:
                if c.status in OPEN:
                    print(f"{c.row}  still {c.status}: {c.why}. {RESOLVE_HINT.format(row=c.row)}")
                else:
                    print(f"{c.row}  settled as {c.status}: {c.why}.")
            return 1 if any(c.status in OPEN for c in results) else 0
        try:
            try:
                done = resolve(conn, args.row, args.request_id, args.by, lock_timeout_ms=WAIT_NOTICE_MS)
            except psycopg.errors.LockNotAvailable:
                print("waiting for a submit in flight to finish...", flush=True)
                done = resolve(conn, args.row, args.request_id, args.by)
            print(done)
        except ValueError as error:
            print(error, file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
