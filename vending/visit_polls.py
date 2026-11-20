"""Live reads of the machine around our Friday visit, taken by the worker.

The machine's counters are cumulative per slot and are read only when polled. A sale between the
last read before a visit and the first read after it can't be told apart (old product or new) when
the price didn't change. A read taken while the door is open settles it: no unit sells with the door
open, so everything before that read is the old product, everything after it is the new one, and the
receipt's door times prove the read fell inside the visit (`vending.stock._split_by_door`).

So on Fridays, from `VISIT_POLL_FROM` to `VISIT_POLL_TO` Eastern (unset: 14:45 to 19:00), the worker
asks the bridge to read the machine every `VISIT_POLL_EVERY_MINUTES` (unset: 1; 0 turns it off; anything
else under 1 or over `MAX_EVERY_MINUTES`, a day, is refused). `VISIT_POLL_DATES` (unset: none) lists more
Eastern days that get the same window and stop rule, such as the day-one fill, which is a visit of ours on
a Monday. The bridge stores each read in its history, and the worker copies the day's history into our
database straight after (`vending.sync_history`), so the read reaches the stock replay like any other poll.
Whether that copy holds a usable row for the session the bridge named is only logged ("storage not
confirmed"): nothing in the repo shows the header's id is the history's id, so a read that went out is not
counted as a failure for it. What counts is the stored rows: the stop rule and the stock replay use their
`started_at`, whichever read they came from (a cached document, `X-Poll-Cached`, is another caller's and is
just a row at its own time).
A visit lasts a few minutes, so the interval has to be short for a read to land inside it.

It stops for the day once a request of ours was fulfilled that day and a read taken after its door
closed is stored. A receipt is only seen when it is read back, so each step first re-reads our
pending requests (a few cheap GETs, not machine reads).

A read is a live poll, about 8.5 s on the bridge's one serial line, so it is gentle by construction:

- never started while an earlier one is still running (`run_round` skips a job that is);
- the window and the spacing are checked again at the POST itself, from the last POST actually sent,
  because reading our receipts first can take a while;
- one try: a busy answer (409), a timeout or a bridge that is down is logged and skipped, and the next
  try is a whole interval later, never a retry;
- after 3 failures in a row, 5 minutes pass before the next try, counted from the POST that failed (from
  the failure, when none was sent). A read that went out but whose row isn't confirmed is not a failure.

A read that fails never fails the worker's round or `/health`: reads are not jobs of the round and
are never in its results. Only the log shows them.

Only Fridays, and the dates in `VISIT_POLL_DATES`. A midweek visit (off-cycle request, $25) has no day we can read from the data: a pending
request can be filled on any day, and reading every day until it is would be about 250 live reads a day.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from vending import db, purchase_requests, settings, sync_history
from vending.bridge import Bridge, BridgeError
from vending.slots import DEVICE_ID

EASTERN = ZoneInfo("America/New_York")
EVERY_SETTING, FROM_SETTING, TO_SETTING = "VISIT_POLL_EVERY_MINUTES", "VISIT_POLL_FROM", "VISIT_POLL_TO"
DATES_SETTING = "VISIT_POLL_DATES"
DEFAULT_EVERY, DEFAULT_FROM, DEFAULT_TO = "1", "14:45", "19:00"
FRIDAY = 4
FAILS_BEFORE_BACKOFF = 3
BACKOFF = timedelta(minutes=5)
# The window is inside one Eastern day, so a longer interval means one read a day at most; and a huge one
# (1e12 minutes passes `timedelta`) overflows when the scheduler adds it to a time.
MAX_EVERY_MINUTES = 24 * 60
# The read (30 s at the bridge) plus copying the history afterwards.
STEP_TIMEOUT_SECONDS = 90
# What a step returns: it read the machine, the visit is over, or it sent nothing and nothing failed
# (the window closed, or the last POST was too recent). 1 is for a failure.
READ, VISIT_OVER, SKIPPED = 0, 2, 3
_MINUTE = timedelta(minutes=1)
# When this process last asked the bridge to read the machine. There is one serial line and one worker,
# so one record; the spacing is measured from it, not from when a step began.
_last_post: datetime | None = None
_last_post_ok: datetime | None = None   # when a read last went out and was answered


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Schedule:
    every: timedelta
    start: time       # Eastern, the first minute of the window
    end: time         # Eastern, the window closes then
    dates: frozenset[date] = frozenset()    # Eastern days that get the window besides Fridays


def schedule() -> Schedule | None:
    """The schedule from the settings, or None when it is switched off (every 0).
    `settings.SettingsError` for a value that isn't one."""
    def clock(name: str, default: str) -> time:
        value = settings.get(name, default)
        try:
            return time.fromisoformat(value.strip())
        except ValueError:
            raise settings.SettingsError(f"{name} is {value!r}, not a time like 14:45") from None

    value = settings.get(EVERY_SETTING, DEFAULT_EVERY)
    try:
        every = float(value)
        gap = timedelta(minutes=every)      # inf, nan and absurdly large values raise here
    except (ValueError, OverflowError):
        gap = None
    if gap is None or (every != 0 and every < 1) or every > MAX_EVERY_MINUTES:   # under a minute is too often
        raise settings.SettingsError(
            f"{EVERY_SETTING} is {value!r}, not 0 (off) or a number of minutes from 1 to {MAX_EVERY_MINUTES}")
    start, end = clock(FROM_SETTING, DEFAULT_FROM), clock(TO_SETTING, DEFAULT_TO)
    if end <= start:
        raise settings.SettingsError(f"{TO_SETTING} ({end:%H:%M}) is not after {FROM_SETTING} ({start:%H:%M})")
    dates = set()
    for part in (settings.get(DATES_SETTING) or "").split(","):
        if part.strip():
            try:
                dates.add(date.fromisoformat(part.strip()))
            except ValueError:
                raise settings.SettingsError(
                    f"{DATES_SETTING} has {part.strip()!r}, not a date like 2026-11-30") from None
    return Schedule(gap, start, end, frozenset(dates)) if every else None


