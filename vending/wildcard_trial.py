"""The wildcard as a testing ground: has the product on trial earned a regular slot? (#110 item 1)

The rule (`docs/agent/wildcard.md`, "A testing ground"):

1. **The trial** is the product in the slot the latest Friday visit's plan
   named as the wildcard. A plan's wildcard takes effect when its visit is
   confirmed (the request is fulfilled), not when it is filed, and a midweek
   visit never moves it. Visits are put in order by when they ended: the
   request's `door_closed_at` where that column exists, else its
   `fulfilled_at`. A fulfilled Friday request with neither can't be put in
   order, so the wildcard in effect is unresolved (`WildcardUnresolved`), not
   guessed from when the plan was filed. The trial counts from when that
   product went in or the slot was first named (in an unbroken run of visits
   naming it), whichever is later. A visit whose plan named no wildcard, or
   another slot, ends the trial. A named slot that isn't one of ours now (the
   stored assignment) is no wildcard at all: no trial, and the planner
   (`vending.planning.gather`) treats it as none.
2. **The measure** is profit per in-stock class day: revenue without tax, less
   the cost of what sold, over the class days the slot held stock
   (`vending.demand`). The restock policy ranks slots by the same yardstick.
3. **Only what the polls saw counts.** Money and days come from the same poll
   intervals: stretches where the units and the cents counters both measured
   every sale and agree on where the stretch begins and ends. A stretch the
   polls didn't cover adds no days, and one where the counters disagree (or
   the replay's sales don't match them) makes the profit unknown. An interval
   with no sales is a zero, not a gap. An interval longer than
   `MAX_POLL_GAP` (the polls stopped) adds neither its days nor its sales.
4. **After `TRIAL_DAYS` in-stock class days** the polls saw, the point where a
   slot's own rate counts, compare it with each of our other slots over the
   intervals both were seen holding stock: identical poll intervals, so both
   rates cover the same class days. Only slots with `TRIAL_DAYS` such shared
   class days count; with none, there is no verdict.
5. **Promote** when the trial earns more per day than the weakest of them over
   the days they share; else **drop** it. A promoted product stays in its
   slot, which becomes a regular slot, and the weakest slot's product goes to
   the closet; that slot becomes the wildcard with the next product to test.
   Either way the agent acts at a Friday visit.
6. **A trial priced above the ceiling** at its latest priced poll is a price
   test, not a promotion trial: a regular slot couldn't charge that, so its
   profit wouldn't carry over (`standing_prices`). It counts again only from
   the first poll strictly after the last one that read a price above the
   ceiling, and only intervals whose polls all read a price at or under it
   count for it. A poll with no price never qualifies one. Polls at the same
   moment that read different prices settle nothing: when they are the latest
   priced moment, the price is unknown and so is the verdict.

The interval holding the load mixes the old product's sales with the new one's,
so it is left out of both money and days. Units sold past what the receipts
loaded have no cost, so they make the profit unknown rather than free.

`judge`, `compare`, `slot_record` and `intervals` are pure; `current_wildcard`,
`standing_prices` and `trial` read the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from fractions import Fraction

import psycopg

from vending import config, counters, demand, stock
from vending.class_days import class_days_after
from vending.pricing import DEFAULT_TAX_PERCENT
from vending.slots import DEVICE_ID
from vending.tools.sales_history import tax_cents

TRIAL_DAYS = demand.OWN_RATE_AFTER   # in-stock class days before a trial is judged

# The longest stretch between two polls that still counts as watched. David polls hourly; a longer gap
# means the polls stopped, and the class days and sales inside it weren't seen, so they count for nothing.
MAX_POLL_GAP = timedelta(hours=6)

TOO_EARLY, PROMOTE, DROP, PRICE_TEST, UNKNOWN = "too early", "promote", "drop", "price test", "unknown"

# `(polled_at, lifetime_units, lifetime_sales_cents, price_cents)`
Reading = tuple[datetime, int, int, int | None]


@dataclass(frozen=True)
class SlotRecord:
    """What one slot's current product earned from `since`, over the class days it held stock."""
    slot: str
    product: str
    since: datetime
    class_days_in_stock: int
    units_sold: int
    profit_cents: Fraction | None   # revenue without tax, less cost of sales; None when a cost is unknown

    @property
    def profit_per_class_day_cents(self) -> Fraction | None:
        if self.profit_cents is None or not self.class_days_in_stock:
            return None
        return self.profit_cents / self.class_days_in_stock


