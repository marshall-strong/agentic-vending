"""Sourcing prices refreshed from purchase receipts (#202, `vending.receipt_prices`). Synthetic receipts shaped
like the bridge's, stored through `vending.purchase_requests.store`, on a throwaway schema."""

import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

pytest.importorskip("psycopg")

from tests.conftest import conn, open_in  # noqa: E402,F401 - migrated schema with a server assignment
from tests.test_sync_requests import (  # noqa: E402
    BODY, CLOSED, OPENED, FakeBridge, door_row, fulfilled, later, pending, receipt, submit)
from vending import receipt_prices, sourcing  # noqa: E402
from vending.purchase_requests import record, store  # noqa: E402
from vending.stock import stock  # noqa: E402
from vending.sync_requests import sync  # noqa: E402

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
RESOLVED = datetime(2026, 11, 7, 16, 0, tzinfo=timezone.utc)     # the receipts' resolved_at


def catalog(conn, *skus):
    for sku in skus:
        conn.execute("insert into catalog_products (sku, name, size, last_response) values (%s, %s, '20 oz bottle', '{}')",
                     (sku, sku))


def table_row(conn, checked, *, sku="COKE20", rank="primary", store="Costco", pack=12, expected=1149, most=1350,
              by="Kevin", note="15 Nov check", url=None):
    sourcing.set_option(conn, sourcing.Option(
        sku=sku, rank=rank, store=store, units_per_pack=pack, expected_cents=expected, max_cents=most, url=url,
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
    fill(coke, "r1", receipt(packs_bought=2, units_bought=24, total_paid="24.00"),
         receipt(packs_bought=1, units_bought=12, total_paid="13.20"))
    receipt_prices.apply(coke, "r1", NOW)
    # Weighted by packs: 37.20 for 3 packs is 12.40 a pack. The mean of the lines' pack prices (12.00 and 13.20)
    # would be 12.60, so this tells the two apart (round 6, finding 4).
    o = row(coke)
    assert o.expected_cents == 1240 and "for 3 packs (lines 0, 1)" in o.note


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
    store(coke, "r1", fulfilled("r1", receipt(total_paid="11.99"), updated_at="2026-11-08T10:00:00Z"), NOW)
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
    sync(coke, FakeBridge({"r1": late}), now=NOW + timedelta(hours=1), log=said.append)
    assert sum("older than the one stored" in line for line in said) == 1, said      # named once


@pytest.mark.parametrize("amount", ["1e30", "0.001", "1e1000000", "1e-20000"],
                         ids=["too large", "rounds to nothing", "past Decimal's range", "past numeric's scale"])
def test_an_amount_that_cannot_be_a_pack_price_is_skipped_and_the_sync_goes_on(coke, amount):
    """Review finding 4: a finite but absurd total_paid must not stop the sync before later requests, and the
    fill still counts: only the amount is unknown."""
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


def test_a_database_error_while_pricing_one_receipt_is_isolated_and_both_receipts_are_kept(coke, monkeypatch):
    """Sol confirmed in round 2: a SQL failure inside pricing r1 rolls back only r1's pricing. Both receipts are
    stored, r2's price is still taken, and the failure is counted for the next run."""
    table_row(coke, NOW - timedelta(days=9))
    submit(coke, "r1", days_ago=3)
    submit(coke, "r2", days_ago=2)
    real = receipt_prices._apply_group

    def failing(conn, request_id, *rest):
        real(conn, request_id, *rest)
        if request_id == "r1":
            conn.execute("select 1 / 0")          # after r1's price was written: it must be rolled back

    monkeypatch.setattr(receipt_prices, "_apply_group", failing)
    said: list[str] = []
    report = sync(coke, FakeBridge({
        "r1": fulfilled("r1", receipt(total_paid="12.10", resolved_at="2026-11-06T16:00:00Z")),
        "r2": fulfilled("r2", receipt(total_paid="12.99", resolved_at="2026-11-08T16:00:00Z")),
    }), now=NOW, log=said.append)
    assert (report.checked, report.fulfilled, report.price_failures) == (2, 2, 1), said
    assert coke.execute("select count(*) from purchase_lines").fetchone() == (2,)
    o = row(coke)
    assert o.expected_cents == 1299 and o.note.startswith("receipt r2:")
    assert coke.execute("select count(*) filter (where arguments->'new'->>'note' like 'receipt r1:%'),"
                        " count(*) filter (where arguments->'new'->>'note' like 'receipt r2:%')"
                        " from action_log where tool = 'sourcing.set'").fetchone() == (0, 1)
    assert any("r1: receipt prices not applied (DivisionByZero)" in line for line in said), said


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


# Sol review, round 2.

def quietly(_):
    pass


def test_a_paused_apply_does_not_put_back_a_price_a_later_sync_corrected(coke, schema_url, monkeypatch):
    """Round 2, finding 2: an apply reads the $12.39 receipt and pauses. A sync stores the corrected $12.99 and
    applies it. The paused apply must not then write $12.39 back."""
    table_row(coke, NOW - timedelta(days=9))
    submit(coke, "r1")
    store(coke, "r1", fulfilled("r1", receipt(total_paid="12.39")), NOW)
    read, go = threading.Event(), threading.Event()
    real = receipt_prices._paid_groups

    def paused(conn, request_id, result):
        groups = real(conn, request_id, result)
        if threading.current_thread().name == "paused":
            read.set()
            go.wait(30)
        return groups

    monkeypatch.setattr(receipt_prices, "_paid_groups", paused)
    errors: list[BaseException] = []

    def run(work):
        def wrapped():
            try:
                work()
            except BaseException as error:  # noqa: BLE001 - reported below
                errors.append(error)
        return wrapped

    first = threading.Thread(target=run(lambda: receipt_prices.apply(coke, "r1", NOW)), name="paused")
    first.start()
    assert read.wait(30)
    corrected = fulfilled("r1", receipt(total_paid="12.99"), updated_at="2026-11-08T10:00:00Z")
    with open_in(*schema_url) as other, open_in(*schema_url) as watch:
        second = threading.Thread(target=run(lambda: sync(other, FakeBridge({"r1": corrected}), now=NOW, log=quietly)))
        second.start()
        blocked(watch, other.info.backend_pid, timeout=3)
        go.set()
        first.join(30)
        second.join(30)
    assert errors == [] and not first.is_alive() and not second.is_alive()
    o = row(coke)
    assert o.expected_cents == 1299 and o.note.startswith("receipt r1: paid $12.99"), o


LISTING_A = "https://www.amazon.com/dp/B000000AAA"
LISTING_B = "https://www.amazon.com/dp/B000000BBB"


def amazon_fill(conn, request_id="r1", url=LISTING_A):
    """A fulfilled request that asked for COKE20 from the Amazon listing `url`, $12.37 paid."""
    submit(conn, request_id)
    answer = fulfilled(request_id, receipt(store="Amazon"))
    answer["lines"][0]["primary"] = {**BODY["lines"][0]["primary"], "store": "Amazon", "url": url}
    store(conn, request_id, answer, NOW)


@pytest.mark.parametrize("bought, takes", [
    (LISTING_A, False),
    (LISTING_B, True),
    ("https://www.amazon.com/Coca-Cola-Bottles/dp/B000000BBB?th=1", True),
    ("https://www.amazon.com/gp/product/B000000BBB/ref=ppx", True),
    # Round 3, finding 6: an ASIN only from a product path on an amazon.com host.
    ("https://www.amazon.com/s?k=/dp/B000000BBB", False),
    ("https://www.amazon.com/s?k=x#/dp/B000000BBB", False),
    ("https://example.com/dp/B000000BBB", False),
    ("https://www.amazon.com.example.com/dp/B000000BBB", False),
    ("https://www.amazon.com@example.com/dp/B000000BBB", False),
], ids=["another listing", "this listing", "this listing, another link", "this listing, gp/product",
        "a search link", "a fragment", "another site", "a lookalike host", "a host after userinfo"])
def test_an_amazon_receipt_prices_only_its_own_listing(coke, bought, takes):
    """Round 2, finding 4: the row now holds replacement listing B at a $13.49 hand price. A receipt from old listing
    A must not put its price on B. The listing is the ASIN when both links have one, else the link as stored."""
    table_row(coke, datetime(2026, 11, 6, 12, tzinfo=timezone.utc), store="Amazon", url=LISTING_B, expected=1349,
              most=1500)
    amazon_fill(coke, url=bought)
    applied = receipt_prices.apply(coke, "r1", NOW)
    o = row(coke)
    assert o.url == LISTING_B
    if takes:
        assert o.expected_cents == 1237 and len(applied.updated) == 1, applied
    else:
        assert (o.expected_cents, o.checked_by) == (1349, "Kevin"), applied
        assert "no row for that store, pack and Amazon listing" in applied.skipped[0], applied


def test_an_amazon_listing_switched_while_a_receipt_is_applied_is_kept(coke, schema_url):
    """Round 2, finding 4, under the write lock: a person switches the row from listing A to B while the receipt for
    A is applied. The hand change is dated before the receipt, so only the listing check guards it."""
    table_row(coke, NOW - timedelta(days=9), store="Amazon", url=LISTING_A, most=1500)
    amazon_fill(coke)
    errors: list[BaseException] = []

    def refresh():
        try:
            receipt_prices.apply(coke, "r1", NOW)
        except BaseException as error:  # noqa: BLE001 - reported below
            errors.append(error)

    worker = threading.Thread(target=refresh)
    with open_in(*schema_url) as other:
        with other.transaction():
            table_row(other, datetime(2026, 11, 6, 12, tzinfo=timezone.utc), store="Amazon", url=LISTING_B,
                      expected=1349, most=1500, note="new listing")
            worker.start()
            waited = blocked(other, coke.info.backend_pid)
        worker.join(30)
    assert waited and not worker.is_alive() and errors == []
    o = row(coke)
    assert (o.url, o.expected_cents, o.note) == (LISTING_B, 1349, "new listing")


def test_apply_all_dry_run_simulates_each_receipt_on_the_one_before(coke):
    """Round 2, finding 7: r0 (newer, $12.99) then r1 (older, $12.37), then r2 ($12.50, newest). Each dry-run step
    must see the rows as the steps before left them: the same sentences as the real run."""
    table_row(coke, NOW - timedelta(days=9))
    fill(coke, "r0", receipt(total_paid="12.99", resolved_at="2026-11-08T16:00:00Z"))
    fill(coke, "r1")
    fill(coke, "r2", receipt(total_paid="12.50", resolved_at="2026-11-08T18:00:00Z"))
    dry = receipt_prices.apply_all(coke, NOW, dry_run=True, log=quietly)
    assert row(coke).expected_cents == 1149
    real = receipt_prices.apply_all(coke, NOW, log=quietly)
    assert dry == real, (dry, real)
    assert len(real.updated) == 2 and "(was $12.99" in real.updated[1]


# Sol review, round 3. A fulfilled receipt is never undone automatically; a person checks any later rejection.

def stocked(conn):
    """What the stock count reads for r1: its status, and its lines' units bought and loaded."""
    status = conn.execute("select status from purchase_requests where request_id = 'r1'").fetchone()[0]
    lines = conn.execute("select line_no, units_bought, units_loaded, total_paid from purchase_lines"
                         " where request_id = 'r1' order by line_no").fetchall()
    return status, lines


@pytest.mark.parametrize("updated_at", [None, "2026-11-08T10:00:00Z"], ids=["unversioned", "versioned"])
def test_a_rejection_after_a_fill_keeps_the_receipt_its_stock_and_its_price(coke, updated_at):
    """Round 3, findings 1, 2, 3, 5 and 9 (by design): the bridge documents rejection only before fulfillment. A
    rejected answer after a fill, versioned or not, is never stored over the receipt and takes back no price. A
    person checks with the instructors; the warning comes once per answer, not on every hourly read."""
    table_row(coke, NOW - timedelta(days=9))
    submit(coke, "r1")
    said: list[str] = []
    sync(coke, FakeBridge({"r1": fulfilled("r1", receipt())}), now=NOW, log=said.append)
    before, price, logs = stocked(coke), row(coke), len(logged(coke))
    rejected = {**pending("r1"), "status": "rejected", "decision_note": "withdrawn"}
    if updated_at:
        rejected["updated_at"] = updated_at
    for hours in (1, 2, 3):
        report = sync(coke, FakeBridge({"r1": rejected}), now=NOW + timedelta(hours=hours), log=said.append)
        assert (report.fulfilled, report.rejected, report.price_failures) == (1, 0, 0)
    assert stocked(coke) == before and before[0] == "fulfilled"
    assert row(coke) == price and price.expected_cents == 1237 and len(logged(coke)) == logs
    warned = [line for line in said if "now says rejected" in line]
    assert len(warned) == 1 and "check with the instructors" in warned[0], said
    assert "Kept the receipt, its stock and its prices" in warned[0]
    # A different answer is a new warning; apply_all reads fulfilled receipts only and changes nothing.
    sync(coke, FakeBridge({"r1": {**rejected, "decision_note": "duplicate"}}), now=NOW + timedelta(hours=4),
         log=said.append)
    assert sum("now says rejected" in line for line in said) == 2
    assert receipt_prices.apply_all(coke, NOW, log=quietly).updated == []


def test_a_receipt_with_no_updated_at_after_a_versioned_one_is_ignored_once(coke):
    """A fulfilled answer with no updated_at can't be dated against the stored, versioned receipt. It is ignored,
    the stored lines stay, and it is named once."""
    table_row(coke, NOW - timedelta(days=9))
    submit(coke, "r1")
    said: list[str] = []
    versioned = fulfilled("r1", receipt(total_paid="12.99"), updated_at="2026-11-08T10:00:00Z")
    sync(coke, FakeBridge({"r1": versioned}), now=NOW, log=said.append)
    before, price = stocked(coke), row(coke)
    unversioned = fulfilled("r1", receipt(total_paid="12.37", units_loaded=4), updated_at=None)
    for hours in (1, 2):
        sync(coke, FakeBridge({"r1": unversioned}), now=NOW + timedelta(hours=hours), log=said.append)
    assert stocked(coke) == before and row(coke) == price and price.expected_cents == 1299
    warned = [line for line in said if "no updated_at" in line]
    assert warned == ["r1: the bridge sent a receipt with no updated_at, but the stored one last changed "
                      "2026-11-08 05:00 ET; kept the stored receipt"], said


def test_a_correction_that_no_longer_gives_a_price_leaves_the_price_and_says_so(coke):
    """A correction makes the only purchase line unavailable. The fill's new lines are stored (the stock follows the
    bridge), but no price is taken back: the row keeps $12.37 and a person is told once."""
    table_row(coke, NOW - timedelta(days=9))
    submit(coke, "r1")
    said: list[str] = []
    sync(coke, FakeBridge({"r1": fulfilled("r1", receipt())}), now=NOW, log=said.append)
    price, logs = row(coke), len(logged(coke))
    gone = fulfilled("r1", receipt(outcome="unavailable", packs_bought=0, units_bought=0, units_loaded=0,
                                   total_paid=None), updated_at="2026-11-08T10:00:00Z")
    for hours in (1, 2):
        sync(coke, FakeBridge({"r1": gone}), now=NOW + timedelta(hours=hours), log=said.append)
    assert row(coke) == price and len(logged(coke)) == logs
    assert stocked(coke)[1] == [(0, 0, 0, None)]
    left = [line for line in said if "left as it is" in line]
    assert left == ["COKE20 primary: still $12.37 from r1, but its receipt as stored now gives no price for that "
                    "row; left as it is. A person can check it (python -m vending.sourcing set)"], said


@pytest.mark.parametrize("resolved, expected, by", [
    ("2026-11-05T16:00:00Z", 1149, "Kevin"),          # before the hand check: the hand price comes back
    ("2026-11-06T12:00:00Z", 1149, "Kevin"),          # at the same moment: the hand check still wins
    ("2026-11-06T18:00:00Z", 1195, "receipt"),        # still after it: the correction applies
], ids=["before the hand check", "at the hand check", "after the hand check"])
def test_a_correction_dated_before_the_last_hand_check_puts_the_hand_price_back(coke, resolved, expected, by):
    """Round 3, finding 4: Kevin checked $11.49 on 6 Nov. The receipt (7 Nov, $12.37) replaced it. A correction
    moves the line to 5 Nov and $11.95. The hand check is now the newer price, so $11.95 must not be written."""
    checked = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
    table_row(coke, checked)
    fill(coke)
    receipt_prices.apply(coke, "r1", NOW)
    assert row(coke).expected_cents == 1237
    store(coke, "r1", fulfilled("r1", receipt(total_paid="11.95", resolved_at=resolved),
                                updated_at="2026-11-08T10:00:00Z"), NOW)
    dry = receipt_prices.apply(coke, "r1", NOW, dry_run=True)
    applied = receipt_prices.apply(coke, "r1", NOW)
    assert dry == applied and len(applied.updated) == 1, applied
    o = row(coke)
    assert (o.expected_cents, o.checked_by) == (expected, by), applied
    if by == "Kevin":
        assert (o.checked_at, o.note) == (checked, "15 Nov check")
        assert "put back $11.49" in applied.updated[0]
    assert receipt_prices.apply(coke, "r1", NOW).updated == []          # and it stays


def test_an_amazon_link_that_is_not_text_skips_that_line_and_the_batch_goes_on(coke):
    """Round 3, finding 7: an echoed `url: 42` raised TypeError and stopped the sync. Now that line gives no price,
    with a reason, and the next request is still read and priced."""
    table_row(coke, NOW - timedelta(days=9), store="Amazon", url=LISTING_A, most=1500)
    submit(coke, "r1", days_ago=3)
    submit(coke, "r2", days_ago=2)
    bad = fulfilled("r1", receipt(store="Amazon"))
    bad["lines"][0]["primary"] = {**BODY["lines"][0]["primary"], "store": "Amazon", "url": 42}
    good = fulfilled("r2", receipt(store="Amazon", total_paid="12.99", resolved_at="2026-11-08T16:00:00Z"))
    good["lines"][0]["primary"] = {**BODY["lines"][0]["primary"], "store": "Amazon", "url": LISTING_A}
    said: list[str] = []
    report = sync(coke, FakeBridge({"r1": bad, "r2": good}), now=NOW, log=said.append)
    assert (report.checked, report.fulfilled, report.failed, report.price_failures) == (2, 2, 0, 0), said
    assert "r1 line 0: no price taken: the request's Amazon link is 42, not a link" in said
    assert row(coke).expected_cents == 1299


def test_two_receipts_with_products_in_opposite_order_do_not_deadlock(coke, schema_url, monkeypatch):
    """Round 3, finding 8: r1 lists COKE20 then PEPSI20, r2 PEPSI20 then COKE20. Locking each row as it was reached
    let r1 hold COKE20 while r2 held PEPSI20, each waiting for the other. Now all rows are locked up front in one
    order, so the second apply waits and then applies."""
    table_row(coke, NOW - timedelta(days=9))
    table_row(coke, NOW - timedelta(days=9), sku="PEPSI20")
    coke_line = receipt()
    pepsi_line = receipt(sku="PEPSI20", total_paid="12.10")
    pepsi_ask = {**BODY["lines"][0]["primary"], "sku": "PEPSI20"}
    for request_id, first, second in (("r1", coke_line, pepsi_line), ("r2", pepsi_line, coke_line)):
        submit(coke, request_id)
        answer = fulfilled(request_id, first, second)
        for line, got in zip(answer["lines"], (first, second)):
            line["primary"] = pepsi_ask if got["sku"] == "PEPSI20" else BODY["lines"][0]["primary"]
        store(coke, request_id, answer, NOW)
    holding, errors = threading.Event(), []
    real = sourcing.locked_row

    def slow(conn, sku, rank):
        found = real(conn, sku, rank)
        if threading.current_thread().name == "first" and not holding.is_set():
            holding.set()
            time.sleep(2)            # long enough for the other apply to take what it can, and Postgres to see a cycle
        return found

    monkeypatch.setattr(sourcing, "locked_row", slow)

    def run(conn, request_id):
        def work():
            try:
                receipt_prices.apply(conn, request_id, NOW)
            except BaseException as error:  # noqa: BLE001 - reported below
                errors.append(error)
        return work

    with open_in(*schema_url) as other:
        first = threading.Thread(target=run(coke, "r1"), name="first")
        first.start()
        assert holding.wait(30)
        second = threading.Thread(target=run(other, "r2"), name="second")
        second.start()
        first.join(60)
        second.join(60)
    assert not first.is_alive() and not second.is_alive()
    assert errors == [], errors
    for sku in ("COKE20", "PEPSI20"):
        o = sourcing._row(coke, sku, "primary")
        assert o.from_receipt and o.note.startswith(("receipt r1:", "receipt r2:")), o


# Sol review, round 4.

def kept_out_rows(conn):
    """The `action_log` rows `store` writes for answers it kept out: (reason, detail)."""
    return conn.execute("select arguments->>'reason', detail from action_log where tool = 'purchase_requests.ignored'"
                        " order by id").fetchall()


def test_a_rejection_with_an_empty_lines_list_does_not_wipe_a_fill(coke):
    """Round 4, finding 1: `{"status": "rejected", "lines": []}` with a newer updated_at was read as fulfilled
    because `lines` was checked before the status, and it deleted an 8-unit fill's lines with no warning. The
    explicit status wins, and a fulfilled answer with no lines never replaces stored lines either."""
    table_row(coke, NOW - timedelta(days=9))
    submit(coke, "r1")
    said: list[str] = []
    sync(coke, FakeBridge({"r1": fulfilled("r1", receipt())}), now=NOW, log=said.append)
    before, price = stocked(coke), row(coke)
    assert before[1][0][2] == 8
    rejected = {**pending("r1"), "status": "rejected", "decision_note": "withdrawn", "lines": [],
                "updated_at": "2026-11-08T10:00:00Z"}
    empty = {**fulfilled("r1", fulfilled_at="2026-11-08T11:00:00Z"), "lines": []}
    for hours in (1, 2):
        for answer in (rejected, empty):
            report = sync(coke, FakeBridge({"r1": answer}), now=NOW + timedelta(hours=hours), log=said.append)
            assert (report.fulfilled, report.failed) == (1, 0), said
    assert stocked(coke) == before and row(coke) == price
    assert sum("now says rejected" in line for line in said) == 1, said
    assert sum("lists no lines" in line for line in said) == 1, said


def test_a_delayed_answer_with_the_same_updated_at_does_not_roll_back_a_correction(coke, schema_url):
    """Round 4, finding 2: the corrected receipt ($12.99, 7 loaded) is stored through one connection. A delayed read
    of the old one ($12.37, 8 loaded) carrying the same updated_at arrives through another. It must not roll the
    price back or the stock up: the stored revision stays, and the difference is named once."""
    table_row(coke, NOW - timedelta(days=9))
    submit(coke, "r1")
    same = "2026-11-08T10:00:00Z"
    corrected = fulfilled("r1", receipt(total_paid="12.99", units_loaded=7), updated_at=same)
    old = fulfilled("r1", receipt(total_paid="12.37"), updated_at=same)
    said: list[str] = []
    sync(coke, FakeBridge({"r1": corrected}), now=NOW, log=said.append)
    with open_in(*schema_url) as other:
        for hours in (1, 2):
            sync(other, FakeBridge({"r1": old}), now=NOW + timedelta(hours=hours), log=said.append)
    assert (row(coke).expected_cents, stocked(coke)[1][0][2]) == (1299, 7)
    assert sum("same updated_at" in line for line in said) == 1, said
    # A replay of the stored revision is no change and no warning; later losses on it are still picked up.
    grown = fulfilled("r1", receipt(total_paid="12.99", units_loaded=7, later_losses=[later(1)]), updated_at=same)
    sync(coke, FakeBridge({"r1": corrected}), now=NOW + timedelta(hours=3), log=said.append)
    sync(coke, FakeBridge({"r1": grown}), now=NOW + timedelta(hours=4), log=said.append)
    assert coke.execute("select count(*) from purchase_line_losses").fetchone()[0] == 1
    assert (row(coke).expected_cents, stocked(coke)[1][0][2]) == (1299, 7)
    assert sum("same updated_at" in line for line in said) == 1, said


def test_a_sourcing_import_and_a_receipt_refresh_do_not_deadlock(coke, schema_url, monkeypatch):
    """Round 4, finding 3: an import that writes PEPSI20 and then COKE20 held PEPSI20 while the refresh held COKE20
    and wanted PEPSI20. The import now locks every row it touches up front, in the refresh's (sku, rank) order."""
    table_row(coke, NOW - timedelta(days=9))
    table_row(coke, NOW - timedelta(days=9), sku="PEPSI20")
    submit(coke, "r1")
    answer = fulfilled("r1", receipt(), receipt(sku="PEPSI20", total_paid="12.10"))
    answer["lines"][1]["primary"] = {**BODY["lines"][0]["primary"], "sku": "PEPSI20"}
    store(coke, "r1", answer, NOW)
    imported = [sourcing.Option(sku=sku, rank="primary", store="Costco", units_per_pack=12, expected_cents=1199,
                                max_cents=1400, url=None, pack_description=None, checked_at=NOW - timedelta(days=8),
                                checked_by="Meaghan", note="import") for sku in ("PEPSI20", "COKE20")]
    holding, errors = threading.Event(), []
    real = sourcing.set_option

    def slow(conn, o, now=None):
        outcome = real(conn, o, now)
        if threading.current_thread().name == "import" and not holding.is_set():
            holding.set()
            time.sleep(2)            # long enough for the refresh to take what it can, and Postgres to see a cycle
        return outcome

    monkeypatch.setattr(sourcing, "set_option", slow)

    def run(work):
        def go():
            try:
                work()
            except BaseException as error:  # noqa: BLE001 - reported below
                errors.append(error)
        return go

    with open_in(*schema_url) as other:
        importer = threading.Thread(target=run(lambda: sourcing.import_rows(coke, imported, NOW)), name="import")
        importer.start()
        assert holding.wait(30)
        refresh = threading.Thread(target=run(lambda: receipt_prices.apply(other, "r1", NOW)), name="refresh")
        refresh.start()
        importer.join(60)
        refresh.join(60)
    assert not importer.is_alive() and not refresh.is_alive()
    assert errors == [], errors
    for sku in ("COKE20", "PEPSI20"):
        o = sourcing._row(coke, sku, "primary")
        assert o.from_receipt and o.note.startswith("receipt r1:"), o     # the receipt (7 Nov) is newer than 1 Nov


def _malformed(kind):
    answer = fulfilled("r1", receipt())
    line, got = answer["lines"][0], answer["lines"][0]["receipt"]
    if kind == "updated_at":
        answer["updated_at"] = 7
    elif kind == "fulfilled_at":
        answer["fulfilled_at"] = 7
    elif kind == "status":
        answer["status"] = 7
    elif kind == "lines":
        answer["lines"] = "all of them"
    elif kind == "line":
        answer["lines"] = [5]
    elif kind == "receipt":
        line["receipt"] = ["primary"]
    elif kind == "later_losses":
        got["later_losses"] = {"units": 1}
    elif kind == "loss":
        got["later_losses"] = [3]
    elif kind == "resolved_at":
        got["resolved_at"] = 7
    elif kind == "recorded_at":
        got["later_losses"] = [{**later(1), "recorded_at": 7}]
    elif kind == "units_loaded":
        got["units_loaded"] = {"coil": 8}
    elif kind == "decision_note":
        answer["decision_note"] = ["no"]
    elif kind == "answer":
        return ["not", "an", "answer"]
    return answer


@pytest.mark.parametrize("kind", ["updated_at", "fulfilled_at", "status", "lines", "line", "receipt", "later_losses",
                                  "loss", "resolved_at", "recorded_at", "units_loaded", "decision_note", "answer"])
def test_a_malformed_answer_fails_that_request_and_the_next_is_still_read(coke, kind):
    """Round 4, finding 4: `updated_at: 7` raised TypeError outside the per-request handling and stopped the run.
    Types are checked where the answer comes in: a malformed answer is that request's validation error."""
    table_row(coke, NOW - timedelta(days=9))
    submit(coke, "r1", days_ago=3)
    submit(coke, "r2", days_ago=2)
    good = fulfilled("r2", receipt(total_paid="12.99", resolved_at="2026-11-08T16:00:00Z"))
    said: list[str] = []
    report = sync(coke, FakeBridge({"r1": _malformed(kind), "r2": good}), now=NOW, log=said.append)
    assert (report.checked, report.failed, report.fulfilled) == (1, 1, 1), said
    assert any(line.startswith("r1: ") and "kept what we had" in line for line in said), said
    assert coke.execute("select status from purchase_requests where request_id = 'r1'").fetchone()[0] == "pending"
    assert row(coke).expected_cents == 1299


def test_the_kept_out_row_holds_the_reason_and_the_warning(coke):
    """Round 4, finding 5: the warning was only logged after the dedupe row committed, so a failed log lost it for
    good. The row itself now holds the reason and the warning a person acts on."""
    submit(coke, "r1")
    store(coke, "r1", fulfilled("r1", receipt()), NOW)
    rejected = {**pending("r1"), "status": "rejected", "decision_note": "withdrawn"}
    warned = store(coke, "r1", rejected, NOW).warnings
    assert store(coke, "r1", rejected, NOW).warnings == ()
    late = fulfilled("r1", receipt(total_paid="12.00"), fulfilled_at="2026-11-01T10:00:00Z")
    late_warned = store(coke, "r1", late, NOW).warnings
    assert kept_out_rows(coke) == [("rejected_after_fill", warned[0]), ("older", late_warned[0])]


# Sol review, round 5.

def test_a_newer_answer_with_fewer_later_losses_is_kept_out(coke, schema_url):
    """Round 5, finding 1: the stored receipt has a later loss (8 loaded, 1 lost later: 7 in the coil). A newer
    answer without that loss replaced the lines and took the loss with them, so the coil read 8. Later losses only
    grow, so that answer is kept out with one stored warning, read through another connection too."""
    submit(coke, "r1")
    said: list[str] = []
    sync(coke, FakeBridge({"r1": fulfilled("r1", receipt(later_losses=[later(1)]))}), now=NOW, log=said.append)
    assert stock(coke).slots[0].units == 7
    shrunk = fulfilled("r1", receipt(), updated_at="2026-11-08T10:00:00Z")
    with open_in(*schema_url) as other:
        for hours in (1, 2):
            report = sync(other, FakeBridge({"r1": shrunk}), now=NOW + timedelta(hours=hours), log=said.append)
            assert (report.fulfilled, report.failed) == (1, 0), said
    assert stock(coke).slots[0].units == 7
    assert coke.execute("select count(*) from purchase_line_losses").fetchone()[0] == 1
    warned = [line for line in said if "fewer later losses" in line]
    assert len(warned) == 1 and "line 0" in warned[0], said
    assert [reason for reason, _ in kept_out_rows(coke)] == ["losses_shrank"]
    # A newer answer that keeps the loss and adds one is stored as usual.
    grown = fulfilled("r1", receipt(later_losses=[later(1), later(2, "back_stock", "2026-11-08T11:00:00Z")]),
                      updated_at="2026-11-08T12:00:00Z")
    report = sync(coke, FakeBridge({"r1": grown}), now=NOW + timedelta(hours=3), log=said.append)
    assert report.new_losses == 1
    assert coke.execute("select count(*) from purchase_line_losses").fetchone()[0] == 2


@pytest.mark.parametrize("field", ["updated_at", "resolved_at", "recorded_at"])
@pytest.mark.parametrize("when", ["0001-01-01T00:00:00Z", "9999-12-31T23:59:59-23:59", "1999-12-31T23:59:59Z",
                                  "2100-01-01T00:00:00Z"], ids=["year 1", "year 9999", "1999", "2100"])
def test_a_time_out_of_range_fails_that_request_only(coke, field, when):
    """Round 5, finding 2: `updated_at` of year 1 parsed, then writing it in Eastern time overflowed, and the
    OverflowError stopped the whole sync. A time outside 2020-2099 is now that request's validation error."""
    table_row(coke, NOW - timedelta(days=9))
    submit(coke, "r1", days_ago=3)
    submit(coke, "r2", days_ago=2)
    sync(coke, FakeBridge({"r1": fulfilled("r1", receipt()), "r2": pending("r2")}), now=NOW, log=quietly)
    bad = fulfilled("r1", receipt(total_paid="12.99"), updated_at="2026-11-08T10:00:00Z")
    if field == "updated_at":
        bad["updated_at"] = when
    elif field == "resolved_at":
        bad["lines"][0]["receipt"]["resolved_at"] = when
    else:
        bad["lines"][0]["receipt"]["later_losses"] = [later(1, at=when)]
    good = fulfilled("r2", receipt(total_paid="12.99", resolved_at="2026-11-08T16:00:00Z"))
    said: list[str] = []
    report = sync(coke, FakeBridge({"r1": bad, "r2": good}), now=NOW + timedelta(hours=1), log=said.append)
    assert (report.checked, report.failed) == (1, 1), said
    assert any(line.startswith("r1: ") and "kept what we had" in line for line in said), said
    assert stocked(coke)[1][0][3] == Decimal("12.37")
    assert row(coke).expected_cents == 1299 and row(coke).note.startswith("receipt r2:")


def test_a_skip_reason_is_named_after_a_lock_timeout_stops_its_first_pricing(coke, schema_url):
    """Round 5, finding 3: the correction (above the max) was stored, but pricing it hit a lock timeout. The next
    read saw the same stored lines and stayed quiet, so the reason was never named. Each reason is now recorded
    when pricing reads that revision, and named once."""
    table_row(coke, NOW - timedelta(days=9))
    submit(coke, "r1")
    said: list[str] = []
    sync(coke, FakeBridge({"r1": fulfilled("r1", receipt())}), now=NOW, log=said.append)
    corrected = fulfilled("r1", receipt(total_paid="13.99"), updated_at="2026-11-08T10:00:00Z")
    coke.execute("set lock_timeout = '300ms'")
    try:
        with open_in(*schema_url) as other:
            with other.transaction():
                other.execute("select 1 from sourcing_options where sku = 'COKE20' for update")
                report = sync(coke, FakeBridge({"r1": corrected}), now=NOW, log=said.append)
        assert report.price_failures == 1, said
        assert any("receipt prices not applied (LockNotAvailable)" in line for line in said), said
    finally:
        coke.execute("reset lock_timeout")
    for hours in (1, 2):
        sync(coke, FakeBridge({"r1": corrected}), now=NOW + timedelta(hours=hours), log=said.append)
    assert sum("above the primary row's max $13.50" in line for line in said) == 1, said


def test_two_imports_adding_the_same_new_rows_in_opposite_order_do_not_deadlock(coke, schema_url, monkeypatch):
    """Round 5, finding 4: neither SKU has a row yet, so `lock_skus` found nothing to lock. One import inserted
    COKE20 then PEPSI20, the other PEPSI20 then COKE20, and each waited on the other's insert. Both now take a lock
    per SKU, in one order, before writing; the second waits and then writes."""
    def rows(order, by):
        return [sourcing.Option(sku=sku, rank="primary", store="Costco", units_per_pack=12, expected_cents=1199,
                                max_cents=1400, url=None, pack_description=None, checked_at=NOW - timedelta(days=8),
                                checked_by=by, note="import") for sku in order]
    holding, errors = threading.Event(), []
    real = sourcing.set_option

    def slow(conn, o, now=None):
        outcome = real(conn, o, now)
        if threading.current_thread().name == "first" and not holding.is_set():
            holding.set()
            time.sleep(2)            # long enough for the other import to take what it can, and Postgres to see a cycle
        return outcome

    monkeypatch.setattr(sourcing, "set_option", slow)

    def run(conn, order, by):
        def work():
            try:
                sourcing.import_rows(conn, rows(order, by), NOW)
            except BaseException as error:  # noqa: BLE001 - reported below
                errors.append(error)
        return work

    with open_in(*schema_url) as other:
        first = threading.Thread(target=run(coke, ("COKE20", "PEPSI20"), "Kevin"), name="first")
        first.start()
        assert holding.wait(30)
        second = threading.Thread(target=run(other, ("PEPSI20", "COKE20"), "Meaghan"), name="second")
        second.start()
        first.join(60)
        second.join(60)
    assert not first.is_alive() and not second.is_alive()
    assert errors == [], errors
    assert {sourcing._row(coke, sku, "primary").checked_by for sku in ("COKE20", "PEPSI20")} == {"Meaghan"}


@pytest.mark.parametrize("hand, expected, by", [
    (datetime(2026, 11, 1, 12, tzinfo=timezone.utc), 1237, "receipt"),     # r1 (7 Nov) is the newest left
    (datetime(2026, 11, 7, 20, tzinfo=timezone.utc), 1149, "Kevin"),       # the hand check (after r1) is
], ids=["older receipt is newest", "hand check is newest"])
def test_a_correction_that_backdates_takes_the_newest_eligible_price(coke, hand, expected, by):
    """Round 5, finding 5: r1 (7 Nov, $12.37) priced the row, then r2 (8 Nov, $12.99). A correction moves r2 to 5 Nov
    and $11.95. The row took $11.95 dated 5 Nov, though r1's 7 Nov price is newer. Now the newest of the correction,
    the hand check and every other fulfilled receipt for that row wins."""
    table_row(coke, hand)
    fill(coke, "r1")
    receipt_prices.apply(coke, "r1", NOW)
    fill(coke, "r2", receipt(total_paid="12.99", resolved_at="2026-11-08T16:00:00Z"))
    receipt_prices.apply(coke, "r2", NOW)
    assert row(coke).expected_cents == 1299
    store(coke, "r2", fulfilled("r2", receipt(total_paid="11.95", resolved_at="2026-11-05T16:00:00Z"),
                                updated_at="2026-11-09T10:00:00Z"), NOW)
    dry = receipt_prices.apply(coke, "r2", NOW, dry_run=True)
    applied = receipt_prices.apply(coke, "r2", NOW)
    assert dry == applied and len(applied.updated) == 1, applied
    o = row(coke)
    assert (o.expected_cents, o.checked_by) == (expected, by), applied
    if by == "receipt":
        assert o.checked_at == RESOLVED and o.note.startswith("receipt r1: paid $12.37"), o
        assert "from r1" in applied.updated[0], applied
    for request_id in ("r1", "r2"):
        assert receipt_prices.apply(coke, request_id, NOW).updated == []        # and it stays


# Sol review, round 6. Once a receipt is stored fulfilled, a newer answer is stored only if it has exactly the same
# lines (line number, slot, source and what we asked for), and each line's stored later losses start its new list.

TWO_SLOTS = {"off_cycle": False, "lines": [BODY["lines"][0], {**BODY["lines"][0], "slot": "16"}]}


def two_slot_answer(*receipts, slots=("15", "16"), **fields):
    answer = fulfilled("r1", *receipts, **fields)
    for line, slot in zip(answer["lines"], slots, strict=True):
        line["slot"] = slot
    return answer


def coil(conn):
    return sorted((s.slot, s.units) for s in stock(conn).slots)


@pytest.mark.parametrize("change", ["missing", "extra", "moved", "source", "asked"])
def test_a_newer_answer_with_different_lines_is_kept_out(coke, change):
    """Round 6, finding 1: the stored receipt fills two slots (8 units each, $12.75 a pack). A newer answer with only
    line 0 ($12.37) replaced both lines: the coil lost 8 units and the price fell to $12.37. An answer that drops,
    adds or changes a line is kept out with one stored warning; one that changes only amounts is stored."""
    table_row(coke, NOW - timedelta(days=9))
    record(coke, "r1", NOW - timedelta(days=2), False, TWO_SLOTS, plan_hash="abc123")
    said: list[str] = []
    sync(coke, FakeBridge({"r1": two_slot_answer(receipt(total_paid="12.75"), receipt(total_paid="12.75"))}),
         now=NOW, log=said.append)
    assert coil(coke) == [("15", 8), ("16", 8)] and row(coke).expected_cents == 1275
    newer = "2026-11-08T10:00:00Z"
    cheap = receipt(total_paid="12.37")
    if change == "missing":
        changed = two_slot_answer(cheap, slots=("15",), updated_at=newer)
    elif change == "extra":
        changed = two_slot_answer(cheap, cheap, cheap, slots=("15", "16", "17"), updated_at=newer)
    else:
        changed = two_slot_answer(cheap, cheap, updated_at=newer)
        line = changed["lines"][1]
        if change == "moved":
            line["slot"] = "17"
        elif change == "source":
            line["source"] = "back_stock"
        else:
            line["primary"] = {**line["primary"], "packs": 2}
    for hours in (1, 2):
        report = sync(coke, FakeBridge({"r1": changed}), now=NOW + timedelta(hours=hours), log=said.append)
        assert (report.fulfilled, report.failed) == (1, 0), said
    assert coil(coke) == [("15", 8), ("16", 8)]
    assert row(coke).expected_cents == 1275
    warned = [line for line in said if "different lines" in line]
    assert len(warned) == 1 and warned[0].startswith("r1: "), said
    assert [reason for reason, _ in kept_out_rows(coke)] == ["lines_changed"]
    # The same two lines with new amounts are a correction, and stored.
    fixed = two_slot_answer(cheap, cheap, updated_at="2026-11-08T12:00:00Z")
    sync(coke, FakeBridge({"r1": fixed}), now=NOW + timedelta(hours=3), log=said.append)
    assert row(coke).expected_cents == 1237 and coil(coke) == [("15", 8), ("16", 8)]


def test_a_correction_whose_lines_differ_from_the_request_is_kept_out(coke):
    """Round 6, finding 1: the lines are checked against the request we sent too. The fill (slots 15 and 17) is
    stored as the bridge gave it, though we asked for 15 and 16; a correction with those same lines is kept out, so a
    person looks at it."""
    record(coke, "r1", NOW - timedelta(days=2), False, TWO_SLOTS, plan_hash="abc123")
    store(coke, "r1", two_slot_answer(receipt(), receipt(), slots=("15", "17")), NOW)
    again = two_slot_answer(receipt(units_loaded=4), receipt(), slots=("15", "17"), updated_at="2026-11-08T10:00:00Z")
    [warning] = store(coke, "r1", again, NOW).warnings
    assert "different lines from the request we sent" in warning, warning
    assert coil(coke) == [("15", 8), ("17", 8)]
    assert [reason for reason, _ in kept_out_rows(coke)] == ["lines_changed"]


def test_a_door_time_out_of_range_fails_that_request_and_keeps_the_stored_pair(coke):
    """Round 6, finding 2: a correction with a 1999 door time was read as having no door times, and it cleared the
    valid pair stored with the fill. A door time out of range is now that request's error: it fails, the stored
    pair stays, and the next request is read as usual. A door time left out still falls back as before."""
    submit(coke, "r1", days_ago=3)
    submit(coke, "r2", days_ago=2)
    sync(coke, FakeBridge({"r1": fulfilled("r1", receipt(), door_opened_at=OPENED, door_closed_at=CLOSED),
                           "r2": pending("r2")}), now=NOW, log=quietly)
    before = door_row(coke, "r1")
    assert before[0] is not None
    bad = fulfilled("r1", receipt(total_paid="12.99"), updated_at="2026-11-08T10:00:00Z",
                    door_opened_at="1999-12-31T23:59:00Z", door_closed_at=CLOSED)
    said: list[str] = []
    report = sync(coke, FakeBridge({"r1": bad, "r2": fulfilled("r2", receipt())}), now=NOW + timedelta(hours=1),
                  log=said.append)
    assert (report.failed, report.fulfilled) == (1, 1), said
    assert any(line.startswith("r1: ") and "kept what we had" in line for line in said), said
    assert door_row(coke, "r1") == before
    assert stocked(coke)[1][0][3] == Decimal("12.37")


def test_a_skip_reason_is_named_again_when_a_later_revision_brings_it_back(coke):
    """Round 6, finding 3: the skip warnings were deduplicated by a hash of the priced lines alone, so revision 3,
    the same as revision 1, was taken as already named. The stored updated_at is part of the key now."""
    table_row(coke, NOW - timedelta(days=9))
    submit(coke, "r1")
    said: list[str] = []
    for hours, (paid, changed) in enumerate([("13.99", "2026-11-08T10:00:00Z"), ("12.37", "2026-11-08T11:00:00Z"),
                                             ("13.99", "2026-11-08T12:00:00Z")]):
        answer = fulfilled("r1", receipt(total_paid=paid), updated_at=changed)
        for again in (0, 1):
            sync(coke, FakeBridge({"r1": answer}), now=NOW + timedelta(hours=hours, minutes=again), log=said.append)
    assert sum("above the primary row's max $13.50" in line for line in said) == 2, said
