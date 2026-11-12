#!/usr/bin/env python
"""Vending machine vmc1 sales analysis for OUR 20 slots, using standard forecasting practice.

One rerunnable script. Fixed seeds. pandas / numpy / statsmodels / scipy / scikit-learn only.

Usage:
    python -I sales_analysis.py [--sales sales_events.csv] [--readings poll_readings.csv]
                                [--polls polls.csv] [--machine machine_hourly.csv] [--doors door_events.csv]
                                [--calendar calendar.json] [--photos planograms_photos.json] [--out OUT_DIR]

Defaults: inputs in analysis/data/*.csv (written by analysis/export_sales.py), the calendar and photo files in
analysis/, outputs in analysis/out/ (both folders gitignored).

Outputs (OUT_DIR):
    lab_data.json        compact data for the interactive labs
    analysis_report.md   headline findings, the slow-seller rule recommendation, caveats

Scope (binding team decision): anything per slot -- product, price, position, segments, experiments, stock-outs,
refill evidence, slow-seller flags -- uses ONLY our slots (vending.slots.SLOTS on vending.slots.DEVICE_ID), and no
slot is ever judged by another slot's state.  Machine-wide data enters only as totals over the whole machine
(machine_hourly.csv), and only for: calendar / day-off effects, time-of-day and day-of-week profiles, the overall
trend, forecasting machine totals, and dating door visits.  The machine-wide history is pre-competition: course
staff stocked every slot, so it reflects no rival strategy.

Conventions (standard forecasting practice):
    error = actual - forecast.  Bias = mean(error); positive = the forecast ran LOW.
    MAE is the headline ("average miss per day / week").  RMSE: big misses count extra.
    MAPE skips zero-actual periods.  WAPE (sum|e| / sum actual) is OUR addition for
    zero-heavy vending data.  Every model is shown next to seasonal naive.
    Walk-forward backtest with an expanding window, one forecast per Saturday plan date.
    Weeks run Saturday to Friday, as in vending/ledger.py (WEEK_STARTS = 5).

Key data choices (each is also written to lab_data.json meta.notes):
  * Sales are counter rises between polls.  For a normal poll interval (<= 2 h) a sale is
    timed at the MIDPOINT of the interval: the regular-era stamps are either hh:00:00 or
    hh:59:59, so using the poll stamp itself would shift half the hours by one.
  * After a polling gap (> 2 h) the lump is (a) kept at the poll time in the "raw" series and
    (b) spread over the gap hours by a regular-era hour-of-week profile in the "spread"
    series, weighted by calendar day type (class day = its weekday profile; open weekday
    without classes = the weekend rate; weekday the Institute is closed = 0).  Totals are
    identical.  A gap is missing data, never zero sales.  Machine totals are spread with the
    machine-wide profile; our slots with the profile of our 20 slots combined.
  * Regular hourly polling starts 2026-09-21 01:00 ET.  Rates, hour profiles and test windows
    use that era; forecast training also uses the gap-spread days from term start (Sep 9).
    In the backtests each plan date re-spreads the lumps with a profile built only from
    regular-era sales reported before that date, so no training window sees later data.
  * Visits come from the machine's door-open counter (EVA-DTS EA2*DO, exported to door_events.csv):
    every rise between two polls is a door opening; rises within 3 h are one visit.  Refill evidence
    per visit uses the door counter and OUR slots only: "proven refill" = capacity proof (one of our slots
    sold more than the largest rail holds, 11, over an interval that contains exactly one visit);
    "likely refill" = >= 2 of our slots that looked empty resume within 4 days; "possible top-up" = 1.
  * Stock-outs: the machine reports sales, not stock.  A slot is "suspected empty" after its last sale when
    its own expected sales since then (its share x a calendar-time clock built from our slots' hour-of-week
    profile) make P(0 sales) = exp(-expected) < 0.05.  The window ends at a DOOR VISIT (the last one before the
    slot sold again), never at the slot's own next sale; a silence that ends in a sale with no door opening in
    between proves the slot was stocked.  A window still open at the end of data is "sold out, not refilled"
    when it starts after the last single-visit-proven refill.  This is an inference, not an observation.
"""
import argparse
import datetime as dt
import json
import math
import re
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.formula.api as smf
from scipy import stats
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score
from sklearn.preprocessing import StandardScaler
from statsmodels.tsa.holtwinters import ExponentialSmoothing
from statsmodels.tsa.seasonal import STL

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vending.slots import DEVICE_ID, SLOTS  # noqa: E402  (plain constants; no database needed)

TZ = "America/New_York"
SEED = 42
WEEK_STARTS = 5                   # Saturday, as vending/ledger.py: "The week turns over on Saturday, after the Friday visit"
GAP_H = 2.0                       # poll interval longer than this = polling gap
REGULAR_START = pd.Timestamp("2026-09-21 01:00", tz=TZ)
TERM_START = dt.date(2026, 9, 9)  # Fall Quarter classes start (calendar.json)
DAY_H0, DAY_H1 = 8, 17            # selling hours used for "daytime" checks (about 99% of regular-era units fall 08:00-17:00)
# stock-out heuristic
ALPHA_EMPTY, MIN_SILENCE_H, MIN_UNITS, MIN_WEEKS, VISIT_HOUR = 0.05, 24, 5, 2, 8
EMPTY_DAY_SHARE = 0.5             # slot-day counts as empty if >= half its daytime hours are in an empty window
GAP_DAY_HOURS = 2                 # day counts as a polling-gap day if >= 2 daytime hours are unobserved
H = 7                             # decision horizon: one Saturday-to-Friday week
FORECAST_DAYS = 28
DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
# visits and refill evidence (door-open counter + our slots)
VISIT_MERGE_H = 3                 # door-counter rises within 3 h of each other = one visit
CAPACITY_MAX = 11                 # largest units per column on the Package Loading label (240/250 ml cans)
SILENT_DAYS = 5                   # a slot silent this many days before a visit counts as "silent"
RESUME_DAYS = 4                   # ... and "resumed" if it sells within this many days after the visit
LIKELY_MIN = 2                    # >= this many of our silent slots resuming = "likely refill" (4 of 40 scaled to 2 of 20)
REFILL_EVIDENCE = ("proven refill", "likely refill", "possible top-up")
# Photo of the machine taken by the student team at the Nov 9 visit: rails with product visible at the front.
# Product further back in a rail can be hidden, so "looks empty" is not proof of empty.  Our slots only.
PHOTO_NOV9 = dict(date="2026-11-09", stocked=["25", "27", "28", "36", "47"],
                  note="Student team photo of the machine at the Mon Nov 9 visit (door counter rise at 09:31). Of our 20 rails, "
                       "product is visible at 25, 27 and 28 (shelf 2), 36 (shelf 3) and 47 (shelf 4); shelves 1 and 5 look empty.")
VISIT_NOTES = {"2026-11-09": "student team photographing the machine (not a refill)",
               "2026-11-11": "not the student team; probably course staff or the operator"}
KNOWN_NOT_REFILL = {"2026-11-09"}  # visits known not to be refills (never end a stock-out window, never count as a refill)
SCOPE_NOTE = ("Machine-wide data is used only as totals over the whole machine, for the calendar and day-off effects, "
              "the time-of-day and day-of-week profiles, the overall trend, the machine-total forecast and dating door visits: "
              "that history is from before the competition, when course staff stocked every slot, so it reflects no rival "
              "strategy. Everything per slot (products, prices, position, segments, stock-outs, refill evidence, slow-seller "
              "flags, experiments) uses only our 20 slots, and no slot is judged by another slot's state.")


# ============================================================== helpers
def to_et(col):
    return pd.to_datetime(col, utc=True).dt.tz_convert(TZ)


def floor_h(x):
    """Hour floor done in UTC (DST-safe), back to Eastern."""
    if isinstance(x, pd.Timestamp):
        return x.tz_convert("UTC").floor("h").tz_convert(TZ)
    return x.dt.tz_convert("UTC").dt.floor("h").dt.tz_convert(TZ)


def local_at(d, hour=0):
    return pd.Timestamp(dt.datetime.combine(d, dt.time(hour))).tz_localize(TZ)


def week_of(d):
    """The Saturday that starts the week holding date d (vending/ledger.py week_of, WEEK_STARTS = 5)."""
    return d - dt.timedelta(days=(d.weekday() - WEEK_STARTS) % 7)


def vwin(v):
    """A visit's door window as text: 'Tue Oct 27 07:00-08:00', or both dates when it spans days (sparse early polls)."""
    a, b = v["start"], v["end"]
    if a.date() == b.date():
        return f"{b:%a %b %d} {a:%H:%M}-{b:%H:%M}"
    return f"{a:%a %b %d %H:%M} to {b:%a %b %d %H:%M}"


def rnd(x, n=2):
    if x is None:
        return None
    if isinstance(x, (np.integer,)):
        return int(x)
    try:
        if isinstance(x, (float, np.floating)) and (math.isnan(x) or math.isinf(x)):
            return None
    except TypeError:
        pass
    if isinstance(x, (float, np.floating)):
        v = round(float(x), n)
        return int(v) if v.is_integer() else v
    return x


def rl(a, n=2):
    return [rnd(v, n) for v in a]


def ds(d):
    return d.isoformat() if isinstance(d, (dt.date, dt.datetime)) else str(d)


def md(d):
    """'Nov 18' (no leading zero)."""
    return f"{d:%b} {d.day}"


def metrics(actual, fc):
    a, f = np.asarray(actual, float), np.asarray(fc, float)
    e = a - f
    nz = a != 0
    return dict(mae=float(np.mean(np.abs(e))), rmse=float(np.sqrt(np.mean(e ** 2))),
                wape=float(np.sum(np.abs(e)) / np.sum(a)) if a.sum() > 0 else None,
                mape=float(np.mean(np.abs(e[nz]) / a[nz]) * 100) if nz.any() else None,
                bias=float(np.mean(e)), actual_total=float(a.sum()), forecast_total=float(f.sum()),
                total_gap_pct=float((f.sum() - a.sum()) / a.sum() * 100) if a.sum() > 0 else None)


def counter_total(values):
    """Total of the rises in one lifetime counter (readings oldest first), with vending.counters.rises' rule for a reading
    that goes down: a low reading followed by one back at or above the old high was a misread (the rise is measured from
    the old high); two low readings in a row mean the counter restarted (measured from the first low one).  Written out
    here, independent of the exporter, so the check below really re-derives the totals.
    Returns (total, restarts, misreads)."""
    total, prev, pending, restarts, misreads = 0, None, None, 0, 0
    for v in values:
        if prev is None:
            prev = v
            continue
        if v >= prev:
            if pending is not None:
                misreads += 1
            total += v - prev
            prev, pending = v, None
        elif pending is None:
            pending = v
        else:
            restarts += 1
            if v > pending:
                total += v - pending
            prev, pending = v, None
    return total, restarts, misreads


# ============================================================== load
def load(args):
    ours = set(SLOTS)
    sales = pd.read_csv(args.sales, dtype={"slot": str})
    reads = pd.read_csv(args.readings, dtype={"slot": str})
    extra = sorted((set(sales.slot) | set(reads.slot)) - ours)
    if extra:
        # the exporter writes our slots only; an older export would carry other slots: never use them
        raise SystemExit(f"SCOPE: the export carries per-slot rows for {len(extra)} slots outside ours: rerun export_sales.py")
    sales["t"] = to_et(sales.at_eastern)
    reads["t"] = to_et(reads.at_eastern)
    polls = pd.read_csv(args.polls)
    polls["t"] = to_et(polls.at_eastern)
    polls = polls.sort_values("t").reset_index(drop=True)
    if not Path(args.machine).exists():
        raise SystemExit(f"no machine totals at {args.machine}: rerun export_sales.py (it writes machine_hourly.csv)")
    mach = pd.read_csv(args.machine)
    mach["t"] = to_et(mach.at_eastern)
    cal = json.loads(Path(args.calendar).read_text(encoding="utf-8"))
    doors = None
    if args.doors and Path(args.doors).exists():
        doors = pd.read_csv(args.doors)
        doors["t"] = to_et(doors.at_eastern)
        doors = doors.sort_values("t").reset_index(drop=True)
    else:
        print(f"WARNING: no door-event file at {args.doors}; visits fall back to the old assumption of a Friday "
              f"{VISIT_HOUR:02d}:00 visit every week (rerun export_sales.py to get door_events.csv)")
    photos = json.loads(Path(args.photos).read_text(encoding="utf-8")) if Path(args.photos).exists() else None
    oct8 = json.loads(Path(args.planogram).read_text(encoding="utf-8")) if Path(args.planogram).exists() else None
    return sales, reads, polls, mach, cal, doors, photos, oct8


