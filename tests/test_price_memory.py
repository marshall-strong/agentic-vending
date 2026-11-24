"""The price memory (Kevin, 23 Nov: weekly price checks are essential spend, so make them cheap): every lookup is
logged, a store that didn't carry a drink is skipped for a while, a known listing is re-checked with one search, a
recent receipt needs no lookup, a cheap model's find gets one check by a better model before it is stored, a
database timeout doesn't end a run, and Amazon links are stored plain. The Anthropic client is faked, as in
`test_price_search`: nothing calls the API or the web."""

import json
from datetime import timedelta
from decimal import Decimal

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("anthropic")

from vending import config, price_search, sourcing  # noqa: E402
from tests.test_price_search import (BEFORE, LIVE, PAGES, SKU, URL, Client, at_store, command,  # noqa: E402,F401
                                     connect, failing, found, lookup, picked_plan, row, searched, searcher,
                                     set_row)
from tests.test_price_compare import HAIKU, SONNET, domains, offer  # noqa: E402

ASIN = "B07X1Y2Z3A"
PLAIN = f"https://www.amazon.com/dp/{ASIN}"


def lookups(conn):
    return conn.execute(
        "select sku, store, kind, asked_min, asked_max, units_per_pack, price_cents, url, stock, outcome, model,"
        " cost_usd, no_store, by, at from price_lookups order by id").fetchall()


def uses(client):
    return [b["tools"][0]["max_uses"] for b in client.sent]


def text(body):
    return body["messages"][0]["content"]


# --- 1. Every lookup is logged ----------------------------------------------------------------

def test_every_lookup_is_logged_with_its_outcome_model_and_cost(connect):
    s = searcher(connect, Client(searched(), failing("Walmart")))
    lookup(s)
    lookup(s, store="Walmart")
    with connect() as conn:
        logged = lookups(conn)
    assert [r[:11] for r in logged] == [
        (SKU, "Amazon", "search", 24, 24, 24, 2748, PLAIN, "in_stock", "found", "claude-opus-5-5"),
        (SKU, "Walmart", "search", 24, 24, None, None, None, None, "not_found", "claude-opus-5-5")]
    assert [r[11] for r in logged] == [Decimal("0.148"), Decimal("0.148")]
    assert {(r[12], r[13], r[14]) for r in logged} == {(False, price_search.AGENT_BY, LIVE)}


def test_a_no_store_run_logs_its_lookups_marked_as_such(connect, tmp_path):
    client = Client(offer("Amazon", 24, "27.48", model=HAIKU), offer("Walmart", 12, "12.00", model=HAIKU),
                    failing("Costco"), offer("BJs", 30, "15.00", stock="out_of_stock", model=HAIKU))
    assert command(connect, client, SKU, "--pack", "24", "--compare", "--no-store", "--model", HAIKU,
                   "--json", str(tmp_path / "x.json"), "--by", "Kevin") == 0
    with connect() as conn:
        logged = lookups(conn)
    assert [(r[1], r[9], r[12], r[13]) for r in logged] == [
        ("Amazon", "found", True, "Kevin"), ("Walmart", "found", True, "Kevin"),
        ("Costco", "not_found", True, "Kevin"), ("BJs", "out_of_stock", True, "Kevin")]
    assert [(r[3], r[4]) for r in logged] == [(2, 60)] * 4         # a comparison asks for any usable pack
    assert logged[3][6:9] == (1500, PAGES["BJs"], "out_of_stock")   # what the page said, though nothing is usable


def test_history_shows_a_drinks_recent_lookups(connect, capsys):
    s = searcher(connect, Client(searched(), failing("Walmart")))
    lookup(s)
    lookup(s, store="Walmart")
    assert command(connect, Client(), SKU, "--history") == 0
    out = capsys.readouterr().out
    assert "Amazon" in out and "found" in out and "$27.48 for 24" in out and PLAIN in out
    assert "Walmart" in out and "not_found" in out and "claude-opus-5-5" in out
    assert out.index("Walmart") < out.index("Amazon")             # newest first


