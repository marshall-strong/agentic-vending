"""The record of every purchase request we tried to send, and how to settle one left open.

    python -m vending.submissions list
    python -m vending.submissions resolve 7 --request-id pr_abc123 --by kevin   # the bridge filed it
    python -m vending.submissions resolve 7 --not-filed --by kevin              # it never did

`submit_desired_state` commits a `sending` row here before it POSTs, and
settles it after. A row left `sending` (a crash in between) or `unknown` (sent,
but no usable answer) may or may not exist on the bridge, and the tool refuses
to submit again until a person settles it. Check the bridge first: its Swagger
page has `GET /api/v1/purchase-requests/{id}`, and the request would carry the
lines in the row's `body`. If you can't find it, ask the instructors before
`--not-filed`, because a resend of a request that was filed would order twice.

Resolving with a request ID also records the request in `purchase_requests`,
so the request sync reads it back from then on. `--by` is required: every
change is logged in `action_log` with your name. This never migrates, and the
halt switch doesn't block it, because it's a person correcting our records.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Jsonb

from vending import db
from vending.purchase_requests import record
from vending.write_gate import log

OPEN = ("sending", "unknown")
RESOLVE_HINT = "a person must check the bridge, then run: python -m vending.submissions resolve {row} ..."


def save_sending(connect: Callable[[], psycopg.Connection], plan_hash: str, body: dict,
                 directive: str | None, visit_fee_cents: int | None = None) -> int:
    """Commit a `sending` row on its own connection, so a crash after this can't lose it.

    `visit_fee_cents` is the fee the plan quoted for an off-cycle request (None
    otherwise). It is saved here, before the POST, so even a request settled
    later by `resolve` carries the fee that was agreed when it was sent.
    """
    with connect() as side:
        (row,) = side.execute(
            "insert into purchase_submissions (plan_hash, body, directive, visit_fee_cents)"
            " values (%s, %s, %s, %s) returning id",
            (plan_hash, Jsonb(body), directive, visit_fee_cents),
        ).fetchone()
    return row


def settle(connect: Callable[[], psycopg.Connection], row: int, status: str, *, request_id: str | None = None,
           response: object = None, detail: str | None = None, submitted_at: datetime | None = None) -> None:
    """Record how a submission ended, on its own connection.

    `accepted` also records the request in `purchase_requests`, in the same
    transaction, so the ID can't be stored in one place and not the other.
    """
    with connect() as side, side.transaction():
        body, plan_hash, fee = side.execute(
            "select body, plan_hash, visit_fee_cents from purchase_submissions where id = %s", (row,)).fetchone()
        side.execute(
            "update purchase_submissions set status = %s, request_id = %s, response = %s, detail = %s"
            " where id = %s",
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


def resolve(conn: psycopg.Connection, row: int, request_id: str | None, who: str) -> str:
    """Settle one open row as filed under `request_id`, or as never filed. Returns what was done."""
    if not who.strip():
        raise ValueError("--by needs your name")
    with conn.transaction():
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.submissions", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list", help="submissions still waiting for a person to check the bridge")
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
        try:
            print(resolve(conn, args.row, args.request_id, args.by))
        except ValueError as error:
            print(error, file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
