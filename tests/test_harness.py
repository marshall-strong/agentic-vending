"""Tests for the model-call harness: pricing without a database, then caps and ledger
entries on a throwaway schema. The Anthropic client is faked; nothing calls the API."""

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

psycopg = pytest.importorskip("psycopg")
anthropic = pytest.importorskip("anthropic")
import httpx2  # noqa: E402  (anthropic 2.x is built on it)

from vending import config  # noqa: E402
from vending.harness import (  # noqa: E402
    CapReached,
    Harness,
    HarnessError,
    _refuse_unpriced,
    cost_usd,
    day_start,
    load_prices,
    padded,
    spent_since,
    worst_case_usd,
)

PRICES = load_prices()
NOW = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)    # 10:00 Eastern


def usage(i=0, o=0, read=0, written=0, one_hour=0):
    split = SimpleNamespace(ephemeral_5m_input_tokens=written - one_hour, ephemeral_1h_input_tokens=one_hour)
    return SimpleNamespace(input_tokens=i, output_tokens=o, cache_read_input_tokens=read,
                           cache_creation_input_tokens=written, cache_creation=split)


# --- Pricing (no database) --------------------------------------------------------

def test_the_price_file_has_the_three_current_models():
    assert set(PRICES.models) == {"claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"}
    assert PRICES.checked == "2026-11-09"


def test_opus_prices_every_kind_of_token_at_its_own_rate():
    # 1000 in at $4, 2000 cache reads at $0.20 (0.05x, not 0.1x), 500 5m writes at $5,
    # 300 1h writes at $8, 100 out at $20; all per million.
    u = usage(i=1000, o=100, read=2000, written=800, one_hour=300)
    expected = (1000 * 4 + 2000 * Decimal("0.20") + 500 * 5 + 300 * 8 + 100 * 20) / Decimal(1_000_000)
    assert cost_usd("claude-opus-5-5", u, PRICES) == expected == Decimal("0.0113")


def test_sonnet_cache_reads_bill_at_ten_cents():
    assert cost_usd("claude-sonnet-5-5", usage(read=1_000_000), PRICES) == Decimal("0.10")


def test_haiku_moves_to_the_long_prompt_rates_past_100k_counting_cached_tokens():
    short = usage(i=50_000, read=50_000, o=1000)                # exactly 100,000: short rates
    long = usage(i=50_000, read=50_001, o=1000)                 # 100,001: every token at long rates
    assert cost_usd("claude-haiku-5-5", short, PRICES) == (50_000 * Decimal("0.10") + 50_000 * Decimal("0.01")
                                                          + 1000 * Decimal("0.50")) / 1_000_000
    assert cost_usd("claude-haiku-5-5", long, PRICES) == (50_000 * Decimal("0.50") + 50_001 * Decimal("0.05")
                                                         + 1000 * Decimal("2.50")) / 1_000_000


def test_us_only_routing_costs_ten_percent_more():
    u = usage(i=1_000_000)
    assert cost_usd("claude-opus-5-5", u, PRICES, us_only=True) == Decimal("4.4")


def test_writes_without_a_split_bill_at_the_five_minute_rate():
    u = SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_input_tokens=0,
                        cache_creation_input_tokens=1_000_000, cache_creation=None)
    assert cost_usd("claude-opus-5-5", u, PRICES) == Decimal(5)


def test_worst_case_pads_the_count_and_prices_it_at_the_dearest_rate():
    # 1,000,000 counted is 1,100,200 padded; US-only routing (x1.1) is always assumed.
    assert padded(1_000_000) == 1_100_200
    assert worst_case_usd("claude-opus-5-5", 1_000_000, 0, PRICES, caches=None) == Decimal("4.4008") * Decimal("1.1")
    assert worst_case_usd("claude-opus-5-5", 1_000_000, 0, PRICES, caches="5m") == Decimal("5.501") * Decimal("1.1")
    assert worst_case_usd("claude-opus-5-5", 0, 1_000_000, PRICES, caches="1h") == (Decimal("0.0016") + 20) * Decimal("1.1")