def test_a_lookup_still_answers_when_its_log_cant_be_written(connect, monkeypatch):
    def broken(*_, **__):
        raise psycopg.OperationalError("connection timeout expired")

    monkeypatch.setattr(price_search, "remember", broken)
    reply, failed = lookup(searcher(connect, Client(searched())))
    assert not failed and reply["price"] == "27.48"


# --- 2. Known dead ends are skipped -------------------------------------------------------------

def test_a_store_that_didnt_carry_the_drink_is_skipped_for_21_days(connect, capsys):
    client = Client(offer("Amazon", 24, "27.48"), failing("Walmart"), failing("Costco"), failing("BJs"))
    assert command(connect, client, SKU, "--pack", "24", "--compare", "--by", "Kevin", when=BEFORE) == 0
    capsys.readouterr()
    later = BEFORE + timedelta(days=20)          # the Amazon row is stale by now
    client = Client(offer("Amazon", 24, "27.48"))
    assert command(connect, client, SKU, "--pack", "24", "--compare", "--by", "Kevin", when=later) == 0
    assert domains(client) == ["amazon.com"]
    out = capsys.readouterr().out
    assert "Walmart: skipped: not carried, checked 2026-12-02" in out and "BJs: skipped: not carried" in out
    # After 21 days the store is searched again.
    client = Client(offer("Amazon", 24, "27.48"), failing("Walmart"), failing("Costco"), failing("BJs"))
    command(connect, client, SKU, "--pack", "24", "--compare", "--by", "Kevin", "--max-age", "1",
            when=BEFORE + timedelta(days=23))
    assert len(client.sent) == 4


def test_full_ignores_the_dead_ends_and_the_setting_can_turn_them_off(connect):
    client = Client(offer("Amazon", 24, "27.48"), failing("Walmart"), failing("Costco"), failing("BJs"))
    command(connect, client, SKU, "--pack", "24", "--compare", "--by", "Kevin", when=BEFORE)
    later = BEFORE + timedelta(days=9)         # a Friday: Amazon's cutoff for the next visit is days away
    client = Client(offer("Amazon", 24, "27.48"), failing("Walmart"), failing("Costco"), failing("BJs"))
    command(connect, client, SKU, "--pack", "24", "--compare", "--full", "--by", "Kevin", when=later)
    assert domains(client) == ["amazon.com", "walmart.com", "costco.com", "bjs.com"]
    with connect() as conn:
        config.set_value(conn, "price_dead_end_days", 0, "test")
    client = Client(offer("Amazon", 24, "27.48"), failing("Walmart"), failing("Costco"), failing("BJs"))
    command(connect, client, SKU, "--pack", "24", "--compare", "--by", "Kevin", when=later + timedelta(days=8))
    assert len(client.sent) == 4
    assert config.KNOWN["price_dead_end_days"]


def test_a_dead_end_covers_only_the_packs_it_asked_for_and_a_later_find_clears_it(connect):
    s = searcher(connect, Client(failing("Amazon")))
    lookup(s, units_per_pack=24)                    # no 24-pack at Amazon: says nothing about a 12-pack
    client = Client(searched(report=found(pack=12, price="13.74")))
    reply, failed = lookup(searcher(connect, client), units_per_pack=12)
    assert not failed and len(client.sent) == 1
    idle = Client()
    reply, failed = lookup(searcher(connect, idle), units_per_pack=24)
    assert failed and reply["refused"] == "not_carried" and idle.sent == []
    assert "not carried" in price_search.REASONS["not_carried"].lower()
    # A later find of a 24-pack there (a person's comparison, say) clears it.
    with connect() as conn:
        price_search.remember(conn, sku=SKU, store="Amazon", kind="search", asked=(2, 60), outcome="found",
                              model=HAIKU, cost=Decimal(0), by="Kevin", no_store=False, at=LIVE,
                              seen={"units": 24, "price": 2748, "url": PLAIN, "stock": "in_stock"})
    client = Client(searched())
    reply, failed = lookup(searcher(connect, client, when=LIVE + timedelta(minutes=1)), units_per_pack=24)
    assert len(client.sent) == 1


