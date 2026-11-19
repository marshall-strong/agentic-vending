"""The machine's door-open counter (EVA-DTS EA2*DO) in a raw poll, and its rises: the rows of door_events.csv.

Pure and standard library only (plus vending.counters, which is too), so the tests run without a database.
analysis/export_sales.py writes the file; analysis/visit_windows.py and analysis/sales_analysis.py read it.

The door counter is machine-wide (docs/integrity.md): it dates visits and is never used per slot.
"""
from __future__ import annotations

import csv
import re
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path

from slots_config import START
from vending.counters import rises

# The longest a restock visit runs, as vending/stock.py _VISIT_MAX: the door opened no more than this before the
# receipt's first line was resolved.
VISIT_MAX = timedelta(hours=1)


def door_counter(raw) -> tuple[int, int] | None:
    """(since_reset, lifetime) from the EA2*DO record of one raw EVA-DTS poll, or None if absent.
    EA2 = event counters; event DO = door open. Segments end in CR/LF, fields split on '*'."""
    for seg in re.split(r"[\r\n]+", bytes(raw).decode("latin-1")):
        f = seg.split("*")
        if len(f) >= 4 and f[0] == "EA2" and f[1] == "DO":
            try:
                return int(f[2]), int(f[3])
            except ValueError:
                return None
    return None


def door_rows(readings: Iterable[tuple[datetime, int, int]]) -> list[tuple[datetime, int, int, int]]:
    """`(at, lifetime, since_reset, rise)` for every `(at, since_reset, lifetime)` reading, oldest first.

    The rise is the lifetime counter's rise at that poll, found with vending.counters.rises, the project's one rule
    for a reading that goes down: a lone low reading is a misread until the next poll agrees, so (1000, 5, 1000)
    rises by 0, not by 995 at the third poll. A real restart (1000, 5, 7) rises by 2 at the third poll."""
    readings = sorted(readings, key=lambda r: r[0])
    up = dict(rises((at, life) for at, _, life in readings))
    return [(at, life, since, up.get(at, 0)) for at, since, life in readings]


def read_receipts(path) -> list[tuple[datetime, datetime]]:
    """`(first line resolved, fulfilled_at)` of each fulfilled receipt of ours in receipts.csv, or [] without the file.
    export_sales.py writes it from purchase_requests and purchase_lines."""
    path = Path(path)
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return [(datetime.fromisoformat(r["first_at_eastern"]), datetime.fromisoformat(r["fulfilled_at_eastern"]))
                for r in csv.DictReader(f)]


def is_our_rise(prev: datetime, at: datetime, receipts, start: datetime = START) -> bool:
    """Is the door-counter rise seen at poll `at` (the poll before it was `prev`) a visit of ours?

    Before the start, yes: nobody's receipts exist yet and the history is read as before. From the start, only when a
    fulfilled receipt of ours falls with it: the door opened in (prev, at], and a visit ends at its `fulfilled_at`
    and runs at most VISIT_MAX, so the receipt's span must reach that interval. Any other rise is left out of our
    visit windows. It isn't merged into ours and nothing is read from it."""
    if at < start:
        return True
    return any(prev < done and at > first - VISIT_MAX for first, done in receipts)
