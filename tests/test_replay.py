"""Tests for the week replayer: replay from stored polls, the plan checks, scoring, cases and the CLI."""

import json
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

import psycopg
import pytest

from tests.conftest import open_in, store_poll
from vending.replay.__main__ import main, run_week
from vending.replay.cases import all_cases
from vending.replay.deciders import Baseline, Plan, SlotPlan, check_plan
from vending.replay.scoring import FAIL, PASS, SKIP, score
from vending.replay.state import NoData, replay_week
from vending.slots import SLOTS

WEEK = date(2026, 10, 2)   # a Friday; the week starts at 04:00 UTC (midnight Eastern)


def fill_week(conn):
    """Polls just before three week boundaries. Slot 17's counter drops; 18 appears late."""
    store_poll(conn, "a", "2026-10-02T03:00:00+00:00", {"15": (250, 100), "16": (300, 10), "17": (200, 50)})
    store_poll(conn, "b", "2026-10-09T03:00:00+00:00",
               {"15": (250, 107), "16": (325, 12), "17": (200, 40), "18": (275, 5)})
    store_poll(conn, "c", "2026-10-16T03:00:00+00:00",
               {"15": (250, 116), "16": (325, 12), "17": (200, 45), "18": (275, 9)})


def case(name):
    return next(c for c in all_cases() if c.name == name)


def checks(result):
    return {c.name: c for c in result.checks}


# --- Replaying a week from stored polls ---------------------------------------

def test_replay_rebuilds_the_week_and_leaves_unknowns_unknown(conn):
    fill_week(conn)
    state = replay_week(conn, WEEK)
    assert state.polls == (datetime(2026, 10, 2, 3, tzinfo=timezone.utc), datetime(2026, 10, 9, 3, tzinfo=timezone.utc))
    by = {s.slot: s for s in state.slots}
    assert len(by) == 20
    assert (by["15"].units_sold, by["15"].price_cents, by["15"].revenue_cents) == (7, 250, 1750)
    assert by["15"].units_per_day == Decimal("1.00") and by["15"].price_changed is False
    assert (by["16"].units_sold, by["16"].price_cents, by["16"].price_changed) == (2, 325, True)
    assert by["17"].units_sold is None          # counter went down: unknown, not negative or zero
    assert (by["18"].units_sold, by["18"].price_cents, by["18"].price_changed) == (None, 275, None)
    assert (by["25"].units_sold, by["25"].price_cents) == (None, None)   # never polled
    assert all(s.stock is None and s.product is None and s.unit_cost_cents is None for s in state.slots)
    assert any("receipts" in n for n in state.notes)


def test_replay_refuses_a_week_with_no_polls(conn):
    with pytest.raises(NoData, match="Fewer than two"):
        replay_week(conn, WEEK)


def test_thin_coverage_is_noted(conn):
    store_poll(conn, "a", "2026-10-05T12:00:00+00:00", {"15": (250, 100)})
    store_poll(conn, "b", "2026-10-06T12:00:00+00:00", {"15": (250, 104)})
    state = replay_week(conn, WEEK)
    assert state.slot("15").units_per_day == Decimal("4.00")
    assert any("cover only 1.0 days" in n for n in state.notes)
    assert any("starts at the first one" in n for n in state.notes)


def test_replay_uses_the_stored_slots_for_prices_and_the_plan_check(conn):
    """A stored assignment that isn't the list in the code: 12 is ours here, 16 isn't."""
    from vending.replay.scoring import score
    conn.execute("update slot_assignment set slots = '{12,15}'")
    store_poll(conn, "a", "2026-10-02T03:00:00+00:00", {"12": (200, 10), "15": (250, 100), "16": (300, 10)})
    store_poll(conn, "b", "2026-10-09T03:00:00+00:00", {"12": (225, 14), "15": (250, 107), "16": (300, 12)})
    state = replay_week(conn, WEEK)
    by = {s.slot: s for s in state.slots}
    assert set(by) == {"12", "15"}
    assert (by["12"].units_sold, by["12"].price_cents, by["12"].price_changed) == (4, 225, True)

    ours = {"slots": [{"slot": s, "fill_to": 5, "sale_price": "2.50", "reason": "r"} for s in ("12", "15")]}
    valid = checks(score(state, ours, None))["valid plan"]
    assert valid.status == "pass" and "All 2 slots once" in valid.detail
    refused = checks(score(state, plan_json(), None))["valid plan"]
    assert refused.status == "fail" and "Slots missing: 12" in refused.detail and "not ours: 16" in refused.detail


