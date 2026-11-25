"""Competition money by Eastern calendar day, and by ledger week, worked out from the ledger.

    python -m vending.money_series                                   # the opening day to today
    python -m vending.money_series --start 2026-12-03 --end 2027-01-31 [--weekly] [--json]

One row per day of what `vending.ledger` counts: takings and the tax inside
them, revenue, cost of sales, shrinkage, visit fees, token spend and
adjustments, so gross profit and net; purchases, capital and tax remitted,
which move cash without being profit or loss; and at the day's end the cash
balance, the sales tax still owed in it, inventory at cost, and net worth (the
balance less that tax, plus inventory at cost). Each day also lists its events
(visits, receipt lines, model calls, observed price changes) for a chart to
mark, and notes for whatever it couldn't count. `by_week` adds the days up into
the ledger's Saturday-to-Friday display weeks. This is the data under the money chart,
the weekly report and the agent's briefing. It reads; it decides nothing.

It is competition money only. Counting starts at the ledger's opening entry,
as `ledger.balance` does: model calls before it are development spend (#104)
and are in no figure here (the agent may know what testing cost; it just never
comes out of the $600). Days before the opening, and every day while there is
no opening, are rows of zeros with a note saying why.

Each line is booked to the Eastern day of the moment the ledger books it at: a
sale to the poll that showed it, a purchase to its receipt line, a visit to its
first receipt line, an entry to its time. So a sale first seen at a poll just
after midnight is in the new day, even if it happened the evening before, and
a day with no good poll shows no sales, which then appear on the next day that
has one (a note says so). A day runs from midnight to midnight Eastern, so
1 Nov 2026, when the clocks went back, has 25 hours. The opening day runs from
the opening: entries at or after it, sales at polls after it, the poll at it
being the baseline, as in the balance.

The days add up, line by line, to `ledger.pnl` over the same window: from the
opening, or from one microsecond before the first midnight when the window
starts later (so a poll at midnight sharp counts), to the last day's end. An
entry stamped in that first microsecond belongs to the day before and is left
out. Each day's cash balance is `ledger.balance` at the day's end. Tax needs
care: the ledger rounds the tax inside each tax week's takings, not each
sale's, so a day's tax is what the day added to its tax week's rounded tax. Tax
weeks run Sunday to Saturday, as David calculates the tax (David, 20 Nov;
`ledger.tax_week_of`), and a day is always inside one. A tax week cut by the
window is rounded on its days inside it, as `ledger.pnl` does, so a window that
starts midweek can put a cent of tax on a different day than one that starts at
the opening. The display weeks (`by_week`, Saturday to Friday) add up their
days' tax, so they carry their share of the tax weeks' rounding.

Cost of sales is `ledger.summarize_sales`: exact costs only. Units sold across
a product change the counters can't split have no exact cost; their estimate
is `units_estimated` and `estimated_cost_of_sales_cents`, reported apart and
in no profit figure, known or not (#172 item 5). The stock those sales took
out of the count is `unattributed_stock_cents`, the ledger's reconciliation
line: in no cost, estimate or profit, so each day's cash and stock at cost
equal the day before's plus net at known costs, capital and the tax set
aside, less tax remitted and that line. Except for timing: a unit the counters
split to the old product at a swap leaves stock at the swap but is booked to
the poll that measured it, so a day (or a week) that ends between the two is
off by that unit's cost, and the period containing the poll makes it up; the whole run
balances. `unattributed_stock_provisional` says a write-off at the same swap
may be one of those sales.

When some units sold, or some units lost, have no known cost, a profit that
leaves them out looks complete and isn't. So `gross_profit_cents` and
`net_cents` are None then, as on the dashboard's money panel, and
`gross_profit_known_cents` and `net_known_cents` carry the figures over the
costs we do know, beside the flags that say which are missing. A running
total (`cumulative`) of an unknown figure is unknown from the first unknown day.

A fulfilled receipt line with no `total_paid` is left out of what was paid, as
`ledger.balance` leaves it out. So `purchases_cents` is the priced lines only and
`purchases_complete` is false on its day, and `cash_balance_cents` is over by
what the line cost and `cash_complete` is false from its day on (the line is
still unpriced on every later day). Net worth is None while it is. A week takes
`purchases_complete` from all its days and `cash_complete` from its last day.

Takings with no poll behind them are counted as 0, as the ledger counts them.
A note says so in the JSON and in the table.

Everything is read once for the whole window: the counters, the entries, the
receipts and every stock movement. A day's inventory is a replay of the
movements up to its end, which is what `stock.stock(until=...)` does, without
going back to the database. Asking `ledger.pnl` and `ledger.balance` for each
day instead reads all of history again every day: on 70 days of hourly polls
of six slots that took 21 s, and this takes about a second.

JSON (`--json`, or `as_json` for one row): money as dollar strings rounded half
up to the cent, as `ledger.money` gives them, so they read as shown but don't
always add up to the cent. `exact_cents` beside them has every money figure as
an exact string, a whole number of cents or a fraction ("6185/11"), which
`Fraction(text)` reads back, for anything that adds rows up. An unknown figure
is null in both. Times are UTC, ISO 8601; dates are Eastern.

Counters are read from their whole history, as the stock replay reads them,
then cut to the window (`ledger.rises_between`), so a window that starts just
after a counter misread reads it as a misread too. The cash balance and tax
owed always count from the opening.

Known limit, the ledger's, so the series keeps agreeing with it until it is fixed there:

- A receipt line with no `total_paid` whose units were all lost replays with
  no shrinkage event in `vending.stock`, so its unknown cost doesn't mark net
  unknown.

The CLI never migrates: on a database that is behind it refuses and names
`python -m vending.db migrate`, like the ledger's.
"""

