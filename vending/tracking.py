"""Our books for the bridge's tracking check: four figures as of one poll, and the one send a day.

    python -m vending.tracking check                       # the figures as of the newest stored poll; sends nothing
    python -m vending.tracking check --as-of SESSION_ID    # as of that poll
    python -m vending.tracking check --send --by NAME      # send them once, store the answer, print it
    python -m vending.tracking show                        # the checks sent so far, newest first

The bridge's `POST /api/v1/tracking/check` (v0.4.0, docs/dex-bridge.md) takes our figures as of one
poll and says, for each, `within` or `outside`. It never says by how much or which way. The four:

- **cash**: the ledger's spendable balance, `vending.ledger.Balance.spendable_cents`. David, 20 Nov:
  cash is what we have to spend after the sales tax owed is taken out.
- **sales_tax_owed**: the ledger's `Balance.tax_owed_cents`, the tax set aside less what was remitted.
  How the tax is grouped into weeks is the ledger's business; this module never works tax out itself.
- **coil**: units in each of our slots (the server's assignment, `vending.slots.our_slots`), from
  `vending.stock`. Every one of our slots and only those, as the bridge requires.
- **back_stock**: units of each product in our closet, from `vending.stock`. A product with none is
  left out, which the bridge reads as 0.

All four are read as of the poll's `started_at`, inclusive: the ledger's balance at that moment and
the stock replayed to it. Nothing after the poll counts, the bridge's rule.

**No guesses.** If a figure can't be worked out, nothing is sent: no opening entry in the ledger, a
receipt with no `total_paid` (cash would leave it out), one of our slots with no counted receipt
(nothing says what is in it), or a slot whose count is a range because sales across a product change
couldn't be split (`stock.SlotCount.at_most`). The poll must be ours (`DEVICE_ID`) with a good
checksum, a counter for every one of our slots, at or after the ledger's opening.

**One check a day.** The bridge allows one check per team per Eastern day, and a request it refuses
before checking (bad body, unknown poll, books not reconciled yet) doesn't use it. David, 20 Nov: no
limit while testing; from the start of the quarter, one a day. So from `TRACKING_DAILY_FROM`
(default the competition start, 3 Dec 2026 00:00 Eastern), a second send on the same Eastern day is
refused here unless every earlier send that day was refused without using the check. A send with no
usable answer (a timeout, a 5xx) may have used it, so it blocks the rest of the day too. Every send
is stored in `tracking_checks` (migration 021) before it goes, then its answer.

**Whose day.** The bridge counts a check on the Eastern day it checks it, which can be the day after
we sent it (reserved 23:59:59, checked 00:00:01). So an answered check counts on the day of the
bridge's `checked_at` (and the day before its `next_check_from`), not the day we sent it. A send
still waiting, or with no usable answer, blocks every day from its send to its answer, or to
`IN_FLIGHT` after the send if no answer was ever stored (`days_used`).

The bridge's tolerance (David, 20 Nov): within $1 for money, within 2 units for counts.

Times are the database clock (`clock`), as in `vending.ledger.db_now`. The CLI never migrates.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from fractions import Fraction
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from vending import db, ledger, settings, stock
from vending.bridge import TRACKING_PATH, Bridge, BridgeAnswered, BridgeError, BridgeUnreachable
from vending.slots import DEVICE_ID, SlotsNotRecorded, our_slots
from vending.start import EASTERN

PATH = TRACKING_PATH

DAILY_SETTING = "TRACKING_DAILY_FROM"
# From the competition start (`vending.start.START`, `vending.slots.DEFAULT_LOCK_AT`), one check a day.
DEFAULT_DAILY_FROM = "2026-12-03T00:00:00-05:00"

# One send at a time across processes, so two can't both pass the once-a-day check.
TRACKING_LOCK = 4_200_021

# How long after its send a request with no stored answer may still reach the bridge. The client gives
# up after `vending.bridge.TIMEOUT_SECONDS`; an hour leaves room for a slow network. Until then an
# unanswered send blocks every Eastern day it touches; after it, a send left behind by a crash stops
# blocking the days that follow.
IN_FLIGHT = timedelta(hours=1)

RESULTS = ("cash", "sales_tax_owed", "coil", "back_stock")
VERDICTS = ("within", "outside")

# Statuses that mean the bridge refused before checking, so the day's check is still there.
REFUSED_BEFORE_CHECK = (400, 401, 403, 404, 409, 422)


class CantCompute(RuntimeError):
    """A figure can't be worked out from our records, so nothing is sent."""


