"""The weekly report card and the incident log (#48): `vending.report_card` on the dashboard."""

import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import psycopg
import pytest
from psycopg.types.json import Jsonb

from tests.conftest import store_poll
from tests.test_dashboard import fulfilled, page_conn  # noqa: F401
from vending import agent_runs, config, dashboard, ledger, report_card, slots, worker
from vending.replay import scoring
from vending.replay.deciders import Plan, SlotPlan
from vending.replay.state import SlotWeek, WeekState

UTC = timezone.utc
# Wed 2 Dec 2026, 10:00 Eastern. The opening week is Sat 28 Nov to Fri 4 Dec.
OPEN = datetime(2026, 12, 2, 15, tzinfo=UTC)
# Midnight Eastern on the Saturdays that start the weeks after it.
SAT1 = datetime(2026, 12, 5, 5, tzinfo=UTC)
SAT2 = datetime(2026, 12, 12, 5, tzinfo=UTC)
SAT3 = datetime(2026, 12, 19, 5, tzinfo=UTC)
NOW = SAT3 + timedelta(days=1)   # Sunday of the week after week 2, so weeks 1 and 2 are over
SKU = "SAMPLE-CAN"
SECRET = "connection to host db.internal refused for user vending_rw"


def product(conn, sku=SKU, size="12 oz can"):
    conn.execute("insert into catalog_products (sku, name, line, flavor, size, first_seen_at, last_response)"
                 " values (%s, %s, 'Fictional', null, %s, '2026-11-01', '{}')", (sku, sku, size))


def plan_for(ours, **changes):
    """A stored live plan in the desired state's shape: every slot kept, except `changes`."""
    rows = []
    for slot in ours:
        if slot in changes:
            rows.append({"slot": slot, "keep": False, **changes[slot]})
        else:
            rows.append({"slot": slot, "keep": True})
    return {"slots": rows, "off_cycle": False, "sourcing": [], "plan_hash": "abc", "visit": "scheduled",
            "reason": "test plan"}


def live_run(conn, at, status="submitted", plan=None, detail=None, request_id=None):
    run = agent_runs.start(conn, mode="live", model="claude-sonnet-5-5")
    if status != "running":
        agent_runs.finish(conn, run, status, plan=plan, detail=detail, request_id=request_id)
    conn.execute("update agent_runs set started_at = %s, finished_at = case when status = 'running' then null"
                 " else %s end where id = %s", (at, at + timedelta(minutes=5), run))
    return run


def week_of_sales(conn, slot, *, price=250):
    """The opening week sells 2 in `slot`, week 1 sells 6, week 2 sells 3 (so far, by NOW)."""
    store_poll(conn, "w0-start", (SAT1 - timedelta(days=7) + timedelta(hours=1)).isoformat(), {slot: (price, 100)})
    store_poll(conn, "w0-end", (SAT1 - timedelta(hours=1)).isoformat(), {slot: (price, 102)})
    store_poll(conn, "w1-start", (SAT1 + timedelta(minutes=30)).isoformat(), {slot: (price, 102)})
    store_poll(conn, "w1-end", (SAT2 - timedelta(hours=1)).isoformat(), {slot: (price, 108)})
    store_poll(conn, "w2-start", (SAT2 + timedelta(minutes=30)).isoformat(), {slot: (price, 108)})
    store_poll(conn, "w2-end", (SAT3 - timedelta(hours=1)).isoformat(), {slot: (price, 111)})
    store_poll(conn, "w3", (NOW - timedelta(hours=1)).isoformat(), {slot: (price, 112)})


@pytest.fixture
def opened(conn):
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=OPEN)
    conn.execute("update slot_assignment set first_read_at = %s", (OPEN - timedelta(days=10),))
    return slots.our_slots(conn)


def cards(page_conn, now=NOW):
    snap = dashboard.snapshot(page_conn, now, public=True)
    assert snap["report_cards"]["ok"], snap["report_cards"]
    return snap, {c["week"]: c for c in snap["report_cards"]["data"]["weeks"]}


# --- Report card --------------------------------------------------------------------------------

def test_before_the_opening_both_panels_say_so(conn, page_conn):
    snap = dashboard.snapshot(page_conn, NOW)
    assert snap["report_cards"]["data"] == {"opened": False}
    assert snap["incidents"]["data"] == {"opened": False}
    page = dashboard.render(snap)
    assert "No report cards yet. They start at the ledger opening" in page
    assert "No incidents yet. The log starts at the ledger opening." in page
    # An opening recorded ahead still shows the empty state until it passes.
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=OPEN)
    assert dashboard.snapshot(page_conn, OPEN - timedelta(seconds=1))["report_cards"]["data"] == {"opened": False}


def test_a_plan_that_restocks_beats_the_baseline(conn, page_conn, opened):
    slot = opened[0]
    product(conn)
    week_of_sales(conn, slot)
    # Planned on Thursday of the opening week; the Friday visit loads 8 at $1.00 a unit.
    run = live_run(conn, datetime(2026, 12, 3, 20, tzinfo=UTC),
                   plan=plan_for(opened, **{slot: {"fill_to": 8, "sale_price": "2.50", "reason": "fill it"}}), request_id="r1")
    fulfilled(conn, "r1", slot, SKU, units_loaded=8, paid="8.00", at=SAT1 - timedelta(hours=3))
    snap, by_week = cards(page_conn)
    assert list(by_week) == ["2026-12-19", "2026-12-12", "2026-12-05", "2026-11-28"]
    opening = by_week["2026-11-28"]
    assert opening["grade"] == "opening" and opening["checks"] == []

    week = by_week["2026-12-05"]
    assert week["final"] and week["run_id"] == run
    assert {c["name"]: c["status"] for c in week["checks"]} == {
        "valid plan": "pass", "catalog": "pass", "prices": "pass", "balance": "pass"}
    # The plan had 8 on the rail and 6 sold; the baseline restocks to last week's 2, which sell out.
    assert week["outcome"]["sold"] == 6 and week["outcome"]["unsold_units"] == 2
    assert week["outcome"]["sold_out_slots"] == 0
    assert week["baseline"]["sold"] == 2 and week["baseline"]["sold_out_slots"] == 1
    cmp = week["comparison"]
    assert cmp["slots"] == 1 and cmp["result"] == "beat"
    # $15.00 with 8% tax inside is $13.89 ex tax, less 6 x $1.00; the baseline's 2 sales: $4.63 less $2.00.
    assert (week["outcome"]["margin"], cmp["plan_margin"], cmp["baseline_margin"], cmp["difference"]) == (
        "7.89", "7.89", "2.63", "5.26")
    assert week["grade"] == "beat" and week["grade_text"] == "Beat the baseline"

    # No run submitted a plan in week 1, so no plan matched week 2's visit.
    assert by_week["2026-12-12"]["grade"] == "no_plan"
    current = by_week["2026-12-19"]
    assert not current["final"] and current["grade"] == "no_plan"

    page = dashboard.render(snap)
    assert page.index('id="plan"') < page.index('id="report-card"') < page.index('id="incidents"') < page.index('id="runs"')
    assert "Beat the baseline" in page and "ahead by $5.26" in page and "Opening week: not graded" in page
    assert '"report_cards"' in dashboard.to_json(snap)