# --- The CLI on stored polls -------------------------------------------------

def test_cli_scores_a_week_against_the_next(conn, capsys):
    fill_week(conn)
    assert main(["--week", "2026-10-02", "--unit-cost", "0.80", "--balance", "600", "--reasons"], conn=conn) == 0
    out = capsys.readouterr().out
    assert "PASS  valid plan" in out and "PASS  prices" in out and "PASS  balance" in out
    assert "SKIP  no writes" in out   # the baseline can't reach the database
    row15 = next(line for line in out.splitlines() if line.startswith("15 "))
    # baseline: 7 on the rail at $2.50; 9 sold the next week, so 7 sell and 2 are short
    assert row15.split()[:10] == ["15", "$2.50", "7", "1.00", "7", "@", "$2.50", "9", "7", "2"]
    assert "every unit cost $0.80" in out and "Demand assumptions" in out
    assert "15: Sold 7 last week" in out


def test_cli_runs_read_only(conn, capsys):
    fill_week(conn)
    main(["--week", "2026-10-02"], conn=conn)
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        conn.execute("insert into action_log (kind, tool, arguments, outcome) values ('read', 't', '{}', 'done')")


def test_cli_says_when_the_following_week_is_missing(conn, capsys):
    store_poll(conn, "a", "2026-10-02T03:00:00+00:00", {"15": (250, 100)})
    store_poll(conn, "b", "2026-10-09T03:00:00+00:00", {"15": (250, 107)})
    assert main(["--week", "2026-10-02"], conn=conn) == 0
    assert "outcome can't be scored" in capsys.readouterr().out


def test_cli_reports_a_week_it_cannot_replay(conn, capsys):
    assert main(["--week", "2026-10-02"], conn=conn) == 1
    assert "Fewer than two" in capsys.readouterr().err


def test_a_decider_that_writes_fails_no_writes(schema_url, conn):
    fill_week(conn)

    class Writer(Baseline):
        name = "writer"
        uses_database = True

        def decide(self, state):   # as a model calling a write tool through the MCP server would
            with open_in(*schema_url) as other:
                other.execute("insert into action_log (kind, tool, arguments, outcome)"
                              " values ('write', 'set_price', '{}', 'refused')")
            return super().decide(state)

    _, _, result = run_week(conn, WEEK, Writer())
    assert checks(result)["no writes"].status == FAIL
    class Reader(Baseline):
        uses_database = True

    _, _, result = run_week(conn, WEEK, Reader())
    assert checks(result)["no writes"].status == PASS
    _, _, result = run_week(conn, WEEK, Baseline())
    assert checks(result)["no writes"].status == SKIP


# --- Checking a plan ---------------------------------------------------------

def plan_json(**slot_fields):
    return {"slots": [{"slot": s, "fill_to": 5, "sale_price": "2.50", "reason": "r", **slot_fields.get(s, {})}
                      for s in SLOTS]}


def test_a_good_json_plan_parses():
    plan, refusals = check_plan(plan_json(**{"16": {"keep": True}}))
    assert refusals == [] and plan.slot("15").price_cents == 250 and plan.slot("16").keep


