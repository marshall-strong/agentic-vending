"""The tracking check: our four figures as of a poll (`vending.tracking`), the once-a-day send and its
record, the client method (`Bridge.check_books`) and the simulated bridge's route. No network."""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime, timedelta, timezone
from fractions import Fraction

import pytest

from vending import settings
from vending.bridge import Bridge, BridgeAnswered, BridgeError, BridgeNoAnswer, BridgeUnreachable
from vending.simulate import bridge as sim
from vending.simulate.bridge import SimBridge
from vending.simulate.world import Machine
from vending.start import EASTERN

from tests.conftest import TEST_SLOTS

SKU = "COKE20"          # the receipts fixture's product; the simulated bridge is given it as its catalog


@pytest.fixture(autouse=True)
def _daily_setting_unset(monkeypatch):
    """The once-a-day start at its default, whatever a developer's `.env` says."""
    from vending import tracking

    monkeypatch.delenv(tracking.DAILY_SETTING, raising=False)
    monkeypatch.setattr(settings, "_FILE", {k: v for k, v in settings._FILE.items() if k != tracking.DAILY_SETTING})


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(timezone.utc)


# --- The simulated bridge's route (no database) ---------------------------------------------------------

class Clock:
    def __init__(self, at: datetime):
        self.at = at

    def __call__(self) -> datetime:
        return self.at


AS_OF = utc("2026-12-04T15:00:00+00:00")


def sim_bridge(count_tolerance="per_entry", at=AS_OF + timedelta(hours=2)) -> tuple[SimBridge, Clock]:
    clock = Clock(at)
    machine = Machine.empty(TEST_SLOTS)
    for slot in TEST_SLOTS:
        machine.slots[slot].sku, machine.slots[slot].units = SKU, 8
    machine.closet[SKU] = 12
    return SimBridge(machine, clock, catalog=[{"sku": SKU, "active": True}], count_tolerance=count_tolerance), clock


def good_body(**changes) -> dict:
    body = {"as_of": "s1", "cash": "500.00", "sales_tax_owed": "10.00",
            "coil": {slot: 8 for slot in TEST_SLOTS}, "back_stock": {SKU: 12}}
    return {**body, **changes}


def refusal(call) -> tuple[int, dict]:
    with pytest.raises(BridgeAnswered) as caught:
        call()
    return caught.value.status, json.loads(caught.value.detail)


def test_sim_before_the_books_begin_it_answers_tracking_not_started():
    b, _ = sim_bridge()
    status, answer = refusal(lambda: b.check_books(good_body()))
    assert (status, answer["error_type"]) == (409, "tracking_not_started")
    assert b.refused[-1]["status"] == 409 and not b.checks


def test_sim_within_at_exactly_one_dollar_and_two_units_outside_past_them():
    b, _ = sim_bridge()
    b.daily_limit_from = None                          # two checks in one day: the tolerances, not the limit
    b.record_books("s1", AS_OF, 50000, 1000)
    answer = b.check_books(good_body(cash="499.00", sales_tax_owed=11,
                                     coil={**{s: 8 for s in TEST_SLOTS}, "15": 10, "16": 6}, back_stock={SKU: 14}))
    assert answer["results"] == {"cash": "within", "sales_tax_owed": "within", "coil": "within",
                                 "back_stock": "within"}
    assert answer["as_of"] == "s1" and answer["as_of_time"] == "2026-12-04T15:00:00Z"
    assert answer["next_check_from"] == "2026-12-05T05:00:00Z"         # midnight Eastern
    answer = b.check_books(good_body(cash="498.99", sales_tax_owed="11.01",
                                     coil={**{s: 8 for s in TEST_SLOTS}, "15": 11}, back_stock={}))
    assert answer["results"] == {"cash": "outside", "sales_tax_owed": "outside", "coil": "outside",
                                 "back_stock": "outside"}   # 12 in the closet, 0 sent
    assert [c["as_of"] for c in b.checks] == ["s1", "s1"]


def test_sim_count_tolerance_per_entry_or_total():
    body = good_body(coil={**{s: 8 for s in TEST_SLOTS}, "15": 10, "16": 10})   # 2 off in each of two slots
    per_entry, _ = sim_bridge("per_entry")
    total, _ = sim_bridge("total")
    for b in (per_entry, total):
        b.record_books("s1", AS_OF, 50000, 1000)
    assert per_entry.check_books(body)["results"]["coil"] == "within"
    assert total.check_books(body)["results"]["coil"] == "outside"
    with pytest.raises(ValueError):
        sim_bridge("net")


def test_sim_refuses_an_unknown_poll_one_before_the_books_and_one_not_reconciled():
    b, _ = sim_bridge()
    b.record_books("s1", AS_OF, 50000, 1000)
    b.record_books("s0", AS_OF - timedelta(days=1), 50000, 1000)
    b.record_books("s2", AS_OF + timedelta(hours=1), 50000, 1000, reconciled=False)
    assert refusal(lambda: b.check_books(good_body(as_of="nope")))[1]["error_type"] == "unknown_poll"
    assert refusal(lambda: b.check_books(good_body(as_of="nope")))[0] == 404
    assert refusal(lambda: b.check_books(good_body(as_of="s0"))) [1]["error_type"] == "before_tracking_start"
    assert refusal(lambda: b.check_books(good_body(as_of="s2"))) == (409, {
        "error": "not reconciled yet", "error_type": "not_reconciled_yet",
        "detail": "polls are folded in on the hour", "items": []})


def test_sim_refuses_a_coil_that_is_not_exactly_our_slots_and_an_unknown_sku():
    b, _ = sim_bridge()
    b.record_books("s1", AS_OF, 50000, 1000)
    coil = {s: 8 for s in TEST_SLOTS if s != "15"} | {"11": 3}
    status, answer = refusal(lambda: b.check_books(good_body(coil=coil)))
    assert (status, answer["error_type"], answer["items"]) == (422, "invalid_coil", ["11: not yours", "15: missing"])
    status, answer = refusal(lambda: b.check_books(good_body(back_stock={"NOPE": 1})))
    assert (status, answer["error_type"], answer["items"]) == (422, "unknown_sku", ["NOPE"])
    assert not b.checks                                # none of these used the check


@pytest.mark.parametrize("change", [
    {"extra": 1}, {"cash": None}, {"cash": True}, {"coil": {"15": 1.5}}, {"coil": {"123456789": 1}},
    {"back_stock": []}, {"as_of": ""}, {"cash": 100001},
])
def test_sim_refuses_a_body_off_the_schema_as_fastapi_would(change):
    b, _ = sim_bridge()
    b.record_books("s1", AS_OF, 50000, 1000)
    status, answer = refusal(lambda: b.check_books(good_body(**change)))
    assert status == 422 and isinstance(answer["detail"], list) and answer["detail"]


