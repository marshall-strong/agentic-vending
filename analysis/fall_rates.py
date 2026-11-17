"""Fall starting rates from all 40 selections: by price band, by shelf, and left half vs right half.

    python -I analysis/fall_rates.py      # after the fall run (rebuild.py runs it)

Before the competition starts no selection is either team's: course staff stocked the whole machine in the fall,
and the inventory server may change our assignment before 3 Dec (docs/integrity.md). So our starting rates should
not depend on which 20 selections we get. This reads the fall run's per-selection in-stock rates
(analysis/out/fall40/lab_data.json, written by sales_analysis.py on the export from export_sales.py --fall-all,
which holds only polls before the start) and summarizes them. It also reads the normal run's lab_data.json
(analysis/out/), which covers our 20 selections (the server's assignment), for the segment figures it is compared
with.

The rate is the analysis's own: units per in-stock class day in the regular-polling era, with each selection's
suspected-empty days taken out (sales_analysis.py). Nothing is re-estimated here. Uncertainty comes from
resampling selections (bootstrap, fixed seed), because selections differ far more than days do.

Writes analysis/out/fall40/fall_rates.json and fall_rates.md (the section rebuild.py adds to REPORT.md).
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import statsmodels.formula.api as smf
import pandas as pd

ANALYSIS = Path(__file__).resolve().parent
sys.path.insert(0, str(ANALYSIS))
from slots_config import ALL_SLOTS, SOURCE_FALL, fall_end  # noqa: E402

SEED = 42
B = 10_000                       # bootstrap resamples
PERMS = 20_000                   # permutations for the left/right test
MIN_INSTOCK_DAYS = 3             # as the row/column regression in sales_analysis.py
SHORT_DAYS = 11                  # fewer in-stock class days than this: a noisy rate (named in the cautions)
QUIET_RATE = 0.15                # in stock 30+ class days but under this rate: maybe an empty or jammed rail
# Price bands by average price paid in the fall (dollars, tax included), lower bound inclusive.
BANDS = [("Low (under $2.50)", 0.0, 2.50), ("Mid ($2.50 to $3.49)", 2.50, 3.50), ("Premium ($3.50 and up)", 3.50, 99.0)]
# The machine's two halves, as a description of the machine (not of whose selections they are).
HALVES = {"left": "columns 1-4", "right": "columns 5-8"}


def band_of(price):
    return next(name for name, lo, hi in BANDS if lo <= price < hi)


def boot(df, rng):
    """95% percentile intervals of the mean rate and the pooled rate over B resamples of the group's selections."""
    rate, units, days = df.rate.to_numpy(float), df.units_rc.to_numpy(float), df.days.to_numpy(float)
    ix = rng.integers(0, len(df), (B, len(df)))
    means = rate[ix].mean(1)
    pooled = units[ix].sum(1) / days[ix].sum(1)
    return ((float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))),
            (float(np.percentile(pooled, 2.5)), float(np.percentile(pooled, 97.5))))


def mean_rate(d):
    return float(d.rate.mean())


def pooled_rate(d):
    return float(d.units_rc.sum() / d.days.sum())


def summarize(df, key, order, rng):
    rows = []
    for level in order:
        g = df[df[key] == level]
        if g.empty:
            continue
        (lo, hi), (plo, phi) = boot(g, rng) if len(g) > 1 else ((None, None), (None, None))
        rows.append(dict(level=str(level), n=len(g), left=int((g.half == "left").sum()), right=int((g.half == "right").sum()),
                         mean_rate=mean_rate(g), ci=(lo, hi), pooled_rate=pooled_rate(g), pooled_ci=(plo, phi),
                         avg_price=float(g.avg_price.mean()), instock_days=float(g.days.sum()), units=int(round(g.units_rc.sum())),
                         slots=list(g.slot)))
    return rows


