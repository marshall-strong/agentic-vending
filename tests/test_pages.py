"""The public pages (#53): served by the worker, on a login that can only read, with no error text."""

import json
import re
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from html import unescape

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from tests.conftest import CONNECT_TIMEOUT, TEST_NOW, open_in, store_poll
from tests.test_dashboard import fulfilled, get, served, shows  # noqa: F401 (the standalone server fixture)
from tests.test_plan_rounds import day_one, projection, write
from tests.test_plan_store import unassigned_slot
from vending import agent_runs, dashboard, db, db_explorer, ledger, pages, plan_store, settings, slots, worker
from vending.write_gate import log

SECRET = 'connection to server at "db.example.net", port 5432 failed: password authentication failed for user "g2_admin"'


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    """Each test starts with no passed read-only check, nothing logged yet, and no pages login,
    not even one in the developer's `.env` (which `settings.get` falls back to)."""
    monkeypatch.setattr(pages, "_read_only_until", 0.0)
    monkeypatch.setattr(pages, "_logged", {})
    monkeypatch.setattr(pages, "_cache", {})
    monkeypatch.delenv(pages.READER, raising=False)
    monkeypatch.delitem(settings._FILE, pages.READER, raising=False)


def test_a_pages_login_in_the_env_file_is_ignored_by_the_tests():
    assert not pages.configured() and settings.get(pages.READER) is None


def _role(admin):
    """A new login that may not even look inside the test schema; `grant` has to give it that.
    Skips when the test database's user may not create roles."""
    name = f"reader_{uuid.uuid4().hex[:8]}"
    try:
        admin.execute(sql.SQL("create role {} login password 'reader'").format(sql.Identifier(name)))
    except psycopg.errors.InsufficientPrivilege:
        pytest.skip("the test database's user can't create roles")
    return name


def _drop(admin, name):
    admin.execute(sql.SQL("drop owned by {}").format(sql.Identifier(name)))
    admin.execute(sql.SQL("drop role {}").format(sql.Identifier(name)))


def _as(url, schema, name, options=pages.OPTIONS):
    return psycopg.connect(make_conninfo(url, user=name, password="reader"), autocommit=True,
                           connect_timeout=CONNECT_TIMEOUT, options=f"{options} -c search_path={schema}")


@pytest.fixture
def reader(schema_url, conn):
    """The name of a role that can read the test schema and nothing more, as `grant` sets one up."""
    url, schema = schema_url
    with psycopg.connect(url, autocommit=True, connect_timeout=CONNECT_TIMEOUT) as admin:
        name = _role(admin)
        pages.grant(conn, name, "test")
        yield name
        _drop(admin, name)


@pytest.fixture
def site(schema_url, reader, monkeypatch):
    """The worker's web server, with the pages on the reader login."""
    url, schema = schema_url
    monkeypatch.setenv(pages.READER, "set")
    monkeypatch.setattr(pages, "connect", lambda: _as(url, schema, reader))
    server = worker.serve_health(worker.State(), 0)
    yield server
    server.shutdown()
    server.server_close()


def _get(server, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}{path}", timeout=20) as reply:
            return reply.status, reply.headers, reply.read().decode()
    except urllib.error.HTTPError as failing:
        return failing.code, failing.headers, failing.read().decode()


# --- Served by the worker -----------------------------------------------------------------------

def test_the_worker_serves_the_dashboard_its_json_and_the_explorer(site, schema_url, conn):
    _, schema = schema_url
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=datetime.now(timezone.utc) - timedelta(minutes=1))
    run = agent_runs.start(conn, mode="live")
    agent_runs.finish(conn, run, "skipped", detail="nothing to restock")

    status, headers, page = _get(site, "/dashboard")
    assert status == 200 and headers["Content-Type"] == "text/html; charset=utf-8" and "Agent dashboard" in page
    # The JSON link and the way home are absolute, so they work whether or not the URL ends in a slash.
    assert 'href="/dashboard/state.json"' in page and '<a href="/">Home</a>' in page and '<a href="/db">Database</a>' in page
    # The dashboard refreshes itself with its own script, from this address only.
    assert "script-src 'unsafe-inline'" in headers["Content-Security-Policy"]
    assert "connect-src 'self'" in headers["Content-Security-Policy"] and headers["Cache-Control"] == "no-store"
    assert _get(site, "/dashboard/")[0] == 200

    status, headers, body = _get(site, "/dashboard/state.json")
    assert status == 200 and headers["Content-Type"] == "application/json"
    assert json.loads(body)["runs"]["data"][0]["detail"] == "nothing to restock"

    status, headers, page = _get(site, "/db")
    assert status == 200 and "action_log" in page and "Choose a table" in page
    assert "script-src" not in headers["Content-Security-Policy"] and "<script" not in page
    # Links are relative, so under /db they stay under /db.
    assert f'href="?schema={schema}&amp;table=config"' in page and 'href="/?' not in page
    assert '<a href="/dashboard">Agent dashboard</a>' in page
    status, _, page = _get(site, f"/db?schema={schema}&table=config")
    assert status == 200 and "halted" in page
    assert _get(site, f"/db?schema={schema}&table=nope")[0] == 404
    assert _get(site, "/db?sql=select")[0] == 400
    assert _get(site, "/dbx")[0] == 404 and _get(site, "/dashboard/other")[0] == 404


def test_nothing_can_be_sent(site):
    request = urllib.request.Request(f"http://127.0.0.1:{site.server_address[1]}/db", data=b"x=1", method="POST")
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(request, timeout=20)
    assert refused.value.code == 405  # only /login takes a POST (vending.gate)


def test_the_home_page_links_to_the_pages_only_when_they_are_set_up(monkeypatch):
    server = worker.serve_health(worker.State(), 0)
    try:
        _, _, page = _get(server, "/")
        assert "aren't set up at this address yet" in page.replace("&#x27;", "'") and 'href="/dashboard"' not in page
        for path in ("/dashboard", "/db"):
            status, _, body = _get(server, path)
            assert status == 503 and "set up here yet" in body.replace("&#x27;", "'")
        status, headers, body = _get(server, "/dashboard/state.json")
        assert status == 503 and headers["Content-Type"] == "application/json" and "error" in json.loads(body)
        monkeypatch.setenv(pages.READER, "set")
        _, _, page = _get(server, "/")
        assert 'href="/dashboard"' in page and 'href="/db"' in page and "<script" not in page
    finally:
        server.shutdown()
        server.server_close()
    assert worker.PAGES_SETTING == pages.READER


# --- Read-only in layers ------------------------------------------------------------------------

def test_the_reader_cannot_write_even_with_the_session_guard_off(schema_url, reader):
    url, schema = schema_url
    with _as(url, schema, reader) as conn:
        assert pages.writable(conn) == [] and pages.unreadable(conn) == []
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("update config set value = 'false' where key = 'halted'")
    # Layer 1 on its own: the same login without the read-only session still can't write.
    with _as(url, schema, reader, options="-c default_transaction_read_only=off") as conn:
        for statement in ("update config set value = 'false' where key = 'halted'",
                          "insert into action_log (kind, tool, arguments, outcome) values ('read', 't', '{}', 'done')",
                          "delete from polls", "truncate config", "create table mine (id int)"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)


def test_a_login_that_can_write_switches_the_pages_off(schema_url, monkeypatch, capsys):
    url, schema = schema_url
    monkeypatch.setenv(pages.READER, "set")
    # The syncs' own login, even on a read-only session: the mistake the check is for.
    monkeypatch.setattr(pages, "connect", lambda: psycopg.connect(
        url, autocommit=True, connect_timeout=CONNECT_TIMEOUT, options=f"{pages.OPTIONS} -c search_path={schema}"))
    for path in ("/dashboard", "/db", f"/db?schema={schema}&table=config"):
        reply = pages.respond(path)
        assert reply.status == 503 and "switched off" in reply.body and "halted" not in reply.body
        assert schema not in reply.body
    reply = pages.respond("/dashboard/state.json")
    assert reply.status == 503 and "switched off" in json.loads(reply.body)["error"]
    logged = capsys.readouterr().err
    assert f"{schema}.config" in logged or "more" in logged
    assert logged.count("pages:") == 1   # said once, not once per request


def test_a_passed_check_is_trusted_for_a_while_then_made_again(schema_url, reader, monkeypatch):
    url, schema = schema_url
    monkeypatch.setenv(pages.READER, "set")
    monkeypatch.setattr(pages, "connect", lambda: _as(url, schema, reader))
    asked = []
    real = pages.writable
    monkeypatch.setattr(pages, "writable", lambda conn: asked.append(1) or real(conn))
    assert pages.respond("/db").status == 200 and pages.respond("/db").status == 200
    assert len(asked) == 1
    monkeypatch.setattr(pages, "_read_only_until", 0.0)
    assert pages.respond("/db").status == 200 and len(asked) == 2


# --- No error text ------------------------------------------------------------------------------

def test_public_dashboard_panel_errors_go_to_stderr(site, conn, capsys):
    conn.execute("drop table product_requests cascade")
    fixed = "This part of the page can't be read right now."
    for path in ("/dashboard", "/dashboard/state.json"):
        status, _, body = _get(site, path)
        assert status == 200
        assert fixed in body.replace("&#x27;", "'")
        for marker in ("product_requests", "does not exist", "LINE 1:", "select id, requested_at"):
            assert marker not in body
    state = json.loads(body)
    assert state["catalog_requests"] == state["catalog_counts"] == {"ok": False, "why": fixed}
    assert 'relation "product_requests" does not exist' in capsys.readouterr().err
    local = dashboard.snapshot(conn)
    assert 'relation "product_requests" does not exist' in local["catalog_requests"]["why"]
    assert "python -m vending.db migrate" in dashboard.render(local)


@pytest.mark.parametrize("source", ["old_write", "scoped_step"])
def test_public_dashboard_omits_retired_selection_text(site, conn, source):
    now = conn.execute("select now()").fetchone()[0]
    before, reassigned = now - timedelta(minutes=10), now - timedelta(minutes=5)
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=before)
    conn.execute("update slot_assignment set slots = array_remove(slots, '15') || array['11'], "
                 "first_read_at = %s, last_checked_at = %s", (reassigned, reassigned))
    store_poll(conn, "assignment-control", now.isoformat(), {"15": (8193, 1), "16": (256, 1)})
    if source == "old_write":
        log(conn, "write", "config.set", {"key": "wildcard_slot", "value": "15"}, "done", "RETIRED-CONFIG slot 15")
        conn.execute("update action_log set at = %s where tool = 'config.set'", (before,))
        owned = {"key": "wildcard_slot", "value": "16"}
        log(conn, "write", "config.set", owned, "done", "OWNED-CONTROL slot 16")
        markers = ("RETIRED-CONFIG", '"value": "15"', "81.93")
    else:
        run = agent_runs.start(conn, mode="live")
        payload = {"slots": [{"slot": "15", "product": "RETIRED-ROW"}, {"slot": "16", "product": "OWNED-CONTROL"}],
                   **{key: f"RETIRED-{key} slot 15" for key in ("directive", "decision_note", "notes")}}
        agent_runs.step(conn, run, 1, "tool_result", json.dumps(payload), tool="get_purchase_requests")
        markers = ("RETIRED-ROW", "RETIRED-directive", "RETIRED-decision_note", "RETIRED-notes", "81.93")
    status, _, page = _get(site, "/dashboard")
    assert status == 200 and "OWNED-CONTROL" in page
    _, _, body = _get(site, "/dashboard/state.json")
    assert "OWNED-CONTROL" in body
    attrs = re.findall(r'(?:aria-label|title)="([^"]*)"', page)
    assert any("2.56" in attr for attr in attrs)
    for marker in markers:
        assert marker not in page and marker not in body
        assert all(marker not in attr for attr in attrs)
    if source == "old_write":
        audit = [row for row in json.loads(body)["activity"]["data"] if row["tool"] == "config.set"]
        assert [row["arguments"] for row in audit] == [owned, {}]


