"""The agent dashboard and the run record it reads."""

import json
import re
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from http.server import ThreadingHTTPServer

import psycopg
import pytest
from psycopg.types.json import Jsonb

from tests.conftest import CONNECT_TIMEOUT, TEST_NOW, TEST_SLOTS, store_poll
from vending import agent_runs, dashboard, ledger
from vending.config import set_halted, set_value
from vending.write_gate import log


@pytest.fixture(autouse=True)
def _show_weekly_plan(monkeypatch):
    """Exercise the weekly dashboard. test_plan_store covers the actual lock and first fill."""
    monkeypatch.setattr(dashboard, "lock_at", lambda: TEST_NOW - timedelta(days=30))


def plan(*slots, reason="restock what sold"):
    return {"visit": "scheduled", "reason": reason, "slots": list(slots)}


def status_cells(snap) -> list[str]:
    """The status bar's six cells, in order, each from its opening tag."""
    return dashboard._status_bar(snap).split('<a class="sc')[1:]


def read_only(url, schema):
    return psycopg.connect(url, autocommit=True, connect_timeout=CONNECT_TIMEOUT,
                           options=f"{dashboard.READ_ONLY} -c search_path={schema}")


@pytest.fixture
def page_conn(schema_url):
    """A connection like the dashboard's own: read-only, in the test schema."""
    with read_only(*schema_url) as conn:
        yield conn


@pytest.fixture
def open_ledger(conn):
    """An opening and assignment in place before the dated live dashboard fixtures."""
    at = MONDAY - timedelta(days=7)
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=at)
    conn.execute("update slot_assignment set first_read_at = %s", (at,))


def _live_run(conn, *, model=None):
    run = agent_runs.start(conn, mode="live", model=model)
    conn.execute("update agent_runs set started_at = %s where id = %s", (MONDAY - timedelta(minutes=1), run))
    return run


# --- agent_runs ---------------------------------------------------------------------------------

def test_a_run_starts_running_and_finishes_once(conn):
    run = agent_runs.start(conn, mode="dry-run", model="claude-sonnet-5-5")
    assert conn.execute("select status, finished_at from agent_runs where id = %s", (run,)).fetchone() == ("running", None)
    agent_runs.finish(conn, run, "skipped", plan=plan({"slot": "15", "keep": True}), update="quiet week",
                      notes="watch 15", detail="nothing sold out", cost_usd=Decimal("0.42"))
    status, finished, cost, saved = conn.execute(
        "select status, finished_at, cost_usd, plan from agent_runs where id = %s", (run,)).fetchone()
    assert (status, cost, saved["slots"][0]["slot"]) == ("skipped", Decimal("0.42"), "15")
    assert finished is not None
    with pytest.raises(ValueError, match="already finished"):
        agent_runs.finish(conn, run, "failed")


def test_run_record_refuses_bad_values(conn):
    with pytest.raises(ValueError, match="mode"):
        agent_runs.start(conn, mode="test")
    run = agent_runs.start(conn, mode="live")
    with pytest.raises(ValueError, match="status"):
        agent_runs.finish(conn, run, "running")
    with pytest.raises(TypeError, match="Decimal"):
        agent_runs.finish(conn, run, "failed", cost_usd=0.42)
    with pytest.raises(ValueError, match="doesn't exist"):
        agent_runs.finish(conn, 999_999, "failed")


def test_a_finished_run_must_have_an_end_time(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("insert into agent_runs (mode, status) values ('live', 'submitted')")


# --- snapshot -----------------------------------------------------------------------------------

def test_the_dashboard_connection_cannot_write(page_conn):
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        page_conn.execute("insert into agent_runs (mode) values ('live')")


def test_a_fresh_database_shows_halted_and_empty_panels(page_conn):
    snap = dashboard.snapshot(page_conn)
    assert snap["status"]["halted"] is True
    assert snap["status"]["latest_run"] is None
    assert snap["plan"] == {"ok": True, "data": None}
    assert all(snap[k] == {"ok": True, "data": []}
               for k in ("runs", "activity", "purchase_requests", "catalog_requests"))
    assert "No plan yet" in dashboard.render(snap)


def test_an_open_submission_says_orders_are_blocked_and_what_to_run(conn, page_conn):
    conn.execute("insert into purchase_submissions (at, plan_hash, body, status, detail) values"
                 " (now() - interval '3 hours', 'h', '{}', 'unknown', 'TimeoutError: <raw bridge text>')")
    snap = dashboard.snapshot(page_conn)
    [blocked] = [a for a in snap["attention"] if "Orders are blocked" in a["text"]]
    assert blocked["level"] == "bad" and "(unknown, 3 h ago)" in blocked["text"]
    assert "python -m vending.submissions resolve " in blocked["text"] and "raw bridge text" not in blocked["text"]
    page = dashboard.render(snap)
    assert "Orders are blocked" in page and "<code>python -m vending.submissions resolve " in page
    conn.execute("update purchase_submissions set status = 'not_filed'")
    assert not any("Orders are blocked" in a["text"] for a in dashboard.snapshot(page_conn)["attention"])


def test_plan_compares_with_the_run_before(conn, page_conn, open_ledger):
    first = agent_runs.start(conn, mode="live")
    agent_runs.finish(conn, first, "skipped", plan=plan(
        {"slot": "15", "fill_to": 6, "sale_price": "2.50", "reason": "sold 6"},
        {"slot": "16", "keep": True, "reason": "no figures"}))
    second = agent_runs.start(conn, mode="live", model="claude-sonnet-5-5")
    agent_runs.finish(conn, second, "submitted", plan=plan(
        {"slot": "15", "fill_to": 6, "sale_price": "2.50", "reason": "sold 6 again"},
        {"slot": "16", "product": "MTDEW-20OZ", "fill_to": 4, "sale_price": "2.25", "reason": "try it"}),
        update="Swapping 16 to Mountain Dew.", notes="see if 16 sells", request_id="pr-1")
    p = dashboard.snapshot(page_conn)["plan"]["data"]
    assert (p["run"]["id"], p["previous_run"]) == (second, first)
    by_slot = {s["slot"]: s for s in p["slots"]}
    assert by_slot["15"]["changed"] is False          # only the reason changed
    assert by_slot["16"]["changed"] is True
    assert by_slot["17"] == {"slot": "17", "now": None, "before": None, "changed": False}
    assert [s["slot"] for s in p["slots"]][:4] == ["15", "16", "17", "18"]
    page = dashboard.render(dashboard.snapshot(page_conn))
    assert "Swapping 16 to Mountain Dew." in page and "see if 16 sells" in page and "MTDEW-20OZ" in page
    assert "$2.25 &middot; fill to 4" in page and "was: keep as is" in page
    assert f"1 of 20 slots changed</b> since run #{first}" in page
    assert page.count('class="pc changed"') == 1
    assert '<a class="pill info" href="#slot-16">16 swap</a>' in page and 'id="slot-16"' in page


def test_a_plan_of_the_wrong_shape_still_renders(conn, page_conn, open_ledger):
    run = agent_runs.start(conn, mode="live")
    agent_runs.finish(conn, run, "failed", plan={"slots": "not a list"}, detail="bad answer")
    snap = dashboard.snapshot(page_conn)
    assert all(s["now"] is None for s in snap["plan"]["data"]["slots"])
    dashboard.render(snap)


def test_status_and_activity(conn, page_conn, open_ledger):
    set_halted(conn, False, "kevin")
    with conn.transaction():
        log(conn, "write", "set_price", {"slot": "15"}, "refused", "halted", None)
    run = agent_runs.start(conn, mode="live")
    conn.execute("update agent_runs set started_at = now() - interval '3 hours' where id = %s", (run,))
    snap = dashboard.snapshot(page_conn)
    status = snap["status"]
    assert (status["halted"], status["halt_changed_by"]) == (False, "kevin")
    assert status["latest_run"]["id"] == run and status["latest_run"]["stale"] is True
    assert status["last_action"]["tool"] == "set_price"
    assert [r["tool"] for r in snap["activity"]["data"]] == ["set_price", "halt", "ledger.opening"]
    assert snap["recent_calls"]["data"] == {"calls": 3, "writes": 3, "refused": 1, "failed": 0}
    assert dashboard.verdict(snap["attention"]) == "problem"
    assert snap["attention"][0]["level"] == "bad" and "probably died" in snap["attention"][0]["text"]
    assert any("1 refused" in a["text"] for a in snap["attention"])
    page = dashboard.render(snap)
    assert 'class="write refused problem"' in page and "writes allowed" in page
    assert "probably died" in status_cells(snap)[0]       # the Agent cell says so


def test_text_from_the_database_is_escaped(conn, page_conn, open_ledger):
    run = agent_runs.start(conn, mode="live")
    agent_runs.finish(conn, run, "failed", update="<script>alert(1)</script>",
                      plan=plan({"slot": "15", "keep": True, "reason": "<b>bold</b>"}))
    page = dashboard.render(dashboard.snapshot(page_conn))
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    assert "<b>bold</b>" not in page


def test_a_missing_table_blanks_only_its_panel(database_url):
    schema = f"test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(database_url, autocommit=True, connect_timeout=CONNECT_TIMEOUT) as admin:
        admin.execute(f"create schema {schema}")
        try:
            with read_only(database_url, schema) as conn:
                snap = dashboard.snapshot(conn)
                assert snap["status"]["halted"] is True     # no config table counts as halted
                assert snap["plan"]["ok"] is False and "migrate" in snap["plan"]["why"]
                assert "Not available" in dashboard.render(snap)
        finally:
            admin.execute(f"drop schema {schema} cascade")


def test_next_cutoff_is_thursday_2359_eastern():
    if not _has_eastern():
        pytest.skip("no IANA time zone database here (pip install tzdata)")
    # Wednesday 11 Nov 2026, noon Eastern (17:00 UTC) -> Thursday 12 Nov 23:59 Eastern.
    cutoff = dashboard.next_cutoff(datetime(2026, 11, 11, 17, 0, tzinfo=timezone.utc))
    assert (cutoff.date().isoformat(), cutoff.hour, cutoff.minute) == ("2026-11-12", 23, 59)
    # Just after the cutoff, the next one is a week on.
    after = dashboard.next_cutoff(cutoff + timedelta(minutes=1))
    assert after - cutoff == timedelta(days=7)


def _has_eastern():
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("America/New_York")
        return True
    except Exception:
        return False


# --- HTTP ---------------------------------------------------------------------------------------

@pytest.fixture
def served(schema_url, monkeypatch):
    monkeypatch.setattr(dashboard, "connect", lambda: read_only(*schema_url))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
    thread = threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as reply:
            return reply.status, reply.headers["Content-Type"], reply.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.headers["Content-Type"], error.read().decode("utf-8")


def test_serves_the_page_and_json(served, conn, open_ledger):
    run = agent_runs.start(conn, mode="live")
    agent_runs.finish(conn, run, "skipped", cost_usd=Decimal("0.10"))
    code, kind, body = get(served + "/")
    assert (code, kind) == (200, "text/html; charset=utf-8") and "Agent dashboard" in body
    code, kind, body = get(served + "/state.json")
    state = json.loads(body)
    assert (code, state["runs"]["data"][0]["cost_usd"]) == (200, "0.10")
    assert get(served + "/nothing")[0] == 404


def test_a_database_that_is_down_gives_a_503_page(monkeypatch):
    def down():
        raise psycopg.OperationalError("connection refused")
    monkeypatch.setattr(dashboard, "connect", down)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
    threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
    try:
        code, _, body = get(f"http://127.0.0.1:{httpd.server_address[1]}/")
        assert code == 503 and "connection refused" in body
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- from the review ----------------------------------------------------------------------------

def test_money_stays_exact_both_ways(conn, page_conn, open_ledger):
    run = agent_runs.start(conn, mode="live")
    agent_runs.finish(conn, run, "submitted", plan=plan({"slot": "15", "fill_to": 6, "sale_price": Decimal("2.50")}))
    assert conn.execute("select plan->'slots'->0->>'sale_price' from agent_runs").fetchone()[0] == "2.50"
    # A price someone stored as a JSON number still reads back exact, not as the float 2.5.
    conn.execute("""update agent_runs set plan = jsonb_set(plan, '{slots,0,sale_price}', '2.50')""")
    snap = dashboard.snapshot(page_conn)
    assert snap["plan"]["data"]["slots"][0]["now"]["sale_price"] == Decimal("2.50")
    assert "$2.50" in dashboard.render(snap)
    assert '"sale_price": "2.50"' in dashboard.to_json(snap)


def test_only_live_runs_after_opening_are_shown(conn, page_conn, open_ledger):
    live = agent_runs.start(conn, mode="live")
    agent_runs.finish(conn, live, "submitted", plan=plan({"slot": "15", "keep": True}))
    dry = agent_runs.start(conn, mode="dry-run")
    agent_runs.finish(conn, dry, "skipped", plan=plan({"slot": "15", "fill_to": 3, "sale_price": "2.00"}))
    replay = agent_runs.start(conn, mode="replay")
    agent_runs.finish(conn, replay, "skipped", plan=plan({"slot": "15", "fill_to": 9, "sale_price": "1.00"}))
    snap = dashboard.snapshot(page_conn)
    assert snap["status"]["latest_run"]["id"] == live
    p = snap["plan"]["data"]
    assert (p["run"]["id"], p["previous_run"]) == (live, None)
    assert [r["id"] for r in snap["runs"]["data"]] == [live]


def test_an_outdated_table_blanks_only_its_panel(conn, page_conn):
    conn.execute("alter table agent_runs drop column cost_usd")
    snap = dashboard.snapshot(page_conn)
    assert snap["runs"]["ok"] is False and "cost_usd" in snap["runs"]["why"]
    assert snap["activity"]["ok"] is True
    dashboard.render(snap)


def test_refuses_a_host_that_isnt_local(served):
    request = urllib.request.Request(served + "/state.json", headers={"Host": "evil.example:8765"})
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=10)
    assert error.value.code == 403
    request = urllib.request.Request(served + "/", headers={"Host": "localhost"})
    with urllib.request.urlopen(request, timeout=10) as reply:
        assert reply.status == 200


@pytest.mark.parametrize("now, before, tag, labels", [
    ({"slot": "16", "product": "MTDEW", "fill_to": 4}, {"slot": "16", "keep": True}, None, ["swap"]),
    ({"slot": "15", "fill_to": 8, "sale_price": "2.75"}, {"slot": "15", "fill_to": 6, "sale_price": "2.50"}, None,
     ["$2.50 to $2.75", "fill 6 to 8"]),
    # After a "keep", a price is judged against the tag: a change only when it differs.
    ({"slot": "15", "sale_price": "2.75"}, {"slot": "15", "keep": True}, "2.50", ["$2.50 to $2.75"]),
    ({"slot": "15", "fill_to": 8, "sale_price": "3.00"}, {"slot": "15", "keep": True}, "3.00", ["restock"]),
    ({"slot": "15", "fill_to": 0}, {"slot": "15", "fill_to": 6}, None, ["pull"]),
    ({"slot": "15", "sale_price": "2.75"}, {"slot": "15", "keep": True}, None, ["set $2.75"]),
    ({"slot": "15", "keep": True}, None, None, ["new"]),
    ({"slot": "15", "keep": True}, {"slot": "15", "fill_to": 6}, None, ["keep"]),
    ({"slot": "15", "sale_price": Decimal("2.50")}, {"slot": "15", "sale_price": "2.50"}, None, ["changed"]),
])
def test_change_labels(now, before, tag, labels):
    assert dashboard._changes(now, before, tag) == labels


# --- the redesign: runs, machine, attention ------------------------------------------------------

# Monday 9 Nov 2026, 14:00 UTC: three days before the cutoff. THURSDAY_NOON is 12 h before it.
MONDAY = datetime(2026, 11, 9, 14, 0, tzinfo=timezone.utc)
THURSDAY_NOON = datetime(2026, 11, 12, 17, 0, tzinfo=timezone.utc)


