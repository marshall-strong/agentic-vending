"""The worker's rounds and health check, with the jobs faked. No database."""

import html
import json
import signal
import socket
import threading
import urllib.request
from contextlib import nullcontext

import pytest

from vending import worker


def test_a_round_runs_every_job_even_when_one_crashes():
    seen = []
    jobs = [
        ("first", lambda: seen.append("first") or 0),
        ("broken", lambda: (_ for _ in ()).throw(RuntimeError("boom"))),
        ("last", lambda: seen.append("last") or 2),
    ]
    results = worker.run_round(jobs, log=lambda _: None)
    assert seen == ["first", "last"]
    assert [(r.job, r.exit_code) for r in results] == [("first", 0), ("broken", None), ("last", 2)]
    assert results[1].error == "RuntimeError: boom"


def test_once_exits_with_the_worst_code():
    jobs = [("a", lambda: 0), ("b", lambda: 2), ("c", lambda: 1)]
    assert worker.main(["--once", "--port", "0"], jobs=jobs, log=lambda _: None) == 2
    assert worker.main(["--once", "--port", "0"], jobs=[("a", lambda: 0)], log=lambda _: None) == 0


def test_a_crash_counts_as_a_failure_for_the_exit_code():
    def crash():
        raise ValueError("no")
    assert worker.main(["--once", "--port", "0"], jobs=[("a", crash)], log=lambda _: None) == 1


def test_the_loop_sleeps_between_rounds_and_stops_on_interrupt():
    rounds = []
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise KeyboardInterrupt
    code = worker.main(["--every", "5", "--at-minute", "off", "--port", "0"],
                       jobs=[("a", lambda: rounds.append(1) or 0)], sleep=sleep, log=lambda _: None)
    assert code == 0 and len(rounds) == 2 and sleeps == [300.0, 300.0]


def test_sigterm_stops_the_loop_and_the_handler_is_put_back():
    before = signal.getsignal(signal.SIGTERM)
    rounds = []

    def sleep(seconds):
        # Without the worker's handler a real SIGTERM would end the test run.
        if signal.getsignal(signal.SIGTERM) is before:
            pytest.fail("no SIGTERM handler while the loop sleeps")
        signal.raise_signal(signal.SIGTERM)
    code = worker.main(["--every", "5", "--port", "0"], jobs=[("a", lambda: rounds.append(1) or 0)],
                       sleep=sleep, log=lambda _: None)
    assert code == 0 and rounds == [1]
    assert signal.getsignal(signal.SIGTERM) is before


def test_health_reports_the_last_round():
    state = worker.State()
    server = worker.serve_health(state, 0)
    port = server.server_address[1]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health") as response:
            fresh = json.loads(response.read())
        assert fresh["status"] == "ok" and fresh["rounds"] == 0
        state.results = worker.run_round([("a", lambda: 3)], log=lambda _: None)
        state.rounds = 1
        with pytest.raises(urllib.error.HTTPError) as failing:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health")
        assert failing.value.code == 503
        body = json.loads(failing.value.read())
        assert body["status"] == "failing" and body["results"][0]["exit_code"] == 3
        assert body["results"][0]["outcome"] == "finished"
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/other")
        assert missing.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_health_drops_a_client_that_sends_nothing(monkeypatch):
    # /health is public, so a connection that never sends a request mustn't hold a thread for ever.
    assert worker._Health.timeout == 10
    monkeypatch.setattr(worker._Health, "timeout", 0.2)
    server = worker.serve_health(worker.State(), 0)
    try:
        with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5) as idle:
            assert idle.recv(1) == b""  # closed by the server, long before our own 5 s timeout
    finally:
        server.shutdown()
        server.server_close()


def test_every_must_be_positive():
    with pytest.raises(SystemExit):
        worker.main(["--every", "0", "--port", "0"], jobs=[], log=lambda _: None)


def test_health_never_shows_error_text():
    # A psycopg connection error names the database host and user, and /health is public.
    def unreachable():
        raise ConnectionError('connection to server at "db.example.net", port 5432 failed: '
                              'password authentication failed for user "vending"')
    state = worker.State()
    state.results = worker.run_round([("sync_history", unreachable)], log=lambda _: None)
    state.rounds = 1
    assert "db.example.net" in state.results[0].error
    server = worker.serve_health(state, 0)
    try:
        with pytest.raises(urllib.error.HTTPError) as failing:
            urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/health")
        text = failing.value.read().decode()
    finally:
        server.shutdown()
        server.server_close()
    assert "db.example.net" not in text and "vending" not in text and "error" not in text
    assert json.loads(text)["results"][0] == {
        "job": "sync_history", "started_at": state.results[0].started_at,
        "seconds": state.results[0].seconds, "exit_code": None, "outcome": "crashed"}