def test_public_dashboard_has_no_spend_error_before_opening(site, capsys):
    for path in ("/dashboard", "/dashboard/state.json"):
        status, _, body = _get(site, path)
        assert status == 200
        assert dashboard.PUBLIC_PANEL_ERROR not in body.replace("&#x27;", "'")
        assert "Spent so far" not in body and "Spent before the opening" not in body
    assert json.loads(body)["spend"] == {"ok": True, "data": None}
    assert capsys.readouterr().err == ""


def test_public_dashboard_keeps_submit_confirmation_without_later_error(site, schema_url, conn):
    _, schema = schema_url
    now = conn.execute("select now()").fetchone()[0]
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=now - timedelta(minutes=1))
    run = agent_runs.start(conn, mode="live")
    confirmation = "Submitted purchase request test-123"
    detail = f"{confirmation}; afterwards: OperationalError: connection to db-private.example.test failed"
    agent_runs.finish(conn, run, "submitted", detail=detail, request_id="test-123")

    for path in ("/dashboard", "/dashboard/state.json", f"/db?schema={schema}&table=agent_runs"):
        status, _, body = _get(site, path)
        assert status == 200 and confirmation in body, path
        assert "db-private.example.test" not in body and "OperationalError" not in body, path
        assert "afterwards" not in body, path
        if path == "/dashboard/state.json":
            state = json.loads(body)
            assert state["status"]["latest_run"]["detail"] == state["runs"]["data"][0]["detail"] == confirmation
    assert dashboard.snapshot(conn)["runs"]["data"][0]["detail"] == detail
    # The team's own explorer and the database keep the whole detail.
    with open_in(*schema_url) as local:
        assert "db-private.example.test" in db_explorer.render(local, db_explorer.Request(schema, "agent_runs", 0))[1]
        assert local.execute("select detail from agent_runs where id = %s", (run,)).fetchone()[0] == detail


def test_failures_keep_their_error_text_to_themselves(site, schema_url, conn):
    _, schema = schema_url
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=datetime.now(timezone.utc) - timedelta(minutes=1))
    log(conn, "read", "get_machine_state", {}, "failed", f"OperationalError: {SECRET}")
    conn.execute("update action_log set result = %s where outcome = 'failed'",
                 (psycopg.types.json.Jsonb({"success": False, "message": SECRET}),))
    log(conn, "write", "submit_desired_state", {}, "refused", "halted: turn the switch off first")
    log(conn, "read", "get_catalog", {}, "done", "26 products")
    run = agent_runs.start(conn, mode="live")
    agent_runs.step(conn, run, 1, "tool_result", "FAILED-STEP-MARKER", tool="get_machine_state", is_error=True)
    agent_runs.step(conn, run, 1, "tool_result", "SUCCESSFUL-STEP-MARKER", tool="get_catalog")
    agent_runs.finish(conn, run, "failed", detail=SECRET)

    pages_seen = [_get(site, path)[2] for path in (
        "/dashboard", "/dashboard/state.json", f"/db?schema={schema}&table=action_log",
        f"/db?schema={schema}&table=agent_runs")]
    for body in pages_seen:
        assert "db.example.net" not in body and "g2_admin" not in body and "password authentication" not in body
        assert "isn't shown on this public page" in body.replace("&#x27;", "'")
    # Refusals and successes are our own words, and stay.
    assert "halted: turn the switch off first" in pages_seen[0] and "halted: turn the switch off first" in pages_seen[2]
    assert "26 products" in pages_seen[2]
    for body in pages_seen[:2]:
        assert "FAILED-STEP-MARKER" not in body and "SUCCESSFUL-STEP-MARKER" in body
    with open_in(*schema_url) as local:
        assert "db.example.net" not in dashboard.to_json(dashboard.snapshot(local))
        assert "db.example.net" in db_explorer.render(local, db_explorer.Request(schema, "action_log", 0))[1]


# A fake database host and connection string, planted in failed steps (#165).
STEP_HOST = "vend-db-7f3a.postgres.example.internal"
STEP_URL = f"postgresql://vend_admin:Sw0rdf1sh@{STEP_HOST}:5432/vending"


@pytest.mark.parametrize("kind", ["tool_result", "tool_call", "text", "thinking"])
def test_a_failed_step_of_any_kind_keeps_its_error_text_off_every_public_page(site, schema_url, conn, kind):
    _, schema = schema_url
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=datetime.now(timezone.utc) - timedelta(minutes=1))
    run = agent_runs.start(conn, mode="live")
    tool = None if kind in ("thinking", "text") else "get_machine_state"
    agent_runs.step(conn, run, 1, kind, f'OperationalError: connection to server at "{STEP_HOST}" failed; '
                    f"tried {STEP_URL}", tool=tool, is_error=True)
    agent_runs.step(conn, run, 1, "tool_result", "SUCCESSFUL-STEP-MARKER", tool="get_catalog")
    agent_runs.finish(conn, run, "skipped", detail="nothing to buy")

    page, body, explorer = (_get(site, path)[2] for path in (
        "/dashboard", "/dashboard/state.json", f"/db?schema={schema}&table=agent_steps"))
    for text in (page, body, explorer):
        assert STEP_HOST not in text and "Sw0rdf1sh" not in text and "postgresql://" not in text
        assert "SUCCESSFUL-STEP-MARKER" in text
        assert "isn't shown on this public page" in unescape(text)
    steps = json.loads(body)["runs"]["data"][0]["steps"]
    assert [s["content"] for s in steps] == [dashboard.HIDDEN, "SUCCESSFUL-STEP-MARKER"]
    # The database keeps the text, for whoever reads it with the team's own login.
    with open_in(*schema_url) as local:
        assert STEP_HOST in db_explorer.render(local, db_explorer.Request(schema, "agent_steps", 0))[1]


def test_db_shows_a_fixed_line_for_a_submission_not_the_bridges_own_words(site, schema_url, conn):
    """#165: a bridge 5xx excerpt can carry an upstream database error, and a resolved row keeps
    the text it had. On /db, `purchase_submissions.detail` is a fixed line for its status."""
    _, schema = schema_url
    leak = f'the bridge answered 502: OperationalError: connection to server at "{STEP_HOST}" failed; {STEP_URL}'
    for status, detail in (("unknown", leak), ("refused", f"422: {leak}"), ("not_filed", f"Could not reach {leak}"),
                           ("accepted", leak), ("accepted", None), ("sending", None)):
        conn.execute("insert into purchase_submissions (plan_hash, body, status, detail) values ('h', '{}', %s, %s)",
                     (status, detail))

    status, _, body = _get(site, f"/db?schema={schema}&table=purchase_submissions")
    shown = unescape(body)
    assert status == 200
    assert STEP_HOST not in shown and "Sw0rdf1sh" not in shown and "postgresql://" not in shown
    assert "OperationalError" not in shown
    for line in pages.SUBMISSION_DETAIL.values():
        assert line in shown
    # The team's own explorer and the database keep the bridge's words.
    with open_in(*schema_url) as local:
        assert STEP_HOST in db_explorer.render(local, db_explorer.Request(schema, "purchase_submissions", 0))[1]
        assert local.execute("select count(*) from purchase_submissions where detail = %s", (leak,)).fetchone()[0] == 2


def test_db_withholds_why_a_bench_case_was_not_judged(site, schema_url, conn):
    """#165: `bench_runs.judge_notes` keeps, per case, the judge's note or why it couldn't judge it,
    and that `problem` can be a database error. /db shows a fixed line in its place."""
    _, schema = schema_url
    leak = f'not judged: OperationalError: connection to server at "{STEP_HOST}" failed; {STEP_URL}'
    notes = {"week-1": {"problem": leak}, "week-2": {"note": "JUDGE-NOTE-MARKER"}, "week-3": leak}
    conn.execute("insert into bench_runs (judge_notes) values (%s)", (Jsonb(notes),))
    conn.execute("insert into bench_runs (judge_notes) values (%s)", (Jsonb(leak),))

    status, _, body = _get(site, f"/db?schema={schema}&table=bench_runs")
    shown = unescape(body)
    assert status == 200
    assert STEP_HOST not in shown and "Sw0rdf1sh" not in shown and "postgresql://" not in shown
    assert "OperationalError" not in shown
    assert "JUDGE-NOTE-MARKER" in shown and "week-1" in shown and pages.BENCH_PROBLEM in shown
    with open_in(*schema_url) as local:
        assert STEP_HOST in db_explorer.render(local, db_explorer.Request(schema, "bench_runs", 0))[1]
        assert local.execute("select judge_notes from bench_runs order by id limit 1").fetchone()[0] == notes


def test_the_served_dashboard_names_the_workers_failing_syncs_without_their_error_text(site):
    # The worker keeps its rounds in memory; the dashboard it serves gets them as /health has them.
    assert not any("hourly sync" in a["text"] for a in json.loads(_get(site, "/dashboard/state.json")[2])["attention"])
    state = site.RequestHandlerClass.state
    state.results = worker.run_round([("sync_history", lambda: 2), ("sync_requests", lambda: 0),
                                      ("sync_catalog", lambda: 1 / 0)], log=lambda line: None)
    state.rounds, state.last_round_at = 1, state.started_at
    status, _, body = _get(site, "/dashboard/state.json")
    found = [a for a in json.loads(body)["attention"] if "hourly sync" in a["text"]]
    assert status == 200 and [a["level"] for a in found] == ["bad"]
    assert "`sync_history` exit 2, `sync_catalog` crashed" in found[0]["text"]
    assert "python -m vending.db migrate" in found[0]["text"]
    # The crash's own text stays in the log, as on /health.
    assert "ZeroDivisionError" not in body and "division by zero" not in body
    page = _get(site, "/dashboard")[2]
    assert "<code>sync_history</code> exit 2" in page and "division by zero" not in page


def test_a_hidden_value_never_leaves_postgres_and_null_stays_null(schema_url, conn):
    _, schema = schema_url
    log(conn, "read", "get_pnl", {}, "failed", None)
    log(conn, "read", "get_pnl", {}, "failed", SECRET)
    table = db_explorer.Table(schema, "action_log", "table")
    with open_in(*schema_url) as local:
        page = db_explorer.rows(local, table, hide=pages.HIDE["action_log"])
    detail = [c.name for c in page.columns].index("detail")
    assert [row[detail] for row in page.rows] == [dashboard.HIDDEN, None]
    # Without the column that decides, every row's value is hidden.
    always = db_explorer.Hide("no_such_column", "failed", ("detail",), "hidden")
    with open_in(*schema_url) as local:
        page = db_explorer.rows(local, table, hide=always)
    assert [row[detail] for row in page.rows] == ["hidden", None]