@dataclass(frozen=True)
class Comparison:
    """The trial and one other slot over the same poll intervals, so over the same class days."""
    trial: SlotRecord
    other: SlotRecord


@dataclass(frozen=True)
class Trial:
    record: SlotRecord | None         # None: nothing to measure (no receipts, not ours, or the price is unknown)
    slot: str | None                  # None: the named slot isn't one of ours now, or the wildcard is unresolved
    price_test: bool
    compared: Comparison | None       # the trial against the weakest other slot, over the days they share
    verdict: str
    reason: str


@dataclass(frozen=True)
class Interval:
    """One stretch, at most `MAX_POLL_GAP` long, in which both counters measured every sale of a slot."""
    begin: datetime
    end: datetime
    units: int
    takings_cents: int                # tax inside
    cost_cents: Fraction | None       # None: a cost is unknown, or the counters and the sales don't agree
    days: tuple[date, ...]            # the class days in it the slot held stock as they began
    held: bool                        # held stock as it began and as every class day in it began


def slot_record(slot: str, product: str, since: datetime, class_days: int, units: int,
                takings_cents: int, costs: list[Fraction | None], tax_percent: int) -> SlotRecord:
    """A slot's record from its takings (tax inside) and the cost of each sale in the same windows. Pure."""
    known = all(c is not None for c in costs)
    profit = Fraction(takings_cents - tax_cents(takings_cents, tax_percent)) - sum(costs) if known else None
    return SlotRecord(slot, product, since, class_days, units, profit)


@dataclass(frozen=True)
class Measured:
    """One slot's product, since when it counts, and its intervals from then."""
    slot: str
    product: str
    since: datetime
    intervals: list[Interval]

    def record(self, tax_percent: int, keep: set[tuple[datetime, datetime]] | None = None) -> SlotRecord:
        """Its record over all its intervals, or only those in `keep`: their money and in-stock class days. Pure."""
        chosen = [i for i in self.intervals if keep is None or (i.begin, i.end) in keep]
        days = {d for i in chosen for d in i.days}
        return slot_record(self.slot, self.product, self.since, len(days), sum(i.units for i in chosen),
                           sum(i.takings_cents for i in chosen), [i.cost_cents for i in chosen], tax_percent)


def compare(trial: Measured, other: Measured, tax_percent: int) -> Comparison:
    """The trial and another slot over the poll intervals that saw both holding stock. Pure.

    The polls are the machine's, so both slots' intervals normally share their
    ends; an interval only one of them has (a misread in one slot) is left out
    of both, so both rates still cover the same class days.
    """
    shared = {(i.begin, i.end) for i in trial.intervals if i.held} & {(i.begin, i.end) for i in other.intervals if i.held}
    return Comparison(trial.record(tax_percent, shared), other.record(tax_percent, shared))


def judge(trial: SlotRecord, price_test: bool, others: list[Comparison]) -> tuple[str, Comparison | None, str]:
    """The verdict on a trial, the comparison with the weakest slot, and why, in words the agent can quote. Pure.

    `others` holds the trial against each other slot over the days the two share.
    """
    if trial.class_days_in_stock < TRIAL_DAYS:
        return TOO_EARLY, None, (f"{trial.class_days_in_stock} of {TRIAL_DAYS} in-stock class days the polls have "
                                 "seen so far; judge it at the first Friday visit after it has them")
    if price_test:
        return PRICE_TEST, None, ("it sells above the ceiling, so this is a price test, not a promotion trial: a "
                                  "regular slot couldn't charge that. Price it at or under the ceiling to test it for "
                                  "promotion; the trial then counts from the first poll that reads that price")
    if trial.profit_per_class_day_cents is None:
        return UNKNOWN, None, ("its profit is unknown: the cost of some of its sales is unknown, more sold than the "
                               "receipts loaded, or the units and takings counters disagree")
    rivals = [c for c in others if c.other.class_days_in_stock >= TRIAL_DAYS
              and c.other.profit_per_class_day_cents is not None and c.trial.profit_per_class_day_cents is not None]
    if not rivals:
        return UNKNOWN, None, (f"no other slot has {TRIAL_DAYS} class days with a known profit on which the polls saw "
                               "both it and the trial holding stock, so there is nothing to compare it with yet")
    weakest = min(rivals, key=lambda c: (c.other.profit_per_class_day_cents, c.other.slot))
    ppd, w = weakest.trial.profit_per_class_day_cents, weakest.other
    days = f"over the {w.class_days_in_stock} class days both held stock"
    if ppd > w.profit_per_class_day_cents:
        return PROMOTE, weakest, (
            f"{_money(ppd)} profit per in-stock class day beats slot {w.slot}'s {_money(w.profit_per_class_day_cents)} "
            f"({w.product}), the weakest of our other slots, {days}. At the next Friday visit, "
            f"keep {trial.product} in slot {trial.slot} as a regular slot, send {w.product} to the closet, "
            f"and make slot {w.slot} the wildcard with the next product to test")
    return DROP, weakest, (
        f"{_money(ppd)} profit per in-stock class day doesn't beat slot {w.slot}'s "
        f"{_money(w.profit_per_class_day_cents)} ({w.product}), the weakest of our other slots, {days}. "
        f"At the next Friday visit, switch slot {trial.slot} to the next product to test")


