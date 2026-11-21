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
    (agent_schedule.MODE_SETTING, "on"),
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
                                       "`python -m vending.agent_schedule` says why.")
    assert line("skipped", "off", run_id=None)[1].startswith(
        "The scheduled run due Wed 02 Dec, 2:00 PM ET skipped: `agent_schedule_mode` is off in the database.")
    assert line("submitted", "live")[1].startswith("The scheduled live run")
    assert line("running") == ("info", "The scheduled dry run due Wed 02 Dec, 2:00 PM ET is running now (agent run 7).")
    assert line("<b>") is None


def test_a_run_stuck_at_running_is_bad_on_the_dashboard():
    claimed = at(WED, 14)
    row = {"due_at": claimed, "mode": "live", "outcome": "running", "run_id": None, "claimed_at": claimed}
    assert dashboard._scheduled_line(row, claimed + timedelta(minutes=30))[0] == "info"
    level, text = dashboard._scheduled_line(row, claimed + agent_schedule.STUCK_AFTER + timedelta(minutes=1))
    assert level == "bad" and "running" in text and "stopped mid-run" in text


def test_the_home_page_shows_the_schedule_only_when_it_is_on():
    shown = {"rounds": 1, "status": "ok", "results": [], "agent_schedule": {
        "mode": "dry-run", "next_at": at(WED, 14).isoformat(),
        "last": {"due_at": at(WED - timedelta(days=7), 14).isoformat(), "outcome": "<b>", "mode": "dry-run"}}}
    page = html.unescape(worker.home_page(shown))
    assert "Scheduled agent run" in page and "at most dry runs; next Wed 02 Dec, 2:00 PM ET" in page
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

    def __call__(self, live):
        self.calls.append((self.clock() if self.clock else None, live))
        if self.gate is not None:
            self.gate.wait(10)
        if self.raises is not None:
            raise self.raises
        with open_in(*self.url) as c:
            run_id = agent_runs.start(c, mode="live" if live else "dry-run", model="fake")
            agent_runs.finish(c, run_id, self.status, detail=self.detail, cost_usd=Decimal(0))
        if self.then is not None:
            self.then()
        return run_id, SimpleNamespace(status=self.status, detail=self.detail)


def scheduler(schema_url, agent, sched=DRY, clock=None, **kw):
    return Scheduler(sched, run_agent=agent, connect=kw.pop("connect", lambda: open_in(*schema_url)),
                     log=kw.pop("log", lambda _: None), background=kw.pop("background", False),
                     clock=clock or Clock(at(WED, 0)), **kw)


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
    """The database lets the worker run what its own setting says (`live` is the highest)."""
    db_mode(conn, "live")


def attention(conn, now, syncs=None):
    return [(a["level"], a["text"]) for a in dashboard.snapshot(conn, now=now, syncs=syncs)["attention"]
            if a["text"].startswith(("The scheduled ", "Scheduled agent runs"))]


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
    runs a dry run, whatever its own setting."""
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
    assert attention(conn, at(WED, 14, 30), syncs=shown) == [(level, text)]


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


def test_the_clock_is_read_again_just_before_the_claim(schema_url, conn, allowed):
    """The same, when it is the database's reads under the lock that are slow."""
    agent = FakeAgent(schema_url)
    clock = Clock(at(WED, 21, 59))
    sched = scheduler(schema_url, agent, clock=clock)
    real = agent_schedule.database_mode

    def slow(conn_):
        clock.now = at(WED, 22, 0)
        return real(conn_)
    import unittest.mock
    with unittest.mock.patch.object(agent_schedule, "database_mode", slow):
        sched.tick(at(WED, 21, 59))
    assert agent.calls == [] and rows(conn)[0][2] == "missed"


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
    """Finding 2: the agent's own entry point holds one lock for every run, by hand or scheduled."""
    import anyio

    from vending import agent
    from vending.config import set_halted

    set_halted(conn, False, "test")

    def never(*a, **k):
        raise AssertionError("a blocked run must not open the tools or call the model")

    def real_entry_point(live):
        return anyio.run(lambda: agent.run(live=live, connect=lambda: open_in(*schema_url), client=never,
                                           open_tools=never))
    by_hand = agent_runs.start(conn, mode="dry-run", model="by hand")
    with open_in(*schema_url) as hand:
        assert hand.execute("select pg_try_advisory_lock(%s)", (agent.RUN_LOCK,)).fetchone()[0]
        tick(scheduler(schema_url, real_entry_point), at(WED, 14))
        hand.execute("select pg_advisory_unlock(%s)", (agent.RUN_LOCK,))
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


@pytest.mark.parametrize("status", ["skipped", "halted", "failed", "submitted"])
def test_each_ending_of_the_agent_is_recorded(schema_url, conn, allowed, status):
    agent = FakeAgent(schema_url, status=status, detail=f"why it {status}")
    sched = scheduler(schema_url, agent, LIVE)
    tick(sched, at(WED, 14))
    assert agent.calls == [(None, True)]
    [(_, mode, outcome, run_id, detail)] = rows(conn)
    assert (mode, outcome, detail) == ("live", status, f"why it {status}") and run_id is not None
    assert sched.public()["last"]["outcome"] == status


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
    with pytest.raises(ValueError, match="agent_schedule_mode must be one of"):
        db_mode(conn, "on")
    db_mode(conn, "dry-run")
    assert agent_schedule.database_mode(conn) == "dry-run"
