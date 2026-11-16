"""Read our catalog requests back from the bridge and store each decision.

    python -m vending.sync_product_requests

`request_catalog_product` stores a request as `pending` once the bridge accepts
it, and until now nothing updated it. This reads every pending one with
`GET /api/v1/product-requests/{id}` and stores what the instructors decided:
`approved` with the SKU it was added under (which may differ from what we
asked for, so order it by that SKU), or `rejected` with their note. The
bridge's whole answer goes in `last_response`. A decided request is not read
again: the bridge doesn't change a decision.

Knowing which requests are decided tells us how many are still open, so the
backlog can file the next ones before the bridge's limit on open requests
(about 24 of ours, seen 15 Nov) refuses them.

A request the bridge says doesn't exist (a 404, say from a mistyped ID given to
`product_requests resolve`) is moved to `unknown`, so it isn't read every hour
and a person settles it with `python -m vending.product_requests resolve`, as
for a request whose answer was lost. An answer we can't use is logged and
counted, kept as it was, and the run carries on with the rest. A run that is
still rate limited after the bridge client's retries stops there and leaves the
rest for next time. An unreachable bridge or a rejected key stops the run, as in
`sync_requests`. Rows left `sending` or `unknown` are a person's job
(`python -m vending.product_requests`), so this never reads them.

It never migrates. On a database that is behind it refuses to run (exit 2) and
names `python -m vending.db migrate`, like the other syncs.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

import psycopg
from psycopg.types.json import Jsonb

from vending import db
from vending.bridge import Bridge, BridgeError, BridgeRateLimited, BridgeUnreachable

STATUSES = ("pending", "approved", "rejected")


@dataclass
class Report:
    checked: int = 0
    pending: int = 0
    approved: int = 0
    rejected: int = 0
    not_found: int = 0
    failed: int = 0
    skipped: int = 0


def due(conn: psycopg.Connection) -> list[tuple[int, str]]:
    """(row, bridge id) of every request the bridge accepted and hasn't decided yet, oldest first."""
    return [(row, bridge_id) for row, bridge_id in conn.execute(
        "select id, bridge_id from product_requests where status = 'pending' and bridge_id is not null"
        " order by requested_at, id")]


def decision(answer: object, bridge_id: str | None = None) -> tuple[str, str | None, str | None]:
    """(status, sku, decision_note) from the bridge's `ProductRequestView`, or ValueError.

    `approved` normally carries `product.sku`; the spec allows `product: null`, which is
    stored as approved with no SKU (the catalog sync will list the product)."""
    if not isinstance(answer, dict):
        raise ValueError("the answer isn't a JSON object")
    if bridge_id is not None and answer.get("id") not in (None, bridge_id):
        raise ValueError(f"the answer is for request {answer.get('id')!r}, not {bridge_id!r}")
    status = answer.get("status")
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    note = answer.get("decision_note")
    if note is not None and not isinstance(note, str):
        raise ValueError("decision_note isn't text")
    sku = None
    if status == "approved" and answer.get("product") is not None:
        product = answer["product"]
        sku = product.get("sku") if isinstance(product, dict) else None
        if not isinstance(sku, str) or not sku.strip():
            raise ValueError("approved with a product that has no usable SKU")
        sku = sku.strip()
    return status, sku, note


def store(conn: psycopg.Connection, row: int, bridge_id: str, answer: dict) -> tuple[str, str | None] | None:
    """Save one answer on our row: (status, sku), or None if the row was no longer pending."""
    status, sku, note = decision(answer, bridge_id)
    with conn.transaction():
        changed = conn.execute("update product_requests set status = %s, sku = %s, decision_note = %s,"
                               " last_response = %s where id = %s and status = 'pending'",
                               (status, sku, note, Jsonb(answer), row)).rowcount
    return (status, sku) if changed else None


def not_found(conn: psycopg.Connection, row: int, detail: str) -> bool:
    """A pending row the bridge says doesn't exist goes to `unknown`, for a person to settle."""
    with conn.transaction():
        return bool(conn.execute(
            "update product_requests set status = 'unknown', last_response = %s where id = %s and status = 'pending'",
            (Jsonb({"sync_error": detail}), row)).rowcount)


def sync(conn: psycopg.Connection, bridge: Bridge, log=print) -> Report:
    report = Report()
    queue = due(conn)
    for index, (row, bridge_id) in enumerate(queue):
        try:
            answer = bridge.product_request(bridge_id)
        except BridgeUnreachable:
            raise
        except BridgeRateLimited as error:
            report.skipped = len(queue) - index
            log(f"{bridge_id}: {error}; stopping, {report.skipped} left for the next run")
            break
        except BridgeError as error:
            if str(error).startswith("404 "):
                if not_found(conn, row, str(error)):
                    report.not_found += 1
                    log(f"{bridge_id}: the bridge has no such request; moved to unknown. Check the ID, then run "
                        f"python -m vending.product_requests resolve {row}")
                continue
            report.failed += 1
            log(f"{bridge_id}: could not read ({error}); skipped")
            continue
        try:
            stored = store(conn, row, bridge_id, answer)
        except ValueError as error:
            report.failed += 1
            log(f"{bridge_id}: {error}; kept what we had")
            continue
        if stored is None:          # settled by someone else meanwhile
            continue
        status, sku = stored
        report.checked += 1
        setattr(report, status, getattr(report, status) + 1)
        if status != "pending":
            log(f"{bridge_id}: {status}" + (f" as {sku}" if sku else "")
                + (f" ({answer['decision_note']})" if answer.get("decision_note") else ""))
    return report


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args(argv)
    try:
        with db.connect() as conn:
            # Never migrates: in production DATABASE_URL is the shared Azure database.
            behind = db.pending(conn)
            if behind:
                print(f"The database is missing migrations: {', '.join(behind)}. "
                      "Check DATABASE_URL points where you mean, then run: python -m vending.db migrate",
                      file=sys.stderr)
                return 2
            report = sync(conn, Bridge())
            (still_open,) = conn.execute("select count(*) from product_requests"
                                         " where status in ('pending', 'sending', 'unknown')").fetchone()
    except BridgeError as error:
        print(f"bridge error: {error}", file=sys.stderr)
        return 3
    print(f"done: {report.checked} read, {report.approved} approved, {report.rejected} rejected, "
          f"{report.pending} still pending, {report.not_found} not found (moved to unknown), "
          f"{report.failed} failed, {report.skipped} left for the next run; {still_open} of ours still open")
    return 1 if report.failed or report.skipped else 0


if __name__ == "__main__":
    sys.exit(main())