def test_a_haiku_prompt_counted_near_100k_is_priced_at_the_long_rates():
    # 95,000 counted could really be over 100,000, which bills every token at the long rates.
    expected = Decimal(padded(95_000)) * Decimal("0.50") / 1_000_000 * Decimal("1.1")
    assert worst_case_usd("claude-haiku-5-5", 95_000, 0, PRICES, caches=None) == expected


def test_an_unpriced_model_or_feature_is_refused():
    with pytest.raises(HarnessError, match="no price for claude-opus-4-8"):
        PRICES.rates("claude-opus-4-8", 10)
    with pytest.raises(HarnessError, match="web_search"):
        _refuse_unpriced({"tools": [{"type": "web_search_20260209", "name": "web_search"}]})
    with pytest.raises(HarnessError, match="fast mode"):
        _refuse_unpriced({"speed": "fast"})
    with pytest.raises(HarnessError, match="streaming"):
        _refuse_unpriced({"stream": True})
    with pytest.raises(HarnessError, match="extra_body"):
        _refuse_unpriced({"extra_body": {"speed": "fast"}})
    with pytest.raises(HarnessError, match="standard capacity"):
        _refuse_unpriced({"service_tier": "auto"})
    _refuse_unpriced({"tools": [{"name": "get_catalog", "input_schema": {"type": "object"}},
                                {"type": "bash_20250124", "name": "bash"}]})


def test_the_day_starts_at_midnight_eastern_through_daylight_saving():
    assert day_start(datetime(2026, 11, 10, 3, 0, tzinfo=timezone.utc)) == datetime(2026, 11, 9, 5, 0, tzinfo=timezone.utc)
    assert day_start(datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)) == datetime(2026, 7, 1, 4, 0, tzinfo=timezone.utc)


# --- A fake client ---------------------------------------------------------------

def _http_error(cls, status):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("fake", response=httpx2.Response(status, request=request), body=None)


class FakeMessages:
    COUNTS = {"messages", "model", "cache_control", "output_config", "output_format", "system", "thinking",
              "tool_choice", "tools"}

    def __init__(self, input_tokens=1000, answer=None, error=None, during=None):
        self.input_tokens, self.answer, self.error, self.during = input_tokens, answer, error, during
        self.sent = []

    def count_tokens(self, **body):
        assert set(body) <= self.COUNTS, set(body) - self.COUNTS    # the real method takes no others
        return SimpleNamespace(input_tokens=self.input_tokens)

    def create(self, **body):
        self.sent.append(body)
        if self.during:
            self.during()
        if self.error:
            raise self.error
        return self.answer or SimpleNamespace(id=f"msg_{len(self.sent)}", model=body["model"],
                                              usage=usage(i=self.input_tokens, o=500))


class FakeClient:
    def __init__(self, **kw):
        self.messages = FakeMessages(**kw)
        self.options = None

    def with_options(self, **options):
        self.options = options
        return self


