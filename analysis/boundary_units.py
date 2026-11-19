"""Units sold in one poll interval whose two ends show different prices: which price did they sell at?

Pure and standard library only, so the tests run without a database. analysis/sales_analysis.py uses it for the
average price. The same rule as vending/stock.py `_old_units`, which splits a sale at a product change."""
from __future__ import annotations


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
