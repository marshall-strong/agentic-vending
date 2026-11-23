"""Scheduled agent runs from the worker (#213). The agent's entry point is faked: no model call, no bridge.

Time is a fake clock that only `sleep` (or a test) moves, so the worker's loop runs a scheduled week in a moment."""

import html
import re
import threading
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from vending import agent_schedule, dashboard, settings, worker
from vending.agent_schedule import Entry, Schedule, Scheduler

ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")
NAMES = (agent_schedule.MODE_SETTING, agent_schedule.TIMES_SETTING, agent_schedule.FROM_SETTING)
WED = datetime(2026, 12, 2, tzinfo=ET)          # the first day with runs, by default
DRY = Schedule("dry-run", (Entry(2, time(14), time(22)),), date(2026, 12, 2))
LIVE = Schedule("live", DRY.entries, DRY.first_day)


def at(day, hour, minute=0):
    return day.replace(hour=hour, minute=minute).astimezone(timezone.utc)


@pytest.fixture(autouse=True)
def _no_schedule_settings(monkeypatch):
    """Whatever a developer's `.env` says, these tests start with every schedule setting unset."""
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(settings, "_FILE", {k: v for k, v in settings._FILE.items() if k not in NAMES})
    monkeypatch.setattr(worker, "_slot_lock", lambda: None)


class Clock:
    """Time moves only when the loop sleeps, or a test sets `now`. Past `stop` the sleep interrupts, as Ctrl+C would."""

    def __init__(self, start, stop=None):
        self.now, self.stop = start, stop or start + timedelta(days=365)

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        assert seconds >= 0
        self.now += timedelta(seconds=seconds)
        if self.now >= self.stop:
            raise KeyboardInterrupt


# --- Off by default, and the settings ------------------------------------------------------------

def test_unset_is_off_and_the_worker_is_as_before(monkeypatch):
    assert agent_schedule.schedule() is None
    assert agent_schedule.default_scheduler(log=lambda _: None) is None
    monkeypatch.setenv(agent_schedule.MODE_SETTING, "off")
    assert agent_schedule.schedule() is None
    # Without the setting /health has no new key, so it reads exactly as before.
    assert "agent_schedule" not in worker.State().public()
    assert "Scheduled agent run" not in worker.home_page(worker.State().public())


def test_with_nothing_set_the_real_worker_never_starts_the_agent(monkeypatch):
    """The hard rule: no setting, no run. The real worker, through a week with a due time in it."""
    made, ran = [], []
    real = agent_schedule.Scheduler
    monkeypatch.setattr(agent_schedule, "Scheduler", lambda *a, **k: made.append(1) or real(*a, **k))
    monkeypatch.setattr(agent_schedule, "default_run_agent", lambda live: ran.append(live))
    monkeypatch.setattr(worker, "default_jobs", lambda: [("a", lambda: 0)])
    from vending import visit_polls
    monkeypatch.setattr(visit_polls, "default_reader", lambda log: None)
    clock = Clock(at(WED, 13), at(WED + timedelta(days=8), 0))
    assert worker.main(["--port", "0"], sleep=clock.sleep, log=lambda _: None, clock=clock) == 0
    assert made == [] and ran == []


def test_the_defaults_are_one_wednesday_afternoon_run_from_2_dec(monkeypatch):
    monkeypatch.setenv(agent_schedule.MODE_SETTING, "Dry-Run")
    assert agent_schedule.schedule() == DRY
    monkeypatch.setenv(agent_schedule.MODE_SETTING, "live")
    assert agent_schedule.schedule() == LIVE


def test_the_times_and_first_day_are_settings(monkeypatch):
    monkeypatch.setenv(agent_schedule.MODE_SETTING, "live")
    monkeypatch.setenv(agent_schedule.TIMES_SETTING, "Wednesday 13:30-21:00, thu 09:00-12:00")
    monkeypatch.setenv(agent_schedule.FROM_SETTING, "2026-12-09")
    sched = agent_schedule.schedule()
    assert sched.entries == (Entry(2, time(13, 30), time(21)), Entry(3, time(9), time(12)))
    assert sched.first_day == date(2026, 12, 9)


def test_the_lower_mode_wins():
    assert agent_schedule.lower("live", "dry-run") == "dry-run"
    assert agent_schedule.lower("dry-run", "live") == "dry-run"
    assert agent_schedule.lower("live", "off") == "off"
    assert agent_schedule.lower("live", "live") == "live"


@pytest.mark.parametrize("name, value", [
    # AGENT_SCHEDULE_MODE "on" is a dry run with a Problem line now (tests/test_live_schedule.py, Sol #238 finding 7).
    (agent_schedule.TIMES_SETTING, "wed 14:00"),             # no latest start
    (agent_schedule.TIMES_SETTING, "wed 22:00-14:00"),       # latest before due
    (agent_schedule.TIMES_SETTING, "someday 14:00-15:00"),
    (agent_schedule.TIMES_SETTING, "wed 25:00-26:00"),
    (agent_schedule.TIMES_SETTING, " , "),
    (agent_schedule.FROM_SETTING, "2 Dec"),
    # Each of these raised something other than SettingsError, which stopped the whole worker.
    (agent_schedule.TIMES_SETTING, "wed 14:00-22:00+01:00"),  # an offset: the times are Eastern
    (agent_schedule.TIMES_SETTING, "wed 14:00+01:00-22:00"),
    (agent_schedule.TIMES_SETTING, "wed 14:00:30-22:00"),
    (agent_schedule.TIMES_SETTING, "wed 14.00-22.00"),
    (agent_schedule.TIMES_SETTING, "we 14:00-22:00"),        # too short to say which day
    (agent_schedule.TIMES_SETTING, "wedx 14:00-22:00"),
    (agent_schedule.TIMES_SETTING, "wed 14:60-22:00"),
])
def test_a_wrong_setting_turns_the_runs_off_and_shows_on_health(monkeypatch, name, value):
    monkeypatch.setenv(agent_schedule.MODE_SETTING, "dry-run")
    monkeypatch.setenv(name, value)
    with pytest.raises(settings.SettingsError):
        agent_schedule.schedule()
    logged = []
    stand_in = agent_schedule.default_scheduler(log=logged.append)
    assert isinstance(stand_in, agent_schedule.Invalid) and "off" in logged[0]
    shown = worker.State(schedule=stand_in).public()
    assert shown["agent_schedule"]["mode"] == "invalid" and shown["status"] == "ok"
    [(level, text)] = dashboard._scheduled_runs(None, shown, at(WED, 14))
    assert level == "bad" and "setting is wrong" in text


def test_a_wrong_setting_never_stops_the_real_worker(monkeypatch):
    monkeypatch.setenv(agent_schedule.MODE_SETTING, "live")
    monkeypatch.setenv(agent_schedule.TIMES_SETTING, "wed 14:00-22:00+01:00")
    monkeypatch.setattr(worker, "default_jobs", lambda: [("a", lambda: 0)])
    from vending import visit_polls
    monkeypatch.setattr(visit_polls, "default_reader", lambda log: None)
    logged = []
    clock = Clock(at(WED, 13), at(WED, 15))
    assert worker.main(["--port", "0"], sleep=clock.sleep, log=logged.append, clock=clock) == 0
    assert any("scheduled agent runs are off" in line for line in logged)


# --- The times ------------------------------------------------------------------------------------

def test_the_next_time_is_wednesday_2_pm_eastern_from_2_dec():
    assert agent_schedule.next_time(DRY, datetime(2026, 11, 21, 12, tzinfo=ET)) == at(WED, 14)
    assert at(WED, 14) == datetime(2026, 12, 2, 19, tzinfo=timezone.utc)      # EST is UTC-5
    assert agent_schedule.next_time(DRY, at(WED, 14)) == at(WED + timedelta(days=7), 14)
    assert agent_schedule.next_time(DRY, at(WED, 13, 59)) == at(WED, 14)


def test_nothing_is_due_before_the_first_day():
    early = Schedule("dry-run", DRY.entries, date(2026, 12, 9))
    assert agent_schedule.due_times(early, at(WED, 23)) == []
    assert agent_schedule.next_time(early, at(WED, 13)) == at(WED + timedelta(days=7), 14)


