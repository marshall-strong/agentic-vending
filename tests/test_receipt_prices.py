"""Sourcing prices refreshed from purchase receipts (#202, `vending.receipt_prices`). Synthetic receipts shaped
like the bridge's, stored through `vending.purchase_requests.store`, on a throwaway schema."""

from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("psycopg")

from tests.conftest import conn  # noqa: E402,F401 - migrated schema with a server assignment
from tests.test_sync_requests import FakeBridge, fulfilled, receipt, submit  # noqa: E402
from vending import receipt_prices, sourcing  # noqa: E402
from vending.purchase_requests import store  # noqa: E402
from vending.sync_requests import sync  # noqa: E402

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
RESOLVED = datetime(2026, 11, 7, 16, 0, tzinfo=timezone.utc)     # the receipts' resolved_at


def catalog(conn, *skus):
    for sku in skus:
        conn.execute("insert into catalog_products (sku, name, size, last_response) values (%s, %s, '20 oz bottle', '{}')",
                     (sku, sku))


def table_row(conn, checked, *, sku="COKE20", rank="primary", store="Costco", pack=12, expected=1149, most=1350,
              by="Kevin", note="15 Nov check"):
    sourcing.set_option(conn, sourcing.Option(
        sku=sku, rank=rank, store=store, units_per_pack=pack, expected_cents=expected, max_cents=most, url=None,
        pack_description="12 x 20 oz bottles", checked_at=checked, checked_by=by, note=note), NOW)


def row(conn, rank="primary"):
    return sourcing._row(conn, "COKE20", rank)


def logged(conn):
    return conn.execute("select arguments->>'by', detail from action_log where tool = 'sourcing.set'"
                        " order by id").fetchall()


@pytest.fixture
def coke(conn):  # noqa: F811
    catalog(conn, "COKE20", "PEPSI20")
    return conn


def fill(conn, request_id="r1", *receipts):
    """A fulfilled request with these receipt lines (default: one Costco 12-pack of COKE20 for $12.37)."""
    submit(conn, request_id)
    store(conn, request_id, fulfilled(request_id, *(receipts or (receipt(),))), NOW)


def test_a_fulfilled_receipt_refreshes_the_matching_row(coke):
    table_row(coke, NOW - timedelta(days=9))
    fill(coke)
    applied = receipt_prices.apply(coke, "r1", NOW)
    assert len(applied.updated) == 1 and applied.skipped == []
    o = row(coke)
    assert (o.expected_cents, o.checked_at, o.checked_by) == (1237, RESOLVED, "receipt")
    assert o.from_receipt and o.note.startswith("receipt r1: paid $12.37 for 1 pack")
    # Everything a person chose stays: store, pack, max, description.
    assert (o.store, o.units_per_pack, o.max_cents, o.pack_description) == ("Costco", 12, 1350, "12 x 20 oz bottles")
    assert not o.is_stale(NOW)
    # The old values are kept in action_log, as for `sourcing set`.
    by, detail = logged(coke)[-1]
    assert by == "receipt" and "was Costco, 12 a pack, $11.49" in detail


def test_several_packs_and_lines_give_one_pack_price(coke):
    table_row(coke, NOW - timedelta(days=9))
    fill(coke, "r1", receipt(packs_bought=2, units_bought=24, total_paid="24.75"),
         receipt(packs_bought=1, units_bought=12, total_paid="12.36"))
    receipt_prices.apply(coke, "r1", NOW)
    # 37.11 for 3 packs is 12.37 a pack.
    o = row(coke)
    assert o.expected_cents == 1237 and "for 3 packs (lines 0, 1)" in o.note


def test_a_pack_price_rounds_half_up_to_the_cent(coke):
    table_row(coke, NOW - timedelta(days=9))
    fill(coke, "r1", receipt(packs_bought=2, units_bought=24, total_paid="24.75"))
    receipt_prices.apply(coke, "r1", NOW)
    assert row(coke).expected_cents == 1238          # 12.375


def test_a_newer_hand_price_is_not_overwritten(coke):
    table_row(coke, NOW - timedelta(days=1))          # checked after the receipt
    fill(coke)
    applied = receipt_prices.apply(coke, "r1", NOW)
    assert applied.updated == [] and applied.skipped == [] and "newer than r1" in applied.kept[0]
    o = row(coke)
    assert (o.expected_cents, o.checked_by) == (1149, "Kevin")


def test_an_older_receipt_read_after_a_newer_one_changes_nothing(coke):
    table_row(coke, NOW - timedelta(days=9))
    fill(coke, "new", receipt(total_paid="12.99", resolved_at="2026-11-08T16:00:00Z"))
    fill(coke, "old")
    receipt_prices.apply(coke, "new", NOW)
    receipt_prices.apply(coke, "old", NOW)
    assert row(coke).expected_cents == 1299