@pytest.mark.parametrize("answer, code", [
    ("restock everything", "not_a_plan"),
    ({"slots": "all of them"}, "not_a_plan"),
    ({"slots": [{"slot": "15", "fill_to": 5, "sale_price": "2.50", "reason": "r"}]}, "slot_list"),
    (plan_json(**{"15": {"sale_price": 2.5}}), "bad_price"),
    (plan_json(**{"15": {"sale_price": 250}}), "bad_price"),          # a number: cents or dollars?
    (plan_json(**{"15": {"sale_price": "sNaN"}}), "bad_price"),
    (plan_json(**{"15": {"sale_price": "1E+999999999"}}), "bad_price"),
    (plan_json(**{"15": {"sale_price": {}}}), "bad_price"),
    (plan_json(**{"15": {"fill_to": 51}}), "bad_fill"),
    (plan_json(**{"15": {"fill_to": 10**30}}), "bad_fill"),
    (plan_json(**{"15": {"product": None}}), "empty_slot"),
    ({**plan_json(), "sourcing": "BJs"}, "not_a_plan"),
    (plan_json(**{"15": {"sale_price": "two fifty"}}), "bad_price"),
    (plan_json(**{"15": {"fill_to": -3}}), "bad_fill"),
    (plan_json(**{"15": {"fill_to": True}}), "bad_fill"),
    (plan_json(**{"15": {"fill_to": "7"}}), "bad_fill"),
    (plan_json(**{"15": {"reason": " "}}), "no_reason"),
    (plan_json(**{"15": {"sale_price": None}}), "not_a_plan"),
    (plan_json(**{"15": {"product": ""}}), "bad_product"),
    ({**plan_json(), "visit": "tomorrow"}, "bad_visit"),
    ({"slots": plan_json()["slots"] + [{"slot": "11", "keep": True, "reason": "r"}]}, "slot_list"),
    ({"slots": plan_json()["slots"] + [{"slot": "15", "keep": True, "reason": "r"}]}, "slot_list"),
])
def test_nonsense_plans_are_refused_not_crashed_on(answer, code):
    plan, refusals = check_plan(answer)
    assert plan is None and code in {r.code for r in refusals}


def test_an_invalid_plan_skips_the_other_checks_and_is_not_scored():
    c = case("low-balance")
    result = score(c.state, {"slots": []}, c.next_week)
    assert checks(result)["valid plan"].status == FAIL and not result.passed
    assert {checks(result)[n].status for n in ("catalog", "prices", "balance")} == {SKIP}
    assert result.slots == ()


def test_off_step_and_over_ceiling_prices_fail():
    c = case("catalog-request")
    result = score(c.state, plan_json(**{"15": {"sale_price": "2.52"}, "16": {"sale_price": "6.50"}}), c.next_week)
    detail = checks(result)["prices"].detail
    assert checks(result)["prices"].status == FAIL and "multiple of 5 cents" in detail and "ceiling" in detail


# --- The baseline on the team's cases ----------------------------------------

def run(name, answer=None):
    c = case(name)
    return score(c.state, Baseline().decide(c.state) if answer is None else answer, c.next_week)


def test_every_case_runs_and_the_baseline_gives_a_valid_plan():
    assert len(all_cases()) == 10
    for c in all_cases():
        assert checks(score(c.state, Baseline().decide(c.state), c.next_week))["valid plan"].status == PASS, c.name


def test_low_balance_and_below_floor_catch_the_baseline():
    assert checks(run("low-balance"))["balance"].status == FAIL
    prices = checks(run("price-below-floor"))["prices"]
    assert prices.status == FAIL and "floor of $2.60" in prices.detail


def test_tuesday_sellout_shows_censored_demand():
    result = run("tuesday-sellout")
    o = next(o for o in result.slots if o.slot == "15")
    assert (o.available, o.demand, o.sold, o.stockout) == (10, 17, 10, 7)


def test_slow_seller_leaves_units_unsold():
    o = next(o for o in run("slow-seller").slots if o.slot == "15")
    assert (o.available, o.sold, o.unsold) == (9, 0, 9)


