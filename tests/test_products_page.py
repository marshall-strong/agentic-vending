"""The Products page (#55): every product we can order, our sourcing, the day-one plan, back stock."""

import re
import time
from datetime import datetime, timedelta, timezone
from html import unescape

import pytest
from psycopg.types.json import Jsonb

from tests.conftest import TEST_SLOTS
from tests.test_dashboard import catalog_request, fulfilled, get, read_only, served  # noqa: F401 (fixture)
from tests.test_pages import _get, _written, fresh, reader, site  # noqa: F401 (fixtures)
from vending import dashboard, demo, ledger, pages, products_page
from vending.demo import make

NOW = datetime(2026, 11, 20, 18, tzinfo=timezone.utc)


def _ok(data):
    return {"ok": True, "data": data}


def _option(sku, checked_at, rank="primary", expected=1749, units=12, store="Walmart"):
    return {"rank": rank, "store": store, "units_per_pack": units, "expected_cents": expected, "max_cents": 2050,
            "pack_description": None, "checked_at": checked_at, "checked_by": "kevin"}


def _plan_slot(slot, sku, units=7, product=None):
    return {"slot": slot, "sku": sku, "product": product or sku, "units": units}


def _data(products=None, sourcing=None, plan_slots=None, closet=None, synced=NOW - timedelta(hours=1),
          requests=None, picked=True):
    """What `read` returns, built by hand."""
    catalog_requests = _ok(requests or [])
    return {
        "as_of": NOW,
        "catalog": _ok({"synced_at": synced, "products": products if products is not None else [
            {"sku": "COKE-20", "name": "Coca-Cola 20 oz", "line": "Coca-Cola", "flavor": None, "size": "20 oz bottle"},
            {"sku": "TOPO-16", "name": "Topo Chico 16 oz", "line": "Topo Chico", "flavor": None, "size": "16 oz glass"},
            {"sku": "DEW-20", "name": "Mountain Dew 20 oz", "line": "Mountain Dew", "flavor": None, "size": "20 oz bottle"}]}),
        "sourcing": _ok(sourcing if sourcing is not None else {"COKE-20": [_option("COKE-20", NOW - timedelta(days=2))]}),
        "plan": _ok({"id": 6, "picked": picked, "slots": plan_slots if plan_slots is not None else [
            _plan_slot("15", "COKE-20"), _plan_slot("16", "COKE-20"), _plan_slot("17", "TOPO-16", 7)]}),
        "closet": _ok(closet if closet is not None else [{"product": "COKE-20", "units": 5, "unit_cost": "1.45"}]),
        "catalog_requests": catalog_requests,
        "catalog_counts": dashboard._catalog_counts(catalog_requests),
    }


def _row(page, sku):
    """The table row naming `sku`, as text."""
    match = re.search(rf"<tr[^>]*>(?:(?!</tr>).)*<code>{re.escape(sku)}</code>(?:(?!</tr>).)*</tr>", page, re.S)
    assert match, sku
    return match.group(0)


def _text(fragment):
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


# --- Without a database ---------------------------------------------------------------------------

def test_each_product_shows_sourcing_plan_and_back_stock():
    page = products_page.html(_data())
    coke = _text(_row(page, "COKE-20"))
    # Pack price $17.49 over 12 is 145.75 cents: $1.46 a unit, rounded half up, never a float.
    assert "Walmart" in coke and "12 a pack at $17.49" in coke and "$1.46 a unit" in coke and "max $20.50" in coke
    assert "checked Wed 18 Nov by kevin" in coke and "STALE" not in coke
    assert "in plan" in coke and "14 units" in coke and "slots 15, 16" in coke
    assert "5 × $1.45 each" in coke
    dew = _text(_row(page, "DEW-20"))
    assert "none" in dew and "in plan" not in dew and "no sourcing" not in dew
    assert "3 products the catalog lists, 1 with a sourcing row" in _text(page)


