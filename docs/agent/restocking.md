# Restocking

One free restock every Friday; an extra dispatch costs $25 per request (not per unit), may take longer than 24 hours, and should be batched.

**The rules** (Project Brief (Revised), section 3.4):

- **Friday is free.** Operations loads whatever our directive says, at the prices it says.
- **An off-cycle dispatch costs $25.00**, charged to our account, however much is loaded.
- **The fee is per request, not per unit.** Batch.
- **Timing is not guaranteed.** Operations tries to fulfil a request within 24 hours where it can, but it may take longer. Act sensibly while waiting, without knowing exactly when it lands.

**The trade** (section 3.4). A midweek dispatch trades $25 and an uncertain arrival against the cost of a slot sitting empty. Compare it with what $25 would buy if we waited for Friday (section 3.3). Decide from those numbers, the uncertainty and our cash, and show the working.

**Ordering** (sections 3.4 and 4). The order goes end to end through the approved retailer and channel, inside every guardrail on the rules card.

**How this works for us.** A restock is one desired state for all 20 slots. Check it with `preview_desired_state` first: it returns the request lines, the worker's directive, the cost, the balance left and every refusal. A midweek visit is the same request with the off-cycle flag. Check `get_purchase_requests` too: never send a second request while one is pending. At any visit, any of our slots can be changed, within the request API and the wildcard rules, with no limit on units or lines (Kevin, 19 Nov).

**Every restock, fill every slot** (Kevin, 19 Nov). Each visit's plan lists each of our slots, not as `keep`, without `fill_to`, so each fills to its column. Almost every visit is Friday's: $25 is a lot. The planner loads from the closet when it covers the gap, else buys. If the floor binds, an explicit `fill_to` may load just the closet's units. Exceptions, each named in the slot's reason:

- A flagged slow seller gets a swap.
- The wildcard follows `wildcard`: at most $82.00 a purchase, and no more of its product while another slot holds it.
- Keep at least $53.00 spendable after the request: if short, fill in rule 4's order (most margin per class day first) and name slots left short.

Fill before the break and at the end: end stock counts at cost (A5, confirmed), so unsold units cost no profit.

**Slow sellers** (team decision, 12 Nov). Flag a slot when it has sold nothing for **5 class days in a row while our records say it held stock** (a receipt or a person's count shows units in it). Days in a break don't count, and a slot that may be empty is not slow, because an empty slot can't sell. At the next restock (normally Friday), act on a flagged slot: swap the product (the planner refuses a price-only change for now). Pulled units go to the closet at cost, not a write-off. Don't flag a slot for selling slowly but steadily; there is no rule for that until we have sales of our own drinks.

**Class days** (the Institute's calendar): Monday to Friday, except **Tue 22 Dec to Fri 1 Jan** (Winter Holiday Break) and **Mon 15 Feb** (Presidents Day). Winter Quarter classes end Fri 19 Feb.

**Evidence** (`analysis/REPORT.md`, "Slow-seller rule"). Over the fall, of 39 flags with an outcome, 10% were live slots and about half probably empty. The held-stock check is **not validated**. A flag is a reason to look, not proof.
