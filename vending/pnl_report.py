"""A printable weekly P&L page, read from the ledger.

    python -m vending.pnl_report              # http://127.0.0.1:8767
    python -m vending.pnl_report --port 9000

One row per week, Saturday to Friday in Eastern time, the ledger's week (the
team's 19 Oct decision; the sales tax is rounded per week). The simple view
(the default) shows units sold, revenue without tax, cost of sales, midweek visit
fees, token spend, profit and the running total of profit; `?view=detailed` adds
takings with tax, the sales tax set aside, shrinkage and adjustments. Above the
table are headline tiles and a bar chart of weekly profit; under it are the
totals for the range, then the balance, the sales tax still owed and the
spendable balance at the end of the range: the end of its last day in Eastern
time, where the table stops. When the range ends today or later, they are as of
now. Every choice is in the URL, so a shared or printed link shows the same page.
Print it, or save it as a PDF, with the browser's Ctrl+P.

The page computes no figure of its own: every figure comes from `vending.ledger`
(`pnl_by_week`, `balance`) and `vending.tools.pnl.get_pnl`. The one sum it adds
is the running total, the running sum of the ledger's weekly profit, which ends
at the ledger's total. Money is shown to the cent, rounded half up only when
shown. A cost the receipts don't know shows as "unknown", and so does the profit
that depends on it.

It reads the database DATABASE_URL points at and never writes: the session is
read-only, each page is one repeatable-read, read-only transaction, so every
figure comes from the same snapshot, and a slow query is cancelled after
STATEMENT_TIMEOUT_MS. It never migrates. It listens on 127.0.0.1 and answers
only a Host of 127.0.0.1 or localhost, so another site can't rebind its name to
us and read the page.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo

import psycopg

from vending import db, ledger, settings
from vending.tools.account_balance import AccountBalance, get_account_balance
from vending.tools.pnl import Pnl, get_pnl

HOST = "127.0.0.1"
PORT = 8767
STATEMENT_TIMEOUT_MS = 15_000
# Every transaction on this connection is read-only, and no statement runs longer than the timeout.
OPTIONS = f"-c default_transaction_read_only=on -c statement_timeout={STATEMENT_TIMEOUT_MS}"
# Host headers the server answers, so another site can't rebind its name to us and read the page.
LOCAL_HOSTS = ("127.0.0.1", "localhost")
EASTERN = ZoneInfo(ledger.TIMEZONE)
UNKNOWN = "unknown"
# Dates the form accepts, so a mistyped year can't overflow the date arithmetic.
FIRST_DAY, LAST_DAY = date(2000, 1, 1), date(2099, 12, 31)
# The longest range one page shows: three years of weeks.
MAX_WEEKS = 157
ROUNDING_NOTE = ("Each figure is rounded to the cent on its own, so a column can differ from its total "
                 "by a cent. The exact figures add up.")


def _connect() -> psycopg.Connection:
    url = settings.require("DATABASE_URL", "Copy the DATABASE_URL line from .env.example into .env.")
    return psycopg.connect(url, autocommit=True, connect_timeout=db.CONNECT_TIMEOUT, options=OPTIONS)


# Tests swap this to point at the local test database.
connect: Callable[[], psycopg.Connection] = _connect


class BadRequest(ValueError):
    """A query string the page can't use, such as a date that isn't YYYY-MM-DD."""


# --- Data ---------------------------------------------------------------------------------------

VIEWS = ("simple", "detailed")


@dataclass(frozen=True)
class Query:
    """Everything the page shows is chosen in the URL, so a shared or printed link shows the same page."""
    start: date | None                  # None means the opening day
    end: date | None                    # None means today
    view: str = "simple"                # "simple" or "detailed": which columns the table shows


@dataclass(frozen=True)
class Report:
    start: date | None                  # as asked; None means the opening day
    end: date | None                    # as asked; None means today
    printed_at: datetime
    totals: Pnl
    weeks: list[ledger.WeekPnl] = field(default_factory=list)
    balance: AccountBalance | None = None
    balance_at: datetime | None = None  # the end of the range's last day; None means now
    view: str = "simple"


def parse(path: str) -> Query:
    """The page's choices from the URL. Only `/` is a page; a missing or empty value is the default.
    Names the page doesn't use are ignored; a value it can't use is refused."""
    parts = urlsplit(path)
    if parts.path != "/":
        raise LookupError(parts.path)
    query = parse_qs(parts.query)
    view = (query.get("view") or [""])[0].strip() or "simple"
    if view not in VIEWS:
        raise BadRequest(f"view must be simple or detailed, not {view!r}")
    return Query(_day(query, "start"), _day(query, "end"), view)


def _day(query: dict[str, list[str]], name: str) -> date | None:
    value = (query.get(name) or [""])[0].strip()
    if not value:
        return None
    try:
        day = date.fromisoformat(value)
    except ValueError:
        raise BadRequest(f"{name} must be a date as YYYY-MM-DD, not {value!r}") from None
    if not FIRST_DAY <= day <= LAST_DAY:
        raise BadRequest(f"{name} must be between {FIRST_DAY} and {LAST_DAY}, not {value!r}")
    return day


def load(conn: psycopg.Connection, start: date | None, end: date | None, view: str = "simple") -> Report:
    """Every figure on the page, from one read-only snapshot."""
    with conn.transaction():
        conn.execute("set transaction isolation level repeatable read, read only")
        # The app's clock, the one get_pnl and ledger.balance use for "today" and "now".
        printed_at = datetime.now(EASTERN)
        totals = get_pnl(conn, start, end)
        if not totals.success:
            return Report(start, end, printed_at, totals, view=view)
        first_day, last_day = (t.astimezone(EASTERN).date() for t in (totals.start, totals.end))
        # Count the Sat-to-Fri rows the table would show: a midweek start or end is a part week, but still a row.
        rows = (_week_of(last_day) - _week_of(first_day)).days // 7 + 1
        if rows > MAX_WEEKS:
            refused = Pnl(success=False, message=f"Choose a range of at most {MAX_WEEKS} weeks.")
            return Report(start, end, printed_at, refused, view=view)
        weeks = ledger.pnl_by_week(conn, totals.start, totals.end)
        # The balance at the end of the last day shown, where the table stops; now when there is no end
        # date, or the end is today or later.
        balance_at = None if end is None or last_day >= printed_at.date() else totals.end
        return Report(start, end, printed_at, totals, weeks, get_account_balance(conn, balance_at), balance_at, view)


def _week_of(day: date) -> date:
    """The Saturday that begins the ledger week holding `day`."""
    return ledger.week_of(datetime.combine(day, datetime.min.time()))


def presets(now: datetime) -> list[tuple[str, date | None, date | None]]:
    """The range links: (label, start, end) in whole Sat-to-Fri weeks of Eastern days, for `now`.
    The links carry the dates, so a shared link keeps showing the same weeks."""
    saturday = _week_of(now.astimezone(EASTERN).date())
    friday = saturday + timedelta(days=6)
    return [("This week", saturday, friday),
            ("Last 4 weeks", saturday - timedelta(weeks=3), friday),
            ("Whole run", None, None)]


def _known(w: ledger.WeekPnl) -> bool:
    """Whether the week's profit is known: every unit sold or lost has a known cost."""
    return w.cost_of_sales_complete and w.shrinkage_complete


