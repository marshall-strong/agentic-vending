"""The agent dashboard and the run record it reads."""

import json
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.server import ThreadingHTTPServer

import psycopg
import pytest

from tests.conftest import CONNECT_TIMEOUT, store_poll
from vending import agent_runs, dashboard
from vending.config import set_halted
from vending.write_gate import log


def plan(*slots, reason="restock what sold"):
    return {"visit": "scheduled", "reason": reason, "slots": list(slots)}


def read_only(url, schema):
    return psycopg.connect(url, autocommit=True, connect_timeout=CONNECT_TIMEOUT,
                           options=f"{dashboard.READ_ONLY} -c search_path={schema}")


@pytest.fixture
def page_conn(schema_url):
    """A connection like the dashboard's own: read-only, in the test schema."""
    with read_only(*schema_url) as conn:
        yield conn


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


def test_plan_compares_with_the_run_before(conn, page_conn):
    first = agent_runs.start(conn, mode="dry-run")
    agent_runs.finish(conn, first, "skipped", plan=plan(
        {"slot": "15", "fill_to": 6, "sale_price": "2.50", "reason": "sold 6"},
        {"slot": "16", "keep": True, "reason": "no figures"}))
    second = agent_runs.start(conn, mode="dry-run", model="claude-sonnet-5-5")
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
    assert f"1 of {len(dashboard.SLOTS)} slots changed</b> since run #{first}" in page
    assert page.count('class="pc changed"') == 1
    assert '<a class="pill info" href="#slot-16">16 swap</a>' in page and 'id="slot-16"' in page


def test_a_plan_of_the_wrong_shape_still_renders(conn, page_conn):
    run = agent_runs.start(conn, mode="dry-run")
    agent_runs.finish(conn, run, "failed", plan={"slots": "not a list"}, detail="bad answer")
    snap = dashboard.snapshot(page_conn)
    assert all(s["now"] is None for s in snap["plan"]["data"]["slots"])
    dashboard.render(snap)


def test_status_and_activity(conn, page_conn):
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
    assert [r["tool"] for r in snap["activity"]["data"]] == ["set_price", "halt"]
    assert snap["recent_calls"]["data"] == {"calls": 2, "writes": 2, "refused": 1, "failed": 0}
    assert dashboard.verdict(snap["attention"]) == "problem"
    assert snap["attention"][0]["level"] == "bad" and "probably died" in snap["attention"][0]["text"]
    assert any("1 refused" in a["text"] for a in snap["attention"])
    page = dashboard.render(snap)
    assert 'class="write refused problem"' in page and "Probably died" in page and "writes allowed" in page


def test_text_from_the_database_is_escaped(conn, page_conn):
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
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
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


def test_serves_the_page_and_json(served, conn):
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
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        code, _, body = get(f"http://127.0.0.1:{httpd.server_address[1]}/")
        assert code == 503 and "connection refused" in body
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- from the review ----------------------------------------------------------------------------

def test_money_stays_exact_both_ways(conn, page_conn):
    run = agent_runs.start(conn, mode="live")
    agent_runs.finish(conn, run, "submitted", plan=plan({"slot": "15", "fill_to": 6, "sale_price": Decimal("2.50")}))
    assert conn.execute("select plan->'slots'->0->>'sale_price' from agent_runs").fetchone()[0] == "2.50"
    # A price someone stored as a JSON number still reads back exact, not as the float 2.5.
    conn.execute("""update agent_runs set plan = jsonb_set(plan, '{slots,0,sale_price}', '2.50')""")
    snap = dashboard.snapshot(page_conn)
    assert snap["plan"]["data"]["slots"][0]["now"]["sale_price"] == Decimal("2.50")
    assert "$2.50" in dashboard.render(snap)
    assert '"sale_price": "2.50"' in dashboard.to_json(snap)


