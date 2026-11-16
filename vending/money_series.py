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
the ledger's Saturday-to-Friday weeks. This is the data under the money chart,
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
care: the ledger rounds the tax inside each week's takings, not each sale's,
so a day's tax is what the day added to its week's rounded tax. A week cut by
the window is rounded on its days inside it, as `ledger.pnl` does, so a window
that starts midweek can put a cent of tax on a different day than one that
starts at the opening.

When some units sold, or some units lost, have no known cost, a profit that
leaves them out looks complete and isn't. So `gross_profit_cents` and
`net_cents` are None then, as on the dashboard's money panel, and
`gross_profit_known_cents` and `net_known_cents` carry the figures over the
costs we do know, beside the flags that say which are missing. A running
total (`cumulative`) of an unknown figure is unknown from the first unknown day.

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

Known limits, both the ledger's, so the series keeps agreeing with it until they are fixed there:

- Sales in a window that starts after a counter misread are read from the
  window's own baseline, as `ledger.pnl` reads them, so the misread can show as
  a rise. From the opening (the default, and what the chart and the briefing
  use) the series reads them as the balance does. The cash balance and tax
  owed always count from the opening.
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
from vending.slots import DEVICE_ID, SlotsNotRecorded, our_slots
from vending.tools.sales_history import tax_cents

EASTERN = ZoneInfo(ledger.TIMEZONE)
MICROSECOND = timedelta(microseconds=1)
NO_OPENING = "the ledger has no opening entry, so none of this is competition money yet"

# The lines a day has and a week adds up. Each is summed exactly, so a week's are its days'.
FLOWS = ("takings_cents", "sales_tax_cents", "units_sold", "units_costed", "cost_of_sales_cents",
         "shrinkage_units", "shrinkage_cents", "visits", "visit_fees_cents", "token_spend_cents",
         "adjustments_cents", "purchases_cents", "capital_cents", "tax_remitted_cents")
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
        return self.units_costed == self.units_sold

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
    def net_worth_cents(self) -> Fraction | None:
        """Spendable cash plus inventory at cost; None unless both are known and every unit on hand has a cost.

        Spendable is `ledger.Balance.spendable_cents`: the balance less the sales tax still owed. That
        tax was never ours, so counting it would overstate what the business is worth.
        """
        if (self.cash_balance_cents is None or self.tax_owed_cents is None or self.inventory_cents is None
                or not self.inventory_complete):
            return None
        return self.cash_balance_cents - self.tax_owed_cents + self.inventory_cents


