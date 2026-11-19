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


def price_sales(readings, sales) -> list[tuple[int | None, int | None, int, int, int]]:
    """What each sale of one slot sold at: `(opening price, closing price, units at opening, at closing, unknown)`.

    `readings`: `(at, lifetime units, lifetime cents, price)` for each poll of the slot. `sales`: `(prev, at, units,
    cents)` for each poll `at` where the counters rose, `prev` the poll before it. The opening price is the last
    reading at or before `prev` (the first reading when there is none), the closing price the reading at `at`.

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
        i = bisect_right(times, prev)
        opening = readings[max(i - 1, 0)][3] if readings else None
        same = unit_from.get(at) is not None and unit_from.get(at) == cents_from.get(at)
        out.append((opening, close.get(at), *split_boundary(int(units), int(cents) if same else None, opening, close.get(at))))
    return out


def priced_average(parts) -> tuple[float | None, int]:
    """`(average price in cents, units unknown)` over `(opening price, closing price, at opening, at closing, unknown)`
    parts of `price_sales`. Unknown units are left out of the average; it is None when no unit has a known price."""
    parts = list(parts)
    units = sum(uo + uc for _, _, uo, uc, _ in parts)
    total = sum((p_open * uo if uo else 0) + (p_close * uc if uc else 0) for p_open, p_close, uo, uc, _ in parts)
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
