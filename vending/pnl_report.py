"""A printable weekly P&L page, read from the ledger.

    python -m vending.pnl_report              # http://127.0.0.1:8767
    python -m vending.pnl_report --port 9000

One row per week, Saturday to Friday in Eastern time, the ledger's week (the
team's 19 Oct decision; the sales tax is rounded per week). Each row shows units
sold, takings with tax, the sales tax set aside, revenue without tax, cost of
sales, shrinkage, midweek visit fees, token spend, adjustments and profit. Under
the weeks are the totals for the range, then the balance, the sales tax still
owed and the spendable balance at the end of the range: the end of its last day
in Eastern time, where the table stops. When the range ends today or later,
they are as of now. Print the page, or save it as a PDF, from the browser.

The page computes nothing: every figure comes from `vending.ledger`
(`pnl_by_week`, `balance`) and `vending.tools.pnl.get_pnl`. Money is shown to
the cent, rounded half up only when shown. A cost the receipts don't know shows
as "unknown", and so does the profit that depends on it.

It reads the database DATABASE_URL points at and never writes: the session is
read-only, each page is one repeatable-read, read-only transaction, so every
figure comes from the same snapshot, and a slow query is cancelled after
STATEMENT_TIMEOUT_MS. It never migrates. It listens on 127.0.0.1 and answers
only a Host of 127.0.0.1 or localhost, so another site can't rebind its name to
us and read the page.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from fractions import Fraction
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit
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

@dataclass(frozen=True)
class Report:
    start: date | None                  # as asked; None means the opening day
    end: date | None                    # as asked; None means today
    printed_at: datetime
    totals: Pnl
    weeks: list[ledger.WeekPnl] = field(default_factory=list)
    balance: AccountBalance | None = None
    balance_at: datetime | None = None  # the end of the range's last day; None means now


def parse(path: str) -> tuple[date | None, date | None]:
    """The date range from the URL. Only `/` is a page; a missing or empty date is the default."""
    parts = urlsplit(path)
    if parts.path != "/":
        raise LookupError(parts.path)
    query = parse_qs(parts.query)
    return _day(query, "start"), _day(query, "end")


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


def load(conn: psycopg.Connection, start: date | None, end: date | None) -> Report:
    """Every figure on the page, from one read-only snapshot."""
    with conn.transaction():
        conn.execute("set transaction isolation level repeatable read, read only")
        # The app's clock, the one get_pnl and ledger.balance use for "today" and "now".
        printed_at = datetime.now(EASTERN)
        totals = get_pnl(conn, start, end)
        if not totals.success:
            return Report(start, end, printed_at, totals)
        first_day, last_day = (t.astimezone(EASTERN).date() for t in (totals.start, totals.end))
        # Count the Sat-to-Fri rows the table would show: a midweek start or end is a part week, but still a row.
        rows = (_week_of(last_day) - _week_of(first_day)).days // 7 + 1
        if rows > MAX_WEEKS:
            refused = Pnl(success=False, message=f"Choose a range of at most {MAX_WEEKS} weeks.")
            return Report(start, end, printed_at, refused)
        weeks = ledger.pnl_by_week(conn, totals.start, totals.end)
        # The balance at the end of the last day shown, where the table stops; now when there is no end
        # date, or the end is today or later.
        balance_at = None if end is None or last_day >= printed_at.date() else totals.end
        return Report(start, end, printed_at, totals, weeks, get_account_balance(conn, balance_at), balance_at)


def _week_of(day: date) -> date:
    """The Saturday that begins the ledger week holding `day`."""
    return ledger.week_of(datetime.combine(day, datetime.min.time()))


# --- Rendering ----------------------------------------------------------------------------------

def _e(value: Any) -> str:
    return escape(str(value), quote=True)


def amount(cents: Fraction | int | None, known: bool = True, note: str = "") -> str:
    """One money cell: the exact figure to the cent, or "unknown" with what is known under it."""
    if not known or cents is None:
        shown = f'<span class="unknown">{UNKNOWN}</span>'
        return shown + (f'<br><small>{_e(note)}</small>' if note else "")
    return _e(ledger.money(cents))


def _text_amount(text: str | None, known: bool = True, note: str = "") -> str:
    """A money cell from a tool reply, which already holds the figure as text."""
    if not known or text is None:
        return f'<span class="unknown">{UNKNOWN}</span>' + (f'<br><small>{_e(note)}</small>' if note else "")
    return _e(text)


def week_label(w: ledger.WeekPnl, first: date, last: date) -> str:
    """The week's days, cut to the range: "2026-11-07 to 2026-11-13"."""
    begin = max(w.week, first)
    finish = min(w.week + timedelta(days=6), last)
    return f"{begin:%a %Y-%m-%d} to {finish:%a %Y-%m-%d}"


