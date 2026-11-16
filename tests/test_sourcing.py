"""Our sourcing table: where to buy each product, checked against the purchase API's limits and logged.
The parsing and validation tests need no database."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from vending import planner, sourcing
from vending.sourcing import Option, SourcingError

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "db" / "sourcing" / "2026-11-11-amazon.csv"
NOW = datetime(2026, 11, 15, 17, tzinfo=timezone.utc)
RED_BULL = "REDBULL-ORIGINAL-8_4CAN"
URL = "https://www.amazon.com/dp/B003PFPFIE"


def option(**changes) -> Option:
    base = dict(sku=RED_BULL, rank="primary", store="Amazon", units_per_pack=24, expected_cents=3499,
                max_cents=4050, url=URL, pack_description="24 x 8.4 oz cans",
                checked_at=NOW - timedelta(days=4), checked_by="Meaghan")
    return Option(**{**base, **changes})


# --- Parsing and validation (no database) ---------------------------------------------------------

@pytest.mark.parametrize("price, expected_max", [
    (3499, 4050), (2157, 2500), (4924, 5700), (5495, 6350), (5099, 5900),   # the 11 Nov price list's own maxes
    (1000, 1150),                                                            # exactly $11.50 stays
])
def test_the_default_max_is_price_plus_15_percent_up_to_50_cents(price, expected_max):
    assert sourcing.default_max(price) == expected_max


def test_prices_are_exact_cents():
    assert sourcing.cents("34.99", "price") == 3499
    assert sourcing.cents("$7.68", "price") == 768
    for bad in ("34.999", "abc", "0", "-1.00", ""):
        with pytest.raises(SourcingError):
            sourcing.cents(bad, "price")


def test_a_checked_date_is_noon_eastern_that_day():
    assert sourcing.checked_at("2026-11-11", NOW) == datetime(2026, 11, 11, 17, tzinfo=timezone.utc)
    assert sourcing.checked_at(None, NOW) == NOW
    morning = datetime(2026, 11, 15, 14, tzinfo=timezone.utc)          # 9 AM Eastern
    assert sourcing.checked_at("2026-11-15", morning) == morning        # today, before noon: now, not the future
    assert sourcing.problems(option(checked_at=sourcing.checked_at("2026-11-15", morning)), morning) == []
    evening = datetime(2026, 11, 15, 23, tzinfo=timezone.utc)          # 6 PM Eastern: today's noon has passed
    assert sourcing.checked_at("2026-11-15", evening) == datetime(2026, 11, 15, 17, tzinfo=timezone.utc)
    for later in ("2026-11-16", "2027-11-15"):                          # a later day, or a mistyped year, stays future
        assert any("in the future" in p
                   for p in sourcing.problems(option(checked_at=sourcing.checked_at(later, morning)), morning))
    with pytest.raises(SourcingError, match="date like"):
        sourcing.checked_at("11/11/2026", NOW)


def test_a_good_row_has_no_problems():
    assert sourcing.problems(option(), NOW) == []
    assert sourcing.problems(option(store="Costco", url=None), NOW) == []


@pytest.mark.parametrize("changes, why", [
    ({"store": "Target"}, "not one of"),
    ({"url": None}, "needs its link"),
    ({"url": "https://www.walmart.com/ip/1"}, "not an Amazon link"),
    ({"units_per_pack": 0}, "units per pack"),
    ({"units_per_pack": 61}, "units per pack"),
    ({"max_cents": 20001}, "at most"),
    ({"max_cents": 3000}, "below the price"),
    ({"pack_description": "x" * 101}, "pack description"),
    ({"checked_by": " "}, "your name"),
    ({"checked_at": NOW + timedelta(days=1)}, "in the future"),
    ({"rank": "backup"}, "primary or alternate"),
])
def test_a_row_the_purchase_api_would_refuse_is_refused_here(changes, why):
    assert any(why in p for p in sourcing.problems(option(**changes), NOW))


def test_staleness_is_a_week():
    assert not option(checked_at=NOW - timedelta(days=7)).is_stale(NOW)
    assert option(checked_at=NOW - timedelta(days=8)).is_stale(NOW)


def test_the_seed_file_parses_and_matches_its_own_maxes():
    rows = sourcing.read_csv(SEED, "Meaghan", NOW)
    assert len(rows) == 5 and {o.store for o in rows} == {"Amazon"}
    assert all(o.max_cents == sourcing.default_max(o.expected_cents) for o in rows)
    assert all(o.checked_at.date().isoformat() == "2026-11-11" for o in rows)


def test_a_bad_csv_reports_every_bad_line_and_imports_nothing(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("sku,store,units_per_pack,price,url\n"
                    f"{RED_BULL},Amazon,24,34.99,{URL}\n"
                    "A,Amazon,24,10.00,\n"
                    "B,Walmart,0,10.00,\n", encoding="utf-8")
    with pytest.raises(SourcingError) as error:
        sourcing.read_csv(path, "Meaghan", NOW)
    assert "line 3" in str(error.value) and "line 4" in str(error.value) and "line 2" not in str(error.value)


# --- Storing it -----------------------------------------------------------------------------------

def add_catalog(conn, *skus):
    for sku in skus:
        conn.execute("insert into catalog_products (sku, name, last_response) values (%s, %s, '{}')", (sku, sku))


def test_set_adds_then_replaces_and_logs_what_it_replaced(conn):
    add_catalog(conn, RED_BULL)
    assert sourcing.set_option(conn, option(), NOW) == "added"
    assert sourcing.set_option(conn, option(expected_cents=3699, max_cents=4300, checked_by="Kevin"), NOW) == "replaced"
    [stored] = sourcing.options(conn)
    assert (stored.expected_cents, stored.max_cents, stored.checked_by) == (3699, 4300, "Kevin")
    logs = conn.execute("select tool, arguments, detail from action_log order by id").fetchall()
    assert [t for t, _, _ in logs] == ["sourcing.set", "sourcing.set"]
    assert logs[1][1]["old"]["expected_cents"] == 3499 and logs[1][1]["by"] == "Kevin"
    assert "(was Amazon, 24 a pack, $34.99 (max $40.50))" in logs[1][2]


def test_a_product_not_in_the_catalog_is_refused(conn):
    with pytest.raises(SourcingError, match="not in our catalog"):
        sourcing.set_option(conn, option(), NOW)
    assert sourcing.options(conn) == []


def test_an_alternate_needs_a_primary_and_a_primary_cant_leave_an_alternate_behind(conn):
    add_catalog(conn, RED_BULL)
    walmart = option(rank="alternate", store="Walmart", url=None, units_per_pack=12, expected_cents=1898, max_cents=2200)
    with pytest.raises(SourcingError, match="no primary"):
        sourcing.set_option(conn, walmart, NOW)
    sourcing.set_option(conn, option(), NOW)
    sourcing.set_option(conn, walmart, NOW)
    with pytest.raises(SourcingError, match="still has an alternate"):
        sourcing.remove(conn, RED_BULL, "primary", "Meaghan")
    sourcing.remove(conn, RED_BULL, "alternate", "Meaghan")
    sourcing.remove(conn, RED_BULL, "primary", "Meaghan")
    assert sourcing.options(conn) == []
    assert conn.execute("select count(*) from action_log where tool = 'sourcing.remove'").fetchone() == (2,)


def test_a_delisted_product_cant_be_set_and_its_old_row_never_reaches_the_planner(conn):
    add_catalog(conn, RED_BULL)
    sourcing.set_option(conn, option(), NOW)
    assert [s.product for s in sourcing.table(conn)] == [RED_BULL]
    conn.execute("update catalog_products set delisted_at = now() where sku = %s", (RED_BULL,))
    assert sourcing.table(conn) == ()
    with pytest.raises(SourcingError, match="delisted"):
        sourcing.set_option(conn, option(expected_cents=3599, max_cents=4150), NOW)


def test_the_database_refuses_an_amazon_row_without_a_link(conn):
    import psycopg
    add_catalog(conn, RED_BULL)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("insert into sourcing_options (sku, rank, store, units_per_pack, max_cents, checked_at,"
                      " checked_by) values (%s, 'primary', 'Amazon', 24, 4050, now(), 'x')", (RED_BULL,))


def test_the_table_is_what_the_planner_takes_and_it_refuses_nothing(conn):
    add_catalog(conn, *(o.sku for o in sourcing.read_csv(SEED, "Meaghan", NOW)))
    for o in sourcing.read_csv(SEED, "Meaghan", NOW):
        sourcing.set_option(conn, o, NOW)
    sourcing.set_option(conn, option(rank="alternate", store="Costco", url=None, units_per_pack=24,
                                     expected_cents=3299, max_cents=3800), NOW)
    table = sourcing.table(conn)
    assert len(table) == 5
    red_bull = next(s for s in table if s.product == RED_BULL)
    assert red_bull.primary.max_price_per_pack == 4050 and red_bull.alternate.store == "Costco"

    refusals: list[planner.Refusal] = []
    sources = planner._Sources((), table, planner.Config(), refusals)
    for s in table:
        assert sources.get(s.product) is not None
    assert refusals == []


# --- The command ----------------------------------------------------------------------------------

@pytest.fixture
def command(schema_url, monkeypatch):
    from tests.conftest import open_in
    from vending import db

    monkeypatch.setattr(db, "connect", lambda: open_in(*schema_url))
    with open_in(*schema_url) as conn:
        add_catalog(conn, *(o.sku for o in sourcing.read_csv(SEED, "x", NOW)))
    return schema_url


def test_import_then_set_then_show(command, capsys):
    assert sourcing.main(["show"]) == 0
    assert "No sourcing rows yet" in capsys.readouterr().out
    assert sourcing.main(["import", str(SEED), "--by", "Meaghan"]) == 0
    assert capsys.readouterr().out.startswith("imported 5 rows")
    assert sourcing.main(["set", RED_BULL, "--store", "Amazon", "--pack", "24", "--price", "36.49",
                          "--url", URL, "--by", "Meaghan"]) == 0
    out = capsys.readouterr().out
    assert "replaced" in out and "max $42.00" in out                 # 36.49 + 15% = 41.96, up to 42.00
    assert sourcing.main([]) == 0
    out = capsys.readouterr().out
    assert out.count("checked 2026-11-11") == 4 and "$36.49" in out   # the seed's dates; Red Bull rechecked now


def test_set_refuses_with_exit_1_and_changes_nothing(command, capsys):
    assert sourcing.main(["set", RED_BULL, "--store", "Amazon", "--pack", "24", "--price", "34.99",
                          "--by", "Meaghan"]) == 1
    assert "needs its link" in capsys.readouterr().err
    assert sourcing.main(["show"]) == 0
    assert "No sourcing rows yet" in capsys.readouterr().out


def test_a_bad_import_changes_nothing(command, tmp_path, capsys):
    bad = tmp_path / "bad.csv"
    bad.write_text(SEED.read_text(encoding="utf-8") + "NOT-IN-CATALOG,primary,Costco,24,10.00,,,,2026-11-11,\n"
                   "ALSO-MISSING,primary,Costco,24,10.00,,,,2026-11-11,\n", encoding="utf-8")
    assert sourcing.main(["import", str(bad), "--by", "Meaghan"]) == 1
    err = capsys.readouterr().err
    assert "Not in our catalog copy" in err and "ALSO-MISSING, NOT-IN-CATALOG" in err   # every one, not the first
    sourcing.main(["show"])
    assert "No sourcing rows yet" in capsys.readouterr().out
