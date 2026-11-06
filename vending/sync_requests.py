"""Read our purchase requests back from the bridge and store what the worker did.

    python -m vending.sync_requests                     # pending, plus fulfilled in the last 30 days
    python -m vending.sync_requests --recheck-days 90   # look further back for later losses

The bridge can't list our requests, so the IDs come from `purchase_requests`,
where `submit_desired_state` records each one. Every pending request is read
until it is rejected or fulfilled. Fulfilled ones are read again for a while
after, because units found broken or missing later are added to a line's
`later_losses` and the stock count has to see them. Each read replaces the
request's stored receipts, so reading again never counts a loss twice.

A request the bridge can't return, or answers with something we can't use, is
logged and counted and the run carries on with the rest. A run that is still
rate limited after the bridge client's retries stops there and leaves the rest
for next time, since every further call would only add to the load. An
unreachable bridge or a rejected key stops the run, as in `sync_history`.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg

from vending import db
from vending.bridge import Bridge, BridgeError, BridgeRateLimited, BridgeUnreachable
from vending.purchase_requests import store

# Long enough to cover a closet count after the next weekly visit or two.
# Units can sit in the closet for weeks, so raise it if losses turn up late.
RECHECK_DAYS = 30


@dataclass
class Report:
    checked: int = 0
    pending: int = 0
    rejected: int = 0
    fulfilled: int = 0
    new_losses: int = 0
    failed: int = 0
    skipped: int = 0


def due(conn: psycopg.Connection, now: datetime, recheck_days: int) -> list[str]:
    """Requests worth reading now: every pending one, and those fulfilled recently. Oldest first."""
    return [row[0] for row in conn.execute(
        """
        select request_id from purchase_requests
         where status = 'pending'
            or (status = 'fulfilled' and fulfilled_at >= %s)
         order by submitted_at, request_id
        """,
        (now - timedelta(days=recheck_days),),
    )]


def sync(conn: psycopg.Connection, bridge: Bridge, now: datetime | None = None,
         recheck_days: int = RECHECK_DAYS, log=print) -> Report:
    now = now or datetime.now(timezone.utc)
    report = Report()
    queue = due(conn, now, recheck_days)
    for index, request_id in enumerate(queue):
        try:
            response = bridge.purchase_request(request_id)
        except BridgeUnreachable:
            raise
        except BridgeRateLimited as error:
            report.skipped = len(queue) - index
            log(f"{request_id}: {error}; stopping, {report.skipped} left for the next run")
            break
        except BridgeError as error:
            report.failed += 1
            log(f"{request_id}: could not read ({error}); skipped")
            continue
        try:
            stored = store(conn, request_id, response, now)
        except ValueError as error:
            report.failed += 1
            log(f"{request_id}: {error}; kept what we had")
            continue
        report.checked += 1
        setattr(report, stored.status, getattr(report, stored.status) + 1)
        report.new_losses += stored.new_losses
        if stored.new_losses:
            log(f"{request_id}: {stored.new_losses} new later losses")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--recheck-days", type=int, default=RECHECK_DAYS,
                        help=f"re-read requests fulfilled in this many days (default {RECHECK_DAYS})")
    args = parser.parse_args(argv)
    try:
        with db.connect() as conn:
            db.migrate(conn)
            report = sync(conn, Bridge(), recheck_days=args.recheck_days)
    except BridgeError as error:
        print(f"bridge error: {error}", file=sys.stderr)
        return 3
    print(f"done: {report.checked} read, {report.pending} still pending, {report.rejected} rejected, "
          f"{report.fulfilled} fulfilled, {report.new_losses} new later losses, "
          f"{report.failed} failed, {report.skipped} left for the next run")
    return 1 if report.failed or report.skipped else 0


if __name__ == "__main__":
    sys.exit(main())
