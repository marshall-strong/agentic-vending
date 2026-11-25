"""Before the slot lock, the machine's Next restock shows the day-one fill that will actually happen.

Three phases: the picked first-fill plan until the fill request is sent; that request's lines until its
receipt is counted; then the agent's latest plan, as from the lock.
"""

import re
from datetime import datetime, timedelta, timezone

import pytest
from psycopg.types.json import Jsonb

from tests.conftest import TEST_SLOTS
from tests.test_plan_rounds import day_one, projection, write
from tests.test_plan_store import unassigned_slot
from vending import agent_runs, dashboard, plan_store

PICKED_AT = datetime(2026, 11, 24, 20, 5, tzinfo=timezone.utc)      # Tue 24 Nov, 15:05 Eastern
SENT_AT = datetime(2026, 11, 28, 15, 0, tzinfo=timezone.utc)        # Sat 28 Nov, 10:00 Eastern
NOW = datetime(2026, 11, 28, 17, 0, tzinfo=timezone.utc)
PURCHASED, FED, OTHER = TEST_SLOTS[0], TEST_SLOTS[1], TEST_SLOTS[-1]
NOT_SENT = TEST_SLOTS[2]


@pytest.fixture
def before_lock(monkeypatch):
    monkeypatch.setattr(dashboard, "lock_at", lambda: NOW + timedelta(days=5))


@pytest.fixture
def picked(conn, tmp_path):
    """The picked first fill (round 3), and an agent run since with another plan for the same slots."""
    write(tmp_path, "round3", "3", {"day_one": day_one()}, projection())
    plan_id = plan_store.load(conn, tmp_path, "3", "Test Loader")
    plan_store.pick(conn, plan_id, "Test Picker")
    conn.execute("update action_log set at = %s where tool = 'plan_store.pick'", (PICKED_AT,))
    run = agent_runs.start(conn, mode="live")
    agent_runs.finish(conn, run, "skipped", plan={"slots": [
        {"slot": s, "product": "AGENT-DRY", "sale_price": "1.25", "fill_to": 3, "reason": "Dry-run reason."}
        for s in (PURCHASED, FED, OTHER)]})
    conn.execute("update agent_runs set started_at = %s, finished_at = %s where id = %s",
                 (NOW - timedelta(hours=2), NOW - timedelta(hours=1), run))
    return plan_id, run


def _choice(sku, store, price):
    return {"sku": sku, "store": store, "packs": 1, "units_per_pack": 24, "max_price_per_pack": "40.00",
            "sale_price": price, "change_product": True}


def send_fill(conn, plan_id, status="pending", request_id="req-fill-1"):
    """The day-one fill as `vending.fill` sends it: a `fill` submission, its request, and the gate's log row."""
    outside = unassigned_slot()
    body = {"off_cycle": False, "lines": [
        {"slot": PURCHASED, "source": "purchase", "primary": _choice("SIM-RB", "Walmart", "3.75"),
         "alternate": _choice("SIM-RB", "BJs", "3.75")},
        {"slot": FED, "source": "back_stock",
         "load": {"sku": "SIM-RB", "units": 10, "sale_price": "3.75", "change_product": True}},
        {"slot": OTHER, "source": "purchase", "primary": _choice("SIM-COKE", "Costco", "2.00")},
        {"slot": outside, "source": "purchase", "primary": _choice("ZZ-NOT-OURS", "Walmart", "9.87")}],
        "note": f"From slot {PURCHASED}'s purchase: {PURCHASED}=9, {FED}=10. Own purchase: {OTHER}=8, {outside}=5."}
    conn.execute("insert into purchase_submissions (at, plan_hash, body, status, request_id, purpose)"
                 " values (%s, 'h', %s, 'accepted', %s, 'fill')", (SENT_AT, Jsonb(body), request_id))
    conn.execute("insert into purchase_requests (request_id, submitted_at, off_cycle, plan_hash, request_body, status)"
                 " values (%s, %s, false, 'h', %s, %s)", (request_id, SENT_AT, Jsonb(body), status))
    conn.execute("insert into action_log (at, kind, tool, arguments, outcome, result)"
                 " values (%s, 'write', 'fill.submit', %s, 'done', %s)",
                 (SENT_AT, Jsonb({"by": "Test", "plan_id": plan_id, "label": "3", "expected_plan_hash": "h"}),
                  Jsonb({"success": True, "request_id": request_id, "status": "pending"})))


