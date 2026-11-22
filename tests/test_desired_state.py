"""`preview_desired_state` on a throwaway schema with synthetic polls, receipts and ledger
entries, against a local HTTP server standing in for the bridge's catalog route, and
through the MCP client."""

import json
import socket
import threading
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

psycopg = pytest.importorskip("psycopg")

from tests.conftest import store_poll  # noqa: E402
from tests.test_ledger import UNIT_COST, deliver, open_ledger  # noqa: E402
from tests.test_server import call, db  # noqa: E402,F401  (db is a fixture)
from tests.test_sync_requests import BODY, fulfilled, receipt  # noqa: E402
from vending import ledger, planning, server  # noqa: E402
from vending.bridge import Bridge  # noqa: E402
from vending.config import set_halted, set_value  # noqa: E402
from tests.conftest import TEST_SLOTS  # noqa: E402
from vending.purchase_requests import record as record_request, store  # noqa: E402
from vending.sync_catalog import sync as catalog_sync  # noqa: E402
from vending.tools import desired_state as schema  # noqa: E402
from vending.tools.preview import preview_desired_state  # noqa: E402

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)      # a Monday, before every cutoff
CATALOG = [
    {"sku": "COKE20", "name": "Coca-Cola", "flavor": "Original", "size": "20 oz bottle"},
    {"sku": "PICKLE", "name": "Pickle Potion", "size": "12 oz can"},
    {"sku": "OLD", "name": "Gone", "size": "20 oz", "active": False},
]
COSTCO = {"store": "Costco", "units_per_pack": 12, "max_price_per_pack": "13.00"}


class FakeBridge:
    """Serves the catalog; records every request. `catalog_status` other than 200 breaks the read."""

    def __init__(self):
        self.gets = 0
        self.posts: list[dict] = []
        self.catalog_status = 200
        self.catalog_body = None     # set to serve something other than {"products": CATALOG}
        self.cut = False             # answer the POST (with `answer`'s status) with a body shorter than its Content-Length
        self.answer = (201, {"id": "pr-1", "status": "pending", "created_at": "2026-11-09T12:00:00Z"})
        self.redirect: str | None = None  # answer the POST `303 See Other` to this path, which GETs as a 404
        self.drop = False            # close the connection once the POST has arrived, before any reply
        self.other_gets: list[str] = []  # GETs of anything but the catalog (a followed redirect, say)
        self.received = threading.Event()  # set once a POST has arrived
        self.hold: threading.Event | None = None  # if set, a POST isn't answered until this is
        self.raw: bytes | None = None  # answer the POST (with `answer`'s status) with exactly these bytes as its body
        self.lookups: dict[str, dict] = {}  # what `GET /api/v1/purchase-requests/{id}` answers, by ID; others are 404
        self.looked_up: list[str] = []  # every ID asked for that way (in `other_gets` too)
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                prefix = "/api/v1/purchase-requests/"
                if self.path.startswith(prefix):
                    asked = self.path[len(prefix):]
                    fake.looked_up.append(asked)
                    fake.other_gets.append(self.path)
                    if asked in fake.lookups:
                        self._reply(200, fake.lookups[asked])
                    else:
                        self._reply(404, {"detail": "Not Found"})
                    return
                if self.path != "/api/v1/products":
                    fake.other_gets.append(self.path)
                    self._reply(404, {"detail": "Not Found"})
                    return
                assert self.headers["X-API-Key"] == "test-key"
                fake.gets += 1
                self._reply(fake.catalog_status, {"products": CATALOG} if fake.catalog_body is None else fake.catalog_body)

            def do_POST(self):
                assert self.path == "/api/v1/purchase-requests" and self.headers["X-API-Key"] == "test-key"
                fake.posts.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                fake.received.set()
                if fake.hold is not None:
                    fake.hold.wait(30)
                if fake.drop:
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                if fake.redirect is not None:
                    self.send_response(303)
                    self.send_header("Location", fake.redirect)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if fake.raw is not None:
                    self.send_response(fake.answer[0])
                    self.send_header("Content-Length", str(len(fake.raw)))
                    self.end_headers()
                    self.wfile.write(fake.raw)
                    return
                if fake.cut:
                    data = b'{"id": "pr-1", "status": "pen'
                    self.send_response(fake.answer[0])
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data) + 40))
                    self.end_headers()
                    self.wfile.write(data)
                    self.wfile.flush()
                    self.close_connection = True
                    return
                self._reply(*fake.answer)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True).start()

    def client(self) -> Bridge:
        return Bridge(self.url, "test-key")


