# Sourcing: where to buy each drink, and for how much

Every purchase line needs a store, a pack size, the most the worker may pay per pack and, for
Amazon, a link. The catalog doesn't carry any of that (David, 19 Oct: "cost comes from the store
and the receipt"), so we keep it in our own table, `sourcing_options` (migration 015). Before the
competition, people look prices up and record them. After each fill, prices also refresh from
what the worker paid (see [Prices from receipts](#prices-from-receipts)). From the start (3 Dec),
the live agent may also look up one price itself with a capped web search on a store's own site
(see [Prices the agent finds](#prices-the-agent-finds)). It has no other way to browse.

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
- **Which row.** Every row with the same SKU, store and pack size, primary or alternate. For
  Amazon the listing must match too: the link the request asked for and the row's link name the
  same ASIN. The ASIN is read only from a product page path (`/dp/ASIN`, `/gp/product/ASIN`) on an
  amazon.com address. Any other link must be exactly the row's link. So a receipt from an old
  listing never prices the listing that replaced it. A request whose Amazon link isn't text gives
  no price for that line, and the sync goes on. Its price and `checked` date change: the date is when the worker
  resolved the line. `checked_by` becomes `receipt`, and the note names the request. The store,
  pack, link, description and max stay. `action_log` keeps the values it replaced, as for `set`.
- **A newer price wins.** A row checked after the receipt keeps its price. Reading the same
  receipt again changes nothing.
- **Corrections.** A corrected receipt (a newer revision from the bridge) replaces the price it
  set, but only while its line is still the newest price for the row. If the correction moves the
  line's time back, the newest of three wins: the correction, the row's last hand check, and the
  price any other fulfilled receipt gives that row (within its max). A hand check wins a tie.
- **A fill is never undone automatically.** The bridge describes rejection only before a request
  is filled. So a rejected answer for a request we hold as fulfilled, with or without a newer
  `updated_at`, is not stored. The receipt, its stock and its prices stay. The sync logs one
  warning for each such answer, telling a person to check with the instructors. The answer's own
  `status` decides: a rejected answer that carries an empty `lines` list is still a rejection. A
  fulfilled answer that lists no lines never replaces the stored lines. A fulfilled answer with no
  `updated_at` (or an older one), after we hold a receipt that has one, is ignored the same way,
  with one warning.
- **Same `updated_at`, different answer.** A fulfilled answer with the same `updated_at` as the
  stored receipt is the same revision. It is stored again if it is the same answer, or the same answer
  with more `later_losses` (the bridge adds those to a filled request, and its spec doesn't say
  they change `updated_at`). Any other difference is kept out with one warning: we can't tell
  which answer is newer, so the stored one stays and a person checks it against the bridge.
- **The correction rule.** Once we hold a fulfilled receipt, a newer fulfilled answer replaces it
  only if it has exactly the same lines and each line's stored later losses start that line's new
  list. A line is its line number, slot, source and the choices we asked for, and the lines must
  also match the slots and sources of the request we sent. So a correction may change amounts,
  prices and counts, add later losses, and change the door times. An answer that drops, adds or
  changes a line (reason `lines_changed`), or drops or changes a later loss (`losses_shrank`), is
  kept out with one warning. Its stock and prices stay, so an answer that leaves out one of two
  lines can't erase that line's units or the price it set.
- **Each answer kept out is recorded once** in `action_log` (tool `purchase_requests.ignored`),
  with the reason and the warning itself, so the warning is on record even if the sync's own log
  is lost.
- **A malformed answer** (a time that is a number or outside 2020-2099, door times included, `lines` that isn't a list, a
  receipt that isn't an object, a count that isn't a whole number) fails that request only, with its reason logged.
  The stored receipt stays and the sync reads the next request.
- **A price a correction stops giving** (the line became unavailable or a substitution) is not
  taken back. The row keeps it, and the sync says so once, so a person can check it.
- **Locks.** While a receipt is applied, its request is locked, so a newer revision of that
  receipt waits and is applied after. All the sourcing rows of the receipt's drinks are locked
  at once, in one fixed order, so two receipts that list the same drinks in a different order
  wait for each other instead of deadlocking. `python -m vending.sourcing import` locks every row
  of its file's drinks the same way before it writes any, so an import and a receipt wait for each
  other too. Each drink also takes its own lock first, in the same order, so two imports adding the
  same new rows in a different order wait too. A change a person makes at the same time is kept.
- **Skipped, and the reason logged:** a line loaded from the closet or not bought, a
  substitution (the receipt's SKU isn't the one we asked for), a missing amount or pack size,
  zero packs, units that don't add up, a pack price above the row's max (a person raises the max),
  and a drink bought at a store or pack size the table has no row for. The sync logs each reason
  once per revision of the receipt (its `updated_at` and lines, so a revision that brings a reason
  back names it again), the first time it prices that revision, and records it in
  `action_log` (tool `receipt_prices.skipped`) with the pricing. A pricing that fails (a lock
  timeout, say) records nothing, so the next read names the reason.
- **An amount that can't be real** (a billion dollars or more, more than 10 decimal places, or
  not a finite number) is stored as unknown, with a warning in the sync log once per revision of
  the receipt. The rest of the receipt is kept, so the fill still counts for stock. That line
  gives no price. A value the database refuses fails that request only; the sync goes on.
- `python -m vending.receipt_prices` applies every receipt already stored (add `--dry-run` to
  see what would change; it plays each receipt on the rows as the ones before it left them). The sync runs it for each fulfilled request on its own.

`python -m vending.sourcing` shows such a row as `from receipt ...`, and the dashboard's Pricing
panel labels a cost taken from it **expected · receipt**. Nobody may set `--by receipt` by hand.

**What still needs a person.** A receipt only prices what we already bought, at a store and pack
size the table already has. These still need `python -m vending.sourcing set`:

- a drink we have never bought (a new catalog product, the wildcard, a swap);
- a new store or pack size for a drink;
- a max the worker's price has passed;
- a drink we haven't bought for over 7 days: its receipt price goes `STALE` like any other.

From the start, the live agent can cover the first and last of these itself (next section). A
max, and a store or pack the table has no row for when both rows are already fresh, still need
a person.

## Prices the agent finds

From the start (3 Dec), a live agent run has one tool of its own, `find_pack_price`
(`vending/price_search.py`, #202 part 2). It looks up one pack's price at one store with
Anthropic's web search tool, and records what it found here. Before the start, people price
drinks as above, as with catalog requests. The agent's topic is `docs/agent/price-lookup.md`.

- **When.** Only before a restock, for a drink in its plan with no checked price or one older than
  7 days. A lookup for a price still fresh at that store is refused (`fresh_price`) before
  anything is spent.
- **Where.** Only Amazon, BJs, Costco and Walmart, the stores the purchase API accepts. The search
  is limited (`allowed_domains`) to that store's own site: `amazon.com`, `bjs.com`, `costco.com`
  or `walmart.com`. So it can't read the other team's dashboard or repo, the Interloper or any
  other seller.
- **Checked in code.** The lookup names its pack size. The cited link must be exactly one the
  search returned (only the case of the scheme and host and a trailing slash may differ), and the
  stored link is the search result's, not the model's text. It must be on the store's own domain
  or its `www.` host, with no port, and be the store's product page: Amazon `/dp/ASIN` or
  `/gp/product/ASIN`, Walmart `/ip/...`, Costco `....product.....html`, BJs `/product/...`. The
  pack must be the one asked for, the price at most $200 a pack and $10 a unit, and the price per
  unit within half and twice the price the table already has for the drink (the row it refreshes,
  else the primary, else the alternate). A refusal tells the agent only a code and a fixed message
  (`price_search.REASONS`); `action_log` keeps the whole report.
- **Stored.** A row for the same drink, store and pack (for Amazon, the same listing) gets the new
  price, dated when it was written, `checked_by` `agent-search`, and a note naming the page. Its max
  stays; a price above it is not stored (`over_max`), and a person decides. With no such row, it
  becomes the primary if the drink has none, else the alternate if that is missing, unpriced or
  stale. A person's fresh rows are never replaced. A new row's max is the price plus 25%, to the
  cent (`price_search.agent_max`: $27.48 gets $34.35); a new row whose max would put the slot's
  floor above the price ceiling is not stored (`over_ceiling`). A row someone changed while the
  search ran is left alone (`changed_meanwhile`). `action_log` keeps the change, as for `set`.
- **Checked, or a candidate.** A price that passed the per-unit check clears the planner's and the
  fill's checked-price gates (`no_checked_price`, `stale_price`) like a person's, and goes `STALE`
  after 7 days. With no price to check it against, it is stored as an **unchecked candidate**: no
  expected price, the found price in its note, so the fill refuses it until a person sets it. The
  max is the margin for a search being a little off: the worker never pays more than the max. The
  next receipt replaces it. This is the default for the team to review on #202.
- **Caps.** `price_search_searches_per_run` (default and most 4) searches a run, every search tried
  counted, at most 2 a lookup, and `price_search_daily_cap_cents` (default and most 100, $1.00) an
  Eastern day for all lookups, inside the model caps (`model_run_cap_cents`,
  `model_daily_cap_cents`). Set them lower with `python -m vending.config set`;
  `price_search_searches_per_run 0` turns the tool off. Each lookup reserves its worst case (the
  fee for its searches, its output and 25,000 tokens of results per search, about $0.41) under the
  harness's lock, in the same transaction that reads the day's lookup spend, on the reservation's
  Eastern day; one that doesn't fit is refused before anything is sent. So reservations never pass
  $1.00. Billed spend can: the API doesn't bound a result's tokens, so a lookup can bill more than
  its worst case. The harness records what it billed, and no further lookup runs that Eastern day:
  **a day's lookups bill at most $1.00 plus one lookup's excess** (assumption A27).
- **Spend.** Each lookup is one `model_calls` row labelled `price_search SKU STORE`, its cost the
  tokens plus $0.01 a billed search (`web_search_requests`: Anthropic's price, $10 per 1,000;
  a search that errored isn't billed), and in a live run a ledger
  `tokens` entry, like every model call.

`python -m vending.sourcing` shows such a row as `by the agent (found by the agent with a web
search DATE: URL)`, and the dashboard's Pricing panel labels its cost **expected · agent**. Nobody
may set `--by agent-search` by hand.

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