def test_a_job_that_overruns_is_timed_out_and_skipped_until_it_ends():
    release = threading.Event()
    calls = []

    def hangs():
        calls.append(1)
        release.wait(5)
        return 0
    jobs = [("hangs", hangs), ("next", lambda: 0)]
    running = {}
    first = worker.run_round(jobs, log=lambda _: None, timeout=0.05, running=running)
    assert [(r.outcome, r.exit_code) for r in first] == [("timed out", None), ("finished", 0)]
    second = worker.run_round(jobs, log=lambda _: None, timeout=0.05, running=running)
    assert [r.outcome for r in second] == ["still running", "finished"] and len(calls) == 1
    release.set()
    running["hangs"].join(5)
    third = worker.run_round(jobs, log=lambda _: None, timeout=5, running=running)
    assert [(r.outcome, r.exit_code) for r in third] == [("finished", 0), ("finished", 0)]
    assert len(calls) == 2


def test_tracking_daily_skips_when_its_sync_failed_this_round():
    """The daily check reads the polls and receipts the round's syncs bring in: a sync that failed
    would leave it reading stale books and spending the day's check, so it skips (worker.NEEDS)."""
    calls = []
    jobs = [("sync_history", lambda: 2), ("tracking_daily", lambda: calls.append(1) or 0)]
    results = worker.run_round(jobs, log=lambda _: None, needs=worker.NEEDS)
    assert calls == []
    assert [(r.outcome, r.exit_code) for r in results] == [("finished", 2), ("skipped", None)]
    assert "sync_history" in results[1].error


def test_tracking_daily_runs_when_its_syncs_passed():
    calls = []
    jobs = [("sync_history", lambda: 0), ("sync_requests", lambda: 0),
            ("tracking_daily", lambda: calls.append(1) or 0)]
    results = worker.run_round(jobs, log=lambda _: None, needs=worker.NEEDS)
    assert calls == [1] and [(r.outcome, r.exit_code) for r in results] == [("finished", 0)] * 3


def test_tracking_daily_skips_when_its_sync_is_still_running():
    release = threading.Event()
    calls = []

    def hangs():
        release.wait(5)
        return 0
    jobs = [("sync_history", hangs), ("tracking_daily", lambda: calls.append(1) or 0)]
    running = {}
    worker.run_round(jobs[:1], log=lambda _: None, timeout=0.05, running=running)
    second = worker.run_round(jobs, log=lambda _: None, timeout=5, running=running, needs=worker.NEEDS)
    assert calls == []
    assert [(r.outcome, r.exit_code) for r in second] == [("still running", None), ("skipped", None)]
    release.set()
    running["sync_history"].join(5)


def test_the_gated_job_is_skipped_through_main_too():
    calls = []
    jobs = [("sync_history", lambda: 1), ("tracking_daily", lambda: calls.append(1) or 0)]
    assert worker.main(["--once", "--port", "0"], jobs=jobs, log=lambda _: None) == 1
    assert calls == []


def test_timeout_must_be_positive():
    with pytest.raises(SystemExit):
        worker.main(["--timeout", "0", "--port", "0"], jobs=[], log=lambda _: None)


def test_the_default_jobs_never_migrate(monkeypatch):
    """On a database that is behind, every scheduled sync refuses (exit 2) and none migrates.

    In production the worker runs these against the shared Azure database at every
    start and every hour, so a sync that migrated would change its schema on deploy.
    """
    pytest.importorskip("psycopg")
    from vending import db

    migrated = []
    monkeypatch.setattr(db, "connect", lambda: nullcontext(object()))
    monkeypatch.setattr(db, "pending", lambda conn: ["999_not_yet_applied.sql"])
    monkeypatch.setattr(db, "migrate", lambda conn: migrated.append(conn) or [])
    monkeypatch.setenv("TRACKING_DAILY", "on")     # off, the daily check never opens the database
    results = worker.run_round(worker.default_jobs(), log=lambda _: None)
    assert [r.job for r in results] == ["slots_check", "sync_history", "sync_requests", "sync_catalog",
                                        "sync_product_requests", "tracking_daily"]
    assert migrated == []
    assert [(r.outcome, r.exit_code) for r in results] == [("finished", 2)] * 6


