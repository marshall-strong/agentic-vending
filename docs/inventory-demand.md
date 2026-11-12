# Inventory and demand estimation

## What this is

The project brief asks for a "separately documented inventory and demand estimation component" (section 5; issue #93). This is that document. It covers our 20 slots on machine `vmc1` (15-18, 25-28, 35-38, 45-48, 55-58; `vending/slots.py`).

Before each Friday restock the agent needs two answers:

1. **What is in each slot now?** Code answers this (`vending/stock.py`, `vending/slot_counts.py`, `vending/tools/machine_state.py`).
2. **How much would each slot sell in a week if it never ran out?** No code answers this yet; the agent reads recent sales and judges. The fall sales analysis (PR #68) and its labs (PR #83, slot segments) are merged but not connected to the agent.

Principles: `docs/agent/inventory-belief.md`. Money: `docs/opening-capital-plan.md` (#90). Restock thresholds: `docs/restock-policy.md` (#91).

## Inputs

| Input | What it gives | Exact? | Source |
|---|---|---|---|
| Polls (DEX), hourly since 21 Sep | Lifetime units and cents per selection, price, door-open count (`EA2*DO`) | Counters yes; timing to the poll | `vending/counters.py`; door count only in `analysis/export_sales.py` |
| Restock receipts | Units loaded, to the closet, lost, pulled; total paid; `resolved_at` | As the worker entered | `vending/stock.py` |
| Person's counts | Product and units per selection | `door` yes, `photo` no | `vending/slot_counts.py` (PR #80) |
| Academic calendar | Class days, breaks, closures | Official, "subject to change" | `analysis/golisano_calendar_2026_27.json` |

The machine never reports stock on hand; there is no camera (A13, `docs/assumptions.md`, which still says "no manual counts" and needs a follow-up after PR #80).

## How stock on hand is estimated

**Receipts are reset points.** `vending/stock.py` replays receipts and polls in time order. A purchase puts `units_loaded` in the slot and `units_to_back_stock` in the closet; survivors carry the cost of units lost on the way. A sale is one counter rise, timed at the later poll. A product change restarts the count and writes off what was left as shrinkage.

So, since the last product change, **on hand = loaded − sold − lost**, never below 0. If polls show more sales than units loaded, the code warns and counts 0.

**Misread polls don't become sales.** If a counter drops and the next poll is back at or above the old high, the low reading is skipped; if it stays low, the counter restarted. So 100000 → 500 → 100250 books 250 sales, not 99,750 (`vending/counters.py`). Stock and ledger share this module.

**Counts check the arithmetic** (`get_machine_state`). `counted_on_hand` = units counted − units sold since. A count newer than the slot's last receipt wins. A count older than 7 days (`COUNT_STALE_DAYS`) is weak evidence. A slot with no receipts shows null (unknown), not 0.

**Day one.** Staff empty the machine before 3 Dec. Our day-one order fills all 20 slots to capacity (7 to 11 units per column, `vending/capacity.py`) from the $600 (PR #106, #107; the $600 part is being checked, #106 row 3). Its receipts are every slot's first reset point.

## How weekly demand is estimated

**What the agent sees today.** `get_machine_state` gives `units_per_day` over the last 7 days and `days_of_stock_left` = on hand ÷ units per day; `get_sales_history` gives units over any window.

**What the fall analysis learned** (562 drinks, $1,881.50 from our slots, 27 Aug to 11 Nov; `analysis/REPORT.md`):

1. **Count class days, not calendar days.** 66% of machine units sold 8-11 AM on weekdays. A weekday without classes was worth −18.58 machine units/day (95% CI −24.22 to −12.94; only 2 such days, so the sign is sure, the size is not).
2. **Sold-out periods are censored, not zero demand.** A slot is "suspected empty" after its last sale once P(0 sales) = e^(−expected) < 0.05, using its own share of our slots' hour-of-week clock. The window ends at a door visit, not the slot's next sale. Rates are per in-stock class day; the "restocked scenario" sums them over our 20 slots.
3. **The recent fall in sales is supply, not demand.** Our weekly units went 61 → 41 → 22; slots selling fell from 20 (week from Sat 26 Sep) to 10. Adding empty slots to the regression cut the week-of-term slope from −1.76 to −0.33 units/day per week (model B vs B0). Kept stocked, our slots would sell about 23 units per class day (recent rates) or 22 (whole era), against 8.7 per weekday observed in the last 4 weeks.

### Censoring will be common

The fast segment (slots 28, 38, 45, 46) sold 1.701 units per in-stock class day (REPORT.md, "Our slots"):

| Column | Capacity (`vending/capacity.py`) | Class days to empty at 1.701 |
|---|---|---|
| 20 oz bottle | 7 | 7 ÷ 1.701 = 4.1 |
| 12 oz can | 8 | 8 ÷ 1.701 = 4.7 |
| 240/250 ml can | 11 | 11 ÷ 1.701 = 6.5 |

A Friday-afternoon fill faces 5 class days (restock policy R2, #91). So a fast slot in bottles or 12 oz cans sells out before the next visit, and its week's sales are a floor. Packages not on the chart (for example a 16 oz can) need a counted `config.capacity_overrides` entry before 27 Nov.

Whole week: 23.3 units per class day (REPORT.md, "Next 4 weeks") × 5 = 116.5, about 117 units. Rough: the REPORT's weekly figures vary (52 for 2 class days, 44.9 for 2, 64.6 for 3). Our slots hold 20 × 7 = 140 to 20 × 11 = 220 units, so a week's demand is 116.5 ÷ 220 = 53% to 116.5 ÷ 140 = 83% of capacity, spread unevenly.

**Receipts make censoring observable.** During the run a slot is empty from the poll at which sold reached loaded until the next load. So the in-stock rate is exact to the poll: units sold ÷ in-stock class days.

## How sure we are

| Claim | Evidence (REPORT.md unless noted) |
|---|---|
| Our sales totals are right | Daily, weekly and per-slot totals reconcile with the counters (562 units, 188,150 cents). "Checks" |
| Inferred stock-outs match a person's view | Nov 9 photo: 16 of 20 rails agree (13 empty flagged, 3 stocked selling); 2 (28, 47) flagged but held product; 2 (56, 57) sold out, not yet flagged. Finding 13 |
| Holt-Winters is the best forecast | Near tie. 7 Saturday plan dates, 7 days ahead, machine-wide: MAE 5.78 units/day (WAPE 52%) vs same-weekday-last-week 5.89 (53%); both high (bias −2.87, −2.73). "Forecast backtest" |
| A weekly forecast can be chosen | No: 5 test weeks, "Too few weeks to choose on." |
| Weekday effects | Every Tue-Fri vs Monday 95% CI crosses zero (models A and B). |
| Receipt arithmetic works on real data | Untested; unit tests only, no filled request yet. `docs/cutover.md` (PR #105) |

## Known gaps

1. **Visit-time sales can land on the wrong product (#97).** An old-product sale just before the door opens is subtracted from the new product. Units balance, but shrinkage is overstated and new on-hand understated. Of 450 units our slots sold 21 Sep to 11 Nov, 37 (8%) fell in a visit window (`docs/cutover.md`, PR #105, which plans the fix).
2. **Hidden product.** A photo sees only the front of a rail. Only a `door` count is exact.
3. **Counts don't correct the books.** They override `get_machine_state` but not `vending/stock.py` or the ledger.
4. **The agent's rate ignores censoring and the calendar.** `units_per_day` divides by calendar days, so a sold-out slot looks slow and a break week makes `days_of_stock_left` huge.
5. **No history for our products.** Per-slot fall rates (staff's products and prices) don't transfer. Category results do, loosely: energy 45% of our units, caffeinated 65% (finding 14).
6. **Machine-wide data ends.** After 3 Dec polls may carry only our 20 selections (#102); whole-machine totals stop before the start (#84).
7. **Winter Holiday Break, 22 Dec to 1 Jan.** Closed Fri 25 Dec, Mon 28 Dec, Fri 1 Jan. A Fri 18 Dec fill faces 1 class day (Mon 21 Dec) before Mon 4 Jan. Presidents Day, Mon 15 Feb, is also closed.
8. **No agent tool reads the door counter**, which the stock-out window and cutover need (#105).

## Recommendation: should #68's forecast feed the agent?

**For the team to decide (#93): keep #68's forecast as evidence for a simpler rule. Don't feed Holt-Winters to the agent.**

- **It barely beats the simple bar** (5.78 vs 5.89 units/day, a 0.11 gap on 7 plan dates, WAPE above 50%).
- **It forecasts a machine that was emptying**, not the restocked scenario, from machine-wide totals and old products we won't have.
- **Capacity binds for fast slots.** 4 of 20 slots empty in 4.1 to 6.5 class days (table above), so they get "fill to capacity" anyway. Mid slots come close (7 ÷ 1.354 = 5.2 to 8 ÷ 1.354 = 5.9); slow ones don't (7 ÷ 0.767 = 9.1, 7 ÷ 0.722 = 9.7). A forecast matters for those and the $25 midweek visit, and Holt-Winters doesn't forecast slots.
- **A simple rule is easier to check** (`docs/agent/pricing.md`, `docs/agent/goal-and-scoring.md`).

**The simpler rule, per slot:**

1. **In-stock rate** = units sold ÷ in-stock class days since the product last changed. In stock = `stock_on_hand` > 0 by receipts, or a newer count. Skip breaks and closures.
2. **Expected demand** = in-stock rate × class days before the next Friday.
3. **Compare with capacity.** At or above: fill to capacity and note the slot was censored. Well below: candidate for the slow-seller rule (PR #100) or a price test (PR #99); see swap-first under Open decisions.
4. **Until a slot has 5 in-stock class days** (Proposed, #91), say so and use the fall segment rate for its price band: 1.701 (fast), 1.354 (mid, premium-priced), 0.767 or 0.722 (slow) (REPORT.md, "Our slots"; R3, Proposed). Category shares rank new slots only. An unused alternative: restocked scenario × share ÷ our slots in the category; for energy, 23.3 × 0.45 ÷ 9 slots (15-18, 25-28, 38) = 1.165.

New products, swapped slots and the wildcard slot start at step 4.

## Assumptions

To number in `docs/assumptions.md` (open PRs use A16-A19: #69 A16, #99 A17-A18, #100 A19).

- **Receipts are accurate** as the worker entered them.
- **One counter rise is one sale**; no test or free vends.
- **Breaks sell about nothing**, as fall days off did (2 no-class weekdays).
- **The fall weekday and time-of-day shape holds in winter.** Unchecked.
- **Sell-out time is the poll at which sold reached loaded:** hourly now; about 10 minutes on Friday afternoons once rate-limited polling is built (#107).
- **Substitution between our slots is not measured.** The brief asks us to allow for it; we have no estimate.

## Open decisions

- **Does #68's forecast feed the agent?** Recommended: no. Team to decide (#93).
- **Add the in-stock rate and a "sold out since" time to `get_machine_state`?** Depends on the above; agent docs wait for #99 and #100.
- **Should a door count post a correction to the books?** Not decided.
- **Cost method:** weighted average (`vending/stock.py` today) or FIFO.
- **Restocks on Fri 25 Dec and Fri 1 Jan** (closures). Not yet asked in #102.
- **New-slot prior** (Proposed, #91 R3): fall segment rate for the price band; category shares for ranking only.
- **When a slot's own rate counts** (Proposed, #91): after 5 in-stock class days.
- **Wildcard slot** (Proposed by Kevin on #100): the agent names one slot with a reason and moves it only at a Friday visit; ceiling about $8.00 vs $6.50; same floor as every slot (cost × 1.08 rounded up to 5 cents, `vending/pricing.py`), which only breaks even after tax, so "every sale profits" needs a higher floor (open). For demand, no fall segment was priced that high to borrow a rate from (the dearest averaged $4.33, REPORT.md, "Our slots"). Rules: `docs/restock-policy.md` section 3; money: `docs/opening-capital-plan.md`.
- **Swap first at a slow-seller flag** (Proposed by Kevin, 12 Nov): at most one price step down, then swap; pulled units to the closet at cost. Depends on A5 (#102, P2). See `docs/restock-policy.md` rule 3.
- **Does the door counter stay in the polls after 3 Dec?** Asked in #102.
- **Day-one deadline:** about Fri 27 Nov, inside the 26-29 Nov closure; exact time asked in #102.

## Sources

- Issues #93, #97, #102, #107; PRs #68, #80, #83, #106 (merged) and #99, #100, #105 (open).
- `analysis/REPORT.md`, `analysis/sales_analysis.py`, `analysis/golisano_calendar_2026_27.json` (data 27 Aug to 11 Nov 2026); `docs/planning/2026-11-12-david-answers.md`; `docs/cutover.md`.
- Code under `vending/` as cited above; tests `tests/test_stock.py`, `tests/test_counters.py`.
- Sibling docs #90 and #91; agent docs `docs/agent/inventory-belief.md`, `pricing.md`, `goal-and-scoring.md`.
