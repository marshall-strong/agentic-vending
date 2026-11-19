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

- **Restocks will be standard on Friday afternoons**, with 3 PM as the target. The student worker gets out of class at 1 and may have to drive to the stores first, so the load may well come at 3 to 5 PM or later. Our sales are lowest then (about 0.2 an hour after 3 PM), so a later visit puts even fewer sales in doubt. Poll about every 10 minutes from about 2 to 6 PM on Fridays. Our worker now does its own Friday reads (below, #172), tighter than that: every minute from 2:45 to 7 PM.
- **`fulfilled_at` is the end of the visit**, set when the worker submits the receipt after loading. There's no push notification; we poll the request.
- **Door-open and door-closed times on the receipt** are live (David, 17 Nov), and the code reads them (below).
- **Faster reads around a visit are fine.** We asked about every 10 minutes, and David said that "won't break anything"; he set no limit, and named a poll every 15 seconds as going badly wrong. Faster reads shrink the one gap door times don't close: a sale between the last poll and the door opening. David named that case himself.

So the plan above gets a usual time to expect, and still handles a visit at any other time. Full notes: `docs/planning/2026-11-12-david-answers.md` (#106).

## Door times on the receipt (17 Nov)

David confirmed on 17 Nov that receipts now carry the door times. Each fulfilled request has `door_opened_at` and `door_closed_at`, beside `fulfilled_at`. They are to the minute. They can be null on older requests, or if the worker leaves them out.

Each line's `resolved_at` is when the worker filled that line in, not when it went in the machine. On our first filled request (`pr_v8hB_h3gQatc`, 12 Nov) the lines ran from 16:31:52 to 17:34:08, and the door was open from 16:34 to 17:34. So one line was filled in before the door opened.

How we use them:

- **Stored** on `purchase_requests` (migration 019) by `vending/purchase_requests.py` `store`. They are kept as a pair or not at all. An answer with only one of them, or with one that isn't a time (a number, say), keeps neither, and the sync logs a warning naming the request; the receipt itself is stored. An answer with close before open is refused and logged, and the receipt we had is kept. The migration fills them in from each request's stored answer. It skips a value that isn't a valid time (such as 31 November), half a pair, or close before open, and names those requests in a notice.
- **Stock** (`vending/stock.py`). The old product leaves at door open: pulls are timed then, and so are its sales from a window that holds the visit. The new product goes in at door close. No unit sells while the door is open. How a window between two polls is split, given the door times are only to the minute, is below.
- **Machine state.** `days_since_loaded` and `units_sold_since_load` count from door close. A sale between door close and `resolved_at` is sold since the load.
- **Ledger.** Purchases and visit fees are dated by door close too, the same clock as the stock they bring.

Without door times, everything works as before: `resolved_at` stands in for the visit.

The one gap they don't close is still a sale between the last poll and the door opening. The Friday reads below shrink it.

### Reading the machine during our visit (#172)

A sale between the last read before a visit and the first one after it can't be told apart (old product or new) when the price didn't change. **A read taken while the door is open settles it:** no unit sells with the door open, so everything before that read is the old product and everything after it is the new one, and the receipt's door times prove the read fell inside the visit. `_split_by_door` already does this with any poll that lands inside the visit; it needed nothing new, only such a poll (`tests/test_stock.py`, "A read taken while the door is open").

So the worker (`vending/visit_polls.py`, settings in `docs/deploy.md`) asks the bridge to read the machine **every minute, Fridays from 2:45 to 7 PM Eastern**, until a request of ours is fulfilled and a read taken after its door closed is stored. **A visit can be only a few minutes long** (the first one, `pr_v8hB_h3gQatc`, kept the door open for an hour, but a quick top-up won't), **so a read has to be close to the next one to land inside it**: with a 10-minute interval a 6-minute visit can fall between two reads. The window starts at 2:45 because the worker leaves a 1 PM class and the target is 3 PM, and ends at 7 PM because 3 to 5 PM "or after" is what David said. A midweek visit has no day we can tell from the data, so it gets no extra reads, unless a person lists the day in `VISIT_POLL_DATES` (below).

**Unknown until we try it: whether the machine answers a poll while its door is open.** The day-one fill (about 30 Nov, a Monday; its exact day isn't in the data) is a visit of ours, but the reads run on Fridays only. **Set `VISIT_POLL_DATES` to the fill day** (an ISO date, Eastern, such as `2026-11-30`; `docs/deploy.md`) to learn on that day whether the machine answers with the door open. It gets the same window and the same stop rule as a Friday. Otherwise the first Friday visit (4 Dec) shows it. Nobody has tried: `EA2*DO` has only ever shown counts, and we may never have polled during a visit (see below). If it doesn't answer, no read lands inside the visit, and the first read after the close still bounds the window: it runs from the last read before the door opened to the first one after it closed, so it is the visit itself plus about a minute before it and a few minutes after it, not the hour between two hourly polls. (Failed reads during the visit would trigger the 5-minute wait after 3 in a row, so the first read after the close can come up to 5 minutes late.) On the fill day (with the date set) or the first Friday, look in the worker's log for "visit reads: skipped" lines while the door was open (the receipt gives the times): a run of them that stops when the door closes would mean the machine doesn't answer with the door open. A stored poll between the door times means it does.

### The door times are minutes, not instants

We don't know whether the bridge truncates the door times or rounds them. We read each one as the minute it happened in: a door "opened at 16:34" opened between 16:34:00 and 16:35:00, and one "closed at 17:34" closed by the end of 17:34. So the door had certainly opened by the end of its open minute, and it can't have closed before the start of its close minute. (If the bridge rounds, a time could also be up to 30 seconds late. We haven't allowed for that.)

For a window between two polls, in a slot whose product changed at the visit (`vending/stock.py` `_split_by_door`):

- **Wholly inside the visit** (starts after the open minute, ends by the start of the close minute): no unit can sell with the door open. If the counters rose, the receipt is contradicted. The units are flagged and left unattributed.
- **All old product**: any other window that ends by the start of the close minute. The new product wasn't in yet.
- **All new product**: any other window that starts after the end of the open minute. The old product was out. A window that starts *during* the open minute is not all new: the door may not have opened yet.
- **Everything else** touches an uncertain minute or holds the whole visit. The price solve in step 4 decides it, as without door times. With the same price on both sides, its units are left unattributed (step 5).
- **From inside one visit to inside the next** (same slot, two changes): the product loaded at the first, since nothing else could have sold between them. A window that could hold two or more products across two or more visits is left unattributed.
- **Two visits to one slot that share a minute** (one closes at 15:34 and the next opens at 15:34, or a visit opens and closes within 15:34 next to another): they go in the order the requests were submitted. If that doesn't settle it (submitted at the same moment, door times that overlap, or door times that disagree with the submission order), neither visit's door times are used. Their lines are timed by `resolved_at`, as before, with a warning.

A poll inside the open minute may have come a few seconds before the door really opened, after an old-product sale. When it already shows the new price, and the poll before it showed another, the price change says the visit had started, so the window after it is the new product's. That reading needs this visit to have set the price. If another visit of ours falls between those two polls, it may have set it instead, and the counters decide as usual (#172).

When the timing can't settle which product sold, we fall back to what we did before door times: the price solve, or units left unattributed and flagged. We never guess.

**Unattributed units belong to neither product** (#172). The stock replay books them as sold, with the reason, but not as the old product's sales or the new one's (`SaleCost.unattributed`, `SlotCount.unattributed`). They are out of the slot's known count, so on hand is that count up to `SlotCount.at_most`: what would be left if none of them were the new product's. Later sales, pulls and losses lower that bound too. What still treats them as the new product's, for now: cost of sales (their cost is taken from the new product's oldest units), machine state's demand and units sold since the load, the dashboard's sold column, and the planner's on-hand count. Items 2 to 5 of #172 fix those.

## What we checked, and what David answered

Asked in #102, with the evidence. David's answers (12 Nov) are in the section above.

- `fulfilled_at` is already in the purchase-request response and we store it (`vending/purchase_requests.py`). David says it marks the end of the visit: it's set when the worker submits the receipt, after loading.
- In all 1,325 CRC-checked polls stored for `vmc1` (read from the raw DEX text in our database), `EA2*DO` carries only its two counts (since reset and lifetime), never a "door open now" flag. We may simply never have polled during a visit. Not discussed on 12 Nov, so still open.
- `EA7` (power outages) changed only 3 times since 27 Aug, so a restock doesn't power-cycle the machine. It isn't a visit signal.
- `POST /api/v1/poll` reads the machine live (about 8.5 s). `docs/dex-bridge.md` says we should rarely need it (our own note, not David's), so we asked before polling every 10 minutes around a visit. David said that won't break anything and set no limit. Our worker reads every minute on Friday afternoons, Kevin's choice (#172).
- Door rises from 3 Dec: the offline analysis (`analysis/visit_windows.py`, `analysis/sales_analysis.py`) counts a rise as our visit only when a fulfilled receipt of ours falls with it (`analysis/data/receipts.csv`, from `export_sales.py`). A receipt's candidate rises are the counter windows (the polls a rise is measured between, so a misread poll doesn't shorten it) that overlap [`fulfilled_at` − 1 hour, first line resolved]: the door opened by the first line and a visit runs at most an hour (`door_events.our_visits`). One candidate: the receipt claims it. Several: the receipt is still one visit of ours, with an ambiguous door window, and none of those rises is in a window or a sales calculation. None: one visit of ours with an unknown window. Our visit is never dropped (`visit_windows.py` marks them `window: ambiguous | unknown`; `sales_analysis.py` lists them as `visits_without_window`). A rise with no receipt is left out of our windows, not merged into them. Door times on the receipt aren't stored yet, so they don't narrow the match. Before 3 Dec every rise counts, so the pre-competition history reads as before.
- Visits after 1 PM: our sales are lowest then, so a visit puts the fewest units in doubt. David's target is 3 PM on Fridays.

The interactive version, with the dark-period charts and a calculator for step 4, is the [Cutover Clock](https://claude.ai/artifact/BSgBkggAC5PYBpdPqRHp1H) (Golisano sign-in).

## Reproduce

```powershell
analysis\.venv\Scripts\python.exe analysis\rebuild.py --export-python .venv\Scripts\python.exe
.venv\Scripts\python.exe analysis\visit_windows.py
```

The first command writes `analysis/data/*.csv` and `analysis/out/lab_data.json` (for the refill grades); the second writes `analysis/out/visit_windows.json` and prints the summary.