def fulfilled(conn, request_id, slot, sku, *, units_loaded, paid, at, to_back_stock=0):
    """A fulfilled purchase request with one primary line, as the requests sync stores it."""
    conn.execute("insert into purchase_requests (request_id, submitted_at, off_cycle, request_body, status,"
                 " total_paid, fulfilled_at) values (%s, %s, false, '{}', 'fulfilled', %s, %s)",
                 (request_id, at - timedelta(days=1), Decimal(paid), at))
    conn.execute(
        "insert into purchase_lines (request_id, line_no, slot, source, asked, outcome, sku, store, packs_bought,"
        " units_per_pack, units_bought, total_paid, unit_cost_paid, units_loaded, units_lost,"
        " units_to_back_stock, units_from_back_stock, units_pulled, slot_reassigned, resolved_at)"
        " values (%s, 0, %s, 'purchase', '{}', 'primary', %s, 'Walmart', 1, %s, %s, %s, null, %s, 0, %s, 0, 0,"
        " false, %s)",
        (request_id, slot, sku, units_loaded + to_back_stock, units_loaded + to_back_stock, Decimal(paid),
         units_loaded, to_back_stock, at))


@pytest.mark.parametrize("offset", [None, timedelta(hours=1), timedelta(hours=-1)],
                         ids=["no-opening", "future-opening", "after-opening"])
def test_live_dashboard_uses_opening_for_calls_purchases_and_stock(conn, page_conn, offset):
    now = datetime(2026, 11, 16, 18, tzinfo=timezone.utc)
    if offset is not None:
        ledger.record(conn, "opening", 60000, "opening capital", "operator", at=now + offset)
    opened = offset is not None and offset < timedelta(0)
    test_at, live_at = now - timedelta(hours=2), now - timedelta(hours=1)
    fulfilled(conn, "test-receipt", "15", "TEST-DRINK", units_loaded=2, paid="81.19", at=test_at, to_back_stock=2)
    if opened:  # a request submitted before opening can still have a live receipt
        fulfilled(conn, "day-one-receipt", "15", "LIVE-DRINK", units_loaded=3, paid="3.25",
                  at=live_at, to_back_stock=1)
    conn.execute("insert into purchase_requests (request_id, submitted_at, off_cycle, request_body, status) values"
                 " ('test-rejection', %s, false, '{}', 'rejected'), ('day-one-pending', %s, false, '{}', 'pending')",
                 (test_at, test_at - timedelta(minutes=1)))
    for status in ("sending", "unknown", "refused") if not opened else ():
        conn.execute("insert into purchase_submissions (at, plan_hash, body, status) values (%s, 'h', '{}', %s)",
                     (test_at, status))
    catalog_request(conn, "REAL-CATALOG-APPROVAL", "approved", test_at, "APPROVED-SKU")
    store_poll(conn, "owned", test_at.isoformat(), {"15": (265, 10)})
    for tool in ("slots.record", "ledger.adjustment", "plan_store.pick", "config.set", "request_catalog_product"):
        conn.execute("insert into action_log (at, kind, tool, arguments, outcome) values (%s, 'write', %s, '{}', 'done')",
                     (test_at, tool))
    conn.execute("insert into action_log (at, kind, tool, arguments, outcome, detail) "
                 "values (%s, 'read', 'get_catalog', '{}', 'failed', 'TEST-CALL')", (test_at,))
    conn.execute("insert into action_log (at, kind, tool, arguments, outcome, detail) "
                 "values (%s, 'read', 'get_catalog', '{}', 'done', 'LIVE-CALL')", (live_at,))
    early_live = conn.execute("insert into agent_runs (mode, started_at, finished_at, status) "
                              "values ('live', %s, %s, 'skipped') returning id", (test_at, test_at)).fetchone()[0]
    for mode in ("dry-run", "replay"):
        conn.execute("insert into agent_runs (mode, started_at, finished_at, status) "
                     "select %s, %s, %s, 'skipped' from generate_series(1, %s)",
                     (mode, test_at, test_at, dashboard.RUN_ROWS + 1))
        conn.execute("insert into agent_runs (mode, started_at) values (%s, %s)",
                     (mode, now - timedelta(minutes=1) if opened else test_at))
    live = conn.execute("insert into agent_runs (mode, started_at) values ('live', %s) returning id",
                        (live_at,)).fetchone()[0]
    agent_runs.step(conn, live, 1, "tool_result", "LIVE-STEP", tool="get_catalog")
    dry = conn.execute("select max(id) from agent_runs where mode = 'dry-run'").fetchone()[0]
    agent_runs.step(conn, dry, 1, "thinking", "TEST-STEP")
    snap = dashboard.snapshot(page_conn, now)
    page, body = dashboard.render(snap), dashboard.to_json(snap)
    for marker in ("TEST-DRINK", "81.19", "test-receipt", "test-rejection", "TEST-CALL", "TEST-STEP"):
        assert marker not in page and marker not in body
    assert snap["sales"]["data"]["slots"]["15"]["price"] == "2.65"
    assert snap["catalog_counts"]["data"]["by_status"]["approved"] == 1
    assert {r["tool"] for r in snap["activity"]["data"]} >= {
        "slots.record", "ledger.adjustment", "plan_store.pick", "config.set", "request_catalog_product",
    }
    assert snap["recent_calls"]["data"]["failed"] == 0
    assert not any("was rejected" in item["text"] for item in snap["attention"])
    assert "day-one-pending" in page and "REAL-CATALOG-APPROVAL" in page
    assert [r["id"] for r in snap["runs"]["data"]] == [live, early_live]
    assert ("LIVE-CALL" in body) is opened and "LIVE-STEP" in body
    assert ("LIVE-DRINK" in page) is opened and ("day-one-receipt" in body) is opened
    assert snap["money"]["data"]["committed"]["unsettled"] == (0 if opened else 2)
    assert f"{2 * (dashboard.RUN_ROWS + (1 if opened else 2))} test runs and 2 test purchases" in page


def test_standalone_routes_scope_reassigned_slots_and_redact_failed_steps(served, conn):
    now = datetime.now(timezone.utc)
    before, reassigned, after = (now - timedelta(minutes=n) for n in (10, 5, 2))
    ledger.record(conn, "opening", 60000, "opening capital", "operator", at=before)
    fulfilled(conn, "old-slot-receipt", "15", "RETIRED-DRINK", units_loaded=3, paid="3.25", at=before)
    store_poll(conn, "old-poll", before.isoformat(), {"15": (8191, 2), "16": (255, 2)})
    conn.execute("update slot_assignment set slots = array_remove(slots, '15') || array['11'], "
                 "first_read_at = %s, last_checked_at = %s", (reassigned, reassigned))
    old = conn.execute("insert into agent_runs (mode, started_at) values ('live', %s) returning id",
                       (before,)).fetchone()[0]
    agent_runs.finish(conn, old, "submitted", update="RETIRED-UPDATE",
                      plan=plan({"slot": "15", "product": "RETIRED-PLAN"},
                                {"slot": "16", "reason": "RETIRED-REASON"}, reason="RETIRED-OVERALL"))
    agent_runs.step(conn, old, 1, "thinking", "RETIRED-THOUGHT")
    live = conn.execute("insert into agent_runs (mode, started_at) values ('live', %s) returning id",
                        (after,)).fetchone()[0]
    agent_runs.step(conn, live, 1, "tool_call", '{"slot":"15","note":"RETIRED-CALL"}', tool="get_sales_history")
    agent_runs.step(conn, live, 1, "tool_result", '{"data":[{"slot":"15","product":"RETIRED-RESULT"},'
                    '{"slot":"16","product":"OWNED-RESULT"}]}', tool="get_machine_state")
    agent_runs.step(conn, live, 1, "tool_result", '{"slot":"15","result":"RETIRED-TRUNCATED',
                    tool="get_machine_state")
    agent_runs.step(conn, live, 1, "tool_result", "SECRET-STEP-TEXT", tool="get_catalog", is_error=True)
    page, body = (get(served + path)[2] for path in ("/", "/state.json"))
    for marker in ("RETIRED-", "81.91", "SECRET-STEP-TEXT"):
        assert marker not in page and marker not in body
    assert all("OWNED-RESULT" in value and "from before the current slot assignment" in value for value in (page, body))
    assert "$2.55" in page and "2.55" in body
    assert dashboard.HIDDEN in body
    assert not any("RETIRED-" in label or "81.91" in label for label in re.findall(r'(?:aria-label|title)="([^"]*)"', page))


@pytest.mark.parametrize("kind", ["tool_result", "tool_call", "text", "thinking"])
def test_failed_steps_hide_their_error_text_and_successful_steps_keep_theirs(conn, page_conn, open_ledger, kind):
    """#165: the content of a step marked `is_error` never reaches the page or the JSON."""
    host = "vend-db-7f3a.postgres.example.internal"
    run = _live_run(conn)
    tool = None if kind in ("thinking", "text") else "get_machine_state"
    agent_runs.step(conn, run, 1, kind, f"could not reach postgresql://vend_admin:Sw0rdf1sh@{host}:5432/vending",
                    tool=tool, is_error=True)
    agent_runs.step(conn, run, 2, "tool_result", '{"products": ["SUCCESSFUL-STEP-MARKER"]}', tool="get_catalog")
    agent_runs.finish(conn, run, "skipped", detail="nothing to buy")
    for public in (False, True):
        snap = dashboard.snapshot(page_conn, MONDAY, public=public)
        page, body = dashboard.render(snap), dashboard.to_json(snap)
        for text in (page, body):
            assert host not in text and "Sw0rdf1sh" not in text and "postgresql://" not in text
            assert "SUCCESSFUL-STEP-MARKER" in text
        steps = snap["runs"]["data"][0]["steps"]
        assert steps[0]["content"] == dashboard.HIDDEN and steps[0]["is_error"]
        assert "SUCCESSFUL-STEP-MARKER" in steps[1]["content"] and not steps[1]["is_error"]


def test_rejected_test_request_does_not_change_verdict(conn, page_conn):
    conn.execute("insert into purchase_requests (request_id, submitted_at, off_cycle, request_body, status) "
                 "values ('test-rejection', %s, false, '{}', 'rejected')", (MONDAY,))
    snap = dashboard.snapshot(page_conn, MONDAY)
    assert snap["purchase_requests"]["data"] == []
    assert dashboard.verdict(snap["attention"]) == "ok"


def test_day_one_request_rejected_after_opening_is_visible_and_needs_a_check(conn, page_conn):
    opening = datetime(2026, 11, 16, 18, tzinfo=timezone.utc)
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=opening)
    conn.execute("insert into purchase_requests (request_id, submitted_at, updated_at, off_cycle,"
                 " request_body, status, decision_note) values ('day-one', %s, %s, false, '{}',"
                 " 'rejected', 'One product needs approval.')",
                 (opening - timedelta(hours=1), opening + timedelta(minutes=1)))
    early = dashboard.snapshot(page_conn, opening - timedelta(minutes=1))
    assert early["purchase_requests"]["data"] == []
    snap = dashboard.snapshot(page_conn, opening + timedelta(minutes=2))
    assert [row["request_id"] for row in snap["purchase_requests"]["data"]] == ["day-one"]
    assert dashboard.verdict(snap["attention"]) == "check"
    assert "Purchase request day-one was rejected" in dashboard.render(snap)
    assert "hidden_tests" not in snap["runs"]


def test_live_run_and_agent_writes_show_before_opening(conn, page_conn):
    now = datetime(2026, 11, 16, 18, tzinfo=timezone.utc)
    at = now - timedelta(minutes=10)
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=now + timedelta(hours=1))
    live = conn.execute("insert into agent_runs (mode, started_at) values ('live', %s) returning id", (at,)).fetchone()[0]
    agent_runs.finish(conn, live, "skipped", plan=plan({"slot": "15", "keep": True}))
    agent_runs.step(conn, live, 1, "text", "LIVE-PRE-OPENING-STEP")
    for mode in ("dry-run", "replay"):
        test = conn.execute("insert into agent_runs (mode, started_at) values (%s, %s) returning id",
                            (mode, at)).fetchone()[0]
        agent_runs.step(conn, test, 1, "text", "TEST-PRE-OPENING-STEP")
    for kind, tool, outcome in (("read", "get_catalog", "failed"), ("write", "submit_desired_state", "refused"),
                                ("write", "set_wildcard_slot", "failed")):
        log(conn, kind, tool, {}, outcome, "test detail")
    conn.execute("update action_log set at = %s where tool <> 'ledger.opening'", (at,))

    snap = dashboard.snapshot(page_conn, now)
    assert snap["status"]["latest_run"]["id"] == live
    assert snap["plan"]["data"]["run"]["id"] == live
    assert [row["id"] for row in snap["runs"]["data"]] == [live]
    assert snap["runs"]["hidden_tests"] == {"runs": 2, "purchases": 0}
    assert {row["tool"]: row["outcome"] for row in snap["activity"]["data"] if row["tool"] != "ledger.opening"} == {
        "submit_desired_state": "refused", "set_wildcard_slot": "failed",
    }
    assert snap["recent_calls"]["data"] == {"calls": 2, "writes": 2, "refused": 1, "failed": 1}
    page = dashboard.render(snap)
    assert "LIVE-PRE-OPENING-STEP" in page and "TEST-PRE-OPENING-STEP" not in page


def test_before_opening_only_exact_write_tools_are_kept(conn, page_conn):
    now = datetime(2026, 11, 16, 18, tzinfo=timezone.utc)
    for tool, arguments in (("ledger.new_tool", {}), ("config.debug", {}),
                            ("ledger.tokens", {"reference": "msg_dev"}), ("plan_store.save", {}),
                            ("ledger.adjustment", {}), ("config.set", {}),
                            ("plan_store.load", {}), ("plan_store.pick", {})):
        log(conn, "write", tool, arguments, "done", tool)
    log(conn, "read", "config.set", {}, "done", "non-write control")
    conn.execute("update action_log set at = %s", (now - timedelta(minutes=10),))

    snap = dashboard.snapshot(page_conn, now)
    assert {row["tool"] for row in snap["activity"]["data"]} == {
        "ledger.adjustment", "config.set", "plan_store.load", "plan_store.pick",
    }
    assert snap["recent_calls"]["data"] == {"calls": 4, "writes": 4, "refused": 0, "failed": 0}
    assert snap["status"]["last_action"]["kind"] == "write"
    for marker in ("ledger.new_tool", "config.debug", "ledger.tokens", "msg_dev", "plan_store.save"):
        assert marker not in dashboard.to_json(snap)


def test_runs_list_what_each_plan_changed(conn, page_conn, open_ledger):
    a = _live_run(conn)
    agent_runs.finish(conn, a, "submitted", plan=plan({"slot": "15", "fill_to": 6, "sale_price": "2.50"},
                                                      {"slot": "28", "fill_to": 6}))
    b = _live_run(conn)
    agent_runs.finish(conn, b, "halted", detail="halted before submitting")
    c = _live_run(conn, model="m")
    agent_runs.finish(conn, c, "submitted", update="raise 15, pull 28", cost_usd=Decimal("0.44"),
                      plan=plan({"slot": "15", "fill_to": 8, "sale_price": "2.75"}, {"slot": "28", "fill_to": 0}))
    runs = {r["id"]: r for r in dashboard.snapshot(page_conn, MONDAY)["runs"]["data"]}
    assert runs[c]["changes"] == [{"slot": "15", "labels": ["$2.50 to $2.75", "fill 6 to 8"]},
                                  {"slot": "28", "labels": ["pull"]}]
    assert (runs[b]["has_plan"], runs[b]["changes"]) == (False, [])   # a run without a plan isn't compared
    assert (runs[a]["has_plan"], runs[a]["compared"]) == (True, False)
    page = dashboard.render(dashboard.snapshot(page_conn, MONDAY))
    assert "28 pull" in page and "halted before submitting" in page and "first plan of this mode" in page


