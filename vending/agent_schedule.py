"""Scheduled agent runs, started by the worker (#213). Off unless two settings both turn them on.

**Live only when both settings say `live`.** A scheduled run is `vending.agent.run(live=True)` only when the
container's `AGENT_SCHEDULE_MODE` and the database's `agent_schedule_mode` both say `live`; any other pair
is a dry run (`live=False`, no write tool: `vending.agent.WRITE_TOOLS`) or off. A live run goes through the
same entry point as `python -m vending.agent --live`, so every live-run rule holds unchanged: the halt
switch, the caps, the run lease, only the plan its last preview accepted, at most one submit (with #217's
retry rules), the `submission_unresolved` block and the planner's spendable floor (#188).

**Checks before a send (`live_blocker`).** Two kinds. A *lasting* one makes a time claimed as live run as a dry
run instead, with why at the start of its detail: the competition hasn't started (`vending.start.START`, by the
database's clock), or a scheduled run already sent a submission for this week's visit, whatever became of it
(`ALREADY_SENT`: one per visit; a person sends another by hand). A *passing* one doesn't spend the window
(`WAITING`): a purchase submission is still open (`vending.submissions.open_rows`: ORDERS BLOCKED), a purchase
request is still pending (`REQUEST_PENDING`: the planner would refuse `request_pending`), our slot
assignment can't be read (`vending.slots.our_slots`) or the worker's hourly `slots check` last confirmed it more
than `SLOTS_FRESH` ago, or another agent run holds the run lease. The worker then claims nothing and tries again
every `LIVE_RETRY` until the time's latest start; still blocked then, the time is `missed`, mode `live`, with the
cause (`LIVE_MISSED`), and the dashboard shows it as a Problem. Before it calls an open submission something only a
person can settle, it runs the same reconciliation as `python -m vending.submissions check`: a row whose POST got
a validated `201` is looked up on the bridge and settled, since nobody watches an unattended run. A run whose
start finds the lease taken after all (between the claim and the lease) puts its time back the same way.

The same checks run again in the transaction that takes the run lease, and there a failed one, or a database
setting lowered from `live` since the claim, stops the run (`skipped`), because the run's mode was chosen before
the lease. That transaction marks the lease with the run's time (`agent_run_lease.schedule_due`). The agent hands
that time and its own lease token to the MCP server it starts (`vending.tools.submit.RunContext`), and the submit
tool checks it all once more at the send: it takes its locks first (the lease, the database switch, this time's
row), then, once the bridge connection is open, reads the database's clock and checks the run's own lease, both
switches, the row, and `live_blocker` (`send_blocker`). A failed one there sends nothing (the attempt is
`not_filed`, the submit refused `lease_lost` or `schedule_not_live`). A scheduled run never buys an off-cycle
visit (`scheduled_off_cycle`). The halt switch is the agent's own check: a halted run is recorded as
`halted` before any model request. Without the model key (`VENDING_ANTHROPIC_API_KEY`) the entry point
records the time as `failed` (`MODEL_KEY_MISSING`) and never starts the agent; `/health` shows `model_key`,
and the dashboard says so before the time comes.

The worker's loop asks `Scheduler.next_due` when the next run is due and calls `Scheduler.tick`
then, as it does for the visit reads (`vending.visit_polls`). The run itself is the agent's own
entry point, `vending.agent.run`, in a background thread, so the hourly syncs go on meanwhile.

**Off by default, twice.** The worker runs the agent only when both of these say so:

- `AGENT_SCHEDULE_MODE` (environment, or `.env`): `off` (unset is off), `dry-run` or `live`. Unset, the
  worker builds no scheduler at all and never touches `agent_schedule`. Anything else is a dry run, never live
  and never off, and `/health` and the dashboard show it as a Problem (`GARBLED_MODE`).
- `agent_schedule_mode` in the database's `config` table: the same three values; no row is off, and a value
  that isn't one of them is a dry run, shown as a Problem (`INVALID_IN_DB`). Set it with `python -m vending.config set agent_schedule_mode dry-run --by NAME`
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
  is when the run is due; the second is the latest it may start. Unset: `wed 14:00-22:00,thu 12:00-20:00`
  (Kevin, 23 Nov): a Wednesday run whose order beats Amazon's Wed 23:59 cutoff (#188), and a Thursday run
  for the week its Wednesday didn't send (one per visit). Wed 2 Dec is before the start, so a dry run; the
  first live order is Thu 3 Dec 12:00 ET's, for the Fri 4 Dec visit.
- `AGENT_SCHEDULE_FROM`: the first Eastern day with runs. Unset: 2026-12-02 (Wed 2 Dec).

Each time in the schedule gets one row in `agent_schedule` (`db/migrations/024_agent_schedule.sql`),
keyed by the time, so it runs at most once whatever restarts or replicas there are:

- The worker inserts the row before it starts the agent, and records the agent's ending
  (`submitted`, live runs only; `skipped`, `halted`, `failed`) and its run id when the agent returns. If that
  write fails, the worker tries it again every minute until it lands. The agent names its run on the row as it
  starts (`run_lease.attach`), so the row links its run, and through it what it sent, even before then.
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

**Midweek runs (#219).** A wake-up the midweek check recorded and requested (`vending.midweek_wake`,
`midweek_wakeups.requested_at`) is one more time to run, with kind `midweek`: due at the request, latest start
the end of that Eastern day (`midweek_latest`). The scheduler claims it as it claims a weekly time, under the same
lock, switches, checks before a send, run lease and caps, and records it in `agent_schedule` with `kind` midweek and
its `wakeup_id` (migration 037). One row per wake-up, and the wake-ups' own index holds one a week. The agent's
opening says why it was woken (`midweek_occasion`). A midweek run buys only an off-cycle visit
(`midweek_on_cycle`), and a weekly run never does (`scheduled_off_cycle`). Each kind sends at most one submission a
week (`ALREADY_SENT`, `MIDWEEK_ALREADY_SENT`). A weekly time due at the same moment goes first. The worker calls
`Scheduler.poke` after each round, so a request recorded in that round is claimed in that same pause.

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
# What a row in `agent_schedule` is for: a time in AGENT_SCHEDULE, or a midweek wake-up's request (migration 037).
WEEKLY, MIDWEEK = "weekly", "midweek"
# Kevin, 23 Nov: Wednesday 2 PM Eastern, so a run's order beats Amazon's Wed 23:59 cutoff (#188), late until 10 PM;
# and Thursday noon, late until 8 PM, for a week whose Wednesday didn't send. Thu 3 Dec 12:00 is the first live order.
DEFAULT_TIMES = "wed 14:00-22:00,thu 12:00-20:00"
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
REQUEST_PENDING = ("purchase request {requests} is still pending (filed, not yet fulfilled or rejected), and only one "
                   "request may be open at a time, so the planner would refuse a new one (request_pending).")
# A midweek run's send after the wake-up's day: its forecast planned the landing from that day (#249 review).
MIDWEEK_EXPIRED = ("this midweek run's wake-up planned a visit requested by {latest}, and that has passed, so its "
                   "forecast no longer holds; the next wake check decides again.")
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
INVALID_IN_DB = (f"Scheduled agent runs are dry runs only: `{DB_MODE_KEY}` in the database isn't \"off\", \"dry-run\" "
                 f"or \"live\", so the worker runs each scheduled time as a dry run, never live. Set it with "
                 f"`python -m vending.config set {DB_MODE_KEY} off --by NAME` (or dry-run, or live).")
# AGENT_SCHEDULE_MODE set to something that isn't a mode: never live, never off (a typo must not stop the dry runs
# a person is watching, nor turn on live ones). `/health` and the dashboard show it as a Problem.
GARBLED_MODE = ("Scheduled agent runs are dry runs only: the container's AGENT_SCHEDULE_MODE is {value!r}, not off, "
                "dry-run or live, so the worker runs each scheduled time as a dry run, never live. Fix the setting "
                "and deploy again (docs/deploy.md).")
# One scheduled live submission per week's visit (the week turns over on Saturday, after the Friday visit, as the
# planner's does), whatever became of it: a request the bridge rejected still counts.
ALREADY_SENT = ("a scheduled run already sent submission {row} (due {when}) for this week's visit, and scheduled "
                "runs send at most one per visit, even if that request was rejected. A person who means to send "
                "another does it by hand: python -m vending.agent --live.")
# The midweek visit's own cap: one midweek submission a week, whatever became of it.
MIDWEEK_ALREADY_SENT = ("a midweek run already sent submission {row} (requested {when}) this week, and the wake-ups "
                        "ask for one midweek visit a week. A person who means to send another does it by hand: "
                        "python -m vending.agent --live.")
# What a midweek run's opening says, before the startup read.
MIDWEEK_OCCASION = ("This run was started by the midweek wake check (docs/restock-policy.md, section 2), not by the "
                    "weekly schedule: {reason} Decide whether a paid midweek visit is worth its fee. If it is, "
                    "submit an off-cycle plan (off_cycle true); this run can't send the Friday order. If no visit "
                    "is worth the fee, submit nothing and say why: that is a valid outcome.")
# A live time held back by a passing blocker: tried again this often until its latest start.
LIVE_RETRY = timedelta(minutes=5)
WAITING = ("The scheduled {kind} due {due} is waiting: {why} The worker tries again every {every} until its "
           "latest start ({latest}); if it still can't send then, it is recorded as missed.")
LIVE_MISSED = "LIVE RUN MISSED: it waited from its time to its latest start ({latest}) and couldn't send: {why}"
# The model credit check before a claim (#262), for live and dry runs alike: one free count_tokens call with the
# run's own key and model (`vending.harness.check_credit`). No credit, or no answer, waits like a passing blocker.
CREDIT_WAITING = ("The scheduled {kind} due {due} is waiting on model credit since {since}: {why} The worker checks "
                  "again every {every} until its latest start ({latest}); if there is still none then, it is recorded "
                  "as missed.")
NO_MODEL_CREDIT = ("no model credit: the API says the team's model key has no credit left, so no model request can "
                   "run. The teachers add credit, or a person swaps in a funded key with scripts/swap_model_key.ps1 "
                   "(docs/deploy.md).")
CREDIT_UNCHECKED = ("the model API couldn't be reached to check the key's credit ({error}), so the run's first "
                    "request would fail the same way.")
DRY_MISSED = "DRY RUN MISSED: it waited from its time to its latest start ({latest}) and couldn't start: {why}"
WAITED_FOR_CREDIT = "Started {late} late: it waited on model credit from {since}. "
# A key the API refuses (401 or 403) fails the time at once, as a missing key does: no wait fixes it.
MODEL_KEY_REFUSED_TAG = "MODEL KEY REFUSED: "
MODEL_KEY_REFUSED = (MODEL_KEY_REFUSED_TAG + "{error} when the worker checked its credit, so the run didn't start and "
                     "no model request was made. A person swaps in a working key with scripts/swap_model_key.ps1 "
                     "(docs/deploy.md).")
LEASE_BUSY = "another agent run{run} holds the run lease, so this live run waits for it to end."
# The checks again in the submit tool, just before the POST (`send_blocker`).
SEND_CONTAINER = "the container's AGENT_SCHEDULE_MODE says {mode}, not live."
SEND_DB = f"{DB_MODE_KEY} in the database says {{mode}} now, not live."
SEND_ROW = "this scheduled time's row says {state}, not a live run going."
# The worker couldn't do its scheduled work just now: what /health and the dashboard say.
TICK_PROBLEM = "Scheduled agent runs can't start: {error}. The worker tries again every minute."
BEHIND = ("Scheduled agent runs can't start: the database is missing migrations ({names}); run "
          "python -m vending.db migrate.")


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
    problem: str | None = None      # AGENT_SCHEDULE_MODE wasn't a mode, so this is a dry run (GARBLED_MODE)


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
    """The schedule from the settings, or None when it is off (the mode unset or `off`). A mode that isn't one is a
    dry run, with `problem` saying so (never live, never off). `settings.SettingsError` for times or a first day
    that aren't."""
    raw = settings.get(MODE_SETTING) or "off"
    mode, problem = raw.strip().lower(), None
    if mode == "off":
        return None
    if mode not in MODES:
        mode, problem = "dry-run", GARBLED_MODE.format(value=raw.strip())
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
    return Schedule(mode, tuple(entries), first_day, problem)


