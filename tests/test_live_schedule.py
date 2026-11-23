"""Live scheduled restock runs (#213): live only when both switches say live, never before the start, and only
through `vending.agent.run(live=True)`, so every live-run rule (halt switch, caps, run lease, one submit, the
unresolved-submission block) applies unchanged. The agent's entry point is faked where the test is about the
scheduler; the one-submit test goes through the real agent, the MCP server and the loopback fake bridge."""

import html
from datetime import datetime, timedelta, timezone
from decimal import Decimal
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


def test_an_unresolved_submission_makes_the_live_run_wait(schema_url, conn, ready):
    """Round 2, finding 4: an open submission nobody can settle from the bridge is a passing blocker. The time waits
    (no row, no dry run) and says why on /health and the dashboard; once a person settles it, the run goes live."""
    row = open_submission(conn)
    agent = FakeAgent(schema_url)
    s = scheduler(schema_url, agent, LIVE9, clock=Clock(at(WED2, 13)))
    tick(s, at(WED2, 14))
    assert agent.calls == [] and rows(conn) == []
    waiting = s.public()["waiting"]
    assert "ORDERS BLOCKED" in waiting and f"submission {row}" in waiting
    assert ("warn", waiting) in attention(conn, at(WED2, 14), syncs={"agent_schedule": s.public()})
    conn.execute("update purchase_submissions set status = 'not_filed'")
    tick(s, at(WED2, 14, 10))
    assert [live for _, live in agent.calls] == [True] and s.public()["waiting"] is None


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
def test_a_stale_slot_check_makes_the_live_run_wait(schema_url, conn, ready, checked):
    conn.execute("update slot_assignment set last_checked_at = %s where id = 1", (checked(),))
    agent = FakeAgent(schema_url)
    s = scheduler(schema_url, agent, LIVE9, clock=Clock(at(WED2, 13)))
    tick(s, at(WED2, 14))
    assert agent.calls == [] and rows(conn) == []
    assert "slot" in s.public()["waiting"] and "vending.slots" in s.public()["waiting"]


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
from tests.test_submit import prepared  # noqa: E402
from tests.test_submit_runs import BODY, mcp_tools  # noqa: E402


class SendClock:
    """The database's clock as the submit tool reads it (`ledger.db_now`): a test moves it."""

    def __init__(self, now):
        self.now = now

    def __call__(self, conn):
        return self.now


@pytest.fixture
def send_clock(monkeypatch):
    from vending import ledger

    clock = SendClock(at(WED2, 14, 5))
    monkeypatch.setattr(ledger, "db_now", clock)
    return clock


@pytest.fixture
def live_env(monkeypatch):
    """The container's own switch, as the MCP server (the worker's child) reads it at the send."""
    monkeypatch.setenv(agent_schedule.MODE_SETTING, "live")


