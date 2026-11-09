"""The ledger: where the money is, worked out from the tables that already record it.

    python -m vending.ledger open --amount 600.00 --by marshall [--at 2027-01-04T13:00:00Z]
    python -m vending.ledger tokens --cost 0.0421 --model claude-sonnet-5-5 --in 8120 --out 640 --reference msg_01
    python -m vending.ledger adjust --amount -3.50 --detail "bottle deposit, per David" --by marshall
    python -m vending.ledger show
    python -m vending.ledger entries

The balance is never a stored figure (build plan, section 5). It is:

    opening capital + takings - receipts - midweek visit fees - token spend +/- adjustments

Takings come from the polls' lifetime sales counters for our 20 slots, tax
included, counted from the last good poll at or before the opening entry.
Receipts are `purchase_lines.total_paid` on fulfilled requests. A midweek visit
fee is charged for every fulfilled off-cycle request (`config.midweek_fee_cents`,
$25.00 by default). Only what has no other record lives in `ledger_entries`:
the one opening entry, each model call's cost, and adjustments.

The sales tax inside the takings is set aside, not spendable: takings x 8/108
per week, rounded half up to the cent (the team's 19 Oct decision; weeks start
on Saturday, after the Friday visit, in Eastern time). The spendable balance is
the balance minus that set-aside, and it is what the planner's balance gate
checks against.

Money is exact (`Fraction` cents, `numeric` in the database) until it is shown.
`money()` rounds half up to the cent only then.

The CLI never migrates: on a database that is behind it refuses to run and
names `python -m vending.db migrate`, like the server and the halt switch.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from vending import db, stock
from vending.pricing import DEFAULT_TAX_PERCENT
from vending.slots import DEVICE_ID, SLOTS
from vending.tools.sales_history import tax_cents

KINDS = ("opening", "tokens", "adjustment")
# The planner's default; a `midweek_fee_cents` row in `config` overrides it.
DEFAULT_MIDWEEK_FEE_CENTS = 2500
# The week turns over on Saturday, after the Friday visit (planner `Config.week_starts`).
WEEK_STARTS = 5
TIMEZONE = "America/New_York"
OPEN_HINT = ("The ledger has no opening entry yet. A person records the starting capital with: "
             "python -m vending.ledger open --amount 600.00 --by NAME")


class LedgerError(ValueError):
    """A record that can't be made: a second opening, a bad amount, no name."""


@dataclass(frozen=True)
class Entry:
    id: int
    at: datetime
    kind: str
    amount_cents: Fraction      # signed: credit positive, debit negative
    detail: str
    reference: str | None
    recorded_by: str


@dataclass(frozen=True)
class WeekTakings:
    week: date                  # the Saturday the week began, Eastern time
    takings_cents: int
    tax_cents: int


@dataclass(frozen=True)
class Takings:
    """Tax-inclusive sales over a window, from the counters, and the tax inside them."""
    start_poll: datetime | None     # the baseline poll the deltas are measured from
    end_poll: datetime | None       # the last poll counted
    cents: int
    tax_cents: int                  # set aside: summed per week, each rounded half up
    by_week: tuple[WeekTakings, ...]
    note: str | None = None


@dataclass(frozen=True)
class Balance:
    as_of: datetime
    opening_at: datetime
    opening_cents: Fraction
    takings: Takings
    receipts_cents: Fraction
    receipt_lines: int
    visit_fees_cents: int
    visits: int
    token_spend_cents: Fraction     # positive: what was spent
    adjustments_cents: Fraction     # signed
    notes: tuple[str, ...]

    @property
    def balance_cents(self) -> Fraction:
        return (self.opening_cents + self.takings.cents + self.adjustments_cents
                - self.receipts_cents - self.visit_fees_cents - self.token_spend_cents)

    @property
    def spendable_cents(self) -> Fraction:
        return self.balance_cents - self.takings.tax_cents