def valid_database_mode(value) -> bool:
    """Whether a stored `agent_schedule_mode` is one of the three words. Any other JSON (a list, a number) isn't."""
    return isinstance(value, str) and value in RANK


def database_mode(conn, *, lock: bool = False) -> str:
    """`agent_schedule_mode` from `config`: off when the row is missing, dry-run when it holds anything that isn't
    one of the three words, JSON null included (never live, never off: the dashboard shows it as a Problem). The
    same mapping as the claim's (`CLAIM_SQL`). With `lock`, inside a transaction, the row is read `for share`: a
    change to it waits until that transaction ends."""
    row = conn.execute("select value from config where key = %s" + (" for share" if lock else ""),
                       (DB_MODE_KEY,)).fetchone()
    if row is None:
        return "off"
    return row[0] if valid_database_mode(row[0]) else "dry-run"


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


def visit_week(moment: datetime) -> date:
    """The Saturday the week of `moment` (Eastern) began: one Friday visit, and its store cutoffs, per week."""
    from vending.planner import Config, _week_of

    return _week_of(moment.astimezone(EASTERN), Config.__dataclass_fields__["week_starts"].default)


def live_blocker(conn, now: datetime, *, due: datetime | None = None, except_row: int | None = None,
                 kind: str = WEEKLY) -> str | None:
    """Why a run claimed as live must not send now, or None. What a person checks before pressing send that the
    agent's own run doesn't: the competition has started, no purchase submission is open, our slot
    assignment is readable and was confirmed by the worker's `slots check` within `SLOTS_FRESH`, and no other
    scheduled run sent a submission for the same week's visit as `due` (the scheduled time; `now` when None),
    whatever became of it. `now` is the database's clock. `except_row` leaves out the submission being sent (the
    submit tool's own check, `send_blocker`). The halt switch and the model key are checked by the agent and the
    entry point. `kind` is the run's: a weekly run counts the weekly runs' submissions, a midweek run the midweek
    runs'."""
    return (_before_start(now) or passing_blocker(conn, now, except_row)
            or _already_sent(conn, now, due=due, except_row=except_row, kind=kind))


