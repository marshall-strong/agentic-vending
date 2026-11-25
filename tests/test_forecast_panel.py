"""The forecast panel: what the agent expected each slot to sell against what sold (`vending.forecast_panel`)."""

from datetime import datetime, timedelta, timezone
from fractions import Fraction

from tests.conftest import store_poll
from tests.test_dashboard import fulfilled, page_conn  # noqa: F401
from tests.test_report_card import (NOW, OPEN, SAT1, SAT2, SKU, cards, live_run, opened, plan_for,  # noqa: F401
                                    product, receipt, two_slot_sales, week_of_sales)
from vending import agent, dashboard, forecast_panel

UTC = timezone.utc
PLANNED = datetime(2026, 12, 3, 20, tzinfo=UTC)   # Thursday of the opening week


def forecast(**rates):
    """A plan's recorded forecast: slot -> units per class day (text, as `get_machine_state` gives it)."""
    return {"source": "get_machine_state", "unit": "class_day", "as_of": None,
            "slots": [{"slot": slot, "rate": rate, "basis": "fall prior: fast seller"} for slot, rate in rates.items()]}


def with_forecast(plan, **rates):
    return {**plan, "forecast": forecast(**rates)}


def weeks(page_conn, now=NOW):  # noqa: F811
    snap, _ = cards(page_conn, now)
    assert snap["forecast"]["ok"], snap["forecast"]
    return snap, {w["week"]: w for w in snap["forecast"]["data"]["weeks"]}


def test_a_week_compares_the_recorded_rate_with_what_sold(conn, page_conn, opened):  # noqa: F811
    slot = opened[0]
    product(conn)
    week_of_sales(conn, slot)
    run = live_run(conn, PLANNED, request_id="r1", plan=with_forecast(
        plan_for(opened, **{slot: {"fill_to": 8, "sale_price": "2.50", "reason": "fill it"}}), **{slot: "1.50"}))
    fulfilled(conn, "r1", slot, SKU, units_loaded=8, paid="8.00", at=SAT1 - timedelta(hours=3))
    snap, by_week = weeks(page_conn)
    week = by_week["2026-12-05"]
    # The polls span Fri 4 Dec 23:00 to Fri 11 Dec 23:00 Eastern: Monday to Friday, 5 class days.
    assert week["status"] == "compared" and week["run_id"] == run and week["class_days"] == "5.0"
    row = next(r for r in week["rows"] if r["slot"] == slot)
    assert (row["rate"], row["expected"], row["sold"], row["difference"], row["product"]) == ("1.50", "7.5", 6, "-1.5", SKU)
    # The stock replay counts the 2 sold across the load too: 8 loaded, 8 sold by the last poll, so it ran out.
    assert row["empty"] is True and row["counted"]
    assert week["summary"] == {"slots": 1, "expected": "7.5", "sold": 6, "difference": "-1.5", "average_off": "1.5",
                               "empty": 1}
    # Every other slot of ours is listed, without a rate, and left out of the totals.
    others = [r for r in week["rows"] if r["slot"] != slot]
    assert len(others) == len(opened) - 1 and all(r["note"] == "no rate recorded for this slot" for r in others)
    assert by_week["2026-11-28"]["status"] == "opening" and by_week["2026-12-12"]["status"] == "no_plan"
    page = dashboard.render(snap)
    assert page.index('id="report-card"') < page.index('id="forecast"') < page.index('id="incidents"')
    assert "Forecast vs actual" in page and "Mean |diff| / slot" in page and f"<code>{SKU}</code>" in page
    assert '"forecast"' in dashboard.to_json(snap)


def test_a_plan_without_a_forecast_says_so(conn, page_conn, opened):  # noqa: F811
    slot = opened[0]
    product(conn)
    week_of_sales(conn, slot)
    live_run(conn, PLANNED, request_id="r1",
             plan=plan_for(opened, **{slot: {"fill_to": 8, "sale_price": "2.50", "reason": "fill it"}}))
    fulfilled(conn, "r1", slot, SKU, units_loaded=8, paid="8.00", at=SAT1 - timedelta(hours=3))
    snap, by_week = weeks(page_conn)
    week = by_week["2026-12-05"]
    assert week["status"] == "no_forecast" and week["rows"] == [] and week["summary"] is None
    assert "No forecast recorded" in dashboard.render(snap)


