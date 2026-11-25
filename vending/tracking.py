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
checksum, a counter for every one of our slots, at or after the ledger's opening, and no other good
poll of ours may share its `started_at` (the bridge gives no order between two polls at one moment).

**One check a day.** The bridge allows one check per team per Eastern day, and a request it refuses
before checking (bad body, unknown poll, books not reconciled yet) doesn't use it. David, 20 Nov: no
limit while testing; from the start of the quarter, one a day. So from `TRACKING_DAILY_FROM`
(default the competition start, 3 Dec 2026 00:00 Eastern), a second send on the same Eastern day is
refused here unless every earlier send that day was refused without using the check. A send with no
usable answer (a timeout, a 5xx) may have used it, so it blocks the rest of the day too. Every send
is stored in `tracking_checks` (migration 021) before it goes, then its answer.

**One request a night.** The request itself is never started later than 21:00 Eastern
(`DISPATCH_DEADLINE`, raising `DispatchClosed`), checked under the reservation's lock and again just
before the request (past it then, the reservation is marked `not_sent` and unused): started by then
it is given up by the client within seconds - the timeout in `vending.bridge` - over three hours before midnight, so the bridge cannot
check it on the next Eastern day, and every stored send belongs to at most the Eastern days up to
midnight. The daily job's window ends at the same hour (`tracking_daily.LAST_SEND`). A request from
the previous day that is still unanswered - the process froze with the request on the wire - may
still reach the bridge after midnight and be checked on the new day, so while it has no answer it
blocks the new day's check until 06:00 Eastern (`MORNING_FROM`, `blocking`): the morning window
opens then, and by it the request is hours past its start. The held-past-midnight case is also why
`days_used` keeps a lost reply blocking the day after its send.

**Whose day.** The bridge counts a check on the Eastern day it checks it, which can be the day after
we sent it (reserved 23:59:59, checked 00:00:01). So an answered check counts on the day of the
bridge's `checked_at` (and the day before its `next_check_from`), not the day we sent it. A send
still waiting, or with no usable answer (a timeout, a dropped connection, a 5xx, a malformed 200),
blocks every day touched from its send to `IN_FLIGHT` after it, even once that outcome is stored: a
connection reset at 23:59:55 doesn't mean the bridge didn't check it at 00:00:01 (`days_used`). Only
a valid answer or a refusal the spec says didn't use the check ends that.

**What the terminal shows.** Only our own words: the four verdicts when the 200 has the spec's shape
(every field present, about the poll we sent, each verdict `within` or `outside`), else a message
picked by the `error_type` codes the spec names (any other code prints as "unknown error type") or by
the status. The bridge's `error`, `detail` and `items` are stored with the check, never printed. A 200
of another shape isn't a success: it is stored as `malformed_answer` with its status, may have used the
day's check, and the command exits 3. So is a 200 that isn't JSON (its start stored as `unparsed`) or
whose JSON holds a NUL. A jsonb column can't hold a NUL, so every stored answer has it replaced by
U+FFFD (`_db_safe`). If recording still fails, only the error's class is printed, never its message.

The bridge's tolerance (David, 20 Nov): within $1 for money, within 2 units for counts.

Times are the database clock (`clock`), as in `vending.ledger.db_now`. The CLI never migrates.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from fractions import Fraction
from typing import Any, NamedTuple

import psycopg
from psycopg.types.json import Jsonb

from vending import db, ledger, settings, stock
from vending.bridge import TRACKING_PATH, Bridge, BridgeAnswered, BridgeError, BridgeNotJson, BridgeUnreachable
from vending.slots import DEVICE_ID, SlotsNotRecorded, our_slots
from vending.start import EASTERN

PATH = TRACKING_PATH

DAILY_SETTING = "TRACKING_DAILY_FROM"
# From the competition start (`vending.start.START`, `vending.slots.DEFAULT_LOCK_AT`), one check a day.
DEFAULT_DAILY_FROM = "2026-12-03T00:00:00-05:00"