def test_sim_by_default_allows_one_check_a_day_from_the_competition_start(monkeypatch):
    """Not told otherwise, the simulated bridge limits checks from the same moment as the client."""
    from vending import tracking

    b, clock = sim_bridge(at=utc("2026-12-02T15:00:00+00:00"))
    assert b.daily_limit_from == tracking.daily_from() == datetime(2026, 12, 3, tzinfo=EASTERN)
    b.record_books("s1", AS_OF, 50000, 1000)
    b.check_books(good_body())
    b.check_books(good_body())                         # before the start: testing, no limit
    clock.at = AS_OF + timedelta(hours=2)              # Fri 4 Dec
    b.check_books(good_body())
    assert refusal(lambda: b.check_books(good_body()))[1]["error_type"] == "daily_check_used"
    monkeypatch.setenv(tracking.DAILY_SETTING, "2026-12-10T00:00:00-05:00")
    assert sim_bridge()[0].daily_limit_from == datetime(2026, 12, 10, tzinfo=EASTERN)


def test_sim_one_check_a_day_from_the_limit_and_refusals_do_not_use_it():
    b, clock = sim_bridge()
    b.daily_limit_from = None                          # no limit: testing
    b.record_books("s1", AS_OF, 50000, 1000)
    b.check_books(good_body())
    b.check_books(good_body())
    b.daily_limit_from = AS_OF
    clock.at = AS_OF + timedelta(days=1)               # a new Eastern day
    refusal(lambda: b.check_books(good_body(as_of="nope")))     # refused: doesn't use it
    b.check_books(good_body())
    status, answer = refusal(lambda: b.check_books(good_body()))
    assert (status, answer["error_type"], answer["next_check_from"]) == (429, "daily_check_used",
                                                                          "2026-12-06T05:00:00Z")
    clock.at = utc("2026-12-06T05:00:00+00:00")        # midnight Eastern: a new day
    assert b.check_books(good_body())["results"]["cash"] == "within"


def test_sim_post_routes_the_tracking_path_and_notes_one_call():
    b, _ = sim_bridge()
    b.record_books("s1", AS_OF, 50000, 1000)
    assert b.post("/api/v1/tracking/check", good_body())["results"]["coil"] == "within"
    assert b.calls == [("POST", "/api/v1/tracking/check")]


def test_sim_books_snapshot_the_machine_when_not_given():
    b, _ = sim_bridge()
    books = b.record_books("s1", AS_OF, 50000, 1000)
    b.machine.slots["15"].units = 0                    # later sales don't change the books as of s1
    assert books.coil["15"] == 8 and books.back_stock == {SKU: 12}


# --- The real client's method (faked at `urlopen`) ------------------------------------------------------

class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_check_books_posts_the_body_once_and_keeps_a_long_error_answer_whole(monkeypatch):
    from vending import bridge as bridge_module

    seen = []
    items = [f"{n}: missing" for n in range(11, 59)]
    error = {"error": "invalid coil", "error_type": "invalid_coil", "detail": "x" * 400, "items": items}

    def urlopen(request, timeout):
        seen.append(request)
        if len(seen) == 1:
            return FakeResponse(b'{"results": {}}')
        raise urllib.error.HTTPError(request.full_url, 422, "Unprocessable", {},
                                     io.BytesIO(json.dumps(error).encode()))

    monkeypatch.setattr(bridge_module.urllib.request, "urlopen", urlopen)
    client = Bridge("http://bridge.test", "key")
    assert client.check_books({"as_of": "s1"}) == {"results": {}}
    assert seen[0].get_method() == "POST" and seen[0].full_url == "http://bridge.test/api/v1/tracking/check"
    assert json.loads(seen[0].data) == {"as_of": "s1"}
    with pytest.raises(BridgeAnswered) as caught:
        client.check_books({"as_of": "s1"})
    assert caught.value.status == 422 and json.loads(caught.value.detail) == error   # > 300 bytes, still whole
    assert len(seen) == 2                              # never retried


# --- Our figures, from the database ---------------------------------------------------------------------

OPENED = utc("2026-11-01T00:00:00+00:00")
FILLED_AT = "2026-11-02T16:00:00Z"
PRICE = 250


def open_ledger(conn, at=OPENED):
    from vending import ledger
    return ledger.record(conn, "opening", 60000, "opening capital", "marshall", at)


def fill(conn, slots=TEST_SLOTS, request_id="fill", total_paid="12.37", resolved_at=FILLED_AT):
    """One fulfilled request for `slots`: in each, 12 bought, 8 loaded, 3 to the closet, 1 broken."""
    from tests.test_sync_requests import BODY, receipt
    from vending.purchase_requests import record, store

    primary = BODY["lines"][0]["primary"]
    body = {"off_cycle": False, "lines": [{"slot": s, "source": "purchase", "primary": primary} for s in slots]}
    record(conn, request_id, OPENED + timedelta(hours=1), False, body)
    store(conn, request_id, {
        "id": request_id, "status": "fulfilled", "device_id": "vmc1", "created_at": "2026-11-01T01:00:00Z",
        "updated_at": resolved_at, "fulfilled_at": resolved_at, "off_cycle": False, "note": None,
        "decision_note": None, "total_paid": "0",
        "lines": [{"line": i, "slot": s, "source": "purchase", "primary": primary, "alternate": None, "load": None,
                   "receipt": receipt(total_paid=total_paid, resolved_at=resolved_at)}
                  for i, s in enumerate(slots)]}, OPENED + timedelta(days=3))


def poll(conn, session_id, started_at, sold_in_15=0, device_id="vmc1"):
    from tests.conftest import store_poll
    store_poll(conn, session_id, started_at,
               {s: (PRICE, 100 + (sold_in_15 if s == "15" else 0)) for s in TEST_SLOTS}, device_id=device_id)


def books(conn):
    """Opening, a fill of all 20 of our slots, and three polls: 2 sold from slot 15 by p1, 5 by p2."""
    open_ledger(conn)
    fill(conn)
    poll(conn, "p0", "2026-11-02T18:00:00+00:00")
    poll(conn, "p1", "2026-11-03T18:00:00+00:00", sold_in_15=2)
    poll(conn, "p2", "2026-11-04T18:00:00+00:00", sold_in_15=5)


def test_figures_are_the_ledger_and_the_stock_as_of_the_poll(conn):
    from vending import ledger, tracking

    books(conn)
    f = tracking.figures(conn, "p1")
    at = utc("2026-11-03T18:00:00+00:00")
    b = ledger.balance(conn, at)
    assert (f.as_of, f.as_of_time) == ("p1", at)
    assert (f.cash_cents, f.tax_owed_cents) == (b.spendable_cents, b.tax_owed_cents)
    # 60000 opening + 500 takings - 20 x 1237 receipts, less the tax inside 500 (37): 35723
    assert (f.cash_cents, f.tax_owed_cents) == (60000 + 500 - 20 * 1237 - 37, 37)
    assert f.coil == {s: (6 if s == "15" else 8) for s in TEST_SLOTS}
    assert list(f.coil) == list(TEST_SLOTS)            # every one of our slots, in the server's order
    assert f.back_stock == {SKU: 60}                   # 3 from each of 20 lines
    assert f.body() == {"as_of": "p1", "cash": "357.23", "sales_tax_owed": "0.37", "coil": f.coil,
                        "back_stock": {SKU: 60}}