def running_totals(weeks: list[ledger.WeekPnl]) -> list[tuple[Fraction, bool]]:
    """(profit so far, known) through each week: the running sum of the ledger's weekly profit.
    Once a week's profit is unknown, every running total from it on is unknown."""
    out, total, known = [], Fraction(0), True
    for w in weeks:
        total += w.net_cents
        known = known and _known(w)
        out.append((total, known))
    return out


# --- Rendering ----------------------------------------------------------------------------------

def _e(value: Any) -> str:
    return escape(str(value), quote=True)


def dollars(text: str) -> str:
    """A signed dollar string from the ledger ("-1234.56") as shown: "-$1,234.56". Text only, no float."""
    sign = "-" if text.startswith("-") else ""
    whole, _, cents = text.lstrip("-").partition(".")
    return f"{sign}${int(whole):,}.{cents}"


def _unknown(note: str = "") -> str:
    return f'<span class="unknown">{UNKNOWN}</span>' + (f'<br><small>{_e(note)}</small>' if note else "")


def amount(cents: Fraction | int | None, known: bool = True, note: str = "") -> str:
    """One money cell: the exact figure to the cent, or "unknown" with what is known under it."""
    if not known or cents is None:
        return _unknown(note)
    return _e(dollars(ledger.money(cents)))


