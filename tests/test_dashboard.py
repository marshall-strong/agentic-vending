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

from tests.conftest import CONNECT_TIMEOUT
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
    assert "Swapping 16 to Mountain Dew." in page and "MTDEW-20OZ" in page
    assert "$2.25 &middot; fill to 4" in page and "was: keep as is" in page
    assert "1 slot changed." in page and page.count('class="cell changed"') == 1


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
    page = dashboard.render(snap)
    assert '<tr class="refused">' in page and "probably died" in page and "RUNNING" in page


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


@pytest.mark.parametrize("now, before, labels", [
    ({"slot": "16", "product": "MTDEW", "fill_to": 4}, {"slot": "16", "keep": True}, ["swap", "change"]),
    ({"slot": "15", "fill_to": 8, "sale_price": "2.75"}, {"slot": "15", "fill_to": 6, "sale_price": "2.50"},
     ["price", "fill 6 to 8"]),
    ({"slot": "15", "fill_to": 0}, {"slot": "15", "fill_to": 6}, ["pull"]),
    ({"slot": "15", "keep": True}, None, ["new"]),
    ({"slot": "15", "sale_price": Decimal("2.50")}, {"slot": "15", "sale_price": "2.50"}, ["changed"]),
])
def test_change_labels(now, before, labels):
    assert dashboard._changes(now, before) == labels