def test_the_times_follow_eastern_time_through_the_clock_changes():
    summer = Schedule("dry-run", DRY.entries, date(2026, 10, 1))
    # 2 PM EDT is 18:00 UTC; 2 PM EST is 19:00 UTC.
    assert agent_schedule.next_time(summer, datetime(2026, 10, 27, tzinfo=timezone.utc)) == \
        datetime(2026, 10, 28, 18, tzinfo=timezone.utc)
    assert agent_schedule.next_time(summer, datetime(2026, 11, 3, tzinfo=timezone.utc)) == \
        datetime(2026, 11, 4, 19, tzinfo=timezone.utc)


def test_a_time_the_clocks_repeat_or_skip_is_one_run():
    # Sun 1 Nov 2026: 1:30 AM happens twice. It is the first (EDT), once.
    autumn = Schedule("dry-run", (Entry(6, time(1, 30), time(3)),), date(2026, 11, 1))
    found = agent_schedule.times_between(autumn, datetime(2026, 11, 1, tzinfo=timezone.utc),
                                         datetime(2026, 11, 2, tzinfo=timezone.utc))
    assert [d for d, _ in found] == [datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)]
    # Sun 8 Mar 2026: 2:30 AM never happens. It runs once, at 3:30 AM EDT.
    spring = Schedule("dry-run", (Entry(6, time(2, 30), time(4)),), date(2026, 3, 1))
    found = agent_schedule.times_between(spring, datetime(2026, 3, 8, tzinfo=timezone.utc),
                                         datetime(2026, 3, 9, tzinfo=timezone.utc))
    assert found == [(datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc), datetime(2026, 3, 8, 8, tzinfo=timezone.utc))]


# --- What /, /health and the dashboard show ---------------------------------------------------------

def test_the_dashboard_line_follows_the_outcome():
    now = at(WED, 15)

    def line(outcome, mode="dry-run", **kw):
        return dashboard._scheduled_line({"due_at": at(WED, 14), "mode": mode, "outcome": outcome, "run_id": 7,
                                          "claimed_at": at(WED, 14), **kw}, now)
    assert line("failed")[0] == "bad" and "failed (agent run 7)" in line("failed")[1]
    assert line("missed")[0] == "bad" and "missed" in line("missed")[1]
    assert line("halted")[0] == "warn" and "halt switch" in line("halted")[1]
    assert line("skipped") == ("info", "The scheduled dry run due Wed 02 Dec, 2:00 PM ET skipped (agent run 7). "
                                       "Nothing sent. `python -m vending.agent_schedule` says why.")
    assert line("skipped", "off", run_id=None)[1].startswith(
        "The scheduled run due Wed 02 Dec, 2:00 PM ET skipped: `agent_schedule_mode` is off in the database.")
    assert line("submitted") is None            # a dry run never submits (a live one may: tests/test_live_schedule.py)
    assert line("skipped", agent_status="running")[0] == "bad"
    assert line("running") == ("info", "The scheduled dry run due Wed 02 Dec, 2:00 PM ET is running now (agent run 7).")
    assert line("<b>") is None


def test_a_run_stuck_at_running_is_bad_on_the_dashboard():
    claimed = at(WED, 14)
    row = {"due_at": claimed, "mode": "dry-run", "outcome": "running", "run_id": None, "claimed_at": claimed}
    assert dashboard._scheduled_line(row, claimed + timedelta(minutes=30))[0] == "info"
    level, text = dashboard._scheduled_line(row, claimed + agent_schedule.STUCK_AFTER + timedelta(minutes=1))
    assert level == "bad" and "running" in text and "stopped mid-run" in text


def test_the_home_page_shows_the_schedule_only_when_it_is_on():
    shown = {"rounds": 1, "status": "ok", "results": [], "agent_schedule": {
        "mode": "dry-run", "next_at": at(WED, 14).isoformat(),
        "last": {"due_at": at(WED - timedelta(days=7), 14).isoformat(), "outcome": "<b>", "mode": "dry-run"}}}
    page = html.unescape(worker.home_page(shown))
    assert "Scheduled agent run" in page
    assert "dry runs; next Wed 02 Dec, 2:00 PM ET" in page
    assert "unknown, due Wed 25 Nov, 2:00 PM ET" in page      # an outcome it doesn't know isn't echoed
    del shown["agent_schedule"]
    assert "Scheduled agent run" not in worker.home_page(shown)


# --- The deploy script and the docs -------------------------------------------------------------------

def _ps_table(script: str, name: str) -> str:
    found = re.search(r"^\$" + name + r" = \[ordered\]@\{(.*?)\}", script, re.MULTILINE | re.DOTALL)
    assert found, name
    return found.group(1)


def test_the_deploy_never_requires_the_model_key():
    """The schedule is off by default, so a deploy must work without the key, and never stop over it
    after the image was pushed."""
    script = (ROOT / "scripts" / "deploy_container_app.ps1").read_text(encoding="utf-8")
    assert "VENDING_ANTHROPIC_API_KEY" not in _ps_table(script, "Secrets")
    assert '"VENDING_ANTHROPIC_API_KEY" = "vending-anthropic-api-key"' in _ps_table(script, "OptionalSecrets")
    assert '"ANTHROPIC_API_KEY" =' not in script      # only ever in $Retired, to remove it


def test_the_docs_say_how_the_first_fill_goes_and_how_to_try_a_dry_run_first():
    doc = (ROOT / "docs" / "agent.md").read_text(encoding="utf-8")
    unattended = doc[doc.index("## When it runs unattended"):doc.index("## Before the first run")]
    assert "29 Nov" in unattended and "#214" in unattended and "by hand" in unattended
    assert "Try it on Azure before" in unattended
    assert "agent_schedule_mode" in unattended and "AGENT_SCHEDULE_FROM" in unattended


# --- Runs, with the database --------------------------------------------------------------------

psycopg = pytest.importorskip("psycopg")

from tests.conftest import open_in  # noqa: E402
from vending import agent_runs, config  # noqa: E402


class FakeAgent:
    """Stands in for `vending.agent.run`: records a run in `agent_runs` as the real one does, and returns."""

    def __init__(self, url, clock=None, status="skipped", detail="Dry run: would have submitted the previewed plan.",
                 raises=None, gate=None, then=None):
        self.url, self.clock, self.status, self.detail, self.raises, self.gate = url, clock, status, detail, raises, gate
        self.then = then
        self.calls = []

    def __call__(self, live, authorize=None):
        if authorize is not None:       # as the real one does, in the transaction that takes the run lease
            with open_in(*self.url) as c, c.transaction():
                refused = authorize(c)
            if refused is not None:
                return None, SimpleNamespace(status="skipped", detail=refused)
        self.calls.append((self.clock() if self.clock else None, live))
        if self.gate is not None:
            self.gate.wait(getattr(self, "gate_timeout", 10))
        if self.raises is not None:
            raise self.raises
        with open_in(*self.url) as c:
            run_id = agent_runs.start(c, mode="live" if live else "dry-run", model="fake")
            agent_runs.finish(c, run_id, self.status, detail=self.detail, cost_usd=Decimal(0))
        if self.then is not None:
            self.then()
        return run_id, SimpleNamespace(status=self.status, detail=self.detail)


def scheduler(schema_url, agent, sched=DRY, clock=None, **kw):
    """A scheduler on the test schema. The fake clock stands in for the database's clock too, unless a test
    passes its own `db_clock`."""
    clock = clock or Clock(at(WED, 0))
    return Scheduler(sched, run_agent=agent, connect=kw.pop("connect", lambda: open_in(*schema_url)),
                     log=kw.pop("log", lambda _: None), background=kw.pop("background", False),
                     clock=clock, db_clock=kw.pop("db_clock", lambda c: clock()), **kw)


def tick(sched, when):
    """The worker's loop at `when`: the scheduler's clock reads the same."""
    sched.clock.now = when
    sched.tick(when)


def rows(conn):
    return conn.execute("select due_at, mode, outcome, run_id, detail from agent_schedule order by due_at").fetchall()