def hooked(before_submit=None, calls=None, after=None):
    """The real in-memory MCP server, with a hook run just before each submit call reaches it, one run after each
    call, and a list of the tool calls that reached it."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def opened(context=None):
        async with mcp_tools()(context) as (listed, call_tool):
            async def call(name, arguments):
                if calls is not None:
                    calls.append(name)
                if name == "submit_desired_state" and before_submit is not None:
                    before_submit()
                result = await call_tool(name, arguments)
                if after is not None:
                    after(name)
                return result
            yield listed, call
    return opened


def scheduled_live_run(schema_url, fake, sched, when, *turns, before_submit=None, calls=None,
                       before_run=None, after=None, **kw):  # noqa: F811
    """One scheduled live run of the real agent (its model scripted), the MCP server and the loopback fake
    bridge."""
    import anyio

    from vending import agent

    turns = turns or (tool_turn(use("preview_desired_state", {"desired": BODY}, id="p1")),
                      tool_turn(use("submit_desired_state", {"desired": BODY}, id="s1")),
                      answer("submit"))
    client = Script(*turns)

    def entry(live, authorize=None):
        if before_run is not None:
            before_run()
        return anyio.run(lambda: agent.run(live=live, connect=lambda: open_in(*schema_url), client=client,
                                           now=lambda: NOW, open_tools=hooked(before_submit, calls, after),
                                           bridge=fake.client, authorize=authorize))
    s = scheduler(schema_url, entry, sched, clock=Clock(when - timedelta(hours=1)), **kw)
    tick(s, when)
    return s


def test_a_live_scheduled_run_sends_one_request_at_most(db, fake, schema_url, conn, send_clock, live_env):  # noqa: F811
    """Sol #238, finding 6: the second submit never reaches the server, even with the first request no longer
    pending, so only the agent's own one-submit rule stands between it and a second POST."""
    prepared(db)
    db_mode(conn, "live")
    fresh_slots(conn, at(WED2, 14))
    calls = []

    def reject_the_first(name):
        # Once the first request is in, the bridge rejects it: nothing pending is left to refuse a second plan.
        if name == "submit_desired_state":
            conn.execute("update purchase_requests set status = 'rejected' where status = 'pending'")
    # The model previews, submits, then previews and submits again.
    turns = (tool_turn(use("preview_desired_state", {"desired": BODY}, id="p1")),
             tool_turn(use("submit_desired_state", {"desired": BODY}, id="s1")),
             tool_turn(use("preview_desired_state", {"desired": BODY}, id="p2")),
             tool_turn(use("submit_desired_state", {"desired": BODY}, id="s2")),
             answer("submit"))
    scheduled_live_run(schema_url, fake, LIVE9, at(WED2, 14), *turns, calls=calls, after=reject_the_first)
    assert calls.count("submit_desired_state") == 1
    assert len(fake.posts) == 1
    [(_, mode, outcome, run_id, _)] = rows(conn)
    assert (mode, outcome) == ("live", "submitted")
    assert conn.execute("select mode, status, request_id from agent_runs where id = %s",
                        (run_id,)).fetchone() == ("live", "submitted", "pr-1")
    [(level, text)] = run_lines(conn, at(WED2, 15))
    assert level == "info" and "live run" in text and "Sent: request pr-1 (submission 1)." in text


# --- Sol review of #238: the checks again at the send ---------------------------------------------------

def _set_mode(schema_url, value):
    with open_in(*schema_url) as other:
        db_mode(other, value)


@pytest.mark.parametrize("change, said", [
    pytest.param(lambda url, clock: _set_mode(url, "off"), "agent_schedule_mode", id="mode off"),
    pytest.param(lambda url, clock: _set_mode(url, "dry-run"), "agent_schedule_mode", id="mode dry-run"),
    pytest.param(lambda url, clock: setattr(clock, "now", at(WED2, 18)), "slot", id="slot check aged"),
])
def test_a_switch_or_check_that_fails_after_the_lease_stops_the_post(db, fake, schema_url, conn, send_clock,
                                                                     live_env, change, said):  # noqa: F811
    """Finding 1: the run took the lease live, then a switch went off (or the slot check aged past 3 h) before its
    submit: the submit tool checks again just before the POST, sends nothing, and says why."""
    prepared(db)
    db_mode(conn, "live")
    fresh_slots(conn, at(WED2, 14))
    scheduled_live_run(schema_url, fake, LIVE9, at(WED2, 14), before_submit=lambda: change(schema_url, send_clock))
    assert fake.posts == []
    [(status, detail, due)] = conn.execute("select status, detail, schedule_due from purchase_submissions").fetchall()
    assert status == "not_filed" and detail.startswith("nothing sent") and said in detail and due == at(WED2, 14)
    [(_, mode, outcome, run_id, detail)] = rows(conn)
    assert (mode, outcome) == ("live", "failed") and run_id is not None and said in detail


def test_a_run_by_hand_is_not_held_to_the_schedules_checks(db, fake, schema_url, conn, send_clock):  # noqa: F811
    """No scheduled time holds the lease: a person's live run goes as before. That is the deliberate override."""
    import anyio

    from vending import agent

    prepared(db)
    client = Script(tool_turn(use("preview_desired_state", {"desired": BODY}, id="p1")),
                    tool_turn(use("submit_desired_state", {"desired": BODY}, id="s1")), answer("submit"))
    _, out = anyio.run(lambda: agent.run(live=True, connect=lambda: open_in(*schema_url), client=client,
                                         now=lambda: NOW, open_tools=mcp_tools(), bridge=fake.client))
    assert out.status == "submitted" and len(fake.posts) == 1
    assert conn.execute("select schedule_due from purchase_submissions").fetchone() == (None,)