@pytest.fixture
def fake(db, monkeypatch):  # noqa: F811
    bridge = FakeBridge()
    monkeypatch.setattr(server, "bridge", bridge.client)
    yield bridge
    bridge.server.shutdown()
    bridge.server.server_close()


def ready(conn, capacity=10):
    """A ledger opened at $600, both products counted at `capacity` a column, and the halt switch off. An old
    poll read every other slot's price, as every real poll does: a kept slot whose price no poll ever read, and
    that no receipt shows empty, is refused (`price_unknown`). Slot 15, the one the tests restock, is left to
    their own polls, so its counters start where they say."""
    open_ledger(conn)
    store_poll(conn, "old", "2026-11-01T12:00:00+00:00", {s: (200, 0) for s in TEST_SLOTS if s != "15"})
    set_value(conn, "capacity_overrides", {"COKE20": capacity, "PICKLE": capacity}, "test")
    set_halted(conn, False, "test")


def stocked(conn):
    """Slot 15 holds 8 COKE20 at 1237/11 cents each, priced $2.50; 3 more are in the closet."""
    deliver(conn, "r1")
    store_poll(conn, "a", "2026-11-08T17:00:00+00:00", {"15": (250, 100)})


def desired(*changes: dict, off_cycle=False, sourcing=()) -> schema.DesiredState:
    """Every slot kept, except the ones given."""
    by_slot = {c["slot"]: c for c in changes}
    return schema.DesiredState(
        slots=[by_slot.get(s, {"slot": s, "keep": True}) for s in TEST_SLOTS],
        off_cycle=off_cycle, sourcing=list(sourcing))


def restock_15(price="2.50", fill_to=10, product="COKE20") -> dict:
    return {"slot": "15", "product": product, "sale_price": price, "fill_to": fill_to}


def coke_sourcing(**primary) -> dict:
    return {"product": "COKE20", "primary": {**COSTCO, **primary}}


def codes(reply) -> list[str]:
    return sorted(r.code for r in reply.refusals)


# --- Not ready ---------------------------------------------------------------------------

def test_without_an_opening_entry_it_says_how_to_open_the_ledger(db, fake):  # noqa: F811
    reply = preview_desired_state(db, desired(restock_15()), fake.client(), NOW)
    assert not reply.success and "python -m vending.ledger open" in reply.message


def test_an_unreadable_catalog_is_not_ready(db, fake):  # noqa: F811
    ready(db)
    fake.catalog_status = 503
    reply = preview_desired_state(db, desired(restock_15()), fake.client(), NOW)
    assert not reply.success and "couldn't read the catalog" in reply.message


@pytest.mark.parametrize("body", [{"products": []}, [], {"items": CATALOG}, {"products": [{"name": "no sku"}]}])
def test_an_empty_or_reshaped_catalog_is_not_ready_rather_than_all_unapproved(db, fake, body):  # noqa: F811
    ready(db)
    fake.catalog_body = body
    reply = preview_desired_state(db, desired(restock_15()), fake.client(), NOW)
    assert not reply.success and "empty or unreadable catalog" in reply.message
    assert reply.refusals == []


def test_halted_with_the_bridge_down_says_to_stop_not_to_check_tailscale(db, fake):  # noqa: F811
    ready(db)
    set_halted(db, True, "test")
    fake.catalog_status = 503
    reply = preview_desired_state(db, desired(restock_15()), fake.client(), NOW)
    assert not reply.success and "halt switch is on" in reply.message and "vending.halt off" in reply.message


# --- Where the catalog comes from ------------------------------------------------------------

def synced(conn, fake, at=NOW):
    """Our copy of the catalog, synced from the fake bridge `at` that time."""
    catalog_sync(conn, fake.client(), now=at, log=lambda _: None)


