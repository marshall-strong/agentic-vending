"""The wildcard as a testing ground: has the product on trial earned a regular slot? (#110 item 1)

The rule (`docs/agent/wildcard.md`, "A testing ground"):

1. **The trial** is the product in the wildcard slot, which is fixed in
   config (`wildcard_slot`, set once, never moved). It counts from when that
   product went in (the receipts' last product change). With no slot set
   there is no trial; a slot set that isn't one of ours now (the stored
   assignment) has no verdict.
2. **The measure** is units sold over the in-stock class days the polls saw
   (`vending.demand` counts a class day when the slot held stock as it
   began). Profit per in-stock class day (revenue without tax, less the cost
   of what sold) is reported too, for information; it decides nothing.
3. **Only what the polls saw counts.** Units and days come from the same poll
   intervals: stretches where the units and the cents counters both measured
   every sale and agree on where the stretch begins and ends. A stretch the
   polls didn't cover adds no days. One where the counters and the stock
   replay's sales disagree, or the sales aren't this product's, makes the
   verdict unknown. An interval with no sales is a zero, not a gap. An
   interval longer than `MAX_POLL_GAP` (the polls stopped) adds neither its
   days nor its sales.
4. **The bar** is absolute, because a promotion only ever fills a silent slot
   (one that sold nothing): **pass** at `PASS_UNITS` units or more over
   `TRIAL_DAYS` in-stock class days or more. Below it (research rule 6):
   `EXTEND_UNITS` extends it one Friday cycle, fewer **drop** it. By
   `LATE_DAYS` it needs `LATE_UNITS` to pass, else drop. A trial judged at an
   earlier Friday (it had `TRIAL_DAYS` by the last Friday visit) is waiting:
   it passes only while it keeps `HOLD_RATE` a day since it began.
5. **Promoted**: another regular slot took the product at or after the trial
   began. The trial is over; the wildcard switches to the next product.
6. **A trial priced above the ceiling** at its latest priced poll is a price
   test, not a promotion trial: a regular slot couldn't charge that, so its
   result wouldn't carry over (`standing_prices`). It counts again only from
   the first poll strictly after the last one that read a price above the
   ceiling, and only intervals whose polls all read a price at or under it
   count for it. A poll with no price never qualifies one. Polls at the same
   moment that read different prices settle nothing: when they are the latest
   priced moment, the price is unknown and so is the verdict.

The interval holding the load mixes the old product's sales with the new one's,
so it is left out of both units and days. Units sold past what the receipts
loaded have no cost, so they make the profit unknown rather than free.

A promotion goes only into a **silent slot** (`silent_slots`): a regular slot
that sold nothing since the last Friday visit, over `SILENT_DAYS` in-stock
class days the polls watched, with its stock confirmed.

`propose` turns a pass and a silent slot into the promotion to make at the
next Friday visit; the planner accepts that switch only when both hold.

`judge`, `slot_record`, `intervals` and `propose` are pure; `last_friday_visit`,
`standing_prices`, `trial` and `silent_slots` read the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from fractions import Fraction

import psycopg

from vending import config, counters, demand, slot_counts, stock
from vending.class_days import EASTERN, class_days_after, eastern_date
from vending.pricing import DEFAULT_TAX_PERCENT
from vending.slots import DEVICE_ID
from vending.tools.sales_history import tax_cents

TRIAL_DAYS = demand.OWN_RATE_AFTER   # in-stock class days before a trial is judged
PASS_UNITS = 4                       # units over TRIAL_DAYS to pass (Kevin, 18 Nov)
EXTEND_UNITS = 3                     # below the bar: one more Friday cycle (research rule 6)
LATE_DAYS = 10                       # by then it needs LATE_UNITS to pass, else drop
LATE_UNITS = 7
HOLD_RATE = Fraction(7, 10)          # units a day a passing trial keeps while it waits for a silent slot

# The longest stretch between two polls that still counts as watched. David polls hourly; a longer gap
# means the polls stopped, and the class days and sales inside it weren't seen, so they count for nothing.
MAX_POLL_GAP = timedelta(hours=6)

TOO_EARLY, PRICE_TEST, PASS, EXTEND, DROP, PROMOTED, UNKNOWN = (
    "too early", "price test", "pass", "extend", "drop", "promoted", "unknown")

# `(polled_at, lifetime_units, lifetime_sales_cents, price_cents)`
Reading = tuple[datetime, int, int, int | None]


@dataclass(frozen=True)
class SlotRecord:
    """What one slot's current product sold from `since`, over the class days it held stock."""
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
class Trial:
    record: SlotRecord | None         # None: nothing to measure (no receipts, not ours, or the price is unknown)
    slot: str | None                  # None: the configured slot isn't one of ours now
    price_test: bool
    verdict: str
    reason: str
    price_cents: int | None = None    # its standing price; None when unknown


