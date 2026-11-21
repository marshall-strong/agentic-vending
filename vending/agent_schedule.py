"""Scheduled agent runs, started by the worker (#213). Off unless `AGENT_SCHEDULE_MODE` is set.

The worker's loop asks `Scheduler.next_due` when the next run is due and calls `Scheduler.tick`
then, as it does for the visit reads (`vending.visit_polls`). The run itself is the agent's own
entry point, `vending.agent.run`, in a background thread, so the hourly syncs go on meanwhile.

Settings (environment, or `.env`):

- `AGENT_SCHEDULE_MODE`: `off` (unset is off), `dry-run` or `live`. A dry run is test spend, under
  `model_test_daily_cap_cents`; a live run may submit a purchase request (`docs/agent.md`).
- `AGENT_SCHEDULE`: when, in Eastern time, as `day HH:MM-HH:MM`, comma-separated. The first time
  is when the run is due; the second is the latest it may start. Unset: `wed 14:00-22:00`, one
  restock run each Wednesday afternoon, so its order beats Amazon's Wed 23:59 cutoff (#188). A
  placeholder until #129 decides.
- `AGENT_SCHEDULE_FROM`: the first Eastern day with runs. Unset: 2026-12-02 (Wed 2 Dec).

Each time in the schedule gets one row in `agent_schedule` (`db/migrations/022_agent_schedule.sql`),
keyed by the time, so it runs at most once whatever restarts or replicas there are:

- The worker inserts the row before it starts the agent, and records the agent's ending
  (`submitted`, `skipped`, `halted`, `failed`) and its run id when the agent returns.
- A time the worker never reached before its latest start (it was down, or the schedule was off)
  is recorded as `missed` once a worker runs again. One it reaches late but before its latest
  start runs late, and its detail says so. Only the last `LOOKBACK` (7 days) is looked at.
- Never two runs at once: a worker holds a database lock (`LOCK`) for the whole run, and a run
  isn't started while another agent run (`live` or `dry-run`, by hand too) is still running; that
  time is then recorded as `skipped`, and the detail names the other run.
- A row left `running` by a worker that stopped mid-run is marked `failed` by the next worker that
  takes the lock. It is never run again.

`/health` shows the mode, the next time and the last scheduled run's outcome (never its detail,
which can hold an error). The dashboard's Needs attention names a failed, missed or halted one.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from vending import settings

EASTERN = ZoneInfo("America/New_York")
MODE_SETTING, TIMES_SETTING, FROM_SETTING = "AGENT_SCHEDULE_MODE", "AGENT_SCHEDULE", "AGENT_SCHEDULE_FROM"
# Placeholders until #129 decides: Wednesday 2 PM Eastern, so a run's order beats Amazon's Wed 23:59
# cutoff (#188), and late until 10 PM, which still leaves time before it.
DEFAULT_TIMES = "wed 14:00-22:00"
DEFAULT_FROM = "2026-12-02"
MODES = ("dry-run", "live")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
ENDINGS = ("submitted", "skipped", "halted", "failed")
# How far back a restarted worker looks for times it missed.
LOOKBACK = timedelta(days=7)
# A run started this long after its time is "late" in its detail.
LATE_AFTER = timedelta(minutes=10)
# The wait before trying again after the database failed or another worker held the lock.
RETRY = timedelta(minutes=1)
# Another agent run younger than this, still `running`, blocks a scheduled one. An older one died
# with its process (the dashboard calls it stale after the same 2 hours).
BUSY_WINDOW = timedelta(hours=2)
# The session advisory lock a worker holds while it runs a scheduled agent run. Not the migration lock.
LOCK = 4_200_213
MISSED = ("No run started between its time and its latest start ({latest}): "
          "the worker was down or the schedule was off.")
SUPERSEDED = "Not run: a later time in the schedule was already due when the worker reached this one."
STOPPED = "The worker stopped before this run ended, so its outcome wasn't recorded here; see agent_runs."


@dataclass(frozen=True)
class Entry:
    weekday: int        # Monday is 0
    due: time           # Eastern
    latest: time        # Eastern, the same day, after `due`


@dataclass(frozen=True)
class Schedule:
    mode: str           # one of MODES
    entries: tuple[Entry, ...]
    first_day: date     # Eastern

    @property
    def live(self) -> bool:
        return self.mode == "live"


def _entry(part: str) -> Entry:
    words = part.split()
    if len(words) != 2 or words[0][:3].lower() not in DAYS or "-" not in words[1]:
        raise ValueError
    start, end = (time.fromisoformat(t.strip()) for t in words[1].split("-", 1))
    if end <= start:
        raise ValueError
    return Entry(DAYS.index(words[0][:3].lower()), start, end)


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
                "(the second, the latest start, after the first, the same day)") from None
    if not entries:
        raise settings.SettingsError(f"{TIMES_SETTING} lists no times")
    first = settings.get(FROM_SETTING, DEFAULT_FROM).strip()
    try:
        first_day = date.fromisoformat(first)
    except ValueError:
        raise settings.SettingsError(f"{FROM_SETTING} is {first!r}, not a date like 2026-12-02") from None
    return Schedule(mode, tuple(entries), first_day)


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


def other_run(conn) -> int | None:
    """Another agent run (live or dry run) still going, started in the last `BUSY_WINDOW`."""
    row = conn.execute("select id from agent_runs where status = 'running' and mode in ('live', 'dry-run')"
                       " and started_at > now() - %s order by started_at desc, id desc limit 1",
                       (BUSY_WINDOW,)).fetchone()
    return None if row is None else row[0]


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

    def connect(self):
        if self._connect is not None:
            return self._connect()
        from vending import db

        return db.connect()

    def next_due(self, now: datetime) -> datetime:
        """Now when a time is due with no row yet (or the rows haven't been read); else the next time."""
        if self.not_before is not None and now < self.not_before:
            return self.not_before
        if not self.loaded or any(d not in self.known for d, _ in due_times(self.sched, now)):
            return now
        return next_time(self.sched, now)

    def public(self) -> dict:
        """What `/health` shows: the mode, the next time, and the last scheduled run without its detail."""
        return {"mode": self.sched.mode, "next_at": next_time(self.sched, self.clock()).isoformat(),
                "last": self.last}

    def tick(self, now: datetime) -> None:
        """Record what is due at `now`, and start the run if one may start. Never raises."""
        if self.not_before is not None and now < self.not_before:
            return
        if self.thread is not None and self.thread.is_alive():
            self.not_before = now + RETRY
            return
        try:
            self._tick(now)
        except Exception as error:     # noqa: BLE001 - the worker must go on; the next try is a minute away
            from vending.worker import _describe

            self.not_before = now + RETRY
            self.log(f"agent schedule: {_describe(error)}; trying again in a minute")

    def _tick(self, now: datetime) -> None:
        from vending import db

        conn = self.connect()
        start = None
        try:
            behind = db.pending(conn)
            if behind:
                raise RuntimeError(f"the database is missing migrations: {', '.join(behind)}")
            if not conn.execute("select pg_try_advisory_lock(%s)", (LOCK,)).fetchone()[0]:
                self.log("agent schedule: another worker is running a scheduled run; trying again in a minute")
                self.not_before = now + RETRY
                return
            try:
                start = self._locked(conn, now)
            finally:
                if start is None:
                    conn.execute("select pg_advisory_unlock(%s)", (LOCK,))
        finally:
            if start is None:
                conn.close()
        # The run owns the connection now, and with it the lock: `_run` releases both.
        if self.background:
            self.thread = threading.Thread(target=self._run, args=(conn, *start), name="agent_schedule", daemon=True)
            self.thread.start()
        else:
            self._run(conn, *start)

    def _locked(self, conn, now: datetime) -> tuple[datetime, str] | None:
        """The work under the lock: (the time to run now, a note for its detail), or None."""
        swept = conn.execute("update agent_schedule set outcome = 'failed', finished_at = now(), detail = %s"
                             " where outcome = 'running' returning due_at", (STOPPED,)).fetchall()
        for (due,) in swept:
            self.log(f"agent schedule: the run due {_eastern(due)} was cut off when a worker stopped; recorded as failed")
        due = due_times(self.sched, now)
        have = {row[0] for row in conn.execute("select due_at from agent_schedule where due_at >= %s",
                                               (now - LOOKBACK - timedelta(days=1),)).fetchall()}
        todo = [(d, latest) for d, latest in due if d not in have]
        runnable = [(d, latest) for d, latest in todo if now < latest]
        newest = runnable[-1] if runnable else None
        for d, latest in todo:
            if (d, latest) == newest:
                continue
            detail = MISSED.format(latest=_eastern(latest)) if now >= latest else SUPERSEDED
            if record(conn, d, latest, self.sched.mode, "missed", detail):
                self.log(f"agent schedule: the run due {_eastern(d)} was missed")
        start = self._claim(conn, now, *newest) if newest is not None else None
        self.known = {d for d, _ in due}
        self.loaded = True
        self.last = last_run(conn)
        return start

    def _claim(self, conn, now: datetime, due: datetime, latest: datetime) -> tuple[datetime, str] | None:
        """Take the time `due` for this worker: (due, a note for the detail), or None when it can't run now."""
        if not record(conn, due, latest, self.sched.mode, "running"):
            return None         # another worker has it
        busy = other_run(conn)
        if busy is not None:
            finish(conn, due, "skipped", f"Not started: agent run {busy} was still running.", None)
            self.log(f"agent schedule: the run due {_eastern(due)} skipped; agent run {busy} is still running")
            return None
        late = now - due
        self.log(f"agent schedule: starting the {self.sched.mode} run due {_eastern(due)}"
                 + (f", {_span(late)} late" if late >= LATE_AFTER else ""))
        return due, (f"Started {_span(late)} late. " if late >= LATE_AFTER else "")

    def _run(self, lock_conn, due: datetime, note: str) -> None:
        """Run the agent, then record its ending. Owns `lock_conn`: releases the lock and closes it."""
        from vending.worker import _describe

        try:
            try:
                run_id, out = self.run_agent(self.sched.live)
                outcome, detail = getattr(out, "status", None), getattr(out, "detail", None)
            except BaseException as error:      # noqa: BLE001 - a thread's error would otherwise be lost
                run_id, outcome, detail = None, "failed", f"The agent's entry point raised {_describe(error)}"
            if outcome not in ENDINGS:
                outcome, detail = "failed", f"The agent ended with an unknown status {outcome!r}. {detail or ''}".strip()
            with self.connect() as conn:        # a fresh connection: the lock's may have dropped during the run
                finish(conn, due, outcome, (note + (detail or "")).strip() or None, run_id)
                self.last = last_run(conn)
            self.log(f"agent schedule: the run due {_eastern(due)} ended {outcome}"
                     + (f" (agent run {run_id})" if run_id is not None else ""))
        except Exception as error:      # noqa: BLE001 - the row stays running; the next worker's sweep fails it
            self.log(f"agent schedule: could not record the run due {_eastern(due)}: {_describe(error)}")
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
    except settings.SettingsError as error:
        log(f"scheduled agent runs are off: {error}")
        return Invalid()
    if sched is None:
        return None
    log(f"scheduled agent runs: {sched.mode}, next due {_eastern(next_time(sched, datetime.now(timezone.utc)))}")
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
        print(f"Scheduled runs are off: {error}")
        sched = None
    else:
        if sched is None:
            print(f"Scheduled runs are off ({MODE_SETTING} is unset or off).")
        else:
            print(f"Scheduled runs: {sched.mode}, from {sched.first_day:%a %d %b %Y}; "
                  f"next due {_eastern(next_time(sched, datetime.now(timezone.utc)))}.")
    with db.connect() as conn:
        behind = db.pending(conn)
        if behind:
            print(f"The database is missing migrations ({', '.join(behind)}); run python -m vending.db migrate.")
            return 2
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
