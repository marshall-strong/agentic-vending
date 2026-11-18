"""Tests for the wildcard trial (`vending.wildcard_trial`): the measure, the verdict, and how the agent sees it."""

from datetime import datetime, timedelta, timezone
from fractions import Fraction

import pytest

from tests.conftest import store_poll
from tests.test_machine_state import fulfilled
from vending.tools.machine_state import get_machine_state
from vending.wildcard_trial import (DROP, MAX_POLL_GAP, PRICE_TEST, PROMOTE, TOO_EARLY, UNKNOWN, Comparison,
                                    SlotRecord, WildcardUnresolved, current_wildcard, judge, slot_record)

SINCE = datetime(2026, 10, 16, 20, tzinfo=timezone.utc)   # Friday 4 PM Eastern


def rec(slot, profit_cents, days=5, product=None):
    return SlotRecord(slot, product or f"P{slot}", SINCE, days, 10, None if profit_cents is None else Fraction(profit_cents))


def against(trial, *others):
    """The trial compared with each of `others` over days they share (here, all of the trial's)."""
    return [Comparison(trial, o) for o in others]


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
    trial = rec("15", 5000, days=4)
    verdict, weakest, reason = judge(trial, False, against(trial, rec("16", 100)))
    assert (verdict, weakest) == (TOO_EARLY, None) and "4 of 5" in reason


def test_a_trial_above_the_ceiling_is_a_price_test():
    trial = rec("15", 5000)
    assert judge(trial, True, against(trial, rec("16", 100)))[0] == PRICE_TEST


def test_promote_when_it_beats_the_weakest_other_slot():
    trial = rec("15", 1000, product="A")
    verdict, weakest, reason = judge(trial, False,
                                     against(trial, rec("16", 2000), rec("17", 900, product="B"), rec("18", 5000)))
    assert (verdict, weakest.other.slot) == (PROMOTE, "17")
    assert "$2.00" in reason and "$1.80" in reason and "B to the closet" in reason and "slot 17 the wildcard" in reason


def test_drop_when_it_doesnt_beat_the_weakest():
    trial = rec("15", 900)
    verdict, weakest, reason = judge(trial, False, against(trial, rec("16", 900), rec("17", 2000)))
    assert (verdict, weakest.other.slot) == (DROP, "16")        # a tie is not a win
    assert "switch slot 15 to the next product" in reason


def test_only_slots_with_five_shared_days_and_a_known_profit_are_compared():
    trial = rec("15", 700)
    others = against(trial, rec("16", 0, days=4), rec("17", None), rec("18", 600))
    verdict, weakest, _ = judge(trial, False, others)
    assert (verdict, weakest.other.slot) == (PROMOTE, "18")
    assert judge(trial, False, others[:2])[0] == UNKNOWN


def test_each_comparison_uses_the_trials_rate_on_the_shared_days():
    # Over all its days the trial earns $1.40 a day, but on the days it shares with 16 only $1.00.
    trial = rec("15", 700)
    shared = Comparison(rec("15", 500), rec("16", 600))
    verdict, weakest, _ = judge(trial, False, [shared])
    assert (verdict, weakest.other.slot) == (DROP, "16")


def test_an_unknown_trial_profit_is_unknown():
    trial = rec("15", None)
    assert judge(trial, False, against(trial, rec("16", 100)))[0] == UNKNOWN


def test_losses_are_shown_as_negative_dollars():
    trial = rec("15", -500)
    _, _, reason = judge(trial, False, against(trial, rec("16", 100)))
    assert "-$1.00" in reason


# --- From the database --------------------------------------------------------

def submit(conn, request_id, wildcard, at=SINCE - timedelta(days=1), status="accepted"):
    conn.execute("insert into purchase_submissions (at, plan_hash, body, status, request_id, wildcard)"
                 " values (%s, 'h', '{}', %s, %s, %s)", (at, status, request_id, wildcard))


def request(conn, request_id, at, status="pending", off_cycle=False):
    """A request with no lines: `at` is when it was fulfilled, or filed if it isn't."""
    conn.execute("insert into purchase_requests (request_id, submitted_at, off_cycle, request_body, status,"
                 " fulfilled_at) values (%s, %s, %s, '{}', %s, %s)",
                 (request_id, at, off_cycle, status, at if status == "fulfilled" else None))