def test_a_planned_product_with_no_sourcing_row_is_flagged_at_the_top_and_in_its_row():
    page = products_page.html(_data())
    topo = _row(page, "TOPO-16")
    assert topo.startswith('<tr class="gap">') and "no sourcing" in topo
    items = products_page.alerts(_data())
    assert items[0]["level"] == "bad" and "1 product in the day-one plan has no sourcing row" in items[0]["text"]
    assert "Topo Chico 16 oz (TOPO-16)" in items[0]["text"]
    # Coca-Cola is sourced, and Mountain Dew isn't planned: neither is flagged.
    assert 'class="gap"' not in _row(page, "COKE-20") and 'class="gap"' not in _row(page, "DEW-20")
    # If the sourcing table can't be read, nobody knows which are missing: no flag, and the page says so.
    blind = _data()
    blind["sourcing"] = {"ok": False, "why": "couldn't be read"}
    assert products_page.unsourced(blind) == [] and 'class="gap"' not in products_page.html(blind)
    assert "Sourcing can&rsquo;t be read" in products_page.html(blind)


def test_a_planned_line_with_no_sku_is_flagged_and_listed_outside_the_catalog():
    data = _data(plan_slots=[_plan_slot("15", None, 7, "Iced Energy Peach"), _plan_slot("16", "GONE-12", 8)])
    text = products_page.alerts(data)[0]["text"]
    assert "2 products" in text and "Iced Energy Peach (no SKU yet)" in text and "GONE-12" in text
    page = products_page.html(data)
    unlisted = page[page.index('id="unlisted"'):page.index('id="catalog-requests"')]
    assert "Iced Energy Peach" in unlisted and "no SKU yet" in unlisted and "<code>GONE-12</code>" in unlisted
    # Its row keeps the plan's units and slot, and the badge the warning above gives it.
    peach = re.search(r'<tr[^>]*>(?:(?!</tr>).)*Iced Energy Peach(?:(?!</tr>).)*</tr>', unlisted, re.S).group(0)
    assert peach.startswith('<tr class="gap">') and "no sourcing" in peach
    assert "in plan" in peach and "7 units" in _text(peach) and "slot 15" in _text(peach)


def test_a_sourcing_price_older_than_7_days_is_stale():
    sourcing = {"COKE-20": [_option("COKE-20", NOW - timedelta(days=7, minutes=1)),
                            _option("COKE-20", NOW - timedelta(days=7), rank="alternate", store="Costco", units=24,
                                    expected=None)],
                "TOPO-16": [_option("TOPO-16", NOW - timedelta(days=1))]}
    coke = _row(products_page.html(_data(sourcing=sourcing)), "COKE-20")
    primary, alternate = coke.split("alternate:")
    assert "STALE" in primary and "STALE" not in alternate
    assert "24 a pack at price unknown" in _text(alternate) and "a unit" not in _text(alternate)
    items = products_page.alerts(_data(sourcing=sourcing))
    assert [i["level"] for i in items] == ["warn"] and "Coca-Cola 20 oz (COKE-20)" in items[0]["text"]


@pytest.mark.parametrize("synced, flagged", [(None, "never been synced"), (NOW - timedelta(hours=36, minutes=1), "over 36 hours"),
                                             (NOW - timedelta(hours=36), None)])
def test_the_catalog_age_is_flagged_at_the_top_by_get_catalogs_36_hour_rule(synced, flagged):
    data = _data(synced=synced)
    alert = products_page.catalog_alert(data)
    page = products_page.html(data)
    if flagged is None:
        assert alert is None
    else:
        assert alert["level"] == "bad" and flagged in alert["text"]
        assert page.index(flagged) < page.index('id="products"')


