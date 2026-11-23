"""The agent's capped price lookup (#202 part 2): one web search request per lookup, through the harness,
limited to the four stores the worker buys from. The Anthropic client is faked: nothing calls the API or
the web. Rows, spend and logs land in a throwaway schema."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("anthropic")

from vending import config, harness as harness_module, price_search, sourcing  # noqa: E402
from vending.harness import Harness, HarnessError, _refuse_unpriced, load_prices, worst_case_usd  # noqa: E402

PRICES = load_prices()
LIVE = datetime(2026, 12, 4, 15, 0, tzinfo=timezone.utc)        # Friday 10:00 Eastern, after the start
BEFORE = datetime(2026, 12, 2, 15, 0, tzinfo=timezone.utc)      # the day before the start
SKU = "DRPEPPER-20BOT"
URL = "https://www.amazon.com/Dr-Pepper-Soda-20-oz/dp/B07X1Y2Z3A"


# --- A fake client that "searches" ---------------------------------------------------

def searched(*, urls=(URL,), report=None, searches=2, input_tokens=30_000, output_tokens=400):
    """A response as the API sends one after web searches: the query, the results and our report tool's call."""
    content = [SimpleNamespace(type="server_tool_use", id="srv_1", name="web_search",
                               input={"query": "Dr Pepper 20 oz 24 pack amazon"}),
               SimpleNamespace(type="web_search_tool_result", tool_use_id="srv_1",
                               content=[SimpleNamespace(type="web_search_result", url=u, title="Dr Pepper",
                                                        page_age=None, encrypted_content="x") for u in urls])]
    if report is not False:
        content.append(SimpleNamespace(type="tool_use", id="toolu_1", name=price_search.REPORT_TOOL["name"],
                                       input=report or found()))
    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens, cache_read_input_tokens=0,
                            cache_creation_input_tokens=0, cache_creation=None,
                            server_tool_use=SimpleNamespace(web_search_requests=searches))
    return SimpleNamespace(id="msg_search", model="claude-opus-5-5", content=content, stop_reason="tool_use",
                           usage=usage)


def found(price="27.48", pack=24, url=URL, description="24 x 20 oz bottles"):
    return {"found": True, "price": price, "units_per_pack": pack, "pack_description": description, "url": url,
            "note": "listed price on the product page"}


class Client:
    def __init__(self, *responses):
        self.responses, self.sent = list(responses), []
        self.messages = self

    def with_options(self, **_):
        return self

    def count_tokens(self, **_):
        return SimpleNamespace(input_tokens=1000)

    def create(self, **body):
        self.sent.append(body)
        return self.responses.pop(0)


# --- Fixtures ------------------------------------------------------------------------

@pytest.fixture
def connect(schema_url):
    from tests.conftest import open_in

    with open_in(*schema_url) as c:
        config.set_halted(c, False, "test")        # a new database starts halted
        c.execute("insert into catalog_products (sku, name, size, last_response)"
                  " values (%s, 'Dr Pepper', '20 oz bottle', '{}')", (SKU,))
    return lambda: open_in(*schema_url)


def searcher(connect, client, *, when=LIVE, purpose="live", **kw):
    h = Harness(client, connect, purpose=purpose, now=lambda: when, **kw)
    return price_search.PriceSearch(h, connect, model="claude-opus-5-5", now=lambda: when)


def lookup(s, **arguments):
    text, failed = s.lookup({"sku": SKU, "store": "Amazon", **arguments})
    return json.loads(text), failed


def row(conn, rank="primary"):
    return sourcing._row(conn, SKU, rank)


def set_row(conn, *, checked, store="Amazon", pack=24, price=2599, most=3000, url=URL, rank="primary", by="Kevin"):
    sourcing.set_option(conn, sourcing.Option(
        sku=SKU, rank=rank, store=store, units_per_pack=pack, expected_cents=price, max_cents=most, url=url,
        pack_description=f"{pack} x 20 oz bottles", checked_at=checked, checked_by=by), now=checked)


# --- The harness prices a web search (no database) ------------------------------------