def test_the_fallback_skips_a_known_dead_end(connect, capsys):
    lookup(searcher(connect, Client(failing("Walmart"))), store="Walmart")
    client = Client(failing("Amazon"), at_store("Costco"))
    assert command(connect, client, SKU, "--store", "Amazon", "--pack", "24", "--by", "Kevin") == 0
    assert domains(client) == ["amazon.com", "costco.com"]
    assert "Walmart: skipped: not carried, checked 2026-12-04" in capsys.readouterr().out


# --- 3. A known listing is re-checked with one search ------------------------------------------

def test_a_known_listing_is_re_checked_with_one_search_on_its_link(connect):
    with connect() as conn:
        set_row(conn, checked=LIVE - timedelta(days=10), price=2599, most=3000)
    client = Client(searched(urls=(URL,), searches=1, report=found(price="26.49")))
    reply, failed = lookup(searcher(connect, client))
    assert not failed and uses(client) == [1]
    assert PLAIN in text(client.sent[0]) and ASIN in text(client.sent[0])
    with connect() as conn:
        assert row(conn).expected_cents == 2649 and lookups(conn)[0][2] == "listing"


def test_a_failed_re_check_falls_back_to_the_open_search(connect):
    with connect() as conn:
        set_row(conn, checked=LIVE - timedelta(days=10), price=2599, most=3000)
    client = Client(failing("Amazon"), searched(report=found(price="26.49")))
    reply, failed = lookup(searcher(connect, client))
    assert not failed and uses(client) == [1, 2] and PLAIN not in text(client.sent[1])
    with connect() as conn:
        assert [(r[2], r[9]) for r in lookups(conn)] == [("listing", "not_found"), ("search", "found")]
        assert row(conn).expected_cents == 2649


def test_compare_re_checks_a_known_listing_and_searches_the_rest(connect):
    with connect() as conn:
        set_row(conn, checked=BEFORE - timedelta(days=10), price=2599, most=3000)
    client = Client(searched(report=found(price="26.49"), searches=1), offer("Walmart", 12, "12.00"),
                    failing("Costco"), failing("BJs"))
    assert command(connect, client, SKU, "--pack", "24", "--compare", "--by", "Kevin", when=BEFORE) == 0
    assert uses(client) == [1, 2, 2, 2] and PLAIN in text(client.sent[0])
    with connect() as conn:
        assert (row(conn).store, row(conn, "alternate").store, row(conn, "alternate").expected_cents) == \
            ("Walmart", "Amazon", 2649)


# --- 4. Receipts first ------------------------------------------------------------------------