def test_every_value_is_escaped():
    evil = "<script>alert(1)</script>"
    data = _data(products=[{"sku": evil, "name": evil, "line": None, "flavor": None, "size": evil}],
                 sourcing={evil: [{**_option(evil, NOW), "store": evil, "checked_by": evil}]},
                 plan_slots=[_plan_slot(evil, evil)], closet=[{"product": evil, "units": 1, "unit_cost": evil}],
                 requests=[{"id": 1, "requested_at": NOW, "name": evil, "size": evil, "status": "pending", "sku": evil,
                            "decision_note": evil}])
    page = products_page.html(data, nav=((evil, evil),))
    assert "<script" not in page and "&lt;script&gt;" in page


def test_back_stock_before_the_opening_says_why_it_is_empty():
    data = _data()
    data["closet"] = _ok(None)
    page = products_page.html(data)
    assert "counts from the ledger&rsquo;s opening" in page and "× $1.45" not in _text(_row(page, "COKE-20"))


def test_the_plan_lede_says_which_plan_and_a_missing_panel_doesnt_take_the_page_down():
    assert "the latest day-one plan (latest, not picked)" in products_page.html(_data(picked=False))
    data = _data()
    data["plan"] = {"ok": False, "why": dashboard.PUBLIC_PANEL_ERROR}
    page = products_page.html(data)
    assert "The day-one plan can&rsquo;t be read" in page and "in plan" not in _row(page, "COKE-20")


def test_the_catalog_requests_are_the_dashboards_own_list():
    requests = [{"id": 2, "requested_at": NOW, "name": "Snapple Peach", "size": "16 oz", "status": "pending",
                 "sku": None, "decision_note": None},
                {"id": 1, "requested_at": NOW - timedelta(days=2), "name": "Red Bull", "size": "250 ml",
                 "status": "approved", "sku": "REDBULL-8", "decision_note": "Approved."}]
    data = _data(requests=requests)
    page = products_page.html(data)
    assert dashboard.catalog_requests_html(data["catalog_requests"], NOW) in page
    assert "2 requests: 1 pending, 1 approved, 0 rejected." in page and "SKU <code>REDBULL-8</code>" in page


def test_the_demo_route_draws_made_up_data_with_no_database(monkeypatch):
    monkeypatch.setattr(pages, "connect", lambda: pytest.fail("the demo opened a connection"))
    monkeypatch.setattr(pages, "our_slots", lambda conn: pytest.fail("the demo read the slots"))
    reply = pages.respond(products_page.DEMO_ROUTE + "/")
    assert reply.status == 200 and reply.content_type == pages.HTML and reply.csp == pages.CSP_PRODUCTS
    assert "script-src" not in reply.csp and "<script" not in reply.body
    assert "DEMO: made-up data, not our products" in reply.body and "<title>DEMO &middot; Products</title>" in reply.body
    # The made-up story: two day-one products found no seller, and Snapple's price is stale.
    assert "2 products in the day-one plan have no sourcing row" in reply.body
    assert "Snapple 16 oz (SNAPPLE-ORIGINAL-16BOT)" in reply.body
    assert demo.DEMO_PRODUCTS == products_page.DEMO_ROUTE
    assert f'href="{products_page.DEMO_ROUTE}"' in demo.page()


def test_a_live_render_failure_answers_the_generic_500_after_the_connection_closes(monkeypatch, capsys):
    from contextlib import contextmanager

    held = []

    @contextmanager
    def connection():
        held.append(True)
        try:
            yield object()
        finally:
            held.pop()

    def broken(*args, **kwargs):
        assert not held, "the page was drawn while it still held a connection"
        raise RuntimeError("db.internal.example user=reader")

    monkeypatch.setattr(pages, "configured", lambda: True)
    monkeypatch.setattr(pages, "_connection", connection)
    monkeypatch.setattr(pages, "_logged", {})
    monkeypatch.setattr(products_page, "read", lambda conn, public: _data())
    monkeypatch.setattr(products_page, "html", broken)
    reply = pages.respond(products_page.ROUTE)
    assert reply.status == 500 and pages.BROKEN in unescape(reply.body)
    assert "db.internal.example" not in reply.body and "RuntimeError" not in reply.body
    assert "RuntimeError" in capsys.readouterr().err


