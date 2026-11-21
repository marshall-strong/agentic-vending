"""Sourcing prices refreshed from purchase receipts (#202, `vending.receipt_prices`). Synthetic receipts shaped
like the bridge's, stored through `vending.purchase_requests.store`, on a throwaway schema."""

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("psycopg")

from tests.conftest import conn, open_in  # noqa: E402,F401 - migrated schema with a server assignment
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


def blocked(other, pid, timeout=10.0):
    """Wait until backend `pid` waits on a lock. `other` is a second connection."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if other.execute("select cardinality(pg_blocking_pids(%s)) > 0", (pid,)).fetchone()[0]:
            return True
        time.sleep(0.05)
    return False


@pytest.mark.parametrize("hand", [{"store": "Walmart", "most": 1200}, {"most": 1200}],
                         ids=["store switched", "cap lowered"])
def test_a_hand_change_made_while_a_receipt_is_applied_is_kept(coke, schema_url, hand):
    """Review finding 1: a person changes the row while the refresh runs. The refresh must not put back the
    old store or cap. The hand change is dated before the receipt, so only the row's identity and cap guard it."""
    table_row(coke, NOW - timedelta(days=9))
    fill(coke)
    errors: list[BaseException] = []

    def refresh():
        try:
            receipt_prices.apply(coke, "r1", NOW)
        except BaseException as error:  # noqa: BLE001 - reported below
            errors.append(error)

    worker = threading.Thread(target=refresh)
    with open_in(*schema_url) as other:
        with other.transaction():
            table_row(other, NOW - timedelta(days=8), note="hand change", **hand)
            worker.start()
            waited = blocked(other, coke.info.backend_pid)
        worker.join(30)
    assert waited, "the refresh never waited on the row the person was changing"
    assert not worker.is_alive() and errors == []
    o = row(coke)
    assert (o.store, o.max_cents, o.expected_cents, o.note) == (hand.get("store", "Costco"), 1200, 1149, "hand change")


def test_a_late_older_answer_does_not_undo_a_corrected_price(coke):
    """Review finding 2: a corrected $12.99 receipt, then a delayed $12.37 answer with an older updated_at."""
    table_row(coke, NOW - timedelta(days=9))
    submit(coke, "r1")
    corrected = fulfilled("r1", receipt(total_paid="12.99"), updated_at="2026-11-08T10:00:00Z")
    late = fulfilled("r1", receipt(total_paid="12.37"))           # updated_at 2026-11-07T16:30
    said: list[str] = []
    sync(coke, FakeBridge({"r1": corrected}), now=NOW, log=said.append)
    sync(coke, FakeBridge({"r1": late}), now=NOW, log=said.append)
    assert row(coke).expected_cents == 1299
    assert any("older than the one stored" in line for line in said), said


def test_a_correction_that_withdraws_the_purchase_puts_back_the_price_before_it(coke):
    """Review finding 3: the only purchase line becomes unavailable. The price that receipt set must not stay."""
    checked = NOW - timedelta(days=9)
    table_row(coke, checked)
    fill(coke)
    receipt_prices.apply(coke, "r1", NOW)
    store(coke, "r1", fulfilled("r1", receipt(total_paid="11.99")), NOW)     # a correction first
    receipt_prices.apply(coke, "r1", NOW)
    assert row(coke).expected_cents == 1199
    store(coke, "r1", fulfilled("r1", receipt(outcome="unavailable", packs_bought=0, units_bought=0,
                                              total_paid=None)), NOW)
    dry = receipt_prices.apply(coke, "r1", NOW, dry_run=True)
    assert row(coke).expected_cents == 1199
    applied = receipt_prices.apply(coke, "r1", NOW)
    assert applied == dry and len(applied.updated) == 1 and "put back $11.49" in applied.updated[0], applied
    o = row(coke)
    assert (o.expected_cents, o.checked_at, o.checked_by, o.note) == (1149, checked, "Kevin", "15 Nov check")
    assert receipt_prices.apply(coke, "r1", NOW) == receipt_prices.Applied()        # and it stays put back


def test_a_withdrawn_receipt_puts_back_an_earlier_receipts_price(coke):
    table_row(coke, NOW - timedelta(days=9))
    fill(coke, "r0", receipt(total_paid="12.10", resolved_at="2026-11-05T16:00:00Z"))
    fill(coke, "r1")
    receipt_prices.apply(coke, "r0", NOW)
    receipt_prices.apply(coke, "r1", NOW)
    store(coke, "r1", fulfilled("r1", receipt(sku="PEPSI20")), NOW)     # now a substitution: no COKE20 price
    receipt_prices.apply(coke, "r1", NOW)
    o = row(coke)
    assert (o.expected_cents, o.checked_by) == (1210, "receipt") and o.note.startswith("receipt r0:")