def test_a_swapped_slot_is_shown_but_not_compared(conn, page_conn, opened):  # noqa: F811
    a, b = opened[0], opened[1]
    for sku in ("OLD-CAN", "NEW-CAN", "KEPT-CAN"):
        product(conn, sku)
    receipt(conn, "fill", OPEN + timedelta(hours=1), [(a, "OLD-CAN", 4, "2.00", 0, None),
                                                      (b, "KEPT-CAN", 10, "10.00", 0, None)])
    two_slot_sales(conn, a, b)
    live_run(conn, PLANNED, request_id="friday", plan=with_forecast(plan_for(opened, **{
        a: {"product": "NEW-CAN", "fill_to": 8, "sale_price": "2.50", "reason": "swap it"}}), **{a: "1.00", b: "0.40"}))
    receipt(conn, "friday", SAT1 - timedelta(hours=3), [(a, "NEW-CAN", 8, "8.00", 2, "OLD-CAN")])
    snap, by_week = weeks(page_conn)
    week = by_week["2026-12-05"]
    rows = {r["slot"]: r for r in week["rows"]}
    # Slot a's rate was the old product's (#172): shown with its sales, never in the totals.
    assert rows[a]["expected"] is None and rows[a]["difference"] is None and not rows[a]["counted"]
    assert rows[a]["sold"] == 6 and "swapped" in rows[a]["note"] and rows[a]["product"] == "NEW-CAN"
    # Slot b: 0.40 a class day over the 5 class days to the last poll (Fri 11 Dec 23:00), 1 sold.
    assert (rows[b]["expected"], rows[b]["sold"], rows[b]["difference"]) == ("2.0", 1, "-1.0")
    assert week["summary"]["slots"] == 1 and week["summary"]["expected"] == "2.0"
    assert "swapped at the visit" in dashboard.render(snap)


def test_short_poll_coverage_shows_figures_but_withholds_the_average(conn, page_conn, opened):  # noqa: F811
    slot = opened[0]
    product(conn)
    for name, at, units in (("w0-start", SAT1 - timedelta(days=7) + timedelta(hours=1), 100),
                            ("w0-end", SAT1 - timedelta(hours=1), 102),
                            ("w1-mid", SAT1 + timedelta(days=3), 105),
                            ("w3", NOW - timedelta(hours=1), 112)):
        store_poll(conn, name, at.isoformat(), {slot: (250, units)})
    live_run(conn, PLANNED, request_id="r1", plan=with_forecast(
        plan_for(opened, **{slot: {"fill_to": 8, "sale_price": "2.50", "reason": "fill it"}}), **{slot: "2.00"}))
    fulfilled(conn, "r1", slot, SKU, units_loaded=8, paid="8.00", at=SAT1 - timedelta(hours=3))
    snap, by_week = weeks(page_conn)
    week = by_week["2026-12-05"]
    # The polls reach Tue 8 Dec 00:00 Eastern: one class day (Monday), so expected is 2.0, over the same stretch.
    assert week["final"] and week["class_days"] == "1.0"
    assert week["summary"]["expected"] == "2.0" and week["summary"]["sold"] == 3
    assert week["summary"]["average_off"] is None and any("3.0 days" in n for n in week["notes"])
    assert "withheld" in dashboard.render(snap)


def test_the_current_week_is_compared_over_the_polls_so_far(conn, page_conn, opened):  # noqa: F811
    slot = opened[0]
    product(conn)
    week_of_sales(conn, slot)
    store_poll(conn, "w1-day3", (SAT1 + timedelta(days=3, hours=9)).isoformat(), {slot: (250, 104)})
    live_run(conn, PLANNED, request_id="r1", plan=with_forecast(
        plan_for(opened, **{slot: {"fill_to": 8, "sale_price": "2.50", "reason": "fill it"}}), **{slot: "1.00"}))
    fulfilled(conn, "r1", slot, SKU, units_loaded=8, paid="8.00", at=SAT1 - timedelta(hours=3))
    now = SAT1 + timedelta(days=3, hours=10)        # Tue 8 Dec 10:00 Eastern
    _, by_week = weeks(page_conn, now)
    week = by_week["2026-12-05"]
    # Monday, and Tuesday to 09:00 Eastern (the 07 and 08 o'clock hours: 28% of a day).
    assert not week["final"] and week["class_days"] == "1.3"
    row = week["rows"][0]
    assert row["sold"] == 2 and row["expected"] == "1.3"
    assert any(n.startswith("In progress") for n in week["notes"])


