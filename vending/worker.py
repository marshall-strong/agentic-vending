"""The long-running process in the Container App: the scheduled syncs, and a health check.

    python -m vending.worker            # every SYNC_EVERY_MINUTES (60), forever
    python -m vending.worker --once     # one round, then exit with the worst exit code

Each round runs, in order, `python -m vending.sync_history`,
`python -m vending.sync_requests` and `python -m vending.sync_catalog`, in this
process. None of them migrates: on a database that is behind each refuses
(exit 2), the round is logged as failed, and the worker tries again next round,
so a deploy can never change the shared database's schema. A person runs
`python -m vending.db migrate`.

Each job gets JOB_TIMEOUT_MINUTES (30). Python can't stop a thread, so a job
that overruns is reported as timed out and left to finish in the background,
and later rounds skip it (as still running) until it does. A hung database call
then shows on /health instead of silently stalling every later round.

`GET /health` on `PORT` (8080) answers with JSON: each job's outcome and exit
code in the last round, when it ran, and when the next one is due. The
Container App's ingress points at that port, so the team can check it from a
browser, and a 503 there means the last round failed. It never shows error
text, because the endpoint is public and a database error names the host and
user; the errors are in the log.

SIGTERM, which Azure sends before stopping a replica, ends the loop like
Ctrl+C: the health check closes and the process exits 0, and a job in flight
is cut off. The process is PID 1 in the container, which ignores SIGTERM
unless a handler is set, so without this a restart would wait out the grace
period and end in SIGKILL.

The agent itself (the model loop) does not run here yet; when it does, it
starts `python -m vending.server` over stdio from the same image.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

Job = tuple[str, Callable[[], int]]


def default_jobs() -> list[Job]:
    from vending import sync_catalog, sync_history, sync_requests

    return [("sync_history", lambda: sync_history.main([])),
            ("sync_requests", lambda: sync_requests.main([])),
            ("sync_catalog", lambda: sync_catalog.main([]))]


@dataclass
class JobResult:
    job: str
    started_at: str
    seconds: float
    exit_code: int | None           # None unless the job finished
    outcome: str = "finished"       # "finished", "crashed", "timed out" or "still running"
    error: str | None = None        # for the log only: /health never shows it


@dataclass
class State:
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    rounds: int = 0
    last_round_at: str | None = None
    next_round_at: str | None = None
    results: list[JobResult] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        """True until a round has run and something in the latest one failed."""
        return all(r.exit_code == 0 for r in self.results)

    def public(self) -> dict:
        """What /health shows: outcomes and exit codes, never error text."""
        return {"status": "ok" if self.healthy else "failing",
                "started_at": self.started_at, "rounds": self.rounds,
                "last_round_at": self.last_round_at, "next_round_at": self.next_round_at,
                "results": [{"job": r.job, "started_at": r.started_at, "seconds": r.seconds,
                             "exit_code": r.exit_code, "outcome": r.outcome} for r in self.results]}


def run_round(jobs: list[Job], log: Callable[[str], None] = print, timeout: float | None = None,
              running: dict[str, threading.Thread] | None = None) -> list[JobResult]:
    """Run every job once, in order, even if one fails. Never raises.

    `timeout` is in seconds (None waits for ever). A job that overruns keeps
    going in a daemon thread and is recorded in `running`; pass the same dict
    each round so it is skipped until that thread ends.
    """
    running = {} if running is None else running
    results = []
    for name, run in jobs:
        started = datetime.now(timezone.utc)
        earlier = running.get(name)
        if earlier is not None and earlier.is_alive():
            log(f"{started:%Y-%m-%d %H:%M:%S} {name}: skipped, still running from an earlier round")
            results.append(JobResult(name, started.isoformat(), 0.0, None, "still running"))
            continue
        running.pop(name, None)
        log(f"{started:%Y-%m-%d %H:%M:%S} {name}: starting")
        done: dict = {}

        def target(run=run, done=done):
            try:
                done["code"] = int(run())
            except BaseException as problem:  # noqa: BLE001 - one job's crash must not stop the others
                done["error"] = f"{type(problem).__name__}: {problem}"

        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        thread.join(timeout)
        seconds = round((datetime.now(timezone.utc) - started).total_seconds(), 1)
        if thread.is_alive():
            running[name] = thread
            result = JobResult(name, started.isoformat(), seconds, None, "timed out",
                               f"still running after {timeout:.0f} s; left to finish in the background")
        elif "error" in done:
            result = JobResult(name, started.isoformat(), seconds, None, "crashed", done["error"])
        else:
            result = JobResult(name, started.isoformat(), seconds, done["code"])
        detail = f"exit {result.exit_code}" if result.outcome == "finished" else f"{result.outcome}: {result.error}"
        log(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} {name}: {detail} after {seconds:.1f} s")
        results.append(result)
    return results


class _Health(BaseHTTPRequestHandler):
    state: State

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.rstrip("/") not in ("", "/health"):
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(self.state.public(), indent=2).encode()
        self.send_response(200 if self.state.healthy else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve_health(state: State, port: int) -> ThreadingHTTPServer:
    """Answer /health in a daemon thread. Returns the server, for tests to shut down."""
    handler = type("Health", (_Health,), {"state": state})
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class Stopped(Exception):
    """SIGTERM arrived; raised in the main thread to end the loop."""


def _stop(signum, frame):
    raise Stopped


def main(argv: list[str] | None = None, jobs: list[Job] | None = None,
         sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.worker", description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="one round, then exit with the worst exit code")
    parser.add_argument("--every", type=float, default=float(os.environ.get("SYNC_EVERY_MINUTES") or 60),
                        help="minutes between rounds (default SYNC_EVERY_MINUTES or 60)")
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("JOB_TIMEOUT_MINUTES") or 30),
                        help="minutes each job may run (default JOB_TIMEOUT_MINUTES or 30)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 8080),
                        help="port for /health (default PORT or 8080); 0 serves nothing")
    args = parser.parse_args(argv)
    if args.every <= 0:
        parser.error("--every must be above zero")
    if args.timeout <= 0:
        parser.error("--timeout must be above zero")
    jobs = default_jobs() if jobs is None else jobs
    state = State()
    running: dict[str, threading.Thread] = {}
    server = serve_health(state, args.port) if args.port and not args.once else None
    previous = None
    try:
        # Only the main thread may set a signal handler.
        if threading.current_thread() is threading.main_thread():
            previous = signal.signal(signal.SIGTERM, _stop)
        while True:
            state.results = run_round(jobs, log, args.timeout * 60, running)
            state.rounds += 1
            state.last_round_at = datetime.now(timezone.utc).isoformat()
            if args.once:
                return max((r.exit_code if r.exit_code is not None else 1) for r in state.results)
            due = datetime.now(timezone.utc) + timedelta(minutes=args.every)
            state.next_round_at = due.isoformat()
            log(f"next round at {due:%Y-%m-%d %H:%M:%S} UTC")
            sleep(args.every * 60)
    except (KeyboardInterrupt, Stopped):
        log("stopping")
        return 0
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
        if server is not None:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    sys.exit(main())
