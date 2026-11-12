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


def answer(decision="skip", reason="nothing sold out", update="Quiet week.", notes="Watch slot 15.",
           slot_reasons=None, stop_reason="end_turn", cut_at=None):
    text = json.dumps({"decision": decision, "reason": reason, "update": update, "notes": notes,
                       "slot_reasons": slot_reasons or []})
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text[:cut_at])], stop_reason=stop_reason)


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

    def __init__(self, names=SERVER_TOOLS, replies=None, schemas=None):
        schemas = schemas or {}
        self.listed = [SimpleNamespace(name=n, description=f"{n} tool", inputSchema=schemas.get(n, {"type": "object"}))
                       for n in names]
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
PREVIEWED = {"success": True, "message": "ok", "ok": True, "plan_hash": "abc"}
SENT = {"success": True, "message": "sent", "request_id": "req_9", "plan_hash": "abc"}
LIVE_TOOLS = SERVER_TOOLS + ["submit_desired_state"]


def preview(id="p1"):
    return tool_turn(use("preview_desired_state", {"desired": PLAN}, id=id))


def submit(id="s1", desired=PLAN):
    return use("submit_desired_state", {"desired": desired}, id=id)


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
    """(entries, cents) of token spend in the ledger: the competition's books, so live runs only."""
    return conn.execute("select count(*), coalesce(sum(-amount_cents), 0) from ledger_entries"
                        " where kind = 'tokens'").fetchone()


def calls(conn):
    """Every model request recorded, as {purpose: count}: live and test alike."""
    return dict(conn.execute("select purpose, count(*) from model_calls group by purpose").fetchall())


# --- No database --------------------------------------------------------------------

def test_the_write_tools_match_the_servers():
    from vending import server

    assert agent.WRITE_TOOLS == server.WRITE_TOOLS


def test_a_dry_run_offers_no_write_tool():
    listed = Tools(LIVE_TOOLS).listed
    assert not {"request_catalog_product", "submit_desired_state"} & {
        t["name"] for t in agent.tool_definitions(listed, live=False)}
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
                    answer("submit", reason="slot 15 sold out", notes="Raise 15 next week?",
                           slot_reasons=[{"slot": "15", "reason": "Sold out by Tuesday."}]))
    tools = Tools(replies={"preview_desired_state": {"success": True, "message": "ok", "ok": True,
                                                     "plan_hash": "abc"}})
    run_id, out = go(connect, client, tools)
    assert [name for name, _ in tools.calls] == ["get_machine_state", "preview_desired_state"]
    mode, status, plan, update, notes, detail, cost, request_id = stored(conn, run_id)
    assert (mode, status, request_id) == ("dry-run", "skipped", None)
    assert detail.startswith("Dry run: would have submitted the previewed plan.")
    # The previewed plan, plus what the dashboard shows beside it: the visit and the reasons.
    assert plan == {"slots": [{**PLAN["slots"][0], "reason": "Sold out by Tuesday."}], "off_cycle": False,
                    "plan_hash": "abc", "visit": "scheduled", "reason": "slot 15 sold out"}
    assert notes == "Raise 15 next week?"
    # Three requests, each 1000 in x $4 + 500 out x $20 per million = 1.4 cents. A dry run is test
    # spend: recorded in model_calls and on the run, never in the ledger.
    assert cost == Decimal("0.042") and calls(conn) == {"test": 3} and tokens(conn) == (0, 0)
    # No write tool was offered, and every request asked for the answer's JSON.
    first = client.sent[0]
    assert "request_catalog_product" not in {t["name"] for t in first["tools"]}
    assert first["output_config"]["format"]["schema"] == agent.ANSWER_SCHEMA
    assert first["service_tier"] == "standard_only" and first["cache_control"] == {"type": "ephemeral"}


def test_a_dry_run_never_sends_a_write_the_model_makes_up(connect, conn):
    client = Script(tool_turn(submit(), use("request_catalog_product", {"name": "x", "size": "12 oz"}, id="t2")),
                    answer())
    tools = Tools(LIVE_TOOLS)          # the server registers them; the dry run still mustn't call them
    go(connect, client, tools)
    assert tools.calls == []
    results = client.sent[1]["messages"][-1]["content"]
    assert len(results) == 2 and all(r["is_error"] for r in results)


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
    client = Script(preview(), tool_turn(submit(), submit(id="s2")), tool_turn(submit(id="s3")),
                    answer("submit", reason="restock 15"))
    schema = {"type": "object", "properties": {"desired": {}, "expected_plan_hash": {}}}
    tools = Tools(LIVE_TOOLS, replies={"preview_desired_state": PREVIEWED, "submit_desired_state": SENT},
                  schemas={"submit_desired_state": schema})
    run_id, out = go(connect, client, tools, live=True)
    sent = [args for name, args in tools.calls if name == "submit_desired_state"]
    assert sent == [{"desired": PLAN, "expected_plan_hash": "abc"}]     # the preview's hash, added for it
    assert "already submitted" in client.sent[2]["messages"][-1]["content"][1]["content"]
    assert "already submitted" in client.sent[3]["messages"][-1]["content"][0]["content"]
    mode, status, plan, *_, request_id = stored(conn, run_id)
    assert (mode, status, request_id) == ("live", "submitted", "req_9")
    assert plan == {**PLAN, "plan_hash": "abc", "visit": "scheduled", "reason": "restock 15"}
    # A live run spends the competition's money: every request is in the ledger as well.
    assert calls(conn) == {"live": 4} and tokens(conn)[0] == 4


