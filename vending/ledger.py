"""The ledger: where the money is, worked out from the tables that already record it.

    python -m vending.ledger open --amount 600.00 --by marshall [--at 2027-01-04T13:00:00Z]
    python -m vending.ledger capital --amount 100.00 --detail "top-up from David" --by marshall
    python -m vending.ledger tokens --cost 0.0421 --model claude-sonnet-5-5 --in 8120 --out 640 --reference msg_01
    python -m vending.ledger remit --amount 8.00 --by marshall [--reference "email 3 Dec"]
    python -m vending.ledger adjust --amount -3.50 --detail "bottle deposit, per David" --by marshall
    python -m vending.ledger show
    python -m vending.ledger entries

The balance is never a stored figure (build plan, section 5). It is:

    opening capital + later capital + takings - receipts - midweek visit fees
    - token spend - tax remitted +/- adjustments

Takings come from the polls' lifetime sales counters for our 20 slots, tax
included, counted from the last good poll at or before the opening entry
(`vending.counters` decides what a reading that goes down means). Receipts are
`purchase_lines.total_paid` on fulfilled requests. A midweek visit fee is
charged for every fulfilled off-cycle request: the fee stored on the request
when it was submitted, or `config.midweek_fee_cents` ($25.00 by default) for
requests that predate that column. Only what has no other record lives in
`ledger_entries`: the one opening entry, later capital, each model call's
cost, tax remittances and adjustments.

The sales tax inside the takings is set aside, not spendable: takings x 8/108
per week, rounded half up to the cent (the team's 19 Oct decision; weeks start
on Saturday, after the Friday visit, in Eastern time). What is still **owed**
is the set-aside less what has been remitted, and the **spendable** balance is
the balance minus that. It is what the planner's balance gate checks against.

Capital and remittances move money without being profit or loss, so the P&L
leaves them out. Adjustments are corrections to the books (a bottle deposit, a
refund), so the P&L counts them. Shrinkage, the cost of units lost without a
sale, comes from `vending.stock` and is its own P&L line.

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

from vending import config, counters, db, stock
from vending.pricing import DEFAULT_TAX_PERCENT
from vending.slots import DEVICE_ID, SLOTS
from vending.tools.sales_history import tax_cents

KINDS = ("opening", "capital", "tokens", "adjustment", "tax_remitted")
# Credits must be positive, debits negative; an adjustment can be either.
CREDITS = ("opening", "capital")
DEBITS = ("tokens", "tax_remitted")
# The planner's default; a `midweek_fee_cents` row in `config` overrides it for
# requests that don't carry their own fee.
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
    capital_cents: Fraction         # top-ups after the opening
    takings: Takings
    receipts_cents: Fraction
    receipt_lines: int
    visit_fees_cents: int
    visits: int
    token_spend_cents: Fraction     # positive: what was spent
    tax_remitted_cents: Fraction    # positive: what was handed over
    adjustments_cents: Fraction     # signed
    notes: tuple[str, ...]

    @property
    def balance_cents(self) -> Fraction:
        return (self.opening_cents + self.capital_cents + self.takings.cents + self.adjustments_cents
                - self.receipts_cents - self.visit_fees_cents - self.token_spend_cents - self.tax_remitted_cents)

    @property
    def tax_owed_cents(self) -> Fraction:
        """Set aside so far, less what has been remitted."""
        return self.takings.tax_cents - self.tax_remitted_cents

    @property
    def spendable_cents(self) -> Fraction:
        return self.balance_cents - self.tax_owed_cents


@dataclass(frozen=True)
class Pnl:
    start: datetime
    end: datetime
    takings: Takings
    units_sold: int                         # every rise in our 20 slots' unit counters in the window
    units_costed: int                       # of those, the units whose cost the receipts know
    cost_of_sales_cents: Fraction           # of the costed units; 0 when nothing sold
    shrinkage_units: int                    # lost without a sale, written off or changed away
    shrinkage_cents: Fraction               # of the shrinkage whose cost is known
    shrinkage_complete: bool
    receipts_cents: Fraction                # cash spent on purchases in the window
    visit_fees_cents: int
    visits: int
    token_spend_cents: Fraction
    adjustments_cents: Fraction
    capital_cents: Fraction                 # shown, not counted: not profit
    tax_remitted_cents: Fraction            # shown, not counted: tax is already outside revenue
    inventory_machine_cents: Fraction | None
    inventory_closet_cents: Fraction | None
    inventory_complete: bool
    notes: tuple[str, ...]

    @property
    def cost_of_sales_complete(self) -> bool:
        return self.units_costed == self.units_sold

    @property
    def revenue_ex_tax_cents(self) -> int:
        return self.takings.cents - self.takings.tax_cents

    @property
    def gross_margin_cents(self) -> Fraction:
        return self.revenue_ex_tax_cents - self.cost_of_sales_cents

    @property
    def net_cents(self) -> Fraction:
        return (self.gross_margin_cents - self.shrinkage_cents - self.visit_fees_cents
                - self.token_spend_cents + self.adjustments_cents)

    @property
    def inventory_cents(self) -> Fraction | None:
        if self.inventory_machine_cents is None and self.inventory_closet_cents is None:
            return None
        return (self.inventory_machine_cents or 0) + (self.inventory_closet_cents or 0)


@dataclass(frozen=True)
class WeekPnl:
    """One week of a P&L window, Saturday to Friday in Eastern time, cut to the window.

    The same lines as `Pnl`, each booked to the week it happened in. The weeks
    of a window add up to `pnl()` over the same window.
    """
    week: date                              # the Saturday the week began, Eastern time
    takings_cents: int
    tax_cents: int
    units_sold: int
    units_costed: int
    cost_of_sales_cents: Fraction           # of the costed units
    shrinkage_units: int
    shrinkage_cents: Fraction               # of the shrinkage whose cost is known
    shrinkage_complete: bool
    visit_fees_cents: int
    visits: int
    token_spend_cents: Fraction
    adjustments_cents: Fraction

    @property
    def cost_of_sales_complete(self) -> bool:
        return self.units_costed == self.units_sold

    @property
    def revenue_ex_tax_cents(self) -> int:
        return self.takings_cents - self.tax_cents

    @property
    def net_cents(self) -> Fraction:
        return (self.revenue_ex_tax_cents - self.cost_of_sales_cents - self.shrinkage_cents - self.visit_fees_cents
                - self.token_spend_cents + self.adjustments_cents)


# --- Money ----------------------------------------------------------------------

def money(cents: Fraction | int | None) -> str | None:
    """Cents as a signed dollar string, rounded half up to the cent: `-12.37`, `600.00`."""
    if cents is None:
        return None
    whole = int(abs(Fraction(cents)) + Fraction(1, 2))
    sign = "-" if cents < 0 and whole else ""     # a tenth of a cent short is 0.00, not -0.00
    return f"{sign}{whole // 100}.{whole % 100:02d}"


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
    The one opening entry is enforced by the database. An entry dated before
    the opening is allowed (development spend, say) but is outside the balance;
    `balance()` says so in a note and the CLI warns.
    """
    if kind not in KINDS:
        raise LedgerError(f"kind must be one of {', '.join(KINDS)}, not {kind!r}")
    if not detail.strip():
        raise LedgerError("an entry needs a detail saying what it is")
    if not by.strip():
        raise LedgerError("an entry needs the name of who recorded it (--by)")
    amount = Fraction(amount_cents)
    if amount == 0:
        raise LedgerError("an entry of 0.00 records nothing")
    if kind in CREDITS and amount < 0:
        raise LedgerError(f"{kind} is a credit: give it as a positive amount")
    if kind in DEBITS and amount > 0:
        raise LedgerError(f"{kind} is a debit: give it as a negative amount")
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
        raise LedgerError("the ledger already has an opening entry; a top-up is a capital entry") from None
    return Entry(row[0], row[1], kind, amount, detail.strip(), reference, by.strip())


