"""Counting sign-ins and visits to the container's pages (`vending.visits`, served by `vending.worker`)."""

from __future__ import annotations

import queue
from urllib.parse import urlencode

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

    monkeypatch.setattr(visits, "connect", broken)
    monkeypatch.setattr(visits, "_logged", {})
    assert visits.flush([("view", "/dashboard", BROWSER)]) is False
    assert "visits: dropped 1" in capsys.readouterr().err


# --- Through the worker, behind the password ---------------------------------------------------

@pytest.fixture
def site(monkeypatch, events):
    monkeypatch.setattr(gate, "ENABLED", True)
    monkeypatch.setattr(gate, "PASSWORD_HASH", gate.make_hash(PASSWORD, n=2**10))
    monkeypatch.setattr(gate, "WRONG_DELAY_SECONDS", 0)
    monkeypatch.setattr(gate, "_next_try", {})
    monkeypatch.setenv(gate.KEY_SETTING, "postgresql://reader:secret@example/db")
    monkeypatch.setattr(pages, "respond", lambda target: pages.Reply(200, pages.HTML, "<main>page</main>"))
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
    monkeypatch.setattr(pages, "respond", lambda target: pages.Reply(503, pages.HTML, "busy"))
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