STEP = timedelta(hours=4)   # how often the polls between the ones a test names come; under MAX_POLL_GAP


def polls(conn, rows, after=None):
    """Store each `(session_id, ISO time, selections)` poll in order, and between two more than `STEP` apart a
    poll every `STEP` reading what the earlier one did: the machine was watched, and nothing sold until the
    later poll. `after` is a stored poll's `(time, selections)` to fill on from. Returns the last poll's."""
    for session_id, at, selections in rows:
        t = datetime.fromisoformat(at)
        if after is not None:
            n, (before, readings) = 1, after
            while before + n * STEP < t:
                store_poll(conn, f"{session_id}-{n}", (before + n * STEP).isoformat(), readings)
                n += 1
        store_poll(conn, session_id, at, selections)
        after = (t, selections)
    return after


def week_of_sales(conn, price15=300, per_day15=3, per_day16=2):
    """Both slots loaded on Friday 16 Oct, polled every few hours to Monday 26 Oct, each class day's sales seen
    at its evening poll. Returns the last poll, for `polls(after=...)`."""
    store_poll(conn, "p0", "2026-10-16T18:00:00+00:00", {"15": (price15, 100), "16": (200, 50)})
    fulfilled(conn, "r1", SINCE, ("15", "AAA", "primary", "10.00", 20, 0, 0), ("16", "BBB", "primary", "10.00", 20, 0, 0))
    # A poll just after the load: the window holding the load is left out of the money.
    rows, u15, u16 = [("p1", "2026-10-16T21:00:00+00:00", {"15": (price15, 100), "16": (200, 50)})], 100, 50
    for n, day in enumerate(("19", "20", "21", "22", "23", "26")):
        u15, u16 = u15 + per_day15, u16 + per_day16
        rows.append((f"d{n}", f"2026-10-{day}T22:00:00+00:00", {"15": (price15, u15), "16": (200, u16)}))
    return polls(conn, rows)


NOW = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
DAYS = ("19", "20", "21", "22", "23", "26")


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
    assert t.units_sold == 18 and t.class_days_in_stock == 6
    assert (t.weakest_slot, t.weakest_product) == ("16", "BBB")
    assert t.weakest_class_days_in_stock == t.class_days_in_stock
    # 18 sold at $3.00 is $54.00, $50.00 without tax, less 18 x $0.50: $41.00 over the days.
    assert t.profit_per_class_day == f"{41 / t.class_days_in_stock:.2f}"
    assert t.profit_per_class_day_on_those_days == t.profit_per_class_day
    # 12 sold at $2.00 is $24.00, $22.22 without tax, less $6.00.
    assert t.weakest_profit_per_class_day == f"{(2400 - 178 - 600) / 100 / t.class_days_in_stock:.2f}"
    assert t.verdict == PROMOTE
    assert "Wildcard trial (AAA in slot 15): promote" in reply.message


def test_a_trial_above_the_ceiling_is_reported_as_a_price_test(conn):
    week_of_sales(conn, price15=750)
    submit(conn, "r1", "15")
    t = get_machine_state(conn, now=NOW).wildcard_trial
    assert (t.verdict, t.price_test) == (PRICE_TEST, True)


def test_the_wildcard_takes_effect_when_its_friday_visit_is_confirmed(conn):
    week_of_sales(conn)
    submit(conn, "r1", "15")
    submit(conn, None, "16", at=SINCE, status="refused")             # refused: never filed
    assert current_wildcard(conn)[0] == "15"
    request(conn, "r2", SINCE, status="rejected")
    submit(conn, "r2", None, at=SINCE)                               # rejected by the bridge: moved nothing
    assert current_wildcard(conn)[0] == "15"
    request(conn, "r3", SINCE + timedelta(hours=1))
    submit(conn, "r3", None, at=SINCE + timedelta(hours=1))          # filed, not yet done: nothing has moved
    assert current_wildcard(conn)[0] == "15"
    conn.execute("update purchase_requests set status = 'fulfilled', fulfilled_at = %s where request_id = 'r3'",
                 (SINCE + timedelta(days=7),))
    assert current_wildcard(conn) == (None, None)                    # its visit named none: the trial ended


