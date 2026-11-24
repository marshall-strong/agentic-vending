"""A midweek wake-up starts one agent run (#219), and a missed scheduled run shows on the failure banner (#220).

The run goes through the scheduler's own claim, lease and checks (`vending.agent_schedule`), so it is held to the
same switches, start, halt switch, run lease and caps as a weekly run. The agent's entry point is faked where the
test is about the scheduler; the submit tests go through the real agent, the MCP server and the loopback fake
bridge. No model calls."""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from tests.test_agent_schedule import (  # noqa: F401 - the autouse fixture clears the schedule settings
    Clock, _no_schedule_settings, at)
from vending import agent_schedule, dashboard
from vending.agent_schedule import Entry, Schedule

ET = ZoneInfo("America/New_York")
WED = datetime(2026, 12, 2, tzinfo=ET)          # before the start (Thu 3 Dec 00:00 ET)
MON = datetime(2026, 12, 7, tzinfo=ET)          # the first Monday after the start
WED2 = datetime(2026, 12, 9, tzinfo=ET)
# Weekly times from Wed 9 Dec on, so a Monday 7 Dec has no weekly time due: only the midweek one.
LIVE9 = Schedule("live", (Entry(2, time(14), time(22)),), WED2.date())
DRY9 = Schedule("dry-run", LIVE9.entries, WED2.date())
REASON = "Slots 11 and 12 are projected to sell out before Friday's restock (test)."


# --- No database -------------------------------------------------------------------------------------

def test_a_midweek_run_may_start_until_the_end_of_the_day_it_was_requested():
    assert agent_schedule.midweek_latest(at(MON, 9, 5)) == datetime(2026, 12, 8, tzinfo=ET)
    assert agent_schedule.midweek_latest(at(MON, 23, 30)) == datetime(2026, 12, 8, tzinfo=ET)


def test_the_opening_says_why_a_midweek_run_runs_and_what_it_may_send():
    from vending import agent

    text = agent.brief(at(MON, 9), "none", live=True, occasion=agent_schedule.midweek_occasion(REASON))
    assert REASON in text and "off_cycle true" in text and "valid outcome" in text
    assert "midweek wake check" not in agent.brief(at(MON, 9), "none", live=True)


def _banner_snap(rows, *, opened, last_finished=None):
    return {"opened_at": opened, "status": {"last_finished": last_finished},
            "scheduled_runs": {"ok": True, "data": {"latest": rows[0], "incidents": rows}}}


def _row(due, outcome, kind="weekly", run_id=None, latest=None):
    return {"due_at": due, "latest_at": latest or due + timedelta(hours=8), "kind": kind, "mode": "live",
            "outcome": outcome, "run_id": run_id}


def test_a_missed_run_in_the_live_period_is_a_banner_line_in_plain_words():
    missed = _row(at(WED2, 14), "missed")
    missed["cause"] = "LIVE RUN MISSED: secret detail"
    [alert] = dashboard.build_alerts(_banner_snap([missed], opened=at(MON, 0)))
    assert alert["kind"] == "missed_run" and alert["at"] == at(WED2, 14)
    assert alert["text"] == ("The scheduled run due Wed 09 Dec, 2:00 PM ET never started by its latest start, "
                             "Wed 09 Dec, 10:00 PM ET. `python -m vending.agent_schedule` says why.")
    midweek = _row(at(MON, 9, 5), "missed", kind="midweek", latest=datetime(2026, 12, 8, tzinfo=ET))
    [alert] = dashboard.build_alerts(_banner_snap([midweek], opened=at(MON, 0)))
    assert alert["text"].startswith("The midweek run requested Mon 07 Dec, 9:05 AM ET never started")
    assert "Scheduled run missed" in dashboard._alerts_html([alert])


def test_a_later_run_that_finished_clears_the_missed_line():
    missed = _row(at(WED2, 14), "missed")
    later = _row(at(WED2 + timedelta(days=1), 12), "skipped", run_id=9)
    assert dashboard.build_alerts(_banner_snap([later, missed], opened=at(MON, 0))) == []
    # A later time that is still running doesn't clear it; nor does one that was missed too.
    going = _row(at(WED2 + timedelta(days=1), 12), "running", run_id=9)
    assert [a["kind"] for a in dashboard.build_alerts(_banner_snap([going, missed], opened=at(MON, 0)))] == [
        "missed_run"]
    # A live run by hand that started after the missed time and finished clears it too.
    done = {"id": 4, "started_at": at(WED2, 23), "finished_at": at(WED2, 23, 10), "status": "skipped"}
    assert dashboard.build_alerts(_banner_snap([missed], opened=at(MON, 0), last_finished=done)) == []