def _money(cents: Fraction) -> str:
    sign = "-" if cents < 0 else ""
    whole = round(abs(cents))
    return f"{sign}${whole // 100}.{whole % 100:02d}"


def intervals(readings: list[Reading], start: datetime, product: str, sales: list[stock.SaleCost],
              spans: list[tuple[datetime, datetime]], ceiling_cents: int | None = None) -> list[Interval]:
    """A slot's intervals that began at or after `start`, as the polls saw them. Pure.

    `readings` are the slot's polls from one at or before `start`; `sales` the
    stock replay's for this slot; `spans` when it held stock (`demand.in_stock_spans`).
    An interval longer than `MAX_POLL_GAP` is left out, days and sales: the
    polls didn't watch it. With `ceiling_cents`, an interval counts only if
    every poll in it read a price at or under it: a missing price doesn't
    qualify one.
    """
    in_stock = set(demand.class_days_in_stock(spans))
    out = []
    for begin, end, units, takings in _joint(readings):
        if begin < start or end - begin > MAX_POLL_GAP:
            continue
        if ceiling_cents is not None and not all(
                price is not None and price <= ceiling_cents for at, _, _, price in readings if begin <= at <= end):
            continue
        inside = [s for s in sales if begin < s.at <= end]
        # The replay's sales in it must be the counters' units, all of this product and costed, or the cost
        # isn't this interval's. Units without takings, or takings without units, can't be reconciled either.
        agree = (sum(s.units for s in inside) == units and (units > 0) == (takings > 0)
                 and all(s.product == product and s.taken_at is None and s.cost_cents is not None
                         and s.costed_units == s.units for s in inside))
        cost = sum((s.cost_cents for s in inside), Fraction(0)) if agree else None
        days = class_days_after(begin, end)
        held = any(a <= begin < b for a, b in spans) and all(d in in_stock for d in days)
        out.append(Interval(begin, end, units, takings, cost, tuple(d for d in days if d in in_stock), held))
    return out


def _joint(readings: list[Reading]) -> list[tuple[datetime, datetime, int, int]]:
    """`(begin, end, units, cents)`: the shortest stretches both counters cover end to end, with the same ends. Pure.

    `counters.windows` decides what a reading that goes down means, for each
    counter alike. A stretch only one counter covers, or neither, is left out.
    """
    unit_w = {b: (a, r) for b, a, r in counters.windows([(at, u) for at, u, _, _ in readings], zeros=True)}
    cent_w = {b: (a, r) for b, a, r in counters.windows([(at, c) for at, _, c, _ in readings], zeros=True)}
    out, done = [], None
    for begin in sorted(unit_w.keys() & cent_w.keys()):
        if done is not None and begin < done:
            continue
        (u_end, units), (c_end, cents) = unit_w[begin], cent_w[begin]
        while u_end != c_end:
            if u_end < c_end:
                if u_end not in unit_w:
                    break
                u_end, units = unit_w[u_end][0], units + unit_w[u_end][1]
            else:
                if c_end not in cent_w:
                    break
                c_end, cents = cent_w[c_end][0], cents + cent_w[c_end][1]
        if u_end == c_end:
            out.append((begin, u_end, units, cents))
            done = u_end
    return out


class WildcardUnresolved(RuntimeError):
    """The wildcard in effect can't be settled from the data. The message says why and what fixes it."""