@dataclass(frozen=True)
class Interval:
    """One stretch, at most `MAX_POLL_GAP` long, in which both counters measured every sale of a slot."""
    begin: datetime
    end: datetime
    units: int
    takings_cents: int                # tax inside
    agree: bool                       # the replay's sales in it are the counters' units, all of this product
    cost_cents: Fraction | None       # None: a cost is unknown, or the counters and the sales don't agree
    days: tuple[date, ...]            # the class days in it the slot held stock as they began


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

    @property
    def agree(self) -> bool:
        return all(i.agree for i in self.intervals)

    def record(self, tax_percent: int, until: datetime | None = None) -> SlotRecord:
        """Its record over its intervals, or those that ended by `until`: units, money and class days. Pure."""
        chosen = [i for i in self.intervals if until is None or i.end <= until]
        days = {d for i in chosen for d in i.days}
        return slot_record(self.slot, self.product, self.since, len(days), sum(i.units for i in chosen),
                           sum(i.takings_cents for i in chosen), [i.cost_cents for i in chosen], tax_percent)


def judge(trial: SlotRecord, price_test: bool, waiting: bool = False) -> tuple[str, str]:
    """The verdict on a trial, and why, in words the agent can quote. Pure.

    `waiting`: it was judged at an earlier Friday already (it had `TRIAL_DAYS`
    by the last Friday visit), so it is extended or waiting for a silent slot.
    """
    days, units = trial.class_days_in_stock, trial.units_sold
    if days < TRIAL_DAYS:
        return TOO_EARLY, (f"{days} of {TRIAL_DAYS} in-stock class days the polls have seen so far; judge it at "
                           "the first Friday visit after it has them")
    if price_test:
        return PRICE_TEST, ("it sells above the ceiling, so this is a price test, not a promotion trial: a regular "
                            "slot couldn't charge that. Price it at or under the ceiling to test it for promotion; "
                            "the trial then counts from the first poll that reads that price")
    seen = f"{units} sold over {days} in-stock class days the polls saw"
    holds = units >= HOLD_RATE * days
    switch = f"At the next Friday visit, switch slot {trial.slot} to the next product to test"
    passed = (f"{seen}: it passes. Promote it at a Friday visit into a silent slot; while none is silent, keep it "
              f"in the wildcard and keep selling. To stay eligible it must keep {_rate(HOLD_RATE)} a day since it "
              f"began, and {LATE_UNITS} by {LATE_DAYS} days")
    if days >= LATE_DAYS:
        if units >= LATE_UNITS and holds:
            return PASS, passed
        return DROP, (f"{seen}: by {LATE_DAYS} days it needs {LATE_UNITS}, and {_rate(HOLD_RATE)} a day since it "
                      f"began, so drop it. {switch}")
    if not waiting:
        if units >= PASS_UNITS:
            return PASS, passed
        if units == EXTEND_UNITS:
            return EXTEND, (f"{seen}, one under the bar of {PASS_UNITS}: extend it one Friday cycle. It is judged "
                            f"again with {LATE_DAYS} days in, and needs {LATE_UNITS} by then")
        return DROP, f"{seen}, under the bar of {PASS_UNITS}, so drop it. {switch}"
    # Judged at an earlier Friday, still under LATE_DAYS (a break paused the clock).
    if units >= PASS_UNITS and holds:
        return PASS, passed
    if units < EXTEND_UNITS:
        return DROP, f"{seen}, under the bar of {PASS_UNITS}, so drop it. {switch}"
    return EXTEND, (f"{seen}: under {_rate(HOLD_RATE)} a day since it began, so it isn't eligible now; it is judged "
                    f"again with {LATE_DAYS} days in, and needs {LATE_UNITS} by then")