THU = datetime(2026, 12, 3, tzinfo=ET)          # the first day of the competition
THU2 = datetime(2026, 12, 10, tzinfo=ET)


def _wed_thu(first):
    return Schedule("live", (agent_schedule._entry("wed 14:00-22:00"), agent_schedule._entry("thu 12:00-20:00")),
                    first)


def _scheduled_submission(conn, due, status="accepted", request="pr-wed", request_status="rejected"):
    """A submission an earlier scheduled run sent for `due`, its request later `request_status`."""
    row = conn.execute("insert into purchase_submissions (plan_hash, body, status, request_id, schedule_due)"
                       " values ('h', %s, %s, %s, %s) returning id",
                       ('{"lines": []}', status, request, due)).fetchone()[0]
    if request is not None:
        conn.execute("insert into purchase_requests (request_id, status, submitted_at, plan_hash, request_body,"
                     " off_cycle) values (%s, %s, %s, 'h', '{}', false)", (request, request_status, due))
    return row


def test_a_rejected_wednesday_request_still_keeps_thursday_from_sending(schema_url, conn, ready):
    """Finding 2: one scheduled live submission per week's visit, whatever became of it. Wednesday's was sent and
    then rejected; Thursday's run for the same Friday visit is a dry run, and says why."""
    fresh_slots(conn, at(THU2, 12))
    conn.execute("insert into agent_schedule (due_at, latest_at, mode, outcome, finished_at)"
                 " values (%s, %s, 'live', 'submitted', now())", (at(WED2, 14), at(WED2, 22)))
    _scheduled_submission(conn, at(WED2, 14))
    agent = FakeAgent(schema_url)
    tick(scheduler(schema_url, agent, _wed_thu(WED2.date()), clock=Clock(at(THU2, 11))), at(THU2, 12))
    assert [live for _, live in agent.calls] == [False]
    thursday = [r for r in rows(conn) if r[0] == at(THU2, 12)][0]
    assert thursday[1] == "dry-run" and "already" in thursday[4] and "python -m vending.agent --live" in thursday[4]


def test_a_wednesday_submission_made_after_thursdays_claim_still_stops_its_post(db, fake, schema_url, conn,
                                                                              send_clock, live_env):  # noqa: F811
    """Finding 2, at the send: the other submission landed after the claim and the lease; the POST still waits."""
    prepared(db)
    db_mode(conn, "live")
    fresh_slots(conn, at(THU2, 12))
    conn.execute("insert into agent_schedule (due_at, latest_at, mode, outcome, finished_at)"
                 " values (%s, %s, 'live', 'submitted', now())", (at(WED2, 14), at(WED2, 22)))
    send_clock.now = at(THU2, 12, 5)
    scheduled_live_run(schema_url, fake, _wed_thu(WED2.date()), at(THU2, 12),
                       before_submit=lambda: _scheduled_submission(conn, at(WED2, 14)))
    assert fake.posts == []
    detail = conn.execute("select detail from purchase_submissions where schedule_due = %s",
                          (at(THU2, 12),)).fetchone()[0]
    assert "already" in detail


def test_the_next_week_may_send_again(schema_url, conn, ready):
    fresh_slots(conn, at(WED2 + timedelta(days=7), 14))
    _scheduled_submission(conn, at(WED2, 14))
    agent = FakeAgent(schema_url)
    tick(scheduler(schema_url, agent, LIVE9, clock=Clock(at(WED2, 13))), at(WED2 + timedelta(days=7), 14))
    assert [live for _, live in agent.calls] == [True]


# --- The first restock: a Thursday window ------------------------------------------------------------

def test_a_thursday_window_runs_live_on_3_dec_while_wednesday_2_dec_stays_dry(schema_url, conn, monkeypatch):
    """With AGENT_SCHEDULE="wed 14:00-22:00,thu 12:00-20:00", Wed 2 Dec (before the start) is a dry run, and
    Thu 3 Dec 12:00 ET may go live, before the first restock request is due (23:59 ET Thu 3 Dec)."""
    monkeypatch.setenv(agent_schedule.MODE_SETTING, "live")
    monkeypatch.setenv(agent_schedule.TIMES_SETTING, "wed 14:00-22:00,thu 12:00-20:00")
    sched = agent_schedule.schedule()
    assert sched.first_day == WED.date() and len(sched.entries) == 2
    db_mode(conn, "live")
    agent = FakeAgent(schema_url)
    s = scheduler(schema_url, agent, sched, clock=Clock(at(WED, 13)))
    tick(s, at(WED, 14))
    fresh_slots(conn, at(THU, 12))
    tick(s, at(THU, 12))
    assert [live for _, live in agent.calls] == [False, True]
    assert [r[1] for r in rows(conn)] == ["dry-run", "live"]
    assert "never live before" in rows(conn)[0][4]