def lasting_blocker(conn, now: datetime, *, due: datetime | None = None, kind: str = WEEKLY) -> str | None:
    """The checks before a send that no wait inside the window can change: the competition hasn't started, or a
    scheduled run already sent this week's submission. A time that fails one runs as a dry run."""
    return _before_start(now) or _already_sent(conn, now, due=due, kind=kind)


def passing_blocker(conn, now: datetime, except_row: int | None = None) -> str | None:
    """The checks before a send that may clear inside the window: an open submission, a purchase request still
    pending (the planner refuses a plan then, `request_pending`: a midweek request awaiting fulfilment holds the
    weekly run back), or our slots unreadable or not confirmed within `SLOTS_FRESH`. A live time that fails one
    waits (`WAITING`)."""
    from vending import slots, submissions

    open_rows = submissions.open_rows(conn, except_row)
    if open_rows:
        return ORDERS_BLOCKED.format(rows=", ".join(str(row[0]) for row in open_rows))
    pending = [r for (r,) in conn.execute(
        "select request_id from purchase_requests where status = 'pending' order by submitted_at")]
    if pending:
        return REQUEST_PENDING.format(requests=", ".join(pending))
    try:
        slots.our_slots(conn, now)
    except slots.SlotsNotRecorded as error:
        return SLOTS_UNREADY.format(error=str(error).rstrip("."))
    age = now - slots.stored(conn).last_checked_at
    if age > SLOTS_FRESH:
        return SLOTS_STALE.format(age=_span(age), limit=_span(SLOTS_FRESH))
    return None


def _before_start(now: datetime) -> str | None:
    from vending.start import START

    return BEFORE_START.format(start=_eastern(START)) if now < START else None


def _already_sent(conn, now: datetime, *, due: datetime | None = None, except_row: int | None = None,
                  kind: str = WEEKLY) -> str | None:
    """A submission a scheduled run of the same `kind` sent for the same week. A submission whose time has no row
    counts as weekly."""
    week = visit_week(due or now)
    for row, sent_for in conn.execute(
            "select p.id, p.schedule_due from purchase_submissions p left join agent_schedule s"
            " on s.due_at = p.schedule_due where p.schedule_due is not null and p.status <> 'not_filed'"
            " and p.id is distinct from %s and p.schedule_due > %s and p.schedule_due < %s"
            " and coalesce(s.kind, %s) = %s order by p.id",
            (except_row, (due or now) - timedelta(days=8), (due or now) + timedelta(days=8), WEEKLY, kind)):
        if visit_week(sent_for) == week:
            return (MIDWEEK_ALREADY_SENT if kind == MIDWEEK else ALREADY_SENT).format(row=row, when=_eastern(sent_for))
    return None


