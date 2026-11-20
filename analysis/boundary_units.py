"""Units sold in one poll interval whose two ends show different prices: which price did they sell at?

Pure and standard library only (plus vending.counters), so the tests run without a database. analysis/sales_analysis.py
uses it for the average price. The same rule as vending/stock.py `_old_units`, which splits a sale at a product change."""
from __future__ import annotations

from bisect import bisect_right
from math import gcd

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


MAX_NODES = 5_000        # the most steps a search over four or more prices may take; past it the units stay unknown


def _three(units: int, cents: int, prices: list[int]) -> tuple[int, list[tuple[int, int]] | None]:
    """`(how many whole splits fit, one of them)` of `units` and `cents` among three prices, counted up to a limit, not searched.

    With prices `a < b < c` and counts `x, y, z`: `x + y + z = units` and `a*x + b*y + c*z = cents`. Taking `a` off every
    price, `(b - a)*y + (c - a)*z = cents - a*units`: one linear equation in two counts, whose solutions are one
    solution and its steps (`y` up by `(c - a)/g`, `z` down by `(b - a)/g`). Counting the steps that keep every count whole
    and not below zero gives the number of splits. No steps, no fit; the number is exact (it may be large), so the caller
    can tell one from several. The one returned is the first, which is valid when any fits."""
    a, b, c = prices
    d1, d2, rest = b - a, c - a, cents - a * units
    g = gcd(d1, d2)
    if rest < 0 or rest % g:
        return 0, None
    d1, d2, rest = d1 // g, d2 // g, rest // g
    y0 = rest * pow(d1, -1, d2) % d2                      # the smallest y that is whole and not below zero (d1, d2 coprime)
    z0 = (rest - d1 * y0) // d2
    steps = min(z0 // d1, (units - y0 - z0) // (d2 - d1))  # z must stay >= 0, and x = units - y - z must too
    if steps < 0:
        return 0, None
    return steps + 1, [(a, units - y0 - z0), (b, y0), (c, z0)]


def split_among(units: int, cents: int | None, prices: list[int]) -> list[tuple[int, int]] | None:
    """`(price, units)` for each of `prices` when exactly one way of selling `units` at them takes in `cents`, else None.

    None means no whole split fits (a misread, or a price not seen) or several do, so nothing says which: the units are
    unknown, not credited to any price. Needs `cents` and a number of units that isn't negative; the prices are listed
    once each.

    The work is bounded however large the counters' jump is. Every sale is `units * lowest price` plus a whole
    number of the prices' differences, so a total that isn't is turned away at once (the gcd of the differences). One,
    two and three prices are solved algebraically. Four or more are searched over all but the last three, and the search
    stops after MAX_NODES steps: the units are then unknown, not guessed."""
    prices = sorted(set(prices))
    if units < 0 or cents is None or not prices:
        return None
    extra = cents - units * prices[0]
    spread = 0
    for p in prices[1:]:
        spread = gcd(spread, p - prices[0])
    if extra < 0 or (spread and extra % spread) or cents > units * prices[-1]:
        return None
    if len(prices) == 1:
        return [(prices[0], units)]
    if len(prices) == 2:
        old, rest = divmod(prices[1] * units - cents, prices[1] - prices[0])
        return [(prices[0], old), (prices[1], units - old)] if not rest and 0 <= old <= units else None
    if len(prices) == 3:
        count, split = _three(units, cents, prices)
        return split if count == 1 else None
    budget = MAX_NODES
    found: list[list[tuple[int, int]]] = []

    def walk(i: int, left: int, owed: int, taken: list[tuple[int, int]]) -> bool:
        """False when the budget ran out."""
        nonlocal budget
        if len(prices) - i == 3:
            count, split = _three(left, owed, prices[i:])
            found.extend([[*taken, *split]] * min(count, 2) if split else [])
            return True
        for n in range(left + 1):
            budget -= 1
            if budget < 0:
                return False
            after, left_over = owed - n * prices[i], left - n
            if prices[i + 1] * left_over <= after <= prices[-1] * left_over:      # what's left can still be reached
                if not walk(i + 1, left_over, after, [*taken, (prices[i], n)]):
                    return False
                if len(found) > 1:
                    return True
        return True

    if not walk(0, units, cents, []):
        return None
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
    two prices at most). A window with one price takes `split_boundary`'s rule. An end with no price reading doesn't
    stop the split: the prices that were seen are split among, and the units at the missing end's price are none.

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
        if len(seen) < 2:                       # one price all through the window (or none seen): nothing to split
            out.append((opening, closing, *split_boundary(int(units), cents, opening, closing), ()))
            continue
        split = split_among(int(units), cents, seen)          # a missing end price doesn't hide the prices that were seen
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