def record_tokens(conn: psycopg.Connection, cost_usd: str | Decimal, model: str, input_tokens: int,
                  output_tokens: int, reference: str, by: str = "harness", at: datetime | None = None) -> Entry:
    """What one model call cost, as the API reports it. The harness calls this after every response."""
    cost = dollars_to_cents(cost_usd)
    if cost <= 0:
        raise LedgerError("a model call's cost must be above zero")
    detail = f"{model}: {input_tokens} tokens in, {output_tokens} out"
    return record(conn, "tokens", -cost, detail, by, at, reference)


def record_remittance(conn: psycopg.Connection, amount_usd: str | Decimal, by: str,
                      at: datetime | None = None, reference: str | None = None) -> Entry:
    """Sales tax handed over, as a positive amount. It settles the set-aside; it isn't a cost."""
    amount = dollars_to_cents(amount_usd)
    if amount <= 0:
        raise LedgerError("a remittance must be above zero")
    return record(conn, "tax_remitted", -amount, "sales tax remitted", by, at, reference)


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


def _sum(recorded: Iterable[Entry], kind: str) -> Fraction:
    return sum((e.amount_cents for e in recorded if e.kind == kind), Fraction(0))


# --- Counters -----------------------------------------------------------------------

@dataclass(frozen=True)
class Rise:
    at: datetime
    selection: str
    amount: int


