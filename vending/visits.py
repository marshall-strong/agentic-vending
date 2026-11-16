"""Count who looks at the container's pages: sign-ins and visits, by browser, never by person.

    python -m vending.visits            # the counts, from DATABASE_URL

The worker calls `sign_in` when someone gives the right password and `view` when a signed-in
browser opens `/dashboard` or `/db`. Each lands in `page_visits` (migration 016).

What counts:
- **A browser** is a random id in its own cookie (`COOKIE`), made the first time a signed-in
  browser shows up. It isn't the password's cookie, so adding it signed nobody out. With one
  shared password there are no names, and no IP address or browser string is kept.
- **A visit** is a browser opening a page, at most once per page per `VISIT_MINUTES`. The
  dashboard reloads itself every 30 seconds, and those reloads aren't visits: the page's own
  refresh says so in a header (`REFRESH_HEADER`), browsers mark any script's fetch with
  `Sec-Fetch-Mode` anyway, and without scripts the page reloads itself to a URL ending in
  `?refresh=1` (`REFRESH_QUERY`).
- **A sign-in** is every right password. A browser stays signed in for 30 days, so it is
  roughly one per new device.
- **Browsers**, all time and this week, are the browsers that opened a page. One that only
  signed in isn't a visitor.

Nothing here can slow or break a page. A page only puts the event on a short queue; one
background thread writes it with the syncs' login (`DATABASE_URL`), since the pages' own login
can only read and must stay that way. The writer borrows one of the pages' own connections
(`vending.pages.MAX_CONNECTIONS`), and only while one is free, so counting never adds a
connection to the server. If the queue is full, no page connection comes free within
`SLOT_WAIT_SECONDS`, the table isn't there yet or the database is down, the events are dropped,
added to `dropped`, and the log says so once a minute. No model is called.

Reading the counts doesn't grow with history: the writer keeps the all-time totals in one row of
`page_visit_totals` and each browser once in `page_visit_browsers` (migration 018), and the
week's numbers read only the last 7 days of `page_visits`, through its index on `at`.
"""

from __future__ import annotations

import queue
import re
import secrets
import sys
import threading
import time
from http.cookies import CookieError, SimpleCookie

COOKIE = "vend_browser"
# A browser keeps its id about as long as browsers let a cookie live (400 days).
COOKIE_DAYS = 400
REFRESH_HEADER = "X-Dashboard-Refresh"
# The query the dashboard's no-script reload adds, so that reload isn't counted as a visit.
REFRESH_QUERY = "refresh=1"
PAGES = ("/dashboard", "/db")
SIGN_IN_PAGE = "/login"
VISIT_MINUTES = 30
# Events waiting to be written. A handful of people make a few a day; past this, drop them.
QUEUE_SIZE = 1000
# Browser-and-page pairs remembered for the half-hour rule; past this, forget them all.
MAX_REMEMBERED = 10_000
# How long the writer waits for more events before it writes what it has.
GATHER_SECONDS = 2.0
STATEMENT_TIMEOUT_MS = 5_000
# Seconds the writer waits for a free page connection before it drops a batch, and how often it looks.
SLOT_WAIT_SECONDS = 30.0
SLOT_POLL_SECONDS = 0.25
LOG_EVERY_SECONDS = 60
# Tests switch recording off except in the tests about it (tests/conftest.py).
ENABLED = True

_ID = re.compile(r"[0-9a-f]{16}")
_events: queue.Queue = queue.Queue(QUEUE_SIZE)
_last_view: dict[tuple[str, str], float] = {}
_lock = threading.Lock()
_writer: threading.Thread | None = None
_logged: dict[str, float] = {}
# Events dropped since the worker started: a full queue, no free connection, or a failed write.
dropped = 0


def new_browser() -> str:
    return secrets.token_hex(8)