def test_figures_default_to_the_newest_good_poll_and_later_sales_do_not_count(conn):
    from vending import tracking

    books(conn)
    conn.execute("insert into polls (session_id, device_id, started_at, raw, byte_count, crc_ok)"
                 " values ('bad', 'vmc1', '2026-11-05T18:00:00+00:00', '', 0, false)")
    assert tracking.figures(conn).as_of == "p2"
    assert tracking.figures(conn).coil["15"] == 3
    assert tracking.figures(conn, "p0").coil["15"] == 8


def swap_15(conn, opened="2026-11-03T18:00:00Z", closed="2026-11-03T19:00:00Z"):
    """A visit that loads another product into slot 15 (pulling nothing), the door open from `opened` to
    `closed`: a product change, so the stock replay splits sales around it."""
    from tests.test_sync_requests import BODY, receipt
    from vending.purchase_requests import record, store

    primary = {**BODY["lines"][0]["primary"], "sku": "PEPSI20", "change_product": True}
    record(conn, "swap", OPENED + timedelta(days=2), False,
           {"off_cycle": False, "lines": [{"slot": "15", "source": "purchase", "primary": primary}]})
    store(conn, "swap", {
        "id": "swap", "status": "fulfilled", "device_id": "vmc1", "created_at": "2026-11-03T12:00:00Z",
        "updated_at": closed, "fulfilled_at": closed, "off_cycle": False, "note": None, "decision_note": None,
        "total_paid": "0", "door_opened_at": opened, "door_closed_at": closed,
        "lines": [{"line": 0, "slot": "15", "source": "purchase", "primary": primary, "alternate": None,
                   "load": None, "receipt": receipt(sku="PEPSI20", resolved_at=closed)}]},
          OPENED + timedelta(days=4))


def test_a_later_poll_does_not_change_the_coil_as_of_an_earlier_one(conn):
    """p1 is taken in the minute the door opens for a swap; a poll before the door closes shows 3 more
    sold. The stock replay moves those sales to door open, before p1, unless only polls up to p1 count."""
    from vending import tracking

    open_ledger(conn)
    fill(conn)
    poll(conn, "p0", "2026-11-02T18:00:00+00:00")
    poll(conn, "p1", "2026-11-03T18:00:00+00:00", sold_in_15=2)
    swap_15(conn)
    assert tracking.figures(conn, "p1").coil["15"] == 6
    poll(conn, "p1b", "2026-11-03T18:30:00+00:00", sold_in_15=5)     # door still open
    assert tracking.figures(conn, "p1").coil["15"] == 6


def test_a_poll_sharing_its_time_with_another_of_our_polls_is_refused(conn):
    """A second good poll of our machine at p1's exact `started_at`, showing 5 sold, turned p1's coil
    for slot 15 from 6 into 3: the replay cuts at the time and can't tell the two apart, and the bridge
    gives no order between them. So neither is used. A poll of another machine at that time is fine."""
    from vending import tracking

    books(conn)
    poll(conn, "elsewhere", "2026-11-03T18:00:00+00:00", sold_in_15=5, device_id="vmc2")
    assert tracking.figures(conn, "p1").coil["15"] == 6
    poll(conn, "p1tie", "2026-11-03T18:00:00+00:00", sold_in_15=5)
    for session in ("p1", "p1tie"):
        with pytest.raises(tracking.CantCompute, match=r"shares its time .* --as-of\. Nothing was sent"):
            tracking.figures(conn, session)
    assert tracking.figures(conn, "p2").coil["15"] == 3
    poll(conn, "p2tie", "2026-11-04T18:00:00+00:00", sold_in_15=5)
    with pytest.raises(tracking.CantCompute, match="shares its time"):
        tracking.figures(conn)                         # the newest poll, by default


def test_a_tied_poll_committed_after_the_tie_check_does_not_change_the_figures(schema_url, monkeypatch):
    """The tie check and every figure read see one snapshot. A tied poll committed by another connection
    between the tie check and the stock read used to slip through: slot 15 read 3, not 6."""
    from tests.conftest import open_in
    from vending import tracking

    with open_in(*schema_url) as setup:
        books(setup)
    real_slots = tracking.our_slots

    def slots_after_a_tie_lands(conn, *args, **kwargs):
        with open_in(*schema_url) as other:            # autocommit: committed before the reads below
            poll(other, "p1tie", "2026-11-03T18:00:00+00:00", sold_in_15=5)
        return real_slots(conn, *args, **kwargs)

    monkeypatch.setattr(tracking, "our_slots", slots_after_a_tie_lands)
    with open_in(*schema_url) as conn:
        f = tracking.figures(conn, "p1")
        assert f.coil["15"] == 6
        monkeypatch.setattr(tracking, "our_slots", real_slots)
        with pytest.raises(tracking.CantCompute, match="shares its time"):
            tracking.figures(conn, "p1")               # the next read sees the tie and refuses


def test_a_good_poll_missing_a_counter_for_one_of_our_slots_is_refused(conn):
    """p1 passed its checksum but has no counter for slot 15: as of it the coil read 8 (not 6) and the
    tax owed $0.00 (not $0.37), because the sales up to it couldn't be seen. Nothing is sent."""
    from tests.conftest import store_poll
    from vending import tracking

    open_ledger(conn)
    fill(conn)
    poll(conn, "p0", "2026-11-02T18:00:00+00:00")
    store_poll(conn, "p1", "2026-11-03T18:00:00+00:00", {s: (PRICE, 100) for s in TEST_SLOTS if s != "15"})
    poll(conn, "p2", "2026-11-04T18:00:00+00:00", sold_in_15=5)
    with pytest.raises(tracking.CantCompute, match="Poll p1 has no counter for our slots 15, so"):
        tracking.figures(conn, "p1")
    store_poll(conn, "p3", "2026-11-05T18:00:00+00:00", {s: (PRICE, 105) for s in TEST_SLOTS if s not in ("15", "58")})
    with pytest.raises(tracking.CantCompute, match="Poll p3 has no counter for our slots 15, 58"):
        tracking.figures(conn)                         # the newest poll, by default
    assert tracking.figures(conn, "p2").coil["15"] == 3


def test_the_coil_is_our_slots_from_the_assignment_only(conn):
    from vending import tracking

    books(conn)
    fill(conn, slots=("11",), request_id="other")       # a receipt for a selection that isn't ours
    ours = [s for s in TEST_SLOTS if s != "58"] + ["11"]
    conn.execute("update slot_assignment set slots = %s", (ours,))
    from tests.conftest import store_poll
    store_poll(conn, "p1x", "2026-11-03T19:00:00+00:00",
               {s: (PRICE, 102 if s == "15" else 100) for s in [*TEST_SLOTS, "11"]})
    f = tracking.figures(conn, "p1x")
    assert list(f.coil) == ours and "58" not in f.coil