def test_a_refused_submit_is_a_failed_run_not_a_submitted_one(connect, conn):
    client = Script(preview(), tool_turn(submit()), answer("submit"))
    tools = Tools(LIVE_TOOLS, replies={"preview_desired_state": PREVIEWED, "submit_desired_state": {
        "success": False, "message": "refused: over balance", "refusals": [{"code": "over_balance", "message": "x"}]}})
    run_id, out = go(connect, client, tools, live=True)
    assert out.status == "failed" and "nothing was bought" in out.detail and "over balance" in out.detail


def test_saying_submit_without_submitting_is_a_failure_on_a_live_run(connect, conn):
    tools = Tools(LIVE_TOOLS)
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
    assert stored(conn, run_id)[1] == "failed" and calls(conn) == {"test": 1}


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
    assert json.loads(reply["content"])["success"] is False and "poll" in reply["content"].lower()
    # The server logged the call in this schema, so it used the same database.
    assert ("get_machine_state",) in conn.execute("select tool from action_log").fetchall()


def test_a_plan_that_was_not_previewed_is_not_submitted(connect, conn):
    changed = {"slots": [{"slot": "15", "fill_to": 8, "sale_price": "2.50"}]}
    client = Script(tool_turn(submit()), preview(), tool_turn(submit(desired=changed)), answer("skip"))
    tools = Tools(LIVE_TOOLS, replies={"preview_desired_state": PREVIEWED})
    go(connect, client, tools, live=True)
    assert [name for name, _ in tools.calls] == ["preview_desired_state"]
    assert "Not sent" in client.sent[1]["messages"][-1]["content"][0]["content"]
    assert "Not sent" in client.sent[3]["messages"][-1]["content"][0]["content"]


def test_a_preview_with_defaults_left_out_still_matches_the_submit(connect, conn):
    client = Script(tool_turn(use("preview_desired_state", {"desired": {"slots": PLAN["slots"]}})),
                    tool_turn(submit()), answer("submit"))
    tools = Tools(LIVE_TOOLS, replies={"preview_desired_state": PREVIEWED, "submit_desired_state": SENT})
    run_id, out = go(connect, client, tools, live=True)
    assert out.status == "submitted"


def test_a_cap_after_the_submit_still_records_submitted(connect, conn):
    # The caps are read when the run starts, so once the submit is in, spend the rest of today elsewhere.
    class DaySpentAfterSubmit(Tools):
        async def call(self, name, arguments):
            result = await super().call(name, arguments)
            if name == "submit_desired_state":
                conn.execute("update ledger_entries set amount_cents = -400 where id ="
                             " (select max(id) from ledger_entries where kind = 'tokens')")
            return result

    client = Script(preview(), tool_turn(submit()), answer("submit"))
    tools = DaySpentAfterSubmit(LIVE_TOOLS, replies={"preview_desired_state": PREVIEWED, "submit_desired_state": SENT})
    run_id, out = go(connect, client, tools, live=True)
    mode, status, plan, update, notes, detail, cost, request_id = stored(conn, run_id)
    assert (status, request_id) == ("submitted", "req_9")
    assert detail.startswith("Submitted purchase request req_9; afterwards: Stopped on turn 3")


def test_a_tool_that_breaks_after_the_submit_keeps_the_request_id(connect, conn):
    class BreaksLater(Tools):
        async def call(self, name, arguments):
            if name == "get_pnl":
                raise RuntimeError("server died")
            return await super().call(name, arguments)

    client = Script(preview(), tool_turn(submit(), use("get_pnl", id="t2")))
    tools = BreaksLater(LIVE_TOOLS, replies={"preview_desired_state": PREVIEWED, "submit_desired_state": SENT})
    run_id, out = go(connect, client, tools, live=True)
    mode, status, plan, update, notes, detail, cost, request_id = stored(conn, run_id)
    # It never answered, so the plan is stored without reasons.
    assert (status, request_id, plan) == ("submitted", "req_9", {**PLAN, "plan_hash": "abc", "visit": "scheduled"})
    assert "server died" in detail