REQUEST = {"model": "claude-opus-5-5", "max_tokens": 16000, "messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture
def connect(schema_url):
    from tests.conftest import open_in

    return lambda: open_in(*schema_url)


def harness(connect, client, **kw):
    kw.setdefault("now", lambda: NOW)
    return Harness(client, connect, purpose=kw.pop("purpose", "live"), **kw)


def tokens_entries(conn):
    return conn.execute("select amount_cents, detail, reference, recorded_by from ledger_entries"
                        " where kind = 'tokens' order by id").fetchall()


# --- Calls on a database ------------------------------------------------------------

def test_a_call_writes_one_debit_with_its_cost_and_turns_retries_off(connect, conn):
    client = FakeClient()
    h = harness(connect, client)
    response = h.call(REQUEST, label="week 3")
    assert response.id == "msg_1" and client.options == {"max_retries": 0}
    assert client.messages.sent[0]["service_tier"] == "standard_only"
    # 1000 in x $4 + 500 out x $20, per million = $0.014 = 1.4 cents
    assert tokens_entries(conn) == [(Decimal("-1.4"), "claude-opus-5-5: 1000 tokens in, 500 out",
                                     "live week 3 msg_1", "harness")]
    assert h.spent == Decimal("0.014") and h.calls[0].recorded


def test_max_tokens_is_lowered_to_fit_the_run_cap(connect, conn):
    client = FakeClient(input_tokens=10_000)
    h = harness(connect, client, run_cap_usd=Decimal("0.10"), daily_cap_usd=Decimal("5"))
    h.call(REQUEST)
    # 10,000 counted pads to 11,200 in; at $4/M x 1.1 that's $0.04928, leaving $0.05072,
    # which buys 2,305 output tokens at $20/M x 1.1.
    assert client.messages.sent[0]["max_tokens"] == 2305


def test_a_small_request_is_not_refused_for_being_small(connect, conn):
    client = FakeClient()
    harness(connect, client).call({**REQUEST, "stop_sequences": ["END"], "max_tokens": 300})
    assert client.messages.sent[0]["max_tokens"] == 300


def test_the_daily_cap_counts_todays_entries_not_yesterdays(connect, conn):
    from vending import ledger

    ledger.record_tokens(conn, "3.99", "claude-opus-5-5", 1, 1, "earlier today", at=datetime(2026, 11, 10, 6, 0, tzinfo=timezone.utc))
    ledger.record_tokens(conn, "50", "claude-opus-5-5", 1, 1, "yesterday", at=datetime(2026, 11, 10, 4, 59, tzinfo=timezone.utc))
    assert spent_since(conn, day_start(NOW)) == Decimal("3.99")
    client = FakeClient()
    with pytest.raises(CapReached, match="Nothing was spent"):
        harness(connect, client, daily_cap_usd=Decimal("4.00")).call(REQUEST)
    assert client.messages.sent == []


def test_caps_come_from_config(connect, conn):
    config.set_value(conn, "model_run_cap_cents", 250, "test")
    config.set_value(conn, "model_daily_cap_cents", 900, "test")
    h = harness(connect, FakeClient())
    assert (h.run_cap, h.daily_cap) == (Decimal("2.5"), Decimal(9))


def test_cap_settings_are_checked(conn):
    with pytest.raises(ValueError, match="whole number of cents"):
        config.set_value(conn, "model_daily_cap_cents", 4.00, "test")


def test_a_refused_request_is_not_recorded(connect, conn):
    client = FakeClient(error=_http_error(anthropic.BadRequestError, 400))
    with pytest.raises(HarnessError, match="not billed"):
        harness(connect, client).call(REQUEST)
    assert tokens_entries(conn) == []


@pytest.mark.parametrize("error", [
    _http_error(anthropic.InternalServerError, 500),
    anthropic.APITimeoutError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")),
])
def test_a_call_that_may_have_run_is_recorded_at_its_worst_case(connect, conn, error):
    client = FakeClient(error=error)
    h = harness(connect, client, run_cap_usd=Decimal("1"), daily_cap_usd=Decimal("4"))
    with pytest.raises(HarnessError, match="worst case"):
        h.call(REQUEST)
    (amount, detail, reference, _), = tokens_entries(conn)
    # 1,300 padded in x $4 + 16,000 out x $20, per million, x 1.1 = $0.35772
    assert amount == Decimal("-35.772") and "estimate" in reference and h.spent == Decimal("0.35772")


def _failing_on(connect, n):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == n:     # 1: config, 2: the estimate, 3: the correction
            raise psycopg.OperationalError("database went away")
        return connect()
    return flaky


def test_the_estimate_is_in_the_ledger_while_the_call_runs(connect, conn):
    seen = []
    client = FakeClient(during=lambda: seen.append(spent_since(conn, day_start(NOW))))
    harness(connect, client).call(REQUEST)
    assert seen == [worst_case_usd("claude-opus-5-5", 1000, 16000, PRICES, caches=None)]


def test_if_the_correction_fails_the_response_comes_back_and_the_estimate_stays(connect, conn, caplog):
    client = FakeClient()
    detailed = harness(_failing_on(connect, 3), client).call_detailed(REQUEST)
    assert detailed.response.id == "msg_1" and not detailed.recorded
    assert "estimate stays" in caplog.text
    (amount, _, reference, _), = tokens_entries(conn)
    assert amount < Decimal("-1.4") and reference == "live pending"     # over, never under


def test_if_the_estimate_cant_be_written_nothing_is_sent(connect, conn):
    client = FakeClient()
    with pytest.raises(HarnessError, match="nothing was sent"):
        harness(_failing_on(connect, 2), client).call(REQUEST)
    assert client.messages.sent == []


def test_a_crash_after_sending_leaves_the_estimate(connect, conn):
    client = FakeClient(error=RuntimeError("process dying"))
    with pytest.raises(RuntimeError):
        harness(connect, client).call(REQUEST)
    (amount, _, reference, _), = tokens_entries(conn)
    assert amount < 0 and reference == "live pending"


def test_a_fallback_answer_is_priced_at_the_model_that_answered(connect, conn):
    answer = SimpleNamespace(id="msg_f", model="claude-sonnet-5-5", usage=usage(i=1000, o=500))
    harness(connect, FakeClient(answer=answer)).call(REQUEST)
    # Sonnet: 1000 x $2 + 500 x $10, per million = $0.007
    (amount, detail, _, _), = tokens_entries(conn)
    assert amount == Decimal("-0.7") and detail.startswith("claude-sonnet-5-5")


@pytest.mark.parametrize("answer_usage", [
    usage(),                                                                  # a proxy that zeroed usage
    SimpleNamespace(**vars(usage(i=1000, o=500)), service_tier="priority"),   # capacity we can't price
])
def test_an_answer_we_cant_price_keeps_its_worst_case(connect, conn, answer_usage):
    answer = SimpleNamespace(id="msg_z", model="claude-opus-5-5", usage=answer_usage)
    harness(connect, FakeClient(answer=answer)).call(REQUEST)
    (amount, _, reference, _), = tokens_entries(conn)
    assert amount == -100 * worst_case_usd("claude-opus-5-5", 1000, 16000, PRICES, caches=None)
    assert "kept at worst case" in reference


def test_standard_capacity_is_forced_and_max_tokens_kept_sendable(connect, conn):
    client = FakeClient()
    harness(connect, client, run_cap_usd=Decimal(5)).call({**REQUEST, "service_tier": None, "max_tokens": 64000})
    sent = client.messages.sent[0]
    assert sent["service_tier"] == "standard_only" and sent["max_tokens"] == 21_333


def test_purpose_must_be_live_or_test(connect):
    with pytest.raises(HarnessError, match="purpose"):
        Harness(FakeClient(), connect, purpose="demo")


# --- model_calls: every call, live or test --------------------------------------------

def model_calls(conn):
    return conn.execute("select purpose, label, run_id, model, answered_by, effort, key_env, status, stop_reason,"
                        " input_tokens, output_tokens, cost_usd, worst_case_usd, ledger_entry, seconds, host"
                        " from model_calls order by id").fetchall()


def writes_logged(conn):
    return conn.execute("select count(*) from action_log where kind = 'write'").fetchone()[0]


def test_a_live_call_is_one_model_call_row_tied_to_its_ledger_entry(connect, conn):
    answer = SimpleNamespace(id="msg_9", model="claude-opus-5-5", usage=usage(i=1000, o=500), stop_reason="end_turn")
    h = harness(connect, FakeClient(answer=answer))
    call = h.call_detailed({**REQUEST, "output_config": {"effort": "high"}}, label="week 3")
    (row,) = model_calls(conn)
    purpose, label, run_id, model, answered_by, effort, key_env, status, stop, i, o, cost, worst, entry, secs, host = row
    assert (purpose, label, run_id, model, answered_by, effort) == ("live", "week 3", h.run_id, "claude-opus-5-5",
                                                                     "claude-opus-5-5", "high")
    assert (key_env, status, stop, i, o) == ("VENDING_ANTHROPIC_API_KEY", "ok", "end_turn", 1000, 500)
    assert cost == Decimal("0.014") and worst > cost and secs is not None and host
    ledger_id = conn.execute("select id from ledger_entries where kind = 'tokens'").fetchone()[0]
    assert entry == ledger_id == call.entry and call.call_id is not None


def test_a_test_call_is_recorded_but_never_reaches_the_books(connect, conn):
    h = harness(connect, FakeClient(), purpose="test", key_env="VENDING_PERSONAL_ANTHROPIC_KEY")
    h.call(REQUEST, label="sweep haiku@low slow-seller")
    assert tokens_entries(conn) == [] and writes_logged(conn) == 0    # not the $600; not an agent write
    (row,) = model_calls(conn)
    assert row[0] == "test" and row[6] == "VENDING_PERSONAL_ANTHROPIC_KEY" and row[7] == "ok"
    assert row[11] == Decimal("0.014") and row[13] is None


def test_each_purpose_has_its_own_daily_cap(connect, conn):
    # $3.99 of test spend today leaves the test cap nearly full, but doesn't touch the live one.
    harness(connect, FakeClient(input_tokens=1000), purpose="test", run_cap_usd=Decimal(10),
            daily_cap_usd=Decimal(10)).call(REQUEST)
    conn.execute("update model_calls set cost_usd = 3.99")
    client = FakeClient()
    with pytest.raises(CapReached, match="test cap"):
        harness(connect, client, purpose="test", daily_cap_usd=Decimal("4.00")).call(REQUEST)
    harness(connect, client, purpose="live", daily_cap_usd=Decimal("4.00")).call(REQUEST)
    assert len(client.messages.sent) == 1


def test_the_test_daily_cap_comes_from_its_own_setting(connect, conn):
    config.set_value(conn, "model_test_daily_cap_cents", 2500, "test")
    config.set_value(conn, "model_daily_cap_cents", 400, "test")
    assert harness(connect, FakeClient(), purpose="test").daily_cap == Decimal(25)
    assert harness(connect, FakeClient(), purpose="live").daily_cap == Decimal(4)


def test_the_test_daily_cap_defaults_to_twenty_dollars(connect, conn):
    assert harness(connect, FakeClient(), purpose="test").daily_cap == Decimal(20)


def test_a_refused_call_stays_visible_at_no_cost(connect, conn):
    client = FakeClient(error=_http_error(anthropic.BadRequestError, 400))
    with pytest.raises(HarnessError, match="not billed"):
        harness(connect, client).call(REQUEST)
    (row,) = model_calls(conn)
    assert row[7] == "refused" and row[11] == 0 and row[13] is None and tokens_entries(conn) == []


def test_a_call_that_may_have_run_is_an_error_row_at_its_worst_case(connect, conn):
    client = FakeClient(error=_http_error(anthropic.InternalServerError, 500))
    with pytest.raises(HarnessError, match="worst case"):
        harness(connect, client, purpose="test").call(REQUEST)
    (row,) = model_calls(conn)
    assert row[7] == "error" and row[11] == row[12] == Decimal("0.35772")


def test_the_row_is_pending_at_its_worst_case_while_the_call_runs(connect, conn):
    seen = []
    client = FakeClient(during=lambda: seen.append(conn.execute(
        "select status, cost_usd from model_calls").fetchall()))
    harness(connect, client, purpose="test").call(REQUEST)
    assert seen == [[("pending", worst_case_usd("claude-opus-5-5", 1000, 16000, PRICES, caches=None))]]


def test_one_harness_is_one_run(connect, conn):
    h = harness(connect, FakeClient(), purpose="test", run_cap_usd=Decimal(2))
    h.call(REQUEST)
    h.call(REQUEST)
    other = harness(connect, FakeClient(), purpose="test")
    other.call(REQUEST)
    runs = [r[2] for r in model_calls(conn)]
    assert runs[0] == runs[1] == h.run_id and runs[2] == other.run_id != h.run_id


def test_a_key_named_like_claude_codes_is_refused(connect):
    with pytest.raises(HarnessError, match="ANTHROPIC_API_KEY"):
        Harness(FakeClient(), connect, purpose="test", key_env="ANTHROPIC_API_KEY")