def kind_of(conn, due: datetime) -> str:
    """The kind of the scheduled time `due` (`WEEKLY` or `MIDWEEK`); weekly when it has no row."""
    row = conn.execute("select kind from agent_schedule where due_at = %s", (due,)).fetchone()
    return row[0] if row is not None else WEEKLY


def midweek_latest(due: datetime) -> datetime:
    """The latest start of a midweek run requested at `due`: midnight at the end of that Eastern day. The wake-up's
    sums plan the visit's landing from the day it was requested, so a run on a later day would plan on less."""
    day = due.astimezone(EASTERN).date() + timedelta(days=1)
    return datetime.combine(day, time.min, EASTERN).astimezone(timezone.utc)


def midweek_occasion(reason: str | None) -> str:
    """The paragraph a midweek run's opening carries: why it was woken, and what it may send."""
    return MIDWEEK_OCCASION.format(reason=(reason or "").strip() or "(no reason was recorded).")


def midweek_times(conn, now: datetime) -> list[tuple[datetime, datetime, int, str]]:
    """(due, latest start, wake-up id, reason) of each midweek run requested in the last `LOOKBACK`, up to `now`,
    with no row in `agent_schedule` yet, oldest first."""
    found = conn.execute(
        "select w.id, w.requested_at, w.reason from midweek_wakeups w where w.requested_at is not null"
        " and w.requested_at > %s and w.requested_at <= %s"
        " and not exists (select from agent_schedule s where s.wakeup_id = w.id) order by w.requested_at",
        (now - LOOKBACK, now)).fetchall()
    return [(midweek_due(due), midweek_latest(due), wakeup, reason) for wakeup, due, reason in found]


def midweek_due(requested: datetime) -> datetime:
    """A midweek row's `due_at`: the request's time, never on a whole minute. Every weekly time is on a whole minute
    (`AGENT_SCHEDULE` gives `HH:MM`), so the two never share a `due_at`: not in `agent_schedule`, the run lease's
    `schedule_due`, a submission's `schedule_due` or the server's `VENDING_SCHEDULE_DUE`. A request made on a whole
    minute (rare: it is the database's clock, to the microsecond) is kept one microsecond later. Migration 037
    holds it in the table (`agent_schedule_midweek_off_the_minute`)."""
    whole = requested.replace(second=0, microsecond=0)
    return requested + timedelta(microseconds=1) if requested == whole else requested


def lease_busy(conn) -> str | None:
    """Why a live time must wait for the run lease: another agent run holds it now. None when it is free."""
    row = conn.execute("select run_id from agent_run_lease"
                       " where token is not null and expires_at > clock_timestamp()").fetchone()
    if row is None:
        return None
    return LEASE_BUSY.format(run="" if row[0] is None else f" (agent run {row[0]})")


def lock_for_send(conn, due: datetime) -> None:
    """The submit tool's locks for a scheduled run's send, taken before it reads the clock and held until the POST
    is answered: the database switch's row and this time's row, `for share`, so an `off` committed meanwhile waits
    and is ordered after the send."""
    database_mode(conn, lock=True)
    conn.execute("select from agent_schedule where due_at = %s for share", (due,))


def send_blocker(conn, due: datetime, now: datetime, row: int) -> str | None:
    """Why the scheduled run for `due` must not POST its submission `row` now, or None. The submit tool runs it in
    the write gate's transaction, after the attempt is saved and just before the POST, so the locks it takes (the
    database's mode row and this time's row, `for share`) hold until the POST is answered: an `off` committed
    meanwhile waits and is ordered after the send. Both switches must still say live, the row must still be a live
    run going, and every check before a send (`live_blocker`) must pass by `now`, the clock at the send."""
    try:
        sched = schedule()
    except settings.SettingsError:
        sched = None
    container = "off" if sched is None else sched.mode
    if container != "live":
        return SEND_CONTAINER.format(mode=container)
    in_db = database_mode(conn, lock=True)
    if in_db != "live":
        return SEND_DB.format(mode=in_db)
    state = conn.execute("select mode, outcome, kind, latest_at from agent_schedule where due_at = %s for share",
                         (due,)).fetchone()
    if state is None or tuple(state[:2]) != ("live", "running"):
        return SEND_ROW.format(state="nothing" if state is None else f"{state[0]} {state[1]}")
    # A midweek run's wake-up holds only for the day it was requested (`midweek_latest`): an off-cycle plan has no
    # store cutoff, so a lease taken at 23:59 must not carry the POST past midnight.
    if state[2] == MIDWEEK and now >= state[3]:
        return MIDWEEK_EXPIRED.format(latest=_eastern(state[3]))
    return live_blocker(conn, now, due=due, except_row=row, kind=state[2])


def credit_state(client: Callable[[], Any] | None = None, model: str | None = None) -> tuple[str, str] | None:
    """Whether the run's own key can pay now, from one free `messages.count_tokens` call (`harness.check_credit`),
    never a paid request. `client` builds the client (`vending.agent.model_client` by default, what the run pays
    with) and `model` is the run's (`vending.agent.DEFAULT_MODEL`).

    None: go on and claim. ("wait", why): no credit (`NO_MODEL_CREDIT`), or no answer that could be read (a
    timeout, a dropped connection, a 429 or 5xx: the run's first request would hit the same), so the time waits.
    ("refused", why): the API refused the key (401 or 403), so the time fails at once (`MODEL_KEY_REFUSED`), as a
    missing key does. Without the key there is no check: the entry point records `MODEL_KEY_MISSING` as before.
    Any other refusal is left to the run, whose own request records it."""
    from vending import harness

    if not model_key_present():
        return None
    if client is None or model is None:
        from vending import agent

        client, model = client or agent.model_client, model or agent.DEFAULT_MODEL
    try:
        harness.check_credit(client(), model)
    except harness.NoCredit:
        return "wait", NO_MODEL_CREDIT
    except harness.KeyRefused as error:
        said = str(error)
        return "refused", MODEL_KEY_REFUSED.format(error=said[:1].upper() + said[1:])
    except harness.Unreachable as error:
        return "wait", CREDIT_UNCHECKED.format(error=str(error))
    except harness.HarnessError:
        return None
    return None


