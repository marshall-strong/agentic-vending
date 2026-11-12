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
from vending import agent_runs, dashboard, db_explorer, pages, settings, worker
from vending.slots import SLOTS
from vending.write_gate import log

SECRET = 'connection to server at "db.example.net", port 5432 failed: password authentication failed for user "g2_admin"'


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    """Each test starts with no passed read-only check, nothing logged yet, and no pages login,
    not even one in the developer's `.env` (which `settings.get` falls back to)."""
    monkeypatch.setattr(pages, "_read_only_until", 0.0)
    monkeypatch.setattr(pages, "_logged", {})
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


# --- The other team's selections ----------------------------------------------------------------

RIVAL = "RIVAL-SNACK"


def _rivals(conn):
    """A machine where selections 11 and 25 are held by someone else's counts and polls, one more
    machine's poll names our selection 25, and ours (15) is counted and polled too."""
    from tests.conftest import store_poll
    from vending import slot_counts
    store_poll(conn, "ours", "2026-11-10T15:00:00+00:00", {"15": (150, 31), "11": (1234, 987654)})
    store_poll(conn, "elsewhere", "2026-11-10T15:00:00+00:00", {"15": (999, 424242)}, device_id="vmc2")
    slot_counts.record(conn, slot_counts.parse({
        "count_id": "p1", "counted_at": "2026-11-09T11:30:00-05:00", "source": "photo", "device_id": "vmc1",
        "selections": [{"selection": "15", "product": "Our Chips", "units": 4, "price_on_tag": "1.50"},
                       {"selection": "11", "product": RIVAL, "units": 9, "price_on_tag": "7.77"}]}), "kevin")


def test_the_public_json_has_only_our_selections_in_its_counts(site, schema_url, conn):
    _rivals(conn)
    status, _, body = _get(site, "/dashboard/state.json")
    counts = json.loads(body)["counts"]["data"]
    assert status == 200 and list(counts) == ["15"] and counts["15"]["product"] == "Our Chips"
    assert RIVAL not in body and "7.77" not in body
    sales, stock = json.loads(body)["sales"]["data"]["slots"], json.loads(body)["stock"]["data"]["slots"]
    assert "11" not in sales and "11" not in stock and set(sales) | set(stock) <= set(SLOTS)
    # The local snapshot, for the team, still has the whole count.
    with open_in(*schema_url) as local:
        assert sorted(dashboard.snapshot(local)["counts"]["data"]) == ["11", "15"]


def test_the_public_explorer_shows_only_our_rows_of_the_selection_tables(site, schema_url, conn):
    _, schema = schema_url
    _rivals(conn)
    counts = _get(site, f"/db?schema={schema}&table=slot_counts")[2]
    assert "Our Chips" in counts and RIVAL not in counts and "7.77" not in counts
    polls = _get(site, f"/db?schema={schema}&table=poll_selections")[2]
    # Our selection 15 on our machine, with its price and sales; not selection 11, not another machine.
    assert "31" in polls and "987654" not in polls and "1234" not in polls and "424242" not in polls
    # The page says how many rows it holds: only the ones it may show.
    with open_in(*schema_url) as local:
        for name, shown in (("slot_counts", 1), ("poll_selections", 1)):
            table = db_explorer.Table(schema, name, "table")
            assert db_explorer.rows(local, table, only=pages.ONLY[name]).total == shown
            assert db_explorer.rows(local, table).total == 2 + (name == "poll_selections")
    # The explorer run locally still shows everything.
    with open_in(*schema_url) as local:
        assert RIVAL in db_explorer.render(local, db_explorer.Request(schema, "slot_counts", 0))[1]


def test_a_table_without_the_columns_a_row_filter_reads_shows_no_rows(schema_url, conn):
    _, schema = schema_url
    _rivals(conn)
    conn.execute("create table slot_counts_copy as select count_id, selection, product from slot_counts")
    table = db_explorer.Table(schema, "slot_counts_copy", "table")
    only = pages.ONLY["slot_counts"]   # reads device_id too, which the copy lacks
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
