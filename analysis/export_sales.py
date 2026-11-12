"""Export the sales history of our 20 slots, plus machine-wide totals, from the database to CSV (read-only).

Run from the repo root with the project's venv and a DATABASE_URL in .env:
    python analysis/export_sales.py            # writes analysis/data/*.csv
    python analysis/export_sales.py OUT_DIR

Scope: per-slot rows are written ONLY for our slots (`vending.slots.SLOTS` on `vending.slots.DEVICE_ID`), filtered
the same way as `vending.ledger._counter_rows`. The other slots in the machine are read only to add them into
machine-wide totals per poll (machine_hourly.csv); no per-slot value of another slot is written anywhere.

Writes:
    poll_readings.csv   every poll x OUR slot: lifetime counters and the current price
    sales_events.csv    one row per poll where one of OUR slots' counters rose (vending.counters.rises, misread-safe)
    polls.csv           the time of every poll, so gaps are not read as zero sales
    machine_hourly.csv  every poll: units and cents the WHOLE machine sold since the previous poll (sum over all
                        selections, each slot's rise found with vending.counters.rises). Aggregates only.
    door_events.csv     every poll's EVA-DTS door-open counter (EA2*DO*<since reset>*<lifetime>) and its rise
                        since the previous poll: a rise means the door was opened (a visit) in between
"""
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vending import db  # noqa: E402
from vending.counters import rises  # noqa: E402
from vending.slots import DEVICE_ID, SLOTS  # noqa: E402

EASTERN = ZoneInfo("America/New_York")
out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "data"
out.mkdir(parents=True, exist_ok=True)

with db.connect() as c:
    c.execute("set default_transaction_read_only = on")
    # our slots: same filter as vending.ledger._counter_rows
    rows = c.execute(
        "select ps.selection, p.started_at, ps.lifetime_units, ps.lifetime_sales_cents, ps.price_cents"
        " from poll_selections ps join polls p using (session_id)"
        " where p.device_id = %s and p.crc_ok and ps.selection = any(%s) order by ps.selection, p.started_at",
        (DEVICE_ID, list(SLOTS))).fetchall()
    # every selection, for machine-wide totals only (never written per slot)
    machine_rows = c.execute(
        "select ps.selection, p.started_at, ps.lifetime_units, ps.lifetime_sales_cents"
        " from poll_selections ps join polls p using (session_id)"
        " where p.device_id = %s and p.crc_ok order by ps.selection, p.started_at", (DEVICE_ID,)).fetchall()
    polls = c.execute(
        "select started_at from polls where device_id = %s and crc_ok order by started_at", (DEVICE_ID,)).fetchall()
    raws = c.execute(
        "select started_at, raw from polls where device_id = %s and crc_ok order by started_at", (DEVICE_ID,)).fetchall()


def door_counter(raw):
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


ours = set(SLOTS)
by_slot: dict[str, list] = {}
for sel, at, units, cents, price in rows:
    assert sel in ours
    by_slot.setdefault(sel, []).append((at, units, cents, price))

with open(out / "poll_readings.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["slot", "row", "col", "at_eastern", "lifetime_units", "lifetime_sales_cents", "price_cents"])
    for sel, series in sorted(by_slot.items()):
        for at, units, cents, price in series:
            w.writerow([sel, sel[0], sel[1], at.astimezone(EASTERN).isoformat(), units, cents, price])

with open(out / "sales_events.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["slot", "row", "col", "at_eastern", "units", "cents"])
    for sel, series in sorted(by_slot.items()):
        unit_rises = dict(rises((at, u) for at, u, _, _ in series))
        cent_rises = dict(rises((at, c) for at, _, c, _ in series))
        for at in sorted(set(unit_rises) | set(cent_rises)):
            w.writerow([sel, sel[0], sel[1], at.astimezone(EASTERN).isoformat(),
                        unit_rises.get(at, 0), cent_rises.get(at, 0)])

# machine-wide totals per poll: each selection's rises (same misread-safe rule), summed over selections
m_series: dict[str, list] = defaultdict(list)
for sel, at, units, cents in machine_rows:
    m_series[sel].append((at, units, cents))
m_units: dict = defaultdict(int)
m_cents: dict = defaultdict(int)
for series in m_series.values():
    for at, r in rises((at, u) for at, u, _ in series):
        m_units[at] += r
    for at, r in rises((at, c) for at, _, c in series):
        m_cents[at] += r
del m_series, machine_rows

with open(out / "machine_hourly.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["at_eastern", "units", "cents"])
    for (at,) in polls:
        w.writerow([at.astimezone(EASTERN).isoformat(), m_units.get(at, 0), m_cents.get(at, 0)])

with open(out / "polls.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["at_eastern"])
    for (at,) in polls:
        w.writerow([at.astimezone(EASTERN).isoformat()])

with open(out / "door_events.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["at_eastern", "door_open_lifetime", "door_open_since_reset", "rise"])
    prev = None
    for at, raw in raws:
        dc = door_counter(raw) if raw is not None else None
        if dc is None:
            continue
        since, life = dc
        rise = life - prev if prev is not None and life >= prev else 0
        w.writerow([at.astimezone(EASTERN).isoformat(), life, since, rise])
        prev = life

print(f"{len(rows)} readings of our {len(SLOTS)} slots on {DEVICE_ID}, {len(polls)} polls, "
      f"machine total {sum(m_units.values())} units -> {out}")