def test_with_no_synced_copy_the_catalog_is_read_from_the_bridge_and_says_so(db, fake):  # noqa: F811
    ready(db)
    stocked(db)
    reply = preview_desired_state(db, desired(restock_15(), sourcing=[coke_sourcing()]), fake.client(), NOW)
    assert reply.success and fake.gets == 1
    assert any("read live from the bridge" in n and "vending.sync_catalog" in n for n in reply.notes)


def test_a_synced_copy_is_used_and_the_bridge_is_not_called(db, fake):  # noqa: F811
    ready(db)
    stocked(db)
    synced(db, fake)
    fake.gets = 0
    fake.catalog_status = 503                       # a bridge outage no longer stops planning
    reply = preview_desired_state(db, desired(restock_15(), sourcing=[coke_sourcing()]), fake.client(), NOW)
    assert reply.success and fake.gets == 0
    assert not any("catalog" in n for n in reply.notes)


def test_the_synced_copy_keeps_inactive_and_delisted_products_out(db, fake):  # noqa: F811
    ready(db)
    synced(db, fake, NOW - timedelta(hours=2))
    fake.catalog_body = {"products": [p for p in CATALOG if p["sku"] != "PICKLE"]}
    synced(db, fake)                                # PICKLE is now delisted
    fake.catalog_status = 503
    bad = desired({"slot": "15", "product": "OLD", "sale_price": "2.50", "fill_to": 10},
                  {"slot": "16", "product": "PICKLE", "sale_price": "2.50", "fill_to": 10})
    reply = preview_desired_state(db, bad, fake.client(), NOW)
    unknown = {r.slot for r in reply.refusals if r.code == "product_unknown"}
    assert unknown == {"15", "16"}


def test_an_old_synced_copy_is_used_but_flagged_as_stale(db, fake):  # noqa: F811
    ready(db)
    stocked(db)
    synced(db, fake, NOW - timedelta(hours=40))
    fake.gets = 0
    reply = preview_desired_state(db, desired(restock_15(), sourcing=[coke_sourcing()]), fake.client(), NOW)
    assert reply.success and fake.gets == 0
    assert any(n.startswith("STALE: the catalog was last synced 40.0 hours ago") for n in reply.notes)


def test_a_copy_with_every_product_delisted_falls_back_to_the_bridge(db, fake):  # noqa: F811
    ready(db)
    synced(db, fake)
    db.execute("update catalog_products set delisted_at = %s", (NOW,))
    fake.gets = 0
    fake.catalog_status = 503
    reply = preview_desired_state(db, desired(restock_15()), fake.client(), NOW)
    assert fake.gets == 1 and not reply.success and "couldn't read the catalog" in reply.message


def test_an_old_poll_is_flagged_as_stale(db, fake):  # noqa: F811
    ready(db)
    stocked(db)                                     # the poll is 19 hours before NOW
    reply = preview_desired_state(db, desired(restock_15(), sourcing=[coke_sourcing()]), fake.client(), NOW)
    assert reply.success and any(n.startswith("STALE: the latest poll is 19.0 hours old") for n in reply.notes)
    fresh = preview_desired_state(db, desired(restock_15(), sourcing=[coke_sourcing()]), fake.client(),
                                  datetime(2026, 11, 8, 18, 0, tzinfo=timezone.utc))
    assert not any(n.startswith("STALE") for n in fresh.notes)


def test_the_planner_counts_a_cost_recorded_just_now_when_the_host_clock_is_behind(db, monkeypatch):  # noqa: F811
    # The planner's spending check must see the same balance as get_account_balance,
    # including the model call recorded a moment ago by the database clock.
    class HostBehind(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) - timedelta(hours=1)

    monkeypatch.setattr(planning, "datetime", HostBehind)
    ready(db)
    ledger.record_tokens(db, "250.00", "m", 1, 1, "dev")
    assert planning.gather(db, []).current.balance_cents == 35000


# --- Plans -----------------------------------------------------------------------------------

