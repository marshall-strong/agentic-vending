"""The dashboard's price-check panel (`vending.price_check_panel`): the weekly checks, lookup spend, and each store's
latest price for the drinks in our plan, with its flags."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from psycopg.types.json import Jsonb

from tests.conftest import TEST_SLOTS
from tests.test_dashboard import catalog, fulfilled, page_conn  # noqa: F401  (a fixture)
from vending import agent_runs, dashboard, ledger, price_check_panel, price_schedule, settings
from vending.config import set_value

# Wed 9 Dec 2026, 10:00 Eastern: a week into the competition.
NOW = datetime(2026, 12, 9, 15, 0, tzinfo=timezone.utc)
OPENED = datetime(2026, 12, 2, 15, 0, tzinfo=timezone.utc)
TUESDAY = datetime(2026, 12, 8, 17, 0, tzinfo=timezone.utc)      # Tue 8 Dec, 12:00 Eastern
COKE, PEPSI, REDBULL, FANTA = "COCA-ORIGINAL-20BOT", "PEPSI-ORIGINAL-20BOT", "REDBULL-ORIGINAL-8_4CAN", "FANTA-ORANGE-20BOT"
OUTSIDE = next(f"{r}{c}" for r in "12345" for c in "12345678" if f"{r}{c}" not in TEST_SLOTS)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    """The weekly check's settings as a fresh machine has them (off), whatever `.env` says; the slot lock passed."""
    names = (price_schedule.MODE_SETTING, price_schedule.TIMES_SETTING, price_schedule.FROM_SETTING)
    for name in names:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(settings, "_FILE", {k: v for k, v in settings._FILE.items() if k not in names})
    monkeypatch.setattr(dashboard, "lock_at", lambda: OPENED - timedelta(days=5))


def products(conn):
    catalog(conn, (COKE, "Coca-Cola 20 oz", "Coca-Cola", None, "20 oz bottle"),
            (PEPSI, "Pepsi 20 oz", "Pepsi", None, "20 oz bottle"),
            (REDBULL, "Red Bull 250 ml", "Red Bull", None, "250 ml can"),
            (FANTA, "Fanta Orange 20 oz", "Fanta", "Orange", "20 oz bottle"))


def source(conn, sku, rank, store, units, cents, max_cents, at, by="kevin"):
    conn.execute("insert into sourcing_options (sku, rank, store, units_per_pack, expected_cents, max_cents, url,"
                 " checked_at, checked_by) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                 (sku, rank, store, units, cents, max_cents,
                  "https://www.amazon.com/dp/B000000000" if store == "Amazon" else None, at, by))


def lookup(conn, sku, store, at, outcome="found", units=12, cents=None, kind="search", model="claude-haiku-5-5",
           cost="0.0100", by=price_schedule.BY, no_store=False):
    conn.execute("insert into price_lookups (at, sku, store, kind, asked_min, asked_max, units_per_pack, price_cents,"
                 " outcome, model, cost_usd, no_store, by) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                 (at, sku, store, kind, units, units, units if cents else None, cents, outcome, model, Decimal(cost),
                  no_store, by))


def weekly_plan(conn, *entries):
    run = agent_runs.start(conn, mode="live", model="claude-sonnet-5-5")
    agent_runs.finish(conn, run, "submitted", plan={"visit": "scheduled", "reason": "r", "slots": list(entries)})
    conn.execute("update agent_runs set started_at = %s, finished_at = %s where id = %s",
                 (NOW - timedelta(days=2), NOW - timedelta(days=2), run))
    return run


def panel(page_conn, **kw):
    snap = dashboard.snapshot(page_conn, NOW, **kw)
    return snap, snap["price_check_panel"]


def drink(data, sku):
    return next(d for d in data["drinks"] if d["sku"] == sku)


def codes(d):
    return [f["code"] for f in d["flags"]]


# --- Empty and before ---------------------------------------------------------------------------