class AlreadyChecked(RuntimeError):
    """Today's check is used, or may be, so a second send is refused."""


@dataclass(frozen=True)
class Figures:
    """Our four figures as of one poll, and what a person should know about them."""
    as_of: str                      # the poll's session id
    as_of_time: datetime            # its `started_at`
    cash_cents: Fraction            # spendable: the balance less the tax owed
    tax_owed_cents: Fraction
    coil: dict[str, int]            # every one of our slots
    back_stock: dict[str, int]      # products with units in the closet
    notes: tuple[str, ...] = ()

    def body(self) -> dict[str, Any]:
        """The request body, exactly as sent: money as dollar strings, rounded half up to the cent."""
        return {"as_of": self.as_of, "cash": ledger.money(self.cash_cents),
                "sales_tax_owed": ledger.money(self.tax_owed_cents),
                "coil": dict(self.coil), "back_stock": dict(self.back_stock)}


@dataclass(frozen=True)
class Outcome:
    """One send, as stored in `tracking_checks`."""
    id: int
    status: int | None              # None: no usable answer
    answer: Any                     # the bridge's JSON answer, or None
    error_type: str | None
    used_check: bool | None         # see migration 021
    results: dict[str, str] | None  # on a 200 of the documented shape
    next_check_from: datetime | None


# --- Time ---------------------------------------------------------------------------------------------

def clock(conn: psycopg.Connection) -> datetime:
    """Now by the database clock, in UTC (`vending.ledger.db_now`)."""
    return ledger.db_now(conn)


def daily_from() -> datetime:
    """From when one check a day applies: `TRACKING_DAILY_FROM`, else the competition start."""
    value = settings.get(DAILY_SETTING) or DEFAULT_DAILY_FROM
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        moment = None
    if moment is None or moment.utcoffset() is None:
        raise settings.SettingsError(
            f"{DAILY_SETTING} is {value!r}; give a time with its offset, like {DEFAULT_DAILY_FROM}")
    return moment


def eastern_day(moment: datetime) -> date:
    return moment.astimezone(EASTERN).date()


# --- The figures --------------------------------------------------------------------------------------

def newest_poll(conn: psycopg.Connection) -> str:
    """The newest stored poll of our machine with a good checksum."""
    row = conn.execute("select session_id from polls where device_id = %s and crc_ok"
                       " order by started_at desc, session_id desc limit 1", (DEVICE_ID,)).fetchone()
    if row is None:
        raise CantCompute(f"No good poll of {DEVICE_ID} is stored yet. Run the history sync first: "
                          "python -m vending.sync_history")
    return row[0]


def poll_time(conn: psycopg.Connection, session_id: str) -> datetime:
    """When the poll `session_id` was taken, or `CantCompute` unless it is a good poll of our machine."""
    row = conn.execute("select device_id, started_at, crc_ok from polls where session_id = %s",
                       (session_id,)).fetchone()
    if row is None:
        raise CantCompute(f"No poll {session_id} is stored. Run the history sync, or pick a stored poll.")
    device, started, good = row
    if device != DEVICE_ID:
        raise CantCompute(f"Poll {session_id} is of {device}, not our machine {DEVICE_ID}.")
    if not good:
        raise CantCompute(f"Poll {session_id} failed its checksum, so its counters aren't known. Pick another.")
    return started