@dataclass(frozen=True)
class Day(_Lines):
    """One Eastern calendar day of competition money: the day's flows, and the balances at its end."""
    date: date
    counted_from: datetime | None   # midnight, or the opening on the opening day; None when not counted
    ended: bool                     # the day was over when the series was read
    takings_cents: int              # tax included, from the polls' counters
    sales_tax_cents: int            # what the day added to its week's rounded set-aside
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
    purchases_cents: Fraction       # receipts: cash out now, cost of sales as the units sell
    capital_cents: Fraction         # cash in, not profit
    tax_remitted_cents: Fraction    # cash out, not a cost: the tax was never revenue
    cash_balance_cents: Fraction | None     # `ledger.balance` at the day's end; None when not counted
    tax_owed_cents: Fraction | None         # its sales tax set aside since the opening, less remitted
    inventory_cents: Fraction | None        # machine and closet at cost, at the day's end
    inventory_complete: bool
    events: tuple[Event, ...]
    notes: tuple[str, ...]

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
    capital_cents: Fraction
    tax_remitted_cents: Fraction
    cash_balance_cents: Fraction | None     # at the end of its last day in the series
    tax_owed_cents: Fraction | None
    inventory_cents: Fraction | None
    inventory_complete: bool
    notes: tuple[str, ...]


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
    opened = ledger.opening(conn)
    until = _midnight(end + timedelta(days=1)) - MICROSECOND
    if opened is None or opened.at > until:
        why = NO_OPENING if opened is None else _before_opening(opened)
        return [_not_counted(d, why, now) for d in dates]
    first = max(_midnight(start), opened.at)                    # the first moment counted
    # `ledger.pnl`'s start for this window. Sales count at polls after it: the
    # poll at the opening is the baseline, and a poll at midnight sharp counts.
    since = opened.at if opened.at >= _midnight(start) else _midnight(start) - MICROSECOND
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
        self.costed: dict[date, int] = defaultdict(int)
        self.cost: dict[date, Fraction] = defaultdict(Fraction)
        self.lost_units: dict[date, int] = defaultdict(int)
        self.lost_cost: dict[date, Fraction] = defaultdict(Fraction)
        self.lost_unknown: set[date] = set()
        self.visits: dict[date, int] = defaultdict(int)
        self.fees: dict[date, int] = defaultdict(int)
        self.purchases: dict[date, Fraction] = defaultdict(Fraction)
        self.unpaid_lines: dict[date, int] = defaultdict(int)
        self.entries: dict[tuple[date, str], Fraction] = defaultdict(Fraction)
        self.events: dict[date, list[Event]] = defaultdict(list)
        # Every cash movement since the opening, (when, signed cents), for the balance at each day's end.
        self.cash: list[tuple[datetime, Fraction]] = []

        # Takings and units: the ledger's own rows and rises, so the counter rules are the same.
        sales_rows = ledger._counter_rows(conn, "lifetime_sales_cents", until)
        _, _, rises, self.poll_note = ledger.rises_between(sales_rows, since, until)
        for r in rises:
            self.takings[_day(r.at)] += r.amount
        if since != opened.at:
            # The balance counts from the opening's baseline, which a later window start can't see
            # (a misread just before the start reads as a rise after it from the window's own baseline).
            rises = ledger.rises_between(sales_rows, opened.at, until)[2]
        self.cash += [(r.at, Fraction(r.amount)) for r in rises]
        # The tax still owed at a moment counts from the opening too, rounded per week as the balance does.
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
        for at, ref, line, slot, sku, paid in conn.execute(
                "select l.resolved_at, l.request_id, l.line_no, l.slot, l.sku, l.total_paid from purchase_lines l"
                " join purchase_requests r using (request_id) where r.status = 'fulfilled'"
                " and l.resolved_at >= %s and l.resolved_at <= %s order by l.resolved_at, l.request_id, l.line_no",
                (opened.at, until)):
            paid = stock.cents(paid)
            if paid is not None:
                self.cash.append((at, -paid))
            if at >= first:
                if paid is None:
                    self.unpaid_lines[_day(at)] += 1
                else:
                    self.purchases[_day(at)] += paid
                self.events[_day(at)].append(Event(at, "receipt", f"slot {slot}: {sku or 'nothing bought'}",
                                                   ref, paid))
        self.cash.sort(key=lambda c: c[0])
        self.cash_moments = [at for at, _ in self.cash]
        self._visit_events(conn)
        self._price_changes(conn)

        # Stock: one read of every movement. The day's end cuts a prefix of them, which is
        # exactly what `stock.stock(conn, until=...)` would replay (same list, same order).
        self.movements = sorted(stock.load_movements(conn), key=lambda m: m.at)
        self.moments = [m.at for m in self.movements]
        self._replays: dict[int, stock.Stock] = {}
        counted = self.stock_at(until)
        for s in counted.sales:
            if since < s.at <= until and s.cost_cents is not None:
                self.costed[_day(s.at)] += s.costed_units
                self.cost[_day(s.at)] += s.cost_cents
        for s in counted.shrinkage:
            if since < s.at <= until:
                self.lost_units[_day(s.at)] += s.units
                if s.cost_cents is None:
                    self.lost_unknown.add(_day(s.at))
                else:
                    self.lost_cost[_day(s.at)] += s.cost_cents

    def stock_at(self, moment: datetime) -> stock.Stock:
        """`stock.stock(conn, until=moment)`, from the movements already read. Days with no movement share one."""
        n = bisect_right(self.moments, moment)
        if n not in self._replays:
            self._replays[n] = stock.weighted_average(self.movements[:n])
        return self._replays[n]

    def cash_at(self, moment: datetime) -> Fraction:
        """`ledger.balance(conn, moment).balance_cents`, from the cash movements already read."""
        n = bisect_right(self.cash_moments, moment)
        return self.opened.amount_cents + sum((cents for _, cents in self.cash[:n]), Fraction(0))

    def tax_owed_at(self, moment: datetime) -> Fraction:
        """`ledger.balance(conn, moment).tax_owed_cents`: tax set aside since the opening, less remitted."""
        weeks: dict[date, int] = defaultdict(int)
        for at, cents in self.open_rises:
            if at > moment:
                break
            weeks[ledger.week_of(at.astimezone(EASTERN))] += cents
        set_aside = sum(tax_cents(c, DEFAULT_TAX_PERCENT) for c in weeks.values())
        return set_aside - sum((c for at, c in self.remitted if at <= moment), Fraction(0))

    def _visit_events(self, conn: psycopg.Connection) -> None:
        """Each fulfilled request, timed at its first receipt line as the ledger times a visit."""
        for ref, off_cycle, at in conn.execute(
                "select request_id, off_cycle, at from ("
                " select r.request_id, r.off_cycle, coalesce(min(l.resolved_at), r.fulfilled_at) as at"
                " from purchase_requests r left join purchase_lines l using (request_id)"
                " where r.status = 'fulfilled' group by r.request_id, r.off_cycle, r.fulfilled_at) v"
                " where at >= %s and at <= %s", (self.first, self.until)):
            self.events[_day(at)].append(Event(at, "midweek_visit" if off_cycle else "restock_visit",
                                               "midweek visit fulfilled" if off_cycle else "restock fulfilled", ref))

    def _price_changes(self, conn: psycopg.Connection) -> None:
        """A price that differs from the slot's last one. Prices are only seen at polls, so the
        event is when it was seen, not when someone changed it."""
        last: dict[str, int] = {}
        for at, slot, price in conn.execute(
                "select p.started_at, s.selection, s.price_cents from poll_selections s"
                " join polls p using (session_id) where p.device_id = %s and p.crc_ok"
                " and s.selection = any(%s) and p.started_at <= %s and s.price_cents is not null"
                " order by p.started_at, p.session_id, s.selection", (DEVICE_ID, list(our_slots(conn)), self.until)):
            if slot in last and last[slot] != price and at >= self.first:
                self.events[_day(at)].append(Event(
                    at, "price_change", f"slot {slot}: {ledger.money(last[slot])} to {ledger.money(price)} (seen at a poll)"))
            last[slot] = price

    def day(self, d: date, week_takings: dict[date, int], now: datetime) -> Day:
        """Day `d`, which must be counted. `week_takings` runs each week's takings so far, for its tax."""
        start = max(_midnight(d), self.first)
        finish = _midnight(d + timedelta(days=1)) - MICROSECOND
        ended = finish < now
        week = ledger.week_of(datetime.combine(d, time.min))
        before = week_takings[week]
        week_takings[week] += self.takings[d]
        tax = tax_cents(week_takings[week], DEFAULT_TAX_PERCENT) - tax_cents(before, DEFAULT_TAX_PERCENT)
        counted = self.stock_at(finish)
        machine, closet, inventory_complete = ledger._inventory(counted)
        inventory = None if machine is None and closet is None else (machine or 0) + (closet or 0)
        units, costed = self.units[d], self.costed[d]

        notes = []
        if start > _midnight(d):
            notes.append(f"counted from the ledger's opening at {start.astimezone(EASTERN):%H:%M} Eastern")
        if start == self.first and self.poll_note:
            notes.append(self.poll_note)
        if not ended:
            notes.append("the day wasn't over when this was read, so these are its figures so far")
        elif d not in self.poll_days:
            notes.append("no good poll this day, so its sales show on the next day that has one")
        if units > costed:
            notes.append(f"{units - costed} of {units} units sold have no known cost (sold from a slot before we "
                         "first loaded it, or past what we loaded), so gross profit and net are unknown; "
                         "the figures at known cost leave their cost out")
        if self.takings[d] and not counted.slots:
            notes.append("the machine sold units but no receipts are stored, so nothing can be costed yet")
        if d in self.lost_unknown:
            notes.append("some units lost without a sale have no known cost, so net is unknown; "
                         "net at known cost leaves them out")
        if self.unpaid_lines[d]:
            notes.append(f"{self.unpaid_lines[d]} receipt lines have no total_paid, so purchases leave them out")
        if not inventory_complete:
            notes.append("some units on hand have no known cost, so inventory at cost leaves them out "
                         "and net worth is unknown")
        return Day(
            d, start, ended, self.takings[d], tax, units, costed, self.cost[d],
            self.lost_units[d], self.lost_cost[d], d not in self.lost_unknown, self.visits[d], self.fees[d],
            -self.entries[d, "tokens"], self.entries[d, "adjustment"], self.purchases[d],
            self.entries[d, "capital"], -self.entries[d, "tax_remitted"],
            self.cash_at(finish), self.tax_owed_at(finish), inventory, inventory_complete,
            tuple(sorted(self.events[d], key=lambda e: (e.at, e.kind, e.reference or ""))), tuple(notes))