def test_no_missed_line_before_the_opening():
    missed = _row(at(WED, 14), "missed")
    assert dashboard.build_alerts(_banner_snap([missed], opened=None)) == []
    assert dashboard.build_alerts(_banner_snap([missed], opened=at(MON, 0))) == []


# --- With the database --------------------------------------------------------------------------------

psycopg = pytest.importorskip("psycopg")

from tests.conftest import open_in  # noqa: E402
from tests.test_agent_schedule import db_mode, rows, scheduler, tick  # noqa: E402
from tests.test_dashboard import MONDAY, open_ledger, page_conn  # noqa: E402,F401
from vending import agent_runs, midweek_wake, run_lease  # noqa: E402
from vending.config import set_halted  # noqa: E402


class MidweekAgent:
    """Stands in for `vending.agent.run`, as the scheduler calls it: records a run as the real one does, with the
    occasion a midweek run is given."""

    def __init__(self, url, status="skipped", detail="No midweek visit is worth the fee this week."):
        self.url, self.status, self.detail = url, status, detail
        self.calls = []

    def __call__(self, live, authorize=None, occasion=None):
        if authorize is not None:       # as the real one does, in the transaction that takes the run lease
            with open_in(*self.url) as c, c.transaction():
                refused = authorize(c)
            if refused is not None:
                return None, SimpleNamespace(status="skipped", detail=refused)
        self.calls.append((live, occasion))
        with open_in(*self.url) as c:
            run_id = agent_runs.start(c, mode="live" if live else "dry-run", model="fake")
            agent_runs.finish(c, run_id, self.status, detail=self.detail, cost_usd=Decimal(0))
        return run_id, SimpleNamespace(status=self.status, detail=self.detail)


def wake(conn, when, reason=REASON):
    """The midweek check's own record and request (`midweek_wake.record`, `request_midweek_run`) at `when`."""
    found = midweek_wake.Wake(midweek_wake.ledger.week_of(when.astimezone(ET)), reason, ("11", "12"), 3000)
    with conn.transaction():
        row = midweek_wake.record(conn, found)
        asked = midweek_wake.request_midweek_run(conn, reason, by="test", now=when, log=lambda *_: None)
    return row, asked


def schedule_rows(conn):
    return conn.execute("select due_at, kind, wakeup_id, mode, outcome, run_id, detail from agent_schedule"
                        " order by due_at").fetchall()


def fresh_slots(conn, when):
    conn.execute("update slot_assignment set last_checked_at = %s where id = 1", (when - timedelta(minutes=30),))


def test_a_wake_up_starts_one_dry_run_with_its_reason(schema_url, conn):
    db_mode(conn, "dry-run")
    wakeup, asked = wake(conn, at(MON, 9, 5))
    assert asked
    agent = MidweekAgent(schema_url)
    s = scheduler(schema_url, agent, DRY9, clock=Clock(at(MON, 9)))
    tick(s, at(MON, 9, 1))
    s.poke()
    assert s.next_due(at(MON, 9, 10)) == at(MON, 9, 10)     # a poke looks now, not at Wednesday's time
    tick(s, at(MON, 9, 10))
    [(live, occasion)] = agent.calls
    assert live is False and REASON in occasion
    [(due, kind, wakeup_id, mode, outcome, run_id, _)] = schedule_rows(conn)
    assert (kind, wakeup_id, mode, outcome) == ("midweek", wakeup, "dry-run", "skipped")
    assert due == conn.execute("select requested_at from midweek_wakeups").fetchone()[0]
    assert conn.execute("select mode from agent_runs where id = %s", (run_id,)).fetchone()[0] == "dry-run"
    # It runs once: later ticks start nothing, and Wednesday's time is next.
    s.poke()
    tick(s, at(MON, 10))
    assert len(agent.calls) == 1 and s.next_due(at(MON, 11)) == at(WED2, 14)


def test_a_second_wake_up_in_the_week_records_and_starts_nothing(schema_url, conn):
    db_mode(conn, "dry-run")
    wake(conn, at(MON, 9, 5))
    assert wake(conn, at(MON + timedelta(days=1), 9, 5)) == (None, False)
    agent = MidweekAgent(schema_url)
    s = scheduler(schema_url, agent, DRY9, clock=Clock(at(MON, 9)))
    tick(s, at(MON, 9, 10))
    tick(s, at(MON + timedelta(days=1), 9, 10))
    assert len(agent.calls) == 1 and len(schedule_rows(conn)) == 1