def vm_when(page: str) -> str:
    found = re.search(r'<span class="vm-when">(.*?)</span>', page)
    return found.group(1) if found else ""


def test_before_the_fill_is_sent_the_machine_shows_the_picked_first_fill(conn, picked, before_lock):
    plan_id, _ = picked
    snap = dashboard.snapshot(conn, NOW)
    view = dashboard.machine_view(snap)
    nxt = view[PURCHASED]["next"]
    assert (nxt["product"], nxt["units"], nxt["fill_to"]) == ("SIM-RB", 11, 11)
    assert dashboard._same_price(nxt["price"], "3.50")
    assert view[OTHER]["next"]["product"] == "SIM-COKE" and view[OTHER]["next"]["units"] == 8
    assert all(v["next"]["product"] != "AGENT-DRY" for v in view.values())
    # The pick's own fill checks, not the dry run's.
    first = {row["slot"]: row for row in snap["plan"]["data"]["slots"]}
    assert dashboard._fill_checks(snap) == {s: r["fill_check"] for s, r in first.items() if r.get("fill_check")}
    page = dashboard.render(snap)
    assert vm_when(page) == f"First fill &middot; round 3 &middot; plan #{plan_id} &middot; picked Tue 24 Nov, 15:05"
    assert "SIM-RB" in dashboard.machine_art.lane_products(snap, TEST_SLOTS)


def test_once_the_fill_is_sent_the_machine_shows_what_was_sent(conn, picked, before_lock):
    plan_id, _ = picked
    send_fill(conn, plan_id)
    snap = dashboard.snapshot(conn, NOW)
    view = dashboard.machine_view(snap)
    assert (view[PURCHASED]["next"]["product"], view[PURCHASED]["next"]["units"]) == ("SIM-RB", 9)
    assert dashboard._same_price(view[PURCHASED]["next"]["price"], "3.75")
    assert (view[FED]["next"]["product"], view[FED]["next"]["units"]) == ("SIM-RB", 10)
    assert (view[OTHER]["next"]["product"], view[OTHER]["next"]["units"]) == ("SIM-COKE", 8)
    assert view[NOT_SENT]["next"]["in_plan"] is False            # a slot the request doesn't load stays as it is
    assert all(v["next"]["product"] != "AGENT-DRY" for v in view.values())
    page = dashboard.render(snap)
    assert vm_when(page) == f"First fill sent &middot; round 3 &middot; plan #{plan_id} &middot; sent Sat 28 Nov, 10:00"
    facts = page[page.index(f'<div class="sd" id="sel-{PURCHASED}"'):]
    facts = facts[:facts.index('</dl>')]
    assert "<dt>Sent</dt><dd>SIM-RB · $3.75 · 9 units · Walmart, backup BJs</dd>" in facts
    # Nothing about a selection outside our assignment, even if a body named one.
    state = dashboard.to_json(snap)
    for text in (page, state):
        assert "ZZ-NOT-OURS" not in text and "9.87" not in text


def test_a_rejected_fill_goes_back_to_the_pick(conn, picked, before_lock):
    plan_id, _ = picked
    send_fill(conn, plan_id, status="rejected")
    snap = dashboard.snapshot(conn, NOW)
    assert dashboard.machine_view(snap)[PURCHASED]["next"]["units"] == 11
    assert vm_when(dashboard.render(snap)).startswith("First fill &middot; round 3")


def test_once_the_fill_is_counted_the_machine_shows_the_agents_plan(conn, picked, before_lock):
    plan_id, run = picked
    send_fill(conn, plan_id, status="fulfilled")
    snap = dashboard.snapshot(conn, NOW)
    view = dashboard.machine_view(snap)
    assert view[PURCHASED]["next"]["product"] == "AGENT-DRY" and view[PURCHASED]["next"]["units"] == 3
    assert view[PURCHASED]["reason"] == "Dry-run reason."
    when = vm_when(dashboard.render(snap))
    assert when.startswith("Next restock &middot; ") and when.endswith(f"&middot; run #{run}")
    # The status bar and the first-fill list still describe the first fill until the lock.
    assert snap["plan"]["data"]["id"] == plan_id
