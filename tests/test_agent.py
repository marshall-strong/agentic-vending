"""Tests for the weekly agent run. The model and the MCP server are both faked: nothing calls the
Anthropic API, the bridge or a real server process. Runs and spend land in a throwaway schema."""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("anthropic")
import anyio  # noqa: E402

from vending import agent  # noqa: E402
from vending.config import set_halted  # noqa: E402

NOW = datetime(2026, 11, 12, 20, 0, tzinfo=timezone.utc)     # Thursday 15:00 Eastern


# --- Fakes -------------------------------------------------------------------------

def use(name, arguments=None, id="t1"):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=arguments or {})


def tool_turn(*blocks):
    return SimpleNamespace(content=list(blocks), stop_reason="tool_use")


def answer(decision="skip", reason="nothing sold out", update="Quiet week.", notes="Watch slot 15."):
    text = json.dumps({"decision": decision, "reason": reason, "update": update, "notes": notes})
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn")


class Script:
    """A fake Anthropic client that answers each request with the next scripted turn."""

    def __init__(self, *turns):
        self.turns, self.sent = list(turns), []
        self.messages = self

    def with_options(self, **_):
        return self

    def count_tokens(self, **_):
        return SimpleNamespace(input_tokens=1000)

    def create(self, **body):
        self.sent.append(json.loads(json.dumps(body, default=lambda o: vars(o))))
        turn = self.turns.pop(0)
        return SimpleNamespace(id=f"msg_{len(self.sent)}", model=body["model"], content=turn.content,
                               stop_reason=turn.stop_reason,
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=500, cache_read_input_tokens=0,
                                                     cache_creation_input_tokens=0, cache_creation=None))


SERVER_TOOLS = ["get_machine_state", "get_sales_history", "get_catalog", "get_account_balance",
                "preview_desired_state", "get_purchase_requests", "get_pnl", "request_catalog_product"]


class Tools:
    """A fake MCP server: records each call and answers from `replies`."""

    def __init__(self, names=SERVER_TOOLS, replies=None):
        self.listed = [SimpleNamespace(name=n, description=f"{n} tool", inputSchema={"type": "object"}) for n in names]
        self.replies, self.calls = replies or {}, []

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        reply = self.replies.get(name, {"success": True, "message": "ok"})
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(reply))],
                               structuredContent=reply, isError=False)

    def opener(self):
        @asynccontextmanager
        async def opened():
            yield self.listed, self.call
        return opened


PLAN = {"slots": [{"slot": "15", "fill_to": 7, "sale_price": "2.50"}], "off_cycle": False}


@pytest.fixture
def connect(schema_url):
    from tests.conftest import open_in

    with open_in(*schema_url) as c:
        set_halted(c, False, "test")    # a new database starts halted
    return lambda: open_in(*schema_url)


def go(connect, client, tools, *, live=False, **kw):
    return anyio.run(lambda: agent.run(live=live, connect=connect, client=client, now=lambda: NOW,
                                       open_tools=tools.opener(), **kw))


def stored(conn, run_id):
    return conn.execute("select mode, status, plan, team_update, notes, detail, cost_usd, request_id"
                        " from agent_runs where id = %s", (run_id,)).fetchone()


def tokens(conn):
    return conn.execute("select count(*), coalesce(sum(-amount_cents), 0) from ledger_entries"
                        " where kind = 'tokens'").fetchone()


# --- No database --------------------------------------------------------------------

def test_the_write_tools_match_the_servers():
    from vending import server

    assert agent.WRITE_TOOLS == server.WRITE_TOOLS


def test_a_dry_run_offers_no_write_tool():
    listed = Tools(SERVER_TOOLS + ["submit_desired_state"]).listed
    assert "request_catalog_product" not in {t["name"] for t in agent.tool_definitions(listed, live=False)}
    assert {"submit_desired_state", "request_catalog_product"} <= {
        t["name"] for t in agent.tool_definitions(listed, live=True)}


# --- Runs on a database -------------------------------------------------------------

def test_a_halted_machine_records_the_run_and_calls_nothing(connect, conn):
    set_halted(conn, True, "test")
    client, tools = Script(), Tools()
    run_id, out = go(connect, client, tools)
    assert out.status == "halted" and client.sent == [] and tools.calls == []
    assert stored(conn, run_id)[:2] == ("dry-run", "halted")
    assert tokens(conn) == (0, 0)


def test_a_dry_run_previews_and_records_what_it_would_submit(connect, conn):
    client = Script(tool_turn(use("get_machine_state")),
                    tool_turn(use("preview_desired_state", {"desired": PLAN}, id="t2")),
                    answer("submit", reason="slot 15 sold out", notes="Raise 15 next week?"))
    tools = Tools(replies={"preview_desired_state": {"success": True, "message": "ok", "ok": True,
                                                     "plan_hash": "abc"}})
    run_id, out = go(connect, client, tools)
    assert [name for name, _ in tools.calls] == ["get_machine_state", "preview_desired_state"]
    mode, status, plan, update, notes, detail, cost, request_id = stored(conn, run_id)
    assert (mode, status, request_id) == ("dry-run", "skipped", None)
    assert detail.startswith("Dry run: would have submitted the previewed plan.")
    assert plan == {**PLAN, "plan_hash": "abc"} and notes == "Raise 15 next week?"
    # Three requests, each 1000 in x $4 + 500 out x $20 per million = 1.4 cents.
    assert tokens(conn) == (3, Decimal("4.2")) and cost == Decimal("0.042")
    # No write tool was offered, and every request asked for the answer's JSON.
    first = client.sent[0]
    assert "request_catalog_product" not in {t["name"] for t in first["tools"]}
    assert first["output_config"]["format"]["schema"] == agent.ANSWER_SCHEMA
    assert first["service_tier"] == "standard_only"


