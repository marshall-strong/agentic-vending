# Pricing

Prices stay within the floor and ceiling, must be explainable, may need deliberate variation to learn demand, and come from our own costs and sales only.

**The bounds** (Project Brief (Revised), sections 2 and 4). Code enforces these, not your memory.

- **Floor:** the most we'd pay per unit, plus 8% tax, rounded up to the next 5 cents. Never guess it; `preview_desired_state` shows it for every slot.
- **Ceiling:** $6.50, the team's decision on 12 Nov (`price_ceiling_cents`; the bridge refuses over $10.00). A10 in `docs/assumptions.md` is waiting on the instructional team to confirm it.
- **Steps:** prices go in 5-cent steps.
- **The wildcard slot:** one slot may go up to $8.00 (`wildcard_ceiling_cents`) instead of $6.50. See below.

The preview refuses any price outside these bounds.

**The wildcard slot** (Kevin's proposal, #100; the team decides on #110, item 1). One of our 20 slots is a riskier bet: a premium drink, a high-price test, a rotating feature, a seasonal bet or an anchor that makes the others look cheap. Name it in the plan's `wildcard` field and give the reason in `slot_reasons`.

- **Ceiling $8.00**; the floor is every slot's floor. Every sale should still profit: no loss leaders.
- **It moves only at a Friday visit.** A midweek plan can't take a new slot above $6.50.
- **One at a time.** Any slot priced above $6.50 is the wildcard, so a plan that keeps one there must name it, or reprice it. To move it, lower the old slot at one visit and start the new one at the next: if both changed in one visit and the old slot's line came back unavailable, two slots would be above $6.50. A new wildcard also waits until every other slot's current price is known.
- **One purchase for it costs at most $82.00** at the worker's maximum (`wildcard_purchase_cap_cents`, #90).
- **Its product is in no other slot**, so step 1 below still holds, and only its own line buys that product: extras to the closet count toward the $82.00. A slot that was the wildcard keeps its product to itself until a visit has brought it back under $6.50, in case the line that switches or lowers it comes back unavailable.
- **Judge it like any slot**: profit per stocked week, and the slow-seller rule. No fall drink sold above $5.00, so there is no rate to borrow; expect it to sell slower, and say so.

**How to choose a price.** Work through these steps for every slot in the plan.

1. **One price per product.** The same drink costs the same in every slot it fills.
2. **A product new to a slot starts at its reference price** from `price-history`. If the floor is higher, use the floor. If the floor is above the reference, the drink costs too much to sell at its usual price, so prefer a cheaper size, pack or store. If you stock it anyway, say it will probably sell slowly.
3. **A slow seller comes first.** Zero sales for 5 class days while our records say the slot held stock is a slow seller (`restocking`): at the next Friday visit, swap it (the planner refuses a price-only change for now). Otherwise, hold a price for two stocked weeks, counting only weeks in stock while classes ran. While stocked, a slot sells about 3 to 9 units a week, so one week can't show the effect of a change.
4. **Then hold, or move one $0.25 step:**
   - It **ran out** before the visit in both weeks: raise it $0.25, up to $6.50, and load more.
   - **Otherwise** hold it, or test a $0.25 step up or down under step 5. Slow but steady sales aren't a slow seller: no low-rate rule until we have sales of our own drinks.
5. **One test at a time** (section 3.2, the identification problem). Apart from new products, change at most one product's price a week, so any change in sales can be traced to it. After two stocked weeks at the new price, compare profit per stocked week at the old and new prices: units × (price ÷ 1.08 − unit cost). Keep whichever price earned more.
6. **Never pay for a price change.** Change prices at the free Friday visit, or with a midweek restock that's worth its $25 anyway.
7. **Never use other slots' prices.** Section 3.2 calls them an input; the team follows the instructor's integrity rule instead (A15, `competition-and-integrity`).

**Inputs:**

- each product's unit cost and floor, from the preview
- its reference price, from `price-history`
- its sales and stocked weeks since its last price change, from `get_sales_history`, `get_machine_state` and your notes from earlier weeks

**Outputs:**

- a `sale_price` for every slot
- `wildcard`: the wildcard slot, when one is above $6.50 or you're starting a bet there
- a reason in `slot_reasons` that names the step used, for example "new, reference $4.50", "hold, week 1 of 2" or "test, up $0.25: ran out both weeks"

**Assumptions** (A17 and A18 in `docs/assumptions.md`):

- What a drink sold for in our slots this fall is a fair first price for it.
- A $0.25 step held for two stocked weeks is big enough to show an effect and small enough to cost little if it's wrong.
- How sales respond to price is unknown until we test it.

Note in each reason when a number used is missing or stale.
