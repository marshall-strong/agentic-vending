"""The wildcard as a testing ground: has the product on trial earned a regular slot? (#110 item 1)

The rule (`docs/agent/wildcard.md`, "A testing ground"):

1. **The trial** is the product in the slot the latest accepted plan named as
   the wildcard, from when that product went in. A plan that names no wildcard
   ends the trial.
2. **The measure** is profit per in-stock class day: revenue without tax, less
   the cost of what sold, over the class days the slot held stock
   (`vending.demand`). The restock policy ranks slots by the same yardstick.
3. **After `TRIAL_DAYS` in-stock class days**, the point where a slot's own
   rate counts, compare it with our other slots over the same days: each from
   when the trial began, or from when its own product went in if that was
   later. Only slots with `TRIAL_DAYS` in-stock class days of their own in that
   time count.
4. **Promote** when the trial earns more per day than the weakest of them; else
   **drop** it. A promoted product stays in its slot, which becomes a regular
   slot, and the weakest slot's product goes to the closet; that slot becomes the
   wildcard with the next product to test. Either way the agent acts at a Friday
   visit.
5. **A trial priced above the ceiling** at any poll is a price test, not a
   promotion trial: a regular slot couldn't charge that, so its profit wouldn't
   carry over.

Revenue and costs count only the poll windows that began after the start. The
window holding the load mixes the old product's sales with the new one's, so it
is left out of both.

`judge` and `slot_record` are pure; `current_wildcard` and `trial` read the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction

import psycopg

from vending import config, counters, demand, stock
from vending.pricing import DEFAULT_TAX_PERCENT
from vending.slots import DEVICE_ID
from vending.tools.sales_history import tax_cents

TRIAL_DAYS = demand.OWN_RATE_AFTER   # in-stock class days before a trial is judged

TOO_EARLY, PROMOTE, DROP, PRICE_TEST, UNKNOWN = "too early", "promote", "drop", "price test", "unknown"


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
class Trial:
    record: SlotRecord | None         # None: no receipts for the wildcard slot, so nothing to measure
    slot: str
    price_test: bool
    weakest: SlotRecord | None
    verdict: str
    reason: str


def slot_record(slot: str, product: str, since: datetime, class_days: int, units: int,
                takings_cents: int, costs: list[Fraction | None], tax_percent: int) -> SlotRecord:
    """A slot's record from its takings (tax inside) and the cost of each sale in the same windows. Pure."""
    known = all(c is not None for c in costs)
    profit = Fraction(takings_cents - tax_cents(takings_cents, tax_percent)) - sum(costs) if known else None
    return SlotRecord(slot, product, since, class_days, units, profit)


def judge(trial: SlotRecord, price_test: bool, others: list[SlotRecord]) -> tuple[str, SlotRecord | None, str]:
    """The verdict on a trial, the weakest slot it was compared with, and why, in words the agent can quote. Pure."""
    if trial.class_days_in_stock < TRIAL_DAYS:
        return TOO_EARLY, None, (f"{trial.class_days_in_stock} of {TRIAL_DAYS} in-stock class days so far; "
                                 "judge it at the first Friday visit after it has them")
    if price_test:
        return PRICE_TEST, None, ("it sold above the ceiling during the trial, so this is a price test, not a "
                                  "promotion trial: a regular slot couldn't charge that. Price it at or under the "
                                  "ceiling to test it for promotion")
    ppd = trial.profit_per_class_day_cents
    if ppd is None:
        return UNKNOWN, None, "the cost of some of its sales is unknown, so its profit is too"
    rivals = [o for o in others if o.class_days_in_stock >= TRIAL_DAYS and o.profit_per_class_day_cents is not None]
    if not rivals:
        return UNKNOWN, None, (f"no other slot has {TRIAL_DAYS} in-stock class days with a known profit over the "
                               "same days yet, so there is nothing to compare it with")
    weakest = min(rivals, key=lambda o: (o.profit_per_class_day_cents, o.slot))
    if ppd > weakest.profit_per_class_day_cents:
        return PROMOTE, weakest, (
            f"{_money(ppd)} profit per in-stock class day beats slot {weakest.slot}'s {_money(weakest.profit_per_class_day_cents)} "
            f"({weakest.product}), the weakest of our other slots over the same days. At the next Friday visit, "
            f"keep {trial.product} in slot {trial.slot} as a regular slot, send {weakest.product} to the closet, "
            f"and make slot {weakest.slot} the wildcard with the next product to test")
    return DROP, weakest, (
        f"{_money(ppd)} profit per in-stock class day doesn't beat slot {weakest.slot}'s "
        f"{_money(weakest.profit_per_class_day_cents)} ({weakest.product}), the weakest of our other slots over the "
        f"same days. At the next Friday visit, switch slot {trial.slot} to the next product to test")