# One send at a time across processes, so two can't both pass the once-a-day check.
TRACKING_LOCK = 4_200_021

# How long after its send a request with no usable answer may still reach the bridge. The client gives
# up after `vending.bridge.TIMEOUT_SECONDS`; an hour leaves room for a slow network. Until then such a
# send blocks every Eastern day it touches, even after its timeout is stored; after it, a send left
# behind by a crash stops blocking the days that follow.
IN_FLIGHT = timedelta(hours=1)

# The request is never started later than this, Eastern. A request started by 21:00 is given up by
# the client within seconds (the 20 second timeout in `vending.bridge`), over three hours before
# midnight, so the bridge cannot check it on the next Eastern day. Past the deadline a send is
# refused (`DispatchClosed`) and nothing is recorded: the reservation is left unused. This is also
# the end of the daily job's window (`tracking_daily.LAST_SEND`).
DISPATCH_DEADLINE = time(21, 0)

# The morning hour the daily send window opens again, Eastern. A reservation from the previous day
# with no answer yet may still reach the bridge after midnight - the request sat on a stalled reader
# - and be checked then, which would spend the new day's check before our first send of the morning.
# So while it has no answer it blocks the new day's check until this hour (`blocking`); after it, a
# request is hours past its start and the bridge no longer reads one.
MORNING_FROM = time(6, 0)

RESULTS = ("cash", "sales_tax_owed", "coil", "back_stock")
VERDICTS = ("within", "outside")


def verdicts(results: Any) -> dict[str, str]:
    """The stored `results` of a check, allowlisted: only the four documented keys (`RESULTS`), each
    only `within` or `outside` (`VERDICTS`). The stored JSON is the bridge's, so the public JSON and
    the failure banner read the check through this one validator (`dashboard._tracking_alert`), and
    the incident log's query (`report_card._tracking`) applies the same allowlist in SQL: an extra
    key, another value or an array — anything undocumented — is dropped, and a non-object gives {}."""
    if not isinstance(results, dict):
        return {}
    return {k: results[k] for k in RESULTS if results.get(k) in VERDICTS}

# Statuses that mean the bridge refused before checking, so the day's check is still there.
REFUSED_BEFORE_CHECK = (400, 401, 403, 404, 409, 422)

# Refusals that settle on their own (the poll isn't folded in yet, or the books haven't begun), so
# the worker retries them on a backoff instead of spending its tries (`tracking_daily.tries_today`).
TRANSIENT_REFUSALS = ("not_reconciled_yet", "tracking_not_started")

# Our `error_type` for a reservation that was never sent: the window, the deadline or the day had
# passed by the moment just before the request (`send`). Stored with `used_check` false; no try.
NOT_SENT = "not_sent"

# A 200's fields, all required by the spec (`TrackingCheckResult`).
ANSWER_FIELDS = ("as_of", "as_of_time", "checked_at", "results", "next_check_from")

# What we print for each `error_type` the spec names (v0.4.0), and for our own words for a send with no
# usable answer. The bridge's own text (`error`, `detail`, `items`) is stored with the check, never
# printed: the terminal shows only these.
ERROR_TYPES = {
    "unknown_poll": "the bridge has no such poll",
    "not_reconciled_yet": "that poll isn't in the bridge's books yet; polls are folded in on the hour",
    "tracking_not_started": "the project's books haven't begun",
    "invalid_coil": "the coil wasn't every slot we own and only those",
    "unknown_sku": "back stock named a product that isn't in the catalog",
    "before_tracking_start": "that poll was taken before the books began",
    "daily_check_used": "today's check was already used",
    # Ours, not the bridge's.
    "unreachable": "it never reached the bridge",
    "no_answer": "sent, but no usable answer came back (a timeout or a dropped connection)",
    "malformed_answer": "the bridge answered, but not the shape the spec gives, so the verdicts can't be read",
    NOT_SENT: "never sent: the send window or the dispatch deadline passed while it was being prepared",
}
UNKNOWN_ERROR_TYPE = "unknown error type"