@pytest.mark.parametrize("case, match", [
    ("unknown", "No poll nope is stored"),
    ("device", "not our machine"),
    ("crc", "failed its checksum"),
    ("before", "before the ledger's opening"),
])
def test_figures_refuse_a_poll_they_cannot_use(conn, case, match):
    from vending import tracking

    books(conn)
    if case == "device":
        poll(conn, "x", "2026-11-03T18:00:00+00:00", device_id="vmc2")
    elif case == "crc":
        conn.execute("insert into polls (session_id, device_id, started_at, raw, byte_count, crc_ok)"
                     " values ('x', 'vmc1', '2026-11-03T19:00:00+00:00', '', 0, false)")
    elif case == "before":
        poll(conn, "x", "2026-10-31T18:00:00+00:00")
    with pytest.raises(tracking.CantCompute, match=match):
        tracking.figures(conn, "nope" if case == "unknown" else "x")


def test_figures_refuse_with_no_poll_no_opening_or_no_assignment(conn):
    from vending import tracking
    from vending.slots import SlotsNotRecorded

    with pytest.raises(tracking.CantCompute, match="No good poll"):
        tracking.figures(conn)
    poll(conn, "p0", "2026-11-02T18:00:00+00:00")
    with pytest.raises(tracking.CantCompute, match="no opening entry"):
        tracking.figures(conn)
    conn.execute("delete from slot_assignment")
    with pytest.raises(SlotsNotRecorded):
        tracking.figures(conn)


def test_figures_refuse_a_slot_no_receipt_has_loaded(conn):
    from vending import tracking

    open_ledger(conn)
    fill(conn, slots=TEST_SLOTS[:-2])
    poll(conn, "p0", "2026-11-02T18:00:00+00:00")
    with pytest.raises(tracking.CantCompute, match=f"slots {TEST_SLOTS[-2]}, {TEST_SLOTS[-1]}"):
        tracking.figures(conn)


def test_figures_refuse_a_receipt_with_no_total_paid(conn):
    from vending import tracking

    open_ledger(conn)
    fill(conn)
    conn.execute("update purchase_lines set total_paid = null where slot = '15'")
    poll(conn, "p0", "2026-11-02T18:00:00+00:00")
    with pytest.raises(tracking.CantCompute, match="1 receipt lines have no total_paid"):
        tracking.figures(conn)


def test_figures_refuse_a_count_that_is_a_range(conn, monkeypatch):
    from vending import stock, tracking

    books(conn)
    real = stock.stock

    def ranged(conn, until=None, lines=None, **kw):
        counted = real(conn, until=until, lines=lines, **kw)
        return stock.Stock(counted.closet, tuple(
            stock.SlotCount(**{**c.__dict__, "units": c.units - 2, "unattributed": 2, "at_most": c.units})
            if c.slot == "16" else c for c in counted.slots), counted.warnings)

    monkeypatch.setattr(stock, "stock", ranged)
    with pytest.raises(tracking.CantCompute, match=r"16 \(6 to 8\)"):
        tracking.figures(conn, "p1")


def test_figures_pass_on_warnings_about_our_slots_and_the_closet_only(conn, monkeypatch):
    from vending import stock, tracking

    books(conn)
    real = stock.stock

    def warned(conn, until=None, lines=None, **kw):
        counted = real(conn, until=until, lines=lines, **kw)
        return stock.Stock(counted.closet, counted.slots, (
            stock.StockWarning("slot 15: ours", "15"), stock.StockWarning("slot 11: not ours", "11"),
            stock.StockWarning("the closet: ours", None)))

    monkeypatch.setattr(stock, "stock", warned)
    notes = tracking.figures(conn, "p1").notes
    assert "slot 15: ours" in notes and "the closet: ours" in notes
    assert not any("not ours" in n for n in notes)


# --- The send and its record ----------------------------------------------------------------------------

class FakeBridge:
    """`check_books` answers from a list: a dict is a 200, an exception is raised."""

    def __init__(self, *answers):
        self.answers, self.sent = list(answers), []

    def check_books(self, body):
        self.sent.append(body)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


WITHIN = {"as_of": "p1", "as_of_time": "2026-11-03T18:00:00Z", "checked_at": "2026-12-04T15:00:00Z",
          "results": {"cash": "within", "sales_tax_owed": "within", "coil": "outside", "back_stock": "within"},
          "next_check_from": "2026-12-05T05:00:00Z"}


def refused(status, error_type, **more):
    return BridgeAnswered(status, json.dumps({"error": error_type, "error_type": error_type, **more}))


BEFORE = utc("2026-11-20T15:00:00+00:00")      # testing: no daily limit
DURING = utc("2026-12-04T15:00:00+00:00")      # Fri 4 Dec 10:00 Eastern


def rows(conn):
    return conn.execute("select status, error_type, used_check, results, eastern_day, sent_by, as_of"
                        " from tracking_checks order by id").fetchall()


def test_a_send_stores_the_request_then_the_answer(conn):
    from vending import tracking

    books(conn)
    f = tracking.figures(conn, "p1")
    bridge = FakeBridge(WITHIN)
    out = tracking.send(conn, bridge, f, "kevin", now=DURING)
    assert bridge.sent == [f.body()]
    assert (out.status, out.used_check, out.results) == (200, True, WITHIN["results"])
    assert out.next_check_from == utc("2026-12-05T05:00:00+00:00")
    [row] = rows(conn)
    assert row == (200, None, True, WITHIN["results"], DURING.astimezone(EASTERN).date(), "kevin", "p1")
    request, answer, as_of_time = conn.execute(
        "select request, answer, as_of_time from tracking_checks").fetchone()
    assert request == f.body() and answer == WITHIN and as_of_time == f.as_of_time
    assert set(request["coil"]) == set(TEST_SLOTS)


def test_from_the_start_a_second_send_the_same_day_is_refused(conn):
    from vending import tracking

    books(conn)
    f = tracking.figures(conn, "p1")
    bridge = FakeBridge(WITHIN)
    tracking.send(conn, bridge, f, "kevin", now=DURING)
    with pytest.raises(tracking.AlreadyChecked, match="#1: 200"):
        tracking.send(conn, bridge, f, "kevin", now=DURING + timedelta(hours=8))   # 18:00 Eastern, same day
    assert len(bridge.sent) == 1 and len(rows(conn)) == 1
    nxt = FakeBridge(WITHIN)
    tracking.send(conn, nxt, f, "kevin", now=utc("2026-12-05T05:00:00+00:00"))      # midnight Eastern
    assert len(nxt.sent) == 1


def test_a_send_refused_before_the_check_leaves_the_day_open(conn):
    from vending import tracking

    books(conn)
    f = tracking.figures(conn, "p1")
    bridge = FakeBridge(refused(409, "not_reconciled_yet", items=[]), refused(422, "invalid_coil", items=["15"]),
                        WITHIN)
    first = tracking.send(conn, bridge, f, "kevin", now=DURING)
    assert (first.status, first.error_type, first.used_check, first.results) == (409, "not_reconciled_yet", False, None)
    assert tracking.send(conn, bridge, f, "kevin", now=DURING).answer["items"] == ["15"]
    assert tracking.send(conn, bridge, f, "kevin", now=DURING).status == 200
    assert [r[:3] for r in rows(conn)] == [(409, "not_reconciled_yet", False), (422, "invalid_coil", False),
                                          (200, None, True)]