def _text_amount(text: str | None, known: bool = True, note: str = "") -> str:
    """A money cell from a tool reply, which already holds the figure as text."""
    if not known or text is None:
        return _unknown(note)
    return _e(dollars(text))


def week_label(w: ledger.WeekPnl, first: date, last: date) -> str:
    """The week's days, cut to the range: "2026-11-07 to 2026-11-13"."""
    begin = max(w.week, first)
    finish = min(w.week + timedelta(days=6), last)
    return f"{begin:%a %Y-%m-%d} to {finish:%a %Y-%m-%d}"


# The table's columns: (key, header, shown in the simple view). The detailed view shows them all.
COLUMNS = (
    ("units", "Units sold", True),
    ("takings", "Takings (incl. tax)", False),
    ("tax", "Sales tax set aside", False),
    ("revenue", "Revenue ex tax", True),
    ("cost", "Cost of sales", True),
    ("shrinkage", "Shrinkage", False),
    ("visits", "Visit fees", True),
    ("tokens", "Token spend", True),
    ("adjustments", "Adjustments", False),
    ("profit", "Profit", True),
    ("running", "Running total", True),
)
WEEK_HEADER = "Week (Sat to Fri, Eastern)"
LOSS = '<span class="cue">Loss</span>'


def columns(view: str) -> list[tuple[str, str]]:
    return [(key, header) for key, header, simple in COLUMNS if view == "detailed" or simple]


def _row(label: str, cells: dict[str, str], view: str, css: str = "") -> str:
    tds = "".join(f'<td class="num">{cells[key]}</td>' for key, _ in columns(view))
    return f'<tr{f" class={chr(34)}{css}{chr(34)}" if css else ""}><th scope="row">{label}</th>{tds}</tr>'


def week_row(w: ledger.WeekPnl, first: date, last: date, running: tuple[Fraction, bool],
             view: str = "detailed") -> str:
    """One week; a week that lost money says "Loss" next to its dates, not only in colour."""
    cost_note = f"{w.units_costed} of {w.units_sold} units costed" if not w.cost_of_sales_complete else ""
    known = _known(w)
    lost = _lost(ledger.money(w.net_cents), known)        # by the figure shown: $0.00 is not a loss
    cells = {
        "units": _e(w.units_sold),
        "takings": amount(w.takings_cents),
        "tax": amount(w.tax_cents),
        "revenue": amount(w.revenue_ex_tax_cents),
        "cost": amount(w.cost_of_sales_cents, w.cost_of_sales_complete, cost_note),
        "shrinkage": amount(w.shrinkage_cents, w.shrinkage_complete, f"{w.shrinkage_units} units"),
        "visits": amount(w.visit_fees_cents),
        "tokens": amount(w.token_spend_cents),
        "adjustments": amount(w.adjustments_cents),
        "profit": amount(w.net_cents, known),
        "running": amount(running[0], running[1]),
    }
    days = week_label(w, first, last).split(" to ")       # a narrow screen wraps between the days, not in one
    label = " to ".join(f'<span class="day">{_e(d)}</span>' for d in days) + (f" {LOSS}" if lost else "")
    return _row(label, cells, view, "loss" if lost else "")


def _complete(t: Pnl, weeks: list[ledger.WeekPnl]) -> bool:
    """Whether the range's profit is known."""
    return bool(t.cost_of_sales_complete) and all(w.shrinkage_complete for w in weeks)


