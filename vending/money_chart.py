"""The money chart: competition money by day, from `vending.money_series`, as one HTML page.

    python -m vending.money_chart --html money-chart.html                  # the opening day to today
    python -m vending.money_chart --html money-chart.html --start 2026-12-03 --end 2027-01-31
    python -m vending.money_chart --json                                    # the data the page carries
    python -m vending.money_chart --demo --html demo.html                   # made-up data, no database

The page (`money_chart.html`) is the money chart from the ideas in #139: running
totals, daily or 7-day rolling values of revenue, costs, profit, cash and net
worth, with series toggles, a date range, zoom, restock, visit, price and AI
markers, and AI spend as a share of gross profit. It carries its data inside
it, so it opens from a file with no server and fetches nothing.

The data is `money_series.daily` from the ledger's opening to the last day
asked for: every figure the ledger counts, competition money only, so model
calls before the opening (development spend, #104) are in none of it. On the
page, cash is the ledger balance less the sales tax still owed, and net worth
is that cash plus stock at cost. When a unit sold has no known cost, gross and
net profit are unknown from that day on and the lines stop there; the page
never draws a guess.

With no opening entry yet there is nothing to chart: `--html` refuses and says
how to record one. `--demo` writes the page with its seeded mock data instead,
marked MOCK, to show the shapes before the competition.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import psycopg

from vending import db, ledger, money_series
from vending.slots import SlotsNotRecorded

PAGE = Path(__file__).with_name("money_chart.html")
_SCRIPT_SAFE = {ord(c): f"\\u{ord(c):04x}" for c in "<>&\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}"}


def data(conn: psycopg.Connection, start: date | None = None, end: date | None = None,
         now: datetime | None = None) -> dict | None:
    """What the page carries: the opening, and one money-series row a day. None with no opening entry.

    `start` defaults to the opening's Eastern day and `end` to today's (the database clock).
    Raises `SlotsNotRecorded` without our slot assignment, as the ledger does.
    """
    opened = ledger.opening(conn)
    if opened is None:
        return None
    now = ledger.db_now(conn) if now is None else now
    start = start or money_series._day(opened.at)
    end = end or money_series._day(now)
    if end < start:
        raise ValueError(f"end ({end}) is before start ({start})")
    days = money_series.daily(conn, start, end, now)
    return {
        # "day" lets the page tell an export that starts at the opening from one that starts later,
        # whose first day has no balance before it in the data.
        "opening": {"at": opened.at.astimezone(timezone.utc).isoformat(), "day": money_series._day(opened.at).isoformat(),
                    "amount": ledger.money(opened.amount_cents), "exact_cents": money_series._exact(opened.amount_cents)},
        "generated_at": now.astimezone(timezone.utc).isoformat(),
        "daily": [money_series.as_json(d) for d in days],
    }


def page(payload: dict | None, template: str | None = None) -> str:
    """The page with `payload` inlined; with None, the page keeps its MOCK data. `template` is the page's
    HTML when the caller changed it first (`live_page`); by default, `PAGE` as it is."""
    html = PAGE.read_text(encoding="utf-8") if template is None else template
    if payload is None:
        return html
    # Inside <script>, "</script" ends the element and "<!--" changes how the rest parses, so no "<",
    # ">" or "&" goes in as itself. U+2028 and U+2029 end lines in older JavaScript. Each becomes its
    # \u escape, which JSON reads back as the same character.
    text = json.dumps(payload, separators=(",", ":")).translate(_SCRIPT_SAFE)
    if "/*DATA*/null" not in html:
        raise RuntimeError(f"{PAGE.name} has lost its /*DATA*/null placeholder")
    return html.replace("/*DATA*/null", text, 1)


# What `live_page` takes out of the page, so a live page carries nothing of the demo: no MOCK generator, no
# made-up figure or event, and no demo-only words. Each is checked to be in the page, so a change to the
# page that moves one fails loudly instead of letting it through.
#
# - Plain swaps, each found exactly once: the mock switch turned off for good, the MOCK tag emptied and
#   hidden, the prototype title, and the comment about the seeded data.
_LIVE = (
    ("const MOCK = !(REAL && REAL.daily && REAL.daily.length);", "const MOCK = false;"),
    ('<span class="mock" id="mockTag">MOCK</span>', '<span class="mock" id="mockTag" hidden></span>'),
    ("<title>Money Chart Prototype v2</title>", "<title>Money chart</title>"),
    ("// With no data (REAL stays null) the page shows seeded MOCK data, so its shapes can be seen before the "
     "competition.\n", ""),
)
# - The banner, whatever it says: emptied and hidden (the script hides it by id, so it stays).
_BANNER = re.compile(r'<div class="banner" id="banner">.*?</div>', re.S)
# - Every `MOCK ? "demo words" : "live words"` between two plain strings: the live words.
_WORDS = re.compile(r'MOCK \? ("(?:[^"\\\n]|\\.)*") : ("(?:[^"\\\n]|\\.)*")')
# - The generator, from its first line to the next line that is only "}": a stub that throws.
_MOCK_START = "function mockDays() {\n"
_MOCK_STUB = 'function mockDays() { throw new Error("this page draws the ledger only"); }\n'


def _live_template() -> str:
    """The page's HTML for a live route, before its data goes in."""
    html = PAGE.read_text(encoding="utf-8")
    for old, new in _LIVE:
        if html.count(old) != 1:
            raise RuntimeError(f"{PAGE.name} has changed: {old!r} isn't there once")
        html = html.replace(old, new)
    html, banners = _BANNER.subn('<div class="banner" id="banner" hidden></div>', html)
    html, words = _WORDS.subn(lambda m: m.group(2), html)
    if banners != 1 or not words:
        raise RuntimeError(f"{PAGE.name} has changed: its MOCK banner or words aren't where live_page looks")
    if html.count(_MOCK_START) != 1:
        raise RuntimeError(f"{PAGE.name} has changed: its MOCK generator isn't there once")
    start = html.index(_MOCK_START)
    end = html.index("\n}\n", start) + 3
    return html[:start] + _MOCK_STUB + html[end:]