def db_mode(conn, value):
    config.set_value(conn, agent_schedule.DB_MODE_KEY, value, "test")


@pytest.fixture
def allowed(conn):
    """The database lets the worker run dry runs, the most a scheduled run is in this version."""
    db_mode(conn, "dry-run")


def attention(conn, now, syncs=None):
    return [(a["level"], a["text"]) for a in dashboard.snapshot(conn, now=now, syncs=syncs)["attention"]
            if a["text"].startswith(("The scheduled ", "Scheduled agent run"))]


# The hard rule, again: the container's setting on, the database's not set, and nothing runs.

def test_with_the_database_setting_unset_nothing_runs(schema_url, conn):
    agent = FakeAgent(schema_url)
    sched = scheduler(schema_url, agent, LIVE)
    tick(sched, at(WED, 14))
    assert agent.calls == []
    [(due, mode, outcome, run_id, detail)] = rows(conn)
    assert (due, mode, outcome, run_id, detail) == (at(WED, 14), "off", "skipped", None, agent_schedule.OFF_IN_DB)
    # Recorded once: a later tick doesn't run it either.
    tick(sched, at(WED, 15))
    assert agent.calls == [] and len(rows(conn)) == 1


def test_the_database_setting_turned_off_stops_the_runs(schema_url, conn):
    db_mode(conn, "off")
    agent = FakeAgent(schema_url)
    tick(scheduler(schema_url, agent, LIVE), at(WED, 14))
    assert agent.calls == [] and rows(conn)[0][1:3] == ("off", "skipped")


def test_an_old_live_replica_runs_only_what_the_database_allows(schema_url, conn):
    """Finding 1: during a deploy the old revision still says `live`. The database says `dry-run`, so it
    runs a dry run, whatever its own setting (and `live` would be a dry run anyway, in this version)."""
    db_mode(conn, "dry-run")
    agent = FakeAgent(schema_url)
    old = scheduler(schema_url, agent, LIVE)
    tick(old, at(WED, 14))
    assert [live for _, live in agent.calls] == [False]
    assert rows(conn)[0][1:3] == ("dry-run", "skipped")
    # The mode is read at each time, under the lock: turned down later, the next week runs that.
    db_mode(conn, "off")
    tick(old, at(WED + timedelta(days=7), 14))
    assert len(agent.calls) == 1 and rows(conn)[1][1:3] == ("off", "skipped")


def test_the_container_setting_is_the_most_a_worker_runs(schema_url, conn, allowed):
    agent = FakeAgent(schema_url)
    tick(scheduler(schema_url, agent, DRY), at(WED, 14))
    assert [live for _, live in agent.calls] == [False] and rows(conn)[0][1] == "dry-run"


def test_the_worker_starts_a_dry_run_at_its_time_and_the_dashboard_reads_it(schema_url, conn, allowed):
    clock = Clock(at(WED, 13, 50), at(WED, 14, 30))
    agent = FakeAgent(schema_url, clock)
    sched = scheduler(schema_url, agent, clock=clock)
    assert worker.main(["--every", "60", "--at-minute", "5", "--port", "0"], jobs=[("a", lambda: 0)],
                       sleep=clock.sleep, log=lambda _: None, clock=clock, scheduler=sched) == 0
    # Started once, as a dry run, at 2:00 PM Eastern on the dot.
    assert agent.calls == [(at(WED, 14), False)]
    [(due, mode, outcome, run_id, detail)] = rows(conn)
    assert (due, mode, outcome) == (at(WED, 14), "dry-run", "skipped") and detail.startswith("Dry run")
    # The agent's own record is linked, and is what the dashboard's runs read.
    assert conn.execute("select mode, status from agent_runs where id = %s", (run_id,)).fetchone() == ("dry-run", "skipped")
    # /health shows it without the detail.
    shown = worker.State(schedule=sched).public()
    last = shown["agent_schedule"]["last"]
    assert (last["outcome"], last["run_id"], last["mode"]) == ("skipped", run_id, "dry-run") and "detail" not in last
    assert shown["agent_schedule"]["next_at"] == at(WED + timedelta(days=7), 14).isoformat()
    # The dashboard reads the table, with or without the worker's /health.
    [(level, text)] = attention(conn, at(WED, 14, 30))
    assert level == "info" and f"agent run {run_id}" in text and "due Wed 02 Dec, 2:00 PM ET" in text
    # With the worker's /health too (here without a model key, which it also says).
    assert [line for line in attention(conn, at(WED, 14, 30), syncs=shown)
            if line[1].startswith("The scheduled ")] == [(level, text)]


def test_a_restart_never_runs_the_same_time_twice(schema_url, conn, allowed):
    agent = FakeAgent(schema_url)
    tick(scheduler(schema_url, agent), at(WED, 14))
    again = scheduler(schema_url, agent)            # the worker restarted
    assert again.next_due(at(WED, 14, 5)) == at(WED, 14, 5)    # it hasn't read the rows yet
    tick(again, at(WED, 14, 5))
    assert len(agent.calls) == 1 and len(rows(conn)) == 1
    assert again.next_due(at(WED, 14, 6)) == at(WED + timedelta(days=7), 14)


def test_a_time_missed_while_the_worker_was_down_is_recorded_and_not_run(schema_url, conn, allowed):
    agent = FakeAgent(schema_url)
    sched = scheduler(schema_url, agent)
    tick(sched, at(WED + timedelta(days=1), 10))     # back up Thursday morning, after the latest start
    assert agent.calls == []
    [(due, mode, outcome, run_id, detail)] = rows(conn)
    assert (due, outcome, run_id) == (at(WED, 14), "missed", None) and "Wed 02 Dec 10:00 PM ET" in detail
    assert sched.public()["last"]["outcome"] == "missed"
    assert attention(conn, at(WED + timedelta(days=1), 10))[0][0] == "bad"
    tick(sched, at(WED + timedelta(days=1), 11))
    assert len(rows(conn)) == 1 and agent.calls == []


def test_a_run_never_starts_after_its_latest_start_even_when_the_lock_was_slow(schema_url, conn, allowed):
    """Finding 5: the loop saw 9:59 PM, but by the time the worker held the lock it was past 10 PM."""
    agent = FakeAgent(schema_url)
    clock = Clock(at(WED, 22, 1))
    sched = scheduler(schema_url, agent, clock=clock)
    sched.tick(at(WED, 21, 59))
    assert agent.calls == []
    [(_, _, outcome, _, detail)] = rows(conn)
    assert outcome == "missed" and "10:00 PM ET" in detail


def test_the_clock_is_read_again_just_before_the_claim(schema_url, conn, allowed, monkeypatch):
    """The same, when it is the work under the lock that is slow."""
    agent = FakeAgent(schema_url)
    clock = Clock(at(WED, 21, 59))
    sched = scheduler(schema_url, agent, clock=clock)
    real = agent_schedule.due_times

    def slow(*a):
        found = real(*a)
        clock.now = at(WED, 22, 0)
        return found
    monkeypatch.setattr(agent_schedule, "due_times", slow)
    sched.tick(at(WED, 21, 59))
    assert agent.calls == [] and rows(conn)[0][2] == "missed"


def test_a_tick_with_nothing_to_run_logs_no_error(schema_url, conn, allowed):
    """It went on to start a run with no claim, and logged an AttributeError every time."""
    logged = []
    sched = scheduler(schema_url, FakeAgent(schema_url), log=logged.append)
    tick(sched, at(WED, 13))
    assert logged == [] and rows(conn) == [] and sched.next_due(at(WED, 13)) == at(WED, 14)


def test_a_time_reached_late_but_before_its_latest_start_runs_late(schema_url, conn, allowed):
    agent = FakeAgent(schema_url)
    tick(scheduler(schema_url, agent), at(WED, 20))
    assert [live for _, live in agent.calls] == [False]
    [(_, _, outcome, _, detail)] = rows(conn)
    assert outcome == "skipped" and detail.startswith("Started 6 h 0 min late. Dry run")


