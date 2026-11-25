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
    model_call(conn, run, 1, "0.50", harness=agent_runs.harness_key(run))
    model_call(conn, run, 2, "0.90", status="pending", harness=agent_runs.harness_key(run))
    model_call(conn, run + 100, 1, "5.00", harness=agent_runs.harness_key(run + 100))   # another run's call
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


# --- Review round (Sol, #247) ----------------------------------------------------------------------

def test_draft_chips_come_from_typed_scoped_entries_only(conn, page_conn):  # noqa: F811
    open_books(conn)
    run_at(conn, NOW - timedelta(hours=6), status="submitted", request_id="r1", plan={"slots": [
        {"slot": A, "keep": True, "sale_price": f"as selection {OUTSIDE}"},
        {"slot": B, "fill_to": 8, "sale_price": f"selection {OUTSIDE} price"}]})
    run = run_at(conn, NOW - timedelta(minutes=5))
    step(conn, run, 1, "tool_call", desired(**{A: {"keep": True, "sale_price": "2.75"}, B: {"fill_to": 8, "sale_price": "2.50"}}),
         tool="preview_desired_state")
    data = run_page.read(page_conn, run, NOW)
    for text in (run_page.html(data), dashboard.to_json(data)):
        assert f"selection {OUTSIDE}" not in text
    changes = {c["slot"]: c["labels"] for c in data["drafts"][0]["changes"]}
    assert A not in changes                    # a kept slot: its price and fill are ignored
    assert all(OUTSIDE not in label for labels in changes.values() for label in labels)


def test_a_refresh_reads_only_steps_after_the_last_one_it_has(conn, page_conn):  # noqa: F811
    open_books(conn)
    run = run_at(conn, NOW - timedelta(minutes=5))
    for n in range(5):
        step(conn, run, n + 1, "thinking", f"thought {n}")
    first = run_page.read(page_conn, run, NOW)
    last = first["run"]["steps"][-1]["id"]
    step(conn, run, 6, "thinking", "a new thought")
    more = run_page.read(page_conn, run, NOW, after=last)
    assert [s["content"] for s in more["run"]["steps"]] == ["a new thought"]
    assert more["counts"] == {"steps": 6, "turns": 6, "calls": 0} and more["after"] == last
    page = run_page.html(more)
    assert "thought 0" not in page and "a new thought" in page and f'data-last="{last + 1}"' in page


def test_spend_is_read_by_the_run_s_own_harness_key(conn, page_conn):  # noqa: F811
    open_books(conn)
    run = run_at(conn, NOW - timedelta(minutes=5))
    model_call(conn, run, 1, "0.25", harness=agent_runs.harness_key(run))
    model_call(conn, run, 2, "0.40", status="pending", harness=agent_runs.harness_key(run))
    model_call(conn, run, 3, "9.00", harness="someone-else")     # labelled like this run's, but another harness
    data = run_page.read(page_conn, run, NOW)
    assert data["spend"] == {"spent": Decimal("0.65"), "calls": 2, "pending": 1}
    # Read through the run_id index: with sequential scans off, the plan still finds the rows by index.
    with page_conn.transaction():
        page_conn.execute("set local enable_seqscan = off")
        plan = " ".join(r[0] for r in page_conn.execute(
            "explain select sum(cost_usd) from model_calls where run_id = %s", (agent_runs.harness_key(run),)))
    assert "model_calls_run" in plan


def test_long_draft_calls_are_cut_on_the_page_not_in_the_record(conn, page_conn):  # noqa: F811
    open_books(conn)
    run = run_at(conn, NOW - timedelta(minutes=5))
    big = desired(**{s: {"keep": True, "reason": "r" * 600} for s in TEST_SLOTS})
    assert len(big) > 10_000
    for n in range(15):
        step(conn, run, n + 1, "tool_call", big, tool="preview_desired_state")
    data = run_page.read(page_conn, run, NOW)
    assert len(data["run"]["steps"][0]["content"]) <= run_page.SHOWN_CHARS + 200
    assert len(run_page.html(data)) < 120_000
    assert page_conn.execute("select max(length(content)) from agent_steps").fetchone()[0] == len(big)


