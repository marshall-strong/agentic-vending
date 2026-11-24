"""Price lookups that compare every approved store (Kevin, 23 Nov): the cheapest usable pack per unit becomes the
primary and the next the alternate, `--max-age` re-prices fresh rows, a cheaper model can run the lookups, and a
no-write benchmark mode saves what a model found. The Anthropic client is faked, as in `test_price_search`:
nothing calls the API or the web. Rows, spend and logs land in a throwaway schema."""

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("anthropic")

from vending import config, planner, price_search, sourcing  # noqa: E402
from tests.test_price_search import (BEFORE, LIVE, PAGES, PLAIN, SKU, URL, Client, at_store, command,  # noqa: E402,F401
                                     connect, failing, found, lookup, picked_plan, row, searched, searcher,
                                     set_row)

SUNDAY = datetime(2026, 11, 29, 17, 0, tzinfo=timezone.utc)      # 12:00 ET, the day before the day-one stocking
THURSDAY = datetime(2026, 12, 3, 17, 0, tzinfo=timezone.utc)     # 12:00 ET, the day before a Friday visit
HAIKU, SONNET = "claude-haiku-5-5", "claude-sonnet-5-5"


def domains(client):
    return [b["tools"][0]["allowed_domains"][0] for b in client.sent]


def offer(store, pack, price, *, stock="in_stock", model="claude-opus-5-5"):
    """A search at `store` that finds a `pack`-pack at `price`."""
    return at_store(store, model=model, report={"pack": pack, "price": price, "stock": stock,
                                                "description": f"{pack} x 20 oz bottles"})


# --- 1. Order at the lowest price ------------------------------------------------------------

def test_the_planner_sizes_packs_and_says_which_packs_a_need_can_use():
    assert planner.packs_for(70, 12) == 6 and planner.packs_for(24, 24) == 1
    assert planner.pack_usable(2, 24) and planner.pack_usable(planner.MAX_UNITS_PER_PACK, 24)
    assert not planner.pack_usable(1, 24)                        # 24 single bottles: more than MAX_PACKS lines
    assert not planner.pack_usable(planner.MAX_UNITS_PER_PACK + 1, 24)


def test_the_next_visit_and_its_store_cutoffs():
    assert price_search.next_visit(datetime(2026, 11, 23, 17, 0, tzinfo=timezone.utc)) == date(2026, 11, 30)
    assert price_search.next_visit(SUNDAY) == date(2026, 11, 30)
    assert price_search.next_visit(BEFORE) == date(2026, 12, 4)       # Wednesday: this Friday
    assert price_search.next_visit(LIVE) == date(2026, 12, 11)        # Friday's own visit is under way
    # The fill's rule: the Friday lead time before the visit. For Mon 30 Nov: Amazon Sat 23:59, in-store Sun 23:59.
    assert price_search.past_cutoff("Amazon", date(2026, 11, 30), SUNDAY)
    assert not price_search.past_cutoff("Walmart", date(2026, 11, 30), SUNDAY)
    assert price_search.past_cutoff("Amazon", date(2026, 12, 4), THURSDAY)
    assert not price_search.past_cutoff("Costco", date(2026, 12, 4), THURSDAY)
    assert not price_search.past_cutoff("Amazon", date(2026, 12, 4), BEFORE)


