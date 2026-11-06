"""Tests for storing purchase requests and their receipts. Synthetic answers only,
shaped like the bridge's `PurchaseRequestPending` and `PurchaseRequestDetail`.
Skipped like `test_sync_history` when there is no database."""

from datetime import datetime, timedelta, timezone
from fractions import Fraction

import pytest

pytest.importorskip("psycopg")

# These import psycopg too, so they come after the skip.
from tests.test_sync_history import conn  # noqa: E402,F401 - the throwaway-schema fixture
from vending.bridge import BridgeError, BridgeRateLimited, BridgeUnreachable  # noqa: E402
from vending.purchase_requests import get_purchase_requests, record, store  # noqa: E402
from vending.stock import stock  # noqa: E402
from vending.sync_requests import sync  # noqa: E402

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
BODY = {"off_cycle": False, "lines": [{"slot": "15", "source": "purchase", "primary": {
    "sku": "COKE20", "store": "Costco", "packs": 1, "units_per_pack": 12,
    "max_price_per_pack": "13.00", "sale_price": "2.50", "change_product": False}}]}


def submit(conn, request_id, days_ago=2):
    record(conn, request_id, NOW - timedelta(days=days_ago), False, BODY, plan_hash="abc123")


def pending(request_id):
    return {"id": request_id, "status": "pending", "created_at": "2026-11-07T14:00:00Z"}


def receipt(**overrides):
    fields = {
        "outcome": "primary", "sku": "COKE20", "store": "Costco", "packs_bought": 1, "units_per_pack": 12,
        "units_bought": 12, "total_paid": "12.37", "unit_cost_paid": "1.1245", "units_loaded": 8,
        "units_lost": 1, "loss_note": "one can split", "later_losses": [], "units_to_back_stock": 3,
        "units_from_back_stock": 0, "units_pulled": 0, "pulled_sku": None, "slot_reassigned": False,
        "resolved_at": "2026-11-07T16:00:00Z",
    }
    return {**fields, **overrides}


def fulfilled(request_id, *receipts, fulfilled_at="2026-11-07T16:30:00Z"):
    return {
        "id": request_id, "status": "fulfilled", "device_id": "vmc1", "created_at": "2026-11-07T14:00:00Z",
        "updated_at": fulfilled_at, "fulfilled_at": fulfilled_at, "off_cycle": False, "note": None,
        "decision_note": None, "total_paid": "12.37",
        "lines": [{"line": i, "slot": "15", "source": "purchase", "primary": BODY["lines"][0]["primary"],
                   "alternate": None, "load": None, "receipt": r} for i, r in enumerate(receipts)],
    }


def later(units, location="coil", at="2026-11-08T10:00:00Z"):
    return {"location": location, "sku": "COKE20", "units": units, "note": "dented", "recorded_at": at}


class FakeBridge:
    """Answers from a dict. A value that is an exception is raised; a list is answered in turn."""

    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def purchase_request(self, request_id):
        self.asked.append(request_id)
        answer = self.answers[request_id]
        if isinstance(answer, list):
            answer = answer.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def quiet(_):
    pass


def test_pending_then_fulfilled(conn):
    submit(conn, "r1")
    bridge = FakeBridge({"r1": [pending("r1"), fulfilled("r1", receipt())]})
    report = sync(conn, bridge, NOW, log=quiet)
    assert (report.checked, report.pending) == (1, 1)
    assert conn.execute("select status, last_checked from purchase_requests").fetchone() == ("pending", NOW)

    report = sync(conn, bridge, NOW, log=quiet)
    assert report.fulfilled == 1
    row = conn.execute("select outcome, total_paid, unit_cost_paid, units_loaded, units_to_back_stock"
                       " from purchase_lines").fetchone()
    assert row[0] == "primary" and str(row[1]) == "12.37" and str(row[2]) == "1.1245" and row[3:] == (8, 3)


def test_later_losses_are_picked_up_without_double_counting(conn):
    submit(conn, "r1")
    bridge = FakeBridge({"r1": [
        fulfilled("r1", receipt(later_losses=[later(1)])),
        fulfilled("r1", receipt(later_losses=[later(1)])),
        fulfilled("r1", receipt(later_losses=[later(1), later(2, "back_stock", "2026-11-08T11:00:00Z")])),
    ]})
    assert [sync(conn, bridge, NOW, log=quiet).new_losses for _ in range(3)] == [1, 0, 1]
    assert conn.execute("select count(*), sum(units) from purchase_line_losses").fetchone() == (2, 3)
    [slot] = stock(conn).slots
    assert (slot.units, slot.lost) == (7, 1)


def test_rejected_is_stored_and_not_read_again(conn):
    submit(conn, "r1")
    bridge = FakeBridge({"r1": {**pending("r1"), "status": "rejected", "decision_note": "Costco is closed"}})
    assert sync(conn, bridge, NOW, log=quiet).rejected == 1
    assert sync(conn, bridge, NOW, log=quiet).checked == 0
    assert get_purchase_requests(conn)[0]["decision_note"] == "Costco is closed"