from __future__ import annotations

import argparse
import json
import sys
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from fractions import Fraction
from zoneinfo import ZoneInfo

import psycopg

from vending import db, ledger, stock
from vending.pricing import DEFAULT_TAX_PERCENT
from vending.slots import SlotsNotRecorded, our_slots
from vending.tools.machine_state import price_history
from vending.tools.sales_history import tax_cents

EASTERN = ZoneInfo(ledger.TIMEZONE)
MICROSECOND = timedelta(microseconds=1)
NO_OPENING = "the ledger has no opening entry, so none of this is competition money yet"
# A receipt line's marker for a slot that isn't ours now: no slot, no product.
NOT_OURS_LINE = "a line for a selection that isn't ours now"
NO_GOOD_POLL = "no good poll this day, so its sales show on the next day that has one"
# How the notes about the polls behind the takings begin: ours above, and `ledger.rises_between`'s
# ("no polls in the window, ..." and "no poll at or before ..."). The table shows these in full.
POLL_NOTES = ("no good poll", "no polls", "no poll at or before")

# The lines a day has and a week adds up. Each is summed exactly, so a week's are its days'.
FLOWS = ("takings_cents", "sales_tax_cents", "units_sold", "units_costed", "cost_of_sales_cents",
         "shrinkage_units", "shrinkage_cents", "visits", "visit_fees_cents", "token_spend_cents",
         "adjustments_cents", "purchases_cents", "unpaid_lines", "capital_cents", "tax_remitted_cents",
         "units_estimated", "estimated_cost_of_sales_cents", "unattributed_stock_cents")
# What `cumulative` can run: the flows and the figures worked out from them.
RUNNING = FLOWS + ("revenue_ex_tax_cents", "gross_profit_known_cents", "net_known_cents",
                   "gross_profit_cents", "net_cents")


@dataclass(frozen=True)
class Event:
    """Something on a day a chart can mark. `amount_cents` is the money it moved, when it moved some."""
    at: datetime
    kind: str                       # restock_visit | midweek_visit | receipt | tokens | price_change
    detail: str
    reference: str | None = None    # the request ID; for tokens, the call's reference when there was one call
    amount_cents: Fraction | None = None


class _Lines:
    """The profit arithmetic a day and a week share, from the lines they both carry.

    The same sums as `ledger.Pnl`, except that gross profit and net are None
    when a cost is missing; the `_known` figures are what `ledger.Pnl` shows.
    """

    @property
    def revenue_ex_tax_cents(self) -> int:
        return self.takings_cents - self.sales_tax_cents

    @property
    def cost_of_sales_complete(self) -> bool:
        """Every unit sold has an exact cost: none estimated, none without a cost."""
        return self.units_costed == self.units_sold and not self.units_estimated

    @property
    def costs_complete(self) -> bool:
        """Every unit sold and every unit lost has a known cost, so gross profit and net are whole figures."""
        return self.cost_of_sales_complete and self.shrinkage_complete

    @property
    def gross_profit_known_cents(self) -> Fraction:
        """Revenue less the cost of the units sold whose cost we know."""
        return self.revenue_ex_tax_cents - self.cost_of_sales_cents

    @property
    def net_known_cents(self) -> Fraction:
        """Net over the known costs: what `ledger.Pnl.net_cents` gives, whole or not."""
        return (self.gross_profit_known_cents - self.shrinkage_cents - self.visit_fees_cents
                - self.token_spend_cents + self.adjustments_cents)

    @property
    def gross_profit_cents(self) -> Fraction | None:
        return self.gross_profit_known_cents if self.cost_of_sales_complete else None

    @property
    def net_cents(self) -> Fraction | None:
        return self.net_known_cents if self.costs_complete else None

    @property
    def purchases_complete(self) -> bool:
        """Every receipt line in the row has a `total_paid`, so `purchases_cents` is the whole figure."""
        return self.unpaid_lines == 0

    @property
    def cash_complete(self) -> bool:
        """Every receipt line since the opening has a `total_paid`, so the cash balance is the whole figure.

        A line without one leaves the balance over by what it cost, from its day on, as in `ledger.balance`.
        """
        return self.cash_unpaid_lines == 0

    @property
    def net_worth_cents(self) -> Fraction | None:
        """Spendable cash plus inventory at cost; None unless both are known and complete.

        Spendable is `ledger.Balance.spendable_cents`: the balance less the sales tax still owed. That
        tax was never ours, so counting it would overstate what the business is worth.
        """
        if (self.cash_balance_cents is None or not self.cash_complete or self.tax_owed_cents is None
                or self.inventory_cents is None or not self.inventory_complete):
            return None
        return self.cash_balance_cents - self.tax_owed_cents + self.inventory_cents


