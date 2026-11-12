#!/usr/bin/env python
"""Dark periods: when the machine's door opened, how long we couldn't see, and what that means for sales.

Reads the CSVs that analysis/export_sales.py writes (and, if present, the visit grades in
analysis/out/lab_data.json from analysis/sales_analysis.py). Standard library only, so the tests run in
the project's own environment.

    python analysis/visit_windows.py [--data analysis/data] [--lab-data analysis/out/lab_data.json]
                                     [--out analysis/out/visit_windows.json]

What it measures (hourly-polling era only, from 21 Sep 2026, when polls came every hour):
    visits        door-counter rises (EA2*DO) between two polls, merged when within 3 hours, as
                  analysis/sales_analysis.py does. The window is the time we can't see into: from the last
                  poll before the first opening to the poll that saw the last one.
    grades        the analysis's refill grade for each visit (proven, likely, possible top-up, no sign).
    poll gaps     how often a gap between polls is longer than an hour: a longer gap widens any window.
    exposure      our units sold inside a visit window (the units a product or price change would make
                  ambiguous), and our class-day sales by hour, which gives the expected exposure for a
                  visit at each hour. A sale is stamped at the poll that saw it (on the hour, at :00 or
                  :59), so it's put in the hour that poll closes: a 14:00 or 13:59 poll means 13:00-14:00.
    after         minutes from the end of each window to our slots' next sale.

Scope (docs/integrity.md): sales are our slots only (sales_events.csv holds only them). The door counter is
machine-wide; it dates visits and is never used per slot.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

MERGE_H = 3                                        # rises within 3 h are one visit (sales_analysis.VISIT_MERGE_H)
HOURLY_FROM = datetime.fromisoformat("2026-09-21T01:00:00-04:00")
DAY_H0, DAY_H1 = 7, 18                             # hours shown in the by-hour profile
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
RESTOCK_GRADES = ("proven refill", "likely refill", "possible top-up")


def _median(values):
    """Median of the values that aren't None, or None when there are none."""
    vals = [v for v in values if v is not None]
    return statistics.median(vals) if vals else None


def _gaps(visits) -> list[int]:
    dates = sorted({v["from"][:10] for v in visits})
    return [(datetime.fromisoformat(b) - datetime.fromisoformat(a)).days for a, b in zip(dates, dates[1:])]


def read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def when(text: str) -> datetime:
    return datetime.fromisoformat(text)


def find_visits(door_rows: list[dict]) -> list[dict]:
    """Door-counter rises merged into visits. Each visit: from (poll before), first and last rise polls,
    openings, and the rise polls themselves."""
    rows = sorted(({"t": when(r["at_eastern"]), "rise": int(r["rise"])} for r in door_rows), key=lambda r: r["t"])
    visits: list[dict] = []
    for prev, r in zip(rows, rows[1:]):
        if r["rise"] <= 0:
            continue
        if visits and (r["t"] - visits[-1]["last"]).total_seconds() / 3600 <= MERGE_H:
            v = visits[-1]
            v["last"] = r["t"]
            v["openings"] += r["rise"]
            v["rise_polls"].append(r["t"])
        else:
            visits.append({"from": prev["t"], "first": r["t"], "last": r["t"], "openings": r["rise"], "rise_polls": [r["t"]]})
    return visits


def poll_gaps(poll_times: list[datetime], since: datetime) -> dict:
    times = sorted(t for t in poll_times if t >= since)
    gaps = [((b - a).total_seconds() / 60, a, b) for a, b in zip(times, times[1:])]
    mins = [g for g, _, _ in gaps]
    if not mins:
        return {"gaps": 0}
    longest = sorted(gaps, key=lambda g: -g[0])[:5]
    return {
        "gaps": len(mins),
        "median_min": round(statistics.median(mins)),
        "over_65_min": sum(1 for g in mins if g > 65),
        "over_125_min": sum(1 for g in mins if g > 125),
        "share_within_65_min": round(sum(1 for g in mins if g <= 65) / len(mins), 3),
        "longest": [{"minutes": round(g), "from": a.isoformat(timespec="minutes"), "to": b.isoformat(timespec="minutes")}
                    for g, a, b in longest],
    }


def class_days(lab: dict | None) -> set[str] | None:
    """Dates with classes, from the analysis's calendar; None when there's no lab data."""
    if not lab:
        return None
    return {d["date"] for d in lab.get("calendar", {}).get("days", []) if d.get("classes") not in (None, "none")}


def grade_of(visit: dict, lab: dict | None) -> str | None:
    """The analysis's refill grade for the visit with the same window start, if lab data is there."""
    if not lab:
        return None
    key = visit["from"].isoformat(timespec="minutes")[:16]
    for v in lab.get("visits", []):
        if str(v.get("start", ""))[:16] == key:
            return v.get("evidence")
    return None


