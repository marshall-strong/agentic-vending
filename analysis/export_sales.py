"""Export assigned selections and their totals from the database to CSV (read-only).

Run from the repo root with the project's venv and a DATABASE_URL in .env:
    python analysis/export_sales.py                     # writes analysis/data/*.csv
    python analysis/export_sales.py OUT_DIR
    python analysis/export_sales.py --slots 15,16,...   # only part of our slots (see slots_config.py)
    python analysis/export_sales.py --fall-all analysis/data/fall40   # all 40 selections, polls before the fall cut only

Which slots are ours: the assignment stored from the inventory server (`python -m vending.slots record`), or the
part of it --slots names. With nothing stored the export stops and says to record it. See analysis/slots_config.py.

Scope: per-slot rows are written ONLY for our slots on `vending.slots.DEVICE_ID`, filtered
the same way as `vending.ledger._counter_rows`. A normal run's `machine_hourly.csv` totals cover our assignment
only, for every poll, so the series has no step in it. The whole machine's totals are the fall run's
(--fall-all), which stops at the fall cut (slots_config.fall_end(): FALL_CUTOFF, never later than the 3 Dec
start). No per-slot value outside our assignment is written.

--fall-all is the one exception: before the fall cut no selection is either team's (docs/integrity.md), so it
writes per-selection rows for all 40, and every query stops at the cut. check_before_start() checks the rows
read as well, and refuses the whole export if any row outside our assignment is from the cut on.

Writes:
    poll_readings.csv   every poll x OUR slot: lifetime counters and the current price
    sales_events.csv    one row per poll where one of OUR slots' counters rose (vending.counters.rises, misread-safe)
    polls.csv           the time of every poll, so gaps are not read as zero sales
    machine_hourly.csv  every poll: units and cents sold by our assignment (all 40 selections for --fall-all),
                        each slot's rise found with vending.counters.rises. Aggregates only.
    door_events.csv     every poll's EVA-DTS door-open counter (EA2*DO*<since reset>*<lifetime>) and its rise
                        since the previous poll (door_events.door_rows: vending.counters.rises, misread-safe): a rise
                        means the door was opened (a visit) in between
    receipts.csv        our fulfilled purchase receipts: request id, first line resolved, fulfilled_at. From the start
                        (3 Dec) a door rise is our visit only when one of these falls with it (door_events.is_our_rise)
    slots.json          which slots the CSVs cover: {device, slots, source, assigned_slots, resolved_at}, where
                        assigned_slots is our assignment. sales_analysis.py reads it, and stops if it is asked
                        about a different slot list or the assignment is missing or malformed.
"""
import argparse
import csv
import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from vending import db  # noqa: E402
from vending.counters import rises  # noqa: E402
from door_events import door_counter, door_rows  # noqa: E402
from slots_config import check_before_start, exempt_from_start, fall_end, resolve_slots  # noqa: E402

EASTERN = ZoneInfo("America/New_York")
ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
ap.add_argument("out", nargs="?", default=str(Path(__file__).resolve().parent / "data"), help="folder for the CSVs")
ap.add_argument("--slots", help='our slot codes, comma or space separated ("15,16,17,18"); overrides the stored list')
ap.add_argument("--fall-all", action="store_true",
                help="all 40 selections, polls before the fall cut (FALL_CUTOFF, never after the 3 Dec 2026 start) "
                     "only; write to its own folder")
args = ap.parse_args()
out = Path(args.out)
out.mkdir(parents=True, exist_ok=True)

with db.connect() as c:
    c.execute("set default_transaction_read_only = on")
    # ASSIGNED is our assignment (the stored server answer), recorded in slots.json; RUN_SLOTS is what this run covers
    DEVICE_ID, RUN_SLOTS, SOURCE, ASSIGNED = resolve_slots(args.slots, c, fall=args.fall_all)
    # a fall run reads only polls before the fall cut; a normal run reads every poll (its slots are ours)
    CUT = fall_end()
    before = CUT if args.fall_all else dt.datetime.max.replace(tzinfo=dt.timezone.utc)
    print(f"slots: {len(RUN_SLOTS)} on {DEVICE_ID}, from {SOURCE}" + (f"; polls before {CUT.isoformat()}" if args.fall_all else ""))
    # our slots: same filter as vending.ledger._counter_rows
    rows = c.execute(
        "select ps.selection, p.started_at, ps.lifetime_units, ps.lifetime_sales_cents, ps.price_cents"
        " from poll_selections ps join polls p using (session_id)"
        " where p.device_id = %s and p.crc_ok and ps.selection = any(%s) and p.started_at < %s"
        " order by ps.selection, p.started_at",
        (DEVICE_ID, list(RUN_SLOTS), before)).fetchall()
    # Totals: our assignment in a normal run, every selection in a fall run (which stops at the cut). The selection
    # filter is on poll_selections alone, so no counter outside it is read (tests/test_assignment_boundaries.py).
    machine_rows = c.execute(
        "select ps.selection, p.started_at, ps.lifetime_units, ps.lifetime_sales_cents"
        " from poll_selections ps join polls p using (session_id)"
        " where p.device_id = %s and p.crc_ok and p.started_at < %s"
        " and ps.selection = any(%s) order by ps.selection, p.started_at",
        (DEVICE_ID, before, list(RUN_SLOTS) if args.fall_all else list(ASSIGNED))).fetchall()
    polls = c.execute(
        "select started_at from polls where device_id = %s and crc_ok and started_at < %s order by started_at",
        (DEVICE_ID, before)).fetchall()
    raws = c.execute(
        "select started_at, raw from polls where device_id = %s and crc_ok and started_at < %s order by started_at",
        (DEVICE_ID, before)).fetchall()
    # our fulfilled receipts: when each visit ended and when its first line was resolved (no per-slot values)
    receipts = c.execute(
        "select r.request_id, coalesce(min(l.resolved_at), r.fulfilled_at), r.fulfilled_at"
        " from purchase_requests r left join purchase_lines l using (request_id)"
        " where r.status = 'fulfilled' and r.fulfilled_at is not null"
        " group by r.request_id, r.fulfilled_at order by r.fulfilled_at").fetchall()


ours = set(RUN_SLOTS)
check_before_start(((sel, at) for sel, at, *_ in rows), exempt_from_start(SOURCE, ASSIGNED), "export")
check_before_start(((sel, at) for sel, at, *_ in machine_rows), exempt_from_start(SOURCE, ASSIGNED), "totals export")
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
    counts = ((at, door_counter(raw)) for at, raw in raws if raw is not None)
    for at, life, since, rise in door_rows((at, *dc) for at, dc in counts if dc is not None):
        w.writerow([at.astimezone(EASTERN).isoformat(), life, since, rise])

with open(out / "receipts.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["request_id", "first_at_eastern", "fulfilled_at_eastern"])
    for request_id, first, done in receipts:
        w.writerow([request_id, first.astimezone(EASTERN).isoformat(), done.astimezone(EASTERN).isoformat()])

(out / "slots.json").write_text(json.dumps(dict(
    device=DEVICE_ID, slots=list(RUN_SLOTS), source=SOURCE, assigned_slots=list(ASSIGNED),
    **({"before": CUT.isoformat()} if args.fall_all else {}),
    resolved_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")), indent=1) + "\n", encoding="utf-8")

print(f"{len(rows)} readings of {len(RUN_SLOTS)} {'selections (fall run)' if args.fall_all else 'of our slots'} on {DEVICE_ID}, {len(polls)} polls, "
      f"machine total {sum(m_units.values())} units -> {out}")
