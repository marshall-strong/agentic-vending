# Wildcard slot

One slot may sell up to $8.00 for a riskier bet; it moves only at a Friday visit, holds a product no other slot holds, and one purchase for it costs at most $82.00.

**Why** (Kevin's proposal, #100; the team decides on #110, item 1). One of our 20 slots is a riskier bet: a premium drink, a high-price test, a rotating feature, a seasonal bet or an anchor that makes the others look cheap. Name it in the plan's `wildcard` field and give the reason in `slot_reasons`.

- **Ceiling $8.00**; the floor is every slot's floor. Every sale should still profit: no loss leaders.
- **It moves only at a Friday visit.** A midweek plan can't take a new slot above $6.50.
- **One at a time.** Any slot priced above $6.50 is the wildcard, so a plan that keeps one there must name it, or reprice it. To move it, lower the old slot at one visit and start the new one at the next: if both changed in one visit and the old slot's line came back unavailable, two slots would be above $6.50. A new wildcard also waits until every other slot's current price is known.
- **One purchase for it costs at most $82.00** at the worker's maximum (`wildcard_purchase_cap_cents`, #90).
- **Its product is in no other slot**, so `pricing` step 1 (one price per product) still holds, and only its own line buys that product: extras to the closet count toward the $82.00. A slot that was the wildcard keeps its product to itself until a visit has brought it back under $6.50, in case the line that switches or lowers it comes back unavailable. For the same reason a wildcard can't start or switch to a product, and its product can't be bought, while that product is still in another slot, whether it comes from a store or the closet: move the other slot first. While a slot above $6.50 has unknown contents, every purchase is held to $82.00 and no other slot may switch product. A slot with stock we can't name, priced above $6.50 or at an unknown price, counts the same way, and a new wildcard waits until every slot's contents are known. A slot whose price is unknown may already be above $6.50, so its product goes into no other slot and purchases of it are held to $82.00 until a poll shows the price.
- **Judge it like any slot**: profit per stocked week, and the slow-seller rule. No fall drink sold above $5.00, so there is no rate to borrow; expect it to sell slower, and say so.

**One known limit.** A price changes only with a load (`price_only_unsupported`), so a full slot above $6.50 can't be lowered until it sells a unit; then restock it at the lower price. With few products to switch to, it can take sales in more than one slot.
