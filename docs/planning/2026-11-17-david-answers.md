# David's answers to our questions, 17 Nov 2026

Kevin sent David the open questions from #102 and the 12 Nov notes (`2026-11-12-david-answers.md`). David answered in writing on 17 Nov. The answers below are quoted or closely paraphrased from his reply. Times are Eastern.

## The dates

| When | What | Source |
|---|---|---|
| ~18 to 19 Nov | **The one slot redraw.** The server's assignment after it is the one we keep for the whole run | Row 3 |
| **Wed 25 Nov** | **Every product for the day-one fill must be requested and approved in the catalog.** The Institute is closed 26 to 29 Nov | Row 1 |
| **Sun 29 Nov, 23:59** | **The day-one purchase request must be received.** A request with a product that isn't in the catalog is denied automatically | Row 1 |
| Mon 30 Nov | Staff buy the fill and stock the machine | Row 1 |
| Thu 3 Dec | The machine goes live with our products | 12 Nov notes, row 2 |
| **Thu 3 Dec, 23:59** | **The first regular restock request is due.** A valid request received by then is filled on Fri 4 Dec | Row 5 |

## The answers

| # | Question | David's answer | Status |
|---|---|---|---|
| 1 | **Day-one deadline**, given the Thanksgiving closure (26 to 27 Nov) | **Purchasing and stocking take place on 30 Nov, so the request must be received by 23:59 on 29 Nov.** A purchase request is **denied automatically** if a product on it is not in the catalog, so every product we want must be **requested and approved by 25 Nov**. | Answered |
| 2 | **Catalog approval** before the fill, and how fast we hear | Yes, that's the normal process. **Poll the catalog request status endpoint** to find out whether each request was approved or refused. | Answered |
| 3 | **Slot redraw** | Yes, there will be one, **within the next day or two** (from 17 Nov). | Answered; watch the server |
| 4 | **Send the fill as a normal purchase request**, with a sale price on each line? | Yes, the normal process works for the initial stocking. | Answered |
| 5 | **First regular restock:** Fri 4 Dec or Fri 11 Dec? | **A valid purchase request received by 23:59 on 3 Dec is filled**, so Fri 4 Dec. | Answered |
| 6 | **The Interloper:** will it stay, and who picks its drinks and prices? | It is **outside the scope of this project**, and the instructors don't control its drink selection. | Answered: out of our control |
| 7 | May we use the **Interloper's prices** as a pricing input? | **No.** | Answered |
| 8 | After 3 Dec, will the polls and history keep the **door counter (`EA2*DO`)**, and carry only our slots? | The door counter is still sent, and **we receive the full, raw DEX response**. | Answered; see "Raw DEX" below |
| 9 | **Door-open and door-close times** on receipts: when? | **Live now.** | Answered |
| 10 | **Midweek visit:** fee, notice, timing | **$25 is correct.** **24 hours' notice** lets the student worker plan the shopping. Amazon items that haven't arrived when the store-bought drinks are stocked are stocked **at the next scheduled visit**. | Answered |
| 11 | Is an **Amazon order** placed as soon as a request is approved? | **Yes.** Example: a request is sent on Tuesday. When the student worker sees it, the Amazon order is placed. The store-bought items are bought on Friday, and everything is stocked on Friday. | Answered |

## What this means for us

- **The catalog deadline comes first (Wed 25 Nov).** It's 8 days away and earlier than the fill itself. Every drink on the day-one list must be approved by then. Filed is not enough. The bridge allows about 24 open catalog requests, so items still in the queue must be filed early enough to leave time for approval. Check status by polling, not by waiting.
- **The fill request (Sun 29 Nov, 23:59).** Send it as a normal purchase request, with a sale price on each line. Plan for every line to be fillable. One product missing from the catalog denies the whole request, and the Institute is closed from 26 to 29 Nov.
- **Record the ledger opening before Mon 30 Nov.** The fill is bought and its receipt is submitted that day (12 Nov notes, "What changes for us"). Record the opening when we send the fill request at the latest.
- **Slots.** The one redraw is due about 18 to 19 Nov. Re-plan the fill against the new assignment then. The fill is still a product list until then (#75).
- **First restock request: Thu 3 Dec, the opening day.** The agent plans it with less than one day of our sales. The day-one fill must cover the first Friday on its own. A 4 Dec request should top up only what the fall rates say will run short (restock policy rule 2). The first slow-seller flag still can't fire before Fri 11 Dec.
- **Midweek visits.** $25 and 24 hours' notice. A midweek request should use store-bought items. An Amazon item on a midweek request may wait for Friday, and we still pay the $25. Restock policy R5 assumed a midweek visit "may take longer than 24 hours". The 24-hour notice period points to a faster visit, so R5 should be re-checked once we have midweek receipts.
- **Amazon on regular restocks.** The order goes in when the student worker sees the request, not at a cutoff. Send early in the week to give Amazon time to arrive by Friday. The planner already warns about Amazon lines after Wednesday 23:59.
- **The Interloper is not a pricing input** (row 7). This is the same rule we follow for the other team's prices. See `docs/integrity.md`.
- **Door times are on receipts now** (row 9). They narrow the visit window in `docs/cutover.md` from "`fulfilled_at` and the next poll" to the door-open time. The code should read them.

## Raw DEX after 3 Dec

"The full, raw DEX response" most likely means that each poll carries **all 40 selections**, including the other team's 20 (inferred; we will know when the first post-start poll arrives). From 3 Dec, our integrity rule forbids using the other team's per-selection sales, prices or stock (`docs/integrity.md`). The agent's tools already filter to our server assignment. Every other reader must apply the same filter from the start: dashboard, analysis, stock count and sim. We may still store the raw text, because the poll is the machine's record. The door counter is machine-wide, so it dates visits and nothing more.

**Where the code stands (read-only check of main at `9523764`, 17 Nov):**

- **The agent is clean.** Every agent tool, the ledger, the planner and the replayer read only our slots, and no tool returns raw DEX.
- **These readers use every selection on main, with no cut at the start:**
  - `analysis/export_sales.py`, whose machine-wide hourly totals feed `sales_analysis.py`. After 3 Dec, the total minus ours gives the other team's sales.
  - `slot_counts.latest`, which feeds the local dashboard's `state.json` and `python -m vending.slot_counts show`.
  - The public `/db` page, which shows `polls.total_paid_*`, the machine-wide VA1 totals.
  - `scripts/bridge.py raw|latest|poll`, which prints the whole poll.
- **Open PR #146 (server slots) fixes the analysis and `slot_counts`.** It does not fix the `/db` totals or `scripts/bridge.py`.
- **The simulator writes 20-selection polls with no door counter.** No test feeds a post-start poll with all 40 selections.
- **Nothing reads the door times yet.** They are most likely already stored in `purchase_requests.last_response`. Read their field names from a fulfilled request there, then use them in `stock._split_at_changes` and in `machine_state`'s load dates.

## Still open

- **Day-one stock from the $600** (12 Nov notes, row 3): David was to confirm it. Not covered in this reply.
- **Restocks on Fri 25 Dec and Fri 1 Jan** (Institute closures). Not asked yet.
- **Door times field names.** Check them on the next real receipt and in the bridge spec.