def test_the_machine_shows_stock_and_sales_beside_the_plan(conn, page_conn, open_ledger):
    fulfilled(conn, "pr-1", "15", "COKE-20OZ", units_loaded=8, paid="9.60", to_back_stock=4,
              at=MONDAY - timedelta(days=3))
    store_poll(conn, "p1", (MONDAY - timedelta(days=4)).isoformat(), {"15": (250, 100)})
    store_poll(conn, "p2", (MONDAY - timedelta(days=2)).isoformat(), {"15": (250, 103)})
    store_poll(conn, "p3", (MONDAY - timedelta(hours=1)).isoformat(), {"15": (250, 105)})
    run = _live_run(conn)
    agent_runs.finish(conn, run, "skipped", plan=plan({"slot": "15", "fill_to": 8, "sale_price": "2.75"}))
    snap = dashboard.snapshot(page_conn, MONDAY)
    held = snap["stock"]["data"]["slots"]["15"]
    assert (held["product"], held["loaded"], held["sold"], held["on_hand"]) == ("COKE-20OZ", 8, 5, 3)
    assert held["unit_cost"] == "0.80"                    # $9.60 over 12 units, exact
    assert snap["stock"]["data"]["closet"] == [{"product": "COKE-20OZ", "units": 4, "unit_cost": "0.80"}]
    assert snap["sales"]["data"]["slots"]["15"] == {   # 5 over 3 d 23 h; under 5 class days, the fall prior
        "price": "2.50", "sold": 5, "per_day": "1.26", "per_class_day": "1.701",
        "rate_basis": "fall prior: fast seller (average $2.63); 1 in-stock class day of our own so far, own rate after 5"}
    assert snap["sales"]["data"]["stale"] is False
    assert snap["spend"]["data"]["drinks"]["paid"] == Decimal("9.60")
    page = dashboard.render(snap)
    assert "3 on hand of COKE-20OZ &middot; the slot sold 5 in the 3 d 23 h to the last poll, whatever it held" in page
    # The tile says how fresh the poll is, never a lone window-start date that reads like an update time (#172).
    assert "5 sold in the last 3 d 23 h" in status_cells(snap)[1]
    assert "since the last poll" not in page
    assert "sold since" not in page
    assert "<s>$2.50</s> <b>$2.75</b>" in page             # the tag now, and what the plan sets it to
    assert '"paid": "9.60"' in dashboard.to_json(snap)


def test_without_an_assignment_the_closet_still_counts_from_the_receipts(conn, page_conn, open_ledger):
    """No slot is ours without an assignment, but the closet isn't a slot: its 4 units are still there."""
    fulfilled(conn, "pr-1", "15", "COKE-20OZ", units_loaded=8, paid="9.60", to_back_stock=4,
              at=MONDAY - timedelta(days=3))
    store_poll(conn, "p1", (MONDAY - timedelta(days=4)).isoformat(), {"15": (250, 100)})
    store_poll(conn, "p2", (MONDAY - timedelta(days=2)).isoformat(), {"15": (250, 103)})
    conn.execute("delete from slot_assignment")
    snap = dashboard.snapshot(page_conn, MONDAY)
    assert snap["ours"]["ok"] is False
    assert snap["stock"]["ok"] is True
    assert snap["stock"]["data"]["slots"] == {}
    assert snap["stock"]["data"]["closet"] == [{"product": "COKE-20OZ", "units": 4, "unit_cost": "0.80"}]
    page = dashboard.render(snap)
    assert "Nothing in back stock" not in page
    assert "4 units of 1 product" in page


def test_a_fresh_database_is_ok_but_explains_every_empty_panel(page_conn):
    snap = dashboard.snapshot(page_conn, MONDAY)
    assert dashboard.verdict(snap["attention"]) == "ok"   # halted, no polls: worth knowing, not wrong
    assert {a["level"] for a in snap["attention"]} == {"info"}
    page = dashboard.render(snap)
    for words in ("No plan yet", "No live runs yet", "No tool calls yet", "No machine polls stored yet",
                  "None submitted yet", "Nothing in back stock"):
        assert words in page, words
    assert len(drawn(page)) == 20


def test_close_to_the_cutoff_halted_and_no_request_needs_checking(conn, page_conn):
    snap = dashboard.snapshot(page_conn, THURSDAY_NOON)
    texts = [a["text"] for a in snap["attention"] if a["level"] == "warn"]
    assert dashboard.verdict(snap["attention"]) == "check"
    assert any("halted" in t and "cutoff is in 11 h 59 min" in t for t in texts)
    assert any("No purchase request for Friday yet" in t for t in texts)
    # A request sent since last week's cutoff settles the second one.
    conn.execute("insert into purchase_requests (request_id, submitted_at, off_cycle, request_body)"
                 " values ('pr-9', %s, false, '{}')", (THURSDAY_NOON - timedelta(hours=2),))
    snap = dashboard.snapshot(page_conn, THURSDAY_NOON)
    assert not any("No purchase request" in a["text"] for a in snap["attention"])


def test_stale_polls_and_unsettled_catalog_requests_need_checking(conn, page_conn):
    store_poll(conn, "old", (MONDAY - timedelta(hours=5)).isoformat(), {"15": (250, 1)})
    conn.execute("insert into product_requests (name, size, words, status) values"
                 " ('Mountain Dew', '20 oz', '{mountain,dew,20,oz}', 'unknown')")
    items = dashboard.snapshot(page_conn, MONDAY)["attention"]
    warns = [a["text"] for a in items if a["level"] == "warn"]
    assert any("latest machine poll is 5 h old" in t for t in warns)
    assert any("Mountain Dew 20 oz" in t and "`unknown`" in t for t in warns)


def test_a_missing_poll_table_blanks_only_sales_and_stock(conn, page_conn, open_ledger):
    run = _live_run(conn)
    agent_runs.finish(conn, run, "submitted", plan=plan({"slot": "15", "keep": True}))
    conn.execute("drop table poll_selections, polls")
    snap = dashboard.snapshot(page_conn, MONDAY)
    assert snap["sales"]["ok"] is False and snap["stock"]["ok"] is False
    assert snap["plan"]["ok"] is True and snap["runs"]["ok"] is True
    assert any("Sales isn't available" in a["text"] for a in snap["attention"])
    assert "keep as is" in dashboard.render(snap)


def test_attention_text_from_the_database_is_escaped(conn, page_conn, open_ledger):
    conn.execute("insert into purchase_requests (request_id, submitted_at, off_cycle, request_body, status,"
                 " decision_note) values ('pr-x', now(), false, '{}', 'rejected', '<img src=x onerror=alert(1)>')")
    snap = dashboard.snapshot(page_conn)
    assert any("rejected" in a["text"] for a in snap["attention"])
    page = dashboard.render(snap)
    assert "<img src=x" not in page and "&lt;img src=x" in page


def test_state_json_carries_the_verdict_inputs(served):
    state = json.loads(get(served + "/state.json")[2])
    assert {a["level"] for a in state["attention"]} <= {"bad", "warn", "info"}
    assert {"sales", "stock", "spend", "recent_calls"} <= state.keys()


# --- v2: merged from the bakeoff, and the upright machine ------------------------------------------

def test_the_refresh_keeps_the_tab_title_current():
    # The in-place refresh swaps <main>; the verdict lives in <title>, so it must be copied too.
    assert "document.title = doc.title" in dashboard.SCRIPT


@pytest.mark.parametrize("halted, at, by, words, absent", [
    (False, None, None, "writes allowed", "by "),
    (False, MONDAY - timedelta(hours=3), None, "halt switched off <time", " by"),
    (False, None, "kevin", "halt switched off by kevin", "&mdash;"),
    (True, None, None, "no halt row: counts as halted", "switched on"),
])
def test_the_halt_switch_reads_cleanly_without_who_or_when(page_conn, halted, at, by, words, absent):
    snap = dashboard.snapshot(page_conn, MONDAY)
    snap["status"].update(halted=halted, halt_changed_at=at, halt_changed_by=by)
    cell = status_cells(snap)[0]      # the Agent cell
    assert words in cell and absent not in cell


def test_snapshot_is_repeatable_read_even_across_optional_readers(conn, page_conn, monkeypatch):
    # Every guarded read, even one that runs in a savepoint, sees the one read-only snapshot.
    seen = []
    original = dashboard._guarded

    def inspect(connection, read):
        seen.append((connection.execute("show transaction_isolation").fetchone()[0],
                     connection.execute("show transaction_read_only").fetchone()[0]))
        return original(connection, read)
    monkeypatch.setattr(dashboard, "_guarded", inspect)
    dashboard.snapshot(page_conn)
    assert len(seen) > 5 and set(seen) == {("repeatable read", "on")}


@pytest.mark.parametrize("table, column, broken", [
    ("polls", "crc_ok", {"sales"}),
    ("purchase_lines", "units_loaded", {"stock", "purchase_requests", "spend"}),
])
def test_optional_readers_roll_back_only_their_panel(conn, page_conn, open_ledger, table, column, broken):
    fulfilled(conn, "pr-1", "15", "COKE-20OZ", units_loaded=1, paid="1.00", at=MONDAY)
    conn.execute(f"alter table {table} drop column {column}")
    snap = dashboard.snapshot(page_conn)
    panels = ("plan", "runs", "activity", "recent_calls", "sales", "stock", "spend",
              "purchase_requests", "catalog_requests")
    assert {k for k in panels if not snap[k]["ok"]} == broken
    page = dashboard.render(snap)
    assert "Not available" in page or "isn&#x27;t available" in page
    assert len(drawn(page)) == len(dashboard.our_selections(snap)) == 20  # the machine still draws


EVEN = tuple(f"{r}{c}" for r in range(1, 6) for c in (2, 4, 6, 8))


def drawn(page: str) -> list[str]:
    """The selections "In the machine" draws as ours, in page order."""
    return re.findall(r'<article class="ma-lane(?: ma-changed)?" id="lane-(\w+)"', page)


@pytest.fixture
def even_columns(conn):
    """Our slots on every other column, so nothing assumes any particular columns. Only the stored
    assignment moves: the dashboard reads that, never a fixed list."""
    conn.execute("update slot_assignment set slots = %s", (list(EVEN),))
    return EVEN


def test_the_machine_lights_only_our_slots_from_slots(page_conn, even_columns):
    page = dashboard.render(dashboard.snapshot(page_conn, MONDAY))
    assert drawn(page) == list(EVEN) and page.count('class="ma-lane ma-notours"') == 20
    assert '<article class="ma-lane ma-notours" id="lane-15" aria-label="Selection 15: not ours">' in page
    assert "No plan yet" in page and '<p class="empty v-next">No plan yet' in page
    assert page.count("<b>?</b> no record yet") == 40       # no receipts: never a made-up count (Now and Next restock)
    assert 'class="seg" role="group" aria-label="Show" hidden' in page   # the toggle needs JS


def test_now_and_next_friday(conn, page_conn, even_columns, open_ledger):
    fulfilled(conn, "pr-1", "12", "COKE-20OZ", units_loaded=6, paid="7.20", at=MONDAY - timedelta(days=3))
    store_poll(conn, "p1", (MONDAY - timedelta(days=4)).isoformat(), {"12": (250, 10), "14": (200, 5)})
    store_poll(conn, "p2", (MONDAY - timedelta(hours=1)).isoformat(), {"12": (250, 12), "14": (200, 5)})
    first = _live_run(conn)
    agent_runs.finish(conn, first, "submitted", plan=plan({"slot": "12", "fill_to": 6, "sale_price": "2.50"},
                                                          {"slot": "14", "keep": True},
                                                          {"slot": "16", "fill_to": 4}))
    second = _live_run(conn)
    agent_runs.finish(conn, second, "submitted", plan=plan(
        {"slot": "12", "fill_to": 7, "sale_price": "2.75", "reason": "<i>sells out</i>"},
        {"slot": "14", "product": "MTDEW-20OZ", "fill_to": 4, "sale_price": "2.25", "reason": "try it"},
        {"slot": "16", "fill_to": 0, "reason": "dead"}))
    snap = dashboard.snapshot(page_conn, MONDAY)
    view = dashboard.machine_view(snap)
    assert view["12"]["now"] == {"product": "COKE-20OZ", "units": 4, "price": "2.50", "source": "receipt",
                                 "counted_at": None}
    assert view["14"]["now"] == {"product": None, "units": None, "price": "2.00", "source": None,
                                 "counted_at": None}    # no receipt or count: unknown
    assert view["12"]["next"]["units"] == 7 and view["12"]["next"]["old_price"] == "2.50"
    assert (view["12"]["change"]["kind"], view["14"]["change"]["kind"], view["16"]["change"]["kind"]) == \
        ("warn", "info", "bad")
    assert view["16"]["next"]["pulled"] is True
    assert view["18"]["next"]["in_plan"] is False and view["18"]["change"] is None
    page = dashboard.render(snap)
    assert '<s>$2.50</s> &rarr; <b>$2.75</b> from the next restock' in page   # the tag after it, old price struck
    assert '<p class="ma-nm">No record yet</p>' in page
    assert "&lt;i&gt;sells out&lt;/i&gt;" in page and "<i>sells out</i>" not in page
    assert '<span class="ma-chip bad">pulled</span>' in page
    assert "Stock is estimated from receipts and polls, not a sensor." in page
    assert "A plan is a proposal, not a restock receipt." in page
    assert "No balance or profit shown yet" not in page and 'id="money"' in page      # the money panel (#48)
    assert f"3 of 20 slots changed</b> since run #{first}" in page
    # The changed-only toggle: hidden until JS shows it, and the grid shows every slot without it.
    assert '<label class="only" hidden><input type="checkbox" id="changed-only">' in page
    assert 'class="plan-grid" data-only="false"' in page


@pytest.mark.parametrize("name, package", [
    ("COKE-20OZ", "20oz PET"), ("Sprite 20 oz bottle", "20oz PET"), ("Jarritos 16oz glass", "16oz glass"),
    ("Water 16 oz PET", "16oz PET"), ("Monster 16 oz can", "16oz can"), ("Snapple 16 oz", None), ("Coke 12 oz can", "12oz can"),
    ("Coke 12 oz", None), ("Red Bull 250ml", "240/250ml can"), ("Starbucks 14 fl oz", "14oz PET"),
    ("Gatorade 28 oz", None), ("MTDEW", None), (None, None),
])
def test_package_from_the_name(name, package):
    assert dashboard._package(name) == package


def test_a_photo_count_fills_in_what_receipts_dont_know(conn, page_conn):
    from tests.conftest import store_poll
    from vending import slot_counts
    store_poll(conn, "a", "2026-11-09T17:00:00+00:00", {"25": (400, 100)})
    store_poll(conn, "b", "2026-11-10T15:00:00+00:00", {"25": (400, 101)})
    slot_counts.record(conn, slot_counts.parse({
        "count_id": "p1", "counted_at": "2026-11-09T11:30:00-05:00", "source": "photo", "device_id": "vmc1",
        "selections": [{"selection": "25", "product": "Premier Protein <shake>", "units": 4, "price_on_tag": "4.00"}]}),
        "kevin")
    snap = dashboard.snapshot(page_conn)
    now = dashboard.machine_view(snap)["25"]["now"]
    assert (now["product"], now["units"], now["source"]) == ("Premier Protein <shake>", 3, "photo")
    page = dashboard.render(snap)
    assert "~3" in page and "Premier Protein &lt;shake&gt;" in page and "<shake>" not in page


def test_the_plan_agrees_with_the_machine_about_a_counted_slot(conn, page_conn, open_ledger):
    from vending import slot_counts
    slot_counts.record(conn, slot_counts.parse({
        "count_id": "p1", "counted_at": "2026-11-09T11:30:00-05:00", "source": "photo", "device_id": "vmc1",
        "selections": [{"selection": "25", "product": "Vita Coco", "units": 3}]}), "kevin")
    run = agent_runs.start(conn, mode="live")
    agent_runs.finish(conn, run, "submitted", plan=plan({"slot": "25", "keep": True}))
    page = dashboard.render(dashboard.snapshot(page_conn))
    assert "3 on hand of Vita Coco" in page


def _view(held, counted):
    snap = {"ours": {"ok": True, "data": ("15",)}, "plan": {"ok": False}, "sales": {"ok": False},
            "stock": {"ok": True, "data": {"slots": {"15": held}}},
            "counts": {"ok": True, "data": {"15": counted}}}
    return dashboard.machine_view(snap)["15"]["now"]


def test_a_refill_after_a_count_beats_the_count():
    # Review on #47: COKE loaded 1 Nov, counted 1 on 9 Nov, refilled 13 Nov to 8 on hand.
    utc = timezone.utc
    held = {"product": "COKE-20OZ", "on_hand": 8, "since": datetime(2026, 11, 1, tzinfo=utc),
            "last_moved": datetime(2026, 11, 13, tzinfo=utc)}
    counted = {"product": "COKE-20OZ", "on_hand": 1, "source": "photo", "price_on_tag": "2.50",
               "counted_at": datetime(2026, 11, 9, tzinfo=utc)}
    assert (_view(held, counted)["units"], _view(held, counted)["source"]) == (8, "receipt")
    held["last_moved"] = datetime(2026, 11, 5, tzinfo=utc)          # no refill since the count
    assert (_view(held, counted)["units"], _view(held, counted)["source"]) == (1, "photo")


