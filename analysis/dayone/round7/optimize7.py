"""Round 7 product mix: round 6's optimiser under project.py's new duplicate rule, plus one test slot.

    python analysis/dayone/round7/optimize7.py analysis/dayone/round7/catalog7.json analysis/dayone/round7/opt7.json [--seed PLAN.json ...]

Same maths as optimize6.py (per SKU, the 5-cent price from the floor to $6.50 that maximises margin x demand;
greedy fill then single-slot swaps; most-we-pay fill at or under the cap (--cap-cents; plan 7 ran at $366, plan 7b at $400); scored by project.py's base gross profit).
What changed (Kevin, 22 Nov, #230):
- project.py now credits a duplicate slot with capacity only, never demand. There is no rule against duplicates:
  the optimiser may still double a product, and it would only gain by it if that product sells out between refills.
- One slot is the test slot: it holds a drink from a category no other slot stocks. For each plannable category C,
  the 19 regular slots are optimised without C, then the test slot takes the best C product given those 19, then the
  19 are polished again around it. The C with the highest total base gross profit wins.
- Brand lean: after that, a regular slot holding a second product of a brand already stocked is swapped for a
  product of a new brand when the swap costs at most BRAND_TOL of base gross profit over the run.
Our slots are read from round 6's plan (the server's assignment round 6 used), not typed here.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import project as P            # noqa: E402
from optimize import best_price  # noqa: E402

SLOTS = P.slots_from(HERE.parent / "round6" / "plan6.json")
CAP_CENTS = P.FILL_CAP_CENTS
BRAND_TOL = 5.00   # dollars of base gross profit over the run a new brand may cost (about 0.5%)


def brand(sku: str) -> str:
    return sku.split("-")[0].split("_")[0]   # GHOST_ZERO_SUGAR -> GHOST, SPINDRIFT_SPARK -> SPINDRIFT


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("catalog"); ap.add_argument("out")
    ap.add_argument("--seed", nargs="*", default=[], help="plan JSONs (day_one lists) to start swaps from")
    ap.add_argument("--cap-cents", type=int, default=CAP_CENTS,
                    help="most-we-pay fill cap (plan 7: 36600; plan 7b: 40000, Kevin 22 Nov)")
    a = ap.parse_args()
    cap_cents = a.cap_cents
    sys.path.insert(0, P.DEFAULT_REPO)
    from vending.pricing import pack_floor_cents
    cat = {p["sku"]: p for p in json.load(open(a.catalog, encoding="utf-8"))["products"]}
    cal = P.load_calendar()
    pool = [s for s, p in cat.items() if p["orderable_now"]]
    price = {s: best_price(cat[s], pack_floor_cents(round(P.most(cat[s]) * 100), cat[s]["pack"])) for s in pool}
    repo = os.environ.get("VENDING_REPO", P.DEFAULT_REPO)
    memo: dict[tuple, float] = {}

    def full(skus: list[str]) -> tuple[float, dict]:
        plan = {"day_one": [dict(slot=SLOTS[i], sku=s, units=cat[s]["cap"], price=price[s]) for i, s in enumerate(skus)]}
        r = P.project(plan, cat, cal, "", repo)
        ok = not r["problems"] and round(r["fill_most"] * 100) <= cap_cents
        return (r["scenarios"]["base"]["gross_profit"] if ok else -1e9), r

    def score(skus: list[str]) -> tuple[float, None]:
        key = tuple(skus)
        if key not in memo:
            memo[key] = full(skus)[0]
        return memo[key], None

    def greedy(cands: list[str], n: int, tail: list[str], ch: list[str] | None = None) -> list[str]:
        ch = list(ch or [])
        while len(ch) < n:
            best = max(((score(ch + [s] + tail)[0], s) for s in cands), default=(-1e9, None))
            if best[0] <= -1e8:
                break
            ch.append(best[1])
        return ch

    def polish(ch: list[str], cands: list[str], tail: list[str]) -> tuple[list[str], float]:
        cur = score(ch + tail)[0]
        improved = True
        while improved:
            improved = False
            for i in range(len(ch)):
                for s in cands:
                    if s == ch[i]:
                        continue
                    trial = ch[:i] + [s] + ch[i + 1:]
                    v = score(trial + tail)[0]
                    if v > cur + 0.01:
                        ch, cur, improved = trial, v, True
        return ch, cur

    seeds = []
    for f in a.seed:
        sk = [d["sku"] for d in json.load(open(f, encoding="utf-8"))["day_one"]]
        seeds.append((f, [s for s in sk if s in price]))
    log: list[str] = []

    def say(msg: str) -> None:
        print(msg, flush=True); log.append(msg)

    # 1. No test slot: what the maths alone does with 20 slots.
    t0 = time.time()
    free, free_v = polish(greedy(pool, len(SLOTS), []), pool, [])
    for name, sk in seeds:
        if len(sk) == len(SLOTS):
            ch, v = polish(list(sk), pool, [])
            say(f"free start {name}: polished ${v:.2f}")
            if v > free_v:
                free, free_v = ch, v
    say(f"free (no test slot): base GP ${free_v:.2f}, {len(set(free))} distinct, {time.time() - t0:.0f}s")
    dups = {s: n for s, n in Counter(free).items() if n > 1}
    if dups:
        say(f"  free plan doubles: {dups}")

    # 2. Test slot: one category no other slot stocks.
    cats = sorted({cat[s]["cat"] for s in pool})
    best = None
    by_cat = {}
    for c in cats:
        t0 = time.time()
        reg_pool = [s for s in pool if cat[s]["cat"] != c]
        test_pool = [s for s in pool if cat[s]["cat"] == c]
        # Start the test slot on the category's best product alone, so the 19 are chosen with its cost under the cap.
        t = max(test_pool, key=lambda s: score([s])[0])
        start = [s for s in free if cat[s]["cat"] != c][:len(SLOTS) - 1]
        while start and score(start + [t])[0] <= -1e8:      # over the cap with the test drink: drop the cheapest-value
            start.pop()
        reg = greedy(reg_pool, len(SLOTS) - 1, [t], start)
        if len(reg) < len(SLOTS) - 1:
            say(f"test {c}: only {len(reg)} regular slots fit under the cap with {t}"); continue
        for _ in range(3):   # polish the 19 around t, then re-pick t around the 19, until t stays
            reg, v = polish(reg, reg_pool, [t])
            tv, t2 = max((score(reg + [s])[0], s) for s in test_pool)
            if t2 == t or tv <= v + 0.01:
                break
            t, v = t2, tv
        by_cat[c] = dict(test=t, base_gp=round(v, 2))
        say(f"test {c}: {t} -> base GP ${v:.2f} ({time.time() - t0:.0f}s)")
        if best is None or v > best[0]:
            best = (v, reg, t, c)
    v, reg, test, test_cat = best

    # 3. Brand lean: prefer a new brand over another flavour of one we stock, if it costs <= BRAND_TOL.
    swaps = []
    while True:
        counts = Counter(brand(s) for s in reg + [test])
        here = {brand(s) for s in reg + [test]}
        opts = []
        for i, s in enumerate(reg):
            if counts[brand(s)] < 2:
                continue
            for n in pool:
                if brand(n) in here or cat[n]["cat"] == test_cat:
                    continue
                trial = reg[:i] + [n] + reg[i + 1:]
                tv = score(trial + [test])[0]
                if tv >= v - BRAND_TOL and tv > -1e8:
                    opts.append((v - tv, i, s, n, tv))
        if not opts:
            break
        loss, i, old, new, tv = min(opts)
        reg[i] = new
        swaps.append(dict(out=old, in_=new, cost=round(loss, 2)))
        say(f"brand lean: {old} -> {new}, costs ${loss:.2f} base GP")
        v = tv

    chosen = reg + [test]
    _, r = full(chosen)
    json.dump({"skus": chosen, "test_sku": test, "test_category": test_cat, "price": {s: price[s] for s in set(chosen)},
               "base_gp": round(v, 2), "low_gp": r["scenarios"]["low"]["gross_profit"], "high_gp": r["scenarios"]["high"]["gross_profit"],
               "fill_most": r["fill_most"], "fill_expected": r["fill_expected"], "cap_cents": cap_cents,
               "free_plan": {"skus": free, "base_gp": round(free_v, 2), "distinct": len(set(free))},
               "test_by_category": by_cat, "brand_swaps": swaps, "log": log},
              open(a.out, "w", encoding="utf-8"), indent=1)
    print(f"best base GP ${v:.2f} low ${r['scenarios']['low']['gross_profit']:.2f} high ${r['scenarios']['high']['gross_profit']:.2f} "
          f"fill ${r['fill_expected']:.2f}/${r['fill_most']:.2f}; test slot {test} ({test_cat})")
    for s, n in Counter(chosen).most_common():
        print(f"  {n} x {s} @ ${price[s]:.2f} ({cat[s]['cat']})")


if __name__ == "__main__":
    main()
