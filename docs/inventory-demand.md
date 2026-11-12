# Inventory and demand estimation

## What this is

The project brief asks for a "separately documented inventory and demand estimation component" (section 5; issue #93). This is that document. It covers our 20 slots on machine `vmc1` (15-18, 25-28, 35-38, 45-48, 55-58; `vending/slots.py`).

The component answers two questions for the agent before each Friday restock:

1. **What is in each slot now?** Code answers this (`vending/stock.py`, `vending/slot_counts.py`, `vending/tools/machine_state.py`).
2. **How much would each slot sell in a week if it never ran out?** No code answers this yet. The agent reads recent sales and judges. The fall sales analysis (`analysis/`, PR #68) tested forecasts on the history, and PR #83 added the labs (`analysis/lab_data.json`, slot segments). Both are merged. Nothing connects them to the agent.

The agent reads the principles in `docs/agent/inventory-belief.md`. This document explains the method, how sure we are, and what is missing.

## Inputs

| Input | What it gives | Exact? | Source |
|---|---|---|---|
| Polls (DEX) | Lifetime units and cents per selection, the price now, the door-open count (`EA2*DO`). Hourly from course staff since 21 Sep. | Counters yes; timing only to the poll interval | Sales: `vending/counters.py`. Door count: `analysis/export_sales.py` only (no agent tool reads it). `analysis/REPORT.md` |
| Restock receipts | Per line: units loaded, units to the closet, units lost, units pulled, total paid, `resolved_at`. Later losses on their own rows. | As exact as the worker's entry | `vending/stock.py` |
| Person's counts | Product and units seen per selection. `door` counts are exact; `photo` counts are estimates. | Door yes, photo no | `vending/slot_counts.py` (PR #80) |
| Academic calendar | Class days, breaks, closures | Official, "subject to change" | `analysis/golisano_calendar_2026_27.json` |

The machine never reports stock on hand. There is no camera (assumption A13, `docs/assumptions.md`). A13 also still says "no manual counts". PR #80 added them, so A13 needs an update. That is a follow-up, not done here, because `docs/assumptions.md` numbering collides across open PRs.

## How stock on hand is estimated

**Receipts are reset points.** At a restock we know what went in. `vending/stock.py` turns every fulfilled receipt and every poll into a list of movements and replays them in time order:

- A purchase puts `units_loaded` in the slot and `units_to_back_stock` in the closet. Units lost on the way are already paid for, so the survivors carry their cost.
- A sale is one rise in a lifetime counter between two polls, timed at the later poll.
- A loss takes units out of the slot or the closet and books them as shrinkage.
- When a slot's product changes, its count starts again. Units still counted in it are written off as shrinkage.

So for each slot, **on hand = loaded − sold − lost**, since the last product change. It never goes below 0. When the polls show more sales than units loaded, the code warns and counts 0 on hand.

**Misread polls don't become sales.** `vending/counters.py` handles a reading that goes down. If the next poll is back at or above the old high, the low one was a misread and is skipped. If it stays low, the counter restarted. Without this rule, 100000 → 500 → 100250 would book 99,750 sales instead of 250. The stock code and the ledger read counters through the same module, so they agree on every sale.

**Counts check the arithmetic.** `get_machine_state` shows a person's latest count beside the receipt arithmetic (`vending/tools/machine_state.py`):

- `counted_on_hand` = units counted − units the polls say sold since.
- If the count is newer than the slot's last receipt movement, the agent trusts the count over the receipts.
- A count older than 7 days (`COUNT_STALE_DAYS`) is called weak evidence.
- A slot with no receipts shows null, not 0: unknown, not empty.

**Day one.** Before 3 Dec, staff empty the machine. Our day-one order fills all 20 slots to capacity (David, 12 Nov; PR #106, issue #107). Its receipts are the first reset point for every slot. The test stock in the machine now doesn't count against us (#102, still to confirm). Capacity per column comes from the loading chart: 7 to 11 units, depending on the package (`vending/capacity.py`).

## How weekly demand is estimated

### What the agent sees today

`get_machine_state` gives each slot `units_sold_in_window` and `units_per_day` over the last 7 days, and `days_of_stock_left` = on hand ÷ units per day. `get_sales_history` gives units over any window. The agent turns these into a weekly figure itself.

### What the fall analysis learned

`analysis/sales_analysis.py` studied 562 drinks and $1,881.50 from our slots, 27 Aug to 11 Nov (`analysis/REPORT.md`). Three findings shape the method.

**1. Count class days, not calendar days.** The machine sells on weekday mornings: 66% of machine units between 8 and 11 AM. Weekends and days off sell close to nothing. A weekday without classes was worth −18.58 machine units/day (95% CI −24.22 to −12.94; only 2 such days, so the size is uncertain, the sign is not). The analysis builds a day table from the calendar file and counts only class days.

**2. Sold-out periods are censored, not zero demand.** When a slot is empty, its missed sales are invisible. Units sold is a floor on demand. The analysis handles this in three steps:

- **A stock-out clock.** A slot is "suspected empty" after its last sale when its own expected sales since then make P(0 sales) = e^(−expected) < 0.05. Expected sales come from the slot's own share of a calendar-time clock built from our slots' hour-of-week profile. No other slot's sales enter it.
- **The window ends at a door visit**, never at the slot's own next sale. Door visits come from the door counter.
- **Rates per in-stock class day.** Units ÷ class days the slot was not suspected empty. The "restocked scenario" adds these rates over our 20 slots.

**3. The recent fall in sales is supply, not demand.** Our units went 61 → 41 → 22 over the last three full weeks. Slots selling fell from 20 (best full week, from Sat 26 Sep) to 10 (last full week). Adding the count of empty slots to the regression cut the week-of-term slope from −1.76 to −0.33 units/day per week (model B vs B0). If each slot were kept stocked, their in-stock rates add up to about 23 units per class day (recent rates) or 22 (whole era). Observed sales in the last 4 weeks were 8.7 per weekday.

### Censoring will be common during the run

The fall data says our fast slots will often sell out before Friday. The "fast seller" segment (slots 28, 38, 45, 46) sold 1.701 units per in-stock class day (REPORT.md, "Our slots"). At that rate:

| Column | Capacity (`vending/capacity.py`) | Class days to empty |
|---|---|---|
| 20 oz bottle | 7 | 7 ÷ 1.701 = 4.1 |
| 12 oz can | 8 | 8 ÷ 1.701 = 4.7 |
| 240/250 ml can | 11 | 11 ÷ 1.701 = 6.5 |

Packages not on the chart (for example a 16 oz can) have no capacity in code until a person counts one into `config.capacity_overrides` (`vending/capacity.py`). Count any such day-one product before 27 Nov.

A Friday-afternoon fill faces 5 class days (Mon to Fri) before the next one. So a fast slot in bottles or 12 oz cans runs out before then. Its sales that week are a floor, not demand. These are rates for staff's products; per-slot rates don't transfer to ours (gap 5).

Across our 20 slots, the restocked scenario is about 23.3 units per class day (REPORT.md, "Next 4 weeks"). × 5 class days = 116.5, or about 117 units a week. That is a rough figure: the REPORT's weekly restocked figures vary by weekday (52 for 2 class days, 44.9 for 2, 64.6 for 3). Our 20 slots hold 20 × 7 = 140 to 20 × 11 = 220 units. So a week's demand is about 116.5 ÷ 220 = 53% to 116.5 ÷ 140 = 83% of what our slots hold, and it is not spread evenly across slots.

### Censoring is easier with receipts

The analysis had to infer stock-outs from silence. During the run, receipts make them observable. A slot with `stock_on_hand` 0 is empty from the poll at which sold reached loaded, until the next load. So the in-stock rate can be computed exactly, to poll precision: units sold ÷ in-stock class days. A person's door count confirms it.

## How sure we are

| Claim | Evidence | Source |
|---|---|---|
| Our sales totals are right | Daily, weekly and per-slot totals reconcile with the counters, slot by slot (562 units, 188,150 cents). All checks OK. | REPORT.md, "Checks" |
| Inferred stock-outs match what a person saw | Nov 9 photo vs the stock-out clock: 16 of 20 rails agree: 13 empty rails flagged empty and 3 stocked rails selling. 2 (28, 47) were called empty but held product. 2 (56, 57) had sold out within the week and were not yet flagged. None looked empty while quiet and unflagged. (28 is in the fast segment over the fall but was quiet by 9 Nov.) | REPORT.md, finding 13 |
| Holt-Winters is the best forecast | A near tie. Walk-forward, 7 Saturday plan dates, 7 days ahead, machine-wide daily totals: Holt-Winters MAE 5.78 units/day (WAPE 52%) vs same weekday last week 5.89 (53%). Both ran high (bias −2.87 and −2.73). | REPORT.md, "Forecast backtest" |
| A weekly forecast can be chosen | No. 5 test weeks: "Too few weeks to choose on." | REPORT.md, "Forecast backtest" |
| Weekday effects | Every Tue-Fri vs Monday 95% CI crosses zero, machine-wide (model A) and for our 20 slots (model B). | REPORT.md, "Calendar regression", models A and B |
| Receipt arithmetic (`vending/stock.py`) is right on real data | Untested on real data: unit tests only (`tests/test_stock.py`, `tests/test_counters.py`). We haven't seen a filled request yet. | `docs/cutover.md` (PR #105) |

## Known gaps

1. **Sales at a visit can land on the wrong product (#97).** A poll shows sales since the last poll at today's price. It doesn't say when the product changed. `vending/stock.py` times a sale at the later poll and a load at `resolved_at`. Reading the code, an old-product sale between the last poll and the door opening is subtracted from the new product instead. The old product's count is then too high at the change, and the extra unit is written off as shrinkage. Units still balance, but shrinkage is overstated and the new product's on hand is understated by the same number. Of the 450 units our slots sold 21 Sep to 11 Nov, 37 (8%) sold inside a visit window (`docs/cutover.md`, PR #105). #105 plans the fix: split at the door times and `fulfilled_at`, and solve price changes from the counters.
2. **Hidden product further back on a rail.** A photo sees the front of a rail. Product further back can be hidden, so "looks empty" is not proof (REPORT.md, finding 13). Only a `door` count is exact.
3. **Counts don't correct the books.** A count overrides the receipt arithmetic in `get_machine_state`, but it doesn't feed `vending/stock.py` or the ledger. A gap between a count and the receipts is not booked as shrinkage.
4. **The agent's rate ignores censoring and the calendar.** `units_per_day` is units ÷ calendar days over the last 7 days. A slot that sold out on Tuesday shows a low rate. A week inside a break shows almost 0, so `days_of_stock_left` comes out huge. No agent tool reads the calendar.
5. **No history for our own products.** The fall history is staff's products at staff's prices. Day one changes every slot. Per-slot fall rates don't transfer. Category results do, loosely: energy drinks were 45% of our units and caffeinated drinks 65% (REPORT.md, finding 14).
6. **Machine-wide data ends.** The forecasts were fit on machine-wide totals. After 3 Dec the polls may carry only our 20 selections (#102), and whole-machine totals stop before the start (#84).
7. **Winter Holiday Break, 22 Dec to 1 Jan.** No classes for two weeks. The Institute is closed Fri 25 Dec, Mon 28 Dec and Fri 1 Jan (calendar file). The Friday restock on 25 Dec and 1 Jan is not settled. A fill on Fri 18 Dec faces 1 class day (Mon 21 Dec) before classes resume Mon 4 Jan, so a naive rate or fill is most wrong here. Presidents Day, Mon 15 Feb, is also a closure.
8. **No agent tool reads the door counter.** The stock-out window and the cutover both depend on door times, but `EA2*DO` is parsed only in `analysis/export_sales.py` and `analysis/sales_analysis.py`. #105 plans visit detection from it, and #102 asks whether it stays in the polls after 3 Dec.

## Recommendation: should #68's forecast feed the agent?

**This is a recommendation for the team to decide (#93).** We recommend: **keep #68's forecast as an analysis that justifies a simpler rule. Don't feed Holt-Winters to the agent.**

Reasons, from the evidence above:

- **It barely beats the simple bar.** 5.78 vs 5.89 units/day is a 0.11 difference on 7 plan dates, with a WAPE above 50% for both.
- **It forecasts the wrong thing.** It follows the level of a machine that was emptying. The report itself says to read demand from the restocked scenario instead.
- **It is fit on data we won't have.** It forecasts machine-wide totals. During the run we may see only our slots, and our products will be new.
- **Capacity binds for the fast slots.** At fall rates, that is 4 of 20 slots (REPORT.md, "Our slots"). In bottles or 12 oz cans they empty in 4.1 to 4.7 class days (6.5 in 240/250 ml cans). Mid sellers come close: 7 ÷ 1.354 = 5.2 to 8 ÷ 1.354 = 5.9. Slow slots don't: 7 ÷ 0.767 = 9.1 and 7 ÷ 0.722 = 9.7. For fast slots the answer is "fill to capacity" whatever the forecast says. These are rates for staff's products, which don't transfer per slot (gap 5). A forecast matters most for the mid and slow slots and for the $25 midweek visit, and Holt-Winters forecasts machine-wide totals, not slots.
- **A simple rule is easier to check.** Prices must be explainable (`docs/agent/pricing.md`), and the agent should use arithmetic where arithmetic will do (`docs/agent/goal-and-scoring.md`). One rate per slot is easy to show and easy for a grader to follow.

**The simpler rule.** For each slot:

1. **In-stock rate** = units sold ÷ in-stock class days since the slot's product last changed. In-stock means `stock_on_hand` > 0 by the receipts, or a newer count says so. Skip break days and closed days.
2. **Expected demand next week** = in-stock rate × class days before the next Friday (from the calendar).
3. **Compare with capacity.** If expected demand is at or above capacity, fill to capacity and note that the slot was censored. If it is well below, it is a candidate for the slow-seller rule (PR #100) or a price test (PR #99).
4. **Until a slot has a full stocked week of its own**, say so. Use its category's fall share (REPORT.md, finding 14) to rank new slots only, not as a rate. A share of units is not units per slot per class day. One way to turn it into a rate is restocked scenario × category share ÷ our slots in that category: for energy, 23.3 × 0.45 ÷ 9 slots (15-18, 25-28, 38) = 1.165. The team hasn't chosen whether to use a number or a ranking (open).

The analysis keeps its job: it is the evidence for counting class days, for treating breaks as zero, for reading sold-out weeks as censored, and for the slow-seller rule.

**Agent-facing follow-up** (not done here; `docs/agent/*.md` is being edited in #99 and #100). If the team agrees, `get_machine_state` could report the in-stock rate per class day and a "sold out since" time, and `docs/agent/inventory-belief.md` could point to this rule.

## Assumptions

To number in `docs/assumptions.md` (open PRs use A16 to A19: #69 A16, #99 A17-A18, #100 A19).

- **Receipts are accurate.** `units_loaded`, losses and `total_paid` are what the worker entered. They are the reset points.
- **One counter rise is one sale.** Every vend is a sale. There are no test vends or free vends during the run.
- **Breaks sell about nothing.** Weekends, the quarter break and closures sell close to zero, as fall days off did (2 no-class weekdays in the data).
- **The fall weekday and time-of-day shape holds in winter.** Unchecked. Winter classes start 3 Dec.
- **The sell-out time is the poll at which sold reached loaded.** Accurate to the poll interval (hourly now; about 10 minutes on Friday afternoons once rate-limited polling is built, #107).
- **Demand doesn't shift between our slots in a way we can measure.** The brief asks us to allow for substitution between our own slots (`docs/agent/inventory-belief.md`). We have no estimate of its size.

## Open decisions

- **Does #68's forecast feed the agent?** Recommendation above: no, use the in-stock-rate rule. Team to decide (#93).
- **Add the in-stock rate and a sold-out time to `get_machine_state`?** Code change; depends on the decision above.
- **Should a door count post a correction to the books** (shrinkage or a found-stock adjustment)? Not decided.
- **Cost method.** Weighted average or FIFO. `vending/stock.py` uses weighted average; the team hasn't chosen.
- **Restocks on Fri 25 Dec and Fri 1 Jan**, both Institute closures. Not yet asked in #102.
- **New-slot prior: a number or a ranking?** Step 4 of the simple rule. Not decided.
- **Does the door counter stay in the polls after 3 Dec?** Asked in #102.
- **Day-one deadline time** (about Fri 27 Nov, Thanksgiving observance). Still being asked (#102).

## Sources

- Issue #93: this deliverable. Issue #97: cutover. Issue #102: open questions for David. Issue #107: actions from David's 12 Nov answers.
- PR #68 (merged): `analysis/REPORT.md`, `analysis/sales_analysis.py`, `analysis/golisano_calendar_2026_27.json`. Data 27 Aug to 11 Nov 2026.
- PR #83 (merged): sales analysis labs, `analysis/lab_data.json`, slot segments.
- PR #80 (merged): `vending/slot_counts.py`, person's counts in `get_machine_state`.
- PR #99 (open): pricing method and price history. PR #100 (open): $6.50 ceiling and the slow-seller rule. PR #105 (open): `docs/cutover.md`, dark-period measurements. PR #106 (open): David's 12 Nov answers.
- Code on main: `vending/stock.py`, `vending/counters.py`, `vending/tools/machine_state.py`, `vending/tools/sales_history.py`, `vending/capacity.py`, `vending/slots.py`.
- Analysis code: `analysis/export_sales.py` (door counter).
- Agent docs: `docs/agent/inventory-belief.md`, `docs/agent/restocking.md`, `docs/agent/pricing.md`, `docs/agent/goal-and-scoring.md`. Assumption A13: `docs/assumptions.md`.