def test_the_pages_own_errors_go_to_the_log_not_the_page(monkeypatch, capsys):
    monkeypatch.setenv(pages.READER, "set")

    def down():
        raise psycopg.OperationalError(SECRET)
    monkeypatch.setattr(pages, "connect", down)
    for path in ("/dashboard", "/dashboard/state.json", "/db"):
        reply = pages.respond(path)
        assert reply.status == 503 and "db.example.net" not in reply.body and "g2_admin" not in reply.body
    assert "db.example.net" in capsys.readouterr().err

    monkeypatch.setattr(pages, "connect", lambda: 1 / 0)
    reply = pages.respond("/db")
    assert reply.status == 500 and "division" not in reply.body
    assert "ZeroDivisionError" in capsys.readouterr().err


# --- Load limits --------------------------------------------------------------------------------

def test_a_request_that_cant_get_a_connection_is_told_the_page_is_busy(monkeypatch):
    monkeypatch.setenv(pages.READER, "set")
    monkeypatch.setattr(pages, "WAIT_SECONDS", 0.05)
    monkeypatch.setattr(pages, "connect", lambda: pytest.fail("no connection should be opened"))
    taken = threading.BoundedSemaphore(1)
    taken.acquire()
    monkeypatch.setattr(pages, "_slots", taken)
    reply = pages.respond("/dashboard")
    assert reply.status == 503 and "busy" in reply.body
    taken.release()


def test_at_most_a_few_connections_are_open_at_once(schema_url, reader, monkeypatch):
    url, schema = schema_url
    monkeypatch.setenv(pages.READER, "set")
    open_now, most, lock = [0], [0], threading.Lock()

    class Counted:
        def __init__(self):
            self.conn = _as(url, schema, reader)

        def __enter__(self):
            with lock:
                open_now[0] += 1
                most[0] = max(most[0], open_now[0])
            return self.conn

        def __exit__(self, *exc):
            with lock:
                open_now[0] -= 1
            self.conn.close()

    monkeypatch.setattr(pages, "connect", Counted)
    replies = []
    threads = [threading.Thread(target=lambda: replies.append(pages.respond("/dashboard").status)) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert replies == [200] * 8 and 1 <= most[0] <= pages.MAX_CONNECTIONS


def test_the_pages_connect_read_only_with_the_timeout_and_their_own_setting(monkeypatch):
    seen = {}
    monkeypatch.setenv("DATABASE_URL", "postgresql://writer@127.0.0.1/x")
    monkeypatch.setenv(pages.READER, "postgresql://reader@127.0.0.1/x")
    monkeypatch.setattr(pages.psycopg, "connect", lambda url, **kw: seen.update(kw, url=url) or "conn")
    assert pages._connect() == "conn" and seen["url"] == "postgresql://reader@127.0.0.1/x"
    assert "default_transaction_read_only=on" in seen["options"] and "statement_timeout=5000" in seen["options"]
    # A connection that goes quiet fails within seconds instead of holding a page slot.
    assert seen["keepalives"] == 1 and seen["keepalives_idle"] <= 10
    assert seen["tcp_user_timeout"] == pages.STATEMENT_TIMEOUT_MS + 5_000


def test_the_keepalive_settings_are_ones_libpq_accepts():
    """A typo in a parameter name would only show up when the container first connected."""
    from psycopg.conninfo import conninfo_to_dict
    params = conninfo_to_dict(make_conninfo("postgresql://reader@127.0.0.1/x", **pages.KEEPALIVES))
    assert {k: int(v) for k, v in params.items() if k in pages.KEEPALIVES} == pages.KEEPALIVES


# --- The commands -------------------------------------------------------------------------------

def test_grant_lets_a_role_read_every_table_now_and_later(schema_url, conn, capsys):
    url, schema = schema_url
    # Grant at schema 018, then apply the actual first-fill migration as the same owner.
    conn.execute("drop table first_fill_plans")
    conn.execute("delete from schema_migrations where version = '020_first_fill_plans.sql'")
    with psycopg.connect(url, autocommit=True, connect_timeout=CONNECT_TIMEOUT) as admin:
        name = _role(admin)
        try:
            # Without usage on the schema the tables are invisible, so the schema itself is reported.
            with _as(url, schema, name) as looking:
                assert pages.unreadable(looking) == [f"schema {schema} (no usage)"]
            admin.execute(sql.SQL("grant usage on schema {} to {}").format(sql.Identifier(schema), sql.Identifier(name)))
            with _as(url, schema, name) as looking:
                assert f"{schema}.config" in pages.unreadable(looking)
            admin.execute(sql.SQL("revoke usage on schema {} from {}").format(sql.Identifier(schema), sql.Identifier(name)))
            done = pages.grant(conn, name, "marshall")
            assert name in done and "marshall" in done and f"usage on schema {schema}" in done
            assert conn.execute("select tool, outcome, detail from action_log order by id desc limit 1").fetchone() == (
                "pages.grant", "done", done)
            # A table a later migration adds is readable too, and still not writable.
            assert db.migrate(conn) == ["020_first_fill_plans.sql"]
            with _as(url, schema, name) as looking:
                assert pages.unreadable(looking) == [] and pages.writable(looking) == []
                assert looking.execute("select count(*) from first_fill_plans").fetchone() == (0,)
        finally:
            _drop(admin, name)
    with pytest.raises(LookupError, match="no role"):
        pages.grant(conn, "nobody_by_this_name", "marshall")
    me = conn.execute("select current_user").fetchone()[0]
    with pytest.raises(ValueError, match="owns the tables"):
        pages.grant(conn, me, "marshall")


def test_check_says_whether_the_login_is_fit_for_a_public_page(schema_url, reader, monkeypatch, capsys):
    url, schema = schema_url
    assert pages.main(["check"]) == 1 and "not set" in capsys.readouterr().out
    monkeypatch.setenv(pages.READER, "set")
    monkeypatch.setattr(pages, "connect", lambda: _as(url, schema, reader))
    assert pages.main(["check"]) == 0
    out = capsys.readouterr().out
    assert f"login {reader}" in out and "read-only" in out and "can read every table" in out
    # The owner's login fails the check, and the reason names what it can change.
    monkeypatch.setattr(pages, "connect", lambda: open_in(url, schema))
    assert pages.main(["check"]) == 1
    assert "NOT read-only" in capsys.readouterr().out


# --- Selections not assigned to us ------------------------------------------------------------------

NOT_OURS = "NOT-OUR-SNACK"


def _stored(conn) -> tuple[str, ...]:
    """Our selections as the fixture stored them (`slot_assignment`)."""
    return tuple(conn.execute("select slots from slot_assignment where id = 1").fetchone()[0])


def _not_ours(conn, ours: str = "", theirs: str = ""):
    """A machine where one selection not assigned to us is counted and polled, one more machine's
    poll names our selection, and ours is counted and polled too. Returns (ours, not ours)."""
    from tests.conftest import store_poll
    from vending import slot_counts
    stored = _stored(conn)
    ours = ours or stored[0]
    theirs = theirs or next(f"{r}{c}" for r in range(1, 6) for c in range(1, 9) if f"{r}{c}" not in stored)
    assert ours in stored and theirs not in stored
    store_poll(conn, "ours", "2026-11-10T15:00:00+00:00", {ours: (150, 31), theirs: (1234, 987654)})
    store_poll(conn, "elsewhere", "2026-11-10T15:00:00+00:00", {ours: (999, 424242)}, device_id="vmc2")
    slot_counts.record(conn, slot_counts.parse({
        "count_id": "p1", "counted_at": "2026-11-09T11:30:00-05:00", "source": "photo", "device_id": "vmc1",
        "selections": [{"selection": ours, "product": "Our Chips", "units": 4, "price_on_tag": "1.50"},
                       {"selection": theirs, "product": NOT_OURS, "units": 9, "price_on_tag": "7.77"}]}), "kevin")
    return ours, theirs


def test_the_public_json_has_only_our_selections_in_its_counts(site, schema_url, conn):
    ours, theirs = _not_ours(conn)
    status, _, body = _get(site, "/dashboard/state.json")
    counts = json.loads(body)["counts"]["data"]
    assert status == 200 and list(counts) == [ours] and counts[ours]["product"] == "Our Chips"
    assert NOT_OURS not in body and '"7.77"' not in body
    sales, stock = json.loads(body)["sales"]["data"]["slots"], json.loads(body)["stock"]["data"]["slots"]
    assert theirs not in sales and theirs not in stock and set(sales) | set(stock) <= set(_stored(conn))
    # The standalone snapshot applies the same assignment filter.
    with open_in(*schema_url) as local:
        assert sorted(dashboard.snapshot(local)["counts"]["data"]) == [ours]


def test_the_public_explorer_shows_only_our_rows_of_the_selection_tables(site, schema_url, conn):
    _, schema = schema_url
    _not_ours(conn)
    counts = _get(site, f"/db?schema={schema}&table=slot_counts")[2]
    assert "Our Chips" in counts and NOT_OURS not in counts and "7.77" not in counts
    polls = _get(site, f"/db?schema={schema}&table=poll_selections")[2]
    # Our selection on our machine, with its price and sales; not the other one, not another machine.
    assert "31" in polls and "987654" not in polls and "1234" not in polls and "424242" not in polls
    # The page says how many rows it holds: only the ones it may show.
    with open_in(*schema_url) as local:
        for name, shown in (("slot_counts", 1), ("poll_selections", 1)):
            table = db_explorer.Table(schema, name, "table")
            assert db_explorer.rows(local, table, only=pages.only(_stored(local))[name]).total == shown
            assert db_explorer.rows(local, table).total == 2 + (name == "poll_selections")
    # `render` without rules shows everything; both explorers pass them (`pages.explorer_rules`).
    with open_in(*schema_url) as local:
        assert NOT_OURS in db_explorer.render(local, db_explorer.Request(schema, "slot_counts", 0))[1]


EVERY_OTHER = tuple(f"{r}{c}" for r in range(1, 6) for c in (2, 4, 6, 8))


def test_the_public_pages_follow_a_changed_stored_assignment(site, schema_url, conn):
    """Our slots on every other column: the pages draw and show those, from the database."""
    _, schema = schema_url
    conn.execute("update slot_assignment set slots = %s", (list(EVERY_OTHER),))
    ours, theirs = _not_ours(conn, ours="12", theirs="15")    # 15 is in the test assignment, but not ours here
    page = _get(site, "/dashboard")[2]
    assert page.count('<article class="ma-lane" id="lane-') + page.count('<article class="ma-lane ma-changed"') == 20
    assert page.count('class="ma-lane ma-notours"') == 20 and 'id="lane-12" role="group"' in page
    assert '<article class="ma-lane ma-notours" id="lane-15" aria-label="Selection 15: not ours">' in page
    assert NOT_OURS not in page and "7.77" not in page
    body = _get(site, "/dashboard/state.json")[2]
    assert list(json.loads(body)["counts"]["data"]) == ["12"] and json.loads(body)["ours"]["data"] == list(EVERY_OTHER)
    counts = _get(site, f"/db?schema={schema}&table=slot_counts")[2]
    assert "Our Chips" in counts and NOT_OURS not in counts and "7.77" not in counts
    polls = _get(site, f"/db?schema={schema}&table=poll_selections")[2]
    assert "31" in polls and "987654" not in polls and "1234" not in polls and "424242" not in polls


def test_the_public_pages_degrade_when_no_assignment_is_stored(site, schema_url, conn):
    """Nothing stored: no selection is ours, so the pages light nothing, show no selection rows,
    and say the assignment is unavailable."""
    _, schema = schema_url
    _not_ours(conn)
    conn.execute("delete from slot_assignment")
    status, _, page = _get(site, "/dashboard")
    assert status == 200 and '<article class="ma-lane' not in page
    assert f"Not available: {dashboard.PUBLIC_PANEL_ERROR}" in page.replace("&#x27;", "'")
    assert "Which selections are ours isn" in page and "Our Chips" not in page and NOT_OURS not in page
    assert "our slots aren't recorded" in page and "run the history sync" not in page   # not "no polls"
    status, _, body = _get(site, "/dashboard/state.json")
    snap = json.loads(body)
    assert status == 200 and snap["ours"]["ok"] is False and snap["counts"]["data"] == {}
    assert snap["ours"]["why"] == dashboard.PUBLIC_PANEL_ERROR
    assert "Our Chips" not in body and NOT_OURS not in body
    for table in ("slot_counts", "poll_selections"):
        status, _, page = _get(site, f"/db?schema={schema}&table={table}")
        assert status == 200 and "Our Chips" not in page and NOT_OURS not in page
        assert "987654" not in page and "424242" not in page and ">31<" not in page


OLD_SLOT = "OLD-SLOT-SODA"


def _stocked_then_reassigned(conn):
    """We stocked 15 (in the fixture's assignment), the server then moved us to every other column,
    and a poll after that shows 15 selling again. Also two live plans that name 15 and 12."""
    from datetime import datetime, timedelta, timezone

    from tests.conftest import store_poll
    from tests.test_dashboard import fulfilled, plan
    at = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)
    assert "15" in _stored(conn)
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=at)
    fulfilled(conn, "pr-old", "15", OLD_SLOT, units_loaded=2, paid="2.40", at=at)
    store_poll(conn, "before", (at - timedelta(hours=1)).isoformat(), {"15": (250, 100), "12": (200, 7)})
    conn.execute("update slot_assignment set slots = %s", (list(EVERY_OTHER),))
    # More sales than we loaded, so the receipts replay warns about slot 15.
    store_poll(conn, "after", (at + timedelta(days=1)).isoformat(), {"15": (250, 105), "12": (200, 9)})
    first = agent_runs.start(conn, mode="live")
    agent_runs.finish(conn, first, "submitted", plan=plan({"slot": "15", "fill_to": 6, "sale_price": "2.50"},
                                                          {"slot": "12", "fill_to": 4}))
    second = agent_runs.start(conn, mode="live")
    agent_runs.finish(conn, second, "submitted", plan=plan({"slot": "15", "fill_to": 8, "sale_price": "9.99"},
                                                           {"slot": "12", "fill_to": 5}))
    return second