def test_a_broken_demo_file_never_shows_its_error(monkeypatch, capsys):
    monkeypatch.setattr(products_page, "DEMO_FILE", products_page.DEMO_FILE.with_name("missing.json"))
    monkeypatch.setattr(products_page.demo_data, "__defaults__", (products_page.DEMO_FILE,))
    reply = pages.respond(products_page.DEMO_ROUTE)
    assert reply.status == 500 and "missing.json" not in reply.body and "FileNotFoundError" in capsys.readouterr().err


# --- With the local database ------------------------------------------------------------------------

def _seed(conn, opened_at=None):
    """A synced catalog of three, sourcing for one, a picked plan with two, a closet of five."""
    synced = NOW - timedelta(hours=2)
    conn.execute("insert into catalog_products (sku, name, size, last_response) values"
                 " ('COKE-20', 'Coca-Cola 20 oz', '20 oz bottle', '{}'), ('TOPO-16', 'Topo Chico 16 oz', '16 oz glass', '{}'),"
                 " ('DEW-20', 'Mountain Dew 20 oz', '20 oz bottle', '{}')")
    conn.execute("insert into catalog_products (sku, name, size, last_response, delisted_at) values"
                 " ('OLD-12', 'Delisted Soda', '12 oz can', '{}', %s)", (NOW - timedelta(days=3),))
    conn.execute("insert into catalog_syncs (synced_at, product_count) values (%s, 3)", (synced,))
    conn.execute("insert into sourcing_options (sku, rank, store, units_per_pack, expected_cents, max_cents, checked_at,"
                 " checked_by) values ('COKE-20', 'primary', 'Walmart', 12, 1749, 2050, %s, 'kevin')",
                 (NOW - timedelta(days=9),))
    day_one = [{"slot": int(TEST_SLOTS[0]), "sku": "COKE-20", "units": 7, "price_cents": 250},
               {"slot": int(TEST_SLOTS[1]), "sku": "TOPO-16", "units": 7, "price_cents": 275}]
    conn.execute("insert into first_fill_plans (loaded_by, label, source_note, plan, projection, slots, picked)"
                 " values ('test', '6', 'test', %s, '{}', %s, true)", (Jsonb({"day_one": day_one}), list(TEST_SLOTS)))
    if opened_at is not None:
        ledger.record(conn, "opening", 60000, "opening capital", "test", at=opened_at)
    fulfilled(conn, "r1", TEST_SLOTS[0], "COKE-20", units_loaded=7, paid="17.49", at=NOW - timedelta(hours=3),
              to_back_stock=5)
    catalog_request(conn, "Red Bull", "approved", NOW - timedelta(days=1), "REDBULL-8", "Approved.")
    catalog_request(conn, "Snapple Peach", "pending", NOW)


def test_read_shows_the_catalog_sourcing_plan_and_closet_in_one_read_only_snapshot(conn, page_conn):
    _seed(conn, opened_at=NOW - timedelta(days=1))
    data = products_page.read(page_conn, NOW)
    assert [p["sku"] for p in data["catalog"]["data"]["products"]] == ["COKE-20", "DEW-20", "TOPO-16"]
    assert data["catalog"]["data"]["synced_at"] == NOW - timedelta(hours=2)
    assert data["plan"]["data"]["picked"] and set(products_page.planned(data)) == {"COKE-20", "TOPO-16"}
    assert data["closet"]["data"] == [{"product": "COKE-20", "units": 5, "unit_cost": "1.46"}]
    assert data["catalog_counts"]["data"]["by_status"]["approved"] == 1
    page = products_page.html(data)
    assert [i["level"] for i in products_page.alerts(data)] == ["bad", "warn"]
    assert 'class="gap"' in _row(page, "TOPO-16") and "STALE" in _row(page, "COKE-20")
    assert "5 × $1.46 each" in _text(_row(page, "COKE-20"))
    assert "OLD-12" not in page