def test_only_our_selections_are_listed(conn, page_conn, opened):  # noqa: F811
    slot = opened[0]
    outside = next(f"{r}{c}" for r in "12345" for c in "12345678" if f"{r}{c}" not in opened)
    product(conn)
    week_of_sales(conn, slot)
    recorded = with_forecast(plan_for(opened, **{slot: {"fill_to": 8, "sale_price": "2.50", "reason": "fill it"}}),
                             **{slot: "1.50", outside: "9.87"})
    recorded["forecast"]["slots"][1]["basis"] = "ROW-NOT-OURS"
    live_run(conn, PLANNED, request_id="r1", plan=recorded)
    fulfilled(conn, "r1", slot, SKU, units_loaded=8, paid="8.00", at=SAT1 - timedelta(hours=3))
    snap, by_week = weeks(page_conn)
    rows = by_week["2026-12-05"]["rows"]
    assert [r["slot"] for r in rows] == list(opened) and outside not in {r["slot"] for r in rows}
    assert [r["slot"] for r in rows if r["counted"]] == [slot]
    for text in (dashboard.to_json(snap["forecast"]), dashboard.render(snap)):
        assert "9.87" not in text and "ROW-NOT-OURS" not in text


def test_before_the_opening_and_without_the_report_card():
    assert forecast_panel.read(None, NOW, None) == {"opened": None}
    assert forecast_panel.read(None, NOW, {"opened": False}) == {"opened": False}
    assert "No data before the ledger opening" in forecast_panel.html({"ok": True, "data": {"opened": False}})
    assert "report card couldn't be read" in forecast_panel.html({"ok": True, "data": {"opened": None}})


def test_class_days_are_weighted_by_the_hour_and_skip_weekends_and_breaks():
    day = datetime(2026, 12, 7, 5, tzinfo=UTC)               # Mon 7 Dec 00:00 Eastern
    assert forecast_panel.class_day_share(day, day + timedelta(days=7)) == 5
    assert forecast_panel.class_day_share(day - timedelta(days=2), day) == 0      # a weekend
    assert forecast_panel.class_day_share(day + timedelta(hours=9), day + timedelta(hours=10)) == Fraction(22, 100)
    holiday = datetime(2026, 12, 23, 5, tzinfo=UTC)          # the winter break
    assert forecast_panel.class_day_share(holiday, holiday + timedelta(days=2)) == 0


# --- What the run records -------------------------------------------------------------------------

def test_the_recorded_plan_carries_the_forecast_only_when_there_is_one():
    plan = {"slots": [{"slot": "15", "fill_to": 7}], "off_cycle": False, "plan_hash": "abc"}
    made = forecast(**{"15": "1.20"})
    assert agent.recorded_plan(plan, {}, made)["forecast"] == made
    assert "forecast" not in agent.recorded_plan(plan, {})
    assert "forecast" not in agent.recorded_plan(plan, {}, None)


def test_the_run_stores_the_rates_the_model_received_and_reads_nothing_again(schema_url, conn, monkeypatch):
    from tests.test_agent import PLAN, Script, Tools, answer, connect as _connect, go, stored, tool_turn, use  # noqa: F401
    from tests.conftest import open_in
    from vending.config import set_halted
    from vending.tools import machine_state

    set_halted(conn, False, "test")
    connect = lambda: open_in(*schema_url)                                     # noqa: E731
    got = {"success": True, "as_of": "2026-11-12T14:00:00+00:00", "data": [
        {"slot": "15", "units_per_class_day": "1.70", "rate_basis": "own: 17 sold over 10 in-stock class days"},
        {"slot": "16", "units_per_class_day": None, "rate_basis": "no price to pick a fall segment"}]}

    def no_second_read(*args, **kwargs):
        raise AssertionError("the run read machine state again for its forecast")
    monkeypatch.setattr(machine_state, "get_machine_state", no_second_read)
    client = Script(tool_turn(use("get_machine_state")),
                    tool_turn(use("preview_desired_state", {"desired": PLAN}, id="t2")), answer("submit"))
    tools = Tools(replies={"get_machine_state": got,
                           "preview_desired_state": {"success": True, "message": "ok", "ok": True, "plan_hash": "abc"}})
    run_id, out = go(connect, client, tools)
    plan = stored(conn, run_id)[2]
    assert plan["forecast"] == {"source": "get_machine_state", "unit": "class_day", "as_of": got["as_of"], "slots": [
        {"slot": "15", "rate": "1.70", "basis": got["data"][0]["rate_basis"]},
        {"slot": "16", "rate": None, "basis": got["data"][1]["rate_basis"]}]}