def test_compare_keeps_the_cheapest_usable_pack_per_unit_and_the_next_best(connect, capsys):
    client = Client(offer("Amazon", 24, "27.48"),          # $1.145 a unit
                    offer("Walmart", 12, "12.00"),         # $1.000 a unit: the cheapest usable
                    offer("Costco", 36, "38.00"),          # $1.056 a unit: next best
                    offer("BJs", 30, "15.00", stock="out_of_stock"))      # $0.50, but out of stock: never wins
    assert command(connect, client, SKU, "--pack", "24", "--compare", "--by", "Kevin", when=BEFORE) == 0
    assert domains(client) == ["amazon.com", "walmart.com", "costco.com", "bjs.com"]
    # The request asks for the cheapest per unit among the packs the slot can use: 2 to 60 for 24 units.
    asked = client.sent[0]["messages"][0]["content"]
    assert "2 to 60" in asked and "per can or bottle" in asked
    with connect() as conn:
        primary, alternate = row(conn), row(conn, "alternate")
    assert (primary.store, primary.units_per_pack, primary.expected_cents) == ("Walmart", 12, 1200)
    assert primary.max_cents == price_search.agent_max(1200) and primary.checked_by == price_search.AGENT_BY
    assert (alternate.store, alternate.units_per_pack, alternate.expected_cents) == ("Costco", 36, 3800)
    out = capsys.readouterr().out
    assert "BJs: out_of_stock" in out and "primary: Walmart" in out and "alternate: Costco" in out


def test_a_pack_the_slot_cant_use_never_wins(connect, capsys):
    # 24 units at one fill: single bottles would take 24 packs, more than a line's MAX_PACKS.
    client = Client(offer("Amazon", 1, "0.50"), failing("Walmart"), failing("Costco"), failing("BJs"))
    assert command(connect, client, SKU, "--pack", "24", "--compare", "--by", "Kevin", when=BEFORE) == 1
    with connect() as conn:
        assert row(conn) is None
    assert "Amazon: pack_mismatch" in capsys.readouterr().out


def test_a_store_past_its_cutoff_is_not_searched_and_cannot_win(connect, capsys):
    # Thursday noon, Friday's visit: Amazon's Wednesday cutoff has passed. Its fresh row is cheaper but can't win.
    with connect() as conn:
        set_row(conn, checked=THURSDAY - timedelta(hours=2), price=2000)          # $0.83 a unit at Amazon
    client = Client(offer("Walmart", 12, "12.00"), failing("Costco"), failing("BJs"))
    assert command(connect, client, SKU, "--pack", "24", "--compare", "--by", "Kevin", "--max-age", "1",
                   when=THURSDAY) == 0
    assert domains(client) == ["walmart.com", "costco.com", "bjs.com"]
    with connect() as conn:
        assert row(conn).store == "Walmart" and row(conn, "alternate") is None
    assert "Amazon: past_cutoff" in capsys.readouterr().out


def test_plan_mode_compares_by_default_and_on_sunday_amazon_is_past_its_cutoff(connect, capsys):
    with connect() as conn:
        picked_plan(conn)
    client = Client(offer("Walmart", 12, "12.37"), failing("Costco"), failing("BJs"),         # COKE20
                    failing("Walmart"), offer("Costco", 24, "18.00"), offer("BJs", 24, "17.00"))   # PICKLE
    assert command(connect, client, "--plan", "picked", "--by", "Kevin", when=SUNDAY) == 0
    assert "amazon.com" not in domains(client) and len(client.sent) == 6
    with connect() as conn:
        coke = sourcing._row(conn, "COKE20", "primary")
        pickle, spare = sourcing._row(conn, "PICKLE", "primary"), sourcing._row(conn, "PICKLE", "alternate")
    assert (coke.store, coke.expected_cents) == ("Walmart", 1237)
    assert (pickle.store, pickle.expected_cents, spare.store) == ("BJs", 1700, "Costco")
    out = capsys.readouterr().out
    assert "Amazon: past_cutoff" in out and "The fill would still refuse" not in out
    # 10 slots of 7 Coca-Cola: 70 units, so packs of 4 or more.
    assert "4 to 60" in client.sent[0]["messages"][0]["content"]


def test_first_usable_keeps_the_old_plan_mode(connect, capsys):
    with connect() as conn:
        picked_plan(conn)
    client = Client(offer("Amazon", 12, "12.37"), searched(report=found(price="18.49")))
    assert command(connect, client, "--plan", "picked", "--by", "Kevin", "--first-usable", when=BEFORE) == 0
    assert domains(client) == ["amazon.com", "amazon.com"]