def test_a_failed_hard_check_fails_the_week_whatever_it_earned(conn, page_conn, opened):
    slot = opened[0]
    product(conn)
    week_of_sales(conn, slot, price=700)
    live_run(conn, datetime(2026, 12, 3, 20, tzinfo=UTC),
             plan=plan_for(opened, **{slot: {"fill_to": 8, "sale_price": "7.00", "reason": "dear"}}), request_id="r1")
    fulfilled(conn, "r1", slot, SKU, units_loaded=8, paid="8.00", at=SAT1 - timedelta(hours=3))
    snap, by_week = cards(page_conn)
    week = by_week["2026-12-05"]
    prices = next(c for c in week["checks"] if c["name"] == "prices")
    assert prices["status"] == "fail" and "$6.50" in prices["detail"]
    assert week["comparison"]["result"] == "beat"       # it earned more, and still fails
    assert week["grade"] == "fail" and week["grade_text"] == "Failed a hard check"
    rows = snap["incidents"]["data"]["rows"]
    failed = [r for r in rows if r["kind"] == "week_failed"]
    assert len(failed) == 1 and failed[0]["note"] == "Failed the prices check; graded as failing."
    assert failed[0]["week"] == "2026-11-28"    # logged when the plan was made
    assert "Week failed" in dashboard.render(snap)


def test_the_wildcard_slot_is_checked_against_its_own_ceiling(conn, page_conn, opened):
    slot = opened[0]
    product(conn)
    config.set_value(conn, "wildcard_slot", slot, "test")
    week_of_sales(conn, slot, price=700)
    live_run(conn, datetime(2026, 12, 3, 20, tzinfo=UTC),
             plan=plan_for(opened, **{slot: {"fill_to": 8, "sale_price": "7.00", "reason": "wildcard"}}), request_id="r1")
    fulfilled(conn, "r1", slot, SKU, units_loaded=8, paid="8.00", at=SAT1 - timedelta(hours=3))
    _, by_week = cards(page_conn)
    prices = next(c for c in by_week["2026-12-05"]["checks"] if c["name"] == "prices")
    assert prices["status"] == "pass" and f"$8.00 for slot {slot}" in prices["detail"]


def test_fill_to_the_column_uses_the_package_capacity(conn, page_conn, opened):
    slot = opened[0]
    product(conn)   # a 12 oz can: 8 a column on the loading chart
    week_of_sales(conn, slot)
    live_run(conn, datetime(2026, 12, 3, 20, tzinfo=UTC),
             plan=plan_for(opened, **{slot: {"sale_price": "2.50", "reason": "fill the column"}}), request_id="r1")
    fulfilled(conn, "r1", slot, SKU, units_loaded=8, paid="8.00", at=SAT1 - timedelta(hours=3))
    _, by_week = cards(page_conn)
    week = by_week["2026-12-05"]
    assert week["checks"][0] == {"name": "valid plan", "status": "pass",
                                 "detail": f"All {len(opened)} slots once, every field well formed."}
    assert week["outcome"]["sold"] == 6 and week["outcome"]["unsold_units"] == 2


def test_a_plan_of_the_wrong_shape_fails_valid_plan(conn, page_conn, opened):
    week_of_sales(conn, opened[0])
    live_run(conn, datetime(2026, 12, 3, 20, tzinfo=UTC), plan={"note": "not a plan"})
    _, by_week = cards(page_conn)
    week = by_week["2026-12-05"]
    assert week["checks"][0]["status"] == "fail" and week["grade"] == "fail"


def _week_text(snap, week):
    card = next(c for c in snap["report_cards"]["data"]["weeks"] if c["week"] == week)
    page = dashboard.render(snap)
    # The report card's own section: the Runs panel shows each run's plan as it was stored.
    section = page[page.index('id="report-card"'):page.index('id="incidents"')]
    return card, dashboard.to_json(card) + section


def test_a_malformed_entry_fails_valid_plan_without_quoting_it(conn, page_conn, opened):
    week_of_sales(conn, opened[0])
    live_run(conn, datetime(2026, 12, 3, 20, tzinfo=UTC), plan=plan_for(opened, **{
        opened[0]: {"product": ["A", "B"], "sale_price": "2.50", "reason": "x"},
        opened[1]: {"fill_to": 3, "sale_price": f"<b>{SECRET}</b>", "reason": "y"},
        opened[2]: {"fill_to": "lots", "sale_price": "2.50", "reason": SECRET}}))
    # A later plan for the week after, so the plan panel shows a well-formed one.
    live_run(conn, datetime(2026, 12, 10, 20, tzinfo=UTC), plan=plan_for(opened))
    snap = dashboard.snapshot(page_conn, NOW, public=True)
    assert snap["report_cards"]["ok"]
    card, text = _week_text(snap, "2026-12-05")
    valid = card["checks"][0]
    assert valid["status"] == "fail" and card["grade"] == "fail"
    assert f"Slot {opened[0]}: the product isn't a catalog SKU." in valid["detail"]
    assert f"Slot {opened[1]}: the sale price isn't a price." in valid["detail"]
    assert f"Slot {opened[2]}: fill_to must be a whole number from 0 to 50." in valid["detail"]
    assert SECRET not in text
    assert "'lots'" not in text and '"lots"' not in text and "&quot;lots" not in text
    assert [c["status"] for c in card["checks"][1:]] == ["skip"] * 3