def test_unknown_inputs_stay_unknown():
    c = case("nonsense-input")
    plan = Baseline().decide(c.state)
    for s in c.state.slots:
        assert plan.slot(s.slot).keep == (s.units_sold is None or s.price_cents is None)
    result = score(c.state, plan, c.next_week)
    assert checks(result)["balance"].status == SKIP and checks(result)["catalog"].status == PASS
    assert result.totals.scored == 7   # only slots known this week and the next
    unknown_next = {s.slot for s in c.next_week.slots if s.units_sold is None}
    for o in result.slots:
        if o.slot in unknown_next or c.state.slot(o.slot).stock is None:
            assert (o.sold, o.margin_cents) == (None, None), o.slot   # unscored, not zero


def test_cold_start_is_not_scoreable():
    result = run("fills-first")
    assert result.passed and result.totals.scored == 0 and result.totals.margin_cents is None


def stocking(product, slot="15"):
    plan = Baseline().decide(case("catalog-request").state)
    slots = tuple(SlotPlan(s.slot, "try it", fill_to=4, price_cents=300, product=product) if s.slot == slot else s
                  for s in plan.slots)
    return Plan(slots, reason=plan.reason)


def test_catalog_requests_pending_or_refused_fail():
    pending = checks(run("catalog-request", stocking("PICKLE-POTION-12OZ")))["catalog"]
    assert pending.status == FAIL and "Not in the active catalog" in pending.detail
    refused = checks(run("rejected-request", stocking("MONSTER-ULTRA-16OZ")))["catalog"]
    assert refused.status == FAIL and "refused" in refused.detail
    assert checks(run("holiday-drinks", stocking("SEASONAL-CIDER-12OZ", "18")))["catalog"].status == PASS


def test_a_new_product_is_unscored_and_its_cost_unknown():
    result = run("holiday-drinks", stocking("SEASONAL-CIDER-12OZ", "18"))
    o = next(o for o in result.slots if o.slot == "18")
    assert o.sold is None and "new product" in o.notes[0]
    assert checks(result)["balance"].status == SKIP   # the cider's unit cost is unknown


def test_a_price_change_is_scored_on_the_same_demand_and_noted():
    plan = Baseline().decide(case("catalog-request").state)
    slots = tuple(SlotPlan(s.slot, s.reason, fill_to=s.fill_to, price_cents=275) if s.slot == "15" else s
                  for s in plan.slots)
    o = next(o for o in run("catalog-request", Plan(slots)).slots if o.slot == "15")
    assert o.sold == 7 and "same demand assumed" in o.notes[0]


def test_a_midweek_visit_is_worth_it_here():
    c = case("midweek-visit")
    baseline = score(c.state, Baseline().decide(c.state), c.next_week)
    assert baseline.totals.stockout_units == 26
    visit = Plan(tuple(SlotPlan(s, "refill the sell-out", fill_to=13, price_cents=250) if s in ("15", "16")
                       else SlotPlan(s, "enough until Friday", keep=True) for s in SLOTS), visit="midweek")
    result = score(c.state, visit, c.next_week)
    assert result.passed and result.totals.stockout_units == 0 and result.totals.fee_cents == 2500
    assert result.totals.margin_cents - baseline.totals.margin_cents == 918   # $34.18 saved - $25 fee


# --- The CLI without a database -----------------------------------------------

def test_cli_prints_the_cases_table(capsys):
    assert main(["--cases"]) == 0
    out = capsys.readouterr().out
    assert "low-balance" in out and "balance" in out.split("low-balance")[1].splitlines()[0]


def test_cli_scores_a_plan_from_a_file(tmp_path, capsys):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan_json(**{"15": {"sale_price": "2.53"}})), encoding="utf-8")
    assert main(["--case", "catalog-request", "--decider", f"file:{path}"]) == 0
    out = capsys.readouterr().out
    assert "FAIL  prices" in out and "15: $2.53 is not a multiple of 5 cents" in out


def test_cli_refuses_an_unknown_decider_or_case(capsys):
    assert main(["--cases", "--decider", "opus"]) == 2
    assert main(["--case", "nope"]) == 2
    assert "Unknown decider" in capsys.readouterr().err


