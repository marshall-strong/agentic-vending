"""The ledger: where the money is, worked out from the tables that already record it.

    python -m vending.ledger open --amount 600.00 --by marshall [--at 2027-01-04T13:00:00Z]
    python -m vending.ledger capital --amount 100.00 --detail "top-up from David" --by marshall
    python -m vending.ledger tokens --cost 0.0421 --model claude-sonnet-5-5 --in 8120 --out 640 --reference msg_01
    python -m vending.ledger remit --amount 8.00 --by marshall [--reference "email 3 Dec"]
    python -m vending.ledger adjust --amount -3.50 --detail "bottle deposit, per David" --by marshall
    python -m vending.ledger show
    python -m vending.ledger entries
    python -m vending.ledger reset-test-opening --by marshall [--yes]

The balance is never a stored figure (build plan, section 5). It is:

    opening capital + later capital + takings - receipts - midweek visit fees
    - token spend - tax remitted +/- adjustments

Takings come from the polls' lifetime sales counters for our 20 slots, tax
included, counted from the last good poll at or before the opening entry
(`vending.counters` decides what a reading that goes down means). Receipts are
`purchase_lines.total_paid` on fulfilled requests. A midweek visit fee is
charged for every fulfilled off-cycle request: the fee stored on the request
when it was submitted, or `config.midweek_fee_cents` ($25.00 by default) for
requests that predate that column. Once the opening is recorded, receipts and
visits from before it are test purchases: no figure counts them, whatever
range is asked for, the same boundary the stock count uses
(`stock.receipts_count_from`). Only what has no other record lives in
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

Cost of sales is exact: only sales whose cost the receipts know
(`summarize_sales`). Units sold across a product change the counters can't
split may be either product, so their cost is not exact. They are left out of
cost of sales, which is then incomplete, and reported apart as an estimate
(`units_estimated`, `estimated_cost_of_sales_cents`) that no profit figure
includes (#172 item 5). The stock those sales took out of the count, at the
cost it was taken (`unattributed_stock_cents`), is in no cost, estimate or
profit either. It is its own reconciliation line, so the books still balance:
opening cash and stock + net at known costs + capital + tax set aside - tax
remitted - unattributed stock = closing cash + stock at cost. One timing
exception, older than this line: a unit the counters split to the old product
at a swap leaves stock at the swap but is booked to the poll that measured it,
so a period that ends between the two is off by that unit's cost, which the
period containing the poll makes up; one that holds both balances
(`stock.replay`). Where a write-off
at the same swap may be one of those sales, the line is provisional
(`unattributed_stock_provisional`). The money series counts all of this the
same way.

Counters are read from their whole history first, as the stock replay reads
them, and only then cut to the window (`rises_between`), so a misread or
restart just before a window reads the same from any window.

Money is exact (`Fraction` cents, `numeric` in the database) until it is shown.
`money()` rounds half up to the cent only then.

`reset-test-opening` moves a test opening into `ledger_entries_archive` so the
real one can be recorded (#215, `docs/ledger-reset.md`). It is a dry run
without `--yes`, and refuses once the competition has started, when the
opening's detail doesn't say "test", or when it is dated on or after 29 Nov.

The CLI never migrates: on a database that is behind it refuses to run and
names `python -m vending.db migrate`, like the server and the halt switch.
"""

from __future__ import annotations

import argparse
import re
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
from vending.slots import DEVICE_ID, SlotsNotRecorded, our_slots
from vending.start import START
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
    units_costed: int                       # of those, the units with an exact cost from receipts
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
    units_estimated: int = 0                # sold across a product change the counters can't split; not costed
    estimated_cost_of_sales_cents: Fraction = Fraction(0)   # their estimate: in no profit figure (#172)
    unattributed_stock_cents: Fraction = Fraction(0)        # stock those sales took: a reconciliation line
    unattributed_stock_provisional: bool = False            # a write-off at the swap may be one of them

    @property
    def cost_of_sales_complete(self) -> bool:
        """Every unit sold has an exact cost. False while any is estimated or has no cost at all."""
        return self.units_costed == self.units_sold and not self.units_estimated

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
    units_estimated: int = 0
    estimated_cost_of_sales_cents: Fraction = Fraction(0)   # in no profit figure, as in `Pnl`
    unattributed_stock_cents: Fraction = Fraction(0)        # a reconciliation line, as in `Pnl`
    unattributed_stock_provisional: bool = False

    @property
    def cost_of_sales_complete(self) -> bool:
        return self.units_costed == self.units_sold and not self.units_estimated

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


