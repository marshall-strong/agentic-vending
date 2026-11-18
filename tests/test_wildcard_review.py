"""Regressions from the second review of the fixed wildcard (#166 at c991f53), one section per finding.

Each test here failed on c991f53. When data can't settle prices, timing, contents or sales, the code
reports unknown or refuses; it never promotes, moves or buys on a guess.
"""

from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import open_in, store_poll
from tests.test_machine_state import fulfilled
from vending import config

UTC = timezone.utc


# --- 7. The setter checks the stored assignment before any success ---------------------------------------

def test_7_asking_again_for_a_slot_that_left_our_assignment_is_refused(conn):
    from vending.config import set_halted
    from vending.tools.wildcard_slot import set_wildcard_slot
    from vending.write_gate import Refused, run_write

    set_halted(conn, False, "test")
    config.set_value(conn, "wildcard_slot", "37", "kevin")
    conn.execute("update slot_assignment set slots = array_remove(slots, '37')")
    with pytest.raises(Refused) as caught:
        run_write(conn, "set_wildcard_slot", {"slot": "37", "reason": "again"}, set_wildcard_slot("37", "again"))
    assert "isn't one of ours" in " ".join(r.message for r in caught.value.refusals)


def test_7_asking_again_still_needs_a_reason(conn):
    from vending.config import set_halted
    from vending.tools.wildcard_slot import set_wildcard_slot
    from vending.write_gate import Refused, run_write

    set_halted(conn, False, "test")
    config.set_value(conn, "wildcard_slot", "37", "kevin")
    with pytest.raises(Refused) as caught:
        run_write(conn, "set_wildcard_slot", {"slot": "37", "reason": " "}, set_wildcard_slot("37", " "))
    assert "give a reason" in " ".join(r.message for r in caught.value.refusals)


def test_7_the_config_path_checks_the_stored_assignment(conn):
    conn.execute("update slot_assignment set slots = array_remove(slots, '37')")
    with pytest.raises(ValueError, match="isn't one of ours in the stored slot assignment"):
        config.set_value(conn, "wildcard_slot", "37", "kevin")
    assert config.get_value(conn, "wildcard_slot") is None


def test_7_the_config_path_refuses_the_same_value_once_it_left_the_assignment(conn):
    config.set_value(conn, "wildcard_slot", "37", "kevin")
    conn.execute("update slot_assignment set slots = array_remove(slots, '37')")
    with pytest.raises(ValueError, match="isn't one of ours"):
        config.set_value(conn, "wildcard_slot", "37", "kevin")


def test_7_the_cli_checks_the_stored_assignment(schema_url, monkeypatch, capsys):
    from vending import db as db_module

    monkeypatch.setattr(db_module, "connect", lambda: open_in(*schema_url))
    with open_in(*schema_url) as c:
        c.execute("update slot_assignment set slots = array_remove(slots, '37')")
    assert config.main(["set", "wildcard_slot", '"37"', "--by", "kevin"]) == 1
    assert "isn't one of ours" in capsys.readouterr().err


# --- 8. Config can't raise the agreed limits ------------------------------------------------------------

@pytest.mark.parametrize("key, value", [
    ("price_ceiling_cents", 700),             # agreed: $6.50
    ("wildcard_ceiling_cents", 850),          # agreed: $8.00
    ("wildcard_purchase_cap_cents", 8300),    # agreed: $82.00
])
def test_8_config_refuses_a_limit_above_the_agreed_one(conn, key, value):
    with pytest.raises(ValueError, match="agreed"):
        config.set_value(conn, key, value, "kevin")
    assert config.get_value(conn, key) is None


@pytest.mark.parametrize("key, value", [
    ("price_ceiling_cents", 600), ("wildcard_ceiling_cents", 750), ("wildcard_purchase_cap_cents", 6000),
    ("price_ceiling_cents", 650), ("wildcard_ceiling_cents", 800), ("wildcard_purchase_cap_cents", 8200),
])
def test_8_config_allows_the_agreed_limit_or_lower(conn, key, value):
    config.set_value(conn, key, value, "kevin")
    assert config.get_value(conn, key) == value


@pytest.mark.parametrize("kwargs", [
    {"price_ceiling_cents": 700}, {"wildcard_ceiling_cents": 850}, {"wildcard_purchase_cap_cents": 8300},
])
def test_8_the_planner_refuses_a_limit_above_the_agreed_one(kwargs):
    from vending.planner import Config

    with pytest.raises(ValueError, match="agreed"):
        Config(**kwargs)