# --- What a run sent, on the dashboard ------------------------------------------------------------------

def test_a_lost_ending_still_says_what_was_sent_and_names_the_run(db, fake, schema_url, conn, send_clock,
                                                                  live_env, monkeypatch):  # noqa: F811
    """Finding 4: the worker couldn't record the ending (its row still says running), yet the POST went: the
    dashboard names the run and the request, never "Nothing sent"."""
    prepared(db)
    db_mode(conn, "live")
    fresh_slots(conn, at(WED2, 14))

    def lost(*a, **k):
        raise psycopg.OperationalError("the database went away")
    monkeypatch.setattr(agent_schedule, "finish", lost)
    scheduled_live_run(schema_url, fake, LIVE9, at(WED2, 14))
    assert len(fake.posts) == 1
    [(_, mode, outcome, run_id, _)] = rows(conn)
    assert (mode, outcome) == ("live", "running") and run_id is not None
    [(_, text)] = run_lines(conn, at(WED2, 15))
    assert f"agent run {run_id}" in text and "Sent: request pr-1" in text and "Nothing sent" not in text


@pytest.mark.parametrize("status", ["unknown", "sending"])
def test_an_unsettled_submission_says_it_may_have_been_sent(schema_url, conn, status):
    conn.execute("insert into agent_schedule (due_at, latest_at, mode, outcome, finished_at)"
                 " values (%s, %s, 'live', 'failed', now())", (at(WED2, 14), at(WED2, 22)))
    row = _scheduled_submission(conn, at(WED2, 14), status=status, request=None)
    [(level, text)] = run_lines(conn, at(WED2, 15))
    assert level == "bad" and "Nothing sent" not in text
    assert f"May have sent: submission {row} ({status})" in text and "vending.submissions resolve" in text


# --- Silent failures ------------------------------------------------------------------------------------

def test_a_live_run_that_ended_skipped_shows_why_until_a_later_live_run_succeeds(schema_url, conn, ready):
    """Finding 5: meant to be live, run as a dry run (a lasting check failed: this week's visit already has a
    scheduled submission): Check, with the cause, and it stays there under newer runs until a later live run runs."""
    agent = FakeAgent(schema_url)
    sched = scheduler(schema_url, agent, LIVE9, clock=Clock(at(WED2, 13)))
    sent = _scheduled_submission(conn, at(WED2, 9))
    tick(sched, at(WED2, 14))        # a dry run, not live: already sent for this visit
    conn.execute("update purchase_submissions set schedule_due = null where id = %s", (sent,))
    # A newer run, a dry one, doesn't hide it.
    conn.execute("insert into agent_schedule (due_at, latest_at, mode, outcome, finished_at)"
                 " values (%s, %s, 'dry-run', 'skipped', now())", (at(WED2, 15), at(WED2, 22)))
    lines = [line for line in run_lines(conn, at(WED2, 16)) if "Wed 09 Dec, 2:00 PM" in line[1]]
    assert lines and lines[0][0] == "warn" and "already sent" in lines[0][1]
    # A later live run that ran clears it.
    fresh_slots(conn, at(WED2 + timedelta(days=7), 14))
    tick(sched, at(WED2 + timedelta(days=7), 14))
    assert rows(conn)[-1][1:3] == ("live", "skipped")
    assert not [line for line in run_lines(conn, at(WED2 + timedelta(days=7), 15)) if "Wed 09 Dec" in line[1]]


def test_a_live_claim_refused_at_the_lease_is_check_with_its_cause(schema_url, conn, ready):
    agent = FakeAgent(schema_url)

    def entry(live, authorize=None):
        with open_in(*schema_url) as other:
            open_submission(other)
        return agent(live, authorize)
    tick(scheduler(schema_url, entry, LIVE9, clock=Clock(at(WED2, 13))), at(WED2, 14))
    conn.execute("insert into agent_schedule (due_at, latest_at, mode, outcome, finished_at)"
                 " values (%s, %s, 'dry-run', 'skipped', now())", (at(WED2, 16), at(WED2, 22)))
    [line] = [line for line in run_lines(conn, at(WED2, 17)) if "2:00 PM" in line[1]]
    assert line[0] == "warn" and "ORDERS BLOCKED" in line[1]