# --- The home page at / (#54) -----------------------------------------------------

def _get(server, path):
    """(status, content type, headers, text) for GET path, 4xx/5xx included."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}{path}") as response:
            return response.status, response.headers["Content-Type"], response.headers, response.read().decode()
    except urllib.error.HTTPError as failing:
        return failing.code, failing.headers["Content-Type"], failing.headers, failing.read().decode()


def test_home_page_shows_each_jobs_outcome():
    state = worker.State()
    state.results = worker.run_round([("sync_history", lambda: 0), ("sync_requests", lambda: 2),
                                      ("sync_catalog", lambda: 1 / 0)], log=lambda _: None)
    state.rounds, state.last_round_at = 1, "2026-11-11T21:05:00+00:00"
    state.next_round_at = "2026-11-11T22:05:00+00:00"
    server = worker.serve_health(state, 0)
    try:
        status, kind, headers, page = _get(server, "/")
    finally:
        server.shutdown()
        server.server_close()
    assert status == 200 and kind == "text/html; charset=utf-8"   # the page itself loaded, even when a job failed
    assert "The last sync round had 2 failed jobs." in page
    assert "passed" in page and "failed, exit code 2" in page and "crashed" in page
    assert "sync_history" in page and "sync_requests" in page and "sync_catalog" in page
    assert "Wed 11 Nov, 4:05 PM ET" in page and "Wed 11 Nov, 5:05 PM ET" in page   # last and next round, Eastern
    assert '<meta http-equiv="refresh" content="60">' in page and 'name="viewport"' in page
    assert headers["Cache-Control"] == "no-store" and "default-src 'none'" in headers["Content-Security-Policy"]
    assert "<script" not in page


def test_home_page_before_the_first_round_says_so():
    server = worker.serve_health(worker.State(), 0)
    try:
        status, _, _, page = _get(server, "/")
    finally:
        server.shutdown()
        server.server_close()
    assert status == 200 and "first sync round hasn't finished yet" in html.unescape(page) and "No jobs have run yet." in page


def test_home_page_never_shows_error_text():
    # The same case as /health: a psycopg error names the database host and user, and / is public.
    def unreachable():
        raise ConnectionError('connection to server at "db.example.net", port 5432 failed: '
                              'password authentication failed for user "vending"')
    state = worker.State()
    state.results = worker.run_round([("sync_history", unreachable)], log=lambda _: None)
    state.rounds = 1
    assert "db.example.net" in state.results[0].error
    server = worker.serve_health(state, 0)
    try:
        _, _, _, page = _get(server, "/")
    finally:
        server.shutdown()
        server.server_close()
    assert "db.example.net" not in page and "password authentication" not in page and "5432" not in page
    assert 'for user "vending"' not in page and "ConnectionError" not in page and "crashed" in page


def test_home_page_escapes_what_it_shows():
    page = worker.home_page({"status": "failing", "rounds": 1, "results": [
        {"job": "<script>alert(1)</script>", "started_at": None, "seconds": 0, "exit_code": 1, "outcome": "finished"}]})
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


def test_health_is_still_json_and_the_root_is_not():
    state = worker.State()
    server = worker.serve_health(state, 0)
    try:
        assert _get(server, "/health")[1] == "application/json"
        assert _get(server, "/health/")[1] == "application/json"
        assert _get(server, "/")[1].startswith("text/html") and _get(server, "/?from=qr")[1].startswith("text/html")
        assert _get(server, "/nope")[0] == 404 and _get(server, "/healthy")[0] == 404
    finally:
        server.shutdown()
        server.server_close()


def test_a_round_runs_a_minute_after_the_slot_lock():
    from datetime import datetime, timedelta, timezone

    lock = datetime(2026, 12, 3, 5, tzinfo=timezone.utc)
    now = lock - timedelta(minutes=20)
    assert worker.next_round(now, 60, lock) == lock + timedelta(minutes=1)
    assert worker.next_round(now, 10, lock) == now + timedelta(minutes=10)          # the hourly round comes first
    after = lock + timedelta(minutes=1)
    assert worker.next_round(after, 60, lock) == lock + timedelta(minutes=5)         # then 5 and 15 minutes after
    assert worker.next_round(lock + timedelta(minutes=3), 60, lock) == lock + timedelta(minutes=5)
    assert worker.next_round(lock + timedelta(minutes=5), 60, lock) == lock + timedelta(minutes=15)
    later = lock + timedelta(minutes=15)
    assert worker.next_round(later, 60, lock) == later + timedelta(hours=1)          # then hourly again
    assert worker.next_round(now, 60, None) == now + timedelta(hours=1)             # a malformed setting


def test_the_loop_sleeps_until_the_round_after_the_lock(monkeypatch):
    from datetime import datetime, timedelta, timezone

    lock = datetime.now(timezone.utc) + timedelta(minutes=10)
    monkeypatch.setattr(worker, "_slot_lock", lambda: lock)
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        raise KeyboardInterrupt
    worker.main(["--every", "60", "--at-minute", "off", "--port", "0"], jobs=[("a", lambda: 0)], sleep=sleep,
                log=lambda _: None)
    assert 600 < sleeps[0] < 680


def test_rounds_keep_to_a_few_minutes_after_the_staff_poll():
    # The staff's poller reads the machine on the hour, so a round at :05 brings each poll in within
    # about five minutes, whenever the worker happened to start.
    from datetime import datetime, timedelta, timezone

    started = datetime(2026, 11, 19, 18, 42, 58, tzinfo=timezone.utc)   # a restart at 18:42
    assert worker.next_round(started, 60, None, 5) == datetime(2026, 11, 19, 19, 5, tzinfo=timezone.utc)
    assert worker.next_round(started, 60, None) == started + timedelta(hours=1)        # off: as before
    early = datetime(2026, 11, 19, 19, 2, tzinfo=timezone.utc)
    assert worker.next_round(early, 60, None, 5) == datetime(2026, 11, 19, 19, 5, tzinfo=timezone.utc)
    on_time = datetime(2026, 11, 19, 19, 5, tzinfo=timezone.utc)                       # a round that took no time
    assert worker.next_round(on_time, 60, None, 5) == datetime(2026, 11, 19, 20, 5, tzinfo=timezone.utc)
    assert worker.next_round(started, 15, None, 5) == datetime(2026, 11, 19, 18, 50, tzinfo=timezone.utc)
    assert worker.next_round(started, 120, None, 5) == datetime(2026, 11, 19, 20, 5, tzinfo=timezone.utc)
    # Under an hour apart with the minute past the gap: every 15 at minute 20 keeps to :05, :20, :35, :50.
    assert worker.next_round(datetime(2026, 11, 19, 19, 2, tzinfo=timezone.utc), 15, None, 20) ==         datetime(2026, 11, 19, 19, 5, tzinfo=timezone.utc)
    assert worker.next_round(datetime(2026, 11, 19, 20, 1, tzinfo=timezone.utc), 15, None, 20) ==         datetime(2026, 11, 19, 20, 5, tzinfo=timezone.utc)
    late = datetime(2026, 11, 19, 23, 58, tzinfo=timezone.utc)                         # across midnight
    assert worker.next_round(late, 60, None, 5) == datetime(2026, 11, 20, 0, 5, tzinfo=timezone.utc)


def test_the_slot_lock_rounds_still_come_first_and_the_hour_comes_back():
    from datetime import datetime, timedelta, timezone

    lock = datetime(2026, 12, 3, 5, tzinfo=timezone.utc)                              # midnight Eastern
    assert worker.next_round(lock - timedelta(minutes=20), 60, lock, 5) == lock + timedelta(minutes=1)
    assert worker.next_round(lock + timedelta(minutes=1), 60, lock, 5) == lock + timedelta(minutes=5)
    assert worker.next_round(lock + timedelta(minutes=5), 60, lock, 5) == lock + timedelta(minutes=15)
    assert worker.next_round(lock + timedelta(minutes=15), 60, lock, 5) == lock + timedelta(hours=1, minutes=5)


def test_the_loop_sleeps_until_the_minute_past_the_hour(monkeypatch):
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(worker, "_slot_lock", lambda: None)
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        raise KeyboardInterrupt
    before = datetime.now(timezone.utc)
    worker.main(["--every", "60", "--at-minute", "5", "--port", "0"], jobs=[("a", lambda: 0)], sleep=sleep,
                log=lambda _: None)
    due = before + timedelta(seconds=sleeps[0])
    assert 0 < sleeps[0] <= 3600 and due.minute in (4, 5)         # 4: the clock moved on while the round ran


@pytest.mark.parametrize("args", [["--at-minute", "60"], ["--at-minute", "-1"], ["--at-minute", "soon"],
                                  ["--every", "7"], ["--every", "90"]])
def test_a_minute_past_the_hour_needs_rounds_that_fit_the_hour(args):
    with pytest.raises(SystemExit):
        worker.main([*args, "--port", "0"], jobs=[], log=lambda _: None)


def test_off_keeps_any_every():
    assert worker.main(["--every", "7", "--at-minute", "off", "--once", "--port", "0"],
                       jobs=[("a", lambda: 0)], log=lambda _: None) == 0


# --- The chart pages behind the password ------------------------------------------------------------

CHARTS = ("/dashboard/money", "/dashboard/pnl", "/dashboard/spend")


def _ask(server, method: str, path: str, headers: dict | None = None):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=20)
    try:
        # A POST with no body: the server refuses it without reading one, and on Windows a body it never
        # read can make the client's send fail with WinError 10053 before the 405 arrives.
        conn.request(method, path, body=b"" if method == "POST" else None, headers=headers or {})
        reply = conn.getresponse()
        return reply.status, dict(reply.getheaders()), reply.read().decode()
    finally:
        conn.close()


@pytest.fixture
def gated(monkeypatch):
    """The worker's server with the password gate on, and every chart loader failing the test if reached."""
    from vending import gate, pages

    monkeypatch.setattr(gate, "ENABLED", True)
    monkeypatch.setenv(gate.KEY_SETTING, "postgresql://reader:secret@example/db")
    monkeypatch.setenv(pages.READER, "set")
    monkeypatch.setattr(pages, "_cache", {})
    for name in ("money_data", "pnl_data", "spend_data"):
        monkeypatch.setattr(pages, name, lambda *args, _name=name: pytest.fail(f"{_name} reached without a sign-in"))
    monkeypatch.setattr(pages, "connect", lambda: pytest.fail("a connection opened without a sign-in"))
    server = worker.serve_health(worker.State(), 0)
    yield server
    server.shutdown()
    server.server_close()