def test_a_pending_request_or_a_midweek_visit_doesnt_move_the_wildcard(conn):
    week_of_sales(conn)
    submit(conn, "r1", "15")
    request(conn, "r2", NOW - timedelta(hours=2))
    submit(conn, "r2", "16", at=NOW - timedelta(hours=2))            # Friday's plan names 16, but isn't done yet
    assert current_wildcard(conn) == ("15", SINCE)
    assert get_machine_state(conn, now=NOW).wildcard_trial.slot == "15"
    request(conn, "r3", SINCE + timedelta(days=4), status="fulfilled", off_cycle=True)
    submit(conn, "r3", "17", at=SINCE + timedelta(days=3))           # a midweek visit can't move it
    assert current_wildcard(conn) == ("15", SINCE)
    conn.execute("update purchase_requests set status = 'fulfilled', fulfilled_at = %s where request_id = 'r2'",
                 (NOW - timedelta(hours=1),))
    assert current_wildcard(conn) == ("16", NOW - timedelta(hours=1))


def test_a_wildcard_without_receipts_is_unknown(conn):
    week_of_sales(conn)
    submit(conn, "r1", "17")
    t = get_machine_state(conn, now=NOW).wildcard_trial
    assert (t.slot, t.product, t.verdict) == ("17", None, UNKNOWN)


def test_a_wildcard_no_longer_in_our_assignment_is_no_trial(conn):
    week_of_sales(conn)
    submit(conn, "r1", "15")
    conn.execute("update slot_assignment set slots = array_remove(slots, '15')")
    reply = get_machine_state(conn, now=NOW)
    t = reply.wildcard_trial
    assert (t.slot, t.product, t.verdict, t.class_days_in_stock) == (None, None, UNKNOWN, None)
    assert "15" not in t.reason and "No wildcard trial" in reply.message


def test_the_poll_window_holding_the_load_is_left_out(conn):
    # The poll after the load also saw 2 sales: before or after the load, nobody knows, so they don't count.
    store_poll(conn, "p0", "2026-10-16T18:00:00+00:00", {"15": (300, 100), "16": (200, 50)})
    fulfilled(conn, "r1", SINCE, ("15", "AAA", "primary", "10.00", 20, 0, 0), ("16", "BBB", "primary", "10.00", 20, 0, 0))
    rows = [("p1", "2026-10-16T22:00:00+00:00", {"15": (300, 102), "16": (200, 50)})]
    rows += [(f"d{n}", f"2026-10-{day}T22:00:00+00:00", {"15": (300, 105 + 3 * n), "16": (200, 52 + 2 * n)})
             for n, day in enumerate(DAYS)]
    polls(conn, rows)
    submit(conn, "r1", "15")
    t = get_machine_state(conn, now=NOW).wildcard_trial
    assert t.units_sold == 18 and t.class_days_in_stock == 6
    assert t.profit_per_class_day == f"{(5000 - 900) / 100 / t.class_days_in_stock:.2f}"


def test_a_price_test_ends_when_the_price_is_back_under_the_ceiling(conn):
    last = week_of_sales(conn, price15=750)                    # $7.50 to the poll of Monday 26 Oct
    submit(conn, "r1", "15")
    # Restocked at $3.00 on Tuesday 27 Oct: the trial counts from the first poll at that price.
    fulfilled(conn, "r2", datetime(2026, 10, 27, 20, tzinfo=timezone.utc), ("15", "AAA", "primary", "10.00", 20, 0, 0),
              ("16", "BBB", "primary", "10.00", 20, 0, 0))
    rows, u15, u16 = [], 118, 62
    for n, day in enumerate(("10-28", "10-29", "10-30", "11-02", "11-03", "11-04")):
        u15, u16 = u15 + 3, u16 + 2
        rows.append((f"e{n}", f"2026-{day}T22:00:00+00:00", {"15": (300, u15), "16": (200, u16)}))
    polls(conn, rows, after=last)
    t = get_machine_state(conn, now=datetime(2026, 11, 5, 12, tzinfo=timezone.utc)).wildcard_trial
    assert not t.price_test and t.since == datetime(2026, 10, 28, 22, tzinfo=timezone.utc)
    assert t.units_sold == 15 and t.verdict == PROMOTE         # the 5 windows after it, at $3.00
    assert t.weakest_class_days_in_stock == t.class_days_in_stock