def test_replays_dont_take_over_the_live_view(conn, page_conn):
    live = agent_runs.start(conn, mode="live")
    agent_runs.finish(conn, live, "submitted", plan=plan({"slot": "15", "keep": True}))
    dry = agent_runs.start(conn, mode="dry-run")
    agent_runs.finish(conn, dry, "skipped", plan=plan({"slot": "15", "fill_to": 3, "sale_price": "2.00"}))
    replay = agent_runs.start(conn, mode="replay")
    agent_runs.finish(conn, replay, "skipped", plan=plan({"slot": "15", "fill_to": 9, "sale_price": "1.00"}))
    snap = dashboard.snapshot(page_conn)
    assert snap["status"]["latest_run"]["id"] == dry
    p = snap["plan"]["data"]
    assert (p["run"]["id"], p["previous_run"]) == (dry, None)     # never compared with the live plan
    assert [r["id"] for r in snap["runs"]["data"]] == [replay, dry, live]


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


def test_runs_list_what_each_plan_changed(conn, page_conn):
    a = agent_runs.start(conn, mode="live")
    agent_runs.finish(conn, a, "submitted", plan=plan({"slot": "15", "fill_to": 6, "sale_price": "2.50"},
                                                      {"slot": "28", "fill_to": 6}))
    b = agent_runs.start(conn, mode="live")
    agent_runs.finish(conn, b, "halted", detail="halted before submitting")
    c = agent_runs.start(conn, mode="live", model="m")
    agent_runs.finish(conn, c, "submitted", update="raise 15, pull 28", cost_usd=Decimal("0.44"),
                      plan=plan({"slot": "15", "fill_to": 8, "sale_price": "2.75"}, {"slot": "28", "fill_to": 0}))
    runs = {r["id"]: r for r in dashboard.snapshot(page_conn, MONDAY)["runs"]["data"]}
    assert runs[c]["changes"] == [{"slot": "15", "labels": ["$2.50 to $2.75", "fill 6 to 8"]},
                                  {"slot": "28", "labels": ["pull"]}]
    assert (runs[b]["has_plan"], runs[b]["changes"]) == (False, [])   # a run without a plan isn't compared
    assert (runs[a]["has_plan"], runs[a]["compared"]) == (True, False)
    page = dashboard.render(dashboard.snapshot(page_conn, MONDAY))
    assert "28 pull" in page and "halted before submitting" in page and "first plan of this mode" in page


def test_the_machine_shows_stock_and_sales_beside_the_plan(conn, page_conn):
    fulfilled(conn, "pr-1", "15", "COKE-20OZ", units_loaded=8, paid="9.60", to_back_stock=4,
              at=MONDAY - timedelta(days=3))
    store_poll(conn, "p1", (MONDAY - timedelta(days=4)).isoformat(), {"15": (250, 100)})
    store_poll(conn, "p2", (MONDAY - timedelta(days=2)).isoformat(), {"15": (250, 103)})
    store_poll(conn, "p3", (MONDAY - timedelta(hours=1)).isoformat(), {"15": (250, 105)})
    run = agent_runs.start(conn, mode="dry-run")
    agent_runs.finish(conn, run, "skipped", plan=plan({"slot": "15", "fill_to": 8, "sale_price": "2.75"}))
    snap = dashboard.snapshot(page_conn, MONDAY)
    held = snap["stock"]["data"]["slots"]["15"]
    assert (held["product"], held["loaded"], held["sold"], held["on_hand"]) == ("COKE-20OZ", 8, 5, 3)
    assert held["unit_cost"] == "0.80"                    # $9.60 over 12 units, exact
    assert snap["stock"]["data"]["closet"] == [{"product": "COKE-20OZ", "units": 4, "unit_cost": "0.80"}]
    assert snap["sales"]["data"]["slots"]["15"] == {"price": "2.50", "sold": 5, "per_day": "1.26"}   # 5 over 3 d 23 h
    assert snap["sales"]["data"]["stale"] is False
    assert snap["spend"]["data"]["drinks"]["paid"] == Decimal("9.60")
    page = dashboard.render(snap)
    assert "3 on hand of COKE-20OZ &middot; 5 sold this week" in page
    assert "<s>$2.50</s> <b>$2.75</b>" in page             # the tag now, and what the plan sets it to
    assert '"paid": "9.60"' in dashboard.to_json(snap)