@pytest.mark.parametrize("reply, is_error", [
    ({"success": False, "status": "unknown", "message": "the bridge answered 502"}, False),
    ({"success": False, "message": "the bridge ACCEPTED purchase request req_7 but storing that failed"}, True),
])
def test_a_submit_with_an_unknown_result_is_flagged_unresolved(connect, conn, reply, is_error):
    class Answers(Tools):
        async def call(self, name, arguments):
            result = await super().call(name, arguments)
            result.isError = is_error and name == "submit_desired_state"
            return result

    client = Script(preview(), tool_turn(submit()), tool_turn(submit(id="s2")), answer("skip"))
    tools = Answers(LIVE_TOOLS, replies={"preview_desired_state": PREVIEWED, "submit_desired_state": reply})
    run_id, out = go(connect, client, tools, live=True)
    row = stored(conn, run_id)
    assert row[1] == "failed" and row[5].startswith("SUBMISSION UNRESOLVED") and reply["message"] in row[5]
    assert [n for n, _ in tools.calls].count("submit_desired_state") == 1      # never sent again


def test_turning_the_halt_on_mid_run_stops_before_the_next_model_call(connect, conn):
    class HaltsOnCall(Tools):
        async def call(self, name, arguments):
            set_halted(conn, True, "test")
            return await super().call(name, arguments)

    client = Script(tool_turn(use("get_pnl")), answer())
    run_id, out = go(connect, client, HaltsOnCall())
    assert out.status == "halted" and len(client.sent) == 1
    assert stored(conn, run_id)[1] == "halted"


def test_a_live_run_reads_only_live_notes(connect, conn):
    go(connect, Script(answer(notes="dry: would submit 15")), Tools())
    client = Script(answer())
    go(connect, client, Tools())                                    # a dry run reads it, labelled
    assert "a dry run (nothing it planned was sent)" in client.sent[0]["messages"][0]["content"]
    client = Script(answer())
    go(connect, client, Tools(LIVE_TOOLS), live=True)               # a live run never does
    opening = client.sent[0]["messages"][0]["content"]
    assert "would submit 15" not in opening and "No notes yet" in opening


def test_the_server_never_gets_the_model_key(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "VENDING_ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.setenv(name, "not-a-real-key")
    env = agent.server_env()
    assert "not-a-real-key" not in env.values() and env["VENDING_TOOLS"] == "built"


# --- The model key ------------------------------------------------------------------

def test_the_client_pays_with_the_team_key_and_nothing_else(monkeypatch):
    from vending import harness

    monkeypatch.setenv("VENDING_ANTHROPIC_API_KEY", " team-key-for-tests ")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "someone-elses-key")     # what a bare Anthropic() would read
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    client = agent.model_client()
    assert client.api_key == "team-key-for-tests" and client.max_retries == 0
    harness.check_client(client, "VENDING_ANTHROPIC_API_KEY")       # the harness accepts it as built


def test_a_run_without_the_team_key_fails_before_the_server_starts(connect, conn, monkeypatch):
    opened = []

    class Watched(Tools):
        def opener(self):
            opened.append(True)
            return super().opener()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "someone-elses-key")     # never a stand-in for ours
    monkeypatch.setattr(agent.settings, "get", lambda name, default=None: default)
    tools = Watched()
    run_id, out = anyio.run(lambda: agent.run(live=False, connect=connect, now=lambda: NOW, open_tools=tools.opener))
    assert out.status == "failed" and "NoModelKey" in out.detail and "VENDING_ANTHROPIC_API_KEY" in out.detail
    assert opened == [] and tools.calls == [] and calls(conn) == {}
    assert stored(conn, run_id)[:2] == ("dry-run", "failed")


# --- Answers that can't be read ------------------------------------------------------

def test_an_answer_cut_off_at_its_token_limit_says_so(connect, conn):
    client = Script(tool_turn(use("get_pnl")), answer(stop_reason="max_tokens", cut_at=40))
    run_id, out = go(connect, client, Tools())
    assert out.status == "failed" and "cut off at its token limit" in out.detail and "turn 2" in out.detail
    assert out.answer is None and stored(conn, run_id)[1] == "failed"


# --- The plan the dashboard reads ----------------------------------------------------

