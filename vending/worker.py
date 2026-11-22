"""The long-running process in the Container App: the scheduled syncs, a health check and the public pages.

    python -m vending.worker            # every SYNC_EVERY_MINUTES (60), forever, at SYNC_AT_MINUTE (5) past the hour
    python -m vending.worker --once     # one round, then exit with the worst exit code

Each round runs, in order, `python -m vending.slots check`,
`python -m vending.sync_history`, `python -m vending.sync_requests`,
`python -m vending.sync_catalog`, `python -m vending.sync_product_requests` and
`python -m vending.tracking_daily`, in this process. The last sends the day's tracking check once it is
due, only when `TRACKING_DAILY` is `on` (off by default; `vending.tracking_daily`), and only when
the round's `sync_history` and `sync_requests` both finished (`worker.NEEDS`): the check reads the
polls and receipts those syncs bring in, so a sync that failed, crashed or is still running would
leave it reading stale books and spending the day's one check. `slots check` goes first. It
only confirms our slot assignment: if the server gives a different one, or none
is stored, it changes nothing and fails the round until a person runs
`python -m vending.slots record` (`vending.slots`). The hourly rounds run at
SYNC_AT_MINUTE (5) minutes past the hour, UTC, because the course staff's poller
reads the machine on the hour: a new poll reaches the dashboard about five
minutes after it is taken, not up to an hour later. `off` runs a round every
SYNC_EVERY_MINUTES from the last, as before. Besides the hourly rounds,
rounds run 1, 5 and 15 minutes after the slot lock (`SLOTS_LOCK_AT`, the start),
so the check that confirms the assignment for the competition doesn't wait up to
an hour, with every reader refusing meanwhile. The schedule uses this host's
clock and the check the database's; the later rounds cover a host clock a few
minutes ahead, or a round still running at the first.
None of them migrates: on a database that is behind each refuses (exit 2), the
round is logged as failed, and the worker tries again next round,
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
error names the host and user; the errors are in the log (a database error as its class and SQLSTATE). The page is built
only from `State.public()`, the same dict as `/health`, so it can't show more.

`GET /dashboard` (with `/dashboard/state.json`) and `GET /db` are the agent
dashboard and the DB explorer, for anyone with the shared password. They are
`vending.pages`: a database login of their own that can only read, a few
connections at most, and no error text. Until that login is set
(`READONLY_DATABASE_URL`) they answer "not set up yet" and the home page says
so. The syncs never use that login, and the pages never use the syncs'.
Sign-ins and visits to them are counted by `vending.visits`, which writes in
the background with the syncs' login and never makes a page wait.

SIGTERM, which Azure sends before stopping a replica, ends the loop like
Ctrl+C: the health check closes and the process exits 0, and a job in flight
is cut off. The process is PID 1 in the container, which ignores SIGTERM
unless a handler is set, so without this a restart would wait out the grace
period and end in SIGKILL.

On Fridays (and the dates in `VISIT_POLL_DATES`) the loop also asks the bridge to read the machine live,
about every minute from 2:45 to 7 PM Eastern, so a read lands while the door is open during our visit
(`vending.visit_polls`). A read that fails is logged and skipped. It is not a job of the round, so it
never fails the round or /health. A read that runs past the round's time delays the round, never ends the loop.

The agent itself (the model loop) does not run here yet; when it does, it
starts `python -m vending.server` over stdio from the same image.

`GET /dashboard/demo/simulations` shows simulated seasons from committed files
(`vending.simulate.demo`), behind the same password. It never opens a database
connection, and every page of it says it is simulated.
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

from vending import gate, visits

Job = tuple[str, Callable[[], int]]

# A job here skips its round when a named earlier job of the same round didn't finish with 0
# (failed, crashed, timed out, or still running from an earlier round): the tracking check reads
# the polls and receipts its round's syncs bring in, so a sync that didn't finish would leave it
# reading stale books and spending the day's one check on them. It tries again next round.
NEEDS = {"tracking_daily": ("sync_history", "sync_requests")}


def default_jobs() -> list[Job]:
    from vending import slots, sync_catalog, sync_history, sync_product_requests, sync_requests, tracking_daily

    return [("slots_check", lambda: slots.main(["check"])),
            ("sync_history", lambda: sync_history.main([])),
            ("sync_requests", lambda: sync_requests.main([])),
            ("sync_catalog", lambda: sync_catalog.main([])),
            ("sync_product_requests", lambda: sync_product_requests.main([])),
            # Last, so the day's check reads the polls and receipts this round brought in; NEEDS
            # holds it back when one of those syncs didn't finish.
            ("tracking_daily", lambda: tracking_daily.main([]))]


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


def _describe(problem: BaseException) -> str:
    """What the log says of a job that crashed: the class and the message. A database error is its class and
    SQLSTATE only: its text can quote the row it refused, which holds a poll (docs/integrity.md)."""
    try:
        import psycopg      # not imported at the top: the worker's other code doesn't need the driver
    except ImportError:
        psycopg = None
    if psycopg is not None and isinstance(problem, psycopg.Error):
        return f"{type(problem).__name__}: SQLSTATE {problem.sqlstate or 'none'}"
    return f"{type(problem).__name__}: {problem}"


def run_round(jobs: list[Job], log: Callable[[str], None] = print, timeout: float | None = None,
              running: dict[str, threading.Thread] | None = None,
              needs: dict[str, tuple[str, ...]] | None = None) -> list[JobResult]:
    """Run every job once, in order, even if one fails. Never raises.

    `timeout` is in seconds (None waits for ever). A job that overruns keeps
    going in a daemon thread and is recorded in `running`; pass the same dict
    each round so it is skipped until that thread ends.

    With `needs` (the worker's `NEEDS`), a job named there is skipped, as "skipped",
    when one of the jobs it names didn't finish with exit 0 this round: it would
    read what that job never brought in. The skipped job is still a row on /health,
    with no exit code, so the round shows it didn't run.
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
        missing = None
        if needs is not None:
            missing = [r.job for r in results if r.job in needs.get(name, ())
                       and (r.outcome != "finished" or r.exit_code != 0)]
        if missing:
            reason = f"skipped, {', '.join(missing)} didn't finish this round"
            log(f"{started:%Y-%m-%d %H:%M:%S} {name}: {reason}")
            results.append(JobResult(name, started.isoformat(), 0.0, None, "skipped", reason))
            continue
        log(f"{started:%Y-%m-%d %H:%M:%S} {name}: starting")
        done: dict = {}

        def target(run=run, done=done):
            try:
                done["code"] = int(run())
            except BaseException as problem:  # noqa: BLE001 - one job's crash must not stop the others
                done["error"] = _describe(problem)

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
# The setting `vending.pages` connects with. Named here too, so the home page can say whether the
# pages are set up without importing them (and the database driver) to draw itself.
PAGES_SETTING = "READONLY_DATABASE_URL"
# The simulated seasons on the dashboard's demo copy (`vending.simulate.demo.ROOT`): files only, no database.
SIMULATIONS = "/dashboard/demo/simulations"
OUTCOMES = ("finished", "crashed", "timed out", "still running", "skipped")
JOB_NAMES = {"slots_check": "Our slot assignment", "sync_history": "Machine readings (sales history)",
             "sync_requests": "Purchase requests and receipts", "sync_catalog": "Product catalog",
             "sync_product_requests": "Catalog requests: approved or rejected",
             "tracking_daily": "Daily tracking check (off unless TRACKING_DAILY=on)"}


def _eastern(moment: str | None) -> str:
    """'Tue 11 Nov, 4:05 PM ET' from an ISO time; 'not yet' for none."""
    if not moment:
        return "not yet"
    try:
        local = datetime.fromisoformat(moment).astimezone(EASTERN)
    except (TypeError, ValueError):
        return "unknown"
    return f"{local:%a %d %b}, {local.hour % 12 or 12}:{local:%M %p} ET"


def pages_ready() -> bool:
    from vending import settings

    return bool(settings.get(PAGES_SETTING))


def home_page(public: dict, pages: bool = False) -> str:
    """The page at /, built only from what /health shows, so no error text can reach it.
    `pages` is whether the dashboard and the DB explorer are set up, so it links to them."""
    if pages:
        look = ("""<ul>
      <li><a href="/dashboard">Agent dashboard</a>: is the agent OK, what is in our slots, what it plans for Friday and why</li>
      <li><a href="/db">Database explorer</a>: every table we keep, its columns and its rows</li>
    </ul>
    <p class="muted">Both only read. Nothing on them can change the machine, an order or our records.</p>""")
    else:
        look = ("""<p class="muted">The agent dashboard and the database explorer aren't set up at this address yet. They wait for a database login that can only read (issue #53).</p>""")
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
    <p class="muted">Each round copies the machine's readings, our purchase requests and the product catalog into our database. It reads the bridge's stored history. Only on Friday afternoons, around our visit, does it also ask the bridge to read the machine.</p>
  </section>
  <section>
    <h2>See what the agent is doing</h2>
    {look}
  </section>
  <section>
    <h2>Learn more</h2>
    <ul>
      <li><a href="{REPO}#readme">What we are building</a> (the README on GitHub)</li>
      <li><a href="{REPO}/blob/main/docs/deploy.md">How this container runs</a></li>
      <li><a href="/health">The same status as JSON</a> (<code>/health</code>)</li>
    </ul>
  </section>
  <footer class="muted">Running since {escape(_eastern(public.get("started_at")))}. This page reloads every minute. Error details stay in the container's log and are never shown here.</footer>
</main>
</body>
</html>
"""


class _Health(BaseHTTPRequestHandler):
    state: State
    # Seconds a client has to send its request. The endpoint is public, and without a limit a
    # connection that sends nothing holds one of the server's threads for ever.
    timeout = 10

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == gate.LOGIN:
            if gate.signed_in(self.headers.get("Cookie")):
                return self._redirect(gate.next_from_query(self.path))
            return self._send(200, "text/html; charset=utf-8",
                              gate.login_page(gate.next_from_query(self.path)).encode(), gate.CSP)
        if path == gate.LOGOUT:
            return self._redirect(gate.LOGIN, gate.cleared_cookie())
        if gate.needs_password(path) and not gate.signed_in(self.headers.get("Cookie")):
            if path in ("/dashboard/state.json", "/dashboard/demo/state.json"):
                # The dashboard's own refresh asks for this; a redirect would only fail there.
                return self._send(401, "application/json", b'{"error": "signed out: reload the page to sign in"}')
            return self._redirect(gate.login_location(self.path))
        if path == "/health":
            body = json.dumps(self.state.public(), indent=2).encode()
            self._send(200 if self.state.healthy else 503, "application/json", body)
        elif path == "":
            self._send(200, "text/html; charset=utf-8", home_page(self.state.public(), pages_ready()).encode())
        elif path == SIMULATIONS or path.startswith(SIMULATIONS + "/"):
            # Before the other /dashboard pages, which read the database; these never do.
            from vending.simulate import demo

            status, kind, body, csp = demo.respond(self.path)
            self._send(status, kind, body.encode(), csp)
        elif path.startswith(("/dashboard", "/db")):
            # Imported here, so the syncs and /health never depend on the pages' code.
            from vending import pages

            # The dashboard names failing syncs from the same outcomes /health shows. They live
            # only in this process's memory, so they are handed over rather than read.
            reply = pages.respond(self.path, self.state.public())
            if reply is None:
                return self._not_found()
            cookies = self._count_view(path, self.path) if reply.status == 200 else ()
            self._send(reply.status, reply.content_type, reply.body.encode(), reply.csp, cookies)
        else:
            self._not_found()

    def do_POST(self):
        """Only the password page takes a POST."""
        if self.path.split("?", 1)[0].rstrip("/") != gate.LOGIN:
            return self._send(405, "text/plain; charset=utf-8", b"Only the password page takes a POST.")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= gate.MAX_FORM_BYTES:
            return self._send(413, "text/plain; charset=utf-8", b"That form is too large.")
        body = self._read_body(length)
        if body is None:
            return self._send(408, "text/plain; charset=utf-8", b"That form took too long to arrive.")
        password, next_path = gate.parse_form(body)
        client = gate.client_of(self.headers.get("X-Forwarded-For"), self.client_address[0])
        try:
            right = gate.check(password, client)
        except gate.TooSoon:
            return self._send(429, "text/html; charset=utf-8",
                              gate.login_page(next_path, "Wait a second, then try again.").encode(), gate.CSP)
        except gate.Busy:
            return self._send(503, "text/html; charset=utf-8",
                              gate.login_page(next_path, "Busy. Try again in a few seconds.").encode(), gate.CSP)
        if not right:
            return self._send(401, "text/html; charset=utf-8",
                              gate.login_page(next_path, "That password isn't right.").encode(), gate.CSP)
        browser, cookies = visits.identify(self.headers.get("Cookie"))
        if browser is not None:
            visits.sign_in(browser)
        self._redirect(next_path, gate.session_cookie(), *cookies)

    def _count_view(self, path: str, target: str) -> tuple[str, ...]:
        """Count a signed-in browser opening `path` (`target` is it with its query; `vending.visits`);
        the cookies to set, if it has no id yet. Only queues the count, so the page never waits on it.

        The demo copy (`/dashboard/demo`, `vending.demo`) isn't one of `visits.PAGES`, so a view of it is
        never counted: not as a visit, and it sets no browser cookie either. Visits are what people
        read from the real page."""
        if path not in visits.PAGES:
            return ()
        browser, cookies = visits.identify(self.headers.get("Cookie"))
        if browser is not None and not visits.is_refresh(self.headers, target):
            visits.view(path, browser)
        return cookies

    def _read_body(self, length: int) -> bytes | None:
        """The request body, or None when it doesn't all arrive within `gate.FORM_SECONDS`. The
        per-read `timeout` alone would let a client trickle a byte every few seconds for hours."""
        deadline = time.monotonic() + gate.FORM_SECONDS
        data = b""
        try:
            while len(data) < length:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self.connection.settimeout(left)
                chunk = self.rfile.read1(length - len(data))
                if not chunk:
                    return None
                data += chunk
        except (TimeoutError, OSError):
            return None
        finally:
            self.connection.settimeout(self.timeout)
        return data

    def _redirect(self, location: str, *cookies: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _not_found(self) -> None:
        self.send_response(404)
        self.end_headers()

    def _send(self, status: int, content_type: str, body: bytes,
              csp: str = "default-src 'none'; style-src 'unsafe-inline'", cookies: tuple[str, ...] = ()) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # The home page has no scripts and loads nothing from anywhere else; a page from
        # `vending.pages` brings its own policy.
        self.send_header("Content-Security-Policy", csp)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


def serve_health(state: State, port: int) -> ThreadingHTTPServer:
    """Answer /, /health and the public pages in a daemon thread. Returns the server, for tests to shut down."""
    handler = type("Health", (_Health,), {"state": state})
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# When the extra rounds after the slot lock run (see the module docstring).
AFTER_LOCK = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15))


def _slot_lock() -> datetime | None:
    """The slot lock (`vending.slots.lock_at`), or None when the setting is malformed (the slot check fails then)."""
    from vending import settings, slots

    try:
        return slots.lock_at()
    except settings.SettingsError:
        return None


def aligns(every_minutes: float) -> bool:
    """Whether rounds `every_minutes` apart can keep to the same minutes past every hour."""
    return every_minutes > 0 and (60 % every_minutes == 0 or every_minutes % 60 == 0)


def next_round(now: datetime, every_minutes: float, lock: datetime | None,
               at_minute: float | None = None) -> datetime:
    """When the next round runs, or sooner, at the next of the extra rounds after the slot lock (AFTER_LOCK)
    that falls in between. Their check confirms our assignment for the competition.

    With `at_minute`, rounds keep to that many minutes past the hour (UTC), `every_minutes` apart: the
    first such time after `now`. A round that ran late, or an extra round, doesn't move the ones after it.
    Without it, the next round is `every_minutes` after `now`. `every_minutes` must align (`aligns`)."""
    if at_minute is None:
        due = now + timedelta(minutes=every_minutes)
    else:
        step = timedelta(minutes=every_minutes)
        # Under an hour apart, the first round of the hour is the minute modulo the gap: every 15 at
        # minute 20 is :05, :20, :35 and :50, so a start at :02 runs at :05, not :20.
        first = at_minute % every_minutes if every_minutes < 60 else at_minute
        due = now.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=first)
        if every_minutes % 60 == 0:      # every few hours: count from midnight, so the hours stay put too
            due = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=at_minute)
        while due <= now:
            due += step
    if lock is not None:
        extra = [lock + after for after in AFTER_LOCK if now < lock + after < due]
        if extra:
            return min(extra)
    return due


class Stopped(Exception):
    """SIGTERM arrived; raised in the main thread to end the loop."""


def _stop(signum, frame):
    raise Stopped


def main(argv: list[str] | None = None, jobs: list[Job] | None = None,
         sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] = print,
         reads=None, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> int:
    """`reads` is the visit reader (`vending.visit_polls.Reader`). Left out, the real worker, started with
    no `jobs`, builds its own from the settings; a test that passes `jobs` gets none, so it can't read the machine."""
    parser = argparse.ArgumentParser(prog="python -m vending.worker", description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="one round, then exit with the worst exit code")
    parser.add_argument("--every", type=float, default=float(os.environ.get("SYNC_EVERY_MINUTES") or 60),
                        help="minutes between rounds (default SYNC_EVERY_MINUTES or 60)")
    parser.add_argument("--at-minute", default=os.environ.get("SYNC_AT_MINUTE") or "5",
                        help="minutes past the hour (UTC) the rounds keep to, or off "
                             "(default SYNC_AT_MINUTE or 5: the staff's poller reads the machine on the hour)")
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("JOB_TIMEOUT_MINUTES") or 30),
                        help="minutes each job may run (default JOB_TIMEOUT_MINUTES or 30)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 8080),
                        help="port for /, /health and the public pages (default PORT or 8080); 0 serves nothing")
    args = parser.parse_args(argv)
    if args.every <= 0:
        parser.error("--every must be above zero")
    if args.timeout <= 0:
        parser.error("--timeout must be above zero")
    at_minute = None
    if args.at_minute.strip().lower() != "off":
        try:
            at_minute = float(args.at_minute)
        except ValueError:
            parser.error("--at-minute must be a number of minutes, or off")
        if not 0 <= at_minute < 60:
            parser.error("--at-minute must be from 0 to under 60")
        if not aligns(args.every):
            parser.error(f"--every {args.every:g} can't keep to the same minute every hour: "
                         "use a number that divides 60 or a multiple of 60, or --at-minute off")
    if reads is None and jobs is None and not args.once:
        from vending import visit_polls    # imported here: it needs the database driver, and the worker's other code doesn't

        reads = visit_polls.default_reader(log)
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
            state.results = run_round(jobs, log, args.timeout * 60, running, NEEDS)
            state.rounds += 1
            state.last_round_at = datetime.now(timezone.utc).isoformat()
            if args.once:
                return max((r.exit_code if r.exit_code is not None else 1) for r in state.results)
            now = clock()
            due = next_round(now, args.every, _slot_lock(), at_minute)
            state.next_round_at = due.isoformat()
            log(f"next round at {due:%Y-%m-%d %H:%M:%S} UTC")
            while True:
                # A read can run past the round's time: then the round is due now, and a negative sleep
                # would raise and end the worker.
                if now >= due:
                    break
                # Until the next round, take the visit reads when they are due. They are not jobs of the
                # round: a read that fails is logged and never reaches `state`, so /health never shows it.
                wake = due if reads is None else min(due, reads.next_due(now))
                sleep((wake - now).total_seconds())
                if wake >= due:
                    break
                reads.tick(clock())
                now = clock()
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