def test_a_429_is_stored_as_the_day_used(conn):
    from vending import tracking

    books(conn)
    f = tracking.figures(conn, "p1")
    out = tracking.send(conn, FakeBridge(refused(429, "daily_check_used", next_check_from="2026-12-05T05:00:00Z")),
                        f, "kevin", now=DURING)
    assert (out.status, out.used_check, out.next_check_from) == (429, True, utc("2026-12-05T05:00:00+00:00"))
    with pytest.raises(tracking.AlreadyChecked):
        tracking.send(conn, FakeBridge(WITHIN), f, "kevin", now=DURING)


def test_no_usable_answer_may_have_used_the_check_but_never_arriving_did_not(conn):
    from vending import tracking

    books(conn)
    f = tracking.figures(conn, "p1")
    out = tracking.send(conn, FakeBridge(BridgeUnreachable("down")), f, "kevin", now=DURING)
    assert (out.status, out.error_type, out.used_check) == (None, "unreachable", False)
    out = tracking.send(conn, FakeBridge(BridgeNoAnswer("timed out")), f, "kevin", now=DURING)
    assert (out.status, out.error_type, out.used_check) == (None, "no_answer", None)
    with pytest.raises(tracking.AlreadyChecked, match="may have used it"):
        tracking.send(conn, FakeBridge(WITHIN), f, "kevin", now=DURING)
    out = tracking.send(conn, FakeBridge(BridgeAnswered(502, "Bad Gateway")), f, "kevin",
                        now=DURING + timedelta(days=1))
    assert (out.status, out.used_check, out.answer) == (502, None, {"unparsed": "Bad Gateway"})


def eastern(*parts) -> datetime:
    return datetime(*parts, tzinfo=EASTERN).astimezone(timezone.utc)


def wall(moment: datetime, **fields) -> datetime:
    """`moment` re-stamped to a wall time in Eastern (say hour=5, minute=59), as UTC."""
    return moment.astimezone(EASTERN).replace(**fields).astimezone(timezone.utc)


def next_midnight(moment: datetime) -> datetime:
    """Midnight Eastern after `moment`: the bridge's `next_check_from`."""
    return eastern(*(moment.astimezone(EASTERN).date() + timedelta(days=1)).timetuple()[:3])


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class Times:
    """The clock a send reads: one moment for the reservation, the next for the answer."""

    def __init__(self, *moments):
        self.moments = list(moments)

    def __call__(self) -> datetime:
        return self.moments.pop(0) if len(self.moments) > 1 else self.moments[0]


# Eastern days a send can cross midnight into: an ordinary one, and the days either side of each clock
# change (spring forward Sun 14 Mar 2027, fall back Sun 7 Nov 2027).
MIDNIGHTS = [(2026, 12, 5), (2027, 3, 14), (2027, 3, 15), (2027, 11, 7), (2027, 11, 8)]


@pytest.mark.parametrize("day", MIDNIGHTS)
def test_a_send_the_bridge_checked_after_midnight_uses_the_new_days_check(conn, day):
    """Reserved at 20:59:59, held by the bridge, and checked by it at 00:00:01: the check used is the
    new day's."""
    from vending import tracking

    books(conn)
    f = tracking.figures(conn, "p1")
    midnight = eastern(*day)
    checked = midnight + timedelta(seconds=1)
    answer = {**WITHIN, "checked_at": iso(checked), "next_check_from": iso(next_midnight(midnight))}
    out = tracking.send(conn, FakeBridge(answer), f, "kevin",
                        now=Times(midnight - timedelta(hours=3, seconds=1), checked))
    assert out.status == 200
    with pytest.raises(tracking.AlreadyChecked):
        tracking.send(conn, FakeBridge(WITHIN), f, "kevin", now=wall(midnight, hour=10))
    assert len(rows(conn)) == 1


def test_a_send_is_never_started_after_the_dispatch_deadline(conn):
    """The request is never started later than 21:00 Eastern. A request started by then, given up
    by the client after its 20 second timeout, is over three hours from midnight, so the bridge
    cannot check it on the next Eastern day. Past the deadline the send is refused and nothing is
    recorded: the reservation is left unused. The next morning is a fine time to send, so the
    deadline guards the evening only."""
    from vending import tracking

    books(conn)
    f = tracking.figures(conn, "p1")
    for moment in (utc("2026-12-04T02:00:00+00:00"),          # 21:00:00 Eastern, exactly
                   utc("2026-12-04T02:00:01+00:00"),          # a second past it
                   utc("2026-12-04T04:59:00+00:00")):         # 23:59 Eastern, the last minute
        with pytest.raises(tracking.DispatchClosed, match="21:00"):
            tracking.send(conn, FakeBridge(WITHIN), f, "kevin", now=moment)
    assert rows(conn) == []
    out = tracking.send(conn, FakeBridge(WITHIN), f, "kevin", now=utc("2026-12-05T09:30:00+00:00"))
    assert (out.status, out.used_check) == (200, True)
    assert [r[:1] for r in rows(conn)] == [(200,)]


@pytest.mark.parametrize("day", MIDNIGHTS)
def test_a_send_still_unanswered_across_midnight_blocks_the_new_day(conn, day):
    """A request the bridge held past its give-up stays without a usable answer, so it may still be
    checked by the bridge on the next day: nothing else may send for that day until the morning
    window opens at 06:00 Eastern."""
    from vending import tracking

    books(conn)
    f = tracking.figures(conn, "p1")
    midnight = eastern(*day)

    class Slow(FakeBridge):
        def check_books(self, body):
            # Just past midnight the held request blocks the new day already (it may still be
            # checked there), and nothing new is recorded.
            with pytest.raises(tracking.AlreadyChecked, match="used or may be"):
                tracking.send(conn, FakeBridge(WITHIN), f, "kevin", now=midnight + timedelta(seconds=1))
            assert len(rows(conn)) == 1
            raise BridgeNoAnswer("timed out")

    out = tracking.send(conn, Slow(), f, "kevin", now=Times(midnight - timedelta(hours=3, seconds=1),
                                                            midnight - timedelta(hours=3, seconds=1)
                                                            + timedelta(seconds=20)))
    assert (out.status, out.used_check) == (None, None)
    # 05:59 Eastern: the held request may still reach the bridge, so the new day stays blocked.
    with pytest.raises(tracking.AlreadyChecked, match="used or may be"):
        tracking.send(conn, FakeBridge(WITHIN), f, "kevin", now=wall(midnight, hour=5, minute=59))
    # 06:00 Eastern opens the morning window: by it the request is hours past its start.
    assert tracking.send(conn, FakeBridge(WITHIN), f, "kevin", now=wall(midnight, hour=6)).status == 200
    assert len(rows(conn)) == 2


@pytest.mark.parametrize("answer", [
    BridgeNoAnswer("timed out"),
    BridgeNoAnswer("ConnectionResetError: the connection ended with no answer"),
    {**WITHIN, "results": {**WITHIN["results"], "coil": "close"}},
    BridgeAnswered(503, "Service Unavailable"),
], ids=["timeout", "reset-after-send", "malformed-200", "5xx"])
@pytest.mark.parametrize("recorded", [timedelta(seconds=-5), timedelta(hours=3, seconds=10)],
                         ids=["before-midnight", "after-midnight"])