def figures(conn: psycopg.Connection, as_of: str | None = None) -> Figures:
    """Our four figures as of the poll `as_of` (default the newest stored one), or `CantCompute`.

    Raises `SlotsNotRecorded` when our slots can't be read (`vending.slots.our_slots`)."""
    session = as_of or newest_poll(conn)
    at = poll_time(conn, session)
    slots = our_slots(conn)
    have = {row[0] for row in conn.execute("select selection from poll_selections where session_id = %s"
                                           " and selection = any(%s)", (session, list(slots)))}
    lacking = [slot for slot in slots if slot not in have]
    if lacking:
        # Without the counter, the sales up to this poll can't be seen: the coil reads high, the tax low.
        raise CantCompute(f"Poll {session} has no counter for our slots {', '.join(lacking)}, so the sales up "
                          "to it can't be counted. Pick another poll with --as-of. Nothing was sent.")
    b = ledger.balance(conn, at)
    if b is None:
        raise CantCompute(ledger.OPEN_HINT)
    if at < b.opening_at:
        raise CantCompute(f"Poll {session} was taken {at.isoformat()}, before the ledger's opening "
                          f"{b.opening_at.isoformat()}. Pick a later poll.")
    if b.receipts_missing:
        raise CantCompute(f"{b.receipts_missing} receipt lines have no total_paid, so cash can't be known. "
                          "Run the purchase-request sync, or record the amounts, then try again.")
    # Only polls up to this one: a later poll must not change the figures as of this one.
    counted = stock.stock(conn, until=at, polls_to_until=True)
    by_slot = {c.slot: c for c in counted.slots}
    unknown = [slot for slot in slots if slot not in by_slot]
    if unknown:
        raise CantCompute(f"No counted receipt has loaded slots {', '.join(unknown)}, so what is in them "
                          "isn't known. Nothing was sent.")
    ranged = [f"{slot} ({by_slot[slot].units} to {by_slot[slot].at_most})" for slot in slots
              if by_slot[slot].at_most is not None and by_slot[slot].at_most != by_slot[slot].units]
    if ranged:
        raise CantCompute("These slots' counts are a range, because sales across a product change couldn't "
                          f"be split: {', '.join(ranged)}. Nothing was sent.")
    back = {c.product: c.units for c in counted.closet if c.units}
    negative = [f"{sku} ({units})" for sku, units in back.items() if units < 0]
    if negative:
        raise CantCompute(f"The closet count is below zero for {', '.join(negative)}. Nothing was sent.")
    ours = set(slots)
    # Only warnings about our slots or the closet: the others may name a selection that isn't ours.
    warnings = tuple(str(w) for w in counted.warnings
                     if getattr(w, "slot", None) is None or getattr(w, "slot", None) in ours)
    return Figures(session, at, b.spendable_cents, b.tax_owed_cents,
                   {slot: by_slot[slot].units for slot in slots}, dict(sorted(back.items())),
                   tuple(b.notes) + warnings)


# --- The send -----------------------------------------------------------------------------------------

def days_used(sent_at: datetime, answered_at: datetime | None, status: int | None, used: bool | None,
              answer: Any, next_from: datetime | None) -> set[date]:
    """The Eastern days whose check a stored send used, or may have.

    The bridge counts a check on its own day, which can be the day after we sent it: reserved at
    23:59:59, checked at 00:00:01. So a check the bridge answered counts on the day of its
    `checked_at`, and the day before its `next_check_from` (midnight Eastern). Without those (a send
    with no usable answer, or one still waiting), it may have used any day from the send to the
    answer, or, still unanswered, to `IN_FLIGHT` after the send."""
    if used is False:
        return set()
    days: set[date] = set()
    if used is True and status in (200, 429):
        checked = _time(answer.get("checked_at")) if isinstance(answer, dict) and status == 200 else None
        if checked is not None:
            days.add(eastern_day(checked))
        if next_from is not None:
            days.add(eastern_day(next_from - timedelta(microseconds=1)))
        if days:
            return days
    end = answered_at if answered_at is not None else sent_at + IN_FLIGHT
    day, last = eastern_day(sent_at), eastern_day(max(end, sent_at))
    while day <= last:
        days.add(day)
        day += timedelta(days=1)
    return days


