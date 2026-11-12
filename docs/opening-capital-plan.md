# Opening capital plan

How we plan to use the $600 we start with: what the day-one fill costs, what we hold back, and why.

## What this is

The project brief asks for "your opening capital plan: how you intend to allocate the initial $600.00, and the reasoning behind holding back whatever you hold back" (section 5; issue #90). This is that plan, for our 20 slots on the shared machine.

The $600 is given once and never topped up. Sales flow back into the same account, and midweek visit fees and the agent's model spend come out of it (`docs/agent/money.md`). An order the balance can't cover is refused.

On 12 Nov the instructor told us how the competition starts (PR #106, issue #107):

- Course staff empty the machine before the start.
- Our **day-one order fills all 20 slots to capacity** with the products we choose. Its cost comes out of the $600.
- The order is due about **Fri 27 Nov**, which falls in the Institute's 26 to 29 Nov closure. The exact day and time are still being asked (#102).
- The machine goes live with our products on **Thu 3 Dec**.
- Restocks are **Friday afternoons**: 3 PM is the target, but 3 to 5 PM or later is likely.
- We record the ledger's opening **before the day-one fill is fulfilled**, so the fill's receipts count against the $600 (#104, #106).

So the plan's first lines are the day-one fill. Everything else is what we keep back.

This plan owns the money numbers for Summative 2. The restock policy (`docs/restock-policy.md`, #91) and the demand doc (`docs/inventory-demand.md`, #93) use the lines and the spendable floor below and cite them here.

## The plan in one table

Every amount marked Proposed is this plan's proposal. The team has not agreed it (see "Open decisions").

| Line | Amount | How we got it |
|---|---|---|
| Day-one fill, 19 regular slots, most it may cost | **$360.00** | Proposed cap. Run 1's dry run was $360 at most for all 20 slots (#90). The example fill below is $356.50 at most for 19 slots ($365.50 for 20, less its $9.00 Coca-Cola can slot) |
| Wildcard slot | $82.00 | Proposed. The most one full column can cost under the wildcard's $8.00 ceiling and today's floor: 11 × $7.407 = $81.48, rounded up (section 2) |
| Midweek visit fees | $50.00 | 2 visits × $25 (`docs/agent/restocking.md`) |
| Model spend | $9.00 | 9 run weeks × 1 weekly run × $1.00 run cap (#51) |
| Product-switch reserve | $68.00 | Proposed. 1 pack × $68.00, the dearest pack on our price list at the worker's maximum (Mountain Dew Baja Blast 20 oz). The first week's takings fund more (section 2) |
| Safety margin | $31.00 | Proposed. What is left: $600 − $360 − $82 − $50 − $9 − $68. It covers one week of model spend at the $4.00 daily cap beyond the planned $1.00 ($27.00) and one live run before 3 Dec ($1.00), with $3.00 unassigned (section 2) |
| **Total** | **$600.00** | |

**Spendable floor: $53.00 (Proposed).** This is a rule, not a line. After any request, at its most-we-pay cost, the spendable balance must still hold $53.00: one midweek fee ($25.00, config `midweek_fee_cents`) plus one week of model spend at the daily cap (7 × $4.00 = $28.00, config `model_daily_cap_cents`, default 400). These are the costs that can leave the account before a Friday's takings come in. The derivation comes from the restock policy's draft (#91). This plan adopts it, because both parts are values the code enforces (`vending/planner.py`, `vending/harness.py`). The held-back lines contain it: $25 of the $50 fee line, plus $1.00 (one week) of the model line and $27.00 of the margin.

The fill cap is a ceiling at the worker's maximum prices. If the worker pays the expected prices, the fill costs less and the margin grows. In the example fill's 19 slots that is $308.26 expected ($315.94 − $7.68), so $360.00 − $308.26 = $51.74 under the cap.

After the day-one fill at its most ($360 + $82 = $442) and one live run before 3 Dec ($1), $600 − $443 = $157 stays spendable. That is above the $53 floor.

**Sales tax is not a line.** It comes out of takings, not out of the $600 (see "Sales tax" below).

## 1. The day-one fill

**What it buys.** Every one of our 20 slots, filled to the column's capacity. Capacity depends on the package, not the slot. The machine's loading chart (`vending/capacity.py`, `vending/dashboard.py` `PACKAGE_CAPACITY`) gives:

| Package | Units per column |
|---|---|
| 20 oz bottle | 7 |
| 16 oz bottle | 7 |
| 14 oz bottle | 8 |
| 12 oz can | 8 |
| 8.4 oz (250 ml) can | 11 |

A 16 oz can is not on the chart. It needs a person to count how many fit before it can be planned (`vending/capacity.py`, which uses a Monster 16 oz can as its example).

**What a slot costs.** Unit cost is the Amazon pack price divided by the units in the pack, from the team's 11 Nov price list (branch `add_price_list`, `docs/agent/price-list.md`). A full slot is that times the column's capacity.

| Drink | Pack | Pack price | Per unit | Column | One full slot |
|---|---|---|---|---|---|
| Pepsi 12 oz can | 12 | $6.97 | $0.58 | 8 | $4.65 |
| Coca-Cola 12 oz can | 12 | $7.68 | $0.64 | 8 | $5.12 |
| Spindrift Grapefruit 12 oz can | 24 | $20.97 | $0.87 | 8 | $6.99 |
| Celsius Kiwi Guava 12 oz can | 12 | $19.98 | $1.67 | 8 | $13.32 |
| Mountain Dew 20 oz | 24 | $49.24 | $2.05 | 7 | $14.36 |
| Celsius Sparkling Orange 12 oz can | 12 | $21.57 | $1.80 | 8 | $14.38 |
| Diet Coke 20 oz | 24 | $50.99 | $2.12 | 7 | $14.87 |
| Coca-Cola 20 oz | 24 | $54.95 | $2.29 | 7 | $16.03 |
| Red Bull Original 8.4 oz | 24 | $34.99 | $1.46 | 11 | $16.04 |
| Pepsi 20 oz | 12 | $29.99 | $2.50 | 7 | $17.49 |
| Red Bull Red Edition 8.4 oz | 24 | $47.99 | $2.00 | 11 | $22.00 |

This is a selection: 11 of the 20 drinks on the list. The other 9 cost $6.97 to $19.47 a slot (Spindrift Raspberry Lime or Lemon: $20.91 ÷ 24 × 8 = $6.97; Red Bull Yellow: $42.48 ÷ 24 × 11 = $19.47). So the cheapest and dearest cases below don't change.

**The range.** Amazon sells packs, not single units. Units that don't fit a slot go to our closet as back stock, but we still pay for them.

| Case | Units loaded | Packs | Cost |
|---|---|---|---|
| Cheapest: all 20 slots Pepsi cans | 160 | 14 × $6.97 | $97.58 |
| Run 1's dry-run plan (#90) | 159 | not recorded | $293 expected, $360 at most |
| Example mix (below) | 163 | 10 | $315.94 expected, $365.50 at most |
| Dearest: all 20 slots Red Bull Red Edition | 220 | 10 × $47.99 | $479.90 expected, $555.00 at most |

The dearest case would leave $45 of the $600 at the worker's maximum prices. That is why the plan caps the fill.

**The example mix.** This is a pricing example, not the team's pick (see "Open decisions"). It leans on energy drinks because they were 45% of our units this fall (`analysis/REPORT.md`, finding 14). It groups slots so packs divide evenly.

| Drink | Slots | Units loaded | Packs | Expected | Most |
|---|---|---|---|---|---|
| Red Bull Original 8.4 oz | 4 | 44 | 2 × 24 | $69.98 | $81.00 |
| Celsius Sparkling Orange | 3 | 24 | 2 × 12 | $43.14 | $50.00 |
| Celsius Kiwi Guava | 3 | 24 | 2 × 12 | $39.96 | $46.00 |
| Coca-Cola 20 oz | 3 | 21 | 1 × 24 | $54.95 | $63.50 |
| Diet Coke 20 oz | 3 | 21 | 1 × 24 | $50.99 | $59.00 |
| Mountain Dew 20 oz | 3 | 21 | 1 × 24 | $49.24 | $57.00 |
| Coca-Cola 12 oz can | 1 | 8 | 1 × 12 | $7.68 | $9.00 |
| **Total** | **20** | **163** | **10** | **$315.94** | **$365.50** |

"Most" is the worker's maximum per pack. For the five drinks in the catalog it comes from the price list. For Kiwi Guava and the Coca-Cola can we work it out. The price list says price plus about 15%. Its five Max values all round up to the next 50 cents (for example $54.95 × 1.15 = $63.19, listed as $63.50), so we do the same ($19.98 × 1.15 = $22.98, so $23.00; $7.68 × 1.15 = $8.83, so $9.00). The mix sends 17 units to the closet: 4 Red Bull, 3 of each 20 oz drink and 4 Coca-Cola cans.

**Grouping saves cash.** Three slots of a 20 oz drink use 21 of a 24-pack. One slot uses 7 and leaves 17 in the closet. Two Red Bull slots use 22 of 24. Three Celsius slots use exactly two 12-packs.

**With a wildcard.** If the team adopts the wildcard slot (section 2), one of the 20 slots holds it and is paid from the wildcard line. In the example, if the agent put it in the Coca-Cola can slot, the other 19 slots cost $308.26 expected and $356.50 at most ($365.50 − $9.00), under the $360.00 cap. Which slot it takes is the agent's choice.

**How long it lasts.** If every slot were kept stocked, our slots' own sales rates add up to about 23.3 units a class day (recent rates) or 22 (whole era) (`analysis/REPORT.md`, finding 4 and "Next 4 weeks"). The restock policy and the demand doc use the same 23.3. An older estimate in #90 is about 70 a week for our 20 slots, which is 14 a class day. So 163 units is about 7 class days at the higher rate and about 12 at the lower one (163 ÷ 23.3 = 7.0; 163 ÷ 14 = 11.6). The fast slots run out sooner than that.

## 2. What we hold back, and why

### Restocking what sold: no reserve needed

The pricing gate sets every price at or above a floor: the most the worker may pay per unit, times 1.08, rounded up to the next 5 cents (`vending/pricing.py`). So each unit sold returns at least its maximum cost once tax is taken out (price ÷ 1.08 is at least the maximum cost). Takings are counted from the machine's counters, hourly, before we send a request (`vending/ledger.py`).

This floor is break-even after tax, not profit: a sale at the floor keeps the unit's cost and at most 4 cents more (the rounding up to 5 cents).

So refilling the same products with what sold pays for itself. A worked example for the first stocked days, Thu 3 to Wed 9 Dec (5 class days), before an order for Fri 11 Dec. In-store purchases are due Thursday 23:59. Amazon has no hard deadline, but the planner warns after Wednesday 23:59 (#106 row 1, `vending/planner.py`).

| | High rate (23.3 a class day) | Low rate (14 a class day) |
|---|---|---|
| Units sold | 116.5 | 70 |
| Takings at $3.35 a unit | $390.28 | $234.50 |
| Spendable after tax (÷ 1.08) | $361.37 | $217.13 |
| Refill at run 1's most per unit, $2.26 | $263.29 | $158.20 |
| Left after the refill | $98.08 | $58.93 |

$3.35 is what our slots took per unit this fall: $1,881.50 ÷ 562 (`analysis/REPORT.md`, finding 1). $2.26 is run 1's most per unit: $360 ÷ 159 (#90). Our real prices will differ.

The catch is whole packs. Refilling 5 units of a 20 oz drink means buying a 24-pack. The extra units sit in the closet and are used at the next visit. That is working capital, which the reserves below cover.

If there is a regular Friday restock on 4 Dec, its request is due the night we go live (#107). Only about one class day will have sold, so it would be small.

### Product-switch reserve: $68.00 (Proposed)

After the first stocked week, we expect to drop products that don't sell and try others. That needs new packs before the old stock is sold.

**The first switch is no earlier than Fri 11 Dec.** The slow-seller rule flags a slot after 5 class days with no sales while it held stock (PR #100). The first 5 class days are Thu 3, Fri 4, Mon 7, Tue 8 and Wed 9 Dec, so the first flag acts at the Fri 11 Dec visit. By then a week of takings is in. In the table above, they leave $58.93 to $98.08 after refilling what sold.

**So the reserve holds one pack, not two.** The pack is the dearest drink on the team's 11 Nov price list (branch `add_price_list`), Mountain Dew Baja Blast 20 oz at $58.80 a 24-pack. The list gives no maximum for it, so we use the same rule as above: $58.80 × 1.15 = $67.62, rounded up to $68.00. One reserved pack plus the first week's surplus buys about 1.9 to 2.4 such packs ((68.00 + 58.93) ÷ 68.00 = 1.87; (68.00 + 98.08) ÷ 68.00 = 2.44). The surplus is an estimate at fall rates and prices. If our first week sells less, the second switch waits a week.

**Swap first would use it sooner.** Kevin proposed swapping a flagged slow seller after at most one price step down (see "Open decisions"). That makes switches more frequent. Pulled units go to the closet at what we paid (assumption 8), so they keep their value but not their cash.

### Wildcard slot: $82.00 (Proposed)

**The proposal.** Kevin proposed this on #100; the team has not agreed it. The agent picks one of our 20 slots as the wildcard. It names the slot in its plan, with a reason, and may move it only at a Friday visit. The wildcard's price ceiling is about $8.00, against the $6.50 ceiling for every other slot (PR #100). Its floor is the same as every slot's: cost × 1.08, rounded up to 5 cents (`vending/pricing.py`). Kevin's aim is that every sale makes a profit, with no loss leaders. Today's floor only breaks even after tax, so that aim needs a floor above today's (open). Ideas for the slot: a premium drink, a high-price test, a rotating feature, a seasonal bet, or an anchor.

**Why a line of its own.** No wildcard product is picked, and the repo has no price for a premium drink. The dearest unit on the 11 Nov price list is Pepsi 20 oz at $2.50 ($29.99 ÷ 12). So this line is a bound, not a quote. Under today's floor, a product that sells at $8.00 can cost at most $8.00 ÷ 1.08 = $7.407 a unit (`vending/pricing.py` spreads the pack's maximum price over its units, so a unit cost need not be whole cents). The largest column on the loading chart holds 11 (8.4 oz cans). So one full wildcard column costs at most 11 × $7.407 = $81.48, rounded up to $82.00. A 12 oz can column would be 8 × $7.407 = $59.26, and a 20 oz bottle column 7 × $7.407 = $51.85.

**Proposed rule.** Any one wildcard purchase costs at most $82.00 at the worker's maximum. A pack bigger than a column can fit if its unit cost is lower. If no pack fits, the agent picks another product. If the floor is raised so that every sale makes a profit, the wildcard's unit cost must be lower still, so $82.00 stays the top.

**When it is spent.** If the agent picks a wildcard on day one, its slot's pack comes from this line. If it doesn't, the line stays held back and adds to the switch reserve. A wildcard moved at a later Friday visit is paid from takings or the switch reserve, still under the $82.00 cap.

### Midweek visit fees: $50.00

A visit outside Friday costs $25, however much it loads (`docs/agent/restocking.md`). We hold back 2 visits: 2 × $25 = $50. When a visit pays is rule 2 of the restock policy (`docs/restock-policy.md`, #91). At run 2's most-we-pay cost, Red Bull at $4.50 makes $2.48 a unit after tax ($4.50 ÷ 1.08 − $40.50 ÷ 24; `docs/sample-directive.md`), so $25 is the margin on about 11 sales (25 ÷ 2.48 = 10.1, rounded up).

### Model spend: $9.00

Every live model call is charged to the ledger at its real cost (`vending/harness.py`, #51). Only live calls from the ledger's opening on come out of the $600. Testing before that is development spend (#104).

- Observed so far: $0.02 to $0.80 a run (#90). Run 2 cost $0.40 (`docs/sample-directive.md`).
- The harness caps a run at $1.00 and a day at $4.00. Both are placeholders (#51).
- Nothing schedules the agent yet (#39, #107). We assume one live run a week.

So 9 run weeks × 1 run × $1.00 = $9.00. Live runs between the ledger's opening and 3 Dec also count, for example one that builds or checks the day-one request. At one such run that is another $1.00 at most, which the margin covers. One week at the $4.00 daily cap is 7 × $4.00 = $28.00; the spendable floor and the margin cover one such week. If the agent ran every day at the cap, 63 days × $4.00 = $252.00, which is 42% of the $600. The daily cap should be set to fit this plan before the agent goes live (see "Open decisions").

### Safety margin: $31.00 (Proposed)

This is what is left after the other lines: $600 − $360 − $82 − $50 − $9 − $68 = $31. It is sized for two named risks:

| Risk | Amount | Basis |
|---|---|---|
| One week of model spend at the daily cap, beyond the planned $1.00 | $27.00 | 7 × $4.00 − $1.00. The $4.00 cap is the config default (`model_daily_cap_cents`), and the harness enforces it (`vending/harness.py`). The $9.00 line assumes one $1.00 run a week |
| One live run between the ledger's opening and 3 Dec | $1.00 | The $1.00 run cap (#51) |
| Unassigned | $3.00 | |
| **Total** | **$31.00** | |

Price rises up to the worker's maximum (about 15%) are already in the fill cap and the pack lines, so the margin does not count them again. In practice the cushion is larger: at expected prices the example's 19 slots leave $51.74 under the fill cap (section "The plan in one table").

### Sales tax: $0 from the capital

Shelf prices include 8% sales tax. The team set aside takings × 0.08 ÷ 1.08 each week (`docs/planning/2026-10-19-david-api-qa.md`, row 6). That is about 7.4% of takings, not 8%. The ledger does this on its own: the **spendable** balance is the balance less the tax set aside and not yet remitted (`vending/ledger.py`). So the tax is paid out of the sales that collected it and needs nothing from the $600. Purchases carry no tax: the Institute is tax-exempt (same file).

## 3. When the held-back money is released

- **At the first Friday restock:** spend only what refills sold units. Takings fund it (section 2).
- **From the second stocked week (Fri 11 Dec at the earliest):** the switch reserve funds product changes for slots that meet the slow-seller rule (#100), with the first week's takings.
- **The wildcard line:** at the day-one fill if the agent picks a wildcard then; otherwise it joins the switch reserve.
- **Midweek visit fees:** only when rule 2 of the restock policy (`docs/restock-policy.md`, #91) says so, with the working shown.
- **Before the winter break:** classes stop 22 Dec to 1 Jan, and the Institute is closed on Fri 25 Dec, Mon 28 Dec and Fri 1 Jan (`analysis/golisano_calendar_2026_27.json`). Fill lightly on Fri 18 Dec. Stock that sits through the break holds cash we can't use.
- **Floor at all times:** after any request, the spendable balance must still hold $53.00 (the spendable floor, section "The plan in one table"). Rule 4 of the restock policy applies it to every order. Nothing in the code enforces it yet (see the follow-ups below).

## Assumptions

To number in `docs/assumptions.md`. They aren't numbered here because open PRs are already taking numbers (A16 in #69; A17 and A18 in #99).

1. **The day-one fill comes out of the $600.** The instructor said so on 12 Nov, but wasn't sure he had understood the question and said he would check (#106, row 3).
2. **The day-one fill is bought from Amazon at the price list's packs and prices.** How the order is sent and bought is not confirmed (#102). If staff buy elsewhere, or Amazon can't deliver while the Institute is closed (26 to 29 Nov), prices and pack sizes change (#106).
3. **Amazon prices hold near 11 Nov's.** The price list says they will change. The plan uses the worker's maximum, about 15% over, to cover that.
4. **The loading chart is right.** The chart calls its numbers approximate (`vending/capacity.py`).
5. **Our new products sell at about the rate our slots sold this fall when stocked**, 14 to 23.3 units a class day. Different products and prices may sell differently. The price history (#99, `docs/agent/price-history.md`) can't tell us how price changes sales. The price history also says our floor for 20 oz Coke and Diet Coke ($2.90 and $2.70) is above the $2.00 they sold at this fall, so the 9 slots of 20 oz soda in the example mix may sell slower than fall rates suggest.
6. **The agent runs live once a week.** No schedule is set (#39).
7. **The run is 9 weeks** from 3 Dec (`docs/agent/goal-and-scoring.md` for week 9; #106 for the 3 Dec start).
8. **Closet stock is worth what we paid for it** at the end. The instructor believes so and is checking (`docs/planning/2026-10-19-david-api-qa.md`, row 16).

## Open decisions

These are the team's to make. This plan does not decide them.

1. **Which products go in the 20 slots on day one, and at what prices** (#107). The example mix is only a pricing example. Three of the top sellers this fall (Monster, Gorgie and Ghost, `analysis/REPORT.md`) aren't on the price list yet.
2. **The wildcard slot** (Kevin's proposal on #100; not agreed). The agent picks one slot, names it in its plan with a reason, and moves it only at a Friday visit; ceiling about $8.00; same floor as every slot. Proposed here: an $82.00 line and an $82.00 cap on any one wildcard purchase (section 2).
3. **The wildcard's floor.** Kevin's aim is that every sale makes a profit. Today's floor only breaks even after tax (`vending/pricing.py`). Raising it, for the wildcard or for every slot, is not decided.
4. **Swap first at a slow-seller flag** (Kevin's proposal, 12 Nov; not agreed). At a flag, at most one price step down, then swap; pulled units go to the closet at cost. It depends on end stock being valued at cost (assumption 8; #102, P2). Rule 3 of the restock policy (#91) has the reasoning.
5. **The fill cap.** Proposed: $360.00 at most for the 19 regular slots.
6. **The switch reserve.** Proposed: one pack ($68.00), counting on the first week's takings for more (section 2). The earlier draft held two ($136.00).
7. **The safety margin.** Proposed: $31.00, sized for one week of model spend at the daily cap and one run before 3 Dec.
8. **The spendable floor.** Proposed: $53.00 after any request ($25 fee + 7 × $4.00 model cap). It replaces the earlier draft's $59 ($50 fee line + $9 model line), which held the whole run's model budget back at all times.
9. **How many midweek visits to hold back for.** This plan says 2.
10. **The model caps and how often the agent runs** (#51, #39). The $4.00 daily cap doesn't fit a $9.00 line. If the cap is lowered, the floor and the margin can shrink by the same amount.
11. **The exact day-one deadline and how to send the order** (#102).

**Agent-facing follow-ups (not done here).** `docs/agent/money.md` says to follow the team's opening capital plan and to say so if there is none. Once this plan is agreed, it should point here, as #90 asks. A rule that keeps $53 spendable after any request would need a check in the planner.

## Sources

- Issue #90 and its comment: the brief's wording, run 1's dry run (159 units, $293 expected, $360 most), the 70-a-week estimate and token spend per run.
- PR #106 (`docs/planning/2026-11-12-david-answers.md`) and issue #107: the instructor's 12 Nov answers.
- PR #100 and its comments: the $6.50 ceiling, the slow-seller rule and the wildcard proposal.
- `docs/restock-policy.md` (#91): the spendable floor's derivation, the midweek rule and the swap-first reasoning.
- PR #99 (`docs/agent/price-history.md`): the 20 oz soda floor against its $2.00 fall price.
- PR #104: development and competition spend split at the ledger's opening.
- PR #51 and `vending/harness.py`: model-call costs and the run and daily caps.
- Branch `add_price_list` (`docs/agent/price-list.md`): Amazon packs and prices, 11 Nov 2026.
- `vending/capacity.py` and `vending/dashboard.py`: units per column from the loading chart.
- `vending/pricing.py`: the price floor.
- `vending/config.py`, `vending/planner.py`, `vending/harness.py`: the $25 fee, the $1.00 run cap and the $4.00 daily cap.
- `vending/ledger.py`: balance, tax set-aside and spendable.
- `docs/agent/money.md`, `docs/agent/restocking.md`, `docs/agent/goal-and-scoring.md`: the account, the $25 fee and the scoring rules.
- `docs/sample-directive.md`: run 2's request ($233.31 expected, $270.00 most, $0.40 model cost).
- `docs/planning/2026-10-19-david-api-qa.md`: the sales-tax method and purchase tax.
- `analysis/REPORT.md`: units, takings and sales rates for our 20 slots.
- `analysis/golisano_calendar_2026_27.json`: the winter break and closures.
