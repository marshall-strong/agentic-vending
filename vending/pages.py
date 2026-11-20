"""The pages anyone can open at the Container App's address: the agent dashboard, its charts and the DB explorer.

    /dashboard               the agent dashboard (`vending.dashboard`)
    /dashboard/state.json    the same data as JSON
    /dashboard/demo          a copy of the dashboard drawn from made-up data (`vending.demo`); no database
    /dashboard/money         the money chart: competition money by day (`vending.money_chart`)
    /dashboard/pnl           the weekly P&L (`vending.pnl_report`)
    /dashboard/spend         development spend by week (`vending.dev_spend`)
    /dashboard/demo/money    the money chart with its made-up MOCK data, under a DEMO banner; no database
    /dashboard/demo/spend    dev spend from the team's run notes, under a DEMO banner; no database
    /db                      the DB explorer (`vending.db_explorer`)

    python -m vending.pages check                            # is the pages' login really read-only?
    python -m vending.pages grant vending_reader --by NAME   # as the tables' owner: let that role read them

`vending.worker` serves these beside `/` and `/health` (issue #53), behind one shared
password (`vending.gate`). Anyone with it can look, and nobody can change anything.

Read-only in three layers, so that a bug in our code still can't write:

 1. **A login that can only read.** The pages connect with their own setting,
    `READONLY_DATABASE_URL`, never with the syncs' `DATABASE_URL`. Before a page
    is served, the login is asked what it may do: if it can insert, update,
    delete or truncate any table, or create one, the pages refuse to serve and
    say so in the log. So pointing the setting at the syncs' login switches the
    pages off instead of weakening them. Until the setting exists, the pages
    answer "not set up yet".
 2. **A read-only session.** Every transaction on the connection is read-only,
    and each page is one `repeatable read, read only` transaction.
 3. **Nothing to send.** GET only, no SQL box (the password form is the worker's, `vending.gate`). The only things a URL can
    choose are a table Postgres already lists and a row offset.

Load limits, because the address is public: at most `MAX_CONNECTIONS` page
connections at once (the server's 50 are shared with the other team; the visit
counter's writer borrows one of these, only when it is free: `spare_slot`), a request
that can't get one within `WAIT_SECONDS` is told the page is busy, every
statement is cancelled after `STATEMENT_TIMEOUT_MS`, and the explorer shows 100
rows a page. Hammering the pages makes the pages slow, not the syncs.

The three chart pages are built per request from real data only, on the same connection and with
the same error handling as the dashboard. Each has a loader (`money_data`, `pnl_data`,
`spend_data`) and a function that renders it from its data alone (`money_page`, `pnl_page`,
`spend_page`), so made-up data can be rendered the same way elsewhere. Before the ledger's opening
entry, or before an opening dated ahead, the money chart and the P&L say there is no competition
data yet (`NotYet`); the P&L never counts from before the opening, whatever dates the URL asks for.
A rendered chart is served again for `CACHE_SECONDS` without its replay: dev spend without the
database, the money chart and the P&L only while the stored slot assignment is the same.

What the pages leave out: the selections that aren't ours by the stored assignment and the
machine-wide poll totals (`explorer_rules`, which `python -m vending.db_explorer` uses too, and
`dashboard.only_ours`), and the error text of failures (`HIDE`, and
`dashboard.hide_errors`), because a database error names the host and the user.
Their own errors are answered with a plain sentence, and the detail goes to the
container's log. No table holds a password or a key.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import re
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from html import escape
from urllib.parse import parse_qs, urlsplit

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from vending import dashboard, db, db_explorer, demo, dev_spend, ledger, money_chart, pnl_report, settings
from vending.slots import DEVICE_ID, SlotsNotRecorded, our_slots

READER = "READONLY_DATABASE_URL"
DASHBOARD, STATE, EXPLORER = "/dashboard", "/dashboard/state.json", "/db"
MONEY_CHART, PNL, SPEND = "/dashboard/money", "/dashboard/pnl", "/dashboard/spend"
# Where the dashboard links to each chart (`dashboard.render`'s `charts`).
CHARTS = {"money": MONEY_CHART, "pnl": PNL, "spend": SPEND}
TITLES = {DASHBOARD: "Agent dashboard", STATE: "Agent dashboard", EXPLORER: "DB explorer",
          MONEY_CHART: "Money chart", PNL: "Weekly P&L", SPEND: "Development spend"}
MAX_CONNECTIONS = 2
# Seconds a request waits for one of those connections before it is told the page is busy.
WAIT_SECONDS = 5
STATEMENT_TIMEOUT_MS = 5_000
OPTIONS = f"-c default_transaction_read_only=on -c statement_timeout={STATEMENT_TIMEOUT_MS}"
# The statement timeout runs on the server, so it can't free a request whose connection has gone
# quiet (a failover, a half-open socket). These client-side limits make a dead connection fail
# within seconds instead of holding one of the page connections for 15 minutes or more.
KEEPALIVES = {"keepalives": 1, "keepalives_idle": 5, "keepalives_interval": 2, "keepalives_count": 3,
              "tcp_user_timeout": STATEMENT_TIMEOUT_MS + 5_000}
# Seconds a rendered chart page is served again without its replay. A chart replays the whole run,
# so two viewers reloading one shouldn't hold both page connections and leave /dashboard busy. The
# money chart and the P&L still read the stored assignment first, and keep a copy per assignment.
CACHE_SECONDS = 60
# Rendered chart pages kept at once: one each for the money chart and dev spend, the rest for P&L ranges.
CACHE_ENTRIES = 32
# Seconds a passed "this login can't write" check is trusted before it is made again.
RECHECK_SECONDS = 600
# Seconds between two log lines about the same kind of problem, so a stuck page can't flood the log.
LOG_EVERY_SECONDS = 60

HTML, JSON, TEXT = "text/html; charset=utf-8", "application/json", "text/plain; charset=utf-8"
# Neither page loads anything from anywhere else. The dashboard's own script refreshes it in place.
CSP = "default-src 'none'; style-src 'unsafe-inline'"
CSP_DASHBOARD = CSP + "; script-src 'unsafe-inline'; connect-src 'self'"
# The chart pages fetch nothing: their data is inside them. The money chart and dev spend run one
# inline script each, allowed by its hash (`chart_csp`), so no other script can run; the P&L has
# none, and its one form sends a GET back to itself.
_LOCKED = "base-uri 'none'; frame-ancestors 'none'"
CSP_PNL = f"{CSP}; form-action 'self'; {_LOCKED}"

# What every explorer leaves out, however it is run (here at /db, or `python -m vending.db_explorer`).
# VA1, the machine-wide lifetime totals: usable only from polls taken before the fall cut
# (`vending.start.fall_cut`); from the cut on, the total minus ours is the other selections' sales
# (`docs/integrity.md`). Hidden for every poll: `Hide` can't test a time, and nothing needs the
# pre-cut ones. `raw` is safe as it is: the explorer shows a bytea's size, never its bytes.
TOTALS = {
    "polls": db_explorer.Hide("", "", ("total_paid_cents", "total_paid_units"), "(hidden)"),
}

# What the public explorer leaves out besides, by table name. The dashboard's own list is
# `dashboard.hide_errors`.
HIDE = {
    "action_log": db_explorer.Hide("outcome", "failed", ("detail", "result"), dashboard.HIDDEN),
    "agent_runs": db_explorer.Hide("status", "failed", ("detail",), dashboard.HIDDEN),
    # The dev machines' hostnames; `when` names no column, so every row is hidden.
    "model_calls": db_explorer.Hide("", "", ("host",), "(hidden)"),
    **TOTALS,
}



def _ours(column: str, slots: tuple[str, ...]) -> sql.Composable:
    """`t.<column>` is one of `slots`, as a condition. False for no slots."""
    if not slots:
        return sql.SQL("false")
    return sql.SQL("t.{} in ({})").format(
        sql.Identifier(column), sql.SQL(", ").join(sql.Literal(slot) for slot in slots))


def only(slots: tuple[str, ...]) -> dict[str, db_explorer.Only]:
    """The rows an explorer shows, by table name, for our selections `slots`.

    The other selections aren't ours (`docs/integrity.md`), so these two tables, which store every
    selection, are cut to `slots` on our machine. `poll_selections` has no device column, so it
    goes through its poll. With no slots, neither shows a row.
    """
    return {
        "poll_selections": db_explorer.Only(("session_id", "selection"), lambda table: _ours("selection", slots)
                                            + sql.SQL(" and t.session_id in (select session_id from {}"
                                                      " where device_id = {})").format(
                                                sql.Identifier(table.schema, "polls"), sql.Literal(DEVICE_ID))),
        "slot_counts": db_explorer.Only(("device_id", "selection"), lambda table: _ours("selection", slots)
                                        + sql.SQL(" and t.device_id = {}").format(sql.Literal(DEVICE_ID))),
        # Raw plans can reference local files and selections from an earlier assignment.
        "first_fill_plans": db_explorer.Only((), lambda table: sql.SQL("false")),
    }


def stored_slots(conn: psycopg.Connection) -> tuple[str, ...]:
    """Our selections from the stored assignment (`vending.slots.our_slots`), read per page. Empty
    when it can't be read (nothing usable stored, no table yet, a login that may not read it): then
    no selection is ours. A savepoint, so a failed read leaves the caller's transaction usable."""
    try:
        with conn.transaction():
            return our_slots(conn)
    except (SlotsNotRecorded, psycopg.Error):
        return ()


