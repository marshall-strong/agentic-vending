# Wildcard slot

One fixed slot tests products for the other 19 and may sell up to $8.00; it never moves, holds a product no other slot holds, and one purchase for it costs at most $82.00.

**Why** (#100; #110, item 1): a new product earns a regular slot by selling well in the wildcard first.

**Pick it once.** If `get_machine_state` says no wildcard slot is set, pick one with `set_wildcard_slot` and say why: one of ours on a middle shelf (2 to 4), not column 5 (beside the centre line), in a column taking cans and bottles. Only a person can change it. Until it is set, no slot goes above $6.50.

**Each Friday plan**, read `wildcard_trial` in `get_machine_state`:

- **too early**: under 5 in-stock class days seen. Keep it.
- **pass**: at least 4 sold over at least 5 in-stock class days. If there is a `proposal`, do it in this Friday's plan: switch `into_slot` to the product at a price you pick, at most $6.50, and switch the wildcard to the next product. With no proposal, keep selling. Once it had 5 days by an earlier Friday, request or not, it must keep 0.7 a day, and 7 by 10 days.
- **extend**: 3 sold. Keep it one more Friday; by 10 days it needs 7.
- **drop** (0 to 2 sold, or under 7 by 10 days) or **promoted** (another slot holds its product): switch the wildcard to the next product.
- **price test**: priced above $6.50, so it can't be promoted.
- **unknown**: don't promote. The reason says what to fix: a slot with no receipt, an unsettled counter, counts that disagree, or, for a pass, no price in 6 hours.

**What counts.** Only what the polls saw: no gap over 6 hours, nor the poll window holding the load. Profit is information only.

**Silent slots** (`silent_slots`). A promotion goes only into a regular slot that:

- sold nothing since the last Friday visit, one product throughout, proven by its counter: read at or before that Friday's midnight and within 6 hours of now, with no restart or gap over 6 hours;
- was watched for 4 in-stock class days, Monday to Thursday, each to 6 PM (so run Thursday evening);
- has stock confirmed: that visit's own receipt loaded it, or a count since its last load names its SKU and its receipted units. A disagreeing count makes it unknown.

Never into a slot that sold. `unconfirmed_slots` may be empty: ask for a count before Friday.

**Price it at or under $6.50** so the result carries over; $8.00 only for a deliberate price test. No loss leaders. Prices change only with a load.

**Preview and submit check:**

- Only the wildcard may go above $6.50, up to $8.00.
- Its own purchase costs at most $82.00 at the worker's maximum, alternate and closet extras included.
- No other slot switches to its product, except a promotion. It doesn't switch to a product another slot holds, or while the plan keeps a slot with no receipts, and doesn't load more of its product while another slot holds it. While no receipt says what it holds, no other slot loads unless the plan sets its product.
- Naming a product for every slot with no receipts, as the day-one fill does, passes with a warning: an unavailable line can leave the old product, so check receipts before trusting the trial.
- A promotion needs a pass, a silent slot, a Friday visit, and the wildcard switching in the same plan, one slot at a time.