@dataclass(frozen=True)
class Day(_Lines):
    """One Eastern calendar day of competition money: the day's flows, and the balances at its end."""
    date: date
    counted_from: datetime | None   # midnight, or the opening on the opening day; None when not counted
    ended: bool                     # the day was over when the series was read
    takings_cents: int              # tax included, from the polls' counters
    sales_tax_cents: int            # what the day added to its tax week's rounded set-aside
    units_sold: int                 # every rise in our slots' unit counters
    units_costed: int               # of those, the units whose cost the receipts know
    cost_of_sales_cents: Fraction   # of the costed units
    shrinkage_units: int
    shrinkage_cents: Fraction       # of the lost units whose cost is known
    shrinkage_complete: bool
    visits: int                     # fulfilled midweek visits
    visit_fees_cents: int
    token_spend_cents: Fraction
    adjustments_cents: Fraction     # signed
    purchases_cents: Fraction       # receipts: cash out now, cost of sales as the units sell; the priced lines
    unpaid_lines: int               # receipt lines with no total_paid, which purchases leave out
    capital_cents: Fraction         # cash in, not profit
    tax_remitted_cents: Fraction    # cash out, not a cost: the tax was never revenue
    cash_balance_cents: Fraction | None     # `ledger.balance` at the day's end; None when not counted
    cash_unpaid_lines: int                  # receipt lines since the opening with no total_paid: it leaves them out
    tax_owed_cents: Fraction | None         # its sales tax set aside since the opening, less remitted
    inventory_cents: Fraction | None        # machine and closet at cost, at the day's end
    inventory_complete: bool
    events: tuple[Event, ...]
    notes: tuple[str, ...]
    units_estimated: int = 0                        # sold across a product change the counters can't split
    estimated_cost_of_sales_cents: Fraction = Fraction(0)   # their estimate: in no profit figure (#172)
    unattributed_stock_cents: Fraction = Fraction(0)        # stock those sales took: a reconciliation line
    unattributed_stock_provisional: bool = False            # a write-off at the swap may be one of them

    @property
    def counted(self) -> bool:
        return self.counted_from is not None

    @property
    def whole(self) -> bool:
        """Counted from midnight to midnight: over, and not cut short by the opening."""
        return self.counted_from is not None and self.ended and self.counted_from <= _midnight(self.date)


@dataclass(frozen=True)
class Week(_Lines):
    """One ledger week, Saturday to Friday in Eastern time, added up from its days in the series.

    Its flows are the sums of its days', so they reconcile to `ledger.pnl` over
    the same days. Week 1 is the week of the opening, however few of its days
    are left by then: an opening on Thursday 3 Dec makes it two days long.
    """
    week: date                      # the Saturday it began
    number: int | None              # week of the competition, 1 holding the opening; None before it
    first_day: date | None          # its first and last counted days in the series; None if it has none
    last_day: date | None
    days: int                       # days counted, 0 to 7
    partial: bool                   # fewer than 7 whole days: cut by the opening, the window or today
    takings_cents: int
    sales_tax_cents: int
    units_sold: int
    units_costed: int
    cost_of_sales_cents: Fraction
    shrinkage_units: int
    shrinkage_cents: Fraction
    shrinkage_complete: bool
    visits: int
    visit_fees_cents: int
    token_spend_cents: Fraction
    adjustments_cents: Fraction
    purchases_cents: Fraction
    unpaid_lines: int
    capital_cents: Fraction
    tax_remitted_cents: Fraction
    cash_balance_cents: Fraction | None     # at the end of its last day in the series
    cash_unpaid_lines: int
    tax_owed_cents: Fraction | None
    inventory_cents: Fraction | None
    inventory_complete: bool
    notes: tuple[str, ...]
    units_estimated: int = 0
    estimated_cost_of_sales_cents: Fraction = Fraction(0)
    unattributed_stock_cents: Fraction = Fraction(0)
    unattributed_stock_provisional: bool = False


# --- Days ----------------------------------------------------------------------------

def daily(conn: psycopg.Connection, start: date, end: date, now: datetime | None = None) -> list[Day]:
    """One row for every Eastern day from `start` to `end`, inclusive, quiet days included.

    `now` (default: the database clock, as the balance uses) only decides which
    days are over. Raises `SlotsNotRecorded` without our slot assignment, as
    the ledger does, rather than count the wrong selections.
    """
    if end < start:
        raise ValueError("end must be on or after start")
    now = ledger.db_now(conn) if now is None else now
    dates = [start + timedelta(days=n) for n in range((end - start).days + 1)]
    opened = ledger.real_opening(conn)
    until = _midnight(end + timedelta(days=1)) - MICROSECOND
    if opened is None or opened.at > until:
        why = NO_OPENING if opened is None else _before_opening(opened)
        return [_not_counted(d, why, now) for d in dates]
    first = max(_midnight(start), opened.at)                    # the first moment counted
    # `ledger.pnl`'s poll start for this window (`ledger.poll_start`). Sales count at polls after it: the
    # poll at the opening is the baseline, and a poll at midnight sharp counts.
    since = ledger.poll_start(first, opened.at)
    book = _Book(conn, opened, first, since, until)

    out = []
    week_takings: dict[date, int] = defaultdict(int)
    for d in dates:
        if _midnight(d + timedelta(days=1)) <= first:
            out.append(_not_counted(d, _before_opening(opened), now))
            continue
        out.append(book.day(d, week_takings, now))
    return out