def _window(sched: Schedule, day: date) -> tuple[datetime, datetime]:
    return datetime.combine(day, sched.start, EASTERN), datetime.combine(day, sched.end, EASTERN)


def _visit_day(sched: Schedule, day: date) -> bool:
    return day.weekday() == FRIDAY or day in sched.dates


def window_open(sched: Schedule, now: datetime) -> bool:
    """Whether `now` is on a Friday (or a listed date), inside the window (Eastern; the end is not in it)."""
    day = now.astimezone(EASTERN).date()
    if not _visit_day(sched, day):
        return False
    opens, closes = _window(sched, day)
    return opens <= now < closes


def next_open(sched: Schedule, now: datetime) -> datetime:
    """The start of the next window (a Friday or a listed date) after `now`."""
    day = now.astimezone(EASTERN).date()
    while True:
        if _visit_day(sched, day) and _window(sched, day)[0] > now:
            return _window(sched, day)[0].astimezone(timezone.utc)
        day += timedelta(days=1)


def _eastern_day(now: datetime) -> tuple[datetime, datetime]:
    day = now.astimezone(EASTERN).date()
    return (datetime.combine(day, time.min, EASTERN), datetime.combine(day + timedelta(days=1), time.min, EASTERN))


def visit_over(conn, now: datetime) -> bool:
    """Whether a request of ours was fulfilled today (Eastern) and a good poll taken after its
    door closed is stored. The close is a minute, so the poll must be taken after that minute ends;
    a receipt with no door times is dated by `fulfilled_at`. The day is the one the receipt was
    fulfilled on, so a receipt filed after Eastern midnight counts on the day it arrived. With more than
    one, the latest counts."""
    first, last = _eastern_day(now)
    [close] = conn.execute(
        """
        select max(case when door_closed_at is not null then door_closed_at + interval '1 minute'
                        else fulfilled_at end)
          from purchase_requests
         where status = 'fulfilled' and coalesce(fulfilled_at, door_closed_at) >= %s
           and coalesce(fulfilled_at, door_closed_at) < %s
        """,
        (first, last)).fetchone()
    if close is None:
        return False
    return conn.execute("select exists (select 1 from polls where device_id = %s and crc_ok and started_at >= %s)",
                        (DEVICE_ID, close)).fetchone()[0]


