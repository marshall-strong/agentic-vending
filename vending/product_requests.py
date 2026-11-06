"""Settle a product request left `sending` or `unknown`, after checking the bridge.

    python -m vending.product_requests list
    python -m vending.product_requests resolve 7 --bridge-id np_abc123 --by kevin   # it was filed
    python -m vending.product_requests resolve 7 --not-filed --by kevin             # it wasn't

`request_catalog_product` leaves a row `sending` or `unknown` when it can't
tell whether the bridge filed the request, and refuses to send that product
again until a person settles it. Check the bridge first: its Swagger page
lists `GET /api/v1/product-requests/{id}`. If you can't find the request, ask
the instructors before using `--not-filed`, because a resend would file it twice.

`--by` is required: every change is logged in `action_log` with your name. This
never migrates, and the halt switch doesn't block it, because it's a person
correcting our records, not the agent acting.
"""

from __future__ import annotations

import argparse
import sys

import psycopg

from vending import db
from vending.write_gate import log

OPEN = ("sending", "unknown")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.product_requests", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list", help="rows still waiting for a person to check the bridge")
    resolve = sub.add_parser("resolve", help="settle one row")
    resolve.add_argument("row", type=int, help="our row number, from `list`")
    outcome = resolve.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--bridge-id", help="the bridge filed it, under this request ID")
    outcome.add_argument("--not-filed", action="store_true", help="the bridge never filed it")
    resolve.add_argument("--by", required=True, help="your name, for the log")
    args = parser.parse_args(argv)

    with db.connect() as conn:
        if args.action == "list":
            return show_open(conn)
        try:
            print(settle(conn, args.row, args.bridge_id, args.by.strip()))
        except ValueError as error:
            print(error, file=sys.stderr)
            return 1
    return 0


def show_open(conn: psycopg.Connection) -> int:
    rows = conn.execute(
        "select id, requested_at, name, size, status from product_requests where status = any(%s) order by id",
        (list(OPEN),),
    ).fetchall()
    if not rows:
        print("Nothing to settle: every request has a bridge ID or was never filed.")
    for row, at, name, size, status in rows:
        print(f"{row}  {at:%Y-%m-%d %H:%M}  {status:8}  {name} ({size})")
    return 0


def settle(conn: psycopg.Connection, row: int, bridge_id: str | None, who: str) -> str:
    """Record the bridge's ID for a row, or delete it if `bridge_id` is None. Returns what was done."""
    if not who:
        raise ValueError("--by needs your name")
    with conn.transaction():
        found = conn.execute("select name, size, status from product_requests where id = %s for update",
                             (row,)).fetchone()
        if found is None:
            raise ValueError(f"There is no product request {row}.")
        name, size, status = found
        if status not in OPEN:
            raise ValueError(f"Request {row} ({name}) is {status}, not sending or unknown; nothing to settle.")
        if bridge_id:
            try:
                conn.execute("update product_requests set bridge_id = %s, status = 'pending' where id = %s",
                             (bridge_id.strip(), row))
            except psycopg.errors.UniqueViolation:
                raise ValueError(f"Bridge ID {bridge_id} is already on another row; check the ID.") from None
            done = f"request {row} ({name}, {size}) recorded as filed under {bridge_id}, pending, by {who}"
        else:
            conn.execute("delete from product_requests where id = %s", (row,))
            done = f"request {row} ({name}, {size}) marked never filed and removed, by {who}"
        log(conn, "write", "product_requests.resolve",
            {"row": row, "bridge_id": bridge_id, "was": status}, "done", done)
    return done[0].upper() + done[1:]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