def test_a_fresh_database_is_ok_but_explains_every_empty_panel(page_conn):
    snap = dashboard.snapshot(page_conn, MONDAY)
    assert dashboard.verdict(snap["attention"]) == "ok"   # halted, no polls: worth knowing, not wrong
    assert {a["level"] for a in snap["attention"]} == {"info"}
    page = dashboard.render(snap)
    for words in ("No plan yet", "No runs recorded yet", "No tool calls yet", "No machine polls stored yet",
                  "None submitted yet", "Nothing in back stock"):
        assert words in page, words
    assert page.count('data-slot="') == 20


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


def test_a_missing_poll_table_blanks_only_sales_and_stock(conn, page_conn):
    run = agent_runs.start(conn, mode="live")
    agent_runs.finish(conn, run, "submitted", plan=plan({"slot": "15", "keep": True}))
    conn.execute("drop table poll_selections, polls")
    snap = dashboard.snapshot(page_conn, MONDAY)
    assert snap["sales"]["ok"] is False and snap["stock"]["ok"] is False
    assert snap["plan"]["ok"] is True and snap["runs"]["ok"] is True
    assert any("Sales isn't available" in a["text"] for a in snap["attention"])
    assert "keep as is" in dashboard.render(snap)


def test_attention_text_from_the_database_is_escaped(conn, page_conn):
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
    (False, MONDAY - timedelta(hours=3), None, "turned off <time", " by"),
    (False, None, "kevin", "turned off by kevin", "&mdash;"),
    (True, None, None, "no halt row: counts as halted", "turned on"),
])
def test_the_halt_tile_reads_cleanly_without_who_or_when(page_conn, halted, at, by, words, absent):
    snap = dashboard.snapshot(page_conn, MONDAY)
    snap["status"].update(halted=halted, halt_changed_at=at, halt_changed_by=by)
    tile = dashboard._tiles(snap).split('<div class="tile">')[1]
    assert words in tile and absent not in tile


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
    ("purchase_lines", "units_loaded", {"stock", "purchase_requests"}),
])
def test_optional_readers_roll_back_only_their_panel(conn, page_conn, table, column, broken):
    conn.execute(f"alter table {table} drop column {column}")
    snap = dashboard.snapshot(page_conn)
    panels = ("plan", "runs", "activity", "recent_calls", "sales", "stock", "spend",
              "purchase_requests", "catalog_requests")
    assert {k for k in panels if not snap[k]["ok"]} == broken
    page = dashboard.render(snap)
    assert "Not available" in page or "isn&#x27;t available" in page
    assert page.count('class="slot ours') == len(dashboard.SLOTS)    # the machine still draws


EVEN = tuple(f"{r}{c}" for r in range(1, 6) for c in (2, 4, 6, 8))


@pytest.fixture
def even_columns(monkeypatch):
    """Our slots on every other column, so nothing assumes columns 5 to 8."""
    from vending.tools import machine_state
    monkeypatch.setattr(dashboard, "SLOTS", EVEN)
    monkeypatch.setattr(machine_state, "SLOTS", EVEN)
    return EVEN


def test_the_machine_lights_only_our_slots_from_slots(page_conn, even_columns):
    page = dashboard.render(dashboard.snapshot(page_conn, MONDAY))
    assert page.count('class="slot ours') == 20 and page.count('class="slot theirs"') == 20
    assert 'data-slot="12"' in page and 'data-slot="15"' not in page
    assert 'title="15: not our selection"' in page
    assert "No plan yet" in page and 'class="noplan v-next"' in page
    assert page.count("units unknown") >= 20                # no receipts: never a made-up count
    assert 'class="seg" role="group" aria-label="Show" hidden' in page   # the toggle needs JS