def left_right(df, rng):
    """Right minus left: mean of selection rates and pooled rate, with bootstrap CIs; a permutation test that swaps
    the half labels within each shelf; and OLS with shelf fixed (and price) so the shelves' mix doesn't drive it."""
    L, R = df[df.half == "left"], df[df.half == "right"]
    diff = mean_rate(R) - mean_rate(L)
    pdiff = pooled_rate(R) - pooled_rate(L)
    def draws(d):
        ix = rng.integers(0, len(d), (B, len(d)))
        return (d.rate.to_numpy(float)[ix].mean(1),
                d.units_rc.to_numpy(float)[ix].sum(1) / d.days.to_numpy(float)[ix].sum(1))
    (lm, lp), (rm, rp) = draws(L), draws(R)
    dd, pd_ = rm - lm, rp - lp
    # permutation: shuffle the half labels within each shelf, so every draw keeps each shelf's left/right split
    is_right = (df.half == "right").to_numpy()
    rate = df.rate.to_numpy(float)
    labels = np.tile(is_right, (PERMS, 1))
    for _, g in df.groupby("row"):
        ix = g.index.to_numpy()
        labels[:, ix] = rng.permuted(labels[:, ix], axis=1)
    perm = (np.where(labels, rate, 0).sum(1) / labels.sum(1)) - (np.where(~labels, rate, 0).sum(1) / (~labels).sum(1))
    p_perm = float((np.abs(perm) >= abs(diff) - 1e-12).mean())
    by_row = [dict(row=int(r), left=float(g[g.half == "left"].rate.mean()), right=float(g[g.half == "right"].rate.mean()))
              for r, g in df.groupby("row")]
    ols = {}
    for name, formula in (("shelf", "rate ~ C(row) + right"), ("shelf_price", "rate ~ C(row) + right + avg_price")):
        m = smf.ols(formula, data=df.assign(right=(df.half == "right").astype(int))).fit()
        ci = m.conf_int().loc["right"]
        ols[name] = dict(formula=formula, coef=float(m.params["right"]), ci=(float(ci[0]), float(ci[1])),
                         p=float(m.pvalues["right"]), n=int(m.nobs), r2=float(m.rsquared))
    return dict(mean_diff=diff, mean_ci=(float(np.percentile(dd, 2.5)), float(np.percentile(dd, 97.5))),
                pooled_diff=pdiff, pooled_ci=(float(np.percentile(pd_, 2.5)), float(np.percentile(pd_, 97.5))),
                p_perm_within_shelf=p_perm, by_row=by_row, ols=ols,
                avg_price=dict(left=float(L.avg_price.mean()), right=float(R.avg_price.mean())))


def f3(x):
    return "n/a" if x is None else f"{x:.3f}"


def ci_text(ci):
    return "n/a" if ci[0] is None else f"{ci[0]:.2f} to {ci[1]:.2f}"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fall", default=str(ANALYSIS / "out" / "fall40" / "lab_data.json"))
    ap.add_argument("--ours", default=str(ANALYSIS / "out" / "lab_data.json"),
                    help="the normal run's lab_data.json (our 20 selections), for its segment figures")
    ap.add_argument("--out", default=str(ANALYSIS / "out" / "fall40"))
    args = ap.parse_args()

    fall = json.loads(Path(args.fall).read_text(encoding="utf-8"))
    sc = fall["scope"]
    if sc.get("source") != SOURCE_FALL:
        raise SystemExit(f"{args.fall}: not a fall run (source {sc.get('source')!r}); run export_sales.py --fall-all first")
    last, cut = pd.Timestamp(fall["meta"]["last_poll"]), fall_end()
    if last >= cut:
        raise SystemExit(f"{args.fall}: the fall run reaches {last}, at or after the fall cut {cut}; it must stop before it")
    ours = json.loads(Path(args.ours).read_text(encoding="utf-8")) if Path(args.ours).exists() else None

    df = pd.DataFrame(fall["slots"])
    df = df[df.slot.isin(ALL_SLOTS)].copy()
    df["days"] = df.instock_class_days.astype(float)
    df["units_rc"] = df.rate * df.days                      # units on in-stock regular-era class days
    df["half"] = np.where(df.col.astype(int) <= 4, "left", "right")
    df["band"] = df.avg_price.map(band_of)
    dropped = sorted(df[df.days < MIN_INSTOCK_DAYS].slot)
    df = df[df.days >= MIN_INSTOCK_DAYS].reset_index(drop=True)

    rng = np.random.default_rng(SEED)
    band_names = [b[0] for b in BANDS]
    by_band = {h: summarize(df if h == "all" else df[df.half == h], "band", band_names, rng) for h in ("all", "left", "right")}
    by_row = summarize(df, "row", sorted(df.row.unique()), rng)
    by_half = summarize(df, "half", ["left", "right"], rng)
    lr = left_right(df, rng)

    # the same rates in the normal run (our 20 selections): the method is per selection, so they must match
    same = None
    if ours:
        o = {r["slot"]: r["rate"] for r in ours["slots"]}
        same = all(abs(o[s] - r) < 1e-9 for s, r in zip(df.slot, df.rate) if s in o)
    old_segments = []
    if ours:
        k4 = ours["clusters"]["by_k"].get("4")
        if k4:
            old_segments = [dict(name=p["name"], slots=p["slots"], rate=p["centroid"]["rate"], avg_price=p["centroid"]["avg_price"])
                            for p in k4["profiles"]]
    new_segments = [dict(name=p["name"], slots=p["slots"], rate=p["centroid"]["rate"], avg_price=p["centroid"]["avg_price"],
                         left=sum(int(s[1]) <= 4 for s in p["slots"]), right=sum(int(s[1]) > 4 for s in p["slots"]))
                    for p in fall["clusters"]["by_k"]["4"]["profiles"]]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    result = dict(scope=dict(source=sc["source"], device=sc["device"], first_poll=fall["meta"]["first_poll"],
                             last_poll=fall["meta"]["last_poll"], before=cut.isoformat(), selections=len(df), dropped=dropped,
                             seed=SEED, bootstrap=B, permutations=PERMS),
                  bands=[dict(name=n, lo=lo, hi=hi) for n, lo, hi in BANDS], by_band=by_band, by_row=by_row, by_half=by_half,
                  left_right=lr, our_rates_match_normal_run=same, segments_ours=old_segments,
                  segments_all_40=new_segments,
                  per_selection=[dict(slot=r.slot, row=int(r.row), col=int(r.col), half=r.half, band=r.band, rate=float(r.rate),
                                      instock_class_days=float(r.days), avg_price=float(r.avg_price)) for r in df.itertuples()])
    (out / "fall_rates.json").write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    md = report(result)
    (out / "fall_rates.md").write_text(md, encoding="utf-8", newline="\n")
    print(md)
    return 0