def test_a_missing_price_never_turns_a_price_test_into_a_trial(conn):
    # $7.50 at the polls around the load, then polls that read no price: it was never seen at or under $6.50.
    store_poll(conn, "p0", "2026-10-16T18:00:00+00:00", {"15": (750, 100), "16": (200, 50)})
    fulfilled(conn, "r1", SINCE, ("15", "AAA", "primary", "10.00", 20, 0, 0), ("16", "BBB", "primary", "10.00", 20, 0, 0))
    rows = [("p1", "2026-10-16T21:00:00+00:00", {"15": (750, 100), "16": (200, 50)})]
    rows += [(f"d{n}", f"2026-10-{day}T22:00:00+00:00",
              {"15": (None, 103 + 3 * n, 75000 + 900 * (n + 1)), "16": (200, 52 + 2 * n)}) for n, day in enumerate(DAYS)]
    last = polls(conn, rows)
    submit(conn, "r1", "15")
    t = get_machine_state(conn, now=NOW).wildcard_trial
    assert (t.verdict, t.price_test) == (PRICE_TEST, True)
    # Back at $3.00 at one poll, then unpriced again: only windows with a price at or under $6.50 at both ends count.
    polls(conn, [("e0", "2026-10-27T22:00:00+00:00", {"15": (300, 124, 81600), "16": (200, 64)}),
                 ("e1", "2026-10-28T22:00:00+00:00", {"15": (None, 127, 82500), "16": (200, 66)}),
                 ("e2", "2026-10-29T22:00:00+00:00", {"15": (None, 130, 83400), "16": (200, 68)})], after=last)
    t = get_machine_state(conn, now=datetime(2026, 10, 30, 12, tzinfo=timezone.utc)).wildcard_trial
    assert not t.price_test and t.since == datetime(2026, 10, 27, 22, tzinfo=timezone.utc)
    assert (t.verdict, t.class_days_in_stock, t.units_sold) == (TOO_EARLY, 0, 0)


def test_money_comes_from_the_same_windows_as_the_units(conn):
    # The units counter misread at the first poll after the load, so the units window runs from before the
    # load and is left out. The cents window from that poll must be left out with it: $3.00 of takings on
    # no counted units would be profit from nothing.
    store_poll(conn, "p0", "2026-10-16T18:00:00+00:00", {"15": (300, 100), "16": (200, 50)})
    fulfilled(conn, "r1", SINCE, ("15", "AAA", "primary", "10.00", 20, 0, 0), ("16", "BBB", "primary", "10.00", 20, 0, 0))
    store_poll(conn, "p1", "2026-10-16T21:00:00+00:00", {"15": (300, 5, 30000), "16": (200, 50)})
    rows = [("p2", "2026-10-16T22:00:00+00:00", {"15": (300, 101), "16": (200, 50)})]
    rows += [(f"d{n}", f"2026-10-{day}T22:00:00+00:00", {"15": (300, 104 + 3 * n), "16": (200, 52 + 2 * n)})
             for n, day in enumerate(DAYS)]
    polls(conn, rows)
    submit(conn, "r1", "15")
    t = get_machine_state(conn, now=NOW).wildcard_trial
    # The profit is 18 units' worth: $54.00 of takings, $50.00 without tax, less 18 x $0.50.
    assert (t.units_sold, t.class_days_in_stock) == (18, 6)
    assert t.profit_per_class_day == f"{(5000 - 900) / 100 / 6:.2f}"