def rises_between(rows: Iterable[tuple[datetime, str, int]], since: datetime, until: datetime
                  ) -> tuple[datetime | None, datetime | None, list[Rise], str | None]:
    """Every rise in a lifetime counter between `since` and `until`, from `(polled_at, selection, value)` rows. Pure.

    The baseline is the last poll at or before `since` (or the first one after,
    with a note), so the rises counted are those at polls after `since` and at
    or before `until`. Returns (baseline poll, last poll counted, rises, note).
    """
    by_poll: dict[datetime, dict[str, int]] = defaultdict(dict)
    for at, selection, value in rows:
        by_poll[at][selection] = value
    times = sorted(t for t in by_poll if t <= until)
    if not times:
        return None, None, [], "no polls in the window, so takings are unknown and counted as 0"
    before = [t for t in times if t <= since]
    note = None
    if before:
        baseline = before[-1]
    else:
        baseline = times[0]
        note = f"no poll at or before {since.isoformat()}, so takings start at the first poll after it"
    series: dict[str, list[tuple[datetime, int]]] = defaultdict(list)
    for t in times:
        if t >= baseline:
            for selection, value in by_poll[t].items():
                series[selection].append((t, value))
    out = [Rise(at, selection, amount)
           for selection, readings in series.items()
           for at, amount in counters.rises(readings)]
    out.sort(key=lambda r: (r.at, r.selection))
    return baseline, times[-1], out, note


def week_of(moment_local: datetime, week_starts: int = WEEK_STARTS) -> date:
    """The day the week holding `moment_local` began: the latest `week_starts` weekday on or before it."""
    return moment_local.date() - timedelta(days=(moment_local.weekday() - week_starts) % 7)


def takings_between(rows: Iterable[tuple[datetime, str, int]], since: datetime, until: datetime,
                    tz: tzinfo, week_starts: int = WEEK_STARTS,
                    tax_percent: int = DEFAULT_TAX_PERCENT) -> Takings:
    """Takings and the tax inside them from `(polled_at, selection, lifetime_sales_cents)` rows. Pure.

    Each rise in a slot's counter is booked to the week of the poll that showed
    it. Tax is rounded per week, then summed.
    """
    baseline, last, rises, note = rises_between(rows, since, until)
    weeks: dict[date, int] = defaultdict(int)
    for r in rises:
        weeks[week_of(r.at.astimezone(tz), week_starts)] += r.amount
    by_week = tuple(WeekTakings(week, cents, tax_cents(cents, tax_percent)) for week, cents in sorted(weeks.items()))
    return Takings(baseline, last, sum(w.takings_cents for w in by_week), sum(w.tax_cents for w in by_week), by_week, note)


def _counter_rows(conn: psycopg.Connection, column: str, until: datetime) -> list[tuple[datetime, str, int]]:
    assert column in ("lifetime_sales_cents", "lifetime_units")
    return conn.execute(
        f"select p.started_at, s.selection, s.{column} from poll_selections s"
        " join polls p using (session_id)"
        " where p.device_id = %s and p.crc_ok and s.selection = any(%s) and p.started_at <= %s",
        (DEVICE_ID, list(SLOTS), until),
    ).fetchall()


def takings(conn: psycopg.Connection, since: datetime, until: datetime) -> Takings:
    return takings_between(_counter_rows(conn, "lifetime_sales_cents", until), since, until, ZoneInfo(TIMEZONE))


def units_sold(conn: psycopg.Connection, since: datetime, until: datetime) -> int:
    """Units sold from all 20 slots in the window, from the unit counters: the figure every cost must account for."""
    _, _, rises, _ = rises_between(_counter_rows(conn, "lifetime_units", until), since, until)
    return sum(r.amount for r in rises)


# --- Balance and P&L ----------------------------------------------------------------