def blocking(conn: psycopg.Connection, day: date) -> list[tuple[int, int | None, bool | None]]:
    """(id, status, used_check) of each send that used `day`'s check, or may have (`days_used`)."""
    rows = conn.execute("select id, sent_at, answered_at, status, used_check, answer, next_check_from"
                        " from tracking_checks where used_check is distinct from false"
                        " and eastern_day between %s and %s order by id",
                        (day - timedelta(days=2), day)).fetchall()
    return [(i, status, used) for i, sent, answered, status, used, answer, next_from in rows
            if day in days_used(sent, answered, status, used, answer, next_from)]


def send(conn: psycopg.Connection, bridge: Any, found: Figures, by: str,
         now: datetime | Callable[[], datetime] | None = None) -> Outcome:
    """Send `found` once, never retried, storing the request first and the answer after.

    `now` is the time for both (default the database clock); a callable is asked for each.
    Raises `AlreadyChecked` from `daily_from()` on, when today's check is used or may be."""
    if not by or not by.strip():
        raise ValueError("a send needs a name, for the record")
    fixed = now
    stamp: Callable[[], datetime] = (now if callable(now) else (lambda: fixed) if fixed is not None
                                     else (lambda: clock(conn)))
    body = found.body()
    with conn.transaction():
        conn.execute("select pg_advisory_xact_lock(%s)", (TRACKING_LOCK,))
        moment = stamp()
        day = eastern_day(moment)
        if moment >= daily_from():
            earlier = blocking(conn, day)
            if earlier:
                said = "; ".join(f"#{i}: " + ("no usable answer, so it may have used it" if s is None
                                              else f"{s}" + ("" if used else ", may have used it"))
                                 for i, s, used in earlier)
                raise AlreadyChecked(f"Today's check ({day}, Eastern) is used or may be ({said}). "
                                     "The next one is from midnight Eastern.")
        row_id = conn.execute(
            "insert into tracking_checks (sent_at, eastern_day, sent_by, as_of, as_of_time, request)"
            " values (%s, %s, %s, %s, %s, %s) returning id",
            (moment, day, by.strip(), found.as_of, found.as_of_time, Jsonb(body))).fetchone()[0]
    status: int | None
    answer: Any
    try:
        answer, status = bridge.check_books(body), 200
    except BridgeAnswered as error:
        status = error.status
        try:
            answer = json.loads(error.detail)
        except (TypeError, ValueError):
            answer = {"unparsed": str(error.detail)[:300]}
    except BridgeUnreachable as error:     # certainly never arrived
        return _answered(conn, stamp, row_id, None, {"no_answer": str(error)[:300]}, "unreachable", False)
    except BridgeError as error:           # sent, but no usable answer: it may have run
        return _answered(conn, stamp, row_id, None, {"no_answer": str(error)[:300]}, "no_answer", None)
    error_type = answer.get("error_type") if isinstance(answer, dict) and status != 200 else None
    if status == 200:
        used: bool | None = True
    elif status == 429:
        used = True                        # `daily_check_used`: today's is gone
    elif status in REFUSED_BEFORE_CHECK:
        used = False
    else:
        used = None                        # a 5xx or a status the spec doesn't name
    return _answered(conn, stamp, row_id, status, answer, error_type, used)


def _answered(conn: psycopg.Connection, stamp: Callable[[], datetime], row_id: int, status: int | None,
              answer: Any, error_type: str | None, used: bool | None) -> Outcome:
    results = None
    if status == 200 and isinstance(answer, dict) and isinstance(answer.get("results"), dict) \
            and all(answer["results"].get(k) in VERDICTS for k in RESULTS):
        results = {k: answer["results"][k] for k in RESULTS}
    next_from = _time(answer.get("next_check_from")) if isinstance(answer, dict) else None
    conn.execute(
        "update tracking_checks set answered_at = %s, status = %s, answer = %s, error_type = %s, used_check = %s,"
        " results = %s, next_check_from = %s where id = %s",
        (stamp(), status, None if answer is None else Jsonb(answer), error_type, used,
         None if results is None else Jsonb(results), next_from, row_id))
    return Outcome(row_id, status, answer, error_type, used, results, next_from)


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.utcoffset() is not None else None


# --- The command line ---------------------------------------------------------------------------------