def test_after_a_reassignment_the_public_json_drops_the_old_slots_stock(site, schema_url, conn):
    _stocked_then_reassigned(conn)
    body = _get(site, "/dashboard/state.json")[2]
    snap = json.loads(body)
    assert set(snap["stock"]["data"]["slots"]) <= set(EVERY_OTHER) and "15" not in snap["stock"]["data"]["slots"]
    assert not any("slot 15" in w for w in snap["stock"]["data"]["warnings"])
    assert OLD_SLOT not in body
    page = _get(site, "/dashboard")[2]
    assert OLD_SLOT not in page and "slot 15" not in page
    # The standalone snapshot uses the same assignment.
    with open_in(*schema_url) as local:
        stock = dashboard.snapshot(local)["stock"]["data"]
        assert "15" not in stock["slots"]
        assert not any("slot 15" in w for w in stock["warnings"])


@pytest.fixture
def redraw_with_receipts(conn, monkeypatch):
    now = conn.execute("select now()").fetchone()[0]
    before, redraw = now - timedelta(minutes=20), now - timedelta(minutes=5)
    retired, owned, new = "15", "16", unassigned_slot()
    monkeypatch.setenv("FALL_CUTOFF", (before - timedelta(days=1)).isoformat())
    monkeypatch.setattr(dashboard, "lock_at", lambda: now + timedelta(days=1))
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=before)
    fulfilled(conn, "retired-receipt", retired, "RETIRED-RECEIPT-SKU", units_loaded=4, paid="4.00", at=before)
    fulfilled(conn, "owned-receipt", owned, "OWNED-RECEIPT-SKU", units_loaded=4, paid="4.00", at=before)
    store_poll(conn, "before-redraw", before.isoformat(), {retired: (9173, 852713), owned: (256, 4)})
    conn.execute("update slot_assignment set slots = array_remove(slots, %s) || array[%s],"
                 " first_read_at = %s, last_checked_at = %s", (retired, new, redraw, redraw))
    return before, retired, owned, new


def _dashboard_bodies(site, served):
    yield tuple(get(served + path)[2] for path in ("/", "/state.json"))
    yield tuple(_get(site, path)[2] for path in ("/dashboard", "/dashboard/state.json"))


def _stale_wildcard_refusal(conn, retired, owned):
    detail = f"The wildcard is slot {retired}. Refused the change to slot {owned}."
    log(conn, "write", "set_wildcard_slot", {"slot": owned}, "refused", detail)
    return detail


def _stale_preview_reply(conn, retired, owned):
    run = agent_runs.start(conn, mode="live")
    warning = f"Counted stock on slot {retired} needs a check."
    control = f"Counted stock on slot {owned} is current."
    content = json.dumps({
        "slots": [{"slot": owned, "fill_to": 4}],
        "message": f"The earlier proposal used selection {retired}.",
        "notes": [warning, control],
        "counted": {"warnings": [warning, control]},
    })
    agent_runs.step(conn, run, 1, "tool_result", content, tool="preview_desired_state")
    return run, content, control


def _stale_purchase_decision(conn, retired):
    note = f"The worker declined the old selection {retired}."
    conn.execute("insert into purchase_requests (request_id, submitted_at, off_cycle, request_body, status, decision_note)"
                 " values ('new-rejection', now(), false, '{}', 'rejected', %s)", (note,))
    return note


def _stale_slot_config(conn, retired, owned):
    for key in ("slot", "selection", "into_slot", "wildcard_slot"):
        log(conn, "write", "config.set", {"key": key, "value": int(retired)}, "done", "Configuration changed.")
    control = {"key": "wildcard_slot", "value": owned}
    log(conn, "write", "config.set", control, "done", f"Configured owned slot {owned}.")
    return control


def test_current_wildcard_refusal_redacts_retired_slot_text(site, served, conn, redraw_with_receipts):
    _, retired, owned, _ = redraw_with_receipts
    detail = _stale_wildcard_refusal(conn, retired, owned)
    for page, body in _dashboard_bodies(site, served):
        audit = next(r for r in json.loads(body)["activity"]["data"] if r["tool"] == "set_wildcard_slot")
        assert audit["detail"] == dashboard.OUTSIDE_ASSIGNMENT
        assert audit["outcome"] == "refused" and audit["arguments"] == {"slot": owned}
        assert detail not in page and dashboard.OUTSIDE_ASSIGNMENT in page
    assert conn.execute("select detail from action_log where tool = 'set_wildcard_slot'").fetchone() == (detail,)


def test_current_preview_redacts_text_and_keeps_owned_notes(site, served, conn, redraw_with_receipts):
    _, retired, owned, _ = redraw_with_receipts
    run, content, control = _stale_preview_reply(conn, retired, owned)
    for page, body in _dashboard_bodies(site, served):
        reply = json.loads(json.loads(body)["runs"]["data"][0]["steps"][0]["content"])
        assert reply["message"] == dashboard.OUTSIDE_ASSIGNMENT
        assert reply["notes"] == reply["counted"]["warnings"] == [control]
        assert reply["slots"] == [{"slot": owned, "fill_to": 4}]
        assert control in page and f"Counted stock on slot {retired}" not in page
    assert conn.execute("select content from agent_steps where run_id = %s", (run,)).fetchone() == (content,)


def test_current_decision_note_is_scoped_before_attention(site, served, conn, redraw_with_receipts):
    _, retired, _, _ = redraw_with_receipts
    note = _stale_purchase_decision(conn, retired)
    for page, body in _dashboard_bodies(site, served):
        snap = json.loads(body)
        request = next(r for r in snap["purchase_requests"]["data"] if r["request_id"] == "new-rejection")
        assert request["decision_note"] == dashboard.OUTSIDE_ASSIGNMENT
        warn = next(a for a in snap["attention"] if "Purchase request new-rejection was rejected" in a["text"])
        assert warn["level"] == "warn" and dashboard.OUTSIDE_ASSIGNMENT in warn["text"]
        assert note not in page and note not in body
    assert conn.execute("select decision_note from purchase_requests where request_id = 'new-rejection'").fetchone() == (note,)


def test_current_audit_slot_key_value_pairs_drop_retired_slots(site, served, conn, redraw_with_receipts):
    _, retired, owned, _ = redraw_with_receipts
    control = _stale_slot_config(conn, retired, owned)
    for page, body in _dashboard_bodies(site, served):
        args = [r["arguments"] for r in json.loads(body)["activity"]["data"] if r["tool"] == "config.set"]
        assert len(args) == 5 and args.count({}) == 4 and control in args
        assert "Configuration changed." not in page and f"Configured owned slot {owned}." in page


def test_redraw_sweep_scopes_recorded_text_on_both_routes(site, served, conn, redraw_with_receipts, monkeypatch):
    _, retired, owned, _ = redraw_with_receipts
    _stale_wildcard_refusal(conn, retired, owned)
    run, _, control = _stale_preview_reply(conn, retired, owned)
    _stale_purchase_decision(conn, retired)
    _stale_slot_config(conn, retired, owned)
    monkeypatch.setattr(dashboard, "lock_at", lambda: TEST_NOW - timedelta(days=30))
    agent_runs.finish(conn, run, "skipped", plan={"slots": [{
        "slot": owned, "fill_to": 5, "sale_price": "2.75",
        "reason": f"Carry over the price of selection {retired}.",
    }]})
    for page, body in _dashboard_bodies(site, served):
        # A grey cabinet position only labels the machine geometry. Check every recorded mention,
        # plus the dynamic title and aria text, independently of that static label.
        geometry = f"Selection {retired}: not ours"
        attrs = re.findall(r'(?:aria-label|title)="([^"]*)"', page)
        texts = (page.replace(geometry, ""), body, *(text for text in attrs if text != geometry))
        for text in texts:
            decoded = unescape(text).lower()
            assert not shows(decoded, f"slot {retired}") and not shows(decoded, f"selection {retired}")
        for row in json.loads(body)["activity"]["data"]:
            if row["tool"] == "config.set":
                assert not shows(json.dumps(row["arguments"]), retired)
        assert control in page and control in body
        assert any(shows(unescape(text).lower(), f"selection {owned}") for text in attrs)
        assert f'class="pr-reason" title="{dashboard.OUTSIDE_ASSIGNMENT}"' in page


def test_fill_check_scopes_the_check_and_plan_reason_before_display(site, served, conn, redraw_with_receipts, monkeypatch):
    _, retired, owned, _ = redraw_with_receipts
    check = f"Fills 4 of 8. See slot {retired}."
    reason = f"Based on a count for selection {retired}."
    monkeypatch.setattr(dashboard, "lock_at", lambda: TEST_NOW - timedelta(days=30))
    monkeypatch.setattr(dashboard.capacity, "fill_check", lambda fill, limit: check)
    run = agent_runs.start(conn, mode="live")
    agent_runs.finish(conn, run, "skipped", plan={"slots": [{"slot": owned, "fill_to": 4, "reason": reason}]})
    for page, body in _dashboard_bodies(site, served):
        snap = json.loads(body)
        row = next(row for row in snap["plan"]["data"]["slots"] if row["slot"] == owned)
        assert row["fill_check"] == row["now"]["reason"] == dashboard.OUTSIDE_ASSIGNMENT
        assert any(item["level"] == "warn" and item["text"].startswith(f"Slot {owned} {dashboard.OUTSIDE_ASSIGNMENT}")
                   for item in snap["attention"])
        assert all(text not in unescape(page) and text not in body for text in (check, reason))