class _Book:
    """Everything the window's days need, read from the database once, and each day cut from it."""

    def __init__(self, conn: psycopg.Connection, opened: ledger.Entry, first: datetime, since: datetime,
                 until: datetime) -> None:
        self.opened, self.first, self.since, self.until = opened, first, since, until
        self.takings: dict[date, int] = defaultdict(int)
        self.units: dict[date, int] = defaultdict(int)
        self.sales: dict[date, ledger.SalesCost] = {}
        self.lost_units: dict[date, int] = defaultdict(int)
        self.lost_cost: dict[date, Fraction] = defaultdict(Fraction)
        self.lost_unknown: set[date] = set()
        self.visits: dict[date, int] = defaultdict(int)
        self.fees: dict[date, int] = defaultdict(int)
        self.purchases: dict[date, Fraction] = defaultdict(Fraction)
        self.unpaid_lines: dict[date, int] = defaultdict(int)
        self.unpaid_moments: list[datetime] = []     # every unpriced line since the opening, for the cash
        self.entries: dict[tuple[date, str], Fraction] = defaultdict(Fraction)
        self.events: dict[date, list[Event]] = defaultdict(list)
        # Every cash movement since the opening, (when, signed cents), for the balance at each day's end.
        self.cash: list[tuple[datetime, Fraction]] = []

        # Takings and units: the ledger's own rows and rises, so the counter rules are the same.
        sales_rows = ledger._counter_rows(conn, "lifetime_sales_cents", until)
        _, _, rises, self.poll_note = ledger.rises_between(sales_rows, since, until)
        for r in rises:
            self.takings[_day(r.at)] += r.amount
        # Cash and tax owed count every rise since the opening, by the balance's own rule
        # (`ledger.opening_rises`), including those before a later window start.
        rises = ledger.opening_rises(sales_rows, opened.at, until)[2]
        self.cash += [(r.at, Fraction(r.amount)) for r in rises]
        # The tax still owed at a moment counts from the opening too, rounded per tax week as the balance does.
        self.open_rises = sorted((r.at, r.amount) for r in rises)
        self.remitted: list[tuple[datetime, Fraction]] = []
        self.poll_days = {_day(at) for at, _, _ in sales_rows}
        for r in ledger.rises_between(ledger._counter_rows(conn, "lifetime_units", until), since, until)[2]:
            self.units[_day(r.at)] += r.amount

        calls: dict[date, list[ledger.Entry]] = defaultdict(list)
        for e in ledger.entries(conn, opened.at, until):
            if e.kind != "opening":
                self.cash.append((e.at, e.amount_cents))
            if e.kind == "tax_remitted":
                self.remitted.append((e.at, -e.amount_cents))
            if e.at >= first:
                self.entries[_day(e.at), e.kind] += e.amount_cents
                if e.kind == "tokens":
                    calls[_day(e.at)].append(e)
        # One event a day for its model calls: a run makes dozens, and a chart marks the day.
        for d, made in calls.items():
            self.events[d].append(Event(made[0].at, "tokens", f"{len(made)} model call{'s' * (len(made) > 1)}",
                                        made[0].reference if len(made) == 1 else None,
                                        -sum((e.amount_cents for e in made), Fraction(0))))

        for at, fee in ledger._visit_rows(conn, opened.at, until):
            self.cash.append((at, Fraction(-fee)))
            if at >= first:
                self.visits[_day(at)] += 1
                self.fees[_day(at)] += fee
        # A receipt line names its slot and product. A slot that isn't ours by the current assignment
        # (reassigned since) keeps its money in every total, but its marker names neither.
        ours = set(our_slots(conn))
        # On the receipt clock the ledger and the stock replay use (`stock.receipt_lines`).
        receipts = stock.receipt_lines(conn)
        for at, ref, _, slot, sku, paid in sorted(
                ((r["at"], r["request_id"], r["line_no"], r["slot"], r["sku"], r["total_paid"]) for r in receipts
                 if r["at"] is not None and opened.at <= r["at"] <= until), key=lambda r: r[:3]):
            paid = stock.cents(paid)
            if paid is None:
                self.unpaid_moments.append(at)          # in order: sorted by the receipt clock
            else:
                self.cash.append((at, -paid))
            if at >= first:
                if paid is None:
                    self.unpaid_lines[_day(at)] += 1
                else:
                    self.purchases[_day(at)] += paid
                detail = f"slot {slot}: {sku or 'nothing bought'}" if slot in ours else NOT_OURS_LINE
                self.events[_day(at)].append(Event(at, "receipt", detail, ref, paid))
        self.cash.sort(key=lambda c: c[0])
        self.cash_moments = [at for at, _ in self.cash]
        self._visit_events(conn, stock.request_times(receipts))
        self._price_changes(conn)

        # Stock: one read of every movement. Each day's end replays them with
        # `stock.replay`, exactly what `stock.stock(conn, until=...)` does, so a
        # product change is split the same way (#97).
        self.movements = stock.load_movements(conn)
        self._replays: dict[datetime, stock.Stock] = {}
        counted = self.stock_at(until)
        days_sales: dict[date, list[stock.SaleCost]] = defaultdict(list)
        for s in counted.sales:
            if since < s.at <= until:
                days_sales[_day(s.at)].append(s)
        # The ledger's rule, so a day's exact and estimated cost add up to `ledger.pnl`'s.
        self.sales = {d: ledger.summarize_sales(sold) for d, sold in days_sales.items()}
        for s in counted.shrinkage:
            if since < s.at <= until:
                self.lost_units[_day(s.at)] += s.units
                if s.cost_cents is None:
                    self.lost_unknown.add(_day(s.at))
                else:
                    self.lost_cost[_day(s.at)] += s.cost_cents

    def stock_at(self, moment: datetime) -> stock.Stock:
        """`stock.stock(conn, until=moment)`, from the movements already read."""
        if moment not in self._replays:
            self._replays[moment] = stock.replay(self.movements, moment)
        return self._replays[moment]

    def cash_at(self, moment: datetime) -> Fraction:
        """`ledger.balance(conn, moment).balance_cents`, from the cash movements already read."""
        n = bisect_right(self.cash_moments, moment)
        return self.opened.amount_cents + sum((cents for _, cents in self.cash[:n]), Fraction(0))

    def unpaid_at(self, moment: datetime) -> int:
        """Receipt lines with no total_paid from the opening to `moment`: what `cash_at` leaves out."""
        return bisect_right(self.unpaid_moments, moment)

    def tax_owed_at(self, moment: datetime) -> Fraction:
        """`ledger.balance(conn, moment).tax_owed_cents`: tax set aside since the opening, less remitted."""
        weeks: dict[date, int] = defaultdict(int)
        for at, cents in self.open_rises:
            if at > moment:
                break
            weeks[ledger.tax_week_of(at.astimezone(EASTERN))] += cents
        set_aside = sum(tax_cents(c, DEFAULT_TAX_PERCENT) for c in weeks.values())
        return set_aside - sum((c for at, c in self.remitted if at <= moment), Fraction(0))

    def _visit_events(self, conn: psycopg.Connection, times: dict[str, datetime]) -> None:
        """Each fulfilled request, timed at its first line on the receipt clock (`times`) as the ledger
        times a visit, or at `fulfilled_at` with no lines."""
        for ref, off_cycle, fulfilled_at in conn.execute(
                "select request_id, off_cycle, fulfilled_at from purchase_requests where status = 'fulfilled'"):
            at = times.get(ref, fulfilled_at)
            if at is None or not self.first <= at <= self.until:
                continue
            self.events[_day(at)].append(Event(at, "midweek_visit" if off_cycle else "restock_visit",
                                               "midweek visit fulfilled" if off_cycle else "restock fulfilled", ref))

    def _price_changes(self, conn: psycopg.Connection) -> None:
        """A price that differs from the slot's last one. Prices are only seen at polls, so the
        event is when it was seen, not when someone changed it."""
        last: dict[str, int] = {}
        for at, slot, price in price_history(conn, self.until):
            if slot in last and last[slot] != price and at >= self.first:
                self.events[_day(at)].append(Event(
                    at, "price_change", f"slot {slot}: {ledger.money(last[slot])} to {ledger.money(price)} (seen at a poll)"))
            last[slot] = price

    def day(self, d: date, week_takings: dict[date, int], now: datetime) -> Day:
        """Day `d`, which must be counted. `week_takings` runs each tax week's takings so far, for its tax."""
        start = max(_midnight(d), self.first)
        finish = _midnight(d + timedelta(days=1)) - MICROSECOND
        ended = finish < now
        week = ledger.tax_week_of(datetime.combine(d, time.min))
        before = week_takings[week]
        week_takings[week] += self.takings[d]
        tax = tax_cents(week_takings[week], DEFAULT_TAX_PERCENT) - tax_cents(before, DEFAULT_TAX_PERCENT)
        counted = self.stock_at(finish)
        machine, closet, inventory_complete = ledger._inventory(counted)
        inventory = None if machine is None and closet is None else (machine or 0) + (closet or 0)
        units = self.units[d]
        cost = self.sales.get(d, ledger.summarize_sales(()))
        cash_unpaid = self.unpaid_at(finish)

        notes = []
        if start > _midnight(d):
            notes.append(f"counted from the ledger's opening at {start.astimezone(EASTERN):%H:%M} Eastern")
        if start == self.first and self.poll_note:
            notes.append(self.poll_note)
        if not ended:
            notes.append("the day wasn't over when this was read, so these are its figures so far")
        elif d not in self.poll_days:
            notes.append(NO_GOOD_POLL)
        notes += ledger.sales_cost_notes(units, cost, "gross profit and net are unknown, and the figures at "
                                                      "known cost")
        if self.takings[d] and not counted.slots:
            notes.append("the machine sold units but no receipts are stored, so nothing can be costed yet")
        if d in self.lost_unknown:
            notes.append("some units lost without a sale have no known cost, so net is unknown; "
                         "net at known cost leaves them out")
        if self.unpaid_lines[d]:
            notes.append(f"{self.unpaid_lines[d]} receipt lines have no total_paid, so purchases leave them out")
        if cash_unpaid:
            notes.append(_cash_note(cash_unpaid))
        if not inventory_complete:
            notes.append("some units on hand have no known cost, so inventory at cost leaves them out "
                         "and net worth is unknown")
        return Day(
            d, start, ended, self.takings[d], tax, units, cost.units_costed, cost.cost_of_sales_cents,
            self.lost_units[d], self.lost_cost[d], d not in self.lost_unknown, self.visits[d], self.fees[d],
            -self.entries[d, "tokens"], self.entries[d, "adjustment"], self.purchases[d], self.unpaid_lines[d],
            self.entries[d, "capital"], -self.entries[d, "tax_remitted"],
            self.cash_at(finish), cash_unpaid, self.tax_owed_at(finish), inventory, inventory_complete,
            tuple(sorted(self.events[d], key=lambda e: (e.at, e.kind, e.reference or ""))), tuple(notes),
            cost.units_estimated, cost.estimated_cost_of_sales_cents,
            cost.unattributed_stock_cents, cost.unattributed_stock_provisional)