def test_the_harness_allows_only_the_basic_web_search_with_a_cap_and_store_domains():
    tool = {"type": price_search.SEARCH_TOOL_TYPE, "name": "web_search", "max_uses": 2,
            "allowed_domains": ["amazon.com"]}
    assert _refuse_unpriced({"tools": [tool]}, PRICES) == 2
    with pytest.raises(HarnessError, match="max_uses"):
        _refuse_unpriced({"tools": [{k: v for k, v in tool.items() if k != "max_uses"}]}, PRICES)
    with pytest.raises(HarnessError, match="max_uses"):
        _refuse_unpriced({"tools": [{**tool, "max_uses": harness_module.MAX_SEARCHES + 1}]}, PRICES)
    with pytest.raises(HarnessError, match="allowed_domains"):
        _refuse_unpriced({"tools": [{k: v for k, v in tool.items() if k != "allowed_domains"}]}, PRICES)
    with pytest.raises(HarnessError, match="web_search_20260209"):     # dynamic filtering runs code: not priced
        _refuse_unpriced({"tools": [{**tool, "type": "web_search_20260209"}]}, PRICES)
    with pytest.raises(HarnessError, match="web_fetch"):
        _refuse_unpriced({"tools": [{"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 1}]}, PRICES)


def test_the_worst_case_adds_each_search_and_the_results_it_could_read():
    # Two searches: the prompt is read up to three times, plus 1 + 2 result allowances, plus $0.01 a search.
    tokens_in = harness_module.padded(1000) * 3 + harness_module.SEARCH_RESULT_TOKENS * 3
    expected = (Decimal(tokens_in) * 4 + 100 * 20) / 1_000_000 * Decimal("1.1") + Decimal("0.02")
    assert worst_case_usd("claude-opus-5-5", 1000, 100, PRICES, caches=None, searches=2) == expected
    assert PRICES.web_search_usd == Decimal("0.01")


# --- Lookups on a database --------------------------------------------------------------

def test_a_drink_with_no_price_gets_a_primary_row_marked_as_found_by_the_agent(connect):
    client = Client(searched())
    reply, failed = lookup(searcher(connect, client), units_per_pack=24)
    assert not failed and reply["recorded"] == "added primary"
    assert reply["price"] == "27.48" and reply["url"] == URL and reply["seen_at"] == LIVE.isoformat()
    tool = client.sent[0]["tools"][0]
    assert tool == {"type": "web_search_20250305", "name": "web_search", "max_uses": 2,
                    "allowed_domains": ["amazon.com"]}
    with connect() as conn:
        o = row(conn)
    assert (o.store, o.units_per_pack, o.expected_cents, o.url) == ("Amazon", 24, 2748, URL)
    assert o.checked_by == price_search.AGENT_BY and o.checked_at == LIVE
    assert o.max_cents == price_search.agent_max(2748) == 3450         # +25%, up to the next 50 cents
    assert URL in o.note and "found by the agent" in o.note


def test_spend_is_recorded_with_the_searches_in_model_calls_and_the_ledger(connect):
    client = Client(searched(searches=2, input_tokens=30_000, output_tokens=400))
    lookup(searcher(connect, client))
    with connect() as conn:
        cost, label, detail, entry = conn.execute(
            "select cost_usd, label, detail, ledger_entry from model_calls").fetchone()
        ledger_cents = conn.execute("select -amount_cents from ledger_entries where id = %s", (entry,)).fetchone()[0]
        logged = conn.execute("select outcome from action_log where tool = %s", (price_search.TOOL_NAME,)).fetchall()
    # 30,000 in at $4/M + 400 out at $20/M + 2 searches at $10 per 1,000
    assert cost == Decimal("0.148")
    assert ledger_cents == Decimal("14.8")
    assert label.startswith(price_search.LABEL) and "2 web searches" in detail
    assert logged == [("done",)]


def test_the_cap_stops_further_searches(connect):
    with connect() as conn:
        config.set_value(conn, "price_search_searches_per_run", 3, "test")
    client = Client(searched(searches=2), searched(searches=1))
    s = searcher(connect, client)
    lookup(s)
    assert client.sent[0]["tools"][0]["max_uses"] == 2
    lookup(s, store="Walmart")
    assert client.sent[1]["tools"][0]["max_uses"] == 1                 # only one search left this run
    reply, failed = lookup(s, store="Costco")
    assert failed and reply["refused"] == "search_cap" and len(client.sent) == 2


def test_the_daily_dollar_cap_stops_a_lookup_before_anything_is_sent(connect):
    with connect() as conn:
        config.set_value(conn, "price_search_daily_cap_cents", 10, "test")     # less than one lookup's worst case
    client = Client(searched())
    reply, failed = lookup(searcher(connect, client))
    assert failed and reply["refused"] == "search_cap" and client.sent == []


def test_a_disallowed_store_is_refused_without_a_search(connect):
    client = Client()
    for store in ("Target", "Interloper", "amazon"):
        reply, failed = lookup(searcher(connect, client), store=store)
        assert failed and reply["refused"] == "disallowed_store"
    assert client.sent == []


def test_an_interloper_or_other_off_store_source_is_refused_and_nothing_stored(connect):
    page = "https://interloper-vending.example/prices"
    client = Client(searched(urls=(page,), report=found(url=page)))
    reply, failed = lookup(searcher(connect, client))
    assert failed and reply["refused"] == "off_store_source"
    with connect() as conn:
        assert row(conn) is None
        assert conn.execute("select count(*) from model_calls where status = 'ok'").fetchone()[0] == 1   # still paid


def test_a_link_the_searches_never_returned_is_refused(connect):
    other = "https://www.amazon.com/dp/B000000000"
    client = Client(searched(urls=(URL,), report=found(url=other)))
    reply, failed = lookup(searcher(connect, client))
    assert failed and reply["refused"] == "unsourced_url"


def test_a_stale_price_is_refreshed_and_keeps_its_max(connect):
    with connect() as conn:
        set_row(conn, checked=LIVE - timedelta(days=10), price=2599, most=3000)
    client = Client(searched(report=found(price="27.48")))
    reply, failed = lookup(searcher(connect, client))
    assert not failed and reply["recorded"] == "refreshed primary"
    with connect() as conn:
        o = row(conn)
    assert (o.expected_cents, o.max_cents, o.checked_at, o.checked_by) == (2748, 3000, LIVE, price_search.AGENT_BY)
    assert not o.is_stale(LIVE)


def test_a_fresh_price_needs_no_search(connect):
    with connect() as conn:
        set_row(conn, checked=LIVE - timedelta(days=2))
    client = Client()
    reply, failed = lookup(searcher(connect, client))
    assert failed and reply["refused"] == "fresh_price" and client.sent == []


def test_a_price_over_the_rows_max_is_not_stored(connect):
    with connect() as conn:
        set_row(conn, checked=LIVE - timedelta(days=10), price=2599, most=2800)
    client = Client(searched(report=found(price="29.99")))
    reply, failed = lookup(searcher(connect, client))
    assert failed and reply["refused"] == "over_max"
    with connect() as conn:
        assert row(conn).expected_cents == 2599


def test_a_new_store_goes_in_the_alternate_and_never_replaces_the_primary(connect):
    with connect() as conn:
        set_row(conn, checked=LIVE - timedelta(days=10), store="Walmart", url=None)
    client = Client(searched())
    reply, failed = lookup(searcher(connect, client))
    assert not failed and reply["recorded"] == "added alternate"
    with connect() as conn:
        assert row(conn).store == "Walmart" and row(conn, "alternate").store == "Amazon"


def test_nothing_found_stores_nothing(connect):
    client = Client(searched(report={"found": False, "price": "", "units_per_pack": 0, "pack_description": "",
                                     "url": "", "note": "no listing for a 20 oz bottle"}))
    reply, failed = lookup(searcher(connect, client))
    assert failed and reply["refused"] == "not_found"
    with connect() as conn:
        assert row(conn) is None


def test_before_the_start_people_price_drinks(connect):
    client = Client()
    reply, failed = lookup(searcher(connect, client, when=BEFORE))
    assert failed and reply["refused"] == "not_live" and client.sent == []
    assert not price_search.offered(live=True, now=BEFORE)
    assert price_search.offered(live=True, now=LIVE) and not price_search.offered(live=False, now=LIVE)


def test_the_halt_switch_stops_a_lookup_before_it_spends(connect):
    with connect() as conn:
        config.set_halted(conn, True, "test")
    client = Client()
    reply, failed = lookup(searcher(connect, client))
    assert failed and reply["refused"] == "halted" and client.sent == []


def test_people_cannot_sign_a_price_as_the_agent():
    with pytest.raises(sourcing.SourcingError, match=price_search.AGENT_BY):
        sourcing._from_fields({"sku": SKU, "store": "Amazon", "units_per_pack": "24", "price": "27.48", "url": URL},
                              price_search.AGENT_BY, LIVE)