def test_both_switches_live_after_the_start_make_it_live(schema_url, conn):
    db_mode(conn, "live")
    fresh_slots(conn, at(MON, 9, 10))
    wake(conn, at(MON, 9, 5))
    agent = MidweekAgent(schema_url)
    tick(scheduler(schema_url, agent, LIVE9, clock=Clock(at(MON, 9))), at(MON, 9, 10))
    assert [live for live, _ in agent.calls] == [True]
    assert [(r[1], r[3]) for r in schedule_rows(conn)] == [("midweek", "live")]


@pytest.mark.parametrize("container, in_db, called", [
    (LIVE9, "dry-run", [False]), (DRY9, "live", [False]), (LIVE9, "off", [])])
def test_either_switch_not_live_is_a_dry_run_or_off(schema_url, conn, container, in_db, called):
    db_mode(conn, in_db)
    fresh_slots(conn, at(MON, 9, 10))
    wake(conn, at(MON, 9, 5))
    agent = MidweekAgent(schema_url)
    tick(scheduler(schema_url, agent, container, clock=Clock(at(MON, 9))), at(MON, 9, 10))
    assert [live for live, _ in agent.calls] == called
    [(_, kind, _, mode, outcome, _, _)] = schedule_rows(conn)
    assert kind == "midweek" and mode == ("off" if in_db == "off" else "dry-run")
    if in_db == "off":
        assert outcome == "skipped"


def test_before_the_start_it_is_a_dry_run_even_with_both_switches_live(schema_url, conn):
    """The check itself never wakes before the start; a request found then still never runs live."""
    db_mode(conn, "live")
    fresh_slots(conn, at(WED, 9, 10))
    conn.execute("insert into midweek_wakeups (week_start, reason, slots, saved_margin_cents, requested_at)"
                 " values ('2026-11-28', %s, '{11}', 3000, %s)", (REASON, at(WED, 9, 5)))
    agent = MidweekAgent(schema_url)
    tick(scheduler(schema_url, agent, LIVE9, clock=Clock(at(WED, 9))), at(WED, 9, 10))
    assert [live for live, _ in agent.calls] == [False]
    [(_, _, _, mode, _, _, detail)] = schedule_rows(conn)
    assert mode == "dry-run" and detail.startswith("Run as a dry run, not live:") and "never live" in detail


def test_halted_it_records_halted_and_calls_no_model(schema_url, conn):
    import anyio

    from vending import agent

    db_mode(conn, "dry-run")
    set_halted(conn, True, "test")
    wake(conn, at(MON, 9, 5))

    def never(*a, **k):
        raise AssertionError("a halted run must not open the tools or call the model")

    def entry(live, authorize=None, occasion=None):
        return anyio.run(lambda: agent.run(live=live, connect=lambda: open_in(*schema_url), client=never,
                                           open_tools=never, authorize=authorize, occasion=occasion))
    tick(scheduler(schema_url, entry, DRY9, clock=Clock(at(MON, 9))), at(MON, 9, 10))
    [(_, kind, _, _, outcome, run_id, _)] = schedule_rows(conn)
    assert (kind, outcome) == ("midweek", "halted")
    assert conn.execute("select status from agent_runs where id = %s", (run_id,)).fetchone()[0] == "halted"
    assert conn.execute("select count(*) from model_calls").fetchone()[0] == 0


def test_a_run_lease_held_by_another_run_makes_it_wait_then_it_runs(schema_url, conn):
    """No overlap: while another run (a weekly one, say) holds the lease, the live midweek run claims nothing and
    waits; once the lease is given back it runs, inside its window."""
    db_mode(conn, "live")
    fresh_slots(conn, at(MON, 9, 10))
    wake(conn, at(MON, 9, 5))
    with open_in(*schema_url) as other:
        held = run_lease.take(other, deadline=3600)
    agent = MidweekAgent(schema_url)
    s = scheduler(schema_url, agent, LIVE9, clock=Clock(at(MON, 9)))
    tick(s, at(MON, 9, 10))
    assert agent.calls == [] and schedule_rows(conn) == []
    assert "run lease" in s.public()["waiting"]
    with open_in(*schema_url) as other:
        run_lease.give_back(other, held.token)
    tick(s, at(MON, 9, 20))
    assert [live for live, _ in agent.calls] == [True]