@dataclass(frozen=True)
class Pnl:
    start: datetime
    end: datetime
    takings: Takings
    cost_of_sales_cents: Fraction | None    # None when no sale in the window could be costed
    cost_of_sales_complete: bool            # False when some sold units have no known cost
    units_sold: int                         # from the receipts' slots, since we first loaded them
    units_costed: int
    receipts_cents: Fraction                # cash spent on purchases in the window
    visit_fees_cents: int
    visits: int
    token_spend_cents: Fraction
    adjustments_cents: Fraction
    inventory_machine_cents: Fraction | None
    inventory_closet_cents: Fraction | None
    inventory_complete: bool
    notes: tuple[str, ...]

    @property
    def revenue_ex_tax_cents(self) -> int:
        return self.takings.cents - self.takings.tax_cents

    @property
    def gross_margin_cents(self) -> Fraction | None:
        return None if self.cost_of_sales_cents is None else self.revenue_ex_tax_cents - self.cost_of_sales_cents

    @property
    def net_cents(self) -> Fraction | None:
        margin = self.gross_margin_cents
        if margin is None:
            return None
        return margin - self.visit_fees_cents - self.token_spend_cents + self.adjustments_cents

    @property
    def inventory_cents(self) -> Fraction | None:
        if self.inventory_machine_cents is None and self.inventory_closet_cents is None:
            return None
        return (self.inventory_machine_cents or 0) + (self.inventory_closet_cents or 0)


# --- Money ----------------------------------------------------------------------

def money(cents: Fraction | int | None) -> str | None:
    """Cents as a signed dollar string, rounded half up to the cent: `-12.37`, `600.00`."""
    if cents is None:
        return None
    whole = int(abs(Fraction(cents)) + Fraction(1, 2))
    return f"{'-' if cents < 0 else ''}{whole // 100}.{whole % 100:02d}"


def dollars_to_cents(amount: str | Decimal) -> Fraction:
    """An exact amount in dollars, as given by a person or an API, to cents. Floats are refused."""
    if isinstance(amount, (float, bool)):
        raise LedgerError(f"an amount must be a string or Decimal, not {amount!r}")
    try:
        value = Decimal(str(amount).strip())
    except InvalidOperation:
        raise LedgerError(f"not an amount: {amount!r}") from None
    if not value.is_finite():
        raise LedgerError(f"not an amount: {amount!r}")
    return Fraction(value) * 100


# --- Entries --------------------------------------------------------------------

def record(conn: psycopg.Connection, kind: str, amount_cents: Fraction | int, detail: str, by: str,
           at: datetime | None = None, reference: str | None = None) -> Entry:
    """One ledger entry, logged in `action_log` in the same transaction.

    A person's record, not the agent's, so the halt switch doesn't block it.
    The one opening entry is enforced by the database.
    """
    if kind not in KINDS:
        raise LedgerError(f"kind must be one of {', '.join(KINDS)}, not {kind!r}")
    if not detail.strip():
        raise LedgerError("an entry needs a detail saying what it is")
    if not by.strip():
        raise LedgerError("an entry needs the name of who recorded it (--by)")
    amount = Fraction(amount_cents)
    if kind == "opening" and amount <= 0:
        raise LedgerError("the opening capital must be above zero")
    if kind == "tokens" and amount >= 0:
        raise LedgerError("token spend is a debit: give it as a negative amount")
    try:
        with conn.transaction():
            row = conn.execute(
                "insert into ledger_entries (at, kind, amount_cents, detail, reference, recorded_by)"
                " values (coalesce(%s::timestamptz, now()), %s, %s, %s, %s, %s)"
                " returning id, at",
                (at, kind, _numeric(amount), detail.strip(), reference, by.strip()),
            ).fetchone()
            conn.execute(
                "insert into action_log (kind, tool, arguments, outcome, detail)"
                " values ('write', %s, %s, 'done', %s)",
                (f"ledger.{kind}", Jsonb({"amount": money(amount), "detail": detail.strip(), "reference": reference,
                                          "at": None if at is None else at.isoformat()}),
                 f"{kind} {money(amount)} recorded by {by.strip()}: {detail.strip()}"),
            )
    except psycopg.errors.UniqueViolation:
        raise LedgerError("the ledger already has an opening entry; a top-up is an adjustment") from None
    return Entry(row[0], row[1], kind, amount, detail.strip(), reference, by.strip())


def record_tokens(conn: psycopg.Connection, cost_usd: str | Decimal, model: str, input_tokens: int,
                  output_tokens: int, reference: str, by: str = "harness", at: datetime | None = None) -> Entry:
    """What one model call cost, as the API reports it. The harness calls this after every response."""
    cost = dollars_to_cents(cost_usd)
    if cost < 0:
        raise LedgerError("a model call's cost can't be negative")
    detail = f"{model}: {input_tokens} tokens in, {output_tokens} out"
    return record(conn, "tokens", -cost, detail, by, at, reference)