def test_an_entry_outside_our_assignment_fails_valid_plan(conn, page_conn, opened):
    week_of_sales(conn, opened[0])
    plan = plan_for(opened)
    plan["slots"].append({"slot": "99", "keep": True, "reason": "not ours"})
    live_run(conn, datetime(2026, 12, 3, 20, tzinfo=UTC), plan=plan)
    snap = dashboard.snapshot(page_conn, NOW, public=True)
    card, text = _week_text(snap, "2026-12-05")
    assert card["checks"][0] == {"name": "valid plan", "status": "fail",
                                 "detail": "The plan names a selection outside our assignment."}
    assert card["grade"] == "fail" and "99" not in dashboard.to_json(card)


def test_a_changed_slot_with_no_reason_fails_valid_plan(conn, page_conn, opened):
    week_of_sales(conn, opened[0])
    live_run(conn, datetime(2026, 12, 3, 20, tzinfo=UTC),
             plan=plan_for(opened, **{opened[0]: {"fill_to": 8, "sale_price": "2.50"}}))
    _, by_week = cards(page_conn)
    valid = by_week["2026-12-05"]["checks"][0]
    assert valid["status"] == "fail" and "Every slot needs a short reason" in valid["detail"]


def test_replay_errors_are_not_quoted_and_one_week_s_failure_spares_the_rest(conn, page_conn, opened, monkeypatch):
    from vending.replay.state import NoData
    real = report_card.replay_week

    def replay(conn, day, until=None):
        if day.isoformat() == "2026-11-28":
            raise NoData(f"Week of {day}: {SECRET}")
        if day.isoformat() == "2026-12-12":
            raise RuntimeError(SECRET)
        return real(conn, day, until)

    monkeypatch.setattr(report_card, "replay_week", replay)
    week_of_sales(conn, opened[0])
    snap = dashboard.snapshot(page_conn, NOW, public=True)
    assert snap["report_cards"]["ok"]
    weeks = {c["week"]: c for c in snap["report_cards"]["data"]["weeks"]}
    assert list(weeks) == ["2026-12-19", "2026-12-12", "2026-12-05", "2026-11-28"]
    assert "Last week can't be rebuilt from the polls." in weeks["2026-12-05"]["notes"]
    assert weeks["2026-12-19"]["grade"] == "unscored"
    assert any(n.startswith("This week couldn't be scored") for n in weeks["2026-12-19"]["notes"])
    assert SECRET not in dashboard.to_json(snap) and SECRET not in dashboard.render(snap)


def receipt(conn, request_id, at, lines, *, status="fulfilled", off_cycle=False, paid="0"):
    """A purchase request and its receipt lines: (slot, sku, units_loaded, paid, pulled, pulled_sku)."""
    conn.execute("insert into purchase_requests (request_id, submitted_at, off_cycle, request_body, status,"
                 " total_paid, fulfilled_at) values (%s, %s, %s, '{}', %s, %s, %s)",
                 (request_id, at - timedelta(days=1), off_cycle, status, Decimal(paid),
                  at if status == "fulfilled" else None))
    for n, (slot, sku, loaded, cost, pulled, pulled_sku) in enumerate(lines):
        conn.execute(
            "insert into purchase_lines (request_id, line_no, slot, source, asked, outcome, sku, store, packs_bought,"
            " units_per_pack, units_bought, total_paid, unit_cost_paid, units_loaded, units_lost,"
            " units_to_back_stock, units_from_back_stock, units_pulled, pulled_sku, slot_reassigned, resolved_at)"
            " values (%s, %s, %s, 'purchase', '{}', 'primary', %s, 'Walmart', 1, %s, %s, %s, null, %s, 0, 0, 0,"
            " %s, %s, false, %s)",
            (request_id, n, slot, sku, loaded, loaded, None if cost is None else Decimal(cost), loaded, pulled,
             pulled_sku, at))


def two_slot_sales(conn, a, b):
    """Slot `a` sells 2 in the opening week and 6 in week 1; slot `b` sells 1 in each, at $2.00."""
    for name, at, (ua, ub) in (("w0-start", OPEN + timedelta(hours=2), (100, 50)),
                               ("w0-end", SAT1 - timedelta(hours=1), (102, 51)),
                               ("w1-start", SAT1 + timedelta(minutes=30), (102, 51)),
                               ("w1-end", SAT2 - timedelta(hours=1), (108, 52)),
                               ("w2", NOW - timedelta(hours=1), (109, 53))):
        store_poll(conn, name, at.isoformat(), {a: (250, ua), b: (200, ub)})


def test_a_fulfilled_product_swap_is_left_out_of_the_comparison(conn, page_conn, opened):
    a, b = opened[0], opened[1]
    product(conn, "OLD-CAN")
    product(conn, "NEW-CAN")
    product(conn, "KEPT-CAN")
    # The first fill, after the opening: 4 of OLD-CAN in a, 10 of KEPT-CAN in b.
    receipt(conn, "fill", OPEN + timedelta(hours=1), [(a, "OLD-CAN", 4, "2.00", 0, None),
                                                      (b, "KEPT-CAN", 10, "10.00", 0, None)])
    two_slot_sales(conn, a, b)
    live_run(conn, datetime(2026, 12, 3, 20, tzinfo=UTC), request_id="friday", plan=plan_for(opened, **{
        a: {"product": "NEW-CAN", "fill_to": 8, "sale_price": "2.50", "reason": "swap it"}}))
    # The Friday visit pulls the 2 OLD-CAN left and loads 8 NEW-CAN.
    receipt(conn, "friday", SAT1 - timedelta(hours=3), [(a, "NEW-CAN", 8, "8.00", 2, "OLD-CAN")])
    snap, by_week = cards(page_conn)
    week = by_week["2026-12-05"]
    cmp = week["comparison"]
    # Slot a's sales this week are NEW-CAN's, which the baseline (keep OLD-CAN) never stocked.
    assert cmp["slots"] == 1 and cmp["result"] == "tied" and cmp["excluded"]["swapped"] == [a]
    assert week["grade"] == "tied"
    assert any(f"Slot {a}" in n and "swapped" in n for n in week["notes"])
    page = dashboard.render(snap)
    assert "ahead by" not in page and "on 1 shared slot:" in page
    assert f"Not in the comparison: slot {a} (swapped at the visit)." in page