def test_one_bad_request_does_not_stop_the_others(conn):
    for request_id in ("gone", "odd", "nolines", "ok"):
        submit(conn, request_id)
    bridge = FakeBridge({
        "gone": BridgeError("404 from /api/v1/purchase-requests/gone"),
        "odd": {"id": "odd", "status": "cancelled"},
        "nolines": fulfilled("nolines", receipt(), receipt()) | {"lines": [
            {"line": 0, "slot": "15", "source": "purchase", "receipt": None}]},
        "ok": fulfilled("ok", receipt()),
    })
    report = sync(conn, bridge, NOW, log=quiet)
    assert (report.failed, report.fulfilled) == (3, 1)
    statuses = dict(conn.execute("select request_id, status from purchase_requests").fetchall())
    assert statuses == {"gone": "pending", "odd": "pending", "nolines": "pending", "ok": "fulfilled"}


def test_a_bad_reread_keeps_the_receipts_we_had(conn):
    submit(conn, "r1")
    good = fulfilled("r1", receipt())
    broken = fulfilled("r1", receipt()) | {"lines": [{"line": 0, "slot": "15", "source": "purchase", "receipt": None}]}
    sync(conn, FakeBridge({"r1": good}), NOW, log=quiet)
    assert sync(conn, FakeBridge({"r1": broken}), NOW, log=quiet).failed == 1
    assert conn.execute("select count(*) from purchase_lines").fetchone()[0] == 1


def test_rate_limit_stops_the_run_and_leaves_the_rest(conn):
    for days_ago, request_id in enumerate(("c", "b", "a")):
        submit(conn, request_id, days_ago=days_ago + 1)
    bridge = FakeBridge({"a": pending("a"), "b": BridgeRateLimited("429"), "c": pending("c")})
    report = sync(conn, bridge, NOW, log=quiet)
    assert (report.checked, report.skipped) == (1, 2)
    assert bridge.asked == ["a", "b"]


def test_unreachable_bridge_stops_the_sync(conn):
    submit(conn, "r1")
    with pytest.raises(BridgeUnreachable):
        sync(conn, FakeBridge({"r1": BridgeUnreachable("Is Tailscale connected?")}), NOW, log=quiet)


def test_only_recently_fulfilled_requests_are_read_again(conn):
    submit(conn, "old", days_ago=60)
    submit(conn, "new")
    sync(conn, FakeBridge({"old": fulfilled("old", receipt(), fulfilled_at="2026-09-10T16:00:00Z"),
                           "new": fulfilled("new", receipt())}), NOW, log=quiet)
    bridge = FakeBridge({"old": fulfilled("old", receipt()), "new": fulfilled("new", receipt())})
    sync(conn, bridge, NOW, log=quiet)
    assert bridge.asked == ["new"]


def test_get_purchase_requests_shows_plan_receipts_and_losses(conn):
    submit(conn, "r1", days_ago=3)
    submit(conn, "r2", days_ago=1)
    sync(conn, FakeBridge({"r1": fulfilled("r1", receipt(later_losses=[later(1)])), "r2": pending("r2")}),
         NOW, log=quiet)
    everything = get_purchase_requests(conn)
    assert [r["request_id"] for r in everything] == ["r2", "r1"]
    r1 = everything[1]
    assert (r1["status"], r1["total_paid"], r1["plan"], r1["plan_hash"]) == ("fulfilled", "12.37", BODY, "abc123")
    [line] = r1["lines"]
    assert line["primary"]["sku"] == "COKE20"
    assert line["receipt"]["total_paid"] == "12.37" and line["receipt"]["unit_cost_paid"] == "1.1245"
    assert line["receipt"]["later_losses"] == [
        {"location": "coil", "sku": "COKE20", "units": 1, "note": "dented", "recorded_at": "2026-11-08T10:00:00+00:00"}]
    assert [r["request_id"] for r in get_purchase_requests(conn, pending_only=True)] == ["r2"]


def test_stock_reads_receipts_and_poll_counters(conn):
    submit(conn, "r1")
    sync(conn, FakeBridge({"r1": fulfilled("r1", receipt(total_paid="12.00"))}), NOW, log=quiet)
    for session_id, started_at, lifetime in (("p1", "2026-11-07T15:00:00Z", 40), ("p2", "2026-11-08T15:00:00Z", 43)):
        conn.execute("insert into polls (session_id, device_id, started_at, raw, byte_count, crc_ok)"
                     " values (%s, 'vmc1', %s, '', 0, true)", (session_id, started_at))
        conn.execute("insert into poll_selections (session_id, selection, price_cents, lifetime_units,"
                     " lifetime_sales_cents) values (%s, '15', 250, %s, 0)", (session_id, lifetime))
    result = stock(conn)
    [slot] = result.slots
    assert (slot.product, slot.units, slot.sold, slot.unit_cost) == ("COKE20", 5, 3, Fraction(1200, 11))
    assert [(c.product, c.units, c.unit_cost) for c in result.closet] == [("COKE20", 3, Fraction(1200, 11))]
    assert result.warnings == ()


def test_record_twice_keeps_what_the_sync_learned(conn):
    submit(conn, "r1")
    sync(conn, FakeBridge({"r1": fulfilled("r1", receipt())}), NOW, log=quiet)
    submit(conn, "r1")
    assert conn.execute("select status from purchase_requests").fetchone() == ("fulfilled",)


def test_a_float_amount_is_refused(conn):
    submit(conn, "r1")
    with pytest.raises(ValueError):
        store(conn, "r1", fulfilled("r1", receipt(total_paid=12.37)), NOW)