def db_now(conn: psycopg.Connection) -> datetime:
    """Now by the database clock, in UTC.

    This is the clock that stamps an entry recorded without a time. The host clock
    can run behind it (a container, a laptop with Postgres in Docker), and then an
    entry made a moment ago would fall after a cut-off taken from the host.
    `clock_timestamp()`, not `now()`: `now()` stays at the start of an open
    transaction and so misses entries other connections have committed since.
    Converted to UTC because psycopg returns it in the session time zone.
    """
    return conn.execute("select clock_timestamp()").fetchone()[0].astimezone(timezone.utc)


def balance(conn: psycopg.Connection, as_of: datetime | None = None) -> Balance | None:
    """The balance now (or at `as_of`), or None when the ledger has no opening entry.
    "Now" is the database clock (see `db_now`)."""
    start = opening(conn)
    if start is None:
        return None
    if as_of is None:
        as_of = db_now(conn)
    notes: list[str] = []
    sold = takings(conn, start.at, as_of)
    if sold.note:
        notes.append(sold.note)
    receipts, lines, missing = _receipts(conn, start.at, as_of)
    if missing:
        notes.append(f"{missing} receipt lines have no total_paid, so the receipts total leaves them out")
    visits, fees = _visits(conn, start.at, as_of)
    recorded = entries(conn, start.at, as_of)
    early = [e for e in entries(conn, None, start.at) if e.at < start.at]
    dev = [e for e in early if e.kind == "tokens"]
    other = [e for e in early if e.kind != "tokens"]
    if dev:
        notes.append(f"{len(dev)} model-call entries totalling {money(-sum(e.amount_cents for e in dev))} are from "
                     f"before the opening on {start.at:%Y-%m-%d}: development spend, not in the balance")
    if other:
        notes.append(f"{len(other)} entries totalling {money(sum(e.amount_cents for e in other))} are dated before "
                     f"the opening on {start.at:%Y-%m-%d} and are not in the balance; "
                     "re-date them or record them as a capital entry or adjustment if they belong in it")
    return Balance(as_of, start.at, start.amount_cents, _sum(recorded, "capital"), sold, receipts, lines, fees, visits,
                   -_sum(recorded, "tokens"), -_sum(recorded, "tax_remitted"), _sum(recorded, "adjustment"),
                   tuple(notes))


def pnl(conn: psycopg.Connection, start: datetime, end: datetime) -> Pnl:
    """Profit and loss between two moments, with inventory at cost as of `end`."""
    notes: list[str] = []
    sold = takings(conn, start, end)
    if sold.note:
        notes.append(sold.note)
    units = units_sold(conn, start, end)
    receipts, _, missing = _receipts(conn, start, end)
    if missing:
        notes.append(f"{missing} receipt lines have no total_paid, so purchases and cost of sales leave them out")
    visits, fees = _visits(conn, start, end)
    recorded = entries(conn, start, end)

    counted = stock.stock(conn, until=end)
    # A rise is booked to the later poll, so the window's sales are the rises at polls after `start`.
    sales = [s for s in counted.sales if start < s.at <= end]
    units_costed = sum(s.costed_units for s in sales if s.cost_cents is not None)
    cost = sum((s.cost_cents for s in sales if s.cost_cents is not None), Fraction(0))
    if not units:
        notes.append("no sales in the window")
    elif units_costed < units:
        notes.append(f"{units - units_costed} of {units} units sold have no known cost (sold from a slot before we "
                     "first loaded it, past what we loaded, or across a product change the counters can't split), "
                     "so cost of sales and margin leave their cost out")
    if sold.cents and not counted.slots:
        notes.append("the machine sold units but no receipts are stored, so nothing can be costed yet")
    lost = [s for s in counted.shrinkage if start < s.at <= end]
    shrinkage = sum((s.cost_cents for s in lost if s.cost_cents is not None), Fraction(0))
    shrinkage_complete = all(s.cost_cents is not None for s in lost)
    if not shrinkage_complete:
        notes.append("some units lost without a sale have no known cost, so shrinkage leaves them out")
    machine, closet, inventory_complete = _inventory(counted)
    if not inventory_complete:
        notes.append("some units on hand have no known cost, so inventory at cost leaves them out")
    return Pnl(start, end, sold, units, units_costed, cost, sum(s.units for s in lost), shrinkage, shrinkage_complete,
               receipts, fees, visits, -_sum(recorded, "tokens"), _sum(recorded, "adjustment"),
               _sum(recorded, "capital"), -_sum(recorded, "tax_remitted"),
               machine, closet, inventory_complete, tuple(notes))