def test_only_the_newest_due_time_runs_and_the_older_ones_are_missed(schema_url, conn, allowed):
    twice = Schedule("dry-run", (Entry(2, time(9), time(22)), Entry(2, time(14), time(22))), date(2026, 12, 2))
    agent = FakeAgent(schema_url)
    tick(scheduler(schema_url, agent, twice), at(WED, 15))
    assert len(agent.calls) == 1
    assert [(d, o) for d, _, o, _, _ in rows(conn)] == [(at(WED, 9), "missed"), (at(WED, 14), "skipped")]
    assert rows(conn)[0][4] == agent_schedule.SUPERSEDED


def test_a_worker_holding_the_lock_keeps_another_from_running(schema_url, conn, allowed):
    agent = FakeAgent(schema_url)
    with open_in(*schema_url) as other:
        assert other.execute("select pg_try_advisory_lock(%s)", (agent_schedule.LOCK,)).fetchone()[0]
        sched = scheduler(schema_url, agent)
        tick(sched, at(WED, 14))
        assert agent.calls == [] and rows(conn) == []
        assert sched.next_due(at(WED, 14)) == at(WED, 14, 1)       # tries again in a minute, never spins
        other.execute("select pg_advisory_unlock(%s)", (agent_schedule.LOCK,))
    tick(sched, at(WED, 14, 1))
    assert len(agent.calls) == 1 and rows(conn)[0][2] == "skipped"


def test_a_run_by_hand_and_a_scheduled_run_never_overlap(schema_url, conn, allowed):
    """Finding 2: the agent's own entry point holds one run lease for every run, by hand or scheduled."""
    from vending import agent, run_lease
    from vending.config import set_halted

    set_halted(conn, False, "test")
    by_hand = agent_runs.start(conn, mode="dry-run", model="by hand")
    held = run_lease.take(conn, deadline=60)
    assert run_lease.attach(conn, held.token, by_hand)
    tick(scheduler(schema_url, _real_entry_point(schema_url)), at(WED, 14))
    run_lease.give_back(conn, held.token)
    [(_, _, outcome, run_id, detail)] = rows(conn)
    assert (outcome, detail) == ("skipped", f"{agent.BUSY} (agent run {by_hand}).")
    assert conn.execute("select status, detail from agent_runs where id = %s", (run_id,)).fetchone() == \
        ("skipped", detail)


def test_a_run_the_worker_was_stopped_in_is_failed_and_never_run_again(schema_url, conn, allowed):
    conn.execute("insert into agent_schedule (due_at, latest_at, mode) values (%s, %s, 'dry-run')",
                 (at(WED, 14), at(WED, 22)))
    agent = FakeAgent(schema_url)
    sched = scheduler(schema_url, agent)
    tick(sched, at(WED, 14, 20))
    assert agent.calls == []
    [(_, _, outcome, _, detail)] = rows(conn)
    assert (outcome, detail) == ("failed", agent_schedule.STOPPED)


def test_a_running_row_is_left_alone_while_an_agent_run_holds_the_lease(schema_url, conn, allowed):
    """The worker's connection dropped (so its lock went), but its run is still going: not swept."""
    from vending import run_lease

    conn.execute("insert into agent_schedule (due_at, latest_at, mode) values (%s, %s, 'dry-run')",
                 (at(WED, 14), at(WED, 22)))
    held = run_lease.take(conn, deadline=60)
    tick(scheduler(schema_url, FakeAgent(schema_url)), at(WED, 14, 20))
    assert rows(conn)[0][2] == "running"
    run_lease.give_back(conn, held.token)


@pytest.mark.parametrize("status", ["skipped", "halted", "failed"])
def test_each_ending_of_the_agent_is_recorded(schema_url, conn, allowed, status):
    agent = FakeAgent(schema_url, status=status, detail=f"why it {status}")
    sched = scheduler(schema_url, agent, LIVE)
    tick(sched, at(WED, 14))
    assert agent.calls == [(None, False)]
    [(_, mode, outcome, run_id, detail)] = rows(conn)
    assert (mode, outcome, detail) == ("dry-run", status, f"why it {status}") and run_id is not None
    assert sched.public()["last"]["outcome"] == status


def test_a_dry_run_that_says_it_submitted_is_recorded_as_failed(schema_url, conn, allowed):
    """A scheduled run is a dry run and can't submit: an agent that says it did is a failure to look at."""
    tick(scheduler(schema_url, FakeAgent(schema_url, status="submitted", detail="sent")), at(WED, 14))
    [(_, _, outcome, _, detail)] = rows(conn)
    assert outcome == "failed" and "unknown status 'submitted'" in detail


def test_an_entry_point_that_raises_is_a_failed_run_and_the_worker_goes_on(schema_url, conn, allowed):
    agent = FakeAgent(schema_url, raises=RuntimeError("no model key"))
    sched = scheduler(schema_url, agent)
    tick(sched, at(WED, 14))
    [(_, _, outcome, run_id, detail)] = rows(conn)
    assert (outcome, run_id) == ("failed", None) and "RuntimeError: no model key" in detail
    # The lock was given back: a later time can run.
    agent.raises = None
    tick(sched, at(WED + timedelta(days=7), 14))
    assert [r[2] for r in rows(conn)] == ["failed", "skipped"]


def test_an_ending_that_couldnt_be_written_is_written_a_minute_later(schema_url, conn, allowed):
    """Finding 4: a failed outcome write left the row `running` until a later time was due."""
    down = []

    def connect():
        if down:
            raise psycopg.OperationalError("connection refused")
        return open_in(*schema_url)
    agent = FakeAgent(schema_url, status="failed", detail="cap reached", then=lambda: down.append(1))
    logged = []
    sched = scheduler(schema_url, agent, connect=connect, log=logged.append)
    tick(sched, at(WED, 14))
    assert rows(conn)[0][2] == "running" and any("could not record" in line for line in logged)
    # The loop comes back in a minute, not next week.
    assert sched.next_due(at(WED, 14, 0, )) == at(WED, 14, 1)
    tick(sched, at(WED, 14, 1))             # still down: kept
    assert rows(conn)[0][2] == "running"
    down.clear()
    assert sched.next_due(at(WED, 14, 2)) == at(WED, 14, 2)
    tick(sched, at(WED, 14, 2))
    [(_, _, outcome, run_id, detail)] = rows(conn)
    assert (outcome, detail) == ("failed", "cap reached") and run_id is not None
    assert len(agent.calls) == 1 and sched.unfinished is None
    assert sched.next_due(at(WED, 14, 3)) == at(WED + timedelta(days=7), 14)


def test_turning_the_schedule_off_never_hides_a_failed_run(schema_url, conn, allowed):
    """Finding 4: the dashboard read the last run from /health, which has no schedule once it is off."""
    tick(scheduler(schema_url, FakeAgent(schema_url, status="failed", detail="boom")), at(WED, 14))
    # The worker restarted with the schedule off: /health has no `agent_schedule`.
    shown = worker.State().public()
    [(level, text)] = attention(conn, at(WED, 15), syncs=shown)
    assert level == "bad" and "failed" in text and "boom" not in text


def test_a_newer_run_never_hides_an_older_failure(schema_url, conn, allowed):
    twice = Schedule("dry-run", (Entry(2, time(9), time(10)), Entry(2, time(14), time(22))), date(2026, 12, 2))
    agent = FakeAgent(schema_url, status="failed", detail="boom")
    sched = scheduler(schema_url, agent, twice)
    tick(sched, at(WED, 9))
    agent.status, agent.detail = "skipped", "quiet week"
    tick(sched, at(WED, 14))
    lines = attention(conn, at(WED, 15))
    assert [level for level, _ in lines] == ["bad", "info"]        # worst first
    assert "9:00 AM ET failed" in lines[0][1] and "2:00 PM ET skipped" in lines[1][1]
    # A week on, the old failure has gone; the newest run stays.
    later = attention(conn, at(WED + timedelta(days=7), 13))
    assert [level for level, _ in later] == ["info"]


def test_a_database_that_is_down_is_logged_and_tried_again_in_a_minute(schema_url):
    logged = []

    def down():
        raise psycopg.OperationalError("connection refused")
    sched = scheduler(schema_url, FakeAgent(schema_url), connect=down, log=logged.append)
    tick(sched, at(WED, 14))
    assert "OperationalError" in logged[0] and sched.next_due(at(WED, 14)) == at(WED, 14, 1)