def test_now_and_next_friday(conn, page_conn, even_columns):
    fulfilled(conn, "pr-1", "12", "COKE-20OZ", units_loaded=6, paid="7.20", at=MONDAY - timedelta(days=3))
    store_poll(conn, "p1", (MONDAY - timedelta(days=4)).isoformat(), {"12": (250, 10), "14": (200, 5)})
    store_poll(conn, "p2", (MONDAY - timedelta(hours=1)).isoformat(), {"12": (250, 12), "14": (200, 5)})
    first = agent_runs.start(conn, mode="live")
    agent_runs.finish(conn, first, "submitted", plan=plan({"slot": "12", "fill_to": 6, "sale_price": "2.50"},
                                                          {"slot": "14", "keep": True},
                                                          {"slot": "16", "fill_to": 4}))
    second = agent_runs.start(conn, mode="live")
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
    assert '<s>2.50</s>2.75' in page                       # the tag next Friday, old price struck
    assert '<span class="lbl v-now">unknown</span>' in page
    assert "&lt;i&gt;sells out&lt;/i&gt;" in page and "<i>sells out</i>" not in page
    assert '<span class="chg bad">' in page and "pulled" in page
    assert "Stock is estimated from receipts and polls, not a sensor." in page
    assert "A plan is a proposal, not a restock receipt." in page
    assert "No balance or profit shown yet: the ledger (#35) has landed, and its panel is the next step." in page
    assert f"3 of 20 slots changed</b> since run #{first}" in page
    # The changed-only toggle: hidden until JS shows it, and the grid shows every slot without it.
    assert '<label class="only" hidden><input type="checkbox" id="changed-only">' in page
    assert 'class="plan-grid" data-only="false"' in page


@pytest.mark.parametrize("name, package", [
    ("COKE-20OZ", "20oz PET"), ("Sprite 20 oz bottle", "20oz PET"), ("Jarritos 16oz glass", "16oz glass"),
    ("Water 16 oz PET", "16oz PET"), ("Snapple 16 oz", None), ("Coke 12 oz can", "12oz can"),
    ("Coke 12 oz", None), ("Red Bull 250ml", "240/250ml can"), ("Starbucks 14 fl oz", "14oz PET"),
    ("Gatorade 28 oz", None), ("MTDEW", None), (None, None),
])
def test_package_from_the_name(name, package):
    assert dashboard._package(name) == package


def test_bottle_level_uses_capacity_only_when_the_package_is_known():
    assert dashboard._level(7, "COKE-20OZ", None) == 100
    assert dashboard._level(4, "Red Bull 250ml", 11) == 36       # 4 of 11
    assert dashboard._level(3, "MYSTERY", 6) == 50               # no package: against the fill
    assert dashboard._level(9, "MYSTERY", 6) == 100              # more than the fill: against itself
    assert dashboard._level(None, "COKE-20OZ", 6) == 0
    assert dashboard._colour("COKE-20OZ") == dashboard._colour(" coke-20oz ") != dashboard._colour("SPRITE-20OZ")


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


def test_the_plan_agrees_with_the_machine_about_a_counted_slot(conn, page_conn):
    from vending import slot_counts
    slot_counts.record(conn, slot_counts.parse({
        "count_id": "p1", "counted_at": "2026-11-09T11:30:00-05:00", "source": "photo", "device_id": "vmc1",
        "selections": [{"selection": "25", "product": "Vita Coco", "units": 3}]}), "kevin")
    run = agent_runs.start(conn, mode="live")
    agent_runs.finish(conn, run, "submitted", plan=plan({"slot": "25", "keep": True}))
    page = dashboard.render(dashboard.snapshot(page_conn))
    assert "3 on hand of Vita Coco" in page


def _view(held, counted):
    snap = {"plan": {"ok": False}, "sales": {"ok": False},
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

    from vending.stock import Movement, weighted_average
    utc = timezone.utc
    first, refill = datetime(2026, 11, 1, tzinfo=utc), datetime(2026, 11, 13, tzinfo=utc)
    found = weighted_average([
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


def test_runs_chips_read_a_keep_against_the_tag_like_the_plan_panel(conn, page_conn):
    store_poll(conn, "p1", (MONDAY - timedelta(hours=1)).isoformat(), {"28": (250, 10)})
    a = agent_runs.start(conn, mode="live")
    agent_runs.finish(conn, a, "submitted", plan=plan({"slot": "28", "keep": True}))
    b = agent_runs.start(conn, mode="live")
    agent_runs.finish(conn, b, "submitted", plan=plan({"slot": "28", "fill_to": 6, "sale_price": "3.00"}))
    runs = {r["id"]: r for r in dashboard.snapshot(page_conn, MONDAY)["runs"]["data"]}
    # Without the tag this read "set $3.00" here and "$2.50 to $3.00" in the Plan panel.
    assert runs[b]["changes"] == [{"slot": "28", "labels": ["$2.50 to $3.00"]}]
