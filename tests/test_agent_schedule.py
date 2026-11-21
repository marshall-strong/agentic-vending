"""Scheduled agent runs from the worker (#213). The agent's entry point is faked: no model call, no bridge.

Time is a fake clock that only `sleep` moves, so the worker's loop runs a scheduled week in a moment."""

import html
import threading
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from vending import agent_schedule, dashboard, settings, worker
from vending.agent_schedule import Entry, Schedule, Scheduler

ET = ZoneInfo("America/New_York")
NAMES = (agent_schedule.MODE_SETTING, agent_schedule.TIMES_SETTING, agent_schedule.FROM_SETTING)
WED = datetime(2026, 12, 2, tzinfo=ET)          # the first day with runs, by default
DRY = Schedule("dry-run", (Entry(2, time(14), time(22)),), date(2026, 12, 2))


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
    """Time moves only when the loop sleeps. Past `stop` the sleep interrupts, as Ctrl+C would."""

    def __init__(self, start, stop):
        self.now, self.stop = start, stop

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


def test_the_real_worker_builds_no_scheduler_when_the_setting_is_unset(monkeypatch):
    made = []
    monkeypatch.setattr(agent_schedule, "Scheduler", lambda *a, **k: made.append(1))
    monkeypatch.setattr(worker, "default_jobs", lambda: [("a", lambda: 0)])
    from vending import visit_polls
    monkeypatch.setattr(visit_polls, "default_reader", lambda log: None)
    clock = Clock(at(WED, 13), at(WED, 15))
    assert worker.main(["--port", "0"], sleep=clock.sleep, log=lambda _: None, clock=clock) == 0
    assert made == []


def test_the_defaults_are_one_wednesday_afternoon_run_from_2_dec(monkeypatch):
    monkeypatch.setenv(agent_schedule.MODE_SETTING, "Dry-Run")
    sched = agent_schedule.schedule()
    assert sched == DRY and not sched.live
    monkeypatch.setenv(agent_schedule.MODE_SETTING, "live")
    assert agent_schedule.schedule().live


def test_the_times_and_first_day_are_settings(monkeypatch):
    monkeypatch.setenv(agent_schedule.MODE_SETTING, "live")
    monkeypatch.setenv(agent_schedule.TIMES_SETTING, "Wednesday 13:30-21:00, thu 09:00-12:00")
    monkeypatch.setenv(agent_schedule.FROM_SETTING, "2026-12-09")
    sched = agent_schedule.schedule()
    assert sched.entries == (Entry(2, time(13, 30), time(21)), Entry(3, time(9), time(12)))
    assert sched.first_day == date(2026, 12, 9)