def test_a_run_in_the_background_lets_the_loop_go_on_and_is_never_started_twice(schema_url, conn, allowed):
    gate = threading.Event()
    agent = FakeAgent(schema_url, gate=gate)
    sched = scheduler(schema_url, agent, background=True)
    tick(sched, at(WED, 14))
    assert sched.thread.is_alive()
    assert rows(conn)[0][2] == "running" and sched.public()["last"]["outcome"] == "running"
    tick(sched, at(WED, 14, 1))                      # still running: nothing new
    with open_in(*schema_url) as other:             # and another worker can't take the lock meanwhile
        assert not other.execute("select pg_try_advisory_lock(%s)", (agent_schedule.LOCK,)).fetchone()[0]
    gate.set()
    sched.thread.join(10)
    assert len(agent.calls) == 1 and rows(conn)[0][2] == "skipped"
    assert sched.public()["last"]["outcome"] == "skipped"


def test_the_show_command_lists_the_rows(schema_url, conn, monkeypatch, capsys):
    from vending import db
    monkeypatch.setattr(db, "connect", lambda: open_in(*schema_url))
    db_mode(conn, "dry-run")
    tick(scheduler(schema_url, FakeAgent(schema_url)), at(WED + timedelta(days=1), 10))
    assert agent_schedule.main([]) == 0
    out = capsys.readouterr().out
    assert "Scheduled runs are off here" in out and "agent_schedule_mode in the database: dry-run" in out
    assert "A worker runs off" in out and "Wed 02 Dec 2:00 PM ET  dry-run  missed" in out


def test_the_database_setting_is_checked_when_it_is_set(conn):
    with pytest.raises(ValueError, match="agent_schedule_mode must be off, dry-run or live"):
        db_mode(conn, "on")
    db_mode(conn, "dry-run")
    assert agent_schedule.database_mode(conn) == "dry-run"


# --- Sol review, round 2 -------------------------------------------------------------------------------
# Each test changes something from a second connection between two steps of a scheduled run.

def _set_from_another_connection(schema_url, value):
    """Commit `agent_schedule_mode` from a connection of its own: `None` deletes the row."""
    with open_in(*schema_url) as other:
        if value is None:
            other.execute("delete from config where key = %s", (agent_schedule.DB_MODE_KEY,))
        else:
            db_mode(other, value)


def _around_the_claim(monkeypatch, before=None, after=None):
    """Run `before` just before the claim's insert, and `after` just after it."""
    real = agent_schedule.record

    def wrapped(conn_, *args, **kw):
        claiming = (args[3] if len(args) > 3 else kw.get("outcome")) == "running"
        if claiming and before is not None:
            before()
        out = real(conn_, *args, **kw)
        if claiming and after is not None:
            after()
        return out
    monkeypatch.setattr(agent_schedule, "record", wrapped)


def _real_entry_point(schema_url):
    """`vending.agent.run`, as the worker calls it, with nothing behind it: opening the tools or a model
    request fails the run."""
    import anyio

    from vending import agent

    def never(*a, **k):
        raise AssertionError("a refused run must not open the tools or call the model")

    def entry(live, authorize=None):
        return anyio.run(lambda: agent.run(live=live, connect=lambda: open_in(*schema_url), client=never,
                                           open_tools=never, authorize=authorize))
    return entry


@pytest.mark.parametrize("change, called, stored", [
    ("dry-run", [False], ("dry-run", "skipped")),
    ("live", [False], ("dry-run", "skipped")),
    ("off", [], ("off", "skipped")),
    (None, [], ("off", "skipped")),
])
def test_a_mode_change_committed_just_before_the_claim_is_what_runs(schema_url, conn, allowed, monkeypatch,
                                                                    change, called, stored):
    """Round 2, blocker 1: the mode was read, then the claim inserted with the cached mode."""
    agent = FakeAgent(schema_url)
    _around_the_claim(monkeypatch, before=lambda: _set_from_another_connection(schema_url, change))
    tick(scheduler(schema_url, agent, LIVE), at(WED, 14))
    assert [live for _, live in agent.calls] == called
    assert rows(conn)[0][1:3] == stored


@pytest.mark.parametrize("change", ["off", None])
def test_a_mode_change_after_the_claim_stops_the_run_before_anything_is_paid_for(schema_url, conn, allowed,
                                                                                monkeypatch, change):
    """Round 2, blocker 1: the agent checks again in the transaction that takes the run lease, before any
    model request. (Round 3: a run is a dry run whatever the settings, so only `off` lowers it now.)"""
    from vending.config import set_halted
    set_halted(conn, False, "test")
    _around_the_claim(monkeypatch, after=lambda: _set_from_another_connection(schema_url, change))
    tick(scheduler(schema_url, _real_entry_point(schema_url), LIVE), at(WED, 14))
    [(_, mode, outcome, run_id, detail)] = rows(conn)
    assert (mode, outcome, run_id) == ("dry-run", "skipped", None)
    assert detail.startswith("Not started: agent_schedule_mode changed")
    assert conn.execute("select count(*) from agent_runs").fetchone()[0] == 0


def test_a_run_never_starts_when_the_latest_start_passes_during_the_claim(schema_url, conn, allowed, monkeypatch):
    """Round 2, major 3: 9:59 PM when the claim began, 10:01 PM when it was inserted."""
    agent = FakeAgent(schema_url)
    clock = Clock(at(WED, 21, 59))
    _around_the_claim(monkeypatch, before=lambda: setattr(clock, "now", at(WED, 22, 1)))
    scheduler(schema_url, agent, clock=clock).tick(at(WED, 21, 59))
    assert agent.calls == []
    [(_, _, outcome, run_id, detail)] = rows(conn)
    assert (outcome, run_id) == ("missed", None) and "10:00 PM ET" in detail


def test_a_run_never_starts_when_the_latest_start_passes_before_it_holds_the_lease(schema_url, conn, allowed,
                                                                                  monkeypatch):
    """Round 2, major 3: claimed at 9:59 PM; by the time the agent held the run lease it was 10:01 PM."""
    from vending.config import set_halted
    set_halted(conn, False, "test")
    clock = Clock(at(WED, 21, 59))
    _around_the_claim(monkeypatch, after=lambda: setattr(clock, "now", at(WED, 22, 1)))
    scheduler(schema_url, _real_entry_point(schema_url), clock=clock).tick(at(WED, 21, 59))
    [(_, _, outcome, run_id, detail)] = rows(conn)
    assert (outcome, run_id) == ("missed", None) and "10:00 PM ET" in detail and "claimed" in detail
    assert conn.execute("select count(*) from agent_runs").fetchone()[0] == 0


def test_a_run_that_never_ends_lets_go_of_the_schedule(schema_url, conn, allowed):
    """Round 2, major 4: a run stuck in the database (or anywhere) held the schedule's lock for ever."""
    gate = threading.Event()
    agent = FakeAgent(schema_url, gate=gate)
    agent.gate_timeout = 30
    sched = scheduler(schema_url, agent, background=True)
    tick(sched, at(WED, 14))
    stuck = sched.thread
    assert stuck.is_alive()
    tick(sched, at(WED, 14) + agent_schedule.STUCK_AFTER - timedelta(minutes=1))     # not yet
    assert rows(conn)[0][2] == "running"
    tick(sched, at(WED, 14) + agent_schedule.STUCK_AFTER + timedelta(minutes=1))
    [(_, _, outcome, _, detail)] = rows(conn)
    assert outcome == "failed" and detail == agent_schedule.ABANDONED
    with open_in(*schema_url) as other:              # the schedule's lock is free again
        assert other.execute("select pg_try_advisory_lock(%s)", (agent_schedule.LOCK,)).fetchone()[0]
        other.execute("select pg_advisory_unlock(%s)", (agent_schedule.LOCK,))
    # The stuck run, when it ends at last, doesn't overwrite what was recorded; the next week runs.
    gate.set()
    stuck.join(10)
    assert rows(conn)[0][2:] == ("failed", None, agent_schedule.ABANDONED)
    agent.gate = None
    tick(sched, at(WED + timedelta(days=7), 14))
    sched.thread.join(10)
    assert [r[2] for r in rows(conn)] == ["failed", "skipped"]