def test_redraw_hides_retired_receipt_stock_in_both_dashboards(site, served, redraw_with_receipts):
    _, retired, owned, _ = redraw_with_receipts
    for page, body in _dashboard_bodies(site, served):
        assert "RETIRED-RECEIPT-SKU" not in page and "RETIRED-RECEIPT-SKU" not in body
        assert "OWNED-RECEIPT-SKU" in page and "OWNED-RECEIPT-SKU" in body
        stock = json.loads(body)["stock"]["data"]["slots"]
        assert retired not in stock and stock[owned]["on_hand"] == 4


@pytest.mark.parametrize("reply_time", ["before_redraw", "after_redraw"])
def test_redraw_scopes_saved_poll_replies_in_both_dashboards(site, served, conn, redraw_with_receipts, reply_time):
    before, retired, owned, _ = redraw_with_receipts
    rows = [{"slot": retired, "price": "91.73", "units_sold_in_window": 852713},
            {"slot": owned, "price": "2.57", "units_sold_in_window": 1, "product": "OWNED-REPLY"}]
    reply_text = f"Selection {retired} had price 91.73 and 852713 sales"
    detail = f"Saved audit summary for selection {retired}: 92.74 and 853714 sales"
    saved = json.dumps({"data": rows, "content": [{"type": "text", "text": reply_text}]})
    run = agent_runs.start(conn, mode="live")
    agent_runs.step(conn, run, 1, "tool_result", saved, tool="get_machine_state")
    log(conn, "read", "get_machine_state", {}, "done", detail)
    conn.execute("update action_log set result = %s where tool = 'get_machine_state'", (Jsonb({"data": rows}),))
    if reply_time == "before_redraw":
        conn.execute("update agent_runs set started_at = %s where id = %s", (before, run))
        conn.execute("update action_log set at = %s where tool = 'get_machine_state'", (before,))

    for page, body in _dashboard_bodies(site, served):
        for marker in ("91.73", "852713", "92.74", "853714", "RETIRED-RECEIPT-SKU"):
            assert marker not in page and marker not in body
        assert "OWNED-RECEIPT-SKU" in page and "OWNED-RECEIPT-SKU" in body
        if reply_time == "after_redraw":
            assert "OWNED-REPLY" in page and "OWNED-REPLY" in body
    assert conn.execute("select content from agent_steps where run_id = %s", (run,)).fetchone() == (saved,)
    assert conn.execute("select detail, result from action_log where tool = 'get_machine_state'").fetchone() == (
        detail, {"data": rows},
    )


def test_prelock_scopes_weekly_plan_and_earlier_first_fill_reasons(site, served, conn, tmp_path, redraw_with_receipts):
    before, retired, owned, new = redraw_with_receipts
    run = agent_runs.start(conn, mode="live")
    agent_runs.finish(conn, run, "skipped", plan={"reason": "RETIRED-WEEKLY-SUMMARY", "slots": [
        {"slot": retired, "product": "RETIRED-WEEKLY-ENTRY"},
        {"slot": owned, "product": "OWNED-WEEKLY-ENTRY", "reason": "RETIRED-WEEKLY-REASON"},
    ]})
    conn.execute("update agent_runs set started_at = %s where id = %s", (before, run))
    # The checked slot set still matches. The copy in the store predates this assignment's check.
    rows = day_one(**{retired: {"slot": new}, owned: {"product": "OWNED-FIRST-FILL", "why": "RETIRED-FILL-REASON"}})
    write(tmp_path, "round3", "3", {"day_one": rows})
    saved = plan_store.load(conn, tmp_path, "3", "Test Loader")
    conn.execute("update first_fill_plans set loaded_at = %s where id = %s", (before, saved))

    for page, body in _dashboard_bodies(site, served):
        assert "OWNED-FIRST-FILL" in page
        snap = json.loads(body)
        assert snap["plan"]["data"]["id"] == saved
        assert snap["weekly_plan"]["data"]["run"]["id"] == run and "OWNED-WEEKLY-ENTRY" in body
        for plan in (snap["plan"], snap["weekly_plan"]):
            assert retired not in {entry["slot"] for entry in plan["data"]["slots"]}
        for marker in ("RETIRED-FILL-REASON", "RETIRED-WEEKLY-SUMMARY",
                       "RETIRED-WEEKLY-ENTRY", "RETIRED-WEEKLY-REASON"):
            assert marker not in page and marker not in body
        assert "from before the current slot assignment" in page


def test_after_a_reassignment_plans_list_only_our_slots(site, schema_url, conn, monkeypatch):
    monkeypatch.setattr(dashboard, "lock_at", lambda: TEST_NOW - timedelta(days=30))
    newest = _stocked_then_reassigned(conn)
    for snap in (json.loads(_get(site, "/dashboard/state.json")[2]),):
        rows = snap["plan"]["data"]["slots"]
        assert [r["slot"] for r in rows] == list(EVERY_OTHER)
        assert next(r for r in rows if r["slot"] == "12")["changed"] is True
        runs = {r["id"]: r for r in snap["runs"]["data"]}
        assert [c["slot"] for c in runs[newest]["changes"]] == ["12"]
    with open_in(*schema_url) as local:
        snap = dashboard.snapshot(local)
        assert [r["slot"] for r in snap["plan"]["data"]["slots"]] == list(EVERY_OTHER)
        page = dashboard.render(snap)
    assert "9.99" not in page and "15 $2.50" not in page and "1 of 20 slots changed" in page


def test_with_no_assignment_the_public_json_has_no_slot_stock_or_plan_rows(site, conn, monkeypatch):
    monkeypatch.setattr(dashboard, "lock_at", lambda: TEST_NOW - timedelta(days=30))
    _stocked_then_reassigned(conn)
    conn.execute("delete from slot_assignment")
    body = _get(site, "/dashboard/state.json")[2]
    snap = json.loads(body)
    assert snap["ours"]["ok"] is False and snap["stock"]["data"]["slots"] == {}
    assert not any("slot " in w for w in snap["stock"]["data"]["warnings"])
    assert snap["plan"]["data"]["slots"] == [] and all(r["changes"] == [] for r in snap["runs"]["data"])
    assert OLD_SLOT not in body and "9.99" not in body


def test_bad_slot_lock_setting_leaves_the_weekly_dashboard_available(site, conn, monkeypatch, capsys):
    run = agent_runs.start(conn, mode="live")
    agent_runs.finish(conn, run, "skipped", plan={"slots": []}, update="Weekly dashboard test update.")
    monkeypatch.setenv("SLOTS_LOCK_AT", "2026-12-03")

    status, _, html = _get(site, "/dashboard")
    assert status == 200
    assert "<h2>The plan " in html and dashboard.OLD_ASSIGNMENT in html
    assert "Weekly dashboard test update." not in html
    assert dashboard.PUBLIC_PANEL_ERROR in html.replace("&#x27;", "'")

    status, _, body = _get(site, "/dashboard/state.json")
    assert status == 200
    snap = json.loads(body)
    assert snap["plan"]["data"]["run"]["id"] == run
    assert snap["plan"]["data"]["slots"] == []
    assert snap["ours"] == {"ok": False, "why": dashboard.PUBLIC_PANEL_ERROR}
    assert "SLOTS_LOCK_AT" in capsys.readouterr().err


def test_first_fill_html_and_json_agree_on_the_read_only_page(site, schema_url, conn, tmp_path, monkeypatch, capsys):
    path = r"C:\Users\Made Up\private\first-fill.json"
    rows = day_one(**{"15": {"product": "Sample Cherry Soda", "why": "Made-up fixture reason."},
                      "16": {"why": f"See {path}"}, "17": {"price_cents": 25}})
    source = {"day_one": rows, "note": path}
    write(tmp_path, "round3", "3", source, projection())
    plan_id = plan_store.load(conn, tmp_path, "3", "Test Loader")
    monkeypatch.setattr(dashboard, "lock_at", lambda: dashboard.datetime.now(dashboard.timezone.utc)
                        + timedelta(days=1))
    status, _, html = _get(site, "/dashboard")
    assert status == 200
    plan_html = html.split('<section id="plan">')[1].split("</section>")[0]
    status, _, body = _get(site, "/dashboard/state.json")
    assert status == 200
    plan = json.loads(body)["plan"]["data"]
    assert plan["id"] == plan_id and plan["picked"] is False
    slot15 = plan["slots"][0]
    assert (slot15["slot"], slot15["product"], slot15["price_cents"], slot15["units"], slot15["reason"]) == (
        "15", "Sample Cherry Soda", 350, 11, "Made-up fixture reason.")
    assert 'id="slot-15"' in plan_html
    assert "Sample Cherry Soda &middot; $3.50 &middot; 11 units" in plan_html
    assert '<div class="why">Made-up fixture reason.</div>' in plan_html
    assert slot15["checks"]["legal_price"] is True
    assert plan["slots"][2]["checks"]["legal_price"] is False and "price refused" in plan_html
    assert plan["fill_cap_cents"] == 36600 and "against the proposed $366.00 cap" in plan_html
    assert "Code checks" in plan_html and "Floor $1.85" in plan_html and "Fill cost share" in plan_html
    assert "private" not in plan_html and "first-fill.json" not in body
    assert "Interloper" not in plan_html and "projection" not in plan
    _, schema = schema_url
    status, _, explorer = _get(site, f"/db?schema={schema}&table=first_fill_plans")
    assert status == 200 and "first-fill.json" not in explorer and "Sample Cherry Soda" not in explorer

    # Bypass the loader and try to publish a row for an unassigned selection.
    rows[0].update(slot=unassigned_slot(), product="Outside slot test marker")
    conn.execute("update first_fill_plans set plan = %s where id = %s", (Jsonb(source), plan_id))
    for url in ("/dashboard", "/dashboard/state.json"):
        status, _, body = _get(site, url)
        assert status == 200 and "Outside slot test marker" not in body
    assert json.loads(body)["plan"]["ok"] is False

    conn.execute("drop table first_fill_plans")
    status, _, html = _get(site, "/dashboard")
    assert status == 200 and "Plan for the first fill" in html
    assert dashboard.PUBLIC_PANEL_ERROR in html.replace("&#x27;", "'")
    assert "first_fill_plans" not in html and 'relation "first_fill_plans" does not exist' in capsys.readouterr().err


def test_a_table_without_the_columns_a_row_filter_reads_shows_no_rows(schema_url, conn):
    _, schema = schema_url
    _not_ours(conn)
    conn.execute("create table slot_counts_copy as select count_id, selection, product from slot_counts")
    table = db_explorer.Table(schema, "slot_counts_copy", "table")
    only = pages.only(_stored(conn))["slot_counts"]   # reads device_id too, which the copy lacks
    with open_in(*schema_url) as local:
        assert db_explorer.rows(local, table).total == 2
        shown = db_explorer.rows(local, table, only=only)
    assert shown.total == 0 and shown.rows == ()