def _rate(rate: Fraction) -> str:
    return f"{float(rate):.1f}"


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
        # The replay's sales in it must be the counters' units, all of this product, or they aren't this
        # interval's. Units without takings, or takings without units, can't be reconciled either.
        agree = (sum(s.units for s in inside) == units and (units > 0) == (takings > 0)
                 and all(s.product == product and s.taken_at is None for s in inside))
        costed = agree and all(s.cost_cents is not None and s.costed_units == s.units for s in inside)
        cost = sum((s.cost_cents for s in inside), Fraction(0)) if costed else None
        days = class_days_after(begin, end)
        out.append(Interval(begin, end, units, takings, agree, cost, tuple(d for d in days if d in in_stock)))
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


def last_friday_visit(conn: psycopg.Connection, now: datetime) -> tuple[datetime | None, str | None]:
    """Eastern midnight of the Friday on or before the last Friday visit, or None and why not.

    The last Friday visit is the latest fulfilled request that isn't off-cycle,
    fulfilled by `now`. `fulfilled_at` may trail the visit, so the Friday
    midnight before it is the safe start: it makes a window from the visit
    longer, never shorter. A fulfilled Friday request with no `fulfilled_at`
    can't be placed, so the visit is unknown.
    """
    rows = conn.execute("select request_id, fulfilled_at from purchase_requests"
                        " where status = 'fulfilled' and not off_cycle").fetchall()
    unplaced = sorted(request_id for request_id, at in rows if at is None)
    if unplaced:
        return None, (f"Friday request {', '.join(unplaced)} is fulfilled but its fulfilled_at is null, so when the "
                      "last Friday visit was is unknown; run the request sync (python -m vending.sync_requests) so "
                      "the time is recorded, or ask a person to check the request")
    done = [at for _, at in rows if at <= now]
    if not done:
        return None, "no Friday visit is recorded yet"
    day = eastern_date(max(done))
    friday = day - timedelta(days=(day.weekday() - 4) % 7)
    return datetime.combine(friday, time(0), tzinfo=EASTERN), None


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
    `trial` reads it here.
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
          slots: tuple[str, ...], slot: str | None) -> Trial | None:
    """The running trial in the wildcard `slot` (config `wildcard_slot`), judged; None when no slot is set.

    `slots` are ours (the stored assignment), `counts` our slots' receipted
    counts and `loads` every load per slot, as `get_machine_state` reads them;
    `sales` is the stock replay's.
    """
    if slot is None:
        return None
    if slot not in slots:
        return Trial(None, None, False, UNKNOWN, (
            f"config wildcard_slot is {slot}, which isn't one of ours in the stored slot assignment, so no trial "
            "can be judged and preview and submit refuse; a person checks the assignment and the setting"))
    w = counts.get(slot)
    if w is None:
        return Trial(None, slot, False, UNKNOWN,
                     f"slot {slot} is the wildcard, but no receipts show what it holds, so there is nothing to measure")
    ceiling_cents, tax_percent = _setting(conn, "price_ceiling_cents", 650), _setting(conn, "tax_percent", DEFAULT_TAX_PERCENT)
    start = w.since
    readings = _readings(conn, start, now, [slot]).get(slot, [])
    standing = standing_prices(conn, [slot]).get(slot)
    if standing is not None and not standing.known:
        return Trial(None, slot, False, UNKNOWN, (
            f"the polls at {standing.at:%Y-%m-%d %H:%M} UTC read different prices for slot {slot} "
            f"({', '.join(f'${p // 100}.{p % 100:02d}' for p in sorted(standing.prices))}), so its price is "
            "unknown and so is whether it is a price test; a later poll that reads one price settles it"))
    price = min(standing.prices) if standing is not None else None
    # Above the ceiling at the latest poll that read a price: a poll with no price changes nothing.
    price_test = standing is not None and bool(standing.above(ceiling_cents))
    priced = [(at, p) for at, _, _, p in readings if at >= start and p is not None]
    above = [at for at, p in priced if p > ceiling_cents]
    if above and not price_test:
        # It counts from the first poll strictly after the last one above the ceiling: the interval before it
        # may hold sales at the old price. A poll with no price doesn't count as back under.
        back = next((at for at, p in priced if at > above[-1]), None)
        if back is None:   # can't happen while the standing price is under the ceiling; refuse rather than guess
            return Trial(None, slot, False, UNKNOWN, (
                f"no poll after the last one that read slot {slot} above the ceiling has read its price, so "
                "whether its price test has ended is unknown"), price)
        start = back
    measured = _measure(w, start, now, loads.get(slot, []), sales, readings, None if price_test else ceiling_cents)
    record = measured.record(tax_percent)
    others = {s: c for s, c in counts.items() if s != slot and c.product == w.product}
    before = sorted(s for s, c in others.items() if c.since < w.since)
    if before:
        # Its sales are split with a slot that held it first, so its rate isn't its own.
        return Trial(record, slot, price_test, UNKNOWN, (
            f"{w.product} is also in slot {', '.join(before)}, so its sales are split and the trial measures "
            f"neither; the wildcard's product is in no other slot. Switch slot {slot} to another product"), price)
    if others:
        took = min(c.since for c in others.values())
        return Trial(record, slot, price_test, PROMOTED, (
            f"slot {', '.join(sorted(others))} took {w.product} on {took:%Y-%m-%d}, after its trial began: the "
            f"trial is over. At the next Friday visit, switch slot {slot} to the next product to test"), price)
    if not measured.agree:
        return Trial(record, slot, price_test, UNKNOWN, (
            "in some poll interval the units and takings counters, or the stock replay's sales, don't agree, so "
            "what it sold is unknown; a person checks the polls and receipts"), price)
    visit, _ = last_friday_visit(conn, now)
    waiting = visit is not None and measured.record(tax_percent, visit).class_days_in_stock >= TRIAL_DAYS
    verdict, reason = judge(record, price_test, waiting)
    return Trial(record, slot, price_test, verdict, reason, price)


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