def refresh_pending(conn, bridge: Bridge, now: datetime, log: Callable[[str], None]) -> None:
    """Read our pending requests back, so a receipt submitted a minute ago is seen now and not at the
    next hourly round. A request that can't be read is logged and left for that round."""
    for (request_id,) in conn.execute(
            "select request_id from purchase_requests where status = 'pending' order by submitted_at").fetchall():
        try:
            purchase_requests.store(conn, request_id, bridge.purchase_request(request_id), now)
        except (BridgeError, ValueError) as error:
            log(f"visit reads: could not read {request_id} ({error}); left for the hourly round")


def _not_confirmed(conn, session_id: str | None, sent: datetime) -> str | None:
    """Why this read's row can't be matched to a usable stored one (a checksum that passed, our device), or
    None when it is. Without a session id the best check left is a usable poll taken since the POST: an
    earlier row, even from a few seconds before, is an observation at its own time and not this read."""
    if session_id is None:
        found = conn.execute("select exists (select 1 from polls where device_id = %s and crc_ok and started_at >= %s)",
                             (DEVICE_ID, sent)).fetchone()[0]
        return None if found else "the bridge named no session and no usable poll taken since the read was sent is stored"
    row = conn.execute("select crc_ok, device_id from polls where session_id = %s", (session_id,)).fetchone()
    if row is None:
        return "it isn't stored (the history listing or the fetch failed, or the poll isn't in the listing)"
    if not row[0]:
        return "its checksum failed or it isn't EVA-DTS"
    if row[1] != DEVICE_ID:
        return "device mismatch"      # not the device it was stored under: the listing's word, read by people
    return None


def step(now: datetime, log: Callable[[str], None] = print, sched: Schedule | None = None) -> int:
    """One turn: read pending requests back, stop if the visit is over, else read the machine and
    copy the history. `READ`, `VISIT_OVER` or `SKIPPED`; a read that failed (an error status, a timeout, a
    bridge that is down) raises. A read that went out but whose row can't be matched is logged ("storage
    not confirmed") and is still `READ`: the header's id may not be the history's id, and the stop rule
    works from the stored rows anyway. Never migrates. `sched` is the schedule the settings give, unless
    one is passed."""
    global _last_post, _last_post_ok
    if sched is None:
        sched = schedule()
    if sched is None:
        return SKIPPED
    with db.connect() as conn:
        behind = db.pending(conn)
        if behind:
            raise RuntimeError(f"the database is missing migrations: {', '.join(behind)}")
        bridge = Bridge()
        refresh_pending(conn, bridge, now, log)
        if visit_over(conn, now):
            return VISIT_OVER
        # Reading the receipts took time (and a step that outlived its timeout can run very late), so
        # look at the clock again, right before the POST.
        sent = _utcnow()
        if (sent - now).total_seconds() >= STEP_TIMEOUT_SECONDS:
            # The reader gave up on this step already and may be backing off; it must not send now.
            log("visit reads: skipped, this step outlived its timeout before the read could be sent")
            return SKIPPED
        if not window_open(sched, sent):
            log("visit reads: skipped, the window closed before the read could be sent")
            return SKIPPED
        if _last_post is not None and sent < _last_post + sched.every:
            log(f"visit reads: skipped, the last read was sent {(sent - _last_post).total_seconds():.0f} s ago")
            return SKIPPED
        _last_post = sent
        read = bridge.poll()
        _last_post_ok = sent
        named = read.session_id or "(no session id)"
        cached = "?" if read.cached is None else str(read.cached).lower()
        age = "?" if read.age_seconds is None else f"{read.age_seconds:g}"
        log(f"visit reads: read {named} cached={cached} age_seconds={age}")
        # The read is in the bridge's history by now. Two UTC days, since 7 PM Eastern is past midnight UTC.
        try:
            sync_history.sync(conn, bridge, (sent - timedelta(days=1)).astimezone(timezone.utc).date(),
                              (sent + _MINUTE).astimezone(timezone.utc).date(),
                              log=lambda line: None if line.endswith(" listed") else log(line))
        except BridgeError as error:        # the POST worked; only the copy didn't
            log(f"visit reads: could not copy the history ({error})")
        problem = _not_confirmed(conn, read.session_id, sent)
        if problem:
            log(f"visit reads: storage not confirmed for read {named}: {problem}")
        else:
            log(f"visit reads: stored read {named}")
    return READ