def test_a_thread_that_cannot_start_is_a_failed_launch_and_lets_go(schema_url, conn, allowed):
    """Round 2, major 5: Thread.start() raised, and the claim stayed `running` with the lock held."""
    class NoThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

        def is_alive(self):
            return False
    agent = FakeAgent(schema_url)
    sched = scheduler(schema_url, agent, background=True)
    sched.thread_factory = NoThread
    tick(sched, at(WED, 14))
    assert agent.calls == []
    [(_, _, outcome, run_id, detail)] = rows(conn)
    assert (outcome, run_id) == ("failed", None) and "RuntimeError" in detail and "start" in detail
    with open_in(*schema_url) as other:
        assert other.execute("select pg_try_advisory_lock(%s)", (agent_schedule.LOCK,)).fetchone()[0]
        other.execute("select pg_advisory_unlock(%s)", (agent_schedule.LOCK,))
    sched.thread_factory = threading.Thread
    tick(sched, at(WED + timedelta(days=7), 14))
    sched.thread.join(10)
    assert [r[2] for r in rows(conn)] == ["failed", "skipped"]


def _row(conn, due, outcome, mode="dry-run", run_id=None):
    conn.execute("insert into agent_schedule (due_at, latest_at, mode, outcome, finished_at, run_id)"
                 " values (%s, %s, %s, %s, case when %s = 'running' then null else now() end, %s)",
                 (due, due + timedelta(hours=1), mode, outcome, outcome, run_id))


def test_one_failure_stays_on_the_dashboard_behind_many_skipped_runs(schema_url, conn):
    """Round 2, major 6: one failure, then 20 skipped runs the same week, and the failure vanished."""
    start = at(WED, 0)
    _row(conn, start, "failed")
    for i in range(1, 21):
        _row(conn, start + timedelta(hours=6 * i), "skipped")
    lines = attention(conn, start + timedelta(days=6))
    assert [level for level, _ in lines] == ["bad", "info"]
    assert "Wed 02 Dec, 12:00 AM ET failed" in lines[0][1]


def test_a_failure_older_than_a_week_that_is_still_the_newest_run_stays(schema_url, conn):
    """Round 2, major 6: the newest run is always shown, however old."""
    _row(conn, at(WED, 14), "failed")
    [(level, text)] = attention(conn, at(WED + timedelta(days=9), 14))
    assert level == "bad" and "failed" in text


def test_an_unreadable_schedule_says_so():
    """Round 2, major 6: a panel that couldn't be read was dropped without a word."""
    [(level, text)] = dashboard._scheduled_runs({"ok": False, "why": "couldn't be read just now"}, None, at(WED, 14))
    assert level == "warn" and "Scheduled agent run status unavailable" in text


def test_the_mode_commands_in_the_docs_work_in_every_shell(schema_url, conn, monkeypatch, capsys):
    """Round 2, major 7: Windows PowerShell 5.1 drops the quotes in '"dry-run"'. The docs use bare words,
    which every shell passes as they are, and the command reads them."""
    import shlex

    from vending import db
    monkeypatch.setattr(db, "connect", lambda: open_in(*schema_url))
    texts = [(ROOT / "docs" / name).read_text(encoding="utf-8") for name in ("agent.md", "deploy.md")]
    texts.append(agent_schedule.__doc__)
    commands = [c for t in texts for c in re.findall(r"python -m vending\.config set agent_schedule_mode [^`\n]*", t)]
    assert len(commands) >= 3, commands
    for command in commands:
        assert '"' not in command and "'" not in command, command
        assert shlex.split(command)[5] in ("off", "dry-run", "live"), command
    # PowerShell 5.1 passes '"dry-run"' as dry-run; bash passes it as "dry-run". Both are read as the word.
    for argv_value, expected in (("dry-run", "dry-run"), ('"dry-run"', "dry-run"), ("off", "off"), ("live", "live")):
        assert config.main(["set", agent_schedule.DB_MODE_KEY, argv_value, "--by", "test"]) == 0
        assert agent_schedule.database_mode(conn) == expected
    capsys.readouterr()
    assert config.main(["set", agent_schedule.DB_MODE_KEY, "on", "--by", "test"]) == 1
    assert "in quotes" not in capsys.readouterr().err


def test_a_window_the_clocks_skip_is_recorded_with_the_reason(schema_url, conn, allowed):
    """Round 2, minor 8: `sun 02:30-03:00` doesn't exist on the spring-forward Sunday."""
    spring = Schedule("dry-run", (Entry(6, time(2, 30), time(3)),), date(2026, 3, 1))
    agent = FakeAgent(schema_url)
    sched = scheduler(schema_url, agent, spring, clock=Clock(datetime(2026, 3, 8, 7, tzinfo=timezone.utc)))
    tick(sched, datetime(2026, 3, 8, 9, tzinfo=timezone.utc))
    assert agent.calls == []
    [(_, _, outcome, _, detail)] = rows(conn)
    assert outcome == "missed" and detail == agent_schedule.CLOCKS_SKIP.format(day="Sun 08 Mar")


def test_a_database_setting_of_the_wrong_type_is_a_dry_run_and_shown(schema_url, conn, allowed):
    """Round 2, minor 9: a stored [] raised TypeError, and the worker tried again every minute. Sol #238, finding 7:
    a value that isn't a mode is a dry run (never live, never off), and a Problem line."""
    conn.execute("update config set value = '[]' where key = %s", (agent_schedule.DB_MODE_KEY,))
    assert agent_schedule.database_mode(conn) == "dry-run"
    agent = FakeAgent(schema_url)
    logged = []
    tick(scheduler(schema_url, agent, LIVE, log=logged.append), at(WED, 14))
    assert [live for _, live in agent.calls] == [False] and not any("trying again" in line for line in logged)
    assert rows(conn)[0][1:3] == ("dry-run", "skipped")
    assert ("bad", agent_schedule.INVALID_IN_DB) in attention(conn, at(WED, 15))


# --- Sol review, round 3 -------------------------------------------------------------------------------
# Scheduled runs are dry runs only in this version (finding 1 is removed by that). The rest change something
# from a second connection, or fake the clocks, between two steps of a scheduled run.

def test_live_in_both_settings_before_the_start_is_a_dry_run_with_no_write_tools(schema_url, conn, allowed):
    """Round 3, finding 1, now with live scheduling (tests/test_live_schedule.py): before the competition starts
    a scheduled run is never live. With both settings `live` on Wed 2 Dec, the agent is called with live=False,
    and the model is offered no write tool: no purchase, no catalog request."""
    import anyio

    from tests.test_agent import LIVE_TOOLS, Script, Tools, answer
    from vending import agent
    from vending.config import set_halted

    set_halted(conn, False, "test")
    db_mode(conn, "live")
    client, tools, called = Script(answer()), Tools(LIVE_TOOLS), []

    def entry(live, authorize=None):
        called.append(live)
        return anyio.run(lambda: agent.run(live=live, connect=lambda: open_in(*schema_url), client=client,
                                           open_tools=tools.opener(), authorize=authorize))
    sched = scheduler(schema_url, entry, LIVE)
    tick(sched, at(WED, 14))
    assert called == [False]
    offered = {t["name"] for t in client.sent[0]["tools"]}
    assert offered and not offered & agent.WRITE_TOOLS
    assert {"submit_desired_state", "request_catalog_product"} <= agent.WRITE_TOOLS
    [(_, mode, outcome, run_id, detail)] = rows(conn)
    assert (mode, outcome) == ("dry-run", "skipped") and "never live before" in detail
    assert conn.execute("select mode from agent_runs where id = %s", (run_id,)).fetchone()[0] == "dry-run"
    # /health shows this worker may run live; the dashboard says both switches are live.
    shown = worker.State(schedule=sched).public()
    assert shown["agent_schedule"]["mode"] == "live" and shown["agent_schedule"]["setting"] == "live"
    assert ("warn", agent_schedule.LIVE_ON) in attention(conn, at(WED, 15), syncs=shown)
    db_mode(conn, "dry-run")
    assert ("info", agent_schedule.HALF_LIVE) in attention(conn, at(WED, 15), syncs=shown)


