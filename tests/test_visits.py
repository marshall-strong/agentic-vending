"""Counting sign-ins and visits to the container's pages (`vending.visits`, served by `vending.worker`)."""

from __future__ import annotations

import queue
import threading
import time
from urllib.parse import urlencode

import psycopg
import pytest

from tests.test_gate import PASSWORD, ask
from vending import gate, pages, visits, worker

BROWSER = "0123456789abcdef"


@pytest.fixture
def events(monkeypatch):
    """The events the pages queue, without a writer thread taking them."""
    q: queue.Queue = queue.Queue(visits.QUEUE_SIZE)
    monkeypatch.setattr(visits, "_events", q)
    monkeypatch.setattr(visits, "_last_view", {})
    monkeypatch.setattr(visits, "_start_writer", lambda: None)
    return q


def drain(q: queue.Queue) -> list[tuple[str, str, str]]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


# --- Pure ------------------------------------------------------------------------------------------

def test_a_browser_id_comes_only_from_its_own_well_formed_cookie():
    assert visits.browser_of(f"vend_session=x; vend_browser={BROWSER}") == BROWSER
    assert visits.browser_of("vend_session=x") is None
    assert visits.browser_of(None) is None
    assert visits.browser_of("vend_browser=not-hex-at-all!") is None
    assert visits.browser_of(f"vend_browser={BROWSER}00") is None
    assert visits.browser_of('vend_browser="; drop table') is None


def test_new_browser_ids_are_random_and_well_formed():
    a, b = visits.new_browser(), visits.new_browser()
    assert a != b and visits.browser_of(f"vend_browser={a}") == a


def test_the_dashboards_refresh_and_any_script_fetch_are_not_visits():
    assert visits.is_refresh({visits.REFRESH_HEADER: "1"})
    assert visits.is_refresh({"Sec-Fetch-Mode": "cors"})
    assert visits.is_refresh({"Sec-Fetch-Mode": "same-origin"})
    assert not visits.is_refresh({"Sec-Fetch-Mode": "navigate"})
    assert not visits.is_refresh({})
    # Without scripts the page reloads itself to ?refresh=1, and the browser calls that a navigation.
    assert visits.is_refresh({"Sec-Fetch-Mode": "navigate"}, "/dashboard?refresh=1")
    assert visits.is_refresh({}, "/dashboard?x=2&refresh=1")
    assert not visits.is_refresh({}, "/dashboard?refresh=10")
    assert not visits.is_refresh({}, "/db?table=refresh")


def test_a_browser_opening_a_page_counts_once_per_half_hour(events):
    assert visits.view("/dashboard", BROWSER, now=1000.0)
    assert not visits.view("/dashboard", BROWSER, now=1000.0 + 29 * 60)
    assert visits.view("/db", BROWSER, now=1000.0 + 29 * 60)
    assert visits.view("/dashboard", BROWSER, now=1000.0 + 30 * 60)
    assert not visits.view("/", BROWSER, now=1000.0)
    assert drain(events) == [("view", "/dashboard", BROWSER), ("view", "/db", BROWSER),
                             ("view", "/dashboard", BROWSER)]


def test_a_full_queue_drops_the_event_instead_of_making_the_page_wait(monkeypatch):
    monkeypatch.setattr(visits, "_events", queue.Queue(1))
    monkeypatch.setattr(visits, "_start_writer", lambda: None)
    visits.sign_in(BROWSER)
    visits.sign_in(BROWSER)   # would block for ever with a blocking put
    assert visits._events.qsize() == 1


def test_switched_off_it_queues_nothing_and_gives_no_id(events, monkeypatch):
    monkeypatch.setattr(visits, "ENABLED", False)
    visits.sign_in(BROWSER)
    visits.view("/dashboard", BROWSER)
    assert events.empty()
    assert visits.identify(None) == (None, ())


def test_a_write_that_fails_is_dropped_and_never_raises(monkeypatch, capsys):
    def broken():
        raise OSError("connection refused")

    monkeypatch.setattr(pages, "_slots", threading.BoundedSemaphore(pages.MAX_CONNECTIONS))
    monkeypatch.setattr(visits, "connect", broken)
    monkeypatch.setattr(visits, "_logged", {})
    monkeypatch.setattr(visits, "dropped", 0, raising=False)
    assert visits.flush([("view", "/dashboard", BROWSER)]) is False
    assert "visits: dropped 1" in capsys.readouterr().err and visits.dropped == 1


