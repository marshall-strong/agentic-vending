# Pricing

Prices stay within the floor and ceiling, must be explainable, may need deliberate variation to learn demand, and come from our own costs and sales only.

**The bounds** (Project Brief (Revised), sections 2 and 4). Code enforces these, not your memory.

- **Floor:** the most we'd pay per unit, plus 8% tax, rounded up to the next 5 cents. Never guess it; `preview_desired_state` shows it for every slot.
- **Ceiling:** $6.50, the team's decision on 12 Nov (`price_ceiling_cents`; the bridge refuses over $10.00). A10 in `docs/assumptions.md` is waiting on the instructional team to confirm it.
- **Steps:** prices go in 5-cent steps.

The preview refuses any price outside these bounds.

**How to choose a price.** Work through these steps for every slot in the plan.

1. **One price per product.** The same drink costs the same in every slot it fills.
2. **A product new to a slot starts at its reference price** from `price-history`. If the floor is higher, use the floor. If the floor is above the reference, the drink costs too much to sell at its usual price, so prefer a cheaper size, pack or store. If you stock it anyway, say it will probably sell slowly.
3. **A slow seller comes first.** Zero sales for 5 school days while our records say the slot held stock is a slow seller (`restocking`): at the next Friday visit, lower its price one step if the floor allows, or swap the product. Otherwise, hold a price for two stocked weeks, counting only weeks in stock while classes ran. While stocked, a slot sells about 3 to 9 units a week, so one week can't show the effect of a change.
4. **Then hold, or move one $0.25 step:**
   - It **ran out** before the visit in both weeks: raise it $0.25, and load more.
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
- a reason in `slot_reasons` that names the step used, for example "new, reference $4.50", "hold, week 1 of 2" or "test, up $0.25: ran out both weeks"

**Assumptions** (A17 and A18 in `docs/assumptions.md`):

- What a drink sold for in our slots this fall is a fair first price for it.
- A $0.25 step held for two stocked weeks is big enough to show an effect and small enough to cost little if it's wrong.
- How sales respond to price is unknown until we test it.

Note in each reason when a number used is missing or stale.