def halves(per):
    """How many of `per` (per-selection dicts) are in each half: the committed report names no single selection."""
    left = sum(p["half"] == "left" for p in per)
    return f"{left} in the left half, {len(per) - left} in the right"


def report(r):
    lines = []
    w = lines.append
    sc, lr = r["scope"], r["left_right"]
    w("## All 40 selections, fall (before the fall cut)")
    w("")
    w("**Why.** While course staff stocked the whole machine, no selection was either team's, and the inventory server changes "
      "our assignment once before the 3 Dec start (assumptions A16 and A21; docs/integrity.md). The sections above use our "
      "selections (the server's assignment) only. Starting rates should not depend on which 20 selections we get, so this "
      f"section uses the fall's sales from all 40, polls before the fall cut ({sc['before'][:16]}, `FALL_CUTOFF`, before the "
      f"day-one fill) only (data {sc['first_poll'][:16]} to {sc['last_poll'][:16]}). It shows groups only (price band, shelf, "
      "half), never one selection's figures. From the cut, nothing reads per-selection data outside our assignment "
      "(`analysis/slots_config.py`, `check_before_start`).")
    w("")
    w("**Method.** The rate is the analysis's own: units per in-stock class day in the regular-polling era, with each selection's "
      "suspected-empty days taken out. It is estimated per selection, so our 20 selections get exactly the rates above"
      + (" (checked: they match the normal run)" if r["our_rates_match_normal_run"] else "") + ". "
      "'Mean' is the average of the selections' rates (as the segment table above); 'pooled' is units / in-stock days over the group, "
      f"which weights selections by how long they were in stock. 95% intervals resample selections ({sc['bootstrap']:,} bootstrap "
      f"draws, seed {sc['seed']}). Price band = the selection's average price paid in the fall. "
      f"{sc['selections']} selections have at least {MIN_INSTOCK_DAYS} in-stock class days"
      + (f"; {len(sc['dropped'])} left out." if sc["dropped"] else "."))
    w("")
    w("### Starting rate by price band")
    w("")
    w("Units per in-stock class day per selection.")
    w("")
    w("| Price band | All 40: n | Mean (95% CI) | Pooled (95% CI) | Left half (columns 1-4): n, mean | Right half (columns 5-8): n, mean |")
    w("|---|---|---|---|---|---|")
    side = {h: {x["level"]: x for x in r["by_band"][h]} for h in ("left", "right")}
    for x in r["by_band"]["all"]:
        lft, rgt = side["left"].get(x["level"]), side["right"].get(x["level"])
        w(f"| {x['level']} | {x['n']} | {x['mean_rate']:.3f} ({ci_text(x['ci'])}) | {x['pooled_rate']:.3f} ({ci_text(x['pooled_ci'])}) | "
          f"{lft['n'] if lft else 0}, {f3(lft['mean_rate']) if lft else 'n/a'} | {rgt['n'] if rgt else 0}, {f3(rgt['mean_rate']) if rgt else 'n/a'} |")
    w("")
    if r["segments_ours"]:
        w("For comparison, the segments above (our 20 selections, k = 4) and the same k-means on all 40 (same features and seed):")
        w("")
        w("| Our segment | Slots | Mean rate | Avg price |")
        w("|---|---|---|---|")
        for s in r["segments_ours"]:
            w(f"| {s['name']} | {', '.join(s['slots'])} | {s['rate']:.3f} | ${s['avg_price']:.2f} |")
        w("")
    w("| All-40 segment (k = 4) | Selections | Left / right | Mean rate | Avg price |")
    w("|---|---|---|---|---|")
    for s in r["segments_all_40"]:
        w(f"| {s['name']} | {len(s['slots'])} | {s['left']} / {s['right']} | {s['rate']:.3f} | ${s['avg_price']:.2f} |")
    w("")
    w("### By shelf (row 1 = top shelf)")
    w("")
    w("| Shelf | n | Mean (95% CI) | Pooled (95% CI) | Avg price |")
    w("|---|---|---|---|---|")
    for x in r["by_row"]:
        w(f"| {x['level']} | {x['n']} | {x['mean_rate']:.3f} ({ci_text(x['ci'])}) | {x['pooled_rate']:.3f} ({ci_text(x['pooled_ci'])}) | ${x['avg_price']:.2f} |")
    w("")
    w("### Left half vs right half")
    w("")
    w("| Half | n | Mean (95% CI) | Pooled (95% CI) | Avg price | In-stock class days |")
    w("|---|---|---|---|---|---|")
    for x in r["by_half"]:
        w(f"| {HALVES[x['level']]} | {x['n']} | {x['mean_rate']:.3f} ({ci_text(x['ci'])}) | {x['pooled_rate']:.3f} ({ci_text(x['pooled_ci'])}) | "
          f"${x['avg_price']:.2f} | {x['instock_days']:.0f} |")
    w("")
    o1, o2 = lr["ols"]["shelf"], lr["ols"]["shelf_price"]
    w(f"Right minus left: mean {lr['mean_diff']:+.3f} (95% CI {lr['mean_ci'][0]:+.2f} to {lr['mean_ci'][1]:+.2f}); pooled "
      f"{lr['pooled_diff']:+.3f} ({lr['pooled_ci'][0]:+.2f} to {lr['pooled_ci'][1]:+.2f}). Swapping the half labels within each shelf "
      f"({sc['permutations']:,} permutations): p = {lr['p_perm_within_shelf']:.3f}. With shelf held fixed (OLS, n = {o1['n']}): "
      f"{o1['coef']:+.3f} ({o1['ci'][0]:+.2f} to {o1['ci'][1]:+.2f}, p = {o1['p']:.3f}); with price held fixed as well: "
      f"{o2['coef']:+.3f} ({o2['ci'][0]:+.2f} to {o2['ci'][1]:+.2f}, p = {o2['p']:.3f}).")
    w("")
    w("Per shelf, mean rate left / right: " + "; ".join(f"shelf {x['row']} {x['left']:.2f} / {x['right']:.2f}" for x in lr["by_row"]) + ".")
    w("")
    verdict = ("The interval includes zero, so the fall data shows no clear left/right difference"
               if lr["mean_ci"][0] <= 0 <= lr["mean_ci"][1] and lr["p_perm_within_shelf"] >= 0.05
               else "The fall data shows a left/right difference")
    hi_left = [str(x["row"]) for x in lr["by_row"] if x["left"] > x["right"]]
    hi_right = [str(x["row"]) for x in lr["by_row"] if x["right"] > x["left"]]
    if hi_left and hi_right:
        verdict += (f". Shelf by shelf it goes both ways (left higher on shelf {', '.join(hi_left)}, right higher on shelf "
                    f"{', '.join(hi_right)}), which points to the products on each rail more than to the side of the machine")
    w(f"**Reading.** {verdict}. The halves held different products (average price ${lr['avg_price']['left']:.2f} left, "
      f"${lr['avg_price']['right']:.2f} right), so a difference, or its absence, is about what was stocked as much as where. "
      "Use the all-40 band rates as starting rates; they don't depend on which 20 selections we get.")
    w("")
    per = r["per_selection"]
    short = [p for p in per if p["instock_class_days"] < SHORT_DAYS]
    quiet = [p for p in per if p["rate"] < QUIET_RATE and p["instock_class_days"] >= 30]
    cautions = []
    if short:
        cautions.append(f"{len(short)} selections were in stock fewer than {SHORT_DAYS} class days ({halves(short)}), so their "
                        "rates are noisy.")
    if quiet:
        cautions.append(f"A selection that sold almost nothing is never flagged as suspected empty, so it counts as in stock all fall "
                        f"({len(quiet)} selections, {halves(quiet)}: under {QUIET_RATE} a class day over 30+ class days): its low "
                        "rate may be an empty or jammed rail, not low demand.")
    cautions.append("Products and prices were the operators' choice, not ours.")
    w("**Cautions.** " + " ".join(cautions))
    w("")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