def _money(cents: Fraction) -> str:
    sign = "-" if cents < 0 else ""
    whole = round(abs(cents))
    return f"{sign}${whole // 100}.{whole % 100:02d}"


def current_wildcard(conn: psycopg.Connection) -> tuple[str | None, datetime | None]:
    """The slot the latest accepted plan named as the wildcard (None: it named none), and when it was sent.

    A request the bridge rejected moved nothing, so it doesn't count.
    """
    row = conn.execute(
        "select s.wildcard, s.at from purchase_submissions s left join purchase_requests r using (request_id)"
        " where s.status = 'accepted' and r.status is distinct from 'rejected' order by s.at desc, s.id desc limit 1"
    ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def trial(conn: psycopg.Connection, now: datetime, counts: dict[str, stock.SlotCount],
          loads: dict[str, list[tuple[datetime, int]]], sales: tuple[stock.SaleCost, ...]) -> Trial | None:
    """The running trial, judged against our other slots; None when no wildcard is named.

    `counts` are our slots' receipted counts and `loads` every load per slot, as
    `get_machine_state` reads them; `sales` is the stock replay's.
    """
    slot, _ = current_wildcard(conn)
    if slot is None:
        return None
    w = counts.get(slot)
    if w is None:
        return Trial(None, slot, False, None, UNKNOWN,
                     f"slot {slot} is the wildcard, but no receipts show what it holds, so there is nothing to measure")
    ceiling_cents, tax_percent = _setting(conn, "price_ceiling_cents", 650), _setting(conn, "tax_percent", DEFAULT_TAX_PERCENT)
    readings = _readings(conn, w.since, now, list(counts))
    records = {s: _record(c, max(w.since, c.since), now, loads.get(s, []), sales, readings.get(s, []), tax_percent)
               for s, c in counts.items()}
    price_test = any(price is not None and price > ceiling_cents
                     for at, _, _, price in readings.get(slot, []) if at > w.since)
    verdict, weakest, reason = judge(records[slot], price_test, [r for s, r in sorted(records.items()) if s != slot])
    return Trial(records[slot], slot, price_test, weakest, verdict, reason)


def _record(count: stock.SlotCount, start: datetime, now: datetime, loads: list[tuple[datetime, int]],
            sales: tuple[stock.SaleCost, ...], readings: list[tuple[datetime, int, int, int | None]],
            tax_percent: int) -> SlotRecord:
    """One slot's record from `start`: in-stock days from its product's spans, money from whole windows after it."""
    mine = [s for s in sales if s.slot == count.slot and s.at > count.since]
    spans = demand.in_stock_spans([(at, u) for at, u in loads if at >= count.since],
                                  [(s.at, s.units) for s in mine], now, count.units)
    clipped = [(max(a, start), b) for a, b in spans if b > start]
    days = len(demand.class_days_in_stock(clipped))
    takings = sum(rise for begin, _, rise in counters.windows([(at, cents) for at, _, cents, _ in readings])
                  if begin >= start)
    # The replay books each sale to the poll that ended its window; keep those whose window began after `start`.
    whole = {at for begin, at, _ in counters.windows([(at, units) for at, units, _, _ in readings]) if begin >= start}
    counted = [s for s in mine if s.at in whole and s.taken_at is None and s.product == count.product]
    return slot_record(count.slot, count.product, start, days, sum(s.units for s in counted),
                       takings, [s.cost_cents for s in counted], tax_percent)


def _readings(conn: psycopg.Connection, since: datetime, until: datetime,
              slots: list[str]) -> dict[str, list[tuple[datetime, int, int, int | None]]]:
    """`(polled_at, lifetime_units, lifetime_sales_cents, price_cents)` per slot, from the last good poll at or
    before `since`."""
    out: dict[str, list[tuple[datetime, int, int, int | None]]] = {}
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