def test_the_table_and_comparison_say_which_slots_they_cover(conn, page_conn, opened):
    slot = opened[0]
    product(conn)
    week_of_sales(conn, slot)
    live_run(conn, datetime(2026, 12, 3, 20, tzinfo=UTC), request_id="r1",
             plan=plan_for(opened, **{slot: {"fill_to": 8, "sale_price": "2.50", "reason": "fill it"}}))
    fulfilled(conn, "r1", slot, SKU, units_loaded=8, paid="8.00", at=SAT1 - timedelta(hours=3))
    page = dashboard.render(cards(page_conn)[0])
    assert ">All scored slots</th>" in page and "on 1 shared slot: ahead by $5.26" in page


def test_an_unknown_cost_on_a_shared_slot_makes_the_comparison_unavailable(conn, page_conn, opened):
    slot = opened[0]
    product(conn)
    week_of_sales(conn, slot)
    live_run(conn, datetime(2026, 12, 3, 20, tzinfo=UTC), request_id="r1",
             plan=plan_for(opened, **{slot: {"fill_to": 8, "sale_price": "2.50", "reason": "fill it"}}))
    receipt(conn, "r1", SAT1 - timedelta(hours=3), [(slot, SKU, 8, None, 0, None)])
    snap, by_week = cards(page_conn)
    week = by_week["2026-12-05"]
    assert week["comparison"]["result"] is None and week["comparison"]["excluded"]["unknown_cost"] == [slot]
    assert week["grade"] == "unscored"
    assert f"Against the baseline: unavailable (unit cost unknown on shared slot {slot})." in dashboard.render(snap)


def pending(conn, request_id, at, *, status="pending", off_cycle=False):
    conn.execute("insert into purchase_requests (request_id, submitted_at, off_cycle, request_body, status)"
                 " values (%s, %s, %s, '{}', %s)", (request_id, at, off_cycle, status))


def test_the_week_s_plan_is_the_one_its_visit_loaded(conn, page_conn, opened):
    slot = opened[0]
    product(conn)
    week_of_sales(conn, slot)
    loaded = live_run(conn, datetime(2026, 12, 3, 20, tzinfo=UTC), request_id="fri1",
                      plan=plan_for(opened, **{slot: {"fill_to": 8, "sale_price": "2.50", "reason": "fill it"}}))
    fulfilled(conn, "fri1", slot, SKU, units_loaded=8, paid="8.00", at=SAT1 - timedelta(hours=3))
    # Friday 13:00 Eastern, after Thursday's cutoff: this request is for the next Friday's visit.
    later = live_run(conn, datetime(2026, 12, 4, 18, tzinfo=UTC), request_id="fri2",
                     plan=plan_for(opened, **{slot: {"fill_to": 3, "sale_price": "2.50", "reason": "less"}}))
    pending(conn, "fri2", datetime(2026, 12, 4, 18, 1, tzinfo=UTC))
    snap, by_week = cards(page_conn)
    week = by_week["2026-12-05"]
    assert (week["run_id"], week["request"]) == (loaded, "fulfilled")
    assert week["outcome"]["sold"] == 6 and week["grade"] == "beat"
    after = by_week["2026-12-12"]
    assert (after["run_id"], after["request"]) == (later, "submitted")
    assert any("no receipt yet" in n for n in after["notes"])
    assert "request submitted" in dashboard.render(snap)


def test_a_paid_midweek_visit_supersedes_the_friday_request(conn, page_conn, opened):
    slot = opened[0]
    product(conn)
    week_of_sales(conn, slot)
    friday = live_run(conn, datetime(2026, 12, 3, 20, tzinfo=UTC), request_id="fri",
                      plan=plan_for(opened, **{slot: {"fill_to": 3, "sale_price": "2.50", "reason": "few"}}))
    pending(conn, "fri", datetime(2026, 12, 3, 20, 1, tzinfo=UTC))
    plan = plan_for(opened, **{slot: {"fill_to": 8, "sale_price": "2.50", "reason": "many"}})
    plan["off_cycle"], plan["visit"] = True, "midweek"
    # A later run on Thursday sends a paid midweek visit, fulfilled on Friday morning.
    midweek = live_run(conn, datetime(2026, 12, 3, 22, tzinfo=UTC), request_id="mid", plan=plan)
    receipt(conn, "mid", datetime(2026, 12, 4, 15, tzinfo=UTC), [(slot, SKU, 8, "8.00", 0, None)], off_cycle=True)
    _, by_week = cards(page_conn)
    week = by_week["2026-12-05"]
    assert (week["run_id"], week["request"]) == (midweek, "fulfilled")
    assert any(f"run {friday}" in n and "superseded" in n for n in week["notes"])


def test_a_rejected_request_loaded_nothing(conn, page_conn, opened):
    slot = opened[0]
    week_of_sales(conn, slot)
    run = live_run(conn, datetime(2026, 12, 3, 20, tzinfo=UTC), request_id="no",
                   plan=plan_for(opened, **{slot: {"fill_to": 8, "sale_price": "2.50", "reason": "fill it"}}))
    pending(conn, "no", datetime(2026, 12, 3, 20, 1, tzinfo=UTC), status="rejected")
    snap, by_week = cards(page_conn)
    week = by_week["2026-12-05"]
    assert (week["run_id"], week["request"], week["grade"]) == (run, "rejected", "rejected")
    assert week["outcome"] is None and week["checks"][0]["status"] == "pass"
    assert any("rejected" in n for n in week["notes"])
    assert "Request rejected" in dashboard.render(snap)


def test_no_matched_plan_says_so_without_claiming_nothing_was_restocked(conn, page_conn, opened):
    week_of_sales(conn, opened[0])
    snap, by_week = cards(page_conn)
    week = by_week["2026-12-05"]
    assert week["grade"] == "no_plan" and week["grade_text"] == "No plan matched"
    assert week["checks"] == [] and week["outcome"] is None
    assert not any("nothing was restocked" in n for n in week["notes"])
    assert any(n.startswith("No submitted live plan matched") for n in week["notes"])