def current_wildcard(conn: psycopg.Connection) -> tuple[str | None, datetime | None]:
    """The wildcard in effect (None: none), and since when.

    It is the slot the plan of the latest confirmed Friday visit named: a
    request takes effect when it is fulfilled, so one still pending (or
    rejected) changes nothing, and a midweek visit can't move it. Visits are
    put in order by when they ended (`_visit_end`). "Since" is the end of the
    visit that began the unbroken run naming that slot.

    Raises `WildcardUnresolved` when a fulfilled Friday request has no end
    time: it can't be put in order, so the wildcard in effect isn't known.
    """
    end = _visit_end(conn)
    rows = conn.execute(
        f"select s.wildcard, {end}, r.request_id from purchase_submissions s"
        " join purchase_requests r using (request_id)"
        " where s.status = 'accepted' and r.status = 'fulfilled' and not r.off_cycle"
        f" order by {end}, s.id"
    ).fetchall()
    unresolved = sorted({request_id for _, at, request_id in rows if at is None})
    if unresolved:
        column = "door_closed_at and fulfilled_at are" if "door_closed_at" in end else "fulfilled_at is"
        raise WildcardUnresolved(
            f"Friday request {', '.join(unresolved)} is fulfilled but its {column} null, so its visit can't be put "
            "in order with the others and which slot is the wildcard is unknown; run the request sync "
            "(python -m vending.sync_requests) so the time is recorded, or ask a person to check the request")
    slot, since = None, None
    for named, at, _ in rows:
        if named == slot:
            continue
        slot, since = named, at
    return slot, since if slot is not None else None


def _visit_end(conn: psycopg.Connection) -> str:
    """SQL for when a fulfilled request's visit ended: `door_closed_at` where that column exists, else `fulfilled_at`."""
    has_door = conn.execute(
        "select exists (select 1 from information_schema.columns where table_schema = current_schema()"
        " and table_name = 'purchase_requests' and column_name = 'door_closed_at')").fetchone()[0]
    return "coalesce(r.door_closed_at, r.fulfilled_at)" if has_door else "r.fulfilled_at"


@dataclass(frozen=True)
class Standing:
    """A slot's price as the polls last read one: the latest moment a good poll read a price for it."""
    at: datetime
    prices: frozenset[int]      # what the polls at that moment read; more than one means they disagree

    @property
    def known(self) -> bool:
        return len(self.prices) == 1

    def above(self, ceiling_cents: int) -> bool | None:
        """Above the ceiling; None when the polls at that moment disagree."""
        return max(self.prices) > ceiling_cents if self.known else None


def standing_prices(conn: psycopg.Connection, slots: tuple[str, ...] | list[str]) -> dict[str, Standing]:
    """Each slot's latest priced moment on our machine, over every good poll. A slot no poll priced is left out.

    A poll that read no price leaves the standing price where it was, so a
    price test ends only when a strictly later poll reads a price at or under
    the ceiling. Polls at the same moment that disagree leave it unknown.
    `trial` and the planner (`vending.planning.gather`) both read it here.
    """
    out: dict[str, Standing] = {}
    for selection, at, prices in conn.execute(
            "select s.selection, p.started_at, array_agg(distinct s.price_cents) from poll_selections s"
            " join polls p using (session_id)"
            " where p.device_id = %s and p.crc_ok and s.selection = any(%s) and s.price_cents is not null"
            " and p.started_at = (select max(p2.started_at) from poll_selections s2 join polls p2 using (session_id)"
            "   where p2.device_id = %s and p2.crc_ok and s2.selection = s.selection and s2.price_cents is not null)"
            " group by s.selection, p.started_at",
            (DEVICE_ID, list(slots), DEVICE_ID)):
        out[selection] = Standing(at, frozenset(prices))
    return out