def test_with_nothing_counted_the_catalog_size_sets_the_capacity(db, fake):  # noqa: F811
    open_ledger(db)
    store_poll(db, "old", "2026-11-01T12:00:00+00:00", {s: (200, 0) for s in TEST_SLOTS if s != "15"})
    set_halted(db, False, "test")
    # Slot 15 is empty, and COKE20 is a "20 oz bottle", 7 a column on the loading chart.
    reply = preview_desired_state(db, desired(restock_15(fill_to=10), sourcing=[coke_sourcing()]), fake.client(), NOW)
    assert codes(reply) == ["over_capacity"] and "0-7" in reply.refusals[0].message
    reply = preview_desired_state(db, desired(restock_15(fill_to=None), sourcing=[coke_sourcing()]), fake.client(), NOW)
    assert reply.ok and reply.lines[0].fill_to == 7


@pytest.mark.parametrize("fill_to", [6, 8, None])
def test_preview_warns_if_a_load_leaves_room(db, fake, fill_to):  # noqa: F811
    ready(db, capacity=8)
    slot = TEST_SLOTS[0]
    reply = preview_desired_state(
        db, desired({**restock_15(fill_to=fill_to), "slot": slot}, sourcing=[coke_sourcing()]), fake.client(), NOW)
    assert reply.success
    checks = [w for w in reply.warnings if "spaces empty" in w]
    assert checks == ([f"slot {slot}: fills 6 of 8: 2 spaces empty"] if fill_to == 6 else [])


def test_preview_describes_unknown_capacity_without_an_underfill_warning(db, fake):  # noqa: F811
    ready(db)
    set_value(db, "capacity_overrides", {}, "test")
    fake.catalog_body = {"products": [{**CATALOG[0], "size": "11 oz carton"}]}
    reply = preview_desired_state(
        db, desired(restock_15(fill_to=6), sourcing=[coke_sourcing()]), fake.client(), NOW)
    assert codes(reply) == ["unknown_capacity"]
    assert "capacity not counted" in reply.refusals[0].message
    assert not any("spaces empty" in w for w in reply.warnings)


def test_a_restock_plans_one_purchase_line_priced_against_its_floor(db, fake):  # noqa: F811
    ready(db, capacity=15)
    stocked(db)
    # 8 on hand, 3 in the closet: filling to 15 needs 7, more than the closet holds, so it buys a pack.
    reply = preview_desired_state(db, desired(restock_15(fill_to=15), sourcing=[coke_sourcing()]), fake.client(), NOW)
    assert reply.success and reply.ok and reply.plan_hash and reply.refusals == []
    [line] = reply.lines
    assert (line.slot, line.action, line.source, line.units_staying, line.units_to_load) == ("15", "restock", "purchase", 8, 7)
    assert any("3 COKE20 in the closet go unused" in w for w in reply.warnings)
    assert line.api["primary"] == {"sku": "COKE20", "store": "Costco", "packs": 1, "units_per_pack": 12,
                                   "max_price_per_pack": "13.00", "sale_price": "2.50", "change_product": False}
    # On hand at 1237/11 cents -> $1.25 floor; the pack at 13.00/12 -> $1.20. The slot takes the higher.
    [floor] = reply.floors
    assert (floor.slot, floor.floor) == ("15", "1.25")
    assert sorted(p.source for p in floor.parts) == ["on hand", "primary Costco 12-pack, max $13.00"]
    # Spendable: 600.00 opening - 12.37 receipt, no sales yet; max cost one pack at 13.00.
    assert (reply.cost.max, reply.cost.spendable, reply.cost.spendable_left_at_max) == ("13.00", "587.63", "574.63")
    assert "Shopping list" in reply.directive and "Costco" in reply.directive
    assert reply.prices_as_of == datetime(2026, 11, 8, 17, 0, tzinfo=timezone.utc)
    assert any("no receipts yet for slots 16" in n for n in reply.notes)


def test_a_price_below_the_floor_is_refused_with_the_plan_still_shown(db, fake):  # noqa: F811
    ready(db)
    stocked(db)
    reply = preview_desired_state(db, desired(restock_15(price="1.20"), sourcing=[coke_sourcing()]), fake.client(), NOW)
    assert not reply.success and not reply.ok and reply.plan_hash is None
    assert codes(reply) == ["below_floor"] and "1.25" in reply.refusals[0].message
    assert len(reply.lines) == 1 and reply.cost is not None
    assert "Plan refused: 1 problem (below_floor)" in reply.message