def test_a_fresh_database_says_no_check_has_run_and_when_the_first_is_due(page_conn):
    snap, p = panel(page_conn)
    assert p["ok"]
    d = p["data"]
    assert d["runs"] == [] and d["drinks"] == [] and d["missing"] == []
    s = d["schedule"]
    assert (s["worker_mode"], s["db_mode"], s["runs_as"], s["from_worker"]) == ("off", "off", "off", False)
    # Off, so when the first would be due: the default times from PRICE_CHECK_FROM, Tue 15 Dec after NOW.
    assert s["next_at"] == datetime(2026, 12, 15, 17, 0, tzinfo=timezone.utc)
    assert d["spend"]["week"] == 0 and d["spend"]["opened"] is False and d["spend"]["since_opening"] is None
    page = dashboard.render(snap)
    section = page[page.index('id="price-checks"'):page.index('id="money"')]
    assert "First due" in section and "Tue 15 Dec, 12:00 PM ET" in section and "No drinks" in section


def test_before_the_opening_the_first_run_is_due_on_the_first_day_and_spend_is_test_spend(conn, page_conn):
    early = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
    lookup(conn, "ANY-SKU", "Walmart", early - timedelta(hours=2), cost="0.2500", by="kevin")
    snap = dashboard.snapshot(page_conn, early)
    d = snap["price_check_panel"]["data"]
    assert d["schedule"]["next_at"] == datetime(2026, 12, 1, 17, 0, tzinfo=timezone.utc)     # Tue 1 Dec 12:00 ET
    assert d["spend"]["opened"] is False and d["spend"]["week"] == Decimal("0.2500")
    section = dashboard.render(snap).split('id="price-checks"')[1].split("</section>")[0]
    assert "Tue 1 Dec, 12:00 PM ET" in section and "test spend" in section and "not opened" in section


def test_the_worker_report_and_the_database_setting_decide_whether_it_is_on(conn, page_conn):
    set_value(conn, price_schedule.DB_MODE_KEY, "live", "kevin")
    worker = {"price_check": {"mode": "live", "next_at": "2026-12-15T17:00:00+00:00", "last": None, "problem": None}}
    s = panel(page_conn, syncs=worker)[1]["data"]["schedule"]
    assert (s["runs_as"], s["from_worker"], s["next_at"]) == ("live", True, datetime(2026, 12, 15, 17, tzinfo=timezone.utc))
    set_value(conn, price_schedule.DB_MODE_KEY, "dry-run", "kevin")
    assert panel(page_conn, syncs=worker)[1]["data"]["schedule"]["runs_as"] == "dry-run"


def test_the_weekly_runs_show_newest_first_with_their_model_and_counts(conn, page_conn):
    conn.execute("insert into price_check_runs (due_at, latest_at, mode, compare, claimed_at, finished_at, outcome,"
                 " drinks, changed, failures, cost_usd) values"
                 " (%s, %s, 'live', true, %s, %s, 'partial', 16, 3, 2, 1.18),"
                 " (%s, %s, 'dry-run', false, %s, %s, 'done', 18, 0, 0, 0)",
                 (TUESDAY, TUESDAY + timedelta(hours=8), TUESDAY, TUESDAY + timedelta(minutes=40),
                  TUESDAY - timedelta(days=7), TUESDAY - timedelta(days=7, hours=-8), TUESDAY - timedelta(days=7),
                  TUESDAY - timedelta(days=7, minutes=-5)))
    lookup(conn, COKE, "Walmart", TUESDAY + timedelta(minutes=5), cents=1740)
    lookup(conn, COKE, "Walmart", TUESDAY + timedelta(minutes=6), cents=1740, kind="check", model="claude-sonnet-5-5")
    snap, p = panel(page_conn)
    runs = p["data"]["runs"]
    assert [r["outcome"] for r in runs] == ["partial", "done"]
    assert runs[0]["models"] == ["claude-haiku-5-5", "claude-sonnet-5-5"] and runs[1]["models"] == []
    assert (runs[0]["drinks"], runs[0]["changed"], runs[0]["failures"]) == (16, 3, 2)
    section = dashboard.render(snap).split('id="price-checks"')[1].split("</section>")[0]
    assert "Next due" in section and "compare all stores" in section and "$1.18" in section
    assert "dry run" in section


def test_lookup_spend_counts_the_week_and_the_opening_and_leaves_out_benchmarks(conn, page_conn):
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=OPENED)
    lookup(conn, COKE, "Walmart", OPENED - timedelta(hours=1), cost="5.0000")              # before the opening
    lookup(conn, COKE, "Walmart", OPENED + timedelta(days=1), cost="0.2000")               # last week
    lookup(conn, COKE, "Costco", TUESDAY, cost="0.3000")                                   # this week
    lookup(conn, COKE, "Amazon", TUESDAY, cost="9.0000", no_store=True)                    # a benchmark
    sp = panel(page_conn)[1]["data"]["spend"]
    assert sp["week_start"] == datetime(2026, 12, 5, 5, 0, tzinfo=timezone.utc)            # Sat 5 Dec 00:00 Eastern
    assert (sp["week"], sp["week_lookups"]) == (Decimal("0.3000"), 1)
    assert (sp["since_opening"], sp["since_lookups"]) == (Decimal("0.5000"), 2)