def test_missing_migrations_are_a_problem_on_the_dashboard(schema_url, conn, ready):
    """Finding 5: the worker can't start a run while the database is behind; the dashboard says so, not only the
    worker's log."""
    conn.execute("delete from schema_migrations where version = '029_live_schedule.sql'")
    logged = []
    sched = scheduler(schema_url, FakeAgent(schema_url), LIVE9, clock=Clock(at(WED2, 13)), log=logged.append)
    tick(sched, at(WED2, 14))
    assert any("missing migrations" in line for line in logged)
    shown = sched.public()
    assert "029_live_schedule.sql" in shown["problem"]
    texts = attention(conn, at(WED2, 14), syncs={"agent_schedule": shown})
    assert [t for level, t in texts if level == "bad" and "029_live_schedule.sql" in t]
    # The dashboard reads the database itself too, with no worker /health.
    assert [t for level, t in attention(conn, at(WED2, 14)) if level == "bad" and "029_live_schedule.sql" in t]


# --- Garbled modes ----------------------------------------------------------------------------------------

def test_a_garbled_container_mode_runs_dry_and_is_a_problem(monkeypatch):
    """Finding 7: never live, never off: a dry run, and a Problem line."""
    monkeypatch.setenv(agent_schedule.MODE_SETTING, "Live!")
    sched = agent_schedule.schedule()
    assert sched.mode == "dry-run" and "Live!" in sched.problem and "never live" in sched.problem
    stand_in = agent_schedule.default_scheduler(log=lambda _: None)
    assert isinstance(stand_in, agent_schedule.Scheduler) and stand_in.mode == "dry-run"
    shown = worker.State(schedule=stand_in).public()["agent_schedule"]
    assert shown["mode"] == "dry-run" and "Live!" in shown["problem"]
    lines = dashboard._scheduled_runs(None, {"agent_schedule": shown}, at(WED2, 13))
    assert [t for level, t in lines if level == "bad" and "AGENT_SCHEDULE_MODE" in t]


def test_a_garbled_database_mode_runs_dry_and_is_a_problem(schema_url, conn, ready):
    conn.execute("update config set value = %s::jsonb where key = %s", ('"LIVE"', agent_schedule.DB_MODE_KEY))
    agent = FakeAgent(schema_url)
    tick(scheduler(schema_url, agent, LIVE9, clock=Clock(at(WED2, 13))), at(WED2, 14))
    assert [live for _, live in agent.calls] == [False]
    assert rows(conn)[0][1] == "dry-run"
    assert agent_schedule.database_mode(conn) == "dry-run"
    assert ("bad", agent_schedule.INVALID_IN_DB) in attention(conn, at(WED2, 15))


# --- Sol review of 38adb62 (second round) -----------------------------------------------------------------

def _other(schema_url, sql, params=()):
    with open_in(*schema_url) as other:
        other.execute(sql, params)


BUSY_LEASE = ("update agent_run_lease set token = gen_random_uuid(), run_id = null, schedule_due = null,"
              " expires_at = clock_timestamp() + interval '1 hour' where one")
EXPIRE_LEASE = "update agent_run_lease set expires_at = clock_timestamp() - interval '1 second' where one"


@pytest.mark.parametrize("lose", [pytest.param(EXPIRE_LEASE, id="expired"),
                                  pytest.param(BUSY_LEASE, id="taken over")])
def test_a_lease_lost_after_the_hand_off_never_turns_a_scheduled_submit_into_a_manual_one(
        db, fake, schema_url, conn, send_clock, live_env, lose):  # noqa: F811
    """Round 2, blocker 1: the run's lease expires (or another run takes it) after the agent handed the submit to the
    server, and the database switch goes off. The server knows the run's own lease and time (not a lookup of
    whichever lease is current), so it refuses: nothing is sent, and the attempt keeps its scheduled time."""
    prepared(db)
    db_mode(conn, "live")
    fresh_slots(conn, at(WED2, 14))

    def lose_it():
        _other(schema_url, lose)
        _set_mode(schema_url, "off")
    scheduled_live_run(schema_url, fake, LIVE9, at(WED2, 14), before_submit=lose_it)
    assert fake.posts == []
    [(status, detail, due)] = conn.execute("select status, detail, schedule_due from purchase_submissions").fetchall()
    assert status == "not_filed" and "lease" in detail and due == at(WED2, 14)


