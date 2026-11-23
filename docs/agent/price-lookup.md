# Price lookup

From the start, a live run may look up one pack's price with find_pack_price: a capped web search on one store's own site, for a drink with no checked price or a stale one.

**When to use it.** Only while planning a restock, for a drink in your plan whose sourcing row has no checked price, or one checked more than 7 days ago (the preview's `no_sourcing` refusal, or a warning that names a stale price). Never to browse, compare stores for fun, or check a price that is still fresh: a fresh one is refused (`fresh_price`) and costs nothing. Receipts refresh the prices of drinks we bought at the last fill, so most weeks need no lookup.

**What it does.** Give the SKU, the store and the pack size (`units_per_pack`). Up to 2 searches on the store's own site. The code checks the link is exactly one the search returned and is that store's product page, the pack is the one you asked for, and the price per unit is within half and twice the price we already have. Then it stores it as found by the agent, with the link and the time. A row for the same drink, store and pack gets the new price and keeps its max. A new row gets a max of the price plus 25%, to the cent; the worker never pays more than the max.

**Unchecked.** With no price for the drink to compare against, the price is stored without a checked price (the reply says `"checked": false`): the fill refuses it (`no_checked_price`) until a person confirms it. Say so in the update.

**Stores.** Only Amazon, BJs, Costco and Walmart, the stores the worker buys from (`restocking`, `guardrails`). Any other store is refused before a search.

**Never.** No prices from vending machines, the Interloper (`interloper`), the other team, or any seller that isn't the store itself (`competition-and-integrity`). The search can't reach them, and the code refuses a page off the store's site.

**Limits.** At most 4 searches a run (every search tried counts) and $1.00 a day for all lookups, inside the run's model caps. Each lookup reserves its worst case, about $0.41, before it runs, so the day's reservations never pass $1.00. A lookup that reads very long pages can bill more than that; then no more lookups run that day, so a day can pass $1.00 by that one excess. Each lookup's cost is in the reply and in the books, like any model call. When a lookup is refused for its cap, plan with the prices you have and name the drink left unpriced in the update.

**When it fails.**

A refusal gives only a code and a fixed message, never what the page or the search said.

- `not_found`, `pack_mismatch`: no matching listing for that pack. Try another store, or plan without the drink.
- `over_max`: the price is above the row's max; a person decides. Plan with the stored price or another drink.
- `implausible_price`: too far from the price we have; plan with that price.
- `over_ceiling`: at its max, the slot couldn't be priced under the ceiling; nothing was stored.
- `changed_meanwhile`: someone changed the row while the search ran; read the table again.
- `off_store_source`, `unsourced_url`, `not_a_product_page`, `bad_report`: the search gave nothing we can use; nothing was stored.
- `halted`: stop all writes and report it.

Say in the update which prices you looked up and what they cost.