def test_last_moved_is_the_latest_load_not_the_last_product_change():
    from fractions import Fraction

    from vending.stock import Movement, first_in_first_out
    utc = timezone.utc
    first, refill = datetime(2026, 11, 1, tzinfo=utc), datetime(2026, 11, 13, tzinfo=utc)
    found = first_in_first_out([
        Movement(first, "purchase", "15", "COKE-20OZ", to_slot=6, paid_cents=Fraction(600)),
        Movement(refill, "purchase", "15", "COKE-20OZ", to_slot=2, paid_cents=Fraction(200))])
    (slot,) = found.slots
    assert (slot.since, slot.last_moved) == (first, refill)


def test_a_price_written_two_ways_is_not_a_change():
    assert dashboard._what({"slot": "15", "sale_price": "2.5"}) == dashboard._what({"slot": "15", "sale_price": "2.50"})
    assert dashboard._what({"slot": "15", "sale_price": "2.5"}) != dashboard._what({"slot": "15", "sale_price": "2.75"})


def test_the_count_tooltip_gives_the_eastern_date():
    late = datetime(2026, 11, 10, 2, 0, tzinfo=timezone.utc)        # 9 Nov, 21:00 Eastern
    assert f"{dashboard._eastern(late):%d %b}" == "09 Nov"


def test_runs_chips_read_a_keep_against_the_tag_like_the_plan_panel(conn, page_conn, open_ledger):
    store_poll(conn, "p1", (MONDAY - timedelta(hours=1)).isoformat(), {"28": (250, 10)})
    a = _live_run(conn)
    agent_runs.finish(conn, a, "submitted", plan=plan({"slot": "28", "keep": True}))
    b = _live_run(conn)
    agent_runs.finish(conn, b, "submitted", plan=plan({"slot": "28", "fill_to": 6, "sale_price": "3.00"}))
    runs = {r["id"]: r for r in dashboard.snapshot(page_conn, MONDAY)["runs"]["data"]}
    # Without the tag this read "set $3.00" here and "$2.50 to $3.00" in the Plan panel.
    assert runs[b]["changes"] == [{"slot": "28", "labels": ["$2.50 to $3.00"]}]


@pytest.mark.parametrize("warning, kept", [
    ("slot 16: the polls show 2 more sales", True),
    ("slot 15: the polls show 2 more sales", False),
    ("slots 15-17 sold across a change", False),      # a range reaching a slot not ours
    ("slots 12, 16 sold across a change", False),
    ("slot 16 and 15 were pulled", False),
    ("slot 16 and slot 18 were pulled", True),
    ("the slots were miscounted", False),             # names a slot we can't read
    ("loaded 4 units of X from the closet but we counted only 2 there", True),
])
def test_only_ours_reads_plural_list_and_range_slot_names_in_warnings(warning, kept):
    """The text check, behind the slot each warning carries (here one that is ours, or None for the closet)."""
    from vending import dashboard
    from vending.stock import StockWarning

    warning = StockWarning(warning, None)
    snap = {"ours": {"ok": True, "data": ["16", "18"]}, "counts": {"ok": False},
            "stock": {"ok": True, "data": {"slots": {}, "warnings": [warning]}}}
    dashboard.only_ours(snap)
    assert (snap["stock"]["data"]["warnings"] == [warning]) is kept


@pytest.mark.parametrize("text, slot, kept", [
    ("Slot 11: the polls show 2 more sales", "11", False),     # the wordings the review found slipping through
    ("selection 11 oversold", "11", False),
    ("slots 27-11 sold across a change", "27", False),
    ("r1 line 2: 4 units of X lost in the closet but we counted only 2", None, True),   # the closet is no selection
    ("r1 line 2: loaded 4 units of X from the closet but we counted only 2 there", "16", True),
    ("r1 line 2: loaded 4 units of X from the closet but we counted only 2 there", "15", False),
    ("slot 16: but it says slot 15 too", "16", False),          # the text is still checked
    ("Slot 11 x", "16", False),                                 # the text check is case-blind
    ("selection 11 oversold", "16", False),
    ("selections 18-15 sold across a change", "16", False),    # a reversed range is read too
])
def test_only_ours_goes_by_the_slot_the_stock_warning_carries(text, slot, kept):
    from vending import dashboard
    from vending.stock import StockWarning

    warning = StockWarning(text, slot)
    snap = {"ours": {"ok": True, "data": ["16", "18"]}, "counts": {"ok": False},
            "stock": {"ok": True, "data": {"slots": {}, "warnings": [warning]}}}
    dashboard.only_ours(snap)
    assert (snap["stock"]["data"]["warnings"] == [warning]) is kept


def test_every_stock_warning_carries_its_slot():
    from vending.stock import Movement, StockWarning, replay

    T = datetime(2026, 11, 2, 15, tzinfo=timezone.utc)
    found = replay([Movement(at=T, kind="pull", slot="27", sku="COKE20", units=3, ref="r1 line 1")])
    assert found.warnings and all(isinstance(w, StockWarning) and w.slot == "27" for w in found.warnings)



def test_a_warning_with_no_slot_attached_is_never_public():
    from vending import dashboard

    snap = {"ours": {"ok": True, "data": ["16", "18"]}, "counts": {"ok": False},
            "stock": {"ok": True, "data": {"slots": {}, "warnings": ["slot 16: plain text", "no slot named"]}}}
    dashboard.only_ours(snap)
    assert snap["stock"]["data"]["warnings"] == []


def test_a_stock_warning_keeps_its_slot_through_copies():
    import copy
    import pickle

    from vending.stock import StockWarning

    w = StockWarning("slot 16: x", "16")
    for again in (copy.copy(w), copy.deepcopy(w), pickle.loads(pickle.dumps(w))):
        assert again == "slot 16: x" and again.slot == "16"


# --- The machine close-up (vending.machine_art) ------------------------------------------------------

def catalog(conn, *rows):
    """Catalog products as the sync stores them: (sku, name, line, flavor, size)."""
    for sku, name, line, flavor, size in rows:
        conn.execute("insert into catalog_products (sku, name, line, flavor, size, last_response)"
                     " values (%s, %s, %s, %s, %s, '{}')", (sku, name, line, flavor, size))


def lane_html(page: str, slot: str) -> str:
    """One lane's card, from its opening tag to the next lane's."""
    start = page.index(f'id="lane-{slot}"')
    end = page.find("<article", start)
    return page[start:end if end != -1 else len(page)]


def shows(text: str, figure: str) -> bool:
    """Whether `text` shows `figure` (a sentinel price or count) on its own: not inside a longer number,
    such as the seconds of a timestamp ("02:54:58.766051" holds "8.76"). A colon counts as a
    boundary only after a digit (a clock), so compact JSON ("sold":4321) is still caught."""
    return re.search(rf"(?<![\d.])(?<!\d:){re.escape(figure)}(?![\d])", text) is not None


def test_a_timestamp_s_seconds_dont_read_as_a_sentinel_figure():
    # A page's own clock ("02:54:58.766051") holds "8.76"; that isn't the other team's price.
    assert not shows('<time datetime="2026-11-20T02:54:58.766051+00:00">', "8.76")
    assert not shows("run 14321 steps", "4321") and not shows("19.875", "9.87")
    # The figure on its own, in a cell, a price, JSON or a sentence, still shows.
    for text, figure in (("<td>8.76</td>", "8.76"), ("$8.76", "8.76"), ('"price": "8.76"', "8.76"), ('"sold":4321', "4321"),
                         ("8.76", "8.76"), ("tag 4321 units", "4321"), ("(9.87)", "9.87")):
        assert shows(text, figure), text


def test_nothing_about_a_selection_that_is_not_ours_reaches_the_page_or_its_json(conn, page_conn, open_ledger):
    # 12 isn't ours (ours are columns 5 to 8): a receipt, a catalog row, a count, a poll and a plan entry name it.
    catalog(conn, ("ZZ-RIVAL-SKU", "Zebra Fizz Rival", "Zebra Fizz", "Mango", "16 oz glass bottle"),
            ("COKE-20OZ", "Coca-Cola", "Coca-Cola", None, "20 oz bottle"))
    fulfilled(conn, "pr-12", "12", "ZZ-RIVAL-SKU", units_loaded=6, paid="5.55", at=MONDAY - timedelta(days=3))
    fulfilled(conn, "pr-15", "15", "COKE-20OZ", units_loaded=5, paid="6.00", at=MONDAY - timedelta(days=3))
    store_poll(conn, "p1", (MONDAY - timedelta(hours=2)).isoformat(), {"12": (987, 4321), "15": (250, 10)})
    from vending import slot_counts
    slot_counts.record(conn, slot_counts.parse({
        "count_id": "c1", "counted_at": (MONDAY - timedelta(hours=1)).isoformat(), "source": "photo",
        "device_id": "vmc1", "selections": [{"selection": "12", "product": "Quokka Cola Secret", "units": 7,
                                             "price_on_tag": "8.76"}]}), "kevin")
    run = _live_run(conn)
    agent_runs.finish(conn, run, "submitted", plan=plan({"slot": "12", "product": "ZZ-RIVAL-SKU", "fill_to": 9},
                                                        {"slot": "15", "fill_to": 7}))
    snap = dashboard.snapshot(page_conn, MONDAY, public=True)
    page, state = dashboard.render(snap), dashboard.to_json(snap)
    assert snap["products"]["ok"] and list(snap["products"]["data"]["catalog"]) == ["COKE-20OZ"]
    # (The request's own total, $5.55, stays in Requests: that is our money, not a lane.)
    for leak in ("ZZ-RIVAL", "Zebra", "Mango", "Quokka"):
        assert leak not in page and leak not in state, leak
    for figure in ("9.87", "8.76", "4321"):
        assert not shows(page, figure) and not shows(state, figure), figure
    card = lane_html(page, "12")
    assert 'class="ma-lane ma-notours"' in page and "Not ours" in card and "<svg" not in card
    assert "+2 at restock" in lane_html(page, "15")        # ours is drawn: fill 7 less 5 on hand


def test_a_selection_reassigned_away_stops_being_drawn(conn, page_conn, open_ledger):
    catalog(conn, ("COKE-20OZ", "Coca-Cola Twenty", "Coca-Cola", None, "20 oz bottle"))
    fulfilled(conn, "pr-15", "15", "COKE-20OZ", units_loaded=5, paid="6.00", at=MONDAY - timedelta(days=3))
    before = dashboard.render(dashboard.snapshot(page_conn, MONDAY, public=True))
    assert "Coca-Cola Twenty" in lane_html(before, "15")
    conn.execute("update slot_assignment set slots = %s", ([s for s in EVEN],))   # 15 is no longer ours
    snap = dashboard.snapshot(page_conn, MONDAY, public=True)
    after, state = dashboard.render(snap), dashboard.to_json(snap)
    assert '<article class="ma-lane ma-notours" id="lane-15"' in after
    assert "Coca-Cola Twenty" not in after and "COKE-20OZ" not in state


def test_units_capacity_and_back_friday_follow_the_rules(conn, page_conn, open_ledger):
    catalog(conn, ("COKE-20OZ", "Coca-Cola", "Coca-Cola", None, "20 oz bottle"),
            ("SHAKE-11CTN", "Protein shake", "Premier Protein", "Chocolate", "11 oz carton"),
            ("CAN-12", "Plain can", "Plain", None, "12 oz can"))
    week = MONDAY - timedelta(days=3)
    fulfilled(conn, "a", "15", "COKE-20OZ", units_loaded=5, paid="6", at=week)    # 5 of 7, fill 7: +2
    fulfilled(conn, "b", "16", "SHAKE-11CTN", units_loaded=2, paid="6", at=week)  # carton: no capacity
    fulfilled(conn, "c", "17", "CAN-12", units_loaded=6, paid="6", at=week)       # fill 4 below 6: nothing to add
    fulfilled(conn, "d", "18", "CAN-12", units_loaded=3, paid="6", at=week)       # not in the plan
    run = _live_run(conn)
    agent_runs.finish(conn, run, "submitted", plan=plan(
        {"slot": "15", "fill_to": 7}, {"slot": "16", "fill_to": 5}, {"slot": "17", "fill_to": 4},
        {"slot": "25", "product": "CAN-12", "fill_to": 8}))
    page = dashboard.render(dashboard.snapshot(page_conn, MONDAY, public=True))
    coke, shake, can, out, unknown = (lane_html(page, s) for s in ("15", "16", "17", "18", "25"))
    assert "<b>5</b> of 7" in coke and "+2 at restock" in coke and "+2 added" in coke
    assert coke.count('class="ma-ghost ma-go"') == 2 and coke.count('class="ma-fill"') == 2
    # No capacity: no empty slots beyond what the plan fills, and it says so.
    assert "capacity not counted yet" in shake and "+3 at restock" in shake
    assert shake.count("ma-ghost") == 3 and "<b>2</b> on hand" in shake
    assert "plan 4, nothing to add" in can and "+" not in can.split("ma-chips")[1].split("</p>")[0]
    assert "not in plan" in out
    # 25 holds no record: a product the plan puts in, never a made-up count before it.
    assert "<b>?</b> no record yet" in unknown and "new product at restock" in unknown
    # A lane with a product but no count says ? and +? (photo count of a product with no units).
    view = {"15": {"now": {"product": "COKE-20OZ", "units": None, "price": "2.50", "source": "receipt",
                           "counted_at": None},
                   "next": {"in_plan": True, "product": "COKE-20OZ", "units": 7, "price": "2.50", "old_price": None,
                            "pulled": False, "fill_to": 7}, "change": None, "fill_to": 7}}
    products = {"catalog": {"COKE-20OZ": {"name": "Coke", "line": "Coca-Cola", "flavor": None,
                                          "size": "20 oz bottle"}}, "overrides": {}}
    html = dashboard.machine_art.closeup_html(view, products, True)
    assert "<b>?</b> of 7" in html and "+? at restock" in html and "filled to 7" in html
    assert "ma-unk" in html and "units unknown" in html


def test_the_current_plan_checks_underfills_and_shows_the_reason(conn, page_conn, open_ledger):
    under, full, unknown, same, pull, keep = TEST_SLOTS[:6]
    catalog(conn, ("CAN-12", "Plain can", "Plain", None, "12 oz can"),
            ("BOTTLE-20", "Test bottle", "Plain", None, "20 oz bottle"),
            ("CARTON-11", "Test shake", "Plain", None, "11 oz carton"))
    set_value(conn, "capacity_overrides", {"BOTTLE-20": 8}, "test")
    fulfilled(conn, "held", same, "CAN-12", units_loaded=2, paid="6", at=MONDAY - timedelta(days=3))
    run = _live_run(conn)
    agent_runs.finish(conn, run, "submitted", plan=plan(
        {"slot": under, "product": "BOTTLE-20", "fill_to": 6, "reason": "Budget < limit"},
        {"slot": full, "product": "CAN-12", "fill_to": 8},
        {"slot": unknown, "product": "CARTON-11", "fill_to": 6},
        {"slot": same, "fill_to": 7},
        {"slot": pull, "product": "CAN-12", "fill_to": 0},
        {"slot": keep, "keep": True, "product": "CAN-12", "fill_to": 6}))
    snap = dashboard.snapshot(page_conn, MONDAY, public=True)
    checks = {s["slot"]: s.get("fill_check") for s in snap["plan"]["data"]["slots"]}
    assert checks[under] == "fills 6 of 8: 2 spaces empty"
    assert checks[same] == "fills 7 of 8: 1 space empty"
    assert checks[unknown] == "capacity not counted"
    assert checks[full] is checks[pull] is checks[keep] is None
    assert dashboard.verdict(snap["attention"]) == "check"
    warnings = [item["text"] for item in snap["attention"] if item["level"] == "warn"]
    assert f"Slot {under} fills 6 of 8: 2 spaces empty. Budget < limit" in warnings
    assert f"Slot {same} fills 7 of 8: 1 space empty." in warnings
    assert not any("capacity not counted" in w for w in warnings)
    page = dashboard.render(snap)
    row = page.split(f'id="slot-{under}"', 1)[1].split("</summary>", 1)[0]
    assert "fills 6 of 8: 2 spaces empty" in row and "Budget &lt; limit" in row
    row = page.split(f'id="slot-{unknown}"', 1)[1].split("</summary>", 1)[0]
    assert "capacity not counted" in row