def test_the_server_gets_the_runs_own_lease_and_time_and_never_an_inherited_one(monkeypatch):
    from vending import agent
    from vending.tools import submit

    monkeypatch.setenv(submit.SCHEDULE_DUE_ENV, "2026-12-09T19:00:00+00:00")
    monkeypatch.setenv(submit.RUN_LEASE_ENV, "inherited")
    env = agent.server_env({submit.RUN_LEASE_ENV: "mine"})
    assert env[submit.RUN_LEASE_ENV] == "mine" and submit.SCHEDULE_DUE_ENV not in env
    env = agent.server_env({submit.RUN_LEASE_ENV: "mine", submit.SCHEDULE_DUE_ENV: "2026-12-10T17:00:00+00:00"})
    assert env[submit.SCHEDULE_DUE_ENV] == "2026-12-10T17:00:00+00:00"


def test_a_scheduled_time_without_a_lease_is_refused(db, fake, monkeypatch):  # noqa: F811
    """Fail closed: a server told it is a scheduled run's but given no usable lease sends nothing."""
    from tests.test_submit import submit as call_submit
    from vending.tools import submit

    prepared(db)
    monkeypatch.setenv(submit.SCHEDULE_DUE_ENV, at(WED2, 14).isoformat())
    monkeypatch.delenv(submit.RUN_LEASE_ENV, raising=False)
    reply = call_submit()
    assert reply["success"] is False and fake.posts == []
    assert [r["code"] for r in reply["refusals"]] == ["run_context"]


def test_a_lock_wait_that_carries_a_scheduled_send_past_the_cutoff_sends_nothing(db, fake, schema_url, conn,
                                                                                send_clock, live_env):  # noqa: F811
    """Round 2, blocker 2: the clock for the final checks is read after the locks are taken. Here the submit waits
    on the database switch's row while the clock passes Thursday midnight: the POST must not go."""
    import threading
    import time as clock_time

    late = Schedule("live", (agent_schedule._entry("thu 23:50-23:58"),), WED2.date())
    prepared(db)
    db_mode(conn, "live")
    fresh_slots(conn, at(THU2, 23, 50))
    send_clock.now = at(THU2, 23, 59) + timedelta(seconds=30)

    def hold_the_switch():
        holder = open_in(*schema_url)
        holder.autocommit = False
        holder.execute("select from config where key = %s for update", (agent_schedule.DB_MODE_KEY,))

        def release():
            clock_time.sleep(1.5)
            send_clock.now = at(THU2 + timedelta(days=1), 0) + timedelta(seconds=30)
            holder.commit()
            holder.close()
        threading.Thread(target=release, daemon=True).start()
    scheduled_live_run(schema_url, fake, late, at(THU2, 23, 50), before_submit=hold_the_switch)
    assert fake.posts == []
    [(status, detail)] = conn.execute("select status, detail from purchase_submissions").fetchall()
    assert status == "not_filed" and "past_cutoff" in detail


OFF_CYCLE = {**BODY, "off_cycle": True}


def test_a_scheduled_run_never_buys_an_off_cycle_visit(db, fake, schema_url, conn, send_clock, live_env):  # noqa: F811
    """Round 2, blocker 3: the weekly run orders for Friday; it never pays the $25 for a midweek visit instead."""
    prepared(db)
    db_mode(conn, "live")
    fresh_slots(conn, at(WED2, 14))
    calls = []
    scheduled_live_run(schema_url, fake, LIVE9, at(WED2, 14),
                       tool_turn(use("preview_desired_state", {"desired": OFF_CYCLE}, id="p1")),
                       tool_turn(use("submit_desired_state", {"desired": OFF_CYCLE}, id="s1")),
                       answer("submit"), calls=calls)
    assert calls.count("submit_desired_state") == 1
    assert fake.posts == []
    assert conn.execute("select count(*) from purchase_submissions").fetchone()[0] == 0
    refused = [c for (c,) in conn.execute("select content from agent_steps where kind = 'tool_result'"
                                          " and tool = 'submit_desired_state'")]
    assert refused and "scheduled_off_cycle" in refused[0]


