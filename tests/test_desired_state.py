"""`preview_desired_state` on a throwaway schema with synthetic polls, receipts and ledger
entries, against a local HTTP server standing in for the bridge's catalog route, and
through the MCP client."""

import json
import threading
from datetime import datetime, timezone
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

psycopg = pytest.importorskip("psycopg")

from tests.conftest import store_poll  # noqa: E402
from tests.test_ledger import UNIT_COST, deliver, open_ledger  # noqa: E402
from tests.test_server import call, db  # noqa: E402,F401  (db is a fixture)
from vending import planning, server  # noqa: E402
from vending.bridge import Bridge  # noqa: E402
from vending.config import set_halted, set_value  # noqa: E402
from vending.planner import OUR_SLOTS  # noqa: E402
from vending.purchase_requests import record as record_request  # noqa: E402
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
                assert self.path == "/api/v1/products" and self.headers["X-API-Key"] == "test-key"
                fake.gets += 1
                self._reply(fake.catalog_status, {"products": CATALOG} if fake.catalog_body is None else fake.catalog_body)

            def do_POST(self):
                assert self.path == "/api/v1/purchase-requests" and self.headers["X-API-Key"] == "test-key"
                fake.posts.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
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
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

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
    """A ledger opened at $600, both products counted at `capacity` a column, and the halt switch off."""
    open_ledger(conn)
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
        slots=[by_slot.get(s, {"slot": s, "keep": True}) for s in OUR_SLOTS],
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


def test_an_old_poll_is_flagged_as_stale(db, fake):  # noqa: F811
    ready(db)
    stocked(db)                                     # the poll is 19 hours before NOW
    reply = preview_desired_state(db, desired(restock_15(), sourcing=[coke_sourcing()]), fake.client(), NOW)
    assert reply.success and any(n.startswith("STALE: the latest poll is 19.0 hours old") for n in reply.notes)
    fresh = preview_desired_state(db, desired(restock_15(), sourcing=[coke_sourcing()]), fake.client(),
                                  datetime(2026, 11, 8, 18, 0, tzinfo=timezone.utc))
    assert not any(n.startswith("STALE") for n in fresh.notes)


# --- Plans -----------------------------------------------------------------------------------

def test_with_nothing_counted_the_catalog_size_sets_the_capacity(db, fake):  # noqa: F811
    open_ledger(db)
    set_halted(db, False, "test")
    # Slot 15 is empty, and COKE20 is a "20 oz bottle", 7 a column on the loading chart.
    reply = preview_desired_state(db, desired(restock_15(fill_to=10), sourcing=[coke_sourcing()]), fake.client(), NOW)
    assert codes(reply) == ["over_capacity"] and "0-7" in reply.refusals[0].message
    reply = preview_desired_state(db, desired(restock_15(fill_to=None), sourcing=[coke_sourcing()]), fake.client(), NOW)
    assert reply.ok and reply.lines[0].fill_to == 7


def test_a_restock_plans_one_purchase_line_priced_against_its_floor(db, fake):  # noqa: F811
    ready(db, capacity=20)
    stocked(db)
    # 8 on hand, 3 in the closet: filling to 20 needs 12, more than the closet holds, so it buys a pack.
    reply = preview_desired_state(db, desired(restock_15(fill_to=20), sourcing=[coke_sourcing()]), fake.client(), NOW)
    assert reply.success and reply.ok and reply.plan_hash and reply.refusals == []
    [line] = reply.lines
    assert (line.slot, line.action, line.source, line.units_staying, line.units_to_load) == ("15", "restock", "purchase", 8, 12)
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
    ready(db, capacity=20)
    stocked(db)
    wanted = desired(restock_15(fill_to=20), off_cycle=True, sourcing=[coke_sourcing()])
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


# --- Through the MCP client -------------------------------------------------------------------

def test_preview_end_to_end(db, fake):  # noqa: F811
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
    assert config.main(["show"]) == 0
    out = capsys.readouterr().out
    assert 'capacity_overrides = {"COKE20": 8}' in out and "midweek_fee_cents = 3000" in out and "note" not in out and "halted = true" in out
    with open_in(*schema_url) as conn:
        assert planning.planner_config(conn).capacity_overrides == {"COKE20": 8}
        assert conn.execute("select count(*) from action_log where tool = 'config.set'").fetchone() == (2,)


@pytest.mark.parametrize("key, value, problem", [
    ("midweek_fee_cents", 25, "less than a dollar"),          # dollars typed: would bill 25 cents a visit
    ("midweek_fee_cents", -2500, "between 0 and 10000"),      # a visit that adds money
    ("midweek_fee_cents", "2500", "whole number of cents"),
    ("price_ceiling_cents", 1200, "over 1000 cents"),         # the API's $10.00 limit
    ("price_ceiling_cents", 603, "multiple of 5"),
    ("capacity_overrides", 10, "JSON object of SKU to units"),
    ("capacity_overrides", {"COKE20": 0}, "between 1 and 30"),
    ("capacity_overrides", {"COKE20": True}, "whole number of units"),
    ("capacity_overrides", {" ": 8}, "blank SKU"),
    ("tax_percent", 108, "between 0 and 100"),
    ("midweek_fee", 2500, "unknown setting"),
])
def test_config_refuses_values_that_would_be_charged_for_real(db, key, value, problem):  # noqa: F811
    with pytest.raises(ValueError, match=problem):
        set_value(db, key, value, "test")
    assert db.execute("select count(*) from config where key = %s", (key,)).fetchone() == (0,)