# What we print for a status with no `error_type` we know.
STATUSES = {
    400: "the bridge refused the request",
    401: "the bridge refused our key",
    403: "the bridge needs a team key, not the instructor's",
    404: "the bridge has no such poll",
    409: "the bridge can't check that poll yet",
    422: "the request body didn't match the bridge's schema",
    429: "today's check was already used",
}


class CantCompute(RuntimeError):
    """A figure can't be worked out from our records, so nothing is sent."""


class NotLoaded(CantCompute):
    """The only reason the figures can't be worked out: no counted receipt has loaded `slots` (a day-one line that
    came back unavailable, say), so what is in them isn't known. Nothing is sent and nothing is guessed. The daily
    job records the round as a skip until they are loaded (#254)."""

    def __init__(self, message: str, slots: tuple[str, ...]):
        super().__init__(message)
        self.slots = slots


class AlreadyChecked(RuntimeError):
    """Today's check is used, or may be, so a second send is refused."""


class WindowClosed(RuntimeError):
    """The caller's send window had closed by the time the send was ready, so nothing was sent
    (`tracking_daily` passes its window; nothing is recorded either)."""


class DispatchClosed(RuntimeError):
    """Past the 21:00 Eastern dispatch deadline, so the request was not started and nothing was
    recorded. A request started by the deadline is given up by the client within seconds, over
    three hours before midnight, so the bridge cannot check it on the next Eastern day; one started
    later could be. See `DISPATCH_DEADLINE`."""


class TooManyTries(RuntimeError):
    """The caller's bounded tries for this Eastern day are spent (`tracking_daily`); nothing was
    sent. Counted under the reservation's lock, so two replicas cannot both pass the check."""


class StaleFigures(RuntimeError):
    """The figures are as of a poll older than the caller's freshness limit by the moment of the
    send (`tracking_daily.STALE_POLL`), so the history sync may have stopped; nothing was sent.
    Checked under the reservation's lock, so a worker that prepared the send a while ago cannot
    spend the day's check on out-of-date books."""


class LockBusy(RuntimeError):
    """Another send holds the reservation's lock at that moment, so nothing was sent and nothing
    was recorded. The lock is taken without waiting (`pg_try_advisory_lock`): a replica stuck
    holding it must not strand every other replica's round behind it. The caller skips this round
    (`tracking_daily` logs a line) and the next round tries again."""


class BackingOff(RuntimeError):
    """A transient refusal today is still within its backoff, so the next try waits; nothing was
    sent. The wait is counted under the reservation's lock, so a replica that passed its own checks
    before another's refusal was stored still waits it out."""


class DailyTries(NamedTuple):
    """The worker's daily bounds, enforced under the send's reservation lock: the `sent_by` name,
    the limit on tries that don't settle on their own, the limit on refusals that do, and the
    backoff between those refusals (the first wait, its cap) (`tracking_daily`)."""
    by: str
    tries: int
    transient: int
    backoff: timedelta
    backoff_cap: timedelta


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


def backoff_wait(count: int, first: timedelta, cap: timedelta) -> timedelta:
    """How long after the day's `count`-th transient refusal the next try waits: `first` after the
    first, doubling up to `cap` (`tracking_daily.BACKOFF`, `BACKOFF_CAP`)."""
    return min(first * 2 ** (count - 1), cap)


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
    # The ledger and the stock cut at a time, so a second good poll of ours at the same moment would count
    # as up to this one, and the bridge gives no order between them.
    tied = conn.execute("select count(*) from polls where device_id = %s and crc_ok and started_at = %s"
                        " and session_id <> %s", (DEVICE_ID, started, session_id)).fetchone()[0]
    if tied:
        raise CantCompute(f"Poll {session_id} shares its time {started.isoformat()} with {tied} other poll"
                          f"{'s' if tied != 1 else ''} of our machine, so the figures as of it can't be told "
                          "apart. Pick a different poll with --as-of. Nothing was sent.")
    return started