def test_a_fresh_row_is_compared_without_a_search_and_moves_to_the_alternate(connect, capsys):
    with connect() as conn:
        set_row(conn, checked=BEFORE - timedelta(days=1), price=2599, most=3000)     # Amazon $1.083 a unit
    client = Client(offer("Walmart", 12, "12.00"), failing("Costco"), failing("BJs"))
    assert command(connect, client, SKU, "--pack", "24", "--compare", "--by", "Kevin", when=BEFORE) == 0
    assert domains(client) == ["walmart.com", "costco.com", "bjs.com"]
    with connect() as conn:
        primary, alternate = row(conn), row(conn, "alternate")
    assert (primary.store, primary.expected_cents) == ("Walmart", 1200)
    assert (alternate.store, alternate.expected_cents, alternate.max_cents, alternate.checked_by,
            alternate.checked_at) == ("Amazon", 2599, 3000, "Kevin", BEFORE - timedelta(days=1))


def test_a_cheaper_fresh_row_stays_primary(connect):
    with connect() as conn:
        set_row(conn, checked=BEFORE - timedelta(days=1), price=2000, most=2500)     # $0.83 a unit
    client = Client(offer("Walmart", 12, "12.00"), failing("Costco"), failing("BJs"))
    assert command(connect, client, SKU, "--pack", "24", "--compare", "--by", "Kevin", when=BEFORE) == 0
    with connect() as conn:
        assert (row(conn).store, row(conn).checked_by) == ("Amazon", "Kevin")
        assert row(conn, "alternate").store == "Walmart"


# --- 2. Re-check fresh prices --------------------------------------------------------------

def test_max_age_re_prices_a_row_that_is_still_fresh_and_keeps_its_max(connect):
    with connect() as conn:
        set_row(conn, checked=BEFORE - timedelta(days=1), price=2599, most=3000)
    client = Client(offer("Amazon", 24, "26.00"), offer("Walmart", 12, "12.00"), failing("Costco"),
                    failing("BJs"))
    assert command(connect, client, SKU, "--pack", "24", "--compare", "--by", "Kevin", "--max-age", "12",
                   when=BEFORE) == 0
    assert domains(client)[0] == "amazon.com"
    with connect() as conn:
        alternate = row(conn, "alternate")
    assert (alternate.store, alternate.expected_cents, alternate.max_cents, alternate.checked_at) == \
        ("Amazon", 2600, 3000, BEFORE)


def test_max_age_applies_to_a_one_store_lookup_too(connect, capsys):
    with connect() as conn:
        set_row(conn, checked=BEFORE - timedelta(days=2))
    idle = Client()
    command(connect, idle, SKU, "--pack", "24", "--by", "Kevin", "--no-fallback", when=BEFORE)
    assert idle.sent == [] and "fresh_price" in capsys.readouterr().out
    client = Client(searched(report=found(price="26.00")))
    assert command(connect, client, SKU, "--pack", "24", "--by", "Kevin", "--max-age", "24", when=BEFORE) == 0
    with connect() as conn:
        assert (row(conn).expected_cents, row(conn).checked_at) == (2600, BEFORE)


def test_plan_mode_lists_rows_older_than_the_max_age(connect, capsys):
    with connect() as conn:
        picked_plan(conn)
        for sku, store, units, url in (("COKE20", "Costco", 12, None),
                                       ("PICKLE", "Amazon", 24, "https://www.amazon.com/dp/B000PICKLE")):
            sourcing.set_option(conn, sourcing.Option(
                sku=sku, rank="primary", store=store, units_per_pack=units, expected_cents=1800, max_cents=2100,
                url=url, pack_description=None, checked_at=BEFORE - timedelta(hours=30), checked_by="Kevin"),
                now=BEFORE)
    assert command(connect, Client(), "--plan", "picked", "--by", "Kevin", "--dry-run", when=BEFORE) == 0
    assert "nothing to look up" in capsys.readouterr().out
    assert command(connect, Client(), "--plan", "picked", "--by", "Kevin", "--dry-run", "--max-age", "24",
                   when=BEFORE) == 0
    out = capsys.readouterr().out
    assert "COKE20" in out and "PICKLE" in out and "older than 24 hours" in out


