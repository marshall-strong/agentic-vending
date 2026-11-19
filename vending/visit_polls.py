"""Live reads of the machine around our Friday visit, taken by the worker.

The machine's counters are cumulative per slot and are read only when polled. A sale between the
last read before a visit and the first read after it can't be told apart (old product or new) when
the price didn't change. A read taken while the door is open settles it: no unit sells with the door
open, so everything before that read is the old product, everything after it is the new one, and the
receipt's door times prove the read fell inside the visit (`vending.stock._split_by_door`).

So on Fridays, from `VISIT_POLL_FROM` to `VISIT_POLL_TO` Eastern (unset: 14:45 to 19:00), the worker
asks the bridge to read the machine every `VISIT_POLL_EVERY_MINUTES` (unset: 1; 0 turns it off). The
bridge stores each read in its history, and the worker copies the day's history into our database
straight after (`vending.sync_history`), so the read reaches the stock replay like any other poll.
A visit lasts a few minutes, so the interval has to be short for a read to land inside it.

It stops for the day once a request of ours was fulfilled that day and a read taken after its door
closed is stored. A receipt is only seen when it is read back, so each step first re-reads our
pending requests (a few cheap GETs, not machine reads).

A read is a live poll, about 8.5 s on the bridge's one serial line, so it is gentle by construction:

- never started while an earlier one is still running (`run_round` skips a job that is);
- one try: a busy answer (409), a timeout or a bridge that is down is logged and skipped, and the next
  try is a whole interval later, never a retry;
- after 3 failures in a row, 5 minutes pass before the next try.

A read that fails never fails the worker's round or `/health`: reads are not jobs of the round and
are never in its results. Only the log shows them.

Only Fridays. A midweek visit (off-cycle request, $25) has no day we can read from the data: a pending
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
DEFAULT_EVERY, DEFAULT_FROM, DEFAULT_TO = "1", "14:45", "19:00"
FRIDAY = 4
FAILS_BEFORE_BACKOFF = 3
BACKOFF = timedelta(minutes=5)
# The read (30 s at the bridge) plus copying the history afterwards.
STEP_TIMEOUT_SECONDS = 90
# What a step returns: it read the machine, or the visit is over and it read nothing.
READ, VISIT_OVER = 0, 2
_MINUTE = timedelta(minutes=1)


@dataclass(frozen=True)
class Schedule:
    every: timedelta
    start: time       # Eastern, the first minute of the window
    end: time         # Eastern, the window closes then


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
    except ValueError:
        every = -1.0
    if every < 0 or every != every:
        raise settings.SettingsError(f"{EVERY_SETTING} is {value!r}, not a number of minutes")
    start, end = clock(FROM_SETTING, DEFAULT_FROM), clock(TO_SETTING, DEFAULT_TO)
    if end <= start:
        raise settings.SettingsError(f"{TO_SETTING} ({end:%H:%M}) is not after {FROM_SETTING} ({start:%H:%M})")
    return Schedule(timedelta(minutes=every), start, end) if every else None


def _window(sched: Schedule, day: date) -> tuple[datetime, datetime]:
    return datetime.combine(day, sched.start, EASTERN), datetime.combine(day, sched.end, EASTERN)


def window_open(sched: Schedule, now: datetime) -> bool:
    """Whether `now` is on a Friday, inside the window (Eastern; the end is not in it)."""
    day = now.astimezone(EASTERN).date()
    if day.weekday() != FRIDAY:
        return False
    opens, closes = _window(sched, day)
    return opens <= now < closes


def next_open(sched: Schedule, now: datetime) -> datetime:
    """The start of the next Friday window after `now`."""
    day = now.astimezone(EASTERN).date()
    while True:
        if day.weekday() == FRIDAY and _window(sched, day)[0] > now:
            return _window(sched, day)[0].astimezone(timezone.utc)
        day += timedelta(days=1)


def _eastern_day(now: datetime) -> tuple[datetime, datetime]:
    day = now.astimezone(EASTERN).date()
    return (datetime.combine(day, time.min, EASTERN), datetime.combine(day + timedelta(days=1), time.min, EASTERN))


def visit_over(conn, now: datetime) -> bool:
    """Whether a request of ours was fulfilled today (Eastern) and a good poll taken after its
    door closed is stored. The close is a minute, so the poll must be taken after that minute ends;
    a receipt with no door times is dated by `fulfilled_at`. With more than one, the latest counts."""
    first, last = _eastern_day(now)
    [close] = conn.execute(
        """
        select max(case when door_closed_at is not null then door_closed_at + interval '1 minute'
                        else fulfilled_at end)
          from purchase_requests
         where status = 'fulfilled' and coalesce(door_closed_at, fulfilled_at) >= %s
           and coalesce(door_closed_at, fulfilled_at) < %s
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


def step(now: datetime, log: Callable[[str], None] = print) -> int:
    """One turn: read pending requests back, stop if the visit is over, else read the machine and
    copy the history. `READ` or `VISIT_OVER`; a failed read raises. Never migrates."""
    with db.connect() as conn:
        behind = db.pending(conn)
        if behind:
            raise RuntimeError(f"the database is missing migrations: {', '.join(behind)}")
        bridge = Bridge()
        refresh_pending(conn, bridge, now, log)
        if visit_over(conn, now):
            return VISIT_OVER
        bridge.poll()
        # The read is in the bridge's history by now. Two UTC days, since 7 PM Eastern is past midnight UTC.
        sync_history.sync(conn, bridge, (now - timedelta(days=1)).astimezone(timezone.utc).date(),
                          (now + _MINUTE).astimezone(timezone.utc).date(),
                          log=lambda line: None if line.endswith(" listed") else log(line))
    return READ


class Reader:
    """Decides when the next read is due and takes it. The worker's loop asks `next_due` and calls `tick`."""

    def __init__(self, sched: Schedule, run: Callable[[datetime], int] = step,
                 log: Callable[[str], None] = print, timeout: float = STEP_TIMEOUT_SECONDS):
        self.sched, self.run, self.log, self.timeout = sched, run, log, timeout
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
            self.failures, self.not_before = 0, now + self.sched.every
        else:
            self.failures += 1
            wait = BACKOFF if self.failures >= FAILS_BEFORE_BACKOFF else self.sched.every
            self.not_before = now + wait
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
    return Reader(sched, lambda now: step(now, log), log)