def figures(conn: psycopg.Connection, as_of: str | None = None) -> Figures:
    """Our four figures as of the poll `as_of` (default the newest stored one), or `CantCompute`.

    Every read, the tie check in `poll_time` included, is in one repeatable-read, read-only
    transaction, so a poll committed part way through can't reach the figures without the check
    seeing it. A caller that already has a transaction open gets its reads in that one instead.
    Raises `SlotsNotRecorded` when our slots can't be read (`vending.slots.our_slots`)."""
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        return _figures(conn, as_of)
    with conn.transaction():
        conn.execute("set transaction isolation level repeatable read, read only")
        return _figures(conn, as_of)


def _figures(conn: psycopg.Connection, as_of: str | None) -> Figures:
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
    ranged = [f"{slot} ({by_slot[slot].units} to {by_slot[slot].at_most})" for slot in slots
              if slot in by_slot and by_slot[slot].at_most is not None
              and by_slot[slot].at_most != by_slot[slot].units]
    back = {c.product: c.units for c in counted.closet if c.units}
    negative = [f"{sku} ({units})" for sku, units in back.items() if units < 0]
    # Every reason at once, so an unloaded slot is the skip's reason (`NotLoaded`) only when it is the only one.
    reasons = []
    if unknown:
        reasons.append(f"No counted receipt has loaded slots {', '.join(unknown)}, so what is in them isn't known.")
    if ranged:
        reasons.append("These slots' counts are a range, because sales across a product change couldn't be split: "
                       f"{', '.join(ranged)}.")
    if negative:
        reasons.append(f"The closet count is below zero for {', '.join(negative)}.")
    if reasons:
        message = " ".join(reasons) + " Nothing was sent."
        if unknown and len(reasons) == 1:
            raise NotLoaded(message, tuple(unknown))
        raise CantCompute(message)
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
    with no usable answer, or one still waiting), it may have used any day from the send to
    `IN_FLIGHT` after it, or to the answer if that came later. Storing a timeout doesn't shorten
    that: the request may still be on its way to the bridge."""
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
    end = max(sent_at + IN_FLIGHT, answered_at or sent_at)
    day, last = eastern_day(sent_at), eastern_day(end)
    while day <= last:
        days.add(day)
        day += timedelta(days=1)
    return days


def blocking(conn: psycopg.Connection, day: date, now: datetime | None = None) -> list[tuple[int, int | None, bool | None]]:
    """(id, status, used_check) of each send that used `day`'s check, or may have (`days_used`).

    With `now`, a reservation from the previous day that still has no answer also blocks the
    morning: the held request may still reach the bridge after midnight and be checked on `day`,
    which would spend the day's check before our first send. The block holds until the morning
    window opens (`MORNING_FROM`, 06:00 Eastern); after it a request is hours past its start -
    none is started later than 21:00 (`DISPATCH_DEADLINE`) - and the bridge no longer reads one,
    so the block lifts and the day's check goes out."""
    rows = conn.execute("select id, sent_at, answered_at, status, used_check, answer, next_check_from"
                        " from tracking_checks where used_check is distinct from false"
                        " and eastern_day between %s and %s order by id",
                        (day - timedelta(days=2), day)).fetchall()
    found = [(i, status, used) for i, sent, answered, status, used, answer, next_from in rows
             if day in days_used(sent, answered, status, used, answer, next_from)]
    if now is not None:
        local = now.astimezone(EASTERN)
        if local.date() == day and local.time() < MORNING_FROM:
            seen = {entry[0] for entry in found}
            found += [(i, status, used) for i, status, used in
                      conn.execute("select id, status, used_check from tracking_checks"
                                   " where eastern_day = %s and used_check is null order by id",
                                   (day - timedelta(days=1),))
                      if i not in seen]
    return found