def explorer_rules(conn: psycopg.Connection, public: bool) -> tuple[dict[str, db_explorer.Hide],
                                                                     dict[str, db_explorer.Only]]:
    """(hide, only) for `db_explorer.render`, the one policy both explorers use.

    Both leave out the selections that aren't ours by the stored assignment (none shown when it
    can't be read) and the machine-wide poll totals (`TOTALS`). The public page also leaves out
    the error text of failures and the dev machines' hostnames (`HIDE`).
    """
    return (HIDE if public else TOTALS), only(stored_slots(conn))


NOT_SET_UP = ("This page isn't set up here yet. It needs a database login that can only read "
              f"({READER}); see docs/deploy.md in the repository.")
BUSY = "This page is busy. Try again in a few seconds."
CANT_READ = "This page can't read the database right now. The detail is in the container's log."
NOT_READ_ONLY = ("This page is switched off: its database login is able to write, and a public page "
                 "must not be. The detail is in the container's log.")
BROKEN = "This page hit an error it doesn't handle yet. The detail is in the container's log."


class Busy(Exception):
    """Every page connection is in use."""


class CanWrite(Exception):
    """The pages' login may change something, so they must not be served with it."""


@dataclass(frozen=True)
class Reply:
    status: int
    content_type: str
    body: str
    csp: str = CSP


