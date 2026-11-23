"""Live scheduled restock runs (#213): live only when both switches say live, never before the start, and only
through `vending.agent.run(live=True)`, so every live-run rule (halt switch, caps, run lease, one submit, the
unresolved-submission block) applies unchanged. The agent's entry point is faked where the test is about the
scheduler; the one-submit test goes through the real agent, the MCP server and the loopback fake bridge."""

import html
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from tests.test_agent_schedule import (  # noqa: F401 - the autouse fixture clears the schedule settings
    LIVE, Clock, _no_schedule_settings, at)
from vending import agent_schedule, dashboard, settings, worker
from vending.agent_schedule import Schedule

ET = ZoneInfo("America/New_York")
WED = datetime(2026, 12, 2, tzinfo=ET)          # the first day with runs: before the start (Thu 3 Dec)
WED2 = datetime(2026, 12, 9, tzinfo=ET)         # the first Wednesday after the start
# From the first Wednesday after the start, so a run there has no older time in its week to record as missed.
LIVE9 = Schedule("live", LIVE.entries, WED2.date())
DRY9 = Schedule("dry-run", LIVE.entries, WED2.date())


# --- No database -------------------------------------------------------------------------------------

def test_the_container_setting_live_is_kept_as_live(monkeypatch):
    monkeypatch.setenv(agent_schedule.MODE_SETTING, "live")
    sched = agent_schedule.default_scheduler(log=lambda _: None)
    assert sched.mode == "live" and sched.public()["mode"] == "live"


def test_a_missing_model_key_fails_before_any_request_and_shows(monkeypatch):
    from vending import agent
    from vending.harness import DEFAULT_KEY_ENV

    monkeypatch.delenv(DEFAULT_KEY_ENV, raising=False)
    monkeypatch.setattr(settings, "_FILE", {k: v for k, v in settings._FILE.items() if k != DEFAULT_KEY_ENV})

    def never(**_):
        raise AssertionError("no run may start without the model key")
    monkeypatch.setattr(agent, "run", never)
    run_id, out = agent_schedule.default_run_agent(True)
    assert run_id is None and out.status == "failed" and out.detail.startswith(agent_schedule.MODEL_KEY_MISSING)
    # /health says so before the time comes, and the dashboard and the home page show it.
    shown = agent_schedule.Scheduler(LIVE, log=lambda _: None).public()
    assert shown["model_key"] is False
    lines = dashboard._scheduled_runs(None, {"agent_schedule": shown}, at(WED2, 13))
    assert ("bad", agent_schedule.NO_KEY_LINE) in lines
    assert "model key missing" in html.unescape(worker.home_page({"rounds": 1, "status": "ok", "results": [],
                                                                  "agent_schedule": shown}))
    monkeypatch.setenv(DEFAULT_KEY_ENV, "sk-test")
    assert agent_schedule.Scheduler(LIVE, log=lambda _: None).public()["model_key"] is True


def test_the_dashboard_says_live_or_dry_and_what_was_sent():
    now = at(WED2, 15)

    def line(outcome, mode="live", **kw):
        return dashboard._scheduled_line({"due_at": at(WED2, 14), "mode": mode, "outcome": outcome, "run_id": 7,
                                          "claimed_at": at(WED2, 14), **kw}, now)
    level, text = line("submitted", request_id="pr-1", submission=3)
    assert level == "info"
    assert text == ("The scheduled live run due Wed 09 Dec, 2:00 PM ET submitted a restock request (agent run 7). "
                    "Sent: request pr-1 (submission 3).")
    assert line("skipped", "dry-run")[1] == ("The scheduled dry run due Wed 09 Dec, 2:00 PM ET skipped (agent run 7). "
                                             "Nothing sent. `python -m vending.agent_schedule` says why.")
    assert "live run" in line("failed")[1] and "Nothing sent." in line("failed")[1]
    assert "Nothing sent." in line("missed", run_id=None)[1]
    assert "sent" not in line("running")[1].lower()
    # A dry run never submits: a dry row that says submitted is no line.
    assert line("submitted", "dry-run") is None