def test_the_current_week_reads_no_poll_after_now(conn, page_conn, opened):
    slot = opened[0]
    product(conn)
    week_of_sales(conn, slot)
    store_poll(conn, "w1-day1", (SAT1 + timedelta(days=1)).isoformat(), {slot: (250, 104)})
    live_run(conn, datetime(2026, 12, 3, 20, tzinfo=UTC), request_id="r1",
             plan=plan_for(opened, **{slot: {"fill_to": 8, "sale_price": "2.50", "reason": "fill it"}}))
    fulfilled(conn, "r1", slot, SKU, units_loaded=8, paid="8.00", at=SAT1 - timedelta(hours=3))
    now = SAT1 + timedelta(days=2)
    snap, by_week = cards(page_conn, now)
    week = by_week["2026-12-05"]
    assert not week["final"] and week["outcome"]["sold"] == 2   # 102 to 104, not the later 108
    assert week["polls"]["this"] == [SAT1 - timedelta(hours=1), SAT1 + timedelta(days=1)]
    assert "Polls covered" in dashboard.render(snap)


def test_a_closed_week_with_thin_polls_shows_figures_but_no_grade(conn, page_conn, opened):
    slot = opened[0]
    product(conn)
    for name, at, units in (("w0-start", SAT1 - timedelta(days=7) + timedelta(hours=1), 100),
                            ("w0-end", SAT1 - timedelta(hours=1), 102),
                            ("w1-mid", SAT1 + timedelta(days=3), 105),
                            ("w3", NOW - timedelta(hours=1), 112)):
        store_poll(conn, name, at.isoformat(), {slot: (250, units)})
    live_run(conn, datetime(2026, 12, 3, 20, tzinfo=UTC), request_id="r1",
             plan=plan_for(opened, **{slot: {"fill_to": 8, "sale_price": "2.50", "reason": "fill it"}}))
    fulfilled(conn, "r1", slot, SKU, units_loaded=8, paid="8.00", at=SAT1 - timedelta(hours=3))
    snap, by_week = cards(page_conn)
    week = by_week["2026-12-05"]
    assert week["final"] and week["outcome"]["sold"] == 3 and week["comparison"]["result"] == "beat"
    assert week["grade"] == "incomplete" and week["grade_text"] == "Incomplete data: not graded"
    assert any("3.0 days" in n for n in week["notes"])
    assert "Incomplete data" in dashboard.render(snap)


def test_without_polls_the_week_is_not_scored(conn, page_conn, opened):
    _, by_week = cards(page_conn)
    week = by_week["2026-12-05"]
    assert week["grade"] == "no_plan" and week["outcome"] is None
    assert week["notes"][0].startswith("Last week can't be rebuilt from the polls")


def test_hard_checks_run_without_last_week_s_polls(conn, page_conn, opened):
    # No polls at all, so neither week can be replayed: the plan is still checked, and fails.
    live_run(conn, datetime(2026, 12, 3, 20, tzinfo=UTC), plan={"note": "not a plan"})
    _, by_week = cards(page_conn)
    week = by_week["2026-12-05"]
    assert [c["name"] for c in week["checks"]] == ["valid plan", "catalog", "prices", "balance"]
    assert week["checks"][0]["status"] == "fail" and week["grade"] == "fail"
    assert week["outcome"] is None
    assert any(n.startswith("Last week can't be rebuilt from the polls") for n in week["notes"])


def test_a_price_over_the_ceiling_fails_when_the_column_capacity_is_unknown(conn, page_conn, opened):
    slot = opened[0]
    product(conn, size="odd crate")     # no capacity on the loading chart
    week_of_sales(conn, slot)
    live_run(conn, datetime(2026, 12, 3, 20, tzinfo=UTC),
             plan=plan_for(opened, **{slot: {"sale_price": "9.00", "reason": "fill the column"}}), request_id="r1")
    fulfilled(conn, "r1", slot, SKU, units_loaded=8, paid="8.00", at=SAT1 - timedelta(hours=3))
    _, by_week = cards(page_conn)
    week = by_week["2026-12-05"]
    checks = {c["name"]: c for c in week["checks"]}
    assert checks["valid plan"]["status"] == "pass"
    assert checks["prices"]["status"] == "fail" and "$6.50" in checks["prices"]["detail"]
    assert week["grade"] == "fail"
    # Only the fill is unknown: that slot isn't scored, and the note says why.
    assert any(f"Slot {slot}" in n and "capacity isn't known" in n for n in week["notes"])


def test_balance_fails_when_the_known_spend_alone_is_over_the_funds():
    state = WeekState(start=OPEN, end=OPEN, balance_cents=500, slots=(
        SlotWeek("15", 250, 1, product="A", stock=0, unit_cost_cents=100),
        SlotWeek("16", 250, 1, product="B", stock=0, unit_cost_cents=None)))
    plan = Plan((SlotPlan("15", "r", fill_to=10, price_cents=250), SlotPlan("16", "r", fill_to=3, price_cents=250)))
    check = scoring.check_balance(state, plan)
    assert check.status == "fail" and "at least $10.00" in check.detail and "$5.00" in check.detail
    # Under the funds, the unknown cost still leaves it unchecked.
    plan = Plan((SlotPlan("15", "r", fill_to=4, price_cents=250), SlotPlan("16", "r", fill_to=3, price_cents=250)))
    assert scoring.check_balance(state, plan).status == "skip"


def test_a_dry_run_or_unsubmitted_plan_is_not_the_week_s_plan(conn, page_conn, opened):
    week_of_sales(conn, opened[0])
    plan = plan_for(opened, **{opened[0]: {"fill_to": 8, "sale_price": "2.50", "reason": "x"}})
    live_run(conn, datetime(2026, 12, 3, 20, tzinfo=UTC), status="skipped", plan=plan)
    dry = agent_runs.start(conn, mode="dry-run")
    agent_runs.finish(conn, dry, "submitted", plan=plan)
    conn.execute("update agent_runs set started_at = %s where id = %s", (datetime(2026, 12, 3, 21, tzinfo=UTC), dry))
    _, by_week = cards(page_conn)
    assert by_week["2026-12-05"]["grade"] == "no_plan"