def test_the_floor_clears_the_dearest_units_staying_not_their_average(db, fake):  # noqa: F811
    """A price must clear every cost a slot carries (`slot_floor_cents`), so units staying at two
    costs set two floors, not one at their average (#172 item 2)."""
    ready(db, capacity=15)
    stocked(db)                                     # 8 on hand at 1237/11 cents -> $1.25 floor
    record_request(db, "r2", NOW - timedelta(days=1), False, BODY)
    store(db, "r2", fulfilled("r2", receipt(units_bought=2, units_per_pack=2, total_paid="6.00", unit_cost_paid="3.00",
                                            units_loaded=2, units_lost=0, loss_note=None, units_to_back_stock=0,
                                            resolved_at="2026-11-08T18:00:00Z"), total_paid="6.00"), NOW)
    # 2 more at $3.00 -> $3.25 floor. Their average, 1499.6/10 cents, would floor at $1.65.
    reply = preview_desired_state(db, desired(restock_15(fill_to=11)), fake.client(), NOW)
    assert codes(reply) == ["below_floor"] and "3.25" in reply.refusals[0].message
    [floor] = reply.floors
    assert sorted(p.floor for p in floor.parts) == ["1.25", "1.25", "3.25"]   # on hand twice, back stock once


def test_every_problem_is_reported_at_once(db, fake):  # noqa: F811
    ready(db)
    stocked(db)
    bad = schema.DesiredState(slots=[{"slot": "15", "product": "OLD", "sale_price": "2.53", "fill_to": 10},
                                     {"slot": "16", "product": "PICKLE", "sale_price": "3.00"}])
    reply = preview_desired_state(db, bad, fake.client(), NOW)
    assert "slot_list" in codes(reply)             # 18 slots missing
    assert "product_unknown" in codes(reply)       # OLD is inactive
    assert "no_sourcing" in codes(reply)           # PICKLE has no sourcing and the closet is empty
    assert "price_off_step" not in codes(reply)    # slot 15's plan failed earlier, so its price isn't checked twice


def test_a_pending_request_refuses_a_new_plan(db, fake):  # noqa: F811
    ready(db)
    stocked(db)
    record_request(db, "pr-0", NOW, False, {"off_cycle": False, "lines": []})
    reply = preview_desired_state(db, desired(restock_15(), sourcing=[coke_sourcing()]), fake.client(), NOW)
    assert codes(reply) == ["request_pending"] and "pr-0" in reply.refusals[0].message


def test_the_halt_switch_refuses_the_plan(db, fake):  # noqa: F811
    ready(db)
    set_halted(db, True, "test")
    reply = preview_desired_state(db, desired(restock_15(), sourcing=[coke_sourcing()]), fake.client(), NOW)
    assert codes(reply) == ["halted"]


def test_a_back_stock_load_costs_nothing(db, fake):  # noqa: F811
    ready(db)
    stocked(db)                                     # 3 COKE20 in the closet
    reply = preview_desired_state(db, desired({"slot": "16", "product": "COKE20", "sale_price": "2.50", "fill_to": 3}),
                                  fake.client(), NOW)
    assert reply.success, reply.refusals
    [line] = reply.lines
    assert (line.slot, line.action, line.source, line.units_to_load) == ("16", "switch", "back_stock", 3)
    assert line.api == {"slot": "16", "source": "back_stock",
                        "load": {"sku": "COKE20", "units": 3, "sale_price": "2.50", "change_product": True}}
    assert reply.cost.max == "0.00"
    [floor] = reply.floors
    assert floor.parts[0].unit_cost == f"{float(UNIT_COST) / 100:.4f}"


def test_a_midweek_visit_adds_the_fee(db, fake):  # noqa: F811
    ready(db, capacity=15)
    stocked(db)
    wanted = desired(restock_15(fill_to=15), off_cycle=True, sourcing=[coke_sourcing()])
    reply = preview_desired_state(db, wanted, fake.client(), NOW)
    assert reply.success and (reply.cost.visit_fee, reply.cost.max) == ("25.00", "38.00")
    set_value(db, "midweek_fee_cents", 3000, "test")
    reply = preview_desired_state(db, wanted, fake.client(), NOW)
    assert reply.cost.visit_fee == "30.00"


