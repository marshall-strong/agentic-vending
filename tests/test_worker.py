"""The worker's rounds and health check, with the jobs faked. No database."""

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
    code = worker.main(["--every", "5", "--port", "0"], jobs=[("a", lambda: rounds.append(1) or 0)],
                       sleep=sleep, log=lambda _: None)
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
    results = worker.run_round(worker.default_jobs(), log=lambda _: None)
    assert [r.job for r in results] == ["sync_history", "sync_requests", "sync_catalog"]
    assert migrated == []
    assert [(r.outcome, r.exit_code) for r in results] == [("finished", 2)] * 3