def _not_counted(d: date, why: str, now: datetime) -> Day:
    """A day outside the competition: zeros, no balances, no events, and the reason."""
    return Day(d, None, _midnight(d + timedelta(days=1)) <= now, 0, 0, 0, 0, Fraction(0), 0, Fraction(0), True,
               0, 0, Fraction(0), Fraction(0), Fraction(0), 0, Fraction(0), Fraction(0), None, 0, None, None, False,
               (), (why,))


def _cash_note(lines: int) -> str:
    return (f"{lines} receipt lines since the opening have no total_paid, so the cash balance leaves them out "
            "and net worth is unknown")


def _before_opening(opened: ledger.Entry) -> str:
    # The date only: never what was spent before it, which is development spend.
    return f"before the ledger's opening on {_day(opened.at):%Y-%m-%d}, so not competition money"


def _midnight(d: date) -> datetime:
    """The start of an Eastern day, in UTC, so it compares and subtracts as a moment, not a wall clock."""
    return datetime.combine(d, time.min, EASTERN).astimezone(timezone.utc)


def _day(moment: datetime) -> date:
    return moment.astimezone(EASTERN).date()


# --- Weeks and running totals ------------------------------------------------------------

def weekly(conn: psycopg.Connection, start: date, end: date, now: datetime | None = None) -> list[Week]:
    """The ledger weeks `start` to `end` touches, from `daily`. A week the window cuts is partial."""
    opened = ledger.real_opening(conn)
    return by_week(daily(conn, start, end, now), None if opened is None else _day(opened.at))


