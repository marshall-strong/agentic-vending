"""Tests for the wildcard trial (`vending.wildcard_trial`): the measure, the verdict, and how the agent sees it."""

from datetime import datetime, timedelta, timezone
from fractions import Fraction

from tests.conftest import store_poll
from tests.test_machine_state import fulfilled
from vending.tools.machine_state import get_machine_state
from vending.wildcard_trial import (DROP, PRICE_TEST, PROMOTE, TOO_EARLY, UNKNOWN, SlotRecord, current_wildcard,
                                    judge, slot_record)

SINCE = datetime(2026, 10, 16, 20, tzinfo=timezone.utc)   # Friday 4 PM Eastern


def rec(slot, profit_cents, days=5, product=None):
    return SlotRecord(slot, product or f"P{slot}", SINCE, days, 10, None if profit_cents is None else Fraction(profit_cents))


# --- The measure --------------------------------------------------------------

def test_profit_is_revenue_without_tax_less_cost():
    r = slot_record("15", "A", SINCE, 4, 3, 1080, [Fraction(150), Fraction(150), Fraction(150)], 8)
    assert r.profit_cents == 1000 - 450                 # $10.80 with 8% tax inside is $10.00 of revenue
    assert r.profit_per_class_day_cents == Fraction(550, 4)


def test_an_unknown_cost_makes_profit_unknown():
    r = slot_record("15", "A", SINCE, 4, 2, 600, [Fraction(150), None], 8)
    assert r.profit_cents is None and r.profit_per_class_day_cents is None


def test_no_days_in_stock_has_no_rate():
    assert slot_record("15", "A", SINCE, 0, 0, 0, [], 8).profit_per_class_day_cents is None


# --- The verdict --------------------------------------------------------------

def test_too_early_before_five_in_stock_class_days():
    verdict, weakest, reason = judge(rec("15", 5000, days=4), False, [rec("16", 100)])
    assert (verdict, weakest) == (TOO_EARLY, None) and "4 of 5" in reason


def test_a_trial_above_the_ceiling_is_a_price_test():
    assert judge(rec("15", 5000), True, [rec("16", 100)])[0] == PRICE_TEST


def test_promote_when_it_beats_the_weakest_other_slot():
    verdict, weakest, reason = judge(rec("15", 1000, product="A"), False,
                                     [rec("16", 2000), rec("17", 900, product="B"), rec("18", 5000)])
    assert (verdict, weakest.slot) == (PROMOTE, "17")
    assert "$2.00" in reason and "$1.80" in reason and "B to the closet" in reason and "slot 17 the wildcard" in reason


def test_drop_when_it_doesnt_beat_the_weakest():
    verdict, weakest, reason = judge(rec("15", 900), False, [rec("16", 900), rec("17", 2000)])
    assert (verdict, weakest.slot) == (DROP, "16")        # a tie is not a win
    assert "switch slot 15 to the next product" in reason


def test_only_slots_with_five_days_and_a_known_profit_are_compared():
    others = [rec("16", 0, days=4), rec("17", None), rec("18", 600)]
    verdict, weakest, _ = judge(rec("15", 700), False, others)
    assert (verdict, weakest.slot) == (PROMOTE, "18")
    assert judge(rec("15", 700), False, others[:2])[0] == UNKNOWN


def test_an_unknown_trial_profit_is_unknown():
    assert judge(rec("15", None), False, [rec("16", 100)])[0] == UNKNOWN


def test_losses_are_shown_as_negative_dollars():
    _, _, reason = judge(rec("15", -500), False, [rec("16", 100)])
    assert "-$1.00" in reason


# --- From the database --------------------------------------------------------

def submit(conn, request_id, wildcard, at=SINCE - timedelta(days=1), status="accepted"):
    conn.execute("insert into purchase_submissions (at, plan_hash, body, status, request_id, wildcard)"
                 " values (%s, 'h', '{}', %s, %s, %s)", (at, status, request_id, wildcard))


