"""Scheduled agent runs, started by the worker (#213). Off unless two settings both turn them on.

**Live only when both settings say `live`.** A scheduled run is `vending.agent.run(live=True)` only when the
container's `AGENT_SCHEDULE_MODE` and the database's `agent_schedule_mode` both say `live`; any other pair
is a dry run (`live=False`, no write tool: `vending.agent.WRITE_TOOLS`) or off. A live run goes through the
same entry point as `python -m vending.agent --live`, so every live-run rule holds unchanged: the halt
switch, the caps, the run lease, only the plan its last preview accepted, at most one submit (with #217's
retry rules), the `submission_unresolved` block and the planner's spendable floor (#188).

**Checks before a send (`live_blocker`).** A time claimed as live runs as a dry run instead, with why at the
start of its detail, when: the competition hasn't started (`vending.start.START`, by the database's clock); a
purchase submission is still open (`vending.submissions.open_rows`: ORDERS BLOCKED); or our slot assignment
can't be read (`vending.slots.our_slots`) or the worker's hourly `slots check` last confirmed it more than
`SLOTS_FRESH` ago. The same checks run again in the transaction that takes the run lease, and there a failed
one, or a database setting lowered from `live` since the claim, stops the run (`skipped`), because the run's
mode was chosen before the lease. The halt switch is the agent's own check: a halted run is recorded as
`halted` before any model request. Without the model key (`VENDING_ANTHROPIC_API_KEY`) the entry point
records the time as `failed` (`MODEL_KEY_MISSING`) and never starts the agent; `/health` shows `model_key`,
and the dashboard says so before the time comes.

The worker's loop asks `Scheduler.next_due` when the next run is due and calls `Scheduler.tick`
then, as it does for the visit reads (`vending.visit_polls`). The run itself is the agent's own
entry point, `vending.agent.run`, in a background thread, so the hourly syncs go on meanwhile.

**Off by default, twice.** The worker runs the agent only when both of these say so:

- `AGENT_SCHEDULE_MODE` (environment, or `.env`): `off` (unset is off), `dry-run` or `live`. Unset, the
  worker builds no scheduler at all and never touches `agent_schedule`.
- `agent_schedule_mode` in the database's `config` table: the same three values; no row, or a value
  that isn't one of them, is off. Set it with `python -m vending.config set agent_schedule_mode dry-run --by NAME`
  (a bare word works in every shell, Windows PowerShell 5.1 too). The lower of the two is what runs. It is read by the same statement
  that claims a scheduled time, and read again in the transaction that takes the run lease, with its
  row locked, before anything is paid for. So a change reaches every replica at once, an old one left
  over from a deploy too. Turning it off is how to stop the runs: a run that finds it off when it would
  start doesn't start, and an `off` committed while a run is being started waits for that start to be
  recorded (`started_at`), so it is ordered after it.

A dry run is test spend, under `model_test_daily_cap_cents`; a live run is the competition's money, in the
ledger, under `model_daily_cap_cents` (`docs/agent.md`).

Other settings (environment, or `.env`):

- `AGENT_SCHEDULE`: when, in Eastern time, as `day HH:MM-HH:MM`, comma-separated. The first time
  is when the run is due; the second is the latest it may start. Unset: `wed 14:00-22:00`, one
  restock run each Wednesday afternoon, so its order beats Amazon's Wed 23:59 cutoff (#188). A
  placeholder until #129 decides.
- `AGENT_SCHEDULE_FROM`: the first Eastern day with runs. Unset: 2026-12-02 (Wed 2 Dec).

Each time in the schedule gets one row in `agent_schedule` (`db/migrations/024_agent_schedule.sql`),
keyed by the time, so it runs at most once whatever restarts or replicas there are:

- The worker inserts the row before it starts the agent, and records the agent's ending
  (`submitted`, live runs only; `skipped`, `halted`, `failed`) and its run id when the agent returns. If that
  write fails, the worker tries it again every minute until it lands.
- A time the database's mode turns off is recorded as `skipped`, with mode `off`.
- A time the worker never reached before its latest start (it was down, or the schedule was off)
  is recorded as `missed` once a worker runs again. Which times are due, and whether one is past its
  latest start, is decided by the database's clock (`clock_timestamp()`), read after the locks are
  taken: just before the claim's insert, and again in the transaction that takes the run lease. So a
  worker whose clock runs slow or fast never starts a run early or past its latest start: past it,
  the time is `missed`. One reached late but before its latest start runs late, and its detail says
  so. Only the last `LOOKBACK` (7 days) is looked at.
- A time whose window the clocks skip in March (`sun 02:30-03:00`, say) has no valid start that
  day. It is recorded as `missed`, and its detail says the clocks went forward (`CLOCKS_SKIP`).
- Never two runs at once: a worker holds a database lock (`LOCK`) while it claims a time and for
  the whole run, and the agent holds the run lease (`vending.run_lease`) for every run, by hand
  too. A scheduled run that finds another agent run going is recorded as `skipped`, and the detail
  names the other run. A run has a deadline (`vending.agent.RUN_DEADLINE`).
- A run that hasn't ended `STUCK_AFTER` its start is let go: its row is recorded as `failed`
  (`ABANDONED`) and the worker's lock is given back, so the schedule goes on. `next_due` wakes the
  worker's loop for that deadline. If its thread ends later, it doesn't overwrite that. A thread that
  can't be started is a `failed` row too. Every database statement of the scheduler has `DB_TIMEOUT`
  (statement and lock). When a run's thread ends, it wakes the worker's loop (`Scheduler.run_ended`), so a
  second time due that day, before the next sync, isn't missed.
- A row left `running` by a worker that stopped mid-run is marked `failed` by the next worker that
  takes the lock, once no agent run holds the run lease. While a lease holds it, `next_due` wakes the worker
  just after the lease expires (`SWEEP_MARGIN`) to try again. It is never run again.
- Two entries with the same start are one time, with the later latest start.

`/health` shows what this worker runs at most (its `AGENT_SCHEDULE_MODE`), whether it has the model key,
the next time and the last scheduled run's outcome (never its detail, which can hold an
error). The dashboard reads the `agent_schedule` table itself, so turning the schedule off never hides a
failed or missed run. A row whose agent run still says `running` in `agent_runs` after the run returned
(its ending couldn't be recorded) stays in Needs attention, however old, until `agent_runs` has an ending.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from vending import settings

EASTERN = ZoneInfo("America/New_York")
MODE_SETTING, TIMES_SETTING, FROM_SETTING = "AGENT_SCHEDULE_MODE", "AGENT_SCHEDULE", "AGENT_SCHEDULE_FROM"
# The `config` key that must also turn the runs on (`vending.config.KNOWN`).
DB_MODE_KEY = "agent_schedule_mode"
# Placeholders until #129 decides: Wednesday 2 PM Eastern, so a run's order beats Amazon's Wed 23:59
# cutoff (#188), and late until 10 PM, which still leaves time before it.
DEFAULT_TIMES = "wed 14:00-22:00"
DEFAULT_FROM = "2026-12-02"
MODES = ("dry-run", "live")
RANK = {"off": 0, "dry-run": 1, "live": 2}
# The most a scheduled run may be: live, when both settings say so and the checks before a send pass.
CEILING = "live"
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
# A dry run's endings. Anything else the agent returns (a dry run can't submit) is recorded as failed.
ENDINGS = ("skipped", "halted", "failed")
# A live run's: it may also have submitted (`vending.agent.run` sends at most one purchase request).
LIVE_ENDINGS = ("submitted", *ENDINGS)
# The worker's `slots check` runs every hourly round. An assignment confirmed longer ago than this means the
# check has been failing (the server's answer changed, or the bridge is down), so a live run waits for a person.
SLOTS_FRESH = timedelta(hours=3)
# What /health and the dashboard say about live runs.
LIVE_ON = ("Scheduled agent runs are live: both `AGENT_SCHEDULE_MODE` and `agent_schedule_mode` say live, so each "
           "scheduled run may send a restock request. Turn it off with "
           "`python -m vending.config set agent_schedule_mode off --by NAME`; it applies from the next scheduled time.")
HALF_LIVE = ("Scheduled agent runs are dry runs: only one of `AGENT_SCHEDULE_MODE` (the container) and "
             "`agent_schedule_mode` (the database) says live, and a run is live only when both do.")
NO_KEY_LINE = ("Scheduled agent runs have no model key: `VENDING_ANTHROPIC_API_KEY` isn't set on the container, so "
               "each scheduled run will fail before any model request. Deploy with `-SetSecrets` (docs/deploy.md).")
# A time's detail when the entry point had no model key: the agent never started.
MODEL_KEY_MISSING = "MODEL KEY MISSING: "
# A time claimed as live that a check turned into a dry run: why, at the start of its detail.
DOWNGRADED = "Run as a dry run, not live: {why} "
# A time claimed as live whose check failed in the transaction that takes the run lease: nothing ran.
NOT_LIVE = "Not started: this live run's check before a send failed as it was about to start: {why}"
BEFORE_START = ("the competition starts {start} (vending.start.START), and a scheduled run is never live before "
                "it.")
ORDERS_BLOCKED = ("ORDERS BLOCKED: purchase submission {rows} is unresolved, so the bridge may already have a "
                  "request. A person settles it with python -m vending.submissions resolve.")
SLOTS_UNREADY = "our slots can't be read ({error})."
SLOTS_STALE = ("our slot assignment was last confirmed with the inventory server {age} ago, more than {limit}, so "
               "the worker's slots check has been failing. A person checks with python -m vending.slots check.")
# Seconds any one of the scheduler's database statements may take, or wait for a lock.
DB_TIMEOUT = 30.0
# `wed 14:00-22:00`: a day (3 letters or more of its name), then two 24-hour times. Nothing else.
ENTRY = re.compile(r"([a-z]{3,})\s+(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", re.IGNORECASE)
# How far back a restarted worker looks for times it missed.
LOOKBACK = timedelta(days=7)
# A run started this long after its time is "late" in its detail.
LATE_AFTER = timedelta(minutes=10)
# The wait before trying again after the database failed or another worker held the lock.
RETRY = timedelta(minutes=1)
# After a row left `running` under a live run lease, how long past the lease's expiry to come back and sweep it.
SWEEP_MARGIN = timedelta(seconds=30)
# A row still `running` this long after it was claimed is stuck: past the agent's deadline
# (`vending.agent.RUN_DEADLINE`, 1 hour) and the most its run lease can last past it (`run_lease.GRACE`).
# The worker lets go of a run that hasn't ended by then. A test keeps them in step.
STUCK_AFTER = timedelta(hours=1, minutes=15)
# The session advisory lock a worker holds while it claims a time and runs it. Not the migration
# lock; the agent's own guard is the run lease (`vending.run_lease`).
LOCK = 4_200_213
MISSED = ("No run started between its time and its latest start ({latest}): "
          "the worker was down or the schedule was off.")
SUPERSEDED = "Not run: a later time in the schedule was already due when the worker reached this one."
STOPPED = "The worker stopped before this run ended, so its outcome wasn't recorded here; see agent_runs."
OFF_IN_DB = (f"Not started: {DB_MODE_KEY} in the database is off or unset. "
             f"Set it with python -m vending.config set {DB_MODE_KEY} to run.")
CLOCKS_SKIP = ("No run: the clocks went forward an hour on {day}, so this time's window didn't exist that day "
               "and the run had no valid start.")
CHANGED = f"Not started: {DB_MODE_KEY} changed to {{mode}} after this time was claimed, so nothing ran."
NOT_CLAIMED = ("Not started: this time's row no longer said running when the run would have started "
               "(another worker recorded it meanwhile).")
LATE_AT_START = ("No run started: this time was claimed, but its latest start ({latest}) passed before the run "
                 "could begin.")
ABANDONED = ("The run hadn't ended {after} after it started, so the worker let it go and went on. "
             "Its own record, if it has one, is in agent_runs.").format(after="1 h 15 min")
LAUNCH_FAILED = "Not started: the worker couldn't start the run's thread ({error})."
INVALID_IN_DB = (f"Scheduled agent runs are off: `{DB_MODE_KEY}` in the database isn't \"off\", \"dry-run\" or "
                 f"\"live\", so the worker treats it as off. Set it with `python -m vending.config set {DB_MODE_KEY} off --by NAME`.")


@dataclass(frozen=True)
class Entry:
    weekday: int        # Monday is 0
    due: time           # Eastern
    latest: time        # Eastern, the same day, after `due`


@dataclass(frozen=True)
class Schedule:
    mode: str           # one of MODES: this worker's AGENT_SCHEDULE_MODE, the most it may run
    entries: tuple[Entry, ...]
    first_day: date     # Eastern


def _entry(part: str) -> Entry:
    """One `day HH:MM-HH:MM`; ValueError for anything else."""
    found = ENTRY.fullmatch(part)
    if found is None:
        raise ValueError
    word, h1, m1, h2, m2 = found.groups()
    days = [i for i, name in enumerate(DAYS) if name.startswith(word.lower())]
    if len(days) != 1:
        raise ValueError
    start, end = time(int(h1), int(m1)), time(int(h2), int(m2))   # ValueError past 23:59
    if end <= start:
        raise ValueError
    return Entry(days[0], start, end)


def schedule() -> Schedule | None:
    """The schedule from the settings, or None when it is off (the mode unset or `off`).
    `settings.SettingsError` for a value that isn't one."""
    mode = (settings.get(MODE_SETTING) or "off").strip().lower()
    if mode == "off":
        return None
    if mode not in MODES:
        raise settings.SettingsError(f"{MODE_SETTING} is {mode!r}, not off, dry-run or live")
    value = settings.get(TIMES_SETTING, DEFAULT_TIMES)
    entries = []
    for part in value.split(","):
        if not part.strip():
            continue
        try:
            entries.append(_entry(part.strip()))
        except ValueError:
            raise settings.SettingsError(
                f"{TIMES_SETTING} has {part.strip()!r}, not a day and two times like 'wed 14:00-22:00' "
                "(Eastern, 24-hour, no offset; the second, the latest start, after the first, the same day)") from None
    if not entries:
        raise settings.SettingsError(f"{TIMES_SETTING} lists no times")
    first = settings.get(FROM_SETTING, DEFAULT_FROM).strip()
    try:
        first_day = date.fromisoformat(first)
    except ValueError:
        raise settings.SettingsError(f"{FROM_SETTING} is {first!r}, not a date like 2026-12-02") from None
    return Schedule(mode, tuple(entries), first_day)