def analyse(door_rows, poll_rows, sales_rows, lab=None, since=HOURLY_FROM) -> dict:
    sales = sorted(((when(r["at_eastern"]), int(r["units"]), r["slot"]) for r in sales_rows), key=lambda s: s[0])
    units_at: dict[datetime, int] = defaultdict(int)
    for t, u, _ in sales:
        units_at[t] += u
    our_units = sum(u for t, u, _ in sales if t >= since)

    visits = []
    for v in find_visits(door_rows):
        if v["first"] < since:
            continue
        inside = sum(u for t, u, _ in sales if v["from"] < t <= v["last"])   # can't be placed before or after
        nxt = next((t for t, u, _ in sales if t > v["last"] and u > 0), None)
        mid = v["from"] + (v["last"] - v["from"]) / 2
        visits.append({
            "weekday": DAYS[v["first"].weekday()],
            "from": v["from"].isoformat(timespec="minutes"), "to": v["last"].isoformat(timespec="minutes"),
            "mid_hour": round(mid.hour + mid.minute / 60, 2),
            "window_min": round((v["last"] - v["from"]).total_seconds() / 60),
            "openings": v["openings"], "our_units_inside": inside,
            "minutes_to_next_sale": round((nxt - v["last"]).total_seconds() / 60) if nxt else None,
            "grade": grade_of(v, lab),
        })

    windows = [v["window_min"] for v in visits]
    restocks = [v for v in visits if v["grade"] in RESTOCK_GRADES]
    restock_dates = sorted({v["from"][:10] for v in restocks})
    between = [(datetime.fromisoformat(b) - datetime.fromisoformat(a)).days for a, b in zip(restock_dates, restock_dates[1:])]

    # Our class-day sales by hour: average units per class day in each hour.
    cdays = class_days(lab)
    by_hour: dict[int, int] = defaultdict(int)
    day_set = set()
    for t, u, _ in sales:
        if t < since or t.weekday() > 4:
            continue
        d = t.date().isoformat()
        if cdays is not None and d not in cdays:
            continue
        day_set.add(d)
        by_hour[(t - timedelta(minutes=30)).hour] += u      # the hour this poll closes
    n_days = len({t.date().isoformat() for t in (when(r["at_eastern"]) for r in poll_rows)
                  if t >= since and t.weekday() <= 4 and (cdays is None or t.date().isoformat() in cdays)}) or 1
    profile = [{"hour": h, "units_per_day": round(by_hour.get(h, 0) / n_days, 2)} for h in range(DAY_H0, DAY_H1 + 1)]
    # "hour" is the start of the hour: 13 means 13:00-14:00. Visits so far fell between 7:30 and 15:30.
    quiet = sorted((p for p in profile if 8 <= p["hour"] <= 17), key=lambda p: p["units_per_day"])

    return {
        "since": since.isoformat(timespec="minutes"),
        "visits": visits,
        "summary": {
            "visits": len(visits),
            "weekdays": dict(Counter(v["weekday"] for v in visits)),
            "window_median_min": round(statistics.median(windows)) if windows else None,
            "window_max_min": max(windows) if windows else None,
            "windows_over_65_min": sum(1 for w in windows if w > 65),
            "openings_median": statistics.median([v["openings"] for v in visits]) if visits else None,
            "visits_with_our_sales_inside": sum(1 for v in visits if v["our_units_inside"] > 0),
            "our_units_inside": sum(v["our_units_inside"] for v in visits),
            "our_units": our_units,
            "graded": lab is not None,
            "grades": dict(Counter(v["grade"] or "ungraded" for v in visits)),
            "restock_visits": len(restocks),
            "restock_weekdays": dict(Counter(v["weekday"] for v in restocks)),
            "restock_mid_hours": sorted(v["mid_hour"] for v in restocks),
            "days_between_restocks": between,
            "days_between_restocks_median": statistics.median(between) if between else None,
            "days_between_refills_median": _median(_gaps([v for v in restocks if v["grade"] != "possible top-up"])),
            "minutes_to_next_sale_median": _median([v["minutes_to_next_sale"] for v in visits]),
            "class_days": n_days,
            "quietest_hours": [p["hour"] for p in quiet[:3]],
            "busiest_hours": [p["hour"] for p in quiet[-3:]][::-1],
        },
        "poll_gaps": poll_gaps([when(r["at_eastern"]) for r in poll_rows], since),
        "hour_profile": profile,
    }


def main(argv=None):
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", default=str(here / "data"), help="folder with the exported CSVs")
    ap.add_argument("--lab-data", default=str(here / "out" / "lab_data.json"), help="the analysis's lab_data.json (for grades)")
    ap.add_argument("--out", default=str(here / "out" / "visit_windows.json"))
    args = ap.parse_args(argv)
    data = Path(args.data)
    lab_path = Path(args.lab_data)
    lab = json.loads(lab_path.read_text(encoding="utf-8")) if lab_path.exists() else None
    result = analyse(read_csv(data / "door_events.csv"), read_csv(data / "polls.csv"), read_csv(data / "sales_events.csv"), lab)
    if lab and any(v["grade"] is None for v in result["visits"]):
        print("WARNING: some visits have no refill grade: lab_data.json may be older than the CSVs; rerun the analysis")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps(result["summary"], indent=1))
    print("poll gaps:", json.dumps(result["poll_gaps"]))
    print(f"written {out}")


if __name__ == "__main__":
    main()
