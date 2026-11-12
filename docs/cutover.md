# Visits and the product and price cutover

**The problem** (David, class meeting 12 Nov, #97). A poll reports each slot's lifetime counters and the price it charges right now. It doesn't say when a worker swapped a product or changed a price. Say slot 15 sells an Arizona at $2.00 in the morning, the worker loads Red Bull at $4.00, and it sells one in the afternoon. If the last poll was the night before, the next poll shows 2 units sold at today's $4.00 price. In fact it was one of each.

**What is already safe.** Takings come from the rise in the lifetime cents counter (`vending/ledger.py`), never from units times today's price, so dollar totals survive any window. What can mix is units and dollars per product, and the price each unit sold at.

## When visits happen

Measured with `analysis/visit_windows.py` from the door counter (`EA2*DO`) in every stored poll since hourly polling began on 21 Sep 2026, to 11 Nov. Sales are our 20 slots only; the door counter is machine-wide and only dates visits.

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

So a visit after 1 PM puts few of our sales in doubt, and an early-morning one (7:30 to 9:30, which has happened) lands on our busiest hour. That's worth asking for (#102).

A door opening isn't always a refill. Before the competition, course staff stocked every slot, and David mentioned openings that weren't our class.

## The plan: handle a visit whenever it happens

We don't depend on a set restock time. A student worker won't always be on time, and the history shows visits on every weekday.

1. **Find the visit in the data.** A door-count rise between two polls marks it. When David adds them, a fulfilled request's time and the worker's door times narrow it. A change in a slot's price field at a poll shows the new setup is in place.
2. **Mark the uncertain window**: from the last poll before the first opening to the first poll after the last one. Before it is the old setup; after it is the new one.
3. **Only our changed slots need care.** A slot whose product and price didn't change counts as usual.
4. **When the price changed, solve with the counters.** With `u` units and `c` cents inside the window, an old price `a` and a new price `b`, the number sold at the new price is `(c - u*a) / (b - a)`. A whole number from 0 to `u` settles it. Anything else means a misread or a third price: flag it for a person.
5. **When only the product changed** (same price), the counters can't say which product sold. Keep those units in takings, but leave them out of per-product demand and the slow-seller clock. Across the history that's about 8% of units on average; for one swapped slot, it's every unit that slot sold inside the window.
6. **Flag what doesn't line up**: a fulfilled request with no door rise, or a price change with no visit.

## What we've checked, and what we're asking David

Asked in #102, with the evidence:

- `fulfilled_at` is already in the purchase-request response and we store it (`vending/purchase_requests.py`). We haven't seen a filled request yet, so we don't know whether it marks the end of the visit or when the receipts were entered.
- In all 1,325 CRC-checked polls stored for `vmc1` (read from the raw DEX text in our database), `EA2*DO` carries only its two counts (since reset and lifetime), never a "door open now" flag. We may simply never have polled during a visit.
- `EA7` (power outages) changed only 3 times since 27 Aug, so a restock doesn't power-cycle the machine. It isn't a visit signal.
- `POST /api/v1/poll` reads the machine live (about 8.5 s). `docs/dex-bridge.md` asks us to use it rarely, so we're asking before polling every 10 minutes while a request of ours is out.
- Visits after 1 PM: our sales are lowest then, so a visit puts the fewest units in doubt.

The interactive version, with the dark-period charts and a calculator for step 4, is the [Cutover Clock](https://claude.ai/artifact/BSgBkggAC5PYBpdPqRHp1H) (Golisano sign-in).

## Reproduce

```powershell
analysis\.venv\Scripts\python.exe analysis\rebuild.py --export-python .venv\Scripts\python.exe
.venv\Scripts\python.exe analysis\visit_windows.py
```

The first command writes `analysis/data/*.csv` and `analysis/out/lab_data.json` (for the refill grades); the second writes `analysis/out/visit_windows.json` and prints the summary. `rebuild.py` comes with #83; on main without it, run `analysis/export_sales.py` and `analysis/sales_analysis.py` instead.