def send(conn: psycopg.Connection, bridge: Any, found: Figures | Callable[[psycopg.Connection, datetime], Figures],
         by: str, now: datetime | Callable[[], datetime] | None = None, once_a_day: bool = False,
         window: tuple[time, time] | None = None, daily_tries: DailyTries | None = None,
         stale: timedelta | None = None) -> Outcome:
    """Send `found` once, never retried, storing the request first and the answer after.

    `found` is the figures as they were when the caller looked, or a callable that computes them
    here: given the send's own connection and the clock of the send's moment, it picks the poll and
    computes the figures from the books of that moment, inside the reservation's transaction - so
    whatever committed while the caller was preparing (a ledger correction, a newer poll, a day's
    turn) is in the books the check reports. The worker's daily send passes the callable.

    `now` stamps the reservation (default the database clock at that moment); a callable is asked
    again for the answer, so a send that runs long records the answer's actual time. Raises
    `AlreadyChecked` from `daily_from()` on, when today's check is used or may be. With
    `once_a_day` (the worker's daily send, `vending.tracking_daily`) it does so before then too.

    Everything is re-checked under the send's advisory lock, with the clock of that moment, so a
    caller that started a while ago cannot send on stale facts: `window` (Eastern start and cutoff)
    raises `WindowClosed` when the moment to send has passed, and no request is started after the
    21:00 Eastern dispatch deadline (`DispatchClosed`); `stale` raises `StaleFigures` when the
    poll the figures are as of is older than the limit at that moment; `daily_tries` raises
    `TooManyTries` when the day's tries are spent and `BackingOff` while the newest transient
    refusal is still within its backoff. All before anything is recorded. The lock is the session's,
    taken without waiting (`pg_try_advisory_lock`) before the reservation's transaction begins: a
    second replica that finds it held raises `LockBusy` and skips its round, so a stuck holder cannot
    strand the others, and one that acquires it after the reservation committed sees the reservation
    (`blocking`) and refuses. Then one repeatable-read transaction holds the poll's pick, its tie
    check, the ledger and stock reads, the day's earlier sends and the reservation's insert, so a
    poll or correction committed part way through cannot reach some reads and not others. The lock
    is released once that transaction ends, in `finally`.

    Just before the request goes out the clock is read again and the window, the deadline and the
    Eastern day are checked once more: computing the figures can take long enough to pass them. If
    they have passed, the committed reservation is marked unused (`used_check` false, `error_type`
    `not_sent`), the request is never started, and `WindowClosed` or `DispatchClosed` is raised."""
    if not by or not by.strip():
        raise ValueError("a send needs a name, for the record")
    fixed = now
    stamp: Callable[[], datetime] = (now if callable(now) else (lambda: fixed) if fixed is not None
                                     else (lambda: clock(conn)))
    # Every connection the app opens is in autocommit (`vending.db.connect`), so the transaction below is
    # the reservation's own; inside a caller's transaction it is a savepoint and the caller's isolation stands.
    own = conn.autocommit and conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    # The session's lock, taken without waiting and before the snapshot below: a holder stuck since an
    # earlier round must not strand every other replica's round behind it (LockBusy), and a snapshot
    # taken after the lock sees every reservation committed before it.
    if not conn.execute("select pg_try_advisory_lock(%s)", (TRACKING_LOCK,)).fetchone()[0]:
        raise LockBusy("another send holds the reservation's lock; nothing was sent")
    try:
        with conn.transaction():
            if own:
                # One snapshot for the poll, the tie check, the ledger, the stock, the day's earlier
                # sends and the reservation itself (`figures` reads in this transaction).
                conn.execute("set transaction isolation level repeatable read")
            moment = stamp()
            day = eastern_day(moment)
            _check_dispatch(moment, window, day)
            if callable(found):
                found = found(conn, moment)
            body = found.body()
            if stale is not None and moment - found.as_of_time > stale:
                raise StaleFigures(
                    f"the figures are as of the poll at {found.as_of_time.astimezone(EASTERN):%Y-%m-%d %H:%M} "
                    f"Eastern, over {int(stale.total_seconds() // 3600)} hours before the send, so the history "
                    "sync may have stopped; nothing was sent")
            if daily_tries is not None:
                _check_tries(conn, day, moment, daily_tries)
            if once_a_day or moment >= daily_from():
                earlier = blocking(conn, day, moment)
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
    finally:
        try:
            conn.execute("select pg_advisory_unlock(%s)", (TRACKING_LOCK,))
        except psycopg.Error:
            pass                           # a broken connection: its session lock goes with it

    # The clock again, just before the request: computing the figures can take long enough to pass
    # the window, the deadline or midnight. Then the reservation is marked unused, never sent.
    ready = stamp()
    try:
        _check_dispatch(ready, window, day)
    except (WindowClosed, DispatchClosed):
        conn.execute("update tracking_checks set answered_at = %s, used_check = false, error_type = %s"
                     " where id = %s", (ready, NOT_SENT, row_id))
        raise
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
    except BridgeNotJson as error:         # it answered, but not JSON: it may have run
        text = error.body.decode(errors="replace")[:300]
        return _answered(conn, stamp, row_id, error.status, {"unparsed": text}, "malformed_answer", None)
    except BridgeUnreachable as error:     # certainly never arrived
        return _answered(conn, stamp, row_id, None, {"no_answer": str(error)[:300]}, "unreachable", False)
    except BridgeError as error:           # sent, but no usable answer: it may have run
        return _answered(conn, stamp, row_id, None, {"no_answer": str(error)[:300]}, "no_answer", None)
    error_type = answer.get("error_type") if isinstance(answer, dict) and status != 200 else None
    if not isinstance(error_type, str):
        error_type = None
    elif len(error_type) > 100:
        error_type = error_type[:100]      # kept as sent, but never printed unless it is one we know
    safe = _db_safe(answer)
    if status == 200 and (safe != answer or _results(answer, found.as_of) is None):
        # The bridge answered, so it may well have used the check, but the verdicts can't be read. A NUL
        # or the like (`_db_safe`) counts too: the bridge never sends one in a good answer.
        return _answered(conn, stamp, row_id, status, answer, "malformed_answer", None, found.as_of)
    if status == 200:
        used: bool | None = True
    elif status == 429:
        used = True                        # `daily_check_used`: today's is gone
    elif status in REFUSED_BEFORE_CHECK:
        used = False
    else:
        used = None                        # a 5xx or a status the spec doesn't name
    return _answered(conn, stamp, row_id, status, answer, error_type, used, found.as_of)