def valid_database_mode(value) -> bool:
    """Whether a stored `agent_schedule_mode` is one of the three words. Any other JSON (a list, a number) isn't."""
    return isinstance(value, str) and value in RANK


def database_mode(conn, *, lock: bool = False) -> str:
    """`agent_schedule_mode` from `config`: off when the row is missing or holds anything else. With `lock`,
    inside a transaction, the row is read `for share`: a change to it waits until that transaction ends."""
    from vending.config import get_value

    if lock:
        row = conn.execute("select value from config where key = %s for share", (DB_MODE_KEY,)).fetchone()
        value = None if row is None else row[0]
    else:
        value = get_value(conn, DB_MODE_KEY)
    return value if valid_database_mode(value) else "off"


def database_now(conn) -> datetime:
    """The database's wall clock at this moment (`clock_timestamp()`, not the transaction's start)."""
    return conn.execute("select clock_timestamp()").fetchone()[0]


def lower(a: str, b: str) -> str:
    """The lower of two modes: `off` < `dry-run` < `live`."""
    return min(a, b, key=lambda m: RANK.get(m, 0))


def _instant(day: date, clock: time) -> datetime:
    """An Eastern wall-clock time on `day`, in UTC. `fold=0`: a time the clocks skip in March is read
    with the offset before the change (2:30 AM is 3:30 AM EDT), and one they repeat in November is the
    first of the two, so each time in the schedule is one instant, the same every time it is worked out."""
    return datetime.combine(day, clock, EASTERN).astimezone(timezone.utc)


