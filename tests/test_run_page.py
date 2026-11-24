"""One live agent run, followed as it happens (`vending.run_page`), on the standalone dashboard and the pages."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from tests.conftest import TEST_SLOTS
from tests.test_dashboard import get, page_conn, read_only, served  # noqa: F401
from tests.test_demo import no_database  # noqa: F401
from vending import agent_runs, dashboard, ledger, pages, run_page
from vending.config import set_value

UTC = timezone.utc
NOW = datetime(2026, 12, 9, 15, tzinfo=UTC)
OPENED = NOW - timedelta(days=7)
SECRET = 'connection to server at "db.internal" failed for user "vending_rw"'
OUTSIDE = next(f"{r}{c}" for r in "12345" for c in "12345678" if f"{r}{c}" not in TEST_SLOTS)
A, B = TEST_SLOTS[0], TEST_SLOTS[1]


def open_books(conn):
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=OPENED)
    conn.execute("update slot_assignment set first_read_at = %s", (OPENED - timedelta(days=10),))


def run_at(conn, at, mode="live", status="running", **fields):
    run = agent_runs.start(conn, mode=mode, model="claude-opus-5-5")
    if status != "running":
        agent_runs.finish(conn, run, status, **fields)
    conn.execute("update agent_runs set started_at = %s, finished_at = case when status = 'running' then null"
                 " else %s end where id = %s", (at, at + timedelta(minutes=4), run))
    return run


def step(conn, run, turn, kind, content, *, tool=None, is_error=False, at=None):
    agent_runs.step(conn, run, turn, kind, content, tool=tool, is_error=is_error)
    if at is not None:
        conn.execute("update agent_steps set at = %s where id = (select max(id) from agent_steps)", (at,))


def model_call(conn, run, turn, cost, status="ok", harness="h1"):
    conn.execute("insert into model_calls (purpose, label, run_id, model, key_env, status, max_tokens,"
                 " worst_case_usd, cost_usd, recorded_by) values ('live', %s, %s, 'claude-opus-5-5',"
                 " 'VENDING_ANTHROPIC_API_KEY', %s, 1000, %s, %s, 'agent')",
                 (f"agent run {run} turn {turn}", harness, status, Decimal(cost), Decimal(cost)))


def desired(**entries):
    return json.dumps({"desired": {"slots": [{"slot": s, **e} for s, e in entries.items()], "off_cycle": False}})


def test_a_run_in_progress_shows_its_steps_drafts_and_spend_against_the_cap(conn, page_conn):  # noqa: F811
    open_books(conn)
    set_value(conn, "model_run_cap_cents", 200, "test")
    run = run_at(conn, NOW - timedelta(minutes=3))
    step(conn, run, 1, "thinking", "Checking what sold.")
    step(conn, run, 1, "tool_call", desired(**{A: {"product": "SAMPLE-CAN", "fill_to": 8, "sale_price": "2.50"}}),
         tool="preview_desired_state")
    step(conn, run, 1, "tool_result", json.dumps({"ok": False, "refusals": [{"code": "over_cap"}]}),
         tool="preview_desired_state")
    step(conn, run, 2, "tool_call", desired(**{A: {"product": "SAMPLE-CAN", "fill_to": 8, "sale_price": "2.75"}}),
         tool="preview_desired_state")
    step(conn, run, 2, "tool_result", json.dumps({"ok": True, "plan_hash": "abc"}), tool="preview_desired_state")
    model_call(conn, run, 1, "0.50")
    model_call(conn, run, 2, "0.90", status="pending")
    model_call(conn, run + 100, 1, "5.00", harness="other")       # another run's call: not this one's
    data = run_page.read(page_conn, run, NOW)
    assert data["found"] and data["shown"] and run_page.running(data)
    assert data["spend"] == {"spent": Decimal("1.40"), "calls": 2, "pending": 1}
    assert data["cap"] == {"usd": Decimal("2"), "set": True}
    assert [d["status"] for d in data["drafts"]] == ["refused", "accepted"]
    assert data["drafts"][1]["changes"] == [{"slot": A, "labels": ["$2.50 to $2.75"]}]
    assert data["plan"] is None and not data["before_opening"]
    page = run_page.html(data, json_href=f"/run/{run}.json")
    assert 'data-running="1"' in page and "<script>" in page and "checks every 5 s" in page
    assert "$1.40" in page and "$2.00 cap" in page and "1 at worst case" in page
    assert "latest draft" in page and "Draft 2" in page and "SAMPLE-CAN" in page
    assert '<details class="steps" open' in page and "Checking what sold." in page


def test_a_failed_run_is_complete_shows_no_error_text_and_stops_polling(conn, page_conn):  # noqa: F811
    open_books(conn)
    run = run_at(conn, NOW - timedelta(hours=1), status="failed", detail=SECRET, cost_usd=Decimal("0.31"))
    step(conn, run, 1, "tool_call", "{}", tool="get_machine_state")
    step(conn, run, 1, "tool_result", SECRET, tool="get_machine_state", is_error=True)
    step(conn, run, 2, "text", SECRET, is_error=True)
    for public in (True, False):
        data = run_page.read(page_conn, run, NOW, public=public)
        assert not run_page.running(data) and data["run"]["detail"] == dashboard.HIDDEN
        page, body = run_page.html(data), dashboard.to_json(data)
        assert SECRET not in page and SECRET not in body and "db.internal" not in page + body
        assert 'data-running="0"' in page and "<script>" not in page and "$0.31" in page
        assert "recorded at the end" in page and '<span class="pill bad">failed</span>' in page


def test_a_live_run_before_the_opening_is_marked_and_a_dry_run_is_not_shown(conn, page_conn):  # noqa: F811
    open_books(conn)
    early = run_at(conn, OPENED - timedelta(days=2), status="skipped", detail="nothing to do", cost_usd=Decimal("0.05"))
    data = run_page.read(page_conn, early, NOW)
    assert data["shown"] and data["before_opening"]
    assert "before the ledger opening: development spend, not the $600" in run_page.html(data)
    dry = run_at(conn, NOW - timedelta(hours=2), mode="dry-run", status="skipped")
    data = run_page.read(page_conn, dry, NOW)
    assert data["found"] and not data["shown"] and "run" not in data
    assert "Not on the live page: a dry-run run." in run_page.html(data)
    assert run_page.read(page_conn, 999999, NOW) == {"as_of": NOW, "run_id": 999999, "found": False}


def test_a_run_left_running_for_hours_is_called_probably_dead(conn, page_conn):  # noqa: F811
    open_books(conn)
    run = run_at(conn, NOW - timedelta(hours=3))
    data = run_page.read(page_conn, run, NOW)
    assert data["run"]["stale"] and not run_page.running(data)
    assert "probably died" in run_page.html(data) and "<script>" not in run_page.html(data)


def test_only_our_selections_reach_the_page(conn, page_conn):  # noqa: F811
    open_books(conn)
    run = run_at(conn, NOW - timedelta(minutes=5))
    step(conn, run, 1, "tool_call", "{}", tool="get_machine_state")
    step(conn, run, 1, "tool_result", json.dumps({"success": True, "data": [
        {"slot": A, "price": "2.50", "units_sold_in_window": 4},
        {"slot": OUTSIDE, "price": "9.87", "units_sold_in_window": 4321}]}), tool="get_machine_state")
    step(conn, run, 1, "thinking", f"Selection {OUTSIDE} sells 4321 a week.")
    step(conn, run, 2, "tool_call", desired(**{A: {"fill_to": 8}, OUTSIDE: {"fill_to": 9, "sale_price": "9.87"}}),
         tool="preview_desired_state")
    data = run_page.read(page_conn, run, NOW)
    for text in (run_page.html(data), dashboard.to_json(data)):
        assert "9.87" not in text and "4321" not in text and f"Selection {OUTSIDE}" not in text
    # The draft keeps our slot's entry; the entry for the selection that isn't ours is dropped.
    assert [s["slot"] for d in data["drafts"] if d["slots"] for s in d["slots"]] == [A]


def test_a_run_from_before_the_current_assignment_shows_no_steps_or_plan(conn, page_conn):  # noqa: F811
    open_books(conn)
    run = run_at(conn, OPENED - timedelta(days=20), status="submitted",
                 plan={"slots": [{"slot": A, "fill_to": 8, "reason": "old reason"}]}, request_id="r0")
    step(conn, run, 1, "thinking", "old thinking")
    data = run_page.read(page_conn, run, NOW)
    assert data["run"]["steps"] == [] and data["plan"] is None and data["drafts"] == []
    assert "old thinking" not in run_page.html(data) and "old reason" not in run_page.html(data)


def test_the_runs_panel_and_the_status_bar_link_to_the_run(conn, page_conn, monkeypatch):  # noqa: F811
    monkeypatch.setattr(dashboard, "lock_at", lambda: NOW - timedelta(days=30))
    open_books(conn)
    run = run_at(conn, NOW - timedelta(minutes=2))
    snap = dashboard.snapshot(page_conn, NOW, public=True)
    page = dashboard.render(snap, charts={"run": run_page.ROUTE})
    assert page.count(f'href="{run_page.ROUTE}{run}"') == 2
    assert f"Follow run #{run} live" in page and "Follow this run live" in page
    assert f'href="{run_page.ROUTE}{run}"' not in dashboard.render(snap)       # no server, no link


def test_the_paths_parse_strictly():
    assert run_page.parse("/dashboard/run/12", run_page.ROUTE) == (12, False)
    assert run_page.parse("/dashboard/run/12.json", run_page.ROUTE) == (12, True)
    for bad in ("/dashboard/run/", "/dashboard/run/x", "/dashboard/run/012", "/dashboard/run/-1",
                "/dashboard/run/1/2", "/dashboard/runs/1", "/dashboard/run/" + "9" * 19):
        assert run_page.parse(bad, run_page.ROUTE) is None, bad


def test_the_standalone_dashboard_serves_the_run_and_its_json(served, conn):  # noqa: F811
    open_books(conn)
    run = run_at(conn, datetime.now(UTC) - timedelta(minutes=1))
    status, kind, body = get(f"{served}/run/{run}")
    assert status == 200 and kind.startswith("text/html") and f"Run #{run}" in body
    status, kind, body = get(f"{served}/run/{run}.json")
    assert status == 200 and json.loads(body)["run"]["id"] == run
    assert get(f"{served}/run/999999")[0] == 404
    assert get(f"{served}/run/abc")[0] == 404
    assert f'href="/run/{run}"' in get(f"{served}/")[2]


def test_the_pages_answer_for_a_run_without_an_error_text(monkeypatch, capsys):
    monkeypatch.setenv(pages.READER, "set")

    def broken():
        raise dashboard.psycopg.OperationalError(SECRET)
    monkeypatch.setattr(pages, "connect", broken)
    reply = pages.respond("/dashboard/run/5")
    assert reply.status == 503 and SECRET not in reply.body and pages.CANT_READ.replace("'", "&#x27;") in reply.body
    reply = pages.respond("/dashboard/run/5.json")
    assert reply.status == 503 and json.loads(reply.body)["error"] == pages.CANT_READ
    assert pages.respond("/dashboard/run/x") is None


def test_a_long_draft_is_stored_whole_and_read_slot_by_slot(conn, page_conn):  # noqa: F811
    open_books(conn)
    run = run_at(conn, NOW - timedelta(minutes=5))
    link = "https://www.example.com/" + "p" * 400
    draft = {"desired": {"off_cycle": False, "slots": [{"slot": s, "keep": True} for s in TEST_SLOTS],
                         "sourcing": [{"product": f"SKU-{n}", "options": [{"store": "Walmart", "url": link}]}
                                      for n in range(20)]}}
    text = json.dumps(draft)
    assert agent_runs.MAX_STEP_TEXT < len(text) < agent_runs.MAX_CALL_TEXT
    step(conn, run, 1, "tool_call", text, tool="preview_desired_state")
    data = run_page.read(page_conn, run, NOW)
    assert [s["slot"] for s in data["drafts"][0]["slots"]] == list(TEST_SLOTS)
    agent_runs.step(conn, run, 2, "tool_call", "z" * 20_000, tool="preview_desired_state")
    data = run_page.read(page_conn, run, NOW)
    assert data["drafts"][1]["slots"] is None and data["drafts"][1]["unreadable"].startswith("arguments not readable")


def test_the_demo_serves_its_run_pages_from_the_file_alone(no_database):  # noqa: F811
    from vending import demo
    pages_in_file = demo.load()["run_pages"]
    running = next(k for k, d in pages_in_file.items() if d["run"]["status"] == "running")
    reply = pages.respond(f"/dashboard/demo/run/{running}")
    assert reply.status == 200 and dashboard.DEMO_BANNER in reply.body and "<script>" not in reply.body
    assert "Draft 2" in reply.body and "$2.00 cap" in reply.body
    reply = pages.respond(f"/dashboard/demo/run/{running}.json")
    assert reply.status == 200 and json.loads(reply.body)["demo"] is True
    assert pages.respond("/dashboard/demo/run/999999").status == 404
    assert f'href="{demo.DEMO_RUN}{running}"' in demo.page()