class Reader:
    """Decides when the next read is due and takes it. The worker's loop asks `next_due` and calls `tick`."""

    def __init__(self, sched: Schedule, run: Callable[[datetime], int] = step,
                 log: Callable[[str], None] = print, timeout: float = STEP_TIMEOUT_SECONDS,
                 clock: Callable[[], datetime] | None = None):
        # `clock` tells when a step failed, when it sent no POST to date the failure by; without one, `now`.
        self.sched, self.run, self.log, self.timeout, self.clock = sched, run, log, timeout, clock
        self.failures = 0                       # in a row
        self.not_before: datetime | None = None
        self.over_on: date | None = None        # the Eastern day the visit ended
        self.running: dict = {}

    def _open(self, now: datetime) -> bool:
        return window_open(self.sched, now) and self.over_on != now.astimezone(EASTERN).date()

    def next_due(self, now: datetime) -> datetime:
        """When the next read is due: now or the earliest try allowed, inside a window; else the next window."""
        if self._open(now):
            due = max(now, self.not_before or now)
            if due < _window(self.sched, now.astimezone(EASTERN).date())[1]:
                return due
        return next_open(self.sched, now)

    def _after_post(self, now: datetime) -> datetime:
        """An interval after the last POST actually sent, which is later than `now` when the receipts were slow."""
        return max(now, _last_post or now) + self.sched.every

    def tick(self, now: datetime) -> None:
        """Take a read if one is due at `now`. Never raises, and never retries."""
        from vending.worker import run_round    # the worker imports this module

        if not self._open(now) or (self.not_before is not None and now < self.not_before):
            return
        [result] = run_round([("visit_read", lambda: self.run(now))], self.log, self.timeout, self.running)
        if result.outcome == "still running":
            self.not_before = now + self.sched.every
            self.log("visit reads: skipped, the earlier read is still running")
        elif result.exit_code == VISIT_OVER:
            self.over_on = now.astimezone(EASTERN).date()
            self.log("visit reads: our receipt is in and a read after the door closed is stored; done for today")
        elif result.exit_code == READ:
            self.failures, self.not_before = 0, self._after_post(now)
        elif result.exit_code == SKIPPED:       # sent nothing, so nothing failed
            self.not_before = self._after_post(now)
        elif result.outcome == "timed out" and _last_post_ok is not None and _last_post_ok >= now:
            # The read went out and was answered; only the copy of the history ran long. Like a copy that
            # failed, that leaves storage unconfirmed, and it isn't a failure of the read.
            self.failures, self.not_before = 0, self._after_post(now)
            self.log("visit reads: the read was sent, but copying the history outlived the step; storage not confirmed")
        else:
            self.failures += 1
            wait = BACKOFF if self.failures >= FAILS_BEFORE_BACKOFF else self.sched.every
            # The wait runs from the POST this step sent (the receipts may have taken a while first), or
            # from the failure when it sent none.
            sent = _last_post is not None and _last_post >= now
            self.not_before = (_last_post if sent else max(now, self.clock() if self.clock else now)) + wait
            detail = result.error or result.outcome
            self.log(f"visit reads: skipped ({detail}); {self.failures} failed in a row, "
                     f"next try in {wait.total_seconds() / 60:g} min")


def default_reader(log: Callable[[str], None] = print) -> Reader | None:
    """The reader the worker runs, or None when it is off or a setting is wrong (logged: it never stops the worker)."""
    try:
        sched = schedule()
    except settings.SettingsError as error:
        log(f"visit reads are off: {error}")
        return None
    if sched is None:
        log(f"visit reads are off ({EVERY_SETTING} is 0)")
        return None
    return Reader(sched, lambda now: step(now, log, sched), log, clock=lambda: _utcnow())