# ============================================================== main analysis
def main():
    root = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sales", default=str(root / "data" / "sales_events.csv"))
    ap.add_argument("--readings", default=str(root / "data" / "poll_readings.csv"))
    ap.add_argument("--polls", default=str(root / "data" / "polls.csv"))
    ap.add_argument("--machine", default=str(root / "data" / "machine_hourly.csv"),
                    help="machine-wide units and cents per poll (aggregates only)")
    ap.add_argument("--doors", default=str(root / "data" / "door_events.csv"),
                    help="door-open counter per poll (export_sales.py); if missing, visits are assumed Fridays 08:00")
    ap.add_argument("--calendar", default=str(root / "golisano_calendar_2026_27.json"))
    ap.add_argument("--photos", default=str(root / "planograms_photos.json"))
    ap.add_argument("--planogram", default=str(root / "planogram_2026-10-08.json"))
    ap.add_argument("--out", default=str(root / "out"))
    args = ap.parse_args()
    OUT = Path(args.out)
    OUT.mkdir(parents=True, exist_ok=True)
    np.random.seed(SEED)
    warnings.filterwarnings("ignore")      # noisy library warnings; Holt-Winters warnings are counted where they matter

    sales, reads, polls, mach, cal, doors, photos, oct8 = load(args)
    P = polls.t
    Pv = P.values
    T0, T1 = P.iloc[0], P.iloc[-1]
    slots = list(SLOTS)                                   # ours, in shelf order
    rowcol = pd.DataFrame({"row": [int(s[0]) for s in slots], "col": [int(s[1]) for s in slots]}, index=slots)
    TOTAL_UNITS, TOTAL_CENTS = int(sales.units.sum()), int(sales.cents.sum())
    MACHINE_UNITS, MACHINE_CENTS = int(mach.units.sum()), int(mach.cents.sum())

    # ---------------------------------------------------------- poll intervals and sale timing
    def time_sales(df):
        idx = np.searchsorted(Pv, df.t.values)
        assert (Pv[idx] == df.t.values).all(), "every sale must sit on a poll"
        df["prev"] = pd.Series(P.iloc[np.maximum(idx - 1, 0)].tolist(), index=df.index)
        df["gap_h"] = (df.t - df.prev).dt.total_seconds() / 3600
        df["lump"] = df.gap_h > GAP_H
        df["t_attr"] = df.t.where(df.lump, df.prev + (df.t - df.prev) / 2)
        return df.sort_values(["t", "slot"]).reset_index(drop=True)
    sales = time_sales(sales.copy())
    msales = time_sales(mach[(mach.units > 0) | (mach.cents > 0)].assign(slot="machine").copy())

    pg = pd.DataFrame({"prev": P.shift(), "t": P})
    pg["hours"] = (pg.t - pg.prev).dt.total_seconds() / 3600
    gaps = pg[pg.hours > GAP_H].copy()
    gaps["units"] = gaps.t.map(msales.groupby("t").units.sum()).fillna(0).astype(int)
    gaps["units_ours"] = gaps.t.map(sales.groupby("t").units.sum()).fillna(0).astype(int)

    # hourly grid (Eastern hour starts)
    hours = pd.date_range(floor_h(T0).tz_convert("UTC"), floor_h(T1).tz_convert("UTC"), freq="h").tz_convert(TZ)
    in_gap = np.zeros(len(hours), bool)
    for g in gaps.itertuples():
        in_gap |= (hours > floor_h(g.prev)) & (hours < floor_h(g.t))
    in_gap = pd.Series(in_gap, index=hours)

    # hour-of-week profiles, regular era, midpoint timing, lumps left out (a regular-era outage lump sits at its poll hour)
    era_hours = hours[hours >= REGULAR_START]
    n_how = pd.Series(1, index=era_hours).groupby([era_hours.dayofweek, era_hours.hour]).sum()

    def how_of(df):
        e = df[(df.t_attr >= REGULAR_START) & ~df.lump]
        ah = floor_h(e.t_attr)
        return e.groupby([ah.dt.dayofweek, ah.dt.hour]).units.sum().reindex(n_how.index).fillna(0) / n_how
    era = msales[(msales.t_attr >= REGULAR_START) & ~msales.lump]       # machine-wide, regular era, no lumps
    how_machine = how_of(msales)       # time-of-day / day-of-week profile of the whole machine (an aggregate)
    how_ours = how_of(sales)           # the same for our 20 slots combined: used for anything per slot

    # calendar-aware spreading weight for one gap hour: class days use the weekday x hour profile, weekends their
    # own profile, open weekdays without classes (break, orientation) the average weekend rate for that hour, and
    # weekdays the Institute is closed (e.g. Labor Day) get 0.
    cal_days = {d["date"]: d for d in cal["days"]}

    def spread_w(prof, h):
        wd, rec = h.dayofweek, cal_days.get(h.strftime("%Y-%m-%d"))
        if wd >= 5 or rec is None or rec.get("classes") == "normal":
            return prof.get((wd, h.hour), 0.0)
        if rec.get("institute") == "closed":
            return 0.0
        return (prof.get((5, h.hour), 0.0) + prof.get((6, h.hour), 0.0)) / 2

    def spread_rows(df, prof):
        """Hourly rows (slot, hour, units, cents): a lump is spread over its gap hours by `prof` (a lump whose weights are
        all zero stays on its poll hour); other sales sit on their midpoint hour."""
        rows = []
        for r in df.itertuples():
            if r.lump:
                hrs = pd.date_range(floor_h(r.prev).tz_convert("UTC"), floor_h(r.t).tz_convert("UTC"), freq="h").tz_convert(TZ)
                w = np.array([spread_w(prof, h) for h in hrs])
                if len(hrs) and w.sum() > 0:
                    rows.extend((r.slot, h, r.units * x, r.cents * x) for h, x in zip(hrs, w / w.sum()) if x > 0)
                    continue
            rows.append((r.slot, floor_h(r.t_attr), float(r.units), float(r.cents)))
        out = pd.DataFrame(rows, columns=["slot", "hour", "units", "cents"])
        out["date"] = out.hour.dt.date
        return out
    mspread = spread_rows(msales, how_machine)
    spread = spread_rows(sales, how_ours)

    def spread_daily_asof(cutoff):
        """Daily gap-spread MACHINE units as a planner standing at `cutoff` (a date, 00:00 ET) would build them: only sales
        reported before the cutoff, lumps spread by an hour-of-week profile estimated only from regular-era sales reported
        before the cutoff."""
        c = local_at(cutoff)
        e2 = era[era.t < c]
        eh2 = era_hours[era_hours < c]
        prof = {}
        if len(eh2):
            n2 = pd.Series(1, index=eh2).groupby([eh2.dayofweek, eh2.hour]).sum()
            a2 = floor_h(e2.t_attr)
            prof = (e2.groupby([a2.dt.dayofweek, a2.dt.hour]).units.sum().reindex(n2.index).fillna(0) / n2).to_dict()
        sub = msales[msales.t < c]
        return spread_rows(sub, prof).groupby("date").units.sum() if len(sub) else pd.Series(dtype=float)
    for df in (sales, msales):
        df["date"] = df.t_attr.dt.date
        df["hour"] = floor_h(df.t_attr)

    # ---------------------------------------------------------- calendar / day table
    d0, d1 = T0.date(), T1.date()
    dates = [d0 + dt.timedelta(days=i) for i in range((d1 - d0).days + 1)]
    D = pd.DataFrame(index=pd.Index(dates, name="date"))
    D["dow"] = [d.weekday() for d in dates]
    D["classes"] = [cal_days.get(ds(d), {}).get("classes", "unknown") for d in dates]
    D["institute"] = [cal_days.get(ds(d), {}).get("institute", "unknown") for d in dates]
    D["label"] = [cal_days.get(ds(d), {}).get("label", "") for d in dates]
    D["class_day"] = (D.classes == "normal") & (D.dow < 5)
    D["no_class_weekday"] = (D.dow < 5) & ~D.class_day
    # machine-wide totals (aggregates): calendar, time profiles, trend, forecasts
    D["units_raw"] = msales.groupby("date").units.sum().reindex(dates).fillna(0).astype(int)
    D["cents_raw"] = msales.groupby("date").cents.sum().reindex(dates).fillna(0).astype(int)
    D["units"] = mspread.groupby("date").units.sum().reindex(dates).fillna(0)      # gap-spread
    D["cents"] = mspread.groupby("date").cents.sum().reindex(dates).fillna(0)
    # our 20 slots
    D["units_ours_raw"] = sales.groupby("date").units.sum().reindex(dates).fillna(0).astype(int)
    D["cents_ours_raw"] = sales.groupby("date").cents.sum().reindex(dates).fillna(0).astype(int)
    D["units_ours"] = spread.groupby("date").units.sum().reindex(dates).fillna(0)
    D["cents_ours"] = spread.groupby("date").cents.sum().reindex(dates).fillna(0)
    gh = in_gap.groupby(in_gap.index.date).sum()
    ghd = in_gap[(in_gap.index.hour >= DAY_H0) & (in_gap.index.hour < DAY_H1)]
    ghd = ghd.groupby(ghd.index.date).sum()
    D["gap_hours"] = gh.reindex(dates).fillna(0).astype(int)
    D["gap_day_hours"] = ghd.reindex(dates).fillna(0).astype(int)
    D["gap_day"] = D.gap_day_hours >= GAP_DAY_HOURS
    D["partial"] = False
    D.loc[d0, "partial"] = T0.hour > DAY_H0
    D.loc[d1, "partial"] = T1.hour < 23
    D["lump_units"] = msales[msales.lump].groupby("date").units.sum().reindex(dates).fillna(0).astype(int)
    D["regular"] = [local_at(d) >= REGULAR_START.normalize() for d in dates]
    D.loc[REGULAR_START.date(), "regular"] = True
    SD = sales.groupby(["date", "slot"]).units.sum().unstack(fill_value=0).reindex(index=dates, columns=slots, fill_value=0).astype(int)

    # ---------------------------------------------------------- the stock-out clock: calendar time, not other slots
    # Expected units of our 20 slots combined in each hour, from their regular-era hour-of-week profile and the calendar.
    # A slot's expected sales over a silence = its share of this clock.  No other slot's sales or state enters it.
    I_h = np.array([spread_w(how_ours, h) for h in hours])
    cum_I = np.concatenate([[0.0], np.cumsum(I_h)])
    h_start = hours[0]

    def clock_at(t):
        x = (t - h_start).total_seconds() / 3600
        if x <= 0:
            return 0.0
        i = int(math.floor(x))
        if i >= len(I_h):
            return float(cum_I[-1])
        return float(cum_I[i] + I_h[i] * (x - i))

    def clock(a, b):
        return max(0.0, clock_at(b) - clock_at(a))

    def shares(cutoff):
        """Each slot's share of the clock from its own sales reported up to `cutoff`, over the weeks it sold anything."""
        sub = sales[sales.t <= cutoff]
        out = {}
        for s in slots:
            st = sub[sub.slot == s]
            wks = sorted(set(st.t.dt.date.map(week_of)))
            units = int(st.units.sum())
            expo = sum(clock(max(local_at(w), T0), min(local_at(w + dt.timedelta(days=7)), cutoff)) for w in wks)
            out[s] = dict(units=units, weeks=len(wks), share=units / expo if expo else 0.0,
                          eligible=bool(units >= MIN_UNITS and len(wks) >= MIN_WEEKS))
        return out
    share = shares(T1)

    # ---------------------------------------------------------- visits from the door-open counter
    # A rise in the lifetime door-open counter between two polls = the door was opened in (previous poll, poll].
    # Rises within VISIT_MERGE_H hours are one visit.  Without door data: one assumed visit every Friday 08:00.
    vis = []
    if doors is not None:
        dr = doors.copy()
        dr["prev"] = dr.t.shift()
        for r in dr[dr.rise > 0].itertuples():
            prev = r.prev if pd.notna(r.prev) else r.t
            if vis and (r.t - vis[-1]["end"]).total_seconds() / 3600 <= VISIT_MERGE_H:
                v = vis[-1]
                v["end"] = r.t
                v["openings"] += int(r.rise)
                v["rise_polls"].append(r.t)
            else:
                vis.append(dict(start=prev, first_rise=r.t, end=r.t, openings=int(r.rise), rise_polls=[r.t], source="door counter"))
    else:
        fr = sorted({d for d in dates if d.weekday() == 4})
        vis = [dict(start=f, first_rise=f, end=f, openings=None, rise_polls=[f], source=f"assumed Friday {VISIT_HOUR:02d}:00 (no door data)")
               for f in (local_at(d, VISIT_HOUR) for d in fr) if T0 < f <= T1]
    for i, v in enumerate(vis):
        v["id"] = i + 1
        v["window_h"] = (v["end"] - v["start"]).total_seconds() / 3600
        v["known_not_refill"] = doors is not None and v["first_rise"].strftime("%Y-%m-%d") in KNOWN_NOT_REFILL
    vis_by_id = {v["id"]: v for v in vis}
    door_vis = [v for v in vis if not v["known_not_refill"]]      # visits that could have refilled something

    # refill evidence 1: capacity proof.  For each of our slots, every minimal run of consecutive sale events whose units
    # exceed CAPACITY_MAX needs a refill after its first unit sold (> the first event's previous poll) and before its last
    # (<= the last event's poll).  If exactly one visit window overlaps that interval, that visit is a proven refill.
    sale_list = {s: list(sales[sales.slot == s].sort_values("t")[["prev", "t", "units"]].itertuples(index=False)) for s in slots}
    proofs = []
    for s in slots:
        ev, last_j = sale_list[s], None
        for k in range(len(ev)):
            tot, j = 0, k
            while j >= 0:
                tot += int(ev[j].units)
                if tot > CAPACITY_MAX:
                    break
                j -= 1
            if tot <= CAPACITY_MAX or j == last_j:      # j == last_j: contains the previous, shorter interval
                continue
            last_j = j
            a, b = ev[j].prev, ev[k].t
            ids = [v["id"] for v in vis if v["start"] < b and v["end"] > a]
            proofs.append(dict(slot=s, start=a, end=b, units=tot, visits=ids,
                               verdict="proven" if len(ids) == 1 else ("unexplained" if not ids else "ambiguous")))
    sales["week"] = sales.t.dt.date.map(week_of)
    sw = sales.groupby(["slot", "week"]).units.sum()
    # refill evidence 2: silent slots resuming.  One of our slots that had gone silent before the visit (>= SILENT_DAYS days
    # AND its own clock says the silence was improbable for a stocked slot, P(0) < ALPHA_EMPTY) and then sells within
    # RESUME_DAYS after the first door opening (before the next visit's first opening) "resumed" here.
    for i, v in enumerate(vis):
        nxt_rise = vis[i + 1]["first_rise"] if i + 1 < len(vis) else None
        horizon = v["end"] + pd.Timedelta(days=RESUME_DAYS)
        if nxt_rise is not None:
            horizon = min(horizon, nxt_rise)
        v["proven_slots"] = sorted({p["slot"] for p in proofs if p["visits"] == [v["id"]]})
        v["ambiguous_slots"] = sorted({p["slot"] for p in proofs if len(p["visits"]) > 1 and v["id"] in p["visits"]})
        v["pair_proof_slots"] = sorted({p["slot"] for p in proofs if len(p["visits"]) == 2 and v["id"] in p["visits"]})
        res = []
        for s in slots:
            before = [e for e in sale_list[s] if e.t <= v["start"]]
            last = before[-1].t if before else None
            since = last if last is not None else T0
            silent_d = (v["start"] - since).total_seconds() / 86400
            p0_sil = math.exp(-share[s]["share"] * clock(since, v["start"]))
            after = [e for e in sale_list[s] if e.prev >= v["first_rise"] and e.t <= horizon]
            if silent_d >= SILENT_DAYS and p0_sil < ALPHA_EMPTY and after:
                res.append(dict(slot=s, silent_days=silent_d, p0_silence=p0_sil, sold_before=last is not None,
                                hours_to_first_sale=(after[0].t - v["end"]).total_seconds() / 3600,
                                units_within=int(sum(e.units for e in after))))
        v["resumed"] = res
        v["resumed_slots"] = [r["slot"] for r in res]
        v["evidence"] = ("proven refill" if v["proven_slots"] else "likely refill" if len(res) >= LIKELY_MIN
                         else "possible top-up" if res else "no sign of refill")
        key = v["first_rise"].strftime("%Y-%m-%d")
        v["note"] = VISIT_NOTES.get(key, "") if doors is not None else ""
        if v["known_not_refill"] and not v["proven_slots"]:
            v["resumed_before_override"] = v["resumed_slots"]
            v["resumed"], v["resumed_slots"] = [], []
            v["evidence"] = "no sign of refill"
            v["note"] += ("; any slot that sold soon after it was already stocked (checked against the photo)"
                          if v["resumed_before_override"] else "")

    def outcome_refill(v, s):
        """Does visit v count as a refill of slot s when SCORING a slow-seller flag?  Yes when s's own counters prove a refill
        at v (capacity proof for s, single or one of a pair), or v is a visit graded as a proven or likely restock (door counter
        plus our slots), with s's own silence left out of that grade.  s's own 'resumed' status is
        deliberately NOT used: it is defined by an improbable silence, the very quantity the Poisson rule flags on, so it
        would score that rule by its own definition."""
        if v["known_not_refill"]:
            return False
        if s in v["proven_slots"] or s in v["pair_proof_slots"]:
            return True
        if any(x != s for x in v["proven_slots"]):
            return True
        return sum(1 for x in v["resumed_slots"] if x != s) >= LIKELY_MIN
    for v in vis:
        v["outcome_refill_slots"] = [s for s in slots if outcome_refill(v, s)]

    proven_vis = [v for v in vis if v["evidence"] == "proven refill"]
    last_proven = proven_vis[-1] if proven_vis else None
    # the latest capacity proof whose refill could be at either of exactly two visits (e.g. "Oct 27 or Nov 2")
    pairs = [p for p in proofs if len(p["visits"]) == 2]
    last_pair = max(pairs, key=lambda p: (max(p["visits"]), min(p["visits"]))) if pairs else None
    if last_pair is not None and last_proven is not None and min(last_pair["visits"]) <= last_proven["id"]:
        last_pair = None
    pair_ids = sorted(last_pair["visits"]) if last_pair else []
    pair_slots = sorted({p["slot"] for p in pairs if sorted(p["visits"]) == pair_ids})
    # last restock = the latest visit with a proven or likely refill: after it, a slot that goes silent and never sells
    # again is "sold out, not refilled"
    restock_vis = [v for v in vis if v["evidence"] in ("proven refill", "likely refill")]
    last_restock = restock_vis[-1] if restock_vis else None
    # visits with refill evidence FOR A SLOT (for display): capacity-proven for it, or it resumed there
    slot_refills = {s: [v for v in vis if s in v["proven_slots"] or s in v["resumed_slots"]] for s in slots}

    def sold_after(v):
        return {s: int(sum(e.units for e in sale_list[s] if v is not None and e.prev >= v["first_rise"])) for s in slots}
    after_proven, after_restock = sold_after(last_proven), sold_after(last_restock)
    # visits after the last single-visit-proven refill that still show refill evidence on our slots (partial refills)
    after_lp = [v for v in vis if last_proven is not None and v["id"] > last_proven["id"] and v["evidence"] in REFILL_EVIDENCE]
    later_text = "; ".join(f"{v['first_rise']:%a %b %d} ({v['evidence']}: " + (f"capacity proof {', '.join(v['proven_slots'])}; " if v["proven_slots"] else "")
                           + f"{len(v['resumed_slots'])} of our {len(slots)} slots resumed, {sum(r['units_within'] for r in v['resumed'])} units "
                           f"within {RESUME_DAYS} days)" for v in after_lp) or "none"

    # ---------------------------------------------------------- stock-out windows, ended at door visits
    # Between two consecutive sales of a slot (or after its last sale), a silence is a suspected stock-out when the slot's
    # own clock makes it improbable (P(0) < ALPHA_EMPTY).  The window ends at the LAST door visit before the slot sold
    # again (the restocker could have refilled it then), never at the sale itself.  If no door opened before the slot
    # sold again, the slot was demonstrably stocked: the silence is listed as improbable but not a stock-out.
    sale_times = {s: [pd.Timestamp(x) for x in sorted(sales.loc[sales.slot == s, "t"].unique())] for s in slots}

    def stock_windows(cutoff, sh):
        win, mid = [], []
        for s in slots:
            if not sh[s]["eligible"]:
                continue
            st = [t for t in sale_times[s] if t <= cutoff]
            if not st:
                continue
            bounds = [(T0, st[0], "start")] + [(st[i], st[i + 1] if i + 1 < len(st) else None, "sale") for i in range(len(st))]
            for a, b, kind in bounds:
                lim = b if b is not None else cutoff
                rv = [v for v in door_vis if a < v["end"] <= lim]
                if b is None:
                    end, how = cutoff, "ongoing"
                elif rv:
                    end, how = rv[-1]["end"], "visit"
                else:
                    end, how = b, "midweek"
                if (end - a).total_seconds() / 3600 < MIN_SILENCE_H:
                    continue
                lam = sh[s]["share"] * clock(a, end)
                p0 = math.exp(-lam)
                if p0 >= ALPHA_EMPTY:
                    continue
                rec = dict(slot=s, start=a, end=end, how=how, kind=kind, visits_inside=len(rv),
                           visit=rv[-1]["id"] if (how == "visit") else None, expected=lam, p0=p0)
                (mid if how == "midweek" else win).append(rec)
        return win, mid
    windows, midweek = stock_windows(T1, share)
    for w in windows:
        s = w["slot"]
        v = vis_by_id.get(w["visit"]) if w["visit"] else None
        if w["how"] == "ongoing":
            w["status"] = ("sold out, not refilled" if last_proven is not None and w["start"] >= last_proven["start"]
                           else "silent since before the last proven refill (skipped then, or no longer selling)" if last_proven is not None
                           else "ongoing")
        else:
            kind_ = ("capacity proof for this slot" if s in v["proven_slots"] else "resumed after the visit" if s in v["resumed_slots"]
                     else "no evidence for this slot")
            w["status"] = f"ended at visit {v['id']} ({v['first_rise']:%a %b %d %H:%M}, {v['evidence']}; {kind_})"
        w["visit_evidence"] = v["evidence"] if v else None
        w["fridays"] = w["visits_inside"]             # kept for older lab code: door visits inside the window
        strong = v is not None and (s in v["proven_slots"] or s in v["resumed_slots"] or v["evidence"] in ("proven refill", "likely refill"))
        w["confidence"] = "high" if (w["how"] == "visit" and strong and w["kind"] == "sale") else "medium"

    # per slot-day empty share of daytime hours.  A window starts in the hour AFTER the last sale (the slot was
    # demonstrably stocked during the hour it sold), and any slot-day with a sale counts as in stock (empty_frac 0).
    day_hours = hours[(hours.hour >= DAY_H0) & (hours.hour < DAY_H1)]

    def empty_frac_of(win):
        ef = pd.DataFrame(0.0, index=dates, columns=slots)
        for w in win:
            m = (day_hours >= floor_h(w["start"]) + pd.Timedelta(hours=1)) & (day_hours < w["end"])
            hh = day_hours[m]
            if len(hh):
                c = pd.Series(1, index=hh.date).groupby(level=0).sum() / (DAY_H1 - DAY_H0)
                ef.loc[c.index, w["slot"]] = np.maximum(ef.loc[c.index, w["slot"]], c.values)
        return ef.where(SD.values == 0, 0.0)
    empty_frac = empty_frac_of(windows)
    empty_day = empty_frac >= EMPTY_DAY_SHARE
    D["empty_slots"] = empty_day.sum(axis=1).astype(int)

    # ============================================================ 1. SERIES (Saturday-start weeks)
    D["week"] = [week_of(d) for d in dates]
    D["term_week"] = [((week_of(d) - week_of(TERM_START)).days // 7) for d in dates]   # 0 = week of Sat Sep 5 (term starts Wed Sep 9)
    daily_out = dict(
        note="units / cents / units_raw / cents_raw are MACHINE-WIDE totals (aggregates over every slot, used only for the calendar, "
             "time profiles, trend and the machine forecast). units_ours* are our 20 slots. empty_slots and active_slots count our slots.",
        date=[ds(d) for d in dates], dow=D.dow.tolist(), units_raw=D.units_raw.tolist(), units=rl(D.units, 1),
        cents_raw=D.cents_raw.tolist(), cents=rl(D.cents, 0), units_ours_raw=D.units_ours_raw.tolist(), units_ours=rl(D.units_ours, 1),
        cents_ours_raw=D.cents_ours_raw.tolist(), classes=D.classes.tolist(), institute=D.institute.tolist(),
        label=D.label.tolist(), class_day=D.class_day.astype(int).tolist(), gap_hours=D.gap_hours.tolist(),
        gap_day=D.gap_day.astype(int).tolist(), lump_units=D.lump_units.tolist(), partial=D.partial.astype(int).tolist(),
        regular=D.regular.astype(int).tolist(), empty_slots=D.empty_slots.tolist(),
        active_slots=(SD > 0).sum(axis=1).tolist(), term_week=D.term_week.tolist())

    W = []
    for w, g in D.groupby("week"):
        sl = SD.loc[g.index]
        W.append(dict(week=ds(w), term_week=int(g.term_week.iloc[0]), days=len(g),
                      partial=bool(len(g) < 7 or g.partial.any()),
                      units_raw=int(g.units_raw.sum()), units=rnd(g.units.sum(), 1), cents=int(g.cents_raw.sum()),
                      cents_spread=rnd(g.cents.sum(), 0),
                      units_ours_raw=int(g.units_ours_raw.sum()), units_ours=rnd(g.units_ours.sum(), 1), cents_ours=int(g.cents_ours_raw.sum()),
                      class_days=int(g.class_day.sum()), observed_class_days=int((g.class_day & ~g.gap_day).sum()),
                      gap_hours=int(g.gap_hours.sum()), lump_units=int(g.lump_units.sum()),
                      units_per_class_day=rnd(g.units.sum() / g.class_day.sum(), 2) if g.class_day.sum() else None,
                      active_slots=int((sl.sum() > 0).sum()),
                      slots_empty_any_day=int(empty_day.loc[g.index].any().sum()),
                      slots_empty_at_end=int(empty_day.loc[g.index[-1]].sum())))
    weekly_df = pd.DataFrame(W)
    full_weeks = [r for r in W if not r["partial"] and r["term_week"] >= 1]
    last_full = full_weeks[-1]
    peak_full = max(full_weeks, key=lambda r: r["active_slots"])

    # ---------------------------------------------------------- supply by week (our slots) and the Nov 9 photo check
    def covering(t):
        """Suspected-empty windows that cover instant t (ongoing windows run to the end of data)."""
        return [w for w in windows if w["start"] < t and (w["end"] > t or w["how"] == "ongoing")]
    supply = []
    for r in W:
        wk = dt.date.fromisoformat(r["week"])
        te = min(local_at(wk + dt.timedelta(days=7)), T1)
        cov = covering(te)
        wv = [v for v in vis if local_at(wk) <= v["first_rise"] < local_at(wk + dt.timedelta(days=7))]
        supply.append(dict(week=r["week"], partial=r["partial"], units=r["units_ours_raw"], slots_selling=r["active_slots"],
                           slots_suspected_empty_any_day=r["slots_empty_any_day"],
                           slots_empty_at_week_end=len({w["slot"] for w in cov}),
                           slots_sold_out_not_refilled=len({w["slot"] for w in cov if w["status"] == "sold out, not refilled"}),
                           slots_silent_since_before_last_restock=len({w["slot"] for w in cov if w["status"].startswith("silent since before")}),
                           slots_empty_never_refilled=len({w["slot"] for w in cov if w["how"] == "ongoing"}),
                           visits=[v["id"] for v in wv], refill_visits=[v["id"] for v in wv if v["evidence"] in REFILL_EVIDENCE],
                           visit_evidence=[v["evidence"] for v in wv]))

    photo_check = None
    ph_vis = [v for v in vis if v["first_rise"].strftime("%Y-%m-%d") == PHOTO_NOV9["date"]] if doors is not None else []
    if ph_vis:
        pv = ph_vis[0]
        t_ph = pv["start"]                       # state when the team opened the machine
        nxt = [v for v in vis if v["id"] > pv["id"]]
        t_next = nxt[0]["first_rise"] if nxt else T1
        empty_now = {w["slot"]: w for w in covering(t_ph)}
        rows_ph = []
        for s in slots:
            last = [e.t for e in sale_list[s] if e.t <= t_ph]
            dsl = (t_ph - last[-1]).total_seconds() / 86400 if last else None
            inferred = ("suspected empty" if s in empty_now else
                        "sold in the last 7 days" if dsl is not None and dsl <= 7 else "quiet, not flagged empty")
            photo = "stocked" if s in PHOTO_NOV9["stocked"] else "looks empty"
            after_ph = int(sum(e.units for e in sale_list[s] if e.prev >= pv["first_rise"] and e.t <= t_next))
            if photo == "stocked":
                verdict = ("conflict: photo shows stock, model says empty" if inferred == "suspected empty"
                           else "agree: stocked" if inferred == "sold in the last 7 days" else "stocked but quiet: a real slow seller")
            else:
                verdict = ("agree: empty" if inferred == "suspected empty" else
                           "photo empty, sold recently: sold out since its last sale" if inferred == "sold in the last 7 days"
                           else "photo empty, model silent: missed stock-out (rarely-selling slot, or the clock too slow)")
                if after_ph:
                    verdict = ("conflict: looks empty but sold before the next visit (product hidden at the back of the rail, "
                               "or bought by the visitors)")
            rows_ph.append(dict(slot=s, photo=photo, inferred=inferred, days_since_last_sale=rnd(dsl, 1),
                                units_after_photo_before_next_visit=after_ph, verdict=verdict,
                                empty_window_status=empty_now[s]["status"] if s in empty_now else None))
        tally = pd.Series([r["verdict"] for r in rows_ph]).value_counts().to_dict()
        ph_st = [r for r in rows_ph if r["photo"] == "stocked"]
        ph_em = [r for r in rows_ph if r["photo"] == "looks empty"]
        photo_check = dict(
            note=PHOTO_NOV9["note"] + " Compared with the model's state at the start of that visit "
                 f"({t_ph:%a %b %d %H:%M}): 'suspected empty' = inside a stock-out window. Product further back in a rail "
                 "can be hidden, so 'looks empty' is not proof.",
            visit=pv["id"], at=t_ph.isoformat(timespec="minutes"), photo_stocked=PHOTO_NOV9["stocked"], rails=len(slots),
            stocked_and_flagged_empty=[r["slot"] for r in ph_st if r["inferred"] == "suspected empty"],
            stocked_and_selling=[r["slot"] for r in ph_st if r["inferred"] == "sold in the last 7 days"],
            stocked_but_quiet=[r["slot"] for r in ph_st if r["inferred"] == "quiet, not flagged empty"],
            empty_and_flagged_empty=[r["slot"] for r in ph_em if r["inferred"] == "suspected empty"],
            empty_and_sold_recently=[r["slot"] for r in ph_em if r["inferred"] == "sold in the last 7 days"],
            empty_and_not_flagged=[r["slot"] for r in ph_em if r["inferred"] == "quiet, not flagged empty"],
            empty_but_sold_after=[r["slot"] for r in ph_em if r["units_after_photo_before_next_visit"] > 0],
            agreement=rnd(sum(1 for r in rows_ph if r["verdict"].startswith("agree")) / len(rows_ph), 3),
            no_conflict_share=rnd(sum(1 for r in rows_ph if not r["verdict"].startswith("conflict")) / len(rows_ph), 3),
            tally=tally, slots=rows_ph)

    # hourly weekday x hour matrices: MACHINE-WIDE (a time-of-day profile), regular era full days, midpoint/spread timing
    hr_days = [d for d in dates if D.loc[d, "regular"] and not D.loc[d, "partial"] and d > REGULAR_START.date()]
    sp = mspread[mspread.date.isin(hr_days)]

    def hmatrix(day_filter):
        dd = [d for d in hr_days if day_filter(d)]
        n = pd.Series([d.weekday() for d in dd]).value_counts()
        u = sp[sp.date.isin(dd)].groupby([sp.hour.dt.dayofweek, sp.hour.dt.hour]).units.sum()
        m = [[(rnd(u.get((wd, h), 0.0) / n[wd], 3) if wd in n.index else None) for h in range(24)] for wd in range(7)]
        return dict(mean_units_per_day=m, n_days=[int(n.get(wd, 0)) for wd in range(7)])

    hourly_out = dict(
        note="MACHINE-WIDE mean units per day in each weekday x hour cell (aggregate over every slot), regular-polling era full days "
             f"({ds(hr_days[0])} to {ds(hr_days[-1])}). Sales timed at the midpoint of each poll interval.",
        all=hmatrix(lambda d: True), class_days=hmatrix(lambda d: D.loc[d, "class_day"]),
        no_class_days=hmatrix(lambda d: not D.loc[d, "class_day"]),
        no_class_weekdays=[ds(d) for d in hr_days if D.loc[d, "no_class_weekday"]])
    hour_tot = sp.groupby(sp.hour.dt.hour).units.sum().reindex(range(24)).fillna(0)
    hour_raw_stamp = era.groupby(era.t.dt.hour).units.sum().reindex(range(24)).fillna(0)
    hourly_out["hour_profile_share"] = rl(hour_tot / hour_tot.sum(), 4)
    hourly_out["hour_units_midpoint"] = rl(hour_tot, 1)
    hourly_out["hour_units_poll_stamp"] = rl(hour_raw_stamp, 0)
    peak_hour = int(hour_tot.idxmax())
    # Friday 3 PM by poll stamp (machine-wide totals): how much of it is one gap lump
    fri = msales[msales.t.dt.weekday == 4]
    f15 = fri[fri.t.dt.hour == 15]
    f15l = f15[f15.lump]
    big15 = f15l.loc[f15l.units.idxmax()] if len(f15l) else None
    hourly_out["poll_stamp"] = dict(
        note="Machine-wide units by the poll stamp (not midpoint timing), for the Friday 3 PM callout.",
        friday_units=int(fri.units.sum()), friday_15h_units=int(f15.units.sum()),
        friday_15h_largest_lump=int(big15.units) if big15 is not None else 0,
        lump_at=big15.t.isoformat(timespec="minutes") if big15 is not None else None,
        lump_hours=rnd(big15.gap_h, 1) if big15 is not None else None)

    # ============================================================ 2. SEASON / TREND / CALENDAR (machine-wide totals)
    reg = D[D.regular].copy()
    y_reg = reg.units.astype(float)
    # classical multiplicative day-of-week index against a centered 7-day moving average, taken as a RATIO OF
    # TOTALS (sum of units / sum of the moving average, by weekday) so the near-empty late weeks do not dominate
    # it the way a mean of daily ratios lets them.  No-class weekdays (break, closure) are left out.
    cma = y_reg.rolling(7, center=True).mean()
    keep_ix = (reg.class_day | (reg.dow >= 5)) & cma.notna() & (cma > 0)
    idx_raw = (y_reg[keep_ix].groupby(reg.dow[keep_ix]).sum() / cma[keep_ix].groupby(reg.dow[keep_ix]).sum()).reindex(range(7))
    dow_idx = idx_raw / idx_raw.mean()
    dow_mean = reg.groupby("dow").units.mean().reindex(range(7))
    dow_mean_class = reg[reg.class_day].groupby("dow").units.mean().reindex(range(7))

    stl = STL(y_reg.values, period=7, robust=True).fit()
    decomp = dict(method="STL (period 7, robust) on daily gap-spread machine-wide units, regular-polling era",
                  date=[ds(d) for d in reg.index], observed=rl(y_reg, 1), trend=rl(stl.trend, 2),
                  seasonal=rl(stl.seasonal, 2), remainder=rl(stl.resid, 2),
                  classical_cma7=rl(cma, 2))
    var_rem = np.var(stl.resid)
    decomp["strength_trend"] = rnd(max(0, 1 - var_rem / np.var(stl.trend + stl.resid)), 3)
    decomp["strength_seasonal"] = rnd(max(0, 1 - var_rem / np.var(stl.seasonal + stl.resid)), 3)

    # trend slope test (machine-wide)
    tdf = reg.assign(t_week=[(d - REGULAR_START.date()).days / 7 for d in reg.index], dowc=[DOW[x] for x in reg.dow])
    m_tr = smf.ols("units ~ t_week + C(dowc)", data=tdf).fit(cov_type="HAC", cov_kwds={"maxlags": 7})
    tcd = tdf[tdf.class_day]
    m_tr_c = smf.ols("units ~ t_week + C(dowc)", data=tcd).fit(cov_type="HAC", cov_kwds={"maxlags": 5})

    def slope(m, name):
        ci = m.conf_int().loc["t_week"]
        return dict(model=name, slope_per_week=rnd(m.params["t_week"], 2), ci_lo=rnd(ci.iloc[0], 2), ci_hi=rnd(ci.iloc[1], 2),
                    p=rnd(m.pvalues["t_week"], 4), n=int(m.nobs))
    trend_test = [slope(m_tr, "all days: machine units ~ week + weekday (HAC SE)"),
                  slope(m_tr_c, "class days only: machine units ~ week + weekday (HAC SE)")]

    # academic-calendar regressions.  A: machine-wide units (calendar effects).  B: our 20 slots' units with the count of
    # our slots suspected empty (supply control).  Each table in the report quotes its own model.
    rdf = tdf.copy()
    rdf["no_class"] = rdf.no_class_weekday.astype(int)
    rdf["week_of_term"] = [(d - TERM_START).days / 7 for d in rdf.index]
    rdf["dowc"] = pd.Categorical(rdf.dowc, categories=DOW)
    nc_days = [d for d in rdf.index if rdf.loc[d, "no_class"] == 1]
    nc_text = ", ".join(f"{d:%a} {md(d)} ({(D.loc[d, 'label'] or 'no classes').split(' - ')[0]})" for d in nc_days) or "none"
    specs = {"A": "units ~ C(dowc) + no_class + week_of_term",
             "B0": "units_ours ~ C(dowc) + no_class + week_of_term",
             "B": "units_ours ~ C(dowc) + no_class + week_of_term + empty_slots"}
    scope_of = {"A": "machine-wide daily units (aggregate over every slot)", "B0": "daily units of our 20 slots",
                "B": "daily units of our 20 slots, with a supply control"}
    meaning = {
        "Intercept": "Model baseline: a Monday with classes, extrapolated back to the start of term (week_of_term = 0 is Wed Sep 9; "
                     "no data before Sep 21, so this value is not observed).",
        "no_class": f"Change in units on a weekday with no classes ({nc_text}), vs the same weekday with classes. "
                    f"Only {len(nc_days)} such day{'s' if len(nc_days) != 1 else ''} in the regular era.",
        "week_of_term": "Change in daily units per extra week into the term (trend), holding weekday fixed.",
        "empty_slots": "Change in our daily units for each extra one of our slots suspected empty that day (supply control).",
    }
    regression = dict(note="OLS on daily gap-spread units, regular-polling era "
                           f"({ds(rdf.index[0])} to {ds(rdf.index[-1])}, n={len(rdf)}). HC3 robust standard errors. "
                           "Weekday coefficients are lifts vs Monday. Model A: machine-wide totals (calendar effects). "
                           "Model B0: our 20 slots. Model B: B0 plus the count of our slots suspected empty (supply control).",
                      models={})
    for k, f in specs.items():
        m = smf.ols(f, data=rdf).fit(cov_type="HC3")
        ci = m.conf_int()
        coefs = []
        for term in m.params.index:
            nm = term.replace("C(dowc)[T.", "").replace("]", "")
            mean = meaning.get(term, f"{nm} vs Monday: extra units per day." if "dowc" in term else "")
            coefs.append(dict(term=nm, coef=rnd(m.params[term], 2), se=rnd(m.bse[term], 2), ci_lo=rnd(ci.loc[term, 0], 2),
                              ci_hi=rnd(ci.loc[term, 1], 2), p=rnd(m.pvalues[term], 4), meaning=mean))
        regression["models"][k] = dict(formula=f, y=scope_of[k], r2=rnd(m.rsquared, 3), adj_r2=rnd(m.rsquared_adj, 3),
                                       n=int(m.nobs), coefs=coefs)
    regression["corr_week_vs_empty_slots"] = rnd(np.corrcoef(rdf.week_of_term, rdf.empty_slots)[0, 1], 3)
    regB = {c["term"]: c for c in regression["models"]["B"]["coefs"]}
    regA = {c["term"]: c for c in regression["models"]["A"]["coefs"]}
    wd_terms = {x: regA.get(x, {}) for x in ("Tue", "Wed", "Thu", "Fri")}
    wd_cross = {x: (t.get("ci_lo") is not None and t["ci_lo"] < 0 < t["ci_hi"]) for x, t in wd_terms.items()}
    if all(wd_cross.values()):
        wd_ci_text = "every Tue-Fri vs Monday 95% CI in model A crosses zero"
    elif not any(wd_cross.values()):
        wd_ci_text = "no Tue-Fri vs Monday 95% CI in model A crosses zero"
    else:
        wd_ci_text = (f"in model A the 95% CI crosses zero for {', '.join(x for x, c in wd_cross.items() if c)} "
                      f"and excludes zero for {', '.join(x for x, c in wd_cross.items() if not c)} (vs Monday)")
    wd_ci_detail = ", ".join(f"{x} {t.get('coef'):+.1f} [{t.get('ci_lo')}, {t.get('ci_hi')}]" for x, t in wd_terms.items() if t)
    dow_out = dict(
        method="Classical ratio-to-moving-average, ratio of totals: sum of each weekday's machine-wide units / sum of the centered "
               "7-day mean on those days, scaled to average 1.0 (multiplicative index: 1.30 = 30% above an average day). Regular-polling "
               f"era; class days and weekends only (no-class weekdays left out). Calendar regression check: {wd_ci_text}.",
        index=rl(dow_idx, 3), mean_units_per_day=rl(dow_mean, 2), mean_units_per_class_day=rl(dow_mean_class, 2),
        n_days=reg.groupby("dow").size().reindex(range(7)).fillna(0).astype(int).tolist(),
        weekday_labels=DOW)

    term_cycle = [dict(term_week=int(r.term_week), week=r.week, units=r.units, units_ours=r.units_ours,
                       units_per_class_day=r.units_per_class_day, class_days=r.class_days, active_slots=r.active_slots,
                       slots_empty_any_day=r.slots_empty_any_day, partial=r.partial)
                  for r in weekly_df.itertuples()]

    # ============================================================ 3. BACKTESTS (daily machine totals, H=7, Saturday origins)
    hist_all = D.loc[D.index >= TERM_START, "units"].astype(float)       # gap-spread units, from term start
    _hist_cache = {}

    def hist_at(o):
        """Training history for plan date o: term start to the day before o, with gap lumps re-spread using only
        data reported before o (no look-ahead into later weeks' weekday/hour shape)."""
        if o not in _hist_cache:
            s_ = spread_daily_asof(o)
            hd = [d for d in dates if TERM_START <= d < o]
            _hist_cache[o] = pd.Series([float(s_.get(d, 0.0)) for d in hd], index=hd)
        return _hist_cache[o]
    ncw = D["no_class_weekday"]
    first_test = week_of(REGULAR_START.date()) + dt.timedelta(days=7)     # first Saturday fully in the regular era
    origins = [d for d in sorted({week_of(x) for x in dates}) if d >= first_test]

    def ses_grid(y):
        best = None
        for a in np.round(np.arange(0.05, 1.0, 0.05), 2):
            lvl, sse = y[0], 0.0
            for v in y[1:]:
                sse += (v - lvl) ** 2
                lvl = a * v + (1 - a) * lvl
            if best is None or sse < best[0]:
                best = (sse, a, lvl)
        return best[1], best[2]

    def wma(y, w):
        w = np.asarray(w, float)
        return float(np.dot(y[-len(w):][::-1], w) / w.sum())

    # every exponential-smoothing fit is counted: how many fell back (exception) and how many raised warnings
    fit_log = {}

    def es_fit(name, y, **kw):
        lg = fit_log.setdefault(name, dict(fits=0, fallbacks=0, warned=0))
        lg["fits"] += 1
        try:
            with warnings.catch_warnings(record=True) as wl:
                warnings.simplefilter("always")
                m = ExponentialSmoothing(y, initialization_method="estimated", **kw).fit(optimized=True)
            if wl:
                lg["warned"] += 1
            return m
        except Exception:
            lg["fallbacks"] += 1
            return None

    def hw(name, y, n, trend=None, damped=False):
        m = es_fit(name, y, trend=trend, damped_trend=damped, seasonal="add", seasonal_periods=7)
        if m is None:   # fallback: seasonal naive (counted in fit_log and reported)
            return np.array([y[-7 + (i % 7)] for i in range(n)])
        return np.asarray(m.forecast(n))

    def calreg(hist, fdates):
        """OLS: units ~ weekday dummies + no-class flag + recent level (mean of days t-13..t-7).
        Multi-step forecasts fill the level recursively with earlier forecasts."""
        full_idx = list(hist.index) + list(fdates)
        y = pd.Series(list(hist.values) + [np.nan] * len(fdates), index=full_idx, dtype=float)
        X = pd.DataFrame(index=full_idx)
        for i in range(7):
            X[DOW[i]] = [1.0 if d.weekday() == i else 0.0 for d in full_idx]
        X["no_class"] = [1.0 if ncw.get(d, cal_days.get(ds(d), {}).get("classes", "normal") != "normal" and d.weekday() < 5) else 0.0
                         for d in full_idx]
        X["level"] = y.shift(7).rolling(7).mean()
        tr = X.loc[hist.index].assign(y=hist.values).dropna()
        cols = DOW + (["no_class"] if tr.no_class.sum() > 0 else []) + ["level"]
        beta = np.linalg.lstsq(tr[cols].values, tr.y.values, rcond=None)[0]
        wk_mean = float(hist[[d.weekday() >= 5 for d in hist.index]].mean())
        out = []
        for d in fdates:
            X.loc[d, "level"] = y.shift(7).rolling(7).mean().loc[d]
            if X.loc[d, "no_class"] == 1 and "no_class" not in cols:
                f = wk_mean                                          # prior: a no-class weekday looks like a weekend day
            else:
                f = float(np.dot(X.loc[d, cols].values.astype(float), beta))
            f = max(f, 0.0)
            y.loc[d] = f
            out.append(f)
        return np.array(out), dict(zip(cols, [float(b) for b in beta]))

    def run_models(hist, fdates):
        y = hist.values.astype(float)
        n = len(fdates)
        last_week = {d.weekday(): v for d, v in zip(hist.index[-7:], y[-7:])}
        res = {}
        res["Naive (last day)"] = np.full(n, y[-1])
        res["Seasonal naive (same weekday last week)"] = np.array([last_week[d.weekday()] for d in fdates])
        for k in (3, 7, 14, 28):
            res[f"SMA k={k}"] = np.full(n, y[-k:].mean())
        res["WMA linear 4-3-2-1"] = np.full(n, wma(y, [4, 3, 2, 1]))
        res["WMA halving 8-4-2-1"] = np.full(n, wma(y, [8, 4, 2, 1]))
        a, lvl = ses_grid(y)
        res["SES (alpha fit by grid)"] = np.full(n, lvl)
        hm = es_fit("Holt (damped trend)", y, trend="add", damped_trend=True)
        res["Holt (damped trend)"] = np.asarray(hm.forecast(n)) if hm is not None else np.full(n, lvl)
        res["Holt-Winters additive (weekly)"] = hw("Holt-Winters additive (weekly)", y, n)
        res["Holt-Winters additive + damped trend"] = hw("Holt-Winters additive + damped trend", y, n, "add", True)
        res["Calendar regression (weekday + class flag + level)"] = calreg(hist, fdates)[0]
        return {k: np.clip(v, 0, None) for k, v in res.items()}, a

    bt_rows, per_origin, resid = [], [], {}
    for o in origins:
        hist = hist_at(o)
        tdates = [d for d in (o + dt.timedelta(days=i) for i in range(H)) if d <= d1]
        act = D.loc[tdates, "units"].values.astype(float)
        fcs, alpha = run_models(hist, tdates)
        calendar_flag = bool(D.loc[tdates, "no_class_weekday"].any())
        po = dict(origin=ds(o), test_dates=[ds(d) for d in tdates], actual=rl(act, 1), days=len(tdates),
                  contains_no_class_weekday=calendar_flag, ses_alpha=rnd(alpha, 2), forecasts={},
                  history_start=ds(hist.index[0]), history=rl(hist, 2))   # the as-of training series, so the lab can rerun it
        cd = D.loc[tdates, "class_day"].values
        for mname, f in fcs.items():
            po["forecasts"][mname] = rl(f, 2)
            bt_rows.append(dict(model=mname, origin=ds(o), calendar_flag=calendar_flag, **metrics(act, f)))
            resid.setdefault(mname, []).extend(zip(act - f, cd, act, f))
        per_origin.append(po)
    bt = pd.DataFrame(bt_rows)
    model_names = list(per_origin[0]["forecasts"].keys())
    fit_log_backtest = {k: dict(v) for k, v in fit_log.items()}

    avg = {}
    for mname in model_names:
        sub = bt[bt.model == mname]
        sub_nb = sub[~sub.calendar_flag]
        ae = np.array([abs(r[0]) for r in resid[mname]])
        acts = np.array([r[2] for r in resid[mname]])
        avg[mname] = dict(mae=sub.mae.mean(), rmse=sub.rmse.mean(), bias=sub.bias.mean(),
                          wape=ae.sum() / acts.sum(), mape=sub.mape.mean(),
                          mae_no_break_windows=sub_nb.mae.mean(), bias_no_break_windows=sub_nb.bias.mean(),
                          wins=int(sum(bt[bt.origin == o].sort_values("mae").iloc[0].model == mname for o in bt.origin.unique())))
    ranking = sorted(model_names, key=lambda m: avg[m]["mae"])
    SN = "Seasonal naive (same weekday last week)"
    best1, best2 = ranking[0], ranking[1]
    beats_sn = [m for m in ranking if avg[m]["mae"] < avg[SN]["mae"]]

    # MAE vs k for SMA ("can a better k fix it?")
    mae_by_k = []
    for k in range(1, 29):
        maes = []
        for o in origins:
            hist = hist_at(o)
            tdates = [d for d in (o + dt.timedelta(days=i) for i in range(H)) if d <= d1]
            act = D.loc[tdates, "units"].values
            maes.append(np.mean(np.abs(act - hist.values[-k:].mean())))
        mae_by_k.append(rnd(np.mean(maes), 3))

    # 80% intervals from backtest residual quantiles, by day type
    intervals = {}
    for mname in model_names:
        r = np.array([(e, c) for e, c, *_ in resid[mname]], dtype=float)
        q = {}
        for key, mask in (("class_day", r[:, 1] == 1), ("other_day", r[:, 1] == 0)):
            e = r[mask, 0]
            q[key] = (float(np.quantile(e, 0.1)), float(np.quantile(e, 0.9))) if len(e) >= 5 else (float(e.min()), float(e.max()))
        cov = np.mean([(q["class_day" if c else "other_day"][0] <= e <= q["class_day" if c else "other_day"][1])
                       for e, c, *_ in resid[mname]])
        intervals[mname] = dict(q10_class=q["class_day"][0], q90_class=q["class_day"][1], q10_other=q["other_day"][0],
                                q90_other=q["other_day"][1], in_sample_coverage=float(cov))

    # weekly backtest, horizon 1 (Saturday-start weeks, machine totals, full weeks from term week 1).  Each test week trains
    # on weekly sums of the series re-spread AS OF that week's start (no later data shapes the training weeks).
    wk_full = weekly_df[(weekly_df.term_week >= 1) & (~weekly_df.partial)].reset_index(drop=True)
    wy = wk_full.units.astype(float).values
    wcd = wk_full.class_days.values
    wweeks = [dt.date.fromisoformat(w) for w in wk_full.week]
    wk_rows, wk_per = [], []
    for i in range(3, len(wk_full)):
        sp_ = spread_daily_asof(wweeks[i])
        y = np.array([sum(float(sp_.get(wweeks[j] + dt.timedelta(days=k), 0.0)) for k in range(7)) for j in range(i)])
        f = {"Naive (last week)": y[-1], "SMA k=2": y[-2:].mean(), "SMA k=3": y[-3:].mean(),
             "WMA linear 3-2-1": wma(y, [3, 2, 1]), "WMA halving 4-2-1": wma(y, [4, 2, 1]), "SES (alpha fit by grid)": ses_grid(y)[1],
             "Class-day naive (last week per class day x class days)": y[-1] / max(wcd[i - 1], 1) * wcd[i]}
        best = None
        for a in np.arange(0.1, 1.0, 0.1):
            for b in np.arange(0.1, 1.0, 0.1):
                lv, tr_, sse = y[0], (y[1] - y[0]) if len(y) > 1 else 0.0, 0.0
                for v in y[1:]:
                    sse += (v - (lv + tr_)) ** 2
                    ln = a * v + (1 - a) * (lv + tr_)
                    tr_ = b * (ln - lv) + (1 - b) * tr_
                    lv = ln
                if best is None or sse < best[0]:
                    best = (sse, lv + tr_)
        f["Holt (grid)"] = max(best[1], 0)
        for k, v in f.items():
            wk_rows.append(dict(model=k, week=wk_full.week[i], actual=wy[i], forecast=v, error=wy[i] - v))
        wk_per.append(dict(week=wk_full.week[i], actual=rnd(wy[i], 1), train=rl(y, 1), forecasts={k: rnd(v, 1) for k, v in f.items()}))
    wkb = pd.DataFrame(wk_rows)
    weekly_bt = dict(note="Weekly (Saturday-start) machine-wide units, gap-spread, horizon 1 week, expanding window; each test week trains on "
                          "weekly sums re-spread with only data reported before it. Seasonal naive needs last year's same week, "
                          "which does not exist, so it is not available here.",
                     weeks=[ds(w) for w in wk_full.week], actual=rl(wy, 1), per_week=wk_per,
                     summary=[dict(model=m, mae=rnd(g.error.abs().mean(), 2), rmse=rnd(np.sqrt((g.error ** 2).mean()), 2),
                                   bias=rnd(g.error.mean(), 2), wape=rnd(g.error.abs().sum() / g.actual.sum(), 3), n=len(g))
                              for m, g in wkb.groupby("model", sort=False)])
    weekly_bt["summary"].sort(key=lambda r: r["mae"])
    # current partial week, scaled by the class-day weekday profile
    last = weekly_df.iloc[-1]
    wk_days = D[D.week == week_of(d1)]
    prof_share = dow_mean_class.fillna(0)
    covered = sum(prof_share[d.weekday()] for d in wk_days.index if wk_days.loc[d, "class_day"])
    full = sum(prof_share[d.weekday()] for d in (week_of(d1) + dt.timedelta(days=i) for i in range(7))
               if cal_days.get(ds(d), {}).get("classes") == "normal" and d.weekday() < 5)
    weekly_bt["current_partial_week"] = dict(week=ds(week_of(d1)), days_observed=int(last.days), units_observed=rnd(last.units, 1),
                                             scaled_full_week=rnd(last.units / covered * full, 1) if covered else None,
                                             method="observed / share of a normal class-week's weekday profile covered so far")

    # ============================================================ 4. FORECAST FORWARD (machine totals)
    fdates = [d1 + dt.timedelta(days=i + 1) for i in range(FORECAST_DAYS)]
    fcal = [cal_days.get(ds(d), {}) for d in fdates]
    f_class = [(c.get("classes") == "normal" and d.weekday() < 5) for c, d in zip(fcal, fdates)]
    full_fc, _ = run_models(hist_all, fdates)
    _, beta_full = calreg(hist_all, fdates)
    wk_mean_all = float(hist_all[[d.weekday() >= 5 for d in hist_all.index]].mean())
    fit_log_all = {k: dict(v) for k, v in fit_log.items()}
    # breaks and closures ahead, from the calendar file (not typed in)
    ahead_events = [e for e in cal.get("events", []) if e["end"] >= ds(fdates[0]) and e["start"] <= ds(fdates[-1])
                    and e.get("classes") == "none"]

    def ev_text(e):
        a, b = dt.date.fromisoformat(e["start"]), dt.date.fromisoformat(e["end"])
        return f"{e['label'].split(' - ')[0].split(' (')[0]} {md(a)}" + (f"-{md(b) if b.month != a.month else b.day}" if b != a else "")
    breaks_text = "; ".join(ev_text(e) for e in ahead_events) or "none in the next four weeks"

    def overlay(name, f):
        if name.startswith("Calendar regression"):
            return f, False
        g = f.copy()
        for i, d in enumerate(fdates):
            if d.weekday() < 5 and not f_class[i]:
                g[i] = wk_mean_all
        return g, True

    forecasts = dict(dates=[ds(d) for d in fdates], dow=[d.weekday() for d in fdates],
                     classes=[c.get("classes", "unknown") for c in fcal], institute=[c.get("institute", "unknown") for c in fcal],
                     label=[c.get("label", "") for c in fcal], models={}, breaks_ahead=breaks_text,
                     note="Machine-wide daily totals (an aggregate). Trained on all days from term start (Sep 9) to the last poll. Models "
                          "that do not know the calendar get a calendar overlay: weekdays with no classes "
                          f"({breaks_text}) are set to the average weekend day ({wk_mean_all:.2f} units). 80% range = forecast + 10th/90th "
                          f"percentile of that model's 1- to {H}-day-ahead backtest errors, by day type (class day vs other), floored at 0. "
                          f"The range is backtested only for the first {H} days; later days are not backtested at that horizon.",
                     interval_horizon_days=H,
                     supply_warning=(f"The forecasts follow the level of a machine whose last single-visit-proven refill was "
                                     + (f"{last_proven['first_rise']:%a %b %d}" if last_proven is not None else "late October")
                                     + f" (door counter; the operators stopped regular refilling to prepare for the project; later visits refilled "
                                     f"only some of our rails: {later_text}), so they forecast what a run-down machine will sell, not demand. "
                                     "Our slots that sold anything fell from "
                                     f"{peak_full['active_slots']} of {len(slots)} (best full week, week of Sat {md(dt.date.fromisoformat(peak_full['week']))}) "
                                     f"to {last_full['active_slots']} in the last full week (week of Sat {md(dt.date.fromisoformat(last_full['week']))}). "
                                     "Read demand from the 'restocked scenario' (our slots' in-stock rates)."),
                     last_full_week=last_full["week"], peak_full_week=peak_full["week"])
    # forward forecasts: the two best models other than the bar, plus seasonal naive (the bar), so the set always has 3
    fwd_models = [m for m in ranking if m != SN][:2] + [SN]
    for mname in fwd_models:
        f, ov = overlay(mname, full_fc[mname])
        iv = intervals[mname]
        lo = [max(0.0, v + (iv["q10_class"] if c else iv["q10_other"])) for v, c in zip(f, f_class)]
        hi = [max(0.0, v + (iv["q90_class"] if c else iv["q90_other"])) for v, c in zip(f, f_class)]
        forecasts["models"][mname] = dict(forecast=rl(f, 2), lo80=rl(lo, 2), hi80=rl(hi, 2), calendar_overlay=ov,
                                          backtest_mae=rnd(avg[mname]["mae"], 2), rank=ranking.index(mname) + 1)
    if beta_full:
        forecasts["calendar_regression_coefficients"] = {k: rnd(v, 3) for k, v in beta_full.items()}
    forecasts["fit_log"] = dict(note="Exponential-smoothing fits in the backtest and the forward forecast: fits, fallbacks (fit failed; "
                                     "Holt-Winters falls back to seasonal naive, Holt to the SES level) and fits that raised a warning "
                                     "(e.g. no convergence).", backtest=fit_log_backtest, all=fit_log_all)

    # restocked (supply-restored) scenario for OUR 20 slots: sum of their in-stock rates per class day
    reg_class = [d for d in dates if D.loc[d, "regular"] and D.loc[d, "class_day"]]
    recent_cut = d1 - dt.timedelta(days=27)

    def rate_over(s, days_, ef=None):
        ef = empty_frac if ef is None else ef
        return float(SD.loc[days_, s].sum()), float((1 - ef.loc[days_, s]).sum())
    rate_rows = {}
    for s in slots:
        u_all, e_all = rate_over(s, reg_class)
        u_rec, e_rec = rate_over(s, [d for d in reg_class if d >= recent_cut])
        rate_rows[s] = dict(rate_all=u_all / e_all if e_all > 0 else 0.0, instock_days_all=e_all,
                            rate_recent=u_rec / e_rec if e_rec >= 3 else (u_all / e_all if e_all > 0 else 0.0),
                            instock_days_recent=e_rec, units_all=int(u_all))
    restock_rate_recent = sum(r["rate_recent"] for r in rate_rows.values())
    restock_rate_all = sum(r["rate_all"] for r in rate_rows.values())
    # our slots' class-day weekday shape (normalised over Mon-Fri); weekends/break days at our average weekend day
    ours_reg = D[D.regular]
    ours_dow_class = ours_reg[ours_reg.class_day].groupby("dow").units_ours.mean().reindex(range(7))
    shape = (ours_dow_class.iloc[:5] / ours_dow_class.iloc[:5].mean()).fillna(1)
    wk_mean_ours = float(D.loc[[d for d in dates if d >= TERM_START and d.weekday() >= 5], "units_ours"].mean())
    scen_recent = [restock_rate_recent * shape[d.weekday()] if c else wk_mean_ours for d, c in zip(fdates, f_class)]
    scen_all = [restock_rate_all * shape[d.weekday()] if c else wk_mean_ours for d, c in zip(fdates, f_class)]
    forecasts["restocked_scenario"] = dict(
        scope="our 20 slots", recent_rates=rl(scen_recent, 2), regular_era_rates=rl(scen_all, 2),
        per_class_day_recent=rnd(restock_rate_recent, 1), per_class_day_regular_era=rnd(restock_rate_all, 1),
        note="OUR 20 SLOTS only (the model forecasts above are machine-wide totals): if each of our slots were kept stocked, the sum of "
             "their units per in-stock class day (last 4 weeks where a slot was in stock >= 3 class days, else its whole regular-era "
             "rate), shaped by our class-day weekday pattern; other days at our average weekend day. Upper reference, not a forecast: "
             "it assumes the products that sold before are still there and still wanted.")
    # weekly sums of forward forecasts (Saturday-start weeks)
    fw = {}
    for i, d in enumerate(fdates):
        fw.setdefault(ds(week_of(d)), []).append(i)
    forecasts["weekly"] = [dict(week=w, days=len(ix), class_days=int(sum(f_class[i] for i in ix)),
                                **{m: rnd(sum(forecasts["models"][m]["forecast"][i] for i in ix), 1) for m in forecasts["models"]},
                                restocked_recent=rnd(sum(scen_recent[i] for i in ix), 1))
                           for w, ix in fw.items()]

    # ============================================================ 5. SLOT LEVEL (our 20 slots only)
    rd = reads.sort_values(["slot", "t"]).copy()
    rd["old"] = rd.groupby("slot").price_cents.shift()
    pcs = rd[rd.old.notna() & (rd.old != rd.price_cents)]
    price_hist = {s: [] for s in slots}
    first_price = rd.groupby("slot").price_cents.first()
    for s in slots:
        price_hist[s].append(dict(from_=ds(T0.date()), cents=int(first_price[s])))
    for r in pcs.itertuples():
        price_hist[r.slot].append(dict(from_=r.t.isoformat(timespec="minutes"), cents=int(r.price_cents)))
    # Price rules (stated, and what the code does):
    #  * price paid on a sale = the listed price in effect when the sale's poll interval began: the slot's last price
    #    reading at or before the interval's opening poll (`prev`); a sale before the slot's first reading takes that reading.
    #  * price on a day (slot_daily.p) = the listed price at 12:00 noon ET: the last reading before noon, or, on a day with
    #    no reading before noon, the day's first reading; days with no reading carry the last price forward.
    prr = rd[["slot", "t", "price_cents"]].rename(columns={"t": "read_t", "price_cents": "price_at_sale"}).sort_values("read_t")
    s2 = pd.merge_asof(sales.sort_values("prev"), prr, left_on="prev", right_on="read_t", by="slot", direction="backward")
    s2b = pd.merge_asof(sales.sort_values("prev"), prr, left_on="prev", right_on="read_t", by="slot", direction="forward")
    s2["price_at_sale"] = s2.price_at_sale.fillna(s2b.price_at_sale)
    sales = s2.drop(columns=["read_t"]).sort_values(["t", "slot"]).reset_index(drop=True)
    sale_list = {s: list(sales[sales.slot == s].sort_values("t")[["prev", "t", "units"]].itertuples(index=False)) for s in slots}
    rd["date"] = rd.t.dt.date
    noon_last = rd[rd.t.dt.hour < 12].groupby(["date", "slot"]).price_cents.last()
    day_first = rd.groupby(["date", "slot"]).price_cents.first()
    price_day = (noon_last.combine_first(day_first).unstack().reindex(index=dates, columns=slots).ffill().bfill())

    weeks_full_reg = [w for w in weekly_df.itertuples() if not w.partial and local_at(dt.date.fromisoformat(w.week)) >= REGULAR_START]
    reg_weeks = [dt.date.fromisoformat(w.week) for w in weeks_full_reg]
    reg_days_all = [d for d in dates if D.loc[d, "regular"]]
    slot_rows = []
    for s in slots:
        su = sales[sales.slot == s]
        wk_units = np.array([SD.loc[[d for d in dates if week_of(d) == w], s].sum() for w in reg_weeks], float)
        wd_units = SD.loc[[d for d in reg_days_all if d.weekday() < 5], s]
        wd_tot = wd_units.sum()
        fri_units = wd_units[[d.weekday() == 4 for d in wd_units.index]].sum()
        # average price = units-weighted price in effect at each sale on regular-era class days; a slot with no such sale
        # takes the mean of its noon listed price over those days
        sc = su[su.date.isin(set(reg_class))]
        avg_p = float((sc.price_at_sale * sc.units).sum() / sc.units.sum()) if sc.units.sum() > 0 else float(price_day.loc[reg_class, s].mean())
        last_sale = su.t.max() if len(su) else None
        slot_rows.append(dict(
            slot=s, row=int(rowcol.loc[s, "row"]), col=int(rowcol.loc[s, "col"]),
            units=int(su.units.sum()), cents=int(su.cents.sum()), units_regular=int(SD.loc[reg_days_all, s].sum()),
            selling_weeks=int((sw[s] > 0).sum()) if s in sw.index.get_level_values(0) else 0,
            units_per_week=su.units.sum() / ((T1 - T0).total_seconds() / 86400 / 7),
            instock_class_days=rate_rows[s]["instock_days_all"], class_days=len(reg_class),
            rate=rate_rows[s]["rate_all"], rate_recent=rate_rows[s]["rate_recent"],
            avg_price=avg_p / 100, avg_price_rule="units-weighted price in effect at each regular-era class-day sale" if sc.units.sum() > 0
            else "mean noon listed price on regular-era class days (no such sales)",
            current_price=int(rd[rd.slot == s].price_cents.iloc[-1]),
            fri_share=float(fri_units / wd_tot) if wd_tot else 0.0,
            zero_week_share=float((wk_units == 0).mean()) if len(wk_units) else None,
            cv_weekly=float(wk_units.std(ddof=0) / wk_units.mean()) if wk_units.mean() > 0 else None,
            empty_windows=sum(1 for w in windows if w["slot"] == s),
            empty_now=bool(empty_day.iloc[-1][s]),
            last_sale=last_sale.isoformat(timespec="minutes") if last_sale is not None else None,
            days_since_last_sale=rnd((T1 - last_sale).total_seconds() / 86400, 1) if last_sale is not None else None,
            share_clock_eligible=share[s]["eligible"]))
    SL = pd.DataFrame(slot_rows).set_index("slot")

    # price change before/after: the day with the most changes among our slots, same number of in-stock class days each side
    chg_days = pcs.assign(day=pcs.t.dt.date).groupby("day").slot.nunique()        # slots repriced per day
    pc_event_day = chg_days.idxmax() if len(chg_days) else None
    n_event = int(chg_days.max()) if len(chg_days) else 0
    before_days = [d for d in reg_class if pc_event_day is not None and d < pc_event_day]
    after_days = [d for d in reg_class if pc_event_day is not None and d > pc_event_day][:len(before_days)]

    def rate_win(s, days_):
        u, e = rate_over(s, days_)
        return int(u), float(e)
    # trend adjustment = the machine-wide total (an allowed aggregate trend) over the same class days, never other slots
    mu_b, mu_a = float(D.loc[before_days, "units"].sum()), float(D.loc[after_days, "units"].sum())
    mach_rr = (mu_a / len(after_days)) / (mu_b / len(before_days)) if mu_b > 0 and after_days and before_days else None
    allu_b = sum(rate_win(s, before_days)[0] for s in slots); alle_b = sum(rate_win(s, before_days)[1] for s in slots)
    allu_a = sum(rate_win(s, after_days)[0] for s in slots); alle_a = sum(rate_win(s, after_days)[1] for s in slots)
    pc_rows, side_units = [], []
    for r in pcs.itertuples():
        s = r.slot
        rec = dict(slot=s, row=int(rowcol.loc[s, "row"]), col=int(rowcol.loc[s, "col"]), when=r.t.isoformat(timespec="minutes"),
                   old=int(r.old), new=int(r.price_cents), pct_price=rnd((r.price_cents - r.old) / r.old * 100, 1))
        if r.t.date() == pc_event_day:
            ub, eb = rate_win(s, before_days)
            ua, ea = rate_win(s, after_days)
            side_units += [ub, ua]
            rb = ub / eb if eb > 0 else None
            ra = ua / ea if ea > 0 else None
            rec.update(units_before=ub, instock_days_before=rnd(eb, 1), units_after=ua, instock_days_after=rnd(ea, 1),
                       rate_before=rnd(rb, 3), rate_after=rnd(ra, 3))
            if ub > 0 and ua > 0 and eb > 0 and ea > 0:
                rr = ra / rb
                se = math.sqrt(1 / ub + 1 / ua)
                rec.update(rate_ratio=rnd(rr, 3), rr_ci_lo=rnd(rr * math.exp(-1.96 * se), 3), rr_ci_hi=rnd(rr * math.exp(1.96 * se), 3),
                           rr_vs_machine_trend=rnd(rr / mach_rr, 3) if mach_rr else None,
                           arc_elasticity=rnd(((ra - rb) / ((ra + rb) / 2)) / ((r.price_cents - r.old) / ((r.price_cents + r.old) / 2)), 2))
            rec["flags"] = [f for f in [
                "few units (<5 either side): treat as noise" if min(ub, ua) < 5 else None,
                "in-stock days differ (stock-outs) - rates per in-stock day used" if abs(eb - ea) > 1 else None,
                "demand was falling over the term (trend); compare with rr_vs_machine_trend"] if f]
        else:
            cd_ = r.t.date()
            day_kind = (D.loc[cd_, "label"] or ("weekend" if cd_.weekday() >= 5 else "class day" if D.loc[cd_, "class_day"] else "no-class weekday")) \
                if cd_ in D.index else "outside the data"
            gap_txt = ", inside a polling gap" if cd_ in D.index and D.loc[cd_, "gap_day"] else ""
            rec["flags"] = [f"changed {cd_:%a} {md(cd_)} ({day_kind}{gap_txt}), apart from the {md(pc_event_day)} reprice: "
                            "no matched before/after class-day windows - not measured"]
        pc_rows.append(rec)
    units_side_text = (f"{min(side_units)}-{max(side_units)} units per side" if side_units else "no measured changes")
    price_changes = dict(
        event_date=ds(pc_event_day) if pc_event_day else None, changed_on_event=n_event, before_days=[ds(d) for d in before_days],
        after_days=[ds(d) for d in after_days], machine_trend_ratio=rnd(mach_rr, 3),
        machine_units_per_class_day_before=rnd(mu_b / len(before_days), 2) if before_days else None,
        machine_units_per_class_day_after=rnd(mu_a / len(after_days), 2) if after_days else None,
        ours_rate_before=rnd(allu_b / alle_b, 3) if alle_b else None, ours_rate_after=rnd(allu_a / alle_a, 3) if alle_a else None,
        units_per_side=units_side_text,
        note="Our slots only. Rates are units per in-stock class day (suspected-empty share of daytime hours removed), "
             + (f"{len(before_days)} class days before {pc_event_day:%a} {md(pc_event_day)} vs the next {len(after_days)} class days after it "
                "(no-class days skipped). " if pc_event_day else "")
             + "rate_ratio > 1 = sold faster after. rr_vs_machine_trend divides by the machine-wide trend: the ratio of the whole "
               "machine's units per class day over the same days (a total, no per-slot values; not stock-adjusted). No slot is "
               "compared with another slot. Price, position and product are confounded and counts are small: read these as hints, "
               "not elasticities.",
        changes=pc_rows)

    # row / column regression (our slots: shelves 1-5, columns 5-8)
    rcdf = SL.reset_index()[["slot", "row", "col", "rate", "avg_price", "instock_class_days"]].copy()
    rcdf = rcdf[rcdf.instock_class_days >= 3]
    m_rc = smf.ols("rate ~ C(row) + C(col) + avg_price", data=rcdf).fit()
    m_rc2 = smf.ols("rate ~ C(row) + avg_price", data=rcdf).fit()
    lv_r = sorted(rcdf.row.unique().tolist())
    lv_c = sorted(rcdf.col.unique().tolist())

    def coef_table(m):
        ci = m.conf_int()
        return [dict(term=t.replace("C(row)[T.", "row ").replace("C(col)[T.", "col ").replace("]", ""),
                     coef=rnd(m.params[t], 3), se=rnd(m.bse[t], 3), ci_lo=rnd(ci.loc[t, 0], 3), ci_hi=rnd(ci.loc[t, 1], 3),
                     p=rnd(m.pvalues[t], 4)) for t in m.params.index]
    rowcol_reg = dict(
        note=f"OLS, one row per slot (n={int(m_rc.nobs)} of our slots with >= 3 in-stock class days). y = units per in-stock class day "
             f"(regular era). Shelf {lv_r[0]} and column {lv_c[0]} are the baselines; avg_price in dollars. Small n, and price, shelf and "
             "product are confounded: associations only.",
        full=dict(formula="rate ~ C(row) + C(col) + avg_price", r2=rnd(m_rc.rsquared, 3), adj_r2=rnd(m_rc.rsquared_adj, 3), n=int(m_rc.nobs), coefs=coef_table(m_rc)),
        reduced=dict(formula="rate ~ C(row) + avg_price (shelf only)", r2=rnd(m_rc2.rsquared, 3), adj_r2=rnd(m_rc2.rsquared_adj, 3), n=int(m_rc2.nobs), coefs=coef_table(m_rc2)),
        row_units={str(k): int(v) for k, v in SL.groupby("row").units.sum().items()},
        col_units={str(k): int(v) for k, v in SL.groupby("col").units.sum().items()},
        row_rate={str(k): float(v) for k, v in SL.groupby("row").rate.sum().round(2).items()},
        col_rate={str(k): float(v) for k, v in SL.groupby("col").rate.sum().round(2).items()},
        rows=lv_r, cols=lv_c)

    def contrasts(kind, levels):
        """Pairwise level differences (a - b) from m_rc with 95% CI, plus Bonferroni-adjusted CI over all pairs."""
        names = list(m_rc.params.index)
        npair = len(levels) * (len(levels) - 1) // 2
        zb = stats.t.ppf(1 - 0.025 / npair, m_rc.df_resid)
        out = []
        for i, a in enumerate(levels):
            for b in levels[i + 1:]:
                L = np.zeros(len(names))
                if a != levels[0]:
                    L[names.index(f"C({kind})[T.{a}]")] += 1
                if b != levels[0]:
                    L[names.index(f"C({kind})[T.{b}]")] -= 1
                t = m_rc.t_test(L)
                est, se = float(np.squeeze(t.effect)), float(np.squeeze(t.sd))
                ci = np.squeeze(t.conf_int())
                out.append(dict(a=int(a), b=int(b), diff=rnd(est, 3), se=rnd(se, 3), ci_lo=rnd(ci[0], 3), ci_hi=rnd(ci[1], 3),
                                p=rnd(float(np.squeeze(t.pvalue)), 4), bonf_lo=rnd(est - zb * se, 3), bonf_hi=rnd(est + zb * se, 3),
                                p_bonf=rnd(min(1.0, float(np.squeeze(t.pvalue)) * npair), 4)))
        return out

    def joint(kind):
        terms = [t for t in m_rc.params.index if t.startswith(f"C({kind})")]
        R = np.zeros((len(terms), len(m_rc.params)))
        for i, t in enumerate(terms):
            R[i, list(m_rc.params.index).index(t)] = 1
        f = m_rc.f_test(R)
        return dict(F=rnd(float(np.squeeze(f.fvalue)), 3), p=rnd(float(np.squeeze(f.pvalue)), 4), df_num=int(f.df_num), df_den=int(f.df_denom))
    rowcol_reg["contrasts"] = dict(note="Pairwise differences a - b in units per in-stock class day from the full model (price held fixed). "
                                        "bonf_lo/bonf_hi: Bonferroni-adjusted 95% CI over all pairs of that kind; p_bonf likewise.",
                                   row=contrasts("row", lv_r), col=contrasts("col", lv_c))
    rowcol_reg["joint"] = dict(row=joint("row"), col=joint("col"))

    # k-means slot segments (our slots)
    feats = ["rate", "avg_price", "fri_share", "zero_week_share", "cv_weekly"]
    F = SL[feats].copy()
    F["cv_weekly"] = F.cv_weekly.fillna(F.cv_weekly.max())          # never sold in regular weeks -> most erratic
    F["zero_week_share"] = F.zero_week_share.fillna(1.0)
    Z = StandardScaler().fit_transform(F.values)
    inertia1 = float(((Z - Z.mean(0)) ** 2).sum())
    f_mean, f_std = F.mean(), F.std(ddof=0).replace(0, 1)
    traits = {"avg_price": ("premium-priced", "low-priced"), "zero_week_share": ("often silent", "sells every week"),
              "fri_share": ("Friday-heavy", "few Friday sales"), "cv_weekly": ("erratic", "steady")}

    def name_cluster(c):
        """Plain name from the centroid's z-scores (rate tier + strongest other trait) and one action."""
        z = {k: (c[k] - f_mean[k]) / f_std[k] for k in feats}
        tier = "Fast seller" if z["rate"] > 0.5 else "Slow seller" if z["rate"] < -0.5 else "Mid seller"
        k2 = max(traits, key=lambda k: abs(z[k]))
        nm = tier + (f", {traits[k2][0] if z[k2] > 0 else traits[k2][1]}" if abs(z[k2]) > 0.5 else "")
        if tier == "Fast seller" and z["zero_week_share"] > 0:
            act = "Restock deeper / check at every visit (likely selling out)"
        elif tier == "Fast seller":
            act = "Hold price, keep full"
        elif tier == "Slow seller" and z["avg_price"] > 0.5:
            act = "Test a lower price"
        elif tier == "Slow seller" and z["zero_week_share"] > 0.5:
            act = "Confirm it is stocked, then swap if still silent for a stocked week"
        elif tier == "Slow seller":
            act = "Swap candidate if it stays slow while stocked"
        elif z["avg_price"] > 0.5:
            act = "Hold; small price test"
        else:
            act = "Hold; watch the slow-seller rule"
        return nm, act

    clusters = dict(features=feats, feature_notes=dict(
        rate="units per in-stock class day (regular era)", avg_price="average price paid on regular-era class days ($; price in effect at each sale)",
        fri_share="share of the slot's weekday units sold on Fridays", zero_week_share="share of full regular-era weeks with 0 units",
        cv_weekly="coefficient of variation of weekly units (std/mean); slots with no regular-era sales set to the max"),
        method=f"Our {len(slots)} slots. Standardized features (z-scores), KMeans n_init=50, random_state={SEED}. Elbow = % of the k=1 "
               "variation explained; silhouette in [-1, 1].",
        k1_inertia=rnd(inertia1, 2), by_k={})
    best_k, best_sil = None, -2
    for k in range(2, 7):
        km = KMeans(n_clusters=k, n_init=50, random_state=SEED).fit(Z)
        sil = float(silhouette_score(Z, km.labels_))
        same = []
        for sd_ in (1, 2, 3, 4, 5):
            l2 = KMeans(n_clusters=k, n_init=50, random_state=sd_).fit(Z).labels_
            same.append(float(((km.labels_[:, None] == km.labels_[None, :]) == (l2[:, None] == l2[None, :])).mean()))
        cents = pd.DataFrame(StandardScaler().fit(F.values).inverse_transform(km.cluster_centers_), columns=feats)
        order = list(cents.sort_values("rate", ascending=False).index)
        remap = {old: new for new, old in enumerate(order)}
        labels = [remap[lb] for lb in km.labels_]
        profiles = []
        for new, old in enumerate(order):
            c = cents.loc[old].to_dict()
            members = [s for s, lb in zip(F.index, labels) if lb == new]
            nm, act = name_cluster(c)
            profiles.append(dict(cluster=new, name=nm, action=act, size=len(members), slots=members,
                                 centroid={k_: rnd(v, 3) for k_, v in c.items()},
                                 units_total=int(SL.loc[members, "units"].sum())))
        clusters["by_k"][str(k)] = dict(inertia=rnd(km.inertia_, 2), pct_explained=rnd(1 - km.inertia_ / inertia1, 4),
                                        silhouette=rnd(sil, 4), stability_pair_agreement=rnd(np.mean(same), 4),
                                        assignments=dict(zip(F.index, labels)), profiles=profiles)
        if sil > best_sil:
            best_k, best_sil = k, sil
    clusters["best_k_by_silhouette"] = best_k
    clusters["slot_features"] = {s: {k_: rnd(F.loc[s, k_], 3) for k_ in feats} for s in F.index}

    # ============================================================ 6. SLOW-SELLER RULE SIMULATION (our slots)
    obs_class = (D.class_day & ~D.gap_day)
    # Decision points = EVERY door visit (the first one each day), from the first Saturday-start week fully in the regular
    # era.  Whether a visit qualifies is known when the door opens (the counter rose); its refill grade is NOT used, because
    # that grade rests on sales after the visit.  The decision uses whole days before the visit day and is made as the door
    # opens.  No future team visit is in the data, so the last point is a PLANNING point: the next Friday.
    dp, seen_d = [], set()
    for v in vis:
        dv = v["first_rise"].date()
        if first_test <= dv <= d1 and dv not in seen_d:
            seen_d.add(dv)
            dp.append((dv, v))
    next_plan = d1 + dt.timedelta(days=(4 - d1.weekday()) % 7 or 7)
    dec_dates = [d for d, _ in dp] + [d1 + dt.timedelta(days=1)]
    dec_labels = [ds(d) for d in dec_dates[:-1]] + [f"{ds(dec_dates[-1])} (now: data to {ds(d1)})"]
    dec_display = ([f"{DOW[d.weekday()]} {ds(d)} visit {v['first_rise']:%H:%M} ({v['evidence']})" for d, v in dp]
                   + [f"Fri {ds(next_plan)} planning point (no team visit scheduled in the data; data to {T1:%Y-%m-%d %H:%M})"])
    dec_kinds = ["visit"] * len(dp) + ["planning point"]
    dec_times = [v["start"] for _, v in dp] + [T1]
    dec_visit_ids = [v["id"] for _, v in dp] + [None]
    N_LIST, A_LIST = (3, 5, 7, 10), (0.01, 0.05, 0.10)
    MIN_SCORED = 5                     # flags with a known outcome needed before a rule's accuracy is judged

    # The "suspected empty" check at a decision uses ONLY what was known when the door opened: the slot's share from sales
    # reported up to then, its silences up to then, and stock-out windows that end at door visits up to then (an ongoing
    # silence runs to the decision).  Nothing after the decision -- in particular not the slot's next sale -- shapes it.
    asof = []
    for t_dec in dec_times:
        sh_ = shares(t_dec)
        win_, _ = stock_windows(t_dec, sh_)
        ef_ = empty_frac_of(win_)
        asof.append((sh_, ef_, ef_ >= EMPTY_DAY_SHARE))

    flags = []
    for di, (D_, lab, t_dec) in enumerate(zip(dec_dates, dec_labels, dec_times)):
        _, ef_d, ed_d = asof[di]
        prior = [d for d in dates if d < D_]
        prior_obs = [d for d in prior if obs_class[d]]
        future_known = t_dec + pd.Timedelta(days=7) <= T1
        for s in slots:
            u = SD[s]
            e = ed_d[s]
            hit = {}
            last7 = [D_ - dt.timedelta(days=i) for i in range(1, 8)]
            if last7[-1] >= d0:
                hit["a_7cal"] = u.loc[last7].sum() == 0
            for N in N_LIST:
                if len(prior_obs) >= N:
                    lastN = prior_obs[-N:]
                    z = u.loc[lastN].sum() == 0
                    hit[f"b_{N}cd"] = z
                    hit[f"c_{N}cd_notempty"] = bool(z and not e.loc[lastN].any())
            # Poisson surprise: rate per in-stock observed class day from history, exposure = observed class days since last sale
            ins = [d for d in prior_obs if d >= REGULAR_START.date() - dt.timedelta(days=12)]
            expo_days = (1 - ef_d.loc[ins, s]).sum() if ins else 0
            rate = u.loc[ins].sum() / expo_days if expo_days >= 5 else None
            sold_days = [d for d in prior if u.loc[d] > 0]
            since = [d for d in prior_obs if (not sold_days or d > sold_days[-1])]
            lam = rate * len(since) if rate is not None else None
            p0 = math.exp(-lam) if lam is not None else None
            for a in A_LIST:
                if p0 is not None:
                    hit[f"d_p{int(a * 100):02d}"] = p0 < a
                    hit[f"d_p{int(a * 100):02d}_notempty"] = bool(p0 < a and not e.loc[since].any()) if since else False
            if not any(hit.values()):
                continue
            # outcome over the 7 days after the decision (event level, from the moment the door opened)
            # the window runs through the poll at the same clock time 7 days later: poll stamps jitter by under a second
            # (hh:59:59.99 vs hh:00:00.00), so the end gets one minute of slack
            nx_ev = [e_ for e_ in sale_list[s] if t_dec < e_.t <= t_dec + pd.Timedelta(days=7, minutes=1)]
            nx_units = int(sum(e_.units for e_ in nx_ev))
            nx14 = int(sum(e_.units for e_ in sale_list[s] if t_dec < e_.t <= t_dec + pd.Timedelta(days=14, minutes=1)))
            refilled = None
            if nx_ev:
                # a refill in between: a visit from this one (the restocker may refill it now) up to the first sale that
                # counts as a refill of this slot without using this slot's own silence test (outcome_refill)
                refilled = [v["id"] for v in vis if t_dec <= v["end"] <= nx_ev[0].t and s in v["outcome_refill_slots"]]
            outcome = ("unknown (future)" if not future_known else
                       "silent the next week (dead, or still not refilled)" if not nx_units else
                       "sold again after a refill (was probably empty)" if refilled else
                       "contradicted: sold again with no refill in between (alive: a slow seller or a lull)")
            for rule, h in hit.items():
                if h:
                    flags.append(dict(rule=rule, decision=lab, slot=s, empty_at_decision=bool(e.loc[prior[-1]]),
                                      p0=rnd(p0, 4), expected=rnd(lam, 2), class_days_silent=len(since),
                                      next7_units=nx_units if future_known else None, next14_units=nx14 if future_known else None,
                                      refill_between=refilled or None, outcome=outcome))
    FL = pd.DataFrame(flags)
    rule_order = ["a_7cal"] + [f"b_{N}cd" for N in N_LIST] + [f"c_{N}cd_notempty" for N in N_LIST] + \
                 [f"d_p{int(a * 100):02d}" for a in A_LIST] + [f"d_p{int(a * 100):02d}_notempty" for a in A_LIST]
    rule_desc = {"a_7cal": "Zero sales in the last 7 calendar days"}
    for N in N_LIST:
        rule_desc[f"b_{N}cd"] = f"Zero sales in the last {N} observed class days"
        rule_desc[f"c_{N}cd_notempty"] = f"Zero sales in the last {N} observed class days AND not suspected empty (as of the decision) on any of them"
    for a in A_LIST:
        rule_desc[f"d_p{int(a * 100):02d}"] = f"Poisson surprise: P(0 sales | own in-stock class-day rate x silent class days) < {a}"
        rule_desc[f"d_p{int(a * 100):02d}_notempty"] = f"Poisson surprise < {a} AND not suspected empty (as of the decision) during the silence"
    dec_of = dict(zip(dec_labels, dec_dates))

    def next14_unique(known):
        """Units sold in the 14 days after each flag, counting each (slot, day) once: a slot flagged at consecutive
        decisions has overlapping windows.  Also returns how many flags have a window cut short by the end of data."""
        seen, tot, partial = set(), 0, 0
        for f in known.itertuples():
            D_ = dec_of[f.decision]
            if D_ + dt.timedelta(days=13) > d1:
                partial += 1
            for i in range(14):
                d = D_ + dt.timedelta(days=i)
                if d <= d1 and (f.slot, d) not in seen:
                    seen.add((f.slot, d))
                    tot += int(SD.loc[d, f.slot])
        return tot, partial

    rule_summary = []
    for r in rule_order:
        g = FL[FL.rule == r] if len(FL) else FL
        known = g[g.next7_units.notna()] if len(g) else g
        contra = int(known.outcome.str.startswith("contradicted").sum()) if len(known) else 0
        refl = int(known.outcome.str.startswith("sold again after a refill").sum()) if len(known) else 0
        n14u, n14p = next14_unique(known) if len(known) else (0, 0)
        rule_summary.append(dict(
            rule=r, description=rule_desc[r], flags=int(len(g)), flags_with_outcome=int(len(known)),
            contradicted=contra, contradicted_share=rnd(contra / len(known), 3) if len(known) else None,
            sold_after_refill=refl, sold_after_refill_share=rnd(refl / len(known), 3) if len(known) else None,
            silent_next_week=int(len(known) - contra - refl), flagged_while_suspected_empty=int(g.empty_at_decision.sum()) if len(g) else 0,
            units_sold_next_14d_by_flagged=n14u, next_14d_partial_windows=n14p,
            flagged_now=sorted(g[g.decision == dec_labels[-1]].slot.tolist()) if len(g) else []))
    rs = {r["rule"]: r for r in rule_summary}

    # Verdict on the empty check, computed: compare each base rule with its '+ not suspected empty' version on the flags
    # with an outcome.  "More accurate" needs a lower share of live slots misjudged (contradicted) among the flags it keeps,
    # no higher share that sold again at all, AND the difference must pass a test: the flags the check keeps vs the flags it
    # drops (disjoint sets), one-sided Fisher exact on live vs not, p < ACC_P.  Otherwise it is "only quieter".
    ACC_P = 0.05
    FLK = FL[FL.next7_units.notna()] if len(FL) else FL

    def fisher_kept_vs_dropped(base, filt):
        if not len(FLK):
            return None, 0, 0
        kb, kf = FLK[FLK.rule == base], FLK[FLK.rule == filt]
        kept_k = set(zip(kf.decision, kf.slot))
        dropped = kb[[(d_, s_) not in kept_k for d_, s_ in zip(kb.decision, kb.slot)]]
        lk = int(kf.outcome.str.startswith("contradicted").sum())
        ld = int(dropped.outcome.str.startswith("contradicted").sum())
        if not len(kf) or not len(dropped):
            return None, len(kf), len(dropped)
        _, p_ = stats.fisher_exact([[lk, len(kf) - lk], [ld, len(dropped) - ld]], alternative="less")
        return float(p_), len(kf), len(dropped)

    def verdict(base, filt):
        b, f = rs[base], rs[filt]
        nb, nf = b["flags_with_outcome"], f["flags_with_outcome"]
        if nb == 0 or nf == 0:
            return dict(base=base, filtered=filt, verdict=(f"only quieter: {b['flags']} -> {f['flags']} flags, and none left with an outcome, "
                                                           "so no gain in accuracy can be shown"), base_known=nb, filtered_known=nf)
        cb, cf = b["contradicted"] / nb, f["contradicted"] / nf
        sb, sf = (b["contradicted"] + b["sold_after_refill"]) / nb, (f["contradicted"] + f["sold_after_refill"]) / nf
        p_, n_kept, n_drop = fisher_kept_vs_dropped(base, filt)
        p_txt = f"Fisher p = {p_:.2f}, kept {n_kept} vs dropped {n_drop}" if p_ is not None else "no test possible"
        if nf < MIN_SCORED:
            vtxt = (f"only quieter, as far as the data can show: {nf} flag{'s' if nf != 1 else ''} left with an outcome "
                    f"({f['contradicted']} of them a live slot), too few to show a gain in accuracy")
        elif cf < cb and sf <= sb and p_ is not None and p_ < ACC_P:
            vtxt = f"more accurate: a lower share of its flags were live slots, no more of them sold again, and the drop passes a test ({p_txt})"
        elif cf < cb and sf <= sb:
            vtxt = f"only quieter: no detectable difference in accuracy (a lower live-slot share, but {p_txt}; needs p < {ACC_P})"
        elif cf <= cb + 1e-9 and sf <= sb + 1e-9:
            vtxt = "only quieter: fewer flags, the same shares"
        elif cf > cb:
            vtxt = "quieter but less accurate: a higher share of the flags it keeps were live slots"
        else:
            vtxt = "quieter, mixed: fewer live slots misjudged, but more of its flags sold again after a refill"
        return dict(base=base, filtered=filt, base_flags=b["flags"], filtered_flags=f["flags"], base_known=nb, filtered_known=nf,
                    base_contradicted_share=rnd(cb, 3), filtered_contradicted_share=rnd(cf, 3),
                    base_sold_again_share=rnd(sb, 3), filtered_sold_again_share=rnd(sf, 3),
                    fisher_p_kept_vs_dropped=rnd(p_, 4) if p_ is not None else None, verdict=vtxt)
    rule_verdicts = [verdict(f"b_{N}cd", f"c_{N}cd_notempty") for N in N_LIST] + \
                    [verdict(f"d_p{int(a * 100):02d}", f"d_p{int(a * 100):02d}_notempty") for a in A_LIST]
    # as-of empty state per decision, for the labs' replay: e = 1 if suspected empty that day, eh = empty daytime hours (0-9),
    # one character per day before the decision day
    asof_out = []
    for di, D_ in enumerate(dec_dates):
        _, ef_d, ed_d = asof[di]
        n_ = sum(1 for d in dates if d < D_)
        asof_out.append({s: dict(e="".join("1" if ed_d.iloc[i][s] else "0" for i in range(n_)),
                                 eh="".join(str(int(round(ef_d.iloc[i][s] * (DAY_H1 - DAY_H0)))) for i in range(n_)))
                         for s in slots})
    rule_sim = dict(
        decision_points=dec_labels,
        decision_display=dec_display,
        decision_kinds=dec_kinds, decision_visit_ids=dec_visit_ids,
        decision_note=("Decision points are every actual door visit (door counter, first visit each day), not assumed weekdays, and "
                       "they do not depend on later sales: a visit's refill grade is not used to pick them. "
                       f"The last point is a planning point (Fri {ds(next_plan)}): no team visit is scheduled in the data."
                       if doors is not None else "No door data: decision points are assumed Friday visits."),
        definitions=dict(observed_class_day=f"calendar class day (Mon-Fri, classes normal) with < {GAP_DAY_HOURS} unobserved daytime hours",
                         empty=(f"slot suspected empty for >= {int(EMPTY_DAY_SHARE * 100)}% of 08:00-17:00 that day, AS OF THE DECISION: "
                                "only sales and door visits up to the moment the door opened; stock-out windows end at door visits, never "
                                "at the slot's next sale"),
                         contradicted=("flagged slot sold again within 7 days of the decision WITHOUT a refill in between: no visit from the "
                                       "decision visit up to that sale that counts as a refill of the slot (a capacity proof for it, or a visit "
                                       "graded as a proven or likely restock from the door counter plus our slots, not counting the slot's own "
                                       "silence). The slot was alive: the flag misjudged a slow seller or a lull"),
                         sold_after_refill=("flagged slot sold again within 7 days, but only after such a refill (starting with the decision visit "
                                            "itself): it was probably empty, which the flag cannot tell from dead"),
                         refill_rule_note=("A slot's own 'resumed after a visit' status is NOT used to score its outcome: it is defined by an "
                                           "improbable silence, the quantity the Poisson rule flags on, so it would score that rule by its own definition."),
                         poisson="rate = units / in-stock observed class days since Sep 9 (needs >= 5); expected = rate x observed class days since the last sale; flag if exp(-expected) < alpha"),
        summary=rule_summary, verdicts=rule_verdicts, asof=asof_out,
        flags=dict(columns=["rule", "decision_index", "slot", "empty_at_decision", "p0", "expected", "class_days_silent",
                            "next7_units", "next14_units", "outcome_code"],
                   outcome_codes={"0": "unknown (future)", "1": "contradicted: sold again with no refill in between (alive)",
                                  "2": "silent the next week (dead, or still not refilled)",
                                  "3": "sold again after a refill (was probably empty)"},
                   rows=[[f["rule"], dec_labels.index(f["decision"]), f["slot"], int(f["empty_at_decision"]), f["p0"], f["expected"],
                          f["class_days_silent"], f["next7_units"], f["next14_units"],
                          0 if f["next7_units"] is None else (1 if f["outcome"].startswith("contradicted") else
                                                               3 if f["outcome"].startswith("sold again after") else 2)] for f in flags]))

    slot_daily = dict(
        note="Our 20 slots only. Per slot-day arrays aligned to daily.date. u = units (midpoint timing, lumps on their poll day), "
             "e = 1 if suspected empty for >= half of 08:00-17:00 (whole-history view: windows end at door visits), "
             "p = listed price in cents at 12:00 noon ET that day.",
        dates=[ds(d) for d in dates],
        slots={s: dict(u=SD[s].astype(int).tolist(), e=empty_day[s].astype(int).tolist(),
                       p=price_day[s].astype(int).tolist()) for s in slots})

    # ============================================================ 7. PRODUCTS (photos x our slots' sales)
    # Brand keyword -> (brand, category, caffeine).  First match wins, so specific names come before general ones.
    BRANDS = [("diet coke", "Diet Coke", "soda", True), ("coke zero", "Coke Zero", "soda", True), ("black-label cola", "Coke Zero", "soda", True),
              ("coca-cola", "Coca-Cola", "soda", True), ("pepsi", "Pepsi", "soda", True),
              ("monster", "Monster Energy", "energy", True), ("gorgie", "Gorgie", "energy", True), ("ghost", "Ghost Energy", "energy", True),
              ("red bull", "Red Bull", "energy", True), ("celsius", "Celsius", "energy", True),
              ("frappuccino", "Starbucks", "coffee", True), ("starbucks", "Starbucks", "coffee", True),
              ("pure leaf", "Pure Leaf", "tea", True), ("arizona", "AriZona", "tea", True),
              ("snapple", "Snapple", "juice", None), ("vitaminwater", "vitaminwater", "water", None), ("gatorade", "Gatorade", "sports", False),
              ("bubly", "bubly", "sparkling water", False), ("pellegrino", "S.Pellegrino", "sparkling water", False),
              ("something & nothing", "Something & Nothing", "sparkling water", False), ("recess", "Recess", "sparkling water", False),
              ("premier protein", "Premier Protein", "protein", False), ("protein", "Premier Protein", "protein", False),
              ("vita coco", "Vita Coco", "water", False), ("pickle", "pickle-juice sports drink", "sports", False),
              ("bottled water", "bottled water", "water", False)]
    CAT_NORMAL = {"functional sparkling": "sparkling water", "juice/lemonade": "juice", "other": "unknown", None: "unknown"}

    def classify(product, category=None, caffeine=None):
        if not product:
            return None, None, None
        low = product.lower()
        for kw, brand, cat, caf in BRANDS:
            if kw in low:
                return brand, cat, caf if caf is not None else caffeine
        return f"unidentified ({CAT_NORMAL.get(category, category)})", CAT_NORMAL.get(category, category), caffeine

    def ts(x):
        try:
            return pd.Timestamp(x).tz_convert(TZ)
        except Exception:
            return None
    products = None
    if photos is not None:
        snaps = []
        for ph in sorted(photos["photos"], key=lambda p: p["chronological_rank"]):
            if ph.get("file") and oct8 is not None:            # the Oct 8 reference, read slot by slot in its own file
                at = ts(oct8["photo_taken"])
                recs = {s["slot"]: s for s in oct8["slots"] if s["slot"] in slots and s.get("ours", True)}
                meta = dict(earliest=at.isoformat(timespec="minutes"), latest=at.isoformat(timespec="minutes"), confidence="dated photo")
            elif "stocked" in ph:                                # the Nov 9 reference: stocked rails named, the rest empty
                at = ts(ph["taken"])
                recs = {s: (dict(slot=s, product=ph["stocked"][s], stock_seen="stocked", confidence="medium") if s in ph["stocked"]
                            else dict(slot=s, product=None, stock_seen="empty")) for s in slots}
                meta = dict(earliest=at.isoformat(timespec="minutes"), latest=at.isoformat(timespec="minutes"), confidence="dated photo")
            elif "window" in ph:
                at = ts(ph["window"]["best_guess"])
                recs = {s["slot"]: s for s in ph.get("slots", []) if s["slot"] in slots and s.get("ours", True)}
                meta = dict(earliest=ph["window"]["earliest"], latest=ph["window"]["latest"], confidence=ph.get("confidence"))
            else:
                continue
            snaps.append(dict(id=ph["id"], at=at, recs=recs, **meta))
        timeline = {}
        for s in slots:
            tl = []
            for sn in snaps:
                r = sn["recs"].get(s)
                if r is None:
                    continue
                stocked = r.get("stock_seen") == "stocked" and bool(r.get("product"))
                brand, cat, caf = classify(r.get("product"), r.get("category"), r.get("caffeine")) if stocked else (None, None, None)
                tl.append(dict(photo=sn["id"], at=sn["at"], earliest=sn["earliest"], latest=sn["latest"], photo_confidence=sn["confidence"],
                               product=r.get("product") if stocked else None, brand=brand, category=cat, caffeine=caf,
                               stock_seen="stocked" if stocked else "empty", read_confidence=r.get("confidence")))
            timeline[s] = tl
        # attribute each sale of a slot to a product: the photo before it shows the rail stocked -> that product
        # ("bracketed" when the photo after it shows the same brand); the photo before shows it empty, or there is no
        # earlier photo -> the next photo's product (the rail had to be reloaded before it could sell again)
        att_rows = []
        for s in slots:
            tl = timeline[s]
            for e in sales[sales.slot == s].itertuples():
                prev_ = [x for x in tl if x["at"] <= e.t]
                next_ = [x for x in tl if x["at"] > e.t]
                pv_, nx_ = (prev_[-1] if prev_ else None), (next_[0] if next_ else None)
                if pv_ is not None and pv_["stock_seen"] == "stocked":
                    src, basis = pv_, ("bracketed" if nx_ is not None and nx_["brand"] == pv_["brand"] else "photo before")
                elif nx_ is not None and nx_["stock_seen"] == "stocked":
                    src, basis = nx_, "photo after"
                else:
                    src, basis = None, "unknown"
                att_rows.append(dict(slot=s, t=e.t, units=int(e.units), cents=int(e.cents), basis=basis,
                                     brand=src["brand"] if src else "unknown", category=src["category"] if src else "unknown",
                                     caffeine=src["caffeine"] if src else None, product=src["product"] if src else None,
                                     photo=src["photo"] if src else None))
        AT = pd.DataFrame(att_rows)
        AT["caf"] = AT.caffeine.map({True: "caffeine", False: "no caffeine"}).fillna("unknown")

        def agg(by):
            out = []
            for k, g in AT.groupby(by, sort=False):
                u = int(g.units.sum())
                out.append(dict(key=k, units=u, cents=int(g.cents.sum()), avg_price=rnd(g.cents.sum() / u / 100, 2) if u else None,
                                units_bracketed=int(g[g.basis == "bracketed"].units.sum()),
                                share=rnd(u / TOTAL_UNITS, 3), slots=sorted(g.slot.unique().tolist())))
            return sorted(out, key=lambda r: -r["units"])
        by_brand = agg("brand")
        for r in by_brand:
            g = AT[AT.brand == r["key"]]
            r.update(brand=r.pop("key"), category=g.category.iloc[0], caffeine=g.caf.iloc[0])
        by_category = agg("category")
        for r in by_category:
            r["category"] = r.pop("key")
        by_caffeine = agg("caf")
        for r in by_caffeine:
            r["caffeine"] = r.pop("key")
        # per slot: consecutive runs of the same brand, with the units they sold
        runs = {}
        for s in slots:
            g = AT[AT.slot == s].sort_values("t")
            rs_ = []
            for e in g.itertuples():
                if rs_ and rs_[-1]["brand"] == e.brand:
                    rs_[-1].update(to=e.t.isoformat(timespec="minutes"), units=rs_[-1]["units"] + e.units, cents=rs_[-1]["cents"] + e.cents)
                else:
                    rs_.append(dict(brand=e.brand, category=e.category, caffeine=e.caf, frm=e.t.isoformat(timespec="minutes"),
                                    to=e.t.isoformat(timespec="minutes"), units=int(e.units), cents=int(e.cents)))
            for r in rs_:
                r["avg_price"] = rnd(r["cents"] / r["units"] / 100, 2) if r["units"] else None
            runs[s] = rs_
        products = dict(
            note=("Our 20 slots only. What each slot held comes from the dated team photos (planograms_photos.json, planogram_2026-10-08.json); "
                  "the units and prices come from that slot's own counters. A sale is attributed to the product in the photo before it, or, when "
                  "that photo shows the rail empty (or there is none), to the product in the photo after it. 'bracketed' units sit between two "
                  "photos that show the same brand. Price paid = cents / units from the counters (tax included)."),
            caveats=["Photo dates are windows, not instants: most undated photos were dated from the poll history, and the attribution uses "
                     "the best-guess time, so sales near a photo can land on the wrong side of it.",
                     "Products can change between photos: a rail can be swapped and sold out between two photos without ever being seen, "
                     "so only 'bracketed' units are well pinned; the rest are best guesses.",
                     "Names come from 600x800 photos: many flavours (and some brands) are uncertain, and 'unidentified' rails are grouped by "
                     "the category the reader guessed.",
                     "Caffeine is by brand (energy, coffee, black tea and colas contain it); vitaminwater and Snapple vary by flavour, so they "
                     "take the photo reader's call or 'unknown'."],
            photos=[dict(id=sn["id"], at=sn["at"].isoformat(timespec="minutes"), earliest=sn["earliest"], latest=sn["latest"],
                         confidence=sn["confidence"]) for sn in snaps],
            timeline={s: [dict({k: v for k, v in x.items() if k != "at"}, at=x["at"].isoformat(timespec="minutes")) for x in tl]
                      for s, tl in timeline.items()},
            runs=runs, by_brand=by_brand, by_category=by_category, by_caffeine=by_caffeine,
            basis_units={k: int(v) for k, v in AT.groupby("basis").units.sum().items()},
            units_total=int(AT.units.sum()))

    # ============================================================ checks
    # Counter check, per slot: the lifetime counters in poll_readings.csv, last minus first with restarts and misreads
    # handled as vending.counters.rises does, must equal the slot's event totals in sales_events.csv.
    counter_check = {}
    for s in slots:
        r_ = reads[reads.slot == s].sort_values("t")
        eu, ec = int(sales.loc[sales.slot == s, "units"].sum()), int(sales.loc[sales.slot == s, "cents"].sum())
        if r_.empty:          # a slot with no readings (stale or partial export): a failed check unless it also has no events
            counter_check[s] = dict(units_counter=0, units_events=eu, cents_counter=0, cents_events=ec, last_minus_first_units=0,
                                    last_minus_first_cents=0, restarts=0, misreads=0, no_readings=True, ok=bool(eu == 0 and ec == 0))
            continue
        tu, ru, mu = counter_total(r_.lifetime_units.astype(int).tolist())
        tc, rc, mc = counter_total(r_.lifetime_sales_cents.astype(int).tolist())
        counter_check[s] = dict(units_counter=tu, units_events=eu, cents_counter=tc, cents_events=ec,
                                last_minus_first_units=int(r_.lifetime_units.iloc[-1] - r_.lifetime_units.iloc[0]),
                                last_minus_first_cents=int(r_.lifetime_sales_cents.iloc[-1] - r_.lifetime_sales_cents.iloc[0]),
                                restarts=ru + rc, misreads=mu + mc, ok=bool(tu == eu and tc == ec))
    counters_ok = all(c["ok"] for c in counter_check.values())
    checks = dict(
        units_ours=TOTAL_UNITS, cents_ours=TOTAL_CENTS, machine_units=MACHINE_UNITS, machine_cents=MACHINE_CENTS,
        daily_units_ours_raw_sum=int(D.units_ours_raw.sum()), daily_units_ours_spread_sum=rnd(D.units_ours.sum(), 3),
        daily_cents_ours_raw_sum=int(D.cents_ours_raw.sum()),
        daily_units_raw_sum=int(D.units_raw.sum()), daily_units_spread_sum=rnd(D.units.sum(), 3),
        daily_cents_raw_sum=int(D.cents_raw.sum()), daily_cents_spread_sum=rnd(D.cents.sum(), 1),
        weekly_units_sum=int(weekly_df.units_raw.sum()), weekly_units_ours_sum=int(weekly_df.units_ours_raw.sum()),
        slot_daily_sum=int(SD.values.sum()), slot_units_sum=int(SL.units.sum()), slot_cents_sum=int(SL.cents.sum()),
        product_units_sum=products["units_total"] if products else None,
        counters_ok=counters_ok, counter_slots_checked=len(counter_check),
        counter_restarts=sum(c["restarts"] for c in counter_check.values()), counter_misreads=sum(c["misreads"] for c in counter_check.values()),
        expected_units=TOTAL_UNITS, expected_cents=TOTAL_CENTS,
        suspected_empty_windows=len(windows), improbable_midweek=len(midweek),
        holt_winters_fallbacks=sum(v["fallbacks"] for v in fit_log_all.values()),
        holt_winters_fits=sum(v["fits"] for v in fit_log_all.values()),
        holt_winters_warned=sum(v["warned"] for v in fit_log_all.values()))
    ok = (checks["daily_units_ours_raw_sum"] == TOTAL_UNITS == checks["weekly_units_ours_sum"] == checks["slot_daily_sum"]
          == checks["slot_units_sum"]
          and checks["slot_cents_sum"] == TOTAL_CENTS == checks["daily_cents_ours_raw_sum"]
          and abs(checks["daily_units_ours_spread_sum"] - TOTAL_UNITS) < 0.01
          and checks["daily_units_raw_sum"] == MACHINE_UNITS == checks["weekly_units_sum"]
          and abs(checks["daily_units_spread_sum"] - MACHINE_UNITS) < 0.01 and checks["daily_cents_raw_sum"] == MACHINE_CENTS
          and abs(checks["daily_cents_spread_sum"] - MACHINE_CENTS) < 1
          and (products is None or checks["product_units_sum"] == TOTAL_UNITS)
          and counters_ok)
    checks["all_ok"] = bool(ok)

    # ============================================================ findings (every number computed)
    def ranges(ss):
        """'15-18, 25-28' from a list of slot ids."""
        ss = sorted(ss, key=int)
        out, run = [], [ss[0]] if ss else []
        for s in ss[1:]:
            if int(s) == int(run[-1]) + 1:
                run.append(s)
            else:
                out.append(run)
                run = [s]
        if run:
            out.append(run)
        return ", ".join(r[0] if len(r) == 1 else f"{r[0]}-{r[-1]}" for r in out)
    slots_text = ranges(slots)
    reg_ours = D[D.regular]
    am = float(hour_tot.loc[8:10].sum() / hour_tot.sum())      # hours 8, 9, 10 = 08:00 to 11:00 (.loc is inclusive)
    lumps = int(gaps.units.sum())
    big = gaps.loc[gaps.units.idxmax()] if len(gaps) else None
    fri_raw = int(D[D.dow == 4].units_raw.sum())
    big_txt = ""
    if big is not None and big.units > 0:
        big_txt = f"The largest single lump was {int(big.units)} units, reported at the {big.t:%a %b %d %H:%M} poll after a {big.hours:.0f}-hour gap"
        if big.t.weekday() == 4 and fri_raw:
            big_txt += f": {int(big.units) / fri_raw:.0%} of all {fri_raw} units stamped on a Friday"
        big_txt += ". "
    nc_coef = regA.get("no_class", {})
    wk_coefB0 = {c["term"]: c for c in regression["models"]["B0"]["coefs"]}.get("week_of_term", {})
    wk_coefB = regB.get("week_of_term", {})
    emp_coef = regB.get("empty_slots", {})
    ra, rb5, rc5 = rs.get("a_7cal", {}), rs.get("b_5cd", {}), rs.get("c_5cd_notempty", {})
    rd5, rd5n = rs.get("d_p05", {}), rs.get("d_p05_notempty", {})
    vd = {v_["filtered"]: v_ for v_ in rule_verdicts}

    def rule_outcome(r, min_n=5):
        """Same form for every rule: flags, then of those with a known outcome: sold again with no refill in between
        (contradicted: the slot was alive) and sold again only after a refill (it was probably empty)."""
        n, c, f_ = r.get("flags_with_outcome") or 0, r.get("contradicted") or 0, r.get("sold_after_refill") or 0
        if n < min_n:
            return (f"{r.get('flags')} flags, only {n} with a known outcome yet ({c} sold again with no refill in between), too few to score")
        return (f"{r.get('flags')} flags; of the {n} with a known outcome, {c} ({c / n:.0%}) sold again with no refill in between "
                f"and {f_} ({f_ / n:.0%}) sold again only after a refill")

    def iso(t):
        return t.isoformat(timespec="minutes") if t is not None else None

    def vbrief(v):
        return None if v is None else dict(visit=v["id"], start=iso(v["start"]), end=iso(v["end"]),
                                            weekday=DOW[v["first_rise"].weekday()], evidence=v["evidence"])
    visits_out = [dict(id=v["id"], start=iso(v["start"]), end=iso(v["end"]), first_rise=iso(v["first_rise"]),
                       weekday=DOW[v["first_rise"].weekday()], window_hours=rnd(v["window_h"], 2), openings=v["openings"],
                       rise_polls=[iso(t) for t in v["rise_polls"]], source=v["source"], evidence=v["evidence"],
                       proven_slots=v["proven_slots"], pair_proof_slots=v["pair_proof_slots"],
                       ambiguous_slots_n=len(v["ambiguous_slots"]), resumed_slots=v["resumed_slots"],
                       outcome_refill_slots=v["outcome_refill_slots"],
                       resumed=[dict(slot=r["slot"], silent_days=rnd(r["silent_days"], 1), p0_silence=rnd(r["p0_silence"], 4),
                                     hours_to_first_sale=rnd(r["hours_to_first_sale"], 1), units_within=r["units_within"])
                                for r in v["resumed"]],
                       resumed_but_known_not_refill=v.get("resumed_before_override", []), note=v["note"])
                  for v in vis]
    per_slot_ev = {s: [dict(visit=v["id"], kind="capacity proof" if s in v["proven_slots"] else "resumed") for v in slot_refills[s]]
                   for s in slots}
    refill_evidence = dict(
        method=(f"Visits = door-open counter rises (EA2*DO) merged within {VISIT_MERGE_H} h; window = (previous poll, last rise poll]. "
                f"Evidence uses our {len(slots)} slots only. Capacity proof: no rail holds more than {CAPACITY_MAX} (Package Loading label), so "
                f"any run of one of our slots' sales over {CAPACITY_MAX} units needs a refill between its first and last unit; if exactly one "
                f"visit window overlaps, that visit is a proven refill (several = at least one of them). Resumed: one of our slots silent >= "
                f"{SILENT_DAYS} days with its own clock giving P(0) < {ALPHA_EMPTY} (it looked empty) that sells within {RESUME_DAYS} days after "
                f"the first opening. Likely refill = >= {LIKELY_MIN} resumed slots; possible top-up = 1."),
        capacity_max=CAPACITY_MAX, silent_days=SILENT_DAYS, resume_days=RESUME_DAYS, likely_min=LIKELY_MIN,
        per_visit={str(v["id"]): dict(evidence=v["evidence"], proven_slots=v["proven_slots"], pair_proof_slots=v["pair_proof_slots"],
                                      resumed_slots=v["resumed_slots"], outcome_refill_slots=v["outcome_refill_slots"]) for v in vis},
        per_slot=per_slot_ev,
        proofs=[dict(slot=p["slot"], start=iso(p["start"]), end=iso(p["end"]), units=p["units"], visits=p["visits"], verdict=p["verdict"])
                for p in proofs],
        proof_counts=pd.Series([p["verdict"] for p in proofs]).value_counts().to_dict() if proofs else {},
        counts=pd.Series([v["evidence"] for v in vis]).value_counts().to_dict())
    lpr = dict(
        **(vbrief(last_proven) or {}), slots=last_proven["proven_slots"] if last_proven else [],
        max_units_one_slot_after=max(after_proven.values()) if last_proven else None,
        later_pair=dict(visits=[vbrief(vis_by_id[i]) for i in pair_ids], slots=pair_slots) if pair_ids else None,
        last_restock=vbrief(last_restock),
        max_units_one_slot_after_last_restock=max(after_restock.values()) if last_restock else None,
        later_possible_topups=[vbrief(v) for v in vis if last_restock is not None and v["id"] > last_restock["id"]
                               and v["evidence"] == "possible top-up"],
        statement=None)
    if last_proven is not None:
        st_ = (f"Last refill proven at a single visit: {vwin(last_proven)} "
               f"(capacity proof, our slots {', '.join(last_proven['proven_slots'])}).")
        if pair_ids:
            a_, b_ = vis_by_id[pair_ids[0]], vis_by_id[pair_ids[1]]
            st_ += (f" A later refill is proven to have happened at {vwin(a_)} OR "
                    f"{vwin(b_)} (slots {', '.join(pair_slots)} sold more than "
                    f"{CAPACITY_MAX} across both), but the counters cannot say which.")
        if last_restock is not None:
            st_ += (f" Last restock with proven or likely evidence: {vwin(last_restock)} "
                    f"({last_restock['evidence']}, {len(last_restock['resumed_slots'])} of our silent slots resumed). "
                    f"Since then none of our slots sold more than {max(after_restock.values())} units, so later refills cannot be proven.")
        lpr["statement"] = st_

    reg_vis = [v for v in vis if v["first_rise"] >= REGULAR_START]
    ev_vis = [v for v in reg_vis if v["evidence"] in REFILL_EVIDENCE]
    wd_count = pd.Series([v["first_rise"].weekday() for v in ev_vis], dtype=int).value_counts().reindex(range(5)).fillna(0).astype(int)
    wd_str = ", ".join(f"{DOW[i]} {wd_count[i]}" for i in range(5))
    n_open = int(sum(v["openings"] or 0 for v in vis))
    last3 = [r for r in supply if not r["partial"]][-3:]
    sup_now = supply[-1]
    instock_obs = reg_ours[(reg_ours.dow < 5) & (reg_ours.index >= recent_cut)].units_ours.mean()
    ev_counts = pd.Series([v["evidence"] for v in vis]).value_counts()
    after_lr = [v for v in vis if last_restock is not None and v["id"] > last_restock["id"]]
    pc_ = photo_check or {}
    n_reg_full = len(reg_weeks)
    n_weeks_total = len(dates) / 7

    def wk(w):
        return f"Sat {md(dt.date.fromisoformat(w))}"
    v5, vp = vd.get("c_5cd_notempty", {}), vd.get("d_p05_notempty", {})
    findings = [
        dict(title="Takings and volume, our 20 slots", number=f"{TOTAL_UNITS} drinks / ${TOTAL_CENTS / 100:,.2f}",
             detail=f"Slots {slots_text}, from {T0:%b %d} to {T1:%b %d %H:%M} ET ({(T1 - T0).days} days, {len(P)} polls). Daily, weekly and "
                    f"per-slot totals reconcile with the counters (checked slot by slot). The whole machine sold {MACHINE_UNITS} "
                    "(an aggregate, used only for the calendar, time-of-day, trend and machine-forecast work)."),
        dict(title="Visits are dated from the door counter, not assumed weekdays",
             number=(f"{len(vis)} visits ({n_open} door openings); {len(ev_vis)} regular-era visits with refill evidence on our slots: {wd_str}"
                     if doors is not None else "no door data: visits assumed every Friday 08:00"),
             detail=("The machine counts door openings (EVA-DTS EA2*DO) and reports the count at every poll, so each visit is dated to within "
                     "the poll interval (one hour since Sep 21). Visits were spread over the week (the last six on "
                     + ", ".join(f"{v['first_rise']:%a %b %d}" for v in vis[-6:]) + "). Evidence per visit, from our slots only: "
                     + ", ".join(f"{k} {int(ev_counts.get(k, 0))}" for k in ("proven refill", "likely refill", "possible top-up", "no sign of refill"))
                     + ". The counter says when the door opened, not what was loaded.")),
        dict(title=("Regular refilling stopped after the last proven refill" + (f" ({last_proven['first_rise']:%b} {last_proven['first_rise'].day})" if last_proven is not None else "")),
             number=(f"last single-visit proof {vwin(last_proven)}; {len(after_lp)} later visits refilled some of our rails" if last_proven is not None else "n/a"),
             detail=(lpr["statement"] or "") + f" Later visits with refill evidence on our slots: {later_text}. The operators stopped regular "
                    "refilling to prepare for the project; by our slots' own counters the later visits put product back in some rails, not the "
                    "whole machine." + (
                     " Later door openings: " + "; ".join(f"{v['first_rise']:%a %b %d %H:%M} ({v['evidence']}"
                                                          + (f": {', '.join(v['resumed_slots'])} resumed" if v["resumed_slots"] else "")
                                                          + (f"; {v['note']}" if v["note"] else "") + ")" for v in after_lr) + "."
                     if after_lr else "")),
        dict(title="The recent decline is supply, not fading demand",
             number=(" -> ".join(str(r["units"]) for r in last3) + " units a week from our slots (last three full weeks); "
                     f"{peak_full['active_slots']} -> {last_full['active_slots']} of our slots selling (best full week vs last full week)"),
             detail=(f"Full weeks starting {', '.join(wk(r['week']) for r in last3)}; the best full week for selling slots began {wk(peak_full['week'])}. "
                     "The operators stopped regular refilling to prepare for the project: by the end of the data "
                     f"{sup_now['slots_empty_at_week_end']} of our slots are suspected empty ({sup_now['slots_sold_out_not_refilled']} sold out "
                     f"after the last proven refill and not refilled since, {sup_now['slots_silent_since_before_last_restock']} silent since before it), and the "
                     f"Nov 9 photo shows product on only {len(PHOTO_NOV9['stocked'])} of our {len(slots)} rails. Demand is better read from in-stock "
                     f"rates: if each of our slots were kept stocked, their own rates add up to about {restock_rate_recent:.0f} units per class day "
                     f"(recent) or {restock_rate_all:.0f} (whole era), vs {instock_obs:.1f} observed per weekday in the last 4 weeks.")),
        dict(title="A large part of the early history is lumped by polling gaps (machine-wide)", number=f"{lumps} units ({lumps / MACHINE_UNITS:.0%})",
             detail=f"Before {REGULAR_START:%b %d} polling was occasional; {lumps} of the machine's units landed on the poll that ended a gap. "
                    + big_txt + "Gap days are missing data, not zero sales."),
        dict(title="Weekday shape (seasonal index, machine-wide)", number=" / ".join(f"{DOW[i]} {dow_idx.iloc[i]:.2f}" for i in range(5)),
             detail="Multiplicative day-of-week index (1.00 = average day, weekends near 0). Mean machine units per class day: "
                    + ", ".join(f"{DOW[i]} {dow_mean_class.iloc[i]:.1f}" for i in range(5))
                    + ". Index = ratio of totals to the 7-day moving average, class days and weekends only. "
                    + f"In the calendar regression (model A, the table below) {wd_ci_text} ({wd_ci_detail}). Friday's index is {dow_idx.iloc[4]:.2f}."),
        dict(title="Morning machine", number=f"{am:.0%} of machine units 8 to 11 AM (08:00-10:59); peak hour {peak_hour}:00",
             detail="Regular-era machine-wide sales timed at the middle of each poll interval (a sale stamped at the poll that closed the hour "
                    "belongs to that hour). Classes meet weekday mornings, so the machine sells before and between them."),
        dict(title="Trend: falling, and it tracks the emptying shelves",
             number=(f"{regA['week_of_term']['coef']:+.2f} machine units/day per week (model A, 95% CI {regA['week_of_term']['ci_lo']:+.2f} "
                     f"to {regA['week_of_term']['ci_hi']:+.2f})"),
             detail=f"Weekday-adjusted daily machine units fall each week of term (model A, the table below; "
                    f"p{'<0.001' if regA['week_of_term']['p'] < 0.001 else '=' + str(regA['week_of_term']['p'])}). "
                    f"For our 20 slots, adding the count of our suspected-empty slots (model B vs B0) changes the week-of-term coefficient from "
                    f"{wk_coefB0.get('coef')} to {wk_coefB.get('coef')}; each extra empty slot is worth {emp_coef.get('coef')} units/day "
                    f"(95% CI {emp_coef.get('ci_lo')} to {emp_coef.get('ci_hi')}). Trend and empty slots move together (r={regression['corr_week_vs_empty_slots']}), "
                    "so the regression alone cannot split them; the door counter and the operators' account (regular refilling stopped in late October) "
                    "say the late fall is mostly supply. The empty-slot count is itself an inference."),
        dict(title="Days off matter", number=f"no-class weekday effect {nc_coef.get('coef')} machine units/day (model A, CI {nc_coef.get('ci_lo')} to {nc_coef.get('ci_hi')})",
             detail=f"The no-class weekdays in the clean data ({nc_text}) sold close to nothing. Only {len(nc_days)} such days, so the size is "
                    f"uncertain, but the sign is clear: the days off ahead ({breaks_text}) should be treated like weekends."),
        dict(title="Forecast winner (machine totals)" + (" (a near tie with seasonal naive)" if avg[SN]["mae"] - avg[best1]["mae"] < 0.1 * avg[SN]["mae"] else ""),
             number=f"{best1}: MAE {avg[best1]['mae']:.2f} units/day vs seasonal naive {avg[SN]['mae']:.2f}",
             detail=f"Walk-forward over {len(origins)} Saturday plan dates, 7 days ahead each. Runner-up: {best2} ({avg[best2]['mae']:.2f}). "
                    f"{len(beats_sn)} of {len(model_names) - 1} other models beat seasonal naive. Bias of the winner {avg[best1]['bias']:+.2f}/day "
                    f"({'ran low' if avg[best1]['bias'] > 0 else 'ran high'}). Exponential-smoothing fits: {checks['holt_winters_fits']}, of which "
                    f"{checks['holt_winters_fallbacks']} fell back (Holt-Winters to seasonal naive) and {checks['holt_winters_warned']} raised a warning. "
                    "These models follow the level of an emptying machine; use the restocked scenario for demand with full shelves."),
        dict(title=("Slow-seller rule: the empty check " + ("makes the 5-class-day rule more accurate" if v5.get("verdict", "").startswith("more accurate")
                                                              else "makes the 5-class-day rule quieter, not more accurate")),
             number=(f"5 class days: {rb5.get('flags', 0)} flags -> {rc5.get('flags', 0)} with the empty check ({v5.get('verdict', 'n/a')})"),
             detail=(f"Decision points are all {len(dp)} door visits (plus a planning point), chosen without looking at later sales. The empty check uses only "
                     "what was known when the door opened (stock-out windows end at door visits, not at the slot's next sale), and a slot's own "
                     "'resumed' status no longer decides its outcome. 'Zero for 7 calendar days' raised " + rule_outcome(ra) + ". "
                     "'5 class days' raised " + rule_outcome(rb5) + "; with the empty check, " + rule_outcome(rc5) + ". "
                     "Poisson at 5%: " + rule_outcome(rd5) + "; with the empty check, " + rule_outcome(rd5n) + f" ({vp.get('verdict', 'n/a')}). "
                     "Fewer flags is not fewer wrong flags: a sales-only rule cannot tell an empty rail from a dead product.")),
        dict(title=f"Price changes ({md(pc_event_day)})" if pc_event_day else "Price changes",
             number=f"machine-wide trend over the same days {mach_rr:.2f}x" if mach_rr else "n/a",
             detail=(f"{n_event} of our {len(slots)} slots were repriced on {pc_event_day:%a} {md(pc_event_day)}. " if pc_event_day else "")
                    + f"Per-slot before/after rates are in price_changes; with {units_side_text} and demand falling over the term, none of the "
                      "changes can be read as a clean price effect."),
    ]
    if photo_check:
        findings.append(dict(
            title="Nov 9 photo check of the inferred stock (our slots)",
            number=(f"{len(pc_['empty_and_flagged_empty']) + len(pc_['stocked_and_selling'])} of {len(slots)} rails agree outright; "
                    f"{len(pc_['stocked_and_flagged_empty'])} called empty but stocked"),
            detail=(f"Photo-stocked rails the model calls empty: {', '.join(pc_['stocked_and_flagged_empty']) or 'none'} (stocked but not selling: slow sellers, "
                    f"not stock-outs). Photo-stocked and quiet, not flagged: {', '.join(pc_['stocked_but_quiet']) or 'none'}. Looks empty and flagged empty: "
                    f"{', '.join(pc_['empty_and_flagged_empty']) or 'none'}. Looks empty after selling in the week before (sold out): "
                    f"{', '.join(pc_['empty_and_sold_recently']) or 'none'}. Looks empty but the model never flagged it (missed stock-outs): "
                    f"{', '.join(pc_['empty_and_not_flagged']) or 'none'}. Looked empty but sold before the next visit: "
                    f"{', '.join(pc_['empty_but_sold_after']) or 'none'}. Product at the back of a rail can be hidden, so the photo is strong but not perfect evidence.")))
    if products:
        pb, pcat, pcaf = products["by_brand"], products["by_category"], {r["caffeine"]: r for r in products["by_caffeine"]}
        top = [r for r in pb if not r["brand"].startswith("unidentified") and r["brand"] != "unknown"][:3]
        findings.append(dict(
            title="Products (from the dated photos)",
            number=(", ".join(f"{r['category']} {r['share']:.0%}" for r in pcat[:3]) + " of our units; "
                    f"caffeinated {pcaf.get('caffeine', {}).get('share', 0):.0%}"),
            detail=("Top product lines on our rails: " + "; ".join(f"{r['brand']} {r['units']} units at ${r['avg_price']:.2f} paid on average "
                                                                   f"(slots {ranges(r['slots'])})" for r in top)
                    + f". {products['basis_units'].get('bracketed', 0)} of {products['units_total']} units sit between two photos that show the "
                      "same brand; the rest are attributed to the nearest photo. Photo dates are windows and products can change between photos.")))

    # ============================================================ JSON
    calendar_out = dict(
        days=[dict(date=d["date"], dow=dt.date.fromisoformat(d["date"]).weekday(), classes=d["classes"], institute=d["institute"],
                   in_term=d["in_term"], label=d["label"])
              for d in cal["days"] if ds(d0) <= d["date"] <= ds(d1 + dt.timedelta(days=35))],
        events=[e for e in cal.get("events", []) if e["end"] >= ds(d0) and e["start"] <= ds(d1 + dt.timedelta(days=35))],
        terms=cal.get("terms", []), checked=cal.get("checked"))

    backtests = dict(
        note=f"Daily MACHINE-WIDE units (gap-spread), plan date = each Saturday from {ds(origins[0])}, forecast once for the next 7 days "
             "(Saturday to Friday), expanding window from term start Sep 9; at each plan date the gap lumps are re-spread with a profile built "
             "only from data reported before it. Error = actual - forecast. MAE in units per day. WAPE = sum|error| / sum actual (our addition). "
             "Windows with a no-class weekday are flagged (calendar_flag).",
        origins=[ds(o) for o in origins], models=model_names, ranking=ranking, winner=best1, runner_up=best2,
        seasonal_naive=SN, beats_seasonal_naive=beats_sn,
        per_origin_metrics=[{k: (rnd(v, 3) if isinstance(v, float) else v) for k, v in r.items()} for r in bt_rows],
        average={m: {k: rnd(v, 3) for k, v in a.items()} for m, a in avg.items()},
        mae_by_k_sma=mae_by_k, intervals={m: {k: rnd(v, 3) for k, v in iv.items()} for m, iv in intervals.items()},
        per_origin=per_origin, weekly=weekly_bt, fit_log=fit_log_backtest)

    lab = dict(
        scope=dict(device=DEVICE_ID, our_slots=slots, note=SCOPE_NOTE),
        visits=visits_out, refill_evidence=refill_evidence, last_proven_refill=lpr, supply=supply, photo_check=photo_check,
        meta=dict(generated_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), device=DEVICE_ID, timezone=TZ,
                  first_poll=T0.isoformat(timespec="minutes"), last_poll=T1.isoformat(timespec="minutes"), polls=len(P),
                  totals=dict(units=TOTAL_UNITS, cents=TOTAL_CENTS, slots=len(slots), days=len(dates), weeks=round(len(dates) / 7, 1)),
                  machine_totals=dict(units=MACHINE_UNITS, cents=MACHINE_CENTS),
                  our_slots=slots, week_starts="Saturday", last_full_week=last_full["week"],
                  regular_polling_from=REGULAR_START.isoformat(timespec="minutes"),
                  gaps=[dict(start=g.prev.isoformat(timespec="minutes"), end=g.t.isoformat(timespec="minutes"), hours=rnd(g.hours, 1),
                             units=int(g.units), units_ours=int(g.units_ours)) for g in gaps.itertuples()],
                  checks=checks, counter_check=counter_check,
                  last_proven_refill=lpr["statement"],
                  visits_source="door-open counter (EA2*DO)" if doors is not None else f"assumed Friday {VISIT_HOUR:02d}:00 (no door data)",
                  notes=[
                      SCOPE_NOTE,
                      "Visits are dated from the machine's door-open counter (EVA-DTS EA2*DO lifetime count, read at every poll): a rise "
                      f"between two polls = the door was opened in between; rises within {VISIT_MERGE_H} h = one visit. The door counter says "
                      "WHEN the machine was opened, not what was loaded.",
                      f"Refill evidence per visit, from our slots only: proven (capacity proof: one of our slots sold more than {CAPACITY_MAX}, the "
                      f"largest rail, over an interval containing exactly one visit), likely (>= {LIKELY_MIN} of our slots that looked empty resumed "
                      f"within {RESUME_DAYS} days), possible top-up (1), none. Nov 9 was the student team photographing the machine and is never counted as a refill.",
                      (lpr["statement"] or "No proven refill.") + " The operators stopped refilling to prepare for the project, so the "
                      "late-term decline is mostly supply running out.",
                      "Suspected-empty windows end at door visits (the last one before the slot sold again), never at the slot's own next sale; a "
                      "silence that ends in a sale with no door opening in between proves the slot was stocked. A window still open at the end of "
                      "the data is 'sold out, not refilled' when it began after the last single-visit-proven refill. stockouts.windows[].fridays is "
                      "kept for older lab code and counts the door visits inside the window.",
                      "Machine reports what it SOLD, not what it HOLDS. Zero sales can mean an empty slot. 'Suspected empty' is inferred, not observed.",
                      "Products come from dated team photos (see products); the machine itself never reports product names.",
                      "Sales are timed at the midpoint of each poll interval (<= 2 h). After a gap > 2 h, 'raw' keeps the lump on the poll day; "
                      "'units'/'cents' spread it over the gap by a regular-era hour-of-week profile.",
                      f"Regular hourly polling from {REGULAR_START:%Y-%m-%d %H:%M}. Earlier days are gap-affected.",
                      "Week = Saturday 00:00 to the next Saturday 00:00 ET, labelled by the Saturday date (vending/ledger.py WEEK_STARTS = 5). "
                      f"The current week is partial; the last full week began {last_full['week']}.",
                      f"Last day {ds(d1)} ends at {T1:%H:%M} (after the selling hours; treated as complete for forecasting).",
                      "Takings are tax-included cents as reported by the machine.",
                      "Calendar from the Golisano Institute's official academic calendar"
                      + (f" (re-checked {cal['checked']['date']}: {cal['checked']['result']})" if cal.get("checked") else "")
                      + "; class meeting times inferred (weekday mornings).",
                      "Error = actual - forecast. Bias > 0 means the forecast ran low.",
                  ]),
        calendar=calendar_out, daily=daily_out, weekly=W, hourly=hourly_out, decomposition=decomp, dow_index=dow_out,
        regression=dict(calendar=regression, trend_tests=trend_test), term_cycle=term_cycle,
        backtests=backtests, forecasts=forecasts,
        slots=[{k: (rnd(v, 3) if isinstance(v, float) else v) for k, v in r.items()} for r in slot_rows],
        slot_rowcol_regression=rowcol_reg, slot_daily=slot_daily,
        price_changes=dict(**price_changes, history={s: [dict(from_=h["from_"], cents=h["cents"]) for h in v] for s, v in price_hist.items()}),
        stockouts=dict(windows=[dict(slot=w["slot"], start=w["start"].isoformat(timespec="minutes"), end=w["end"].isoformat(timespec="minutes"),
                                     how=w["how"], fridays=w["fridays"], visit=w["visit"], visit_evidence=w["visit_evidence"],
                                     status=w["status"], expected=rnd(w["expected"], 2), p0=rnd(w["p0"], 5), confidence=w["confidence"])
                                for w in windows],
                       improbable_midweek=[dict(slot=w["slot"], start=w["start"].isoformat(timespec="minutes"), end=w["end"].isoformat(timespec="minutes"),
                                                expected=rnd(w["expected"], 2), p0=rnd(w["p0"], 5)) for w in midweek]),
        clusters=clusters, rule_sim=rule_sim, products=products, findings=findings)

    def clean(o):
        if isinstance(o, dict):
            return {k: clean(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [clean(v) for v in o]
        if isinstance(o, (float, np.floating)):
            return None if (math.isnan(o) or math.isinf(o)) else float(o)
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.bool_):
            return bool(o)
        return o
    lab = clean(lab)
    # scope guard: every per-slot record and every slot-keyed map in the lab data must be one of our slots
    def slot_values(o):
        found = set()
        if isinstance(o, dict):
            if isinstance(o.get("slot"), str):
                found.add(o["slot"])
            for v in o.values():
                found |= slot_values(v)
        elif isinstance(o, list):
            for v in o:
                found |= slot_values(v)
        return found
    keyed = ([lab["slot_daily"]["slots"], lab["clusters"]["slot_features"], lab["meta"]["counter_check"], lab["price_changes"]["history"],
              lab["refill_evidence"]["per_slot"]] + [k_["assignments"] for k_ in lab["clusters"]["by_k"].values()]
             + list(lab["rule_sim"]["asof"]) + ([lab["products"]["timeline"], lab["products"]["runs"]] if lab["products"] else []))
    leaks = sorted((slot_values(lab) | {k for m in keyed for k in m}) - set(slots))
    if leaks:
        raise SystemExit(f"SCOPE: lab data carries slots outside ours: {leaks}")
    js = json.dumps(lab, separators=(",", ":"), allow_nan=False, default=lambda o: rnd(o) if isinstance(o, (np.floating, np.integer)) else (bool(o) if isinstance(o, np.bool_) else str(o)))
    js = js.replace('"from_"', '"from"')
    (OUT / "lab_data.json").write_text(js, encoding="utf-8")

    # ============================================================ report
    write_report(OUT / "analysis_report.md", locals())

    print("=== checks ===")
    for k, v in checks.items():
        print(f"  {k}: {v}")
    bad = {s: c for s, c in counter_check.items() if not c["ok"]}
    print(f"  counter check per slot: {len(counter_check) - len(bad)} of {len(counter_check)} ok" + (f"; FAILED {bad}" if bad else ""))
    print(f"lab_data.json: {len(js) / 1024:.0f} KB")
    print("=== backtest average (daily machine totals, H=7) ===")
    for m in ranking:
        a = avg[m]
        print(f"  {m:55s} MAE {a['mae']:.2f}  RMSE {a['rmse']:.2f}  WAPE {a['wape']:.2f}  bias {a['bias']:+.2f}  no-break MAE {a['mae_no_break_windows']:.2f}  wins {a['wins']}")
    print("  exponential-smoothing fits:", fit_log_all)
    print("=== weekly backtest ===")
    for r in weekly_bt["summary"]:
        print("  ", r)
    print("=== rules ===")
    for r in rule_summary:
        print(f"  {r['rule']:20s} flags {r['flags']:3d} known {r['flags_with_outcome']:3d} contra {r['contradicted']:3d} ({r['contradicted_share']}) "
              f"refill {r['sold_after_refill']:3d} empty {r['flagged_while_suspected_empty']:3d} next14 {r['units_sold_next_14d_by_flagged']:3d} now {r['flagged_now']}")
    for v_ in rule_verdicts:
        print(f"  {v_['base']} -> {v_['filtered']}: {v_['verdict']}")
    print("=== visits (door counter; evidence from our slots) ===")
    for v in vis:
        print(f"  {v['id']:2d} {vwin(v)} open {v['openings']}  {v['evidence']:18s} "
              f"proven {v['proven_slots']} pair {v['pair_proof_slots']} resumed {v['resumed_slots']} {v['note']}")
    print(f"  {lpr['statement']}")
    print("=== supply, our slots (week: units, selling, empty at end, sold out not refilled, silent since before last proven refill) ===")
    for r in supply:
        print(f"  {r['week']} {r['units']:4d} {r['slots_selling']:3d} {r['slots_empty_at_week_end']:3d} {r['slots_sold_out_not_refilled']:3d} "
              f"{r['slots_silent_since_before_last_restock']:3d} visits {r['visits']} ({', '.join(r['visit_evidence'])})")
    if photo_check:
        print("=== Nov 9 photo check ===")
        for k in ("stocked_and_flagged_empty", "stocked_and_selling", "stocked_but_quiet", "empty_and_flagged_empty",
                  "empty_and_sold_recently", "empty_and_not_flagged", "empty_but_sold_after", "agreement", "no_conflict_share"):
            print(f"  {k}: {photo_check[k]}")
    print("=== clusters ===")
    for k, v in clusters["by_k"].items():
        print(f"  k={k} expl {v['pct_explained']} sil {v['silhouette']} stab {v['stability_pair_agreement']} sizes {[p['size'] for p in v['profiles']]} {[p['name'] for p in v['profiles']]}")
    if products:
        print("=== products ===")
        for r in products["by_category"]:
            print(f"  {r['category']:16s} {r['units']:4d} ({r['share']})  ${r['avg_price']}  bracketed {r['units_bracketed']}")
        print("  ", products["by_caffeine"])
    print(f"all_ok: {checks['all_ok']}")
    if not ok:
        raise SystemExit("CHECKS FAILED")


# ============================================================== report writer
def write_report(path, L):
    avg, ranking, SN = L["avg"], L["ranking"], L["SN"]
    best1, best2 = L["best1"], L["best2"]
    rs = {r["rule"]: r for r in L["rule_summary"]}
    findings, checks = L["findings"], L["checks"]
    regA, regB = L["regA"], L["regB"]
    weekly_bt, fc, clusters, pc = L["weekly_bt"], L["forecasts"], L["clusters"], L["price_changes"]
    T0, T1, slots = L["T0"], L["T1"], L["slots"]
    reg_models = L["regression"]["models"]
    cal, products = L["cal"], L["products"]
    lines = []
    w = lines.append

    def pv_(p):
        return "<0.001" if p is not None and p < 0.001 else p

    def coef_rows(model):
        w("| Term | Coef | 95% CI | p | Meaning |")
        w("|---|---|---|---|---|")
        for c in reg_models[model]["coefs"]:
            w(f"| {c['term']} | {c['coef']} | {c['ci_lo']} to {c['ci_hi']} | {pv_(c['p'])} | {c['meaning']} |")

    w("# Vending machine vmc1: sales analysis of our 20 slots")
    w("")
    w(f"Data: {T0:%Y-%m-%d %H:%M} to {T1:%Y-%m-%d %H:%M} ET. Our slots {L['slots_text']}: {checks['units_ours']} drinks, "
      f"${checks['cents_ours'] / 100:,.2f} tax-included. Generated by `analysis/sales_analysis.py` (rerunnable, fixed seed 42).")
    w("")
    w(f"**Scope.** {SCOPE_NOTE} Weeks run Saturday to Friday, as in the ledger and the P&L page (`vending/ledger.py`, `WEEK_STARTS = 5`).")
    w("")
    w("## Headline findings")
    w("")
    for i, f in enumerate(findings, 1):
        w(f"{i}. **{f['title']}: {f['number']}.** {f['detail']}")
    w("")
    vis, supply, lpr, photo_check = L["vis"], L["supply"], L["lpr"], L["photo_check"]
    if L["doors"] is not None:
        w("## Visits and refills (door counter + our slots)")
        w("")
        w(f"Each rise in the machine's door-open counter between two polls is a door opening; rises within {VISIT_MERGE_H} h are one visit. "
          f"A visit is a **proven refill** when one of our slots sold more than {CAPACITY_MAX} units (the largest rail on the loading label) "
          f"over a stretch that contains no other visit; **likely** when {LIKELY_MIN} or more of our slots that looked empty (silent "
          f"{SILENT_DAYS}+ days, P(0) < {ALPHA_EMPTY} on their own clock) resumed within {RESUME_DAYS} days; **possible top-up** when one did. "
          "The counter says when the door opened, not what was loaded. Only our slots are used as evidence.")
        w("")
        w("| # | When (ET) | Door openings | Evidence | Capacity-proven slots | Slots that resumed | Note |")
        w("|---|---|---|---|---|---|---|")
        for v in vis:
            w(f"| {v['id']} | {vwin(v)} | {v['openings']} | {v['evidence']} | "
              f"{', '.join(v['proven_slots']) or '-'}{' (pair: ' + ', '.join(v['pair_proof_slots']) + ')' if v['pair_proof_slots'] and not v['proven_slots'] else ''} | "
              f"{', '.join(v['resumed_slots']) or '-'} | {v['note'] or ''} |")
        w("")
        w(f"Times before Sep 21 are only 'between two polls' (polling was days apart). {lpr['statement'] or ''}")
        w("")
        w("Supply of our slots by week (Saturday start; slots that sold; slots suspected empty at the end of the week; of those, sold out "
          "after the last proven refill and not refilled since):")
        w("")
        w("| Week (Sat start) | Units | Slots selling | Suspected empty at week end | Sold out, not refilled | Empty since before the last proven refill | Visits (evidence) |")
        w("|---|---|---|---|---|---|---|")
        for r in supply:
            if r["units"] == 0 and not r["visits"]:
                continue
            w(f"| {r['week']}{' (partial)' if r['partial'] else ''} | {r['units']} | {r['slots_selling']} | {r['slots_empty_at_week_end']} | "
              f"{r['slots_sold_out_not_refilled']} | {r['slots_silent_since_before_last_restock']} | "
              f"{', '.join(f'{i} ({e})' for i, e in zip(r['visits'], r['visit_evidence'])) or '-'} |")
        w("")
        if photo_check:
            phc = photo_check
            w("**Nov 9 photo check (our slots).** " + phc["note"])
            w("")
            w("| | Model: suspected empty | Model: sold in the 7 days before | Model: quiet, not flagged |")
            w("|---|---|---|---|")
            w(f"| Photo: product visible | {', '.join(phc['stocked_and_flagged_empty']) or '-'} | {', '.join(phc['stocked_and_selling']) or '-'} | {', '.join(phc['stocked_but_quiet']) or '-'} |")
            w(f"| Photo: looks empty | {', '.join(phc['empty_and_flagged_empty']) or '-'} | {', '.join(phc['empty_and_sold_recently']) or '-'} | {', '.join(phc['empty_and_not_flagged']) or '-'} |")
            w("")
            w(f"Read: the diagonal agrees ({len(phc['empty_and_flagged_empty'])} empty rails flagged empty, {len(phc['stocked_and_selling'])} stocked rails "
              f"still selling). {', '.join(phc['stocked_and_flagged_empty']) or 'None'} {'were' if len(phc['stocked_and_flagged_empty']) != 1 else 'was'} called "
              "empty but hold product: slow sellers, not stock-outs. "
              f"{', '.join(phc['empty_and_sold_recently']) or 'None'} sold in the week before and now look empty: sold out. "
              f"The {len(phc['empty_and_not_flagged'])} looks-empty rails the model never flagged are missed stock-outs (rarely-selling slots are not scored). "
              + (f"{', '.join(phc['empty_but_sold_after'])} looked empty but sold soon after the visit (product hidden at the back, or bought by the visitors)."
                 if phc["empty_but_sold_after"] else ""))
            w("")
    if products:
        w("## Products (from the dated photos, our slots only)")
        w("")
        w(products["note"])
        w("")
        w("**By category**")
        w("")
        w("| Category | Units | Share of our units | Takings | Avg price paid | Units between two photos of the same brand | Slots |")
        w("|---|---|---|---|---|---|---|")
        for r in products["by_category"]:
            w(f"| {r['category']} | {r['units']} | {r['share']:.0%} | ${r['cents'] / 100:,.2f} | "
              f"{'$%.2f' % r['avg_price'] if r['avg_price'] is not None else '-'} | {r['units_bracketed']} | {L['ranges'](r['slots'])} |")
        w("")
        w("**Caffeine:** " + "; ".join(f"{r['caffeine']} {r['units']} units ({r['share']:.0%}, ${r['avg_price']:.2f} paid on average)"
                                       for r in products["by_caffeine"]) + ".")
        w("")
        w("**By product line**")
        w("")
        w("| Product line | Category | Caffeine | Units | Avg price paid | Bracketed units | Slots |")
        w("|---|---|---|---|---|---|---|")
        for r in products["by_brand"]:
            w(f"| {r['brand']} | {r['category']} | {r['caffeine']} | {r['units']} | "
              f"{'$%.2f' % r['avg_price'] if r['avg_price'] is not None else '-'} | {r['units_bracketed']} | {L['ranges'](r['slots'])} |")
        w("")
        w("**What each slot held** (photo date: product; 'empty' = no unit visible at the front). Then the runs of sales attributed to each "
          "product, with units and average price paid.")
        w("")
        w("| Slot | Photos | Sales by product (first to last sale, units, avg price) |")
        w("|---|---|---|")
        for s in slots:
            tl = products["timeline"][s]
            seen = []
            for x in tl:
                lab_ = x["brand"] or "empty"
                if seen and seen[-1][1] == lab_:
                    continue
                seen.append((x["at"][5:10], lab_))
            runs = products["runs"][s]
            w(f"| {s} | " + " · ".join(f"{d} {b}" for d, b in seen) + " | "
              + "; ".join(f"{r['brand']} {r['frm'][5:10]}..{r['to'][5:10]} {r['units']}u ${r['avg_price']:.2f}" for r in runs) + " |")
        w("")
        w("Caveats: " + " ".join(products["caveats"]))
        w("")
    w("## Forecast backtest (machine-wide daily totals, 7 days ahead from each Saturday)")
    w("")
    w(f"Plan dates: {', '.join(L['backtests']['origins'])}. Expanding window from term start. Error = actual - forecast; "
      "bias > 0 means the forecast ran low. WAPE is our addition (MAE as a share of sales). Machine-wide totals are allowed here "
      "(an aggregate over the whole machine).")
    w("")
    w("| Rank | Model | MAE / day | RMSE | WAPE | Bias | MAE without break windows | Windows won |")
    w("|---|---|---|---|---|---|---|---|")
    for i, m in enumerate(ranking, 1):
        a = avg[m]
        tag = " (the bar)" if m == SN else ""
        w(f"| {i} | {m}{tag} | {a['mae']:.2f} | {a['rmse']:.2f} | {a['wape']:.0%} | {a['bias']:+.2f} | {a['mae_no_break_windows']:.2f} | {a['wins']} |")
    w("")
    beat = L["beats_sn"]
    w(f"**Winner: {best1}.** Runner-up: {best2}. {len(beat)} model{'s beat' if len(beat) != 1 else ' beats'} seasonal naive on average MAE"
      + (f" ({', '.join(beat)})." if beat else "."))
    w("")
    fl = L["fit_log_all"]
    w("Exponential-smoothing fits (backtest and forward forecast): " + "; ".join(
        f"{k}: {v['fits']} fits, {v['fallbacks']} fell back{' to seasonal naive' if 'Winters' in k else ' to the SES level'}, {v['warned']} warned"
        for k, v in fl.items()) + ".")
    w("")
    w("Weekly view (horizon 1 week, " + f"{len(weekly_bt['per_week'])} test weeks, training weeks re-spread as of each test week): " +
      "; ".join(f"{r['model']} MAE {r['mae']}" for r in weekly_bt["summary"][:4]) + ". Too few weeks to choose on.")
    cp = weekly_bt["current_partial_week"]
    w(f"The current week ({cp['week']}) is partial: {cp['units_observed']} machine units in {cp['days_observed']} days, about {cp['scaled_full_week']} scaled to a full class week.")
    w("")
    w("## Next 4 weeks (machine-wide totals)")
    w("")
    w("| Week (Sat start) | Days | Class days | " + " | ".join(fc["models"].keys()) + " | Restocked scenario (our 20 slots) |")
    w("|---|---|---|" + "---|" * len(fc["models"]) + "---|")
    for r in fc["weekly"]:
        w(f"| {r['week']} | {r['days']} | {r['class_days']} | " + " | ".join(str(r[m]) for m in fc["models"]) + f" | {r['restocked_recent']} |")
    w("")
    w(f"- {fc['supply_warning']}")
    w(f"- Days off ahead (calendar file): {fc['breaks_ahead']}. Expect near-zero sales on them; they must not count as 'nothing moved' for any slot.")
    w(f"- Restocked scenario: about {fc['restocked_scenario']['per_class_day_recent']} units per class day from our 20 slots if each were kept full (recent in-stock rates).")
    w("")
    w("## Calendar regression (daily units, regular-polling era)")
    w("")
    w(f"**Model A: {reg_models['A']['y']}.** `{reg_models['A']['formula']}`, R² = {reg_models['A']['r2']}, n = {reg_models['A']['n']}. "
      f"The weekday and day-off statements in the findings quote this table.")
    w("")
    coef_rows("A")
    w("")
    w(f"**Model B: {reg_models['B']['y']}.** `{reg_models['B']['formula']}`, R² = {reg_models['B']['r2']}. Without the empty-slot control "
      f"(model B0, R² = {reg_models['B0']['r2']}) the week-of-term slope is "
      f"{ {c['term']: c for c in reg_models['B0']['coefs']}['week_of_term']['coef'] } units/day per week. The trend finding quotes B and B0.")
    w("")
    coef_rows("B")
    w("")
    w("## Our slots")
    w("")
    bk = clusters["best_k_by_silhouette"]
    w(f"K-means on 5 standardized slot features, our {len(slots)} slots; k = 2..6 tried. Best silhouette at k = {bk} "
      f"(silhouette {clusters['by_k'][str(bk)]['silhouette']}, {clusters['by_k'][str(bk)]['pct_explained']:.0%} of variation explained). "
      "Elbow and silhouette for every k are in lab_data.json so the team can choose. "
      f"Smallest segment at k = {bk}: {min(p['size'] for p in clusters['by_k'][str(bk)]['profiles'])} slots; at k = 3 the segments are "
      f"{', '.join(str(p['size']) for p in clusters['by_k']['3']['profiles'])} slots ({', '.join(p['name'] for p in clusters['by_k']['3']['profiles'])}). "
      "Tiny segments fail the segment-size test.")
    w("")
    w("| Segment | Slots | Units/in-stock class day | Avg price paid | Zero-week share | Action |")
    w("|---|---|---|---|---|---|")
    for p in clusters["by_k"][str(bk)]["profiles"]:
        c = p["centroid"]
        w(f"| {p['name']} | {', '.join(p['slots'])} | {c['rate']} | ${c['avg_price']} | {c['zero_week_share']} | {p['action']} |")
    w("")
    rr = L["rowcol_reg"]
    w(f"Row/column regression (n = {rr['full']['n']} of our slots, shelves {rr['rows'][0]}-{rr['rows'][-1]}, columns {rr['cols'][0]}-{rr['cols'][-1]}): "
      f"R² {rr['full']['r2']}, adjusted {rr['full']['adj_r2']}; joint test for shelf p = {rr['joint']['row']['p']}, for column p = {rr['joint']['col']['p']}. "
      "Shelf, column, price and product are confounded, so these are associations only.")
    w("")
    if pc["event_date"]:
        w(f"Price changes: the {pc['event_date']} reprice ({pc['changed_on_event']} of our slots) is compared over {len(pc['before_days'])} class days "
          f"each side. Over the same days the whole machine sold {pc['machine_trend_ratio']}x its earlier units per class day (machine-wide "
          "trend, a total); each repriced slot's ratio is also shown divided by that trend. No slot is compared with another slot. "
          f"Counts are small ({pc['units_per_side']}).")
        w("")
        w("Price rules: the price paid on a sale is the listed price in effect when its poll interval began (the slot's last price reading at "
          "or before that poll); the price on a day is the listed price at 12:00 noon ET (last reading before noon, else the day's first reading).")
        w("")
    w("## Slow-seller rule: what to change in the agent's guideline")
    w("")
    rsim = L["rule_sim"]
    w(f"Simulated at every actual door visit ({len(L['dp'])} visits, the first each day, dated by the door counter) plus a planning point "
      f"({rsim['decision_display'][-1]}). Each flag's next 7 days are split three ways: **sold again with no refill in between** "
      "(the slot was alive, so the flag misjudged a slow seller or a lull), **sold again only after a refill** (a visit that counts as a refill "
      "of that slot, starting with the decision visit itself: probably empty), and **silent**. The 14-day column counts each slot-day once.")
    w("")
    w("**How circularity was removed.** (1) The 'not suspected empty' check is computed as of each decision, from sales and door visits up "
      "to the moment the door opened; stock-out windows end at door visits, never at the slot's own next sale. Before this change the windows "
      "ended at the slot's next sale, so the filter was partly built from the outcome it was scored on. (2) A slot's own 'resumed after a visit' "
      "status no longer counts as a refill of that slot when scoring its outcome: it is defined by an improbable silence, the very quantity the "
      "Poisson rule flags on. A refill now needs a capacity proof for the slot, or a visit graded as a proven or likely restock (door counter "
      "plus our slots, not counting the slot's own silence). (3) Decision points no longer depend on later sales: every door visit is one, "
      "not only the visits whose refill grade (which rests on sales after the visit) showed evidence.")
    w("")
    w("| Rule | Flags | With outcome | Sold again, no refill (alive) | Sold again after a refill | Silent next 7 days | Flagged while suspected empty | Units the flagged slots sold in the next 14 days | Flagged now |")
    w("|---|---|---|---|---|---|---|---|---|")
    for r in L["rule_summary"]:
        cs = f"{r['contradicted']} ({r['contradicted_share']:.0%})" if r["contradicted_share"] is not None else "-"
        fs = f"{r['sold_after_refill']} ({r['sold_after_refill_share']:.0%})" if r["sold_after_refill_share"] is not None else "-"
        w(f"| {r['description']} | {r['flags']} | {r['flags_with_outcome']} | {cs} | {fs} | {r['silent_next_week']} | {r['flagged_while_suspected_empty']} | "
          f"{r['units_sold_next_14d_by_flagged']} | {', '.join(r['flagged_now']) or '-'} |")
    w("")
    w("**Is the empty check more accurate, or only quieter?** Compared on the flags with a known outcome. 'More accurate' needs a lower "
      "live-slot share that passes a one-sided Fisher exact test (flags the check keeps vs flags it drops, p < 0.05); a lower share that "
      "does not pass is reported as only quieter.")
    w("")
    w("| Rule -> with the empty check | Flags | With outcome | Live slots misjudged (share) | Sold again at all (share) | Verdict |")
    w("|---|---|---|---|---|---|")
    for v in L["rule_verdicts"]:
        if "base_flags" not in v:
            w(f"| {v['base']} -> {v['filtered']} | - | {v['base_known']} -> {v['filtered_known']} | - | - | {v['verdict']} |")
            continue
        w(f"| {v['base']} -> {v['filtered']} | {v['base_flags']} -> {v['filtered_flags']} | {v['base_known']} -> {v['filtered_known']} | "
          f"{v['base_contradicted_share']:.0%} -> {v['filtered_contradicted_share']:.0%} | {v['base_sold_again_share']:.0%} -> "
          f"{v['filtered_sold_again_share']:.0%} | {v['verdict']} |")
    w("")
    vd = {v["filtered"]: v for v in L["rule_verdicts"]}
    v5 = vd.get("c_5cd_notempty", {})
    more_acc = [v for v in L["rule_verdicts"] if v["verdict"].startswith("more accurate")]
    w(f"Plainly: fewer flags is not fewer wrong flags. For the 5-class-day rule the empty check is **{v5.get('verdict', 'not scorable')}**. "
      + (f"Across all {len(L['rule_verdicts'])} pairs it is more accurate for {len(more_acc)} ({', '.join(v['filtered'] for v in more_acc)}); "
         if more_acc else f"In none of the {len(L['rule_verdicts'])} pairs is it more accurate; ")
      + "the rest of the time it only removes flags. The check cannot be validated from sales data, because a sales-only signal cannot tell an "
        "empty rail from a dead product; in practice the check is the restocker looking at the rail.")
    w("")
    a7, c5, d5n, c7 = rs["a_7cal"], rs["c_5cd_notempty"], rs["d_p05_notempty"], rs["c_7cd_notempty"]
    lp = L["last_proven"]
    w("**Recommended guideline for the agent:**")
    w("")
    w("1. **Detect visits from the door counter, and require a restock log.** Every poll carries the door-open count (EA2*DO); a rise is a visit. "
      "The agent should date visits from it rather than assume a weekday, and ask the restocker to log units loaded per slot at each visit. "
      "The counter proves the door opened, not what was loaded; only the log turns 'suspected empty' into a fact, and only the log can show "
      "whether an empty check makes the rule more accurate.")
    w("2. **Never judge a slot that has not been refilled.** A slot that sold out after the last refill and was not restocked is a supply problem, "
      "not a slow seller: refill it first, then watch one stocked school week."
      + (f" No refill has been proven at a single visit since {lp['first_rise']:%a %b %d} (the operators stopped regular refilling to prepare "
         f"for the project; later visits refilled only some of our rails: {L['later_text']}), so silences after that date say little about demand."
         if lp is not None else ""))
    w("3. **Count class days, not calendar days.** A 'week' = 5 observed class days (Mon-Fri with classes, polling working). Weekends, the quarter "
      f"break and holidays from the calendar file ({fc['breaks_ahead']}) and any day with a polling gap do not count toward the silence.")
    w("4. **If the slot was stocked (seen at the visit), ask whether the silence is surprising for that slot.** Expected = the slot's own in-stock "
      "rate per class day x silent class days; P(0) = e^(-expected). A slot that sells 0.2 a day going 5 class days silent is normal (P(0) = 0.37): "
      "wait. A slot that sells 1 a day is not (P(0) = 0.007): look at the rail. Judge each slot only by its own history, never by another slot.")
    w("5. **Act at the next real visit** (whenever the door counter shows one): a price test or a swap. Remove only after a full stocked "
      "school week (5 class days) with zero sales. Never act on weeks inside a break; the clock simply pauses.")
    w("")
    w(f"Evidence from the simulation: the plain 7-calendar-day rule raised {rule_line(a7)}. "
      f"Counting 5 observed class days and skipping slots suspected empty (as of the decision) gives {rule_line(c5)}; 7 class days: {rule_line(c7)}. "
      f"The Poisson rule at 5% with the empty check gives {rule_line(d5n)}. lab_data.json rule_sim and slot_daily let the lab recompute any N or alpha.")
    w("")
    w("Read the simulation with two cautions. (1) 'Refill in between' is itself inferred from the door counter plus our slots' sales, so a slow "
      "seller that happened to sell right after a restock visit is counted as refilled. (2) 'Silent' does not prove dead demand: an empty slot "
      "that nobody refills is also silent.")
    now_c5 = c5["flagged_now"]
    w("")
    w(f"Flagged today by '5 class days + not suspected empty': {', '.join(now_c5) or 'none'}. No refill has been proven at a single visit since "
      + (f"{lp['first_rise']:%a %b %d}" if lp is not None else "late October") + ", "
      "so check them at the next visit and refill before judging any of them.")
    w("")
    w("## Caveats")
    w("")
    w(f"- **{L['n_weeks_total']:.0f} weeks, one machine, our 20 slots.** {L['n_reg_full']} full Saturday-start weeks of regular polling. Every estimate "
      f"has wide error bars; weekday and calendar effects rest on a handful of days ({len(L['nc_days'])} no-class weekdays in the clean data).")
    w("- **Products come from photos.** Shelf, column, price and product are confounded, so row/column/price results are associations, not causes; "
      "product attributions depend on photo dates that are windows.")
    w("- **Stock is inferred, not observed.** Visits are observed (door counter) but what was loaded is not. 'Suspected empty' comes from each slot's "
      "own clock; the Nov 9 photo shows where it misses.")
    w(f"- **Capacity proofs use the label's largest rail ({CAPACITY_MAX}).** A rail loaded with smaller packs holds fewer, so the proofs are conservative.")
    w("- **Poll-hour attribution.** Sales are counter rises between polls. Before Sep 21 polls were days apart and lumps were spread by an hour-of-week "
      "profile; in the regular era sales are timed at the midpoint of the hour. Hour-of-day results can move by an hour.")
    lp_ = L["last_proven"]
    later_ = ", ".join(f"{v_['first_rise']:%a %b %d}" for v_ in L["after_lp"]) or "none"
    w("- **The late-term decline is supply.** "
      + (f"Regular refilling stopped after the {lp_['first_rise']:%a %b %d} proven refill (no refill proven at a single visit since); later "
         f"visits with refill evidence ({later_}) refilled only some of our rails, so " if lp_ is not None
         else "No refill is proven at a single visit, so ")
      + "forecasts that follow the recent level predict an emptying machine. Re-check after a full restock.")
    chk = cal.get("checked") or {}
    w(f"- **Calendar.** Official dates, 'subject to change'; re-checked against {chk.get('url', 'the official page')} on {chk.get('date', '?')}: "
      f"{chk.get('result', 'not recorded')}. Class times: weekday mornings (team observation; not from the official calendar).")
    w("- **Backtest once.** Models were scored once on the plan dates; dials (SES alpha, Holt-Winters) were fit only on data before each plan date.")
    w("")
    w(f"Checks: our units {checks['daily_units_ours_raw_sum']} by day, {checks['weekly_units_ours_sum']} by week, {checks['slot_daily_sum']} by slot-day, "
      f"{checks['slot_units_sum']} by slot (expected {checks['expected_units']}); takings {checks['slot_cents_sum']} cents (expected {checks['expected_cents']}); "
      f"counters: {checks['counter_slots_checked']} slots, last-minus-first of lifetime units and cents (restarts {checks['counter_restarts']}, misreads "
      f"{checks['counter_misreads']}) equal the event totals: {checks['counters_ok']}; products {checks['product_units_sum']}; machine-wide "
      f"{checks['daily_units_raw_sum']} (expected {checks['machine_units']}). All OK: {checks['all_ok']}.")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def rule_line(r):
    n, c, f_ = r.get("flags_with_outcome") or 0, r.get("contradicted") or 0, r.get("sold_after_refill") or 0
    if n == 0:
        return f"{r.get('flags')} flags, none with a known outcome yet"
    return (f"{r.get('flags')} flags ({n} with an outcome: {c} ({c / n:.0%}) alive, {f_} ({f_ / n:.0%}) sold only after a refill, "
            f"{r.get('silent_next_week')} silent; {r.get('flagged_while_suspected_empty')} raised on slots suspected empty)")


if __name__ == "__main__":
    main()
