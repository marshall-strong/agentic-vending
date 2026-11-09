"""The long-running process in the Container App: the scheduled syncs, and a health check.

    python -m vending.worker            # every SYNC_EVERY_MINUTES (60), forever
    python -m vending.worker --once     # one round, then exit with the worst exit code

Each round runs, in order, `python -m vending.sync_history` and
`python -m vending.sync_requests`, in this process. Neither migrates: on a
database that is behind each refuses (exit 2), the round is logged as failed,
and the worker tries again next round, so a deploy can never change the shared
database's schema. A person runs `python -m vending.db migrate`.

`GET /health` on `PORT` (80) answers with JSON: the last round's outcome per
job, when it ran, and when the next one is due. The Container App's ingress
points at that port, so the team can check it from a browser, and a 503 there
means the last round failed.

The agent itself (the model loop) does not run here yet; when it does, it
starts `python -m vending.server` over stdio from the same image.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

Job = tuple[str, Callable[[], int]]


def default_jobs() -> list[Job]:
    from vending import sync_history, sync_requests

    return [("sync_history", lambda: sync_history.main([])),
            ("sync_requests", lambda: sync_requests.main([]))]


@dataclass
class JobResult:
    job: str
    started_at: str
    seconds: float
    exit_code: int | None       # None when the job raised
    error: str | None = None


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


def run_round(jobs: list[Job], log: Callable[[str], None] = print) -> list[JobResult]:
    """Run every job once, in order, even if one fails. Never raises."""
    results = []
    for name, run in jobs:
        started = datetime.now(timezone.utc)
        log(f"{started:%Y-%m-%d %H:%M:%S} {name}: starting")
        code: int | None = None
        error = None
        try:
            code = int(run())
        except Exception as problem:  # noqa: BLE001 - one job's crash must not stop the others
            error = f"{type(problem).__name__}: {problem}"
        seconds = (datetime.now(timezone.utc) - started).total_seconds()
        outcome = f"exit {code}" if error is None else f"crashed: {error}"
        log(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} {name}: {outcome} after {seconds:.1f} s")
        results.append(JobResult(name, started.isoformat(), round(seconds, 1), code, error))
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
        body = json.dumps({"status": "ok" if self.state.healthy else "failing",
                           **asdict(self.state)}, indent=2).encode()
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


def main(argv: list[str] | None = None, jobs: list[Job] | None = None,
         sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.worker", description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="one round, then exit with the worst exit code")
    parser.add_argument("--every", type=float, default=float(os.environ.get("SYNC_EVERY_MINUTES") or 60),
                        help="minutes between rounds (default SYNC_EVERY_MINUTES or 60)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 80),
                        help="port for /health (default PORT or 80); 0 serves nothing")
    args = parser.parse_args(argv)
    if args.every <= 0:
        parser.error("--every must be above zero")
    jobs = default_jobs() if jobs is None else jobs
    state = State()
    server = serve_health(state, args.port) if args.port and not args.once else None
    try:
        while True:
            state.results = run_round(jobs, log)
            state.rounds += 1
            state.last_round_at = datetime.now(timezone.utc).isoformat()
            if args.once:
                return max((r.exit_code if r.exit_code is not None else 1) for r in state.results)
            due = datetime.now(timezone.utc) + timedelta(minutes=args.every)
            state.next_round_at = due.isoformat()
            log(f"next round at {due:%Y-%m-%d %H:%M:%S} UTC")
            sleep(args.every * 60)
    except KeyboardInterrupt:
        return 0
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    sys.exit(main())
