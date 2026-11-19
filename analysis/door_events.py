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
from vending.counters import rises, windows

# The longest a restock visit runs, as vending/stock.py _VISIT_MAX: the door opened no more than this before the
# receipt's fulfilment.
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


def our_windows(readings, receipts, start: datetime = START) -> list[tuple[datetime, datetime, int]]:
    """`(from, at, rise)` for each door-counter rise (`readings`: `(poll time, lifetime count)`) that is a visit of ours.

    Before `start` every rise is ours: nobody's receipts exist yet and the history is read as before, from the poll
    before it. From `start`, a rise is ours only when a fulfilled receipt of ours falls with it, and `from` is the
    counter window's real start (vending.counters.windows: after a misread, the good poll before it). Each receipt claims:
    - the rises whose window contains the receipt (opened before its first line was resolved, seen no earlier than
      its fulfilment); else
    - the one rise whose window ends after the visit began, a visit running at most VISIT_MAX before `fulfilled_at`.
      With two such rises nothing says which is ours, so the receipt claims neither.
    A rise no receipt claims is left out of our visit windows. It isn't merged into ours and nothing is read from it."""
    readings = sorted(readings, key=lambda r: r[0])
    found = windows(readings)
    before = {t: p for (p, _), (t, _) in zip(readings, readings[1:])}
    ours = {at for _, at, _ in found if at < start}
    for first, done in receipts:
        later = [(s, at) for s, at, _ in found if at >= start]
        claimed = [at for s, at in later if s < first and done <= at]
        if not claimed:
            claimed = [at for s, at in later if s < done and at > done - VISIT_MAX]
            claimed = claimed if len(claimed) == 1 else []
        ours.update(claimed)
    return [(s if at >= start else before[at], at, rise) for s, at, rise in found if at in ours]
