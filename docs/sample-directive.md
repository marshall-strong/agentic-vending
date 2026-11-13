# Sample restock and pricing directive

The project brief asks for "a sample restock and pricing directive -- the artifact operations would
actually receive and act on" (Project Brief (Revised), section 5). This is a real one, not a mock-up:
the agent's first live test run produced it, and it went to the bridge as purchase request
`pr_v8hB_h3gQatc` for the scheduled visit on Friday 13 Nov 2026.

| | |
|---|---|
| Run | Agent run 2, a live test before the competition, `claude-opus-5-5` at `medium` effort, 12 Nov 2026 00:02-00:04 ET, model cost $0.40 |
| Request | `pr_v8hB_h3gQatc`, accepted by the bridge as `pending` at 05:04 UTC |
| Visit | Scheduled (Friday, no fee) |
| Money | $233.31 expected, $270.00 at most, from a spendable balance of $599.71 |
| Code | `preview_desired_state` wrote it; `submit_desired_state` sent it (`main` plus PR #58's agent loop) |

**This was a test.** The instructors agreed the request can be reset before the December live run, and
the Azure database it was planned against holds test data. The receipt goes below once the worker
reports Friday's visit.

## The directive, exactly as stored

`submit_desired_state` saves this text in `purchase_submissions.directive` before it sends anything.

```text
Visit: scheduled

Slot  Action     Product                         Price  Load  From  Pull
15    switch     REDBULL-ORIGINAL-8_4CAN         $4.50  11    buy   -
16    no change  -                               $4.50  -     -     -
17    switch     CELSIUS-SPARKLING_ORANGE-12CAN  $4.50  8     buy   -
18    no change  -                               $5.00  -     -     -
25    no change  -                               $4.00  -     -     -
26    switch     CELSIUS-SPARKLING_ORANGE-12CAN  $4.50  8     buy   -
27    no change  -                               $3.00  -     -     -
28    no change  -                               $3.00  -     -     -
35    switch     MOUNTAIN-ORIGINAL-20BOT         $3.25  7     buy   -
36    no change  -                               $2.00  -     -     -
37    no change  -                               $2.00  -     -     -
38    no change  -                               $3.00  -     -     -
45    no change  -                               $2.00  -     -     -
46    no change  -                               $2.00  -     -     -
47    switch     COCA-ORIGINAL-20BOT             $3.50  7     buy   -
48    no change  -                               $4.00  -     -     -
55    no change  -                               $1.00  -     -     -
56    switch     COCA-DIET-20BOT                 $3.25  7     buy   -
57    no change  -                               $1.00  -     -     -
58    no change  -                               $1.50  -     -     -

Shopping list
Amazon
  REDBULL-ORIGINAL-8_4CAN: 1 x 24-pack, max $40.50 a pack, for slot 15 (13 to closet)
    https://www.amazon.com/dp/B003PFPFIE
  CELSIUS-SPARKLING_ORANGE-12CAN: 1 x 12-pack, max $25.00 a pack, for slot 17 (4 to closet)
    https://www.amazon.com/dp/B007R8XGJA
  CELSIUS-SPARKLING_ORANGE-12CAN: 1 x 12-pack, max $25.00 a pack, for slot 26 (4 to closet)
    https://www.amazon.com/dp/B007R8XGJA
  MOUNTAIN-ORIGINAL-20BOT: 1 x 24-pack, max $57.00 a pack, for slot 35 (17 to closet)
    https://www.amazon.com/dp/B00ODVQ8U8
  COCA-ORIGINAL-20BOT: 1 x 24-pack, max $63.50 a pack, for slot 47 (17 to closet)
    https://www.amazon.com/dp/B004I405P4
  COCA-DIET-20BOT: 1 x 24-pack, max $59.00 a pack, for slot 56 (17 to closet)
    https://www.amazon.com/dp/B008CAB1OE
```

How to read it:

- **Action:** `switch` puts a new product in the slot; `no change` leaves the slot as it is.
- **Price:** the price on the tag after the visit, for every slot, including the ones that don't change.
- **Load:** units to put in the slot. **From:** `buy` means from this visit's shopping; `back stock` would mean from our closet.
- **Pull:** what to take out of the slot and put in the closet when the product changes.
- **Shopping list:** what to buy, grouped by store, with the most the worker may pay per pack. Units that don't fit the slot go to our closet.

## Why the agent chose this

The agent writes a reason for every slot it changes. These are its own words, stored with the run in
`agent_runs.plan`, lightly trimmed.

| Slot | Product and price | Agent's reason |
|---|---|---|
| 15 | Red Bull Original 8.4 oz can, $4.50 | Sold 17 units at $4.50 over 6 weeks but none last week, so it is probably empty. Red Bull costs about $1.46 a unit, so $4.50 makes about $2.71 a unit after tax. |
| 17 | Celsius Sparkling Orange 12 oz can, $4.50 | Sold 20 units at $4.50 over 6 weeks but none last week, so it is probably empty. Celsius costs about $1.80 a unit and makes about $2.37 a unit at $4.50. |
| 26 | Celsius Sparkling Orange 12 oz can, $4.50 | Sold 20 units at $4.00 over 6 weeks but none last week, so it is probably empty. A second Celsius slot, because a 12-pack is cheap ($21.57). |
| 35 | Mountain Dew 20 oz bottle, $3.25 | Sold nothing last week, so it is probably empty. $3.25 is above its $2.60 floor and makes about $0.96 a unit. |
| 47 | Coca-Cola 20 oz bottle, $3.50 | Sold nothing last week, so it is probably empty. $3.50 is above its $2.90 floor and makes about $0.95 a unit. |
| 56 | Diet Coke 20 oz bottle, $3.25 | Sold nothing last week, and $1 earlier did not move much. $3.25 is above its $2.70 floor. |

Its summary: "This is the first run and we have no stock records, and most of our 20 slots sold nothing
last week, so I submitted a $233 order (at most $270) that fills 6 slots with the five drinks we have
prices for." Only five drinks on our price list were in the catalog that night, which is why the other
14 slots stay as they are.

The floors come from the pricing gate (`vending/pricing.py`): the most the worker may pay per unit,
plus 8% sales tax, rounded up to the next 5 cents.

## What operations actually receives

The bridge does not take this text. It takes the request body below, and the worker reads it through
the instructors' purchaser portal. The directive is our rendering of exactly the same lines, so a
person can check the plan without reading JSON. How the portal lays the request out is the
instructors' design, and we have not seen it.

<details>
<summary>The request body as sent (<code>POST /api/v1/purchase-requests</code>)</summary>

```json
{
  "off_cycle": false,
  "lines": [
    {"slot": "15", "source": "purchase", "primary": {"sku": "REDBULL-ORIGINAL-8_4CAN", "store": "Amazon", "url": "https://www.amazon.com/dp/B003PFPFIE", "packs": 1, "units_per_pack": 24, "max_price_per_pack": "40.50", "sale_price": "4.50", "change_product": true}},
    {"slot": "17", "source": "purchase", "primary": {"sku": "CELSIUS-SPARKLING_ORANGE-12CAN", "store": "Amazon", "url": "https://www.amazon.com/dp/B007R8XGJA", "packs": 1, "units_per_pack": 12, "max_price_per_pack": "25.00", "sale_price": "4.50", "change_product": true}},
    {"slot": "26", "source": "purchase", "primary": {"sku": "CELSIUS-SPARKLING_ORANGE-12CAN", "store": "Amazon", "url": "https://www.amazon.com/dp/B007R8XGJA", "packs": 1, "units_per_pack": 12, "max_price_per_pack": "25.00", "sale_price": "4.50", "change_product": true}},
    {"slot": "35", "source": "purchase", "primary": {"sku": "MOUNTAIN-ORIGINAL-20BOT", "store": "Amazon", "url": "https://www.amazon.com/dp/B00ODVQ8U8", "packs": 1, "units_per_pack": 24, "max_price_per_pack": "57.00", "sale_price": "3.25", "change_product": true}},
    {"slot": "47", "source": "purchase", "primary": {"sku": "COCA-ORIGINAL-20BOT", "store": "Amazon", "url": "https://www.amazon.com/dp/B004I405P4", "packs": 1, "units_per_pack": 24, "max_price_per_pack": "63.50", "sale_price": "3.50", "change_product": true}},
    {"slot": "56", "source": "purchase", "primary": {"sku": "COCA-DIET-20BOT", "store": "Amazon", "url": "https://www.amazon.com/dp/B008CAB1OE", "packs": 1, "units_per_pack": 24, "max_price_per_pack": "59.00", "sale_price": "3.25", "change_product": true}}
  ]
}
```

The bridge answered `{"id": "pr_v8hB_h3gQatc", "status": "pending"}`.

</details>

## Checked against our own rules

`docs/agent/directive.md` says a person who has never seen our code must be able to load every slot
and set every price without asking a question. Line by line:

| Rule | Met? | Notes |
|---|---|---|
| Slot number exactly as on the machine | Yes | |
| Product as brand, flavor and size, with no internal names or codes | **No** | The directive prints the catalog's SKU (`REDBULL-ORIGINAL-8_4CAN`), not its name ("Red Bull Original 8.4 oz can"). The catalog has the name; `_directive` in `vending/planner.py` doesn't use it. |
| How many units, or that the slot stays as it is | Yes | `Load`, or `no change` |
| The price on the tag | Yes | Every slot, including unchanged ones |
| What to do with units removed at a product change | **No** | `Pull` says `-` on every switch. The planner fills it only when our records show stock in the slot, and on this first run we had no receipts. The worker may well find old product in slots 15, 17, 26, 35, 47 and 56 and has nothing telling them what to do with it. |
| No conditions for the worker to work out | Partly | The order went in after the Wednesday Amazon cutoff, so it may arrive after Friday. The agent wrote "load these slots when the stock arrives" in its update to the team, but that never reached the worker: the request has an optional `note` field (up to 500 characters) that our planner doesn't fill. |
| Prices in 5-cent steps | Yes | Enforced by the pricing gate |

Three fixes follow from this, all in how the planner writes the request, not in the agent:

1. Print the catalog name instead of the SKU.
2. When the slot's contents are unknown, write "remove whatever is in the slot and put it in the closet" in `Pull`, not `-`.
3. Let the agent pass a short `note` for operations, and send it in the request's `note` field.

## Receipt

Pending. Friday 13 Nov's visit will be read by the hourly `sync_requests` job, and the worker's receipt
(units loaded, units to the closet, price paid) goes here next to the plan.