class _FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_writer_uses_one_of_the_pages_connections_not_a_third(monkeypatch):
    """The pages may hold at most `pages.MAX_CONNECTIONS` connections, the writer's included."""
    slots = threading.BoundedSemaphore(pages.MAX_CONNECTIONS)
    monkeypatch.setattr(pages, "_slots", slots)
    free_while_writing = []

    def connect():
        free_while_writing.append(slots._value)
        return _FakeConn()

    monkeypatch.setattr(visits, "connect", connect)
    monkeypatch.setattr(visits, "write", lambda conn, batch: None)
    assert visits.flush([("view", "/dashboard", BROWSER)]) is True
    assert free_while_writing == [pages.MAX_CONNECTIONS - 1]
    assert slots._value == pages.MAX_CONNECTIONS   # given back


def test_with_every_page_connection_in_use_the_writer_drops_the_batch_without_connecting(monkeypatch, capsys):
    slots = threading.BoundedSemaphore(pages.MAX_CONNECTIONS)
    for _ in range(pages.MAX_CONNECTIONS):
        slots.acquire()
    monkeypatch.setattr(pages, "_slots", slots)
    monkeypatch.setattr(visits, "SLOT_WAIT_SECONDS", 0.3, raising=False)
    monkeypatch.setattr(visits, "SLOT_POLL_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(visits, "_logged", {})
    monkeypatch.setattr(visits, "dropped", 0, raising=False)
    connected = []
    monkeypatch.setattr(visits, "connect", lambda: connected.append(1) or _FakeConn())
    monkeypatch.setattr(visits, "write", lambda conn, batch: None)
    started = time.monotonic()
    assert visits.flush([("view", "/dashboard", BROWSER), ("sign_in", "/login", BROWSER)]) is False
    assert time.monotonic() - started < 2
    assert connected == [] and visits.dropped == 2
    assert "no page connection came free" in capsys.readouterr().err


def test_a_page_waiting_for_a_connection_gets_it_before_the_writer(monkeypatch):
    """The writer only takes a connection that is free, so it never queues ahead of a page."""
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(pages, "_slots", slots)
    page_got = threading.Event()

    def page():
        slots.acquire(timeout=5)
        page_got.set()

    waiting = threading.Thread(target=page)
    waiting.start()
    time.sleep(0.1)
    slots.release()           # the page that held it is done
    waiting.join(5)
    assert page_got.is_set()
    with pages.spare_slot(0.2, 0.05) as free:
        assert not free       # the page still has it
    slots.release()


def test_the_writers_connection_has_the_page_readers_limits(monkeypatch):
    """A connection gone silent mid-write must fail within seconds, as a page's does, or it would
    hold one of the two page connections for as long as TCP retries (15 minutes or more)."""
    seen, said = {}, []

    class Conn:
        def execute(self, statement, *args):
            said.append(str(statement))

    conn = Conn()
    monkeypatch.setenv("DATABASE_URL", "postgresql://writer@127.0.0.1/x")
    monkeypatch.setattr(psycopg, "connect", lambda url, **kw: seen.update(kw, url=url) or conn)
    assert visits._connect() is conn and seen["url"] == "postgresql://writer@127.0.0.1/x"
    assert {k: seen.get(k) for k in pages.KEEPALIVES} == pages.KEEPALIVES
    timeout = f"statement_timeout={visits.STATEMENT_TIMEOUT_MS}"
    assert timeout in seen.get("options", "") or any(timeout in s.replace(" ", "") for s in said)
    assert seen["connect_timeout"] <= 10 and seen["autocommit"] is True


# --- Through the worker, behind the password ---------------------------------------------------

@pytest.fixture
def site(monkeypatch, events):
    monkeypatch.setattr(gate, "ENABLED", True)
    monkeypatch.setattr(gate, "PASSWORD_HASH", gate.make_hash(PASSWORD, n=2**10))
    monkeypatch.setattr(gate, "WRONG_DELAY_SECONDS", 0)
    monkeypatch.setattr(gate, "_next_try", {})
    monkeypatch.setenv(gate.KEY_SETTING, "postgresql://reader:secret@example/db")
    monkeypatch.setattr(pages, "respond", lambda target, syncs=None: pages.Reply(200, pages.HTML, "<main>page</main>"))
    server = worker.serve_health(worker.State(), 0)
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def set_cookies(port: int, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
    """(status, every Set-Cookie value, body). `ask` keeps only one header of each name."""
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        reply = conn.getresponse()
        return reply.status, reply.headers.get_all("Set-Cookie") or [], reply.read()
    finally:
        conn.close()


def test_a_sign_in_gives_a_browser_id_and_is_counted(site, events):
    form = urlencode({"password": PASSWORD, "next": "/dashboard"}).encode()
    status, cookies, _ = set_cookies(site, "POST", "/login", form,
                                     {"Content-Type": "application/x-www-form-urlencoded"})
    assert status == 303
    names = [c.split("=", 1)[0] for c in cookies]
    assert names == [gate.COOKIE, visits.COOKIE]
    browser = visits.browser_of(cookies[1].split(";", 1)[0])
    assert browser and all("HttpOnly" in c and "Secure" in c for c in cookies)
    assert drain(events) == [("sign_in", "/login", browser)]


def test_signing_in_again_keeps_the_browsers_id(site, events):
    form = urlencode({"password": PASSWORD}).encode()
    _, cookies, _ = set_cookies(site, "POST", "/login", form, {
        "Content-Type": "application/x-www-form-urlencoded", "Cookie": f"vend_browser={BROWSER}"})
    assert [c.split("=", 1)[0] for c in cookies] == [gate.COOKIE]
    assert drain(events) == [("sign_in", "/login", BROWSER)]


def test_a_wrong_password_counts_nothing(site, events):
    form = urlencode({"password": "wrong"}).encode()
    status, cookies, _ = set_cookies(site, "POST", "/login", form,
                                     {"Content-Type": "application/x-www-form-urlencoded"})
    assert status == 401 and cookies == [] and events.empty()


def test_opening_the_dashboard_counts_and_its_refresh_does_not(site, events):
    signed = f"{gate.COOKIE}={gate.token()}; vend_browser={BROWSER}"
    status, cookies, _ = set_cookies(site, "GET", "/dashboard", headers={"Cookie": signed, "Sec-Fetch-Mode": "navigate"})
    assert status == 200 and cookies == []
    set_cookies(site, "GET", "/db?table=ledger", headers={"Cookie": signed})
    events_seen = drain(events)
    assert events_seen == [("view", "/dashboard", BROWSER), ("view", "/db", BROWSER)]
    visits._last_view.clear()
    set_cookies(site, "GET", "/dashboard", headers={"Cookie": signed, visits.REFRESH_HEADER: "1"})
    set_cookies(site, "GET", "/dashboard", headers={"Cookie": signed, "Sec-Fetch-Mode": "cors"})
    assert events.empty()


def test_the_no_script_reload_of_the_dashboard_is_not_a_visit(site, events):
    """A tab without scripts reloads every 30 seconds through the meta refresh, as a navigation
    with no header; it lands on ?refresh=1, so an idle tab isn't a visit every half hour."""
    signed = f"{gate.COOKIE}={gate.token()}; vend_browser={BROWSER}"
    status, _, _ = set_cookies(site, "GET", "/dashboard?refresh=1",
                               headers={"Cookie": signed, "Sec-Fetch-Mode": "navigate"})
    assert status == 200 and events.empty()


def test_a_browser_signed_in_before_counting_started_gets_an_id_without_signing_out(site, events):
    status, cookies, _ = set_cookies(site, "GET", "/dashboard", headers={"Cookie": f"{gate.COOKIE}={gate.token()}"})
    assert status == 200
    assert [c.split("=", 1)[0] for c in cookies] == [visits.COOKIE]
    browser = visits.browser_of(cookies[0].split(";", 1)[0])
    assert drain(events) == [("view", "/dashboard", browser)]


def test_signed_out_nothing_is_counted(site, events):
    status, _, _ = ask(site, "GET", "/dashboard")
    assert status == 303 and events.empty()


def test_a_page_that_failed_is_not_a_visit(site, events, monkeypatch):
    monkeypatch.setattr(pages, "respond", lambda target, syncs=None: pages.Reply(503, pages.HTML, "busy"))
    signed = f"{gate.COOKIE}={gate.token()}; vend_browser={BROWSER}"
    status, _, _ = set_cookies(site, "GET", "/dashboard", headers={"Cookie": signed})
    assert status == 503 and events.empty()


# --- Database ------------------------------------------------------------------------------------

def test_counts_and_the_dashboards_tile(conn):
    from vending import dashboard

    other = "fedcba9876543210"
    visits.write(conn, [("sign_in", "/login", BROWSER), ("view", "/dashboard", BROWSER),
                        ("view", "/db", BROWSER), ("sign_in", "/login", other), ("view", "/dashboard", other)])
    conn.execute("update page_visits set at = now() - interval '10 days' where browser = %s", (other,))
    c = visits.counts(conn)
    assert (c["browsers"], c["browsers_7_days"], c["sign_ins"], c["visits"], c["visits_7_days"]) == (2, 1, 2, 3, 2)
    snap = dashboard.snapshot(conn, public=True)
    assert snap["visits"]["ok"] and snap["visits"]["data"]["visits"] == 3
    page = dashboard.render(snap)
    assert "Visitors" in page and "2 browsers" in page and "3 visits" in page
    assert visits.REFRESH_HEADER in page
    # Without scripts the page reloads to ?refresh=1, which the counter skips.
    assert '<meta http-equiv="refresh" content="30; url=?refresh=1">' in page


def test_a_browser_that_only_signed_in_is_not_a_visitor(conn):
    """Otherwise the tile could say "this week: 1 visit from 2 browsers"."""
    from vending import dashboard

    signed_only = "fedcba9876543210"
    visits.write(conn, [("sign_in", "/login", BROWSER), ("view", "/dashboard", BROWSER),
                        ("sign_in", "/login", signed_only)])
    c = visits.counts(conn)
    assert (c["browsers"], c["browsers_7_days"], c["sign_ins"], c["visits"], c["visits_7_days"]) == (1, 1, 2, 1, 1)
    page = dashboard.render(dashboard.snapshot(conn, public=True))
    assert "this week: 1 visit from 1 browser<" in page


def test_the_counts_read_running_totals_and_only_the_last_week_of_rows(conn):
    """The read doesn't grow with history. Rows older than a week that the writer never added to
    the totals (here put straight into the table) don't show up, so nothing old was scanned."""
    visits.write(conn, [("sign_in", "/login", BROWSER), ("view", "/dashboard", BROWSER)])
    conn.execute("""insert into page_visits (at, kind, page, browser)
                    select now() - interval '30 days', 'view', '/dashboard', 'fedcba9876543210'
                      from generate_series(1, 50)""")
    c = visits.counts(conn)
    assert (c["browsers"], c["browsers_7_days"], c["sign_ins"], c["visits"], c["visits_7_days"]) == (1, 1, 1, 1, 1)
    # The week's part reads through the index on `at`; there is no other way to read all of it.
    with conn.transaction():
        conn.execute("set local enable_seqscan = off")
        from datetime import datetime, timedelta, timezone
        week = datetime.now(timezone.utc) - timedelta(days=7)
        plan = "\n".join(r[0] for r in conn.execute(
            "explain select count(distinct browser), count(*) from page_visits where at > %s and kind = 'view'",
            (week,)))
    assert "page_visits_at" in plan and "Seq Scan on page_visits" not in plan


def test_the_totals_add_up_batch_after_batch_and_count_a_browser_once(conn):
    visits.write(conn, [("view", "/dashboard", BROWSER)])
    visits.write(conn, [("view", "/db", BROWSER), ("sign_in", "/login", BROWSER), ("view", "/dashboard", BROWSER)])
    since = conn.execute("select since from page_visit_totals").fetchone()[0]
    visits.write(conn, [("view", "/dashboard", "fedcba9876543210")])
    c = visits.counts(conn)
    assert (c["browsers"], c["sign_ins"], c["visits"]) == (2, 1, 4) and c["since"] == since
    assert conn.execute("select count(*) from page_visit_totals").fetchone()[0] == 1


def test_the_migration_counts_what_was_stored_before_it(conn):
    """Rows written before the totals existed are in the totals once migration 018 runs."""
    from vending import db

    other = "fedcba9876543210"
    conn.execute("""insert into page_visits (at, kind, page, browser) values
                    (now() - interval '40 days', 'sign_in', '/login', %(a)s),
                    (now() - interval '40 days', 'view', '/dashboard', %(a)s),
                    (now() - interval '2 days', 'view', '/db', %(a)s),
                    (now() - interval '1 day', 'sign_in', '/login', %(b)s)""", {"a": BROWSER, "b": other})
    conn.execute("drop table page_visit_totals, page_visit_browsers")
    conn.execute("delete from schema_migrations where version = '018_page_visit_totals.sql'")
    assert db.migrate(conn) == ["018_page_visit_totals.sql"]
    c = visits.counts(conn)
    assert (c["browsers"], c["browsers_7_days"], c["sign_ins"], c["visits"], c["visits_7_days"]) == (1, 1, 2, 2, 1)
    assert c["since"] == conn.execute("select min(at) from page_visits").fetchone()[0]


def _lock_visits(schema_url):
    """Another session holding `page_visits` locked, as a long migration or a stuck write would."""
    from tests.conftest import open_in

    other = open_in(*schema_url)
    other.execute("begin")
    other.execute("lock table page_visits in access exclusive mode")
    return other


def test_a_locked_visits_table_blanks_only_its_tile(schema_url, conn):
    """With the page reader's 5-second statement timeout, a locked table used to fail the whole
    snapshot (and so the page). Now the tile gives up within about a second and says so."""
    from vending import dashboard, ledger

    ledger.record(conn, "opening", 60000, "opening capital", "test")
    visits.write(conn, [("view", "/dashboard", BROWSER)])
    conn.execute("set statement_timeout = 5000")
    other = _lock_visits(schema_url)
    try:
        started = time.monotonic()
        snap = dashboard.snapshot(conn, public=True)
        took = time.monotonic() - started
        page = dashboard.render(snap)
    finally:
        other.close()
    assert took < 3, took
    assert not snap["visits"]["ok"] and snap["spend"]["ok"] and snap["runs"]["ok"]
    assert "Visitors" in page and "Unavailable" in page
    # The rest of the snapshot keeps its own limit, not the tile's short one.
    assert conn.execute("show statement_timeout").fetchone()[0] == "5s"


def test_the_optional_panels_short_limits_end_with_the_panel(conn):
    from vending import dashboard

    conn.execute("set statement_timeout = 5000")
    with conn.transaction():
        dashboard._isolated(conn, lambda: conn.execute("select 1").fetchone())
        assert conn.execute("show statement_timeout").fetchone()[0] == "5s"
        assert conn.execute("show lock_timeout").fetchone()[0] == "0"
        with pytest.raises(dashboard.Unavailable):
            dashboard._isolated(conn, lambda: conn.execute("select pg_sleep(3)"))
        assert conn.execute("show statement_timeout").fetchone()[0] == "5s"


def test_the_public_dashboard_still_serves_while_the_visits_table_is_locked(schema_url, conn, monkeypatch, events):
    """Through the worker, on the pages' read-only login: 200 with the tile unavailable, not 503."""
    import urllib.request

    from tests.test_pages import _as, _drop, _role

    url, schema = schema_url
    with psycopg.connect(url, autocommit=True, connect_timeout=5) as admin:
        name = _role(admin)
        try:
            pages.grant(conn, name, "test")
            monkeypatch.setattr(pages, "_read_only_until", 0.0)
            monkeypatch.setenv(pages.READER, "set")
            monkeypatch.setattr(pages, "connect", lambda: _as(url, schema, name))
            server = worker.serve_health(worker.State(), 0)
            other = _lock_visits(schema_url)
            try:
                started = time.monotonic()
                with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/dashboard",
                                            timeout=20) as reply:
                    status, body = reply.status, reply.read().decode()
                took = time.monotonic() - started
            finally:
                other.close()
                server.shutdown()
                server.server_close()
        finally:
            _drop(admin, name)
    assert status == 200 and "Unavailable" in body and took < 4, took


def test_the_table_refuses_what_the_counter_never_writes(conn):
    import psycopg

    for row in (("view", "/login", BROWSER), ("sign_in", "/dashboard", BROWSER),
                ("view", "/elsewhere", BROWSER), ("view", "/dashboard", "1.2.3.4")):
        with pytest.raises(psycopg.errors.CheckViolation):
            visits.write(conn, [row])


def test_a_dashboard_with_no_visits_says_none_yet(conn):
    from vending import dashboard

    page = dashboard.render(dashboard.snapshot(conn, public=True))
    assert "Visitors" in page and "None yet" in page


def test_the_flush_writes_through_its_own_connection(schema_url, monkeypatch):
    from tests.conftest import open_in

    monkeypatch.setattr(visits, "connect", lambda: open_in(*schema_url))
    assert visits.flush([("view", "/dashboard", BROWSER)]) is True
    with open_in(*schema_url) as conn:
        assert visits.counts(conn)["visits"] == 1