# --- Resetting the test opening (#215) ---------------------------------------------

# The real opening is recorded on Sun 29 Nov, before the Mon 30 Nov fill. An opening dated before this
# is from testing; one dated on or after it may be the real one, so the reset refuses it.
RESET_CUTOFF = datetime(2026, 11, 29, tzinfo=ZoneInfo(TIMEZONE))
# The test opening's detail says so: "opening capital (test state, 11 Nov; to be reset before launch)".
TEST_MARK = re.compile(r"\btest\b", re.IGNORECASE)
ARCHIVE_REASON = "test opening, reset before the real opening (#215)"


@dataclass(frozen=True)
class ResetPlan:
    """What `reset_test_opening` would do: move `opening` to the archive, and nothing else.

    The rest is what stays where it is and falls before the real opening once
    that is recorded after it: the entries dated at or after the test opening,
    by kind (count, signed total), and the receipt lines and midweek visits
    since the test opening (count, total paid).
    """
    opening: Entry
    now: datetime
    later_entries: dict[str, tuple[int, Fraction]]
    receipts: tuple[int, Fraction]
    visits: tuple[int, int]


def reset_refusal(opened: Entry | None, now: datetime, start: datetime | None = None) -> str | None:
    """Why the opening can't be reset, or None if it can. Pure.

    Three guards, all needed: the competition hasn't started (`vending.start.START`),
    the opening's detail marks it as a test, and it is dated before `RESET_CUTOFF`.
    The real opening is recorded on 29 Nov with a plain detail, so it fails both of
    the last two, and a second reset finds no opening at all.
    """
    start = start or START
    if now >= start:
        return (f"the competition started at {start.isoformat()}, so the opening can't be reset now. "
                "Nothing was changed")
    if opened is None:
        return f"the ledger has no opening entry, so there is nothing to reset. {OPEN_HINT.split('. ', 1)[1]}"
    if not TEST_MARK.search(opened.detail):
        return (f"the opening (entry {opened.id}, {opened.detail!r}) doesn't say it is a test, so it may be the "
                "real one. Nothing was changed")
    if opened.at >= RESET_CUTOFF:
        return (f"the opening (entry {opened.id}) is dated {opened.at.isoformat()}, on or after "
                f"{RESET_CUTOFF.isoformat()}, when the real opening is recorded, so it may be the real one. "
                "Nothing was changed")
    return None


def plan_reset(conn: psycopg.Connection, now: datetime | None = None) -> ResetPlan:
    """What the reset would change, read only. Raises `LedgerError` when it would refuse."""
    now = now or db_now(conn)
    opened = opening(conn)
    why = reset_refusal(opened, now)
    if why:
        raise LedgerError(why)
    later: dict[str, tuple[int, Fraction]] = {}
    for e in entries(conn, opened.at):
        if e.id == opened.id:
            continue
        count, total = later.get(e.kind, (0, Fraction(0)))
        later[e.kind] = (count + 1, total + e.amount_cents)
    paid = [stock.cents(line["total_paid"]) for line in stock.receipt_lines(conn)
            if line["at"] is not None and line["at"] >= opened.at]
    visits = _visit_rows(conn, opened.at, now)
    return ResetPlan(opened, now, later, (len(paid), sum((p for p in paid if p is not None), Fraction(0))),
                     (len(visits), sum(fee for _, fee in visits)))


def reset_test_opening(conn: psycopg.Connection, by: str, now: datetime | None = None) -> Entry:
    """Move the test opening from `ledger_entries` to `ledger_entries_archive`, in one transaction, logged.

    Only the opening moves. Model calls stay, and once the real opening is recorded after them they are
    development spend (`_development`); receipts and visits before it are test purchases
    (`stock.receipts_count_from`). The guards (`reset_refusal`) are checked again with the row locked, so
    two people running it at once can't both move something.
    """
    if not by.strip():
        raise LedgerError("a reset needs the name of who ran it (--by)")
    with conn.transaction():
        row = conn.execute("select id, at, kind, amount_cents, detail, reference, recorded_by from ledger_entries"
                           " where kind = 'opening' for update").fetchone()
        opened = None if row is None else Entry(row[0], row[1], row[2], Fraction(row[3]), row[4], row[5], row[6])
        why = reset_refusal(opened, now or db_now(conn))
        if why:
            raise LedgerError(why)
        conn.execute(
            "insert into ledger_entries_archive (id, at, kind, amount_cents, detail, reference, recorded_by,"
            " recorded_at, archived_by, reason)"
            " select id, at, kind, amount_cents, detail, reference, recorded_by, recorded_at, %s, %s"
            " from ledger_entries where id = %s",
            (by.strip(), ARCHIVE_REASON, opened.id))
        conn.execute("delete from ledger_entries where id = %s", (opened.id,))
        conn.execute(
            "insert into action_log (kind, tool, arguments, outcome, detail) values ('write', %s, %s, 'done', %s)",
            ("ledger.reset_test_opening", Jsonb({"entry": opened.id, "amount": money(opened.amount_cents),
                                                 "at": opened.at.isoformat(), "detail": opened.detail}),
             f"test opening {money(opened.amount_cents)} of {opened.at:%Y-%m-%d} (entry {opened.id}) moved to "
             f"ledger_entries_archive by {by.strip()}"))
    return opened