def entries(conn: psycopg.Connection, start: datetime | None = None, end: datetime | None = None) -> list[Entry]:
    rows = conn.execute(
        "select id, at, kind, amount_cents, detail, reference, recorded_by from ledger_entries"
        " where (%s::timestamptz is null or at >= %s) and (%s::timestamptz is null or at <= %s)"
        " order by at, id",
        (start, start, end, end),
    )
    return [Entry(i, at, kind, Fraction(amount), detail, reference, by) for i, at, kind, amount, detail, reference, by in rows]


def opening(conn: psycopg.Connection) -> Entry | None:
    found = [e for e in entries(conn) if e.kind == "opening"]
    return found[0] if found else None


# --- Takings ----------------------------------------------------------------------

def week_of(moment_local: datetime, week_starts: int = WEEK_STARTS) -> date:
    """The day the week holding `moment_local` began: the latest `week_starts` weekday on or before it."""
    return moment_local.date() - timedelta(days=(moment_local.weekday() - week_starts) % 7)


def takings_between(rows: Iterable[tuple[datetime, str, int]], since: datetime, until: datetime,
                    tz: tzinfo, week_starts: int = WEEK_STARTS,
                    tax_percent: int = DEFAULT_TAX_PERCENT) -> Takings:
    """Takings and the tax inside them from `(polled_at, selection, lifetime_sales_cents)` rows. Pure.

    The baseline is the last poll at or before `since` (or the first one after,
    with a note). Each rise in a slot's counter between two polls is a sale,
    booked to the week of the later poll; a counter that goes down (a board
    swap, a misread) starts a new baseline rather than negative sales, as in
    `vending.stock`. Tax is rounded per week, then summed.
    """
    by_poll: dict[datetime, dict[str, int]] = defaultdict(dict)
    for at, selection, cents in rows:
        by_poll[at][selection] = cents
    times = sorted(t for t in by_poll if t <= until)
    if not times:
        return Takings(None, None, 0, 0, (), "no polls in the window, so takings are unknown and counted as 0")
    before = [t for t in times if t <= since]
    note = None
    if before:
        baseline = before[-1]
    else:
        baseline = times[0]
        note = f"no poll at or before {since.isoformat()}, so takings start at the first poll after it"
    previous = dict(by_poll[baseline])
    weeks: dict[date, int] = defaultdict(int)
    last = baseline
    for t in times:
        if t <= baseline:
            continue
        for selection, cents in by_poll[t].items():
            was = previous.get(selection)
            if was is not None and cents > was:
                weeks[week_of(t.astimezone(tz), week_starts)] += cents - was
            previous[selection] = cents
        last = t
    by_week = tuple(WeekTakings(week, cents, tax_cents(cents, tax_percent)) for week, cents in sorted(weeks.items()))
    return Takings(baseline, last, sum(w.takings_cents for w in by_week), sum(w.tax_cents for w in by_week), by_week, note)


def takings(conn: psycopg.Connection, since: datetime, until: datetime) -> Takings:
    rows = conn.execute(
        "select p.started_at, s.selection, s.lifetime_sales_cents from poll_selections s"
        " join polls p using (session_id)"
        " where p.device_id = %s and p.crc_ok and s.selection = any(%s) and p.started_at <= %s",
        (DEVICE_ID, list(SLOTS), until),
    ).fetchall()
    return takings_between(rows, since, until, ZoneInfo(TIMEZONE))


# --- Balance and P&L ----------------------------------------------------------------

def balance(conn: psycopg.Connection, as_of: datetime | None = None) -> Balance | None:
    """The balance now (or at `as_of`), or None when the ledger has no opening entry."""
    start = opening(conn)
    if start is None:
        return None
    as_of = as_of or datetime.now(timezone.utc)
    notes: list[str] = []
    sold = takings(conn, start.at, as_of)
    if sold.note:
        notes.append(sold.note)
    receipts, lines, missing = _receipts(conn, start.at, as_of)
    if missing:
        notes.append(f"{missing} receipt lines have no total_paid, so the receipts total leaves them out")
    visits, fee = _visits(conn, start.at, as_of)
    recorded = entries(conn, start.at, as_of)
    tokens = -sum((e.amount_cents for e in recorded if e.kind == "tokens"), Fraction(0))
    adjustments = sum((e.amount_cents for e in recorded if e.kind == "adjustment"), Fraction(0))
    return Balance(as_of, start.at, start.amount_cents, sold, receipts, lines, visits * fee, visits,
                   tokens, adjustments, tuple(notes))