def pnl_by_week(conn: psycopg.Connection, start: datetime, end: datetime) -> list[WeekPnl]:
    """`pnl()` split into weeks: one row for every week the window touches, quiet weeks included.

    Each line is booked to the week of the moment it happened, read the same
    way `pnl()` reads it, so each column of the weeks sums to `pnl()`'s figure.
    """
    tz = ZoneInfo(TIMEZONE)

    def week(moment: datetime) -> date:
        return week_of(moment.astimezone(tz))

    sold = {w.week: w for w in takings(conn, start, end).by_week}
    units: dict[date, int] = defaultdict(int)
    for r in rises_between(_counter_rows(conn, "lifetime_units", end), start, end)[2]:
        units[week(r.at)] += r.amount
    counted = stock.stock(conn, until=end)
    costed: dict[date, int] = defaultdict(int)
    cost: dict[date, Fraction] = defaultdict(Fraction)
    for s in counted.sales:
        if start < s.at <= end and s.cost_cents is not None:
            costed[week(s.at)] += s.costed_units
            cost[week(s.at)] += s.cost_cents
    lost_units: dict[date, int] = defaultdict(int)
    lost_cost: dict[date, Fraction] = defaultdict(Fraction)
    lost_unknown: set[date] = set()
    for s in counted.shrinkage:
        if start < s.at <= end:
            lost_units[week(s.at)] += s.units
            if s.cost_cents is None:
                lost_unknown.add(week(s.at))
            else:
                lost_cost[week(s.at)] += s.cost_cents
    visits: dict[date, int] = defaultdict(int)
    fees: dict[date, int] = defaultdict(int)
    for at, fee in _visit_rows(conn, start, end):
        visits[week(at)] += 1
        fees[week(at)] += fee
    tokens: dict[date, Fraction] = defaultdict(Fraction)
    adjustments: dict[date, Fraction] = defaultdict(Fraction)
    for e in entries(conn, start, end):
        if e.kind == "tokens":
            tokens[week(e.at)] -= e.amount_cents
        elif e.kind == "adjustment":
            adjustments[week(e.at)] += e.amount_cents

    out = []
    day, last = week(start), week(end)
    while day <= last:
        w = sold.get(day)
        out.append(WeekPnl(day, w.takings_cents if w else 0, w.tax_cents if w else 0, units[day], costed[day],
                           cost[day], lost_units[day], lost_cost[day], day not in lost_unknown, fees[day],
                           visits[day], tokens[day], adjustments[day]))
        day += timedelta(days=7)
    return out


def _receipts(conn: psycopg.Connection, start: datetime, end: datetime) -> tuple[Fraction, int, int]:
    """(total paid in cents, lines counted, lines with no total) for receipt lines resolved in the window.

    Lines go by their own `resolved_at`, the same clock the stock replay uses,
    so a purchase and the sales it costs can't fall in different windows.
    """
    total, lines, missing = conn.execute(
        "select coalesce(sum(l.total_paid), 0), count(l.total_paid), count(*) - count(l.total_paid)"
        " from purchase_lines l join purchase_requests r using (request_id)"
        " where r.status = 'fulfilled' and l.resolved_at >= %s and l.resolved_at <= %s",
        (start, end),
    ).fetchone()
    return Fraction(Decimal(total)) * 100, lines, missing


def _visits(conn: psycopg.Connection, start: datetime, end: datetime) -> tuple[int, int]:
    """(fulfilled off-cycle requests in the window, their fees in total)."""
    found = _visit_rows(conn, start, end)
    return len(found), sum(fee for _, fee in found)