def test_a_top_up_the_closet_covers_loads_from_back_stock(db, fake):  # noqa: F811
    ready(db)
    stocked(db)                                     # 8 on hand, 3 in the closet, rail holds 10
    reply = preview_desired_state(db, desired(restock_15(), sourcing=[coke_sourcing()]), fake.client(), NOW)
    assert reply.success, reply.refusals
    [line] = reply.lines
    assert (line.action, line.source, line.units_to_load) == ("restock", "back_stock", 2)
    assert reply.cost.max == "0.00"


def test_nothing_to_load_is_refused(db, fake):  # noqa: F811
    ready(db)
    stocked(db)
    reply = preview_desired_state(db, desired(), fake.client(), NOW)
    assert codes(reply) == ["nothing_to_load"]


# --- The sourcing table (#132) ----------------------------------------------------------------

def table_row(conn, fake, checked=NOW - timedelta(days=2), **changes):
    """COKE20's primary in the sourcing table: Costco, 12 a pack, $11.49 (max $13.50)."""
    from vending import sourcing
    synced(conn, fake)
    fields = dict(sku="COKE20", rank="primary", store="Costco", units_per_pack=12, expected_cents=1149,
                  max_cents=1350, url=None, pack_description="12 x 20 oz bottles", checked_at=checked,
                  checked_by="test")
    sourcing.set_option(conn, sourcing.Option(**{**fields, **changes}), NOW)


def test_with_no_sourcing_given_the_plan_buys_from_the_table(db, fake):  # noqa: F811
    ready(db, capacity=15)
    stocked(db)
    table_row(db, fake)
    reply = preview_desired_state(db, desired(restock_15(fill_to=15)), fake.client(), NOW)
    assert reply.success and reply.ok and reply.refusals == []
    [line] = reply.lines
    assert line.api["primary"] == {"sku": "COKE20", "store": "Costco", "packs": 1, "units_per_pack": 12,
                                   "max_price_per_pack": "13.50", "sale_price": "2.50", "change_product": False,
                                   "pack_description": "12 x 20 oz bottles"}
    assert (reply.cost.expected, reply.cost.max) == ("11.49", "13.50")   # the table's expected price, not just the max
    assert not any("sourcing table" in w for w in reply.warnings)       # checked 2 days ago: fresh


def test_a_table_price_over_a_week_old_is_used_but_flagged(db, fake):  # noqa: F811
    ready(db, capacity=15)
    stocked(db)
    table_row(db, fake, checked=NOW - timedelta(days=9))
    reply = preview_desired_state(db, desired(restock_15(fill_to=15)), fake.client(), NOW)
    assert reply.ok
    [stale] = [w for w in reply.warnings if "sourcing table" in w]
    assert "COKE20" in stale and "9 days ago" in stale and "python -m vending.sourcing set COKE20" in stale


def test_the_agents_own_max_wins_but_an_old_borrowed_expected_price_is_named(db, fake):  # noqa: F811
    """Marshall, #133: the agent's Costco 12-pack gives no expected price, so the planner borrows the
    table's $11.49 for the cost. The line keeps the agent's max; the 30-day-old price is named."""
    ready(db, capacity=15)
    stocked(db)
    table_row(db, fake, checked=NOW - timedelta(days=30))
    reply = preview_desired_state(db, desired(restock_15(fill_to=15), sourcing=[coke_sourcing()]), fake.client(), NOW)
    assert reply.ok and reply.lines[0].api["primary"]["max_price_per_pack"] == "13.00"
    assert reply.cost.expected == "11.49"
    [stale] = [w for w in reply.warnings if "sourcing table" in w]
    assert "your primary option gave no expected price" in stale
    assert "Costco 12 a pack at $11.49 (max $13.50), was checked 2026-10-10, 30 days ago" in stale