def test_a_person_may_still_send_an_off_cycle_visit_by_hand(db, fake, schema_url, conn, send_clock):  # noqa: F811
    import anyio

    from vending import agent

    prepared(db)
    client = Script(tool_turn(use("preview_desired_state", {"desired": OFF_CYCLE}, id="p1")),
                    tool_turn(use("submit_desired_state", {"desired": OFF_CYCLE}, id="s1")), answer("submit"))
    _, out = anyio.run(lambda: agent.run(live=True, connect=lambda: open_in(*schema_url), client=client,
                                         now=lambda: NOW, open_tools=mcp_tools(), bridge=fake.client))
    assert out.status == "submitted" and len(fake.posts) == 1


def test_a_stale_slot_check_waits_and_the_run_goes_live_once_it_recovers(schema_url, conn, ready):
    """Round 2, finding 4: a passing blocker doesn't spend the window. The worker tries again until the latest
    start, and the run goes live once the slot check is fresh."""
    fresh_slots(conn, at(WED2, 9))          # confirmed 5 h 30 min before 14:00: stale
    agent = FakeAgent(schema_url)
    s = scheduler(schema_url, agent, LIVE9, clock=Clock(at(WED2, 13)))
    tick(s, at(WED2, 14))
    assert agent.calls == [] and rows(conn) == []
    assert "slot" in s.public()["waiting"]
    assert s.next_due(at(WED2, 14)) <= at(WED2, 14) + agent_schedule.LIVE_RETRY
    fresh_slots(conn, at(WED2, 15))
    tick(s, at(WED2, 15))
    assert [live for _, live in agent.calls] == [True]
    assert rows(conn)[0][1] == "live"


def test_a_blocker_still_there_at_the_latest_start_is_a_missed_live_run_and_a_problem(schema_url, conn, ready):
    fresh_slots(conn, at(WED2, 9))
    agent = FakeAgent(schema_url)
    s = scheduler(schema_url, agent, LIVE9, clock=Clock(at(WED2, 13)))
    tick(s, at(WED2, 14))
    tick(s, at(WED2, 22))
    assert agent.calls == []
    [(_, mode, outcome, _, detail)] = rows(conn)
    assert (mode, outcome) == ("live", "missed") and "slot" in detail
    [(level, text)] = run_lines(conn, at(WED2, 22, 5))
    assert level == "bad" and "slot" in text


def test_a_busy_lease_that_expires_inside_the_window_still_lets_the_run_go_live(schema_url, conn, ready):
    _other(schema_url, BUSY_LEASE)
    agent = FakeAgent(schema_url)
    s = scheduler(schema_url, agent, LIVE9, clock=Clock(at(WED2, 13)))
    tick(s, at(WED2, 14))
    assert agent.calls == [] and rows(conn) == []
    _other(schema_url, EXPIRE_LEASE)
    tick(s, at(WED2, 14, 30))
    assert [live for _, live in agent.calls] == [True]


def test_a_lease_taken_between_the_claim_and_the_start_puts_the_time_back(schema_url, conn, ready):
    """The race the check before the claim can't see: the real entry point finds the lease busy. The time goes back
    to waiting, not to an informational skip."""
    real, fake_agent, first = _real_entry_point(schema_url), FakeAgent(schema_url), []

    def entry(live, authorize=None):
        if not first:
            first.append(live)
            _other(schema_url, BUSY_LEASE)
            return real(live, authorize)
        return fake_agent(live, authorize)
    s = scheduler(schema_url, entry, LIVE9, clock=Clock(at(WED2, 13)))
    tick(s, at(WED2, 14))
    assert first == [True] and rows(conn) == []
    assert "another agent run" in s.public()["waiting"]
    _other(schema_url, EXPIRE_LEASE)
    tick(s, at(WED2, 14, 30))
    assert [live for _, live in fake_agent.calls] == [True]