def test_a_weekly_time_due_at_once_goes_first_and_the_midweek_run_after_it(schema_url, conn):
    db_mode(conn, "dry-run")
    monday = Schedule("dry-run", (Entry(0, time(9), time(17)),), MON.date())
    wake(conn, at(MON, 9, 5))
    agent = MidweekAgent(schema_url)
    s = scheduler(schema_url, agent, monday, clock=Clock(at(MON, 8)))
    tick(s, at(MON, 9, 10))
    assert [occasion for _, occasion in agent.calls] == [None]
    tick(s, at(MON, 9, 11))
    assert [occasion is not None for _, occasion in agent.calls] == [False, True]
    assert [r[1] for r in schedule_rows(conn)] == ["weekly", "midweek"]


def test_a_midweek_run_never_started_by_its_latest_start_is_missed(schema_url, conn):
    db_mode(conn, "dry-run")
    wake(conn, at(MON, 9, 5))
    agent = MidweekAgent(schema_url)
    tick(scheduler(schema_url, agent, DRY9, clock=Clock(at(MON, 9))), at(MON + timedelta(days=1), 9))
    assert agent.calls == []
    [(_, kind, _, _, outcome, _, _)] = schedule_rows(conn)
    assert (kind, outcome) == ("midweek", "missed")


def _submission_for(conn, due, kind, wakeup=None):
    conn.execute("insert into agent_schedule (due_at, latest_at, mode, outcome, finished_at, kind, wakeup_id)"
                 " values (%s, %s, 'live', 'submitted', now(), %s, %s)", (due, due + timedelta(hours=8), kind, wakeup))
    return conn.execute("insert into purchase_submissions (plan_hash, body, status, schedule_due)"
                        " values ('h', '{\"lines\": []}', 'accepted', %s) returning id", (due,)).fetchone()[0]


def test_each_kind_sends_at_most_once_a_week_and_neither_blocks_the_other(conn):
    weekly = _submission_for(conn, at(WED2 - timedelta(days=7), 14), "weekly")
    wakeup = conn.execute("insert into midweek_wakeups (week_start, reason, slots, saved_margin_cents)"
                          " values ('2026-12-05', 'r', '{11}', 1) returning id").fetchone()[0]
    assert agent_schedule._already_sent(conn, at(MON, 9), due=at(MON, 9), kind="midweek") is None
    midweek = _submission_for(conn, at(MON, 9, 5), "midweek", wakeup)
    # The Wednesday after a midweek visit may still send the Friday order.
    assert agent_schedule._already_sent(conn, at(WED2, 14), due=at(WED2, 14)) is None
    again = agent_schedule._already_sent(conn, at(MON, 12), due=at(MON + timedelta(days=1), 9), kind="midweek")
    assert again is not None and f"submission {midweek}" in again and "one midweek visit a week" in again
    # The weekly cap still counts only the weekly run's submission (the week before here).
    weekly_sent = agent_schedule._already_sent(conn, at(WED2, 9), due=at(WED2 - timedelta(days=6), 12))
    assert f"submission {weekly}" in weekly_sent


def test_the_table_holds_one_run_per_wake_up(conn):
    wakeup = conn.execute("insert into midweek_wakeups (week_start, reason, slots, saved_margin_cents)"
                          " values ('2026-12-05', 'r', '{11}', 1) returning id").fetchone()[0]
    assert agent_schedule.record(conn, at(MON, 9, 5), at(MON, 23), "dry-run", "missed", kind="midweek",
                                 wakeup=wakeup) is not None
    assert agent_schedule.record(conn, at(MON, 9, 6), at(MON, 23), "dry-run", "missed", kind="midweek",
                                 wakeup=wakeup) is None
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("insert into agent_schedule (due_at, latest_at, mode, kind) values (%s, %s, 'dry-run',"
                     " 'midweek')", (at(MON, 10), at(MON, 23)))


def test_needs_attention_and_runs_say_it_was_a_midweek_run(schema_url, conn):
    db_mode(conn, "dry-run")
    wake(conn, at(MON, 9, 5))
    tick(scheduler(schema_url, MidweekAgent(schema_url), DRY9, clock=Clock(at(MON, 9))), at(MON, 9, 10))
    snap = dashboard.snapshot(conn, now=at(MON, 10))
    [text] = [a["text"] for a in snap["attention"] if a["text"].startswith("The midweek ")]
    assert text.startswith("The midweek dry run requested Mon 07 Dec, 9:05 AM ET skipped (agent run ")
    runs = dashboard._runs(conn, at(MON, 10), modes=("dry-run",))
    assert runs[0]["scheduled"] == "midweek"
    assert "midweek run" in dashboard._runs_html({"ok": True, "data": runs}, at(MON, 10))