def test_a_dry_run_never_sends_a_submit_the_model_makes_up(connect, conn):
    client = Script(tool_turn(use("submit_desired_state", {"desired": PLAN})), answer())
    tools = Tools(SERVER_TOOLS + ["submit_desired_state"])
    go(connect, client, tools)
    assert tools.calls == []
    result = client.sent[1]["messages"][-1]["content"][0]
    assert result["is_error"] and "dry run" in result["content"]


def test_notes_carry_from_one_run_to_the_next(connect, conn):
    go(connect, Script(answer(notes="Slot 3 is slow; pull it if under 2 a week.")), Tools())
    client = Script(answer())
    go(connect, client, Tools())
    assert "Slot 3 is slow" in client.sent[0]["messages"][0]["content"]
    assert "Thursday 12 November 2026 15:00 Eastern" in client.sent[0]["messages"][0]["content"]


def test_a_live_run_without_submit_built_stops_before_any_model_call(connect, conn):
    client = Script()
    run_id, out = go(connect, client, Tools(), live=True)
    assert out.status == "failed" and "isn't built" in out.detail and client.sent == []
    assert stored(conn, run_id)[:2] == ("live", "failed")


def test_a_live_run_submits_once_and_records_the_request_id(connect, conn):
    client = Script(tool_turn(use("submit_desired_state", {"desired": PLAN, "expected_plan_hash": "abc"})),
                    tool_turn(use("submit_desired_state", {"desired": PLAN}, id="t2")),
                    answer("submit", reason="restock 15"))
    tools = Tools(SERVER_TOOLS + ["submit_desired_state"],
                  replies={"submit_desired_state": {"success": True, "message": "sent", "request_id": "req_9"}})
    run_id, out = go(connect, client, tools, live=True)
    assert [name for name, _ in tools.calls] == ["submit_desired_state"]     # the second was blocked
    assert "already submitted" in client.sent[2]["messages"][-1]["content"][0]["content"]
    mode, status, plan, *_, request_id = stored(conn, run_id)
    assert (mode, status, request_id, plan) == ("live", "submitted", "req_9", PLAN)


def test_a_refused_submit_is_a_failed_run_not_a_submitted_one(connect, conn):
    client = Script(tool_turn(use("submit_desired_state", {"desired": PLAN})), answer("submit"))
    tools = Tools(SERVER_TOOLS + ["submit_desired_state"],
                  replies={"submit_desired_state": {"success": False, "message": "refused: over balance"}})
    run_id, out = go(connect, client, tools, live=True)
    assert out.status == "failed" and "over balance" in out.detail


def test_saying_submit_without_submitting_is_a_failure_on_a_live_run(connect, conn):
    tools = Tools(SERVER_TOOLS + ["submit_desired_state"])
    run_id, out = go(connect, Script(answer("submit")), tools, live=True)
    assert out.status == "failed" and "never called" in out.detail


def test_a_cap_stops_the_run_and_records_it_as_failed(connect, conn):
    from vending import config

    config.set_value(conn, "model_run_cap_cents", 1, "test")   # a cent can't cover the smallest answer
    client = Script(answer())
    run_id, out = go(connect, client, Tools())
    assert out.status == "failed" and "Stopped on turn 1" in out.detail and client.sent == []
    assert stored(conn, run_id)[1] == "failed"


def test_the_last_turn_allows_no_more_tools(connect, conn):
    client = Script(tool_turn(use("get_pnl")), tool_turn(use("get_pnl", id="t2")), answer())
    run_id, out = go(connect, client, Tools(), max_turns=1)
    assert [r["tool_choice"]["type"] for r in client.sent[:2]] == ["auto", "none"]
    assert out.status == "failed" and "Ran out of turns" in out.detail


def test_a_server_that_breaks_still_records_the_run(connect, conn):
    class Broken(Tools):
        async def call(self, name, arguments):
            raise RuntimeError("server died")

    run_id, out = go(connect, Script(tool_turn(use("get_pnl"))), Broken())
    assert out.status == "failed" and "server died" in out.detail
    assert stored(conn, run_id)[1] == "failed" and tokens(conn)[0] == 1


def test_a_dry_run_against_the_real_server_process(schema_url, conn, monkeypatch):
    # The real `python -m vending.server` over stdio, on this test's schema; only the model is faked.
    from urllib.parse import quote

    from tests.conftest import open_in

    url, schema = schema_url
    set_halted(conn, False, "test")
    monkeypatch.setenv("DATABASE_URL", f"{url}{'&' if '?' in url else '?'}options={quote(f'-c search_path={schema}')}")
    client = Script(tool_turn(use("get_machine_state")), answer())
    run_id, out = anyio.run(lambda: agent.run(live=False, connect=lambda: open_in(url, schema), client=client,
                                              now=lambda: NOW))
    assert out.status == "skipped", out.detail
    offered = {t["name"] for t in client.sent[0]["tools"]}
    assert {"get_machine_state", "preview_desired_state", "get_pnl"} <= offered
    assert not offered & agent.WRITE_TOOLS and "set_price" not in offered
    reply = client.sent[1]["messages"][-1]["content"][0]
    assert "No polls" in reply["content"] or "polls" in reply["content"].lower()
    # The server logged the call in this schema, so it used the same database.
    assert ("get_machine_state",) in conn.execute("select tool from action_log").fetchall()
