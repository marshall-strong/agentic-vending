"""Units sold in one poll interval whose two ends show different prices: which price did they sell at?

Pure and standard library only (plus vending.counters), so the tests run without a database. analysis/sales_analysis.py
uses it for the average price. The same rule as vending/stock.py `_old_units`, which splits a sale at a product change."""
from __future__ import annotations

from bisect import bisect_right

from vending.counters import windows


def split_boundary(units: int, cents: int | None, p_open: int | None, p_close: int | None) -> tuple[int, int, int]:
    """`(units at the opening price, units at the closing price, units unknown)` for a sale of `units` and `cents`.

    With old price `a` and new price `b`, `a*old + b*(units - old) = cents`, so `old = (b*units - cents) / (b - a)`.
    Only a whole number from 0 to `units` fits. Anything else is a misread or a third price, and the units are
    unknown: they are not credited to the opening price. A single price (or no reading before the interval) takes all
    the units."""
    if p_open is None or p_open == p_close:
        return (0, units, 0) if p_open is None else (units, 0, 0)
    if p_close is None:
        return units, 0, 0
    if cents is None:
        return 0, 0, units
    old, rest = divmod(p_close * units - cents, p_close - p_open)
    return (old, units - old, 0) if not rest and 0 <= old <= units else (0, 0, units)


def split_among(units: int, cents: int | None, prices: list[int]) -> list[tuple[int, int]] | None:
    """`(price, units)` for each of `prices` when exactly one way of selling `units` at them takes in `cents`, else None.

    None means no whole split fits (a misread, or a price not seen) or several do, so nothing says which: the units are
    unknown, not credited to any price. Needs `cents`; the prices are distinct."""
    if cents is None or not prices:
        return None
    prices = sorted(prices)
    found: list[list[tuple[int, int]]] = []

    def walk(i: int, left: int, owed: int, taken: list[tuple[int, int]]) -> None:
        if len(found) > 1:
            return
        if i == len(prices) - 1:
            if left * prices[i] == owed:
                found.append([*taken, (prices[i], left)])
            return
        rest = prices[i + 1:]
        for n in range(left + 1):
            after = owed - n * prices[i]
            if min(rest) * (left - n) <= after <= max(rest) * (left - n):       # what's left can still be reached
                walk(i + 1, left - n, after, [*taken, (prices[i], n)])

    walk(0, units, cents, [])
    return found[0] if len(found) == 1 else None


def price_sales(readings, sales) -> list[tuple[int | None, int | None, int, int, int, tuple[tuple[int, int], ...]]]:
    """What each sale of one slot sold at: `(opening price, closing price, units at opening, at closing, unknown, other)`.

    `readings`: `(at, lifetime units, lifetime cents, price)` for each poll of the slot. `sales`: `(prev, at, units,
    cents)` for each poll `at` where the counters rose, `prev` the poll before it. The opening price is the last
    reading at or before the unit counter's window start (the first reading when there is none), the closing price the
    reading at `at`. After a misread that start is the good poll before it, not `prev` (vending.counters.windows): the
    units may have sold at the price before the misread, which a misread poll's own price would hide.

    The window can hold other prices than those two (a misread widens it over several polls): a price listed and then
    changed back, say 150, 250, 150. Every price seen in the window is a price the units may have sold at, so the units
    and cents are split among all of them, when exactly one whole split fits (`split_among`); `other` is `(price,
    units)` for the units at a price that is neither the opening nor the closing one (none in the common case, with
    two prices at most). With the opening and closing price alone, or one price, the split is `split_boundary`'s.

    The units and cents of a poll can be measured from different polls (vending.counters.windows: a misread in one
    counter and not the other). Then the cents do not cover the same units, so a sale whose prices differ is not
    split: its units are unknown, and the cents stay in the slot's takings."""
    readings = sorted(readings, key=lambda r: r[0])
    times = [r[0] for r in readings]
    close = {at: price for at, _, _, price in readings}
    unit_from = {at: s for s, at, _ in windows((at, u) for at, u, _, _ in readings)}
    cents_from = {at: s for s, at, _ in windows((at, c) for at, _, c, _ in readings)}
    out = []
    for prev, at, units, cents in sales:
        i = bisect_right(times, unit_from.get(at, prev))
        first = max(i - 1, 0)
        opening = readings[first][3] if readings else None
        closing = close.get(at)
        same = unit_from.get(at) is not None and unit_from.get(at) == cents_from.get(at)
        cents = int(cents) if same else None
        seen = sorted({r[3] for r in readings[first:bisect_right(times, at)] if r[3] is not None})
        two_ends_only = len(seen) <= 1 or (len(seen) == 2 and opening != closing)     # the common case
        if opening is None or closing is None or two_ends_only:
            out.append((opening, closing, *split_boundary(int(units), cents, opening, closing), ()))
            continue
        split = split_among(int(units), cents, seen)
        if split is None:
            out.append((opening, closing, 0, 0, int(units), ()))
            continue
        by = dict(split)
        out.append((opening, closing, by.get(opening, 0), by.get(closing, 0) if closing != opening else 0, 0,
                    tuple((p, n) for p, n in split if n and p not in (opening, closing))))
    return out


def priced_average(parts) -> tuple[float | None, int]:
    """`(average price in cents, units unknown)` over the parts of `price_sales`: `(opening price, closing price, at
    opening, at closing, unknown)`, and `other` (`(price, units)` at any other price) when there is one. Unknown units
    are left out of the average; it is None when no unit has a known price."""
    parts = [(*p, ()) if len(p) == 5 else p for p in parts]
    units = sum(uo + uc + sum(n for _, n in other) for _, _, uo, uc, _, other in parts)
    total = sum((p_open * uo if uo else 0) + (p_close * uc if uc else 0) + sum(p * n for p, n in other)
                for p_open, p_close, uo, uc, _, other in parts)
    return (total / units if units else None), sum(p[4] for p in parts)


SALES_RULE = "units-weighted price in effect at each regular-era class-day sale"
NOON_RULE = "mean noon listed price on regular-era class days (no sales with a known price)"


def slot_average_price(parts, noon_mean) -> tuple[float, str, int]:
    """`(average price in cents, the rule that gave it, units unknown)` as analysis/sales_analysis.py reports a slot's.

    The average of the units whose price is known (`priced_average`); a slot with none takes `noon_mean()`, the mean
    of its noon listed price over the same days (called only then). Unknown units are counted either way."""
    avg, unknown = priced_average(parts)
    if avg is not None:
        return float(avg), SALES_RULE, unknown
    return float(noon_mean()), NOON_RULE, unknown