def test_the_briefing_s_calendar_rate_is_kept_only_until_a_class_day_rate_arrives():
    out = agent.Outcome("skipped", "")
    briefing = {"success": True, "computed_at": "2026-11-12T13:00:00+00:00", "slots": [{"slot": "15", "units_per_day": "0.86"}]}
    agent.capture_forecast(out, "get_briefing", briefing)
    assert out.forecast["unit"] == "calendar_day" and out.forecast["slots"][0]["rate"] == "0.86"
    agent.capture_forecast(out, "get_machine_state", {"success": True, "data": [{"slot": "15", "units_per_class_day": "1.20"}]})
    agent.capture_forecast(out, "get_briefing", briefing)
    assert out.forecast["source"] == "get_machine_state" and out.forecast["slots"][0]["rate"] == "1.20"
    for broken in (None, "x", {"data": "x"}, {"data": [None, {"slot": object()}]}, {"success": False, "data": [{"slot": "15"}]}):
        agent.capture_forecast(out, "get_machine_state", broken)          # never raises; keeps what it had
    assert out.forecast["slots"][0]["rate"] == "1.20"


def test_a_briefing_rate_is_per_calendar_day(conn, page_conn, opened):  # noqa: F811
    slot = opened[0]
    product(conn)
    week_of_sales(conn, slot)
    recorded = plan_for(opened, **{slot: {"fill_to": 8, "sale_price": "2.50", "reason": "fill it"}})
    recorded["forecast"] = {"source": "get_briefing", "unit": "calendar_day", "as_of": None,
                            "slots": [{"slot": slot, "rate": "1.00", "basis": None}]}
    live_run(conn, PLANNED, request_id="r1", plan=recorded)
    fulfilled(conn, "r1", slot, SKU, units_loaded=8, paid="8.00", at=SAT1 - timedelta(hours=3))
    _, by_week = weeks(page_conn)
    row = next(r for r in by_week["2026-12-05"]["rows"] if r["slot"] == slot)
    # 1.00 a calendar day over the 7 days the polls span: 7.0, not the 5 class days.
    assert row["expected"] == "7.0" and row["unit"] == "calendar_day"


def test_a_product_changed_midweek_is_not_compared(conn, page_conn, opened):  # noqa: F811
    a, b = opened[0], opened[1]
    for sku in ("OLD-CAN", "NEW-CAN", "KEPT-CAN"):
        product(conn, sku)
    receipt(conn, "fill", OPEN + timedelta(hours=1), [(a, "OLD-CAN", 4, "2.00", 0, None),
                                                      (b, "KEPT-CAN", 10, "10.00", 0, None)])
    two_slot_sales(conn, a, b)
    live_run(conn, PLANNED, request_id="friday", plan=with_forecast(plan_for(opened, **{
        b: {"fill_to": 10, "reason": "keep it"}}), **{a: "1.00", b: "0.40"}))
    receipt(conn, "friday", SAT1 - timedelta(hours=3), [(b, "KEPT-CAN", 1, "1.00", 0, None)])
    # A paid midweek visit on Tue 8 Dec swaps slot a to NEW-CAN: the week's sales in a are two products'.
    receipt(conn, "midweek", SAT1 + timedelta(days=3, hours=15), [(a, "NEW-CAN", 8, "8.00", 0, "OLD-CAN")],
            off_cycle=True)
    _, by_week = weeks(page_conn)
    rows = {r["slot"]: r for r in by_week["2026-12-05"]["rows"]}
    assert not rows[a]["counted"] and rows[a]["difference"] is None and "product changed" in rows[a]["note"]
    assert rows[b]["counted"]


def test_missing_prior_week_polls_keep_this_week_s_sales(conn, page_conn, opened):  # noqa: F811
    slot = opened[0]
    product(conn)
    # Only this week's polls: last week can't be rebuilt, so the report card can't score a baseline.
    store_poll(conn, "w1-start", (SAT1 + timedelta(minutes=30)).isoformat(), {slot: (250, 102)})
    store_poll(conn, "w1-end", (SAT2 - timedelta(hours=1)).isoformat(), {slot: (250, 108)})
    live_run(conn, PLANNED, request_id="r1", plan=with_forecast(
        plan_for(opened, **{slot: {"fill_to": 8, "sale_price": "2.50", "reason": "fill it"}}), **{slot: "1.50"}))
    fulfilled(conn, "r1", slot, SKU, units_loaded=8, paid="8.00", at=SAT1 - timedelta(hours=3))
    snap, by_week = weeks(page_conn)
    card = next(c for c in snap["report_cards"]["data"]["weeks"] if c["week"] == "2026-12-05")
    assert card["outcome"] is None and "Last week can't be rebuilt from the polls." in card["notes"]
    row = next(r for r in by_week["2026-12-05"]["rows"] if r["slot"] == slot)
    assert by_week["2026-12-05"]["status"] == "compared" and row["sold"] == 6 and row["counted"]