# --- Drinks and flags ---------------------------------------------------------------------------

def test_each_store_shows_its_latest_price_and_the_cheapest_fresh_one(conn, page_conn):
    products(conn)
    weekly_plan(conn, {"slot": "15", "product": COKE, "fill_to": 7, "sale_price": "2.50"},
                {"slot": "16", "product": COKE, "fill_to": 7, "sale_price": "2.50"})
    source(conn, COKE, "primary", "Walmart", 12, 1740, 2050, TUESDAY - timedelta(days=4))
    source(conn, COKE, "alternate", "Costco", 24, 3149, 3600, TUESDAY - timedelta(days=4))
    lookup(conn, COKE, "Costco", TUESDAY, units=24, cents=3299, model="claude-sonnet-5-5")   # newer than the row
    lookup(conn, COKE, "Amazon", TUESDAY, outcome="not_found")
    snap, p = panel(page_conn)
    d = drink(p["data"], COKE)
    assert d["slots"] == ["15", "16"] and d["name"] == "Coca-Cola 20 oz"
    cells = {c["store"]: c for c in d["stores"]}
    assert cells["Walmart"]["price"]["unit"] == "1.45" and cells["Walmart"]["price"]["source"] == "person"
    assert cells["Costco"]["price"]["unit"] == "1.37" and cells["Costco"]["price"]["source"] == "price check"
    assert cells["Amazon"]["price"] is None and cells["Amazon"]["last_try"]["words"] == "not found"
    assert cells["BJs"] == {"store": "BJs", "price": None, "last_try": None, "unverified": None}
    assert d["cheapest"] == "Costco"
    assert d["primary"]["store"] == "Walmart" and d["alternate"]["store"] == "Costco"
    assert codes(d) == []          # Amazon isn't a store we buy from, so its miss isn't a flag
    section = dashboard.render(snap).split('id="price-checks"')[1].split("</section>")[0]
    assert "Coca-Cola 20 oz" in section and COKE in section and "cheapest" in section


def test_stale_not_found_and_over_max_are_flagged_from_the_rows_we_buy_from(conn, page_conn):
    products(conn)
    weekly_plan(conn, {"slot": "15", "product": COKE}, {"slot": "16", "product": PEPSI},
                {"slot": "17", "product": REDBULL})
    source(conn, COKE, "primary", "Walmart", 12, 1740, 2050, NOW - timedelta(days=9))           # stale
    source(conn, PEPSI, "primary", "Walmart", 12, 1620, 1900, TUESDAY - timedelta(days=3))
    lookup(conn, PEPSI, "Walmart", TUESDAY, outcome="out_of_stock")                            # after its check
    source(conn, REDBULL, "primary", "Walmart", 12, 2040, 2400, TUESDAY - timedelta(days=3))
    lookup(conn, REDBULL, "Walmart", TUESDAY, cents=2520, model="claude-sonnet-5-5")           # over its max
    d = panel(page_conn)[1]["data"]
    coke, pepsi, rb = drink(d, COKE), drink(d, PEPSI), drink(d, REDBULL)
    assert codes(coke) == ["stale"] and coke["flags"][0]["days"] == 9
    assert codes(pepsi) == ["not_found"] and pepsi["flags"][0]["stores"] == [{"store": "Walmart", "words": "out of stock"}]
    assert "over_max" in codes(rb)
    over = next(f for f in rb["flags"] if f["code"] == "over_max")
    assert (over["price_cents"], over["max_cents"]) == (2520, 2400)


