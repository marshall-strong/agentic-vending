"""The public pages (#53): served by the worker, on a login that can only read, with no error text."""

import json
import threading
import urllib.error
import urllib.request
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from tests.conftest import CONNECT_TIMEOUT, open_in
from vending import agent_runs, dashboard, db_explorer, pages, worker
from vending.write_gate import log

SECRET = 'connection to server at "db.example.net", port 5432 failed: password authentication failed for user "g2_admin"'


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    """Each test starts with no passed read-only check and nothing logged yet."""
    monkeypatch.setattr(pages, "_read_only_until", 0.0)
    monkeypatch.setattr(pages, "_logged", {})
    monkeypatch.delenv(pages.READER, raising=False)


def _role(admin, schema, usage=True):
    """A new login with nothing but the right to look inside the test schema. Skips when the
    test database's user may not create roles."""
    name = f"reader_{uuid.uuid4().hex[:8]}"
    try:
        admin.execute(sql.SQL("create role {} login password 'reader'").format(sql.Identifier(name)))
    except psycopg.errors.InsufficientPrivilege:
        pytest.skip("the test database's user can't create roles")
    if usage:
        admin.execute(sql.SQL("grant usage on schema {} to {}").format(sql.Identifier(schema), sql.Identifier(name)))
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
        name = _role(admin, schema)
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
    run = agent_runs.start(conn, mode="dry-run")
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
    assert refused.value.code == 501


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

def test_failures_keep_their_error_text_to_themselves(site, schema_url, conn):
    _, schema = schema_url
    log(conn, "read", "get_machine_state", {}, "failed", f"OperationalError: {SECRET}")
    conn.execute("update action_log set result = %s where outcome = 'failed'",
                 (psycopg.types.json.Jsonb({"success": False, "message": SECRET}),))
    log(conn, "write", "submit_desired_state", {}, "refused", "halted: turn the switch off first")
    log(conn, "read", "get_catalog", {}, "done", "26 products")
    run = agent_runs.start(conn, mode="live")
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
    # The local pages, for the team, still show it.
    with open_in(*schema_url) as local:
        assert "db.example.net" in dashboard.to_json(dashboard.snapshot(local))
        assert "db.example.net" in db_explorer.render(local, db_explorer.Request(schema, "action_log", 0))[1]


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


# --- The commands -------------------------------------------------------------------------------

def test_grant_lets_a_role_read_every_table_now_and_later(schema_url, conn, capsys):
    url, schema = schema_url
    with psycopg.connect(url, autocommit=True, connect_timeout=CONNECT_TIMEOUT) as admin:
        name = _role(admin, schema)
        try:
            with _as(url, schema, name) as looking:
                assert f"{schema}.config" in pages.unreadable(looking)
            done = pages.grant(conn, name, "marshall")
            assert name in done and "marshall" in done
            assert conn.execute("select tool, outcome, detail from action_log order by id desc limit 1").fetchone() == (
                "pages.grant", "done", done)
            # A table a later migration adds is readable too, and still not writable.
            conn.execute("create table later_table (id int)")
            with _as(url, schema, name) as looking:
                assert pages.unreadable(looking) == [] and pages.writable(looking) == []
                assert looking.execute("select count(*) from later_table").fetchone() == (0,)
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