def week_of_sales(conn, price15=300, per_day15=3, per_day16=2):
    """Both slots loaded on Friday 16 Oct, then a poll every evening to Monday 26 Oct."""
    store_poll(conn, "p0", "2026-10-16T12:00:00+00:00", {"15": (price15, 100), "16": (200, 50)})
    fulfilled(conn, "r1", SINCE, ("15", "AAA", "primary", "10.00", 20, 0, 0), ("16", "BBB", "primary", "10.00", 20, 0, 0))
    # A poll just after the load: the window holding the load is left out of the money.
    store_poll(conn, "p1", "2026-10-16T21:00:00+00:00", {"15": (price15, 100), "16": (200, 50)})
    u15, u16 = 100, 50
    for n, day in enumerate(("19", "20", "21", "22", "23", "26")):
        u15, u16 = u15 + per_day15, u16 + per_day16
        store_poll(conn, f"d{n}", f"2026-10-{day}T22:00:00+00:00", {"15": (price15, u15), "16": (200, u16)})


NOW = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)


def test_no_wildcard_named_means_no_trial(conn):
    week_of_sales(conn)
    reply = get_machine_state(conn, now=NOW)
    assert reply.wildcard_trial is None and "No wildcard trial" in reply.message


def test_the_trial_is_judged_against_the_weakest_other_slot(conn):
    week_of_sales(conn)
    submit(conn, "r1", "15")
    reply = get_machine_state(conn, now=NOW)
    t = reply.wildcard_trial
    assert (t.slot, t.product, t.since, t.price_test) == ("15", "AAA", SINCE, False)
    assert t.units_sold == 18 and t.class_days_in_stock >= 5
    assert (t.weakest_slot, t.weakest_product) == ("16", "BBB")
    assert t.weakest_class_days_in_stock == t.class_days_in_stock
    # 18 sold at $3.00 is $54.00, $50.00 without tax, less 18 x $0.50: $41.00 over the days.
    assert t.profit_per_class_day == f"{41 / t.class_days_in_stock:.2f}"
    # 12 sold at $2.00 is $24.00, $22.22 without tax, less $6.00.
    assert t.weakest_profit_per_class_day == f"{(2400 - 178 - 600) / 100 / t.class_days_in_stock:.2f}"
    assert t.verdict == PROMOTE
    assert "Wildcard trial (AAA in slot 15): promote" in reply.message


def test_a_trial_above_the_ceiling_is_reported_as_a_price_test(conn):
    week_of_sales(conn, price15=750)
    submit(conn, "r1", "15")
    t = get_machine_state(conn, now=NOW).wildcard_trial
    assert (t.verdict, t.price_test) == (PRICE_TEST, True)


def test_the_latest_accepted_plan_names_the_wildcard(conn):
    week_of_sales(conn)
    submit(conn, "r1", "15")
    submit(conn, None, "16", at=SINCE, status="refused")             # refused: never filed
    assert current_wildcard(conn)[0] == "15"
    conn.execute("insert into purchase_requests (request_id, submitted_at, off_cycle, request_body, status)"
                 " values ('r2', %s, false, '{}', 'rejected')", (SINCE,))
    submit(conn, "r2", None, at=SINCE)                               # rejected by the bridge: moved nothing
    assert current_wildcard(conn)[0] == "15"
    conn.execute("insert into purchase_requests (request_id, submitted_at, off_cycle, request_body)"
                 " values ('r3', %s, false, '{}')", (SINCE + timedelta(hours=1),))
    submit(conn, "r3", None, at=SINCE + timedelta(hours=1))          # a plan naming none ends the trial
    assert current_wildcard(conn)[0] is None


def test_a_wildcard_without_receipts_is_unknown(conn):
    week_of_sales(conn)
    submit(conn, "r1", "17")
    t = get_machine_state(conn, now=NOW).wildcard_trial
    assert (t.slot, t.product, t.verdict) == ("17", None, UNKNOWN)


def test_the_poll_window_holding_the_load_is_left_out(conn):
    week_of_sales(conn)
    conn.execute("delete from poll_selections where session_id = 'p1'")
    conn.execute("delete from polls where session_id = 'p1'")
    submit(conn, "r1", "15")
    t = get_machine_state(conn, now=NOW).wildcard_trial
    assert t.units_sold == 15                  # Monday's 3 sold in the window that began before the load
    assert t.profit_per_class_day == f"{(4500 - 333 - 750) / 100 / t.class_days_in_stock:.2f}"
