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
- An Amazon product link is stored plain, `https://www.amazon.com/dp/<ASIN>`, with no name in the path, no
  query, no `ref=` path and no affiliate tag (`sourcing.plain_link`). That is the link the worker gets.
  Migration 033 cleaned the links stored before, and logged each change (`sourcing.plain_link` in
  `action_log`).

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
Anthropic's web search tool, and records what it found here. The agent's topic is
`docs/agent/price-lookup.md`. People run the same lookup with a command, before the start too
(next section).

- **When.** Only before a restock, for a drink in its plan with no checked price or one older than
  7 days. A lookup for a price still fresh at that store is refused (`fresh_price`) before
  anything is spent. The agent's tool prices one store a call; comparing every store is the
  command's (next section).
- **Where.** Only Amazon, BJs, Costco and Walmart, the stores the purchase API accepts. The search
  is limited (`allowed_domains`) to that store's own site: `amazon.com`, `bjs.com`, `costco.com`
  or `walmart.com`. So it can't read the other team's dashboard or repo, the Interloper or any
  other seller.
- **Checked in code.** The lookup names its pack size. The cited link must be exactly one the
  search returned (only the case of the scheme and host and a trailing slash may differ), and the
  stored link is the search result's, not the model's text. It must be on the store's own domain
  or its `www.` host, with no port, and be the store's product page: Amazon `/dp/ASIN` or
  `/gp/product/ASIN`. Amazon's `us.amazon.com` and `smile.amazon.com` count as its own site. An
  Amazon link is stored plain, `https://www.amazon.com/dp/<ASIN>` (a search once cited one with
  someone else's affiliate tag), but only after the cited link matched a search result. Walmart `/ip/...`, Costco
  `....product.....html`, BJs `/product/...`. The pack must be the one asked for (with
  `--compare`, one the slots can use), the price at most $200 a pack and $10 a unit, and the price per
  unit within half and twice the price the table already has for the drink (the row it refreshes,
  else the primary, else the alternate). A pack the page or search result says is out of stock is
  not stored (`out_of_stock`). A refusal tells the agent only a code and a fixed message
  (`price_search.REASONS`); `action_log` keeps the whole report.
- **Stored.** A row for the same drink, store and pack (for Amazon, the same listing) gets the new
  price, dated when it was written, `checked_by` `agent-search`, and a note naming the page. Its max
  stays; a price above it is not stored (`over_max`), and a person decides. With no such row, it
  becomes the primary if the drink has none, else the alternate if that is missing, unpriced or
  stale. A person's fresh rows are never replaced. A new row's max is the price plus 25%, to the
  cent (`price_search.agent_max`: $27.48 gets $34.35); a new row whose max would put the slot's
  floor above the price ceiling is not stored (`over_ceiling`). A row someone changed while the
  search ran is left alone (`changed_meanwhile`). `action_log` keeps the change, as for `set`.
- **Checked.** Every price stored clears the planner's and the fill's checked-price gates
  (`no_checked_price`, `stale_price`) like a person's, and goes `STALE` after 7 days. That includes
  a drink's first price, with nothing to compare it against: the exact pack, on the store's own
  product page, within $200 a pack and $10 a unit. The max is the margin for a search being a
  little off: the worker never pays more than the max. The next receipt replaces it. This is the
  default for the team to review on #202 and #237.
- **Stock.** The row keeps what the page said: `stock` is `in_stock` or `unknown` (migration 031).
  `python -m vending.sourcing` shows it. The database refuses an `out_of_stock` row with a price.
  A person's row or a receipt's has none.
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

`python -m vending.sourcing` shows such a row as `by the agent (found with a web search DATE:
URL)`, and the dashboard's Pricing panel labels its cost **expected · agent**. Nobody may set
`--by agent-search` by hand.

## Looking prices up with the command

Nobody needs to check a price by hand. This runs the agent's lookup for you:

```
python -m vending.price_search --plan picked --by NAME --dry-run          # list what it would compare
python -m vending.price_search --plan picked --by NAME                    # compare every store, keep the cheapest
python -m vending.price_search --plan picked --max-age 24 --by NAME       # the same, re-pricing anything over 24 hours old
python -m vending.price_search --plan ours --max-age 24 --by NAME         # also the drinks in our slots now (weekly)
python -m vending.price_search SKU --pack 24 --compare --by NAME          # one drink, every store
python -m vending.price_search SKU --store Amazon --pack 24 --by NAME     # one drink, one store at a time
```

- **Order at the lowest price (`--compare`, the default with `--plan`).** For each drink it prices
  every approved store and keeps the cheapest usable pack **per unit** as the primary and the next
  best as the alternate (Kevin, 23 Nov). With only one usable price the alternate is removed.
  Usable means:
  - a pack the slots can use: the units one fill loads into all the drink's slots, bought in at most
    20 packs on one line (`planner.pack_usable`). Extras go to the closet at cost (A5), so a bigger
    pack is fine. The search asks for the lowest price per unit among those packs, so the stores'
    different pack sizes are compared;
  - not out of stock;
  - from a store whose cutoff for the next stocking hasn't passed (the fill's rule,
    `planner.cutoff_deadline`: Amazon Wednesday 23:59 and in-store Thursday 23:59 for a Friday visit,
    the same lead time for another day). **For the Mon 30 Nov stocking that is Amazon Sat 28 Nov 23:59
    and in-store Sun 29 Nov 23:59, so a compare run on Sunday can't pick Amazon.** A store past its
    cutoff isn't searched;
  - every check the one-store lookup makes (below and in the previous section).

  Ties go to the pack the page says is in stock, then the order Amazon, Walmart, Costco, BJs. A
  store with a row still fresh (see `--max-age`) is compared from the table, not searched again. A
  person's fresh row can lose to a cheaper find; `action_log` keeps what it replaced. The pack may
  differ from the pick's, so the fill lists it (`pick_deviation`), and the submit needs
  `--accept-deviations`.