def test_max_age_must_be_a_positive_number_of_hours(connect, capsys):
    assert command(connect, Client(), SKU, "--pack", "24", "--by", "Kevin", "--max-age", "0") == 2


def test_the_weekly_check_compares_our_current_and_planned_drinks(connect, monkeypatch):
    # One call for a weekly job: our slots' current drinks and the picked plan's, compare, max-age N.
    with connect() as conn:
        picked_plan(conn)
        for sku, store, units, url in (("COKE20", "Costco", 12, None),
                                       ("PICKLE", "Amazon", 24, "https://www.amazon.com/dp/B000PICKLE")):
            sourcing.set_option(conn, sourcing.Option(
                sku=sku, rank="primary", store=store, units_per_pack=units, expected_cents=1800, max_cents=2100,
                url=url, pack_description=None, checked_at=LIVE - timedelta(hours=1), checked_by="Kevin"),
                now=LIVE)
    monkeypatch.setattr(price_search, "current_drinks", lambda conn, ours, now: {SKU: 7})
    client = Client(offer("Amazon", 24, "27.48"), failing("Walmart"), failing("Costco"), failing("BJs"))
    assert price_search.check_prices(by="weekly check", max_age_hours=24, connect=connect, client=client,
                                     now=lambda: LIVE).exit_code == 0
    assert len(client.sent) == 4 and all(SKU in b["messages"][0]["content"] for b in client.sent)
    with connect() as conn:
        assert row(conn).expected_cents == 2748


def test_current_drinks_is_empty_with_no_receipts(connect):
    from tests.conftest import TEST_SLOTS

    with connect() as conn:
        assert price_search.current_drinks(conn, tuple(TEST_SLOTS), LIVE) == {}


# --- 3. A cheaper model for lookups -----------------------------------------------------------

@pytest.mark.parametrize("model", [HAIKU, SONNET, "claude-opus-5-5"])
def test_the_lookup_model_setting_takes_a_priced_model(connect, model):
    with connect() as conn:
        config.set_value(conn, "price_search_model", model, "test")
        assert config.get_value(conn, "price_search_model") == model


@pytest.mark.parametrize("value", ["claude-made-up-9", "", 5, None])
def test_the_lookup_model_setting_refuses_anything_unpriced(connect, value):
    with connect() as conn, pytest.raises(ValueError, match="price_search_model"):
        config.set_value(conn, "price_search_model", value, "test")


@pytest.mark.parametrize("model,cost", [(HAIKU, "0.0232"), (SONNET, "0.084")])
def test_the_agents_tool_uses_the_lookup_model_setting(connect, model, cost):
    with connect() as conn:
        config.set_value(conn, "price_search_model", model, "test")
    client = Client(searched(model=model), searched(model=SONNET, searches=1))     # a Haiku find gets a check
    reply, failed = lookup(searcher(connect, client))          # the run's model is Opus
    assert not failed and client.sent[0]["model"] == model
    assert client.sent[0]["output_config"] == {"effort": "low"}
    assert client.sent[0]["tools"][0]["type"] == "web_search_20250305"
    with connect() as conn:
        billed, used = conn.execute("select cost_usd, model from model_calls order by id").fetchone()
    # 30,000 in and 400 out at the model's rates, plus 2 searches at $0.01.
    assert (billed, used) == (Decimal(cost), model)


