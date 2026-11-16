"""Rises in a lifetime counter, with one rule for a reading that goes down.

The machine's lifetime counters (units and sales cents per selection) only ever
go up, so the difference between two polls is what sold between them. A reading
below the one before it is either a board swap (the counter really restarted)
or a misread of one poll. The two look the same until the next poll:

- if the next reading is back at or above the old high, the low one was a
  misread, and the rise is measured from the old high, not from the low value;
- if the next reading is still below the old high, the counter really
  restarted, and the rise is measured from the first low reading.

Without that rule a single misread between good polls books the whole gap as
sales (100000 -> 500 -> 100250 would count 99750 instead of 250). A lone drop
with no poll after it counts nothing yet.

Both `vending.stock` (units) and `vending.ledger` (takings) read counters
through this module, so they agree on every sale.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime


def rises(readings: Iterable[tuple[datetime, int]]) -> list[tuple[datetime, int]]:
    """`(at, rise)` for every poll at which the counter rose, oldest first. Pure."""
    return [(at, rise) for _, at, rise in windows(readings)]


def windows(readings: Iterable[tuple[datetime, int]]) -> list[tuple[datetime, datetime, int]]:
    """`(start, at, rise)`: each rise with the poll it is measured from. Pure.

    The units in a rise sold somewhere between `start` and `at`, so a restock
    inside that window can't be placed before or after them (#97). After a
    misread, `start` is the good poll before it, not the misread.
    """
    out: list[tuple[datetime, datetime, int]] = []
    previous: tuple[datetime, int] | None = None
    pending: tuple[datetime, int] | None = None   # one reading below `previous`, waiting for the next poll to agree
    for at, value in sorted(readings, key=lambda r: r[0]):
        if previous is None:
            previous = (at, value)
            continue
        if value >= previous[1]:
            if value > previous[1]:
                out.append((previous[0], at, value - previous[1]))
            previous, pending = (at, value), None
        elif pending is None:
            pending = (at, value)
        else:
            # Two polls in a row below the old high: the counter restarted at `pending`.
            if value > pending[1]:
                out.append((pending[0], at, value - pending[1]))
            previous, pending = (at, value), None
    return out