def pnl(conn: psycopg.Connection, start: datetime, end: datetime) -> Pnl:
    """Profit and loss between two moments, with inventory at cost as of `end`."""
    notes: list[str] = []
    sold = takings(conn, start, end)
    if sold.note:
        notes.append(sold.note)
    receipts, _, missing = _receipts(conn, start, end)
    if missing:
        notes.append(f"{missing} receipt lines have no total_paid, so purchases and cost of sales leave them out")
    visits, fee = _visits(conn, start, end)
    recorded = entries(conn, start, end)
    tokens = -sum((e.amount_cents for e in recorded if e.kind == "tokens"), Fraction(0))
    adjustments = sum((e.amount_cents for e in recorded if e.kind == "adjustment"), Fraction(0))

    counted = stock.stock(conn, until=end)
    sales = [s for s in counted.sales if start <= s.at <= end]
    units_sold = sum(s.units for s in sales)
    units_costed = sum(s.costed_units for s in sales if s.cost_cents is not None)
    cost = sum((s.cost_cents for s in sales if s.cost_cents is not None), Fraction(0))
    complete = units_costed == units_sold
    if not sales:
        cost_of_sales = None
        notes.append("no sales from slots we have loaded in the window, so cost of sales is unknown"
                     if not counted.slots else "no sales in the window")
    else:
        cost_of_sales = cost
        if not complete:
            notes.append(f"{units_sold - units_costed} of {units_sold} units sold have no known cost, "
                         "so cost of sales and margin are too low by their cost")
    machine, closet, inventory_complete = _inventory(counted)
    if not inventory_complete:
        notes.append("some units on hand have no known cost, so inventory at cost leaves them out")
    if sold.cents and not counted.slots:
        notes.append("the machine sold units but no receipts are stored, so nothing can be costed yet")
    return Pnl(start, end, sold, cost_of_sales, complete, units_sold, units_costed, receipts, visits * fee, visits,
               tokens, adjustments, machine, closet, inventory_complete, tuple(notes))


def _receipts(conn: psycopg.Connection, start: datetime, end: datetime) -> tuple[Fraction, int, int]:
    """(total paid in cents, lines counted, lines with no total) for fulfilled requests in the window."""
    total, lines, missing = conn.execute(
        "select coalesce(sum(l.total_paid), 0), count(l.total_paid), count(*) - count(l.total_paid)"
        " from purchase_lines l join purchase_requests r using (request_id)"
        " where r.status = 'fulfilled' and r.fulfilled_at >= %s and r.fulfilled_at <= %s",
        (start, end),
    ).fetchone()
    return Fraction(Decimal(total)) * 100, lines, missing


def _visits(conn: psycopg.Connection, start: datetime, end: datetime) -> tuple[int, int]:
    """(fulfilled off-cycle requests in the window, the fee for each)."""
    (visits,) = conn.execute(
        "select count(*) from purchase_requests where off_cycle and status = 'fulfilled'"
        " and fulfilled_at >= %s and fulfilled_at <= %s",
        (start, end),
    ).fetchone()
    row = conn.execute("select value from config where key = 'midweek_fee_cents'").fetchone()
    fee = DEFAULT_MIDWEEK_FEE_CENTS if row is None or not isinstance(row[0], int) or isinstance(row[0], bool) else row[0]
    return visits, fee


def _inventory(counted: stock.Stock) -> tuple[Fraction | None, Fraction | None, bool]:
    """(machine, closet) at cost, and whether every unit on hand had a known cost."""
    complete = True
    machine = closet = None
    for s in counted.slots:
        if not s.units:
            continue
        if s.unit_cost is None:
            complete = False
            continue
        machine = (machine or 0) + s.unit_cost * s.units
    for c in counted.closet:
        if c.unit_cost is None:
            complete = False
            continue
        closet = (closet or 0) + c.unit_cost * c.units
    if counted.slots and machine is None and complete:
        machine = Fraction(0)
    if counted.closet and closet is None and complete:
        closet = Fraction(0)
    return machine, closet, complete


def _numeric(cents: Fraction) -> Decimal:
    """An exact `numeric` for the database. Model costs have fractions of a cent, so no rounding."""
    return Decimal(cents.numerator) / Decimal(cents.denominator)