def test_a_swap_and_a_price_change_on_friday(conn, page_conn, open_ledger):
    catalog(conn, ("VW-20", "vitaminwater", "vitaminwater", "dark", "20 oz bottle"),
            ("TOPO-16G", "Topo Chico", "Topo Chico", None, "16 oz glass bottle"),
            ("BUB-12", "bubly", "bubly", "Strawberry", "12 oz can"),
            ("RB-250", "Red Bull", "Red Bull", None, "250 ml can"))
    week = MONDAY - timedelta(days=3)
    fulfilled(conn, "a", "15", "VW-20", units_loaded=2, paid="6", at=week)
    fulfilled(conn, "b", "16", "BUB-12", units_loaded=3, paid="6", at=week)
    fulfilled(conn, "c", "17", "RB-250", units_loaded=6, paid="6", at=week)
    store_poll(conn, "p", (MONDAY - timedelta(hours=1)).isoformat(), {"15": (300, 1), "16": (100, 1), "17": (300, 1)})
    first = _live_run(conn)
    agent_runs.finish(conn, first, "submitted", plan=plan({"slot": "15", "fill_to": 6}, {"slot": "16", "fill_to": 8},
                                                          {"slot": "17", "fill_to": 6}))
    reason = "Topo Chico sold out twice; <b>2 old</b> units come out\nand 7 go in. " + "x" * 200
    second = _live_run(conn)
    agent_runs.finish(conn, second, "submitted", plan=plan(
        {"slot": "15", "product": "TOPO-16G", "fill_to": 7, "reason": reason},
        {"slot": "16", "fill_to": 8, "sale_price": "1.25", "reason": "sold out 3 Fridays running"},
        {"slot": "17", "fill_to": 0, "reason": "slow"}))
    page = dashboard.render(dashboard.snapshot(page_conn, MONDAY, public=True))
    swap, price, pulled = lane_html(page, "15"), lane_html(page, "16"), lane_html(page, "17")
    # The swap draws the new package and look at the next restock, chips "swap", and gives the reason on one line.
    assert '<g class="v-now">' in swap and '<g class="v-next ma-swapin">' in swap
    assert "#ma-p-bottle-20" in swap and "#ma-p-bottle-16-glass" in swap
    assert '<span class="ma-chip info">swap</span>' in swap and "swap at restock" in swap
    assert '<p class="ma-nm v-next">Topo Chico' in swap
    why = swap.split('<p class="ma-why"')[1].split("</p>")[0]
    assert "&lt;b&gt;2 old&lt;/b&gt;" in why and "<b>2 old</b>" not in why and "\n" not in why.split(">", 1)[1]
    assert why.endswith("…") and len(why.split(">", 1)[1]) < 200
    # The price change: the tag after the restock strikes the old price, and the reason sits under it.
    assert "<s>$1.00</s> &rarr; <b>$1.25</b> from the next restock" in price and ">sold out 3 Fridays running</p>" in price
    assert 'stroke="#d6232a"' in price and ">$1.25</text>" in price
    assert '<span class="ma-chip bad">pulled</span>' in pulled and "<b>0</b> pulled" in pulled


def test_the_close_up_survives_a_failed_products_read(conn, page_conn, open_ledger, monkeypatch):
    catalog(conn, ("COKE-20OZ", "Coca-Cola", "Coca-Cola", None, "20 oz bottle"))
    fulfilled(conn, "a", "15", "COKE-20OZ", units_loaded=5, paid="6", at=MONDAY - timedelta(days=3))
    slot = TEST_SLOTS[0]
    run = _live_run(conn)
    agent_runs.finish(conn, run, "submitted", plan=plan({"slot": slot, "product": "COKE-20OZ", "fill_to": 6}))

    def broken(connection, skus):
        connection.execute("select 1 / 0")     # any database error inside the read
    monkeypatch.setattr(dashboard.machine_art, "read_products", broken)
    snap = dashboard.snapshot(page_conn, MONDAY, public=True)
    assert snap["products"] == {"ok": False, "why": dashboard.PUBLIC_PANEL_ERROR}
    row = next(row for row in snap["plan"]["data"]["slots"] if row["slot"] == slot)
    assert "fill_check" not in row
    assert not any(item["level"] == "warn" for item in snap["attention"])
    assert snap["stock"]["ok"] and snap["catalog_requests"]["ok"]   # the transaction carried on
    page = dashboard.render(snap)
    lane = lane_html(page, "15")
    assert "<b>5</b> on hand" in lane and "capacity not counted yet" in lane and "#ma-p-bottle-20" in lane
    assert "Packages are approximate" in page and "division" not in page


def test_fill_checks_skip_first_fill_rows(conn, page_conn, open_ledger, monkeypatch):
    weekly, first_fill = TEST_SLOTS[:2]
    catalog(conn, ("CAN-12", "Plain can", "Plain", None, "12 oz can"))
    run = _live_run(conn)
    agent_runs.finish(conn, run, "submitted", plan=plan({"slot": weekly, "product": "CAN-12", "fill_to": 6}))
    weekly_plan = dashboard._plan
    saved_row = {"slot": first_fill, "product": "Plain can", "units": 4,
                 "fill_check": "fills 4 of 8: 4 spaces empty"}

    def combined_plan(connection, ours, now, assignment_at):
        result = weekly_plan(connection, ours, now, assignment_at)
        result["slots"] = [dict(saved_row) if row["slot"] == first_fill else row for row in result["slots"]]
        return result

    monkeypatch.setattr(dashboard, "_plan", combined_plan)
    snap = dashboard.snapshot(page_conn, MONDAY, public=True)
    rows = {row["slot"]: row for row in snap["plan"]["data"]["slots"]}
    assert rows[first_fill] == saved_row
    assert rows[weekly]["fill_check"] == "fills 6 of 8: 2 spaces empty"
    assert [item["text"] for item in snap["attention"] if item["level"] == "warn"] == [
        f"Slot {weekly} fills 6 of 8: 2 spaces empty."]


def test_a_missing_catalog_table_only_makes_the_close_up_approximate(conn, page_conn, open_ledger):
    conn.execute("drop table sourcing_options")
    conn.execute("drop table catalog_products")
    fulfilled(conn, "a", "15", "MYSTERY", units_loaded=5, paid="6", at=MONDAY - timedelta(days=3))
    snap = dashboard.snapshot(page_conn, MONDAY, public=True)
    assert snap["products"]["ok"] is False and snap["stock"]["ok"]
    lane = lane_html(dashboard.render(snap), "15")
    assert "<b>5</b> on hand" in lane and "#ma-p-can-12" in lane     # the generic can


def test_the_close_up_escapes_everything_it_prints():
    from vending import machine_art
    view = {"55": {"now": {"product": "<img src=x>", "units": 3, "price": "<b>1</b>", "source": "photo",
                           "counted_at": MONDAY},
                   "next": {"in_plan": True, "product": "EVIL", "units": 4, "price": "1.00", "old_price": "<i>",
                            "pulled": False, "fill_to": 4},
                   "change": {"labels": ["swap"], "kind": "info", "reason": "<script>alert(1)</script>"},
                   "fill_to": 4}}
    products = {"catalog": {"EVIL": {"name": "\"><svg onload=x>", "line": "<l>", "flavor": "<f>",
                                     "size": "12 oz can"}}, "overrides": {}}
    html = machine_art.closeup_html(view, products, True)
    for raw in ("<img src=x>", "<script>", "<b>1</b>", "<svg onload", "<l>", "<f>", "<i>"):
        assert raw not in html, raw
    assert "&lt;img src=x&gt;" in html and "&lt;script&gt;" in html and "named from a photo count" in html


def test_two_skus_with_one_name_are_a_swap_not_a_refill():
    from vending import machine_art
    view = {"55": {"now": {"product": "BUBLY-STRAW", "units": 3, "price": "1.00", "source": "receipt",
                           "counted_at": None},
                   "next": {"in_plan": True, "product": "BUBLY-MANGO", "units": 8, "price": "1.00",
                            "old_price": None, "pulled": False, "fill_to": 8},
                   "change": {"labels": ["swap"], "kind": "info", "reason": None}, "fill_to": 8}}
    catalog = {sku: {"name": "Bubly", "line": "bubly", "flavor": flavor, "size": "12 oz can"}
               for sku, flavor in (("BUBLY-STRAW", "Strawberry"), ("BUBLY-MANGO", "Mango"))}
    lane = machine_art.Lane("55", view["55"], {"catalog": catalog, "overrides": {}})
    assert lane.swap and lane.back is None and lane.added == 8


def test_a_lane_with_no_known_size_draws_no_full_looking_gauge():
    from vending import machine_art
    view = {"55": {"now": {"product": "MILK-CARTON", "units": 3, "price": None, "source": "receipt",
                           "counted_at": None},
                   "next": None, "change": None, "fill_to": None}}
    products = {"catalog": {"MILK-CARTON": {"name": "Milk", "line": None, "flavor": None, "size": "11 oz carton"}},
                "overrides": {}}
    lanes = {"55": machine_art.Lane("55", view["55"], products)}
    front = machine_art.front_html(lanes, {"55"})
    assert lanes["55"].cap is None
    assert '<i class="v-now"' not in front and 'class="fc-g open"' in front


@pytest.mark.parametrize("value, shown", [
    (Fraction(5, 4), "$1.25"), (Fraction(1, 3), "$0.33"), (Fraction(5, 2), "$2.50"), ("2.5", "$2.50"),
    (Decimal("1.255"), "$1.255"),
])
def test_close_up_prices_are_exact_dollars(value, shown):
    from vending import machine_art
    assert machine_art.money(value) == shown


def test_a_shelf_with_nothing_planned_says_so_rather_than_plus_zero():
    from vending import machine_art
    lane = {"product": "COKE", "units": None, "price": None, "source": "receipt", "counted_at": None}
    view = {"41": {"now": lane, "next": None, "change": None, "fill_to": None},
            "51": {"now": dict(lane, units=3), "next": {"in_plan": True, "product": "COKE", "units": 3,
                                                        "price": None, "old_price": None, "pulled": False,
                                                        "fill_to": 3},
                   "change": None, "fill_to": 3}}
    html = machine_art.closeup_html(view, {"catalog": {}, "overrides": {}}, True)
    shelf4 = html[html.index('id="rail-4"'):html.index("</a>", html.index('id="rail-4"'))]
    shelf5 = html[html.index('id="rail-5"'):html.index("</a>", html.index('id="rail-5"'))]
    assert "? on hand &middot; no restock plan" in shelf4 and "+0" not in shelf4
    assert '<span class="ma-fr">+0</span> at restock' in shelf5      # planned, and already full


@pytest.mark.parametrize("size, key", [
    ("12 oz can", "can-12"), ("12 fl oz sleek can", "can-12-sleek"), ("16 oz can", "can-16"),
    ("250 ml can", "can-250ml"), ("8.4 oz can", "can-250ml"), ("7.5 oz can", "can-7.5"),
    ("20 oz bottle", "bottle-20"), ("16 oz bottle", "bottle-16"), ("16 oz glass bottle", "bottle-16-glass"),
    ("16.9 oz bottle", "bottle-16.9"), ("14 oz bottle", "bottle-14"), ("11 oz carton", "carton-11"),
    ("1 L bottle", "bottle-16"), ("2 liter bottle", "bottle-16"), ("large", "can-12"), (None, "can-12"),
])
def test_a_package_shape_comes_from_the_catalog_size(size, key):
    assert dashboard.machine_art.shape_for_size(size)[0] == key


def test_a_product_look_falls_back_to_a_steady_hash():
    from vending import machine_art
    assert machine_art.look_for("ANY", "Bubly", "Strawberry")["c1"] == "#94c93a"     # by line and flavor
    assert machine_art.look_for("ANY", "bubly", "Mango")["label"] == "bubly"          # by line
    new = machine_art.look_for("NEW-SKU-1", "Brand New Line", None)
    assert new == machine_art.look_for("NEW-SKU-1", "Brand New Line", None) != machine_art.look_for("NEW-SKU-2")
    assert new["label"] == "Brand"


@pytest.mark.parametrize("value, text", [("2.5", "$2.50"), (Decimal("1.25"), "$1.25"), ("3", "$3.00"),
                                         ("1.255", "$1.255"), (None, None)])
def test_tag_prices_are_exact_text(value, text):
    assert dashboard.machine_art.money(value) == text


# --- catalog requests and the worker's syncs --------------------------------------------------------

def catalog_request(conn, name, status, at, sku=None, note=None):
    conn.execute("insert into product_requests (requested_at, name, size, words, status, sku, decision_note)"
                 " values (%s, %s, '12 oz can', %s, %s, %s, %s)",
                 (at, name, name.lower().split(), status, sku, note))


def test_every_catalog_request_shows_with_counts_and_the_places_left(conn, page_conn):
    # 40 requests, the oldest first: a `sending` one far past the old limit of 10, then
    # 22 pending, 14 approved, 2 rejected and an `unknown`.
    statuses = ["sending"] + ["pending"] * 22 + ["approved"] * 14 + ["rejected"] * 2 + ["unknown"]
    for i, status in enumerate(statuses):
        sku = "DRPEP-12OZ-CAN" if (status, i) == ("approved", 30) else None
        catalog_request(conn, f"Drink {i:02d}", status, MONDAY - timedelta(hours=len(statuses) - i), sku=sku,
                        note="added as Dr Pepper 12 oz" if sku else None)
    snap = dashboard.snapshot(page_conn, MONDAY)
    rows = snap["catalog_requests"]["data"]
    assert len(rows) == 40 and rows[0]["name"] == "Drink 39" and rows[-1]["name"] == "Drink 00"
    counts = snap["catalog_counts"]["data"]
    assert counts["by_status"] == {"pending": 22, "approved": 14, "rejected": 2, "sending": 1, "unknown": 1}
    assert (counts["total"], counts["open"], counts["cap"], counts["left"], counts["unsettled"]) == (40, 22, 25, 3, 2)
    assert dashboard.OPEN_REQUEST_CAP == 25
    # Both unsettled rows need checking, the old one too.
    warns = [a["text"] for a in snap["attention"] if a["level"] == "warn"]
    assert any("Drink 00" in t and "`sending`" in t for t in warns)
    assert any("Drink 39" in t and "`unknown`" in t for t in warns)
    page = dashboard.render(snap)
    assert ('40 requests: 22 pending, 14 approved, 2 rejected, <span class="warn-text">1 sending</span>, '
            '<span class="warn-text">1 unknown</span>.') in page
    assert "<b>Open: 22 of about 25</b>" in page
    assert "3 places left, fewer if any of the 2 sending or unknown reached the bridge" in page
    panel = page.split("<h3>Catalog requests</h3>", 1)[1]
    assert all(f"<b>Drink {i:02d}</b>" in panel for i in range(40))
    # An approved request shows its SKU, which may not be the name's; one without says so.
    assert "SKU <code>DRPEP-12OZ-CAN</code>" in page and "added as Dr Pepper 12 oz" in page
    assert page.count("no SKU recorded yet") == 13
    assert '"sku": "DRPEP-12OZ-CAN"' in dashboard.to_json(snap)


@pytest.mark.parametrize("pending", [dashboard.OPEN_REQUEST_CAP, dashboard.OPEN_REQUEST_CAP + 1])
def test_at_the_cap_no_places_are_left(conn, page_conn, pending):
    for i in range(pending):
        catalog_request(conn, f"Drink {i:02d}", "pending", MONDAY - timedelta(minutes=i))
    catalog_request(conn, "Drink unsure", "unknown", MONDAY - timedelta(hours=2))
    snap = dashboard.snapshot(page_conn, MONDAY)
    assert (snap["catalog_counts"]["data"]["open"], snap["catalog_counts"]["data"]["left"]) == (pending, 0)
    page = dashboard.render(snap)
    assert f'<b>Open: {pending} of about 25</b>' in page and '<span class="bad-text">0 places left</span>' in page
    assert "fewer if any" not in page


def test_no_catalog_requests_still_shows_the_counts(page_conn):
    snap = dashboard.snapshot(page_conn, MONDAY)
    page = dashboard.render(snap).split("<h3>Catalog requests</h3>", 1)[1]
    assert "0 requests: 0 pending, 0 approved, 0 rejected." in page and "<b>Open: 0 of about 25</b>" in page
    assert "25 places left" in page and "None yet." in page


def test_a_missing_catalog_column_blanks_its_counts_too(conn, page_conn):
    conn.execute("alter table product_requests drop column sku")
    snap = dashboard.snapshot(page_conn, MONDAY)
    assert snap["catalog_requests"]["ok"] is False and snap["catalog_counts"]["ok"] is False
    assert snap["purchase_requests"]["ok"] is True
    dashboard.render(snap)


def test_a_missing_catalog_table_blanks_only_its_panel(conn, page_conn):
    conn.execute("drop table product_requests cascade")   # the request backlog (#140) references it
    snap = dashboard.snapshot(page_conn, MONDAY)
    assert snap["catalog_requests"]["ok"] is False and snap["catalog_counts"]["ok"] is False
    assert snap["purchase_requests"]["ok"] is True
    dashboard.render(snap)