def test_a_midweek_plan_reads_as_a_midweek_visit():
    plan, notes, unknown_fill = report_card._as_replay_plan(
        {"off_cycle": True, "slots": [{"slot": "15", "fill_to": 4, "sale_price": Decimal("2.50")}]},
        type("R", (), {"ours": ("15",), "capacity_of": lambda self, sku: None})(),
        WeekState(start=OPEN, end=OPEN, slots=(SlotWeek("15", 250, 1),)))
    # A changed slot gets no default reason: the agent writes one for every slot it changes.
    assert plan == {"visit": "midweek", "reason": "", "slots": [{"slot": "15", "fill_to": 4, "sale_price": "2.50"}]}
    assert notes == [] and unknown_fill == frozenset()


def test_the_replayer_s_price_check_takes_a_slot_s_own_ceiling():
    state = WeekState(start=OPEN, end=OPEN, slots=(SlotWeek("15", 250, 1, unit_cost_cents=100),),
                      ceilings=(("15", 800),))
    plan = Plan((SlotPlan("15", "r", fill_to=3, price_cents=750),))
    assert scoring.check_prices(state, plan).status == "pass"
    assert scoring.check_prices(WeekState(start=OPEN, end=OPEN, slots=state.slots), plan).status == "fail"


# --- Incident log -------------------------------------------------------------------------------

def test_incidents_since_the_opening_with_no_error_text(conn, page_conn, opened):
    log = "insert into action_log (at, kind, tool, arguments, outcome, detail) values (%s, %s, %s, %s, %s, %s)"
    conn.execute(log, (OPEN - timedelta(hours=1), "write", "submit_desired_state", Jsonb({}), "refused",
                       "before the opening"))
    conn.execute(log, (OPEN + timedelta(hours=1), "read", "get_pnl", Jsonb({}), "failed", SECRET))
    conn.execute(log, (OPEN + timedelta(hours=2), "write", "submit_desired_state", Jsonb({}), "refused",
                       "A request for this Friday is already pending.\nSecond line."))
    config.set_halted(conn, True, "kevin")
    conn.execute("update action_log set at = %s where tool = 'halt'", (OPEN + timedelta(hours=3),))
    failed = live_run(conn, OPEN + timedelta(hours=4), status="failed", detail=SECRET)
    halted = live_run(conn, OPEN + timedelta(hours=5), status="halted", detail="The halt switch is on.")
    stepped = live_run(conn, OPEN + timedelta(hours=6), status="skipped")
    stuck = live_run(conn, NOW - timedelta(hours=3), status="running")
    for run in (failed, stepped):
        agent_runs.step(conn, run, 1, "tool_result", SECRET, tool="get_pnl", is_error=True)

    for public in (True, False):
        snap = dashboard.snapshot(page_conn, NOW, public=public)
        data = snap["incidents"]["data"]
        kinds = [(r["kind"], r["what"]) for r in data["rows"]]
        assert kinds == [("run_stuck", f"Run {stuck}"), ("tool_errors", f"Run {stepped}"),
                         ("run_halted", f"Run {halted}"), ("run_failed", f"Run {failed}"),
                         ("halt_on", "Halt switch"), ("refused", "submit_desired_state"), ("failed", "get_pnl")]
        notes = {r["kind"]: r["note"] for r in data["rows"]}
        assert notes["refused"] == "A request for this Friday is already pending."
        assert notes["failed"] == notes["run_failed"] == dashboard.HIDDEN
        assert notes["halt_on"] == "halt turned on by kevin"
        assert notes["tool_errors"].startswith("The model was told 1 tool call failed")
        assert all(r["week"] == "2026-11-28" for r in data["rows"][1:])
        assert SECRET not in dashboard.to_json(snap["incidents"]) and SECRET not in dashboard.render(snap)
        assert "before the opening" not in dashboard.to_json(snap["incidents"])


def test_incident_notes_from_before_the_assignment_are_omitted(conn, page_conn, opened):
    conn.execute("update slot_assignment set first_read_at = %s", (OPEN + timedelta(hours=2),))
    conn.execute("insert into action_log (at, kind, tool, arguments, outcome, detail)"
                 " values (%s, 'write', 'submit_desired_state', '{}', 'refused', 'old words')",
                 (OPEN + timedelta(hours=1),))
    row, = dashboard.snapshot(page_conn, NOW)["incidents"]["data"]["rows"]
    assert row["note"] == dashboard.OLD_ASSIGNMENT


def test_the_incident_total_is_counted_and_duplicates_go_before_the_limit(conn, page_conn, opened):
    for n in range(60):
        conn.execute("insert into action_log (at, kind, tool, arguments, outcome, detail)"
                     " values (%s, 'write', 'submit_desired_state', '{}', 'refused', 'no')",
                     (OPEN + timedelta(minutes=n),))
    failed = live_run(conn, NOW - timedelta(hours=5), status="failed", detail=SECRET)
    stepped = live_run(conn, NOW - timedelta(hours=4), status="skipped")
    for run in (failed, stepped):
        agent_runs.step(conn, run, 1, "tool_result", SECRET, tool="get_pnl", is_error=True)
    data = dashboard.snapshot(page_conn, NOW)["incidents"]["data"]
    # 60 refusals, the failed run and the other run's tool errors; the failed run's errors are one incident.
    assert data["total"] == 62 and len(data["rows"]) == report_card.INCIDENTS_SHOWN
    assert [r["kind"] for r in data["rows"][:2]] == ["tool_errors", "run_failed"]
    assert sum(r["what"] == f"Run {failed}" for r in data["rows"]) == 1
    assert "Latest 50 of 62." in dashboard.render(dashboard.snapshot(page_conn, NOW))


def test_no_incidents_says_so(conn, page_conn, opened):
    snap = dashboard.snapshot(page_conn, NOW)
    assert snap["incidents"]["data"] == {"opened": True, "total": 0, "rows": []}
    assert "No incidents since the opening." in dashboard.render(snap)