def test_a_change_over_five_percent_is_flagged_with_the_second_check(conn, page_conn):
    products(conn)
    weekly_plan(conn, {"slot": "15", "product": COKE}, {"slot": "16", "product": PEPSI})
    source(conn, COKE, "primary", "Walmart", 12, 1680, 2050, TUESDAY - timedelta(days=6))
    lookup(conn, COKE, "Walmart", TUESDAY - timedelta(days=6), cents=1680, model="claude-sonnet-5-5")
    lookup(conn, COKE, "Walmart", TUESDAY, cents=1800)                                         # +7.1%
    lookup(conn, COKE, "Walmart", TUESDAY + timedelta(minutes=1), cents=1800, kind="check", model="claude-sonnet-5-5")
    source(conn, PEPSI, "primary", "Walmart", 12, 1620, 1900, TUESDAY - timedelta(days=6))
    lookup(conn, PEPSI, "Walmart", TUESDAY - timedelta(days=6), cents=1620, model="claude-sonnet-5-5")
    lookup(conn, PEPSI, "Walmart", TUESDAY, cents=1800)                                        # +11.1%
    lookup(conn, PEPSI, "Walmart", TUESDAY + timedelta(minutes=1), outcome="check_disagrees", cents=1650,
           kind="check", model="claude-sonnet-5-5")
    lookup(conn, PEPSI, "Costco", TUESDAY - timedelta(days=6), cents=1620, model="claude-sonnet-5-5")
    lookup(conn, PEPSI, "Costco", TUESDAY, cents=1660)                                         # +2.5%: no flag
    d = panel(page_conn)[1]["data"]
    coke = next(f for f in drink(d, COKE)["flags"] if f["code"] == "changed")
    assert (coke["percent"], coke["from_unit"], coke["to_unit"]) == ("+7.1", "1.40", "1.50")
    assert coke["verified_by"] == "claude-sonnet-5-5"
    pepsi = drink(d, PEPSI)
    # The find the check didn't confirm is unverified, not a change, and not a miss.
    assert [f["code"] for f in pepsi["flags"]] == ["unverified"]
    assert pepsi["flags"][0]["store"] == "Walmart" and pepsi["flags"][0]["check"]["model"] == "claude-sonnet-5-5"
    # The find the check didn't confirm is never a price: Walmart still shows the stored row's.
    walmart = next(c for c in pepsi["stores"] if c["store"] == "Walmart")
    assert walmart["price"]["price_cents"] == 1620


def test_an_unknown_price_shows_as_unknown_never_zero(conn, page_conn):
    products(conn)
    weekly_plan(conn, {"slot": "15", "product": FANTA})
    conn.execute("insert into sourcing_options (sku, rank, store, units_per_pack, expected_cents, max_cents,"
                 " checked_at, checked_by) values (%s, 'primary', 'Walmart', 12, null, 2000, %s, 'kevin')",
                 (FANTA, TUESDAY))
    snap, p = panel(page_conn)
    d = drink(p["data"], FANTA)
    assert codes(d) == ["no_price"] and d["primary"]["price_cents"] is None and d["primary"]["unit"] is None
    assert all(c["price"] is None for c in d["stores"]) and d["cheapest"] is None
    row = dashboard.render(snap).split('id="price-checks"')[1].split("</section>")[0].split(FANTA)[1].split("</tr>")[0]
    assert "no price" in row.lower() and "$0.00" not in row


def test_a_drink_with_no_row_at_all_says_no_price_and_where_it_wasnt_found(conn, page_conn):
    products(conn)
    weekly_plan(conn, {"slot": "15", "product": FANTA})
    lookup(conn, FANTA, "Walmart", TUESDAY, outcome="not_found")
    lookup(conn, FANTA, "Amazon", TUESDAY, outcome="out_of_stock", cents=1890)
    d = drink(panel(page_conn)[1]["data"], FANTA)
    assert codes(d) == ["no_price", "not_found"] and d["primary"] is None
    assert d["flags"][1]["stores"] == [{"store": "Amazon", "words": "out of stock"},
                                       {"store": "Walmart", "words": "not found"}]


def test_the_first_fill_plan_names_the_drinks_before_the_lock(conn, page_conn, monkeypatch):
    monkeypatch.setattr(dashboard, "lock_at", lambda: NOW + timedelta(days=30))
    products(conn)
    day_one = [{"slot": s, "sku": COKE if s == "15" else PEPSI, "units": 7, "price_cents": 250, "why": "w"}
               for s in TEST_SLOTS]
    conn.execute("insert into first_fill_plans (loaded_at, loaded_by, label, source_note, plan, projection, slots,"
                 " picked) values (%s, 'test', '6', 'test', %s, '{}', %s, true)",
                 (NOW - timedelta(days=3), Jsonb({"day_one": day_one}), list(TEST_SLOTS)))
    d = panel(page_conn)[1]["data"]
    assert d["plan"] == "first fill"
    assert [x["sku"] for x in d["drinks"]] == [COKE, PEPSI]
    assert drink(d, COKE)["slots"] == ["15"] and len(drink(d, PEPSI)["slots"]) == len(TEST_SLOTS) - 1