def totals_row(t: Pnl, weeks: list[ledger.WeekPnl], view: str = "detailed") -> str:
    complete = bool(t.cost_of_sales_complete)
    shrinkage_complete = all(w.shrinkage_complete for w in weeks)
    cost_note = "" if complete else f"{t.units_costed} of {t.units_sold} units costed"
    cells = {
        "units": _e(t.units_sold),
        "takings": _text_amount(t.revenue),
        "tax": _text_amount(t.tax_set_aside),
        "revenue": _text_amount(t.revenue_ex_tax),
        "cost": _text_amount(t.cost_of_sales, complete, cost_note),
        "shrinkage": _text_amount(t.shrinkage, shrinkage_complete, f"{t.shrinkage_units} units"),
        "visits": _text_amount(t.visit_fees),
        "tokens": _text_amount(t.token_spend),
        "adjustments": _text_amount(t.adjustments),
        "profit": _text_amount(t.net, complete and shrinkage_complete),
        "running": "",                                      # the last week's running total is the total
    }
    return _row("Total", cells, view)


def _lost(text: str | None, known: bool) -> bool:
    return known and text is not None and text.startswith("-")


def _as_shown(cents: Fraction) -> Fraction:
    """Cents rounded as the table shows them, so a bar matches its figure: $0.00 sits on the zero line."""
    return Fraction(Decimal(ledger.money(cents))) * 100