def configured() -> bool:
    return bool(settings.get(READER))


def _connect() -> psycopg.Connection:
    url = settings.require(READER, "It is the read-only login's URL; see docs/deploy.md.")
    return psycopg.connect(url, autocommit=True, connect_timeout=db.CONNECT_TIMEOUT, options=OPTIONS, **KEEPALIVES)


# Tests swap this to point at a throwaway schema.
connect: Callable[[], psycopg.Connection] = _connect

_slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
# (path, P&L query[, our slots]) -> (monotonic time it expires, reply), for the chart pages (`CACHE_SECONDS`).
_cache: dict[tuple, tuple[float, "Reply"]] = {}
_cache_lock = threading.Lock()
_read_only_until = 0.0
_logged: dict[str, float] = {}

_OURS = "n.nspname not in ('pg_catalog', 'information_schema') and left(n.nspname, 3) <> 'pg_'"
_WRITABLE = f"""
    select n.nspname || '.' || c.relname
      from pg_class c join pg_namespace n on n.oid = c.relnamespace
     where c.relkind in ('r', 'p', 'v', 'm', 'f') and {_OURS}
       and has_table_privilege(c.oid, 'INSERT, UPDATE, DELETE, TRUNCATE')
    union all
    select 'schema ' || n.nspname || ' (create)' from pg_namespace n
     where {_OURS} and has_schema_privilege(n.oid, 'CREATE')
     order by 1"""
