# Restock and dispatch policy

## What this is

The project brief asks for a "documented restock and dispatch policy, with the derivation of its thresholds, including how it accounts for your available balance" (Project Brief (Revised), section 5; issue #91). This doc gives four rules for our 20 slots, and shows where each number comes from:

1. When a slot gets stock at the Friday visit.
2. When a $25 midweek visit pays.
3. When a slow seller is pulled.
4. How the balance limits every order.

`docs/agent/restocking.md` states the rules of the game (Friday free, $25 midweek, one request at a time). This doc sets the thresholds. Every number below cites a file or PR, or shows its arithmetic. Unsettled choices are under "Open decisions".

## The facts the policy rests on

| Fact | Value | Source |
|---|---|---|
| Regular visit | Friday afternoon, 3 PM target, likely 3 to 5 PM or later | David, 12 Nov (#106 row 14, `docs/planning/2026-11-12-david-answers.md`) |
| Friday request cutoffs | In-store: Thursday 23:59. Amazon: no hard deadline, but it must arrive in time; the planner warns after Wednesday 23:59 | #106 row 1 and "Checked against the calendar and our code"; `vending/planner.py` |
| Our sales after 3 PM | about 0.2 units an hour | `docs/cutover.md` (#105) |
| Machine units sold 8 to 11 AM | 66% | `analysis/REPORT.md`, finding 7 |
| Midweek visit | $25 a request, any number of slots; may take longer than 24 hours | `docs/agent/restocking.md`; config `midweek_fee_cents` |
| Open requests | One at a time today; David may lift this | `docs/process-map.md`; #106 row 12 |
| Column capacity | 7 (20 oz bottle), 8 (12 oz can), 11 (8.4 oz can) | `vending/capacity.py` (loading chart) |
| Sales tax inside the shelf price | 8% | `vending/pricing.py`, config `tax_percent` |
| Competition start | Thu 3 Dec. Before that, staff empty the machine and fill all 20 slots to capacity with our day-one choices, paid from our $600 (David to confirm the $600 part, #106 row 3). The ledger opening is recorded before the day-one fill is fulfilled. | #106 rows 2 and 3, "What changes for us"; #107 |
| Run length | to the end of run week 9 | `docs/agent/rules-card.md` |

**Sales rates.** The sales analysis groups our 20 slots into four segments by their own history. Rates are units per class day while the slot held stock (`analysis/REPORT.md`, "Our slots"):

| Segment | Units per in-stock class day | Average price paid |
|---|---|---|
| Fast seller | 1.701 | $2.626 |
| Mid seller, premium-priced | 1.354 | $4.326 |
| Slow seller, low-priced | 0.767 | $2.165 |
| Slow seller, erratic | 0.722 | $3.007 |

If every slot were kept full, the 20 slots together would sell about 23.3 units a class day (`analysis/REPORT.md`, "Next 4 weeks"). These rates come from this fall's products and prices. Our own drinks will sell at their own rates, so the agent replaces these with each slot's measured rate once it has one (see "When a rate counts" below).

**The gap between visits.** A Friday visit at 3 to 5 PM lands after Friday's busy morning. The next visit lands at the same time a week later. So one gap holds Monday to Thursday plus next Friday's morning: **5 class days** of sales in a normal week. Friday afternoon adds little (0.2 an hour). In a week with a holiday, count only the class days (`analysis/golisano_calendar_2026_27.json`; for example, the Winter Holiday Break is 22 Dec to 1 Jan).

## 1. Friday restock: how much stock a slot needs

**Rule.** Purchases go in the Friday request, not at the visit. In-store items must be in by Thursday 23:59. Amazon has no hard deadline, but the order must arrive in time, so we send Amazon items by Wednesday 23:59, when the planner starts to warn (#106 row 1, `vending/planner.py`). In that request:

1. **Fill every slot to capacity from the closet**, when the closet holds that slot's product. This costs no cash, so there is no threshold.
2. **Buy a pack for a slot when the stock it will have at the visit covers less than 2 weeks of sales.** Stock is units in the slot plus closet units of the same product, less the sales expected before the visit. In `get_machine_state` terms, buy when `(stock_on_hand + closet units - units_per_day x days until the visit) / units_per_day < 14`.

**Derivation.** Sales in a 5-class-day gap vary even when the rate is steady. We model a gap's sales as Poisson with mean 5 × the slot's rate, and ask how many units cover the gap 95% of the time:

| Segment | Mean sales per gap (5 × rate) | Units to cover 95% of gaps | Ratio to the mean |
|---|---|---|---|
| Fast | 8.5 | 14 | 1.65 |
| Mid | 6.8 | 11 | 1.62 |
| Slow, low-priced | 3.8 | 7 | 1.83 |
| Slow, erratic | 3.6 | 7 | 1.94 |

So the safety factor is 1.6 to 1.9. Poisson is a floor on the variability: it treats each rate as known, and the rates rest on 11 weeks of one machine (`analysis/REPORT.md`, "Caveats"). We round up to **2** (Proposed; see "Open decisions"). Two weeks of sales is the same as 14 days of stock left, because `units_per_day` is the last 7 calendar days' sales divided by 7 (`vending/tools/machine_state.py`). Rounding up is cheap: unsold units keep their cost in the closet (A5 in `docs/assumptions.md`), while a stockout loses the sale and the cash it would have returned (`docs/agent/money.md`).

**What the 95% target covers.** The target applies to the units we hold, slot plus closet, when we decide a purchase. The next purchase lands a week after this one, so this stock must refill the slot at this visit and carry it to the next. It does not promise that the slot sells through the gap. Closet units can't be sold until a visit puts them in the column, and one column caps what a slot can sell in one gap.

**What the numbers say about capacity.** One column holds the 95% gap cover only for a slow seller (7 units, any package) or a mid seller in 8.4 oz cans (11 units). For the rest, the same Poisson model gives the chance that a gap's sales are more than a full column, so the slot runs out before the next visit:

| Segment | 8.4 oz cans (11) | 12 oz cans (8) | 20 oz bottles (7) |
|---|---|---|---|
| Fast (mean 8.5) | 15.2% | 47.8% | 61.5% |
| Mid (mean 6.8) | 4.4% | 24.1% | 36.7% |

The fix is a product choice, not a visit: put fast and mid sellers in high-capacity packages, or give one product two columns (two 20 oz columns hold 14, the fast seller's 95% cover).

**When a rate counts.** A slot's own rate counts only after **5 class days** of our own sales while the slot held stock (Proposed; see "Open decisions"). Until then, use the segment rate we expect for that product (R3, Proposed), and buy when slot plus closet holds less than that segment's 95% cover from the table above (14, 11, 7 or 7). Do not use `units_per_day` before then. Its 7-day window can hold break days, when the machine was empty, so it can divide one day of sales by 7.

This matters on the first Friday. If there is a visit on Fri 4 Dec (#102), its in-store cutoff is Thu 3 Dec, our first day of sales, and Amazon's warning cutoff is Wed 2 Dec, before any sales. Every slot will be full from the day-one fill, and the closet will hold whatever the day-one packs left over. So a slot we expect to sell fast, in 12 oz cans (8 units) with no closet units, gets a pack (8 < 14). A slot we expect to sell slowly, full at 7, does not. The day-one pack choice (#90) sets most of this.

**Three cases the rule must not get wrong.**

- **The slot sold out inside the window.** `units_per_day` counts the empty days as zero, so it understates demand (A1 in `docs/assumptions.md`: sales are a floor). Use the slot's in-stock rate, or buy. No tool gives the in-stock rate yet (see "Agent follow-ups"), so until one does, buy.
- **The window held a holiday.** Its rate is near zero and says nothing about demand. Use the last full class week's rate, or the segment prior if there is none.
- **No rate yet** (a new product, or day one). Use the segment prior, as in "When a rate counts".

A slot with stock and zero sales has `days_of_stock_left` null, not zero (`_days_left` in `vending/tools/machine_state.py`). Null never triggers a purchase. That slot goes to rule 3. Note that `days_of_stock_left` uses slot stock only, not the closet, so it is not the figure in the rule above.

## 2. Midweek visit: when $25 pays

**Rule.** Ask for a midweek visit only when the margin it saves before Friday is more than $25:

> saved margin = sum over empty slots of (rate per class day × class days saved × margin per unit)

Plan on **2 class days saved** for a request sent Monday morning, and **1** for a request sent Tuesday. Send none after Tuesday. (Proposed; see "Open decisions".)

**Class days saved.** David said the worker gets out of class at 1 PM. He said this about the Friday visit (#106 row 14). That it also holds midweek is our inference (R5). So a midweek visit likely lands in the afternoon, after the morning rush. A Monday request filled Tuesday afternoon saves Wednesday, Thursday and Friday morning: 3 class days at best. A visit "may take longer than 24 hours" (`docs/agent/restocking.md`), so we plan on Wednesday afternoon: 2 class days. A Tuesday request filled Thursday afternoon saves only Friday morning: 1 class day. A Thursday request saves nothing over the Friday visit.

**Margin per unit.** Margin = shelf price ÷ 1.08 − unit cost. Our only real costs are the most-we-pay pack prices in run 2's request (`docs/sample-directive.md`). Using the most we pay gives the worst-case margin. The table rounds for display. The arithmetic below uses unrounded values.

| Product (run 2) | Unit cost at most | Price | Price ex tax | Margin | Break-even units (25 ÷ margin, rounded up) |
|---|---|---|---|---|---|
| Red Bull 8.4 oz | $40.50 ÷ 24 = $1.69 | $4.50 | $4.17 | $2.48 | 11 |
| Celsius 12 oz | $25.00 ÷ 12 = $2.08 | $4.50 | $4.17 | $2.08 | 13 |
| Mountain Dew 20 oz | $57.00 ÷ 24 = $2.38 | $3.25 | $3.01 | $0.63 | 40 |
| Coca-Cola 20 oz | $63.50 ÷ 24 = $2.65 | $3.50 | $3.24 | $0.59 | 43 |
| Diet Coke 20 oz | $59.00 ÷ 24 = $2.46 | $3.25 | $3.01 | $0.55 | 46 |

**Empty slots needed.** Use the segment priced near the product. In the fall data, the premium-priced segment sold at 1.354 a class day at an average $4.33. The fast segment's average price was $2.63 (`analysis/REPORT.md`, "Our slots").

- **Premium slots (Red Bull, Celsius) on a Monday request.** One empty Red Bull slot saves 1.354 × 2 = 2.708 units, worth 2.708 × $2.4792 = $6.71. A visit pays when **4 or more** are empty by Monday (25 ÷ 6.71 = 3.7). At the Celsius margin: 2.708 × $2.0833 = $5.64, so 5 slots (25 ÷ 5.64 = 4.4).
- **If a $4.50 product sells at the fast rate** (R3 wrong for it): 1.701 × 2 = 3.402 units × $2.4792 = $8.43, so 3 slots (25 ÷ 8.43 = 2.97).
- **Soda slots at the fast rate.** 3.402 units × margin: Mountain Dew $2.16 (25 ÷ 2.16 = 11.6, so 12 slots), Coca-Cola $2.02 (12.4, so 13), Diet Coke $1.87 (13.3, so 14). That is more than half our slots.
- **A Tuesday request** saves 1 class day, so each slot saves half as much and the counts roughly double. Red Bull at the premium rate needs 8 (25 ÷ (1.354 × $2.4792) = 7.4).

One empty slot never pays for a visit.

**When cash is short, the bar rises.** When the balance does not limit Friday's order, the fee costs $25 and nothing more: the stock we want still gets bought. `docs/agent/money.md` calls this "$25 of product we did not buy". When the balance does limit the order, the $25 pushes the last whole pack out of it, and we lose that pack's margin too. Stock comes in whole packs, so count the pack, not units:

| Pack the order drops | Cost at most | Margin if every unit sells | Bar for the visit | Premium Red Bull slots needed (bar ÷ $6.71) |
|---|---|---|---|---|
| Celsius 12-pack | $25.00 | 12 × $2.0833 = $25.00 | $50.00 | 8 (7.4) |
| Red Bull 24-pack | $40.50 | 24 × $2.4792 = $59.50 | $84.50 | 13 (12.6) |

The margin column is a ceiling: units that don't sell keep their cost in the closet (A5). In practice, a cash-short week waits for Friday.

**Riders.** A paid visit can carry price changes and top-ups for every slot at no extra fee, so put them all on the one request. Only one request may be open at a time (`request_pending`, `docs/process-map.md`). A midweek request must be fulfilled before the Friday request can go in. A Monday request fulfilled Wednesday afternoon leaves little time before the Amazon warning cutoff (Wednesday 23:59), so Amazon items for that Friday may miss it. David may lift the one-request limit (#106 row 12). Until he does, decide on a midweek visit before planning the Friday request.

## 3. Slow sellers: when to pull

This is the team's 12 Nov decision, written up in PR #100 (agreed 12 Nov, not yet merged; `docs/agent/restocking.md` on that branch). We use it as is:

- **Flag** a slot that sells nothing for **5 school days in a row while our records say it held stock** (a receipt or a person's count). Days in a break don't count. A slot that may be empty is not slow.
- **Act at the next Friday visit:** try a lower price, or swap the product. Pulled units go to the closet at what we paid, so a pull is not a write-off.

**Evidence** (`analysis/REPORT.md`, "Slow-seller rule"). Replayed over this fall, 5 class days of zero sales raised 75 flags. Of the 39 with an outcome, 4 (10%) were live slots and 20 (51%) sold again only after a refill, so they were probably empty. That is why the rule needs the held-stock check. The check is not validated: the fall data had no receipts.

**How it meets rules 1 and 2.** A flagged slot gets no new purchase. Its stock is pulled or repriced at Friday, and the new product starts with no rate (the "no rate yet" case in rule 1). Pricing for the swap follows the pricing method in PR #99 (`docs/agent/price-history.md`) and the $6.50 ceiling in PR #100 (agreed 12 Nov, not yet merged).

**Swap first, not mark down (Proposed by Kevin, 12 Nov; the team has not agreed).** The rule above allows either action. We propose the swap as the default:

- What we paid for the units is spent either way. The choice depends only on what the slot earns from here.
- Marking down earns little per sale. At the floor it earns almost nothing: the floor is cost x 1.08 rounded up to 5 cents, so a sale there keeps the unit's cost and at most 4 cents more (`vending/pricing.py`). And the slot stays tied up.
- A swap gives the slot to a product that sells, at a normal margin. The pull happens at the Friday visit, which is free. The pulled units stay in the closet at cost (R6, A5). They can come back later: as a wildcard test, or if demand returns.

So at a flag: allow **at most one price step down** (PR #99's step) if the slot has had no price test yet. If the slot is flagged again, swap it. Keep a slow seller only when it is expected to earn more over the weeks left than its replacement, at their prices. With a 9-week run, a product that sells almost nothing rarely passes that test.

This depends on R6. If end-of-run stock is valued at less than cost, closet units lose value, and marking down to the floor to recover cash becomes closer to a swap. The question is P2 on #102.

**Paying for a swap.** The new product's pack comes from takings and the capital plan's product-switch reserve ($68, one pack; `docs/opening-capital-plan.md`, #90). The first flag can't act before Fri 11 Dec: Thu 3 to Wed 9 Dec are the first 5 class days. So a week of takings is in by then.

**The wildcard slot (Proposed by Kevin on #100; the team has not agreed).** The agent picks one of our 20 slots as the wildcard. It names the slot in its plan, with a reason, and may move it only at a Friday visit. Its ceiling is about $8.00, against $6.50 for every other slot (PR #100). Its floor is the same as every slot's: cost × 1.08, rounded up to 5 cents (`vending/pricing.py`), which breaks even after tax. Kevin's aim is that every sale makes a profit, with no loss leaders; that needs a floor above today's (open). Ideas: a premium drink, a high-price test, a rotating feature, a seasonal bet, or an anchor. Rules 1 to 3 apply to it like any slot, including the slow-seller flag and swap first. The capital plan caps any one wildcard purchase at $82 at the most we pay (Proposed there, #90).

## 4. How the balance limits orders

**Rule.** After an order, at its most-we-pay cost, the spendable balance must still hold the capital plan's **spendable floor, $53** (`docs/opening-capital-plan.md`, #90; Proposed there). If it would not, cut the order in this order:

1. Drop purchases for slow sellers and flagged slots.
2. Drop purchases where the closet already covers a week.
3. Keep the slots with the most margin per class day (rate × margin), high-margin slots first.

**On day one, nothing can be dropped.** Staff fill all 20 slots to capacity, and a slot can't be left empty (#106 rows 2 and 10). So the day-one order meets the floor by product and pack choice: cheaper products, smaller packs, one pack shared across slots. That choice belongs to the capital plan (#90).

**Where $53 comes from.** The capital plan owns the number and its derivation. It covers the two costs that can leave the account before a Friday's takings come in: one midweek fee ($25.00, config `midweek_fee_cents`) and one week of model spend at the daily cap (7 × $4.00 = $28.00, config `model_daily_cap_cents`, default 400). The model cap is the most the code lets the agent spend, not what it usually spends (#90 reports $0.02 to $0.80 a run). Using the cap means token spend can never eat the stock money. If the team lowers the cap, the capital plan lowers the floor, and this rule follows.

**Why "spendable" and "most-we-pay".** Spendable is the balance less sales tax owed (`docs/agent/money.md`, `get_account_balance`). The planner already refuses any order whose most-we-pay cost exceeds spendable (`insufficient_balance` in `vending/planner.py`). The reserve is one step stricter. The order usually costs less than its maximum (run 2: $233.31 expected, $270.00 at most, `docs/sample-directive.md`), so the real cushion is larger.

**Tie to the capital plan (#90).** The capital plan decides how much of the $600 the day-one fill spends (David to confirm it comes from the $600, #106 row 3). This policy does not depend on that number. It needs one condition the plan meets: **after the day-one fill, at least $53 stays spendable**. The plan's proposed caps leave $157 after the fill at its most and one live run before 3 Dec ($600 − $360 − $82 − $1). From the first Friday on, sales refill the account. At the restocked rate of 23.3 units a class day, a 5-day week sells about 117 units. The cash that returns depends on our prices, so the reserve, not a sales forecast, is the floor.

## Agent follow-ups (separate PR, after #99 and #100)

The agent can't follow every step above with today's tools. Issue #91 is done only when the agent's rules card points here. These need a separate PR after #99 and #100 land, because both edit `docs/agent/`:

- A pointer to this doc from `docs/agent/rules-card.md` and `docs/agent/restocking.md`.
- An in-stock rate per slot: a tool field, or a stated way to compute it from receipts (`vending/stock.py`). No tool gives one today.
- A days-of-cover figure that includes the closet, or the arithmetic for the agent to do it. `days_of_stock_left` counts slot stock only.
- The "when a rate counts" check (5 class days of in-stock sales), so the agent knows when to stop using the segment prior.
- Align `docs/agent/money.md` on the fee: it costs $25 when cash doesn't bind, and $25 plus the dropped pack's margin when it does.

Until these land, the agent should buy when unsure.

## Assumptions

To number in `docs/assumptions.md` (open PRs already take A16 to A19).

| | Assumption | What would show it's wrong |
|---|---|---|
| R1 | A gap's sales in a stocked slot are Poisson around its rate, so the 95% cover is 1.6 to 1.9 times the mean. | Weekly sales of our own drinks varying more than that. |
| R2 | One gap between Friday visits holds 5 class days of sales, because the visit lands after Friday's morning rush. | Visits landing in the morning, or classes on weekends. |
| R3 | Fall segment rates are a fair prior for our new products until each slot has its own rate, and a product sells at the rate of the segment priced near it. | Our first weeks' rates far from these. |
| R4 | Run 2's most-we-pay pack prices stand for unit cost until receipts give real costs. | Receipts with different costs. |
| R5 | A midweek visit lands in the afternoon (David's 1 PM point was about Friday), 2 class days after a Monday request and 1 after a Tuesday request. | Receipts showing faster or slower fills, or morning visits. |
| R6 | Unsold stock is valued at cost at the end (A5), so over-stocking costs cash timing, not profit. | David valuing end stock at less than cost (#102). |
| R7 | The held-stock check in the slow-seller rule works on receipts and counts. | Flagged slots that turn out empty. |
| R8 | 5 class days of in-stock sales are enough for a slot's own rate to replace the segment prior. | Rates that keep moving a lot after 5 days. |

## Open decisions

- **Safety factor 2 and the 95% target.** Proposed here; the team has not agreed.
- **When a rate counts** (5 class days of in-stock sales). Proposed here; the demand doc (#93) uses the same rule.
- **The prior for a slot with no rate yet:** the fall segment rate for the product's price band (R3). Proposed here; the demand doc (#93) uses it for the buy rule and keeps category shares for ranking.
- **The $53 spendable floor.** Proposed in the capital plan (#90), which owns it; rule 4 uses it.
- **The midweek trigger** (4 or more premium-priced slots empty by Monday; 8 by Tuesday; none after Tuesday). Proposed here. The capital plan (#90) holds back 2 visits ($50).
- **The order of cuts when the floor binds** (rule 4). Proposed here.
- **Day-one stock from the $600.** David to confirm (#106 row 3).
- **First regular restock:** Fri 4 Dec or Fri 11 Dec (#102).
- **Day-one deadline:** about Fri 27 Nov, which falls in the Institute's 26 to 29 Nov closure; the exact day and time are still being asked (#102).
- **More than one open request** (#106 row 12). It would remove the clash between a midweek request and the Friday request.
- **A Friday visit in the Winter Holiday Break** (25 Dec, 1 Jan): whether one happens. Not yet asked.
- **Swap first at a slow-seller flag** (at most one price step down, then swap; pulled units to the closet at cost). Proposed by Kevin, 12 Nov; depends on R6 (#102, P2).
- **The wildcard slot** (Kevin's proposal on #100: the agent picks one slot, names it in its plan with a reason, and moves it only at a Friday visit; ceiling about $8.00; the floor is the same as every other slot). Not decided. It changes that slot's margin, not these rules. Its $82 purchase cap is the capital plan's (#90).
- **The floor: break-even or profit.** Today's floor breaks even after tax (`vending/pricing.py`). Kevin's aim that every sale makes a profit needs a higher floor. Not decided.
- **A rule for slow-but-steady slots.** None until we have sales of our own drinks (#100).

## Sources

- Project Brief (Revised), sections 3.3, 3.4 and 5, as quoted in `docs/agent/restocking.md` and `docs/agent/money.md`.
- `analysis/REPORT.md`: segment rates and prices, the restocked scenario, time of day, the slow-seller simulation, caveats.
- `analysis/golisano_calendar_2026_27.json`: class days and breaks.
- `vending/tools/machine_state.py`: `units_per_day`, `days_of_stock_left`, the 7-day window.
- `vending/stock.py`: stock on hand and the closet, from receipts.
- `vending/capacity.py`: units per column.
- `vending/config.py`, `vending/pricing.py`, `vending/planner.py`: fee, tax, model caps, store cutoffs, the balance check.
- `docs/sample-directive.md`: run 2's pack prices and order cost.
- `docs/process-map.md`: one open request at a time.
- PR #99 (pricing method; not yet merged), PR #100 (slow-seller rule and $6.50 ceiling; agreed 12 Nov, not yet merged), PR #105 (`docs/cutover.md`), PR #106 (David's 12 Nov answers, `docs/planning/2026-11-12-david-answers.md`; merged 12 Nov).
- `docs/opening-capital-plan.md` (#90): the spendable floor, the switch reserve and the wildcard cap.
- Issues #90 (capital plan), #91 (this deliverable), #102 (open questions), #107 (actions from 12 Nov).