def chart(weeks: list[ledger.WeekPnl], first: date, last: date) -> str:
    """Weekly profit as bars above and below a zero line, in inline SVG (no script). A week whose
    profit is unknown gets a "?" at the zero line instead of a bar. Each bar's <title> gives its figure;
    a scale on the left gives the top and bottom of the plot (and zero, when there is room) in dollars."""
    if not weeks:
        return ""
    width, height, top, bottom, left = 720, 170, 12, 22, 100
    plot = height - top - bottom
    known = [_as_shown(w.net_cents) for w in weeks if _known(w)]
    high, low = max([Fraction(0), *known]), min([Fraction(0), *known])
    span = (high - low) or Fraction(1)
    zero = top + plot * high / span
    slot = Fraction(width - left, len(weeks))
    every = max(1, len(weeks) // 12)                        # at most about 12 date labels
    px = lambda v: f"{float(v):.1f}"  # noqa: E731  (pixels, not money)
    marks = [(Fraction(top), high)] + ([(Fraction(top + plot), low)] if low != high else [])
    if 0 not in (high, low) and min(zero - top, top + plot - zero) >= 26:   # room for a "$0.00" label
        marks.insert(1, (zero, Fraction(0)))
    scale = "".join(f'<line class="grid" x1="{left}" x2="{width}" y1="{px(y)}" y2="{px(y)}"></line>'
                    f'<text class="scale" x="{left - 6}" y="{px(y)}" text-anchor="end" dominant-baseline="middle">'
                    f'{_e(dollars(ledger.money(v)))}</text>' for y, v in marks)
    bars, losing = [], 0
    for i, w in enumerate(weeks):
        label = week_label(w, first, last)
        x = left + slot * i + slot / 10
        if _known(w):
            kind = "neg" if _lost(ledger.money(w.net_cents), True) else "pos"   # as the table shows it
            losing += kind == "neg"
            size = max(plot * abs(_as_shown(w.net_cents)) / span, Fraction(1, 2))   # at least a hairline
            y = zero if kind == "neg" else zero - size
            tip = f"{label}: {dollars(ledger.money(w.net_cents))}" + (" (loss)" if kind == "neg" else "")
            bars.append(f'<rect class="bar {kind}" x="{px(x)}" y="{px(y)}" width="{px(slot * 8 / 10)}" '
                        f'height="{px(size)}"><title>{_e(tip)}</title></rect>')
        else:
            bars.append(f'<text class="bar unknown" x="{px(x + slot * 4 / 10)}" y="{px(zero - 3)}" '
                        f'text-anchor="middle"><title>{_e(f"{label}: profit unknown")}</title>?</text>')
        if i % every == 0:
            bars.append(f'<text class="axis" x="{px(x + slot * 4 / 10)}" y="{height - 6}" text-anchor="middle">'
                        f'{_e(f"{max(w.week, first):%b %d}")}</text>')
    desc = (f"{len(weeks)} weeks from {first:%a %Y-%m-%d} to {last:%a %Y-%m-%d}; {losing} lost money"
            + (f"; {len(weeks) - len(known)} unknown" if len(known) < len(weeks) else "") + ".")
    return (f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
            'aria-labelledby="chart-title chart-desc">'
            '<title id="chart-title">Profit by week</title>'
            f'<desc id="chart-desc">{_e(desc)}</desc>'
            f'{scale}<line class="zero" x1="{left}" x2="{width}" y1="{px(zero)}" y2="{px(zero)}"></line>'
            + "".join(bars) + "</svg>")


def render(report: Report) -> tuple[int, str]:
    """(HTTP status, page) for a report."""
    controls = _controls(report.start, report.end, report.view, report.printed_at)
    printed = f"Printed {report.printed_at:%Y-%m-%d %H:%M %Z}"
    t = report.totals
    if not t.success:
        body = f'{controls}<p class="meta">{_e(printed)}</p><p class="error">{_e(t.message)}</p>'
        return 200 if report.start is None and report.end is None else 400, page(body)
    first, last = t.start.astimezone(EASTERN).date(), t.end.astimezone(EASTERN).date()
    shown = _shown(report.balance, report.balance_at)
    head = f'<th scope="col">{_e(WEEK_HEADER)}</th>' + "".join(
        f'<th scope="col">{_e(h)}</th>' for _, h in columns(report.view))
    rows = "".join(week_row(w, first, last, r, report.view)
                   for w, r in zip(report.weeks, running_totals(report.weeks)))
    hint = ("" if report.view == "detailed" else
            '<p class="meta">Profit also counts shrinkage and adjustments; Detailed shows them.</p>')
    swipe = '<p class="meta swipe no-print">The table is wider than the screen: swipe it sideways for profit.</p>'
    table = (f'{swipe}<div class="scroll"><table class="weeks"><thead><tr>{head}</tr></thead><tbody>{rows}</tbody>'
             f'<tfoot>{totals_row(t, report.weeks, report.view)}</tfoot></table></div>'
             f'{hint}<p class="meta">{_e(ROUNDING_NOTE)}</p>')
    figure = chart(report.weeks, first, last)
    body = (f'{controls}<p class="meta">{_e(f"{first:%a %Y-%m-%d} to {last:%a %Y-%m-%d} (Eastern days)")}'
            f' &middot; {_e(printed)}</p>{_tiles(t, report.weeks, shown, report.balance_at)}'
            + (f'<h2>Profit by week</h2>{figure}' if figure else "")
            + f'<h2>Weeks</h2>{table}{_balance(report.balance, report.balance_at)}{_notes(t, shown)}')
    return 200, page(body)


def _href(start: date | None, end: date | None, view: str) -> str:
    query = [(n, d.isoformat()) for n, d in (("start", start), ("end", end)) if d is not None]
    query += [("view", view)] if view != "simple" else []
    return "/" + (f"?{urlencode(query)}" if query else "")


def _link(label: str, href: str, active: bool) -> str:
    current = ' aria-current="page" class="active"' if active else ""
    return f'<a href="{_e(href)}"{current}>{_e(label)}</a>'


def _controls(start: date | None, end: date | None, view: str, now: datetime) -> str:
    """The date form, the range links, the view switch and how to print. None of it prints."""
    value = lambda d: "" if d is None else d.isoformat()  # noqa: E731
    keep = f'<input type="hidden" name="view" value="{_e(view)}">' if view != "simple" else ""
    form = ('<form class="no-print" method="get" action="/">'
            f'<label>From <input type="date" name="start" value="{_e(value(start))}"></label> '
            f'<label>to <input type="date" name="end" value="{_e(value(end))}"></label> {keep}'
            '<button type="submit">Show</button>'
            '<span class="hint">Empty dates mean the whole run: the opening day to today.</span></form>')
    ranges = " ".join(_link(label, _href(s, e, view), (s, e) == (start, end)) for label, s, e in presets(now))
    views = " ".join(_link(label, _href(start, end, v), v == view)
                     for label, v in (("Simple", "simple"), ("Detailed", "detailed")))
    return (f'<div class="controls no-print">{form}'
            f'<nav aria-label="Range" class="links"><span class="label">Range</span> {ranges}</nav>'
            f'<nav aria-label="Columns" class="links"><span class="label">Columns</span> {views}</nav>'
            '<p class="hint">Press Ctrl+P to print, or to save as a PDF (Cmd+P on a Mac).</p></div>')


def _shown(b: AccountBalance | None, at: datetime | None) -> AccountBalance | None:
    """The balance the page shows: none when it fails, or when the range ends before the ledger opened."""
    if b is None or not b.success:
        return None
    if at is not None and b.opening_at is not None and at < b.opening_at:
        return None
    return b


def _prefix(b: AccountBalance, at: datetime | None) -> str:
    """How a balance figure is labelled: "{} now", or "{} at the end of Tue 2026-11-03"."""
    return "{} now" if at is None else f"{{}} at the end of {at.astimezone(EASTERN):%a %Y-%m-%d}"


def _tiles(t: Pnl, weeks: list[ledger.WeekPnl], b: AccountBalance | None, at: datetime | None) -> str:
    """The headline figures: profit and units for the range, then the balance and spendable at its end."""
    complete = _complete(t, weeks)
    lost = _lost(t.net, complete)
    tiles = [("Profit for the range", _text_amount(t.net, complete) + (f" {LOSS}" if lost else ""),
              "loss" if lost else ""),
             ("Units sold", _e(t.units_sold), "")]
    if b is not None:
        tiles += [(_prefix(b, at).format("Balance"), _text_amount(b.balance), ""),
                  (_prefix(b, at).format("Spendable"), _text_amount(b.spendable), "")]
    cards = "".join(f'<div class="tile{f" {css}" if css else ""}"><div class="tile-label">{_e(label)}</div>'
                    f'<div class="tile-value">{value}</div></div>' for label, value, css in tiles)
    return f'<section class="tiles" aria-label="Headline figures">{cards}</section>'


def _balance(b: AccountBalance | None, at: datetime | None) -> str:
    """The balance, tax still owed and spendable at the end of the range (`at`), or now when `at` is None."""
    if b is None or not b.success:
        return ""
    if _shown(b, at) is None:
        opened = b.opening_at.astimezone(EASTERN)
        return (f'<h2>{_e("No balance for this range")}</h2><p class="meta">'
                f'{_e(f"The range ends before the ledger opened on {opened:%a %Y-%m-%d}.")}</p>')
    if at is None:
        when = b.as_of.astimezone(EASTERN) if b.as_of else None
        caption = f"Now ({when:%Y-%m-%d %H:%M %Z}): the range ends today or later" if when else "Now"
    else:
        caption = f"At the end of {at.astimezone(EASTERN):%a %Y-%m-%d} (Eastern), the last day in the range"
    prefix = _prefix(b, at)
    rows = [("Balance", b.balance), ("Sales tax still owed", b.tax_owed), ("Spendable", b.spendable)]
    cells = "".join(f'<tr><th scope="row">{_e(prefix.format(label))}</th><td class="num">{_text_amount(text)}</td></tr>'
                    for label, text in rows)
    return f'<h2>{_e(caption)}</h2><table class="balance"><tbody>{cells}</tbody></table>'


# The ledger gives this note with the moment as the caller passed it, so the P&L and the balance can
# give the same note with the same moment in two time zones.
_NO_POLL = re.compile(r"^no poll at or before (\S+?),")


def _note_key(note: str) -> str:
    """The note with its "no poll" moment in UTC, so the same note in another time zone is one note."""
    match = _NO_POLL.match(note)
    if match is None:
        return note
    try:
        moment = datetime.fromisoformat(match.group(1))
    except ValueError:
        return note
    if moment.tzinfo is None:
        return note
    return note[:match.start(1)] + moment.astimezone(timezone.utc).isoformat() + note[match.end(1):]


_NO_POLL_NOTE = re.compile(r"^no poll at or before (\S+), so takings start at the first poll after it$")
_NO_POLLS_NOTE = "no polls in the window, so takings are unknown and counted as 0"


def plain_note(note: str) -> str:
    """A ledger note as a sentence a teammate can read: the "poll" notes in plain words, with Eastern time."""
    if note == _NO_POLLS_NOTE:
        return "The machine's sales counters were not read in this range, so takings are unknown and counted as $0.00."
    match = _NO_POLL_NOTE.match(note)
    if match is not None:
        try:
            moment = datetime.fromisoformat(match.group(1))
        except ValueError:
            moment = None
        if moment is not None and moment.tzinfo is not None:
            when = moment.astimezone(EASTERN)
            return (f"The machine's sales counters were first read after {when:%a %Y-%m-%d %H:%M %Z}, so"
                    " takings count from that first reading; any sale before it is not counted.")
    text = note[:1].upper() + note[1:]
    return text if text.endswith(".") else text + "."


def _notes(t: Pnl, b: AccountBalance | None) -> str:
    notes, seen = [], set()
    for note in list(t.notes) + (list(b.notes) if b is not None else []):
        if _note_key(note) not in seen:
            seen.add(_note_key(note))
            notes.append(note)
    if not notes:
        return ""
    return '<h2>Notes</h2><ul class="notes">' + "".join(f"<li>{_e(plain_note(n))}</li>" for n in notes) + "</ul>"


STYLE = """
:root { color-scheme: light dark; --ink: #1a1a1a; --muted: #5c5c5c; --rule: #c8c8c8; --band: #f3f3f3;
        --bg: #ffffff; --tile: #f6f6f6; --link: #0b57d0; --loss: #a8201a; --loss-bg: #fdecea; --gain: #2f6f44;
        --error: #9b1c1c; }
@media (prefers-color-scheme: dark) {
  :root { --ink: #e6e6e6; --muted: #a3a3a3; --rule: #3d3d3d; --band: #1c1c1c; --bg: #121212; --tile: #1e1e1e;
          --link: #8ab4f8; --loss: #ff8a80; --loss-bg: #3a1714; --gain: #7fcf98; --error: #ff8a80; }
}
* { box-sizing: border-box; }
body { font: 14px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; color: var(--ink); background: var(--bg);
       margin: 24px auto; max-width: 1200px; padding: 0 16px; }
a { color: var(--link); }
h1 { font-size: 22px; margin: 0 0 4px; } h2 { font-size: 16px; margin: 24px 0 6px; }
.meta { color: var(--muted); margin: 0 0 12px; }
.controls { margin: 0 0 12px; }
form { margin: 0 0 8px; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.links { display: flex; flex-wrap: wrap; gap: 6px 12px; align-items: baseline; margin: 0 0 6px; }
.links .label { color: var(--muted); font-size: 12px; min-width: 64px; }
.links a.active { color: var(--ink); font-weight: 600; text-decoration: none; border-bottom: 2px solid var(--ink); }
.hint { color: var(--muted); font-size: 12px; flex-basis: 100%; margin: 0; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 8px; margin: 0 0 8px; }
.tile { background: var(--tile); border: 1px solid var(--rule); border-radius: 6px; padding: 8px 12px; }
.tile-label { color: var(--muted); font-size: 12px; }
.tile-value { font-size: 20px; font-weight: 600; font-variant-numeric: tabular-nums; }
.tile.loss .tile-value { color: var(--loss); }
.cue { display: inline-block; font-size: 11px; font-weight: 600; color: var(--loss); border: 1px solid var(--loss);
       border-radius: 3px; padding: 0 4px; vertical-align: middle; }
.chart { display: block; width: 100%; height: auto; }
.chart .bar.pos { fill: var(--gain); } .chart .bar.neg { fill: var(--loss); }
.chart .bar.unknown { fill: var(--muted); font-size: 12px; }
.chart .zero { stroke: var(--ink); stroke-width: 1; vector-effect: non-scaling-stroke; }
.chart .axis, .chart .scale { fill: var(--muted); font-size: 10px; }
.chart .grid { stroke: var(--rule); stroke-dasharray: 3 3; vector-effect: non-scaling-stroke; }
.swipe { display: none; }
@media (max-width: 600px) {
  .chart .axis { font-size: 22px; } .chart .scale { font-size: 20px; } .chart .bar.unknown { font-size: 26px; }
  .swipe { display: block; }
  table.weeks tbody th, table.weeks tfoot th { white-space: normal; min-width: 8.5em; }
}
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th, td { border-bottom: 1px solid var(--rule); padding: 4px 6px; text-align: left; vertical-align: top; }
thead th { font-size: 12px; border-bottom: 2px solid var(--ink); }
td.num { text-align: right; white-space: nowrap; }
.day { white-space: nowrap; }
tbody th { white-space: nowrap; }
tfoot th, tfoot td { border-top: 2px solid var(--ink); font-weight: 600; }
tbody tr:nth-child(even) { background: var(--band); }
tbody tr.loss { background: var(--loss-bg); }
tbody tr.loss td:nth-last-child(2) { color: var(--loss); font-weight: 600; }
table.balance { width: auto; } table.balance th { padding-right: 24px; }
.unknown { font-style: italic; } small { color: var(--muted); }
.error { color: var(--error); font-weight: 600; }
@media print {
  :root { color-scheme: light; --ink: #000; --muted: #444; --rule: #bbb; --band: #fff; --bg: #fff; --tile: #fff;
          --link: #0b57d0; --loss: #8b1a14; --loss-bg: #fff; --gain: #2f6f44; --error: #9b1c1c; }
  @page { size: landscape; margin: 12mm; }
  body { margin: 0; max-width: none; padding: 0; font-size: 9pt; }
  .no-print { display: none; }
  .scroll { overflow: visible; }
  table.weeks { table-layout: auto; width: 100%; font-size: 8pt; }
  tbody th { white-space: normal; }
  .tiles { grid-template-columns: repeat(4, 1fr); }
  .tile-value { font-size: 14pt; }
  .chart { height: 120px; break-inside: avoid; }
  thead { display: table-header-group; } tr { break-inside: avoid; }
  tbody tr:nth-child(even) { background: none; }
  th, td { padding: 2px 4px; }
}
"""


def page(body: str) -> str:
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>Weekly P&amp;L</title><style>{STYLE}</style></head>'
            f'<body><h1>Weekly P&amp;L</h1>{body}</body></html>')