def syncs(*results, rounds=1):
    """The worker's /health, as `vending.worker.State.public` builds it."""
    from vending import worker

    state = worker.State(rounds=rounds, last_round_at=(MONDAY - timedelta(minutes=12)).isoformat())
    state.results = [worker.JobResult(job, MONDAY.isoformat(), 1.0, code, outcome, "secret error text")
                     for job, code, outcome in results]
    return state.public()


def test_failing_syncs_name_each_job_and_what_its_exit_code_means(page_conn):
    failing = syncs(("sync_history", 2, "finished"), ("sync_requests", 0, "finished"),
                    ("sync_catalog", None, "timed out"))
    snap = dashboard.snapshot(page_conn, MONDAY, syncs=failing)
    bad = [a["text"] for a in snap["attention"] if a["level"] == "bad"]
    assert bad == ["The worker's latest hourly sync (12 min ago) failed: `sync_history` exit 2, "
                   "`sync_catalog` timed out. Exit 2: the database is missing migrations; run "
                   "`python -m vending.db migrate`. See `/health`."]
    assert dashboard.verdict(snap["attention"]) == "problem"
    page = dashboard.render(snap)
    assert "<code>sync_history</code> exit 2" in page and "<code>python -m vending.db migrate</code>" in page
    assert "sync_requests" not in bad[0]
    assert snap["syncs"]["results"][0]["exit_code"] == 2 and "secret error text" not in dashboard.to_json(snap)


def test_a_sync_that_finished_with_problems_is_a_problem_too(page_conn):
    snap = dashboard.snapshot(page_conn, MONDAY, syncs=syncs(("sync_history", 1, "finished")))
    bad = [a["text"] for a in snap["attention"] if a["level"] == "bad"]
    assert len(bad) == 1 and "`sync_history` exit 1" in bad[0] and "finished with problems" in bad[0]
    assert dashboard.verdict(snap["attention"]) == "problem"


@pytest.mark.parametrize("given", [
    None,                                                       # run on its own, not by the worker
    syncs(rounds=0),                                            # the first round hasn't finished
    syncs(("sync_history", 0, "finished"), ("sync_catalog", 0, "finished")),
])
def test_no_sync_item_unless_the_latest_round_failed(page_conn, given):
    snap = dashboard.snapshot(page_conn, MONDAY, syncs=given)
    assert not any("hourly sync" in a["text"] for a in snap["attention"])
    assert snap["syncs"] == given


def test_the_sales_window_says_how_long_it_really_is():
    from datetime import datetime, timedelta, timezone
    end = datetime(2026, 12, 9, 15, tzinfo=timezone.utc)

    def words(span):
        return dashboard._window({"ok": True, "data": {"window_start": end - span, "as_of": end}})
    assert words(timedelta(days=7)) == "the 7 days"
    assert words(timedelta(days=7, minutes=40)) == "the 7 days"              # a poll a little early or late
    assert words(timedelta(days=3, hours=23)) == "the 3 d 23 h"
    assert words(timedelta(days=10)) == "the 10 d"                           # a gap in the polls: not "the 7 days"
    assert words(timedelta(seconds=30)) == "under a minute"


# --- The ops layout: the status bar, the front beside Needs attention, and the close-up --------------

def section(page: str, start: str, end: str) -> str:
    i = page.index(start)
    return page[i:page.index(end, i)]


def front_of(page: str) -> str:
    return section(page, '<nav class="fm"', "</nav>")


def test_the_status_bar_has_six_cells_and_visitors_only_in_the_footer(conn, page_conn):
    from vending import visits
    visits.write(conn, [("view", "/dashboard", "0123456789abcdef")])
    snap = dashboard.snapshot(page_conn, MONDAY)
    labels = [re.search(r'<span class="label">([^<]+)</span>', c).group(1) for c in status_cells(snap)]
    assert labels == ["Agent", "Machine poll", "Order cutoff", "Spendable", "Catalog approvals", "Plan"]
    page = dashboard.render(snap)
    top, footer = page.split("<footer>")
    assert "Visitors" not in top and 'class="tile"' not in top
    assert footer.count('<p class="visitors">') == 1 and '<p class="visitors">Visitors: 1 browser, 1 visit' in footer
    # The verdict sits in the Agent cell, not the title row; the tab still has it.
    assert 'class="verdict ' in status_cells(snap)[0] and 'class="verdict ' not in page.split("</header>")[0]
    assert page.count('<a class="sc') == 6 and "<title>" in page


def test_the_machine_poll_cell_says_when_the_poll_was_taken_never_a_lone_window_date(conn, page_conn, monkeypatch):
    monkeypatch.delenv("SYNC_AT_MINUTE", raising=False)
    # The polls 7 days apart: a full window (a wider gap says its own length, see `_window_words`).
    store_poll(conn, "w1", (MONDAY - timedelta(days=7, minutes=34)).isoformat(), {"15": (250, 10)})
    store_poll(conn, "w2", (MONDAY - timedelta(minutes=34)).isoformat(), {"15": (250, 14)})
    snap = dashboard.snapshot(page_conn, MONDAY)
    cell = status_cells(snap)[1]
    assert "Polled <time" in cell and 'data-f="ago"' in cell and 'data-f="et"' in cell
    assert "next sync ~<time" in cell and 'data-f="etclock"' in cell and "4 sold in the last 7 days" in cell
    start = snap["sales"]["data"]["window_start"]
    assert start is not None and start.isoformat() not in cell and "since" not in cell


@pytest.mark.parametrize("now, at", [
    (datetime(2026, 11, 19, 19, 34, tzinfo=timezone.utc), datetime(2026, 11, 19, 20, 5, tzinfo=timezone.utc)),
    (datetime(2026, 11, 19, 19, 3, tzinfo=timezone.utc), datetime(2026, 11, 19, 19, 5, tzinfo=timezone.utc)),
    (datetime(2026, 11, 19, 19, 5, tzinfo=timezone.utc), datetime(2026, 11, 19, 20, 5, tzinfo=timezone.utc)),
])
def test_the_next_sync_is_the_workers_next_round(now, at, monkeypatch):
    monkeypatch.delenv("SYNC_AT_MINUTE", raising=False)
    assert dashboard.next_sync(now) == at
    # The worker's own word wins when it serves the page; with no schedule there is no time to give.
    assert dashboard.next_sync(now, {"next_round_at": "2026-11-19T21:05:00+00:00"}) == at.replace(hour=21)
    monkeypatch.setenv("SYNC_AT_MINUTE", "off")
    assert dashboard.next_sync(now) is None


def test_the_catalog_cell_counts_approvals_places_and_the_deadline(conn, page_conn):
    for i, status in enumerate(["approved"] * 3 + ["pending"] * 2 + ["rejected"]):
        conn.execute("insert into product_requests (name, size, words, status) values (%s, '12 oz can', '{x}', %s)",
                     (f"Drink {i}", status))
    cell = status_cells(dashboard.snapshot(page_conn, MONDAY))[4]
    assert "3 approved" in cell and "2 pending" in cell and "1 rejected" in cell and "23 places left" in cell
    assert "due Wed 25 Nov" in cell
    after = status_cells(dashboard.snapshot(page_conn, dashboard.APPROVALS_DUE + timedelta(minutes=1)))[4]
    assert "due" not in after and "3 approved" in after


def _first_fill_plan(conn, tmp_path):
    from tests.test_plan_rounds import day_one, projection, write
    from vending import plan_store
    write(tmp_path, "round3", "3", {"day_one": day_one()}, projection())
    plan_id = plan_store.load(conn, tmp_path, "3", "Test Loader")
    plan_store.pick(conn, plan_id, "Test Picker")


def test_the_plan_cell_says_first_fill_before_the_lock_and_next_restock_after(conn, page_conn, tmp_path, monkeypatch):
    _first_fill_plan(conn, tmp_path)
    first = _live_run(conn)
    agent_runs.finish(conn, first, "submitted", plan=plan({"slot": "15", "fill_to": 6}, {"slot": "16", "fill_to": 4}))
    second = _live_run(conn)
    agent_runs.finish(conn, second, "submitted", plan=plan({"slot": "15", "fill_to": 7}, {"slot": "16", "fill_to": 0}))
    for run, back in ((first, 2), (second, 1)):        # made on Mon 9 Nov, whatever day the tests run
        conn.execute("update agent_runs set started_at = %s where id = %s", (MONDAY - timedelta(hours=back), run))
    after = dashboard.snapshot(page_conn, MONDAY)                  # the autouse fixture puts the lock behind us
    cell = status_cells(after)[5]
    assert ">For the next restock<" in cell and "2 changes" in cell and f"run #{second} submitted" in cell
    # The plan was made on Mon 9 Nov: the restock it is for is the Friday visit after Thursday's cutoff.
    assert '"sub">Fri 13 Nov &middot; 2 changes' in cell
    assert '<span class="vm-when">Next restock &middot; Fri 13 Nov</span>' in dashboard.render(after)
    assert "first fill" not in cell.lower()
    monkeypatch.setattr(dashboard, "lock_at", lambda: MONDAY + timedelta(days=20))
    before = dashboard.snapshot(page_conn, MONDAY)
    p = before["plan"]["data"]
    cell = status_cells(before)[5]
    assert ">For the first fill<" in cell and "Friday" not in cell
    assert f"20 slots &middot; {dashboard._cents(p['fill_most_cents'])} of $400" in cell and "not picked" not in cell
    # Neither "day-one" nor "round" is anywhere on the page, before or after the lock.
    for snap in (before, after):
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", dashboard.render(snap), flags=re.S)
        text = re.sub(r"<[^>]+>", " ", text).lower()
        assert not re.search(r"\bday[- ]one\b", text) and not re.search(r"\bround\b", text)


@pytest.mark.parametrize("most, cap, shown, tone", [(36_140, 36_600, "$361.40 of $366", ""),
                                                    (36_600, 36_600, "$366 of $366", ""),
                                                    (37_000, 36_600, "$370 of $366", "bad")])
def test_the_first_fill_cost_reads_against_the_cap(page_conn, monkeypatch, most, cap, shown, tone):
    monkeypatch.setattr(dashboard, "lock_at", lambda: MONDAY + timedelta(days=20))
    snap = dashboard.snapshot(page_conn, MONDAY)
    snap["plan"] = {"ok": True, "data": {"id": 1, "picked": True, "slots": [{"slot": "15"}] * 20,
                                         "fill_most_cents": most, "fill_cap_cents": cap}}
    cell = status_cells(snap)[5]
    assert shown in cell and cell.startswith(f' {tone}"' if tone else '" href')


def test_nothing_is_picked_until_a_viewer_picks_and_the_side_shows_attention_and_changes(conn, page_conn, even_columns, open_ledger):
    first = _live_run(conn)
    agent_runs.finish(conn, first, "submitted", plan=plan({"slot": "12", "fill_to": 6}, {"slot": "14", "fill_to": 4}))
    second = _live_run(conn)
    agent_runs.finish(conn, second, "submitted", plan=plan(
        {"slot": "12", "fill_to": 7, "sale_price": "2.75", "reason": "sells out"}, {"slot": "14", "fill_to": 4}))
    page = dashboard.render(dashboard.snapshot(page_conn, MONDAY))
    assert '<section id="vm" class="vm" data-view="now">' in page              # no pick in the HTML
    side = section(page, '<div class="ops-side">', '<aside class="vm-panel"')
    assert side.index('id="attention"') < side.index('id="plan-changes"')
    # Picking from a plan-change row: the whole row is the link.
    row = section(side, '<a class="chg" id="chg-12" href="?sel=12#sel-12"', "</a>")
    assert 'data-sel="12"' in row and "fill 6 to 7" in row and "sells out" in row
    assert '#sel-14"' not in side                                        # 14 didn't change
    # Picking from the front: each of ours is a link to its facts, with its whole aria label.
    front = front_of(page)
    cells = re.findall(r'<a class="fc[^"]*" id="fc-\w+" href="\?sel=\w+#sel-(\w+)" data-sel="(\w+)" aria-label="([^"]+)"', front)
    assert [a for a, b, _ in cells if a == b] == list(EVEN)
    assert all(label.startswith(f"Selection {s}. ") and label.endswith("Opens its close-up.") for s, _, label in cells)
    for slot in EVEN:
        assert f'<div class="sd" id="sel-{slot}" data-sel="{slot}" tabindex="-1">' in page
        assert f'.vm[data-sel="{slot}"] #sel-{slot}' in page and f'.vm[data-sel="{slot}"] #lane-{slot}' in page
        assert f'.vm:not([data-js]):has(#sel-{slot}:target) #lane-{slot}' in page


def test_picking_from_a_plan_section_row(conn, page_conn, tmp_path, monkeypatch, open_ledger):
    run = _live_run(conn)
    agent_runs.finish(conn, run, "submitted", plan=plan({"slot": "15", "fill_to": 6}))
    weekly = section(dashboard.render(dashboard.snapshot(page_conn, MONDAY)), '<section id="plan">', "</section>")
    assert ('<a class="num" id="num-15" href="?sel=15#sel-15" data-sel="15" aria-label="Selection 15: show it in the machine">15</a>'
            in weekly)
    _first_fill_plan(conn, tmp_path)
    monkeypatch.setattr(dashboard, "lock_at", lambda: MONDAY + timedelta(days=20))
    page = dashboard.render(dashboard.snapshot(page_conn, MONDAY))
    first = section(page, '<section id="plan">', "</section>")
    assert first.count('<a class="num" id="num-') == 20 and 'data-sel="58"' in first
    # Before the lock the side lists the first fill, each row picking its selection, and its facts say it.
    side = section(page, '<div class="ops-side">', '<aside class="vm-panel"')
    assert "First fill" in side and side.count('<a class="chg" id="chg-') == 20
    facts = section(page, '<div class="sd" id="sel-15"', '<div class="sd" id="sel-16"')
    assert "<dt>First fill</dt><dd>Red Bull Original · $3.50 · 11 units</dd>" in facts
    assert "Why, for the first fill</span>reason 15" in facts
    # The script picks from any of them, and brings the machine into view when the pick came from below.
    script = dashboard.SCRIPT
    assert "t.closest('a[data-sel]')" in script and "from && !from.closest('.vm')" in script


def test_the_close_up_has_a_close_button_and_the_pick_survives_a_refresh(page_conn):
    page = dashboard.render(dashboard.snapshot(page_conn, MONDAY))
    panel = section(page, '<aside class="vm-panel"', "</aside>")
    assert '<a class="vm-x" id="vm-close" href="?#vm" data-close aria-label="Close the close-up">' in panel
    assert '<a class="vm-scrim" href="?#vm" data-close tabindex="-1" aria-hidden="true"></a>' in page
    script, style = dashboard.SCRIPT, dashboard.STYLE
    # Close, Escape or a tap outside return the side to Needs attention and the plan's changes.
    assert "if (close) { e.preventDefault(); shut(); }" in script and "e.key === 'Escape'" in script
    assert "sel = ''; sheet = false;" in script
    assert ".vm[data-sel] .ops-home" in style and ".vm[data-sel] .vm-panel" in style
    # The pick lives in the script, not the page: the in-place refresh swaps <main> and puts it back.
    assert "main.innerHTML = fresh.innerHTML;" in script and "if (sel) v.setAttribute('data-sel', sel)" in script
    assert script.index("main.innerHTML = fresh.innerHTML;") < script.index("failed = false; lastOk = Date.now(); filter();")
    # Without scripts the link's #target picks, and on a phone opens the sheet.
    assert ".vm:not([data-js]) .sd:target { display:block }" in dashboard.machine_art.STYLE
    assert ".vm:not([data-js]):has(.sd:target) .vm-panel" in style
    assert "@media (prefers-reduced-motion:reduce) { .vm-panel { transition:none !important } }" in style


def test_not_ours_is_half_as_wide_on_the_front_and_in_the_close_up():
    style = dashboard.machine_art.STYLE
    assert re.search(r"\.fc \{[^}]*flex:2 1 0", style) and re.search(r"\.fc-no \{[^}]*flex:1 1 0", style)
    assert re.search(r"\.ma-lane \{[^}]*flex:1 1 0", style) and re.search(r"\.ma-notours \{[^}]*flex:\.5 1 0", style)
    view = {"11": {"now": {"product": None, "units": None, "price": None, "source": None, "counted_at": None},
                   "next": None, "change": None, "fill_to": None}}
    front, panels = dashboard.machine_art.split_html(view, None, False)
    assert front.count('<a class="fc"') == 1 and front.count('<span class="fc fc-no">') == 39
    assert panels.count('<article class="ma-lane" id="lane-11"') == 1 and panels.count('class="ma-lane ma-notours"') == 7