_UNREADABLE = f"""
    select n.nspname || '.' || c.relname
      from pg_class c join pg_namespace n on n.oid = c.relnamespace
     where c.relkind in ('r', 'p', 'v', 'm', 'f') and {_OURS}
       and has_schema_privilege(n.oid, 'USAGE') and not has_table_privilege(c.oid, 'SELECT')
    union all
    select 'schema ' || n.nspname || ' (no usage)'
      from unnest(string_to_array(current_setting('search_path'), ',')) as p(entry)
      join pg_namespace n on n.nspname = replace(trim(both ' "' from p.entry), '$user', current_user)
     where {_OURS} and not has_schema_privilege(n.oid, 'USAGE')
     order by 1"""


def writable(conn: psycopg.Connection) -> list[str]:
    """Every table this login may change and every schema it may create in. Empty for a reader."""
    return [row[0] for row in conn.execute(_WRITABLE)]


def unreadable(conn: psycopg.Connection) -> list[str]:
    """Tables this login can see but not read, and schemas on its search path it may not look
    inside (whose tables it can't even see); a page that needs one would fail."""
    return [row[0] for row in conn.execute(_UNREADABLE)]


def _log(kind: str, message: str) -> None:
    now = time.monotonic()
    if now - _logged.get(kind, -LOG_EVERY_SECONDS) >= LOG_EVERY_SECONDS:
        _logged[kind] = now
        print(f"pages: {message}", file=sys.stderr, flush=True)


@contextmanager
def _connection() -> Iterator[psycopg.Connection]:
    """One of the few page connections, on a login shown to be read-only."""
    global _read_only_until
    if not _slots.acquire(timeout=WAIT_SECONDS):
        raise Busy
    try:
        with connect() as conn:
            if time.monotonic() >= _read_only_until:
                found = writable(conn)
                if found:
                    raise CanWrite(", ".join(found[:5]) + (f" and {len(found) - 5} more" if len(found) > 5 else ""))
                _read_only_until = time.monotonic() + RECHECK_SECONDS
            yield conn
    finally:
        _slots.release()


@contextmanager
def spare_slot(wait: float, every: float) -> Iterator[bool]:
    """One of the `MAX_CONNECTIONS` page connections for background work (the visit counter's
    writer, `vending.visits`), so that work never adds a connection of its own. Yields whether
    one came free within `wait` seconds.

    It only ever takes a free one, looking every `every` seconds instead of queueing, so a page
    already waiting gets the next one that frees up. The caller opens its own connection and must
    keep it bounded (statement timeout and `KEEPALIVES`), since a page may wait on it."""
    deadline = time.monotonic() + wait
    while not _slots.acquire(blocking=False):
        if time.monotonic() >= deadline:
            yield False
            return
        time.sleep(every)
    try:
        yield True
    finally:
        _slots.release()