def test_report_card_text_naming_a_selection_outside_ours_is_redacted():
    ours = {"15", "16"}
    panel = {"ok": True, "data": {"weeks": [{"notes": ["Slot 15 was kept.", "Slot 23 was to be filled.",
                                                       "All 20 slots once, every field well formed."]}]}}
    notes = dashboard._scope_numbered(panel, ours)["data"]["weeks"][0]["notes"]
    assert notes == ["Slot 15 was kept.", dashboard.OUTSIDE_ASSIGNMENT, "All 20 slots once, every field well formed."]


# --- Incident log: sync rounds and tracking checks -----------------------------------------------

JOBS = [{"job": "sync_history", "exit_code": 3, "outcome": "finished"}]


class _OneConn:
    """`vending.db.connect` stood in for: the worker's one write, on the test schema's connection."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        return False


def _round(at: datetime, jobs=JOBS):
    return (at.isoformat(), jobs)


def test_a_failed_sync_round_and_an_outside_tracking_check_are_incidents(conn, page_conn, opened):
    from tests.test_dashboard import _syncs
    checked = OPEN + timedelta(hours=2)
    conn.execute(
        "insert into tracking_checks (sent_at, eastern_day, sent_by, as_of, as_of_time, request,"
        " answered_at, status, answer, used_check, results)"
        " values (%s, '2026-12-02', 'test', 's1', %s, '{}', %s, 200, %s, true, %s)",
        (checked, checked, checked + timedelta(minutes=5), Jsonb({"error": SECRET}),
         Jsonb({"cash": "outside", "sales_tax_owed": "within", "coil": "within", "back_stock": "within"})))
    assert worker.record_rounds([_round(OPEN + timedelta(hours=4))], conn)
    snap = dashboard.snapshot(page_conn, NOW)
    rows = snap["incidents"]["data"]["rows"]
    kinds = [r["kind"] for r in rows]
    assert "sync_failed" in kinds and "tracking_outside" in kinds
    sync_row = next(r for r in rows if r["kind"] == "sync_failed")
    assert sync_row["what"] == "Sync round" and "`sync_history` exit 3" in sync_row["note"]
    tracking_row = next(r for r in rows if r["kind"] == "tracking_outside")
    assert "cash" in tracking_row["note"] and SECRET not in tracking_row["note"]
    assert SECRET not in dashboard.to_json(snap["incidents"]) and SECRET not in dashboard.render(snap)


def test_each_failed_round_stays_an_incident(conn, page_conn, opened):
    """One sanitized event per failed round (`sync_incidents`, migration 026): a later round that
    passes leaves the earlier rows in the log, and the worker's latest round (memory) decides only
    the banner."""
    first, second = OPEN + timedelta(hours=1), OPEN + timedelta(hours=2)
    assert worker.record_rounds([_round(first), _round(second)], conn)
    rows = [r for r in dashboard.snapshot(page_conn, NOW)["incidents"]["data"]["rows"]
            if r["kind"] == "sync_failed"]
    assert [r["at"] for r in rows] == [second, first]
    assert all("`sync_history` exit 3" in r["note"] for r in rows)
    assert dashboard.snapshot(page_conn, NOW)["incidents"]["data"]["total"] == 2
    # The banner says only what the worker's latest round says, so a passing round clears it
    # without touching the log.
    from tests.test_dashboard import _syncs
    syncs = _syncs(("sync_history", {"exit_code": 0}), at=OPEN + timedelta(hours=3))
    snap = dashboard.snapshot(page_conn, NOW, syncs=syncs)
    assert snap["alerts"] == [] and snap["incidents"]["data"]["total"] == 2


def test_a_recorded_round_is_never_written_twice(conn, page_conn, opened):
    assert worker.record_rounds([_round(OPEN + timedelta(hours=1))], conn)
    assert worker.record_rounds([_round(OPEN + timedelta(hours=1))], conn)   # a retried write is a no-op
    assert conn.execute("select count(*) from sync_incidents").fetchone()[0] == 1


def test_a_round_the_database_missed_is_recorded_after_a_later_round(monkeypatch, conn, page_conn, opened):
    """The database is down for a failed round (it was down for its jobs too): the round waits and
    a later round writes it, so the outage's failures are not lost."""
    from tests.test_dashboard import SECRET
    import vending.db as db_mod
    waited: list = []

    def down(**k):
        raise psycopg.OperationalError(SECRET)

    monkeypatch.setattr(db_mod, "connect", down)
    state = worker.State()
    state.results = worker.run_round([("sync_history", lambda: 3)], log=lambda _: None)
    state.rounds, state.last_round_at = 1, (OPEN + timedelta(hours=1)).isoformat()
    writer = worker.record_failed_round(state, waited, log=lambda _: None)
    writer.join(60)
    assert len(waited) == 1 and waited[0][1][0]["exit_code"] == 3
    assert page_conn.execute("select count(*) from sync_incidents").fetchone()[0] == 0
    monkeypatch.setattr(db_mod, "connect", lambda **k: _OneConn(conn))
    state.results = worker.run_round([("sync_history", lambda: 0)], log=lambda _: None)
    state.rounds, state.last_round_at = 2, (OPEN + timedelta(hours=2)).isoformat()
    writer = worker.record_failed_round(state, waited, log=print)
    writer.join(60)
    assert waited == []
    row, = page_conn.execute("select round_at, jobs from sync_incidents").fetchall()
    assert row[0] == OPEN + timedelta(hours=1) and row[1][0]["job"] == "sync_history"


def test_a_sync_round_from_before_the_opening_is_not_an_incident(conn, page_conn, opened):
    from tests.test_dashboard import _syncs
    assert worker.record_rounds([_round(OPEN - timedelta(hours=1))], conn)
    snap = dashboard.snapshot(page_conn, NOW,
                              syncs=_syncs(("sync_history", {"exit_code": 1}), at=OPEN - timedelta(hours=1)))
    assert not [r for r in snap["incidents"]["data"]["rows"] if r["kind"] == "sync_failed"]
    # The banner is current state, so it still says the latest round failed; the log starts at the opening.
    assert [a["kind"] for a in snap["alerts"]] == ["sync_failed"]


