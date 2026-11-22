"""Round 7 product mix: round 6's optimiser under project.py's duplicate rule, plus one test slot.

    python analysis/dayone/round7/optimize7.py analysis/dayone/round7/catalog7.json analysis/dayone/local/round7/opt7b.json \
        [--slots-from PLAN.json] [--seed PLAN.json ...] [--cap-cents 40000]

Same maths as optimize6.py (per SKU, the 5-cent price from the floor to $6.50 that maximises margin x demand;
greedy fill then single-slot swaps; most-we-pay fill at or under the cap, --cap-cents: plan 7 ran at $366, plan 7b at
$400; scored by project.py's base gross profit).
What changed (Kevin, 22 Nov, #230):
- project.py credits a duplicate slot with capacity only, never demand.
- No duplicates on day one: every slot gets a different drink. A mix that would repeat one scores as infeasible, so the
  search never picks A, A, B where a different third drink fits. If no mix of different drinks fits under the cap,
  the run stops and says so (NoMix). The planner's runtime warning about duplicates stays advisory: this rule is only
  for the opening mix.
- One slot is the test slot: it holds a drink from a category no other slot stocks. For each plannable category C,
  the 19 regular slots are optimised without C, then the test slot takes the best C product given those 19, then the
  19 are polished again around it. The C with the highest total base gross profit wins.
- Brand lean: after that, a regular slot holding a second product of a brand already stocked is swapped for a
  product of a new brand when the swap costs at most BRAND_TOL of base gross profit over the run.
- Only products with a price estimate are candidates (optimize.candidates).
Our slots are read from a plan made for the server's assignment (--slots-from; by default round 6's plan in the
git-ignored local folder), never typed here.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import project as P            # noqa: E402
from optimize import best_price, candidates  # noqa: E402

CAP_CENTS = P.FILL_CAP_CENTS
BRAND_TOL = 5.00   # dollars of base gross profit over the run a new brand may cost (about 0.5%)
INFEASIBLE = -1e9


class NoMix(Exception):
    """No mix of different drinks for every slot fits under the cap."""


def brand(sku: str) -> str:
    return sku.split("-")[0].split("_")[0]   # GHOST_ZERO_SUGAR -> GHOST, SPINDRIFT_SPARK -> SPINDRIFT


def optimise(cat: dict, slots: list[int], cap_cents: int, cal: dict | None = None, seeds=(), *,
             distinct: bool = True, test_slot: bool = True, repo: str | None = None, say=print) -> dict:
    """The best mix for `slots` under the cap. `distinct=False` allows a drink in two slots (not for day one).
    `test_slot=False` skips the test-slot and brand-lean steps. Raises NoMix when no mix fits."""
    sys.path.insert(0, P.DEFAULT_REPO)
    from vending.pricing import pack_floor_cents
    cal = cal if cal is not None else P.load_calendar()
    repo = repo or os.environ.get("VENDING_REPO", P.DEFAULT_REPO)
    pool = candidates(cat)
    price = {s: best_price(cat[s], pack_floor_cents(round(P.most(cat[s]) * 100), cat[s]["pack"])) for s in pool}
    n_slots = len(slots)
    memo: dict[tuple, float] = {}
    log: list[str] = []

    def note(msg: str) -> None:
        say(msg); log.append(msg)

    def full(skus: list[str]) -> tuple[float, dict]:
        plan = {"day_one": [dict(slot=slots[i], sku=s, units=cat[s]["cap"], price=price[s]) for i, s in enumerate(skus)]}
        r = P.project(plan, cat, cal, "", repo)
        ok = not r["problems"] and round(r["fill_most"] * 100) <= cap_cents and (not distinct or len(set(skus)) == len(skus))
        return (r["scenarios"]["base"]["gross_profit"] if ok else INFEASIBLE), r

    def score(skus: list[str]) -> float:
        key = tuple(skus)
        if key not in memo:
            memo[key] = full(skus)[0]
        return memo[key]

    def cost(s: str) -> float:
        """Most-we-pay for one slot of s, in whole packs. With every drink different, a mix's cost is the sum."""
        return math.ceil(cat[s]["cap"] / cat[s]["pack"]) * P.most(cat[s])

    def completes(chosen: list[str], n: int, cands: list[str]) -> bool:
        """Different drinks: the cheapest drinks left can still fill the other slots under the cap. Without this
        look-ahead an early, dear pick can leave no affordable third drink although a cheaper mix fits."""
        left = n - len(chosen)
        rest = sorted(cost(s) for s in cands if s not in chosen)
        return len(rest) >= left and sum(cost(s) for s in chosen) + sum(rest[:left]) <= cap_cents / 100 + 1e-9

    def greedy(cands: list[str], n: int, tail: list[str], ch: list[str] | None = None) -> list[str]:
        ch = list(ch or [])
        while len(ch) < n:
            ok = [s for s in cands if not distinct or completes(ch + [s] + tail, n + len(tail), cands)]
            best = max(((score(ch + [s] + tail), s) for s in ok), default=(INFEASIBLE, None))
            if best[0] <= INFEASIBLE / 10:
                break
            ch.append(best[1])
        return ch

    def polish(ch: list[str], cands: list[str], tail: list[str]) -> tuple[list[str], float]:
        cur = score(ch + tail)
        improved = True
        while improved:
            improved = False
            for i in range(len(ch)):
                for s in cands:
                    if s == ch[i]:
                        continue
                    trial = ch[:i] + [s] + ch[i + 1:]
                    v = score(trial + tail)
                    if v > cur + 0.01:
                        ch, cur, improved = trial, v, True
        return ch, cur

    def no_mix(found: int) -> NoMix:
        what = "different drinks" if distinct else "drinks"
        return NoMix(f"No mix of {n_slots} {what} fits under the ${cap_cents / 100:.2f} cap: the best found fills "
                     f"{found}. Raise the cap or add cheaper products to the catalog; no plan was written.")

    # 1. No test slot: what the maths alone does with every slot.
    t0 = time.time()
    # The search is local: --seed starts it from earlier plans too (round 7b was seeded with plans 7 and 6).
    free, free_v = polish(greedy(pool, n_slots, []), pool, [])
    for name, sk in seeds:
        sk = [s for s in sk if s in price]
        if len(sk) == n_slots:
            ch, v = polish(list(sk), pool, [])
            note(f"free start {name}: polished ${v:.2f}" if v > INFEASIBLE / 10 else
                 f"free start {name}: no single swap makes it fit (it repeats a drink or is over the cap); skipped")
            if v > free_v:
                free, free_v = ch, v
    if len(free) < n_slots or free_v <= INFEASIBLE / 10:
        raise no_mix(len(free))
    note(f"free (no test slot): base GP ${free_v:.2f}, {len(set(free))} distinct, {time.time() - t0:.0f}s")
    dups = {s: n for s, n in Counter(free).items() if n > 1}
    if dups:
        note(f"  free plan doubles: {dups}")
    if not test_slot:
        _, r = full(free)
        return {"skus": free, "price": {s: price[s] for s in set(free)}, "base_gp": round(free_v, 2),
                "fill_most": r["fill_most"], "fill_expected": r["fill_expected"], "cap_cents": cap_cents, "log": log}

    # 2. Test slot: one category no other slot stocks.
    cats = sorted({cat[s]["cat"] for s in pool})
    best = None
    by_cat = {}
    for c in cats:
        t0 = time.time()
        reg_pool = [s for s in pool if cat[s]["cat"] != c]
        test_pool = [s for s in pool if cat[s]["cat"] == c]
        # Start the test slot on the category's best product alone, so the regular slots are chosen with its cost
        # under the cap.
        t = max(test_pool, key=lambda s: score([s]))
        start = [s for s in free if cat[s]["cat"] != c][:n_slots - 1]
        # Over the cap with the test drink, or (different drinks) unable to fill the rest: drop the last.
        while start and (score(start + [t]) <= INFEASIBLE / 10
                         or (distinct and not completes(start + [t], n_slots, reg_pool + [t]))):
            start.pop()
        reg = greedy(reg_pool, n_slots - 1, [t], start)
        if len(reg) < n_slots - 1:
            note(f"test {c}: only {len(reg)} regular slots fit under the cap with {t}")
            continue
        for _ in range(3):   # polish the regular slots around t, then re-pick t around them, until t stays
            reg, v = polish(reg, reg_pool, [t])
            tv, t2 = max((score(reg + [s]), s) for s in test_pool)
            if t2 == t or tv <= v + 0.01:
                break
            t, v = t2, tv
        by_cat[c] = dict(test=t, base_gp=round(v, 2))
        note(f"test {c}: {t} -> base GP ${v:.2f} ({time.time() - t0:.0f}s)")
        if best is None or v > best[0]:
            best = (v, reg, t, c)
    if best is None:
        raise no_mix(n_slots - 1)
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
                tv = score(trial + [test])
                if tv >= v - BRAND_TOL and tv > INFEASIBLE / 10:
                    opts.append((v - tv, i, s, n, tv))
        if not opts:
            break
        loss, i, old, new, tv = min(opts)
        reg[i] = new
        swaps.append(dict(out=old, in_=new, cost=round(loss, 2)))
        note(f"brand lean: {old} -> {new}, costs ${loss:.2f} base GP")
        v = tv

    chosen = reg + [test]
    _, r = full(chosen)
    return {"skus": chosen, "test_sku": test, "test_category": test_cat, "price": {s: price[s] for s in set(chosen)},
            "base_gp": round(v, 2), "low_gp": r["scenarios"]["low"]["gross_profit"], "high_gp": r["scenarios"]["high"]["gross_profit"],
            "fill_most": r["fill_most"], "fill_expected": r["fill_expected"], "cap_cents": cap_cents,
            "free_plan": {"skus": free, "base_gp": round(free_v, 2), "distinct": len(set(free))},
            "test_by_category": by_cat, "brand_swaps": swaps, "log": log}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("catalog"); ap.add_argument("out")
    ap.add_argument("--slots-from", default=str(P.LOCAL / "round6" / "plan6.json"),
                    help="a plan made for our current assignment; only its slots are read")
    ap.add_argument("--seed", nargs="*", default=[], help="plan JSONs (day_one lists) to start swaps from")
    ap.add_argument("--cap-cents", type=int, default=CAP_CENTS,
                    help="most-we-pay fill cap (plan 7: 36600; plan 7b: 40000, Kevin 22 Nov)")
    a = ap.parse_args()
    cat = {p["sku"]: p for p in json.load(open(a.catalog, encoding="utf-8"))["products"]}
    seeds = [(f, [d["sku"] for d in json.load(open(f, encoding="utf-8"))["day_one"]]) for f in a.seed]
    try:
        res = optimise(cat, P.slots_from(a.slots_from), a.cap_cents, seeds=seeds)
    except NoMix as e:
        sys.exit(str(e))
    json.dump(res, open(a.out, "w", encoding="utf-8"), indent=1)
    print(f"best base GP ${res['base_gp']:.2f} low ${res['low_gp']:.2f} high ${res['high_gp']:.2f} "
          f"fill ${res['fill_expected']:.2f}/${res['fill_most']:.2f}; test drink {res['test_sku']} ({res['test_category']})")
    for s, n in Counter(res["skus"]).most_common():
        print(f"  {n} x {s} @ ${res['price'][s]:.2f} ({cat[s]['cat']})")


if __name__ == "__main__":
    main()
