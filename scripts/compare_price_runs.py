#!/usr/bin/env python3
"""Line up two or three no-write price benchmarks, one per model.

    python -m vending.price_search --plan picked --compare --no-store --model claude-haiku-5-5 \
        --json haiku.json --by NAME
    python scripts/compare_price_runs.py haiku.json sonnet.json opus.json

For each drink and store it prints what each run found (price, pack, stock, or the refusal code). It also says
whether the runs found the same listing (the same link, ignoring the case of the host, `www.` and a trailing
slash; for Amazon, the same ASIN) and the same price. Then it prints each run's winner and cost per drink, and
each run's total. It reads only the files: no database, no network.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from decimal import Decimal
from urllib.parse import urlsplit

ASIN = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)", re.IGNORECASE)


def listing(url: str | None) -> str | None:
    """A link reduced to what names the listing: Amazon's ASIN, else the host without `www.` and the path."""
    if not url:
        return None
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    found = ASIN.search(parts.path + "/")
    if "amazon." in host and found:
        return "amazon:" + found.group(1).upper()
    return host.removeprefix("www.") + parts.path.rstrip("/")


def _found(store: dict | None) -> str:
    if store is None:
        return "-"
    if store.get("code") and store.get("price") is None:
        return store["code"]
    text = f"${store['price']} for {store['units_per_pack']}"
    if store.get("stock"):
        text += f", {store['stock'].replace('_', ' ')}"
    return text + (" (from the table)" if store.get("source") == "table" else "")


def _same(values: list) -> str:
    present = [v for v in values if v is not None]
    if len(present) < 2:
        return "n/a"
    return "yes" if len(set(present)) == 1 else "no"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Line up price benchmark files (--no-store --json) side by side.")
    parser.add_argument("files", nargs="+", help="two or three JSON files from python -m vending.price_search")
    args = parser.parse_args(argv)
    if not 2 <= len(args.files) <= 3:
        print("give two or three files", file=sys.stderr)
        return 2
    runs = []
    for path in args.files:
        with open(path, encoding="utf-8") as f:
            runs.append(json.load(f))
    names = [r.get("model") or path for r, path in zip(runs, args.files)]
    print("Runs: " + "; ".join(f"{n} ({p})" for n, p in zip(names, args.files)))
    drinks = []
    for r in runs:
        for d in r.get("drinks", []):
            if d["sku"] not in drinks:
                drinks.append(d["sku"])
    totals = [Decimal(0)] * len(runs)
    for sku in drinks:
        print(f"\n{sku}")
        mine = [next((d for d in r.get("drinks", []) if d["sku"] == sku), None) for r in runs]
        stores = []
        for d in mine:
            for s in (d or {}).get("stores", []):
                if s["store"] not in stores:
                    stores.append(s["store"])
        for store in stores:
            found = [next((s for s in (d or {}).get("stores", []) if s["store"] == store), None) for d in mine]
            print(f"  {store}:")
            for name, s in zip(names, found):
                print(f"    {name}: {_found(s)}" + (f"  {s['url']}" if s and s.get("url") else ""))
            print(f"    same listing: {_same([listing(s.get('url')) if s else None for s in found])}; "
                  f"same price: {_same([(s.get('price'), s.get('units_per_pack')) if s and s.get('price') else None for s in found])}")
        for n, (name, d) in enumerate(zip(names, mine)):
            if d is None:
                print(f"  {name}: not in this run")
                continue
            cost = Decimal(d.get("cost_usd") or 0)
            totals[n] += cost
            best = d.get("primary")
            pick = f"{best['store']} ${best.get('price', '?')} for {best.get('units_per_pack', '?')}" if best else "none"
            print(f"  {name}: winner {pick}, cost ${cost:.4f}")
    print("\nTotal cost: " + "; ".join(f"{name} ${total:.4f}" for name, total in zip(names, totals)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
