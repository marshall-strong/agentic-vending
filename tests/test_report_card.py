"""The weekly report card and the incident log (#48): `vending.report_card` on the dashboard."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from psycopg.types.json import Jsonb

from tests.conftest import store_poll
from tests.test_dashboard import fulfilled, page_conn  # noqa: F401
from vending import agent_runs, config, dashboard, ledger, report_card, slots
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


def live_run(conn, at, status="submitted", plan=None, detail=None):
    run = agent_runs.start(conn, mode="live", model="claude-sonnet-5-5")
    if status != "running":
        agent_runs.finish(conn, run, status, plan=plan, detail=detail)
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
                   plan=plan_for(opened, **{slot: {"fill_to": 8, "sale_price": "2.50", "reason": "fill it"}}))
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

    # No run submitted a plan in week 1, so week 2 restocked nothing.
    assert by_week["2026-12-12"]["grade"] == "no_plan"
    current = by_week["2026-12-19"]
    assert not current["final"] and any(n.startswith("In progress") for n in current["notes"])

    page = dashboard.render(snap)
    assert page.index('id="plan"') < page.index('id="report-card"') < page.index('id="incidents"') < page.index('id="runs"')
    assert "Beat the baseline" in page and "ahead by $5.26" in page and "Opening week: not graded" in page
    assert '"report_cards"' in dashboard.to_json(snap)


def test_a_failed_hard_check_fails_the_week_whatever_it_earned(conn, page_conn, opened):
    slot = opened[0]
    product(conn)
    week_of_sales(conn, slot, price=700)
    live_run(conn, datetime(2026, 12, 3, 20, tzinfo=UTC),
             plan=plan_for(opened, **{slot: {"fill_to": 8, "sale_price": "7.00", "reason": "dear"}}))
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
             plan=plan_for(opened, **{slot: {"fill_to": 8, "sale_price": "7.00", "reason": "wildcard"}}))
    fulfilled(conn, "r1", slot, SKU, units_loaded=8, paid="8.00", at=SAT1 - timedelta(hours=3))
    _, by_week = cards(page_conn)
    prices = next(c for c in by_week["2026-12-05"]["checks"] if c["name"] == "prices")
    assert prices["status"] == "pass" and f"$8.00 for slot {slot}" in prices["detail"]


def test_fill_to_the_column_uses_the_package_capacity(conn, page_conn, opened):
    slot = opened[0]
    product(conn)   # a 12 oz can: 8 a column on the loading chart
    week_of_sales(conn, slot)
    live_run(conn, datetime(2026, 12, 3, 20, tzinfo=UTC),
             plan=plan_for(opened, **{slot: {"sale_price": "2.50", "reason": "fill the column"}}))
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

    def replay(conn, day):
        if day.isoformat() == "2026-11-28":
            raise NoData(f"Week of {day}: {SECRET}")
        if day.isoformat() == "2026-12-12":
            raise RuntimeError(SECRET)
        return real(conn, day)

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
            (request_id, n, slot, sku, loaded, loaded, Decimal(cost), loaded, pulled, pulled_sku, at))


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
    live_run(conn, datetime(2026, 12, 3, 20, tzinfo=UTC), plan=plan_for(opened, **{
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
    assert "ahead by" not in dashboard.render(snap)


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
             plan=plan_for(opened, **{slot: {"sale_price": "9.00", "reason": "fill the column"}}))
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