def browser_of(cookie_header: str | None) -> str | None:
    """This browser's id from its cookies, or None when it has none (or a malformed one)."""
    if not cookie_header:
        return None
    try:
        jar = SimpleCookie(cookie_header)
    except CookieError:
        return None
    value = jar[COOKIE].value if COOKIE in jar else ""
    return value if _ID.fullmatch(value) else None


def browser_cookie(browser: str) -> str:
    return f"{COOKIE}={browser}; Max-Age={COOKIE_DAYS * 86400}; Path=/; HttpOnly; Secure; SameSite=Lax"


def identify(cookie_header: str | None) -> tuple[str | None, tuple[str, ...]]:
    """(this browser's id, the cookies to set). A browser without an id gets a new one and its
    cookie. With counting switched off there is nothing to count, so no id and no cookie."""
    if not ENABLED:
        return None, ()
    browser = browser_of(cookie_header)
    if browser is not None:
        return browser, ()
    browser = new_browser()
    return browser, (browser_cookie(browser),)


def is_refresh(headers, target: str = "") -> bool:
    """Whether a request is the dashboard refreshing itself (a script's fetch, or the no-script
    reload to `?refresh=1`), not a person opening the page. `target` is the request's path and query."""
    if headers.get(REFRESH_HEADER):
        return True
    if REFRESH_QUERY in target.partition("?")[2].partition("#")[0].split("&"):
        return True
    mode = headers.get("Sec-Fetch-Mode")
    return bool(mode) and mode != "navigate"


def sign_in(browser: str) -> None:
    _put("sign_in", SIGN_IN_PAGE, browser)


def view(page: str, browser: str, now: float | None = None) -> bool:
    """Count `browser` opening `page`, unless it already did within `VISIT_MINUTES`. Returns whether it counted."""
    if page not in PAGES:
        return False
    now = time.monotonic() if now is None else now
    with _lock:
        last = _last_view.get((browser, page))
        if last is not None and now - last < VISIT_MINUTES * 60:
            return False
        if len(_last_view) >= MAX_REMEMBERED:
            _last_view.clear()
        _last_view[(browser, page)] = now
    _put("view", page, browser)
    return True


def _put(kind: str, page: str, browser: str) -> None:
    if not ENABLED or not _ID.fullmatch(browser):
        return
    try:
        _events.put_nowait((kind, page, browser))
    except queue.Full:
        _drop(1)
        _log("full", f"visits: queue full, dropped a {kind} ({dropped} dropped so far)")
        return
    _start_writer()


def _start_writer() -> None:
    global _writer
    with _lock:
        if _writer is None or not _writer.is_alive():
            _writer = threading.Thread(target=_write_forever, name="visits", daemon=True)
            _writer.start()


def _drop(n: int) -> None:
    global dropped
    with _lock:
        dropped += n


def _log(kind: str, message: str) -> None:
    now = time.monotonic()
    if now - _logged.get(kind, -LOG_EVERY_SECONDS) >= LOG_EVERY_SECONDS:
        _logged[kind] = now
        print(message, file=sys.stderr, flush=True)


def _gather() -> list[tuple[str, str, str]]:
    """The next event, waiting for it, then any that follow within `GATHER_SECONDS`."""
    batch = [_events.get()]
    deadline = time.monotonic() + GATHER_SECONDS
    while len(batch) < QUEUE_SIZE:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        try:
            batch.append(_events.get(timeout=left))
        except queue.Empty:
            break
    return batch


def write(conn, batch: list[tuple[str, str, str]]) -> None:
    """Store a batch of (kind, page, browser) events and add them to the running totals, in one
    transaction. Times are when they're written, within seconds."""
    viewers = list(dict.fromkeys(browser for kind, _, browser in batch if kind == "view"))
    sign_ins = sum(kind == "sign_in" for kind, _, _ in batch)
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany("insert into page_visits (kind, page, browser) values (%s, %s, %s)", batch)
        cur.execute("insert into page_visit_browsers (browser) select unnest(%s::text[]) on conflict do nothing",
                    (viewers,))
        new = max(cur.rowcount, 0)
        cur.execute("""
            insert into page_visit_totals as t (browsers, sign_ins, visits, since) values (%s, %s, %s, now())
            on conflict (only_row) do update
               set browsers = t.browsers + excluded.browsers, sign_ins = t.sign_ins + excluded.sign_ins,
                   visits = t.visits + excluded.visits, since = coalesce(t.since, excluded.since)""",
                    (new, sign_ins, len(batch) - sign_ins))