def test_the_command_uses_the_setting_and_model_overrides_it(connect):
    with connect() as conn:
        config.set_value(conn, "price_search_model", HAIKU, "test")
    client = Client(searched(model=HAIKU), searched(model=SONNET, searches=1))
    command(connect, client, SKU, "--pack", "24", "--by", "Kevin", "--no-fallback", when=BEFORE)
    assert client.sent[0]["model"] == HAIKU
    client = Client(at_store("Walmart", model=SONNET))
    command(connect, client, SKU, "--store", "Walmart", "--pack", "24", "--by", "Kevin", "--no-fallback",
            "--model", SONNET, when=BEFORE)
    assert client.sent[0]["model"] == SONNET


def test_the_agents_caps_still_hold_on_a_cheaper_model(connect):
    with connect() as conn:
        config.set_value(conn, "price_search_model", HAIKU, "test")
    client = Client(*(at_store(s, model=HAIKU) for s in ("Amazon", "Walmart", "Costco")))
    s = searcher(connect, client)
    lookup(s)
    lookup(s, store="Walmart")
    reply, failed = lookup(s, store="Costco")               # 4 searches a run, whatever the model
    assert failed and reply["refused"] == "search_cap" and len(client.sent) == 2
    assert price_search.HARD_SEARCHES_PER_RUN == 4 and price_search.HARD_DAILY_CAP_CENTS == 100
    with connect() as conn:
        config.set_value(conn, "price_search_daily_cap_cents", 1, "test")       # under one Haiku lookup's worst
    client = Client(at_store("Costco", model=HAIKU))
    reply, failed = lookup(searcher(connect, client), store="Costco")
    assert failed and reply["refused"] == "search_cap" and client.sent == []


def test_compare_is_offered_to_people_only(connect):
    s = searcher(connect, Client())
    with pytest.raises(ValueError):
        s.compare(price_search.Drink(SKU, 24, 24), store=True)


# --- 4. The no-write benchmark ----------------------------------------------------------------

def test_no_store_searches_every_store_writes_nothing_and_books_test_spend(connect, tmp_path, capsys):
    with connect() as conn:
        set_row(conn, checked=LIVE - timedelta(hours=1), price=2599, most=3000)         # fresh: searched anyway
        before = sourcing.options(conn)
    client = Client(offer("Amazon", 24, "27.48", model=HAIKU), offer("Walmart", 12, "12.00", model=HAIKU),
                    failing("Costco"), offer("BJs", 30, "15.00", stock="out_of_stock", model=HAIKU))
    out_file = tmp_path / "haiku.json"
    assert command(connect, client, SKU, "--pack", "24", "--compare", "--no-store", "--model", HAIKU,
                   "--json", str(out_file), "--by", "Kevin") == 0
    assert domains(client) == ["amazon.com", "walmart.com", "costco.com", "bjs.com"]
    assert {b["model"] for b in client.sent} == {HAIKU}
    with connect() as conn:
        assert sourcing.options(conn) == before
        calls = conn.execute("select purpose, label from model_calls order by id").fetchall()
        entries = conn.execute("select count(*) from ledger_entries where kind = 'tokens'").fetchone()[0]
        writes = conn.execute("select count(*) from action_log where tool = %s and outcome = 'done'",
                              (price_search.TOOL_NAME,)).fetchone()[0]
    # After the start, still test spend: nothing was bought or stored with it.
    assert {p for p, _ in calls} == {"test"} and entries == 0 and writes == 0
    assert all(label.startswith(price_search.BENCH_LABEL + " ") for _, label in calls)
    saved = json.loads(out_file.read_text(encoding="utf-8"))
    assert saved["model"] == HAIKU and saved["stored"] is False
    drink = saved["drinks"][0]
    assert drink["sku"] == SKU and [s["store"] for s in drink["stores"]] == ["Amazon", "Walmart", "Costco", "BJs"]
    walmart = drink["stores"][1]
    assert (walmart["code"], walmart["price"], walmart["units_per_pack"], walmart["stock"], walmart["url"]) == \
        (None, "12.00", 12, "in_stock", PAGES["Walmart"])
    assert drink["stores"][2]["code"] == "not_found" and drink["stores"][3]["code"] == "out_of_stock"
    assert drink["primary"]["store"] == "Walmart" and drink["alternate"]["store"] == "Amazon"
    assert Decimal(drink["cost_usd"]) == sum(Decimal(s["cost_usd"]) for s in drink["stores"])
    assert "nothing was stored" in capsys.readouterr().out.lower()


