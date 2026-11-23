# Price lookup

From the start, a live run may look up one pack's price with find_pack_price: a capped web search on one store's own site, for a drink with no checked price or a stale one.

**When to use it.** Only while planning a restock, for a drink in your plan whose sourcing row has no checked price, or one checked more than 7 days ago (the preview's `no_sourcing` refusal, or a warning that names a stale price). Never to browse, compare stores for fun, or check a price that is still fresh: a fresh one is refused (`fresh_price`) and costs nothing. Receipts refresh the prices of drinks we bought at the last fill, so most weeks need no lookup.

**What it does.** One request: up to 2 searches limited to the store's own site, then the page's price, pack size and link. The code checks the page is on that store's site and is one the search returned, then stores it in our sourcing table as found by the agent, with the link and the time. A row for the same drink, store and pack gets the new price and keeps its max. A new row gets a max of the price plus 25%, rounded up to 50 cents; the worker never pays more than the max.

**Stores.** Only Amazon, BJs, Costco and Walmart, the stores the worker buys from (`restocking`, `guardrails`). Any other store is refused before a search.

**Never.** No prices from vending machines, the Interloper (`interloper`), the other team, or any seller that isn't the store itself (`competition-and-integrity`). The search can't reach them, and the code refuses a page off the store's site.

**Limits.** At most 4 searches a run and $1.00 a day for all lookups, inside the run's model caps. Each lookup's cost is in the reply and in the books, like any model call. When a lookup is refused for its cap, plan with the prices you have and name the drink left unpriced in the update.

**When it fails.**

- `not_found`: no matching listing. Try another store, or plan without the drink.
- `over_max`: the price is above the row's max; a person decides. Plan with the stored price or another drink.
- `off_store_source`, `unsourced_url`, `bad_report`: the search gave nothing we can use; nothing was stored.
- `halted`: stop all writes and report it.

Say in the update which prices you looked up and what they cost.