def by_week(days: list[Day], opening_day: date | None) -> list[Week]:
    """Days, oldest first, added up into Saturday-to-Friday weeks (`ledger.week_of`). Pure.

    `opening_day` (Eastern) numbers the weeks, 1 holding it. A week is partial
    unless all seven of its days are in `days`, counted from midnight, and over.
    """
    groups: dict[date, list[Day]] = {}
    for d in days:
        groups.setdefault(ledger.week_of(datetime.combine(d.date, time.min)), []).append(d)
    first_week = None if opening_day is None else ledger.week_of(datetime.combine(opening_day, time.min))
    out = []
    for saturday, group in groups.items():
        counted = [d for d in group if d.counted]
        sums = {name: sum((getattr(d, name) for d in group), Fraction(0)) for name in FLOWS}
        for name in ("takings_cents", "sales_tax_cents", "units_sold", "units_costed", "shrinkage_units", "visits",
                     "visit_fees_cents", "unpaid_lines", "units_estimated"):
            sums[name] = int(sums[name])
        last = group[-1]
        partial = sum(d.whole for d in group) < 7
        notes = []
        if not counted:
            notes.append(group[0].notes[0])
        elif partial:
            why = f"partial week: {len(counted)} of 7 days counted, {counted[0].date:%a %d %b} to {counted[-1].date:%a %d %b}"
            if counted[0].counted_from > _midnight(counted[0].date):
                why += f", the first from the opening at {counted[0].counted_from.astimezone(EASTERN):%H:%M}"
            if not counted[-1].ended:
                why += ", the last not over"
            notes.append(why)
        # The window's own note on its polls (none at all, or no baseline) is on its first counted day.
        notes += [n for d in counted for n in d.notes if n.startswith(POLL_NOTES) and n != NO_GOOD_POLL]
        no_poll = sum(NO_GOOD_POLL in d.notes for d in counted)
        if no_poll:
            notes.append(f"{no_poll} of its days had no good poll, so their sales show on the next day that has one")
        if sums["units_estimated"]:
            notes.append(f"{sums['units_estimated']} of {sums['units_sold']} units sold across a product change the "
                         "counters can't split have no exact cost, so gross profit and net are unknown; estimated "
                         f"apart at {ledger.money(sums['estimated_cost_of_sales_cents'])}, in no profit figure")
        if sums["units_sold"] > sums["units_costed"] + sums["units_estimated"]:
            notes.append(f"{sums['units_sold'] - sums['units_costed'] - sums['units_estimated']} of "
                         f"{sums['units_sold']} units sold have no known cost, so gross profit and net are unknown")
        provisional = any(d.unattributed_stock_provisional for d in group)
        if sums["unattributed_stock_cents"]:
            notes.append(f"{ledger.money(sums['unattributed_stock_cents'])} of stock at cost left the count with sales "
                         "of unknown product: a reconciliation line, in no cost, estimate or profit"
                         + ("; provisional, since a write-off at the same swap may be one of them" if provisional else ""))
        if not all(d.shrinkage_complete for d in group):
            notes.append("some units lost without a sale have no known cost, so net is unknown")
        if sums["unpaid_lines"]:
            notes.append(f"{sums['unpaid_lines']} receipt lines have no total_paid, so purchases leave them out")
        if counted and last.cash_unpaid_lines:
            notes.append(_cash_note(last.cash_unpaid_lines))
        if counted and not last.inventory_complete:
            notes.append("some units on hand have no known cost, so net worth is unknown")
        out.append(Week(
            saturday, None if first_week is None or saturday < first_week else (saturday - first_week).days // 7 + 1,
            counted[0].date if counted else None, counted[-1].date if counted else None, len(counted), partial,
            sums["takings_cents"], sums["sales_tax_cents"], sums["units_sold"], sums["units_costed"],
            sums["cost_of_sales_cents"], sums["shrinkage_units"], sums["shrinkage_cents"],
            all(d.shrinkage_complete for d in group), sums["visits"], sums["visit_fees_cents"],
            sums["token_spend_cents"], sums["adjustments_cents"], sums["purchases_cents"], sums["unpaid_lines"],
            sums["capital_cents"], sums["tax_remitted_cents"], last.cash_balance_cents, last.cash_unpaid_lines,
            last.tax_owed_cents, last.inventory_cents,
            last.inventory_complete,
            tuple(notes), sums["units_estimated"], sums["estimated_cost_of_sales_cents"],
            sums["unattributed_stock_cents"], provisional))
    return out