def test_the_dashboard_says_when_both_switches_are_live_or_only_one():
    def lines(container, in_db):
        panel = {"ok": True, "data": {"latest": None, "incidents": [], "setting_ok": True,
                                      "setting_live": in_db == "live"}}
        return dashboard._scheduled_runs(panel, {"agent_schedule": {"mode": container, "setting": container,
                                                                    "model_key": True}}, at(WED2, 13))
    assert ("warn", agent_schedule.LIVE_ON) in lines("live", "live")
    assert ("info", agent_schedule.HALF_LIVE) in lines("live", "dry-run")
    assert ("info", agent_schedule.HALF_LIVE) in lines("dry-run", "live")
    assert not [t for _, t in lines("dry-run", "dry-run") if "live" in t.lower()]


def test_the_docs_say_how_to_turn_live_on_and_off_and_that_the_team_agrees_first():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for name in ("agent.md", "deploy.md"):
        doc = (root / "docs" / name).read_text(encoding="utf-8")
        assert "python -m vending.config set agent_schedule_mode live" in doc, name
        assert "python -m vending.config set agent_schedule_mode off" in doc, name
        assert "team" in doc and "agree" in doc, name
        assert "dry runs only" not in doc.lower(), name


# --- With the database --------------------------------------------------------------------------------

psycopg = pytest.importorskip("psycopg")

from tests.conftest import open_in  # noqa: E402
from tests.test_agent_schedule import (  # noqa: E402
    FakeAgent, _around_the_claim, _real_entry_point, _set_from_another_connection, attention, db_mode, rows,
    scheduler, tick)
from vending import config  # noqa: E402
from vending.config import set_halted  # noqa: E402


def fresh_slots(conn, when):
    """The worker's hourly `slots check` confirmed the assignment just before `when`, after the lock."""
    conn.execute("update slot_assignment set last_checked_at = %s where id = 1", (when - timedelta(minutes=30),))


@pytest.fixture
def ready(conn):
    """Both switches can say live; the slots were confirmed half an hour before the first live time."""
    db_mode(conn, "live")
    fresh_slots(conn, at(WED2, 14))


def run_lines(conn, now):
    """Needs attention's lines on each scheduled run (not the lines on the settings)."""
    return [line for line in attention(conn, now) if line[1].startswith("The scheduled ")]


def open_submission(conn, status="unknown"):
    return conn.execute("insert into purchase_submissions (plan_hash, body, status) values ('h', %s, %s)"
                        " returning id", ('{"lines": []}', status)).fetchone()[0]


@pytest.mark.parametrize("container, in_db, called, mode", [
    ("live", "live", [True], "live"),
    ("live", "dry-run", [False], "dry-run"),
    ("dry-run", "live", [False], "dry-run"),
    ("live", "off", [], "off"),
    ("dry-run", "dry-run", [False], "dry-run"),
])
def test_live_only_when_both_switches_say_live(schema_url, conn, ready, container, in_db, called, mode):
    db_mode(conn, in_db)
    agent = FakeAgent(schema_url)
    tick(scheduler(schema_url, agent, LIVE9 if container == "live" else DRY9, clock=Clock(at(WED2, 13))), at(WED2, 14))
    assert [live for _, live in agent.calls] == called
    assert rows(conn)[0][1] == mode


def test_before_the_start_a_live_run_is_a_dry_run_and_says_why(schema_url, conn, ready):
    agent = FakeAgent(schema_url)
    tick(scheduler(schema_url, agent, LIVE), at(WED, 14))
    assert [live for _, live in agent.calls] == [False]
    [(_, mode, outcome, _, detail)] = rows(conn)
    assert (mode, outcome) == ("dry-run", "skipped")
    assert detail.startswith("Run as a dry run, not live:") and "Thu 03 Dec" in detail and "never live" in detail


