# Wildcard slot

One fixed slot tests products for the other 19 and may sell up to $8.00; it never moves, holds a product no other slot holds, and one purchase for it costs at most $82.00.

**Why** (#100; #110, item 1): a new product earns a regular slot by selling well in the wildcard first.

**Pick it once.** If `get_machine_state` says no wildcard slot is set, pick one with `set_wildcard_slot` and say why. Pick an ordinary spot: one of ours on a middle shelf (2 to 4), not column 5 (beside the machine's centre line), in a column that takes cans and bottles. It never moves; only a person can change it. Until it is set, no slot goes above $6.50.

**Each Friday plan**, read `wildcard_trial` in `get_machine_state`:

- **too early**: under 5 in-stock class days seen. Keep it.
- **pass**: at least 4 sold over at least 5 in-stock class days. If there is a `proposal`, do it in this Friday's plan: switch `into_slot` to the product at a price you pick, at most $6.50, and switch the wildcard to the next product to test. With no proposal, no slot is silent: keep it and keep selling. While it waits it must keep 0.7 a day, and 7 by 10 days.
- **extend**: 3 sold. Keep it one more Friday cycle; by 10 days it needs 7.
- **drop**: 0 to 2 sold, or under 7 by 10 days. Switch the wildcard to the next product.
- **promoted**: another slot holds its product now. Switch the wildcard to the next product.
- **price test**: priced above $6.50, so it can't be promoted.
- **unknown**: don't promote; the reason says what to fix.

**What counts.** Only units and in-stock class days the polls saw: a gap over 6 hours between polls counts for nothing, and neither does the poll window holding the load. If the counters and receipts disagree, the verdict is unknown. Profit is shown for information only.

**Silent slots** (`silent_slots`). A promotion goes only into a regular slot that:

- sold nothing since the last Friday visit, with one product all that time;
- was watched for 4 in-stock class days, Monday to Thursday, each to 6 PM (so the Thursday run is an evening run);
- has stock confirmed: our receipt loaded it at that visit, or a person counted units in it since.

Never promote into a slot that sold. `unconfirmed_slots` sold nothing but may be empty: ask for a count before Friday. The proposal uses the best silent slot (quiet longest, then lowest margin).

**Price it at or under $6.50** so the result carries over. Use $8.00 only for a deliberate price test, and say so. No loss leaders. A price changes only with a load.

**Preview and submit check:**

- Only the wildcard may go above $6.50, up to $8.00.
- Its own purchase costs at most $82.00 at the worker's maximum, alternate and closet extras included.
- No other slot switches to its product, except a promotion. It doesn't switch to a product another slot holds now, or while any slot has no receipts, and doesn't load more of its product while another slot holds it.
- A promotion needs a pass, a silent slot, a Friday visit, and the wildcard switching in the same plan, one slot at a time.