def test_the_public_explorer_hides_the_hostnames_of_the_dev_machines(site, schema_url, conn):
    _, schema = schema_url
    conn.execute("insert into model_calls (purpose, run_id, model, key_env, max_tokens, worst_case_usd, cost_usd,"
                 " recorded_by, host) values ('test', 'r1', 'm', 'K', 1, 0, 0, 'kevin', 'KEVINS-LAPTOP')")
    page = _get(site, f"/db?schema={schema}&table=model_calls")[2]
    assert "KEVINS-LAPTOP" not in page and "(hidden)" in page and "kevin" in page
    with open_in(*schema_url) as local:
        assert "KEVINS-LAPTOP" in db_explorer.render(local, db_explorer.Request(schema, "model_calls", 0))[1]


def _leaves(value, path=()):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield from _leaves(inner, (*path, key))
    elif isinstance(value, list):
        for inner in value:
            yield from _leaves(inner, (*path, "[]"))
    else:
        yield path, value


def test_the_public_json_money_panel_carries_only_counts_and_dollar_strings(schema_url, reader, conn, monkeypatch):
    """`/dashboard/state.json` through `pages.respond`: the money panel's figures, never a request body."""
    from datetime import datetime, timedelta, timezone

    from psycopg.types.json import Jsonb

    from tests.test_dashboard import fulfilled
    from vending import ledger

    url, schema = schema_url
    monkeypatch.setenv(pages.READER, "set")
    monkeypatch.setattr(pages, "connect", lambda: _as(url, schema, reader))
    opened = datetime.now(timezone.utc) - timedelta(days=5)
    fulfilled(conn, "pr-test", "15", "RB", units_loaded=0, to_back_stock=4, paid="9.99", at=opened - timedelta(days=3))
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=opened)
    fulfilled(conn, "pr-1", "15", "RB", units_loaded=11, paid="34.99", at=opened + timedelta(days=1))
    secret = {"off_cycle": False, "lines": [{"slot": "15", "source": "purchase", "primary": {
        "sku": "ZZ-SECRET-SKU", "store": "SecretMart", "packs": 2, "units_per_pack": 24,
        "max_price_per_pack": "40.50", "sale_price": "3.50", "change_product": True}}]}
    conn.execute("insert into purchase_requests (request_id, submitted_at, off_cycle, request_body)"
                 " values ('pr-2', now(), false, %s)", (Jsonb(secret),))

    reply = pages.respond(pages.STATE)
    assert reply.status == 200 and reply.content_type == pages.JSON
    text = reply.body if isinstance(reply.body, str) else reply.body.decode()
    money = json.loads(text)["money"]
    assert money["ok"] and money["data"]["opened"] is True and money["data"]["balance"] == "565.01"
    assert money["data"]["committed"] == {"requests": 1, "unsettled": 0, "max": "81.00", "unpriced_lines": 0}
    blob = json.dumps(money)
    assert "ZZ-SECRET-SKU" not in blob and "SecretMart" not in blob and "request_body" not in blob
    _only_counts_dollars_and_times(money["data"])


def _only_counts_dollars_and_times(data):
    """Every leaf of the money panel's data is a count, a flag, a dollar string, a timestamp (the
    opening, or when a future-dated opening opens) or a ledger note."""
    import re
    from datetime import datetime

    dollars = re.compile(r"-?\d+\.\d{2}")
    for path, value in _leaves(data):
        if path in (("opening_at",), ("opens_at",)):
            datetime.fromisoformat(value)
        elif path[:1] == ("notes",):
            assert isinstance(value, str)       # the ledger's own sentences
        else:
            assert value is None or isinstance(value, (bool, int)) or dollars.fullmatch(value), (path, value)


def test_the_public_json_money_panel_before_a_future_dated_opening(schema_url, reader, conn, monkeypatch):
    """An opening recorded ahead: the public JSON says when it opens, plus counts, and nothing else."""
    from datetime import datetime, timedelta, timezone

    from psycopg.types.json import Jsonb

    from vending import ledger

    url, schema = schema_url
    monkeypatch.setenv(pages.READER, "set")
    monkeypatch.setattr(pages, "connect", lambda: _as(url, schema, reader))
    opens = datetime.now(timezone.utc) + timedelta(days=3)
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=opens)
    secret = {"off_cycle": False, "lines": [{"slot": "15", "source": "purchase", "primary": {
        "sku": "ZZ-SECRET-SKU", "store": "SecretMart", "packs": 1, "units_per_pack": 24,
        "max_price_per_pack": "40.50", "sale_price": "3.50", "change_product": True}}]}
    conn.execute("insert into purchase_requests (request_id, submitted_at, off_cycle, request_body)"
                 " values ('pr-ahead', now(), false, %s)", (Jsonb(secret),))

    reply = pages.respond(pages.STATE)
    assert reply.status == 200 and reply.content_type == pages.JSON
    text = reply.body if isinstance(reply.body, str) else reply.body.decode()
    money = json.loads(text)["money"]
    assert money["ok"] and money["data"]["opened"] is False
    assert datetime.fromisoformat(money["data"]["opens_at"]) == opens
    assert "ZZ-SECRET-SKU" not in text and "SecretMart" not in text
    _only_counts_dollars_and_times(money["data"])


# --- The chart pages: /dashboard/money, /dashboard/pnl, /dashboard/spend ------------------------------

CHART_PATHS = (pages.MONEY_CHART, pages.PNL, pages.SPEND)


def _inlined(page: str, const: str) -> dict:
    """The JSON a chart page carries in place of /*DATA*/null."""
    import re
    found = re.search(rf"const {const} = (\{{.*?\}});\n", page, re.S)
    assert found, f"no {const} data in the page"
    return json.loads(found.group(1))


def _scripts_allowed_by_hash(page: str, csp: str) -> None:
    """Every inline script runs by its hash, and nothing else may run or be fetched."""
    import base64
    import hashlib
    import re
    scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
    assert scripts and page.count("<script") == len(scripts)
    for code in scripts:
        assert f"'sha256-{base64.b64encode(hashlib.sha256(code.encode()).digest()).decode()}'" in csp
    assert "default-src 'none'" in csp and "'unsafe-inline'" not in csp.split("script-src", 1)[1].split(";")[0]
    assert "connect-src" not in csp and "frame-ancestors 'none'" in csp and "form-action 'none'" in csp


def _competition(conn):
    """An opening on 1 Nov, a test call before it ($0.80, development) and a live call after ($0.40),
    a delivery to slot 15 and five sold from it."""
    from datetime import datetime

    from tests.test_ledger import EASTERN, deliver, open_ledger, sell
    from vending import ledger
    ledger.record_tokens(conn, "0.80", "claude-opus-5-5", 10, 5, "test dry run",
                         at=datetime(2026, 10, 30, 12, tzinfo=EASTERN))
    open_ledger(conn)
    deliver(conn, "r1")
    sell(conn)
    ledger.record_tokens(conn, "0.40", "claude-opus-5-5", 10, 5, "live agent run",
                         at=datetime(2026, 11, 8, 12, tzinfo=EASTERN))


def _written(conn) -> tuple:
    """Row counts of every table a page could write to, to show none of them did."""
    return tuple(conn.execute(f"select count(*) from {table}").fetchone()[0]
                 for table in ("action_log", "ledger_entries", "page_visits", "config", "model_calls"))


def test_the_money_chart_is_served_from_the_ledger_never_mock(site, conn):
    _competition(conn)
    before = _written(conn)
    status, headers, page = _get(site, pages.MONEY_CHART)
    assert status == 200 and headers["Content-Type"] == pages.HTML and headers["Cache-Control"] == "no-store"
    _scripts_allowed_by_hash(page, headers["Content-Security-Policy"])
    # Real data only: the mock switch is off for good and the MOCK banner and tag start hidden.
    assert "const MOCK = false;" in page and '<div class="banner" id="banner" hidden>' in page
    assert '<span class="mock" id="mockTag" hidden>' in page and "<title>Money chart</title>" in page
    data = _inlined(page, "REAL")
    # The opening, 1 Nov 00:00 UTC, is the evening of 31 Oct in Eastern time.
    assert data["opening"]["day"] == "2026-10-31" and data["daily"][0]["date"] == "2026-10-31"
    # Development spend before the opening is in no figure; the live call after it is.
    assert sum(float(d["token_spend"]) for d in data["daily"]) == 0.40
    assert '<a href="/dashboard" style="color:inherit">&larr; Agent dashboard</a>' in page
    assert _get(site, pages.MONEY_CHART + "/")[0] == 200
    assert _written(conn) == before


def test_dev_spend_is_served_and_ends_at_the_opening(site, conn):
    _competition(conn)
    status, headers, page = _get(site, pages.SPEND)
    assert status == 200 and headers["Content-Type"] == pages.HTML
    _scripts_allowed_by_hash(page, headers["Content-Security-Policy"])
    data = _inlined(page, "DATA")
    weekly = data["weekly"]
    assert weekly["source"] == "model_calls and the ledger" and weekly["opening_at"].startswith("2026-10-31T20:00")
    # The test call before the opening is development spend; the live call after it is the competition's.
    assert weekly["totals"]["total_usd"] == "0.8" and [r["label"] for r in weekly["runs"]] == ["dry run"]
    assert "live agent run" not in page


def test_the_pnl_is_served_under_its_own_path_and_never_counts_before_the_opening(site, conn):
    import re

    from tests.conftest import store_poll
    # Five sold from slot 15 in October, before the opening: test sales, not the competition's.
    store_poll(conn, "pre-1", "2026-10-25T17:00:00+00:00", {"15": (250, 90)})
    store_poll(conn, "pre-2", "2026-10-28T17:00:00+00:00", {"15": (250, 95)})
    _competition(conn)
    before = _written(conn)

    def units(page):
        return re.search(r'Units sold</div><div class="tile-value">(\d+)<', page).group(1)
    status, headers, page = _get(site, pages.PNL)
    assert status == 200 and headers["Content-Security-Policy"] == pages.CSP_PNL and "<script" not in page
    assert "Weekly P&amp;L" in page and "$0.40" in page and "$0.80" not in page
    # Its form and links stay under /dashboard/pnl.
    assert 'action="/dashboard/pnl"' in page and 'href="/dashboard/pnl?start=' in page and 'href="/?' not in page
    assert 'href="/dashboard/pnl?view=detailed"' in page
    # A start before the opening counts from the opening: October's sales and the development call stay out.
    status, _, early = _get(site, f"{pages.PNL}?start=2026-10-01&end=2026-11-30&view=detailed")
    assert status == 200 and "$0.40" in early and "$0.80" not in early and "$1.20" not in early
    assert units(early) == units(page) and int(units(page)) > 0
    status, _, bad = _get(site, f"{pages.PNL}?view=%3Cscript%3E")
    assert status == 400 and "&lt;script&gt;" in bad and "<script>" not in bad
    assert _written(conn) == before