def test_the_log_survives_a_missing_optional_table(conn, page_conn, opened):
    """A database without migration 026 has no `sync_incidents`, and one without 021 no
    `tracking_checks`: either source counts as no incidents and the panel's other incidents stay.
    Each optional read runs in its own savepoint (`_error_steps`' pattern), so one missing table
    cannot abort the panel's transaction and hide every incident."""
    conn.execute("insert into action_log (at, kind, tool, arguments, outcome, detail)"
                 " values (%s, 'write', 'submit_desired_state', '{}', 'refused', 'no')",
                 (OPEN + timedelta(hours=1),))
    assert worker.record_rounds([_round(OPEN + timedelta(hours=2))], conn)
    conn.execute("drop table sync_incidents")          # a database without migration 026
    data = dashboard.snapshot(page_conn, NOW)["incidents"]["data"]
    assert data["total"] == 1 and [r["kind"] for r in data["rows"]] == ["refused"]
    conn.execute("drop table tracking_checks")         # a database without migration 021
    data = dashboard.snapshot(page_conn, NOW)["incidents"]["data"]
    assert data["total"] == 1 and [r["kind"] for r in data["rows"]] == ["refused"]


def test_a_locked_sync_incidents_table_cannot_stall_the_round(monkeypatch, conn, page_conn, schema_url, opened):
    """An exclusive lock on `sync_incidents` (a backup, a migration) must not hang the sync: the
    write runs off the scheduling path, its writer gives up at `worker.WRITE_TIMEOUT`, and the
    round stays in the pending list for a later round to write (`record_failed_round`)."""
    import vending.db as db_mod
    url, schema = schema_url
    holder = psycopg.connect(url, connect_timeout=3,
                             options=db_mod.session_options(url, f"-c search_path={schema}"))
    try:
        holder.execute("lock table sync_incidents in access exclusive mode")
        monkeypatch.setattr(db_mod, "connect", lambda **k: _OneConn(conn))
        monkeypatch.setattr(worker, "WRITE_TIMEOUT", "300ms")
        state = worker.State()
        state.results = worker.run_round([("sync_history", lambda: 3)], log=lambda _: None)
        state.rounds, state.last_round_at = 1, (OPEN + timedelta(hours=1)).isoformat()
        waited: list = []
        lines: list[str] = []
        writer = worker.record_failed_round(state, waited, log=lines.append)
        assert writer is not None, "the round loop started the write in the background"
        writer.join(60)
        assert not writer.is_alive(), "the incident write stalled on the lock"
        assert waited and waited[0][1][0]["exit_code"] == 3, "the round stayed in the pending list"
        assert any("57014" in line for line in lines), lines   # the write gave up on its timeout
        holder.rollback()                                      # the lock is gone
        assert page_conn.execute("select count(*) from sync_incidents").fetchone()[0] == 0
        # A later round writes the waited one, as it would after any database refusal.
        state.results = worker.run_round([("sync_history", lambda: 0)], log=lambda _: None)
        state.rounds, state.last_round_at = 2, (OPEN + timedelta(hours=2)).isoformat()
        writer = worker.record_failed_round(state, waited, log=lambda _: None)
        writer.join(60)
        assert waited == []
        assert page_conn.execute("select count(*) from sync_incidents").fetchone()[0] == 1
    finally:
        holder.rollback()
        holder.close()


def test_a_silent_database_cannot_stall_the_round_loop(monkeypatch):
    """A connection that goes silent after login blocks every write on it, and no server-side
    timeout can fire: the query never reaches a server. The incident write runs off the
    scheduling path, on a connection bounded at the TCP layer (`worker.INCIDENT_TCP`), so the
    round loop still schedules its next round while the writer waits."""
    import vending.db as db_mod

    gate = threading.Event()
    asked: dict = {}

    class Silent:
        """A connection whose login succeeds and whose first execute never answers."""

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def transaction(self):
            return self

        def execute(self, *a, **k):
            if not gate.is_set():
                gate.wait()                     # silent: no answer ever comes
            return None

    def connect(**options):
        asked.update(options)
        return Silent()

    monkeypatch.setattr(db_mod, "connect", connect)
    monkeypatch.setattr(worker, "_slot_lock", lambda: None)
    lines: list[str] = []
    outcome: dict = {}

    def sleep_until_next_round(seconds):
        gate.set()                              # the loop got past the stuck write...
        raise worker.Stopped()                  # ...to its next round: stop there

    def run():
        outcome["code"] = worker.main(["--every", "60", "--at-minute", "off", "--port", "0"],
                                     jobs=[("sync_history", lambda: 3)],
                                     sleep=sleep_until_next_round, log=lines.append)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(60)
    assert not thread.is_alive(), "the round loop stalled on the incident write"
    assert outcome.get("code") == 0
    assert any("next round at" in line for line in lines), lines   # it scheduled the next round
    assert not any("sync_incidents:" in line for line in lines), lines   # and logged no failure
    # The writer was asked to connect with the TCP-level bounds, and it finished once the gate opened.
    assert asked.get("keepalives") == 1 and "tcp_user_timeout" in asked, asked
    with worker._INCIDENT_LOCK:
        writer = worker._WRITER
    writer.join(60)
    assert not writer.is_alive()


def test_a_check_is_windowed_by_when_it_was_answered(conn, page_conn, opened):
    """The banner and the log use the same event time (`answered_at`, falling back to `sent_at`):
    a check sent before the opening but answered after it is a competition incident in both, and
    the log's row carries the moment it was answered."""
    sent = OPEN - timedelta(hours=2)
    answered = OPEN + timedelta(hours=1)
    conn.execute(
        "insert into tracking_checks (sent_at, eastern_day, sent_by, as_of, as_of_time, request,"
        " answered_at, status, answer, used_check, results)"
        " values (%s, '2026-12-01', 'test', 's0', %s, '{}', %s, 200, '{}', true, %s)",
        (sent, sent, answered,
         Jsonb({"cash": "outside", "sales_tax_owed": "within", "coil": "within", "back_stock": "within"})))
    snap = dashboard.snapshot(page_conn, NOW)
    [alert] = snap["alerts"]
    assert alert["kind"] == "tracking_outside" and alert["at"] == answered
    row, = [r for r in snap["incidents"]["data"]["rows"] if r["kind"] == "tracking_outside"]
    assert row["at"] == answered and "cash" in row["note"]
    assert snap["incidents"]["data"]["total"] == 1