def _not_counted(d: date, why: str, now: datetime) -> Day:
    """A day outside the competition: zeros, no balances, no events, and the reason."""
    return Day(d, None, _midnight(d + timedelta(days=1)) <= now, 0, 0, 0, 0, Fraction(0), 0, Fraction(0), True,
               0, 0, Fraction(0), Fraction(0), Fraction(0), Fraction(0), Fraction(0), None, None, None, False, (), (why,))


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
    opened = ledger.opening(conn)
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
                     "visit_fees_cents"):
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
        if sums["units_sold"] > sums["units_costed"]:
            notes.append(f"{sums['units_sold'] - sums['units_costed']} of {sums['units_sold']} units sold have no "
                         "known cost, so gross profit and net are unknown")
        if not all(d.shrinkage_complete for d in group):
            notes.append("some units lost without a sale have no known cost, so net is unknown")
        if counted and not last.inventory_complete:
            notes.append("some units on hand have no known cost, so net worth is unknown")
        out.append(Week(
            saturday, None if first_week is None or saturday < first_week else (saturday - first_week).days // 7 + 1,
            counted[0].date if counted else None, counted[-1].date if counted else None, len(counted), partial,
            sums["takings_cents"], sums["sales_tax_cents"], sums["units_sold"], sums["units_costed"],
            sums["cost_of_sales_cents"], sums["shrinkage_units"], sums["shrinkage_cents"],
            all(d.shrinkage_complete for d in group), sums["visits"], sums["visit_fees_cents"],
            sums["token_spend_cents"], sums["adjustments_cents"], sums["purchases_cents"], sums["capital_cents"],
            sums["tax_remitted_cents"], last.cash_balance_cents, last.tax_owed_cents, last.inventory_cents,
            last.inventory_complete,
            tuple(notes)))
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
         "tax_remitted", "cash_balance", "tax_owed", "inventory", "net_worth")


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
    out |= {"units_sold": row.units_sold, "units_costed": row.units_costed, "shrinkage_units": row.shrinkage_units,
            "visits": row.visits, "cost_of_sales_complete": row.cost_of_sales_complete,
            "shrinkage_complete": row.shrinkage_complete, "costs_complete": row.costs_complete,
            "inventory_complete": row.inventory_complete,
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
    if not row.cost_of_sales_complete:
        out.append(f"{row.units_sold - row.units_costed} of {row.units_sold} units not costed")
    if not row.shrinkage_complete:
        out.append("shrinkage not all costed")
    if not row.costs_complete:
        out.append(f"net at known cost {ledger.money(row.net_known_cents)}")
    for label, cents in (("shrinkage", row.shrinkage_cents), ("visit fees", row.visit_fees_cents),
                         ("adjustments", row.adjustments_cents), ("purchases", row.purchases_cents),
                         ("capital", row.capital_cents), ("tax remitted", row.tax_remitted_cents)):
        if cents:
            out.append(f"{label} {ledger.money(cents)}")
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
            opened = ledger.opening(conn)
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
