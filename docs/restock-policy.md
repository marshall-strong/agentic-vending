# Restock and dispatch policy

## What this is

The project brief asks for a "documented restock and dispatch policy, with the derivation of its thresholds, including how it accounts for your available balance" (Project Brief (Revised), section 5; issue #91). This doc gives four rules for our 20 slots, and shows where each number comes from:

1. When a slot gets stock at the Friday visit.
2. When a $25 midweek visit pays.
3. When a slow seller is pulled.
4. How the balance limits every order.

`docs/agent/restocking.md` states the game's rules; this doc sets the thresholds. Money numbers belong to the capital plan (`docs/opening-capital-plan.md`, #90); rate and stock estimates to the demand doc (`docs/inventory-demand.md`, #93).

## The facts the policy rests on

| Fact | Value | Source |
|---|---|---|
| Regular visit | Friday afternoon, 3 PM target, likely 3 to 5 PM or later | #106 row 14 (`docs/planning/2026-11-12-david-answers.md`) |
| Friday request cutoffs | In-store: Thursday 23:59. Amazon: no hard deadline; the planner warns after Wednesday 23:59 | #106 row 1; `vending/planner.py` |
| Our sales on Friday afternoons | 0.2 to 0.4 units an hour from 2 to 4 PM; under 0.7 an hour after 1 PM | `docs/cutover.md` (PR #105, open) |
| Machine units sold 8 to 11 AM | 66% | `analysis/REPORT.md`, finding 7 |
| Midweek visit | $25 a request, any number of slots; may take longer than 24 hours | `docs/agent/restocking.md`; config `midweek_fee_cents` |
| Open requests | One at a time today; David may lift this | `docs/process-map.md`; #106 row 12 |
| Column capacity | 7 (20 oz bottle), 8 (12 oz can), 11 (8.4 oz can) | `vending/capacity.py` |
| Sales tax inside the shelf price | 8% | `vending/pricing.py`, config `tax_percent` |
| Competition start | Thu 3 Dec. Staff first fill all 20 slots to capacity from our $600 (David to confirm the $600 part; whether a slot may take fewer units is still asked, #102). The ledger opening is recorded before that fill. | #106 rows 2 and 3; #107 |
| Run length | to the end of run week 9 | `docs/agent/rules-card.md` |

**Sales rates.** Units per class day while the slot held stock, by segment of our 20 slots (`analysis/REPORT.md`, "Our slots"):

| Segment | Units per in-stock class day | Average price paid |
|---|---|---|
| Fast seller | 1.701 | $2.626 |
| Mid seller, premium-priced | 1.354 | $4.326 |
| Slow seller, low-priced | 0.767 | $2.165 |
| Slow seller, erratic | 0.722 | $3.007 |

Kept full, the 20 slots would sell about 23.3 units a class day (`analysis/REPORT.md`, "Next 4 weeks"). These fall rates are for other products; each slot's own rate replaces them (rule 1).

**The gap between visits: 5 class days.** A Friday afternoon visit lands after the morning rush, so one gap holds Monday to Thursday plus next Friday morning. Friday afternoon adds little (0.2 to 0.4 an hour from 2 to 4 PM). Holiday weeks count class days only (`analysis/golisano_calendar_2026_27.json`; Winter Holiday Break 22 Dec to 1 Jan).

## 1. Friday restock: how much stock a slot needs

**Rule.** In the Friday request (in-store items by Thursday 23:59, Amazon items by Wednesday 23:59):

1. **Fill every slot to capacity from the closet.** No cash, so no threshold.
2. **Buy a pack when slot plus closet at the visit covers less than 2 weeks of sales:** `(stock_on_hand + closet units - units_per_day x days until the visit) / units_per_day < 14`.

**Derivation.** Model a gap's sales as Poisson with mean 5 × rate. Units that cover 95% of gaps:

| Segment | Mean sales per gap (5 × rate) | Units to cover 95% of gaps | Ratio to the mean |
|---|---|---|---|
| Fast | 8.5 | 14 | 1.65 |
| Mid | 6.8 | 11 | 1.62 |
| Slow, low-priced | 3.8 | 7 | 1.83 |
| Slow, erratic | 3.6 | 7 | 1.94 |

The factor is 1.6 to 1.9. Poisson understates the spread (rates rest on 11 weeks of one machine; `analysis/REPORT.md`, "Caveats"), so we round up to **2** (Proposed). 2 weeks = 14 days, because `units_per_day` is the last 7 calendar days' sales ÷ 7 (`vending/tools/machine_state.py`). Rounding up is cheap: unsold units keep their cost (A5); a stockout loses the sale.

**Why slot plus closet.** 14 days of `units_per_day` is 2 weeks of mean sales, which equals one gap's 95% cover (1.6 to 1.9 × the mean, rounded to 2), not two gaps' need. Closet units count because step 1 loads them into the slot at this visit. Only slot units serve the gap, so where the cover exceeds a column (below) the excess waits in the closet for next Friday (Proposed).

**Capacity limits.** One column holds the 95% cover only for slow sellers (7) or mid sellers in 8.4 oz cans (11). Chance a gap's sales exceed a full column:

| Segment | 8.4 oz cans (11) | 12 oz cans (8) | 20 oz bottles (7) |
|---|---|---|---|
| Fast (mean 8.5) | 15.2% | 47.8% | 61.5% |
| Mid (mean 6.8) | 4.4% | 24.1% | 36.7% |

The fix is product choice, not visits: high-capacity packages, or two columns for one product (two 20 oz columns hold 14, the fast seller's 95% cover).

**When a rate counts.** After **5 class days** of our in-stock sales (Proposed). Before that, use the segment rate priced near the product (R3, Proposed), and buy when slot plus closet is below that segment's 95% cover (14, 11, 7 or 7), not `units_per_day`. On the first Friday (Fri 4 Dec, if there is a visit; #102) the cutoffs fall on or before our first sales day, so an expected-fast slot in 12 oz cans with no closet units gets a pack (8 < 14); a slow slot full at 7 does not.

**Edge cases.**

- **Sold out in the window:** `units_per_day` counts empty days as zero, so it understates demand (A1). No tool gives the in-stock rate yet, so buy.
- **Holiday in the window:** use the last full class week's rate, or the segment prior.
- **No rate yet** (new product, day one): segment prior.
- **Stock, zero sales:** `days_of_stock_left` is null, and null never triggers a purchase (`_days_left`). Go to rule 3. That field also ignores the closet, so it is not the rule's figure.

## 2. Midweek visit: when $25 pays

**Rule.** Ask for a midweek visit only when the margin it saves before Friday is more than $25 (no substitution between our slots, R9):

> saved margin = sum over empty slots of (rate per class day × class days saved × margin per unit)

Plan on **2 class days saved** for a Monday-morning request, **1** for Tuesday, none after Tuesday (Proposed).

**Class days saved.** Visits land after 1 PM (said of Friday, #106 row 14; midweek is our inference, R5). Monday's request filled Tuesday saves 3 days at best; visits "may take longer than 24 hours", so plan on Wednesday: 2. Tuesday's, filled Thursday: 1. Thursday's: 0.

**Margin per unit** = shelf price ÷ 1.08 − unit cost, at run 2's most-we-pay pack prices (`docs/sample-directive.md`), so worst case. The table rounds; the arithmetic uses unrounded values.

| Product (run 2) | Unit cost at most | Price | Price ex tax | Margin | Break-even units (25 ÷ margin, rounded up) |
|---|---|---|---|---|---|
| Red Bull 8.4 oz | $40.50 ÷ 24 = $1.69 | $4.50 | $4.17 | $2.48 | 11 |
| Celsius 12 oz | $25.00 ÷ 12 = $2.08 | $4.50 | $4.17 | $2.08 | 12 |
| Mountain Dew 20 oz | $57.00 ÷ 24 = $2.38 | $3.25 | $3.01 | $0.63 | 40 |
| Coca-Cola 20 oz | $63.50 ÷ 24 = $2.65 | $3.50 | $3.24 | $0.59 | 43 |
| Diet Coke 20 oz | $59.00 ÷ 24 = $2.46 | $3.25 | $3.01 | $0.55 | 46 |

**Empty slots needed** (rate of the segment priced near the product):

| Case | Saved per empty slot | Slots needed |
|---|---|---|
| Red Bull, premium rate, Monday | 1.354 × 2 = 2.708 units × $2.4792 = $6.71 | **4** (25 ÷ 6.71 = 3.7) |
| Celsius, premium rate, Monday | 2.708 × $2.0833 = $5.64 | 5 (4.4) |
| A $4.50 product at the fast rate (R3 wrong), Monday | 1.701 × 2 = 3.402 × $2.4792 = $8.43 | 3 (2.97) |
| Mountain Dew, fast rate, Monday | 3.402 × margin = $2.16 | 12 (11.6) |
| Coca-Cola, fast rate, Monday | $2.02 | 13 (12.4) |
| Diet Coke, fast rate, Monday | $1.87 | 14 (13.3) |
| Red Bull, premium rate, Tuesday | 1.354 × 1 × $2.4792 | **8** (25 ÷ (1.354 × $2.4792) = 7.4) |

One empty slot never pays; soda needs over half our slots empty.

**When cash is short, the bar rises.** If cash does not limit Friday's order, a visit costs $25. If it does, the $25 also pushes the last whole pack out of the order, with its margin:

| Pack the order drops | Cost at most | Margin if every unit sells | Bar for the visit | Premium Red Bull slots needed (bar ÷ $6.71) |
|---|---|---|---|---|
| Celsius 12-pack | $25.00 | 12 × $2.0833 = $25.00 | $50.00 | 8 (7.4) |
| Red Bull 24-pack | $40.50 | 24 × $2.4792 = $59.50 | $84.50 | 13 (12.6) |

The margin is a ceiling (A5). In practice, a cash-short week waits for Friday.

**Riders.** Put all price changes and top-ups on the paid request. Only one request may be open (`request_pending`), so decide on a midweek visit before planning Friday.

## 3. Slow sellers: when to pull

The team's 12 Nov decision (PR #100, agreed, not yet merged):

- **Flag** a slot with no sales for **5 school days in a row while records (a receipt or a count) say it held stock**. Break days don't count. A slot that may be empty is not slow.
- **Act at the next Friday visit:** lower the price or swap the product. Pulled units go to the closet at cost.

**Proposed here (not in PR #100):** a flagged slot gets no new purchase; a swapped-in product is priced by PR #99's method (open) under the $6.50 ceiling.

**Evidence** (`analysis/REPORT.md`, "Slow-seller rule"): over the fall, 75 flags. Of 39 with an outcome, 4 (10%) were live and 20 (51%) sold again only after a refill (probably empty). Hence the held-stock check, unvalidated (no fall receipts).

**Swap first (Proposed by Kevin, 12 Nov; not agreed).** At a flag, allow **at most one price step down** (PR #99's step), then swap; pulled units go to the closet at cost. The units' cost is spent either way. At the floor (cost × 1.08, rounded up to 5 cents; `vending/pricing.py`) a sale keeps at most 4 cents over cost and ties up the slot; a swap at the free Friday visit gives it to a product that sells. This depends on A5 (R6): if end stock is valued below cost, a markdown gets closer to a swap (#102, P2).

**Paying for a swap:** takings plus the switch reserve ($68, one pack; #90). The first flag can't act before Fri 11 Dec (Thu 3 to Wed 9 Dec are the first 5 class days), so a week of takings is in.

**The wildcard slot (Proposed by Kevin on #100; not agreed).** The agent picks one of our 20 slots, names it in its plan with a reason, and moves it only at a Friday visit. Ceiling about $8.00 (others $6.50, PR #100); same floor as every slot. Aim: every sale profits, no loss leaders, which needs a floor above today's break-even (open). Ideas: premium drink, high-price test, rotating feature, seasonal bet, anchor. Purchase cap $82 (#90).

## 4. How the balance limits orders

**Rule.** After an order, at its most-we-pay cost, the spendable balance must still hold the **spendable floor, $53** (Proposed in #90, which owns it). If it would not, cut in this order (Proposed):

1. Purchases for slow sellers and flagged slots.
2. Purchases where the closet already covers a week.
3. Then keep slots by margin per class day (rate × margin), highest first.

**Day one:** all 20 slots must be filled (#106 rows 2 and 10), so nothing can be dropped. The floor is met by product and pack choice (#90).

**Where $53 comes from** (#90): one midweek fee ($25.00, `midweek_fee_cents`) + one week of model spend at the daily cap (7 × $4.00 = $28.00, `model_daily_cap_cents`). The cap is the code's maximum, not typical spend ($0.02 to $0.80 a run, #90).

**Spendable** = balance less sales tax owed (`get_account_balance`). The planner refuses orders above spendable (`insufficient_balance`); the floor is stricter.

**Condition on the capital plan:** at least $53 spendable after the day-one fill. Its caps leave $233 ($600 − $366 − $1), or $157 if the team adopts the wildcard option ($600 − $360 − $82 − $1). Then sales refill the account (about 23.3 × 5 = 117 units a week; cash depends on prices).

## Agent follow-ups (separate PR, after #99 and #100)

Once the team signs off, the agent needs: pointers from `docs/agent/rules-card.md` and `restocking.md`; an in-stock rate per slot (`vending/stock.py`); days of cover including the closet; and the "when a rate counts" check. Until then, the agent buys when unsure.

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
| R9 | No substitution between our slots: a buyer who finds a slot empty buys nothing from us (unmeasured; `docs/inventory-demand.md`). If some switch, rule 2's saved margin is too high. | Sales in our other slots rising while one is empty. |

## Open decisions

- **Safety factor 2 and the 95% target.** Proposed here.
- **When a rate counts** (5 class days of in-stock sales). Proposed here; #93 uses the same rule.
- **Prior for a slot with no rate:** the fall segment rate for the product's price band (R3). Proposed here; #93 uses it for the buy rule.
- **The $53 spendable floor.** Proposed in #90, which owns it.
- **Midweek trigger** (4 or more premium-priced slots empty by Monday; 8 by Tuesday; none after). Proposed here. #90 holds back 2 visits ($50).
- **Order of cuts when the floor binds** (rule 4). Proposed here.
- **Slot plus closet in rule 1**, and **no purchase for a flagged slot; swaps priced by PR #99 under $6.50** (rule 3). Proposed here.
- **Swap first at a slow-seller flag** (rule 3). Proposed by Kevin, 12 Nov; depends on R6 (#102, P2).
- **The wildcard slot** (rule 3; ceiling about $8.00, same floor). Proposed by Kevin on #100; the team decides on #110 (item 1). Its $82 cap is an option in #90, not part of the plan.
- **The floor: break-even or profit.** Today's breaks even after tax. Kevin proposed (unit cost + $0.25) × 1.08, rounded up to 5 cents, with a setting `min_margin_cents` (#100). Not decided.
- **Day-one stock from the $600.** David to confirm (#106 row 3).
- **First regular restock:** Fri 4 Dec or Fri 11 Dec (#102).
- **Day-one deadline:** about Fri 27 Nov, inside the Institute's 26 to 29 Nov closure; time still being asked (#102).
- **More than one open request** (#106 row 12).
- **A Friday visit in the Winter Holiday Break** (25 Dec, 1 Jan). Not yet asked.
- **A rule for slow-but-steady slots.** None until we have sales of our own drinks (#100).

## Sources

- Project Brief (Revised), sections 3.3, 3.4 and 5, as quoted in `docs/agent/restocking.md` and `docs/agent/money.md`.
- `analysis/REPORT.md` (segment rates, restocked scenario, time of day, slow-seller simulation, caveats); `analysis/golisano_calendar_2026_27.json`.
- `vending/tools/machine_state.py`, `vending/stock.py`, `vending/capacity.py`, `vending/config.py`, `vending/pricing.py`, `vending/planner.py`.
- `docs/sample-directive.md` (run 2 pack prices and order cost); `docs/process-map.md` (one open request).
- PR #99 (pricing method; not merged), PR #100 (slow-seller rule, $6.50 ceiling; agreed 12 Nov, not merged), PR #105 (`docs/cutover.md`; open), PR #106 (David's 12 Nov answers; merged).
- `docs/opening-capital-plan.md` (#90); `docs/inventory-demand.md` (#93): sibling branches, to merge with this one.
- Issues #90, #91 (this deliverable), #102 (open questions), #107 (actions from 12 Nov).