def week_row(w: ledger.WeekPnl, first: date, last: date) -> str:
    cost_note = f"{w.units_costed} of {w.units_sold} units costed" if not w.cost_of_sales_complete else ""
    known = w.cost_of_sales_complete and w.shrinkage_complete
    cells = [
        f'<th scope="row">{_e(week_label(w, first, last))}</th>',
        _e(w.units_sold),
        amount(w.takings_cents),
        amount(w.tax_cents),
        amount(w.revenue_ex_tax_cents),
        amount(w.cost_of_sales_cents, w.cost_of_sales_complete, cost_note),
        amount(w.shrinkage_cents, w.shrinkage_complete, f"{w.shrinkage_units} units"),
        amount(w.visit_fees_cents),
        amount(w.token_spend_cents),
        amount(w.adjustments_cents),
        amount(w.net_cents, known),
    ]
    return "<tr>" + "".join(c if c.startswith("<th") else f'<td class="num">{c}</td>' for c in cells) + "</tr>"


def totals_row(t: Pnl, weeks: list[ledger.WeekPnl]) -> str:
    complete = bool(t.cost_of_sales_complete)
    shrinkage_complete = all(w.shrinkage_complete for w in weeks)
    cost_note = "" if complete else f"{t.units_costed} of {t.units_sold} units costed"
    cells = [
        '<th scope="row">Total</th>',
        _e(t.units_sold),
        _text_amount(t.revenue),
        _text_amount(t.tax_set_aside),
        _text_amount(t.revenue_ex_tax),
        _text_amount(t.cost_of_sales, complete, cost_note),
        _text_amount(t.shrinkage, shrinkage_complete, f"{t.shrinkage_units} units"),
        _text_amount(t.visit_fees),
        _text_amount(t.token_spend),
        _text_amount(t.adjustments),
        _text_amount(t.net, complete and shrinkage_complete),
    ]
    return "<tr>" + "".join(c if c.startswith("<th") else f'<td class="num">{c}</td>' for c in cells) + "</tr>"


HEADERS = ("Week (Sat to Fri, Eastern)", "Units sold", "Takings (incl. tax)", "Sales tax set aside",
           "Revenue ex tax", "Cost of sales", "Shrinkage", "Visit fees", "Token spend", "Adjustments", "Profit")


def render(report: Report) -> tuple[int, str]:
    """(HTTP status, page) for a report."""
    form = _form(report.start, report.end)
    printed = f"Printed {report.printed_at:%Y-%m-%d %H:%M %Z}"
    t = report.totals
    if not t.success:
        body = f'{form}<p class="meta">{_e(printed)}</p><p class="error">{_e(t.message)}</p>'
        return 200 if report.start is None and report.end is None else 400, page(body)
    first, last = t.start.astimezone(EASTERN).date(), t.end.astimezone(EASTERN).date()
    head = "".join(f'<th scope="col">{_e(h)}</th>' for h in HEADERS)
    rows = "".join(week_row(w, first, last) for w in report.weeks)
    table = (f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody>'
             f'<tfoot>{totals_row(t, report.weeks)}</tfoot></table></div>'
             f'<p class="meta">{_e(ROUNDING_NOTE)}</p>')
    body = (f'{form}<p class="meta">{_e(f"{first:%a %Y-%m-%d} to {last:%a %Y-%m-%d} (Eastern days)")}'
            f' &middot; {_e(printed)}</p>{table}{_balance(report.balance, report.balance_at)}'
            f'{_notes(t, _shown(report.balance, report.balance_at))}')
    return 200, page(body)