@dataclass(frozen=True)
class Wait:
    """A time held back before its claim: tried again every `LIVE_RETRY` until `latest`."""
    latest: datetime
    why: str
    mode: str = "live"          # what would run: `live`, or `dry-run` (the credit check holds dry runs too)
    since: datetime | None = None   # when the wait for model credit began (the database's clock)
    credit: bool = False        # the credit check holds it (`credit_state`), not a check before a send


def default_run_agent(live: bool, authorize: Callable[[Any], str | None] | None = None,
                      occasion: str | None = None) -> tuple[int | None, Any]:
    """The agent's own entry point, as `python -m vending.agent [--live]` runs it: (run id, outcome). `occasion`
    is what a midweek run's opening says about why it runs (`midweek_occasion`).
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
    extra = {} if occasion is None else {"occasion": occasion}     # a weekly run's call is as before
    return anyio.run(lambda: agent.run(live=live, authorize=authorize, **extra))


def _eastern(moment: datetime) -> str:
    local = moment.astimezone(EASTERN)
    return f"{local:%a %d %b} {local.hour % 12 or 12}:{local:%M %p} ET"


def _clock(moment: datetime) -> str:
    """The Eastern time of day: "12:00 PM ET"."""
    local = moment.astimezone(EASTERN)
    return f"{local.hour % 12 or 12}:{local:%M %p} ET"


def _span(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    return f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"


LAST_COLUMNS = "due_at, latest_at, mode, claimed_at, finished_at, outcome, run_id, kind"


def last_run(conn) -> dict | None:
    """The latest time in `agent_schedule`, without its detail: what `/health` may show."""
    row = conn.execute(f"select {LAST_COLUMNS} from agent_schedule order by due_at desc limit 1").fetchone()
    if row is None:
        return None
    out = dict(zip([c.strip() for c in LAST_COLUMNS.split(",")], row))
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in out.items()}


# A claim, in one statement: the lower of this worker's mode, the database's and `CEILING` (live), read by
# the statement that inserts the row, so a change committed before it is what runs. No row is off; a value that
# isn't one of the three words is dry-run. Off is `skipped`; past the latest start, by the database's clock read
# just before, `missed`.
CLAIM_SQL = """
with chosen as (
    select (array['off', 'dry-run', 'live'])[1 + least(%(rank)s, %(ceiling)s, coalesce(
        (select case when value = '"live"'::jsonb then 2 when value = '"off"'::jsonb then 0 else 1 end
         from config where key = %(key)s), 0))] as mode)
insert into agent_schedule (due_at, latest_at, mode, outcome, detail, finished_at, kind, wakeup_id)
select %(due)s, %(latest)s, case when %(past)s then %(cap)s else mode end,
       case when %(past)s then 'missed' when mode = 'off' then 'skipped' else 'running' end,
       case when %(past)s then %(missed)s when mode = 'off' then %(off)s end,
       case when %(past)s or mode = 'off' then now() end,
       %(kind)s, %(wakeup)s
