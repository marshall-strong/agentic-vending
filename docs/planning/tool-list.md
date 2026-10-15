# Vending Agent Tool List

Snapshot of the team's shared Claude doc, taken 15 Oct 2026. **The live copy is the doc:**
<https://claude.ai/code/artifact/82ae22ab-53a2-48c0-b4d5-c636efcc877d>. Edit and comment
there; this file is a point-in-time copy so the plan sits next to the code.

## Purpose

The group-b vending agent should get **11 MCP tools**: 7 that read and 4 that write. Every write is checked in code before it runs. This is the working list for the team. It reconciles the 21 September build plan, the bridge's live API spec (`http://aihub-pi:8000/docs`, version `0.1.0`), and the whiteboard from the 8 October team meeting.

Questions we still need the instructors to answer are collected at the end.

## What the DEX Bridge API gives us

The bridge covers reading the machine and restocking our slots. **It has no route for price changes, midweek visits or catalog requests.** Every route except `/health` needs the `X-API-Key` header, and the bridge is reachable only over Tailscale.

| Route | What it does |
| --- | --- |
| `GET /health` | Checks the bridge is up |
| `POST /api/v1/poll` | Reads the machine live (about 9 seconds) |
| `GET /api/v1/history?start=&end=` | Lists stored polls: every poll from anyone, back to 28 Aug, plus an hourly poll by David |
| `GET /api/v1/history/{session_id}/raw` | Returns one stored poll as raw EVA-DTS text |
| `GET /api/v1/products` | Lists orderable products: `sku`, `name`, `pack_size`. **No prices.** |
| `GET /api/v1/slots` | Lists our 20 slots on `vmc1`: columns 5-8 of every row (15-18, 25-28, 35-38, 45-48, 55-58) |
| `POST /api/v1/purchase-requests` | Submits a restock request. Each line fills one slot. |
| `GET /api/v1/purchase-requests/{id}` | Shows `pending`, `rejected` (with a `decision_note`), or the full receipts |

What matters for the tools:

- **A purchase line is `slot` + `sku` + `packs` (1-20), with an optional alternate.** There is no sale price and no product link. A back-stock line is `slot` + `sku` + `units` (1-200) taken from the closet.
- **`change_product` must be `true` exactly when the product differs from what the slot holds now.** A wrong value in either direction is refused.
- **The bridge does not check back-stock lines.** Tracking what's in the closet is our job.
- **A request shows nothing until every line is done.** Then each line gets a receipt: `outcome`, `total_paid` (exact), `unit_cost_paid`, `units_loaded`, `units_to_back_stock`, `units_from_back_stock`, `units_pulled`, `pulled_sku`.
- **There is no route that lists our requests,** so we save every request ID ourselves. Submitting too often returns `429`.
- **The machine never reports stock on hand** (`PA1-08` is empty). Stock = units loaded (from receipts) − units sold (from polls).
- **The catalog was still empty on 8 Oct** (`{"products":[]}`), so nothing can be ordered yet.

## Proposed tools

Eleven tools: seven read data and four change something. **Every check on a write tool runs in code and in the same statement as the write,** so the model can't get around it. Three of the write tools have no API route yet; see the open questions.

| # | Tool | R/W | What it returns or does | Checks before it runs | Backed by |
| --- | --- | --- | --- | --- | --- |
| 1 | `get_machine_state` | R | For each of our slots: SKU, current price (`PA1-02`), units sold since the last load, stock on hand (loaded − sold), days since loaded, units per day, days of stock left. Closet units and their value at cost for each SKU. Rival prices while they are visible (optional field). | — | History sync + receipts |
| 2 | `get_sales_history` | R | Units and ex-tax revenue per slot over a time window | — | History sync |
| 3 | `get_catalog` | R | Orderable SKUs with `pack_size`, plus our last known unit cost for each (`null` if we have never bought it). Searchable. | — | `/products` + receipts |
| 4 | `get_account_balance` | R | Balance calculated from the ledger: $600 start, minus receipts, $25 visit fees and token spend | — | Our ledger |
| 5 | `preview_purchase_request` | R | Runs every check from #6 and estimates the cost, without submitting anything | Same as #6 | Same code as #6 |
| 6 | `submit_purchase_request` | W | Lines of `{slot, sku, packs, alternate?}` or `{slot, sku, back_stock_units}`. **Works out `change_product` itself** from our slot records. Saves the request ID it gets back. Pulling a slow seller or moving a product is done here. | Not halted; before the Thursday-midnight cutoff; balance covers the estimated cost; enough units in the closet; no request already pending; slots are ours | `POST /purchase-requests` |
| 7 | `get_purchase_requests` | R | Our requests with their status, `decision_note` and receipts | — | Our database, filled by the sync job |
| 8 | `set_price` | W | Queues a price change for the next visit | Not halted; at or above our floor (cost + tax, rounded up to the next 5 cents); at or below our ceiling; a multiple of 5 cents (rejected, not rounded) | **No API route yet** |
| 9 | `request_service_visit` | W | Asks for a $25 midweek visit and bundles every queued restock, swap and price change into it | Not halted; balance covers $25; no visit already pending | **No API route yet** |
| 10 | `get_pnl` | R | Ex-tax revenue, cost of goods (from `unit_cost_paid`), visit fees, token spend, and inventory at cost (machine + closet) | — | Our ledger |
| 11 | `request_catalog_product` | W | Asks the instructors to add a drink to the catalog | Not halted; it is a drink; no alcohol or THC; not already in the catalog | **No API route yet** |