# --- Serving ------------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "vending-pnl-report"

    def do_GET(self) -> None:  # noqa: N802 (the stdlib's name)
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        if host not in LOCAL_HOSTS:
            return self._send(403, "text/plain; charset=utf-8", "open this page as 127.0.0.1 or localhost")
        try:
            query = parse(self.path)
        except LookupError:
            return self._send(404, "text/plain; charset=utf-8", "not found")
        except BadRequest as error:
            return self._send(400, "text/html; charset=utf-8",
                              page(f'{_controls(None, None, "simple", datetime.now(EASTERN))}'
                                   f'<p class="error">{_e(error)}</p>'))
        try:
            with connect() as conn:
                code, body = render(load(conn, query.start, query.end, query.view))
        except (psycopg.Error, settings.SettingsError) as error:
            message = str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__
            return self._send(503, "text/html; charset=utf-8", page(f'<p class="error">{_e(message)}</p>'))
        except Exception as error:  # something we didn't foresee: answer, and keep the details in the log
            print(f"pnl_report: {self.path}: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
            return self._send(500, "text/html; charset=utf-8",
                              page('<p class="error">This page hit an error it doesn\'t handle yet. '
                                   "The details are in the report's terminal.</p>"))
        self._send(code, "text/html; charset=utf-8", body)

    def _send(self, code: int, content_type: str, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 (the stdlib's name)
        pass


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.pnl_report", description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args(argv)
    httpd = ThreadingHTTPServer((HOST, args.port), Handler)
    print(f"Weekly P&L on http://{HOST}:{args.port}  (read-only; Ctrl+C to stop)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