# --- Command line -------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.ledger", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="action", required=True)
    opener = sub.add_parser("open", help="record the opening capital, once")
    opener.add_argument("--amount", required=True, help="dollars, such as 600.00")
    opener.add_argument("--by", required=True, help="your name, for the log")
    opener.add_argument("--at", type=datetime.fromisoformat, help="when the run started (ISO 8601; default now)")
    opener.add_argument("--detail", default="opening capital", help="default: opening capital")
    tokens = sub.add_parser("tokens", help="record what one model call cost")
    tokens.add_argument("--cost", required=True, help="dollars, as the API reports it, such as 0.0421")
    tokens.add_argument("--model", required=True)
    tokens.add_argument("--in", dest="input_tokens", type=int, required=True, help="input tokens")
    tokens.add_argument("--out", dest="output_tokens", type=int, required=True, help="output tokens")
    tokens.add_argument("--reference", required=True, help="the API's request or message ID")
    tokens.add_argument("--by", default="harness")
    tokens.add_argument("--at", type=datetime.fromisoformat)
    adjust = sub.add_parser("adjust", help="a correction: positive credits the account, negative debits it")
    adjust.add_argument("--amount", required=True, help="signed dollars, such as -3.50")
    adjust.add_argument("--detail", required=True, help="why, in a sentence")
    adjust.add_argument("--by", required=True)
    adjust.add_argument("--reference")
    adjust.add_argument("--at", type=datetime.fromisoformat)
    sub.add_parser("show", help="the balance now")
    sub.add_parser("entries", help="every entry, oldest first")
    args = parser.parse_args(argv)

    with db.connect() as conn:
        behind = db.pending(conn)
        if behind:
            print(f"The database is missing migrations: {', '.join(behind)}. "
                  "Check DATABASE_URL points where you mean, then run: python -m vending.db migrate",
                  file=sys.stderr)
            return 2
        try:
            if args.action == "open":
                entry = record(conn, "opening", dollars_to_cents(args.amount), args.detail, args.by,
                               _aware(args.at))
                print(f"Opened with {money(entry.amount_cents)} at {entry.at.isoformat()}, recorded by {entry.recorded_by}.")
            elif args.action == "tokens":
                entry = record_tokens(conn, args.cost, args.model, args.input_tokens, args.output_tokens,
                                      args.reference, args.by, _aware(args.at))
                print(f"Recorded {money(-entry.amount_cents)} of token spend: {entry.detail}.")
            elif args.action == "adjust":
                entry = record(conn, "adjustment", dollars_to_cents(args.amount), args.detail, args.by,
                               _aware(args.at), args.reference)
                print(f"Adjusted by {money(entry.amount_cents)}: {entry.detail}.")
            elif args.action == "show":
                return _show(conn)
            else:
                for e in entries(conn):
                    print(f"{e.id:>4}  {e.at:%Y-%m-%d %H:%M}  {e.kind:10}  {money(e.amount_cents):>10}  {e.detail}"
                          + (f"  [{e.reference}]" if e.reference else "") + f"  by {e.recorded_by}")
        except LedgerError as error:
            print(error, file=sys.stderr)
            return 1
    return 0


def _show(conn: psycopg.Connection) -> int:
    b = balance(conn)
    if b is None:
        print(OPEN_HINT)
        return 1
    rows = [
        ("Opening capital", b.opening_cents, b.opening_at.strftime("%Y-%m-%d")),
        ("Takings (tax included)", b.takings.cents, f"{len(b.takings.by_week)} weeks"),
        ("Receipts", -b.receipts_cents, f"{b.receipt_lines} lines"),
        ("Midweek visit fees", -b.visit_fees_cents, f"{b.visits} visits"),
        ("Token spend", -b.token_spend_cents, ""),
        ("Adjustments", b.adjustments_cents, ""),
        ("Balance", b.balance_cents, ""),
        ("Sales tax set aside", -b.takings.tax_cents, ""),
        ("Spendable", b.spendable_cents, ""),
    ]
    width = max(len(r[0]) for r in rows)
    for label, cents, extra in rows:
        print(f"{label.ljust(width)}  {money(cents):>10}  {extra}")
    for note in b.notes:
        print(f"Note: {note}")
    return 0


def _aware(moment: datetime | None) -> datetime | None:
    """A time given on the command line; one without a zone is taken as UTC."""
    if moment is None or moment.tzinfo is not None:
        return moment
    return moment.replace(tzinfo=timezone.utc)


if __name__ == "__main__":
    sys.exit(main())