def test_an_ambiguous_answer_just_before_midnight_still_blocks_the_new_day(conn, answer, recorded):
    """Sent at 20:59:50 with no usable answer, recorded before or after midnight (a reply held for
    three hours): the bridge may still have checked it after midnight, using the new day's check.
    The new day stays blocked at least until the morning window opens; a reply recorded after
    midnight keeps it blocked all morning, and the day after is free."""
    from vending import tracking

    books(conn)
    f = tracking.figures(conn, "p1")
    midnight = eastern(2026, 12, 5)
    out = tracking.send(conn, FakeBridge(answer), f, "kevin",
                        now=Times(midnight - timedelta(hours=3, seconds=10), midnight + recorded))
    assert out.used_check is None
    # 05:59 Eastern: still blocked - it may have reached the bridge after midnight.
    with pytest.raises(tracking.AlreadyChecked):
        tracking.send(conn, FakeBridge(WITHIN), f, "kevin", now=wall(midnight, hour=5, minute=59))
    try:
        tracking.send(conn, FakeBridge(WITHIN), f, "kevin", now=wall(midnight, hour=10))
        assert recorded < timedelta(0)          # recorded before midnight: the morning block lifts
    except tracking.AlreadyChecked:
        assert recorded >= timedelta(0)         # recorded after midnight: it may have used either day
    assert tracking.send(conn, FakeBridge(WITHIN), f, "kevin", now=next_midnight(midnight)).status == 200


def test_a_send_the_bridge_checked_before_midnight_leaves_the_new_day_open(conn):
    """Reserved at 20:59:58 and checked at 20:59:59, answered a moment later: the old day's check, so
    the new day is free."""
    from vending import tracking

    books(conn)
    f = tracking.figures(conn, "p1")
    midnight = eastern(2026, 12, 5)
    answer = {**WITHIN, "checked_at": iso(midnight - timedelta(seconds=1)), "next_check_from": iso(midnight)}
    tracking.send(conn, FakeBridge(answer), f, "kevin",
                  now=Times(midnight - timedelta(hours=3, seconds=2), midnight - timedelta(hours=3, seconds=1)))
    assert tracking.send(conn, FakeBridge(WITHIN), f, "kevin", now=wall(midnight, hour=10)).status == 200


def test_an_unanswered_send_left_behind_by_a_crash_blocks_only_its_own_days(conn):
    """A row with no answer at all (the process died mid-send) blocks the days it could have reached the
    bridge in, not every day after."""
    from vending import tracking

    books(conn)
    f = tracking.figures(conn, "p1")
    midnight = eastern(2026, 12, 5)

    class Crash(FakeBridge):
        def check_books(self, body):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        tracking.send(conn, Crash(), f, "kevin", now=midnight - timedelta(hours=3, minutes=10))
    # Past the deadline nothing new is sent; the Friday row may still reach the bridge, so:
    with pytest.raises(tracking.DispatchClosed):
        tracking.send(conn, FakeBridge(WITHIN), f, "kevin", now=midnight - timedelta(minutes=1))
    with pytest.raises(tracking.AlreadyChecked):
        tracking.send(conn, FakeBridge(WITHIN), f, "kevin", now=wall(midnight, hour=5, minute=59))
    # By 06:00 the morning window opens, and by then the request is hours old, so Saturday is free.
    assert tracking.send(conn, FakeBridge(WITHIN), f, "kevin", now=wall(midnight, hour=10)).status == 200
    with pytest.raises(KeyboardInterrupt):
        tracking.send(conn, Crash(), f, "kevin", now=midnight + timedelta(days=1, hours=20, minutes=50))
    tomorrow = next_midnight(midnight)
    with pytest.raises(tracking.AlreadyChecked):
        tracking.send(conn, FakeBridge(WITHIN), f, "kevin", now=wall(tomorrow, hour=5, minute=59))
    assert tracking.send(conn, FakeBridge(WITHIN), f, "kevin",
                         now=wall(tomorrow + timedelta(days=1), hour=10)).status == 200


@pytest.mark.parametrize("answer", [
    {**WITHIN, "results": {**WITHIN["results"], "coil": "close"}},
    {**WITHIN, "results": {k: v for k, v in WITHIN["results"].items() if k != "back_stock"}},
    {**WITHIN, "results": ["within"] * 4},
    {k: v for k, v in WITHIN.items() if k != "checked_at"},
    {k: v for k, v in WITHIN.items() if k != "next_check_from"},
    {k: v for k, v in WITHIN.items() if k != "as_of_time"},
    {**WITHIN, "checked_at": "yesterday"},
    {**WITHIN, "as_of": "p2"},                         # not the poll we sent
    ["within", "within", "within", "within"],
], ids=["verdict", "missing-verdict", "results-list", "no-checked_at", "no-next_check_from", "no-as_of_time",
        "bad-checked_at", "other-poll", "not-an-object"])
def test_a_malformed_200_is_not_a_success_and_may_have_used_the_check(conn, answer):
    from vending import tracking

    books(conn)
    f = tracking.figures(conn, "p1")
    out = tracking.send(conn, FakeBridge(answer), f, "kevin", now=DURING)
    assert (out.status, out.results, out.used_check, out.error_type) == (200, None, None, "malformed_answer")
    assert rows(conn)[0][:4] == (200, "malformed_answer", None, None)
    with pytest.raises(tracking.AlreadyChecked):
        tracking.send(conn, FakeBridge(WITHIN), f, "kevin", now=DURING)


def test_before_the_start_sends_are_not_limited(conn):
    from vending import tracking

    books(conn)
    f = tracking.figures(conn, "p1")
    bridge = FakeBridge(WITHIN, WITHIN, WITHIN)
    for _ in range(3):
        tracking.send(conn, bridge, f, "kevin", now=BEFORE)
    assert len(bridge.sent) == 3


def test_the_daily_start_is_a_setting(conn, monkeypatch):
    from vending import tracking

    assert tracking.daily_from() == datetime(2026, 12, 3, tzinfo=EASTERN)
    monkeypatch.setenv(tracking.DAILY_SETTING, "2026-11-20T00:00:00-05:00")
    books(conn)
    f = tracking.figures(conn, "p1")
    checked = {**WITHIN, "checked_at": iso(BEFORE), "next_check_from": iso(next_midnight(BEFORE))}
    tracking.send(conn, FakeBridge(checked), f, "kevin", now=BEFORE)
    with pytest.raises(tracking.AlreadyChecked):
        tracking.send(conn, FakeBridge(WITHIN), f, "kevin", now=BEFORE)
    monkeypatch.setenv(tracking.DAILY_SETTING, "3 Dec")
    with pytest.raises(settings.SettingsError, match="TRACKING_DAILY_FROM"):
        tracking.daily_from()


def test_a_send_needs_a_name(conn):
    from vending import tracking

    books(conn)
    with pytest.raises(ValueError):
        tracking.send(conn, FakeBridge(WITHIN), tracking.figures(conn, "p1"), " ", now=DURING)
    assert rows(conn) == []


