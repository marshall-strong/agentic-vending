# Sourcing: where to buy each drink, and for how much

Every purchase line needs a store, a pack size, the most the worker may pay per pack and, for
Amazon, a link. The catalog doesn't carry any of that (David, 19 Oct: "cost comes from the store
and the receipt"), so we keep it in our own table, `sourcing_options` (migration 015). People
look prices up and record them; **the agent has no tool that browses stores**, by the team's
choice in [`planning/tool-list.md`](planning/tool-list.md). After each fill, prices also refresh
from what the worker paid (see [Prices from receipts](#prices-from-receipts)).

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
prices aren't in either file. Coke 12 oz cans are cheapest at BJ's ($0.56 a can against $0.64 on Amazon), which
needs a membership and sends about 27 cans to the closet per pack, so Amazon is its alternate. Pepsi 12 oz cans
cost about the same either way ($0.58), so Amazon is the primary and BJ's the alternate.

**Re-checking prices.** Re-check one product with `python -m vending.sourcing set`. To re-check many, add a
new dated file. A product may appear in a later file only with a later `checked` date, and importing files in
date order keeps the newest price. Every row from 15 Nov will be `STALE` by the day-one order (~27 Nov), so
re-check prices around 25 Nov. A few rows note that their price came from search results, or that stock was low.

## Prices from receipts

After each fill, the hourly `python -m vending.sync_requests` reads the worker's receipt and
refreshes our prices from what was really paid (#202, `vending/receipt_prices.py`). No person and
no web access is needed, and the price is never older than our last purchase of it.

- **The rule.** The pack price is the line's `total_paid` over its `packs_bought`, rounded half up
  to the cent. Purchases are tax-exempt and the bottle deposit is not a concern (David, 12 Nov), so
  that is the shelf price. Lines of one request for the same drink, store and pack size are added
  together first.
- **Which row.** Every row with the same SKU, store and pack size, primary or alternate. Its price
  and `checked` date change: the date is when the worker resolved the line. `checked_by` becomes
  `receipt`, and the note names the request. The store, pack, link, description and max stay.
  `action_log` keeps the values it replaced, as for `set`.
- **A newer price wins.** A row checked after the receipt keeps its price. Reading the same
  receipt again changes nothing. A corrected receipt replaces the price it set.
- **Skipped, and the reason logged:** a line loaded from the closet or not bought, a
  substitution (the receipt's SKU isn't the one we asked for), a missing amount or pack size,
  zero packs, units that don't add up, a pack price above the row's max (a person raises the max),
  and a drink bought at a store or pack size the table has no row for. The sync logs the reasons
  when it first finds the request fulfilled, and again when a corrected receipt changes its lines.
  A re-read that changes nothing logs nothing.
- **An amount that can't be real** (a billion dollars or more, or not a finite number) is stored
  as unknown, with a warning in the sync log. The rest of the receipt is kept, so the fill still
  counts for stock. That line gives no price.
- `python -m vending.receipt_prices` applies every receipt already stored (add `--dry-run` to
  see what would change). The sync runs it for each fulfilled request on its own.

`python -m vending.sourcing` shows such a row as `from receipt ...`, and the dashboard's Pricing
panel labels a cost taken from it **expected · receipt**. Nobody may set `--by receipt` by hand.

**What still needs a person.** A receipt only prices what we already bought, at a store and pack
size the table already has. These still need `python -m vending.sourcing set`:

- a drink we have never bought (a new catalog product, the wildcard, a swap);
- a new store or pack size for a drink;
- a max the worker's price has passed;
- a drink we haven't bought for over 7 days: its receipt price goes `STALE` like any other.

## What reads it

- **`preview_desired_state` and `submit_desired_state`.** A product the agent gives no
  `sourcing` for is bought the way this table says: its primary, and its alternate if it has
  one. When the agent does give sourcing, its store, pack and max go into the request, but if it
  gives no `expected_price_per_pack`, the plan's cost uses the expected price of the table's row
  for the same store and pack, when there is one. A max below that borrowed price is refused
  `bad_sourcing`, and the message says the price came from the table. Any table price the plan
  uses, either way, that was checked more than 7 days ago is named in the plan's `warnings`, with
  the command to update it. A product
  with no sourcing given or in the table is refused `no_sourcing`, as before.
- **The planner treats a receipt price like any other.** It is a checked price, dated by the
  receipt, so it is `STALE` only 7 days after the fill.
- **Next:** `get_catalog` shows each product's stores and prices, and our last real cost from
  receipts.