@pytest.mark.parametrize("name, value", [
    (agent_schedule.MODE_SETTING, "on"),
    (agent_schedule.TIMES_SETTING, "wed 14:00"),             # no latest start
    (agent_schedule.TIMES_SETTING, "wed 22:00-14:00"),       # latest before due
    (agent_schedule.TIMES_SETTING, "someday 14:00-15:00"),
    (agent_schedule.TIMES_SETTING, "wed 25:00-26:00"),
    (agent_schedule.TIMES_SETTING, " , "),
    (agent_schedule.FROM_SETTING, "2 Dec"),
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
    level, text = dashboard._scheduled_run(shown)
    assert level == "bad" and "setting is wrong" in text


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
    def line(outcome, mode="dry-run"):
        last = {"due_at": at(WED, 14).isoformat(), "mode": mode, "outcome": outcome, "run_id": 7}
        return dashboard._scheduled_run({"agent_schedule": {"mode": mode, "last": last}})
    assert line("failed")[0] == "bad" and "failed (agent run 7)" in line("failed")[1]
    assert line("missed")[0] == "bad" and "missed" in line("missed")[1]
    assert line("halted")[0] == "warn" and "halt switch" in line("halted")[1]
    assert line("skipped") == ("info", "The scheduled dry run due Wed 02 Dec, 2:00 PM ET skipped (agent run 7). "
                                       "`python -m vending.agent_schedule` says why.")
    assert line("submitted", "live")[1].startswith("The scheduled live run")
    assert dashboard._scheduled_run(None) is None
    assert dashboard._scheduled_run({"rounds": 1}) is None
    assert dashboard._scheduled_run({"agent_schedule": {"mode": "dry-run", "last": None}}) is None


def test_the_home_page_shows_the_schedule_only_when_it_is_on():
    shown = {"rounds": 1, "status": "ok", "results": [], "agent_schedule": {
        "mode": "dry-run", "next_at": at(WED, 14).isoformat(),
        "last": {"due_at": at(WED - timedelta(days=7), 14).isoformat(), "outcome": "<b>", "mode": "dry-run"}}}
    page = html.unescape(worker.home_page(shown))
    assert "Scheduled agent run" in page and "dry runs; next Wed 02 Dec, 2:00 PM ET" in page
    assert "unknown, due Wed 25 Nov, 2:00 PM ET" in page      # an outcome it doesn't know isn't echoed
    del shown["agent_schedule"]
    assert "Scheduled agent run" not in worker.home_page(shown)


# --- Runs, with the database --------------------------------------------------------------------

psycopg = pytest.importorskip("psycopg")

from tests.conftest import open_in  # noqa: E402
from vending import agent_runs  # noqa: E402


class FakeAgent:
    """Stands in for `vending.agent.run`: records a run in `agent_runs` as the real one does, and returns."""

    def __init__(self, url, clock=None, status="skipped", detail="Dry run: would have submitted the previewed plan.",
                 raises=None, gate=None):
        self.url, self.clock, self.status, self.detail, self.raises, self.gate = url, clock, status, detail, raises, gate
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
        return run_id, SimpleNamespace(status=self.status, detail=self.detail)


def scheduler(schema_url, agent, sched=DRY, **kw):
    return Scheduler(sched, run_agent=agent, connect=lambda: open_in(*schema_url), log=lambda _: None,
                     background=kw.pop("background", False), **kw)


def rows(conn):
    return conn.execute("select due_at, mode, outcome, run_id, detail from agent_schedule order by due_at").fetchall()


def test_the_worker_starts_a_dry_run_at_its_time_and_the_dashboard_reads_it(schema_url, conn):
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
    # /health and the dashboard read the same, without the detail.
    shown = worker.State(schedule=sched).public()
    last = shown["agent_schedule"]["last"]
    assert (last["outcome"], last["run_id"], last["mode"]) == ("skipped", run_id, "dry-run") and "detail" not in last
    assert shown["agent_schedule"]["next_at"] == at(WED + timedelta(days=7), 14).isoformat()
    level, text = dashboard._scheduled_run(shown)
    assert level == "info" and f"agent run {run_id}" in text and "due Wed 02 Dec, 2:00 PM ET" in text


def test_a_restart_never_runs_the_same_time_twice(schema_url, conn):
    agent = FakeAgent(schema_url)
    scheduler(schema_url, agent).tick(at(WED, 14))
    again = scheduler(schema_url, agent)            # the worker restarted
    assert again.next_due(at(WED, 14, 5)) == at(WED, 14, 5)    # it hasn't read the rows yet
    again.tick(at(WED, 14, 5))
    assert len(agent.calls) == 1 and len(rows(conn)) == 1
    assert again.next_due(at(WED, 14, 6)) == at(WED + timedelta(days=7), 14)


def test_a_time_missed_while_the_worker_was_down_is_recorded_and_not_run(schema_url, conn):
    agent = FakeAgent(schema_url)
    sched = scheduler(schema_url, agent)
    sched.tick(at(WED + timedelta(days=1), 10))     # back up Thursday morning, after the latest start
    assert agent.calls == []
    [(due, mode, outcome, run_id, detail)] = rows(conn)
    assert (due, outcome, run_id) == (at(WED, 14), "missed", None) and "Wed 02 Dec 10:00 PM ET" in detail
    assert sched.public()["last"]["outcome"] == "missed"
    assert dashboard._scheduled_run(worker.State(schedule=sched).public())[0] == "bad"
    sched.tick(at(WED + timedelta(days=1), 11))
    assert len(rows(conn)) == 1 and agent.calls == []


def test_a_time_reached_late_but_before_its_latest_start_runs_late(schema_url, conn):
    agent = FakeAgent(schema_url)
    scheduler(schema_url, agent).tick(at(WED, 20))
    assert [live for _, live in agent.calls] == [False]
    [(_, _, outcome, _, detail)] = rows(conn)
    assert outcome == "skipped" and detail.startswith("Started 6 h 0 min late. Dry run")


def test_only_the_newest_due_time_runs_and_the_older_ones_are_missed(schema_url, conn):
    twice = Schedule("dry-run", (Entry(2, time(9), time(22)), Entry(2, time(14), time(22))), date(2026, 12, 2))
    agent = FakeAgent(schema_url)
    scheduler(schema_url, agent, twice).tick(at(WED, 15))
    assert len(agent.calls) == 1
    assert [(d, o) for d, _, o, _, _ in rows(conn)] == [(at(WED, 9), "missed"), (at(WED, 14), "skipped")]
    assert rows(conn)[0][4] == agent_schedule.SUPERSEDED


def test_a_worker_holding_the_lock_keeps_another_from_running(schema_url, conn):
    agent = FakeAgent(schema_url)
    with open_in(*schema_url) as other:
        assert other.execute("select pg_try_advisory_lock(%s)", (agent_schedule.LOCK,)).fetchone()[0]
        sched = scheduler(schema_url, agent)
        sched.tick(at(WED, 14))
        assert agent.calls == [] and rows(conn) == []
        assert sched.next_due(at(WED, 14)) == at(WED, 14, 1)       # tries again in a minute, never spins
        other.execute("select pg_advisory_unlock(%s)", (agent_schedule.LOCK,))
    sched.tick(at(WED, 14, 1))
    assert len(agent.calls) == 1 and rows(conn)[0][2] == "skipped"


def test_a_run_still_going_by_hand_blocks_the_scheduled_one_and_that_is_recorded(schema_url, conn):
    by_hand = agent_runs.start(conn, mode="live", model="claude-opus-5-5")
    agent = FakeAgent(schema_url)
    scheduler(schema_url, agent).tick(at(WED, 14))
    assert agent.calls == []
    [(_, _, outcome, run_id, detail)] = rows(conn)
    assert (outcome, run_id) == ("skipped", None) and detail == f"Not started: agent run {by_hand} was still running."


def test_a_run_the_worker_was_stopped_in_is_failed_and_never_run_again(schema_url, conn):
    conn.execute("insert into agent_schedule (due_at, latest_at, mode) values (%s, %s, 'dry-run')",
                 (at(WED, 14), at(WED, 22)))
    agent = FakeAgent(schema_url)
    sched = scheduler(schema_url, agent)
    sched.tick(at(WED, 14, 20))
    assert agent.calls == []
    [(_, _, outcome, _, detail)] = rows(conn)
    assert (outcome, detail) == ("failed", agent_schedule.STOPPED)


@pytest.mark.parametrize("status", ["skipped", "halted", "failed", "submitted"])
def test_each_ending_of_the_agent_is_recorded(schema_url, conn, status):
    agent = FakeAgent(schema_url, status=status, detail=f"why it {status}")
    sched = scheduler(schema_url, agent, Schedule("live", DRY.entries, DRY.first_day))
    sched.tick(at(WED, 14))
    assert agent.calls == [(None, True)]
    [(_, mode, outcome, run_id, detail)] = rows(conn)
    assert (mode, outcome, detail) == ("live", status, f"why it {status}") and run_id is not None
    assert sched.public()["last"]["outcome"] == status


def test_an_entry_point_that_raises_is_a_failed_run_and_the_worker_goes_on(schema_url, conn):
    agent = FakeAgent(schema_url, raises=RuntimeError("no model key"))
    sched = scheduler(schema_url, agent)
    sched.tick(at(WED, 14))
    [(_, _, outcome, run_id, detail)] = rows(conn)
    assert (outcome, run_id) == ("failed", None) and "RuntimeError: no model key" in detail
    # The lock was given back: a later time can run.
    agent.raises = None
    sched.tick(at(WED + timedelta(days=7), 14))
    assert [r[2] for r in rows(conn)] == ["failed", "skipped"]


def test_a_database_that_is_down_is_logged_and_tried_again_in_a_minute(schema_url):
    logged = []

    def down():
        raise psycopg.OperationalError("connection refused")
    sched = Scheduler(DRY, run_agent=FakeAgent(schema_url), connect=down, log=logged.append, background=False)
    sched.tick(at(WED, 14))
    assert "OperationalError" in logged[0] and sched.next_due(at(WED, 14)) == at(WED, 14, 1)


def test_a_run_in_the_background_lets_the_loop_go_on_and_is_never_started_twice(schema_url, conn):
    gate = threading.Event()
    agent = FakeAgent(schema_url, gate=gate)
    sched = scheduler(schema_url, agent, background=True)
    sched.tick(at(WED, 14))
    assert sched.thread.is_alive()
    assert rows(conn)[0][2] == "running" and sched.public()["last"]["outcome"] == "running"
    sched.tick(at(WED, 14, 1))                      # still running: nothing new
    with open_in(*schema_url) as other:             # and another worker can't take the lock meanwhile
        assert not other.execute("select pg_try_advisory_lock(%s)", (agent_schedule.LOCK,)).fetchone()[0]
    gate.set()
    sched.thread.join(10)
    assert len(agent.calls) == 1 and rows(conn)[0][2] == "skipped"
    assert sched.public()["last"]["outcome"] == "skipped"


def test_the_show_command_lists_the_rows(schema_url, conn, monkeypatch, capsys):
    from vending import db
    monkeypatch.setattr(db, "connect", lambda: open_in(*schema_url))
    scheduler(schema_url, FakeAgent(schema_url)).tick(at(WED + timedelta(days=1), 10))
    assert agent_schedule.main([]) == 0
    out = capsys.readouterr().out
    assert "Scheduled runs are off" in out and "Wed 02 Dec 2:00 PM ET  dry-run  missed" in out