`check_books` waits until the instructors' balance-check endpoint exists. It would compare our figures with theirs and report a match or the difference.

## Scheduled code, not tools

**Anything that runs the same way every time is a scheduled job, not a tool the model has to choose to call.** These jobs never cost model tokens:

- Syncing poll history from the bridge (this replaces our laptop poller)
- Checking each pending purchase request until it resolves, which is how we "receive updates" from the student worker
- Writing receipts into the ledger, slot loads and closet stock
- Logging token usage from every model response
- The halt switch: checked inside every write, and the MCP server can be switched off as a backstop

**Proposed frequencies (to be decided).** The scheduled jobs cost no tokens, so they can run often. The agent costs tokens, so it runs on a weekly rhythm and is woken in between only when a job flags something.

| Job | Proposed frequency | Why |
| --- | --- | --- |
| Sync poll history | Hourly, at 10 past the hour | David's poller reads the machine on the hour. Reading history never touches the machine. |
| Check pending purchase requests and write receipts | Hourly, only while a request is pending | Requests resolve after the Friday visit; there's nothing to check otherwise. Receipts are written as soon as one resolves. |
| Check the catalog for new products | Once a day | The catalog changes rarely. This also spots catalog requests that have been fulfilled. |
| Log token usage | After every model call | Not on a timer |
| Halt switch | Inside every write | Not on a timer |
| Run the agent (model) | Main run Thursday, ahead of the midnight cutoff; otherwise only when the sync job flags something | Flags: a slot close to running out, a machine price that doesn't match what we asked for, a rejected request. See "Decide as a team". |

Fixed rules (price bounds, our slot list, the Thursday/Friday timeline) go in the system prompt or an MCP resource, not in a tool.

```mermaid
flowchart TD
    bridge["DEX Bridge (aihub-pi, Tailscale only)<br/>Poll history, purchase requests, receipts"]
    sync["Scheduled sync job (not a tool)<br/>Copies new polls from history<br/>Checks pending requests until they resolve<br/>Writes receipts into the ledger and closet stock"]
    db["Our database<br/>Polls, slot loads, closet stock, requests,<br/>ledger, price queue, halt switch, action log"]
    tools["MCP server: 11 tools<br/>7 read, 4 write; every write is checked in code"]
    agent["Agent and harness (runs on a schedule)<br/>Calls the model only where judgment is needed<br/>Logs token usage from every API response"]
    bridge --> sync --> db
    tools <--> db
    agent --> tools
    agent -- "token usage" --> db
    tools -- "#6 submits restock requests" --> bridge
```

The agent never talks to the bridge directly, except through `submit_purchase_request`. Everything else it learns from our database.

## Back stock (the closet)

**Drinks in the closet count as assets, valued at what we paid for them, so pulling a slow seller costs nothing on paper.** The closet holds extra drinks for later restocks and drinks pulled from slots that weren't selling.

The bridge doesn't track the closet, so our database is the only record of it. Receipts update it:

| Receipt field | Effect on the closet |
| --- | --- |
| `units_to_back_stock` | Adds extra drinks bought that didn't fit in the slot |
| `units_pulled` + `pulled_sku` | Adds drinks taken out of a slot when it switched product |
| `units_from_back_stock` | Removes drinks loaded from the closet into a slot |

Each unit carries its `unit_cost_paid`, so the closet's value and our cost of goods both come from the same records. Moving a product from slot A to slot B takes two purchase-request lines: switch slot A (which pulls the old drink into the closet), then load slot B from the closet. Whether both can go in the same request is an open question.

## Whiteboard mapping (8 Oct meeting)

**Every idea on the board maps to a tool above or to scheduled code.** Three of them added to the list: how long items sit in the machine, comparing store prices, and asking for new products.