def test_days_without_polls_are_not_days_with_no_sales(conn):
    # One poll after the load and nothing since: no class day was seen, so it is too early, not a drop.
    store_poll(conn, "p0", "2026-10-16T12:00:00+00:00", {"15": (300, 100), "16": (200, 50)})
    fulfilled(conn, "r1", SINCE, ("15", "AAA", "primary", "10.00", 20, 0, 0), ("16", "BBB", "primary", "10.00", 20, 0, 0))
    store_poll(conn, "p1", "2026-10-16T21:00:00+00:00", {"15": (300, 100), "16": (200, 50)})
    submit(conn, "r1", "15")
    t = get_machine_state(conn, now=NOW).wildcard_trial
    assert (t.verdict, t.class_days_in_stock, t.units_sold) == (TOO_EARLY, 0, 0)


def test_polls_that_saw_no_sales_are_days_with_no_sales(conn):
    week_of_sales(conn, per_day15=0)
    submit(conn, "r1", "15")
    t = get_machine_state(conn, now=NOW).wildcard_trial
    assert (t.verdict, t.class_days_in_stock, t.units_sold, t.profit_per_class_day) == (DROP, 6, 0, "0.00")


def test_rates_are_compared_over_the_days_both_held_stock(conn):
    # 15 sells out in week one; 16 is loaded only after that. They share no in-stock day, so no verdict.
    store_poll(conn, "p0", "2026-10-16T18:00:00+00:00", {"15": (300, 100), "16": (200, 50)})
    fulfilled(conn, "r1", SINCE, ("15", "AAA", "primary", "10.00", 15, 0, 0))
    rows = [("p1", "2026-10-16T21:00:00+00:00", {"15": (300, 100), "16": (200, 50)})]
    rows += [(f"d{n}", f"2026-10-{day}T22:00:00+00:00", {"15": (300, 103 + 3 * n), "16": (200, 50)})
             for n, day in enumerate(("19", "20", "21", "22", "23"))]
    fulfilled(conn, "r2", datetime(2026, 10, 23, 23, tzinfo=timezone.utc), ("16", "BBB", "primary", "10.00", 20, 0, 0))
    rows += [(f"e{n}", f"2026-{day}T22:00:00+00:00", {"15": (300, 115), "16": (200, 51 + n)})
             for n, day in enumerate(("10-26", "10-27", "10-28", "10-29", "10-30", "11-02"))]
    polls(conn, rows)
    submit(conn, "r1", "15")
    t = get_machine_state(conn, now=datetime(2026, 11, 3, 12, tzinfo=timezone.utc)).wildcard_trial
    assert t.class_days_in_stock == 5 and t.verdict == UNKNOWN
    assert t.weakest_slot is None and "both it and the trial holding stock" in t.reason


def test_a_trial_whose_product_is_in_another_slot_has_no_verdict(conn):
    week_of_sales(conn)
    fulfilled(conn, "r0", SINCE - timedelta(days=3), ("17", "AAA", "primary", "10.00", 20, 0, 0))
    submit(conn, "r1", "15")
    t = get_machine_state(conn, now=NOW).wildcard_trial
    assert t.verdict == UNKNOWN and "also in slot 17" in t.reason


def test_the_trial_counts_from_when_the_slot_was_named(conn):
    week_of_sales(conn)
    request(conn, "r0", SINCE - timedelta(days=2), status="fulfilled")
    submit(conn, "r0", None, at=SINCE - timedelta(days=3))
    named = datetime(2026, 10, 21, 23, tzinfo=timezone.utc)
    request(conn, "r2", named, status="fulfilled")
    submit(conn, "r2", "15", at=named - timedelta(days=1))
    t = get_machine_state(conn, now=NOW).wildcard_trial
    assert t.since == named
    assert t.verdict == TOO_EARLY and t.units_sold == 9        # sold at the polls of 22, 23 and 26 Oct
    assert t.class_days_in_stock == 3
    assert current_wildcard(conn) == ("15", named)
    request(conn, "r3", NOW - timedelta(hours=1), status="fulfilled")
    submit(conn, "r3", "15", at=NOW - timedelta(hours=2))      # named again: the run began on the 21st
    assert current_wildcard(conn)[1] == named