def test_8_the_planner_allows_lower_limits():
    from vending.planner import Config

    c = Config(price_ceiling_cents=600, wildcard_ceiling_cents=700, wildcard_purchase_cap_cents=5000)
    assert (c.price_ceiling_cents, c.wildcard_ceiling_cents, c.wildcard_purchase_cap_cents) == (600, 700, 5000)


@pytest.mark.parametrize("key, value", [
    ("price_ceiling_cents", 700), ("wildcard_ceiling_cents", 850), ("wildcard_purchase_cap_cents", 8300),
])
def test_8_a_hand_edited_limit_above_the_agreed_one_stops_planning(conn, key, value):
    from psycopg.types.json import Jsonb

    from vending import planning

    conn.execute("insert into config (key, value, updated_by) values (%s, %s, 'hand')", (key, Jsonb(value)))
    with pytest.raises(planning.NotReady, match=key):
        planning.planner_config(conn)


# --- 1. Silence needs the counters to prove it end to end ------------------------------------------------

def _silent_report(conn, now=None, **machine_args):
    from tests.test_silent_slots import NOW, machine, report

    machine(conn, **machine_args)
    return report(conn, now=now or NOW)


def _why(r, slot):
    from tests.test_silent_slots import why

    return why(r, slot)


def test_1_the_control_slot_16_is_silent(conn):
    # The baseline the four tests below break one way each.
    assert [s.slot for s in _silent_report(conn).silent] == ["16"]


def test_1_no_reading_of_the_slot_at_or_before_fridays_midnight_is_unknown(conn):
    # 16 is missing from every poll from Thursday 6 PM to Friday 4 AM Eastern: a sale then can't be ruled out.
    def edit(t, units):
        if datetime(2026, 10, 22, 22, tzinfo=UTC) <= t <= datetime(2026, 10, 23, 8, tzinfo=UTC):
            del units["16"]
    r = _silent_report(conn, edit=edit)
    assert "16" not in [s.slot for s in r.silent]
    assert _why(r, "16").startswith("unknown: no reading of its counter at or before")


def test_1_a_confirmed_counter_restart_in_the_window_is_unknown(conn):
    # Saturday 24 Oct the board is swapped: 16 reads 3 from then on, so 3 may have sold before or after.
    def edit(t, units):
        if t >= datetime(2026, 10, 24, 10, tzinfo=UTC):
            units["16"] = 3
    r = _silent_report(conn, edit=edit)
    assert "16" not in [s.slot for s in r.silent]
    assert _why(r, "16").startswith("unknown: its counter restarted between the polls at 2026-10-24 06:00")


def test_1_a_24_hour_polling_outage_is_unknown_not_silent(conn):
    # No poll from Saturday morning to Sunday morning: no class day is lost, but the stretch wasn't watched.
    r = _silent_report(conn, gaps=[(datetime(2026, 10, 24, 2, tzinfo=UTC), datetime(2026, 10, 25, 3, tzinfo=UTC))])
    assert "16" not in [s.slot for s in r.silent]
    assert _why(r, "16").startswith("unknown: no poll read it from 2026-10-24 02:00 to 2026-10-25 06:00")


def test_1_a_stale_closing_reading_is_unknown(conn):
    from tests.test_silent_slots import THURSDAY_EVENING

    r = _silent_report(conn, now=THURSDAY_EVENING + timedelta(hours=8))
    assert "16" not in [s.slot for s in r.silent]
    assert _why(r, "16").startswith("unknown: its latest reading, at 2026-10-29 22:00 UTC, is over 6 hours old")


# --- 3. Only evidence about the slot's own product and units confirms stock ---------------------------

def test_3_a_count_naming_a_different_product_doesnt_confirm_a_slot(conn):
    from tests.test_silent_slots import count, machine, report

    machine(conn)
    count(conn, "17", 10, product="ZZZ-NOT-CCC")
    r = report(conn)
    assert "17" not in [s.slot for s in r.silent]
    assert _why(r, "17").startswith("unknown: the door count c1 on 2026-10-24 saw 10 of ZZZ-NOT-CCC")