- **Which drinks.** `--plan picked`: each drink in the picked plan whose rows the fill would refuse (no
  row, or a primary or alternate with no checked price or one older than 7 days), plus, with
  `--max-age HOURS`, any with a row checked longer ago than that. `--plan ours` adds the drinks in our
  slots now, from our receipts (`price_search.current_drinks`). `--recheck` looks each drink up at its
  primary store and pack only (a drink with no primary is compared). The weekly check is
  `price_search.check_prices(...)`, the same as `--plan ours --max-age N` (with `--recheck` in the weeks
  between comparisons); see [The weekly price check](#the-weekly-price-check).
  Afterwards the command lists anything the fill would still refuse and exits 1.
- **`--first-usable`** keeps the old way for `--plan picked`: one store at a time, in the row's pack
  size, the next store only when one fails (Amazon, Walmart, Costco, BJs), and the first usable price
  is stored. `--no-fallback` tries one store.
- **The model.** The setting `price_search_model` (`python -m vending.config set price_search_model
  '"claude-haiku-5-5"' --by NAME`) picks the model for the agent's tool and the command; it must be a
  model `vending/model_prices.json` prices. Unset, it is the agent's model. `--model` overrides it for
  one run.
- **A benchmark (`--no-store`).** `--compare --no-store --json FILE` searches every store for every
  drink (fresh rows and cutoffs aside), works out which would win, and saves what each store gave:
  price, pack, stock, link, refusal code and cost. Nothing is written to the sourcing table. Its
  spend is test spend (`model_calls`, label `price_search_bench`, under `model_test_daily_cap_cents`),
  even after the start. `python scripts/compare_price_runs.py a.json b.json [c.json]` lines up two or
  three runs: same listing, same price, the winner and the cost per drink.
- **Stored** exactly as the agent's lookups are: same checks, `checked_by` `agent-search`, the note
  naming you, the halt switch and `action_log`.
- **Cost.** Each run has a hard cap, `--max-usd` (default $5.00, at most $10.00), and at most 160
  searches: a lookup whose worst case doesn't fit is refused before anything is sent, and the rest
  stop. The command prints each lookup's cost, each drink's and the run's total. Before the start
  this is dev spend on the team key (`model_calls`, purpose `test`, under
  `model_test_daily_cap_cents`), not the ledger; from the start it is live spend in the ledger. It
  doesn't use the agent's $1.00 a day. A two-search lookup's worst case is about $0.42 on Opus,
  $0.22 on Sonnet and $0.03 on Haiku, so comparing four stores reserves up to about $1.68, $0.88 and
  $0.12 a drink. The first real run (23 Nov, Opus) billed about $0.10 a lookup.
- It needs `VENDING_ANTHROPIC_API_KEY`. `--dry-run` needs no key and spends nothing.
- **A database timeout doesn't end a run.** Each connection is tried 3 times, 1 and 2 seconds apart. A drink
  that still can't reach the database is skipped (`db_error`) and the run goes on with the next drink.
  The `--json` file is saved however the run ends. (On 23 Nov one Azure Postgres timeout ended a Haiku
  run at drink 9 and lost its results.)

## A backup for every fill line

The worker buys a line's primary, else its alternate, else nothing. A line with no alternate stays empty when its
primary is sold out (the 25 Nov rehearsal, #253). This step gives each line of the picked plan an alternate from
the prices the compare logged. It searches nothing.

```
python -m vending.alternates                       # dry run: what each line would get
python -m vending.alternates --write --by NAME     # store them
```

- **Which price.** The same drink at another store, in the pick's pack size, from `price_lookups` in the last
  `--max-age` hours (default 24, at most 144). The store's cutoff for `--visit` (default Mon 30 Nov) must not have
  passed. Not out of stock, and no later lookup at that store says out of stock. Read by the check model or a
  dearer one, or the check model's own reading when it disagreed on price with a cheaper find of the same listing
  and pack. A cheaper model's unchecked find never counts. Per unit within half and twice the primary's. Cheapest
  per unit wins.
- **Its max.** The price plus 25%, held to the pick's most-we-pay per pack. A price above that is left out.
- **An existing alternate** whose max alone is over the pick's gets its max lowered to the pick's (C4 on 25 Nov).
- **The fill's checks.** Each one is tried on the fill's planner first. It is kept only if the fill gains no
  refusal and no `pick_deviation`, its most-we-pay stays within the $400 cap, and the spendable floor holds out of
  the $600 opening. The fill counts each line at its dearer choice. While the fill is over the cap or the floor,
  only a change that lowers it is kept, and `--write` writes nothing (`over_limit`).
- **Output.** Each line: kept, set, replaced, lowered, or none with each store's reason. Then the lines with a
  backup before and after, and the cap room.
- **Stored** with `sourcing.set_option` in the write gate: `checked_by` `agent-search`, dated when it was looked up,
  the note naming you. A changed pick, or any changed row of the plan's drinks, refuses the write
  (`changed_meanwhile`). `set_option` takes the SKU lock every writer takes, so a `sourcing set` at the same time
  waits.
- **When.** After the go-live re-price (`price_search --plan picked --compare --max-age 24`), before the fill preview.

## The price memory

Weekly price checks are worth the spend, and should spend as little as they can (Kevin, 23 Nov). So
every lookup is remembered, and the next run uses what it learned. The agent's tool and the command
both do this.

- **Every lookup is logged** in `price_lookups` (migration 032): the drink, the store, the packs asked
  for, what the page showed (pack, price, link, stock), the outcome (`found`, `not_found`,
  `out_of_stock`, `check_disagrees` or another refusal code), the model, the cost, the time and who ran
  it. `--no-store` runs are logged too, marked as search only. See a drink's latest lookups with
  `python -m vending.price_search SKU --history`.
- **Known dead ends are skipped.** A store whose open search found no listing for a drink, in the packs
  asked for, is skipped for 21 days (`price_dead_end_days`; 0 turns it off): "skipped: not carried,
  checked DATE". A later find there in those packs ends it. The agent's tool says `not_carried`.
- **Known listings are re-checked.** When a row at that store has the product's link, the lookup asks for
  that exact listing (the link, and the ASIN for Amazon) with one search. If that gives no usable price,
  it searches the store as usual. With `--compare` this means a store we already buy from is re-checked
  on its listing; another pack size there is found only with `--full`.
- **Receipts first.** With `--plan`, a drink a fulfilled receipt priced within `--max-age` (else 7 days)
  needs no web lookup: "priced from a receipt on DATE; no lookup".
- **A better model checks a cheaper model's find.** When the lookup model is cheaper than
  `price_check_model` (default `claude-sonnet-5-5`), a find that is the first price for that drink, store
  and pack, or more than `price_check_percent` (default 5) from the stored one, gets one check before it
  is stored: the check model re-reads that listing with one search. It is stored, at the check's price,
  only when both name the same listing and pack and agree within 2%. Otherwise nothing is stored from
  that store (`check_disagrees`). With no row for that store and pack, the stored price is the newest one a
  check confirmed (or the check model found) in the last 28 days (`confirmed_price`), so a store whose price lost
  the comparison isn't checked again every week. A cheap model's unchecked find never counts. A find so far off
  that it would be refused anyway (`implausible_price`) isn't checked. Nothing is checked in a `--no-store` run. Why: in the 23 Nov benchmark Haiku priced 19
  of 20 drinks for $1.69 against Sonnet's 16 for $4.86, but misread a few prices (Celsius Arctic, Celsius
  Kiwi, Snapple Kiwi, and Red Bull Yellow at the Original's price).
- **`--full`** ignores the dead ends, the listing re-checks and receipts first: it searches every store openly. The check still applies.

## The weekly price check

The worker checks prices once a week by itself (`vending/price_schedule.py`), so they are fresh before
Wednesday's restock run. It is off until both `PRICE_CHECK_MODE` on the container and `price_check_mode` in
the database turn it on (`off`, `dry-run` or `live`; the lower runs). [`docs/deploy.md`](deploy.md) shows how.

- **When.** `PRICE_CHECK_SCHEDULE`, default `tue 12:00-20:00` (Eastern), from `PRICE_CHECK_FROM` (default
  2026-12-01). One row a time in `price_check_runs` (migration 034), so it runs once whatever restarts.
- **What.** The drinks in our slots now and in the picked plan, each one whose price is older than 5 days
  (`MAX_AGE_HOURS`, 120). A drink a receipt priced in that time is skipped. Every `price_compare_every_days`
  (default 14) it compares every store; in the weeks between it re-checks each drink's primary store only.
  The price memory applies throughout: dead ends, listing re-checks, and the check by `price_check_model`.
- **The model.** The `price_search_model` setting. The cheap way is Haiku with the Sonnet check:
  `python -m vending.config set price_search_model claude-haiku-5-5 --by NAME`. That setting also moves the
  agent's own `find_pack_price` to Haiku, with the same check.
- **Money.** Its own cap, `price_check_weekly_cap_cents` (default 300, at most 1000), over the 7 days before
  a run, read from its `model_calls` (label `price_check`). A run's cap is what is left; with under $0.10
  left it is `skipped`. It never uses the agent's $1.00 a day. Expected: about $0.20 to $0.45 a re-check
  week and $1.00 to $1.50 a compare week on Haiku with the Sonnet check; the first compare, with an empty
  memory, can reach the $3.00 cap (estimates from the 23 Nov benchmark).
- **Stops.** The halt switch: a time due while halted is `halted`. `price_check_mode off` stops the next one.
- **Seeing it.** `python -m vending.price_schedule` shows the settings, the week's spend and the latest runs:
  cost, drinks looked up, prices changed, drinks without a usable price. `/health` shows the last run as
  `price_check`. The dashboard's Needs attention shows the last run when it ended `partial` (a drink got no
  usable price), `failed`, `halted` or `missed`, and its Price checks panel lists the last 8 runs and each plan
  drink's prices by store ([`dashboard.md`](dashboard.md#what-it-shows)).

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