def test_units_sold_past_the_receipts_make_profit_unknown(conn):
    # 10 loaded, 12 sold: the last window's extra units have no cost, so the profit isn't free money.
    store_poll(conn, "p0", "2026-10-16T18:00:00+00:00", {"15": (300, 100), "16": (200, 50)})
    fulfilled(conn, "r1", SINCE, ("15", "AAA", "primary", "5.00", 10, 0, 0), ("16", "BBB", "primary", "10.00", 20, 0, 0))
    rows = [("p1", "2026-10-16T21:00:00+00:00", {"15": (300, 100), "16": (200, 50)})]
    rows += [(f"d{n}", f"2026-10-{day}T22:00:00+00:00", {"15": (300, 103 + 3 * n), "16": (200, 52 + 2 * n)})
             for n, day in enumerate(("19", "20", "21", "22"))]
    polls(conn, rows)
    submit(conn, "r1", "15")
    reply = get_machine_state(conn, now=NOW)
    t = reply.wildcard_trial
    assert any("more sales than units we loaded" in w for w in reply.warnings)
    assert t.units_sold == 12 and t.profit_per_class_day is None


def test_the_planner_gets_the_wildcard_in_effect_and_which_slots_it_cant_see(conn):
    from tests.test_ledger import open_ledger
    from vending import planning

    open_ledger(conn)
    week_of_sales(conn)
    submit(conn, "r1", "15")
    gathered = planning.gather(conn, [], NOW)
    assert gathered.current.wildcard == "15"
    by_slot = {s.slot: s for s in gathered.current.slots}
    assert by_slot["15"].contents_known and not by_slot["17"].contents_known


# --- Regressions from the review of ff4caa2 -----------------------------------

def plan_now(conn, wildcard, off_cycle=False, now=NOW):
    """The planner on live data, every slot kept: what preview and submit run (`planning.plan` gathers the same).
    The halt switch, on in a new database, is left out."""
    from dataclasses import replace

    from vending import planner, planning

    gathered = planning.gather(conn, [], now)
    desired = planner.DesiredState(tuple(planner.DesiredSlot(s, keep=True) for s in gathered.config.slots),
                                   off_cycle=off_cycle, wildcard=wildcard)
    return planner.plan_restock(gathered.current, desired, replace(gathered.config, halted=False), now), gathered


def wildcard_refusals(p):
    return {r.slot for r in p.refusals if r.code == "wildcard"}


def test_two_polls_a_week_apart_see_no_class_days(conn):
    # The poll after Friday's load, then nothing until the next Friday: the five class days between weren't
    # watched, so neither they nor the sales in that stretch count. Before, this was five days and "promote".
    store_poll(conn, "p0", "2026-10-16T18:00:00+00:00", {"15": (300, 100), "16": (200, 50)})
    fulfilled(conn, "r1", SINCE, ("15", "AAA", "primary", "10.00", 20, 0, 0), ("16", "BBB", "primary", "10.00", 20, 0, 0))
    store_poll(conn, "p1", "2026-10-16T21:00:00+00:00", {"15": (300, 100), "16": (200, 50)})
    store_poll(conn, "p2", "2026-10-23T22:00:00+00:00", {"15": (300, 115), "16": (200, 60)})
    submit(conn, "r1", "15")
    t = get_machine_state(conn, now=NOW).wildcard_trial
    assert (t.verdict, t.class_days_in_stock, t.units_sold) == (TOO_EARLY, 0, 0)