def test_3_a_count_that_disagrees_with_the_receipted_units_doesnt_confirm_a_slot(conn):
    from tests.test_silent_slots import count, machine, report

    machine(conn)
    count(conn, "17", 6)                    # the receipts say 10 of CCC
    r = report(conn)
    assert "17" not in [s.slot for s in r.silent]
    assert "receipts say it holds 10 of CCC" in _why(r, "17")


def test_3_an_off_cycle_receipt_after_friday_isnt_a_friday_load(conn):
    from tests.test_silent_slots import machine, report

    machine(conn)
    # A Monday off-cycle visit tops up 18: it says nothing about what 18 held from Friday.
    fulfilled(conn, "mon", datetime(2026, 10, 26, 15, tzinfo=UTC), ("18", "FFF", "primary", "2.00", 2, 0, 0))
    conn.execute("update purchase_requests set off_cycle = true where request_id = 'mon'")
    r = report(conn)
    assert "18" not in [s.slot for s in r.silent]
    assert "18" in {u.slot for u in r.unconfirmed}


def test_3_a_conflicting_count_withholds_the_proposal(conn):
    from tests.test_silent_slots import NOW, count, machine, trial_sales
    from vending.tools.machine_state import get_machine_state

    machine(conn, sales=trial_sales(), full=True)
    config.set_value(conn, "wildcard_slot", "15", "test")
    assert get_machine_state(conn, now=NOW).wildcard_trial.proposal.into_slot == "16"   # the control
    count(conn, "16", 5)                     # the receipts say 12 of BBB: what would the promotion pull?
    t = get_machine_state(conn, now=NOW).wildcard_trial
    assert t.verdict == "pass"
    assert t.proposal is None and "16" not in [s.slot for s in t.silent_slots]


# --- 2. A pass needs a fresh, single price and settled counters -------------------------------------------

def test_2_a_pass_with_no_price_read_lately_is_unknown(conn):
    from tests.test_wildcard_trial import set_wildcard, week_of_sales
    from vending.tools.machine_state import get_machine_state

    week_of_sales(conn)                      # $3.00 to the poll of Monday 26 Oct, 22:00 UTC
    set_wildcard(conn, "15")
    for n, hour in enumerate((1, 5, 8)):    # then polls that read no price for it
        store_poll(conn, f"np{n}", f"2026-10-27T0{hour}:00:00+00:00", {"15": (None, 118, 35400), "16": (200, 62)})
    t = get_machine_state(conn, now=datetime(2026, 10, 27, 9, tzinfo=UTC)).wildcard_trial
    assert (t.units_sold, t.class_days_in_stock) == (18, 6)
    assert t.verdict == "unknown" and "its price now" in t.reason


def test_2_an_unsettled_drop_in_the_wildcards_counter_is_unknown(conn):
    from tests.test_wildcard_trial import FRESH, set_wildcard, week_of_sales
    from vending.tools.machine_state import get_machine_state

    week_of_sales(conn)
    set_wildcard(conn, "15")
    store_poll(conn, "low", "2026-10-27T01:00:00+00:00", {"15": (300, 5, 1500), "16": (200, 62)})
    t = get_machine_state(conn, now=FRESH).wildcard_trial
    assert t.verdict == "unknown" and "hasn't settled" in t.reason


def test_2_the_price_is_the_one_read_by_now(conn):
    # A poll after `now` read $7.50: as of now the price is $3.00, read 4 hours ago, and the trial passes.
    from tests.test_wildcard_trial import FRESH, set_wildcard, week_of_sales
    from vending.tools.machine_state import get_machine_state

    week_of_sales(conn)
    set_wildcard(conn, "15")
    store_poll(conn, "later", "2026-10-27T03:00:00+00:00", {"15": (750, 118, 35400), "16": (200, 62)})
    t = get_machine_state(conn, now=FRESH).wildcard_trial
    assert (t.price_test, t.sale_price, t.verdict) == (False, "3.00", "pass")


# --- 4. Unknown contents can hide a duplicate of the trial's product -----------------------------------

def test_4_no_verdict_while_a_regular_slot_has_no_receipt(conn):
    from tests.test_wildcard_trial import FRESH, set_wildcard, week_of_sales
    from vending.tools.machine_state import get_machine_state

    week_of_sales(conn, leave=("27", "48"))
    set_wildcard(conn, "15")
    t = get_machine_state(conn, now=FRESH).wildcard_trial
    assert t.verdict == "unknown" and "slot 27, 48 holds" in t.reason