def test_a_live_claim_before_the_start_never_runs_live_even_if_the_check_is_passed(schema_url, conn, ready,
                                                                                  monkeypatch):
    """The lease transaction checks the start again, by the database's clock: a live claim there refuses."""
    clock = Clock(at(WED2, 13))
    sched = scheduler(schema_url, FakeAgent(schema_url), LIVE, clock=clock)
    claim = agent_schedule.Claim(at(WED2, 14), at(WED2, 22), "live", "")
    sched.db_clock = lambda c: at(WED, 14)
    refused = {}
    with open_in(*schema_url) as c, c.transaction():
        c.execute("insert into agent_schedule (due_at, latest_at, mode) values (%s, %s, 'live')",
                  (claim.due, claim.latest))
        reason = sched._authorize(claim, refused)(c)
    assert reason is not None and "never live" in reason


def test_the_halt_switch_stops_a_live_scheduled_run_before_any_model_request(schema_url, conn, ready):
    set_halted(conn, True, "test")
    tick(scheduler(schema_url, _real_entry_point(schema_url), LIVE9, clock=Clock(at(WED2, 13))), at(WED2, 14))
    [(_, mode, outcome, run_id, _)] = rows(conn)
    assert (mode, outcome) == ("live", "halted")
    assert conn.execute("select mode, status from agent_runs where id = %s", (run_id,)).fetchone() == ("live", "halted")
    assert conn.execute("select count(*) from model_calls").fetchone()[0] == 0


def test_an_unresolved_submission_makes_it_a_dry_run(schema_url, conn, ready):
    row = open_submission(conn)
    agent = FakeAgent(schema_url)
    tick(scheduler(schema_url, agent, LIVE9, clock=Clock(at(WED2, 13))), at(WED2, 14))
    assert [live for _, live in agent.calls] == [False]
    [(_, mode, _, _, detail)] = rows(conn)
    assert mode == "dry-run" and "ORDERS BLOCKED" in detail and f"submission {row}" in detail
    assert "Nothing sent." in run_lines(conn, at(WED2, 15))[0][1]


def test_a_submission_left_open_after_the_claim_stops_the_live_run(schema_url, conn, ready, monkeypatch):
    """Between the claim and the lease (a run by hand ended with an unknown submit): the lease's own
    transaction checks again, and nothing runs."""
    agent = FakeAgent(schema_url)

    def entry(live, authorize=None):
        assert live        # claimed, checked and still live: then a submission is left open
        with open_in(*schema_url) as other:
            open_submission(other)
        return agent(live, authorize)
    tick(scheduler(schema_url, entry, LIVE9, clock=Clock(at(WED2, 13))), at(WED2, 14))
    assert agent.calls == []
    [(_, mode, outcome, run_id, detail)] = rows(conn)
    assert (mode, outcome, run_id) == ("live", "skipped", None) and "ORDERS BLOCKED" in detail


@pytest.mark.parametrize("checked", [
    pytest.param(lambda: at(WED2, 14) - timedelta(days=1), id="a day old"),
    pytest.param(lambda: at(WED, 12), id="not confirmed since the start"),
])
def test_a_stale_slot_check_makes_it_a_dry_run(schema_url, conn, ready, checked):
    conn.execute("update slot_assignment set last_checked_at = %s where id = 1", (checked(),))
    agent = FakeAgent(schema_url)
    tick(scheduler(schema_url, agent, LIVE9, clock=Clock(at(WED2, 13))), at(WED2, 14))
    assert [live for _, live in agent.calls] == [False]
    [(_, mode, _, _, detail)] = rows(conn)
    assert mode == "dry-run" and "slot" in detail and "vending.slots" in detail


def test_the_database_setting_lowered_after_a_live_claim_stops_the_run(schema_url, conn, ready, monkeypatch):
    set_halted(conn, False, "test")
    _around_the_claim(monkeypatch, after=lambda: _set_from_another_connection(schema_url, "dry-run"))
    tick(scheduler(schema_url, _real_entry_point(schema_url), LIVE9, clock=Clock(at(WED2, 13))), at(WED2, 14))
    [(_, mode, outcome, run_id, detail)] = rows(conn)
    assert (mode, outcome, run_id) == ("live", "skipped", None)
    assert detail.startswith("Not started: agent_schedule_mode changed to dry-run")
    assert conn.execute("select count(*) from agent_runs").fetchone()[0] == 0


