# Restocking

One free restock every Friday; an extra dispatch costs $25 per request (not per unit), may take longer than 24 hours, and should be batched.

**The rules** (Project Brief (Revised), section 3.4):

- **Friday is free.** Operations loads whatever our directive says, at the prices it says.
- **An off-cycle dispatch costs $25.00**, charged to our account, however much is loaded.
- **The fee is per request, not per unit.** One request covering many slots and products costs the same $25 as one covering a single item. Batch.
- **Timing is not guaranteed.** Operations tries to fulfil a request within 24 hours where it can, but it may take longer. Act sensibly while waiting, without knowing exactly when it lands.

**The trade** (section 3.4). Most planning is weekly. A midweek dispatch trades $25 and an uncertain arrival against the cost of a slot sitting empty. Compare it with what $25 would buy if we waited for Friday (section 3.3). Decide from those numbers, the uncertainty and our cash, and show the working.

**Ordering** (sections 3.4 and 4). The order is placed end to end through the approved retailer and channel, inside the balance, within the price floor and ceiling, and subject to the halt switch.

**How this works for us.** A restock is one desired state for all 20 slots. Check it with `preview_desired_state` first: it returns the request lines, the worker's directive, the cost, the balance left and every refusal. A midweek visit is the same request with the off-cycle flag. Check open requests with `get_purchase_requests` before asking for another, and do not send a second request while one is pending. Prices change only at a visit, so a price change waits for Friday or rides on a paid visit.

**Slow sellers** (team decision, 12 Nov). Flag a slot when it has sold nothing for **5 class days in a row while our records say it held stock** (a receipt or a person's count shows units in it). Days in a break don't count, and a slot that may be empty is not slow, because an empty slot can't sell. At the next Friday visit, act on a flagged slot: try a lower price, or swap the product. Pulled units go to the closet at what we paid, so a pull is not a write-off. Don't flag a slot for selling slowly but steadily; there is no rule for that until we have sales of our own drinks. Say in the slot's reason which rule applied.

**Class days** (the Institute's calendar): Monday to Friday, except **Tue 22 Dec to Fri 1 Jan** (Winter Holiday Break) and **Mon 15 Feb** (Presidents Day). Winter Quarter classes end Fri 19 Feb.

**Evidence** (`analysis/REPORT.md`, "Slow-seller rule"). Replayed over this fall, zero sales for 5 class days raised 75 flags; of the 39 with an outcome, 10% were live slots and about half were probably empty. 7 calendar days did the same (77 flags, 10% live): we count class days because a break says nothing about a drink. The held-stock check is **not validated**. The report's nearest check, inferred from sales, cut 75 flags to 6 without making them more accurate. Ours rests on receipts (loaded minus sold, `vending/stock.py`) or a person's count, which that data lacked. A flag is a reason to look, not proof.

**Qualifying.** Restocking keeps us on the leaderboard: four complete locations or thirty units across the quarter (section 6).
