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
    with pytest.raises(HarnessError, match="fallback bills a second model"):
        _refuse_unpriced({"fallbacks": "default"})
    with pytest.raises(HarnessError, match="beta features"):
        _refuse_unpriced({"betas": ["server-side-fallback-2026-07-01"]})
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
    return Harness(client, connect, purpose=kw.pop("purpose", "test"), **kw)


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
                                     "test week 3 msg_1", "harness")]
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


def test_without_a_clock_the_day_and_the_stamp_come_from_the_database(connect, conn, monkeypatch):
    # The host clock says 10:00 Eastern on the 10th; the database says 23:30 on the 9th.
    # The day, and so the daily cap, follow the database, and the entry is stamped by it.
    from vending import ledger

    db_time = datetime(2026, 11, 10, 4, 30, tzinfo=timezone.utc)     # 23:30 Eastern, the 9th
    monkeypatch.setattr(ledger, "db_now", lambda conn: db_time)
    ledger.record_tokens(conn, "3.99", "claude-opus-5-5", 1, 1, "the 9th",
                         at=datetime(2026, 11, 9, 20, 0, tzinfo=timezone.utc))
    client = FakeClient()
    with pytest.raises(CapReached):
        Harness(client, connect, purpose="test", daily_cap_usd=Decimal("4.00")).call(REQUEST)
    assert client.messages.sent == []

    Harness(client, connect, purpose="test", daily_cap_usd=Decimal("10")).call(REQUEST)
    stamped, = conn.execute("select at from ledger_entries where reference like 'test msg_%'").fetchone()
    assert stamped == db_time


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
    assert amount < Decimal("-1.4") and reference == "test pending"     # over, never under


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
    assert amount < 0 and reference == "test pending"


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