def test_before_the_opening_money_and_pnl_say_there_is_no_competition_data(site, conn):
    from datetime import datetime, timedelta, timezone

    from tests.test_ledger import EASTERN
    from vending import ledger
    ledger.record_tokens(conn, "0.80", "claude-opus-5-5", 10, 5, "test dry run",
                         at=datetime(2026, 10, 30, 12, tzinfo=EASTERN))
    for path in (pages.MONEY_CHART, pages.PNL, f"{pages.PNL}?start=2026-10-01&end=2026-12-31"):
        status, headers, page = _get(site, path)
        text = page.replace("&#x27;", "'")
        said = ("No competition money yet: the chart starts at the ledger opening (about 30 Nov)."
                if path == pages.MONEY_CHART else "There's no competition data until the ledger opens.")
        assert status == 200 and said in text, path
        assert "python -m vending.ledger open" in text and "$" not in text and "<script" not in page
        # No mock values: no chart data and none of the page's seeded MOCK series. The money chart
        # links its demo instead, the one page that may draw them.
        assert "const REAL" not in page and "MOCK" not in page and "mockDays" not in page and "0.80" not in page
        assert ('<a href="/dashboard/demo/money">See the demo with made-up data</a>' in page) == (path == pages.MONEY_CHART)
    # Dev spend has real data before the opening: every call so far is development spend.
    status, _, page = _get(site, pages.SPEND)
    assert status == 200 and _inlined(page, "DATA")["weekly"]["totals"]["total_usd"] == "0.8"

    opens = datetime.now(timezone.utc) + timedelta(days=3)
    ledger.record(conn, "opening", 60000, "opening capital", "test", at=opens)
    pages._cache.clear()        # each page above is kept for a minute
    for path in (pages.MONEY_CHART, pages.PNL):
        status, _, page = _get(site, path)
        when = f"{opens.astimezone(EASTERN):%a %d %b %Y %H:%M} Eastern"
        assert status == 200 and when in page and "recorded ahead of time" in page and "const REAL" not in page


def test_the_money_chart_names_no_selection_that_isnt_ours_now(site, conn):
    """A receipt for a slot reassigned away from us: its money stays in the totals, its slot and product don't show."""
    from datetime import datetime, timezone

    from tests.test_dashboard import fulfilled
    from tests.test_ledger import open_ledger
    open_ledger(conn)
    assert "15" in _stored(conn)
    fulfilled(conn, "pr-old", "15", OLD_SLOT, units_loaded=2, paid="2.40",
              at=datetime(2026, 11, 9, 15, tzinfo=timezone.utc))
    fulfilled(conn, "pr-ours", "12", "OUR-SODA", units_loaded=2, paid="1.10",
              at=datetime(2026, 11, 9, 16, tzinfo=timezone.utc))
    conn.execute("update slot_assignment set slots = %s", (list(EVERY_OTHER),))
    status, _, page = _get(site, pages.MONEY_CHART)
    assert status == 200 and OLD_SLOT not in page and "slot 15" not in page
    day = next(d for d in _inlined(page, "REAL")["daily"] if d["date"] == "2026-11-09")
    assert day["purchases"] == "3.50"
    assert sorted(e["detail"] for e in day["events"] if e["kind"] == "receipt") == [
        "a line for a selection that isn't ours now", "slot 12: OUR-SODA"]


def test_the_dashboard_links_to_its_charts_and_its_json_is_unchanged(site, conn):
    status, _, page = _get(site, "/dashboard")
    assert status == 200
    # In the header: the money chart as a button beside the verdict, the others beside Home and Database.
    assert '<a class="chartlink" href="/dashboard/money">Money chart</a>' in page
    for href, label in ((pages.PNL, "Weekly P&amp;L"), (pages.SPEND, "Dev spend")):
        assert f'<a href="{href}">{label}</a>' in page
    # From the money panel, and from the spend tile.
    assert ('<p class="charts">Charts: <a href="/dashboard/money">Money by day</a> &middot; '
            '<a href="/dashboard/pnl">Weekly P&amp;L</a></p>') in page
    assert '<a href="/dashboard/spend">Development spend by week</a>' in page
    status, headers, body = _get(site, pages.STATE)
    assert status == 200 and headers["Content-Type"] == pages.JSON and "/dashboard/money" not in body
    assert "charts" not in json.loads(body)


def test_the_dashboard_run_on_its_own_has_no_chart_links(schema_url):
    with open_in(*schema_url) as local:
        page = dashboard.render(dashboard.snapshot(local))
    assert "/dashboard/money" not in page and 'class="charts"' not in page and "Development spend by week" not in page


def test_the_chart_pages_follow_the_pages_rules_when_they_cant_serve(monkeypatch, capsys):
    # Not set up yet.
    for path in CHART_PATHS:
        reply = pages.respond(path)
        assert reply.status == 503 and "set up here yet" in reply.body.replace("&#x27;", "'")
    monkeypatch.setenv(pages.READER, "set")

    def down():
        raise psycopg.OperationalError(SECRET)
    monkeypatch.setattr(pages, "connect", down)
    for path in CHART_PATHS:
        reply = pages.respond(path)
        assert reply.status == 503 and "db.example.net" not in reply.body and "g2_admin" not in reply.body
    assert "db.example.net" in capsys.readouterr().err


class _Quiet:
    """A connection that is never used: the loaders are replaced. It opens a snapshot as a real one does."""
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def transaction(self):
        from contextlib import nullcontext
        return nullcontext()

    def execute(self, *args):
        return None


def test_a_chart_loader_or_renderer_that_fails_never_shows_its_error(monkeypatch, capsys):
    monkeypatch.setenv(pages.READER, "set")
    monkeypatch.setattr(pages, "connect", _Quiet)
    monkeypatch.setattr(pages, "stored_slots", lambda conn: ("15",))   # the fake connection holds none
    monkeypatch.setattr(pages, "writable", lambda conn: [])

    def failing(*args):
        raise psycopg.errors.QueryCanceled(SECRET)
    for name in ("money_data", "pnl_data", "spend_data"):
        monkeypatch.setattr(pages, name, failing)
    for path in CHART_PATHS:
        reply = pages.respond(path)
        assert reply.status == 503 and reply.body.count("db.example.net") == 0 and "can't read" in reply.body.replace("&#x27;", "'")
    assert "db.example.net" in capsys.readouterr().err

    def broken(*args):
        raise RuntimeError(SECRET)
    monkeypatch.setattr(pages, "money_data", lambda conn: {"daily": [{}]})
    monkeypatch.setattr(pages.money_chart, "live_page", broken)
    reply = pages.respond(pages.MONEY_CHART)
    assert reply.status == 500 and "db.example.net" not in reply.body and "hit an error" in reply.body
    # Without our slot assignment, a plain sentence, not the exception's.
    from vending.slots import SlotsNotRecorded

    def unrecorded(conn):
        raise SlotsNotRecorded(SECRET)
    monkeypatch.setattr(pages, "money_data", unrecorded)
    reply = pages.respond(pages.MONEY_CHART)
    assert reply.status == 503 and "slot assignment isn't recorded" in reply.body.replace("&#x27;", "'")
    assert "db.example.net" not in reply.body


def test_a_chart_page_waits_its_turn_for_a_connection(monkeypatch):
    monkeypatch.setenv(pages.READER, "set")
    monkeypatch.setattr(pages, "WAIT_SECONDS", 0.05)
    monkeypatch.setattr(pages, "connect", lambda: pytest.fail("no connection should be opened"))
    taken = threading.BoundedSemaphore(1)
    taken.acquire()
    monkeypatch.setattr(pages, "_slots", taken)
    for path in CHART_PATHS:
        reply = pages.respond(path)
        assert reply.status == 503 and "busy" in reply.body
    taken.release()


def test_the_live_money_chart_refuses_an_empty_series_rather_than_draw_mock_data():
    from vending import money_chart
    for empty in (None, {}, {"opening": {}, "daily": []}):
        with pytest.raises(money_chart.NoData):
            money_chart.live_page(empty)
    # The file the CLI writes with --demo still has its MOCK data; only the served page turns it off.
    assert "const MOCK = false;" not in money_chart.page(None)


def test_a_chart_policy_allows_only_scripts_it_can_hash():
    assert "script-src 'sha256-" in pages.chart_csp("<html><body><script>1</script></body></html>")
    for page in ("<script src=x></script>", '<script type="module">1</script>', "<p>no script</p>"):
        with pytest.raises(RuntimeError):
            pages.chart_csp(page)


def test_a_cached_chart_is_served_without_a_connection_for_a_minute(monkeypatch):
    monkeypatch.setenv(pages.READER, "set")
    monkeypatch.setattr(pages, "connect", _Quiet)
    monkeypatch.setattr(pages, "writable", lambda conn: [])
    loads = []
    monkeypatch.setattr(pages, "spend_data", lambda conn: loads.append(1) or ("weeks", "days"))
    monkeypatch.setattr(pages, "spend_page", lambda weeks, days: f"<html><body><script>{len(loads)}</script></body></html>")
    first = pages.respond(pages.SPEND)
    assert first.status == 200 and loads == [1]
    # Every connection taken: the kept page still answers, and no connection is asked for.
    monkeypatch.setattr(pages, "connect", lambda: pytest.fail("no connection should be opened"))
    taken = threading.BoundedSemaphore(1)
    taken.acquire()
    monkeypatch.setattr(pages, "_slots", taken)
    monkeypatch.setattr(pages, "WAIT_SECONDS", 0.05)
    assert pages.respond(pages.SPEND) is first and pages.respond(pages.SPEND + "/") is first and loads == [1]
    # After CACHE_SECONDS it reads again (here: is told the page is busy, and keeps nothing).
    key = (pages.SPEND, None)
    pages._cache[key] = (0.0, first)
    assert pages.respond(pages.SPEND).status == 503 and pages._cache[key][0] == 0.0
    taken.release()


def test_each_pnl_range_is_kept_apart_and_errors_are_never_kept(monkeypatch):
    monkeypatch.setenv(pages.READER, "set")
    monkeypatch.setattr(pages, "connect", _Quiet)
    monkeypatch.setattr(pages, "stored_slots", lambda conn: ("15",))   # the fake connection holds none
    monkeypatch.setattr(pages, "writable", lambda conn: [])
    asked = []
    monkeypatch.setattr(pages, "pnl_data", lambda conn, query: asked.append(query) or query)
    monkeypatch.setattr(pages, "pnl_page", lambda q: (400 if q.view == "detailed" else 200, f"<p>{q}</p>"))
    for target in (pages.PNL, pages.PNL, f"{pages.PNL}?start=2026-12-05", f"{pages.PNL}?view=detailed",
                   f"{pages.PNL}?view=detailed"):
        pages.respond(target)
    # The whole run once (then kept), the other range once, and the 400 both times (never kept).
    assert [(q.start is None, q.view) for q in asked] == [(True, "simple"), (False, "simple"),
                                                          (True, "detailed"), (True, "detailed")]
    # Kept at most CACHE_ENTRIES, dropping the soonest to expire.
    monkeypatch.setattr(pages, "CACHE_ENTRIES", 2)
    pages._cache.clear()
    for day in ("2026-12-05", "2026-12-06", "2026-12-07"):
        pages.respond(f"{pages.PNL}?start={day}")
    assert len(pages._cache) == 2


def test_visits_to_the_chart_pages_are_not_counted():
    """`page_visits.page` allows only /login, /dashboard and /db (migration 016)."""
    from vending import visits
    assert not set(CHART_PATHS) & set(visits.PAGES)


# --- The chart demos: /dashboard/demo/money and /dashboard/demo/spend -------------------------------

