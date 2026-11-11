"""The long-running process in the Container App: the scheduled syncs, a health check and a home page.

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
browser, and a 503 there means the last round failed. `GET /` shows the same
facts as a page for people: whether the last round passed, each job's outcome,
when the next round runs, and links to the docs. It reloads itself every
minute. Neither ever shows error text, because both are public and a database
error names the host and user; the errors are in the log. The page is built
only from `State.public()`, the same dict as `/health`, so it can't show more.

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
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

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


EASTERN = ZoneInfo("America/New_York")
REPO = "https://github.com/marshall-strong/agentic-vending"
OUTCOMES = ("finished", "crashed", "timed out", "still running")
JOB_NAMES = {"sync_history": "Machine readings (sales history)", "sync_requests": "Purchase requests and receipts",
             "sync_catalog": "Product catalog"}


def _eastern(moment: str | None) -> str:
    """'Tue 11 Nov, 4:05 PM ET' from an ISO time; 'not yet' for none."""
    if not moment:
        return "not yet"
    try:
        local = datetime.fromisoformat(moment).astimezone(EASTERN)
    except (TypeError, ValueError):
        return "unknown"
    return f"{local:%a %d %b}, {local.hour % 12 or 12}:{local:%M %p} ET"


def home_page(public: dict) -> str:
    """The page at /, built only from what /health shows, so no error text can reach it."""
    results = public.get("results") or []
    if not public.get("rounds"):
        verdict, tone = "Starting up: the first sync round hasn't finished yet.", "wait"
    elif public.get("status") == "ok":
        verdict, tone = "The last sync round passed.", "ok"
    else:
        failed = sum(r.get("exit_code") != 0 for r in results)
        verdict, tone = f"The last sync round had {failed} failed job{'' if failed == 1 else 's'}.", "bad"
    rows = []
    for r in results:
        ok = r.get("exit_code") == 0
        outcome = r.get("outcome") if r.get("outcome") in OUTCOMES else "unknown"
        if ok:
            detail = "passed"
        elif r.get("exit_code") is not None:
            detail = f"failed, exit code {int(r['exit_code'])}"
        else:
            detail = outcome
        job = str(r.get("job"))
        rows.append(
            "<tr>"
            f"<td>{escape(JOB_NAMES.get(job, job))}<br><code>{escape(job)}</code></td>"
            f"<td><span class=\"pill {'ok' if ok else 'bad'}\">{escape(detail)}</span></td>"
            f"<td>{escape(_eastern(r.get('started_at')))}</td>"
            f"<td class=\"num\">{float(r.get('seconds') or 0):.1f} s</td>"
            "</tr>")
    table = ("<table><thead><tr><th>Job</th><th>Outcome</th><th>Started</th><th class=\"num\">Took</th></tr></thead>"
             f"<tbody>{''.join(rows)}</tbody></table>") if rows else "<p class=\"muted\">No jobs have run yet.</p>"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>Agentic Vending</title>
<style>
  :root {{ --bg: #f5f6f8; --fg: #1b1f24; --muted: #5a6370; --line: #dce0e5; --panel: #ffffff;
          --ok: #1d7a46; --bad: #b42318; --wait: #8a6100; color-scheme: light; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #111418; --fg: #e8eaed; --muted: #9aa3ae; --line: #2a3038; --panel: #181c21;
            --ok: #4cc38a; --bad: #f97066; --wait: #e9b949; color-scheme: dark; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--bg); color: var(--fg);
         font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }}
  main {{ max-width: 46rem; margin: 0 auto; display: grid; gap: 1.5rem; padding-block: 2rem 3rem;
         padding-left: max(16px, env(safe-area-inset-left)); padding-right: max(16px, env(safe-area-inset-right)); }}
  h1 {{ margin: 0; font-size: 1.9rem; line-height: 1.2; }}
  h2 {{ margin: 0 0 .5rem; font-size: 1.05rem; }}
  p {{ margin: 0; }}
  .muted {{ color: var(--muted); }}
  .verdict {{ font-size: 1.15rem; font-weight: 600; padding: .9rem 1rem; border-radius: 10px;
             border: 1px solid var(--line); background: var(--panel); border-left: 6px solid var(--{tone}); }}
  .facts {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr)); gap: .75rem; }}
  .fact {{ background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: .75rem 1rem; }}
  .fact b {{ display: block; font-size: .78rem; color: var(--muted); font-weight: 500; text-transform: uppercase; letter-spacing: .05em; }}
  .wrap {{ overflow-x: auto; border: 1px solid var(--line); border-radius: 10px; background: var(--panel); }}
  table {{ border-collapse: collapse; width: 100%; min-width: 30rem; font-size: .95rem; }}
  th, td {{ text-align: left; padding: .6rem .8rem; border-bottom: 1px solid var(--line); vertical-align: top; }}
  tr:last-child td {{ border-bottom: 0; }}
  th {{ color: var(--muted); font-weight: 500; font-size: .85rem; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
  code {{ font-size: .8rem; color: var(--muted); }}
  .pill {{ display: inline-block; padding: .05rem .55rem; border-radius: 999px; border: 1px solid currentColor; font-size: .85rem; white-space: nowrap; }}
  .pill.ok {{ color: var(--ok); }}
  .pill.bad {{ color: var(--bad); }}
  ul {{ margin: 0; padding-left: 1.2rem; display: grid; gap: .3rem; }}
  a {{ color: inherit; }}
  a:focus-visible {{ outline: 2px solid var(--fg); outline-offset: 2px; }}
  footer {{ font-size: .85rem; }}
</style>
</head>
<body>
<main>
  <header>
    <h1>Agentic Vending</h1>
    <p class="muted">Group B, Applied AI, Fall 2026. An AI agent that runs 20 slots of the drink machine as a business.</p>
  </header>
  <p class="verdict">{escape(verdict)}</p>
  <section class="facts" aria-label="Sync rounds">
    <div class="fact"><b>Last round</b>{escape(_eastern(public.get("last_round_at")))}</div>
    <div class="fact"><b>Next round</b>{escape(_eastern(public.get("next_round_at")))}</div>
    <div class="fact"><b>Rounds since start</b>{int(public.get("rounds") or 0)}</div>
  </section>
  <section>
    <h2>Each job in the last round</h2>
    <div class="wrap">{table}</div>
    <p class="muted">Each round copies the machine's readings, our purchase requests and the product catalog into our database. It reads the bridge's stored history and never polls the machine.</p>
  </section>
  <section>
    <h2>Learn more</h2>
    <ul>
      <li><a href="{REPO}#readme">What we are building</a> (the README on GitHub)</li>
      <li><a href="{REPO}/blob/main/docs/deploy.md">How this container runs</a></li>
      <li><a href="/health">The same status as JSON</a> (<code>/health</code>)</li>
      <li class="muted">The database explorer and the agent dashboard will be linked here once this address serves them (issue #53).</li>
    </ul>
  </section>
  <footer class="muted">Running since {escape(_eastern(public.get("started_at")))}. This page reloads every minute. Error details stay in the container's log and are never shown here.</footer>
</main>
</body>
</html>
"""


class _Health(BaseHTTPRequestHandler):
    state: State

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == "/health":
            body = json.dumps(self.state.public(), indent=2).encode()
            self._send(200 if self.state.healthy else 503, "application/json", body)
        elif path == "":
            self._send(200, "text/html; charset=utf-8", home_page(self.state.public()).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # The page has no scripts and loads nothing from anywhere else.
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


def serve_health(state: State, port: int) -> ThreadingHTTPServer:
    """Answer / and /health in a daemon thread. Returns the server, for tests to shut down."""
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
                        help="port for / and /health (default PORT or 8080); 0 serves nothing")
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