# --- The failure banner, from the database ---------------------------------------------------------------

def _missed(conn, due):
    conn.execute("insert into agent_schedule (due_at, latest_at, mode, outcome, detail, finished_at)"
                 " values (%s, %s, 'live', 'missed', 'LIVE RUN MISSED: secret detail', now())",
                 (due, due + timedelta(hours=8)))


def test_a_missed_scheduled_run_banners_until_a_later_run_finishes(conn, page_conn, open_ledger):
    _missed(conn, MONDAY - timedelta(hours=12))
    snap = dashboard.snapshot(page_conn, MONDAY)
    [alert] = snap["alerts"]
    assert alert["kind"] == "missed_run" and "never started" in alert["text"]
    assert "secret detail" not in alert["text"]
    assert "Scheduled run missed" in dashboard.render(snap)
    run = agent_runs.start(conn, mode="dry-run", model="fake")
    agent_runs.finish(conn, run, "skipped", detail="ok", cost_usd=Decimal(0))
    conn.execute("insert into agent_schedule (due_at, latest_at, mode, outcome, finished_at, run_id)"
                 " values (%s, %s, 'dry-run', 'skipped', now(), %s)",
                 (MONDAY - timedelta(hours=1), MONDAY + timedelta(hours=7), run))
    assert dashboard.snapshot(page_conn, MONDAY)["alerts"] == []


def test_no_missed_banner_before_the_opening(conn, page_conn):
    _missed(conn, MONDAY - timedelta(hours=12))
    assert dashboard.snapshot(page_conn, MONDAY)["alerts"] == []


# --- What a midweek run may send: the real agent, the MCP server and the fake bridge ---------------------

pytest.importorskip("anthropic")

from tests.test_agent import Script, answer, tool_turn, use  # noqa: E402
from tests.test_desired_state import NOW, coke_sourcing, desired, fake, restock_15  # noqa: E402,F401
from tests.test_live_schedule import OFF_CYCLE, hooked, live_env, send_clock  # noqa: E402,F401
from tests.test_server import db  # noqa: E402,F401
from tests.test_submit import prepared  # noqa: E402
from tests.test_submit_runs import BODY  # noqa: E402


def midweek_live_run(schema_url, fake, conn, plan, calls):  # noqa: F811
    """A live midweek run on Wed 9 Dec at 1 PM (the send clock reads 2:05 PM), before the weekly 2 PM time."""
    import anyio

    from vending import agent

    db_mode(conn, "live")
    fresh_slots(conn, at(WED2, 13))
    conn.execute("insert into midweek_wakeups (week_start, reason, slots, saved_margin_cents, requested_at)"
                 " values ('2026-12-05', %s, '{15}', 3000, %s)", (REASON, at(WED2, 13)))
    client = Script(tool_turn(use("preview_desired_state", {"desired": plan}, id="p1")),
                    tool_turn(use("submit_desired_state", {"desired": plan}, id="s1")), answer("submit"))

    def entry(live, authorize=None, occasion=None):
        return anyio.run(lambda: agent.run(live=live, connect=lambda: open_in(*schema_url), client=client,
                                           now=lambda: NOW, open_tools=hooked(calls=calls), bridge=fake.client,
                                           authorize=authorize, occasion=occasion))
    tick(scheduler(schema_url, entry, LIVE9, clock=Clock(at(WED2, 12))), at(WED2, 13, 1))


def test_a_live_midweek_run_sends_an_off_cycle_visit(db, fake, schema_url, conn, send_clock, live_env):  # noqa: F811
    prepared(db)
    calls = []
    midweek_live_run(schema_url, fake, conn, OFF_CYCLE, calls)
    [posted] = fake.posts
    assert posted["off_cycle"] is True
    [(due, kind, _, mode, outcome, _, _)] = schedule_rows(conn)
    assert (kind, mode, outcome) == ("midweek", "live", "submitted")
    assert conn.execute("select schedule_due from purchase_submissions").fetchone()[0] == due


def test_a_midweek_run_never_sends_the_friday_order(db, fake, schema_url, conn, send_clock, live_env):  # noqa: F811
    prepared(db)
    calls = []
    midweek_live_run(schema_url, fake, conn, BODY, calls)
    assert calls.count("submit_desired_state") == 1 and fake.posts == []
    assert conn.execute("select count(*) from purchase_submissions").fetchone()[0] == 0
    refused = [c for (c,) in conn.execute("select content from agent_steps where kind = 'tool_result'"
                                          " and tool = 'submit_desired_state'")]
    assert refused and "midweek_on_cycle" in refused[0]
