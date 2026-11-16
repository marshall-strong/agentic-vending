# Sourcing: where to buy each drink, and for how much

Every purchase line needs a store, a pack size, the most the worker may pay per pack and, for
Amazon, a link. The catalog doesn't carry any of that (David, 19 Oct: "cost comes from the store
and the receipt"), so we keep it in our own table, `sourcing_options` (migration 015). People
look prices up and record them; **the agent has no tool that browses stores**, by the team's
choice in [`planning/tool-list.md`](planning/tool-list.md).

Each product has a **primary** way to buy it and, optionally, an **alternate**: the same shape
as a purchase line.

## Adding or changing a price

```
python -m vending.sourcing set COCA-ORIGINAL-20BOT --store Amazon --pack 24 --price 54.95 --url https://www.amazon.com/dp/B004I405P4 --description "24 x 20 oz bottles" --by NAME
```

- `--price` is what the pack cost when you looked. `--max` is the most the worker may pay;
  leave it out and it's the price plus 15%, rounded up to the next 50 cents (54.95 → 63.50).
- `--checked 2026-11-11` if you saw the price on an earlier day. The default is now.
- `--alternate` sets the alternate instead. A product needs a primary first.
- Setting a row again replaces it. `action_log` keeps the values it replaced and your name.

The command refuses anything the purchase API would refuse (an unknown store, an Amazon row
without an Amazon link, a pack outside 1-60, a max under the price or over $200) and any SKU
that isn't in our copy of the catalog.

## Seeing and removing

```
python -m vending.sourcing                                     # every row, its unit price and its age
python -m vending.sourcing remove SKU [--alternate] --by NAME
```

A price checked more than **7 days** ago is marked `STALE`. Prices move; check before a big order.

## Loading many at once

```
python -m vending.sourcing import db/sourcing/2026-11-11-amazon.csv --by NAME
```

Columns: `sku, rank, store, units_per_pack, price, max, url, pack_description, checked, note`.
An import is all or nothing: one bad line and nothing is stored, with every bad line listed.
`db/sourcing/2026-11-11-amazon.csv` is the team's 11 Nov Amazon list for the five products
already in the catalog.
`db/sourcing/2026-11-15-catalog.csv` adds 18 more catalog products (20 rows, with two alternates), from the
15 Nov price checks and the rest of the 11 Nov list. Load it after the first file:

```
python -m vending.sourcing import db/sourcing/2026-11-15-catalog.csv --by NAME
```

Together they cover 23 of the 34 catalog products. **Still without a price:** the six bubly 12 oz cans
(no single-flavour link checked), Mountain Dew Baja Blast 20 oz (no Amazon offer on 15 Nov) and the four Pure
Leaf 16.9 oz bottles (no price at an allowed store). Walmart pages blocked the 15 Nov checks, so Walmart
prices aren't in either file. Coke and Pepsi 12 oz cans are cheapest at BJ's, which needs a membership; each
has an Amazon alternate. A few rows note that their price came from search results, or that stock was low.

## What reads it

Nothing yet. Next:

- `preview_desired_state` fills in a purchase line from this table when the agent gives no
  sourcing of its own, and warns on a stale price (`vending.sourcing.table`, the planner's
  `sourcing_table`).
- `get_catalog` shows each product's stores and prices, and our last real cost from receipts.
