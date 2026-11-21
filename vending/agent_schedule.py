"""Scheduled agent runs, started by the worker (#213). Off unless two settings both turn them on.

The worker's loop asks `Scheduler.next_due` when the next run is due and calls `Scheduler.tick`
then, as it does for the visit reads (`vending.visit_polls`). The run itself is the agent's own
entry point, `vending.agent.run`, in a background thread, so the hourly syncs go on meanwhile.

**Off by default, twice.** The worker runs the agent only when both of these say so, and it runs
the lower of the two (`off` < `dry-run` < `live`):

- `AGENT_SCHEDULE_MODE` (environment, or `.env`): `off` (unset is off), `dry-run` or `live`. Unset,
  the worker builds no scheduler at all and never touches `agent_schedule`.
- `agent_schedule_mode` in the database's `config` table: the same three values; no row is off.
  Set it with `python -m vending.config set agent_schedule_mode '"dry-run"' --by NAME`. It is
  read under the scheduler's lock at each scheduled time, so a change reaches every replica at
  once, an old one left over from a deploy too. Turning it down is how to stop the runs.

A dry run is test spend, under `model_test_daily_cap_cents`; a live run may submit a purchase
request (`docs/agent.md`).

Other settings (environment, or `.env`):

- `AGENT_SCHEDULE`: when, in Eastern time, as `day HH:MM-HH:MM`, comma-separated. The first time
  is when the run is due; the second is the latest it may start. Unset: `wed 14:00-22:00`, one
  restock run each Wednesday afternoon, so its order beats Amazon's Wed 23:59 cutoff (#188). A
  placeholder until #129 decides.
- `AGENT_SCHEDULE_FROM`: the first Eastern day with runs. Unset: 2026-12-02 (Wed 2 Dec).

Each time in the schedule gets one row in `agent_schedule` (`db/migrations/024_agent_schedule.sql`),
keyed by the time, so it runs at most once whatever restarts or replicas there are:

- The worker inserts the row before it starts the agent, and records the agent's ending
  (`submitted`, `skipped`, `halted`, `failed`) and its run id when the agent returns. If that
  write fails, the worker tries it again every minute until it lands.
- A time the database's mode turns off is recorded as `skipped`, with mode `off`.
- A time the worker never reached before its latest start (it was down, or the schedule was off)
  is recorded as `missed` once a worker runs again. The clock is read again under the lock, so a
  run never starts after its latest start. One reached late but before its latest start runs late,
  and its detail says so. Only the last `LOOKBACK` (7 days) is looked at.
- Never two runs at once: a worker holds a database lock (`LOCK`) while it claims a time and for
  the whole run, and the agent holds its own lock (`vending.agent.RUN_LOCK`) for every run, by
  hand too. A scheduled run that finds another agent run going is recorded as `skipped`, and the
  detail names the other run. A run has a deadline (`vending.agent.RUN_DEADLINE`).
- A row left `running` by a worker that stopped mid-run is marked `failed` by the next worker that
  takes the lock. It is never run again.

`/health` shows this worker's `AGENT_SCHEDULE_MODE`, the next time and the last scheduled run's
outcome (never its detail, which can hold an error). The dashboard reads the `agent_schedule`
table itself, so turning the schedule off never hides a failed or missed run.
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
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
ENDINGS = ("submitted", "skipped", "halted", "failed")
# `wed 14:00-22:00`: a day (3 letters or more of its name), then two 24-hour times. Nothing else.
ENTRY = re.compile(r"([a-z]{3,})\s+(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", re.IGNORECASE)
# How far back a restarted worker looks for times it missed.
LOOKBACK = timedelta(days=7)
# A run started this long after its time is "late" in its detail.
LATE_AFTER = timedelta(minutes=10)
# The wait before trying again after the database failed or another worker held the lock.
RETRY = timedelta(minutes=1)
# A row still `running` this long after it was claimed is stuck: past the agent's deadline
# (`vending.agent.RUN_DEADLINE`, 1 hour) and a last model request (5 minutes). A test keeps them in step.
STUCK_AFTER = timedelta(hours=1, minutes=15)
# The session advisory lock a worker holds while it claims a time and runs it. Not the migration
# lock, and not the agent's own `RUN_LOCK`.
LOCK = 4_200_213
MISSED = ("No run started between its time and its latest start ({latest}): "
          "the worker was down or the schedule was off.")
SUPERSEDED = "Not run: a later time in the schedule was already due when the worker reached this one."
STOPPED = "The worker stopped before this run ended, so its outcome wasn't recorded here; see agent_runs."
OFF_IN_DB = (f"Not started: {DB_MODE_KEY} in the database is off or unset. "
             f"Set it with python -m vending.config set {DB_MODE_KEY} to run.")


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


def database_mode(conn) -> str:
    """`agent_schedule_mode` from `config`: off when the row is missing or holds anything else."""
    from vending.config import get_value

    value = get_value(conn, DB_MODE_KEY)
    return value if value in RANK else "off"


def lower(a: str, b: str) -> str:
    """The lower of two modes: `off` < `dry-run` < `live`."""
    return min(a, b, key=lambda m: RANK.get(m, 0))


def _instant(day: date, clock: time) -> datetime:
    """An Eastern wall-clock time on `day`, in UTC. `fold=0`: a time the clocks skip in March is read
    with the offset before the change (2:30 AM is 3:30 AM EDT), and one they repeat in November is the
    first of the two, so each time in the schedule is one instant, the same every time it is worked out."""
    return datetime.combine(day, clock, EASTERN).astimezone(timezone.utc)


def times_between(sched: Schedule, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """(due, latest start) of each time in the schedule due from `start` to `end`, both included, oldest first."""
    day = max(start.astimezone(EASTERN).date() - timedelta(days=1), sched.first_day)
    last = end.astimezone(EASTERN).date() + timedelta(days=1)
    found = set()
    while day <= last:
        for e in sched.entries:
            if day.weekday() == e.weekday:
                due = _instant(day, e.due)
                if start <= due <= end:
                    found.add((due, max(due, _instant(day, e.latest))))
        day += timedelta(days=1)
    return sorted(found)


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


def default_run_agent(live: bool) -> tuple[int, Any]:
    """The agent's own entry point, as `python -m vending.agent [--live]` runs it: (run id, outcome)."""
    import anyio

    from vending import agent

    return anyio.run(lambda: agent.run(live=live))


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