def test_the_chart_demos_draw_made_up_data_under_a_banner_with_no_database(monkeypatch):
    # Not even set up: a demo needs no login, no connection and no slot assignment.
    monkeypatch.setattr(pages, "connect", lambda: pytest.fail("a demo opened a connection"))
    monkeypatch.setattr(pages, "our_slots", lambda conn: pytest.fail("a demo read the slots"))
    money = pages.respond(pages.DEMO_MONEY)
    assert money.status == 200 and money.content_type == pages.HTML
    _scripts_allowed_by_hash(money.body, money.csp)
    # The page's own MOCK switch is left on, so it draws its seeded data, and says so twice.
    assert "const REAL = /*DATA*/null;" in money.body and "const MOCK = !(REAL" in money.body
    assert "DEMO: made-up data, not our machine" in money.body and "position:sticky" in money.body
    assert '<div class="banner" id="banner"><strong>MOCK DATA:</strong>' in money.body
    assert money.body.index("DEMO: made-up data") < money.body.index('id="banner"')
    assert '<a href="/dashboard/money" style="color:#fff">the real page</a>' in money.body
    spend = pages.respond(pages.DEMO_SPEND + "/")
    assert spend.status == 200 and "DEMO: the runs the team wrote down" in spend.body
    _scripts_allowed_by_hash(spend.body, spend.csp)
    assert _inlined(spend.body, "DATA")["weekly"]["source"] == "from run notes, not the ledger"
    # No demo for the P&L, and nothing else under /dashboard/demo/ is one of ours.
    assert pages.respond("/dashboard/demo/pnl") is None and pages.respond("/dashboard/demo/money.json") is None


def test_only_the_demo_routes_may_draw_mock_data(site, conn):
    from tests.test_ledger import open_ledger
    open_ledger(conn)
    live = _get(site, pages.MONEY_CHART)[2]
    demo = _get(site, pages.DEMO_MONEY)[2]
    assert "const MOCK = false;" in live and "DEMO:" not in live and "const MOCK = false;" not in demo


def test_a_demo_that_fails_never_shows_its_error(monkeypatch, capsys):
    def broken(*args):
        raise RuntimeError(SECRET)
    monkeypatch.setattr(pages.money_chart, "page", broken)
    reply = pages.respond(pages.DEMO_MONEY)
    assert reply.status == 500 and "db.example.net" not in reply.body and "hit an error" in reply.body
    assert "db.example.net" in capsys.readouterr().err


# --- Review fixes (Sol, 19 Nov) ----------------------------------------------------------------------

def _units(page: str) -> str | None:
    import re
    found = re.search(r'Units sold</div><div class="tile-value">(\d+)<', page)
    return found and found.group(1)


def test_a_kept_chart_follows_a_changed_or_removed_assignment(site, conn):
    """Within the minute a chart is kept, a new or removed slot assignment still changes what it shows."""
    from vending.money_series import NOT_OURS_LINE
    _competition(conn)
    money, pnl = _get(site, pages.MONEY_CHART)[2], _get(site, pages.PNL)[2]
    assert "slot 15: " in money and _units(pnl) == "5"

    conn.execute("update slot_assignment set slots = %s", (list(EVERY_OTHER),))
    status, _, money = _get(site, pages.MONEY_CHART)
    assert status == 200 and "slot 15: " not in money and NOT_OURS_LINE in money.replace("&#x27;", "'")
    status, _, pnl = _get(site, pages.PNL)
    assert status == 200 and _units(pnl) == "0"          # slot 15's sales aren't ours any more

    conn.execute("delete from slot_assignment")
    status, _, money = _get(site, pages.MONEY_CHART)
    assert status == 503 and "slot assignment isn't recorded" in money.replace("&#x27;", "'")
    status, _, pnl = _get(site, pages.PNL)
    assert _units(pnl) is None and "slot 15" not in pnl


def test_the_live_money_chart_carries_no_mock_generator(site, conn):
    from tests.test_ledger import open_ledger
    open_ledger(conn)
    live, demo = _get(site, pages.MONEY_CHART)[2], _get(site, pages.DEMO_MONEY)[2]
    for synthetic in ("Day-one fill: $165.60 of drinks", "let seed = 7", "Raised the five best sellers"):
        assert synthetic not in live and synthetic in demo, synthetic
    assert "function mockDays()" in live and "const MOCK = false;" in live   # a stub, never called


def test_the_money_panel_links_the_served_pnl_only_where_it_is_served(site, schema_url, conn):
    from tests.test_ledger import open_ledger
    open_ledger(conn)
    served = _get(site, "/dashboard")[2]
    assert '<a href="/dashboard/pnl">the weekly P&amp;L</a>' in served
    with open_in(*schema_url) as local:
        alone = dashboard.render(dashboard.snapshot(local))
    assert "Profit and loss since the opening" in alone
    assert "/dashboard/pnl" not in alone and "Weekly detail: <code>python -m vending.pnl_report</code>" in alone


def test_a_kept_money_chart_skips_its_replay_only_for_the_same_assignment(monkeypatch):
    monkeypatch.setenv(pages.READER, "set")
    monkeypatch.setattr(pages, "connect", _Quiet)
    monkeypatch.setattr(pages, "writable", lambda conn: [])
    slots, loads = [("15", "16")], []
    monkeypatch.setattr(pages, "stored_slots", lambda conn: slots[0])
    monkeypatch.setattr(pages, "money_data", lambda conn: loads.append(slots[0]) or {"daily": [{}]})
    monkeypatch.setattr(pages, "money_page", lambda payload: f"<html><body><script>{len(loads)}</script></body></html>")
    first = pages.respond(pages.MONEY_CHART)
    assert pages.respond(pages.MONEY_CHART) is first and loads == [("15", "16")]
    slots[0] = ("12", "14")
    assert pages.respond(pages.MONEY_CHART) is not first and loads == [("15", "16"), ("12", "14")]


# --- Review round 2 (Sol, 19 Nov) --------------------------------------------------------------------

def test_a_kept_chart_is_the_one_its_assignment_counted_even_if_it_changes_mid_request(site, conn, monkeypatch):
    """A reassignment committed after the page reads the assignment, but before it builds the chart, must
    not be kept under the assignment it read: both come from one snapshot."""
    _competition(conn)
    real, moved = pages.stored_slots, []

    def then_reassign(c):
        slots = real(c)
        if not moved:                   # committed on the test's own (writer) connection
            conn.execute("update slot_assignment set slots = %s", (list(EVERY_OTHER),))
            moved.append(slots)
        return slots
    monkeypatch.setattr(pages, "stored_slots", then_reassign)
    from tests.conftest import TEST_SLOTS
    from vending import pnl_report
    for path, query, counted_a in ((pages.MONEY_CHART, None, lambda page: "slot 15: " in page),
                                   (pages.PNL, pnl_report.parse("/"), lambda page: _units(page) == "5")):
        moved.clear()
        conn.execute("update slot_assignment set slots = %s", (list(TEST_SLOTS),))
        status, _, page = _get(site, path)
        assert status == 200 and moved == [TEST_SLOTS], path
        kept = {key: reply for key, (_, reply) in pages._cache.items() if key[0] == path}
        assert list(kept) == [(path, query, TEST_SLOTS)], path
        # The page and its kept copy are assignment A's, the one in the key.
        assert counted_a(page) and counted_a(next(iter(kept.values())).body), path
        pages._cache.clear()


def test_the_live_money_chart_carries_no_demo_copy(site, conn):
    import re
    from tests.test_ledger import open_ledger
    open_ledger(conn)
    live, demo = _get(site, pages.MONEY_CHART)[2], _get(site, pages.DEMO_MONEY)[2]
    for copy in ("MOCK DATA:", "(seeded random data)", "Mock data, seeded.", "Mock data.",
                 "AI planning runs are on Thursdays in this mock", '"Weekly plans"', '"AI plan"',
                 '"Competition starts"', 'id="mockTag">MOCK<', "Money Chart Prototype", "seeded MOCK data"):
        assert copy not in live and copy in demo, copy
    # No visible text and no string in the script mentions mock, seeded or made-up data. The one name
    # allowed is the id of the (emptied, hidden) tag the script looks up.
    markup = re.sub(r"<(style|script)>.*?</\1>", "", live, flags=re.S)
    script = "".join(re.findall(r"<script>(.*?)</script>", live, re.S))
    texts = re.findall(r">([^<>]+)<", markup) + re.findall(r'"(?:[^"\\\n]|\\.)*"|`(?:[^`\\]|\\.)*`', script)
    assert [t for t in texts if re.search(r"mock|made.up|seeded", t, re.I)] == ['"mockTag"']
    assert [line for line in script.split("\n") if re.search(r"//.*(mock|seeded|made.up)", line, re.I)] == []


def test_the_public_dashboard_keeps_a_pick_of_ours_through_its_no_script_reload(site):
    # The pick links ask for ?sel=NN#sel-NN; the reload keeps a selection of ours, and nothing else.
    kept = _get(site, "/dashboard?sel=15")[2]
    assert 'content="30; url=?refresh=1&amp;sel=15#sel-15"' in kept
    for other in ("12", "15%22%3E%3Cscript%3E", ""):     # not ours, not a selection, empty
        page = _get(site, f"/dashboard?sel={other}")[2]
        assert 'content="30; url=?refresh=1"' in page and "sel=12" not in page.split("<noscript>")[1][:120], other


def test_when_the_database_is_down_the_worker_still_names_its_failed_syncs(monkeypatch, capsys, site):
    """#220: with Postgres unreachable the page can't be drawn, but an open tab must still learn of
    a failed sync round. The 503s carry the worker's own alert lines, and the page's refresh
    handler lifts the banner out of the outage page instead of keeping a stale one."""
    state = site.RequestHandlerClass.state
    state.results = worker.run_round([("sync_history", lambda: 3)], log=lambda _: None)
    state.rounds, state.last_round_at = 1, state.started_at

    def down():
        raise psycopg.OperationalError(SECRET)

    monkeypatch.setattr(pages, "connect", down)
    status, _, body = _get(site, "/dashboard")
    assert status == 503 and SECRET not in body
    assert '<div class="failbanner" role="alert">' in body
    assert "Sync round failed" in body and "sync_history" in body and "exit 3" in body
    assert "Can't read the database" in body
    assert 'data-sync-ok="1"' in body
    status, _, body = _get(site, "/dashboard/state.json")
    data = json.loads(body)
    assert status == 503 and SECRET not in body
    assert [a["kind"] for a in data["alerts"]] == ["sync_failed"]
    assert "`sync_history` exit 3" in data["alerts"][0]["text"]
    # The refresh handler swaps only the banner in an outage, and the error still reaches the log.
    assert "failbanner" in dashboard.SCRIPT and "data-sync-ok" in dashboard.SCRIPT
    assert SECRET in capsys.readouterr().err
    # A worker whose latest round passed: the outage names nothing. It still marks itself, so the
    # page's refresh handler can tell a marked outage with no banner from another error page and
    # clear a stale banner instead of keeping it.
    state.results = worker.run_round([("sync_history", lambda: 0)], log=lambda _: None)
    body = _get(site, "/dashboard")[2]
    assert '<div class="failbanner"' not in body and 'data-sync-ok="1"' in body
    assert json.loads(_get(site, "/dashboard/state.json")[2])["alerts"] == []


def test_a_generic_500_is_not_marked_as_the_outage(monkeypatch, site):
    """A bug drawing the page is not a database outage: the 500 carries neither the outage marker
    (which would let the refresh handler swap a banner in) nor the worker's banner, so an open tab
    keeps what it shows."""
    def broken(*a, **k):
        raise RuntimeError("a bug drawing the page")

    monkeypatch.setattr(pages.dashboard, "snapshot", broken)
    status, _, body = _get(site, "/dashboard")
    assert status == 500 and 'data-sync-ok="1"' not in body and '<div class="failbanner"' not in body
    status, _, body = _get(site, "/dashboard/state.json")
    assert status == 500 and json.loads(body)["alerts"] == []
