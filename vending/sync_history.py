"""Copy new polls from the bridge's stored history into our database.

Replaces the laptop poller: the bridge already stores every poll from anyone,
plus David's hourly one, and reading history never touches the machine.

    python -m vending.sync_history                  # from the last synced day to today
    python -m vending.sync_history --since 2026-08-28   # backfill

Safe to run as often as you like: each day is listed, and only sessions we
don't have are fetched. The last synced day and the day before it are always
re-checked, because polls can land on them after the previous run.

A day the bridge can't list, or a poll it can't return, is logged and counted
and the run carries on, so one bad poll can't block everything after it. That
includes a poll whose connection keeps dropping, as long as `/health` still
answers. Re-run with `--since` to retry them; that pass also fetches again any
poll stored as unparseable, in case the bridge sent a cut-off document. Only an
unreachable bridge or a rejected key stops the run.

Each poll carries all 40 selections, but we store counters only for our 20
(`vending.slots.SLOTS`) and only from our machine (`DEVICE_ID`). The poll row and
its raw document are kept whole. This is the team integrity rule: we don't base
decisions on the other team's slots. See docs/integrity.md.

It never migrates. On a database that is behind it refuses to run (exit 2) and
names `python -m vending.db migrate`, like the server and the halt switch, so a
sync started from a branch can't change the shared database's schema.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import psycopg

from vending import db
from vending.bridge import Bridge, BridgeError, BridgeUnreachable
from vending.evadts import ParseError, parse
from vending.slots import DEVICE_ID, SLOTS

# The bridge's history starts here.
HISTORY_START = date(2026, 8, 28)


@dataclass
class Report:
    days: int = 0
    listed: int = 0
    new: int = 0
    crc_failed: int = 0
    unparseable: int = 0
    failed_days: int = 0
    failed_polls: int = 0


def store(conn: psycopg.Connection, meta: dict, raw: bytes) -> str:
    """Insert one poll and its selections in one transaction. Returns 'ok', 'crc' or 'bad'.

    A poll already stored is left alone, except one stored as unparseable
    (`crc_ok` null, so no selections): a document that can now be read replaces it.

    Selections are stored only for our slots, and only for polls of our machine
    (docs/integrity.md).
    """
    try:
        poll = parse(raw)
    except ParseError:
        poll = None
    ok = poll is not None and poll.crc_ok
    with conn.transaction():
        inserted = conn.execute(
            """
            insert into polls (session_id, device_id, started_at, raw, byte_count, crc_ok,
                               machine_serial, decimal_places, total_paid_cents, total_paid_units)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (session_id) do update set
                device_id = excluded.device_id, started_at = excluded.started_at,
                raw = excluded.raw, byte_count = excluded.byte_count, crc_ok = excluded.crc_ok,
                machine_serial = excluded.machine_serial, decimal_places = excluded.decimal_places,
                total_paid_cents = excluded.total_paid_cents, total_paid_units = excluded.total_paid_units,
                synced_at = now()
            where polls.crc_ok is null and excluded.crc_ok is not null
            """,
            (
                # `crc_ok` stays null when the document couldn't be read at all.
                meta["session_id"], meta.get("device_id") or "unknown", meta["started_at"], raw, len(raw),
                None if poll is None else poll.crc_ok,
                poll.machine_serial if ok else None, poll.decimal_places if ok else None,
                poll.total_paid_cents if ok else None, poll.total_paid_units if ok else None,
            ),
        ).rowcount
        ours = ([s for s in poll.selections if s.selection in SLOTS]
                if ok and meta.get("device_id") == DEVICE_ID else [])
        if inserted and ours:
            with conn.cursor() as cur:
                cur.executemany(
                    "insert into poll_selections (session_id, selection, price_cents,"
                    " lifetime_units, lifetime_sales_cents) values (%s, %s, %s, %s, %s)",
                    [(meta["session_id"], s.selection, s.price_cents, s.lifetime_units,
                      s.lifetime_sales_cents) for s in ours],
                )
    if poll is None:
        return "bad"
    return "ok" if ok else "crc"


def sync(conn: psycopg.Connection, bridge: Bridge, since: date, until: date, log=print,
         retry_bad: bool = False) -> Report:
    """Fetch and store every poll from `since` to `until` that we don't hold.

    With `retry_bad`, polls stored as unparseable (`crc_ok` null) are fetched
    again, in case the bridge returned a cut-off or garbled document last time.
    A poll whose checksum failed (`crc_ok` false) was read whole and is left alone.
    """
    report = Report()
    start = datetime.combine(since, datetime.min.time(), timezone.utc)
    known = {row[0] for row in conn.execute(
        "select session_id from polls where started_at >= %s" + (" and crc_ok is not null" if retry_bad else ""),
        (start,))}
    retrying = set() if not retry_bad else {row[0] for row in conn.execute(
        "select session_id from polls where started_at >= %s and crc_ok is null", (start,))}
    day = since
    while day <= until:
        report.days += 1
        try:
            listing = bridge.history(day.isoformat())
        except BridgeUnreachable:
            raise
        except BridgeError as error:
            report.failed_days += 1
            log(f"{day}: could not list ({error}); skipped")
            day += timedelta(days=1)
            continue
        report.listed += len(listing)
        for meta in listing:
            if meta["session_id"] in known:
                continue
            try:
                raw = bridge.raw(meta["session_id"])
            except BridgeUnreachable:
                raise
            except BridgeError as error:
                report.failed_polls += 1
                log(f"  could not fetch {meta['session_id']} ({meta['started_at']}): {error}; skipped")
                continue
            outcome = store(conn, meta, raw)
            known.add(meta["session_id"])
            again = meta["session_id"] in retrying
            if outcome == "bad":
                report.unparseable += 1
                log(f"  {'still ' if again else ''}not EVA-DTS: {meta['session_id']} ({meta['started_at']}); kept raw")
                continue
            report.new += 1
            if again:
                log(f"  re-fetched {meta['session_id']} ({meta['started_at']}): now readable")
            if outcome == "crc":
                report.crc_failed += 1
                log(f"  checksum failed: {meta['session_id']} ({meta['started_at']}); kept raw, counters skipped")
        log(f"{day}: {len(listing)} listed")
        day += timedelta(days=1)
    return report


def default_since(conn: psycopg.Connection) -> date:
    last = conn.execute("select max(started_at) from polls").fetchone()[0]
    if not last:
        return HISTORY_START
    # One day back: a poll from late on day D can be stored after we've already
    # synced an early poll from D+1.
    return max(last.astimezone(timezone.utc).date() - timedelta(days=1), HISTORY_START)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", type=date.fromisoformat,
                        help="first UTC day to sync (default: the day before the last day already synced)")
    parser.add_argument("--until", type=date.fromisoformat,
                        help="last UTC day to sync (default: today, UTC)")
    args = parser.parse_args(argv)
    try:
        with db.connect() as conn:
            # Never migrates: in production DATABASE_URL is the shared Azure
            # database, and a schema change there should be a person's decision.
            behind = db.pending(conn)
            if behind:
                print(f"The database is missing migrations: {', '.join(behind)}.\n"
                      "Check DATABASE_URL points where you mean, then run: python -m vending.db migrate",
                      file=sys.stderr)
                return 2
            since = args.since or default_since(conn)
            until = args.until or datetime.now(timezone.utc).date()
            # An explicit `--since` is a retry pass, so unparseable polls are fetched again.
            report = sync(conn, Bridge(), since, until, retry_bad=args.since is not None)
    except BridgeError as error:
        print(f"bridge error: {error}", file=sys.stderr)
        return 3
    print(f"done: {report.days} days, {report.listed} polls listed, {report.new} new, "
          f"{report.crc_failed} failed the checksum, {report.unparseable} unparseable, "
          f"{report.failed_days} days and {report.failed_polls} polls the bridge couldn't return")
    problems = report.crc_failed + report.unparseable + report.failed_days + report.failed_polls
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