def _print_figures(f: Figures) -> None:
    print(f"Our figures as of poll {f.as_of}, taken {f.as_of_time.astimezone(EASTERN):%Y-%m-%d %H:%M %Z}:")
    print(f"  cash (spendable, after tax owed)  {ledger.money(f.cash_cents):>10}")
    print(f"  sales tax owed                    {ledger.money(f.tax_owed_cents):>10}")
    print(f"  coil, our {len(f.coil)} slots:  " + "  ".join(f"{s}={u}" for s, u in f.coil.items()))
    print("  back stock:  " + ("  ".join(f"{s}={u}" for s, u in f.back_stock.items()) or "(none)"))
    for note in f.notes:
        print(f"Note: {note}")
    print("Request body:")
    print(json.dumps(f.body(), indent=2))


def _print_outcome(o: Outcome) -> None:
    if o.status is None:
        print(f"Check #{o.id}: no usable answer ({o.error_type}). "
              + ("It never reached the bridge." if o.used_check is False
                 else "It may have used today's check."))
        return
    if o.results is not None:
        print(f"Check #{o.id}: " + ", ".join(f"{k} {v}" for k, v in o.results.items()))
    else:
        print(f"Check #{o.id}: {o.status} {o.error_type or ''}".rstrip())
        if isinstance(o.answer, dict):
            for key in ("error", "detail"):
                if o.answer.get(key):
                    print(f"  {key}: {o.answer[key]}")
            for item in o.answer.get("items") or []:
                print(f"  - {item}")
    if o.used_check is False:
        print("The bridge refused it before checking, so today's check is still there.")
    if o.next_check_from is not None:
        print(f"Next check from {o.next_check_from.astimezone(EASTERN):%Y-%m-%d %H:%M %Z}.")


def _show(conn: psycopg.Connection) -> int:
    rows = conn.execute("select id, sent_at, sent_by, as_of, status, error_type, used_check, results"
                        " from tracking_checks order by id desc limit 50").fetchall()
    if not rows:
        print("No checks sent yet.")
    for i, sent, by, as_of, status, error_type, used, results in rows:
        said = ", ".join(f"{k} {v}" for k, v in results.items()) if results else (error_type or "no answer")
        print(f"#{i}  {sent.astimezone(EASTERN):%Y-%m-%d %H:%M}  by {by}  as of {as_of}  "
              f"{status if status is not None else '-'}  {said}  used: {used}")
    return 0


def main(argv: list[str] | None = None, bridge: Callable[[], Any] = Bridge) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.tracking", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="action", required=True)
    check = sub.add_parser("check", help="work out our figures as of a poll; send them only with --send")
    check.add_argument("--as-of", help="the poll's session id (default: the newest stored poll)")
    check.add_argument("--send", action="store_true", help="send them to the bridge, once")
    check.add_argument("--by", help="your name, for the record (needed with --send)")
    sub.add_parser("show", help="the checks sent so far, newest first")
    args = parser.parse_args(argv)
    if args.action == "check" and args.send and not (args.by or "").strip():
        parser.error("--send needs --by NAME")

    with db.connect() as conn:
        behind = db.pending(conn)
        if behind:
            print(f"The database is missing migrations: {', '.join(behind)}. "
                  "Check DATABASE_URL points where you mean, then run: python -m vending.db migrate",
                  file=sys.stderr)
            return 2
        if args.action == "show":
            return _show(conn)
        try:
            found = figures(conn, args.as_of)
        except (CantCompute, SlotsNotRecorded) as error:
            print(error, file=sys.stderr)
            return 1
        _print_figures(found)
        if not args.send:
            print("Not sent. Add --send --by NAME to send it (one check a day from "
                  f"{daily_from().astimezone(EASTERN):%Y-%m-%d %H:%M %Z}).")
            return 0
        try:
            outcome = send(conn, bridge(), found, args.by)
        except (AlreadyChecked, settings.SettingsError) as error:
            print(error, file=sys.stderr)
            return 1
        _print_outcome(outcome)
        if outcome.status is None:
            return 3
        return 0 if outcome.status == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
