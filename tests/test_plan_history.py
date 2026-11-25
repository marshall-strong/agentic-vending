"""The dashboard's plan history (`vending.plan_history`): one card per week, each plan and update, every changed
slot with the agent's reason and the inputs its run read for it."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from psycopg.types.json import Jsonb

from tests.conftest import TEST_SLOTS
from tests.test_dashboard import catalog, page_conn  # noqa: F401  (a fixture)
from vending import agent_runs, dashboard, ledger, plan_history

NOW = datetime(2026, 12, 9, 15, 0, tzinfo=timezone.utc)        # Wed 9 Dec, 10:00 Eastern
OPENED = datetime(2026, 12, 2, 15, 0, tzinfo=timezone.utc)     # Wed 2 Dec: the week of Sat 28 Nov
COKE, PEPSI = "COCA-ORIGINAL-20BOT", "PEPSI-ORIGINAL-20BOT"
OUTSIDE = next(f"{r}{c}" for r in "12345" for c in "12345678" if f"{r}{c}" not in TEST_SLOTS)


@pytest.fixture(autouse=True)
def _after_the_lock(monkeypatch):
    monkeypatch.setattr(dashboard, "lock_at", lambda: OPENED - timedelta(days=5))


@pytest.fixture
def opened(conn):
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=OPENED)
    conn.execute("update slot_assignment set first_read_at = %s", (OPENED - timedelta(days=10),))
    catalog(conn, (COKE, "Coca-Cola 20 oz", "Coca-Cola", None, "20 oz bottle"),
            (PEPSI, "Pepsi 20 oz", "Pepsi", None, "20 oz bottle"))


def entry(slot, product=COKE, price="2.50", fill=7, reason="restock what sold"):
    return {"slot": slot, "product": product, "sale_price": price, "fill_to": fill, "reason": reason}


def live(conn, at, slots, *, status="submitted", mode="live", request=None, cost="0.42", update=None, notes=None,
         reason="the week's plan", steps=()):
    run = agent_runs.start(conn, mode=mode, model="claude-sonnet-5-5")
    if status != "running":
        agent_runs.finish(conn, run, status, plan=None if slots is None else
                          {"visit": "scheduled", "reason": reason, "slots": list(slots)},
                          cost_usd=None if cost is None else Decimal(cost), request_id=request, update=update,
                          notes=notes)
    conn.execute("update agent_runs set started_at = %s, finished_at = %s where id = %s",
                 (at, None if status == "running" else at + timedelta(minutes=10), run))
    for n, (kind, tool, content, error) in enumerate(steps):
        conn.execute("insert into agent_steps (run_id, turn, at, kind, tool, content, is_error)"
                     " values (%s, 1, %s, %s, %s, %s, %s)", (run, at + timedelta(seconds=n), kind, tool, content, error))
    return run


def request(conn, request_id, at, *, status="pending", paid=None, off_cycle=False, fee=None, lines=None):
    body = {"off_cycle": off_cycle, "lines": lines if lines is not None else [
        {"slot": "15", "source": "purchase", "primary": {"sku": COKE, "store": "Walmart", "packs": 2,
                                                         "units_per_pack": 12, "max_price_per_pack": "20.50"}}]}
    conn.execute("insert into purchase_requests (request_id, submitted_at, off_cycle, request_body, status, total_paid,"
                 " visit_fee_cents) values (%s, %s, %s, %s, %s, %s, %s)",
                 (request_id, at, off_cycle, Jsonb(body), status, None if paid is None else Decimal(paid), fee))


def history(page_conn, now=NOW):
    snap = dashboard.snapshot(page_conn, now)
    return snap, snap["plan_history"]


def section(snap):
    return dashboard.render(snap).split('id="plan-history"')[1].split("</section>")[0]


def week(data, day):
    return next(w for w in data["weeks"] if w["week"] == day)


# --- Empty and before ---------------------------------------------------------------------------

def test_before_the_opening_there_is_no_history(conn, page_conn):
    live(conn, OPENED - timedelta(days=3), [entry("15")])
    snap, p = history(page_conn)
    assert p["ok"] and p["data"] == {"opened": False, "opens_at": None, "weeks": []}
    assert "the ledger hasn&#39;t opened" in section(snap)


def test_an_opening_recorded_ahead_says_when_the_history_starts(conn, page_conn):
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=NOW + timedelta(days=1))
    snap, p = history(page_conn)
    assert p["data"]["opened"] is False and p["data"]["opens_at"] == NOW + timedelta(days=1)
    assert "opens Thu 10 Dec" in section(snap)


def test_empty_tables_give_one_empty_card_per_week_newest_first(opened, page_conn):
    snap, p = history(page_conn)
    weeks = p["data"]["weeks"]
    assert [w["week"] for w in weeks] == [date(2026, 12, 5), date(2026, 11, 28)]
    assert weeks[0]["current"] and not weeks[1]["current"]
    assert all(w["plans"] == [] and w["fill"] is None for w in weeks)
    assert section(snap).count("No live plan this week") == 2


def test_at_most_eight_weeks_are_shown(opened, page_conn):
    later = NOW + timedelta(weeks=10)
    assert len(history(page_conn, later)[1]["data"]["weeks"]) == plan_history.WEEKS_SHOWN


# --- Plans, changes and what drove them ---------------------------------------------------------

def test_each_change_shows_was_now_reason_request_cost_and_its_inputs(conn, opened, page_conn):
    request(conn, "pr-1", OPENED + timedelta(hours=1), status="fulfilled", paid="34.80")
    first = live(conn, OPENED, [entry("15"), entry("16", PEPSI)], request="pr-1")
    sales = json.dumps({"data": [{"slot": "15", "units_sold_in_window": 7, "price": "2.50"},
                                 {"slot": OUTSIDE, "units_sold_in_window": 99, "price": "9.95"}]})
    request(conn, "pr-2", NOW - timedelta(days=2), lines=[
        {"slot": "15", "source": "purchase", "primary": {"sku": COKE, "packs": 2, "max_price_per_pack": "20.50"},
         "alternate": {"sku": COKE, "packs": 1, "max_price_per_pack": "45.00"}}])
    second = live(conn, NOW - timedelta(days=2), [entry("15", price="2.75", reason="sold out by Sunday"),
                                                  entry("16", PEPSI)],
                  request="pr-2", cost="0.58", update="Prices up on sell-outs.", notes="Watch Coke.",
                  steps=[("tool_call", "get_sales_history", '{"slot": "15"}', False),
                         ("tool_result", "get_sales_history", sales, False),
                         ("tool_call", "find_pack_price", json.dumps({"sku": COKE, "store": "Walmart",
                                                                      "units_per_pack": 12}), False),
                         ("tool_result", "find_pack_price", "the call timed out at the database host", True),
                         ("tool_call", "get_account_balance", "{}", False),
                         ("tool_result", "get_account_balance", '{"balance": "500.00"}', False)])
    snap, p = history(page_conn)
    this = week(p["data"], date(2026, 12, 5))
    plan = this["plans"][0]
    assert plan["id"] == second and plan["compared_with"] == f"#{first}"
    assert (plan["update"], plan["notes"], plan["cost_usd"]) == ("Prices up on sell-outs.", "Watch Coke.", Decimal("0.58"))
    assert plan["request"]["status"] == "pending" and plan["request"]["most"] == "45.00"
    [change] = plan["changes"]
    assert change["slot"] == "15" and change["labels"] == ["$2.50 to $2.75"]
    assert (change["old"]["price"], change["new"]["price"], change["new"]["name"]) == ("2.50", "2.75", "Coca-Cola 20 oz")
    assert change["reason"] == "sold out by Sunday"
    sold, price = change["inputs"]
    assert sold["tool"] == "get_sales_history" and sold["reply"] == {"units_sold_in_window": 7, "price": "2.50"}
    assert price["tool"] == "find_pack_price" and price["failed"] is True and price["reply"] is None
    assert plan["other_calls"] == [{"tool": "get_account_balance", "n": 1, "failed": 0}]
    first_card = week(p["data"], date(2026, 11, 28))["plans"][0]
    assert first_card["id"] == first and first_card["request"]["paid"] == "34.80"
    html = section(snap)
    assert f'href="#run-{second}"' in html and f'href="#steps-{second}"' in html
    assert f'id="run-{second}"' in dashboard.render(snap) and f'id="steps-{second}"' in dashboard.render(snap)
    assert "get_sales_history(slot=15)" in html and "units_sold_in_window 7" in html
    assert "database host" not in html and "9.95" not in html and "at most $45.00" in html and "paid $34.80" in html


def test_a_reason_that_cites_nothing_is_shown_as_is_and_lists_no_inputs(conn, opened, page_conn):
    live(conn, OPENED, [entry("15")])
    live(conn, NOW - timedelta(days=1), [entry("15", PEPSI, reason="Pepsi might do better.")])
    [change] = history(page_conn)[1]["data"]["weeks"][0]["plans"][0]["changes"]
    assert change["labels"] == ["swap"] and change["reason"] == "Pepsi might do better." and change["inputs"] == []


def test_dry_runs_are_counted_not_shown_and_runs_with_no_plan_are_listed(conn, opened, page_conn):
    live(conn, OPENED - timedelta(days=1), [entry("15", price="9.00")])      # live, before the opening
    live(conn, OPENED, [entry("15")])
    live(conn, NOW - timedelta(days=1), [entry("15", price="3.00")], mode="dry-run", status="skipped")
    failed = live(conn, NOW - timedelta(days=2), None, status="failed")
    running = live(conn, NOW - timedelta(hours=1), None, status="running")
    snap, p = history(page_conn)
    d = p["data"]
    assert d["hidden_runs"] == 1
    this = week(d, date(2026, 12, 5))
    assert this["plans"] == [] and [r["id"] for r in this["other_runs"]] == [running, failed]
    first = week(d, date(2026, 11, 28))["plans"][0]
    assert first["compared_with"] is None and [c["labels"] for c in first["changes"]] == [["new"]]
    assert "9.00" not in dashboard.to_json(snap["plan_history"]) and "1 dry runs or replays not shown" in section(snap)


def test_the_day_one_fill_is_in_its_requests_week_and_the_first_plan_compares_with_it(conn, opened, page_conn):
    day_one = [{"slot": s, "sku": COKE, "units": 7, "price_cents": 250, "why": "w"} for s in TEST_SLOTS]
    conn.execute("insert into first_fill_plans (loaded_at, loaded_by, label, source_note, plan, projection, slots,"
                 " picked) values (%s, 'test', '6', 'test', %s, '{}', %s, true)",
                 (OPENED - timedelta(days=4), Jsonb({"day_one": day_one}), list(TEST_SLOTS)))
    request(conn, "pr-fill", OPENED + timedelta(hours=2), status="fulfilled", paid="300.00")
    run = live(conn, NOW - timedelta(days=1), [entry(s) for s in TEST_SLOTS if s != "16"] + [entry("16", price="2.25")])
    snap, p = history(page_conn)
    fill = week(p["data"], date(2026, 11, 28))["fill"]
    assert fill["picked"] and fill["request"]["id"] == "pr-fill" and len(fill["lines"]) == len(TEST_SLOTS)
    plan = week(p["data"], date(2026, 12, 5))["plans"][0]
    assert plan["id"] == run and plan["compared_with"] == "day-one fill"
    assert [(c["slot"], c["labels"]) for c in plan["changes"]] == [("16", ["$2.50 to $2.25"])]
    assert "Day-one fill" in section(snap)


def test_unknown_costs_stay_unknown(conn, opened, page_conn):
    request(conn, "pr-3", OPENED + timedelta(hours=1), lines=[
        {"slot": "15", "source": "purchase", "primary": {"sku": COKE, "packs": 1, "max_price_per_pack": "ten"}}])
    live(conn, OPENED, [entry("15")], request="pr-3", cost=None)
    snap, p = history(page_conn)
    plan = week(p["data"], date(2026, 11, 28))["plans"][0]
    assert plan["cost_usd"] is None and plan["request"]["most"] is None
    html = section(snap)
    assert "model cost <span class=\"dim\">not recorded</span>" in html and "at most: unknown" in html
    assert "$0.00" not in html


# --- Missing tables and older assignments -------------------------------------------------------

def test_missing_steps_and_fill_tables_blank_only_those_parts(conn, opened, page_conn):
    live(conn, OPENED, [entry("15")], steps=[("tool_call", "get_sales_history", '{"slot": "15"}', False)])
    conn.execute("drop table agent_steps")
    conn.execute("drop table first_fill_plans")
    p = history(page_conn)[1]
    assert p["ok"]
    plan = week(p["data"], date(2026, 11, 28))["plans"][0]
    assert plan["changes"][0]["inputs"] == [] and week(p["data"], date(2026, 11, 28))["fill"] is None


def test_a_missing_runs_table_blanks_the_panel_with_how_to_fix_it(conn, opened, page_conn):
    conn.execute("drop table agent_runs cascade")
    snap, p = history(page_conn)
    assert p["ok"] is False and "migrate" in p["why"]
    assert "Not available" in section(snap)


def test_text_from_before_the_current_assignment_is_replaced(conn, opened, page_conn):
    conn.execute("update slot_assignment set first_read_at = %s", (NOW - timedelta(days=1),))
    live(conn, OPENED, [entry("15")])
    live(conn, NOW - timedelta(days=2), [entry("15", price="3.00", reason="an old reason")], update="old update",
         steps=[("tool_call", "get_sales_history", '{"slot": "15"}', False)])
    plan = history(page_conn)[1]["data"]["weeks"][0]["plans"][0]
    assert plan["changes"][0]["reason"] == dashboard.OLD_ASSIGNMENT and plan["update"] is None
    assert plan["changes"][0]["inputs"] == [] and plan["reason"] == dashboard.OLD_ASSIGNMENT


# --- Integrity ----------------------------------------------------------------------------------

def test_nothing_about_a_selection_outside_our_assignment_is_shown(conn, opened, page_conn):
    live(conn, OPENED, [entry("15"), entry(OUTSIDE, PEPSI, price="1.95")])
    live(conn, NOW - timedelta(days=2),
         [entry("15", price="2.75", reason=f"selection {OUTSIDE} sold more"), entry(OUTSIDE, PEPSI, price="4.45")],
         steps=[("tool_call", "get_sales_history", json.dumps({"slot": OUTSIDE}), False),
                ("tool_result", "get_sales_history", json.dumps({"data": [{"slot": OUTSIDE, "units_sold_in_window": 4242}]}),
                 False),
                ("tool_call", "find_pack_price", json.dumps({"slot": OUTSIDE, "sku": COKE}), False),
                ("tool_result", "find_pack_price", '{"price": "17.40"}', False)])
    snap, p = history(page_conn)
    plan = week(p["data"], date(2026, 12, 5))["plans"][0]
    assert [c["slot"] for c in plan["changes"]] == ["15"]
    assert plan["changes"][0]["inputs"] == []          # a call naming another selection isn't this slot's input
    assert plan["changes"][0]["reason"] == dashboard.OUTSIDE_ASSIGNMENT
    text = dashboard.to_json(snap["plan_history"]) + section(snap)
    assert f'"{OUTSIDE}"' not in text and "4.45" not in text and "1.95" not in text and "4242" not in text
    assert f"slot={OUTSIDE}" not in text


# --- Sol's review of #246 ------------------------------------------------------------------------

def test_a_call_carrying_an_outside_selection_in_any_argument_is_never_shown(conn, opened, page_conn):
    live(conn, OPENED, [entry("15")])
    live(conn, NOW - timedelta(days=2), [entry("15", price="2.75")],
         steps=[("tool_call", "find_pack_price",
                 json.dumps({"sku": COKE, "store": "Walmart", "wildcard_slot": OUTSIDE}), False),
                ("tool_result", "find_pack_price", '{"price_per_pack": "17.40"}', False)])
    snap, p = history(page_conn)
    plan = week(p["data"], date(2026, 12, 5))["plans"][0]
    assert plan["changes"][0]["inputs"] == []
    text = dashboard.to_json(snap["plan_history"]) + section(snap)
    assert f"wildcard_slot={OUTSIDE}" not in text and f'"wildcard_slot": "{OUTSIDE}"' not in text


def test_replies_match_calls_by_turn_order_and_a_slots_list_names_the_slot(conn, opened, page_conn):
    live(conn, OPENED, [entry("15"), entry("16")])
    rows = lambda slot, n: json.dumps({"data": [{"slot": slot, "units_sold_in_window": n}]})   # noqa: E731
    run = live(conn, NOW - timedelta(days=2), [entry("15", price="2.75"), entry("16", price="2.25")],
               steps=[("tool_result", "get_briefing", '{"data": {"slots": []}}', False),     # the start-up read
                      ("tool_call", "get_sales_history", json.dumps({"slots": ["15"]}), False),
                      ("tool_call", "get_sales_history", json.dumps({"slot": "16"}), False),
                      ("tool_result", "get_sales_history", rows("15", 1515), False),
                      ("tool_result", "get_sales_history", rows("16", 1616), False)])
    changes = {c["slot"]: c for c in week(history(page_conn)[1]["data"], date(2026, 12, 5))["plans"][0]["changes"]}
    assert changes["15"]["inputs"][0]["reply"] == {"units_sold_in_window": 1515}
    assert changes["15"]["inputs"][0]["args"] == {"slots": "15"}
    assert changes["16"]["inputs"][0]["reply"] == {"units_sold_in_window": 1616}
    assert run


def test_a_midweek_request_with_no_stored_fee_uses_the_configured_fee(conn, opened, page_conn):
    from vending.config import set_value

    set_value(conn, "midweek_fee_cents", 3000, "test")
    request(conn, "pr-mid", OPENED + timedelta(hours=1), status="fulfilled", paid="20.00", off_cycle=True)
    request(conn, "pr-mid2", NOW - timedelta(days=1), off_cycle=True)
    live(conn, OPENED, [entry("15")], request="pr-mid")
    live(conn, NOW - timedelta(days=1), [entry("15", price="2.75")], request="pr-mid2")
    d = history(page_conn)[1]["data"]
    done = week(d, date(2026, 11, 28))["plans"][0]["request"]
    pending = week(d, date(2026, 12, 5))["plans"][0]["request"]
    assert (done["paid"], done["visit_fee"]) == ("20.00", "30.00")
    assert pending["most"] == "71.00"          # 2 packs at $20.50, plus the $30.00 fee


def test_a_failed_call_keeps_no_content_and_shows_a_fixed_label(conn, opened, page_conn):
    live(conn, OPENED, [entry("15")])
    live(conn, NOW - timedelta(days=2), [entry("15", price="2.75")],
         steps=[("tool_call", "get_sales_history", '{"slot": "15", "note": "db-host-secret"}', True),
                ("tool_result", "get_sales_history", "connection to db-host-secret refused", True)])
    snap, p = history(page_conn)
    plan = week(p["data"], date(2026, 12, 5))["plans"][0]
    assert plan["changes"][0]["inputs"] == [] and plan["other_calls"] == [
        {"tool": "get_sales_history", "n": 1, "failed": 1}]
    text = dashboard.to_json(snap["plan_history"]) + section(snap)
    assert "db-host-secret" not in text and "1 failed" in section(snap)


def test_the_test_opening_starts_no_history(conn, page_conn):
    test_at = datetime(2026, 11, 11, 17, 0, tzinfo=timezone.utc)
    ledger.record(conn, "opening", 60000, ledger.TEST_OPENING_DETAIL, "test", at=test_at)
    live(conn, test_at + timedelta(days=1), [entry("15")])
    p = history(page_conn)[1]
    assert p["data"]["opened"] is False and p["data"]["weeks"] == []