def archived(conn: psycopg.Connection) -> list[tuple[Entry, datetime, str]]:
    """Entries taken out of the books: (entry, when archived, by whom), oldest first."""
    if conn.execute("select to_regclass('ledger_entries_archive')").fetchone()[0] is None:
        return []
    return [(Entry(i, at, kind, Fraction(amount), detail, reference, by), when, who)
            for i, at, kind, amount, detail, reference, by, when, who in conn.execute(
                "select id, at, kind, amount_cents, detail, reference, recorded_by, archived_at, archived_by"
                " from ledger_entries_archive order by archived_at, id")]


def _development(e: Entry, opened: Entry | None) -> bool:
    """A model call before the opening, or before any opening: development spend, not from the $600 (#104)."""
    return e.kind == "tokens" and (opened is None or e.at < opened.at)


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

    The counters are walked over every row first, and only then cut to the
    window, as the stock replay walks them: a reading just before `since` that
    the history shows is a misread or a restart reads the same from any window,
    so the units counted here are the units the replay costs (#172).
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
        for selection, value in by_poll[t].items():
            series[selection].append((t, value))
    out = [Rise(at, selection, amount)
           for selection, readings in series.items()
           for at, amount in counters.rises(readings) if at > baseline]
    out.sort(key=lambda r: (r.at, r.selection))
    return baseline, times[-1], out, note


def opening_rises(rows: Iterable[tuple[datetime, str, int]], opened_at: datetime, until: datetime
                  ) -> tuple[datetime | None, datetime | None, list[Rise], str | None]:
    """The opening baseline: every rise in a counter from the ledger's opening to `until`, as
    `rises_between` reads it. One rule for the balance (`balance`) and the money chart's cash and tax
    owed (`vending.money_series`), so they agree. The counter is read over its whole history first,
    so a misread just before the opening is not the baseline: cents 25000 -> 24000 | opening | 25250
    took 250 cents since the opening, not 1250 (#172 review round 2).
    """
    return rises_between(rows, opened_at, until)


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
    return _takings(rises_between(rows, since, until), tz, week_starts, tax_percent)


def _takings(read: tuple[datetime | None, datetime | None, list[Rise], str | None], tz: tzinfo,
             week_starts: int = WEEK_STARTS, tax_percent: int = DEFAULT_TAX_PERCENT) -> Takings:
    """`takings_between` from rises already read: (baseline poll, last poll, rises, note)."""
    baseline, last, rises, note = read
    weeks: dict[date, int] = defaultdict(int)
    for r in rises:
        weeks[week_of(r.at.astimezone(tz), week_starts)] += r.amount
    by_week = tuple(WeekTakings(week, cents, tax_cents(cents, tax_percent)) for week, cents in sorted(weeks.items()))
    return Takings(baseline, last, sum(w.takings_cents for w in by_week), sum(w.tax_cents for w in by_week), by_week, note)


def _counter_rows(conn: psycopg.Connection, column: str, until: datetime) -> list[tuple[datetime, str, int]]:
    assert column in ("lifetime_sales_cents", "lifetime_units")
    # Our slots from the stored assignment: raises SlotsNotRecorded rather than count the wrong selections.
    slots = our_slots(conn)
    return conn.execute(
        f"select p.started_at, s.selection, s.{column} from poll_selections s"
        " join polls p using (session_id)"
        " where p.device_id = %s and p.crc_ok and s.selection = any(%s) and p.started_at <= %s",
        (DEVICE_ID, list(slots), until),
    ).fetchall()


def takings(conn: psycopg.Connection, since: datetime, until: datetime) -> Takings:
    return takings_between(_counter_rows(conn, "lifetime_sales_cents", until), since, until, ZoneInfo(TIMEZONE))


def units_sold(conn: psycopg.Connection, since: datetime, until: datetime) -> int:
    """Units sold from all our slots in the window, from the unit counters: the figure every cost must account for."""
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
    # The opening baseline, the same rule as the money chart's cash (`opening_rises`).
    sold = _takings(opening_rises(_counter_rows(conn, "lifetime_sales_cents", as_of), start.at, as_of),
                    ZoneInfo(TIMEZONE))
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


@dataclass(frozen=True)
class SalesCost:
    """What some sales cost: exactly, and apart from that, as an estimate (`summarize_sales`)."""
    units_costed: int
    cost_of_sales_cents: Fraction
    units_estimated: int
    estimated_cost_of_sales_cents: Fraction
    unattributed_stock_cents: Fraction = Fraction(0)
    unattributed_stock_provisional: bool = False
    unattributed_stock_known: bool = False     # some debit's cost is known, $0.00 included


def summarize_sales(sales: Iterable[stock.SaleCost]) -> SalesCost:
    """The cost of sales the P&L, its weeks and the money series all count, by one rule.

    Exact: the sales with a `cost_cents`, their `costed_units` and cost. Estimated:
    the sales with no exact cost but an `estimated_cost_cents` (an unsplit product
    change, #172 item 5), all their units. Those are never in the exact figures, so
    cost of sales is incomplete while there are any. Unattributed stock: what
    sales of unknown product took out of the count (`unattributed_cost_cents`),
    the reconciliation line that keeps the books whole; provisional if a write-off
    at the same change may be one of those sales.
    """
    sales = list(sales)
    exact = [s for s in sales if s.cost_cents is not None]
    guessed = [s for s in sales if s.cost_cents is None and s.estimated_cost_cents is not None]
    taken = [s for s in sales if s.cost_cents is None and s.unattributed_cost_cents is not None]
    return SalesCost(sum(s.costed_units for s in exact), sum((s.cost_cents for s in exact), Fraction(0)),
                     sum(s.units for s in guessed), sum((s.estimated_cost_cents for s in guessed), Fraction(0)),
                     sum((s.unattributed_cost_cents for s in taken), Fraction(0)),
                     # Whether or not the debit's cost is known (#172 review round 2).
                     any(s.may_be_written_off for s in sales if s.cost_cents is None),
                     bool(taken))


def sales_cost_notes(units_sold: int, cost: SalesCost, what: str) -> list[str]:
    """Notes on the units sold whose cost isn't exact; `what` names the figures that leave it out."""
    notes = []
    if cost.units_estimated:
        notes.append(f"{cost.units_estimated} of {units_sold} units sold across a product change the counters "
                     f"can't split, so which product sold is unknown; {what} leave their cost out. It is estimated "
                     f"apart at {money(cost.estimated_cost_of_sales_cents)} (the midpoint of the two products' "
                     "costs); the estimate is in no profit figure")
    unknown = units_sold - cost.units_costed - cost.units_estimated
    if unknown > 0:
        notes.append(f"{unknown} of {units_sold} units sold have no known cost (sold from a slot before we first "
                     "loaded it, past what we loaded, or across a product change with no estimate), "
                     f"so {what} leave their cost out")
    if cost.unattributed_stock_known:
        notes.append(f"{money(cost.unattributed_stock_cents)} of stock at cost left the count with sales of unknown "
                     "product: a reconciliation line, in no cost, estimate or profit, by which net worth falls"
                     + ("; provisional, since units written off at the same swap may be some of those sales"
                        if cost.unattributed_stock_provisional else ""))
    elif cost.unattributed_stock_provisional:
        notes.append("units written off at a swap that sales of unknown product span may be some of those sales, "
                     "so that shrinkage is provisional")
    return notes


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
    opened = opening(conn)
    recorded = entries(conn, start, end)
    # Model calls before the opening are development spend: counting them would make the competition
    # look dearer than it is. Only they are left out; the window's other lines stay (#104).
    dev = money(-sum((e.amount_cents for e in recorded if _development(e, opened)), Fraction(0)))
    recorded = [e for e in recorded if not _development(e, opened)]
    if dev != money(0):
        notes.append(f"{dev} of model calls before the ledger's opening is development spend, not from the $600, "
                     "so token spend and net leave it out" if opened else
                     f"no opening is recorded yet, so every model call ({dev}) is development spend, not from the "
                     "$600, and token spend and net leave it out")

    counted = stock.stock(conn, until=end)
    # A rise is booked to the later poll, so the window's sales are the rises at polls after `start`.
    sales = [s for s in counted.sales if start < s.at <= end]
    cost = summarize_sales(sales)
    if not units:
        notes.append("no sales in the window")
    notes += sales_cost_notes(units, cost, "cost of sales and margin")
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
    return Pnl(start, end, sold, units, cost.units_costed, cost.cost_of_sales_cents, sum(s.units for s in lost),
               shrinkage, shrinkage_complete, receipts, fees, visits, -_sum(recorded, "tokens"),
               _sum(recorded, "adjustment"), _sum(recorded, "capital"), -_sum(recorded, "tax_remitted"),
               machine, closet, inventory_complete, tuple(notes),
               cost.units_estimated, cost.estimated_cost_of_sales_cents,
               cost.unattributed_stock_cents, cost.unattributed_stock_provisional)


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
    weeks_sales: dict[date, list[stock.SaleCost]] = defaultdict(list)
    for s in counted.sales:
        if start < s.at <= end:
            weeks_sales[week(s.at)].append(s)
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
    opened = opening(conn)
    for e in entries(conn, start, end):
        if _development(e, opened):
            continue                                      # left out, as in `pnl()`
        if e.kind == "tokens":
            tokens[week(e.at)] -= e.amount_cents
        elif e.kind == "adjustment":
            adjustments[week(e.at)] += e.amount_cents

    out = []
    day, last = week(start), week(end)
    while day <= last:
        w = sold.get(day)
        cost = summarize_sales(weeks_sales[day])
        out.append(WeekPnl(day, w.takings_cents if w else 0, w.tax_cents if w else 0, units[day], cost.units_costed,
                           cost.cost_of_sales_cents, lost_units[day], lost_cost[day], day not in lost_unknown,
                           fees[day], visits[day], tokens[day], adjustments[day],
                           cost.units_estimated, cost.estimated_cost_of_sales_cents,
                           cost.unattributed_stock_cents, cost.unattributed_stock_provisional))
        day += timedelta(days=7)
    return out


def _receipts(conn: psycopg.Connection, start: datetime, end: datetime) -> tuple[Fraction, int, int]:
    """(total paid in cents, lines counted, lines with no total) for receipt lines in the window.

    Lines go by the receipt clock the stock replay uses (`stock.receipt_lines`:
    door close when the replay uses the receipt's door times, else the line's
    own `resolved_at`), so a purchase and the sales it costs can't fall in
    different windows. Lines before the opening are test purchases and left
    out, at the boundary the stock replay uses (`stock.receipts_count_from`).
    """
    lo = stock.receipts_count_from(conn, start)
    paid = [stock.cents(line["total_paid"]) for line in stock.receipt_lines(conn)
            if line["at"] is not None and lo <= line["at"] <= end]
    known = [p for p in paid if p is not None]
    return sum(known, Fraction(0)), len(known), len(paid) - len(known)


def _visits(conn: psycopg.Connection, start: datetime, end: datetime) -> tuple[int, int]:
    """(fulfilled off-cycle requests in the window, their fees in total)."""
    found = _visit_rows(conn, start, end)
    return len(found), sum(fee for _, fee in found)


def _visit_rows(conn: psycopg.Connection, start: datetime, end: datetime) -> list[tuple[datetime, int]]:
    """(when, fee) for each fulfilled off-cycle request in the window.

    Each request is charged the fee stored on it at submission; requests from
    before that column existed fall back to `config.midweek_fee_cents`. A visit
    is timed like the stock it delivered, at its first line on the receipt
    clock (`stock.request_times`), or at `fulfilled_at` with no lines. So a
    visit timed before the opening brought test purchases and its fee is left
    out (`stock.receipts_count_from`).
    """
    fee = config.get_value(conn, "midweek_fee_cents", DEFAULT_MIDWEEK_FEE_CENTS)
    if not isinstance(fee, int) or isinstance(fee, bool) or fee < 0:
        fee = DEFAULT_MIDWEEK_FEE_CENTS
    times = stock.request_times(stock.receipt_lines(conn))
    lo = stock.receipts_count_from(conn, start)
    rows = []
    for request_id, fulfilled_at, cents in conn.execute(
            "select request_id, fulfilled_at, coalesce(visit_fee_cents, %s) from purchase_requests"
            " where off_cycle and status = 'fulfilled'", (fee,)):
        at = times.get(request_id, fulfilled_at)
        if at is not None and lo <= at <= end:
            rows.append((at, int(cents)))
    return sorted(rows, key=lambda row: row[0])


def _inventory(counted: stock.Stock) -> tuple[Fraction | None, Fraction | None, bool]:
    """(machine, closet) at cost, and whether every unit on hand had a known cost.

    Each count is valued by its layers, so units of unknown cost leave out
    only themselves, not the known units sharing a slot or shelf with them.
    """
    complete = True
    machine = closet = None
    for s in counted.slots:
        for units, each in (s.layers or ((s.units, s.unit_cost),)) if s.units else ():
            if each is None:
                complete = False
                continue
            machine = (machine or 0) + each * units
    for c in counted.closet:
        for units, each in c.layers or ((c.units, c.unit_cost),):
            if each is None:
                complete = False
                continue
            closet = (closet or 0) + each * units
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
    reset = sub.add_parser("reset-test-opening",
                           help="move the test opening out of the books before the real one (#215); a dry run "
                                "unless --yes")
    reset.add_argument("--by", required=True, help="your name, for the log")
    reset.add_argument("--yes", action="store_true", help="make the change; without it, only say what would change")
    sub.add_parser("show", help="the balance now")
    sub.add_parser("entries", help="every entry, oldest first, then any archived ones")
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
            elif args.action == "reset-test-opening":
                return _reset(conn, args.by, args.yes)
            elif args.action == "show":
                return _show(conn)
            else:
                for e in entries(conn):
                    print(_entry_line(e))
                gone = archived(conn)
                if gone:
                    print("Archived, not in the books:")
                    for e, when, who in gone:
                        print(f"{_entry_line(e)}  (archived {when:%Y-%m-%d %H:%M} by {who})")
                return 0
        except (LedgerError, SlotsNotRecorded) as error:
            # SlotsNotRecorded: the balance counts our slots' sales, so `show` needs the assignment
            # (python -m vending.slots record), which Azure lacks between `migrate` and `record`.
            print(error, file=sys.stderr)
            return 1
        if args.action != "open":
            start = opening(conn)
            if start is not None and entry.at < start.at:
                print(f"Warning: this entry is dated before the opening on {start.at:%Y-%m-%d}, "
                      "so it is not in the balance.", file=sys.stderr)
    return 0


def _entry_line(e: Entry) -> str:
    return (f"{e.id:>4}  {e.at:%Y-%m-%d %H:%M}  {e.kind:12}  {money(e.amount_cents):>10}  {e.detail}"
            + (f"  [{e.reference}]" if e.reference else "") + f"  by {e.recorded_by}")


def _reset(conn: psycopg.Connection, by: str, yes: bool) -> int:
    """`reset-test-opening`: say what would change, then change it only with --yes. Times shown in UTC."""
    plan = plan_reset(conn)         # LedgerError when it would refuse; main prints it
    o = plan.opening
    if not yes:
        print("Dry run: nothing was changed. With --yes this moves one entry out of the books, into "
              "ledger_entries_archive:")
    else:
        reset_test_opening(conn, by)
        print("Moved one entry out of the books, into ledger_entries_archive:")
    print(f"  {_entry_line(o)}")
    print("Nothing else moves. Once the real opening is recorded, these fall before it:")
    tokens = plan.later_entries.get("tokens")
    print(f"  model calls since {o.at:%Y-%m-%d}: {tokens[0] if tokens else 0} entries, "
          f"{money(-tokens[1] if tokens else 0)} spent. Development spend: on the dev-spend page, not in the balance")
    print(f"  receipts since {o.at:%Y-%m-%d}: {plan.receipts[0]} lines, {money(plan.receipts[1])} paid. "
          "Test purchases: not in the balance, the P&L or stock")
    print(f"  midweek visits since {o.at:%Y-%m-%d}: {plan.visits[0]}, {money(plan.visits[1])} in fees. "
          "Not in the balance or the P&L")
    for kind, (count, total) in sorted(plan.later_entries.items()):
        if kind != "tokens":
            print(f"  {kind} entries since {o.at:%Y-%m-%d}: {count}, {money(total)}. Not in the balance; "
                  "`ledger show` names them. Re-record any that belong in the competition books")
    if yes:
        print("The ledger has no opening now. Record the real one next: "
              "python -m vending.ledger open --amount 600.00 --by NAME")
    else:
        print(f"To make the change: python -m vending.ledger reset-test-opening --by {by.strip() or 'NAME'} --yes")
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
