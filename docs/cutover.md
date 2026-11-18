# Visits and the product and price cutover

**The problem** (David, class meeting 12 Nov, #97). A poll reports each slot's lifetime counters and the price it charges right now. It doesn't say when a worker swapped a product or changed a price. Say slot 15 sells an Arizona at $2.00 in the morning, the worker loads Red Bull at $4.00, and it sells one in the afternoon. If the last poll was the night before, the next poll shows 2 units sold at today's $4.00 price. In fact it was one of each.

**What is already safe.** Takings come from the rise in the lifetime cents counter (`vending/ledger.py`), never from units times today's price, so dollar totals survive any window. What can mix is units and dollars per product, and the price each unit sold at.

## When visits happen

Measured with `analysis/visit_windows.py` from the door counter (`EA2*DO`) in every stored poll since hourly polling began on 21 Sep 2026, to 11 Nov. Sales are from our 20 selections (the server's assignment when this was measured). Fall data for all 40 selections may be used, so this could be widened. The door counter is machine-wide and only dates visits.

| | |
|---|---|
| Door visits | 19, on all five weekdays: 7 Mon, 3 Tue, 2 Wed, 5 Thu, 2 Fri |
| Of those, refills (graded by `analysis/sales_analysis.py`) | 11: 2 proven, 7 likely, 2 possible top-ups. Only 1 on a Friday |
| Time of day | 7:30 AM to 3:30 PM; refills most often 1 to 2 PM, some 7:30 to 9:30 AM |
| Days between refills | median 5.5 for proven and likely refills (4.5 counting possible top-ups) |
| The window we can't see into | usually 60 min (one poll gap); 3 of 19 were longer, up to 4 h 7 min |
| Our units sold inside a window | 37 of 450 (8%), at 12 of the 19 visits |
| Poll gaps | 99.5% within 65 min; 7 longer, the longest three 3 hours each |
| Our sales by hour on class days | busiest 8 to 9 AM (3.8 a day), then 10 to 11 AM (2.6); after 1 PM under 0.7 an hour, and 0.2 to 0.4 from 2 to 4 PM |

So a visit after 1 PM puts few of our sales in doubt, and an early-morning one (7:30 to 9:30, which has happened) lands on our busiest hour. We asked for afternoon visits (#102); David's answer below makes Friday afternoon the usual time.

A door opening isn't always a refill. Before the competition, course staff stocked every slot, and David mentioned openings that weren't our class.

## The plan: handle a visit whenever it happens

We don't depend on a set restock time. A student worker won't always be on time, and the history shows visits on every weekday.

1. **Find the visit in the data.** A door-count rise between two polls marks it. A fulfilled request's `fulfilled_at` marks the end of the visit, and the receipt's door times pin it to the minute (below). A change in a slot's price field at a poll shows the new setup is in place.
2. **Mark the uncertain window**: from the last poll before the first opening to the first poll after the last one. Before it is the old setup; after it is the new one.
3. **Only our changed slots need care.** A slot whose product and price didn't change counts as usual.
4. **When the price changed, solve with the counters.** With `u` units and `c` cents inside the window, an old price `a` and a new price `b`, the number sold at the new price is `(c - u*a) / (b - a)`. A whole number from 0 to `u` settles it. Anything else means a misread or a third price: flag it for a person.
5. **When only the product changed** (same price), the counters can't say which product sold. Keep those units in takings, but leave them out of per-product demand and the slow-seller clock. Across the history that's about 8% of units on average; for one swapped slot, it's every unit that slot sold inside the window.
6. **Flag what doesn't line up**: a fulfilled request with no door rise, or a price change with no visit.

## What David said (12 Nov)

- **Restocks will be standard on Friday afternoons**, with 3 PM as the target. The student worker gets out of class at 1 and may have to drive to the stores first, so the load may well come at 3 to 5 PM or later. Our sales are lowest then (about 0.2 an hour after 3 PM), so a later visit puts even fewer sales in doubt. Poll about every 10 minutes from about 2 to 6 PM on Fridays. That isn't happening yet: our container only copies the bridge's hourly polls today. Our own Friday polling is #107.
- **`fulfilled_at` is the end of the visit**, set when the worker submits the receipt after loading. There's no push notification; we poll the request.
- **Door-open and door-closed times on the receipt** are live (David, 17 Nov), and the code reads them (below).
- **Polling about every 10 minutes is allowed.** That shrinks the one gap door times don't close: a sale between the last poll and the door opening. David named that case himself.

So the plan above gets a usual time to expect, and still handles a visit at any other time. Full notes: `docs/planning/2026-11-12-david-answers.md` (#106).

## Door times on the receipt (17 Nov)

David confirmed on 17 Nov that receipts now carry the door times. Each fulfilled request has `door_opened_at` and `door_closed_at`, beside `fulfilled_at`. They are to the minute. They can be null on older requests, or if the worker leaves them out.

Each line's `resolved_at` is when the worker filled that line in, not when it went in the machine. On our first filled request (`pr_v8hB_h3gQatc`, 12 Nov) the lines ran from 16:31:52 to 17:34:08, and the door was open from 16:34 to 17:34. So one line was filled in before the door opened.

How we use them:

- **Stored** on `purchase_requests` (migration 019) by `vending/purchase_requests.py` `store`. They are kept as a pair or not at all. An answer with only one of them keeps neither, and the sync logs a warning naming the request. An answer with close before open is refused and logged, and the receipt we had is kept. The migration fills them in from each request's stored answer. It skips a value that isn't a valid time (such as 31 November), half a pair, or close before open, and names those requests in a notice.
- **Stock** (`vending/stock.py`). The old product leaves at door open: pulls are timed then, and so are its sales from a window that holds the visit. The new product goes in at door close. No unit sells while the door is open. How a window between two polls is split, given the door times are only to the minute, is below.
- **Machine state.** `days_since_loaded` and `units_sold_since_load` count from door close. A sale between door close and `resolved_at` is sold since the load.
- **Ledger.** Purchases and visit fees are dated by door close too, the same clock as the stock they bring.

Without door times, everything works as before: `resolved_at` stands in for the visit.

The one gap they don't close is still a sale between the last poll and the door opening. Polling about every 10 minutes on Fridays (#107) shrinks it.

### The door times are minutes, not instants

We don't know whether the bridge truncates the door times or rounds them. We read each one as the minute it happened in: a door "opened at 16:34" opened between 16:34:00 and 16:35:00, and one "closed at 17:34" closed by the end of 17:34. So the door had certainly opened by the end of its open minute, and it can't have closed before the start of its close minute. (If the bridge rounds, a time could also be up to 30 seconds late. We haven't allowed for that.)

For a window between two polls, in a slot whose product changed at the visit (`vending/stock.py` `_split_by_door`):

- **Wholly inside the visit** (starts after the open minute, ends by the start of the close minute): no unit can sell with the door open. If the counters rose, the receipt is contradicted. The units are flagged and left unattributed.
- **All old product**: any other window that ends by the start of the close minute. The new product wasn't in yet.
- **All new product**: any other window that starts after the end of the open minute. The old product was out. A window that starts *during* the open minute is not all new: the door may not have opened yet.
- **Everything else** touches an uncertain minute or holds the whole visit. The price solve in step 4 decides it, as without door times. With the same price on both sides, its units are left unattributed (step 5).
- **From inside one visit to inside the next** (same slot, two changes): the product loaded at the first, since nothing else could have sold between them. A window that could hold two or more products across two or more visits is left unattributed.
- **Two visits to one slot that share a minute** (one closes at 15:34 and the next opens at 15:34, or a visit opens and closes within 15:34 next to another): they go in the order the requests were submitted. If that doesn't settle it (submitted at the same moment, door times that overlap, or door times that disagree with the submission order), neither visit's door times are used. Their lines are timed by `resolved_at`, as before, with a warning.

When the timing can't settle which product sold, we fall back to what we did before door times: the price solve, or units left unattributed and flagged. We never guess.

## What we checked, and what David answered

Asked in #102, with the evidence. David's answers (12 Nov) are in the section above.

- `fulfilled_at` is already in the purchase-request response and we store it (`vending/purchase_requests.py`). David says it marks the end of the visit: it's set when the worker submits the receipt, after loading.
- In all 1,325 CRC-checked polls stored for `vmc1` (read from the raw DEX text in our database), `EA2*DO` carries only its two counts (since reset and lifetime), never a "door open now" flag. We may simply never have polled during a visit. Not discussed on 12 Nov, so still open.
- `EA7` (power outages) changed only 3 times since 27 Aug, so a restock doesn't power-cycle the machine. It isn't a visit signal.
- `POST /api/v1/poll` reads the machine live (about 8.5 s). `docs/dex-bridge.md` asks us to use it rarely, so we asked before polling every 10 minutes around a visit. David allowed it; our own polling isn't built yet (#107).
- Visits after 1 PM: our sales are lowest then, so a visit puts the fewest units in doubt. David's target is 3 PM on Fridays.

The interactive version, with the dark-period charts and a calculator for step 4, is the [Cutover Clock](https://claude.ai/artifact/BSgBkggAC5PYBpdPqRHp1H) (Golisano sign-in).

## Reproduce

```powershell
analysis\.venv\Scripts\python.exe analysis\rebuild.py --export-python .venv\Scripts\python.exe
.venv\Scripts\python.exe analysis\visit_windows.py
```

The first command writes `analysis/data/*.csv` and `analysis/out/lab_data.json` (for the refill grades); the second writes `analysis/out/visit_windows.json` and prints the summary.