def _check_dispatch(moment: datetime, window: tuple[time, time] | None, day: date) -> None:
    """`WindowClosed` when `moment` is outside the caller's `window` (Eastern) or no longer on the
    Eastern `day` the send was reserved under, `DispatchClosed` when it is past the 21:00 Eastern
    deadline. Checked under the reservation's lock and again just before the request goes out."""
    local = moment.astimezone(EASTERN)
    if window is not None and not window[0] <= local.time() < window[1]:
        raise WindowClosed(f"the send window ({window[0]:%H:%M}-{window[1]:%H:%M} Eastern) closed while the "
                           f"check was being prepared; it was ready at {local:%H:%M}, so nothing was sent")
    if local.time() >= DISPATCH_DEADLINE:
        raise DispatchClosed(
            f"past the {DISPATCH_DEADLINE:%H:%M} Eastern dispatch deadline ({local:%H:%M} now): a request "
            "started by the deadline is given up by the client within seconds, hours before midnight, but "
            "one started now could still be checked by the bridge after midnight. Nothing was sent")
    if local.date() != day:
        raise WindowClosed(f"the Eastern day turned ({day} to {local.date()}) while the check was being "
                           "prepared, so nothing was sent")


def _check_tries(conn: psycopg.Connection, day: date, moment: datetime, daily_tries: DailyTries) -> None:
    """`TooManyTries` when the day's tries are spent, `BackingOff` while the day's newest transient
    refusal is still within its backoff, and `BackingOff` while one of the day's sends has no answer
    recorded yet (a concurrent send's outcome is unknown, so this one waits for it). All three are
    counted here, inside the reservation's transaction and with the clock of the send, so two
    replicas cannot together exceed the limits or slip past the waits: one that passed its own
    checks before another's refusal was stored still waits it out. `blocking` already refuses a day
    whose check is used or may be, so what is left to bound is tries that never block anything. A
    transient refusal (`TRANSIENT_REFUSALS`) settles on its own, so it has its own, larger bound."""
    by, tries_limit, transient_limit, backoff, backoff_cap = daily_tries
    rows = conn.execute("select id, sent_at, used_check, error_type from tracking_checks"
                        " where eastern_day = %s and sent_by = %s order by sent_at, id", (day, by)).fetchall()
    rows = [row for row in rows if row[3] != NOT_SENT]     # never sent, so no try
    transient = [(id_, sent, error) for id_, sent, used, error in rows
                 if used is False and error in TRANSIENT_REFUSALS]
    tries = len(rows) - len(transient)
    if tries >= tries_limit:
        raise TooManyTries(f"{by} has made {tries} tries today ({day}, Eastern) that don't settle on their own, "
                           f"up to its limit of {tries_limit}, so no more until tomorrow")
    if len(transient) >= transient_limit:
        raise TooManyTries(f"{by} has had {len(transient)} transient refusals today ({day}, Eastern), up to its "
                           f"limit of {transient_limit}, so no more until tomorrow")
    waiting = next((id_ for id_, sent, used, error in rows if used is None), None)
    if waiting is not None:
        raise BackingOff(f"a send's answer is still being recorded or was left unfinished (#{waiting}); nothing "
                         "is sent until its outcome is known")
    if transient:
        wait = backoff_wait(len(transient), backoff, backoff_cap)
        newest = transient[-1][1]
        if moment < newest + wait:
            raise BackingOff(f"the books aren't ready yet ({len(transient)} transient refusals today); backing "
                             f"off until {(newest + wait).astimezone(EASTERN):%H:%M} Eastern")


