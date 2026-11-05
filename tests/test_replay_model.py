"""The model decider, against a local HTTP server standing in for the Anthropic API.

The real SDK makes real HTTP calls to it, so the request shape, the caps and
the spend log are tested end to end, and no test spends money.
"""

import json
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from vending.replay.__main__ import main
from vending.replay.cases import all_cases
from vending.replay.deciders import Baseline
from vending.replay.model import BudgetError, ModelDecider, spent_today
from vending.replay.scoring import FAIL, PASS, score

KEY = "test-team-key"


def case(name):
    return next(c for c in all_cases() if c.name == name)


def baseline_json(state, nulls=True):
    """The baseline's plan as the model would send it: every key present, null for "left out"."""
    plan = Baseline().decide(state)
    rows = [{"slot": s.slot, "keep": s.keep, "fill_to": s.fill_to,
             "sale_price": None if s.price_cents is None else f"{s.price_cents // 100}.{s.price_cents % 100:02d}",
             "product": s.product, "reason": s.reason} for s in plan.slots]
    return {"visit": "scheduled", "reason": "restock to last week", "slots": rows}


class FakeAnthropic:
    """Records requests. `tokens` is what count_tokens reports; `reply` is (status, stop_reason, text)."""

    def __init__(self):
        self.counts, self.posts, self.keys = [], [], []
        self.tokens = 3000
        self.usage = (3000, 2000)
        self.reply = (200, "end_turn", "{}")
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

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                # SDK 2.x sends the key as a bearer token; 1.x sent x-api-key.
                fake.keys.append(self.headers.get("Authorization") or self.headers.get("x-api-key"))
                if self.path.startswith("/v1/messages/count_tokens"):
                    fake.counts.append(body)
                    return self._reply(200, {"input_tokens": fake.tokens})
                assert self.path.startswith("/v1/messages")
                fake.posts.append(body)
                status, stop, text = fake.reply
                if status != 200:
                    return self._reply(status, {"type": "error", "error": {"type": "api_error", "message": "boom"}})
                self._reply(200, {
                    "id": "msg_test", "type": "message", "role": "assistant", "model": body["model"],
                    "content": [{"type": "text", "text": text}], "stop_reason": stop, "stop_sequence": None,
                    "stop_details": {"type": "refusal", "category": "cyber", "explanation": "x"}
                    if stop == "refusal" else None,
                    "usage": {"input_tokens": fake.usage[0], "output_tokens": fake.usage[1],
                              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                })

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def api(monkeypatch, tmp_path):
    fake = FakeAnthropic()
    monkeypatch.setenv("VENDING_ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "the-claude-code-key-must-not-be-used")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", fake.url)
    monkeypatch.setenv("VENDING_SPEND_LOG", str(tmp_path / "spend.jsonl"))
    yield fake
    fake.server.shutdown()
    fake.server.server_close()


def log_rows(tmp_path):
    path = tmp_path / "spend.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_a_run_sends_the_week_gets_a_valid_plan_and_logs_its_cost(api, tmp_path):
    c = case("catalog-request")
    api.reply = (200, "end_turn", json.dumps(baseline_json(c.state)))
    decider = ModelDecider()
    result = score(c.state, decider.decide(c.state), c.next_week)
    assert result.passed and {ch.name: ch.status for ch in result.checks}["valid plan"] == PASS
    sent = api.posts[0]
    assert sent["model"] == "claude-opus-5-5" and sent["max_tokens"] == 16000
    assert sent["output_config"]["effort"] == "medium"
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert "temperature" not in sent and "thinking" not in sent
    week = json.loads(sent["messages"][0]["content"].split("\n", 1)[1])
    assert week["slots"][0]["unit_cost"] == "1.00" and len(week["slots"]) == 20
    assert set(api.keys) == {f"Bearer {KEY}"}   # the team key, never ANTHROPIC_API_KEY
    rows = log_rows(tmp_path)
    assert [r["status"] for r in rows] == ["sent", "end_turn"]
    assert rows[0]["run_id"] == rows[1]["run_id"] and rows[1]["key_env"] == "VENDING_ANTHROPIC_API_KEY"
    # 3,000 in at $4/M + 2,000 out at $20/M
    assert Decimal(rows[1]["cost_usd"]) == Decimal("0.052")
    assert Decimal(rows[0]["cost_usd"]) == Decimal("0.340")   # worst case: 5,000 in + 16,000 out
    assert decider.runs[0]["request_id"] == "req_test"


def test_unknowns_reach_the_model_as_null():
    from vending.replay.model import state_json
    week = state_json(case("nonsense-input").state)
    assert week["balance"] is None and week["catalog"] is None
    assert week["slots"][0] == {"slot": "15", "product": None, "price": "2.50", "units_sold": None,
                                "units_per_day": None, "price_changed_during_week": None,
                                "stock_on_rail": None, "unit_cost": None}


def test_the_answer_is_shortened_to_fit_the_run_cap(api, tmp_path):
    api.tokens = 180_000   # 182,000 in at $4/M = $0.728, leaving $0.272 = 13,600 output tokens
    api.reply = (200, "end_turn", json.dumps(baseline_json(case("low-balance").state)))
    ModelDecider().decide(case("low-balance").state)
    assert api.posts[0]["max_tokens"] == 13_600
    assert Decimal(log_rows(tmp_path)[0]["cost_usd"]) == Decimal("1.000")


def test_a_prompt_too_big_for_the_run_cap_is_never_sent(api, tmp_path):
    api.tokens = 240_000
    with pytest.raises(BudgetError, match="Not sent"):
        ModelDecider().decide(case("low-balance").state)
    assert api.posts == [] and log_rows(tmp_path) == []


def test_the_daily_cap_counts_todays_runs_only(api, tmp_path):
    now = datetime.now(timezone.utc)
    path = tmp_path / "spend.jsonl"
    rows = [{"run_id": "old", "at": (now - timedelta(days=2)).isoformat(), "cost_usd": "9.00"},
            {"run_id": "a", "at": now.isoformat(), "cost_usd": "1.00"},     # worst case, then...
            {"run_id": "a", "at": now.isoformat(), "cost_usd": "0.50"},     # ...what it really cost
            {"run_id": "b", "at": now.isoformat(), "cost_usd": "3.45"}]     # never closed: worst case
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert spent_today(path, now) == Decimal("3.95")
    with pytest.raises(BudgetError, match=r"only \$0.0500 of the cap is left"):
        ModelDecider().decide(case("low-balance").state)
    assert api.posts == []


def test_a_refusal_fails_valid_plan_and_is_still_logged(api, tmp_path):
    api.reply = (200, "refusal", "")
    c = case("low-balance")
    result = score(c.state, ModelDecider().decide(c.state), c.next_week)
    assert result.checks[0].status == FAIL and "declined" in result.checks[0].detail
    assert log_rows(tmp_path)[-1]["status"] == "refusal"


def test_an_answer_cut_off_mid_json_fails_valid_plan(api):
    api.reply = (200, "max_tokens", '{"visit": "scheduled", "slots": [{"slot": "15"')
    c = case("low-balance")
    assert score(c.state, ModelDecider().decide(c.state), c.next_week).checks[0].status == FAIL


def test_a_failed_call_is_not_retried_and_counts_at_its_worst_case(api, tmp_path):
    api.reply = (500, None, None)
    with pytest.raises(BudgetError, match="counted at its worst case"):
        ModelDecider().decide(case("low-balance").state)
    assert len(api.posts) == 1
    assert [r["status"] for r in log_rows(tmp_path)] == ["sent"]
    assert spent_today(tmp_path / "spend.jsonl", datetime.now(timezone.utc)) == Decimal("0.340")


def test_no_key_no_call(api, monkeypatch):
    monkeypatch.delenv("VENDING_ANTHROPIC_API_KEY")
    with pytest.raises(BudgetError, match="VENDING_ANTHROPIC_API_KEY is not set"):
        ModelDecider().decide(case("low-balance").state)
    assert api.counts == [] and api.posts == []


def test_bad_settings_are_refused_up_front():
    with pytest.raises(ValueError, match="not ANTHROPIC_API_KEY"):
        ModelDecider(key_env="ANTHROPIC_API_KEY")
    with pytest.raises(ValueError, match="No price"):
        ModelDecider(model="claude-opus-9")
    with pytest.raises(ValueError, match="effort"):
        ModelDecider(effort="huge")


def test_cli_prints_the_cost_and_stops_cases_at_the_daily_cap(api, tmp_path, capsys, monkeypatch):
    api.reply = (200, "end_turn", json.dumps(baseline_json(case("low-balance").state)))
    assert main(["--case", "low-balance", "--decider", "model", "--model", "claude-sonnet-5-5"]) == 0
    out = capsys.readouterr().out
    assert "decider: model:claude-sonnet-5-5@medium" in out and "Model cost: $0.0260 over 1 runs" in out
    # 3,000 in at $2/M + 2,000 out at $10/M = $0.026. Now leave room for about two more runs.
    now = datetime.now(timezone.utc).isoformat()
    (tmp_path / "spend.jsonl").write_text(json.dumps({"run_id": "x", "at": now, "cost_usd": "3.91"}) + "\n",
                                          encoding="utf-8")
    assert main(["--cases", "--decider", "model", "--model", "claude-sonnet-5-5"]) == 0
    out = capsys.readouterr().out
    assert out.count("not run") >= 7 and "Model cost:" in out