def test_the_front_and_close_up_carry_nothing_about_a_selection_that_is_not_ours(conn, page_conn, even_columns, open_ledger):
    catalog(conn, ("ZZ-RIVAL-SKU", "Zebra Fizz Rival", "Zebra Fizz", "Mango", "16 oz glass bottle"),
            ("COKE-20OZ", "Coca-Cola", "Coca-Cola", None, "20 oz bottle"))
    fulfilled(conn, "pr-15", "15", "ZZ-RIVAL-SKU", units_loaded=6, paid="5.55", at=MONDAY - timedelta(days=3))
    fulfilled(conn, "pr-12", "12", "COKE-20OZ", units_loaded=5, paid="6.00", at=MONDAY - timedelta(days=3))
    store_poll(conn, "p1", (MONDAY - timedelta(days=3)).isoformat(), {"15": (987, 4000), "12": (250, 10)})
    store_poll(conn, "p2", (MONDAY - timedelta(hours=2)).isoformat(), {"15": (987, 4321), "12": (250, 12)})
    first = _live_run(conn)
    agent_runs.finish(conn, first, "submitted", plan=plan({"slot": "15", "fill_to": 2}, {"slot": "12", "fill_to": 5}))
    second = _live_run(conn)
    agent_runs.finish(conn, second, "submitted", plan=plan(
        {"slot": "15", "product": "ZZ-RIVAL-SKU", "fill_to": 9, "sale_price": "8.76", "reason": "Quokka reason"},
        {"slot": "12", "fill_to": 6, "reason": "ours"}))
    for public in (True, False):
        snap = dashboard.snapshot(page_conn, MONDAY, public=public)
        page, state = dashboard.render(snap), dashboard.to_json(snap)
        for leak in ("ZZ-RIVAL", "Zebra", "Mango", "Quokka", "$9.87", "$8.76"):
            assert leak not in page, (public, leak)
        for leak in ("ZZ-RIVAL", "Zebra", "Quokka", '"9.87"', '"8.76"'):
            assert leak not in state or not public, leak
        for anchor in ('data-sel="15"', 'id="sel-15"', '#sel-15', 'id="lane-15" role="group"', '.vm[data-sel="15"]'):
            assert anchor not in page, anchor
        # Every not-ours cell on the front is its number and "Not ours", and nothing else.
        cells = re.findall(r'<span class="fc fc-no">(.*?)</span></span>', front_of(page))
        assert len(cells) == 20 and all(
            re.fullmatch(r'<span class="fc-n">\d\d</span><span class="fc-t">Not ours', c) for c in cells)
        assert '<article class="ma-lane ma-notours" id="lane-15" aria-label="Selection 15: not ours">' in page
        assert "at restock" in lane_html(page, "12")


def test_the_facts_say_where_each_number_comes_from(conn, page_conn, open_ledger):
    catalog(conn, ("COKE-20OZ", "Coca-Cola", "Coca-Cola", None, "20 oz bottle"))
    fulfilled(conn, "pr-15", "15", "COKE-20OZ", units_loaded=5, paid="6.00", at=MONDAY - timedelta(days=3))
    page = dashboard.render(dashboard.snapshot(page_conn, MONDAY))
    ours = section(page, '<div class="sd" id="sel-15"', '<div class="sd" id="sel-16"')
    assert "5 of 7 &middot; from receipts and polls" in ours and "unknown: no price from the polls" in ours
    assert "unknown: no sales from the polls" in ours and "No plan yet." in ours
    blank = section(page, '<div class="sd" id="sel-16"', '<div class="sd" id="sel-17"')
    assert "unknown: no receipt or count yet" in blank and "<b>No record yet</b>" in blank


def test_without_an_assignment_attention_still_shows_beside_an_empty_machine(conn, page_conn):
    conn.execute("delete from slot_assignment")
    page = dashboard.render(dashboard.snapshot(page_conn, MONDAY))
    vm = section(page, '<section id="vm"', "</section>")
    assert 'id="attention"' in vm and "Which selections are ours isn&#x27;t known" in vm
    assert '<nav class="fm"' not in vm and 'class="vm-panel"' not in vm


@pytest.mark.parametrize("visit, started, shown", [
    ("scheduled", datetime(2026, 12, 7, 15, tzinfo=timezone.utc), "Fri 11 Dec"),       # Monday: that week's Friday
    ("scheduled", datetime(2026, 12, 11, 3, tzinfo=timezone.utc), "Fri 11 Dec"),       # Thu 22:00 Eastern: before the cutoff
    ("scheduled", datetime(2026, 12, 11, 6, tzinfo=timezone.utc), "Fri 18 Dec"),       # Fri 01:00 Eastern: past it
    ("midweek", datetime(2026, 12, 7, 15, tzinfo=timezone.utc), "midweek visit"),     # the plan records no day
])
def test_the_next_restock_is_the_friday_after_the_cutoff_or_a_midweek_visit(visit, started, shown):
    if dashboard.next_cutoff(started) is None:
        pytest.skip("no IANA time-zone data to place the cutoff")
    assert dashboard._restock({"visit": visit, "run": {"started_at": started}}) == shown
    assert dashboard._restock(None) is None


def test_the_page_says_next_restock_for_what_the_plan_does_and_friday_only_for_the_day(conn, page_conn, even_columns, open_ledger):
    fulfilled(conn, "pr-1", "12", "COKE-20OZ", units_loaded=4, paid="4.80", at=MONDAY - timedelta(days=3))
    first = _live_run(conn)
    agent_runs.finish(conn, first, "submitted", plan=plan({"slot": "12", "fill_to": 6}, {"slot": "14", "fill_to": 4}))
    second = _live_run(conn)
    agent_runs.finish(conn, second, "submitted", plan={**plan(
        {"slot": "12", "fill_to": 7, "sale_price": "2.75"}, {"slot": "14", "fill_to": 0}), "visit": "midweek"})
    snap = dashboard.snapshot(page_conn, MONDAY)
    page = dashboard.render(snap)
    assert '<button type="button" data-view="next" aria-pressed="false">Next restock</button>' in page
    assert '<span class="vm-when">Next restock &middot; midweek visit</span>' in page
    assert "midweek visit &middot; 2 changes" in status_cells(snap)[5]
    assert "+3 at restock" in lane_html(page, "12") and "pulled at restock" in lane_html(page, "14")
    assert "<dt>Next restock</dt>" in page and "pulled at the next restock" in page
    # "Friday" is left only where it is the day: the cutoff and the order deadline, and the requests' visit type.
    machine = page[page.index('<section id="vm"'):page.index('<section id="money"')]
    assert "Friday" not in re.sub(r"<[^>]+>", " ", machine).replace("free Friday visit", "")
    assert "Friday" not in dashboard._status_bar(snap)


def _set_wildcard(conn, slot: str) -> None:
    from psycopg.types.json import Jsonb
    conn.execute("insert into config (key, value, updated_by) values ('wildcard_slot', %s, 'test')"
                 " on conflict (key) do update set value = excluded.value", (Jsonb(slot),))


def test_the_wildcard_slot_is_marked_on_the_front_the_close_up_and_the_plan(conn, page_conn, open_ledger):
    _set_wildcard(conn, "37")
    first = _live_run(conn)
    agent_runs.finish(conn, first, "submitted", plan=plan({"slot": "37", "fill_to": 4}, {"slot": "15", "fill_to": 4}))
    second = _live_run(conn)
    agent_runs.finish(conn, second, "submitted", plan=plan({"slot": "37", "fill_to": 6, "sale_price": "7.50"},
                                                           {"slot": "15", "fill_to": 4}))
    snap = dashboard.snapshot(page_conn, MONDAY, public=True)
    assert snap["wildcard"] == "37" and '"wildcard": "37"' in dashboard.to_json(snap)
    page = dashboard.render(snap)
    cell = section(front_of(page), 'href="?sel=37#sel-37"', "</a>")
    assert "fc-wild" in page.split('href="?sel=37#sel-37"')[0][-60:] and '>W</span>' in cell
    assert "Selection 37, the wildcard." in page
    assert '<p class="ma-wl"' in lane_html(page, "37") and 'class="ma-lane ma-changed ma-wild"' in page
    facts = section(page, '<div class="sd" id="sel-37"', '<div class="sd" id="sel-38"')
    assert "<dt>Wildcard</dt>" in facts and ">wildcard</span>" in facts
    changes = section(page, '<a class="chg" id="chg-37" href="?sel=37#sel-37"', "</a>")
    assert ">wildcard</span>" in changes
    rows = section(page, '<section id="plan">', "</section>")
    # In the row's text, not beside the number, so the row's columns stay in line.
    row = rows.split('id="slot-37"', 1)[1].split("</summary>", 1)[0]
    assert re.search(r'data-sel="37"[^>]*>37</a><span class="now">', row)
    assert '<span class="what"><span class="pill wild"' in row
    # Only the one slot: no other selection carries the mark.
    assert page.count('class="fc fc-wild"') + page.count('fc-chg fc-wild"') == 1 and page.count("<dt>Wildcard</dt>") == 1


def test_a_wildcard_that_is_not_ours_is_never_marked(conn, page_conn, even_columns):
    _set_wildcard(conn, "37")                    # 37 isn't in the stored assignment here
    snap = dashboard.snapshot(page_conn, MONDAY, public=True)
    page = dashboard.render(snap)
    assert snap["wildcard"] is None and ' fc-wild"' not in page and 'class="ma-wl"' not in page
    assert "<dt>Wildcard</dt>" not in page and ">wildcard</span>" not in page
    assert 'data-sel="37"' not in page


# Thursdays either side of both 2026 clock changes (8 Mar, 1 Nov), in Eastern.
_DST_THURSDAYS = ((2026, 3, 5), (2026, 3, 12), (2026, 10, 29), (2026, 11, 5))


@pytest.mark.parametrize("thursday", _DST_THURSDAYS)
@pytest.mark.parametrize("clock, same_week", [((23, 58, 59), True), ((23, 59, 0), True), ((23, 59, 59), True),
                                              (None, False)])        # None: Friday 00:00:00
def test_the_restock_date_follows_the_planners_cutoff_minute(thursday, clock, same_week):
    """The planner still accepts this Friday's purchases through 23:59:59 Thursday (`planner._past_cutoff`
    is past only after the cutoff minute), so the page names this Friday until midnight."""
    zoneinfo = pytest.importorskip("zoneinfo")
    try:
        eastern = zoneinfo.ZoneInfo("America/New_York")
    except zoneinfo.ZoneInfoNotFoundError:
        pytest.skip("no IANA time-zone data")
    thu = datetime(*thursday, tzinfo=eastern)
    local = (thu.replace(hour=clock[0], minute=clock[1], second=clock[2]) if clock
             else thu + timedelta(days=1))
    friday = thu + timedelta(days=1 if same_week else 8)
    shown = dashboard._restock({"visit": "scheduled", "run": {"started_at": local.astimezone(timezone.utc)}})
    assert shown == f"{friday:%a} {friday.day} {friday:%b}", (local, shown)


def _reload(page: str) -> str:
    return re.search(r'<noscript><meta http-equiv="refresh" content="30; url=([^"]*)"></noscript>', page).group(1)


def test_without_scripts_the_reload_keeps_a_pick_only_when_it_is_ours(page_conn):
    snap = dashboard.snapshot(page_conn, MONDAY)
    assert _reload(dashboard.render(snap)) == "?refresh=1"
    assert _reload(dashboard.render(snap, picked="15")) == "?refresh=1&amp;sel=15#sel-15"
    # Not ours (12), not on the face, or not a selection at all: the reload carries nothing.
    for bad in ("12", "99", "15#x", '15"><script>', "", None):
        assert _reload(dashboard.render(snap, picked=bad)) == "?refresh=1", bad
    # The pick links carry it in the query, so a click without scripts asks the server for it.
    page = dashboard.render(snap)
    assert 'id="fc-15" href="?sel=15#sel-15" data-sel="15"' in page and 'href="#sel-' not in page


def test_without_scripts_a_shelf_link_opens_its_first_selection_of_ours(page_conn, even_columns):
    page = dashboard.render(dashboard.snapshot(page_conn, MONDAY))
    rails = re.findall(r'<a class="fm-sh" id="rail-(\d)" href="([^"]*)" data-shelf="\d"', page)
    # Each shelf's link targets a selection of ours (so a phone's sheet opens), the first on that shelf.
    assert rails == [(str(r), f"?sel={r}2#sel-{r}2") for r in range(1, 6)]
    assert 'href="#shelf-' not in page



def test_without_scripts_closing_the_close_up_stays_closed_through_the_reload(page_conn):
    from urllib.parse import parse_qs, urljoin, urlsplit
    snap = dashboard.snapshot(page_conn, MONDAY)
    here = "http://127.0.0.1:8765/?refresh=1&sel=15#sel-15"          # a pick kept by the timed reload
    page = dashboard.render(snap, picked="15")
    for link in ("vm-x", "vm-scrim"):
        href = re.search(rf'<a class="{link}"[^>]* href="([^"]*)"', page).group(1)
        followed = urljoin(here, href.replace("&amp;", "&"))
        assert "sel" not in parse_qs(urlsplit(followed).query), (link, followed)   # a real load, without the pick
        # The page that link loads reloads itself without a pick too, so the sheet stays closed.
        picked = parse_qs(urlsplit(followed).query).get("sel", [None])[0]
        assert _reload(dashboard.render(snap, picked=picked)) == "?refresh=1", link


def test_the_status_bar_and_facts_say_how_long_the_sales_window_really_is():
    # The same bounds as `_window` (main): 7 days give or take an hour; a gap in the polls says its length.
    end = datetime(2026, 12, 9, 15, tzinfo=timezone.utc)

    def words(span):
        return dashboard._window_words({"window_start": end - span, "as_of": end})
    assert words(timedelta(days=7)) == "in the last 7 days"
    assert words(timedelta(days=7, minutes=40)) == "in the last 7 days"
    assert words(timedelta(days=3, hours=23)) == "in the last 3 d 23 h"
    assert words(timedelta(days=10)) == "in the last 10 d"                     # a gap: not "the last 7 days"
    assert words(timedelta(seconds=30)) == "in under a minute"


# --- Main's panels inside the layout (#191 pricing, #193 leaderboards, #194 fill checks, #196 charts, #192) --

def test_the_under_fill_flag_shows_in_the_side_card_and_the_facts_too(conn, page_conn, open_ledger):
    under = TEST_SLOTS[0]
    catalog(conn, ("BOTTLE-20", "Test bottle", "Plain", None, "20 oz bottle"))
    set_value(conn, "capacity_overrides", {"BOTTLE-20": 8}, "test")
    first = _live_run(conn)
    agent_runs.finish(conn, first, "submitted", plan=plan({"slot": under, "product": "BOTTLE-20", "fill_to": 4}))
    second = _live_run(conn)
    agent_runs.finish(conn, second, "submitted", plan=plan(
        {"slot": under, "product": "BOTTLE-20", "fill_to": 6, "reason": "Budget < limit"}))
    page = dashboard.render(dashboard.snapshot(page_conn, MONDAY, public=True))
    row = section(page, f'<a class="chg" id="chg-{under}"', "</a>")
    assert '<span class="pill warn">fills 6 of 8: 2 spaces empty</span>' in row and "Budget &lt; limit" in row
    facts = section(page, f'<div class="sd" id="sel-{under}"', "</dl>")
    assert '<dt>Fill check</dt><dd><span class="sd-fr unk">fills 6 of 8: 2 spaces empty</span></dd>' in facts
    plan_row = page.split(f'id="slot-{under}"', 1)[1].split("</summary>", 1)[0]      # main's flag on The plan
    assert "fills 6 of 8: 2 spaces empty" in plan_row


