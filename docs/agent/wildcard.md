# Wildcard slot

One slot tests products for the other 19 and may sell up to $8.00; it moves only at a Friday visit, holds a product no other slot holds, and one purchase for it costs at most $82.00.

**Why** (#100; #110, item 1): a new product earns a regular slot there. Name it in every plan's `wildcard`, with a reason in `slot_reasons`.

**When it changes.** A Friday plan's choice takes effect once fulfilled. Visits are ordered by `door_closed_at` where that column exists (ours lacks it), else `fulfilled_at`, which may trail the visit; with neither, the wildcard is unknown and preview and submit refuse. Naming none on Friday ends the trial. A slot no longer ours is no wildcard.

**The trial** (`get_machine_state`, `wildcard_trial`): profit per in-stock class day (revenue without tax, less cost of sales) against the weakest other slot's, with a verdict.

- **Only watched intervals count.** Money and days share poll intervals; if the counters and stock replay disagree, or a cost is unknown, so is the profit. Left out, days and sales: the interval holding the load, and any over 6 hours (`MAX_POLL_GAP`). A class day counts in the interval spanning its Eastern midnight.
- **Price it at or under $6.50** so the result carries over; above, it is a price test. The latest priced poll decides (unpriced polls change nothing): a strictly later poll at or under $6.50 ends the test, and the trial counts from it. If polls at that moment disagree, price and verdict are unknown.
- **It counts** from the later of the load and the naming.
- **Judge it at the first Friday visit after 5 in-stock class days**, against slots with 5 class days the polls saw both stocked.
- **Promote** if it beats the weakest: it stays as a regular slot, the weakest's product goes to the closet, and that slot becomes the wildcard. **Drop** if not: test the next product.

**$8.00** is for a deliberate price test; say so. No loss leaders.

**Preview and submit check:**

- **Above $6.50** means the latest poll that read its price read it above (or disagreed) and no strictly later poll read it at or under. Every plan names it, even one lowering it (that line may come back unavailable). So lower it, then name a new one on a later Friday, after a poll reads it at or under. A slot goes above only when no other is and every price is known.
- **Midweek** plans name the wildcard in effect (the latest fulfilled Friday request's if ours, else the only slot above $6.50) and raise nothing above $6.50.
- **A new wildcard** (another slot, a product switch, or going above $6.50) needs receipts for every other slot, and a product no other slot holds now.
- **Protected products**: the named wildcard's, any slot above $6.50's, and what the wildcard in effect holds now while the plan keeps it, even if switching. No other slot may switch to one; while a non-owner holds one, nobody buys it; buying one is capped at $82.00 at the worker's maximum, alternate and closet extras included.
- **Unknown price** (latest poll read none, or disagreed): that slot's product goes into no other slot, and buying it is capped.
- **Unknown contents** (no receipts or unnamed stock) in a slot above $6.50, in effect, named, or of unknown price: it may hold anything, so every purchase is capped and no other slot switches product.

**Limit:** prices change only with a load, so a full slot above $6.50 can't be lowered until it sells one.