def test_before_the_opening_the_closet_is_not_counted(conn, page_conn):
    _seed(conn)
    assert products_page.read(page_conn, NOW)["closet"] == {"ok": True, "data": None}


def test_a_missing_table_blanks_only_its_part(conn, page_conn):
    _seed(conn, opened_at=NOW - timedelta(days=1))
    conn.execute("drop table sourcing_options")
    data = products_page.read(page_conn, NOW, public=True)
    assert data["sourcing"] == {"ok": False, "why": dashboard.PUBLIC_PANEL_ERROR}
    assert data["catalog"]["ok"] and data["closet"]["ok"]


@pytest.mark.parametrize("table, panel", [("sourcing_options", "sourcing"), ("catalog_products", "catalog"),
                                          ("product_requests", "catalog_requests")])
def test_a_locked_table_blanks_only_its_part(schema_url, conn, page_conn, table, panel):
    """A long migration or a stuck write holding one table used to fail the whole read, and so the route
    (503). Now that panel gives up within about a second and says its fixed sentence; the rest is drawn."""
    from tests.conftest import open_in

    _seed(conn, opened_at=NOW - timedelta(days=1))
    page_conn.execute("set statement_timeout = 5000")          # the pages' own limit
    other = open_in(*schema_url)
    try:
        other.execute("begin")
        other.execute(f"lock table {table} in access exclusive mode")
        started = time.monotonic()
        data = products_page.read(page_conn, NOW, public=True)
        took = time.monotonic() - started
    finally:
        other.close()
    assert took < 3, took
    assert data[panel] == {"ok": False, "why": dashboard.PUBLIC_PANEL_ERROR}
    others = {"catalog", "sourcing", "plan", "closet", "catalog_requests"} - {panel}
    assert all(data[name]["ok"] for name in others), {name: data[name] for name in others}
    page = products_page.html(data)
    assert dashboard.PUBLIC_PANEL_ERROR in unescape(page)
    # The snapshot keeps its own limit, not the panel's short one.
    assert page_conn.execute("show statement_timeout").fetchone()[0] == "5s"


def test_the_worker_serves_the_page_behind_the_read_only_login(site, conn):
    _seed(conn, opened_at=NOW - timedelta(days=1))
    before = _written(conn)
    status, headers, page = _get(site, pages.PRODUCTS)
    assert status == 200 and headers["Content-Type"] == pages.HTML
    assert headers["Content-Security-Policy"] == pages.CSP_PRODUCTS and "<script" not in page
    assert "<code>COKE-20</code>" in page and '<a href="/dashboard">Agent dashboard</a>' in page
    assert _written(conn) == before
    dashboard_page = _get(site, pages.DASHBOARD)[2]
    assert f'<a href="{pages.PRODUCTS}">Products</a>' in dashboard_page
    assert f'<a href="{pages.PRODUCTS}">Every product we can order</a>' in dashboard_page


def test_the_local_dashboard_serves_the_page_beside_it(served, conn):
    _seed(conn, opened_at=NOW - timedelta(days=1))
    code, kind, body = get(served + "/products")
    assert (code, kind) == (200, "text/html; charset=utf-8") and "<code>TOPO-16</code>" in body
    assert 'href="/products"' in get(served + "/")[2]


def test_the_committed_demo_products_file_is_what_the_generator_makes(schema_url):
    from tests.conftest import open_in
    url, schema = schema_url
    with open_in(url, schema) as writer, read_only(url, schema) as page_reader:
        writer.execute("delete from slot_assignment")             # the fixture's stored assignment isn't the demo's
        make.make(writer, page_reader)
        text = products_page.render_file(make.make_products(writer, page_reader))
    assert text == products_page.DEMO_FILE.read_text(encoding="utf-8"), \
        "vending/demo/products.json is out of date: run python -m vending.demo.make (on a scratch local database)"


@pytest.fixture
def page_conn(schema_url):
    with read_only(*schema_url) as connection:
        yield connection
