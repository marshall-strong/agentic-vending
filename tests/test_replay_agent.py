"""The agent replay: several weeks, real tools on fixture polls, a local stand-in for the Anthropic API."""

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tests.test_replay import WEEK, fill_week
from tests.test_replay_model import api, baseline_json  # noqa: F401  (api is a fixture)
from vending.replay import agent
from vending.replay.__main__ import main
from vending.replay.agent import AgentReplay, run_tool
from vending.replay.state import replay_week

AS_OF = datetime(2026, 10, 9, 4, tzinfo=timezone.utc)   # the end of the week of 2 Oct, midnight Eastern


def answer(conn, day, notes="watch slot 15", update="Restocked to last week's sales."):
    return json.dumps({"plan": baseline_json(replay_week(conn, day)), "update": update, "notes": notes})


def tool_call(name, arguments, call_id="tu_1"):
    return [{"type": "tool_use", "id": call_id, "name": name, "input": arguments}]


def test_tools_see_nothing_after_the_replay_date(conn):
    fill_week(conn)   # polls just before 2, 9 and 16 Oct
    state, failed = run_tool(conn, AS_OF, "get_machine_state", {})
    slot15 = next(s for s in json.loads(state)["data"] if s["slot"] == "15")
    assert not failed and slot15["lifetime_units"] == 107   # the 9 Oct poll, not the 16 Oct one
    history, failed = run_tool(conn, AS_OF, "get_sales_history",
                               {"start": "2026-10-01T00:00:00", "end": "2026-10-20T00:00:00", "slots": ["15"]})
    assert not failed and json.loads(history)["data"][0]["units"] == 7   # cut at now: not the 9 sold after
    future, failed = run_tool(conn, AS_OF, "get_sales_history",
                              {"start": "2026-10-10T00:00:00", "end": "2026-10-16T00:00:00", "slots": None})
    assert failed and "after now" in json.loads(future)["message"]
    bad, failed = run_tool(conn, AS_OF, "get_sales_history", {"start": "soon", "end": "later", "slots": None})
    assert failed and "Bad arguments" in json.loads(bad)["message"]
    assert run_tool(conn, AS_OF, "set_price", {})[1]


def test_two_weeks_with_tools_notes_and_updates(conn, api, tmp_path):
    fill_week(conn)
    api.turns = [
        (200, "tool_use", tool_call("get_machine_state", {})),
        (200, "end_turn", answer(conn, WEEK)),
        (200, "end_turn", answer(conn, datetime(2026, 10, 9).date(), notes="15 slowed", update="Week two.")),
    ]
    replay = AgentReplay()
    weeks = replay.run(conn, WEEK, 2, unit_cost_cents=80, balance_cents=60000)
    assert len(weeks) == 2 and not any(w.error for w in weeks)
    first, second = weeks
    assert first.tool_calls == ["get_machine_state"] and first.turns == 2
    assert first.result.passed and first.result.totals.scored == 2   # 15 and 16 have figures both weeks
    assert second.result.slots == ()   # no polls after 16 Oct: the plan is checked, not scored
    # The tool's answer went back to the model, and the first week's notes opened the second week.
    tool_result = api.posts[1]["messages"][2]["content"][0]
    assert tool_result["type"] == "tool_result" and '"lifetime_units":107' in tool_result["content"]
    assert "watch slot 15" in api.posts[2]["messages"][0]["content"]
    assert "every unit costs $0.80" in api.posts[0]["messages"][0]["content"]
    assert api.posts[0]["tool_choice"] == {"type": "auto"} and len(api.posts[0]["tools"]) == 2
    assert first.cost_usd == Decimal("0.104") and second.cost_usd == Decimal("0.052")   # 2 requests, then 1
    path = agent.save_updates(weeks, replay.name, tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "Restocked to last week's sales." in text and "15 slowed" in text and "Cost: $0.1040" in text


def test_a_week_that_runs_out_of_budget_stops_the_replay(conn, api):
    fill_week(conn)
    api.turns = [(200, "tool_use", tool_call("get_machine_state", {})),
                 (200, "end_turn", answer(conn, WEEK))]
    # $0.10 a week: the first request fits ($0.052 spent), the second can't.
    weeks = AgentReplay(week_cap=Decimal("0.10")).run(conn, WEEK, 2)
    assert len(weeks) == 1 and "Not sent" in weeks[0].error
    assert weeks[0].result.checks[0].status == "fail" and weeks[0].cost_usd == Decimal("0.052")
    assert len(api.posts) == 1


def test_the_last_turn_must_answer(conn, api, monkeypatch):
    fill_week(conn)
    monkeypatch.setattr(agent, "MAX_TURNS", 1)
    api.turns = [(200, "tool_use", tool_call("get_machine_state", {})),
                 (200, "end_turn", answer(conn, WEEK))]
    weeks = AgentReplay().run(conn, WEEK, 1)
    assert api.posts[1]["tool_choice"] == {"type": "none"} and weeks[0].result.passed


def test_a_week_with_no_polls_stops_the_replay(conn, api):
    weeks = AgentReplay().run(conn, WEEK, 3)
    assert len(weeks) == 1 and weeks[0].state is None and "Fewer than two" in weeks[0].error
    assert api.posts == []


def test_cli_prints_the_weeks_beside_the_baseline(conn, api, capsys):
    fill_week(conn)
    api.turns = [(200, "end_turn", answer(conn, WEEK))]
    assert main(["--week", "2026-10-02", "--agent", "1", "--unit-cost", "0.80"], conn=conn) == 0
    out = capsys.readouterr().out
    assert "Agent replay, agent:claude-opus-5-5@medium, 1 week" in out and "Baseline margin" in out
    assert "Weekly updates and notes, for people to grade:" in out and "Model cost: $0.0520 for 1 request" in out


def test_cli_agent_needs_a_week_and_sane_length(capsys):
    assert main(["--cases", "--agent", "2"]) == 2
    assert main(["--week", "2026-10-02", "--agent", "40"]) == 2
    err = capsys.readouterr().err
    assert "--agent needs --week" in err and "1 to 12 weeks" in err