def test_no_store_needs_compare(connect, capsys):
    assert command(connect, Client(), SKU, "--pack", "24", "--no-store", "--by", "Kevin") == 2


def test_no_store_plan_mode_searches_every_drink_even_with_fresh_prices(connect, tmp_path):
    with connect() as conn:
        picked_plan(conn)
        for sku, store, units, url in (("COKE20", "Costco", 12, None),
                                       ("PICKLE", "Amazon", 24, "https://www.amazon.com/dp/B000PICKLE")):
            sourcing.set_option(conn, sourcing.Option(
                sku=sku, rank="primary", store=store, units_per_pack=units, expected_cents=1800, max_cents=2100,
                url=url, pack_description=None, checked_at=BEFORE, checked_by="Kevin"), now=BEFORE)
    client = Client(*(failing(s) for s in price_search.STORE_ORDER * 2))
    command(connect, client, "--plan", "picked", "--compare", "--no-store", "--by", "Kevin", "--json",
            str(tmp_path / "x.json"), when=BEFORE)
    assert len(client.sent) == 8


def test_the_comparison_script_lines_up_runs(tmp_path, capsys):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "compare_price_runs.py"
    spec = importlib.util.spec_from_file_location("compare_price_runs", path)
    compare_price_runs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(compare_price_runs)

    def run(model, price, url, cost):
        return {"model": model, "stored": False, "drinks": [{"sku": SKU, "cost_usd": cost, "stores": [
            {"store": "Amazon", "code": None, "price": price, "units_per_pack": 24, "stock": "in_stock", "url": url,
             "cost_usd": cost}], "primary": {"store": "Amazon"}, "alternate": None}]}

    a, b = tmp_path / "haiku.json", tmp_path / "opus.json"
    a.write_text(json.dumps(run(HAIKU, "27.48", URL, "0.0232")), encoding="utf-8")
    b.write_text(json.dumps(run("claude-opus-5-5", "27.48", URL.replace("www.", ""), "0.1480")), encoding="utf-8")
    assert compare_price_runs.main([str(a), str(b)]) == 0
    out = capsys.readouterr().out
    assert HAIKU in out and "claude-opus-5-5" in out
    assert "same listing: yes" in out and "same price: yes" in out
    assert "$0.0232" in out and "$0.1480" in out


# --- 5. us.amazon.com and smile.amazon.com ----------------------------------------------------

@pytest.mark.parametrize("host", ["us.amazon.com", "smile.amazon.com", "US.Amazon.com"])
def test_amazons_regional_hosts_are_its_own_site_and_stored_as_www(connect, host):
    page = f"https://{host}/Dr-Pepper-Soda-20-oz/dp/B07X1Y2Z3A"
    client = Client(searched(urls=(page,), report=found(url=page)))
    reply, failed = lookup(searcher(connect, client))
    assert not failed and reply["url"] == PLAIN           # on www, and plain (#202)
    with connect() as conn:
        assert row(conn).url == PLAIN


@pytest.mark.parametrize("page", ["https://fresh.amazon.com/dp/B07X1Y2Z3A",
                                  "https://us.amazon.com.example.net/dp/B07X1Y2Z3A",
                                  "https://us.walmart.com/ip/Dr-Pepper/123"])
def test_other_hosts_are_still_off_the_store(page):
    store = "Walmart" if "walmart" in page else "Amazon"
    assert not price_search.on_store(page, store)