def _results(answer: Any, as_of: str) -> dict[str, str] | None:
    """The four verdicts of a 200 of the spec's shape about the poll we sent, or None.

    Every field is required: `as_of` is our poll, `as_of_time`, `checked_at` and `next_check_from`
    are times with an offset, and `results` holds the four, each `within` or `outside`."""
    if not isinstance(answer, dict) or any(k not in answer for k in ANSWER_FIELDS):
        return None
    if answer["as_of"] != as_of or any(_time(answer[k]) is None
                                       for k in ("as_of_time", "checked_at", "next_check_from")):
        return None
    given = answer["results"]
    if not isinstance(given, dict) or any(given.get(k) not in VERDICTS for k in RESULTS):
        return None
    return {k: given[k] for k in RESULTS}


def _answered(conn: psycopg.Connection, stamp: Callable[[], datetime], row_id: int, status: int | None,
              answer: Any, error_type: str | None, used: bool | None, as_of: str = "") -> Outcome:
    # Every column written from the reply goes through `_db_safe`: the answer, and `error_type`, its text.
    answer, error_type = _db_safe(answer), _db_safe(error_type)
    results = _results(answer, as_of) if status == 200 and used else None
    next_from = _time(answer.get("next_check_from")) if isinstance(answer, dict) else None
    conn.execute(
        "update tracking_checks set answered_at = %s, status = %s, answer = %s, error_type = %s, used_check = %s,"
        " results = %s, next_check_from = %s where id = %s",
        (stamp(), status, None if answer is None else Jsonb(answer), error_type, used,
         None if results is None else Jsonb(results), next_from, row_id))
    return Outcome(row_id, status, answer, error_type, used, results, next_from)


