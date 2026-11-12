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
| Regular visit | Friday afternoon, 3 PM target, likely 3 to 5 PM or later | David, 12 Nov (#106, `docs/planning/2026-11-12-david-answers.md`) |
| Our sales after 3 PM | about 0.2 units an hour | `docs/cutover.md` (#105) |
| Machine units sold 8 to 11 AM | 66% | `analysis/REPORT.md`, finding 7 |
| Midweek visit | $25 a request, any number of slots; may take longer than 24 hours | `docs/agent/restocking.md`; config `midweek_fee_cents` |
| Column capacity | 7 (20 oz bottle), 8 (12 oz can), 11 (8.4 oz can) | `vending/capacity.py` (loading chart) |
| Sales tax inside the shelf price | 8% | `vending/pricing.py`, config `tax_percent` |
| Competition start | Thu 3 Dec; staff fill all 20 slots to capacity from our $600 first | #106, #107 |
| Run length | to the end of run week 9 | `docs/agent/rules-card.md` |

**Sales rates.** The sales analysis groups our 20 slots into four segments by their own history. Rates are units per class day while the slot held stock (`analysis/REPORT.md`, "Our slots"):

| Segment | Units per in-stock class day |
|---|---|
| Fast seller | 1.701 |
| Mid seller | 1.354 |
| Slow seller, low-priced | 0.767 |
| Slow seller, erratic | 0.722 |

If every slot were kept full, the 20 slots together would sell about 23.3 units a class day (`analysis/REPORT.md`, "Next 4 weeks"). These rates come from this fall's products and prices. Our own drinks will sell at their own rates, so the agent replaces these with each slot's measured rate once it has one.

**The gap between visits.** A Friday visit at 3 to 5 PM lands after Friday's busy morning. The next visit lands at the same time a week later. So one gap holds Monday to Thursday plus next Friday's morning: **5 class days** of sales in a normal week. Friday afternoon adds little (0.2 an hour). In a week with a holiday, count only the class days (`analysis/golisano_calendar_2026_27.json`; for example, the Winter Holiday Break is 22 Dec to 1 Jan).

## 1. Friday restock: how much stock a slot needs

**Rule.** At each Friday visit:

1. **Fill every slot to capacity from the closet**, when the closet holds that slot's product. This costs no cash, so there is no threshold.
2. **Buy a pack for a slot when its stock in hand covers less than 2 weeks of sales.** Stock in hand is units in the slot plus closet units of the same product. In `get_machine_state` terms: buy when `(stock_on_hand + closet units) / units_per_day < 14`.

**Derivation.** Sales in a 5-class-day gap vary even when the rate is steady. We model a week's sales as Poisson with mean 5 × the slot's rate, and ask how many units cover the week 95% of the time:

| Segment | Mean sales per gap (5 × rate) | Units to cover 95% of weeks | Ratio to the mean |
|---|---|---|---|
| Fast | 8.5 | 14 | 1.65 |
| Mid | 6.8 | 11 | 1.62 |
| Slow, low-priced | 3.8 | 7 | 1.83 |
| Slow, erratic | 3.6 | 7 | 1.94 |

So the safety factor is 1.6 to 1.9. Poisson is a floor on the variability: it treats each rate as known, and the rates rest on 11 weeks of one machine (`analysis/REPORT.md`, "Caveats"). We round up to **2**. Two weeks of sales is the same as 14 days of stock left, because `units_per_day` is the last 7 calendar days' sales divided by 7 (`vending/tools/machine_state.py`). Rounding up is cheap: unsold units keep their cost in the closet (A5 in `docs/assumptions.md`), while a stockout loses the sale and the cash it would have returned (`docs/agent/money.md`).

**What the numbers say about capacity.** A full column of a fast seller lasts 11 / 1.701 = 6.5 class days in 8.4 oz cans, 8 / 1.701 = 4.7 in 12 oz cans, and 7 / 1.701 = 4.1 in 20 oz bottles. So a fast seller in bottles runs out before the next Friday even when full. The fix is a product choice, not a visit: put fast sellers in high-capacity packages, or give one product two columns. A slow seller's full column (7) lasts 7 / 0.722 = 9.7 class days, about two gaps.

**Three cases the rule must not get wrong.**

- **The slot sold out inside the window.** `units_per_day` counts the empty days as zero, so it understates demand (A1 in `docs/assumptions.md`: sales are a floor). Use the slot's in-stock rate, or buy.
- **The window held a holiday.** Its rate is near zero and says nothing about demand. Use the last full class week's rate.
- **No rate yet** (a new product, or day one). Buy when stock in hand is below one full column.

A slot with stock and zero sales has `days_of_stock_left` null, not zero (`_days_left` in `vending/tools/machine_state.py`). Null never triggers a purchase. That slot goes to rule 3.

## 2. Midweek visit: when $25 pays

**Rule.** Ask for a midweek visit only when the margin it saves before Friday is more than $25:

> saved margin = sum over empty slots of (rate per class day × class days saved × margin per unit)

Plan on **2 class days saved** for a request sent Monday morning. Send none after Tuesday.

**Class days saved.** The worker gets out of class at 1 PM (#106), so a midweek visit lands in the afternoon, after the morning rush. A Monday request filled Tuesday afternoon saves Wednesday, Thursday and Friday morning: 3 class days at best. A visit "may take longer than 24 hours" (`docs/agent/restocking.md`), so we plan on Wednesday afternoon: 2 class days. A Wednesday request saves at most Friday morning. A Thursday request saves nothing over the Friday visit.

**Margin per unit.** Margin = shelf price ÷ 1.08 − unit cost. Our only real costs are the most-we-pay pack prices in run 2's request (`docs/sample-directive.md`). Using the most we pay gives the worst-case margin:

| Product (run 2) | Unit cost at most | Price | Price ex tax | Margin | Break-even units (25 ÷ margin, rounded up) |
|---|---|---|---|---|---|
| Red Bull 8.4 oz | $40.50 ÷ 24 = $1.69 | $4.50 | $4.17 | $2.48 | 11 |
| Celsius 12 oz | $25.00 ÷ 12 = $2.08 | $4.50 | $4.17 | $2.08 | 13 |
| Mountain Dew 20 oz | $57.00 ÷ 24 = $2.38 | $3.25 | $3.01 | $0.63 | 40 |
| Coca-Cola 20 oz | $63.50 ÷ 24 = $2.65 | $3.50 | $3.24 | $0.59 | 43 |
| Diet Coke 20 oz | $59.00 ÷ 24 = $2.46 | $3.25 | $3.01 | $0.55 | 46 |

**Empty slots needed.** One empty fast slot saves 1.701 × 2 = 3.4 units in 2 class days. At the Red Bull margin that is 3.4 × $2.48 = $8.43, so a visit pays only when **3 or more** high-margin fast slots are empty by Monday (25 ÷ 8.43 = 2.97). At the Celsius margin it takes 4 (25 ÷ 7.09 = 3.53). At the soda margins it takes 13 or more (Coca-Cola: 3.4 × $0.59 = $2.02, and 25 ÷ 2.02 = 12.4). One empty slot never pays for a visit.

**When cash is short, the bar rises.** If the balance already limits Friday's order, the $25 also stops us buying stock. $25 buys 14 Red Bull units at $1.69 (25 ÷ 1.69 = 14.8), worth 14 × $2.48 = $34.71 of margin if they sell. The visit must then save $25 + $34.71 = $59.71: 8 fast Red Bull slots (59.71 ÷ 8.43 = 7.1). In practice, a cash-short week waits for Friday.

**Riders.** A paid visit can carry price changes and top-ups for every slot at no extra fee, so put them all on the one request. Only one request may be open at a time (`request_pending`, `docs/process-map.md`), so decide on a midweek visit before the Friday request goes in.

## 3. Slow sellers: when to pull

This is the team's 12 Nov decision, written up in PR #100 (`docs/agent/restocking.md` on that branch). We use it as is:

- **Flag** a slot that sells nothing for **5 school days in a row while our records say it held stock** (a receipt or a person's count). Days in a break don't count. A slot that may be empty is not slow.
- **Act at the next Friday visit:** try a lower price, or swap the product. Pulled units go to the closet at what we paid, so a pull is not a write-off.

**Evidence** (`analysis/REPORT.md`, "Slow-seller rule"). Replayed over this fall, 5 class days of zero sales raised 75 flags. Of the 39 with an outcome, 4 (10%) were live slots and 20 (51%) sold again only after a refill, so they were probably empty. That is why the rule needs the held-stock check. The check is not validated: the fall data had no receipts.

**How it meets rules 1 and 2.** A flagged slot gets no new purchase. Its stock is pulled or repriced at Friday, and the new product starts with no rate (the "no rate yet" case in rule 1). Pricing for the swap follows the pricing method in PR #99 (`docs/agent/price-history.md`) and the $6.50 ceiling in PR #100.

## 4. How the balance limits orders

**Rule.** After an order, at its most-we-pay cost, the spendable balance must still hold **$53**. If it would not, cut the order in this order:

1. Drop purchases for slow sellers and flagged slots.
2. Drop purchases where the closet already covers a week.
3. Keep the slots with the most margin per class day (rate × margin), fast high-margin slots first.

**Derivation of $53.** The reserve covers the two costs that can arrive before sales top the account up:

| Item | Amount | Source |
|---|---|---|
| One midweek visit | $25.00 | config `midweek_fee_cents` |
| A full week of model spend at the hard cap | 7 × $4.00 = $28.00 | config `model_daily_cap_cents` (default 400) |
| **Reserve** | **$53.00** | |

The model cap is the most the code lets the agent spend, not what it usually spends (#90 reports $0.02 to $0.80 a run). Using the cap means token spend can never eat the stock money.

**Why "spendable" and "most-we-pay".** Spendable is the balance less sales tax owed (`docs/agent/money.md`, `get_account_balance`). The planner already refuses any order whose most-we-pay cost exceeds spendable (`insufficient_balance` in `vending/planner.py`). The reserve is one step stricter. The order usually costs less than its maximum (run 2: $233.31 expected, $270.00 at most, `docs/sample-directive.md`), so the real cushion is larger.

**Tie to the capital plan (#90).** The capital plan decides how much of the $600 the day-one fill spends. This policy does not depend on that number. It sets one condition the plan must meet: **after the day-one fill, at least $53 stays spendable**. From the first Friday on, sales refill the account. At the restocked rate of 23.3 units a class day, a 5-day week sells about 117 units. The cash that returns depends on our prices, so the reserve, not a sales forecast, is the floor.

## Assumptions

To number in `docs/assumptions.md` (open PRs already take A16 to A19).

| | Assumption | What would show it's wrong |
|---|---|---|
| R1 | A week's sales in a stocked slot are Poisson around its rate, so the 95% cover is 1.6 to 1.9 times the mean. | Weekly sales of our own drinks varying more than that. |
| R2 | One gap between Friday visits holds 5 class days of sales, because the visit lands after Friday's morning rush. | Visits landing in the morning, or classes on weekends. |
| R3 | Fall segment rates are a fair prior for our new products until each slot has its own rate. | Our first weeks' rates far from these. |
| R4 | Run 2's most-we-pay pack prices stand for unit cost until receipts give real costs. | Receipts with different costs. |
| R5 | A midweek visit lands 2 class days after a Monday request. | Receipts showing faster or slower fills. |
| R6 | Unsold stock is valued at cost at the end (A5), so over-stocking costs cash timing, not profit. | David valuing end stock at less than cost (#102). |
| R7 | The held-stock check in the slow-seller rule works on receipts and counts. | Flagged slots that turn out empty. |

## Open decisions

- **Safety factor 2 and the 95% target.** Proposed here; the team has not agreed.
- **The $53 reserve.** Proposed here; the capital plan (#90) must allow for it.
- **The midweek trigger** (3 or more fast, high-margin slots empty by Monday). Proposed here.
- **First regular restock:** Fri 4 Dec or Fri 11 Dec (#102).
- **Day-one deadline:** "the 27th" falls on the Thanksgiving closure (#102).
- **A Friday visit in the Winter Holiday Break** (25 Dec, 1 Jan): whether one happens. Not yet asked.
- **The wildcard slot** (Kevin's proposal on #100: one slot with a ceiling near $8.00). Not decided. It changes that slot's margin, not these rules.
- **A rule for slow-but-steady slots.** None until we have sales of our own drinks (#100).

## Sources

- Project Brief (Revised), sections 3.3, 3.4 and 5, as quoted in `docs/agent/restocking.md` and `docs/agent/money.md`.
- `analysis/REPORT.md`: segment rates, the restocked scenario, time of day, the slow-seller simulation, caveats.
- `analysis/golisano_calendar_2026_27.json`: class days and breaks.
- `vending/tools/machine_state.py`: `units_per_day`, `days_of_stock_left`.
- `vending/stock.py`: stock on hand and the closet, from receipts.
- `vending/capacity.py`: units per column.
- `vending/config.py`, `vending/pricing.py`, `vending/planner.py`: fee, tax, model caps, the balance check.
- `docs/sample-directive.md`: run 2's pack prices and order cost.
- `docs/process-map.md`: one open request at a time.
- PR #99 (pricing method), PR #100 (slow-seller rule, $6.50 ceiling), PR #105 (`docs/cutover.md`), PR #106 (David's 12 Nov answers).
- Issues #90 (capital plan), #91 (this deliverable), #102 (open questions), #107 (actions from 12 Nov).
