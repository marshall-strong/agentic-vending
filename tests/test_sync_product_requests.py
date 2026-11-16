"""Tests for reading catalog requests' decisions back from the bridge. Synthetic answers only,
shaped like the bridge's `ProductRequestView` (live spec, 16 Nov 2026). Skipped like
`test_sync_history` when there is no database."""

import pytest

pytest.importorskip("psycopg")

# These import psycopg too, so they come after the skip.
from tests.test_sync_history import conn  # noqa: E402,F401 - the throwaway-schema fixture
from vending.bridge import BridgeError, BridgeRateLimited, BridgeUnreachable  # noqa: E402
from vending.sync_product_requests import decision, sync  # noqa: E402


def add(conn, bridge_id, name, status="pending"):
    return conn.execute("insert into product_requests (name, size, words, bridge_id, status)"
                        " values (%s, '12 oz can', %s, %s, %s) returning id",
                        (name, [name.lower()], bridge_id, status)).fetchone()[0]


def view(bridge_id, status, sku=None, note=None):
    return {"id": bridge_id, "status": status, "created_at": "2026-11-15T20:00:00Z",
            "requested": {"name": "x", "line": None, "flavor": None, "size": "12 oz can", "note": None},
            "decision_note": note, "product": {"sku": sku, "name": "x"} if sku else None}


class FakeBridge:
    """Answers from a dict. A value that is an exception is raised."""

    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def product_request(self, request_id):
        self.asked.append(request_id)
        answer = self.answers[request_id]
        if isinstance(answer, Exception):
            raise answer
        return answer


def quiet(_):
    pass


def row(conn, bridge_id):
    return conn.execute("select status, sku, decision_note, last_response is not null from product_requests"
                        " where bridge_id = %s", (bridge_id,)).fetchone()


def test_stores_approved_rejected_and_still_pending(conn):
    for b, name in [("np_1", "Sprite"), ("np_2", "Fanta"), ("np_3", "Pibb")]:
        add(conn, b, name)
    bridge = FakeBridge({"np_1": view("np_1", "approved", sku="SPRITE-12CAN"),
                         "np_2": view("np_2", "rejected", note="not sold near campus"),
                         "np_3": view("np_3", "pending")})
    report = sync(conn, bridge, log=quiet)
    assert (report.checked, report.approved, report.rejected, report.pending) == (3, 1, 1, 1)
    assert row(conn, "np_1") == ("approved", "SPRITE-12CAN", None, True)
    assert row(conn, "np_2") == ("rejected", None, "not sold near campus", True)
    assert row(conn, "np_3") == ("pending", None, None, True)


def test_reads_only_pending_rows_with_a_bridge_id(conn):
    add(conn, "np_1", "Sprite", status="approved")
    add(conn, "np_2", "Fanta", status="rejected")
    add(conn, None, "Pibb", status="sending")
    add(conn, None, "Fresca", status="unknown")
    add(conn, "np_5", "Tab")
    bridge = FakeBridge({"np_5": view("np_5", "pending")})
    sync(conn, bridge, log=quiet)
    assert bridge.asked == ["np_5"]


def test_a_decided_request_is_not_read_again(conn):
    add(conn, "np_1", "Sprite")
    bridge = FakeBridge({"np_1": view("np_1", "approved", sku="SPRITE-12CAN")})
    sync(conn, bridge, log=quiet)
    sync(conn, bridge, log=quiet)
    assert bridge.asked == ["np_1"]


def test_an_unusable_answer_or_a_404_is_counted_and_the_rest_go_on(conn):
    for b, name in [("np_1", "Sprite"), ("np_2", "Fanta"), ("np_3", "Pibb")]:
        add(conn, b, name)
    bridge = FakeBridge({"np_1": BridgeError("404 from /api/v1/product-requests/np_1: b'not found'"),
                         "np_2": view("np_2", "approved"),          # approved without a SKU
                         "np_3": view("np_3", "rejected", note="no")})
    report = sync(conn, bridge, log=quiet)
    assert (report.failed, report.checked, report.rejected) == (2, 1, 1)
    assert row(conn, "np_2") == ("pending", None, None, False)   # kept what we had


def test_rate_limited_stops_and_leaves_the_rest(conn):
    for b, name in [("np_1", "Sprite"), ("np_2", "Fanta"), ("np_3", "Pibb")]:
        add(conn, b, name)
    bridge = FakeBridge({"np_1": view("np_1", "pending"), "np_2": BridgeRateLimited("429"), "np_3": view("np_3", "pending")})
    report = sync(conn, bridge, log=quiet)
    assert (report.checked, report.skipped) == (1, 2)
    assert bridge.asked == ["np_1", "np_2"]


def test_unreachable_stops_the_run(conn):
    add(conn, "np_1", "Sprite")
    with pytest.raises(BridgeUnreachable):
        sync(conn, FakeBridge({"np_1": BridgeUnreachable("401")}), log=quiet)


@pytest.mark.parametrize("answer", [[], {"status": "maybe"}, {"status": "rejected", "decision_note": 5},
                                    {"status": "approved", "product": {"sku": " "}}, {"status": "approved", "product": None}])
def test_decision_refuses_what_it_cant_use(answer):
    with pytest.raises(ValueError):
        decision(answer)


def test_decision_reads_the_sku_it_was_added_under():
    assert decision(view("np_1", "approved", sku="FRESCA-12CAN", note="added as the 12-pack")) == (
        "approved", "FRESCA-12CAN", "added as the 12-pack")