from chosen
on conflict do nothing
returning mode, outcome"""


def record(conn, due: datetime, latest: datetime, mode: str, outcome: str, detail: str | None = None, *,
           clock: Callable[[], datetime] | None = None, kind: str = WEEKLY,
           wakeup: int | None = None) -> tuple[str, str] | None:
    """Insert a row for the time `due`: the (mode, outcome) stored, or None when it already has one.

    `running` is a claim, and `mode` is then this worker's own mode, the most it may run. The row gets
    the lower of that, `CEILING` and the database's `agent_schedule_mode`, read by the insert itself;
    `skipped` with mode `off` when that is off, and `missed` when `clock()` (the scheduler passes the
    database's clock), read just before, is past `latest`. A row that isn't a claim is stored with
    `mode` capped at `CEILING`. A midweek row (`kind` MIDWEEK) names its wake-up (`wakeup`); no second row is
    inserted for the same time or the same wake-up."""
    if outcome != "running":
        row = conn.execute(
            "insert into agent_schedule (due_at, latest_at, mode, outcome, detail, finished_at, kind, wakeup_id)"
            " values (%s, %s, %s, %s, %s, now(), %s, %s) on conflict do nothing returning mode, outcome",
            (due, latest, lower(mode, CEILING), outcome, detail, kind, wakeup)).fetchone()
        return None if row is None else tuple(row)
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    row = conn.execute(CLAIM_SQL, {"rank": RANK[mode], "ceiling": RANK[CEILING], "key": DB_MODE_KEY, "due": due,
                                   "latest": latest, "past": now >= latest, "cap": lower(mode, CEILING),
                                   "off": OFF_IN_DB, "kind": kind, "wakeup": wakeup,
                                   "missed": MISSED.format(latest=_eastern(latest))}).fetchone()
    return None if row is None else tuple(row)


def finish(conn, due: datetime, outcome: str, detail: str | None, run_id: int | None) -> None:
    """Record how the run for `due` ended. Overwrites a `failed` a sweep wrote meanwhile: this is what happened.
    A run id the agent already wrote there (`run_lease.attach`) stays when the ending has none."""
    conn.execute("update agent_schedule set outcome = %s, detail = %s, run_id = coalesce(%s, run_id),"
                 " finished_at = now() where due_at = %s", (outcome, detail, run_id, due))


@dataclass(frozen=True)
class Claim:
    due: datetime
    latest: datetime
    mode: str           # what runs: `live` or `dry-run` (a live claim a check turned into a dry run is `dry-run`)
    note: str           # goes before the agent's detail: "Started 6 h 0 min late. ", or why it isn't live
    kind: str = WEEKLY  # WEEKLY, or MIDWEEK for a wake-up's run
    reason: str | None = None   # a midweek run's: the wake-up's reason, for its opening

    @property
    def name(self) -> str:
        return _name(self.kind)


def _name(kind: str) -> str:
    """How the log names a time: "run due Wed 09 Dec ..." or "midweek run requested Mon 07 Dec ..."."""
    return "midweek run requested" if kind == MIDWEEK else "run due"


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
                 db_clock: Callable[[Any], datetime] | None = None, bridge: Callable[[], Any] | None = None,
                 credit_check: Callable[[], tuple[str, str] | None] | None = credit_state):
        self.sched, self.run_agent, self.log, self.background = sched, run_agent, log, background
        # Asked before each claim (`credit_state`): whether the run's key can pay. None: no check.
        self.credit_check = credit_check
        self._credit: tuple[str, str] | None | bool = False     # this tick's answer (False: not asked yet)
        # The bridge client the reconciliation of open submissions reads with (`vending.bridge.Bridge` by default).
        self.bridge = bridge
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
        # Why the last tick couldn't do its work (the database is down or behind), or None: /health shows it, and
        # the dashboard makes it a Problem.
        self.problem: str | None = None
        # A time held back before its claim (a live time by a passing blocker, any time by the credit check), and
        # why (the newest words): it has no row yet, and is tried again every `LIVE_RETRY` until its latest start
        # (`WAITING`, `CREDIT_WAITING`).
        self.waiting: dict[datetime, Wait] = {}
        # Set by `poke` after a worker round: a midweek wake-up may have asked for a run, so look now.
        self.check_now = False

    def poke(self) -> None:
        """Look for a midweek run to start at the next tick, not at the next weekly time. The worker calls it after
        each round, since that round's wake check may have requested one (`vending.midweek_wake`)."""
        self.check_now = True

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
        if self.waiting:        # a live time waiting on a passing blocker: try again now (`not_before` spaces it)
            return now
        if self.unfinished is not None or not self.loaded or self.check_now \
                or any(d not in self.known for d, _ in due_times(self.sched, now)):
            return now
        upcoming = next_time(self.sched, now)
        if self.sweep_at is not None:
            return max(now, min(upcoming, self.sweep_at))
        return upcoming

    def public(self) -> dict:
        """What `/health` shows: the most this worker runs, its setting, whether it has the model key (never the
        key), the next time, and the last scheduled run without its detail."""
        waiting = None
        for due, wait in sorted(self.waiting.items()):
            kind = "live run" if wait.mode == "live" else "dry run"
            if wait.credit:
                waiting = CREDIT_WAITING.format(kind=kind, due=_eastern(due), since=_clock(wait.since or due),
                                                why=wait.why, every=_span(LIVE_RETRY), latest=_eastern(wait.latest))
            else:
                waiting = WAITING.format(kind=kind, due=_eastern(due), why=wait.why, every=_span(LIVE_RETRY),
                                         latest=_eastern(wait.latest))
        return {"mode": self.mode, "setting": self.sched.mode, "model_key": model_key_present(),
                "next_at": next_time(self.sched, self.clock()).isoformat(), "last": self.last,
                "problem": self.sched.problem or self.problem, "waiting": waiting}

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
            self.problem = None
        except Exception as error:     # noqa: BLE001 - the worker must go on; the next try is a minute away
            from vending.worker import _describe

            self.not_before = now + RETRY
            self.problem = TICK_PROBLEM.format(error=_describe(error))
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
        self.log(f"agent schedule: the {running.claim.name} {_eastern(running.claim.due)} hasn't ended after "
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
            self.log(f"agent schedule: the {claim.name} {_eastern(claim.due)} couldn't start: {_describe(error)}")
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
        self._credit = False        # the credit check is asked at most once a tick
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
        # A midweek wake-up's run (#219): after the weekly time, which goes first when both are due.
        midweek = midweek_times(conn, now)
        soonest = [m for m in midweek if now < m[1]]
        wake = soonest[-1] if soonest else None
        for due_at, latest, wakeup, _ in midweek:
            if wake is None or wakeup != wake[2]:
                self._miss(conn, due_at, latest, now, kind=MIDWEEK, wakeup=wakeup)
        if claim is None and wake is not None:
            claim = self._claim(conn, wake[0], wake[1], kind=MIDWEEK, wakeup=wake[2], reason=wake[3])
        # Only the newest runnable time of each kind can still be waiting; every other one has its row now.
        keep = {t[0] for t in (newest, wake) if t is not None}
        self.waiting = {d: w for d, w in self.waiting.items() if d in keep}
        self.known = {d for d, _ in due}
        self.loaded = True
        self.check_now = False
        self.last = last_run(conn)
        return claim

    def _miss(self, conn, due: datetime, latest: datetime, now: datetime, *, kind: str = WEEKLY,
              wakeup: int | None = None) -> None:
        if latest <= due:       # only the clocks' spring change makes a window with no valid start
            detail = CLOCKS_SKIP.format(day=f"{due.astimezone(EASTERN):%a %d %b}")
        else:
            detail = MISSED.format(latest=_eastern(latest)) if now >= latest else SUPERSEDED
        mode, wait = self.mode, self.waiting.get(due)
        if wait is not None:        # a time that waited: say what held it back
            missed = LIVE_MISSED if wait.mode == "live" else DRY_MISSED
            detail = (missed.format(latest=_eastern(latest), why=wait.why) if now >= latest
                      else f"{SUPERSEDED} It had been waiting: {wait.why}")
            mode = lower(mode, wait.mode)
        stored = record(conn, due, latest, mode, "missed", detail, kind=kind, wakeup=wakeup)
        self.waiting.pop(due, None)     # only once the row is written: a failed insert keeps why it waited
        if stored:
            self.log(f"agent schedule: the {_name(kind)} {_eastern(due)} was missed")

    def _claim(self, conn, due: datetime, latest: datetime, *, kind: str = WEEKLY, wakeup: int | None = None,
               reason: str | None = None, again: bool = False) -> Claim | None:
        """Take the time `due` for this worker, or None when it doesn't run now. The insert itself reads the
        database's mode and checks the latest start (`record`). A midweek run (`kind` MIDWEEK) names its wake-up
        and carries its reason. The credit check is a call to the API, so the database's mode is read again after
        it: one that changed meanwhile decides again, once (`again`), with this tick's answer to the check."""
        name = _name(kind)
        if latest <= due:
            self._miss(conn, due, latest, self.db_clock(conn), kind=kind, wakeup=wakeup)
            return None
        effective = lower(self.mode, database_mode(conn))
        if effective == "live" and (why := self._live_wait(conn, due, self.db_clock(conn), kind)) is not None:
            self._hold(due, Wait(latest, why), name)
            return None
        checked = self._credit_now() if effective != "off" else None
        if not again and lower(self.mode, database_mode(conn)) != effective:
            return self._claim(conn, due, latest, kind=kind, wakeup=wakeup, reason=reason, again=True)
        if checked is not None:
            state, why = checked
            now = self.db_clock(conn)
            meant = ("live" if effective == "live" and lasting_blocker(conn, now, due=due, kind=kind) is None
                     else "dry-run")
            if state == "refused":      # no wait fixes a key the API refuses: the time fails, as a missing key does
                self.waiting.pop(due, None)
                if record(conn, due, latest, meant, "failed", why, kind=kind, wakeup=wakeup):
                    self.log(f"agent schedule: the {name} {_eastern(due)} failed: the API refused the model key")
                return None
            before = self.waiting.get(due)
            since = before.since if before is not None and before.credit else now
            self._hold(due, Wait(latest, why, meant, since, credit=True), name)
            return None
        waited = self.waiting.pop(due, None)
        stored = record(conn, due, latest, self.mode, "running", clock=lambda: self.db_clock(conn), kind=kind,
                        wakeup=wakeup)
        if stored is None:
            return None         # another worker has it
        mode, outcome = stored
        if outcome == "missed":
            self.log(f"agent schedule: the {name} {_eastern(due)} was missed")
            return None
        if outcome == "skipped":
            self.log(f"agent schedule: the {name} {_eastern(due)} skipped; {DB_MODE_KEY} is off in the database")
            return None
        now = self.db_clock(conn)
        late = now - due
        note = f"Started {_span(late)} late. " if late >= LATE_AFTER else ""
        if waited is not None and waited.credit:    # how late, and why, however short the wait
            note = WAITED_FOR_CREDIT.format(late=_span(max(late, timedelta(0))), since=_clock(waited.since or due))
        # The lasting reason first: a run held back for the whole window (one per visit) says so, not a passing
        # reason that happens to hold too (Wednesday's request still pending on Thursday), which reads as a wait.
        if mode == "live" and (why := lasting_blocker(conn, now, due=due, kind=kind)
                               or live_blocker(conn, now, due=due, kind=kind)) is not None:
            # Under the schedule's lock, before the run's thread exists: the row says what will run.
            conn.execute("update agent_schedule set mode = 'dry-run' where due_at = %s", (due,))
            mode, note = "dry-run", DOWNGRADED.format(why=why) + note
            self.log(f"agent schedule: the {name} {_eastern(due)} runs as a dry run, not live: {why}")
        self.log(f"agent schedule: starting the {mode} {name} {_eastern(due)}"
                 + (f", {_span(late)} late" if late >= LATE_AFTER else ""))
        return Claim(due, latest, mode, note, kind, reason)

    def _hold(self, due: datetime, wait: Wait, name: str) -> None:
        """Keep the time `due` waiting, with the newest words, and come back in `LIVE_RETRY`."""
        if self.waiting.get(due, Wait(wait.latest, "")).why != wait.why:
            self.log(f"agent schedule: the {'live' if wait.mode == 'live' else 'dry-run'} {name} {_eastern(due)} "
                     f"waits: {wait.why}")
        self.waiting[due] = wait
        self.not_before = self.clock() + LIVE_RETRY

    def _credit_now(self) -> tuple[str, str] | None:
        """This tick's credit check (`credit_state`), asked once a tick. A check that breaks holds nothing back:
        the run's own requests record what is wrong."""
        if self._credit is False:
            try:
                self._credit = self.credit_check() if self.credit_check is not None else None
            except Exception as error:      # noqa: BLE001 - the check is a guard, never a reason to stop the worker
                from vending.worker import _describe

                self.log(f"agent schedule: the model credit check broke ({_describe(error)}); going on without it")
                self._credit = None
        return self._credit

    def _live_wait(self, conn, due: datetime, now: datetime, kind: str = WEEKLY) -> str | None:
        """Why a time that would run live must wait, or None to claim it now. A lasting blocker doesn't wait: the
        claim runs it as a dry run and says why. Before an open submission counts as a passing blocker, what the
        bridge can settle by itself is settled (`reconcile`)."""
        if lasting_blocker(conn, now, due=due, kind=kind) is not None:
            return None
        self.reconcile(conn)
        return passing_blocker(conn, now) or lease_busy(conn)

    def reconcile(self, conn) -> None:
        """`python -m vending.submissions check`, unattended: settle each open submission whose POST got a validated
        `201` from the bridge's own answer. Nothing is sent, and a row in doubt stays open. Never raises."""
        from vending import submissions
        from vending.halt import WAIT_NOTICE_MS

        try:
            if not any(row[5] for row in submissions.open_rows(conn)):
                return
            if self.bridge is None:
                from vending.bridge import Bridge

                self.bridge = Bridge
            for checked in submissions.check(conn, self.bridge, lock_timeout_ms=WAIT_NOTICE_MS):
                if checked.status == "accepted":     # the rest stay open, and the wait already says so
                    self.log(f"agent schedule: settled submission {checked.row}: {checked.why}")
        except Exception as error:      # noqa: BLE001 - the rows stay open, and the run waits
            from vending.worker import _describe

            self.log(f"agent schedule: couldn't check the open submissions ({_describe(error)})")

    def _put_back(self, claim: Claim, detail: str) -> bool:
        """A live run whose start found the run lease taken after all: nothing started, so its row is taken back
        and its time waits again, as a lease found busy before the claim does. False if that can't be done."""
        try:
            with self.connect() as conn:
                gone = conn.execute("delete from agent_schedule where due_at = %s and outcome = 'running'"
                                    " and started_at is null returning due_at", (claim.due,)).fetchone()
        except Exception:       # noqa: BLE001 - recorded as an ending instead
            return False
        if gone is None:
            return False
        self.waiting[claim.due] = Wait(claim.latest, LEASE_BUSY.format(run="") + f" ({detail.rstrip('.')})")
        self.not_before = self.clock() + LIVE_RETRY
        self.log(f"agent schedule: the live {claim.name} {_eastern(claim.due)} found the run lease taken; it waits")
        return True

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
        of the clock; else nothing runs and the row is `skipped`, with why. A run that starts marks the lease
        with its time (`agent_run_lease.schedule_due`), so the submit tool checks it all again before the POST."""
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
            elif claim.mode == "live" and (why := live_blocker(conn, now, due=claim.due, kind=claim.kind)) is not None:
                refused["ending"] = ("skipped", NOT_LIVE.format(why=why))
            else:
                conn.execute("update agent_schedule set started_at = %s where due_at = %s", (now, claim.due))
                # The lease about to be taken is this time's: the submit tool checks it all again before a POST
                # (`send_blocker`), and the agent names its run on this row (`run_lease.attach`).
                conn.execute("update agent_run_lease set schedule_due = %s where one", (claim.due,))
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
                if claim.kind == MIDWEEK:     # its opening says why it was woken
                    run_id, out = self.run_agent(live, self._authorize(claim, refused),
                                                 occasion=midweek_occasion(claim.reason))
                else:
                    run_id, out = self.run_agent(live, self._authorize(claim, refused))
                outcome, detail = getattr(out, "status", None), getattr(out, "detail", None)
            except BaseException as error:      # noqa: BLE001 - a thread's error would otherwise be lost
                run_id, outcome, detail = None, "failed", f"The agent's entry point raised {_describe(error)}"
            if not running.end(abandon=False):
                return      # the worker let it go and recorded it already
            if refused.get("keep"):
                self.log(f"agent schedule: the {claim.name} {_eastern(claim.due)} wasn't started: {NOT_CLAIMED}")
                return      # another worker recorded the row: leave what it wrote
            if claim.mode == "live" and "ending" not in refused and outcome == "skipped" \
                    and isinstance(detail, str) and detail.startswith(_busy()) and self._put_back(claim, detail):
                return      # the lease was taken after the claim: nothing started, so the time waits again
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
                self.log(f"agent schedule: could not record the {claim.name} {_eastern(claim.due)}: "
                         f"{_describe(error)}; "
                         "trying again in a minute")
        finally:
            running.let_go()
            running.over = True
            self.run_ended.set()


def _busy() -> str:
    from vending.agent import BUSY

    return BUSY


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
            times = ", ".join(f"{DAYS[e.weekday][:3]} {e.due:%H:%M}-{e.latest:%H:%M}" for e in sched.entries)
            print(f"{MODE_SETTING} here: {sched.mode}, from {sched.first_day:%a %d %b %Y}; times (ET) {times}; "
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
        rows = conn.execute("select s.due_at, s.mode, s.outcome, s.run_id, s.detail, s.kind,"
                            " coalesce(r.request_id, (select p.request_id from purchase_submissions p"
                            "  where p.schedule_due = s.due_at and p.status = 'accepted' order by p.id desc limit 1)),"
                            " array(select p.id::text || ' ' || p.status from purchase_submissions p"
                            "  where p.schedule_due = s.due_at and p.status in ('sending', 'unknown') order by p.id)"
                            " from agent_schedule s left join agent_runs r on r.id = s.run_id"
                            " order by s.due_at desc limit %s", (args.rows,)).fetchall()
    if not rows:
        print("No scheduled run has been recorded yet.")
    for due, mode, outcome, run_id, detail, kind, request_id, unsettled in rows:
        run = f", agent run {run_id}" if run_id is not None else ""
        if request_id:
            sent = f", sent request {request_id}"
        elif unsettled:
            sent = (f", may have sent submission {', '.join(unsettled)}: a person settles it with "
                    "python -m vending.submissions resolve")
        else:
            sent = "" if outcome == "running" else ", nothing sent"
        mode = f"{mode} midweek" if kind == MIDWEEK else mode
        print(f"{_eastern(due)}  {mode}  {outcome}{run}{sent}" + (f"\n    {detail}" if detail else ""))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