class NoData(ValueError):
    """A payload with no days: the live page draws nothing rather than fall back to MOCK data."""


def live_page(payload: dict) -> str:
    """The page for a served route: `payload` inlined (as `data` returns it, or made-up data of the same
    shape from a demo), with nothing of the MOCK demo in it (`_live_template`). Raises `NoData` for a
    payload with no days."""
    if not payload or not payload.get("daily"):
        raise NoData("no days to chart")
    return page(payload, _live_template())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.money_chart", description=__doc__.splitlines()[0])
    parser.add_argument("--html", type=Path, metavar="PATH", help="write the page, data inside it, to PATH")
    parser.add_argument("--json", action="store_true", help="print the data the page carries")
    parser.add_argument("--start", type=date.fromisoformat, help="first Eastern day (default: the opening's)")
    parser.add_argument("--end", type=date.fromisoformat, help="last Eastern day, inclusive (default: today)")
    parser.add_argument("--demo", action="store_true", help="the page's made-up MOCK data; no database")
    args = parser.parse_args(argv)
    if not args.html and not args.json:
        parser.error("give --html PATH, --json, or both")
    if args.demo:
        if args.json or args.start or args.end:
            parser.error("--demo only writes the page (--html)")
        args.html.write_text(page(None), encoding="utf-8")
        print(f"wrote {args.html} (MOCK data)")
        return 0
    with db.connect() as conn:
        behind = db.pending(conn)
        if behind:
            print(f"The database is missing migrations: {', '.join(behind)}. "
                  "Check DATABASE_URL points where you mean, then run: python -m vending.db migrate", file=sys.stderr)
            return 2
        try:
            payload = data(conn, args.start, args.end)
        except (SlotsNotRecorded, ValueError) as error:
            print(error, file=sys.stderr)
            return 1
    if payload is None:
        print(ledger.OPEN_HINT + " Or use --demo for the page with made-up data.", file=sys.stderr)
        return 1
    if args.html:
        args.html.write_text(page(payload), encoding="utf-8")
        print(f"wrote {args.html} ({len(payload['daily'])} days from the ledger)")
    if args.json:
        print(json.dumps(payload, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