def test_before_the_opening_the_spendable_cell_shows_no_test_figures_only_its_chart(conn, page_conn):
    # A test purchase and a test run before any opening: development spend, which the live page doesn't count.
    fulfilled(conn, "pr-test", "15", "COKE-20OZ", units_loaded=5, paid="6.25", at=MONDAY - timedelta(days=3))
    run = _live_run(conn)
    agent_runs.finish(conn, run, "submitted", cost_usd=Decimal("0.77"))
    snap = dashboard.snapshot(page_conn, MONDAY)
    cell = dashboard._status_bar(snap, charts={"spend": "/dashboard/spend"}).split("Spendable", 1)[1].split("</div>")[0]
    assert "$0.00 / $85.00" in cell and "No recorded spend" in cell
    assert "$6.25" not in cell and "$0.77" not in cell and "drinks" not in cell
    assert '<a href="/dashboard/spend">Development spend by week</a>' in cell
    # A link can't hold a link: the cell is a box, and its label alone links to the money panel.
    assert '<a class="label" href="#money">Spendable</a>' in dashboard._status_bar(snap, charts={"spend": "/x"})
    assert "Development spend by week" not in dashboard._status_bar(snap)       # run on its own: no chart


def test_after_the_opening_the_spendable_cell_reads_the_ledger_and_links_its_chart(conn, page_conn, open_ledger):
    snap = dashboard.snapshot(page_conn, MONDAY)
    bar = dashboard._status_bar(snap, charts={"spend": "/dashboard/spend"})
    cell = bar.split("Spendable", 1)[1].split("</div>")[0]
    assert "$600.00" in cell and "Spent since the opening" in cell
    assert '<a href="/dashboard/spend">Development spend by week</a>' in cell


def test_the_pricing_leaderboards_and_charts_sit_inside_the_layout(conn, page_conn, open_ledger):
    snap = dashboard.snapshot(page_conn, MONDAY)
    page = dashboard.render(snap, charts={"money": "/dashboard/money", "pnl": "/dashboard/pnl",
                                          "spend": "/dashboard/spend"})
    # Status bar, the machine, then Pricing right after it, then Money; leaderboards after Back stock.
    order = [page.index(anchor) for anchor in ('<nav class="strip"', '<section id="vm"', '<section id="pricing"',
                                               '<section id="money"', '<section id="plan"', '<section id="closet"',
                                               '<section id="leaderboards"', "<footer>")]
    assert order == sorted(order)
    head = page.split("</header>")[0]
    assert '<a class="chartlink" href="/dashboard/money">Money chart</a>' in head
    assert ('<p class="charts">Charts: <a href="/dashboard/money">Money by day</a> &middot; '
            '<a href="/dashboard/pnl">Weekly P&amp;L</a></p>') in page
    assert '<a href="/dashboard/spend">Development spend by week</a>' in page.split('<section id="vm"')[0]


def test_lane_products_skips_a_product_that_is_not_text():
    """A stored plan whose product is a list (or any non-text) must not crash the whole snapshot."""
    from vending import machine_art
    snap = {
        "stock": {"ok": True, "data": {"slots": {"12": {"product": "RB-8CAN"}, "14": {"product": ["A", "B"]}}}},
        "counts": {"ok": True, "data": {"16": {"product": {"sku": "X"}}}},
        "plan": {"ok": True, "data": {"slots": [{"slot": "18", "now": {"product": ["C"]}},
                                                {"slot": "21", "now": {"product": "CELSIUS-12CAN"}}]}},
    }
    assert machine_art.lane_products(snap, ["12", "14", "16", "18", "21"]) == ["CELSIUS-12CAN", "RB-8CAN"]


# --- Failure banner (#220) -----------------------------------------------------------------------

SECRET = "connection to host db.private.internal refused for user vending_rw"


def _syncs(*jobs, at=None):
    """A worker round like `vending.worker.State.public` hands the page (`vending.worker.run_round`):
    `(job name, fields)` pairs, `fields` holding `exit_code` and, when it has none, an `outcome`."""
    at = at or (MONDAY - timedelta(minutes=5))
    return {"started_at": (at - timedelta(hours=1)).isoformat(), "rounds": 3,
            "last_round_at": at.isoformat(), "next_round_at": MONDAY.isoformat(),
            "results": [{"job": job, "started_at": at.isoformat(), "seconds": 1.0,
                         "outcome": "finished", **fields} for job, fields in jobs]}


def test_a_failed_sync_round_shows_the_banner(conn, page_conn, open_ledger):
    syncs = _syncs(("slots_check", {"exit_code": 0}), ("sync_history", {"exit_code": 3}),
                   ("sync_catalog", {"exit_code": None, "outcome": "timed out"}))
    snap = dashboard.snapshot(page_conn, MONDAY, syncs=syncs)
    [alert] = snap["alerts"]
    assert alert["kind"] == "sync_failed"
    assert alert["at"] == MONDAY - timedelta(minutes=5)
    assert "`sync_history` exit 3" in alert["text"] and "`sync_catalog` timed out" in alert["text"]
    page = dashboard.render(snap)
    assert '<div class="failbanner" role="alert">' in page
    assert "Sync round failed" in page and "sync_history" in page and "timed out" in page


def test_no_banner_when_everything_is_fine(conn, page_conn, open_ledger):
    syncs = _syncs(("slots_check", {"exit_code": 0}), ("sync_history", {"exit_code": 0}),
                   ("sync_requests", {"exit_code": 0}), ("sync_catalog", {"exit_code": 0}),
                   ("sync_product_requests", {"exit_code": 0}))
    snap = dashboard.snapshot(page_conn, MONDAY, syncs=syncs)
    assert snap["alerts"] == []
    page = dashboard.render(snap)
    assert '<div class="failbanner"' not in page and 'role="alert"' not in page


def test_a_fresh_database_shows_no_banner(page_conn):
    snap = dashboard.snapshot(page_conn, MONDAY)
    assert snap["alerts"] == []
    assert '<div class="failbanner"' not in dashboard.render(snap)


def test_a_failed_run_shows_the_banner_without_its_error_text(conn, page_conn, open_ledger):
    run = _live_run(conn)
    agent_runs.finish(conn, run, "failed", detail=SECRET)
    snap = dashboard.snapshot(page_conn, MONDAY)
    [alert] = snap["alerts"]
    assert alert["kind"] == "run_failed"
    assert alert["text"] == f"Run #{run} failed. See Runs below."
    page = dashboard.render(snap)
    assert SECRET not in page and SECRET not in dashboard.to_json(snap)
    assert "Agent run failed" in page


@pytest.mark.parametrize("status,kind", [("halted", "run_halted"), ("running", "run_stuck")])
def test_a_halted_or_stuck_latest_run_shows_the_banner(conn, page_conn, open_ledger, status, kind):
    run = _live_run(conn)
    if status == "running":
        conn.execute("update agent_runs set started_at = %s where id = %s",
                     (MONDAY - dashboard.STALE_RUN - timedelta(minutes=1), run))
    else:
        agent_runs.finish(conn, run, status, detail="The halt switch is on.")
    snap = dashboard.snapshot(page_conn, MONDAY)
    [alert] = snap["alerts"]
    assert alert["kind"] == kind
    assert dashboard.render(snap).count('role="alert"') == 1


def test_a_tracking_check_outside_shows_the_banner_and_an_incident(conn, page_conn, open_ledger):
    at = MONDAY - timedelta(hours=1)
    conn.execute(
        "insert into tracking_checks (sent_at, eastern_day, sent_by, as_of, as_of_time, request,"
        " answered_at, status, answer, used_check, results)"
        " values (%s, '2026-11-09', 'test', 's1', %s, '{}', %s, 200, %s, true, %s)",
        (at, at, at + timedelta(minutes=5), Jsonb({"error": SECRET, "detail": SECRET}),
         Jsonb({"cash": "outside", "sales_tax_owed": "within", "coil": "within", "back_stock": "within"})))
    snap = dashboard.snapshot(page_conn, MONDAY)
    [alert] = snap["alerts"]
    assert alert["kind"] == "tracking_outside"
    assert alert["text"].startswith("The bridge's tracking check said cash outside")
    page = dashboard.render(snap)
    assert "Tracking check outside" in page and "cash" in page
    assert SECRET not in page and SECRET not in dashboard.to_json(snap)
    row, = snap["incidents"]["data"]["rows"]
    assert row["kind"] == "books_outside" and "cash" in row["note"] and SECRET not in row["note"]
    assert snap["incidents"]["data"]["total"] == 1
    # The next check coming back all within clears the banner.
    conn.execute(
        "insert into tracking_checks (sent_at, eastern_day, sent_by, as_of, as_of_time, request,"
        " answered_at, status, answer, used_check, results)"
        " values (%s, '2026-11-09', 'test', 's2', %s, '{}', %s, 200, '{}', true, %s)",
        (MONDAY, MONDAY, MONDAY,
         Jsonb({k: "within" for k in ("cash", "sales_tax_owed", "coil", "back_stock")})))
    assert dashboard.snapshot(page_conn, MONDAY)["alerts"] == []


def test_the_missed_run_hook_stays_quiet_until_a_schedule_is_recorded(page_conn):
    """Nothing records an expected run schedule yet (`agent_runs` holds runs that happened; the
    worker's round times are memory only), so the hook returns None and no alert appears."""
    assert dashboard._missed_scheduled_run({"syncs": None}) is None
    assert dashboard.snapshot(page_conn, MONDAY)["alerts"] == []


def test_a_retry_still_running_keeps_the_last_failure_banner(conn, page_conn, open_ledger):
    """A retry that is still running must not clear the alert of the run before it - neither a
    finished run's failure (the banner stays until a later run FINISHES successfully) nor a run
    left `running` past two hours (starting a retry doesn't resolve it; the retry finishing
    does)."""
    failed = _live_run(conn)
    conn.execute("update agent_runs set started_at = %s where id = %s",
                 (MONDAY - timedelta(hours=8), failed))
    agent_runs.finish(conn, failed, "failed", detail=SECRET)
    retry = agent_runs.start(conn, mode="live", model="m")
    conn.execute("update agent_runs set started_at = %s where id = %s",
                 (MONDAY - timedelta(hours=1), retry))
    snap = dashboard.snapshot(page_conn, MONDAY)
    [alert] = [a for a in snap["alerts"] if a["kind"] == "run_failed"]
    assert alert["text"] == f"Run #{failed} failed. See Runs below."
    # The retry finishing successfully clears it.
    agent_runs.finish(conn, retry, "submitted")
    assert not [a for a in dashboard.snapshot(page_conn, MONDAY)["alerts"] if a["kind"] == "run_failed"]

    later = MONDAY + timedelta(hours=5)
    # A run left `running` past the stale bound banners too, and it is the newest thing that
    # happened: before any retry, the stuck run itself carries the line. (It starts inside the
    # ledger window, after the finished runs above.)
    stuck = agent_runs.start(conn, mode="live", model="m")
    conn.execute("update agent_runs set started_at = %s where id = %s",
                 (MONDAY + timedelta(hours=2), stuck))
    [alert] = [a for a in dashboard.snapshot(page_conn, later)["alerts"] if a["kind"] == "run_stuck"]
    assert alert["text"].startswith(f"Run #{stuck} has said `running` for")
    # Starting a retry doesn't clear it: the stuck row is still unresolved. The newest run is the
    # retry, which used to hide the stuck one; the last finished run started before it.
    after = agent_runs.start(conn, mode="live", model="m")
    conn.execute("update agent_runs set started_at = %s where id = %s",
                 (MONDAY + timedelta(hours=4), after))
    [alert] = [a for a in dashboard.snapshot(page_conn, later)["alerts"] if a["kind"] == "run_stuck"]
    assert alert["text"].startswith(f"Run #{stuck} has said `running` for")
    assert not [a for a in dashboard.snapshot(page_conn, later)["alerts"] if a["kind"] == "run_failed"]
    # The retry finishing successfully resolves the stuck run: the banner is clear.
    agent_runs.finish(conn, after, "submitted")
    assert dashboard.snapshot(page_conn, later)["alerts"] == []


def test_run_and_tracking_alerts_stay_inside_the_ledger_window(conn, page_conn, open_ledger):
    """A failure from before the ledger's opening is development spend, not a competition incident,
    so it never banners (`open_ledger` opens MONDAY - 7 days). A sync round is current state, so
    it banners whenever it ran."""
    before = MONDAY - timedelta(days=8)
    run = _live_run(conn)
    conn.execute("update agent_runs set started_at = %s where id = %s", (before, run))
    agent_runs.finish(conn, run, "failed", detail=SECRET)
    conn.execute(
        "insert into tracking_checks (sent_at, eastern_day, sent_by, as_of, as_of_time, request,"
        " answered_at, status, answer, used_check, results)"
        " values (%s, '2026-11-01', 'test', 's0', %s, '{}', %s, 200, '{}', true, %s)",
        (before, before, before + timedelta(minutes=5),
         Jsonb({"cash": "outside", "sales_tax_owed": "within", "coil": "within", "back_stock": "within"})))
    assert dashboard.snapshot(page_conn, MONDAY)["alerts"] == []
    # The same failures after the opening banner.
    late = MONDAY - timedelta(hours=1)
    late_run = _live_run(conn)
    conn.execute("update agent_runs set started_at = %s where id = %s", (late, late_run))
    agent_runs.finish(conn, late_run, "failed", detail=SECRET)
    conn.execute(
        "insert into tracking_checks (sent_at, eastern_day, sent_by, as_of, as_of_time, request,"
        " answered_at, status, answer, used_check, results)"
        " values (%s, '2026-11-09', 'test', 's1', %s, '{}', %s, 200, '{}', true, %s)",
        (late, late, late + timedelta(minutes=5),
         Jsonb({"cash": "within", "sales_tax_owed": "within", "coil": "within", "back_stock": "outside"})))
    kinds = sorted(a["kind"] for a in dashboard.snapshot(page_conn, MONDAY)["alerts"])
    assert kinds == ["run_failed", "tracking_outside"]


def test_tracking_results_are_read_through_the_allowlist(conn, page_conn, open_ledger):
    """`results` is the bridge's JSON: an array there must not crash the incident log (which would
    hide all of it), and nothing undocumented reaches the public JSON. A newest check whose results
    cannot be read names nothing and is skipped, so it cannot clear a known `outside` alert: the
    newest valid check decides the banner."""
    at = MONDAY - timedelta(hours=2)
    conn.execute(
        "insert into tracking_checks (sent_at, eastern_day, sent_by, as_of, as_of_time, request,"
        " answered_at, status, answer, used_check, results)"
        " values (%s, '2026-11-09', 'test', 's2', %s, '{}', %s, 200, '{}', true, %s)",
        (at, at, at + timedelta(minutes=5), Jsonb(["cash", "outside"])))
    earlier = MONDAY - timedelta(hours=3)
    conn.execute(
        "insert into tracking_checks (sent_at, eastern_day, sent_by, as_of, as_of_time, request,"
        " answered_at, status, answer, used_check, results)"
        " values (%s, '2026-11-09', 'test', 's1', %s, '{}', %s, 200, '{}', true, %s)",
        (earlier, earlier, earlier + timedelta(minutes=5),
         Jsonb({"cash": "outside", "smuggled": "outside", "coil": "within"})))
    snap = dashboard.snapshot(page_conn, MONDAY)
    # The newest check's results aren't the documented object, so it names nothing and is skipped;
    # the newest valid check decides, and its verdicts survive the same allowlist (`smuggled` doesn't).
    [alert] = snap["alerts"]
    assert alert["kind"] == "tracking_outside"
    assert alert["text"].startswith("The bridge's tracking check said cash outside")
    assert snap["tracking_alert"]["ok"]
    assert snap["tracking_alert"]["data"]["results"] == {"cash": "outside", "coil": "within"}
    # The log survives the array and reads the same check through the same allowlist.
    row, = [r for r in snap["incidents"]["data"]["rows"] if r["kind"] == "books_outside"]
    assert "cash" in row["note"] and "smuggled" not in row["note"]
    assert snap["incidents"]["data"]["total"] == 1
    assert "smuggled" not in dashboard.to_json(snap)


def test_the_halted_banner_reports_the_switch_as_it_is_now(conn, page_conn, open_ledger):
    """The run's halt is history; the switch line comes from `config` now, and a finished run shows
    when it finished."""
    run = _live_run(conn)
    agent_runs.finish(conn, run, "halted", detail=SECRET)
    finished = conn.execute("select finished_at from agent_runs where id = %s", (run,)).fetchone()[0]
    [alert] = dashboard.snapshot(page_conn, MONDAY)["alerts"]
    assert alert["kind"] == "run_halted" and alert["at"] == finished
    assert alert["text"] == f"Run #{run} ended halted. The halt switch is on."
    conn.execute("update config set value = 'false' where key = 'halted'")
    [alert] = dashboard.snapshot(page_conn, MONDAY)["alerts"]
    assert alert["text"] == f"Run #{run} ended halted. The halt switch is off now."