def test_the_agents_own_expected_price_borrows_nothing_and_says_nothing(db, fake):  # noqa: F811
    ready(db, capacity=15)
    stocked(db)
    table_row(db, fake, checked=NOW - timedelta(days=30))
    mine = coke_sourcing(expected_price_per_pack="12.25")
    reply = preview_desired_state(db, desired(restock_15(fill_to=15), sourcing=[mine]), fake.client(), NOW)
    assert reply.ok and reply.cost.expected == "12.25"
    assert not any("sourcing table" in w for w in reply.warnings)


def test_a_max_below_a_borrowed_expected_price_says_it_came_from_the_table(db, fake):  # noqa: F811
    ready(db, capacity=15)
    stocked(db)
    table_row(db, fake, expected_cents=1349, max_cents=1400)
    reply = preview_desired_state(db, desired(restock_15(fill_to=15), sourcing=[coke_sourcing()]), fake.client(), NOW)
    [refusal] = [r for r in reply.refusals if r.code == "bad_sourcing"]
    assert "max_price_per_pack $13.00 is below the sourcing table's expected $13.49" in refusal.message


def test_the_tables_alternate_reaches_the_request_and_an_old_one_is_named(db, fake):  # noqa: F811
    from vending import sourcing
    ready(db, capacity=15)
    stocked(db)
    table_row(db, fake)                                                   # a fresh Costco primary
    sourcing.set_option(db, sourcing.Option(
        sku="COKE20", rank="alternate", store="Walmart", units_per_pack=12, expected_cents=1199, max_cents=1400,
        url=None, pack_description=None, checked_at=NOW - timedelta(days=10), checked_by="test"), NOW)
    reply = preview_desired_state(db, desired(restock_15(fill_to=15)), fake.client(), NOW)
    assert reply.ok
    api = reply.lines[0].api
    assert (api["primary"]["store"], api["alternate"]["store"]) == ("Costco", "Walmart")
    assert api["alternate"]["max_price_per_pack"] == "14.00"
    [stale] = [w for w in reply.warnings if "sourcing table" in w]
    assert "the sourcing table's alternate price, Walmart 12 a pack at $11.99 (max $14.00), was checked" in stale
    assert "10 days ago" in stale


def test_an_old_row_for_a_product_the_plan_doesnt_buy_says_nothing(db, fake):  # noqa: F811
    ready(db)
    stocked(db)
    table_row(db, fake, checked=NOW - timedelta(days=30))
    reply = preview_desired_state(db, desired({"slot": "15", "keep": True, "sale_price": "2.75"}), fake.client(), NOW)
    assert not any("sourcing table" in w for w in reply.warnings)


def test_no_sourcing_anywhere_says_how_to_add_a_table_row(db, fake):  # noqa: F811
    ready(db)
    stocked(db)
    reply = preview_desired_state(db, desired({"slot": "16", "product": "PICKLE", "sale_price": "3.00"}),
                                  fake.client(), NOW)
    [refusal] = [r for r in reply.refusals if r.code == "no_sourcing"]
    assert "python -m vending.sourcing set" in refusal.message


# --- Through the MCP client -------------------------------------------------------------------

def test_preview_end_to_end(db, fake, monkeypatch):  # noqa: F811
    # Through the MCP server "now" is the database clock: pinned to NOW, a Monday before every cutoff, so
    # the Costco restock isn't refused past_cutoff late in the week (as tests/test_submit.py pins it).
    monkeypatch.setattr(ledger, "db_now", lambda conn: NOW)
    ready(db)
    stocked(db)
    body = desired(restock_15(), sourcing=[coke_sourcing()]).model_dump()
    result = call("preview_desired_state", {"desired": body})
    assert not result.isError
    reply = result.structuredContent
    assert reply["success"] and reply["ok"] and reply["lines"][0]["slot"] == "15"
    assert db.execute("select kind, tool, outcome from action_log where tool = 'preview_desired_state'").fetchall() == [
        ("read", "preview_desired_state", "done")]
    refused = call("preview_desired_state", {"desired": desired(restock_15(price="1.20"), sourcing=[coke_sourcing()]).model_dump()})
    assert refused.structuredContent["success"] is False
    assert db.execute("select outcome from action_log where tool = 'preview_desired_state' order by id").fetchall() == [
        ("done",), ("failed",)]