def test_a_week_without_polls_counts_neither_its_days_nor_its_sales(conn):
    # Watched Monday and Tuesday, then the polls stop for a week (15 units sell meanwhile), then resume.
    store_poll(conn, "p0", "2026-10-16T18:00:00+00:00", {"15": (300, 100), "16": (200, 50)})
    fulfilled(conn, "r1", SINCE, ("15", "AAA", "primary", "10.00", 40, 0, 0), ("16", "BBB", "primary", "10.00", 40, 0, 0))
    polls(conn, [("p1", "2026-10-16T21:00:00+00:00", {"15": (300, 100), "16": (200, 50)}),
                 ("d0", "2026-10-19T22:00:00+00:00", {"15": (300, 103), "16": (200, 51)}),
                 ("d1", "2026-10-20T22:00:00+00:00", {"15": (300, 106), "16": (200, 52)})])
    assert timedelta(days=7) > MAX_POLL_GAP
    rows, u15, u16 = [("e0", "2026-10-27T22:00:00+00:00", {"15": (300, 121), "16": (200, 57)})], 121, 57
    for n, day in enumerate(("10-28", "10-29", "10-30", "11-02", "11-03")):
        u15, u16 = u15 + 3, u16 + 1
        rows.append((f"e{n + 1}", f"2026-{day}T22:00:00+00:00", {"15": (300, u15), "16": (200, u16)}))
    polls(conn, rows)
    submit(conn, "r1", "15")
    t = get_machine_state(conn, now=datetime(2026, 11, 4, 12, tzinfo=timezone.utc)).wildcard_trial
    # 19 and 20 Oct, then 28 Oct to 3 Nov: 7 class days and 3 a day. The week's 21, 22, 23, 26 and 27 Oct,
    # and the 15 units sold in it, are left out.
    assert (t.class_days_in_stock, t.units_sold) == (7, 21)
    assert t.weakest_class_days_in_stock == 7
    assert t.verdict == PROMOTE


def test_an_unpriced_poll_keeps_a_price_test_the_wildcard_in_preview_and_submit(conn):
    from tests.test_ledger import open_ledger

    open_ledger(conn)
    last = week_of_sales(conn, price15=750)                    # $7.50 to the poll of Monday 26 Oct
    submit(conn, "r1", "15")
    store_poll(conn, "x0", "2026-10-27T01:00:00+00:00", {"15": (None, 118, 88500), "16": (200, 62)})
    # get_machine_state calls it a price test: the latest poll that read a price read $7.50.
    assert get_machine_state(conn, now=NOW).wildcard_trial.price_test
    # So the planner (preview and submit both run `planning.plan`) keeps it the wildcard: naming 16 is refused.
    p, gathered = plan_now(conn, "16")
    s15 = next(s for s in gathered.current.slots if s.slot == "15")
    assert s15.price_cents is None and s15.price_test
    assert "15" in wildcard_refusals(p)
    assert "15" not in wildcard_refusals(plan_now(conn, "15")[0])
    # A strictly later poll reads it at $6.00: the price test is over, and the wildcard may move.
    polls(conn, [("x1", "2026-10-27T04:00:00+00:00", {"15": (600, 118, 88500), "16": (200, 62)})], after=last)
    assert not get_machine_state(conn, now=NOW).wildcard_trial.price_test
    assert "15" not in wildcard_refusals(plan_now(conn, "16")[0])


def test_polls_at_the_same_moment_that_disagree_on_a_price_settle_nothing(conn):
    from tests.test_ledger import open_ledger

    open_ledger(conn)
    last = week_of_sales(conn, price15=750)
    submit(conn, "r1", "15")
    same = "2026-10-27T01:00:00+00:00"
    store_poll(conn, "x1", same, {"15": (750, 118), "16": (200, 62)})
    store_poll(conn, "x2", same, {"15": (300, 118, 88500), "16": (200, 62)})
    t = get_machine_state(conn, now=NOW).wildcard_trial                  # this raised StopIteration
    assert (t.verdict, t.price_test) == (UNKNOWN, False) and "different prices" in t.reason
    # The planner treats it as a price test still open: its price is unknown, and it stays the wildcard.
    p, gathered = plan_now(conn, "16")
    s15 = next(s for s in gathered.current.slots if s.slot == "15")
    assert s15.price_cents is None and s15.price_test
    assert "15" in wildcard_refusals(p)
    # A strictly later poll at $3.00 ends the price test: the trial counts from it.
    later = datetime(2026, 10, 27, 2, tzinfo=timezone.utc)
    polls(conn, [("x3", later.isoformat(), {"15": (300, 118, 88500), "16": (200, 62)})], after=last)
    t = get_machine_state(conn, now=NOW).wildcard_trial
    assert (t.price_test, t.since, t.verdict) == (False, later, TOO_EARLY)
    assert "15" not in wildcard_refusals(plan_now(conn, "16")[0])