def test_a_resync_is_idempotent(coke):
    table_row(coke, NOW - timedelta(days=9))
    submit(coke, "r1")
    bridge = FakeBridge({"r1": fulfilled("r1", receipt())})
    lines: list[str] = []
    first = sync(coke, bridge, now=NOW, log=lines.append)
    before, logs = row(coke), len(logged(coke))
    again = sync(coke, bridge, now=NOW + timedelta(hours=1), log=lines.append)
    assert (first.prices_updated, again.prices_updated) == (1, 0)
    assert row(coke) == before and len(logged(coke)) == logs
    assert sum("sourcing price from receipt" in line for line in lines) == 1
    assert receipt_prices.apply(coke, "r1", NOW).updated == []


def test_a_corrected_receipt_replaces_the_price_it_set(coke):
    table_row(coke, NOW - timedelta(days=9))
    fill(coke)
    receipt_prices.apply(coke, "r1", NOW)
    store(coke, "r1", fulfilled("r1", receipt(total_paid="11.99")), NOW)
    assert len(receipt_prices.apply(coke, "r1", NOW).updated) == 1
    assert row(coke).expected_cents == 1199


@pytest.mark.parametrize("changes, why", [
    ({"sku": "PEPSI20"}, "a substitution: we asked for COKE20 as the primary, the receipt says PEPSI20"),
    ({"packs_bought": 0, "units_bought": 0}, "zero packs bought"),
    ({"units_bought": 10}, "10 units bought isn't 1 packs of 12"),
    ({"total_paid": None}, "no amount paid"),
    ({"sku": None}, "names no SKU"),
])
def test_an_unmappable_line_is_skipped_and_logged(coke, changes, why):
    table_row(coke, NOW - timedelta(days=9))
    submit(coke, "r1")
    lines: list[str] = []
    sync(coke, FakeBridge({"r1": fulfilled("r1", receipt(**changes))}), now=NOW, log=lines.append)
    o = row(coke)
    assert (o.expected_cents, o.checked_by) == (1149, "Kevin")
    assert any(why in line and "r1 line 0: no price taken" in line for line in lines), lines
    # Named once: a re-read doesn't log it again.
    sync(coke, FakeBridge({"r1": fulfilled("r1", receipt(**changes))}), now=NOW, log=lines.append)
    assert sum(why in line for line in lines) == 1


def test_no_row_for_that_store_and_pack_is_skipped_not_invented(coke):
    table_row(coke, NOW - timedelta(days=9), store="Walmart")
    fill(coke)
    applied = receipt_prices.apply(coke, "r1", NOW)
    assert applied.updated == [] and "no row for that store and pack" in applied.skipped[0]
    assert row(coke).store == "Walmart" and row(coke).expected_cents == 1149


def test_a_price_above_the_rows_max_waits_for_a_person(coke):
    table_row(coke, NOW - timedelta(days=9), most=1200)
    fill(coke)
    applied = receipt_prices.apply(coke, "r1", NOW)
    assert applied.updated == [] and "above the primary row's max $12.00" in applied.skipped[0]
    assert row(coke).expected_cents == 1149


def test_closet_loads_and_unavailable_lines_are_not_prices(coke):
    table_row(coke, NOW - timedelta(days=9))
    fill(coke, "r1", receipt(outcome="unavailable", packs_bought=0, units_bought=0, total_paid=None))
    applied = receipt_prices.apply(coke, "r1", NOW)
    assert (applied.updated, applied.skipped) == ([], [])


def test_the_alternate_row_takes_an_alternate_purchase(coke):
    table_row(coke, NOW - timedelta(days=9))
    table_row(coke, NOW - timedelta(days=9), rank="alternate", store="Walmart", expected=1199, most=1400)
    alt = {"sku": "COKE20", "store": "Walmart", "packs": 1, "units_per_pack": 12, "max_price_per_pack": "14.00",
           "sale_price": "2.50", "change_product": False}
    submit(coke, "r1")
    answer = fulfilled("r1", receipt(outcome="alternate", store="Walmart", total_paid="12.48"))
    answer["lines"][0]["alternate"] = alt
    store(coke, "r1", answer, NOW)
    receipt_prices.apply(coke, "r1", NOW)
    assert row(coke, "alternate").expected_cents == 1248 and row(coke).expected_cents == 1149


def test_a_pending_request_changes_nothing(coke):
    table_row(coke, NOW - timedelta(days=9))
    submit(coke, "r1")
    assert receipt_prices.apply(coke, "r1", NOW) == receipt_prices.Applied()


def test_applying_every_stored_receipt_and_a_dry_run_that_changes_nothing(coke):
    table_row(coke, NOW - timedelta(days=9))
    fill(coke)
    seen: list[str] = []
    dry = receipt_prices.apply_all(coke, NOW, dry_run=True, log=seen.append)
    assert len(dry.updated) == 1 and row(coke).expected_cents == 1149
    assert len(receipt_prices.apply_all(coke, NOW, log=seen.append).updated) == 1
    assert row(coke).expected_cents == 1237


def test_nobody_can_sign_a_hand_price_as_a_receipt():
    fields = {"sku": "COKE20", "store": "Costco", "units_per_pack": "12", "price": "11.49"}
    with pytest.raises(sourcing.SourcingError, match="kept for prices taken from receipts"):
        sourcing._from_fields(fields, "Receipt", NOW)