def test_4_the_planner_gets_no_pass_while_a_regular_slot_has_no_receipt(conn):
    from tests.test_ledger import open_ledger
    from tests.test_wildcard_trial import FRESH, set_wildcard, week_of_sales
    from vending import planning

    open_ledger(conn)
    week_of_sales(conn, leave=("27",))
    set_wildcard(conn, "15")
    assert planning.gather(conn, [], FRESH).current.wildcard_verdict == "unknown"


def test_4_no_regular_load_while_the_wildcards_contents_are_unknown():
    from tests.test_planner import restock_15, wild, where
    from vending.planner import SlotState

    unknown = {"17": SlotState("17", None, None, 0, None, contents_known=False)}
    p = wild(restock_15(), slot="17", slots=unknown)
    assert where(p) == [("wildcard", "17")]
    assert "may put the same product in two slots" in p.refusals[0].message


def test_4_a_plan_that_sets_the_wildcards_product_may_load_others():
    from tests.test_planner import SPRITE, SPRITE_SOURCING, restock_15, wild
    from vending.planner import DesiredSlot, SlotState

    unknown = {"17": SlotState("17", None, None, 0, None, contents_known=False)}
    sprite_17 = DesiredSlot("17", product=SPRITE, sale_price="3.00", fill_to=6)
    p = wild(restock_15(), sprite_17, slot="17", slots=unknown, sourcing=(SPRITE_SOURCING,))
    assert p.ok, [r.message for r in p.refusals]


# --- 6. Every Friday after the trial's fifth day is a judgment, a request filed or not ---------------------

def test_6_a_waiting_week_with_no_request_must_hold_its_rate(conn):
    # Loaded Fri 16 Oct; 4 sold over the 5 class days to Fri 23 Oct; no visit, no request on Fri 30 Oct; then
    # 2 more class days with no sales: 4 over 7, under 0.7 a day. It was judged at 30 Oct all the same.
    from tests.test_wildcard_trial import SINCE, polls, set_wildcard, stock_the_rest
    from vending.tools.machine_state import get_machine_state

    store_poll(conn, "p0", "2026-10-16T18:00:00+00:00", {"15": (300, 100), "16": (200, 50)})
    fulfilled(conn, "r1", SINCE, ("15", "AAA", "primary", "10.00", 20, 0, 0), ("16", "BBB", "primary", "10.00", 20, 0, 0))
    stock_the_rest(conn)
    rows = [("p1", "2026-10-16T21:00:00+00:00", {"15": (300, 100), "16": (200, 50)})]
    rows += [(f"d{n}", f"2026-10-{day}T22:00:00+00:00", {"15": (300, units), "16": (200, 50)})
             for n, (day, units) in enumerate((("19", 101), ("20", 102), ("21", 103), ("22", 104), ("23", 104)))]
    polls(conn, rows)
    polls(conn, [(f"e{n}", f"2026-11-0{day}T22:00:00+00:00", {"15": (300, 104), "16": (200, 50)})
                 for n, day in enumerate((1, 2, 3))])
    set_wildcard(conn, "15")
    assert conn.execute("select count(*) from purchase_requests where fulfilled_at > %s",
                        (SINCE,)).fetchone() == (0,)
    t = get_machine_state(conn, now=datetime(2026, 11, 4, 1, tzinfo=UTC)).wildcard_trial
    assert (t.units_sold, t.class_days_in_stock) == (4, 7)
    assert t.verdict == "extend" and "isn't eligible" in t.reason


def test_6_the_friday_cutoff_is_the_one_before_the_friday_planned_for():
    from vending.class_days import EASTERN
    from vending.wildcard_trial import judged_by

    friday_23 = datetime(2026, 10, 23, 0, tzinfo=EASTERN)
    assert judged_by(datetime(2026, 10, 29, 20, tzinfo=EASTERN)) == friday_23     # Thursday evening, for the 30th
    assert judged_by(datetime(2026, 10, 30, 9, tzinfo=EASTERN)) == friday_23      # Friday morning, for that day
    assert judged_by(datetime(2026, 10, 31, 9, tzinfo=EASTERN)) == friday_23 + timedelta(days=7)   # Saturday