def test_a_pull_buys_nothing_and_a_kept_slot_takes_its_product_from_the_receipts(conn, page_conn):
    products(conn)
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=OPENED)
    fulfilled(conn, "pr-1", "16", PEPSI, units_loaded=7, paid="16.20", at=OPENED + timedelta(days=1))
    weekly_plan(conn, {"slot": "15", "product": COKE, "fill_to": 0}, {"slot": "16", "keep": True})
    d = panel(page_conn)[1]["data"]
    assert [x["sku"] for x in d["drinks"]] == [PEPSI]


# --- Missing tables -----------------------------------------------------------------------------

def test_missing_migrations_blank_only_their_part(conn, page_conn):
    products(conn)
    weekly_plan(conn, {"slot": "15", "product": COKE})
    source(conn, COKE, "primary", "Walmart", 12, 1740, 2050, TUESDAY)
    conn.execute("drop table price_check_runs")
    conn.execute("drop table price_lookups")
    snap, p = panel(page_conn)
    assert p["ok"]
    d = p["data"]
    assert d["runs"] is None and d["spend"] is None and d["lookups_missing"] is True
    assert d["missing"] == ["weekly checks", "lookup log"]
    assert drink(d, COKE)["primary"]["store"] == "Walmart"
    section = dashboard.render(snap).split('id="price-checks"')[1].split("</section>")[0]
    assert "python -m vending.db migrate" in section and "Coca-Cola 20 oz" in section


def test_a_missing_sourcing_table_says_so_and_the_rest_still_shows(conn, page_conn):
    weekly_plan(conn, {"slot": "15", "product": COKE})
    conn.execute("drop table sourcing_options")
    snap, p = panel(page_conn)
    assert p["ok"] and p["data"]["sourcing_missing"] is True and p["data"]["runs"] == []
    assert p["data"]["missing"] == ["sourcing"] and "Sourcing table missing" in dashboard.render(snap)


# --- Integrity ----------------------------------------------------------------------------------

def test_nothing_about_a_selection_outside_our_assignment_is_read_or_shown(conn, page_conn):
    products(conn)
    # A plan that names a selection that isn't ours, with a drink only it holds.
    weekly_plan(conn, {"slot": "15", "product": COKE}, {"slot": OUTSIDE, "product": FANTA})
    source(conn, COKE, "primary", "Walmart", 12, 1740, 2050, TUESDAY)
    source(conn, FANTA, "primary", "Walmart", 12, 1777, 2099, TUESDAY)
    lookup(conn, FANTA, "Costco", TUESDAY, cents=1888)
    snap, p = panel(page_conn)
    assert [x["sku"] for x in p["data"]["drinks"]] == [COKE]
    text = dashboard.to_json(snap["price_check_panel"]) + dashboard.render(snap).split('id="price-checks"')[1].split("</section>")[0]
    assert FANTA not in text and "17.77" not in text and "18.88" not in text and f'"{OUTSIDE}"' not in text


def test_plan_drinks_reads_only_our_selections():
    snap = {"ours": {"ok": True, "data": ["15", "16"]},
            "plan": {"ok": True, "data": {"slots": [{"slot": "15", "now": {"product": COKE}},
                                                   {"slot": OUTSIDE, "now": {"product": FANTA}}]}},
            "stock": {"ok": True, "data": {"slots": {OUTSIDE: {"product": PEPSI}}}}}
    assert price_check_panel.plan_drinks(snap) == {"source": "weekly", "drinks": {COKE: ["15"]}}
    assert price_check_panel.plan_drinks({"ours": {"ok": False}, "plan": snap["plan"]})["drinks"] == {}


# --- Sol's review of #246 ------------------------------------------------------------------------

