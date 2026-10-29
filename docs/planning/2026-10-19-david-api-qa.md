# API Q&A with David — decisions and answers

Monday 19 October 2026, about 09:40-10:31, David Robinson with Marshall Strong and Kevin
Sykes. Extracted from the meeting transcript (kept in the team's shared folder, not in this
repo), which was made from two recordings and cross-checked between them.

**David is still building the DEX Bridge API, so these answers outrank the live spec.** As of
12:01 the same day the spec was unchanged (`0.1.0`, identical to the 5 October copy); nothing
below is deployed yet unless it says so. [`tool-list.md`](tool-list.md) has been redesigned
around them.

## The big changes

 - **The purchase request is becoming a "desired state" for our slots.** David wants each request to describe what our slots should look like after the next restock: per slot, no change, restock, reposition a product, or a new price. He's still thinking it through. It folds price changes and product moves into one request, and the worker sees one combined display for both teams.
 - **Each purchase line will carry the store, a max price per pack and the sale price.** Store is an enum (Walmart, Amazon, BJ's; Costco was also mentioned), plus a product link for Amazon. The max price is set per pack for the primary and for the alternate. The worker buys if the price is at or under the max, and won't wait for the agent to re-price after buying.
 - **Catalog products become single items, not packs.** A product is, for example, "Coca-Cola, default flavor, 20 oz"; the pack size (6, 12, 24) is chosen on the purchase line. The catalog has no prices.
 - **Price changes may become free and remote.** David thinks the bridge can set machine prices with a DEX statement, which would mean no worker visit and no $25. He'd probably limit it to about once a day and remove the black price tags (the display shows the price). This depends on the Cantaloupe card reader reading prices live rather than only at power-up, which is unverified.
 - **There is no shared log database for us to write to.** The instructors keep their own database, mirroring every request and worker entry, as the source of truth, and will build a story of the competition at the end. We keep our own database. A soft-check endpoint is coming that says whether our numbers are accurate, "wildly off", or in between, never the exact figures.
 - **Graham and David cover our Azure hosting.** We host our own database there: Postgres 17.

## Answers to our questions

Numbers match the question list in the team's plan doc.

| # | Question | Answer | Status |
| --- | --- | --- | --- |
| 1 | When will the shared log schema arrive? | There isn't one for us. The instructors keep their own database as the source of truth and will build the competition story themselves. We are responsible for our own database. | Answered; conflicts with brief section 3.5 (see open items) |
| 2 | How does a price change reach the worker? | On the purchase request: each line will carry the sale price. A separate "change the price of slot X" route is also needed and doesn't exist yet. If DEX price-setting works, the bridge sends prices itself. | Answered, not built |
| 3 | Can we change a price without a restock? | Possibly, via a DEX statement from the bridge: free, likely limited to about once a day. Depends on whether the Cantaloupe card reader reads prices live. If it only reads at power-up, price changes have to happen at a restock (when the machine is power-cycled). | Open, being verified |
| 4 | How do we request a $25 midweek visit? | No endpoint yet; there will be one. | Not built |
| 5 | Will the catalog be filled, and is it shared with group A? | Being restructured: products are single items, packs chosen on the purchase line. Whether it's shared wasn't answered directly; the worker's display combines both teams. | Partly answered |
| 6 | Sales-tax method? | 8%, arithmetic rounding, shelf prices **include** tax, calculated once a week on that week's takings and set aside. David said "multiply by 1.08". **Team decision (19 Oct): set aside takings × 0.08 ÷ 1.08 each week, rounded arithmetically; revisit if David says otherwise.** | Decided |
| 7 | Is Postgres provided? | We host our own in Azure; Graham and David cover the hosting cost. Postgres 17. | Answered |
| 8 | How do we request a catalog product? | Two endpoints coming: submit a product request, and check it (pending, denied, or approved with the new product code). Or just watch the catalog for it to appear. Quick turnaround; they check it's "nothing outlandish." | Answered, not built |
| 9 | Can a catalog request be refused? | Yes, but rarely expected. A purchase request for a product not in the catalog (or inactive) is denied with a message naming the product. | Answered |
| 10 | Will the catalog include prices? | No. The catalog is only which products are approved. Cost comes from the store and the receipt. | Answered |
| 11 | Can one request move a drink from slot A to slot B? | Yes, under the desired-state model: a line can reposition a product into a slot. David is still thinking it through. | Answered in principle |
| 12 | Can we pull a product and leave the slot empty? | Not asked directly. The desired-state model ("no change to this slot") suggests empty slots are expressible. | Open |
| 13 | Will the worker report anything beyond receipts? | Receipts will report the actual per-unit price paid, units loaded per slot versus sent to inventory, and (planned) a spoilage field for damaged items. | Answered, partly planned |
| 14 | Can we ask for a closet count? | Not needed: we track the closet ourselves; damaged items come through the spoilage field. | Answered |
| 15 | Balance-check endpoint? | Coming. A "soft check": send our numbers and get back accurate, wildly off, or in between. Never the exact figures. No tolerance given. | Answered, not built |
| 16 | Unsold stock at the end of the run? | David believes it counts at what we paid for it, and is verifying. A small chance sales tax applies to unsold stock; he doesn't think so. | Probable, to verify |
| 17-20 | Token spend in development; repo visibility; bottle deposit; promotions | Not asked. | Open |

## What it means for our design

 - **Set the sale price against the max buy price, not the expected price.** The worker won't come back for a new price after buying, so each purchase line's sale price must still make money if the purchase hits its max. The price floor becomes max price per unit + tax, rounded up to the next 5 cents.
 - **Replace "submit lines" with "submit a desired state."** The agent decides what each of our 20 slots should hold and charge; deterministic code diffs that against the current state and the closet stock, produces the request lines, and refuses impossible plans before they're sent. This is the brief's restock and pricing directive in one artifact. See the restock planner in [`tool-list.md`](tool-list.md).
 - **Amazon is the low-risk default channel.** Fixed price, wide variety (unusual drinks like Pickle Potion), and no driving for the worker. Orders are due Wednesday for two-day shipping before the Friday restock. Bulk staples are probably cheaper at Costco or BJ's. A late Amazon delivery means a $25 midweek restock or waiting a week.
 - **If DEX price changes work, pricing experiments become cheap.** Daily price changes at no cost would make it practical to vary prices deliberately to learn demand, which the brief raises as the "identification problem."
 - **The ledger uses actual cost from receipts, after spoilage.** If 1 of 12 is damaged, the pack's cost is spread over 11 units.
 - **Our database is our record.** The brief's shared-log deliverable, as David describes it, is satisfied by the instructors mirroring our requests plus our own action log; keep the logging interface thin in case that changes.

## Open items to confirm with David

 - [x] **Sales-tax formula:** team decision 19 Oct: weekly set-aside = takings × 0.08 ÷ 1.08, arithmetic rounding. Revisit if David says otherwise.
 - [ ] **Shared log deliverable:** the brief (section 3.5 and the deliverables checklist) says writing to an institute shared log is mandatory and graded; David says there isn't one for us. Confirm the checklist item is dropped or what replaces it.
 - [ ] **Ordering cutoff:** still open. The only definite is that restocks happen Friday afternoon. David floated Friday noon for in-store purchasing, the 21 Sep meeting said Thursday midnight, and Amazon orders are due Wednesday for two-day shipping.
 - [ ] **Store list:** three enums (Walmart, Amazon, BJ's), or four with Costco?
 - [ ] **DEX price changes:** whether the Cantaloupe reader picks up price changes without a power cycle.
 - [ ] **Unsold inventory:** valued at cost (David verifying).
 - [ ] **Catalog:** shared with group A or separate?
 - [ ] **Empty slots:** can the desired state leave a slot empty?
 - [x] **Hosting cost:** Graham and David cover it.
 - [ ] Not yet asked: token spend during development, repo visibility, bottle deposit, promotions.