def _form(start: date | None, end: date | None) -> str:
    value = lambda d: "" if d is None else d.isoformat()  # noqa: E731
    return ('<form class="no-print" method="get" action="/">'
            f'<label>From <input type="date" name="start" value="{_e(value(start))}"></label> '
            f'<label>to <input type="date" name="end" value="{_e(value(end))}"></label> '
            '<button type="submit">Show</button> <a href="/">Whole run</a>'
            '<span class="hint">Empty dates mean the whole run: the opening day to today.</span></form>')


def _shown(b: AccountBalance | None, at: datetime | None) -> AccountBalance | None:
    """The balance the page shows: none when it fails, or when the range ends before the ledger opened."""
    if b is None or not b.success:
        return None
    if at is not None and b.opening_at is not None and at < b.opening_at:
        return None
    return b


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
        prefix = "{} now"
    else:
        day = at.astimezone(EASTERN)
        caption = f"At the end of {day:%a %Y-%m-%d} (Eastern), the last day in the range"
        prefix = f"{{}} at the end of {day:%a %Y-%m-%d}"
    rows = [("Balance", b.balance), ("Sales tax still owed", b.tax_owed), ("Spendable", b.spendable)]
    cells = "".join(f'<tr><th scope="row">{_e(prefix.format(label))}</th><td class="num">{_text_amount(text)}</td></tr>'
                    for label, text in rows)
    return f'<h2>{_e(caption)}</h2><table class="balance"><tbody>{cells}</tbody></table>'


def _notes(t: Pnl, b: AccountBalance | None) -> str:
    notes = list(t.notes) + ([n for n in b.notes if n not in t.notes] if b is not None else [])
    if not notes:
        return ""
    return '<h2>Notes</h2><ul class="notes">' + "".join(f"<li>{_e(n)}</li>" for n in notes) + "</ul>"


STYLE = """
:root { color-scheme: light; --ink: #1a1a1a; --muted: #5c5c5c; --rule: #c8c8c8; --band: #f3f3f3; }
body { font: 14px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; color: var(--ink); background: #fff;
       margin: 24px auto; max-width: 1200px; padding: 0 16px; }
h1 { font-size: 22px; margin: 0 0 4px; } h2 { font-size: 16px; margin: 24px 0 6px; }
.meta { color: var(--muted); margin: 0 0 12px; }
form { margin: 0 0 12px; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.hint { color: var(--muted); font-size: 12px; flex-basis: 100%; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th, td { border-bottom: 1px solid var(--rule); padding: 4px 6px; text-align: left; vertical-align: top; }
thead th { font-size: 12px; border-bottom: 2px solid var(--ink); }
td.num { text-align: right; white-space: nowrap; }
tfoot th, tfoot td { border-top: 2px solid var(--ink); font-weight: 600; }
tbody tr:nth-child(even) { background: var(--band); }
table.balance { width: auto; } table.balance th { padding-right: 24px; }
.unknown { font-style: italic; } small { color: var(--muted); }
.error { color: #9b1c1c; font-weight: 600; }
@media print {
  @page { size: landscape; margin: 12mm; }
  body { margin: 0; max-width: none; padding: 0; font-size: 10pt; }
  .no-print { display: none; }
  .scroll { overflow: visible; }
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
            start, end = parse(self.path)
        except LookupError:
            return self._send(404, "text/plain; charset=utf-8", "not found")
        except BadRequest as error:
            return self._send(400, "text/html; charset=utf-8",
                              page(f'{_form(None, None)}<p class="error">{_e(error)}</p>'))
        try:
            with connect() as conn:
                code, body = render(load(conn, start, end))
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