def test_an_unchecked_cheap_find_is_unverified_never_a_price_or_the_cheapest(conn, page_conn):
    # The lookup logs a find before it checks it; a run whose searches ran out never logs the check.
    products(conn)
    weekly_plan(conn, {"slot": "15", "product": COKE})
    source(conn, COKE, "primary", "Walmart", 12, 1740, 2050, TUESDAY - timedelta(days=1))
    lookup(conn, COKE, "Costco", TUESDAY, units=24, cents=2400)               # Haiku, no check: $1.00 a unit
    d = drink(panel(page_conn)[1]["data"], COKE)
    costco = next(c for c in d["stores"] if c["store"] == "Costco")
    assert costco["price"] is None and costco["unverified"]["unit"] == "1.00"
    assert d["cheapest"] == "Walmart" and codes(d) == ["unverified"]


def test_a_cheap_find_within_the_check_percent_of_a_confirmed_price_is_verified(conn, page_conn):
    products(conn)
    weekly_plan(conn, {"slot": "15", "product": COKE})
    source(conn, COKE, "primary", "Walmart", 12, 1740, 2050, TUESDAY - timedelta(days=8))
    lookup(conn, COKE, "Costco", TUESDAY - timedelta(days=7), units=24, cents=3100, model="claude-sonnet-5-5")
    lookup(conn, COKE, "Costco", TUESDAY, units=24, cents=3150)               # Haiku, 1.6% off: no check needed
    d = drink(panel(page_conn)[1]["data"], COKE)
    costco = next(c for c in d["stores"] if c["store"] == "Costco")
    assert costco["price"]["price_cents"] == 3150 and costco["unverified"] is None and d["cheapest"] == "Costco"


def test_the_test_opening_is_not_the_competitions_opening(conn, page_conn):
    from vending import ledger as ledger_mod

    test_at = datetime(2026, 11, 11, 17, 0, tzinfo=timezone.utc)
    ledger_mod.record(conn, "opening", 60000, ledger_mod.TEST_OPENING_DETAIL, "test", at=test_at)
    assert ledger_mod.is_test_opening(ledger_mod.opening(conn))
    lookup(conn, COKE, "Walmart", NOW - timedelta(days=1), cost="0.5000", by="kevin")
    sp = panel(page_conn)[1]["data"]["spend"]
    assert sp["opened"] is False and sp["since_opening"] is None


def test_the_change_threshold_uses_the_confirmed_price(conn, page_conn):
    products(conn)
    weekly_plan(conn, {"slot": "15", "product": COKE})
    source(conn, COKE, "primary", "Walmart", 12, 1680, 2050, TUESDAY - timedelta(days=6))
    lookup(conn, COKE, "Walmart", TUESDAY - timedelta(days=6), cents=1680, model="claude-sonnet-5-5")
    lookup(conn, COKE, "Walmart", TUESDAY, cents=1770)                        # the find: +5.4%
    lookup(conn, COKE, "Walmart", TUESDAY + timedelta(minutes=1), cents=1750, kind="check", model="claude-sonnet-5-5")
    d = drink(panel(page_conn)[1]["data"], COKE)                              # stored at $17.50: +4.2%
    assert "changed" not in codes(d)


def test_over_max_compares_the_price_the_store_cell_shows(conn, page_conn):
    products(conn)
    weekly_plan(conn, {"slot": "15", "product": COKE})
    lookup(conn, COKE, "Walmart", TUESDAY - timedelta(days=2), cents=2100, model="claude-sonnet-5-5")
    source(conn, COKE, "primary", "Walmart", 12, 1740, 2050, TUESDAY)         # a newer row, under its max
    d = drink(panel(page_conn)[1]["data"], COKE)
    walmart = next(c for c in d["stores"] if c["store"] == "Walmart")
    assert walmart["price"]["price_cents"] == 1740 and "over_max" not in codes(d)


def test_the_lookup_read_is_bounded_per_store_and_indexed(conn, page_conn):
    products(conn)
    for n in range(price_check_panel.LOOKUPS_PER_STORE + 15):
        lookup(conn, COKE, "Walmart", TUESDAY - timedelta(hours=n), cents=1740, model="claude-sonnet-5-5")
    got = price_check_panel._lookups(page_conn, [COKE], NOW)
    assert len(got[COKE]["Walmart"]["readings"]) == price_check_panel.LOOKUPS_PER_STORE
    names = {r[0] for r in conn.execute("select indexname from pg_indexes where tablename = 'price_lookups'")}
    assert {"price_lookups_by_who", "price_lookups_at"} <= names