def trial(conn: psycopg.Connection, now: datetime, counts: dict[str, stock.SlotCount],
          loads: dict[str, list[tuple[datetime, int]]], sales: tuple[stock.SaleCost, ...],
          slots: tuple[str, ...]) -> Trial | None:
    """The running trial, judged against our other slots; None when no wildcard is named.

    `slots` are ours (the stored assignment), `counts` our slots' receipted
    counts and `loads` every load per slot, as `get_machine_state` reads them;
    `sales` is the stock replay's.
    """
    try:
        slot, named = current_wildcard(conn)
    except WildcardUnresolved as error:
        return Trial(None, None, False, None, UNKNOWN, str(error))
    if slot is None:
        return None
    if slot not in slots:
        return Trial(None, None, False, None, UNKNOWN, (
            "the slot the latest Friday visit named as the wildcard isn't one of ours in the stored slot "
            "assignment, so no trial is running; name one of ours at the next Friday visit"))
    w = counts.get(slot)
    if w is None:
        return Trial(None, slot, False, None, UNKNOWN,
                     f"slot {slot} is the wildcard, but no receipts show what it holds, so there is nothing to measure")
    ceiling_cents, tax_percent = _setting(conn, "price_ceiling_cents", 650), _setting(conn, "tax_percent", DEFAULT_TAX_PERCENT)
    start = max(w.since, named)
    readings = _readings(conn, start, now, list(counts))
    standing = standing_prices(conn, [slot]).get(slot)
    if standing is not None and not standing.known:
        return Trial(None, slot, False, None, UNKNOWN, (
            f"the polls at {standing.at:%Y-%m-%d %H:%M} UTC read different prices for slot {slot} "
            f"({', '.join(f'${p // 100}.{p % 100:02d}' for p in sorted(standing.prices))}), so its price is "
            "unknown and so is whether it is a price test; a later poll that reads one price settles it"))
    # Above the ceiling at the latest poll that read a price: a poll with no price changes nothing.
    price_test = standing is not None and bool(standing.above(ceiling_cents))
    priced = [(at, price) for at, _, _, price in readings.get(slot, []) if at >= start and price is not None]
    above = [at for at, price in priced if price > ceiling_cents]
    if above and not price_test:
        # It counts from the first poll strictly after the last one above the ceiling: the interval before it
        # may hold sales at the old price. A poll with no price doesn't count as back under.
        back = next((at for at, price in priced if at > above[-1]), None)
        if back is None:   # can't happen while the standing price is under the ceiling; refuse rather than guess
            return Trial(None, slot, False, None, UNKNOWN, (
                f"no poll after the last one that read slot {slot} above the ceiling has read its price, so "
                "whether its price test has ended is unknown"))
        start = back
    measured = {s: _measure(c, max(start, c.since), now, loads.get(s, []), sales, readings.get(s, []),
                            None if s != slot or price_test else ceiling_cents)
                for s, c in counts.items()}
    on_trial = measured[slot]
    others = [compare(on_trial, m, tax_percent) for s, m in sorted(measured.items()) if s != slot]
    record = on_trial.record(tax_percent)
    shared = sorted(s for s, c in counts.items() if s != slot and c.product == w.product)
    if shared:
        # Its sales are split with another slot, so its rate isn't its own (the planner refuses this for a new trial).
        return Trial(record, slot, price_test, None, UNKNOWN, (
            f"{w.product} is also in slot {', '.join(shared)}, so its sales are split and the trial measures "
            f"neither; the wildcard's product is in no other slot. Move {', '.join(shared)} to another product"))
    verdict, compared, reason = judge(record, price_test, others)
    return Trial(record, slot, price_test, compared, verdict, reason)


def _measure(count: stock.SlotCount, start: datetime, now: datetime, loads: list[tuple[datetime, int]],
             sales: tuple[stock.SaleCost, ...], readings: list[Reading], ceiling_cents: int | None) -> Measured:
    """One slot's intervals from `start`, with when its current product held stock."""
    mine = [s for s in sales if s.slot == count.slot and s.at > count.since]
    spans = demand.in_stock_spans([(at, u) for at, u in loads if at >= count.since],
                                  [(s.at, s.units) for s in mine], now, count.units)
    return Measured(count.slot, count.product, start,
                    intervals(readings, start, count.product, mine, spans, ceiling_cents))


def _readings(conn: psycopg.Connection, since: datetime, until: datetime, slots: list[str]) -> dict[str, list[Reading]]:
    """`(polled_at, lifetime_units, lifetime_sales_cents, price_cents)` per slot, from the last good poll at or
    before `since`."""
    out: dict[str, list[Reading]] = {}
    for selection, at, units, cents, price in conn.execute(
            "select s.selection, p.started_at, s.lifetime_units, s.lifetime_sales_cents, s.price_cents"
            " from poll_selections s"
            " join polls p using (session_id) where p.device_id = %s and p.crc_ok and s.selection = any(%s)"
            " and p.started_at <= %s and p.started_at >= coalesce((select max(started_at) from polls"
            "   where device_id = %s and crc_ok and started_at <= %s), %s) order by p.started_at",
            (DEVICE_ID, slots, until, DEVICE_ID, since, since)):
        out.setdefault(selection, []).append((at, units, cents, price))
    return out


def _setting(conn: psycopg.Connection, key: str, default: int) -> int:
    value = config.get_value(conn, key)
    return value if isinstance(value, int) and not isinstance(value, bool) else default
