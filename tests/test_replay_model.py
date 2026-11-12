"""The model decider and its spending caps, against a local HTTP server standing in for the Anthropic API.

The real SDK makes real HTTP calls to it, so the request shape, the caps and
the model_calls records are tested end to end, and no test spends money. Every
record goes to the test's throwaway schema, never the real database.
"""

import json
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from vending import config, db, harness
from vending.replay.__main__ import main
from vending.replay.cases import all_cases
from vending.replay.deciders import Baseline
from vending.replay.model import BudgetError, ModelDecider, state_json
from vending.replay.scoring import FAIL, PASS, score

KEY = "test-team-key"


def case(name):
    return next(c for c in all_cases() if c.name == name)


def baseline_json(state):
    """The baseline's plan as the model would send it: every key present, null for "left out"."""
    plan = Baseline().decide(state)
    rows = [{"slot": s.slot, "keep": s.keep, "fill_to": s.fill_to,
             "sale_price": None if s.price_cents is None else f"{s.price_cents // 100}.{s.price_cents % 100:02d}",
             "product": s.product, "reason": s.reason} for s in plan.slots]
    return {"visit": "scheduled", "reason": "restock to last week", "slots": rows}


class FakeAnthropic:
    """Records requests. `tokens` is what count_tokens reports; `count_status` fails the count;
    `reply` is (status, stop_reason, text); `turns` (if set) is a list of replies served in order."""

    def __init__(self):
        self.counts, self.posts, self.keys = [], [], []
        self.tokens, self.count_status = 3000, 200
        self.usage = (3000, 2000)
        self.reply = (200, "end_turn", "{}")
        self.turns = None
        self.delay = 0.0   # seconds before answering a message, to keep requests in flight
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("request-id", "req_test")
                self.end_headers()
                self.wfile.write(data)

            def _error(self, status):
                self._reply(status, {"type": "error", "error": {"type": "api_error", "message": "boom"}})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.keys.append(self.headers.get("Authorization"))   # SDK 2.x: a bearer token
                if self.path.startswith("/v1/messages/count_tokens"):
                    fake.counts.append(body)
                    if fake.count_status != 200:
                        return self._error(fake.count_status)
                    return self._reply(200, {"input_tokens": fake.tokens})
                assert self.path.startswith("/v1/messages")
                fake.posts.append(body)
                time.sleep(fake.delay)
                status, stop, content = fake.turns.pop(0) if fake.turns else fake.reply
                if status != 200:
                    return self._error(status)
                if isinstance(content, str):
                    content = [{"type": "text", "text": content}]
                self._reply(200, {
                    "id": "msg_test", "type": "message", "role": "assistant", "model": body["model"],
                    "content": content, "stop_reason": stop, "stop_sequence": None,
                    "stop_details": {"type": "refusal", "category": "cyber", "explanation": "x"}
                    if stop == "refusal" else None,
                    "usage": {"input_tokens": fake.usage[0], "output_tokens": fake.usage[1],
                              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                })

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def api(monkeypatch, schema_url):
    """The fake API, with every model call recorded in this test's throwaway schema."""
    from tests.conftest import open_in

    fake = FakeAnthropic()
    monkeypatch.setenv("VENDING_ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("VENDING_ANTHROPIC_BASE_URL", fake.url)
    # Decoys: Claude Code's variables must never be used.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "the-claude-code-key-must-not-be-used")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(db, "connect", lambda: open_in(*schema_url))
    yield fake
    fake.server.shutdown()
    fake.server.server_close()


def calls(conn):
    """Every model_calls row, oldest first."""
    rows = conn.execute("select purpose, label, model, effort, key_env, status, stop_reason, input_tokens,"
                        " output_tokens, cost_usd, worst_case_usd, max_tokens, ledger_entry, recorded_by"
                        " from model_calls order by id").fetchall()
    keys = ("purpose", "label", "model", "effort", "key_env", "status", "stop_reason", "input_tokens",
            "output_tokens", "cost_usd", "worst_case_usd", "max_tokens", "ledger_entry", "recorded_by")
    return [dict(zip(keys, r)) for r in rows]


def spent_today(conn):
    return harness.spent_since(conn, harness.day_start(datetime.now(timezone.utc)), "test")


def spend_earlier_today(conn, dollars):
    """A settled test call already on today's books."""
    conn.execute("insert into model_calls (purpose, run_id, model, key_env, status, max_tokens, worst_case_usd,"
                 " cost_usd, recorded_by) values ('test', 'earlier', 'claude-opus-5-5', 'VENDING_ANTHROPIC_API_KEY',"
                 " 'ok', 1, %s, %s, 'test')", (dollars, dollars))


# With the fake's defaults: 3,000 tokens counted, padded to 3,500, 16,000 out, on Opus at $4/$20,
# x 1.1 for US-only routing: the worst case the harness reserves.
WORST = Decimal("0.3674")
ACTUAL = Decimal("0.052")      # 3,000 in + 2,000 out


def test_a_run_sends_the_week_gets_a_valid_plan_and_records_its_cost(api, conn):
    c = case("catalog-request")
    api.reply = (200, "end_turn", json.dumps(baseline_json(c.state)))
    decider = ModelDecider()
    result = score(c.state, decider.decide(c.state), c.next_week)
    assert result.passed and {ch.name: ch.status for ch in result.checks}["valid plan"] == PASS
    sent = api.posts[0]
    assert sent["model"] == "claude-opus-5-5" and sent["max_tokens"] == 16000
    assert sent["output_config"]["effort"] == "medium" and sent["service_tier"] == "standard_only"
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert "temperature" not in sent and "thinking" not in sent
    assert api.counts[0]["output_config"] == sent["output_config"]   # the schema is counted exactly
    week = json.loads(sent["messages"][0]["content"].split("\n", 1)[1])
    assert week["slots"][0]["unit_cost"] == "1.00" and len(week["slots"]) == 20
    assert set(api.keys) == {f"Bearer {KEY}"}   # the team key, at the URL we chose
    (row,) = calls(conn)
    assert (row["purpose"], row["model"], row["effort"], row["key_env"]) == (
        "test", "claude-opus-5-5", "medium", "VENDING_ANTHROPIC_API_KEY")
    assert (row["status"], row["stop_reason"], row["input_tokens"], row["output_tokens"]) == ("ok", "end_turn", 3000, 2000)
    assert (row["cost_usd"], row["worst_case_usd"]) == (ACTUAL, WORST) and row["label"].startswith("exam 2026-10-02")
    assert row["ledger_entry"] is None and row["recorded_by"] == "replay"
    assert decider.runs[0]["request_id"] == "req_test" and spent_today(conn) == ACTUAL


def test_test_calls_never_reach_the_competition_books(api, conn):
    api.reply = (200, "end_turn", json.dumps(baseline_json(case("low-balance").state)))
    ModelDecider().decide(case("low-balance").state)
    assert conn.execute("select count(*) from ledger_entries").fetchone()[0] == 0
    assert conn.execute("select count(*) from action_log where kind = 'write'").fetchone()[0] == 0


def test_unknowns_reach_the_model_as_null():
    week = state_json(case("nonsense-input").state)
    assert week["balance"] is None and week["catalog"] is None
    assert week["slots"][0] == {"slot": "15", "product": None, "price": "2.50", "units_sold": None,
                                "units_per_day": None, "price_changed_during_week": None,
                                "stock_on_rail": None, "unit_cost": None}


def test_the_answer_is_shortened_to_fit_the_run_cap(api, conn):
    api.tokens = 180_000   # padded to 198,200 in: $0.87208 at $4/M x 1.1, leaving 5,814 tokens at $22/M
    api.reply = (200, "end_turn", json.dumps(baseline_json(case("low-balance").state)))
    ModelDecider().decide(case("low-balance").state)
    assert api.posts[0]["max_tokens"] == 5_814
    assert calls(conn)[0]["max_tokens"] == 5_814


def test_a_prompt_too_big_for_the_run_cap_is_never_sent(api, conn):
    api.tokens = 240_000
    with pytest.raises(BudgetError, match="Not sent"):
        ModelDecider().decide(case("low-balance").state)
    assert api.posts == [] and calls(conn) == []


def test_the_daily_test_cap_counts_todays_calls(api, conn):
    config.set_value(conn, "model_test_daily_cap_cents", 400, "test")
    spend_earlier_today(conn, Decimal("3.95"))
    with pytest.raises(BudgetError, match=r"Not sent: \$0.0500 is left"):
        ModelDecider().decide(case("low-balance").state)
    assert api.posts == []


def test_the_daily_test_cap_defaults_to_the_teams_setting(api, conn):
    api.reply = (200, "end_turn", json.dumps(baseline_json(case("low-balance").state)))
    decider = ModelDecider()
    assert decider.budget.daily_cap is None            # not read until a request runs: printing it never fails
    decider.decide(case("low-balance").state)
    assert decider.budget.daily_cap == Decimal(20)     # the cap that request ran under
    config.set_value(conn, "model_test_daily_cap_cents", 1500, "test")
    later = ModelDecider()
    later.decide(case("low-balance").state)
    assert later.budget.daily_cap == Decimal(15)
    assert ModelDecider(daily_cap=Decimal(2)).budget.daily_cap == Decimal(2)


def test_concurrent_runs_cannot_both_spend_the_last_dollar(api, conn):
    # $3.40 of a $4.00 test cap spent today: $0.60 left between four requests in flight together.
    config.set_value(conn, "model_test_daily_cap_cents", 400, "test")
    spend_earlier_today(conn, Decimal("3.40"))
    api.reply = (200, "end_turn", json.dumps(baseline_json(case("low-balance").state)))
    api.delay = 1.0
    outcomes = []

    def one():
        try:
            ModelDecider().decide(case("low-balance").state)
            outcomes.append("sent")
        except BudgetError as error:
            outcomes.append(str(error))

    threads = [threading.Thread(target=one) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Each reserves its worst case under the database lock, lowering max_tokens to fit what's left.
    reserved = sum(r["worst_case_usd"] for r in calls(conn)[1:])
    assert reserved <= Decimal("0.60") and 1 <= outcomes.count("sent") <= 2
    assert any("Not sent" in o for o in outcomes)


def test_a_refusal_fails_valid_plan_and_is_still_recorded(api, conn):
    api.reply = (200, "refusal", "")
    c = case("low-balance")
    result = score(c.state, ModelDecider().decide(c.state), c.next_week)
    assert result.checks[0].status == FAIL and "declined" in result.checks[0].detail
    assert calls(conn)[-1]["stop_reason"] == "refusal"


def test_an_answer_cut_off_mid_json_fails_valid_plan(api):
    api.reply = (200, "max_tokens", '{"visit": "scheduled", "slots": [{"slot": "15"')
    c = case("low-balance")
    assert score(c.state, ModelDecider().decide(c.state), c.next_week).checks[0].status == FAIL


def test_an_empty_answer_fails_valid_plan(api):
    api.reply = (200, "end_turn", [])
    c = case("low-balance")
    assert score(c.state, ModelDecider().decide(c.state), c.next_week).checks[0].status == FAIL


def test_a_server_error_is_not_retried_and_counts_at_its_worst_case(api, conn):
    api.reply = (500, None, None)
    decider = ModelDecider()
    with pytest.raises(BudgetError, match="kept at its worst case"):
        decider.decide(case("low-balance").state)
    assert len(api.posts) == 1 and spent_today(conn) == WORST
    assert decider.runs[0]["status"] == "error" and Decimal(decider.runs[0]["cost_usd"]) == WORST
    assert calls(conn)[0]["status"] == "error"


def test_a_rejected_request_is_not_billed_but_is_seen(api, conn):
    api.reply = (429, None, None)
    decider = ModelDecider()
    with pytest.raises(BudgetError, match="not billed"):
        decider.decide(case("low-balance").state)
    (row,) = calls(conn)
    assert row["status"] == "refused" and row["cost_usd"] == 0 and spent_today(conn) == 0
    assert [(r["status"], r["cost_usd"]) for r in decider.runs] == [("refused", "0.000000")]


def test_a_failed_count_sends_nothing(api, conn):
    api.count_status = 401
    decider = ModelDecider()
    with pytest.raises(BudgetError, match="Counting the request failed .*AuthenticationError 401"):
        decider.decide(case("low-balance").state)
    assert api.posts == [] and calls(conn) == [] and decider.runs == []


def test_no_database_no_call(api, monkeypatch):
    def down():
        raise OSError("database is down")

    monkeypatch.setattr(db, "connect", down)
    with pytest.raises(BudgetError, match="usage records couldn't be opened"):
        ModelDecider(daily_cap=Decimal(4)).decide(case("low-balance").state)
    assert api.counts == [] and api.posts == []


def test_no_key_no_call(api, monkeypatch):
    monkeypatch.delenv("VENDING_ANTHROPIC_API_KEY")
    with pytest.raises(BudgetError, match="VENDING_ANTHROPIC_API_KEY is not set"):
        ModelDecider().decide(case("low-balance").state)
    assert api.counts == [] and api.posts == []


@pytest.mark.parametrize("name", ["ANTHROPIC_API_KEY", "anthropic_api_key", "Anthropic_Auth_Token"])
def test_claude_codes_variables_are_refused_in_any_case(name):
    with pytest.raises(ValueError, match="Claude Code reads"):
        ModelDecider(key_env=name)


def test_bad_settings_are_refused_up_front():
    with pytest.raises(ValueError, match="(?i)no price"):
        ModelDecider(model="claude-opus-9")
    with pytest.raises(ValueError, match="effort"):
        ModelDecider(effort="huge")


def test_requests_go_to_anthropic_unless_we_say_otherwise(monkeypatch):
    monkeypatch.delenv("VENDING_ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://some-gateway")
    assert ModelDecider().budget.base_url == "https://api.anthropic.com"


def test_cli_prints_the_cost_and_stops_cases_at_the_daily_cap(api, conn, capsys):
    api.reply = (200, "end_turn", json.dumps(baseline_json(case("low-balance").state)))
    assert main(["--case", "low-balance", "--decider", "model", "--model", "claude-sonnet-5-5"]) == 0
    out = capsys.readouterr().out
    # 3,000 in at $2/M + 2,000 out at $10/M = $0.026
    assert "decider: model:claude-sonnet-5-5@medium" in out and "Model cost: $0.0260 for 1 request " in out
    assert "row in model_calls" in out
    config.set_value(conn, "model_test_daily_cap_cents", 400, "test")
    spend_earlier_today(conn, Decimal("3.91"))
    assert main(["--cases", "--decider", "model", "--model", "claude-sonnet-5-5"]) == 0
    out = capsys.readouterr().out
    assert out.count("not scored") >= 7 and "Model cost:" in out


def test_cli_turns_a_failed_count_into_a_row_not_a_traceback(api, capsys):
    api.count_status = 401
    assert main(["--cases", "--decider", "model"]) == 0
    assert capsys.readouterr().out.count("Counting the request failed") == 10


# --- Found by the blind review (11 Nov) ----------------------------------------------

def test_costs_stay_exact_for_the_caps(api, conn):
    api.usage = (12_345, 1)
    api.reply = (200, "end_turn", json.dumps(baseline_json(case("low-balance").state)))
    decider = ModelDecider()
    decider.decide(case("low-balance").state)
    (run,) = decider.runs
    assert isinstance(run["cost"], Decimal) and run["cost"] == calls(conn)[0]["cost_usd"]


def test_the_footer_reads_no_database_after_paid_calls(api, monkeypatch):
    from vending.replay.__main__ import spend

    api.reply = (200, "end_turn", json.dumps(baseline_json(case("low-balance").state)))
    decider = ModelDecider()
    decider.decide(case("low-balance").state)

    def down():
        raise OSError("database went away after the call")

    monkeypatch.setattr(db, "connect", down)
    lines = spend(decider)
    assert "$20 a day" in lines[0] and lines[1].startswith(f"  call {decider.runs[0]['call_id']}:")


def test_custom_headers_from_the_environment_are_refused(api, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "x-api-key: someone-elses-key")
    with pytest.raises(BudgetError, match="ANTHROPIC_CUSTOM_HEADERS"):
        ModelDecider().decide(case("low-balance").state)
    assert api.counts == [] and api.posts == []


def test_a_key_with_spaces_inside_is_refused(api, monkeypatch):
    monkeypatch.setenv("VENDING_ANTHROPIC_API_KEY", "sk-ant-abc def")
    with pytest.raises(BudgetError, match="unusual characters"):
        ModelDecider().decide(case("low-balance").state)
    assert api.counts == []


def test_the_harness_checks_the_clients_real_key(api, conn):
    import anthropic

    other = anthropic.Anthropic(api_key="not-the-team-key", base_url=api.url)
    with pytest.raises(harness.HarnessError, match="doesn't use the key in VENDING_ANTHROPIC_API_KEY"):
        harness.Harness(other, db.connect, purpose="test")
    bearer = anthropic.Anthropic(api_key=KEY, base_url=api.url)
    bearer.auth_token = "a-bearer-token"   # the SDK refuses both in its constructor, so set it after
    with pytest.raises(harness.HarnessError, match="doesn't use the key"):
        harness.Harness(bearer, db.connect, purpose="test")
    harness.Harness(anthropic.Anthropic(api_key=KEY, base_url=api.url), db.connect, purpose="test")
