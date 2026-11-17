# Wildcard slot

One slot tests products for the other 19 and may sell up to $8.00; it moves only at a Friday visit, holds a product no other slot holds, and one purchase for it costs at most $82.00.

**Why** (#100; adopted on #110, item 1). One of our 20 slots is a testing ground: a product we have no sales for gets a trial there, and if it earns its place it moves into a regular slot. Name the slot in the plan's `wildcard` field in every plan, and give the reason in `slot_reasons`. A plan that names no wildcard ends the trial.

**A testing ground.** `get_machine_state` shows `wildcard_trial`: the product on trial, its profit per in-stock class day (revenue without tax, less the cost of what sold), the weakest of our other slots over the same days, and a verdict.

- **Price a trial as a regular slot would**, at or under $6.50, so the result carries over. A trial that sells above $6.50 is a price test, not a promotion trial.
- **Judge it at the first Friday visit after 5 in-stock class days**, when its own rate counts (`restocking`).
- **Promote** when it earns more per day than the weakest other slot. Keep it where it is, now as a regular slot; send the weakest slot's product to the closet; make that slot the wildcard with the next product to test. No stock moves between slots.
- **Drop** when it doesn't: switch the wildcard to the next product to test. Pick from the catalog, and say why in `slot_reasons`.

**The $8.00 ceiling** is for a deliberate price test: a premium drink, a seasonal bet or an anchor that makes the others look cheap. Say it is a price test. Every sale should still profit: no loss leaders.

- **It moves only at a Friday visit.** A midweek plan can't take a new slot above $6.50.
- **One at a time.** Any slot priced above $6.50 is the wildcard, so a plan that keeps one there must name it, or reprice it. To move it, lower the old slot at one visit and start the new one at the next. A new wildcard above $6.50 also waits until every other slot's price and contents are known.
- **One purchase for it costs at most $82.00** at the worker's maximum (`wildcard_purchase_cap_cents`, #90).
- **Its product is in no other slot**, so `pricing` step 1 (one price per product) still holds, and only its own line buys that product: extras to the closet count toward the $82.00. A slot that was above $6.50 keeps its product to itself until a visit has brought it back under, in case the line that changes it comes back unavailable. When a slot's contents or price are unknown, the preview holds purchases to $82.00 and blocks switches that could put the wildcard's product in two slots; its refusals say what to count or wait for.

**One known limit.** A price changes only with a load (`price_only_unsupported`), so a full slot above $6.50 can't be lowered until it sells a unit; then restock it at the lower price.