def _db_safe(value: Any) -> Any:
    """`value` with what a jsonb column refuses replaced, so storing an answer can't fail on the reply's
    text: a NUL becomes U+FFFD, a lone surrogate `?`, and NaN or infinity its name as a string. Lossy
    by design: the stored answer is for people to read, and a 200 that needed it is malformed anyway."""
    if isinstance(value, str):
        return value.replace("\x00", "\ufffd").encode("utf-8", "replace").decode("utf-8")
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, list):
        return [_db_safe(v) for v in value]
    if isinstance(value, dict):
        return {_db_safe(k): _db_safe(v) for k, v in value.items()}
    return value


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


def said(status: int | None, error_type: str | None) -> str:
    """Our own words for an answer: by its `error_type` if the spec names it, else by its status."""
    if error_type is not None:
        return ERROR_TYPES.get(error_type, UNKNOWN_ERROR_TYPE)
    if status is None:
        return "no answer stored"
    if status >= 500:
        return "the bridge failed (a server error)"
    return STATUSES.get(status, "a status the spec doesn't name")


def _verdicts(results: Any) -> str:
    """`cash within, ...` from stored verdicts: only the four we know, each only `within` or `outside`."""
    if not isinstance(results, dict):
        return ""
    return ", ".join(f"{k} {results[k]}" if results.get(k) in VERDICTS else f"{k} ?" for k in RESULTS)


def _print_outcome(o: Outcome) -> None:
    if o.status is None:
        print(f"Check #{o.id}: no usable answer: {said(None, o.error_type)}. "
              + ("Today's check is still there." if o.used_check is False
                 else "It may have used today's check."))
        return
    if o.results is not None:
        print(f"Check #{o.id}: " + _verdicts(o.results))
    else:
        print(f"Check #{o.id}: {o.status}: {said(o.status, o.error_type)}.")
        items = o.answer.get("items") if isinstance(o.answer, dict) else None
        if isinstance(items, list) and items:
            print(f"  The bridge listed {len(items)} item{'s' if len(items) != 1 else ''}. They are stored "
                  "with the check in tracking_checks, not shown here.")
    if o.used_check is False:
        print("The bridge refused it before checking, so today's check is still there.")
    elif o.used_check is None:
        print("It may have used today's check.")
    if o.next_check_from is not None:
        print(f"Next check from {o.next_check_from.astimezone(EASTERN):%Y-%m-%d %H:%M %Z}.")


def _show(conn: psycopg.Connection) -> int:
    rows = conn.execute("select id, sent_at, sent_by, as_of, status, error_type, used_check, results"
                        " from tracking_checks order by id desc limit 50").fetchall()
    if not rows:
        print("No checks sent yet.")
    for i, sent, by, as_of, status, error_type, used, results in rows:
        words = _verdicts(results) if results else said(status, error_type)
        print(f"#{i}  {sent.astimezone(EASTERN):%Y-%m-%d %H:%M}  by {by}  as of {as_of}  "
              f"{status if status is not None else '-'}  {words}  used: {used}")
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
        except (AlreadyChecked, DispatchClosed, WindowClosed, LockBusy, settings.SettingsError) as error:
            print(error, file=sys.stderr)
            return 1
        except psycopg.Error as error:
            # Its message can quote what the bridge sent, so only its class is printed.
            print(f"Recording the check failed ({type(error).__name__}), so it may have been sent and may "
                  "have used today's check. See: python -m vending.tracking show", file=sys.stderr)
            return 3
        _print_outcome(outcome)
        if outcome.results is not None:
            return 0
        # No verdicts we can read: 3 when the check may have been used without them, else 1.
        return 3 if outcome.status is None or outcome.error_type == "malformed_answer" else 1


if __name__ == "__main__":
    sys.exit(main())