def test_the_chart_pages_send_a_signed_out_browser_to_the_password_page(gated):
    from vending import gate

    for path in CHARTS + tuple(p + "/" for p in CHARTS) + ("/dashboard/pnl?start=2026-12-05",):
        status, headers, body = _ask(gated, "GET", path)
        assert status == 303 and headers["Location"] == gate.login_location(path) and body == "", path
    # A forged cookie is no better.
    status, _, _ = _ask(gated, "GET", CHARTS[0], {"Cookie": f"{gate.COOKIE}=9999999999.forged"})
    assert status == 303
    # The dashboard's JSON twin still answers a signed-out refresh with JSON, as before.
    status, headers, body = _ask(gated, "GET", "/dashboard/state.json")
    assert status == 401 and headers["Content-Type"] == "application/json" and "signed out" in json.loads(body)["error"]


def test_the_chart_pages_take_no_post_signed_in_or_not(gated):
    from vending import gate

    for path in CHARTS:
        for headers in ({}, {"Cookie": f"{gate.COOKIE}={gate.token()}"}):
            status, _, _ = _ask(gated, "POST", path, headers)
            assert status == 405, path


def test_a_signed_in_browser_gets_each_chart_page_with_its_policy(gated, monkeypatch):
    from vending import gate, pages

    seen = []

    def respond(target, syncs=None):
        seen.append(target)
        return pages.Reply(200, pages.HTML, f"<p>{target}</p>", pages.CSP_PNL)
    monkeypatch.setattr(pages, "respond", respond)
    for path in CHARTS:
        status, headers, body = _ask(gated, "GET", path, {"Cookie": f"{gate.COOKIE}={gate.token()}"})
        assert status == 200 and body == f"<p>{path}</p>" and headers["Content-Security-Policy"] == pages.CSP_PNL
        assert headers["Cache-Control"] == "no-store"
    assert seen == list(CHARTS)


def test_the_chart_demos_are_behind_the_password_too(gated, monkeypatch):
    from vending import gate, pages

    monkeypatch.setattr(pages, "demo_page", lambda path, now=None: pytest.fail("a demo rendered without a sign-in"))
    for path in ("/dashboard/demo/money", "/dashboard/demo/spend"):
        status, headers, _ = _ask(gated, "GET", path)
        assert status == 303 and headers["Location"] == gate.login_location(path)
        assert _ask(gated, "POST", path)[0] == 405
