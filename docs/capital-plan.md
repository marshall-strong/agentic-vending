# Opening capital plan

How we plan to use the $600 we start with: what the day-one fill costs, what we hold back, and why.

## What this is

The project brief asks for "your opening capital plan: how you intend to allocate the initial $600.00, and the reasoning behind holding back whatever you hold back" (section 5; issue #90). This is that plan, for our 20 slots on the shared machine.

The $600 is given once and never topped up. Sales flow back into the same account, and midweek visit fees and the agent's model spend come out of it (`docs/agent/money.md`). An order the balance can't cover is refused.

On 12 Nov the instructor told us how the competition starts (PR #106, issue #107):

- Course staff empty the machine before the start.
- Our **day-one order fills all 20 slots to capacity** with the products we choose. Its cost comes out of the $600.
- The order is due about **Fri 27 Nov**. The exact day and time are still being asked (#102). The Institute is closed 26 to 29 Nov.
- The machine goes live with our products on **Thu 3 Dec**.
- Restocks are **Friday afternoons**: 3 PM is the target, but 3 to 5 PM or later is likely.
- We record the ledger's opening **before the day-one fill is fulfilled**, so the fill's receipts count against the $600 (#104, #106).

So the plan's first line is the day-one fill. Everything else is what we keep back.

## The plan in one table

| Line | Amount | How we got it |
|---|---|---|
| Day-one fill, most it may cost | **$400.00** | Proposed cap. The example fill below is $365.50 at most; run 1's dry run was $360 at most (#90) |
| Midweek visit fees | $50.00 | 2 visits × $25 (`docs/agent/restocking.md`) |
| Model spend | $9.00 | 9 run weeks × 1 weekly run × $1.00 run cap (#51) |
| Product-switch reserve | $127.00 | 2 packs × $63.50, the dearest pack on our price list |
| Safety margin | $14.00 | What is left: $600 − $400 − $50 − $9 − $127 |
| **Total** | **$600.00** | |

The fill cap is a ceiling at the worker's maximum prices. If the worker pays the expected prices, the fill costs less and the margin grows. In the example fill that is $365.50 − $315.94 = $49.56 more, on top of the $400.00 − $365.50 = $34.50 the example leaves under the cap.

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

"Most" is the worker's maximum per pack. For the five drinks in the catalog it comes from the price list. For Kiwi Guava and the Coca-Cola can it is the price list's rule: price plus about 15%, rounded up to the next 50 cents ($19.98 × 1.15 = $22.98, so $23.00; $7.68 × 1.15 = $8.83, so $9.00). The mix sends 17 units to the closet: 4 Red Bull, 3 of each 20 oz drink and 4 Coca-Cola cans.

**Grouping saves cash.** Three slots of a 20 oz drink use 21 of a 24-pack. One slot uses 7 and leaves 17 in the closet. Two Red Bull slots use 22 of 24. Three Celsius slots use exactly two 12-packs.

**How long it lasts.** If every slot were kept stocked, our slots' own sales rates add up to about 22 to 23 units a class day (`analysis/REPORT.md`, finding 4). An older estimate in #90 is about 70 a week for our 20 slots, which is 14 a class day. So 163 units is about 7 class days at the higher rate and about 12 at the lower one (163 ÷ 23 = 7.1; 163 ÷ 14 = 11.6). The fast slots run out sooner than that.

## 2. What we hold back, and why

### Restocking what sold: no reserve needed

The pricing gate sets every price at or above a floor: the most the worker may pay per unit, times 1.08, rounded up to the next 5 cents (`vending/pricing.py`). So each unit sold returns at least its maximum cost once tax is taken out (price ÷ 1.08 is at least the maximum cost). Takings are counted from the machine's counters, hourly, before we send a request (`vending/ledger.py`).

So refilling the same products with what sold pays for itself. A worked example for the first full week, 3 to 9 Dec (5 class days, the Wednesday before a Friday-11-Dec Amazon order):

| | High rate (23 a class day) | Low rate (14 a class day) |
|---|---|---|
| Units sold | 115 | 70 |
| Takings at $3.35 a unit | $385.25 | $234.50 |
| Spendable after tax (÷ 1.08) | $356.71 | $217.13 |
| Refill at run 1's most per unit, $2.26 | $259.90 | $158.20 |

$3.35 is what our slots took per unit this fall: $1,881.50 ÷ 562 (`analysis/REPORT.md`, finding 1). $2.26 is run 1's most per unit: $360 ÷ 159 (#90). Our real prices will differ.

The catch is whole packs. Refilling 5 units of a 20 oz drink means buying a 24-pack. The extra units sit in the closet and are used at the next visit. That is working capital, which the reserves below cover.

If there is a regular Friday restock on 4 Dec, its request is due the night we go live (#107). Only about one class day will have sold, so it would be small.

### Product-switch reserve: $127.00

After the first stocked week, we expect to drop products that don't sell and try others. That needs new packs before the old stock is sold, so takings don't cover it. The reserve buys 2 packs at the dearest maximum on our price list (Coca-Cola 20 oz, $63.50): 2 × $63.50 = $127.00. That is 2 to 6 slots of new products, depending on how many slots a pack fills.

### Midweek visit fees: $50.00

A visit outside Friday costs $25, however much it loads (`docs/agent/restocking.md`). We hold back 2 visits: 2 × $25 = $50. A midweek visit only makes sense when an empty slot loses more than $25 before Friday. At $4.50, Red Bull makes about $2.71 a unit after tax (`docs/sample-directive.md`), so $25 is the profit on about 9 sales (25 ÷ 2.71 = 9.2).

### Model spend: $9.00

Every live model call is charged to the ledger at its real cost (`vending/harness.py`, #51). Only live calls from the ledger's opening on come out of the $600. Testing before that is development spend (#104).

- Observed so far: $0.02 to $0.80 a run (#90). Run 2 cost $0.40 (`docs/sample-directive.md`).
- The harness caps a run at $1.00 and a day at $4.00. Both are placeholders (#51).
- Nothing schedules the agent yet (#39, #107). We assume one live run a week.

So 9 run weeks × 1 run × $1.00 = $9.00. If the agent ran every day at the $4.00 daily cap, 63 days × $4.00 = $252.00, which is 42% of the $600. The daily cap should be set to fit this plan before the agent goes live (see "Open decisions").

### Safety margin: $14.00

This is the remainder. It is small because the other lines are already sized at the worker's maximum prices. In practice it is larger: the fill is unlikely to hit its cap, and the example fill at expected prices leaves $84.06 under it ($400.00 − $315.94).

### Sales tax: $0 from the capital

Shelf prices include 8% sales tax. The team set aside takings × 0.08 ÷ 1.08 each week (`docs/planning/2026-10-19-david-api-qa.md`, row 6). That is about 7.4% of takings, not 8%. The ledger does this on its own: the **spendable** balance is the balance less the tax set aside and not yet remitted (`vending/ledger.py`). So the tax is paid out of the sales that collected it and needs nothing from the $600. Purchases carry no tax: the Institute is tax-exempt (same file).

## 3. When the held-back money is released

- **At the first Friday restock:** spend only what refills sold units. Takings fund it (section 2).
- **From the second stocked week:** the switch reserve funds product changes for slots that meet the slow-seller rule (#100).
- **Midweek visit fees:** only when the trade in `docs/agent/restocking.md` says so, with the working shown.
- **Before the winter break:** classes stop 22 Dec to 1 Jan, and the Institute is closed on Fri 25 Dec and Fri 1 Jan (`analysis/golisano_calendar_2026_27.json`). Fill lightly on Fri 18 Dec. Stock that sits through the break holds cash we can't use.
- **Floor at all times:** after any request, the spendable balance should not drop below the fee and model lines ($50 + $9 = $59). Nothing in the code enforces this yet (see the follow-ups below).

## Assumptions

To number in `docs/assumptions.md`. They aren't numbered here because open PRs are already taking numbers (A16 in #69; A17 and A18 in #99).

1. **The day-one fill comes out of the $600.** The instructor said so on 12 Nov, but wasn't sure he had understood the question and said he would check (#106, row 3).
2. **Amazon prices hold near 11 Nov's.** The price list says they will change. The plan uses the worker's maximum, about 15% over, to cover that.
3. **The loading chart is right.** The chart calls its numbers approximate (`vending/capacity.py`).
4. **Our new products sell at about the rate our slots sold this fall when stocked**, 14 to 23 units a class day. Different products and prices may sell differently. The price history (#99, `docs/agent/price-history.md`) can't tell us how price changes sales.
5. **The agent runs live once a week.** No schedule is set (#39).
6. **The run is 9 weeks** from 3 Dec (`docs/agent/goal-and-scoring.md`).
7. **Closet stock is worth what we paid for it** at the end. The instructor believes so and is checking (`docs/planning/2026-10-19-david-api-qa.md`, row 16).

## Open decisions

These are the team's to make. This plan does not decide them.

1. **Which products go in the 20 slots on day one, and at what prices** (#107). The example mix is only a pricing example. Three of the top sellers this fall (Monster, Gorgie and Ghost, `analysis/REPORT.md`) aren't on the price list yet.
2. **The wildcard slot.** Kevin proposed one slot with a looser ceiling, about $8.00, against $6.50 for the rest (comment on #100). Not decided. Its effect on the fill is small: the dearest slot on the price list is one 24-pack at $55.50 most ($47.99 × 1.15 = $55.19, rounded up; $47.99 expected), against $57.00 to $63.50 for a 20 oz pack.
3. **The fill cap.** $400.00 at most is this plan's proposal.
4. **How many midweek visits to hold back for.** This plan says 2.
5. **The model caps and how often the agent runs** (#51, #39). The $4.00 daily cap doesn't fit a $9.00 line.
6. **The exact day-one deadline and how to send the order** (#102).

**Agent-facing follow-ups (not done here).** `docs/agent/money.md` says to follow the team's opening capital plan and to say so if there is none. Once this plan is agreed, it should point here, as #90 asks. #90 also names the file `docs/opening-capital-plan.md`. A rule that keeps $59 spendable after any request would need a check in the planner.

## Sources

- Issue #90 and its comment: the brief's wording, run 1's dry run (159 units, $293 expected, $360 most), the 70-a-week estimate and token spend per run.
- PR #106 (`docs/planning/2026-11-12-david-answers.md`) and issue #107: the instructor's 12 Nov answers.
- PR #100 and its comments: the $6.50 ceiling and the wildcard proposal.
- PR #99 (`docs/agent/price-history.md`): reference prices.
- PR #104: development and competition spend split at the ledger's opening.
- PR #51 and `vending/harness.py`: model-call costs and the run and daily caps.
- Branch `add_price_list` (`docs/agent/price-list.md`): Amazon packs and prices, 11 Nov 2026.
- `vending/capacity.py` and `vending/dashboard.py`: units per column from the loading chart.
- `vending/pricing.py`: the price floor.
- `vending/ledger.py`: balance, tax set-aside and spendable.
- `docs/agent/money.md`, `docs/agent/restocking.md`, `docs/agent/goal-and-scoring.md`: the account, the $25 fee and the scoring rules.
- `docs/sample-directive.md`: run 2's request ($233.31 expected, $270.00 most, $0.40 model cost).
- `docs/planning/2026-10-19-david-api-qa.md`: the sales-tax method and purchase tax.
- `analysis/REPORT.md`: units, takings and sales rates for our 20 slots.
- `analysis/golisano_calendar_2026_27.json`: the winter break and closures.