def test_a_fulfilled_visit_without_an_end_time_leaves_the_wildcard_unresolved(conn):
    from tests.test_ledger import open_ledger
    from vending import planning

    open_ledger(conn)
    week_of_sales(conn)
    submit(conn, "r1", "15")
    # Filed on the 20th naming 16 and fulfilled, but with no fulfilled_at: when its visit happened is unknown,
    # so it can't be put before or after r1's (the 16th). Its filing time is no stand-in.
    conn.execute("insert into purchase_requests (request_id, submitted_at, off_cycle, request_body, status)"
                 " values ('r2', %s, false, '{}', 'fulfilled')", (SINCE + timedelta(days=4),))
    submit(conn, "r2", "16", at=SINCE + timedelta(days=4))
    conn.execute("update purchase_requests set fulfilled_at = %s where request_id = 'r1'", (SINCE + timedelta(days=7),))
    with pytest.raises(WildcardUnresolved, match="r2"):
        current_wildcard(conn)
    reply = get_machine_state(conn, now=NOW)
    assert (reply.wildcard_trial.slot, reply.wildcard_trial.verdict) == (None, UNKNOWN)
    assert "r2" in reply.wildcard_trial.reason
    with pytest.raises(planning.NotReady, match="r2"):
        planning.gather(conn, [], NOW)
    # Once its time is recorded, it is put in order.
    conn.execute("update purchase_requests set fulfilled_at = %s where request_id = 'r2'", (SINCE + timedelta(days=8),))
    assert current_wildcard(conn) == ("16", SINCE + timedelta(days=8))


def test_the_visit_end_is_the_door_closing_where_it_is_recorded(conn):
    # r1 (naming 15) closed the door on the 16th but was marked fulfilled on the 23rd, after r2 (naming 16),
    # which closed the door on the 23rd. The visits happened r1 then r2, so 16 is the wildcard.
    conn.execute("alter table purchase_requests add column door_closed_at timestamptz")
    r2_door = datetime(2026, 10, 23, 19, tzinfo=timezone.utc)
    for request_id, named, door, done in (("r1", "15", SINCE, datetime(2026, 10, 23, 21, tzinfo=timezone.utc)),
                                          ("r2", "16", r2_door, r2_door + timedelta(minutes=30))):
        conn.execute("insert into purchase_requests (request_id, submitted_at, off_cycle, request_body, status,"
                     " fulfilled_at, door_closed_at) values (%s, %s, false, '{}', 'fulfilled', %s, %s)",
                     (request_id, door - timedelta(days=1), done, door))
        submit(conn, request_id, named, at=door - timedelta(days=1))
    assert current_wildcard(conn) == ("16", r2_door)
    # Where the door time is missing, fulfilled_at stands in; where both are, the visit is unresolved.
    conn.execute("update purchase_requests set door_closed_at = null where request_id = 'r2'")
    assert current_wildcard(conn) == ("16", r2_door + timedelta(minutes=30))
    conn.execute("update purchase_requests set fulfilled_at = null where request_id = 'r2'")
    with pytest.raises(WildcardUnresolved, match="door_closed_at"):
        current_wildcard(conn)


def test_a_wildcard_dropped_from_our_assignment_is_no_wildcard_for_the_planner(conn):
    from tests.test_ledger import open_ledger

    open_ledger(conn)
    week_of_sales(conn)
    submit(conn, "r1", "15")
    conn.execute("update slot_assignment set slots = array_remove(slots, '15')")
    # A midweek plan names none: before, the planner required 15 and refused it as not ours.
    p, gathered = plan_now(conn, None, off_cycle=True)
    assert gathered.current.wildcard is None
    assert not wildcard_refusals(p), [r.message for r in p.refusals]
    assert any("isn't one of ours" in note for note in gathered.notes)
    # Naming 15 is refused: it isn't ours. get_machine_state agrees there is no trial.
    assert codes_of(plan_now(conn, "15", off_cycle=True)[0]) == ["wildcard"]
    assert get_machine_state(conn, now=NOW).wildcard_trial.slot is None


def codes_of(p):
    return [r.code for r in p.refusals if r.code == "wildcard"]