| Whiteboard idea | Where it lands | Notes |
| --- | --- | --- |
| Receive updates | Scheduled sync job + `get_purchase_requests` (#7) | MCP tools can't receive pushes; the job checks pending requests and records receipts |
| Get machine state: units sold vs stock on hand | `get_machine_state` (#1) | |
| How long items sit in the machine, over a set time period | #1 and #2 | Days since loaded, units per day, days of stock left; a time window on sales history |
| Get account balance, start with $600 | `get_account_balance` (#4) | |
| Get sales history | `get_sales_history` (#2) | |
| Pricing: set price and change | `set_price` (#8) | Waits for a visit, since prices are physical tags |
| Pricing: pull prices from stores, comparison | Not yet decided | Fills the "catalog has no prices" gap. Use a narrow `get_store_price(sku)` against one retailer, or a price table the team fills in. **No general web-browsing tool.** |
| Request service visit ($25): switch inventory, change pricing, restock | `request_service_visit` (#9) | Bundle all three jobs into one visit |
| Keep track of token usage | Harness, not a tool | Shows up in #4 and #10 |
| Submit purchase request: preview, check, send it out | #5 then #6 | Updates arrive through #7 |
| Move product if not selling well | #6 | A slot switch pulls it to the closet |
| New product research: search catalog, request product | `get_catalog` (#3), `request_catalog_product` (#11) | |
| Get P&L | `get_pnl` (#10) | |
| Holiday drinks | Strategy, not a tool | Depends on what the catalog offers; #2 shows whether seasonal items sell |

## Open questions

**Three write tools (#8, #9, #11) can't be built until the first four questions (prices, midweek visits, catalog requests) are answered.** Tick each one off as it's answered and note the answer beside it.

### Ask David

- [ ] How does a price change reach the worker? A purchase request has no price field. Is the 500-character `note` the intended channel?
- [ ] Can we change a price without restocking anything? If a purchase request needs at least one line, a price-only change may have no way through.
- [ ] How do we ask for a $25 midweek visit?
- [ ] How do we ask for a product to be added to the catalog, and how long does it take?
- [ ] Is the catalog shared with group A, or does each team have its own? (`/products` is described as "What your team can order".)
- [ ] Can a catalog request be refused, and will we be told why?
- [ ] Will the catalog include prices, or do we only learn the cost from the first receipt?
- [ ] Does sales revenue go back into our account, or is $600 the most we can ever spend?
- [ ] Can one purchase request pull a drink from slot A and load it into slot B? Or do pulled drinks only reach the closet after that request finishes?
- [ ] Can we pull a product and leave the slot empty? Every pull in the API happens by switching the slot to another product.
- [ ] Apart from purchase-request receipts, will the worker report anything back to us (jammed slots, damaged cans, closet counts), and where?
- [ ] Can we ask the worker for a count of our closet stock between visits (for example through the `note`), and where would we see the answer?
- [ ] What is the official sales-tax method: is tax included in the shelf price (price ÷ 1.08), or 8% added on top?
- [ ] When will the balance-check endpoint exist, and what tolerance does it allow (about $0.25 was mentioned)?
- [ ] At the end of the run, how is unsold stock in the machine and the closet counted in our score?
- [ ] Does token spend during development and testing count against the $600, or only during the live run?
- [ ] Is a Postgres database provided, or do we host our own?
- [ ] Can our repo be public?
- [ ] Bottle deposit: who pays it, and does it count in cost of goods?
- [ ] What promotions are allowed?

Already answered on 8 Oct: the closet exists and holds pulled stock at cost, and we can ask for new catalog products.

### Decide as a team

These are ours to answer; nobody else will decide them for us.

- [ ] Price bounds: what are the actual floor and ceiling? Proposal: floor = average unit cost + tax, rounded up to the next 5 cents; ceiling = a flat cap such as $6.00.
- [ ] Cost method for cost of goods and closet value: average cost per SKU (the build plan's proposal) or first in, first out?
- [ ] Where do the sync job and the agent run? Marshall's laptop only runs while he's logged on; the Pi or another always-on Tailscale machine is better.
- [ ] How often does each scheduled job run? See the proposed frequencies in "Scheduled code, not tools": history hourly, pending requests hourly while one is open, catalog daily.
- [ ] Build the MCP server in n8n or plain Python? We ruled out n8n on 20 Sep; the build plan suggests reconsidering.
- [ ] Which model does the agent use, how often does it run, and what is our weekly token budget?
- [ ] Store price comparison: a narrow `get_store_price(sku)` tool, or a price table we fill in by hand?
- [ ] When do we pull a slow seller? What units-per-day threshold, over how many days?
- [ ] What goes in our 20 slots first once the catalog has products? Do holiday drinks get a slot?
- [ ] Who can flip the halt switch, and how?