def times_between(sched: Schedule, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """(due, latest start) of each time in the schedule due from `start` to `end`, both included, oldest first.
    Entries that start at the same instant (`wed 14:00-14:30, wed 14:00-15:00`) are one time, with the later
    latest start: a time has one row, so two would record each other as missed."""
    day = max(start.astimezone(EASTERN).date() - timedelta(days=1), sched.first_day)
    last = end.astimezone(EASTERN).date() + timedelta(days=1)
    found: dict[datetime, datetime] = {}
    while day <= last:
        for e in sched.entries:
            if day.weekday() == e.weekday:
                due = _instant(day, e.due)
                if start <= due <= end:
                    found[due] = max(found.get(due, due), _instant(day, e.latest))
        day += timedelta(days=1)
    return sorted(found.items())


def due_times(sched: Schedule, now: datetime) -> list[tuple[datetime, datetime]]:
    """The times due in the last `LOOKBACK`, up to `now`, oldest first."""
    return times_between(sched, now - LOOKBACK, now)


def next_time(sched: Schedule, now: datetime) -> datetime:
    """The first time in the schedule after `now`."""
    start = max(now + timedelta(microseconds=1), _instant(sched.first_day, time.min))
    while True:     # every entry falls once a week, so the first 8 days hold one
        found = times_between(sched, start, start + timedelta(days=8))
        if found:
            return found[0][0]
        start += timedelta(days=8)      # unreachable with any entry; kept so a bug can't hang the loop for ever


def model_key_present() -> bool:
    """Whether the team's model key is set where `vending.agent.model_client` reads it (never its value)."""
    from vending.harness import DEFAULT_KEY_ENV

    return bool((settings.get(DEFAULT_KEY_ENV) or "").strip())


def live_blocker(conn, now: datetime) -> str | None:
    """Why a run claimed as live must not send now, or None. What a person checks before pressing send that the
    agent's own run doesn't: the competition has started, no purchase submission is open, and our slot
    assignment is readable and was confirmed by the worker's `slots check` within `SLOTS_FRESH`. `now` is the
    database's clock. The halt switch and the model key are checked by the agent and the entry point."""
    from vending import slots, submissions
    from vending.start import START

    if now < START:
        return BEFORE_START.format(start=_eastern(START))
    open_rows = submissions.open_rows(conn)
    if open_rows:
        return ORDERS_BLOCKED.format(rows=", ".join(str(row[0]) for row in open_rows))
    try:
        slots.our_slots(conn, now)
    except slots.SlotsNotRecorded as error:
        return SLOTS_UNREADY.format(error=str(error).rstrip("."))
    age = now - slots.stored(conn).last_checked_at
    if age > SLOTS_FRESH:
        return SLOTS_STALE.format(age=_span(age), limit=_span(SLOTS_FRESH))
    return None


def default_run_agent(live: bool, authorize: Callable[[Any], str | None] | None = None) -> tuple[int | None, Any]:
    """The agent's own entry point, as `python -m vending.agent [--live]` runs it: (run id, outcome).
    `authorize` runs in the transaction that takes the run lease, before anything is paid for
    (`vending.agent.run`). Without the model key it returns a `failed` outcome and never starts the agent:
    nothing is recorded in `agent_runs` and no request is made."""
    import anyio

    from vending import agent

    if not model_key_present():
        from vending.harness import DEFAULT_KEY_ENV

        return None, agent.Outcome("failed", f"{MODEL_KEY_MISSING}{DEFAULT_KEY_ENV} isn't set on this worker, so the "
                                             "run didn't start and no model request was made. Deploy with "
                                             "-SetSecrets (docs/deploy.md).")
    return anyio.run(lambda: agent.run(live=live, authorize=authorize))


def _eastern(moment: datetime) -> str:
    local = moment.astimezone(EASTERN)
    return f"{local:%a %d %b} {local.hour % 12 or 12}:{local:%M %p} ET"


def _span(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    return f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"


LAST_COLUMNS = "due_at, latest_at, mode, claimed_at, finished_at, outcome, run_id"


def last_run(conn) -> dict | None:
    """The latest time in `agent_schedule`, without its detail: what `/health` may show."""
    row = conn.execute(f"select {LAST_COLUMNS} from agent_schedule order by due_at desc limit 1").fetchone()
    if row is None:
        return None
    out = dict(zip([c.strip() for c in LAST_COLUMNS.split(",")], row))
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in out.items()}


# A claim, in one statement: the lower of this worker's mode, the database's and `CEILING` (live), read by
# the statement that inserts the row, so a change committed before it is what runs. Off is `skipped`; past the
# latest start, by the database's clock read just before, `missed`.
CLAIM_SQL = """
with chosen as (
    select (array['off', 'dry-run', 'live'])[1 + least(%(rank)s, %(ceiling)s, coalesce(
        (select case when value = '"live"'::jsonb then 2 when value = '"dry-run"'::jsonb then 1 else 0 end
         from config where key = %(key)s), 0))] as mode)
insert into agent_schedule (due_at, latest_at, mode, outcome, detail, finished_at)
select %(due)s, %(latest)s, case when %(past)s then %(cap)s else mode end,
       case when %(past)s then 'missed' when mode = 'off' then 'skipped' else 'running' end,
       case when %(past)s then %(missed)s when mode = 'off' then %(off)s end,
       case when %(past)s or mode = 'off' then now() end
from chosen
on conflict (due_at) do nothing
returning mode, outcome"""


def record(conn, due: datetime, latest: datetime, mode: str, outcome: str, detail: str | None = None, *,
           clock: Callable[[], datetime] | None = None) -> tuple[str, str] | None:
    """Insert a row for the time `due`: the (mode, outcome) stored, or None when it already has one.

    `running` is a claim, and `mode` is then this worker's own mode, the most it may run. The row gets
    the lower of that, `CEILING` and the database's `agent_schedule_mode`, read by the insert itself;
    `skipped` with mode `off` when that is off, and `missed` when `clock()` (the scheduler passes the
    database's clock), read just before, is past `latest`. A row that isn't a claim is stored with
    `mode` capped at `CEILING`."""
    if outcome != "running":
        row = conn.execute(
            "insert into agent_schedule (due_at, latest_at, mode, outcome, detail, finished_at)"
            " values (%s, %s, %s, %s, %s, now()) on conflict (due_at) do nothing returning mode, outcome",
            (due, latest, lower(mode, CEILING), outcome, detail)).fetchone()
        return None if row is None else tuple(row)
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    row = conn.execute(CLAIM_SQL, {"rank": RANK[mode], "ceiling": RANK[CEILING], "key": DB_MODE_KEY, "due": due,
                                   "latest": latest, "past": now >= latest, "cap": lower(mode, CEILING),
                                   "off": OFF_IN_DB,
                                   "missed": MISSED.format(latest=_eastern(latest))}).fetchone()
    return None if row is None else tuple(row)


def finish(conn, due: datetime, outcome: str, detail: str | None, run_id: int | None) -> None:
    """Record how the run for `due` ended. Overwrites a `failed` a sweep wrote meanwhile: this is what happened."""
    conn.execute("update agent_schedule set outcome = %s, detail = %s, run_id = %s, finished_at = now()"
                 " where due_at = %s", (outcome, detail, run_id, due))


@dataclass(frozen=True)
class Claim:
    due: datetime
    latest: datetime
    mode: str           # what runs: `live` or `dry-run` (a live claim a check turned into a dry run is `dry-run`)
    note: str           # goes before the agent's detail: "Started 6 h 0 min late. ", or why it isn't live


class Running:
    """A run in its thread, and the connection that holds `LOCK` for it. Whichever ends it first, the
    thread or the worker letting it go, records its ending; the other doesn't."""

    def __init__(self, claim: Claim, conn, started: datetime):
        self.claim, self.conn, self.started = claim, conn, started
        self.thread = None
        self.ended = False          # its ending is decided: by the thread, or by the worker letting it go
        self.abandoned = False
        self.over = False           # its thread has given everything back and is about to end
        self._guard = threading.Lock()
        self._released = False

    def end(self, *, abandon: bool) -> bool:
        """Claim the right to record the ending; False when the other side already did."""
        with self._guard:
            if self.ended:
                return False
            self.ended, self.abandoned = True, abandon
            return True

    def let_go(self) -> None:
        """Give back `LOCK` and close its connection, once."""
        with self._guard:
            if self._released:
                return
            self._released = True
        _let_go(self.conn)


def _let_go(conn) -> None:
    try:
        conn.execute("select pg_advisory_unlock(%s)", (LOCK,))
    except Exception:       # noqa: BLE001 - a dropped connection has released the lock already
        pass
    try:
        conn.close()
    except Exception:       # noqa: BLE001
        pass


@dataclass(frozen=True)
class Ending:
    """A run's ending not yet written to its row."""
    due: datetime
    outcome: str
    detail: str | None
    run_id: int | None


class Scheduler:
    """Decides when a scheduled run is due and starts it. The worker's loop asks `next_due` and calls `tick`."""

    def __init__(self, sched: Schedule, run_agent: Callable[..., tuple[int | None, Any]] = default_run_agent,
                 connect: Callable[[], Any] | None = None, log: Callable[[str], None] = print,
                 background: bool = True, clock: Callable[[], datetime] | None = None,
                 db_clock: Callable[[Any], datetime] | None = None):
        self.sched, self.run_agent, self.log, self.background = sched, run_agent, log, background
        self._connect = connect
        # The worker's clock: when to wake, and how long a run has gone. Never what decides a start.
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        # The database's clock, read on a connection after the locks: which times are due, and whether one is
        # past its latest start. A test may pass its own.
        self.db_clock = db_clock or database_now
        self.thread: threading.Thread | None = None
        self.running: Running | None = None
        self.thread_factory = threading.Thread
        self.not_before: datetime | None = None
        self.known: set[datetime] = set()       # times that have a row
        self.loaded = False                     # whether the rows were read since the worker started
        self.last: dict | None = None
        self.unfinished: Ending | None = None   # a run's ending whose write failed: written before anything else
        # When to come back and sweep a row left `running` while an agent run held the run lease (by this
        # worker's clock), or None when there is no such row.
        self.sweep_at: datetime | None = None
        # Set when a run's thread ends, so the worker's loop wakes and asks `next_due` again: a second time that
        # day may be due before the next sync. The loop clears it.
        self.run_ended = threading.Event()

    @property
    def mode(self) -> str:
        """The most this worker runs: its AGENT_SCHEDULE_MODE (`CEILING` is live, so it is the setting itself)."""
        return lower(self.sched.mode, CEILING)

    def connect(self):
        """A connection with `DB_TIMEOUT` on every statement and lock wait."""
        from vending import db

        return db.with_timeouts(self._connect or db.connect, DB_TIMEOUT)()

    def next_due(self, now: datetime) -> datetime:
        """When the worker's loop should call `tick` next. While a run goes on, its supervisor's deadline (just
        past `STUCK_AFTER`), so a stuck run is let go; else now when a time is due with no row yet (or the rows
        haven't been read, or an ending is still to be written); else the next time, or sooner the expiry of a run
        lease that kept a row left `running` from being swept."""
        if self.not_before is not None and now < self.not_before:
            return self.not_before
        if self._going():
            # Already past it (a round of the worker ran long): now, never a time in the past.
            return max(now, self.running.started + STUCK_AFTER + timedelta(seconds=1))
        if self.unfinished is not None or not self.loaded \
                or any(d not in self.known for d, _ in due_times(self.sched, now)):
            return now
        upcoming = next_time(self.sched, now)
        if self.sweep_at is not None:
            return max(now, min(upcoming, self.sweep_at))
        return upcoming

    def public(self) -> dict:
        """What `/health` shows: the most this worker runs, its setting, whether it has the model key (never the
        key), the next time, and the last scheduled run without its detail."""
        return {"mode": self.mode, "setting": self.sched.mode, "model_key": model_key_present(),
                "next_at": next_time(self.sched, self.clock()).isoformat(), "last": self.last}

    def tick(self, now: datetime) -> None:
        """Record what is due, and start the run if one may start. Never raises."""
        if self.not_before is not None and now < self.not_before:
            return
        if self._going():
            if self.running is None or now - self.running.started <= STUCK_AFTER or not self._abandon():
                self.not_before = now + RETRY
                return
        try:
            if self.unfinished is not None:
                self._write_ending(self.unfinished)
                self.unfinished = None
            self._tick()
        except Exception as error:     # noqa: BLE001 - the worker must go on; the next try is a minute away
            from vending.worker import _describe

            self.not_before = now + RETRY
            self.log(f"agent schedule: {_describe(error)}; trying again in a minute")

    def _going(self) -> bool:
        """Whether a run's thread is still going. Not once it has given back the lock: its thread may still be
        alive a moment after waking the loop (`run_ended`), and that must not read as a run still going."""
        return (self.thread is not None and self.thread.is_alive()
                and (self.running is None or not self.running.over))

    def _abandon(self) -> bool:
        """Let go of a run that hasn't ended `STUCK_AFTER` after it started: record it as failed and give
        back the lock, so the schedule goes on. False when the run is ending just now."""
        running = self.running
        if not running.end(abandon=True):
            return False
        running.let_go()
        self.thread, self.running = None, None
        self.log(f"agent schedule: the run due {_eastern(running.claim.due)} hasn't ended after "
                 f"{_span(STUCK_AFTER)}; recorded as failed and let go")
        ending = Ending(running.claim.due, "failed", ABANDONED, None)
        try:
            self._write_ending(ending)
        except Exception:       # noqa: BLE001 - written at the next tick, before anything else
            self.unfinished = ending
        return True

    def _tick(self) -> None:
        from vending import db

        conn = self.connect()
        claim = None
        try:
            behind = db.pending(conn)
            if behind:
                raise RuntimeError(f"the database is missing migrations: {', '.join(behind)}")
            if not conn.execute("select pg_try_advisory_lock(%s)", (LOCK,)).fetchone()[0]:
                self.log("agent schedule: another worker is running a scheduled run; trying again in a minute")
                self.not_before = self.clock() + RETRY
                return
            try:
                claim = self._locked(conn)
            finally:
                if claim is None:
                    conn.execute("select pg_advisory_unlock(%s)", (LOCK,))
        finally:
            if claim is None:
                conn.close()
        if claim is None:
            return
        # This holds the connection, and with it the lock, until the run's thread has started; then the
        # run does (`Running`), and gives both back when it ends.
        running = Running(claim, conn, self.clock())
        if not self.background:
            self._run(running)
            return
        try:
            thread = self.thread_factory(target=self._run, args=(running,), name="agent_schedule", daemon=True)
            thread.start()
        except Exception as error:      # noqa: BLE001 - the claim is spent: record it and give the lock back
            from vending.worker import _describe

            ending = Ending(claim.due, "failed", LAUNCH_FAILED.format(error=_describe(error)), None)
            self.log(f"agent schedule: the run due {_eastern(claim.due)} couldn't start: {_describe(error)}")
            try:
                finish(conn, ending.due, ending.outcome, ending.detail, None)
                self.last = last_run(conn)
            except Exception:       # noqa: BLE001 - written at the next tick
                self.unfinished = ending
            finally:
                _let_go(conn)
            return
        running.thread = thread
        self.thread, self.running = thread, running

    def _locked(self, conn) -> Claim | None:
        """The work under the lock: the time to run now, or None."""
        # Only once no agent run holds the run lease: a run whose worker's connection dropped may still be going.
        swept = conn.execute("update agent_schedule set outcome = 'failed', finished_at = now(), detail = %s"
                             " where outcome = 'running' and not exists (select from agent_run_lease"
                             " where token is not null and expires_at > now()) returning due_at",
                             (STOPPED,)).fetchall()
        for (due,) in swept:
            self.log(f"agent schedule: the run due {_eastern(due)} was cut off when a worker stopped; recorded as failed")
        # A row still `running` now (this worker has no run going: `tick` doesn't come here while one does) waits
        # for the run lease that kept it: come back just after the lease expires, not next week.
        left = conn.execute("select (select max(expires_at) from agent_run_lease where token is not null)"
                            " - clock_timestamp() where exists (select from agent_schedule where outcome = 'running')"
                            ).fetchone()
        if left is None or left[0] is None:
            self.sweep_at = None
        else:
            self.sweep_at = self.clock() + max(left[0], timedelta(0)) + SWEEP_MARGIN
        # The database's clock, read after the locks: a worker's clock that runs slow or fast never decides.
        now = self.db_clock(conn)
        due = due_times(self.sched, now)
        # Due by this worker's clock but not yet by the database's: come back then, never spin.
        ahead = [d for d, _ in due_times(self.sched, self.clock()) if d > now]
        if ahead:
            self.not_before = self.clock() + min(RETRY, ahead[0] - now + timedelta(seconds=1))
        have = {row[0] for row in conn.execute("select due_at from agent_schedule where due_at >= %s",
                                               (now - LOOKBACK - timedelta(days=1),)).fetchall()}
        todo = [(d, latest) for d, latest in due if d not in have]
        runnable = [(d, latest) for d, latest in todo if now < latest]
        newest = runnable[-1] if runnable else None
        for d, latest in todo:
            if (d, latest) == newest:
                continue
            self._miss(conn, d, latest, now)
        claim = self._claim(conn, *newest) if newest is not None else None
        self.known = {d for d, _ in due}
        self.loaded = True
        self.last = last_run(conn)
        return claim

    def _miss(self, conn, due: datetime, latest: datetime, now: datetime) -> None:
        if latest <= due:       # only the clocks' spring change makes a window with no valid start
            detail = CLOCKS_SKIP.format(day=f"{due.astimezone(EASTERN):%a %d %b}")
        else:
            detail = MISSED.format(latest=_eastern(latest)) if now >= latest else SUPERSEDED
        if record(conn, due, latest, self.mode, "missed", detail):
            self.log(f"agent schedule: the run due {_eastern(due)} was missed")

    def _claim(self, conn, due: datetime, latest: datetime) -> Claim | None:
        """Take the time `due` for this worker, or None when it doesn't run now. The insert itself reads the
        database's mode and checks the latest start (`record`)."""
        if latest <= due:
            self._miss(conn, due, latest, self.db_clock(conn))
            return None
        stored = record(conn, due, latest, self.mode, "running", clock=lambda: self.db_clock(conn))
        if stored is None:
            return None         # another worker has it
        mode, outcome = stored
        if outcome == "missed":
            self.log(f"agent schedule: the run due {_eastern(due)} was missed")
            return None
        if outcome == "skipped":
            self.log(f"agent schedule: the run due {_eastern(due)} skipped; {DB_MODE_KEY} is off in the database")
            return None
        now = self.db_clock(conn)
        late = now - due
        note = f"Started {_span(late)} late. " if late >= LATE_AFTER else ""
        if mode == "live" and (why := live_blocker(conn, now)) is not None:
            # Under the schedule's lock, before the run's thread exists: the row says what will run.
            conn.execute("update agent_schedule set mode = 'dry-run' where due_at = %s", (due,))
            mode, note = "dry-run", DOWNGRADED.format(why=why) + note
            self.log(f"agent schedule: the run due {_eastern(due)} runs as a dry run, not live: {why}")
        self.log(f"agent schedule: starting the {mode} run due {_eastern(due)}"
                 + (f", {_span(late)} late" if late >= LATE_AFTER else ""))
        return Claim(due, latest, mode, note)

    def _authorize(self, claim: Claim, refused: dict):
        """What the agent runs in the transaction that takes the run lease (`run_lease.take`), once the lease is
        free and before anything is paid for. It takes its locks first: the database's mode row `for share`,
        and this time's row `for update`. Only then does it read the database's clock, once: so a wait on
        either lock can't carry a start past its latest start. The row must still say `running`, unstarted;
        the latest start must not have passed by that reading; and the mode must not be off. Then it records
        that same reading as the start (`started_at`), in that same transaction. So an `off` committed
        meanwhile either stops the run, or waits until the start is recorded and is ordered after it.

        A live run also needs the database's mode still `live` (it was chosen live before the lease, and can't
        become a dry run now), and the checks before a send (`live_blocker`) to pass again, by that same reading
        of the clock; else nothing runs and the row is `skipped`, with why."""
        def authorize(conn) -> str | None:
            mode = lower(self.mode, database_mode(conn, lock=True))
            mine = conn.execute("select from agent_schedule where due_at = %s and outcome = 'running'"
                                " and started_at is null for update", (claim.due,)).fetchone() is not None
            now = self.db_clock(conn)
            if not mine:
                refused["ending"], refused["keep"] = ("skipped", NOT_CLAIMED), True
            elif now >= claim.latest:
                refused["ending"] = ("missed", LATE_AT_START.format(latest=_eastern(claim.latest)))
            elif mode == "off" or (claim.mode == "live" and mode != "live"):
                refused["ending"] = ("skipped", CHANGED.format(mode=mode))
            elif claim.mode == "live" and (why := live_blocker(conn, now)) is not None:
                refused["ending"] = ("skipped", NOT_LIVE.format(why=why))
            else:
                conn.execute("update agent_schedule set started_at = %s where due_at = %s", (now, claim.due))
            return refused["ending"][1] if "ending" in refused else None
        return authorize

    def _write_ending(self, ending: Ending) -> None:
        with self.connect() as conn:        # a fresh connection: the lock's may have dropped during the run
            finish(conn, ending.due, ending.outcome, ending.detail, ending.run_id)
            self.last = last_run(conn)
        self.log(f"agent schedule: the run due {_eastern(ending.due)} ended {ending.outcome}"
                 + (f" (agent run {ending.run_id})" if ending.run_id is not None else ""))

    def _run(self, running: Running) -> None:
        """Run the agent, then record its ending, unless the worker let the run go first. Gives back the lock."""
        from vending.worker import _describe

        claim = running.claim
        try:
            refused: dict = {}
            try:
                live = claim.mode == "live"
                run_id, out = self.run_agent(live, self._authorize(claim, refused))
                outcome, detail = getattr(out, "status", None), getattr(out, "detail", None)
            except BaseException as error:      # noqa: BLE001 - a thread's error would otherwise be lost
                run_id, outcome, detail = None, "failed", f"The agent's entry point raised {_describe(error)}"
            if not running.end(abandon=False):
                return      # the worker let it go and recorded it already
            if refused.get("keep"):
                self.log(f"agent schedule: the run due {_eastern(claim.due)} wasn't started: {NOT_CLAIMED}")
                return      # another worker recorded the row: leave what it wrote
            if "ending" in refused:
                ending = Ending(claim.due, *refused["ending"], None)
            else:
                if outcome not in (LIVE_ENDINGS if claim.mode == "live" else ENDINGS):
                    outcome, detail = ("failed",
                                       f"The agent ended with an unknown status {outcome!r}. {detail or ''}".strip())
                ending = Ending(claim.due, outcome, (claim.note + (detail or "")).strip() or None, run_id)
            try:
                self._write_ending(ending)
            except Exception as error:      # noqa: BLE001 - kept, and written at the next tick, a minute away
                self.unfinished = ending
                self.not_before = self.clock() + RETRY
                self.log(f"agent schedule: could not record the run due {_eastern(claim.due)}: {_describe(error)}; "
                         "trying again in a minute")
        finally:
            running.let_go()
            running.over = True
            self.run_ended.set()


class Invalid:
    """Stands in for the scheduler when a setting is wrong, so `/health` shows the schedule isn't running."""

    def public(self) -> dict:
        return {"mode": "invalid", "next_at": None, "last": None}


def default_scheduler(log: Callable[[str], None] = print) -> Scheduler | Invalid | None:
    """The scheduler the worker runs: None when the schedule is off, `Invalid` when a setting is wrong
    (logged; it never stops the worker)."""
    try:
        sched = schedule()
        if sched is None:
            return None
        first = next_time(sched, datetime.now(timezone.utc))
    except Exception as error:      # noqa: BLE001 - a wrong setting must never stop the worker
        log(f"scheduled agent runs are off: {error}")
        return Invalid()
    log(f"scheduled agent runs: {MODE_SETTING} is {sched.mode} ({DB_MODE_KEY} in the database must say so too; "
        f"the lower of the two runs), next due {_eastern(first)}"
        + ("" if model_key_present() else ". The model key isn't set, so each run will fail before any request"))
    return Scheduler(sched, log=log)


def main(argv: list[str] | None = None) -> int:
    """`python -m vending.agent_schedule`: the settings as this machine reads them, and the latest rows."""
    import argparse

    from vending import db

    parser = argparse.ArgumentParser(prog="python -m vending.agent_schedule",
                                     description="Show the scheduled agent runs: the settings and the latest rows.")
    parser.add_argument("--rows", type=int, default=10, help="how many rows to show (default 10)")
    args = parser.parse_args(argv)
    try:
        sched = schedule()
    except settings.SettingsError as error:
        print(f"Scheduled runs are off here: {error}")
        sched = None
    else:
        if sched is None:
            print(f"Scheduled runs are off here ({MODE_SETTING} is unset or off).")
        else:
            print(f"{MODE_SETTING} here: {sched.mode}, from {sched.first_day:%a %d %b %Y}; "
                  f"next due {_eastern(next_time(sched, datetime.now(timezone.utc)))}.")
    with db.connect() as conn:
        behind = db.pending(conn)
        if behind:
            print(f"The database is missing migrations ({', '.join(behind)}); run python -m vending.db migrate.")
            return 2
        in_db = database_mode(conn)
        runs = lower(lower(sched.mode, in_db), CEILING) if sched else "off"
        print(f"{DB_MODE_KEY} in the database: {in_db}. A worker runs {runs} (the lower of the two).")
        if runs == "live":
            why = live_blocker(conn, database_now(conn))
            print("A live run may send a restock request." if why is None
                  else f"A run due now would be a dry run, not live: {why}")
        if sched is not None and not model_key_present():
            print("The model key isn't set here, so a run started here would fail before any request.")
        rows = conn.execute("select s.due_at, s.mode, s.outcome, s.run_id, s.detail, r.request_id"
                            " from agent_schedule s left join agent_runs r on r.id = s.run_id"
                            " order by s.due_at desc limit %s", (args.rows,)).fetchall()
    if not rows:
        print("No scheduled run has been recorded yet.")
    for due, mode, outcome, run_id, detail, request_id in rows:
        run = f", agent run {run_id}" if run_id is not None else ""
        sent = f", sent request {request_id}" if request_id else ("" if outcome == "running" else ", nothing sent")
        print(f"{_eastern(due)}  {mode}  {outcome}{run}{sent}" + (f"\n    {detail}" if detail else ""))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
