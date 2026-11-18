# Wildcard slot

One slot tests products for the other 19 and may sell up to $8.00; it moves only at a Friday visit, holds a product no other slot holds, and one purchase for it costs at most $82.00.

**Why** (#100; adopted on #110, item 1). One of our 20 slots is a testing ground: a product we have no sales for gets a trial there, and if it earns its place it moves into a regular slot. Name the slot in every plan's `wildcard` field, and give the reason in `slot_reasons`. A plan's choice takes effect once its Friday visit is done. A Friday plan that names no wildcard ends the trial.

**A testing ground.** `get_machine_state` shows `wildcard_trial`: the product on trial, its profit per in-stock class day (revenue without tax, less cost of sales), the weakest of our other slots over the same days, and a verdict. Only days the polls saw count; revenue and cost come from the same polls, or the profit is unknown.

- **Price a trial as a regular slot would**, at or under $6.50, so the result carries over. Above $6.50 it is a price test, not a promotion trial. The trial counts again from the first poll that reads $6.50 or less; a poll with no price doesn't count.
- **Test a product no other slot holds**, at any price, so its sales aren't split. The trial counts from when the product went in or the slot was named, whichever is later.
- **Judge it at the first Friday visit after 5 in-stock class days**, when its own rate counts (`restocking`). Compare over the class days the polls saw both slots stocked; a slot with fewer than 5 isn't compared.
- **Promote** when it earns more per day than the weakest other slot. Keep it where it is, now as a regular slot; send the weakest slot's product to the closet; make that slot the wildcard with the next product to test.
- **Drop** when it doesn't: switch the wildcard to the next product to test. Pick from the catalog, and say why in `slot_reasons`.

**The $8.00 ceiling** is for a deliberate price test: a premium drink, a seasonal bet or an anchor. Say it is a price test. Every sale should still profit: no loss leaders.

- **It moves only at a Friday visit.** A midweek plan names the wildcard in effect and can't raise it above $6.50.
- **One at a time.** Any slot priced above $6.50 is the wildcard, so a plan that keeps one there must name it, or reprice it. To move it, lower the old slot at one visit and start the new one at the next. A new wildcard waits until receipts show what every other slot holds, and above $6.50 until every price is known.
- **One purchase for it costs at most $82.00** at the worker's maximum (`wildcard_purchase_cap_cents`, #90).
- **Its product is in no other slot**, so only its own line buys that product, and `pricing` step 1 (one price per product) still holds. Extras to the closet count toward the $82.00. A slot that was above $6.50 keeps its product to itself until a visit brings it back under, in case its line comes back unavailable. While a slot's contents or price are unknown, the preview caps purchases at $82.00 and blocks risky switches.

**One known limit.** A price changes only with a load (`price_only_unsupported`), so a full slot above $6.50 can't be lowered until it sells a unit; then restock it at the lower price.
