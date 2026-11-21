# David's update, 20 Nov 2026

David emailed both teams on 20 Nov. The facts below come from that email. Times are Eastern. Earlier answers: [12 Nov](2026-11-12-david-answers.md), [17 Nov](2026-11-17-david-answers.md).

## The dates

| When | What | Changes from before |
|---|---|---|
| 20 Nov | **The slot redraw has happened.** Slots will not change during the quarter | Expected 18 to 19 Nov (17 Nov notes, row 3) |
| Mon 30 Nov | Staff empty the machine, buy and stock our initial requests, and reset the interval counters. The lifetime counters keep their counts; they can't be reset | Confirms the day. The counter reset is new |
| 30 Nov to 2 Dec | **The machine does not operate until 3 Dec, "to provide a clean start"** | New |
| "1 or 2 Dec" | **"First day of operation", "depending on the machine getting stocked"** | **Contradicts the line above.** See the questions |
| Thu 3 Dec, 23:59 | The purchase request for the first restock is due | As on 17 Nov |
| Fri 4 Dec | First official restock | As on 17 Nov |
| Sat 5 Dec, 23:59 | First sales tax calculation | New |

The catalog deadline (Wed 25 Nov) and the fill request deadline (23:59 Sun 29 Nov) are unchanged from 17 Nov.

## What David said, and what it changes

### 1. A tracking check

A new route checks our own totals against the course staff's: `POST /api/v1/tracking/check`. We send four things:

- **cash**: what we have to spend, less the sales tax owed;
- **sales tax owed**;
- **coil units** for each slot;
- **back-stock units** for each product.

Each answer is "within" (±$1 or ±2 units) or "outside". There is no rate limit while we test. Once the quarter starts, it is **1 call a day**. David means it for early detection, not troubleshooting.

**What it changes.** Another branch builds the client. Our figures already exist: cash is the ledger's `spendable` (balance less sales tax owed, `vending/ledger.py`), coil units come from the stock count (`vending/stock.py`), and back stock is the closet count. One call a day means the worker should call it once, at a set time, and log the answer. A slot with unattributed units (`docs/cutover.md`) has a known count and an upper bound, so it can read "outside" by up to those units. Which figure to send for such a slot is a choice for that branch.

### 2. Sales tax is calculated weekly, at 23:59 on Saturday

David does it this way to reduce rounding. The first calculation is Sat 5 Dec.

**What it changes.** Our ledger already rounds tax per week, but its weeks start at 00:00 on Saturday (`vending/ledger.py`, `WEEK_STARTS`). David's week ends at 23:59 on Saturday, so ours ends a day earlier. Another branch aligns the ledger. Until it does, a Saturday's tax falls in a different week from David's, and "sales tax owed" can differ from his by a few cents. That is inside the check's $1 tolerance, but it adds up if we also differ on when tax is owed (question 2).

### 3. Slots are reassigned and final for the quarter

The bridge's answer for our team changed between 16 and 20 Nov. This is the one redraw David promised. The server's assignment on 20 Nov is 12, 14, 16, 18, 21, 23, 25, 27, 32, 34, 36, 38, 41, 43, 45, 47, 52, 54, 56, 58. Ten of these were ours before; ten are new. Our slots are now on every shelf and across the whole width of the machine.

David added an honor rule: **don't influence others with knowledge of who owns what in the machine.** That matches `docs/agent/competition-and-integrity.md`: never steer customers toward or away from a slot except through our own prices and products.

**What it changes.** A person runs `python -m vending.slots record` to store the new assignment in the team database. Until then, the worker's `slots check` fails every round. The day-one plan is placed again on the new slots. See "What the redraw touches" below.

### 4. Mon 30 Nov: stocking and the counter reset

Staff reset the **interval** counters. The **lifetime** counters keep counting.

**What it changes. Nothing breaks.** Every sale and every takings figure we use comes from the lifetime counters:

- `vending/evadts.py` reads only `PA2` fields 1 and 2 (lifetime units and value) and `VA1` fields 1 and 2 (lifetime paid vends). It ignores the interval fields.
- `vending/counters.py` measures rises in lifetime counters only. If a lifetime counter did drop, its rule (two low readings in a row mean a restart) would handle it.
- The door counter `EA2*DO` has two counts, since reset and lifetime. `analysis/door_events.py`, `analysis/sales_analysis.py` and `analysis/visit_windows.py` find visits from the lifetime count only. The since-reset count is only written to `door_events.csv` as a column and printed by `scripts/bridge.py`. On 30 Nov it drops to 0. Nothing reads it for a decision.
- `vending/visit_polls.py` asks the bridge for reads, and `vending/sync_history.py` stores each whole poll and parses it with `evadts.parse`. Neither reads the interval counters.

The 30 Nov stocking is our visit, so set `VISIT_POLL_DATES=2026-11-30` on the Container App (`docs/deploy.md`). Then the worker reads the machine every minute that afternoon and we learn whether it answers with the door open (`docs/cutover.md`).

### 5. When the machine opens

The email says both "will not operate until 3 Dec" and "1 or 2 Dec (depending on the machine getting stocked) – first day of operation". Our code takes 3 Dec 00:00 as the start (`vending/start.py`, `START`; `SLOTS_LOCK_AT`). We keep it until David answers.

If the machine opens on 1 or 2 Dec:

- `START` in `vending/start.py` is a constant, not a setting, so a code change is needed. It sets the catalog weekly limits (`MACHINE_LIVE`), the first class day (`vending/class_days.py`) and the latest fall cut.
- `SLOTS_LOCK_AT` is a setting. Set it to that day's 00:00 Eastern on the Container App, and on any laptop that runs `record` (`docs/deploy.md`).
- Sales from 1 to 2 Dec still count in the ledger. It counts from the opening entry, not from `START`.

### 6. The first restock

Unchanged: a valid request received by 23:59 Thu 3 Dec is filled on Fri 4 Dec. If the machine opens on 1 Dec, the agent plans that request with two days of our sales instead of less than one.

## Dates our code and docs use

| Setting or rule | Value now | Keep? |
|---|---|---|
| `SLOTS_LOCK_AT` (`vending/slots.py`) | 3 Dec 00:00 ET | **Keep.** The redraw has happened and David says it is final. A person runs `record` now, and the first check after the lock confirms it. Move it only if David says the machine opens on 1 or 2 Dec. Moving it earlier, before the fill, would add nothing: no change is expected, and `record` before the lock is still the person's step |
| Ledger opening (`python -m vending.ledger open`, no default date; `--at` defaults to now) | Recorded by a person | **Record it by 23:59 Sun 29 Nov,** when the fill request goes in, as the 17 Nov notes say. Staff buy and stock on Mon 30 Nov, so the fill's receipt is dated 30 Nov. An opening after that would leave the fill out of the books. Nothing in the new dates moves this |
| `START` (`vending/start.py`) | 3 Dec 00:00 ET, a constant | Keep until David answers question 1 |
| `FALL_CUTOFF` | 18 Nov 00:00 ET | Keep. It is well before the 30 Nov stocking |
| Simulator `DAY_ONE_FILL` (`vending/simulate/world.py`) | Wed 2 Dec 15:00 | Should be Mon 30 Nov. A code change for later |

## What the redraw touches

A read-only check of main at `9a1eb8b`, 20 Nov.

| Area | Follows the server's assignment? | Change needed? |
|---|---|---|
| Agent tools, ledger, stock count, planner (`vending/slots.py` `our_slots`) | Yes | No. A person runs `record` |
| Capacity (`vending/capacity.py`) | Not position-based: every column is the same, and capacity depends on package size only (`docs/capacity.md`, Kevin 19 Nov) | No. The redraw changes no slot's capacity |
| Wildcard rule (`vending/tools/wildcard_slot.py`, `docs/agent/wildcard.md`) | Yes for "ours" and the middle shelves | **Yes.** It refuses only column 5, written for the old layout, where column 5 was the one of ours beside the centre line. Now column 4 is beside it too, and the rule lets those through |
| Day-one plan rounds (`vending/plan_rounds.py`, `vending/plan_page.py`) | Yes: a round with a slot outside the assignment is refused | **Rounds made for the old slots fail to load after `record`.** Place them again on the new slots |
| Analysis (`analysis/slots_config.py`) | Yes | No code change. **Rerun `analysis/rebuild.py` after `record`.** `analysis/REPORT.md`, `analysis/product_history.md` and the planogram JSONs still name the old slots |
| Product history for the ten new slots | Fall data, before the fall cut, may be used for all 40 (`docs/integrity.md`) | The photo planograms were trimmed to the old slots. The ten new ones have no product history in the repo yet |
| Simulator seasons (`vending/simulate/seasons/*.json`) | Each season uses the assignment it was given (`--assignment`) | The 16 Nov seasons used the old slots. Rerun with the new one for current numbers |
| Simulator demand (`vending/simulate/world.py` `FALL_SEGMENT`) | Fall rates by product, keyed by where each drink sold in the fall | No. It doesn't say which slots are ours |
| Test and example slots (`tests/conftest.py` `TEST_SLOTS`, `vending/replay/cases.py` `CASE_SLOTS`, `vending/demo/make.py`) | Labelled as made-up | No. No test uses an assignment spread over every shelf, so a wildcard test for column 4 would need one |
| Pages on claude.ai (Slot Assignment, Machine Sales Map, Day-One Plan Rounds, Dashboard sample, Day-One Countdown) | Made from the old assignment or dates | Refresh after `record` (`docs/artifacts.md`) |

## Questions for David

1. **Opening day.** The email says the machine won't operate until 3 Dec, and also that 1 or 2 Dec is the first day of operation. Which is it? If it depends on stocking, when will we know?
2. **When tax becomes owed.** Does `sales_tax_owed` grow with each sale during the week, or only after Saturday's 23:59 calculation? The tracking check compares our figure with yours, so we need the same rule.
3. **Paying the tax.** Is sales tax ever taken out of our cash? If so, when: after each Saturday calculation, or once at the end?
4. **The day-one fill and cash.** When the books start, is the cost of the 30 Nov fill already taken out of the $600? On 12 Nov you said the fill comes out of the $600 and you would check (12 Nov notes, row 3).
5. **Tracking tolerance.** Is ±2 units for each slot and each product, or for the total? Is ±$1 for cash and tax each?
6. **Interval counters.** After the 30 Nov reset, will the interval counters be reset again during the quarter (at each restock, say)? We don't use them, but the door counter's since-reset count is in every poll.
7. **Tracking check timing.** Once the quarter starts, is the 1 call a day counted per calendar day, Eastern? Does a call that gets an error count?