def test_a_live_run_skipped_for_a_busy_lease_is_a_check_and_clears_nothing(schema_url, conn):
    """A live row that never started (skipped, the lease busy) is meant-live-not-run: Check, and it doesn't count as
    the later live run that clears an older one."""
    from vending import agent_runs

    conn.execute("insert into agent_schedule (due_at, latest_at, mode, outcome, detail, finished_at, started_at)"
                 " values (%s, %s, 'dry-run', 'skipped', %s, now(), now())",
                 (at(WED2, 14), at(WED2, 22), agent_schedule.DOWNGRADED.format(why="ORDERS BLOCKED: x.")))
    run_id = agent_runs.start(conn, mode="live", model="fake")
    agent_runs.finish(conn, run_id, "skipped", detail="Not started: another agent run was still going.",
                      cost_usd=Decimal(0))
    conn.execute("insert into agent_schedule (due_at, latest_at, mode, outcome, detail, finished_at, run_id)"
                 " values (%s, %s, 'live', 'skipped', 'Not started: another agent run was still going.', now(), %s)",
                 (at(WED2 + timedelta(days=7), 14), at(WED2 + timedelta(days=7), 22), run_id))
    lines = run_lines(conn, at(WED2 + timedelta(days=7), 15))
    assert [level for level, text in lines if "16 Dec" in text] == ["warn"]
    assert [level for level, text in lines if "09 Dec" in text] == ["warn"]


def test_a_validated_201_left_open_is_settled_by_the_scheduler_and_the_run_goes_live(db, fake, schema_url, conn,
                                                                                     ready):  # noqa: F811
    """Round 2, finding 4: an unattended run settles what `submissions check` can settle before calling it
    something only a person can."""
    conn.execute("insert into purchase_submissions (plan_hash, body, status, created_id)"
                 " values ('h', %s, 'sending', 'pr-7')", ('{"lines": []}',))
    fake.lookups["pr-7"] = {"id": "pr-7", "status": "pending", "created_at": "2026-12-09T12:00:00Z",
                            "decision_note": None}
    agent = FakeAgent(schema_url)
    tick(scheduler(schema_url, agent, LIVE9, clock=Clock(at(WED2, 13)), bridge=fake.client), at(WED2, 14))
    assert conn.execute("select status from purchase_submissions").fetchone() == ("accepted",)
    assert [live for _, live in agent.calls] == [True]


def test_the_default_schedule_has_the_thursday_window(monkeypatch):
    """Round 2, finding 5 (Kevin's decision): unset, AGENT_SCHEDULE is Wednesday 14:00-22:00 and Thursday
    12:00-20:00, so the first live order is Thu 3 Dec 12:00 ET's run."""
    monkeypatch.setenv(agent_schedule.MODE_SETTING, "live")
    sched = agent_schedule.schedule()
    assert sched.entries == (agent_schedule._entry("wed 14:00-22:00"), agent_schedule._entry("thu 12:00-20:00"))
    assert agent_schedule.next_time(sched, at(WED, 15)) == at(THU, 12)


def test_the_activation_steps_set_the_schedule_and_check_it_on_the_revision():
    from pathlib import Path

    for name in ("docs/agent.md", "docs/deploy.md"):
        text = Path(name).read_text(encoding="utf-8")
        assert "AGENT_SCHEDULE=" in text and "AGENT_SCHEDULE_FROM=" in text, name
        assert "029" in text and "030" in text, name
        assert "python -m vending.agent_schedule" in text, name


def test_a_json_null_setting_is_a_dry_run_at_the_claim_and_at_the_lease(schema_url, conn, ready):
    """Round 2, finding 6: a row holding JSON null isn't a missing row. It is a garbled value: a dry run at the claim
    and at the lease alike, never a skip."""
    conn.execute("update config set value = 'null'::jsonb where key = %s", (agent_schedule.DB_MODE_KEY,))
    assert agent_schedule.database_mode(conn) == "dry-run"
    with conn.transaction():
        assert agent_schedule.database_mode(conn, lock=True) == "dry-run"
    agent = FakeAgent(schema_url)
    tick(scheduler(schema_url, agent, LIVE9, clock=Clock(at(WED2, 13))), at(WED2, 14))
    assert [live for _, live in agent.calls] == [False]
    [(_, mode, outcome, _, detail)] = rows(conn)
    assert mode == "dry-run" and "changed to" not in (detail or "")
    conn.execute("delete from config where key = %s", (agent_schedule.DB_MODE_KEY,))
    assert agent_schedule.database_mode(conn) == "off"