# --- Silent slots -------------------------------------------------------------
# A promotion fills a silent slot: a regular slot that sold nothing since the last
# Friday visit while it held stock (Kevin, 18 Nov). Never one that sold.

SILENT_DAYS = 4             # in-stock class days the polls must have seen since the visit: Monday to Thursday
SEEN_UNTIL = time(18, 0)    # Eastern: a class day counts once the polls watched it to here, so its sales are in


@dataclass(frozen=True)
class Silent:
    slot: str
    product: str
    class_days_seen: int              # in-stock class days the polls watched since the window began
    quiet_since: datetime             # the later of its last sale and its last load
    unit_margin_cents: Fraction | None   # latest polled price without tax, less unit cost; None if either is unknown
    evidence: str                     # what confirms it held stock


@dataclass(frozen=True)
class Unconfirmed:
    """Sold nothing, but nothing confirms it held stock: maybe empty, not silent."""
    slot: str
    product: str
    why: str


@dataclass(frozen=True)
class NotSilent:
    slot: str
    why: str


@dataclass(frozen=True)
class SilentReport:
    window_start: datetime | None     # the last Friday visit's Friday, 00:00 Eastern; None: unknown or none yet
    silent: tuple[Silent, ...]        # ranked: silent longest, then lowest unit margin, then slot
    unconfirmed: tuple[Unconfirmed, ...]
    not_silent: tuple[NotSilent, ...]
    reason: str | None = None         # why `window_start` is None