def _notice(title: str, text: str) -> str:
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{escape(title)}</title><style>body{{font:16px/1.5 system-ui,sans-serif;max-width:40rem;"
            "margin:3rem auto;padding:0 16px}@media (prefers-color-scheme:dark){body{background:#111418;"
            "color:#e8eaed}a{color:inherit}}</style></head>"
            f'<body><h1>{escape(title)}</h1><p>{escape(text)}</p><p><a href="/">Back to the home page</a></p>'
            "</body></html>")


def _problem(path: str, status: int, text: str) -> Reply:
    if path == STATE:
        return Reply(status, JSON, dashboard.to_json({"error": text}))
    return Reply(status, HTML, _notice(TITLES[path], text))


# --- The chart pages ----------------------------------------------------------------------------

NO_SLOTS = ("This page can't be drawn: our slot assignment isn't recorded, and the money counts only "
            "our slots. A person records it with python -m vending.slots record.")
_SCRIPT = re.compile(r"<script>(.*?)</script>", re.S)


def chart_csp(page: str) -> str:
    """The policy for a chart page: nothing fetched, and only the page's own inline scripts run,
    each allowed by its hash. Raises if a script tag has attributes, which the hashes would miss."""
    scripts = _SCRIPT.findall(page)
    if not scripts or page.count("<script") != len(scripts):
        raise RuntimeError("a chart page's scripts can't all be allowed by hash")
    hashes = " ".join(f"'sha256-{base64.b64encode(hashlib.sha256(code.encode()).digest()).decode()}'"
                      for code in scripts)
    return f"{CSP}; script-src {hashes}; form-action 'none'; {_LOCKED}"


@dataclass(frozen=True)
class NotYet:
    """No competition data yet: the ledger has no opening entry (`opens_at` None), or one dated ahead."""
    opens_at: datetime | None


def _opened(conn: psycopg.Connection, now: datetime) -> ledger.Entry | NotYet:
    """The ledger's opening entry, if it is at or before `now`."""
    opened = ledger.opening(conn)
    if opened is None:
        return NotYet(None)
    if opened.at > now:
        return NotYet(opened.at)
    return opened


@contextmanager
def snapshot(conn: psycopg.Connection) -> Iterator[psycopg.Connection]:
    """One repeatable-read, read-only transaction: every read inside it sees the same database."""
    with conn.transaction():
        conn.execute("set transaction isolation level repeatable read, read only")
        yield conn


def money_data(conn: psycopg.Connection) -> dict | NotYet:
    """What the money chart carries (`money_chart.data`), from the opening to today, or NotYet. Call it
    inside a `snapshot`, the one the caller read our slot assignment in. Raises `SlotsNotRecorded`
    without our slot assignment."""
    now = ledger.db_now(conn)
    opened = _opened(conn, now)
    if isinstance(opened, NotYet):
        return opened
    return money_chart.data(conn, now=now) or NotYet(None)


def spend_data(conn: psycopg.Connection) -> tuple[dev_spend.Report, dev_spend.Report]:
    """Development spend by week and by day, from one snapshot. It ends at the ledger's opening
    (`dev_spend.summarise`), so no competition money is in it."""
    with snapshot(conn):
        now = ledger.db_now(conn)
        recorded, opened = dev_spend.calls(conn), dev_spend.opening_at(conn)
    return (dev_spend.summarise(recorded, opened, "week", now),
            dev_spend.summarise(recorded, opened, "day", now))


def pnl_data(conn: psycopg.Connection, query: pnl_report.Query) -> pnl_report.Report | NotYet:
    """The weekly P&L for `query`, or NotYet. The opening is checked before any date in the URL is used,
    and a start on or before the opening's day counts from the opening itself: a model call made before
    it is development spend, not the competition's (#104). Call it inside a `snapshot`, the one the caller
    read our slot assignment in."""
    opened = _opened(conn, ledger.db_now(conn))
    if isinstance(opened, NotYet):
        return opened
    first = opened.at.astimezone(pnl_report.EASTERN).date()
    start = query.start if query.start is not None and query.start > first else None
    return pnl_report.read(conn, start, query.end, query.view)


def _chart_nav(here: str) -> str:
    """Links back to the dashboard and across to the other charts, above a chart page."""
    links = [f'<a href="{DASHBOARD}" style="color:inherit">&larr; Agent dashboard</a>'] + [
        f'<a href="{href}" style="color:inherit">{escape(TITLES[href])}</a>'
        for href in (MONEY_CHART, PNL, SPEND) if href != here]
    return ('<nav aria-label="Pages" style="font:14px/1.5 system-ui,sans-serif;margin:0 0 8px">'
            + " &middot; ".join(links) + "</nav>")


def _with_nav(page: str, here: str) -> str:
    if page.count("<body>") != 1:
        raise RuntimeError(f"{here}: the page has no single <body> to put its links in")
    return page.replace("<body>", "<body>" + _chart_nav(here), 1)


def not_yet_page(path: str, found: NotYet) -> str:
    """What the money chart and the P&L show before the ledger opens: no figures, and why. The money
    chart also links its demo, the one page that may draw made-up data."""
    when = found.opens_at.astimezone(pnl_report.EASTERN) if found.opens_at is not None else None
    if path == MONEY_CHART:
        starts = (f"at the ledger opening ({PLANNED_OPENING})" if when is None else
                  f"at the ledger opening, {when:%a %d %b %Y %H:%M} Eastern (recorded ahead of time)")
        text = f"No competition money yet: the chart starts {starts}."
        how = ledger.OPEN_HINT + "." if when is None else ""
    elif when is None:
        text, how = "There's no competition data until the ledger opens. " + ledger.OPEN_HINT + ".", ""
    else:
        text, how = (f"There's no competition data until the ledger opens, at {when:%a %d %b %Y %H:%M} Eastern. "
                     "The opening was recorded ahead of time."), ""
    page = _notice(TITLES[path], text)
    extra = (f'<p><a href="{DEMO_MONEY}">See the demo with made-up data</a></p>' if path == MONEY_CHART else "")
    extra += f'<p style="font-size:14px;opacity:.75">{escape(how)}</p>' if how else ""
    page = page.replace('<p><a href="/">Back to the home page</a></p>', extra)
    return _with_nav(page, path)


# --- The chart demos: made-up data, no database ----------------------------------------------------

DEMO_MONEY, DEMO_SPEND = "/dashboard/demo/money", "/dashboard/demo/spend"
# When the team plans to record the opening (docs/opening-capital-plan.md); said only while none is recorded.
PLANNED_OPENING = "about 30 Nov"
DEMO_BANNERS = {
    DEMO_MONEY: "DEMO: made-up data, not our machine",
    DEMO_SPEND: "DEMO: the runs the team wrote down in its notes, not the ledger",
}


def _demo_banner(path: str) -> str:
    """A banner that stays at the top of the screen while the page scrolls."""
    return ('<div role="note" style="position:sticky;top:0;z-index:1000;background:#8a1a2a;color:#fff;'
            'font:600 15px/1.4 system-ui,sans-serif;padding:8px 16px;text-align:center;margin:0 0 8px">'
            f'{escape(DEMO_BANNERS[path])} &middot; <a href="{DEMO_LIVE[path]}" style="color:#fff">'
            "the real page</a></div>")


def demo_page(path: str, now: datetime | None = None) -> str:
    """A chart demo: the money chart's own seeded MOCK data, or dev spend from the team's run notes.
    Reads no database. Only these routes may draw made-up data."""
    if path == DEMO_MONEY:
        page = money_chart.page(None)
    else:
        now = now or datetime.now(pnl_report.EASTERN)
        page = dev_spend.page(dev_spend.demo("week", now), dev_spend.demo("day", now))
    page = _with_nav(page, DEMO_LIVE[path])
    return page.replace("<body>", "<body>" + _demo_banner(path), 1)


def _demo_reply(path: str) -> Reply:
    """A chart demo. Opens no connection. Never raises, and puts no error text in a reply."""
    try:
        page = demo_page(path)
        return Reply(200, HTML, page, chart_csp(page))
    except Exception as error:  # noqa: BLE001 - a page answers, whatever happened
        _log(type(error).__name__, f"{path}: {type(error).__name__}: {error}")
        return Reply(500, HTML, _notice("Demo", BROKEN))


DEMO_LIVE = {DEMO_MONEY: MONEY_CHART, DEMO_SPEND: SPEND}


def money_page(payload: dict) -> str:
    """The money chart from its data (`money_chart.data`'s shape), never with MOCK data."""
    return _with_nav(money_chart.live_page(payload), MONEY_CHART)


def spend_page(weeks: dev_spend.Report, days: dev_spend.Report) -> str:
    """The development spend page from its two reports."""
    return _with_nav(dev_spend.page(weeks, days), SPEND)


def pnl_page(report: pnl_report.Report) -> tuple[int, str]:
    """(status, page) for the weekly P&L from its report; its links and form stay under `PNL`."""
    return pnl_report.render(report, base=PNL, nav=_chart_nav(PNL))


def _cached(key: tuple) -> Reply | None:
    with _cache_lock:
        hit = _cache.get(key)
    return hit[1] if hit is not None and time.monotonic() < hit[0] else None


def _keep(key: tuple, reply: Reply) -> None:
    """Keep a good reply for `CACHE_SECONDS`. Errors and refusals are never kept."""
    if reply.status != 200:
        return
    with _cache_lock:
        now = time.monotonic()
        if key not in _cache and len(_cache) >= CACHE_ENTRIES:
            for old in [k for k, (until, _) in _cache.items() if until <= now] or [min(_cache, key=lambda k: _cache[k][0])]:
                del _cache[old]
        _cache[key] = (now + CACHE_SECONDS, reply)


def _chart_reply(path: str, found) -> Reply:
    """The reply for a chart page from what its loader found."""
    if isinstance(found, NotYet):
        return Reply(200, HTML, not_yet_page(path, found), CSP_PNL)
    if path == PNL:
        status, page = pnl_page(found)
        return Reply(status, HTML, page, CSP_PNL)
    page = money_page(found) if path == MONEY_CHART else spend_page(*found)
    return Reply(200, HTML, page, chart_csp(page))


def respond(target: str, syncs: dict | None = None) -> Reply | None:
    """The reply to a GET of `target` (path and query), or None when the path isn't one of ours.

    `syncs` is the worker's latest sync round as `/health` shows it (no error text), for the
    dashboard's Needs attention; the worker keeps it in memory, so it can only be handed over.
    Never raises, and never puts an error's own text in a reply.
    """
    parts = urlsplit(target)
    path = parts.path.rstrip("/")
    if path in (demo.DEMO, demo.DEMO_STATE):
        # The made-up copy: read from a file, never from the database, so it comes before every
        # database check and nothing below it can run for it.
        return _demo(path)
    if path in DEMO_LIVE:
        # The charts' made-up versions, from files in the image: before every database check too.
        return _demo_reply(path)
    if path not in TITLES:
        return None
    request = query = None
    if path == EXPLORER:
        try:
            request = db_explorer.parse("/?" + parts.query if parts.query else "/")
        except db_explorer.BadRequest as error:
            return Reply(400, TEXT, str(error))
    if path == PNL:
        try:
            query = pnl_report.parse("/?" + parts.query if parts.query else "/")
        except pnl_report.BadRequest as error:   # what the visitor typed, escaped
            return Reply(400, HTML, pnl_report.bad_request_page(error, PNL, _chart_nav(PNL)), CSP_PNL)
    if not configured():
        return _problem(path, 503, NOT_SET_UP)
    # Dev spend reads no slot, so a kept copy needs no connection. The money chart and the P&L count our
    # selections only, so theirs is kept per stored assignment, read below on every request.
    key = (path, query)
    if path == SPEND and (hit := _cached(key)) is not None:
        return hit
    try:
        with _connection() as conn:
            if request is not None:
                hide, rows = explorer_rules(conn, public=True)
                status, body = db_explorer.render(
                    conn, request, hide, (("Home", "/"), ("Agent dashboard", DASHBOARD)), rows)
                return Reply(status, HTML, body)
            if path in (MONEY_CHART, PNL):
                # One snapshot for the assignment and the chart, so a copy is kept under the assignment
                # its figures counted, even if a reassignment commits while it is being built.
                with snapshot(conn):
                    key = (path, query, stored_slots(conn))
                    if (hit := _cached(key)) is not None:
                        return hit
                    found = money_data(conn) if path == MONEY_CHART else pnl_data(conn, query)
            elif path == SPEND:
                found = spend_data(conn)
            else:
                snap = dashboard.snapshot(conn, public=True, syncs=syncs)
        if path in (MONEY_CHART, PNL, SPEND):
            reply = _chart_reply(path, found)
            _keep(key, reply)
            return reply
    except SlotsNotRecorded:
        return _problem(path, 503, NO_SLOTS)
    except Busy:
        return _problem(path, 503, BUSY)
    except CanWrite as error:
        _log("can write", f"not serving {path}: the {READER} login can write ({error}). "
                          "Give it a login with select only; run python -m vending.pages check")
        return _problem(path, 503, NOT_READ_ONLY)
    except (psycopg.Error, settings.SettingsError) as error:
        first = str(error).strip().splitlines()[0] if str(error).strip() else ""
        _log(type(error).__name__, f"{path}: {type(error).__name__}: {first}")
        return _problem(path, 503, CANT_READ)
    except Exception as error:  # noqa: BLE001 - a public page answers, whatever happened
        _log(type(error).__name__, f"{path}: {type(error).__name__}: {error}")
        return _problem(path, 500, BROKEN)
    if path == STATE:
        return Reply(200, JSON, dashboard.to_json(snap))
    picked = parse_qs(urlsplit(target).query).get("sel", [None])[0]    # checked against our slots by render
    page = dashboard.render(snap, json_href=STATE, charts=CHARTS,
                            nav=(("Home", "/"), ("Database", EXPLORER), ("Weekly P&L", PNL), ("Dev spend", SPEND),
                                 ("Demo copy", demo.DEMO)), picked=picked)
    return Reply(200, HTML, page, CSP_DASHBOARD)


def _demo(path: str) -> Reply:
    """The demo page or its JSON. Opens no connection. Never raises, and puts no error text in a reply."""
    try:
        if path == demo.DEMO_STATE:
            return Reply(200, JSON, demo.state_json())
        return Reply(200, HTML, demo.page(), CSP_DASHBOARD)
    except Exception as error:  # noqa: BLE001 - a page answers, whatever happened
        _log(type(error).__name__, f"{path}: {type(error).__name__}: {error}")
        if path == demo.DEMO_STATE:
            return Reply(500, JSON, dashboard.to_json({"demo": True, "error": BROKEN}))
        return Reply(500, HTML, _notice("Demo copy", BROKEN))


# --- Commands -----------------------------------------------------------------------------------

def check() -> int:
    """Connect as the pages do and say whether that login is fit for a public page."""
    if not configured():
        print(f"{READER} is not set, so the pages answer \"not set up yet\". See docs/deploy.md.")
        return 1
    with connect() as conn:
        who, database, limit = conn.execute(
            "select current_user, current_database(),"
            " (select rolconnlimit from pg_roles where rolname = current_user)").fetchone()
        can_write, cant_read = writable(conn), unreadable(conn)
    print(f"login {who} on database {database}"
          + (f", at most {limit} connections" if limit is not None and limit >= 0 else ", no connection limit"))
    if can_write:
        print("NOT read-only, so the pages refuse to serve. It can change: " + ", ".join(can_write))
    else:
        print("read-only: it can't insert, update, delete or truncate any table, or create one")
    if cant_read:
        print("can't read: " + ", ".join(cant_read)
              + "\n  As the tables' owner, run: python -m vending.pages grant " + str(who) + " --by NAME")
    else:
        print("can read every table")
    return 1 if can_write or cant_read else 0


def grant(conn: psycopg.Connection, role: str, by: str) -> str:
    """Let `role` read every table in our schema, now and after later migrations. Returns what was done.

    Run as the tables' owner (the `DATABASE_URL` login). It only grants `usage` on the schema and
    `select` on its tables. Creating the role is for whoever administers the server; see
    docs/deploy.md. If this login may not grant `usage` on the schema, Postgres only warns, so
    `check` is what proves the role can read.
    """
    schema, me = conn.execute("select current_schema(), current_user").fetchone()
    if role == me:
        raise ValueError(f"{role} is the login that owns the tables; the pages need a different one")
    if conn.execute("select 1 from pg_roles where rolname = %s", (role,)).fetchone() is None:
        raise LookupError(f"no role {role} on this server. Ask the server's administrator to create it; "
                          "docs/deploy.md has the two statements")
    target, where = sql.Identifier(role), sql.Identifier(schema)
    done = (f"usage on schema {schema} and select on every table in it granted to {role} by {by}, "
            "for later tables too")
    with conn.transaction():
        # Without usage the role can't see the tables at all. Stock Postgres gives it to everyone on
        # `public`, but a hardened server or another schema may not.
        conn.execute(sql.SQL("grant usage on schema {} to {}").format(where, target))
        conn.execute(sql.SQL("grant select on all tables in schema {} to {}").format(where, target))
        # Tables this login creates later, which is what a migration does.
        conn.execute(sql.SQL("alter default privileges in schema {} grant select on tables to {}").format(where, target))
        if conn.execute("select to_regclass('action_log')").fetchone()[0] is not None:
            conn.execute("insert into action_log (kind, tool, arguments, outcome, detail)"
                         " values ('write', 'pages.grant', %s, 'done', %s)", (Jsonb({"role": role}), done))
    return done


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.pages", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help=f"is the {READER} login read-only, and can it read every table?")
    granting = commands.add_parser("grant", help="as the tables' owner: let a role read every table")
    granting.add_argument("role")
    granting.add_argument("--by", required=True, help="your name, for the log")
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            return check()
        if not args.by.strip():
            parser.error("--by NAME is required")
        with db.connect() as conn:
            print(grant(conn, args.role, args.by.strip()))
        return 0
    except (LookupError, ValueError, settings.SettingsError) as error:
        print(error, file=sys.stderr)
        return 2
    except psycopg.Error as error:
        print(f"{type(error).__name__}: {str(error).strip().splitlines()[0]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