def test_figures_and_the_simulated_bridge_agree_end_to_end(conn):
    """Our figures sent to the simulated bridge holding the same books: all within."""
    from vending import tracking

    books(conn)
    f = tracking.figures(conn, "p1")
    b, clock = sim_bridge(at=DURING)
    b.record_books("p1", f.as_of_time, 60000 + 500 - 20 * 1237 - 37, 37,
                   coil={s: (6 if s == "15" else 8) for s in TEST_SLOTS}, back_stock={SKU: 61})
    out = tracking.send(conn, b, f, "kevin", now=DURING)
    assert out.results == {"cash": "within", "sales_tax_owed": "within", "coil": "within", "back_stock": "within"}
    clock.at = DURING + timedelta(days=1)
    out = tracking.send(conn, b, f, "kevin", now=clock.at)
    assert out.status == 200
    assert b.checks[-1]["as_of"] == "p1"


# --- The command line -----------------------------------------------------------------------------------

def test_check_prints_the_figures_and_sends_nothing(schema_url, monkeypatch, capsys):
    from tests.conftest import open_in
    from vending import db, tracking

    with open_in(*schema_url) as conn:
        books(conn)
    monkeypatch.setattr(db, "connect", lambda: open_in(*schema_url))
    assert tracking.main(["check"], bridge=lambda: pytest.fail("sent without --send")) == 0
    out = capsys.readouterr().out
    assert "as of poll p2" in out and "Not sent" in out and '"cash": "364.17"' in out
    assert '"as_of": "p2"' in out and "15=3" in out
    with open_in(*schema_url) as conn:
        assert rows(conn) == []


def test_check_send_needs_by_and_stores_the_answer(schema_url, monkeypatch, capsys):
    from tests.conftest import open_in
    from vending import db, tracking

    monkeypatch.setattr(tracking, "clock", lambda conn: utc("2026-12-04T11:05:00+00:00"))   # 06:05 Eastern

    with open_in(*schema_url) as conn:
        books(conn)
    monkeypatch.setattr(db, "connect", lambda: open_in(*schema_url))
    monkeypatch.setenv(tracking.DAILY_SETTING, "2099-01-01T00:00:00-05:00")   # two sends, whatever today is
    with pytest.raises(SystemExit):
        tracking.main(["check", "--send"], bridge=lambda: pytest.fail("sent without a name"))
    bridge = FakeBridge(WITHIN)
    assert tracking.main(["check", "--as-of", "p1", "--send", "--by", "kevin"], bridge=lambda: bridge) == 0
    out = capsys.readouterr().out
    assert "coil outside" in out and bridge.sent[0]["as_of"] == "p1"
    bridge = FakeBridge(refused(409, "tracking_not_started", detail="nothing to check against", items=[]))
    assert tracking.main(["check", "--send", "--by", "kevin"], bridge=lambda: bridge) == 1
    out = capsys.readouterr().out
    assert "409: the project's books haven't begun" in out and "still there" in out
    assert tracking.main(["show"]) == 0
    shown = capsys.readouterr().out
    assert "#2" in shown and "#1" in shown and "coil outside" in shown


EVIL = "EVIL[2J]0;owned"     # reply text, escape codes and all, that must never reach the terminal


@pytest.mark.parametrize("answer, status, says", [
    (refused(409, "not_reconciled_yet", error=EVIL, detail=EVIL, items=[EVIL, EVIL]), 1,
     ["409", "isn't in the bridge's books yet", "2 items", "still there"]),
    (refused(422, EVIL, detail=EVIL, items=[EVIL]), 1, ["422", "unknown error type", "1 item"]),
    (BridgeAnswered(422, json.dumps({"detail": [{"msg": EVIL, "loc": ["body", EVIL]}]})), 1,
     ["422", "didn't match the bridge's schema"]),
    (BridgeAnswered(503, EVIL), 1, ["503", "may have used today's check"]),
    ({**WITHIN, "as_of": "p1", "results": {**WITHIN["results"], "coil": EVIL}, "next_check_from": EVIL}, 3,
     ["200", "not the shape the spec gives", "may have used today's check"]),
], ids=["known", "unknown-type", "fastapi-422", "5xx", "malformed-200"])
def test_the_terminal_shows_only_our_own_words_never_the_reply(schema_url, monkeypatch, capsys, answer, status,
                                                               says):
    from tests.conftest import open_in
    from vending import db, tracking

    monkeypatch.setattr(tracking, "clock", lambda conn: utc("2026-12-04T11:05:00+00:00"))   # 06:05 Eastern

    with open_in(*schema_url) as conn:
        books(conn)
    monkeypatch.setattr(db, "connect", lambda: open_in(*schema_url))
    assert tracking.main(["check", "--as-of", "p1", "--send", "--by", "kevin"],
                         bridge=lambda: FakeBridge(answer)) == status
    out = capsys.readouterr()
    for word in says:
        assert word in out.out
    assert tracking.main(["show"]) == 0
    shown = capsys.readouterr()
    for text in (out.out, out.err, shown.out, shown.err):
        assert "EVIL" not in text and "" not in text and "owned" not in text


class Answer200(FakeResponse):
    status = 200


NUL_WITHIN = json.dumps({**WITHIN, "note": EVIL + "\x00"}).encode()                 # the shape, plus a NUL
NUL_VERDICT = json.dumps({**WITHIN, "results": {**WITHIN["results"], "coil": "within\x00" + EVIL}}).encode()


@pytest.mark.parametrize("reply", [
    Answer200(b"<html>" + EVIL.encode() + b"\x00</html>"),
    Answer200(b"\xff\xfe" + EVIL.encode()),
    Answer200(NUL_WITHIN),
    Answer200(NUL_VERDICT),
], ids=["html", "not-utf8", "nul-extra-field", "nul-verdict"])
def test_a_200_that_is_not_json_or_holds_a_nul_is_malformed_through_the_real_client(
        schema_url, monkeypatch, capsys, reply):
    """Through `Bridge.check_books`: a 200 that isn't JSON, or whose JSON holds a NUL (which jsonb
    can't store), is stored as `malformed_answer` with its status, may have used the check, exits 3,
    and prints only our words."""
    from tests.conftest import open_in
    from vending import bridge as bridge_module
    from vending import db, tracking

    monkeypatch.setattr(tracking, "clock", lambda conn: utc("2026-12-04T11:05:00+00:00"))   # 06:05 Eastern

    with open_in(*schema_url) as conn:
        books(conn)
    monkeypatch.setattr(db, "connect", lambda: open_in(*schema_url))
    monkeypatch.setattr(bridge_module.urllib.request, "urlopen", lambda request, timeout: reply)
    assert tracking.main(["check", "--as-of", "p1", "--send", "--by", "kevin"],
                         bridge=lambda: Bridge("http://bridge.test", "key")) == 3
    out = capsys.readouterr()
    assert "200" in out.out and "not the shape the spec gives" in out.out and "may have used" in out.out
    assert tracking.main(["show"]) == 0
    shown = capsys.readouterr()
    for text in (out.out, out.err, shown.out, shown.err):
        assert "EVIL" not in text and "\x1b" not in text and "owned" not in text and "\x00" not in text
    with open_in(*schema_url) as conn:
        assert rows(conn)[0][:4] == (200, "malformed_answer", None, None)
        [answer] = conn.execute("select answer from tracking_checks").fetchone()
        assert answer is not None and "\x00" not in json.dumps(answer, ensure_ascii=False)