def silent_slots(conn: psycopg.Connection, now: datetime, counts: dict[str, stock.SlotCount],
                 loads: dict[str, list[tuple[datetime, int]]], sales: tuple[stock.SaleCost, ...],
                 slots: tuple[str, ...], wildcard: str | None) -> SilentReport:
    """Which of our regular slots are silent: sold nothing since the last Friday visit, while holding stock.

    A slot is silent when all of these hold, over the window from 00:00 Eastern
    on the Friday of the last Friday visit (`last_friday_visit`) to `now`:

    1. Its units counter rose in no poll window ending in it, however long the
       window: one sale inside a gap must not be missed. A lone drop the next
       poll hasn't settled, or no good poll since the window began, makes it
       unknown, not silent.
    2. It held one product the whole window (its last product change is at
       or before the window's start).
    3. Its stock is confirmed: the receipts say it holds units now, and a
       receipt shows units loaded into it at or since the visit, or a person's
       count since the visit saw units in it. Otherwise it is unconfirmed,
       maybe empty, not silent; a count since the visit that saw it empty
       says so.
    4. The polls watched `SILENT_DAYS` in-stock class days of it in the window
       (intervals no longer than `MAX_POLL_GAP`, each day watched from its
       midnight to `SEEN_UNTIL`). With fewer it isn't silent yet.

    The wildcard is never a silent slot, nor is a slot no receipt describes.
    """
    regular = [s for s in slots if s != wildcard]
    start, why = last_friday_visit(conn, now)
    if start is None:
        return SilentReport(None, (), (), tuple(NotSilent(s, why) for s in regular), why)
    readings = _readings(conn, start, now, regular)
    seen = _counted(conn)
    tax_percent = _setting(conn, "tax_percent", DEFAULT_TAX_PERCENT)
    silent: list[Silent] = []
    unconfirmed: list[Unconfirmed] = []
    not_silent: list[NotSilent] = []
    for slot in regular:
        c = counts.get(slot)
        if c is None:
            not_silent.append(NotSilent(slot, "no receipts say what it holds"))
            continue
        mine = readings.get(slot, [])
        units = [(at, u) for at, u, _, _ in mine]
        if not any(at > start for at, _ in units):
            not_silent.append(NotSilent(slot, "unknown: no good poll since the window began"))
            continue
        if counters.unsettled(units):
            not_silent.append(NotSilent(slot, "unknown: its units counter's latest reading is a drop the next "
                                              "poll hasn't settled"))
            continue
        sold = sum(rise for _, at, rise in counters.windows(units) if at > start)
        if sold:
            not_silent.append(NotSilent(slot, f"sold {sold} since the window began"))
            continue
        if c.since > start:
            not_silent.append(NotSilent(slot, f"its product changed on {c.since:%Y-%m-%d}"))
            continue
        loaded = [(at, n) for at, n in loads.get(slot, []) if at >= start]
        look = seen.get(slot)
        look = look if look is not None and look["counted_at"] >= start else None
        if c.units <= 0:
            unconfirmed.append(Unconfirmed(slot, c.product, "the receipts say it is empty"))
            continue
        if look is not None and look["units_counted"] == 0:
            unconfirmed.append(Unconfirmed(slot, c.product, (
                f"a person counted it empty on {look['counted_at']:%Y-%m-%d}")))
            continue
        if look is not None and look["units_counted"]:
            evidence = (f"{look['source']} count {look['count_id']} on {look['counted_at']:%Y-%m-%d}: "
                        f"{look['units_counted']} units")
        elif loaded:
            evidence = f"loaded {sum(n for _, n in loaded)} at the visit on {loaded[0][0]:%Y-%m-%d}"
        else:
            unconfirmed.append(Unconfirmed(slot, c.product, (
                "no receipt shows units loaded at the last Friday visit and no count since saw it, so it may be "
                "empty; a count of it confirms it")))
            continue
        days = len(_watched_days(_measure(c, start, now, loads.get(slot, []), sales, mine, None).intervals))
        if days < SILENT_DAYS:
            not_silent.append(NotSilent(slot, f"{days} of {SILENT_DAYS} in-stock class days seen since the window "
                                              "began; not silent yet"))
            continue
        last_sale = max((s.at for s in sales if s.slot == slot), default=None)
        last_load = max((at for at, _ in loads.get(slot, [])), default=None)
        quiet = max(at for at in (last_sale, last_load, c.since) if at is not None)
        price = next((p for _, _, _, p in reversed(mine) if p is not None), None)
        margin = (None if price is None or c.unit_cost is None
                  else Fraction(price - tax_cents(price, tax_percent)) - c.unit_cost)
        silent.append(Silent(slot, c.product, days, quiet, margin, evidence))
    silent.sort(key=lambda s: (s.quiet_since, s.unit_margin_cents is None, s.unit_margin_cents or 0, s.slot))
    return SilentReport(start, tuple(silent), tuple(unconfirmed), tuple(not_silent))