def test_the_default_entry_point_is_the_agents_own_live_or_not(monkeypatch):
    """`vending.agent.run`, with `live` as the scheduler chose it: no second submit path."""
    from vending import agent
    from vending.harness import DEFAULT_KEY_ENV

    monkeypatch.setenv(DEFAULT_KEY_ENV, "sk-test")
    seen = []

    async def run(**kw):
        seen.append(kw)
        return 1, SimpleNamespace(status="skipped", detail="")
    monkeypatch.setattr(agent, "run", run)
    assert agent_schedule.default_run_agent(True)[0] == 1
    assert agent_schedule.default_run_agent(False)[0] == 1
    assert [k["live"] for k in seen] == [True, False] and set(seen[0]) == {"live", "authorize"}


def test_off_committed_while_the_run_is_being_authorized_waits_for_the_start(schema_url, conn, allowed,
                                                                           monkeypatch):
    """Round 3, blocker 3: `off` committed after the mode was read and before the lease was installed still
    started a run. The mode is read under a row lock in the lease's transaction, so `off` waits for the
    authorized start to be recorded, and is ordered after it."""
    from vending.config import set_halted

    set_halted(conn, False, "test")
    real, setter = agent_schedule.database_mode, {}

    def read_then_turn_off(c, **kw):
        mode = real(c, **kw)
        if "thread" not in setter:
            def turn_off():
                with open_in(*schema_url) as other:
                    db_mode(other, "off")
                    setter["started_at"] = other.execute("select started_at from agent_schedule").fetchone()[0]
            setter["thread"] = threading.Thread(target=turn_off, daemon=True)
            setter["thread"].start()
            setter["thread"].join(1.0)
            setter["blocked"] = setter["thread"].is_alive()
        return mode
    monkeypatch.setattr(agent_schedule, "database_mode", read_then_turn_off)
    tick(scheduler(schema_url, _real_entry_point(schema_url)), at(WED, 14))
    setter["thread"].join(10)
    assert setter["blocked"], "off was committed between the mode's read and the run's start"
    assert setter["started_at"] is not None       # the start was recorded before off landed
    assert agent_schedule.database_mode(conn) == "off"


def test_a_worker_clock_that_runs_slow_never_starts_a_run_past_its_cutoff(schema_url, conn, allowed):
    """Round 3, major 4: the worker's clock says 9:05 AM two days ago, inside a run's window; the database's
    says now. The final checks read the database's clock, so the time is `missed`."""
    agent = FakeAgent(schema_url)
    day = (conn.execute("select clock_timestamp()").fetchone()[0].astimezone(ET) - timedelta(days=2)).date()
    past = Schedule("dry-run", (Entry(day.weekday(), time(9), time(10)),), day - timedelta(days=7))
    window = datetime.combine(day, time(), ET)
    slow = Clock(at(window, 9, 5))      # two days slow: inside the window by its own reading
    sched = Scheduler(past, run_agent=agent, connect=lambda: open_in(*schema_url), log=lambda _: None,
                      background=False, clock=slow)          # the database's own clock: no db_clock given
    sched.tick(slow())
    assert agent.calls == []
    [(due, _, outcome, run_id, _)] = rows(conn)
    assert (due, outcome, run_id) == (at(window, 9), "missed", None)


def test_a_worker_clock_that_runs_fast_never_starts_a_run_early(schema_url, conn, allowed):
    agent = FakeAgent(schema_url)
    fast = Clock(at(WED, 14, 5))
    sched = scheduler(schema_url, agent, clock=fast, db_clock=lambda c: at(WED, 13, 58))
    sched.tick(fast())
    assert agent.calls == [] and rows(conn) == []
    assert sched.next_due(fast()) > fast()          # waits for the database's clock, never spins
    sched.db_clock = lambda c: at(WED, 14, 0)
    tick(sched, sched.next_due(fast()))
    assert len(agent.calls) == 1


def test_the_main_loop_lets_go_of_a_stuck_run(schema_url, conn, allowed):
    """Round 3, major 5: next_due() said next week while a run was going, so the loop never reached the
    supervisor's deadline. Through the worker's own loop, with no other round due."""
    gate = threading.Event()
    agent = FakeAgent(schema_url, gate=gate)
    agent.gate_timeout = 30
    clock = Clock(at(WED, 13, 50), at(WED, 14) + agent_schedule.STUCK_AFTER + timedelta(minutes=5))
    sched = scheduler(schema_url, agent, clock=clock, background=True)
    try:
        assert worker.main(["--every", "1440", "--at-minute", "off", "--port", "0"], jobs=[("a", lambda: 0)],
                           sleep=clock.sleep, log=lambda _: None, clock=clock, scheduler=sched) == 0
        [(_, _, outcome, _, detail)] = rows(conn)
        assert (outcome, detail) == ("failed", agent_schedule.ABANDONED)
    finally:
        gate.set()


def test_the_schedulers_database_calls_have_time_limits(schema_url):
    sched = scheduler(schema_url, FakeAgent(schema_url))
    with sched.connect() as c:
        limit = f"{agent_schedule.DB_TIMEOUT:g}s"
        assert c.execute("select current_setting('statement_timeout'), current_setting('lock_timeout')"
                         ).fetchone() == (limit, limit)


def test_an_agent_run_whose_ending_wasnt_recorded_stays_in_needs_attention(schema_url, conn, allowed):
    """Round 3, major 6: the agent's ending write failed, so agent_runs said `running` while the schedule said
    `skipped`, and newer rows hid it. The line stays, however old, until the agent's row has an ending."""
    class Unrecorded(FakeAgent):
        def __call__(self, live, authorize=None):
            with open_in(*self.url) as c:
                run_id = agent_runs.start(c, mode="dry-run", model="fake")
            self.calls.append(live)
            return run_id, SimpleNamespace(status="skipped", detail="its ending couldn't be recorded")
    sched = scheduler(schema_url, Unrecorded(schema_url))
    tick(sched, at(WED, 14))
    [(_, _, outcome, run_id, _)] = rows(conn)
    assert outcome == "skipped"
    for week in range(1, 4):            # newer, quiet runs; the old one is past the week's look-back
        sched.run_agent = FakeAgent(schema_url)
        tick(sched, at(WED + timedelta(days=7 * week), 14))
    lines = attention(conn, at(WED + timedelta(days=21), 15))
    stuck = [text for level, text in lines if level == "bad"]
    assert len(stuck) == 1 and f"agent run {run_id}" in stuck[0] and "still says running" in stuck[0]
    agent_runs.finish(conn, run_id, "failed", detail="fixed by hand", cost_usd=Decimal(0))
    assert not [1 for level, _ in attention(conn, at(WED + timedelta(days=21), 15)) if level == "bad"]


# --- Sol review, round 4 ---------------------------------------------------------------------------

def test_a_start_held_up_by_a_lock_past_its_latest_start_doesnt_start(schema_url, conn, allowed):
    """Round 4, finding 2: the database's clock was read before `for share` waited on a lock, so a start held up
    past its latest start still went ahead. Now the locks come first, then one reading of the clock."""
    now = agent_schedule.database_now(conn)
    due, latest = now - timedelta(minutes=1), now + timedelta(seconds=1.5)
    assert agent_schedule.record(conn, due, latest, "dry-run", "running",
                                 clock=lambda: agent_schedule.database_now(conn)) == ("dry-run", "running")
    locked, done = threading.Event(), threading.Event()

    def hold_config():
        with open_in(*schema_url) as other, other.transaction():
            other.execute("select from config where key = %s for update", (agent_schedule.DB_MODE_KEY,))
            locked.set()
            done.wait(2.5)
    holder = threading.Thread(target=hold_config, daemon=True)
    holder.start()
    assert locked.wait(10)
    sched = scheduler(schema_url, FakeAgent(schema_url), db_clock=agent_schedule.database_now)
    refused: dict = {}
    claim = agent_schedule.Claim(due, latest, "dry-run", "")
    with open_in(*schema_url) as c, c.transaction():
        why = sched._authorize(claim, refused)(c)
    holder.join(10)
    assert why is not None and refused["ending"][0] == "missed"
    assert conn.execute("select started_at from agent_schedule where due_at = %s", (due,)).fetchone()[0] is None