@pytest.mark.parametrize("answer", [
    BridgeAnswered(409, json.dumps({"error": "x\x00", "error_type": "not_reconciled_yet", "detail": "\x00",
                                    "items": ["\x00"]})),
    BridgeAnswered(502, "Bad\x00Gateway"),
], ids=["json-error", "unparsed-error"])
def test_an_error_answer_holding_a_nul_is_stored(conn, answer):
    """jsonb can't hold a NUL: the stored answer has it replaced, and the status and verdict are kept."""
    from vending import tracking

    books(conn)
    f = tracking.figures(conn, "p1")
    out = tracking.send(conn, FakeBridge(answer), f, "kevin", now=DURING)
    stored = conn.execute("select status, answer from tracking_checks").fetchone()
    assert stored[0] == out.status == answer.status and "\x00" not in json.dumps(stored[1], ensure_ascii=False)
    assert out.used_check is (False if answer.status == 409 else None)


class SendFails:
    """A connected socket that fails while the request is being written to it."""

    def __init__(self, error):
        self.error = error

    def setsockopt(self, *args):
        pass

    def sendall(self, data):
        raise self.error

    def close(self):
        pass


def failing_connection(monkeypatch, connect_error=None, send_error=None):
    """The real urllib and http.client, with the network replaced: connecting raises `connect_error`, or
    connects and writing the request raises `send_error`."""
    import socket

    def create_connection(*args, **kwargs):
        if connect_error is not None:
            raise connect_error
        return SendFails(send_error)

    monkeypatch.setattr(socket, "create_connection", create_connection)
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("no_proxy", "*")


@pytest.mark.parametrize("connect_error, send_error, expected", [
    (__import__("socket").gaierror(11001, "getaddrinfo failed"), None, BridgeUnreachable),
    (ConnectionRefusedError(10061, "refused"), None, BridgeUnreachable),
    (TimeoutError("timed out"), None, BridgeUnreachable),
    (OSError(10051, "network unreachable"), None, BridgeUnreachable),
    (None, ConnectionResetError(10054, "reset by peer"), BridgeNoAnswer),
    (None, TimeoutError("timed out"), BridgeNoAnswer),
    (None, BrokenPipeError(32, "broken pipe"), BridgeNoAnswer),
], ids=["dns", "refused", "connect-timeout", "no-route", "reset-sending", "timeout-sending", "broken-pipe"])
def test_only_a_failure_before_the_connection_opens_is_unreachable(monkeypatch, connect_error, send_error,
                                                                   expected):
    """Unreachable means nothing reached the bridge: the name didn't resolve, or connecting failed. Once
    the connection is open, a reset or timeout may come after the bridge read the request."""
    failing_connection(monkeypatch, connect_error, send_error)
    with pytest.raises(BridgeError) as raised:
        Bridge("http://bridge.test", "key").check_books(good_body())
    assert type(raised.value) is expected


def test_a_reset_while_sending_may_have_used_the_check(conn, monkeypatch):
    """Through the real client: a reset while the request was being written is stored as no answer, may
    have used the day's check, and blocks a second send that day."""
    from vending import tracking

    books(conn)
    failing_connection(monkeypatch, send_error=ConnectionResetError(10054, "reset by peer"))
    f = tracking.figures(conn, "p1")
    out = tracking.send(conn, Bridge("http://bridge.test", "key"), f, "kevin", now=DURING)
    assert (out.status, out.error_type, out.used_check) == (None, "no_answer", None)
    with pytest.raises(tracking.AlreadyChecked, match="may have used it"):
        tracking.send(conn, FakeBridge(WITHIN), f, "kevin", now=DURING)


@pytest.mark.parametrize("error_type, stored", [
    ("not_reconciled_yet\x00", "not_reconciled_yet�"),
    ("not_reconciled_yet\ud800", "not_reconciled_yet?"),
], ids=["nul", "lone-surrogate"])
def test_an_error_type_the_database_cannot_hold_is_stored_made_safe(conn, error_type, stored):
    """`error_type` is a text column, written from the reply: a NUL left the answer unwritten, and a lone
    surrogate raised before the write. It is made safe as the JSON answer is, and the answer stored."""
    from vending import tracking

    books(conn)
    f = tracking.figures(conn, "p1")
    out = tracking.send(conn, FakeBridge(refused(409, error_type)), f, "kevin", now=DURING)
    assert (out.status, out.error_type, out.used_check) == (409, stored, False)
    assert rows(conn)[0][:3] == (409, stored, False)
    [answer] = conn.execute("select answer from tracking_checks").fetchone()
    assert answer["error_type"] == stored
    out = tracking.send(conn, FakeBridge(WITHIN), f, "kevin", now=DURING)    # the day is still open
    assert out.results is not None


def test_a_database_error_storing_the_answer_prints_only_our_words(schema_url, monkeypatch, capsys):
    """Should storing the answer fail anyway, the error's text (which can quote the reply) isn't printed."""
    import psycopg

    from tests.conftest import open_in
    from vending import db, tracking

    monkeypatch.setattr(tracking, "clock", lambda conn: utc("2026-12-04T11:05:00+00:00"))   # 06:05 Eastern

    with open_in(*schema_url) as conn:
        books(conn)
    monkeypatch.setattr(db, "connect", lambda: open_in(*schema_url))

    def broken(*args, **kwargs):
        raise psycopg.errors.UntranslatableCharacter(f"unsupported Unicode escape sequence near {EVIL}")

    monkeypatch.setattr(tracking, "_answered", broken)
    assert tracking.main(["check", "--as-of", "p1", "--send", "--by", "kevin"],
                         bridge=lambda: FakeBridge(WITHIN)) == 3
    out = capsys.readouterr()
    assert "may have used" in out.out + out.err
    assert "EVIL" not in out.out + out.err and "\x1b" not in out.out + out.err


def test_check_refuses_when_a_figure_cannot_be_worked_out(schema_url, monkeypatch, capsys):
    from tests.conftest import open_in
    from vending import db, tracking

    monkeypatch.setattr(db, "connect", lambda: open_in(*schema_url))
    assert tracking.main(["check", "--send", "--by", "kevin"], bridge=lambda: pytest.fail("sent a guess")) == 1
    assert "No good poll" in capsys.readouterr().err


def test_the_ledger_counts_receipt_lines_with_no_total_paid(conn):
    from vending import ledger

    open_ledger(conn)
    fill(conn, slots=("15", "16"))
    conn.execute("update purchase_lines set total_paid = null where slot = '15'")
    b = ledger.balance(conn, utc("2026-11-05T00:00:00+00:00"))
    assert b.receipts_missing == 1 and b.receipts_cents == 1237


def test_the_tolerances_are_davids():
    assert sim.MONEY_TOLERANCE_CENTS == 100 and sim.UNIT_TOLERANCE == 2
    assert Fraction(sim._dollars_cents("1.005")) == Fraction(201, 2)