def test_a_drink_priced_by_a_recent_receipt_needs_no_lookup(connect, capsys):
    with connect() as conn:
        picked_plan(conn)
        for sku, rank, store, by, age in (("COKE20", "primary", "Costco", "Kevin", 1),
                                          ("PICKLE", "primary", "Costco", sourcing.RECEIPT_BY, 2),
                                          ("PICKLE", "alternate", "Walmart", "Kevin", 9)):       # stale
            sourcing.set_option(conn, sourcing.Option(
                sku=sku, rank=rank, store=store, units_per_pack=24, expected_cents=1800, max_cents=2100, url=None,
                pack_description=None, checked_at=LIVE - timedelta(days=age), checked_by=by), now=LIVE)
    client = Client()
    assert command(connect, client, "--plan", "picked", "--by", "Kevin", "--max-age", "72") == 0
    assert client.sent == []
    out = capsys.readouterr().out
    assert "PICKLE: priced from a receipt on 2026-12-02; no lookup" in out
    # Older than the max age: looked up as before.
    assert command(connect, Client(), "--plan", "picked", "--by", "Kevin", "--max-age", "24", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "  PICKLE: " in out and "priced from a receipt" not in out


# --- 5. A better model checks a cheap model's find ----------------------------------------------

def cheap(conn):
    config.set_value(conn, "price_search_model", HAIKU, "test")


def test_a_cheap_find_far_from_the_stored_price_is_checked_and_the_checks_price_stored(connect):
    with connect() as conn:
        cheap(conn)
        set_row(conn, checked=LIVE - timedelta(days=10), price=2599, most=3000, url=None, store="Walmart")
    client = Client(at_store("Walmart", model=HAIKU, report={"price": "27.48"}),      # 5.7% above $25.99
                    at_store("Walmart", model=SONNET, searches=1, report={"price": "27.60"}))   # within 2%
    reply, failed = lookup(searcher(connect, client), store="Walmart")
    assert not failed and [b["model"] for b in client.sent] == [HAIKU, SONNET] and uses(client) == [2, 1]
    assert PAGES["Walmart"] in text(client.sent[1])
    with connect() as conn:
        assert row(conn).expected_cents == 2760
        assert [(r[2], r[9], r[10]) for r in lookups(conn)] == [("search", "found", HAIKU),
                                                               ("check", "found", SONNET)]


def test_a_check_that_disagrees_stores_nothing_and_logs_check_disagrees(connect):
    with connect() as conn:
        cheap(conn)
        set_row(conn, checked=LIVE - timedelta(days=10), price=2599, most=3000, url=None, store="Walmart")
    client = Client(at_store("Walmart", model=HAIKU, report={"price": "21.48"}),
                    at_store("Walmart", model=SONNET, searches=1, report={"price": "25.99"}))
    reply, failed = lookup(searcher(connect, client), store="Walmart")
    assert failed and reply["refused"] == "check_disagrees"
    with connect() as conn:
        assert (row(conn).expected_cents, row(conn).checked_by) == (2599, "Kevin")
        assert [(r[2], r[9]) for r in lookups(conn)] == [("search", "found"), ("check", "check_disagrees")]
        assert conn.execute("select count(*) from action_log where tool = %s and detail like 'check_disagrees%%'",
                            (price_search.TOOL_NAME,)).fetchone()[0] == 1
    assert "check_disagrees" in price_search.FALLBACK_ON


def test_a_cheap_find_close_to_the_stored_price_needs_no_check(connect):
    with connect() as conn:
        cheap(conn)
        set_row(conn, checked=LIVE - timedelta(days=10), price=2599, most=3000, url=None, store="Walmart")
    client = Client(at_store("Walmart", model=HAIKU, report={"price": "26.99"}))      # 3.8%
    reply, failed = lookup(searcher(connect, client), store="Walmart")
    assert not failed and len(client.sent) == 1
    with connect() as conn:
        config.set_value(conn, "price_check_percent", 3, "test")
        assert config.get_value(conn, "price_check_model", None) is None             # default: Sonnet 5.5
    assert price_search.DEFAULT_CHECK_MODEL == SONNET and price_search.DEFAULT_CHECK_PERCENT == 5


def test_a_first_price_from_a_cheap_model_is_checked(connect):
    with connect() as conn:
        cheap(conn)
    client = Client(searched(model=HAIKU), searched(model=SONNET, searches=1))
    reply, failed = lookup(searcher(connect, client))
    assert not failed and [b["model"] for b in client.sent] == [HAIKU, SONNET]
    assert PLAIN in text(client.sent[1]) and uses(client) == [2, 1]


@pytest.mark.parametrize("model", ["claude-opus-5-5", SONNET])
def test_no_check_when_the_lookup_model_is_the_check_model_or_dearer(connect, model):
    with connect() as conn:
        config.set_value(conn, "price_search_model", model, "test")
    client = Client(searched(model=model))
    reply, failed = lookup(searcher(connect, client))
    assert not failed and len(client.sent) == 1


def test_a_price_a_check_confirmed_counts_as_stored_so_it_isnt_checked_again(connect):
    # Store what each check learns: Costco's 24-pack loses the comparison, so it gets no sourcing row, but the
    # check confirmed $30.00. Next time a find within 5% of that needs no check.
    with connect() as conn:
        cheap(conn)
    client = Client(offer("Amazon", 24, "27.48", model=HAIKU), searched(model=SONNET, searches=1),
                    offer("Walmart", 12, "12.00", model=HAIKU),
                    at_store("Walmart", model=SONNET, searches=1, report={"pack": 12, "price": "12.00"}),
                    offer("Costco", 24, "30.00", model=HAIKU),
                    at_store("Costco", model=SONNET, searches=1, report={"pack": 24, "price": "30.00"}),
                    failing("BJs"))
    assert command(connect, client, SKU, "--pack", "24", "--compare", "--by", "Kevin", when=BEFORE) == 0
    assert len(client.sent) == 7
    later = BEFORE + timedelta(days=9)          # every row stale; BJs is a known dead end
    client = Client(offer("Amazon", 24, "27.48", model=HAIKU), offer("Walmart", 12, "12.00", model=HAIKU),
                    offer("Costco", 24, "30.20", model=HAIKU))
    assert command(connect, client, SKU, "--pack", "24", "--compare", "--by", "Kevin", when=later) == 0
    assert [b["model"] for b in client.sent] == [HAIKU] * 3
    # An unchecked cheap find is not a confirmed price: a Haiku price alone never vouches for the next one.
    with connect() as conn:
        assert price_search.confirmed_price(conn, SKU, "Costco", 24, PAGES["Costco"], LIVE,
                                            lambda model: model != HAIKU) == 3000


def test_compare_checks_a_cheap_find_and_the_benchmark_does_not(connect, tmp_path):
    with connect() as conn:
        cheap(conn)
    client = Client(offer("Amazon", 24, "27.48", model=HAIKU), searched(model=SONNET, searches=1),
                    failing("Walmart"), failing("Costco"), failing("BJs"))
    assert command(connect, client, SKU, "--pack", "24", "--compare", "--by", "Kevin", when=BEFORE) == 0
    assert [b["model"] for b in client.sent] == [HAIKU, SONNET, HAIKU, HAIKU, HAIKU]
    with connect() as conn:
        assert row(conn).expected_cents == 2748
    client = Client(offer("Amazon", 24, "27.48", model=HAIKU), failing("Walmart"), failing("Costco"),
                    failing("BJs"))
    command(connect, client, SKU, "--pack", "24", "--compare", "--no-store", "--full", "--by", "Kevin",
            "--json", str(tmp_path / "x.json"))
    assert len(client.sent) == 4


def test_compare_stores_nothing_from_a_store_whose_check_disagrees(connect, capsys):
    with connect() as conn:
        cheap(conn)
    client = Client(offer("Amazon", 24, "20.00", model=HAIKU), searched(model=SONNET, searches=1),   # 27.48
                    offer("Walmart", 12, "13.00", model=HAIKU),
                    at_store("Walmart", model=SONNET, searches=1, report={"pack": 12, "price": "13.10"}),
                    failing("Costco"), failing("BJs"))
    assert command(connect, client, SKU, "--pack", "24", "--compare", "--by", "Kevin", when=BEFORE) == 0
    with connect() as conn:
        assert (row(conn).store, row(conn).expected_cents, row(conn, "alternate")) == ("Walmart", 1310, None)
    assert "Amazon: check_disagrees" in capsys.readouterr().out


# --- 6. A database timeout doesn't end a run -----------------------------------------------------

def test_a_failed_connect_is_retried(connect, monkeypatch):
    slept = []
    monkeypatch.setattr(price_search, "_sleep", slept.append)
    tries = []

    def flaky():
        tries.append(1)
        if len(tries) < 3:
            raise psycopg.OperationalError("connection timeout expired")
        return connect()

    with price_search.retrying(flaky)() as conn:
        assert conn.execute("select 1").fetchone() == (1,)
    assert len(tries) == 3 and len(slept) == 2 and slept[0] < slept[1]
    tries.clear()

    def down():
        tries.append(1)
        raise psycopg.OperationalError("connection timeout expired")

    with pytest.raises(psycopg.OperationalError):
        price_search.retrying(down)()
    assert len(tries) == price_search.CONNECT_TRIES == 3


def test_a_drink_that_cant_reach_the_database_is_skipped_and_the_json_saved(connect, monkeypatch, tmp_path,
                                                                             capsys):
    with connect() as conn:
        picked_plan(conn)
    real = price_search.PriceSearch.compare

    def compare(self, drink, **kw):
        if drink.sku == "COKE20":
            raise psycopg.OperationalError("connection timeout expired")
        return real(self, drink, **kw)

    monkeypatch.setattr(price_search.PriceSearch, "compare", compare)
    client = Client(offer("Amazon", 24, "18.00"), failing("Walmart"), failing("Costco"), failing("BJs"))
    out_file = tmp_path / "run.json"
    assert command(connect, client, "--plan", "picked", "--by", "Kevin", "--json", str(out_file),
                   when=BEFORE) == 1
    saved = json.loads(out_file.read_text(encoding="utf-8"))
    assert [(d["sku"], d["code"]) for d in saved["drinks"]] == [("COKE20", "db_error"), ("PICKLE", None)]
    assert "COKE20: the database failed" in capsys.readouterr().out


def test_the_json_is_saved_on_any_error(connect, monkeypatch, tmp_path):
    with connect() as conn:
        picked_plan(conn)
    real = price_search.PriceSearch.compare

    def compare(self, drink, **kw):
        if drink.sku == "PICKLE":
            raise RuntimeError("something else broke")
        return real(self, drink, **kw)

    monkeypatch.setattr(price_search.PriceSearch, "compare", compare)
    client = Client(offer("Amazon", 12, "12.37"), failing("Walmart"), failing("Costco"), failing("BJs"))
    out_file = tmp_path / "run.json"
    with pytest.raises(RuntimeError):
        command(connect, client, "--plan", "picked", "--by", "Kevin", "--json", str(out_file), when=BEFORE)
    assert [d["sku"] for d in json.loads(out_file.read_text(encoding="utf-8"))["drinks"]] == ["COKE20"]


def test_the_command_retries_its_connections(connect, monkeypatch):
    monkeypatch.setattr(price_search, "_sleep", lambda _: None)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise psycopg.OperationalError("connection timeout expired")
        return connect()

    assert command(flaky, Client(searched()), SKU, "--pack", "24", "--by", "Kevin", "--no-fallback") == 0


# --- 7. Amazon links are stored plain --------------------------------------------------------------

TAGGED = "https://www.amazon.com/dp/B09RGKQW2L/ref=nosim?tag=happywishlist-20"


def test_an_amazon_link_is_stored_plain_without_its_tag_or_ref(connect):
    client = Client(searched(urls=(TAGGED,), report=found(url=TAGGED)))
    reply, failed = lookup(searcher(connect, client))
    assert not failed and reply["url"] == "https://www.amazon.com/dp/B09RGKQW2L"
    with connect() as conn:
        assert row(conn).url == "https://www.amazon.com/dp/B09RGKQW2L"
    # The cited link must still be one the search returned, exactly.
    cited = found(url="https://www.amazon.com/dp/B09RGKQW2L")
    client = Client(searched(urls=(TAGGED,), report=cited), searched(urls=(TAGGED,), report=cited))
    reply, failed = lookup(searcher(connect, client, when=LIVE + timedelta(days=9)))
    assert failed and reply["refused"] == "unsourced_url" and uses(client) == [1, 2]     # re-check, then search


@pytest.mark.parametrize("given,plain", [
    (URL, PLAIN),
    (TAGGED, "https://www.amazon.com/dp/B09RGKQW2L"),
    ("https://us.amazon.com/gp/product/b09rgkqw2l?th=1", "https://www.amazon.com/dp/B09RGKQW2L"),
    ("https://www.amazon.com/dp/B000TEST", "https://www.amazon.com/dp/B000TEST"),      # no ASIN: as it is
])
def test_people_and_imports_store_plain_amazon_links(connect, given, plain):
    assert sourcing.plain_link("Amazon", given) == plain
    assert sourcing.plain_link("Walmart", PAGES["Walmart"] + "?x=1") == PAGES["Walmart"] + "?x=1"
    with connect() as conn:
        set_row(conn, checked=LIVE, url=given)
        assert row(conn).url == plain


def test_the_migration_cleans_amazon_links_already_stored(connect):
    from vending import db

    with connect() as conn:
        set_row(conn, checked=LIVE)
        conn.execute("update sourcing_options set url = %s where sku = %s", (TAGGED, SKU))
        conn.execute((db.MIGRATIONS / "033_plain_amazon_links.sql").read_text(encoding="utf-8"))
        assert row(conn).url == "https://www.amazon.com/dp/B09RGKQW2L"
        logged = conn.execute("select arguments ->> 'old_url' from action_log where tool = 'sourcing.plain_link'"
                              ).fetchall()
        assert logged == [(TAGGED,)]