def test_the_main_loop_survives_crossing_the_supervisors_deadline_during_a_round(schema_url, conn, allowed):
    """Round 4, finding 3: an hourly round ran past the stuck run's supervisor deadline, so the wake time was in
    the past and the sleep negative (-299 s), which raised and ended the worker."""
    gate = threading.Event()
    agent = FakeAgent(schema_url, gate=gate)
    agent.gate_timeout = 30
    clock = Clock(at(WED, 13, 50), at(WED, 14) + agent_schedule.STUCK_AFTER + timedelta(minutes=30))
    rounds = []

    def slow_round():
        rounds.append(clock())
        if len(rounds) > 1:
            clock.now += timedelta(minutes=20)      # each later round takes 20 minutes
        return 0
    sched = scheduler(schema_url, agent, clock=clock, background=True)
    try:
        assert worker.main(["--every", "60", "--at-minute", "5", "--port", "0"], jobs=[("a", slow_round)],
                           sleep=clock.sleep, log=lambda _: None, clock=clock, scheduler=sched) == 0
        deadline = at(WED, 14) + agent_schedule.STUCK_AFTER
        assert any(r < deadline < r + timedelta(minutes=20) for r in rounds), "no round crossed the deadline"
        [(_, _, outcome, _, detail)] = rows(conn)
        assert (outcome, detail) == ("failed", agent_schedule.ABANDONED)
    finally:
        gate.set()


def test_an_overdue_supervisor_deadline_is_now_not_the_past(schema_url, allowed):
    gate = threading.Event()
    sched = scheduler(schema_url, FakeAgent(schema_url, gate=gate), background=True)
    try:
        tick(sched, at(WED, 14))
        late = at(WED, 14) + agent_schedule.STUCK_AFTER + timedelta(minutes=5)
        assert sched.next_due(late) == late
    finally:
        gate.set()


# --- Sol review, round 5 ---------------------------------------------------------------------------

def until(check, seconds=10):
    """Wait in real time for a background thread to get somewhere."""
    end = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    while not check():
        if datetime.now(timezone.utc) > end:
            return False
        threading.Event().wait(0.01)
    return True


TWO_WINDOWS = Schedule("dry-run", (Entry(2, time(14), time(14, 10)), Entry(2, time(14, 20), time(14, 30))),
                       date(2026, 12, 2))


def test_a_run_that_ends_wakes_the_loop_so_a_second_window_that_day_runs(schema_url, conn, allowed):
    """Round 5, finding 1: with 2:00-2:10 and 2:20-2:30, the first run ended during the sleep after the 2:05 sync,
    but the loop slept on to the 3:05 sync, so 2:20 was recorded missed. A run's end now wakes the loop."""
    gate = threading.Event()
    clock = Clock(at(WED, 13, 50), at(WED, 15, 30))
    agent = FakeAgent(schema_url, clock, gate=gate)
    logged = []
    sched = scheduler(schema_url, agent, TWO_WINDOWS, clock=clock, background=True, log=logged.append)
    woken = []

    def wait(event, seconds):
        """The loop's wait on the fake clock. A run's thread goes on in real time, so it is let reach its gate (or
        its end) before the clock moves; the first run is let go during the wait after the 2:05 sync."""
        run = sched.thread
        if run is not None and run.is_alive():
            if gate.is_set():
                run.join(10)
            else:
                assert until(lambda: agent.calls), "the run never reached its gate"
        if event.is_set():
            return True
        if clock.now >= at(WED, 14, 5) and not gate.is_set():
            gate.set()
            assert event.wait(10), "the run's end didn't wake the loop"
            clock.now += timedelta(minutes=2)       # it ended at 2:07
            woken.append(clock.now)
            return True
        clock.sleep(seconds)
        return False
    try:
        assert worker.main(["--every", "60", "--at-minute", "5", "--port", "0"], jobs=[("a", lambda: 0)],
                           sleep=clock.sleep, wait=wait, log=logged.append, clock=clock, scheduler=sched) == 0
    finally:
        gate.set()
        if sched.thread is not None:
            sched.thread.join(10)
    assert woken == [at(WED, 14, 7)]
    assert [when for when, _ in agent.calls] == [at(WED, 14), at(WED, 14, 20)]
    assert [(due, outcome) for due, _, outcome, _, _ in rows(conn)] == [(at(WED, 14), "skipped"),
                                                                         (at(WED, 14, 20), "skipped")]


def test_a_thread_that_has_woken_the_loop_is_not_a_run_still_going(schema_url, conn, allowed):
    """A run's thread wakes the loop, then ends a moment later. Woken in that moment, `next_due` must not read the
    still-alive thread as a run going on and sleep to the supervisor's deadline."""
    gate = threading.Event()
    sched = scheduler(schema_url, FakeAgent(schema_url, gate=gate), TWO_WINDOWS, background=True)
    tick(sched, at(WED, 14))
    gate.set()
    assert sched.run_ended.wait(10)
    sched.thread.join(10)
    sched.thread = SimpleNamespace(is_alive=lambda: True)      # as it is in the moment before it ends
    assert sched.next_due(at(WED, 14, 7)) == at(WED, 14, 20)


def test_the_real_wait_returns_as_soon_as_a_run_ends():
    event = threading.Event()
    threading.Timer(0.2, event.set).start()
    started = datetime.now(timezone.utc)
    assert worker.wait_or_wake(event, 30) is True
    assert datetime.now(timezone.utc) - started < timedelta(seconds=5)
    assert worker.wait_or_wake(threading.Event(), 0.05) is False


def test_a_row_left_running_under_a_live_lease_is_swept_when_the_lease_expires(schema_url, conn, allowed):
    """Round 5, finding 2: a worker restarted before its old run's lease expired; the row isn't swept then (the
    run may still be going), and next_due said next week, so it stayed `running` for a week."""
    from vending import run_lease

    conn.execute("insert into agent_schedule (due_at, latest_at, mode) values (%s, %s, 'dry-run')",
                 (at(WED, 14), at(WED, 22)))
    held = run_lease.take(conn, deadline=3600)
    conn.execute("update agent_run_lease set expires_at = clock_timestamp() + interval '3 minutes'")
    sched = scheduler(schema_url, FakeAgent(schema_url))
    tick(sched, at(WED, 14, 20))
    assert rows(conn)[0][2] == "running"
    wake = sched.next_due(at(WED, 14, 20))
    assert at(WED, 14, 23) <= wake <= at(WED, 14, 23) + agent_schedule.SWEEP_MARGIN + timedelta(seconds=5)
    # The old run's lease expires: the next tick sweeps the row, and then nothing is due till next week.
    conn.execute("update agent_run_lease set expires_at = clock_timestamp() - interval '1 second'")
    tick(sched, wake)
    [(_, _, outcome, _, detail)] = rows(conn)
    assert (outcome, detail) == ("failed", agent_schedule.STOPPED)
    assert sched.next_due(wake) == at(WED + timedelta(days=7), 14)
    run_lease.give_back(conn, held.token)


def test_two_windows_with_the_same_start_are_one_run_with_the_later_cutoff(schema_url, conn, allowed):
    """Round 5, finding 3: `wed 14:00-14:30, wed 14:00-15:00` recorded 14:00 as missed, then couldn't claim it."""
    same = Schedule("dry-run", (Entry(2, time(14), time(14, 30)), Entry(2, time(14), time(15))), date(2026, 12, 2))
    assert agent_schedule.due_times(same, at(WED, 14, 40)) == [(at(WED, 14), at(WED, 15))]
    agent = FakeAgent(schema_url)
    tick(scheduler(schema_url, agent, same), at(WED, 14, 40))
    assert len(agent.calls) == 1
    [(due, _, outcome, _, _)] = rows(conn)
    assert (due, outcome) == (at(WED, 14), "skipped")
    assert conn.execute("select latest_at from agent_schedule").fetchone()[0] == at(WED, 15)
