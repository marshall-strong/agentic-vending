# Restock and dispatch policy

## What this is

The project brief asks for a "documented restock and dispatch policy, with the derivation of its thresholds, including how it accounts for your available balance" (Project Brief (Revised), section 5; issue #91). This doc gives four rules for our 20 slots, and shows where each number comes from:

1. What every restock loads: each slot to its column.
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
| What a visit can change | Anything in our slots, at any restock visit, the free Friday one or a paid $25 midweek one: any drink, price or stock, a full fresh board, with no limit on units or lines. A confirmed fact | Kevin, 19 Nov |
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

## 1. Each restock: fill every slot to its column

**Rule** (Kevin, 19 Nov). At every restock visit, the free Friday one or a paid $25 midweek one, fill every one of our slots to its column. All columns are the same, so a column's capacity depends only on the drink's package: 7 for a 20 oz bottle, 8 for a 12 oz can, 11 for an 8.4 oz can (`vending/capacity.py`). Almost every restock is the free Friday one, because $25 is a lot (rule 2). Order for Friday by the store cutoffs: in-store items by Thursday 23:59, Amazon items by Wednesday 23:59 so they can arrive by Friday. The Amazon cutoff is a warning, not a deadline: a late Amazon item is stocked at the next scheduled visit (David, 17 Nov).

1. **Closet first.** A slot loads from the closet when the closet covers its whole gap to the column.
2. **Buy the rest.** When the closet can't fill the column, buy whole packs. Units beyond the column go to the closet for the next restock.
3. **Exceptions**, each named as the slot's reason in the plan:
   - A flagged slow seller is swapped, not refilled (rule 3).
   - The wildcard follows `docs/agent/wildcard.md`: at most $82.00 a purchase, and no loading more of its product while another slot holds it.
   - **The $53.00 spendable floor** (rule 4). Fill every column as long as spendable stays at or above $53.00 after the request, at most-we-pay cost. If it can't, fill the fastest sellers (`units_per_class_day`) first and name the slots left short.
4. **Breaks and the end.** Filling is still right before the Winter Holiday Break and in the last weeks. End stock counts at cost (A5, confirmed), so unsold units cost no profit.

This replaces the earlier rule 1: fill from the closet, then buy a pack only when slot plus closet covered less than 2 weeks of sales.

**Known limit: a closet that covers only part of a gap is skipped.** The planner loads each line from one source (`vending/planner.py`, step 4, "Allocate"). If the closet covers only part of a slot's gap, the planner buys the whole gap and those closet units wait for a later restock. The preview warns, and names the `fill_to` that would load them instead, but that leaves the slot part-filled, so the rule above buys. **Follow-up:** let one line load the closet's units and buy only the rest. The planner is unchanged for now.

**What the 2-week figure still means.** It is no longer a buy trigger, and it doesn't set pack counts: the planner buys the fewest whole packs that fill the column, and the rest go to the closet. The derivation below now answers one question: does a full column last a gap between restocks? Where it doesn't, the answer is a second column or a larger-capacity package, not more visits.

**Derivation.** Model a gap's sales as Poisson with mean 5 × rate. Units that cover 95% of gaps:

| Segment | Mean sales per gap (5 × rate) | Units to cover 95% of gaps | Ratio to the mean |
|---|---|---|---|
| Fast | 8.5 | 14 | 1.65 |
| Mid | 6.8 | 11 | 1.62 |
| Slow, low-priced | 3.8 | 7 | 1.83 |
| Slow, erratic | 3.6 | 7 | 1.94 |

The factor is 1.6 to 1.9. Poisson understates the spread (rates rest on 11 weeks of one machine; `analysis/REPORT.md`, "Caveats"), so we round up to **2** (Proposed): a slot should hold about 2 weeks of mean sales. Holding more is cheap: unsold units keep their cost (A5); a stockout loses the sale.

**Capacity limits.** One column holds the 95% cover only for slow sellers (7) or mid sellers in 8.4 oz cans (11). Chance a gap's sales exceed a full column:

| Segment | 8.4 oz cans (11) | 12 oz cans (8) | 20 oz bottles (7) |
|---|---|---|---|
| Fast (mean 8.5) | 15.2% | 47.8% | 61.5% |
| Mid (mean 6.8) | 4.4% | 24.1% | 36.7% |

The fix is product choice, not visits: high-capacity packages, or two columns for one product (two 20 oz columns hold 14, the fast seller's 95% cover).

**When a rate counts.** After **5 class days** of our in-stock sales (Proposed). Before that, use the segment rate priced near the product (R3, Proposed). Rates matter here for two things: which slots to fill first when cash is short (exception 3), and which products need a second column.

**Edge cases.**

- **Sold out in the window:** `units_per_day` counts empty days as zero, so it understates demand (A1). Rank with `units_per_class_day` from `get_machine_state`, which leaves empty days out. The slot is filled either way.
- **Holiday in the window:** use the last full class week's rate, or the segment prior.
- **No rate yet** (new product, day one): segment prior.
- **Stock, zero sales:** after 5 class days holding stock with no sale, the slot is a slow seller: swap it, don't refill it (rule 3).

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
- **Act at the next restock (normally Friday):** lower the price or swap the product. Pulled units go to the closet at cost.

**Proposed here (not in PR #100):** a flagged slot gets no new purchase; a swapped-in product is priced by PR #99's method (open) under the $6.50 ceiling.

**Evidence** (`analysis/REPORT.md`, "Slow-seller rule"): over the fall, 75 flags. Of 39 with an outcome, 4 (10%) were live and 20 (51%) sold again only after a refill (probably empty). Hence the held-stock check, unvalidated (no fall receipts).

**Swap first (Proposed by Kevin, 12 Nov; not agreed).** At a flag, allow **at most one price step down** (PR #99's step), then swap; pulled units go to the closet at cost. The units' cost is spent either way. At the floor (cost × 1.08, rounded up to 5 cents; `vending/pricing.py`) a sale keeps at most 4 cents over cost and ties up the slot; a swap at the free Friday visit gives it to a product that sells. This depends on A5 (R6): if end stock is valued below cost, a markdown gets closer to a swap (#102, P2).

**Paying for a swap:** takings plus the switch reserve ($68, one pack; #90). The first flag can't act before Fri 11 Dec (Thu 3 to Wed 9 Dec are the first 5 class days), so a week of takings is in.

**The wildcard slot (Kevin, #100; adopted on #110, item 1, 16 Nov; fixed, 18 Nov).** One fixed slot of our 20 tests products for the other 19 (`docs/agent/wildcard.md`). It is set once after the slot redraw (config `wildcard_slot`) and never moves. A product on trial there is priced as a regular slot would be. It passes at 4 units or more over 5 in-stock class days or more the polls saw (`get_machine_state`, `wildcard_trial`); 3 extends it one Friday cycle, fewer drop it, and by 10 days it needs 7. A pass is copied into a silent slot, a regular slot that sold nothing since the last Friday visit over 4 watched in-stock class days with its stock confirmed; that slot's product is pulled to the closet at cost, as for a flag above, and the wildcard switches to the next product in the same Friday plan. With no silent slot, the pass stays in the wildcard. Ceiling $8.00 for a deliberate price test (others $6.50, PR #100); same floor as every slot. Purchase cap $82 (#90).

## 4. How the balance limits orders

**Rule.** After an order, at its most-we-pay cost, the spendable balance must still hold the **spendable floor, $53** (Proposed in #90, which owns it). If it would not, cut in this order (Proposed):

1. Purchases for slow sellers and flagged slots.
2. Purchases where the closet already covers a week.
3. Then keep slots by margin per class day (rate × margin), highest first.

**Day one:** all 20 slots must be filled (#106 rows 2 and 10), so nothing can be dropped. The floor is met by product and pack choice (#90).

**Where $53 comes from** (#90): one midweek fee ($25.00, `midweek_fee_cents`) + one week of model spend at the daily cap (7 × $4.00 = $28.00, `model_daily_cap_cents`). The cap is the code's maximum, not typical spend ($0.02 to $0.80 a run, #90).

**Spendable** = balance less sales tax owed (`get_account_balance`). The planner refuses orders above spendable (`insufficient_balance`); the floor is stricter.

**Condition on the capital plan:** at least $53 spendable after the day-one fill. Its caps leave $157 ($600 − $360 for 19 regular slots − $82 for the wildcard − $1). Then sales refill the account (about 23.3 × 5 = 117 units a week; cash depends on prices).

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

- **Safety factor 2 and the 95% target.** Proposed here; now used only to judge whether a column lasts a gap (rule 1).
- **When a rate counts** (5 class days of in-stock sales). Proposed here; #93 uses the same rule.
- **Prior for a slot with no rate:** the fall segment rate for the product's price band (R3). Proposed here; #93 uses it for the buy rule.
- **The $53 spendable floor.** Proposed in #90, which owns it.
- **Midweek trigger** (4 or more premium-priced slots empty by Monday; 8 by Tuesday; none after). Proposed here. #90 holds back 2 visits ($50).
- **Order of cuts when the floor binds** (rule 4). Proposed here.
- **Fill every slot to its column at each restock, closet first, subject to the $53 floor** (rules 1 and 4). Decided by Kevin, 19 Nov; it replaces slot plus closet against 2 weeks of sales.
- **No purchase for a flagged slot; swaps priced by PR #99 under $6.50** (rule 3). Proposed here.
- **Swap first at a slow-seller flag** (rule 3). Proposed by Kevin, 12 Nov; depends on R6 (#102, P2).
- **The wildcard slot** (rule 3; ceiling $8.00, same floor). Kevin's proposal on #100, adopted on #110 (item 1), 16 Nov; rules in `docs/agent/wildcard.md`, $82 line in `docs/opening-capital-plan.md`. How a trial is judged (4 units over 5 in-stock class days into a silent slot, Kevin, 18 Nov; below that, research rule 6) is Proposed here.
- **The floor: break-even or profit.** Today's breaks even after tax. Kevin proposed (unit cost + $0.25) × 1.08, rounded up to 5 cents, with a setting `min_margin_cents` (#100). Not decided.
- **Day-one stock from the $600.** David to confirm (#106 row 3).
- **First regular restock:** answered 17 Nov: a valid request received by 23:59 Thu 3 Dec is filled at the Fri 4 Dec visit.
- **Day-one deadline:** answered 17 Nov: catalog approvals by Wed 25 Nov, the fill request by 23:59 Sun 29 Nov, stocking Mon 30 Nov (`docs/planning/2026-11-17-david-answers.md`).
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