def test_parallel_previews_pair_each_reply_with_its_own_call(conn, page_conn):  # noqa: F811
    open_books(conn)
    run = run_at(conn, NOW - timedelta(minutes=5))
    agent_runs.step(conn, run, 1, "tool_call", desired(**{A: {"fill_to": 8}}), tool="preview_desired_state",
                    tool_use_id="first")
    agent_runs.step(conn, run, 1, "tool_call", desired(**{A: {"fill_to": 9}}), tool="preview_desired_state",
                    tool_use_id="second")
    # The second call's reply is stored first.
    agent_runs.step(conn, run, 1, "tool_result", json.dumps({"ok": True}), tool="preview_desired_state",
                    tool_use_id="second")
    agent_runs.step(conn, run, 1, "tool_result", json.dumps({"ok": False}), tool="preview_desired_state",
                    tool_use_id="first")
    data = run_page.read(page_conn, run, NOW)
    assert [d["status"] for d in data["drafts"]] == ["refused", "accepted"]


def test_an_unmigrated_database_answers_the_fixed_503(monkeypatch):
    monkeypatch.setenv(pages.READER, "set")

    def missing(*args, **kwargs):
        raise dashboard.Missing('relation "agent_steps" does not exist')
    monkeypatch.setattr(pages, "_connection", _fake_connection)
    monkeypatch.setattr(run_page, "read", missing)
    for path, kind in (("/dashboard/run/5", "text/html"), ("/dashboard/run/5.json", "application/json")):
        reply = pages.respond(path)
        assert reply.status == 503 and reply.content_type.startswith(kind) and "agent_steps" not in reply.body


def _fake_connection():
    from contextlib import contextmanager

    @contextmanager
    def opened():
        yield None
    return opened()


def test_the_standalone_page_answers_503_on_an_unmigrated_database(served, monkeypatch):  # noqa: F811
    def missing(*args, **kwargs):
        raise dashboard.Missing('relation "agent_steps" does not exist')
    monkeypatch.setattr(run_page, "read", missing)
    status, _, body = get(f"{served}/run/5")
    assert status == 503


def test_a_refresh_page_carries_only_what_is_new(conn, page_conn):  # noqa: F811
    open_books(conn)
    run = run_at(conn, NOW - timedelta(minutes=5))
    agent_runs.step(conn, run, 1, "tool_call", desired(**{A: {"fill_to": 8}}), tool="preview_desired_state",
                    tool_use_id="u1")
    first = run_page.read(page_conn, run, NOW)
    page = run_page.html(first)
    assert 'data-use="u1"' in page and ">waiting<" in page and 'data-after="0"' in page
    # Next: u1's reply arrives, and a thought that changes nothing in the plan.
    agent_runs.step(conn, run, 1, "tool_result", json.dumps({"ok": True}), tool="preview_desired_state",
                    tool_use_id="u1")
    agent_runs.step(conn, run, 2, "thinking", "next thought")
    later = run_page.read(page_conn, run, NOW, after=first["last"])
    assert later["drafts"] == [] and later["replies"] == [{"use": "u1", "status": "accepted"}]
    page = run_page.html(later)
    steps = page.split('<ol id="rp-steps">')[1].split("</ol>")[0]
    assert "next thought" in steps and "Called" not in steps
    assert '<div id="rp-plan" data-keep="1">' in page
    assert '<template id="rp-replies"><li data-use="u1"><span class="pill ok">accepted</span></li></template>' in page
    # A new draft numbers on from the ones shown and compares with the last one shown.
    agent_runs.step(conn, run, 3, "tool_call", desired(**{A: {"fill_to": 9}}), tool="preview_desired_state",
                    tool_use_id="u2")
    newest = run_page.read(page_conn, run, NOW, after=later["last"])
    assert [(d["n"], d["changes"]) for d in newest["drafts"]] == [(2, [{"slot": A, "labels": ["fill 8 to 9"]}])]
