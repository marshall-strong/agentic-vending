"""The worker's rounds and health check, with the jobs faked. No database."""

import json
import urllib.request

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
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/other")
        assert missing.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_every_must_be_positive():
    with pytest.raises(SystemExit):
        worker.main(["--every", "0", "--port", "0"], jobs=[], log=lambda _: None)