def cumulative(rows: list[Day] | list[Week], field: str) -> list[Fraction | None]:
    """The running total of a flow over days or weeks, exact, without rounding between them.

    Gross profit and net, which are unknown on a row with a missing cost, make
    every total from that row on unknown too: a total can't be known when one
    of its parts isn't. Their `_known` versions always add up.
    """
    if field not in RUNNING:
        raise ValueError(f"not a flow that adds up: {field}")
    total: Fraction | None = Fraction(0)
    out = []
    for row in rows:
        value = getattr(row, field)
        total = None if total is None or value is None else total + value
        out.append(total)
    return out


# --- JSON and the command line -----------------------------------------------------------

MONEY = ("takings", "sales_tax", "revenue_ex_tax", "cost_of_sales", "gross_profit", "gross_profit_known",
         "shrinkage", "visit_fees", "token_spend", "adjustments", "net", "net_known", "purchases", "capital",
         "tax_remitted", "cash_balance", "tax_owed", "inventory", "net_worth", "estimated_cost_of_sales",
         "unattributed_stock")


def as_json(row: Day | Week) -> dict:
    """One row as plain JSON values; see the module docstring for the shape."""
    if isinstance(row, Day):
        out = {"date": row.date.isoformat(),
               "counted_from": None if row.counted_from is None else _utc(row.counted_from),
               "ended": row.ended, "whole": row.whole}
    else:
        out = {"week": row.week.isoformat(), "number": row.number,
               "first_day": row.first_day and row.first_day.isoformat(),
               "last_day": row.last_day and row.last_day.isoformat(), "days": row.days, "partial": row.partial}
    out |= {name: ledger.money(getattr(row, name + "_cents")) for name in MONEY}
    out |= {"units_sold": row.units_sold, "units_costed": row.units_costed, "units_estimated": row.units_estimated,
            "shrinkage_units": row.shrinkage_units,
            "visits": row.visits, "cost_of_sales_complete": row.cost_of_sales_complete,
            "unattributed_stock_provisional": row.unattributed_stock_provisional,
            "shrinkage_complete": row.shrinkage_complete, "costs_complete": row.costs_complete,
            "inventory_complete": row.inventory_complete, "unpaid_lines": row.unpaid_lines,
            "purchases_complete": row.purchases_complete, "cash_unpaid_lines": row.cash_unpaid_lines,
            "cash_complete": row.cash_complete,
            "exact_cents": {name: _exact(getattr(row, name + "_cents")) for name in MONEY}}
    if isinstance(row, Day):
        out["events"] = [{"at": _utc(e.at), "kind": e.kind, "detail": e.detail, "reference": e.reference,
                          "amount": ledger.money(e.amount_cents), "exact_cents": _exact(e.amount_cents)}
                         for e in row.events]
    out["notes"] = list(row.notes)
    return out