def record(conn, due: datetime, latest: datetime, mode: str, outcome: str, detail: str | None = None) -> bool:
    """Insert a row for the time `due`; False when it already has one. `running` is a claim."""
    return conn.execute(
        "insert into agent_schedule (due_at, latest_at, mode, outcome, detail, finished_at)"
        " values (%s, %s, %s, %s, %s, case when %s = 'running' then null else now() end)"
        " on conflict (due_at) do nothing returning due_at",
        (due, latest, mode, outcome, detail, outcome)).fetchone() is not None


def finish(conn, due: datetime, outcome: str, detail: str | None, run_id: int | None) -> None:
    """Record how the run for `due` ended. Overwrites a `failed` a sweep wrote meanwhile: this is what happened."""
    conn.execute("update agent_schedule set outcome = %s, detail = %s, run_id = %s, finished_at = now()"
                 " where due_at = %s", (outcome, detail, run_id, due))


@dataclass(frozen=True)
class Claim:
    due: datetime
    live: bool
    note: str           # goes before the agent's detail: "Started 6 h 0 min late. "


@dataclass(frozen=True)
class Ending:
    """A run's ending not yet written to its row."""
    due: datetime
    outcome: str
    detail: str | None
    run_id: int | None


class Scheduler:
    """Decides when a scheduled run is due and starts it. The worker's loop asks `next_due` and calls `tick`."""

    def __init__(self, sched: Schedule, run_agent: Callable[[bool], tuple[int, Any]] = default_run_agent,
                 connect: Callable[[], Any] | None = None, log: Callable[[str], None] = print,
                 background: bool = True, clock: Callable[[], datetime] | None = None):
        self.sched, self.run_agent, self.log, self.background = sched, run_agent, log, background
        self._connect = connect
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.thread: threading.Thread | None = None
        self.not_before: datetime | None = None
        self.known: set[datetime] = set()       # times that have a row
        self.loaded = False                     # whether the rows were read since the worker started
        self.last: dict | None = None
        self.unfinished: Ending | None = None   # a run's ending whose write failed: written before anything else

    def connect(self):
        if self._connect is not None:
            return self._connect()
        from vending import db

        return db.connect()

    def next_due(self, now: datetime) -> datetime:
        """Now when a time is due with no row yet (or the rows haven't been read, or an ending is
        still to be written); else the next time."""
        if self.not_before is not None and now < self.not_before:
            return self.not_before
        if self.unfinished is not None or not self.loaded \
                or any(d not in self.known for d, _ in due_times(self.sched, now)):
            return now
        return next_time(self.sched, now)

    def public(self) -> dict:
        """What `/health` shows: this worker's mode, the next time, and the last scheduled run without its detail."""
        return {"mode": self.sched.mode, "next_at": next_time(self.sched, self.clock()).isoformat(),
                "last": self.last}

    def tick(self, now: datetime) -> None:
        """Record what is due, and start the run if one may start. Never raises."""
        if self.not_before is not None and now < self.not_before:
            return
        if self.thread is not None and self.thread.is_alive():
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
        # The run owns the connection now, and with it the lock: `_run` releases both.
        if self.background:
            self.thread = threading.Thread(target=self._run, args=(conn, claim), name="agent_schedule", daemon=True)
            self.thread.start()
        else:
            self._run(conn, claim)

    def _locked(self, conn) -> Claim | None:
        """The work under the lock: the time to run now, or None."""
        swept = conn.execute("update agent_schedule set outcome = 'failed', finished_at = now(), detail = %s"
                             " where outcome = 'running' returning due_at", (STOPPED,)).fetchall()
        for (due,) in swept:
            self.log(f"agent schedule: the run due {_eastern(due)} was cut off when a worker stopped; recorded as failed")
        now = self.clock()      # read again: taking the lock can take a while
        due = due_times(self.sched, now)
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
        detail = MISSED.format(latest=_eastern(latest)) if now >= latest else SUPERSEDED
        if record(conn, due, latest, self.sched.mode, "missed", detail):
            self.log(f"agent schedule: the run due {_eastern(due)} was missed")

    def _claim(self, conn, due: datetime, latest: datetime) -> Claim | None:
        """Take the time `due` for this worker, or None when it doesn't run now."""
        mode = lower(self.sched.mode, database_mode(conn))
        now = self.clock()          # the last check before the claim: never start past the latest start
        if now >= latest:
            self._miss(conn, due, latest, now)
            return None
        if mode == "off":
            if record(conn, due, latest, "off", "skipped", OFF_IN_DB):
                self.log(f"agent schedule: the run due {_eastern(due)} skipped; {DB_MODE_KEY} is off in the database")
            return None
        if not record(conn, due, latest, mode, "running"):
            return None         # another worker has it
        late = now - due
        self.log(f"agent schedule: starting the {mode} run due {_eastern(due)}"
                 + (f", {_span(late)} late" if late >= LATE_AFTER else ""))
        return Claim(due, mode == "live", f"Started {_span(late)} late. " if late >= LATE_AFTER else "")

    def _write_ending(self, ending: Ending) -> None:
        with self.connect() as conn:        # a fresh connection: the lock's may have dropped during the run
            finish(conn, ending.due, ending.outcome, ending.detail, ending.run_id)
            self.last = last_run(conn)
        self.log(f"agent schedule: the run due {_eastern(ending.due)} ended {ending.outcome}"
                 + (f" (agent run {ending.run_id})" if ending.run_id is not None else ""))

    def _run(self, lock_conn, claim: Claim) -> None:
        """Run the agent, then record its ending. Owns `lock_conn`: releases the lock and closes it."""
        from vending.worker import _describe

        try:
            try:
                run_id, out = self.run_agent(claim.live)
                outcome, detail = getattr(out, "status", None), getattr(out, "detail", None)
            except BaseException as error:      # noqa: BLE001 - a thread's error would otherwise be lost
                run_id, outcome, detail = None, "failed", f"The agent's entry point raised {_describe(error)}"
            if outcome not in ENDINGS:
                outcome, detail = "failed", f"The agent ended with an unknown status {outcome!r}. {detail or ''}".strip()
            ending = Ending(claim.due, outcome, (claim.note + (detail or "")).strip() or None, run_id)
            try:
                self._write_ending(ending)
            except Exception as error:      # noqa: BLE001 - kept, and written at the next tick, a minute away
                self.unfinished = ending
                self.not_before = self.clock() + RETRY
                self.log(f"agent schedule: could not record the run due {_eastern(claim.due)}: {_describe(error)}; "
                         "trying again in a minute")
        finally:
            try:
                lock_conn.execute("select pg_advisory_unlock(%s)", (LOCK,))
                lock_conn.close()
            except Exception:       # noqa: BLE001 - a dropped connection has released the lock already
                pass


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
    log(f"scheduled agent runs: at most {sched.mode} ({DB_MODE_KEY} in the database must say so too), "
        f"next due {_eastern(first)}")
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
        print(f"{DB_MODE_KEY} in the database: {in_db}. "
              f"A worker runs {lower(sched.mode, in_db) if sched else 'off'} (the lower of the two).")
        rows = conn.execute("select due_at, mode, outcome, run_id, detail from agent_schedule"
                            " order by due_at desc limit %s", (args.rows,)).fetchall()
    if not rows:
        print("No scheduled run has been recorded yet.")
    for due, mode, outcome, run_id, detail in rows:
        run = f", agent run {run_id}" if run_id is not None else ""
        print(f"{_eastern(due)}  {mode}  {outcome}{run}" + (f"\n    {detail}" if detail else ""))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