def _connect():
    """The syncs' login with the page reader's limits: the statement timeout on the server, and
    keepalives and a TCP timeout (`vending.pages.KEEPALIVES`) so a connection gone silent fails
    within seconds instead of holding a page connection for minutes."""
    import psycopg

    from vending import db, pages, settings

    url = settings.require("DATABASE_URL", "The visit counter writes with the syncs' login.")
    return psycopg.connect(url, autocommit=True, connect_timeout=db.CONNECT_TIMEOUT,
                           options=db.session_options(url, f"-c statement_timeout={STATEMENT_TIMEOUT_MS}"),
                           **pages.KEEPALIVES)


# Tests swap this to point at a throwaway schema.
connect = _connect


def _write_forever() -> None:
    """Write batches as they come, opening a connection only while there is something to write,
    so the counter holds none of the server's shared connections while nobody is looking."""
    while True:
        flush(_gather())


def flush(batch: list[tuple[str, str, str]]) -> bool:
    """Write one batch on one of the pages' connections, once one is free (`vending.pages.spare_slot`).
    Never raises: no free connection within `SLOT_WAIT_SECONDS`, or any failure, drops the batch,
    adds it to `dropped` and logs it (once a minute per kind). Returns whether it was written."""
    from vending import pages

    with pages.spare_slot(SLOT_WAIT_SECONDS, SLOT_POLL_SECONDS) as free:
        if not free:
            _drop(len(batch))
            _log("busy", f"visits: dropped {len(batch)}: no page connection came free ({dropped} dropped so far)")
            return False
        try:
            with connect() as conn:
                write(conn, batch)
            return True
        except Exception as error:  # noqa: BLE001 - counting must never take the pages down
            _drop(len(batch))
            first = str(error).strip().splitlines()[0] if str(error).strip() else ""
            _log(type(error).__name__, f"visits: dropped {len(batch)}: {type(error).__name__}: {first}")
            return False


def counts(conn, now=None) -> dict:
    """Browsers that opened a page, sign-ins and visits, all time and over the last 7 days.

    The all-time numbers are the writer's running totals, one row. The week's read only the last
    7 days of `page_visits` through its index on `at`, so neither read grows with history."""
    from datetime import datetime, timedelta, timezone

    week = (now or datetime.now(timezone.utc)) - timedelta(days=7)
    row = conn.execute("""
        select coalesce(t.browsers, 0), w.browsers, coalesce(t.sign_ins, 0), coalesce(t.visits, 0),
               w.visits, t.since
          from (select count(distinct browser) as browsers, count(*) as visits
                  from page_visits where at > %(week)s and kind = 'view') w
          left join page_visit_totals t on true""", {"week": week}).fetchone()
    keys = ("browsers", "browsers_7_days", "sign_ins", "visits", "visits_7_days", "since")
    return dict(zip(keys, row))


def main(argv: list[str]) -> int:
    if argv:
        print("usage: python -m vending.visits", file=sys.stderr)
        return 2
    from vending import db

    with db.connect() as conn:
        c = counts(conn)
    if c["since"] is None:
        print("No visits recorded yet.")
        return 0
    print(f"since {c['since']:%Y-%m-%d %H:%M} UTC: {c['browsers']} browsers, {c['sign_ins']} sign-ins, "
          f"{c['visits']} visits")
    print(f"last 7 days: {c['browsers_7_days']} browsers, {c['visits_7_days']} visits")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