def _visit_rows(conn: psycopg.Connection, start: datetime, end: datetime) -> list[tuple[datetime, int]]:
    """(when, fee) for each fulfilled off-cycle request in the window.

    Each request is charged the fee stored on it at submission; requests from
    before that column existed fall back to `config.midweek_fee_cents`. A visit
    is timed at its first receipt line, like the stock it delivered.
    """
    fee = config.get_value(conn, "midweek_fee_cents", DEFAULT_MIDWEEK_FEE_CENTS)
    if not isinstance(fee, int) or isinstance(fee, bool) or fee < 0:
        fee = DEFAULT_MIDWEEK_FEE_CENTS
    rows = conn.execute(
        "select v.at, v.fee from ("
        " select coalesce((select min(l.resolved_at) from purchase_lines l where l.request_id = r.request_id),"
        "                 r.fulfilled_at) as at, coalesce(r.visit_fee_cents, %s) as fee"
        " from purchase_requests r where r.off_cycle and r.status = 'fulfilled') v"
        " where v.at between %s and %s order by v.at",
        (fee, start, end),
    ).fetchall()
    return [(at, int(cents)) for at, cents in rows]


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
    capital = sub.add_parser("capital", help="record a top-up after the opening (not profit)")
    capital.add_argument("--amount", required=True, help="dollars, such as 100.00")
    capital.add_argument("--detail", required=True, help="where it came from, in a sentence")
    capital.add_argument("--by", required=True)
    capital.add_argument("--reference")
    capital.add_argument("--at", type=datetime.fromisoformat)
    tokens = sub.add_parser("tokens", help="record what one model call cost")
    tokens.add_argument("--cost", required=True, help="dollars, as the API reports it, such as 0.0421")
    tokens.add_argument("--model", required=True)
    tokens.add_argument("--in", dest="input_tokens", type=int, required=True, help="input tokens")
    tokens.add_argument("--out", dest="output_tokens", type=int, required=True, help="output tokens")
    tokens.add_argument("--reference", required=True, help="the API's request or message ID")
    tokens.add_argument("--by", default="harness")
    tokens.add_argument("--at", type=datetime.fromisoformat)
    remit = sub.add_parser("remit", help="record sales tax handed over; it settles the set-aside")
    remit.add_argument("--amount", required=True, help="dollars, such as 8.00")
    remit.add_argument("--by", required=True)
    remit.add_argument("--reference", help="the email or receipt that shows it")
    remit.add_argument("--at", type=datetime.fromisoformat)
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
            elif args.action == "capital":
                entry = record(conn, "capital", dollars_to_cents(args.amount), args.detail, args.by,
                               _aware(args.at), args.reference)
                print(f"Added {money(entry.amount_cents)} of capital: {entry.detail}.")
            elif args.action == "tokens":
                entry = record_tokens(conn, args.cost, args.model, args.input_tokens, args.output_tokens,
                                      args.reference, args.by, _aware(args.at))
                print(f"Recorded {money(-entry.amount_cents)} of token spend: {entry.detail}.")
            elif args.action == "remit":
                entry = record_remittance(conn, args.amount, args.by, _aware(args.at), args.reference)
                print(f"Recorded {money(-entry.amount_cents)} of sales tax remitted.")
            elif args.action == "adjust":
                entry = record(conn, "adjustment", dollars_to_cents(args.amount), args.detail, args.by,
                               _aware(args.at), args.reference)
                print(f"Adjusted by {money(entry.amount_cents)}: {entry.detail}.")
            elif args.action == "show":
                return _show(conn)
            else:
                for e in entries(conn):
                    print(f"{e.id:>4}  {e.at:%Y-%m-%d %H:%M}  {e.kind:12}  {money(e.amount_cents):>10}  {e.detail}"
                          + (f"  [{e.reference}]" if e.reference else "") + f"  by {e.recorded_by}")
                return 0
        except LedgerError as error:
            print(error, file=sys.stderr)
            return 1
        if args.action != "open":
            start = opening(conn)
            if start is not None and entry.at < start.at:
                print(f"Warning: this entry is dated before the opening on {start.at:%Y-%m-%d}, "
                      "so it is not in the balance.", file=sys.stderr)
    return 0


def _show(conn: psycopg.Connection) -> int:
    b = balance(conn)
    if b is None:
        print(OPEN_HINT)
        return 1
    rows = [
        ("Opening capital", b.opening_cents, b.opening_at.strftime("%Y-%m-%d")),
        ("Capital added", b.capital_cents, ""),
        ("Takings (tax included)", b.takings.cents, f"{len(b.takings.by_week)} weeks"),
        ("Receipts", -b.receipts_cents, f"{b.receipt_lines} lines"),
        ("Midweek visit fees", -b.visit_fees_cents, f"{b.visits} visits"),
        ("Token spend", -b.token_spend_cents, ""),
        ("Sales tax remitted", -b.tax_remitted_cents, ""),
        ("Adjustments", b.adjustments_cents, ""),
        ("Balance", b.balance_cents, ""),
        ("Sales tax still owed", -b.tax_owed_cents, f"{money(b.takings.tax_cents)} set aside"),
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
