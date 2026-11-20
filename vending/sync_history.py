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

Each row of a day's listing is checked on its own, before anything is fetched
(`vending.bridge.listed_poll`): it needs a session id of 32 hex digits and a start
that is a real time with an offset. A row that fails is counted as a failed poll,
skipped, and never logged. Storing a poll is just as separate: one the database
refuses (a NUL byte in its serial, say) is counted and logged as the error's class
and SQLSTATE, and the polls after it are still stored.

A run that couldn't store a poll ends its summary with the earliest day and the `--since <day>` that
fetches it again: the default window follows the newest poll stored, so it may not go back that far. A
connection that is gone raises `StorageLost`, with a message of our own.

The log says what happened in words of our own (a status, a size, a session id
that passes `valid_session_id`) and never quotes what the bridge sent or the
database's error text: the log is read by people, and a poll can hold selections
that aren't ours (`docs/integrity.md`).

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
from vending.bridge import Bridge, BridgeError, BridgeUnreachable, listed_poll
from vending.evadts import ParseError, parse

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
    failed_polls: int = 0           # skipped: not listed usably, not fetched, or not stored
    storage_failed: int = 0         # of those, the ones the database refused
    first_storage_failure: date | None = None   # the earliest day (UTC, as listed) one was refused on


class StorageLost(RuntimeError):
    """The database connection is gone, so no poll after this one can be stored either. The message is ours:
    the database's own text can quote the row it refused, which holds a poll."""


def _device(meta: dict) -> str:
    device = meta.get("device_id")
    return device if isinstance(device, str) and device else "unknown"


def store(conn: psycopg.Connection, meta: dict, raw: bytes) -> str:
    """Insert one poll and its selections in one transaction. Returns 'ok', 'crc' or 'bad'.

    A poll already stored is left alone, except one stored as unparseable
    (`crc_ok` null, so no selections): a document that can now be read replaces it.
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
                # `crc_ok` stays null when the document couldn't be read at all. The listing's device
                # is the bridge's word: only text is a device.
                meta["session_id"], _device(meta), meta["started_at"], raw, len(raw),
                None if poll is None else poll.crc_ok,
                poll.machine_serial if ok else None, poll.decimal_places if ok else None,
                poll.total_paid_cents if ok else None, poll.total_paid_units if ok else None,
            ),
        ).rowcount
        if inserted and ok:
            with conn.cursor() as cur:
                cur.executemany(
                    "insert into poll_selections (session_id, selection, price_cents,"
                    " lifetime_units, lifetime_sales_cents) values (%s, %s, %s, %s, %s)",
                    [(meta["session_id"], s.selection, s.price_cents, s.lifetime_units,
                      s.lifetime_sales_cents) for s in poll.selections],
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
        for row in listing:
            listed = listed_poll(row)
            if listed is None:
                # The listing is the bridge's words, and the log is read by people: a row that has no usable
                # session id or start is neither logged, fetched nor stored, and the rows after it carry on.
                report.failed_polls += 1
                log("  skipped a listed poll whose session id or start time isn't usable")
                continue
            session, when = listed.session_id, listed.started_at.isoformat()
            if session in known:
                continue
            try:
                raw = bridge.raw(session)
            except BridgeUnreachable:
                raise
            except BridgeError as error:
                report.failed_polls += 1
                log(f"  could not fetch {session} ({when}): {error}; skipped")
                continue
            try:
                outcome = store(conn, {**row, "started_at": listed.started_at}, raw)
            except psycopg.Error as error:
                # The database's text can quote the row it refused, which holds the document: the class and
                # the SQLSTATE say enough, here and in the error raised below.
                why = f"{type(error).__name__}, SQLSTATE {error.sqlstate or 'none'}"
                if conn.closed or conn.broken:      # nothing after this can be stored either
                    raise StorageLost(f"the database connection was lost while storing {session}: {why}") from None
                # One poll the database refuses mustn't stop the rest.
                report.failed_polls += 1
                report.storage_failed += 1
                report.first_storage_failure = report.first_storage_failure or day
                log(f"  could not store {session} ({when}): {why}; skipped")
                continue
            known.add(session)
            again = session in retrying
            if outcome == "bad":
                report.unparseable += 1
                log(f"  {'still ' if again else ''}not EVA-DTS: {session} ({when}); kept raw")
                continue
            report.new += 1
            if again:
                log(f"  re-fetched {session} ({when}): now readable")
            if outcome == "crc":
                report.crc_failed += 1
                log(f"  checksum failed: {session} ({when}); kept raw, counters skipped")
        log(f"{day}: {len(listing)} listed")
        day += timedelta(days=1)
    return report


def default_since(conn: psycopg.Connection) -> date:
    last = conn.execute("select max(started_at) from polls").fetchone()[0]
    if not last:
        return HISTORY_START
    # One day back: a poll from late on day D can be stored after we've already
    # synced an early poll from D+1.
    #
    # This window moves with the newest stored poll, so a poll the database refused on an earlier day is not
    # fetched again once later polls are stored. Nothing records it (there is no row to count from), so the
    # run says so and names `--since <day>` (`main`), and it exits 1 until someone reruns that.
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
          f"{report.failed_days} days couldn't be listed, {report.failed_polls} polls skipped")
    if report.storage_failed:
        day = report.first_storage_failure
        print(f"storage failed for {report.storage_failed} of the polls skipped, the first on {day}. Fetch them again "
              f"with: python -m vending.sync_history --since {day}\n"
              "(a default run starts a day before the newest poll stored, so it may not go back that far)")
    problems = report.crc_failed + report.unparseable + report.failed_days + report.failed_polls
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