def test_the_recorded_plan_carries_the_visit_and_each_slots_reason():
    plan = {"slots": [{"slot": "15", "fill_to": 7}, {"slot": "16", "keep": True}], "off_cycle": True, "plan_hash": "abc"}
    said = {"reason": "15 sold out", "slot_reasons": [{"slot": "15", "reason": "Sold out by Tuesday."},
                                                      {"slot": "99", "reason": "not in the plan"}, "junk"]}
    assert agent.recorded_plan(plan, said) == {
        "slots": [{"slot": "15", "fill_to": 7, "reason": "Sold out by Tuesday."}, {"slot": "16", "keep": True}],
        "off_cycle": True, "plan_hash": "abc", "visit": "midweek", "reason": "15 sold out"}
    assert plan["slots"][0] == {"slot": "15", "fill_to": 7}                # the plan it compares submits with is untouched
    assert agent.recorded_plan(plan, {"slot_reasons": "junk"})["visit"] == "midweek"
    assert agent.recorded_plan(None, said) is None


def test_the_dashboard_shows_the_recorded_reasons(connect, conn):
    from vending import dashboard

    said = [{"slot": "15", "reason": "Sold out by Tuesday."}]
    tools = Tools(replies={"preview_desired_state": PREVIEWED})
    go(connect, Script(preview(), answer("submit", reason="restock 15", slot_reasons=said)), tools)
    shown = dashboard._plan(conn)
    assert (shown["visit"], shown["reason"]) == ("scheduled", "restock 15")
    assert {s["slot"]: s["now"] for s in shown["slots"]}["15"]["reason"] == "Sold out by Tuesday."


def test_a_submit_in_the_same_response_as_its_preview_is_not_sent(connect, conn):
    client = Script(tool_turn(use("preview_desired_state", {"desired": PLAN}, id="p1"), submit()),
                    tool_turn(submit(id="s2")), answer("submit"))
    tools = Tools(LIVE_TOOLS, replies={"preview_desired_state": PREVIEWED, "submit_desired_state": SENT})
    run_id, out = go(connect, client, tools, live=True)
    assert "next turn" in client.sent[1]["messages"][-1]["content"][1]["content"]
    assert [n for n, _ in tools.calls] == ["preview_desired_state", "submit_desired_state"]   # sent a turn later
    assert out.status == "submitted"


def test_a_submit_call_that_raises_is_flagged_unresolved(connect, conn):
    class DiesOnSubmit(Tools):
        async def call(self, name, arguments):
            if name == "submit_desired_state":
                raise RuntimeError("stdio closed")
            return await super().call(name, arguments)

    client = Script(preview(), tool_turn(submit()))
    run_id, out = go(connect, client, DiesOnSubmit(LIVE_TOOLS, replies={"preview_desired_state": PREVIEWED}), live=True)
    status, detail = stored(conn, run_id)[1], stored(conn, run_id)[5]
    assert status == "failed" and detail.startswith("SUBMISSION UNRESOLVED") and "stdio closed" in detail


# --- The rules card -----------------------------------------------------------------

def test_the_system_prompt_is_the_rules_card_then_the_instructions(connect, conn):
    go(connect, Script(answer(notes="Slot 3 is slow.")), Tools())
    client = Script(tool_turn(use("get_machine_state")), answer())
    go(connect, client, Tools())
    card = agent.RULES_CARD.read_text(encoding="utf-8").strip()
    system = client.sent[0]["system"]
    assert system.startswith(card) and system.index(card) < system.index(agent.SYSTEM)
    assert "get_project_rules" in system
    # The volatile part (the time, last week's notes) is in the messages, never in the cached system prompt.
    assert "Slot 3 is slow" not in system and "Eastern" not in system
    assert "Slot 3 is slow" in client.sent[0]["messages"][0]["content"]
    assert [r["system"] for r in client.sent] == [system, system]


def test_a_missing_rules_card_fails_the_run_before_any_model_request(connect, conn, tmp_path):
    opened = []

    class Watched(Tools):
        def opener(self):
            opened.append(True)
            return super().opener()

    client, tools = Script(answer()), Watched()
    missing = tmp_path / "rules-card.md"
    run_id, out = anyio.run(lambda: agent.run(live=False, connect=connect, client=client, now=lambda: NOW,
                                              open_tools=tools.opener, rules_card=missing))
    assert out.status == "failed" and "RulesCardMissing" in out.detail and "rules card" in out.detail
    assert client.sent == [] and tools.calls == [] and opened == []
    assert stored(conn, run_id)[:2] == ("dry-run", "failed")
    assert tokens(conn) == (0, 0)


def test_an_empty_rules_card_is_refused(tmp_path):
    empty = tmp_path / "rules-card.md"
    empty.write_text("  \n", encoding="utf-8")
    with pytest.raises(agent.RulesCardMissing):
        agent.system_prompt(empty)


def test_get_project_rules_is_offered_when_the_server_lists_it(connect, conn):
    client = Script(answer())
    go(connect, client, Tools(SERVER_TOOLS + ["get_project_rules"]))
    assert "get_project_rules" in {t["name"] for t in client.sent[0]["tools"]}