def test_a_live_run_that_submits_is_recorded_with_what_it_sent(schema_url, conn, ready):
    agent = FakeAgent(schema_url, status="submitted", detail="Submitted request pr-7.")
    sched = scheduler(schema_url, agent, LIVE9, clock=Clock(at(WED2, 13)))
    tick(sched, at(WED2, 14))
    [(_, mode, outcome, run_id, _)] = rows(conn)
    assert (mode, outcome) == ("live", "submitted") and run_id is not None
    shown = worker.State(schedule=sched).public()["agent_schedule"]
    assert shown["mode"] == "live" and shown["last"]["outcome"] == "submitted"


def test_turning_the_database_setting_off_skips_the_next_time(schema_url, conn, ready, monkeypatch, capsys):
    from vending import db

    agent = FakeAgent(schema_url)
    sched = scheduler(schema_url, agent, LIVE9, clock=Clock(at(WED2, 13)))
    tick(sched, at(WED2, 14))
    assert [live for _, live in agent.calls] == [True]
    # One command, as the docs give it.
    monkeypatch.setattr(db, "connect", lambda: open_in(*schema_url))
    assert config.main(["set", agent_schedule.DB_MODE_KEY, "off", "--by", "test"]) == 0
    fresh_slots(conn, at(WED2 + timedelta(days=7), 14))
    tick(sched, at(WED2 + timedelta(days=7), 14))
    assert len(agent.calls) == 1
    assert [r[1:3] for r in rows(conn)][1] == ("off", "skipped")


def test_the_table_holds_live_runs_and_their_submits(conn):
    conn.execute("insert into agent_schedule (due_at, latest_at, mode, outcome, finished_at)"
                 " values (%s, %s, 'live', 'submitted', now())", (at(WED2, 14), at(WED2, 22)))
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("insert into agent_schedule (due_at, latest_at, mode) values (%s, %s, 'on')",
                     (at(WED2 + timedelta(days=7), 14), at(WED2 + timedelta(days=7), 22)))


# --- One submit at most: the real agent, the MCP server and the fake bridge -----------------------------

pytest.importorskip("anthropic")

from tests.test_agent import Script, answer, tool_turn, use  # noqa: E402
from tests.test_desired_state import NOW, coke_sourcing, desired, fake, restock_15  # noqa: E402,F401
from tests.test_server import db  # noqa: E402,F401
from tests.test_submit import monday, prepared  # noqa: E402,F401
from tests.test_submit_runs import BODY, mcp_tools  # noqa: E402


def test_a_live_scheduled_run_sends_one_request_at_most(db, fake, schema_url, conn, monday):  # noqa: F811
    import anyio

    from vending import agent

    prepared(db)
    db_mode(conn, "live")
    fresh_slots(conn, at(WED2, 14))
    # The model previews, submits, then tries again twice: only the first reaches the bridge.
    client = Script(tool_turn(use("preview_desired_state", {"desired": BODY}, id="p1")),
                    tool_turn(use("submit_desired_state", {"desired": BODY}, id="s1")),
                    tool_turn(use("preview_desired_state", {"desired": BODY}, id="p2")),
                    tool_turn(use("submit_desired_state", {"desired": BODY}, id="s2")),
                    answer("submit"))

    def entry(live, authorize=None):
        return anyio.run(lambda: agent.run(live=live, connect=lambda: open_in(*schema_url), client=client,
                                           now=lambda: NOW, open_tools=mcp_tools(), bridge=fake.client,
                                           authorize=authorize))
    tick(scheduler(schema_url, entry, LIVE9, clock=Clock(at(WED2, 13))), at(WED2, 14))
    assert len(fake.posts) == 1
    [(_, mode, outcome, run_id, _)] = rows(conn)
    assert (mode, outcome) == ("live", "submitted")
    assert conn.execute("select mode, status, request_id from agent_runs where id = %s",
                        (run_id,)).fetchone() == ("live", "submitted", "pr-1")
    [(level, text)] = run_lines(conn, at(WED2, 15))
    assert level == "info" and "live run" in text and "Sent: request pr-1 (submission 1)." in text