def test_preview_through_the_client_with_nothing_set_up(db, fake):  # noqa: F811
    reply = call("preview_desired_state", {"desired": desired(restock_15()).model_dump()}).structuredContent
    assert reply["success"] is False and "Can't plan yet" in reply["message"]


# --- The config CLI ---------------------------------------------------------------------------------

def test_config_cli_sets_and_shows(schema_url, monkeypatch, capsys):
    from tests.conftest import open_in
    from vending import config, db as db_module
    monkeypatch.setattr(db_module, "connect", lambda: open_in(*schema_url))
    assert config.main(["set", "capacity_overrides", '{"COKE20": 8}', "--by", "marshall"]) == 0
    assert config.main(["set", "midweek_fee_cents", "3000", "--by", "marshall"]) == 0
    assert config.main(["set", "note", "free text", "--by", "marshall"]) == 1       # unknown key
    assert "unknown setting 'note'" in capsys.readouterr().err
    assert config.main(["set", "halted", "false", "--by", "marshall"]) == 1
    with open_in(*schema_url) as conn:      # left over from before the loading chart
        conn.execute("insert into config (key, value, updated_by) values ('slot_capacity', '10', 'marshall')")
    assert config.main(["show"]) == 0
    out = capsys.readouterr().out
    assert 'capacity_overrides = {"COKE20": 8}' in out and "midweek_fee_cents = 3000" in out and "note" not in out and "halted = true" in out
    [old] = [line for line in out.splitlines() if line.startswith("slot_capacity")]
    assert old.endswith("[no longer used]") and "[no longer used]" not in out.replace(old, "")
    with open_in(*schema_url) as conn:
        assert planning.planner_config(conn).capacity_overrides == {"COKE20": 8}
        assert conn.execute("select count(*) from action_log where tool = 'config.set'").fetchone() == (2,)


def test_config_cli_names_counts_a_new_override_object_drops(schema_url, monkeypatch, capsys):
    from tests.conftest import open_in
    from vending import config, db as db_module
    monkeypatch.setattr(db_module, "connect", lambda: open_in(*schema_url))
    assert config.main(["set", "capacity_overrides", '{"COKE20": 8, "PICKLE": 6}', "--by", "marshall"]) == 0
    capsys.readouterr()
    # Someone sets one SKU, forgetting `set` replaces the whole object: PICKLE's count is lost.
    assert config.main(["set", "capacity_overrides", '{"COKE20": 9}', "--by", "kevin"]) == 0
    err = capsys.readouterr().err
    assert 'Removed counts that were in the old value: {"PICKLE": 6}' in err
    assert config.main(["set", "capacity_overrides", '{"COKE20": 9, "PICKLE": 6}', "--by", "kevin"]) == 0
    assert "Removed" not in capsys.readouterr().err


@pytest.mark.parametrize("key, value, problem", [
    ("midweek_fee_cents", 25, "less than a dollar"),          # dollars typed: would bill 25 cents a visit
    ("midweek_fee_cents", -2500, "between 0 and 10000"),      # a visit that adds money
    ("midweek_fee_cents", "2500", "whole number of cents"),
    ("price_ceiling_cents", 1200, "over 1000 cents"),         # the API's $10.00 limit
    ("price_ceiling_cents", 603, "multiple of 5"),
    ("capacity_overrides", 10, "JSON object of SKU to units"),
    ("capacity_overrides", {"COKE20": 0}, "between 1 and 15"),
    ("capacity_overrides", {"COKE20": 18}, "between 1 and 15"),       # 18 typed for an 8-can column
    ("capacity_overrides", {"COKE20": True}, "whole number of units"),
    ("capacity_overrides", {" ": 8}, "blank SKU"),
    ("capacity_overrides", {"COKE20 ": 8}, "has a space"),            # would never match the catalog
    ("capacity_overrides", {"COKE 20": 8}, "has a space"),
    ("tax_percent", 108, "between 0 and 100"),
    ("midweek_fee", 2500, "unknown setting"),
])
def test_config_refuses_values_that_would_be_charged_for_real(db, key, value, problem):  # noqa: F811
    with pytest.raises(ValueError, match=problem):
        set_value(db, key, value, "test")
    assert db.execute("select count(*) from config where key = %s", (key,)).fetchone() == (0,)