def _exact(cents: Fraction | int | None) -> str | None:
    """Exact cents as text: "1250" or "6185/11". A string, so no JSON reader turns it into a float."""
    return None if cents is None else str(Fraction(cents))


def _utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def table(rows: list[Day] | list[Week]) -> str:
    """Rows as a fixed-width table for a terminal. "?" is an unknown figure; the notes column says why."""
    columns = ("Revenue", "Cost", "Gross", "Tokens", "Net", "Cash", "Inventory")
    weekly_rows = bool(rows) and isinstance(rows[0], Week)
    head = "Week of     No Days" if weekly_rows else "Date      "
    lines = [head + "".join(f"{c:>11}" for c in columns) + "  Notes"]
    for row in rows:
        if weekly_rows:
            label = f"{row.week}  {row.number or '':>2} {row.days:>4}"
        else:
            label = f"{row.date}"
        if not (row.days if weekly_rows else row.counted):
            lines.append(f"{label}  not counted: {row.notes[0]}")
            continue
        values = (row.revenue_ex_tax_cents, row.cost_of_sales_cents, row.gross_profit_cents, row.token_spend_cents,
                  row.net_cents, row.cash_balance_cents, row.inventory_cents)
        lines.append(label + "".join(f"{ledger.money(v) or '?':>11}" for v in values) + "  " + "; ".join(_flags(row)))
    return "\n".join(lines)


def _flags(row: Day | Week) -> list[str]:
    """Short notes for a table row: what makes it partial or unknown, and the lines the table has no column for."""
    out = []
    if isinstance(row, Week) and row.partial:
        out.append(row.notes[0].replace("partial week: ", "partial: ", 1))   # its first note says why
    if isinstance(row, Day) and row.counted_from > _midnight(row.date):
        out.append(f"from the opening at {row.counted_from.astimezone(EASTERN):%H:%M}")
    if isinstance(row, Day) and not row.ended:
        out.append("so far")
    # Takings with no poll behind them read as 0.00, so the row says why. A week's note counts its days.
    out += [n for n in row.notes if n.startswith(POLL_NOTES) or (isinstance(row, Week) and "had no good poll" in n)]
    if not row.cost_of_sales_complete:
        out.append(f"{row.units_sold - row.units_costed} of {row.units_sold} units not costed")
    if row.units_estimated:
        out.append(f"estimate for {row.units_estimated} of them {ledger.money(row.estimated_cost_of_sales_cents)}")
    if row.unattributed_stock_cents:
        out.append(f"unattributed stock {ledger.money(row.unattributed_stock_cents)}"
                   + (" (provisional)" if row.unattributed_stock_provisional else ""))
    if not row.shrinkage_complete:
        out.append("shrinkage not all costed")
    if not row.costs_complete:
        out.append(f"net at known cost {ledger.money(row.net_known_cents)}")
    for label, cents in (("shrinkage", row.shrinkage_cents), ("visit fees", row.visit_fees_cents),
                         ("adjustments", row.adjustments_cents), ("purchases", row.purchases_cents),
                         ("capital", row.capital_cents), ("tax remitted", row.tax_remitted_cents)):
        if cents:
            out.append(f"{label} {ledger.money(cents)}")
    if not row.purchases_complete:
        out.append(f"purchases leave out {row.unpaid_lines} receipt lines with no total_paid")
    if not row.cash_complete:
        out.append(f"cash leaves out {row.cash_unpaid_lines} receipt lines with no total_paid, so net worth unknown")
    if not row.inventory_complete:
        out.append("inventory not all costed")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.money_series", description=__doc__.splitlines()[0])
    parser.add_argument("--start", type=date.fromisoformat, help="first Eastern day (default: the opening's)")
    parser.add_argument("--end", type=date.fromisoformat, help="last Eastern day, inclusive (default: today)")
    parser.add_argument("--weekly", action="store_true", help="Saturday-to-Friday weeks instead of days")
    parser.add_argument("--json", action="store_true", help="JSON rows instead of a table")
    args = parser.parse_args(argv)
    if args.start and args.end and args.end < args.start:
        parser.error("end must be on or after start")
    with db.connect() as conn:
        behind = db.pending(conn)
        if behind:
            print(f"The database is missing migrations: {', '.join(behind)}. "
                  "Check DATABASE_URL points where you mean, then run: python -m vending.db migrate", file=sys.stderr)
            return 2
        start, end = args.start, args.end
        if start is None:
            opened = ledger.real_opening(conn)
            if opened is None:
                print(ledger.OPEN_HINT + " Or give --start.", file=sys.stderr)
                return 1
            start = _day(opened.at)
        end = end or _day(ledger.db_now(conn))
        if end < start:
            parser.error(f"end ({end}) is before start ({start})")
        try:
            rows = weekly(conn, start, end) if args.weekly else daily(conn, start, end)
        except SlotsNotRecorded as error:
            print(error, file=sys.stderr)
            return 1
    print(json.dumps([as_json(r) for r in rows], indent=2) if args.json else table(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