def _watched_days(kept: list[Interval]) -> list[date]:
    """The in-stock class days in `kept` the polls watched from midnight to `SEEN_UNTIL`, Eastern. Pure."""
    covered: list[list[datetime]] = []
    for i in sorted(kept, key=lambda i: i.begin):
        if covered and i.begin <= covered[-1][1]:
            covered[-1][1] = max(covered[-1][1], i.end)
        else:
            covered.append([i.begin, i.end])
    out = []
    for day in sorted({d for i in kept for d in i.days}):
        lo, hi = datetime.combine(day, time(0), tzinfo=EASTERN), datetime.combine(day, SEEN_UNTIL, tzinfo=EASTERN)
        if any(a <= lo and hi <= b for a, b in covered):
            out.append(day)
    return out


def _counted(conn: psycopg.Connection) -> dict[str, dict]:
    """The latest person's count per selection (`vending.slot_counts.latest`), or none if the table isn't there."""
    try:
        with conn.transaction():
            return slot_counts.latest(conn, DEVICE_ID)
    except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
        return {}


# --- The proposal -------------------------------------------------------------

@dataclass(frozen=True)
class Proposal:
    """A passing trial's promotion into the top-ranked silent slot, for the next Friday plan."""
    into_slot: str
    product: str
    trial_price_cents: int | None     # what it sold at in the wildcard; the new slot's price is the agent's pick
    pulls: str                        # what the silent slot holds now, which goes to the closet
    wildcard_next: str
    why: str


def propose(t: Trial | None, report: SilentReport, counts: dict[str, stock.SlotCount],
            ceiling_cents: int) -> Proposal | None:
    """The promotion to make at the next Friday visit, or None: only a pass, only into a silent slot. Pure.

    The planner (`vending.planner`, the promotion gate) checks the same two things again on the plan.
    """
    if t is None or t.verdict != PASS or t.record is None or not report.silent or report.window_start is None:
        return None
    target, r = report.silent[0], t.record
    held = counts[target.slot]
    price = "" if t.price_cents is None else f" at ${t.price_cents // 100}.{t.price_cents % 100:02d}"
    return Proposal(
        target.slot, r.product, t.price_cents,
        f"{held.product} ({held.units} units to the closet)",
        f"switch slot {t.slot} to the next product to test in the same plan",
        (f"{r.units_sold} sold over {r.class_days_in_stock} in-stock class days{price}; slot {target.slot} sold "
         f"nothing since {report.window_start:%a %d %b} (stock confirmed: {target.evidence}). Load {r.product} "
         f"into slot {target.slot} at a price you pick, at most ${ceiling_cents // 100}.{ceiling_cents % 100:02d}"))