# --- Found by the blind review ------------------------------------------------

def test_a_zero_price_fails_even_with_no_unit_cost():
    c = case("catalog-request")
    state = replace(c.state, slots=tuple(replace(s, unit_cost_cents=None) for s in c.state.slots))
    plan = Baseline().decide(state)
    assert checks(score(state, plan, c.next_week))["prices"].status == SKIP   # floor unchecked
    free = Plan(tuple(SlotPlan(s.slot, s.reason, fill_to=s.fill_to, price_cents=0) if s.slot == "16" else s
                      for s in plan.slots))
    prices = checks(score(state, free, c.next_week))["prices"]
    assert prices.status == FAIL and "16: $0.00 is below" in prices.detail


def test_a_long_gap_in_the_polls_is_flagged(conn):
    store_poll(conn, "a", "2026-09-28T12:00:00+00:00", {"15": (250, 100)})
    store_poll(conn, "b", "2026-10-09T03:00:00+00:00", {"15": (250, 111)})
    state = replay_week(conn, WEEK)
    assert state.slot("15").units_sold == 11
    assert any("span 10.6 days, not 7" in n for n in state.notes)


def test_a_partial_following_week_is_shown(conn, capsys):
    store_poll(conn, "a", "2026-10-02T03:00:00+00:00", {"15": (250, 100)})
    store_poll(conn, "b", "2026-10-09T03:00:00+00:00", {"15": (250, 107)})
    store_poll(conn, "c", "2026-10-11T03:00:00+00:00", {"15": (250, 109)})   # 2 days in
    main(["--week", "2026-10-02"], conn=conn)
    out = capsys.readouterr().out
    assert "following week's polls: Thu 08 Oct 2026 23:00 to Sat 10 Oct 2026 23:00 (2.0 days)" in out
    assert "Note, following week: WARNING: the polls cover only 2.0 days" in out


def test_the_daylight_saving_week_ends_at_eastern_midnight(conn):
    # 30 Oct 00:00 EDT is 04:00 UTC; 6 Nov 00:00 EST is 05:00 UTC (clocks went back 1 Nov).
    store_poll(conn, "a", "2026-10-30T03:59:00+00:00", {"15": (250, 100)})
    store_poll(conn, "b", "2026-11-06T04:30:00+00:00", {"15": (250, 108)})   # 23:30 EST on 5 Nov
    store_poll(conn, "c", "2026-11-06T05:30:00+00:00", {"15": (250, 120)})   # 00:30 EST on 6 Nov
    state = replay_week(conn, date(2026, 10, 30))
    assert state.end == datetime(2026, 11, 6, 5, tzinfo=timezone.utc)
    assert state.slot("15").units_sold == 8


def test_cli_refuses_a_negative_unit_cost(capsys):
    assert main(["--week", "2026-10-02", "--unit-cost", "-1"]) == 2
    assert "above zero" in capsys.readouterr().err


def test_a_plan_file_that_is_not_json_fails_valid_plan(tmp_path, capsys):
    path = tmp_path / "plan.json"
    path.write_text("Restock everything, thanks!", encoding="utf-8")
    assert main(["--case", "low-balance", "--decider", f"file:{path}"]) == 0
    assert "FAIL  valid plan" in capsys.readouterr().out
    assert main(["--case", "low-balance", "--decider", f"file:{tmp_path / 'missing.json'}"]) == 2
    assert "Can't read the plan file" in capsys.readouterr().err


def test_a_named_product_on_a_replayed_week_is_unscored_and_says_why(conn):
    fill_week(conn)
    state, next_week, _ = run_week(conn, WEEK, Baseline())
    plan = Baseline().decide(state)
    named = Plan(tuple(SlotPlan(s.slot, s.reason, fill_to=s.fill_to, price_cents=s.price_cents, product="COKE-20OZ")
                       if s.slot == "15" else s for s in plan.slots))
    o = next(o for o in score(state, named, next_week).slots if o.slot == "15")
    assert o.sold is None and "may be a switch" in o.notes[0]