def test_a_withdrawn_receipt_with_no_earlier_price_on_record_leaves_the_price_unknown(coke):
    table_row(coke, NOW - timedelta(days=9))
    fill(coke)
    # A receipt price with no action_log entry behind it, so there is nothing to put back.
    coke.execute("update sourcing_options set expected_cents = 1237, checked_at = %s, checked_by = 'receipt',"
                 " note = 'receipt r1: paid $12.37 for 1 pack (line 0)'", (RESOLVED,))
    store(coke, "r1", fulfilled("r1", receipt(outcome="unavailable", packs_bought=0, units_bought=0,
                                              total_paid=None)), NOW)
    applied = receipt_prices.apply(coke, "r1", NOW)
    o = row(coke)
    assert o.expected_cents is None and o.max_cents == 1350 and "price unknown" in applied.updated[0], applied
    assert not o.note.startswith("receipt r1:")


@pytest.mark.parametrize("amount", [
    pytest.param("1e30", marks=pytest.mark.xfail(strict=True, reason="Sol finding 4, not fixed yet")),
    "0.001"], ids=["too large", "rounds to nothing"])
def test_an_amount_that_cannot_be_a_pack_price_is_skipped_and_the_sync_goes_on(coke, amount):
    """Review finding 4: a finite but absurd total_paid must not stop the sync before later requests."""
    table_row(coke, NOW - timedelta(days=9))
    submit(coke, "r1", days_ago=3)
    submit(coke, "r2", days_ago=2)
    said: list[str] = []
    report = sync(coke, FakeBridge({
        "r1": fulfilled("r1", receipt(total_paid=amount)),
        "r2": fulfilled("r2", receipt(total_paid="12.99", resolved_at="2026-11-08T16:00:00Z")),
    }), now=NOW, log=said.append)
    assert (report.checked, report.fulfilled, report.price_failures) == (2, 2, 0)
    assert row(coke).expected_cents == 1299
    assert any(line.startswith("r1 line 0: no price taken") for line in said), said


@pytest.mark.xfail(strict=True, reason="Sol finding 5, not fixed yet")
def test_a_correction_that_brings_a_new_problem_is_logged_once(coke):
    """Review finding 5: the request was already fulfilled, and its corrected receipt puts the pack above the max."""
    table_row(coke, NOW - timedelta(days=9))
    submit(coke, "r1")
    said: list[str] = []
    sync(coke, FakeBridge({"r1": fulfilled("r1", receipt())}), now=NOW, log=said.append)
    corrected = fulfilled("r1", receipt(total_paid="13.99"), updated_at="2026-11-08T10:00:00Z")
    sync(coke, FakeBridge({"r1": corrected}), now=NOW, log=said.append)
    sync(coke, FakeBridge({"r1": corrected}), now=NOW + timedelta(hours=1), log=said.append)
    assert sum("above the primary row's max $13.50" in line for line in said) == 1, said


@pytest.mark.parametrize("problem", ["future", "delisted"])
def test_a_dry_run_says_what_the_write_will_do(coke, problem):
    """Review finding 6: a dry run must refuse what the write refuses."""
    table_row(coke, NOW - timedelta(days=9))
    if problem == "future":
        fill(coke, "r1", receipt(resolved_at="2026-11-09T12:10:00Z"))     # 10 minutes after NOW
    else:
        fill(coke)
        coke.execute("update catalog_products set delisted_at = %s where sku = 'COKE20'", (NOW,))
    dry = receipt_prices.apply(coke, "r1", NOW, dry_run=True)
    real = receipt_prices.apply(coke, "r1", NOW)
    assert dry == real and real.updated == [] and len(real.skipped) == 1, (dry, real)
    assert row(coke).expected_cents == 1149


def test_the_listing_dates_a_price_by_the_eastern_day(coke, capsys):
    """Review finding 7: 11:30 PM ET on 27 Nov is 04:30 UTC on 28 Nov. The listing says 27 Nov."""
    late = datetime(2026, 11, 28, 4, 30, tzinfo=timezone.utc)
    sourcing.set_option(coke, sourcing.Option(
        sku="COKE20", rank="primary", store="Costco", units_per_pack=12, expected_cents=1149, max_cents=1350,
        url=None, pack_description=None, checked_at=late, checked_by="receipt", note="receipt r9: paid"),
        late + timedelta(hours=1))
    sourcing._show(coke, late + timedelta(hours=1))
    out = capsys.readouterr().out
    assert "checked 2026-11-27" in out, out

