# The MCP server

The agent's 13 tools, served over stdio by `python -m vending.server`. It's plain Python on the
official MCP SDK, in the same package as the history sync, so the tools import the parser, the
pricing gate and the database layer directly. The read tools only read our database, except `get_project_rules`, which reads our notes in `docs/agent/`; of the
built write tools, `request_catalog_product` also calls the DEX bridge. Nothing here polls the
machine.

| Tool | State |
| --- | --- |
| `get_briefing` | **Works.** One read-only snapshot: data quality and source ages, current Saturday–Friday ledger week, previous week and competition totals, all assigned slots with stock evidence and sales rates, closet and pending requests. Returns typed data and compact Markdown with `computed_at` and `snapshot_id`. Live API spend starts at the ledger opening. Incomplete sales or costs leave profit unknown. Logged through the normal read path. The agent calls it before its first model request. |
| `get_machine_state` | **Works.** Per slot: price, lifetime units, units sold and units/day over the last 7 days, from stored polls; and from the receipts (`vending.stock`), the product, stock on hand (since the product went in), units sold and days since the last load, and days of stock left. The demand to plan with, `units_per_class_day` (`vending.demand`): units sold per class day while the slot held stock, since its product went in (weekends, breaks and closures from `vending.class_days` left out); until a slot has 5 such days, the fall segment rate for its price, with `rate_basis` saying which, `class_days_in_stock` and `class_days_of_stock_left`. The closet per product with its value at cost, and any stock warnings. A slot with no receipts has those fields `null` and is named in the message. Where receipts don't know a slot, or a person's count is newer than its last receipt, `counted_product` and `counted_on_hand` say what the latest count in `slot_counts` saw (units less sales since; an estimate for a photo), with `counted_at`, `counted_days_ago` and `count_source`; a count over 7 days old is weak evidence and the message says so, and where `stock_on_hand` and `counted_on_hand` both appear the count is the newer. Sets `stale: true` and says so when the latest poll is over 3 hours old. `wildcard_trial` (null until a wildcard slot is set): the product on trial in the fixed wildcard slot, its units over the in-stock class days the polls saw, a verdict (`vending.wildcard_trial`), our silent and unconfirmed slots, and on a pass with a silent slot the promotion to make (`docs/agent/wildcard.md`). |
| `get_sales_history` | **Works.** Per slot over any window: units, revenue (includes the 8% tax) and revenue ex-tax, plus totals. Says which two polls the window actually spans. |
| `get_account_balance` | **Works.** The balance from the ledger (opening and later capital, plus revenue, minus receipts, midweek visit fees, token spend and sales tax remitted, plus or minus adjustments), the sales tax set aside and still owed, and the spendable balance after it. Fails with the command to run until a person has recorded the opening capital. |
| `get_pnl` | **Works.** Over any window of Eastern calendar days: revenue ex tax, units sold and how many of them could be costed, cost of sales at first-in, first-out cost, shrinkage, visit fees, token spend, adjustments, net, purchases, and inventory at cost in the machine and the closet. Capital and tax remittances are shown but are not in net. Model calls before the opening are development spend and are left out of token spend and net, with a note giving the amount. |
| `preview_desired_state` | **Works.** Runs the restock planner on live data: the request lines, the worker's directive, cost and balance left, each slot's price floor and how it was worked out, warnings, and every refusal at once. `success` is whether the plan could be submitted as it stands. Says what a person must do first when the ledger isn't open, our slots haven't been recorded, the catalog can't be read, or `capacity_overrides` in config is invalid. |
| `get_purchase_requests` | **Works.** Our requests, newest first (or only pending ones): status, decision note, the plan we sent, and each line's receipt with later losses. Reads only our database, so it's as fresh as the last `python -m vending.sync_requests`; pending requests that sync hasn't read in 6 hours (counted from submission if it has never read them) are listed in `not_checked_recently` and named in the message. |
| `get_catalog` | **Works, version 1.** The single-item products the bridge lists (sku, name, line, flavor, size), searchable by text, with when the catalog was last synced. Says so when that was over 36 hours ago. Our last unit cost, sourcing rows and pending catalog requests are not in it yet. |
| `get_project_rules` | **Works.** Our own notes on the project brief, from [`agent/`](agent/README.md). With no topic, the list of topics and their one-line summaries; with a topic, that file's text. A topic is a plain name from the list: paths, `..` and unknown names are refused with the list. Logged like any read. |
| `request_catalog_product` | **Works.** Files a request to add a drink to the catalog (`POST /api/v1/product-requests`), recorded in `product_requests`. See below for what it refuses. |
| `submit_desired_state` | **Works.** Plans the desired state again on live data inside the write gate, refuses with every problem at once, otherwise POSTs it once and records the request. Saves what it is about to send before sending, so a lost answer leaves a row a person settles with `python -m vending.submissions resolve`. |
| `set_wildcard_slot` | **Works.** Sets config `wildcard_slot` once, through the write gate: one of our slots, on shelf 2 to 4, not column 5, with a reason. Asking again for the same slot changes nothing; a different slot is refused once one is set, and only a person can change it (`python -m vending.config`). |
| `set_price` | **Dropped.** Prices change only at a worker visit (free Friday, $25 midweek), through `submit_desired_state`. The stub stays so a call gets that answer. |

## Run it

You need the local database from the history sync first ([`README.md`](../README.md), and
`DATABASE_URL` in your `.env`):

```
pip install -r requirements-dev.txt
docker compose up -d db
python -m vending.db migrate            # creates or updates the tables
python -m vending.slots record          # needs the class tailnet; stores which slots are ours
python -m vending.sync_history          # needs the class tailnet; loads real polls
python -m vending.server                # waits silently on stdin: that's correct
```

The server never migrates. If the database is missing a migration, it refuses to start and names
the missing ones; check that `DATABASE_URL` points where you mean (in production, our Azure
Container App, it's the shared Azure database), then run `python -m vending.db migrate`. Stop the server with Ctrl+C.

**For the live run, set `VENDING_TOOLS=built` in the real environment** (it is not read from `.env`). Then only working tools are registered, so the
agent doesn't pay tokens on every request to describe stubs. The default, `all`, registers all
12 so the team can see the whole contract in a client.

**The sync needs the class tailnet; the tests don't.** On a laptop also on a personal tailnet,
sign in to the class account once with `tailscale login`, then move between tailnets with
`tailscale switch <tailnet>` (`tailscale switch --list` shows them). Only one tailnet is active
at a time, so your personal machines drop off while you're on the class one. In production the
sync runs in our Azure Container App, which has to join the class tailnet.

### Try it in the MCP Inspector

Needs Node.js. From the repository root:

```
npx @modelcontextprotocol/inspector python -m vending.server
```

Open the URL it prints, click **Connect**, then **Tools → List Tools**, then run
`get_machine_state`.

### Connect Claude Desktop

Add this to `claude_desktop_config.json` (Settings → Developer → Edit Config), with your own
paths, then restart Claude Desktop:

```json
{
  "mcpServers": {
    "vending": {
      "command": "C:\\path\\to\\agentic-vending\\.venv\\Scripts\\python.exe",
      "args": ["-m", "vending.server"],
      "env": { "PYTHONPATH": "C:\\path\\to\\agentic-vending" }
    }
  }
}
```

The server reads `DATABASE_URL` from the repository's `.env`, so the config needs no secrets.

Every tool that reads our slots (`get_machine_state`, `get_sales_history`, `get_account_balance`,
`get_pnl`, `preview_desired_state`) takes them from the assignment `python -m vending.slots record`
stored. Until it has run, each one answers `success: false` and names that command.

## The halt switch

```
python -m vending.halt status
python -m vending.halt on --by kevin
python -m vending.halt off --by kevin
```

It starts **on**. Every write tool refuses with `halted` until a person turns it off. If the
`config` row is missing, it counts as on. The switch never migrates, so it can't change a shared
database's schema: on a database with no `config` table, `status` and `on` report halted, `off`
fails, and each names `python -m vending.db migrate`. Every flip is logged with the `--by` name, which is
required because every process in the shared Container App runs as the same user. If a write is in flight, the
command prints "waiting for an in-flight write to finish..." and goes through when it ends. The harder stop needs nothing
from the database: stop the server process, or remove it from the agent's MCP config.

## The write gate

Every write tool goes through `vending.write_gate.run_write`. In one transaction it:

1. Checks the halt flag and holds a lock on it.
2. Runs the write.
3. Inserts the `action_log` row.

The write and its log row commit together. A refusal or a crash rolls the write back and is then
logged on its own, so every attempt leaves a row.

**Read tools are logged too**, with `kind = 'read'`, their arguments and what they returned, so
you can reconstruct what the agent saw before each decision. The log is written by our code, not
sent to the model, so it costs no tokens. Read it with:

```sql
select at, kind, tool, outcome, detail from action_log order by at;
select at, tool, outcome, detail from action_log where kind = 'write' order by at;  -- writes only
```

`tests/test_server.py` calls every write tool through an MCP client and checks that it is
refused and logged while halted. A write tool registered without the gate fails that test.

### `request_catalog_product`

Each request goes to an instructor, and too many open ones get a `429`, so before sending it
refuses, all at once: a field the bridge would reject, a size that isn't a liquid volume (`20 oz
bottle`, `12 fl oz can`, `500 mL`), alcohol or THC (root beer and ginger ale are fine), a product
already in the catalog, a product we already asked for, and a request over our weekly limit.
**Before the machine goes live (midnight Eastern, Thu 3 Dec 2026, `MACHINE_LIVE`) there is
none**, so the team can ask for as many drinks as it wants; the bridge still refuses once about 24
of ours are open. From go-live the limit is per competition week, Thursday to Wednesday: 20 in the
first week, then 15, 10 and 5, and 3 a week after that (Kevin, 15 Nov; `WEEKLY_LIMITS` and
`FINAL_WEEKLY_LIMIT` in `vending/tools/catalog_request.py`, a starting point the team can tune).
Only requests filed since the current week began count, so requests made before go-live, or late in
the week before, never use up a later week's share. A request is a duplicate of an
earlier one, or of a catalog product, when its name, flavor and size have exactly the same words.
Before comparing, units are normalised ("20oz", "20 ounce" and "20 fl oz" are all "20 oz"), and
words that don't tell drinks apart are dropped: containers (bottle, can, pack) and default-flavor
words (original, classic, regular). So "20 oz" and "20 oz bottle" are the same request, while
"Sprite" and "Sprite Cherry" are different in either order. The alcohol check is a list of words
(beer, wine, spirits, wine grapes, cocktails such as margarita and mojito). Exceptions cover soft
drinks with those words in their names: root beer, ginger ale, juice cocktail, Sangría Señorial,
Kola Champagne, mocktails and "virgin" cocktails.

The bridge has no route that lists our requests, so `product_requests` (migration 005) is our only
record, and the duplicate check reads it. Neither bridge call retries, because the halt lock is
held while they run. The POST would never be retried anyway: a retry after a lost answer could
file it twice.

**Every step is saved on its own connection.** Just before the POST, the tool commits a row with
status `sending` on its own short connection, outside the write gate's transaction. The bridge's
answer is saved the same way as soon as it arrives. So whatever fails afterwards (the log insert,
the database connection, the server), what the bridge told us survives. Then:

| What happened | The row |
| --- | --- |
| The bridge accepted it | becomes `pending`, with the bridge's request ID |
| It certainly wasn't filed (no connection, a 4xx, a `429`) | is deleted |
| No answer came back (a timeout or a 5xx), or the answer had no request ID | becomes `unknown` |
| The server died before storing the answer | stays `sending` |

A request left `sending` or `unknown` may or may not exist on the bridge, so the same product is
refused until a person checks the bridge and settles the row:

```
python -m vending.product_requests list
python -m vending.product_requests resolve 7 --bridge-id np_abc123 --by kevin   # it was filed
python -m vending.product_requests resolve 7 --not-filed --by kevin             # it wasn't
```

Check the bridge's Swagger page (`GET /api/v1/product-requests/{id}`) first, and ask the
instructors before using `--not-filed`, because a resend of a request that was filed would file it
twice. Each change is logged in `action_log` with the `--by` name.

`pending` rows are updated by `python -m vending.sync_product_requests`, which the worker runs every
hour. It reads `GET /api/v1/product-requests/{id}` for each and stores the decision: `approved`
with the SKU the drink was added under (order it by that SKU; it may differ from what we asked for),
or `rejected` with the instructors' note. A decided request is not read again.

When the bridge refuses because too many requests are open, the next drinks wait in the
**backlog** (`python -m vending.request_backlog`, migration 017). See [`request-backlog.md`](request-backlog.md).

### `get_account_balance` and `get_pnl`: the ledger

Both read `vending/ledger.py`. **The balance is computed, never stored:**

```
opening capital + later capital + takings - receipts - midweek visit fees - token spend
- tax remitted +/- adjustments
```

Takings are the rise in the polls' lifetime sales counters for our slots since the opening
entry, tax included; `vending/counters.py` decides what a reading that goes down means (one low
poll between good ones is a misread, two in a row a counter that restarted). Receipts are
`purchase_lines.total_paid` on fulfilled requests, timed by the request's door close when the
receipt has door times, else by each line's `resolved_at`: the same clock as the stock they
delivered (`docs/cutover.md`). Every fulfilled off-cycle request costs the midweek visit fee
stored on it when it was submitted (`purchase_requests.visit_fee_cents`), or `config`
`midweek_fee_cents` ($25.00 by default) for requests without one, so a later change to the config
fee never rewrites past visits. The sales tax inside the takings (takings x 8/108, per tax week,
Sunday 00:00 to Saturday 23:59:59 Eastern as David calculates it, rounded half up; the week in
progress on its takings so far) is set aside; what is still **owed** is the set-aside less what has been
remitted, and the **spendable** balance is the balance minus that. It is what the planner's
balance gate checks against. Only what no other table records lives in `ledger_entries`
(migration 007): the one opening entry, later capital, each model call's cost, tax remittances
and adjustments. `python -m vending.ledger` is the only writer, and like the halt switch it never
migrates:

```
python -m vending.ledger open --amount 600.00 --by marshall --at 2027-01-04T13:00:00Z
python -m vending.ledger capital --amount 100.00 --detail "top-up from David" --by marshall
python -m vending.ledger tokens --cost 0.0421 --model claude-sonnet-5-5 --in 8120 --out 640 --reference msg_01
python -m vending.ledger remit --amount 8.00 --by marshall --reference "email 3 Dec"
python -m vending.ledger adjust --amount -3.50 --detail "bottle deposit, per David" --by marshall
python -m vending.ledger show
python -m vending.ledger entries
```

The kinds differ in what they mean for the P&L. **Capital** (a top-up) and a **remittance** (tax
handed over) move money without being profit or loss, so `get_pnl` shows them but leaves them out
of net. An **adjustment** is a correction to the books (a bottle deposit, a refund), so it is in
net. An entry dated before the opening is accepted but is outside the balance; the CLI warns and
`get_account_balance` says so in a note.

Until `open` has been run, `get_account_balance` fails and names the command, so the agent can't
plan against a balance nobody has set. The harness records every model call with
`ledger.record_tokens(conn, cost_usd, model, input_tokens, output_tokens, reference)`, or the
`tokens` command above; token spend is a dollar not spent on stock, so it is in the balance from
day one. Every entry is logged in `action_log` with who recorded it.

`get_pnl` leaves model calls before the opening out of token spend and net, even when the window
starts earlier, and a note gives their amount: they are development spend, not from the $600. With no
opening yet, every model call is development spend. The other lines still cover the whole window.

`get_pnl` takes its window as Eastern calendar days, inclusive, so a Friday evening sale lands in
Friday. After the opening, a poll at midnight sharp is in the day it starts, so a P&L over a tax
week's days counts that Sunday's 00:00 poll. When the ledger opened at that midnight, the opening
poll stays the baseline, so sales before the opening are not counted; a window that starts before
the opening reads its polls from its start, as before. Only the polls move: an entry, receipt or
visit at Saturday 23:59:59.999999 is Saturday's. Each row of `tax_weeks` (Sunday to Saturday,
as David calculates the tax) gives `from` and `to`, the part of the week the window counted, and
`partial: true` when the window cut it (the first week from the opening, a midweek start, or a week
still in progress), so its tax is the window's, not the week's figure. `units_sold` counts every rise in our slots' unit counters; cost of sales covers the
units the receipts can cost (first in, first out, `vending.stock`), and `units_costed`,
`cost_of_sales_complete: false` and a note say how many could not be (sold before we first loaded
the slot, or past what we loaded). Units sold across a product change that the counters can't split
(#112) may be either product, so they have no exact cost: they are left out of `units_costed` and
cost of sales, `cost_of_sales_complete` is false, and `units_estimated` and
`estimated_cost_of_sales` report an estimate apart (the midpoint of the two products' unit costs,
when both are known). The estimate is in no profit figure (#172 item 5). The stock they took is
`unattributed_stock`, a reconciliation line, also in no profit figure, so the books still balance,
except for timing: a unit the counters split to the old product at a swap leaves stock at the swap but is booked to the poll that measured it, so a period that ends between the two (usually a day) is off by that unit's cost, which the period containing the poll makes up; a period that holds both balances.
While a cost is missing, `gross_margin` and `net` are over known costs only, and the summary says
"net at known costs (incomplete...)". Cost of sales is never "unknown" as a whole: a quiet week with
a $25 visit and 40 cents of tokens nets -25.40. **Shrinkage** is the cost of units lost without a sale: later
losses, units still counted in a slot when its product changed, and a purchase line that lost
every unit. It is its own line, taken out of net, so a lost unit's cost never silently disappears.

### `preview_desired_state`: the planner on live data

`vending/planning.py` gathers what `plan_restock` needs and `vending/tools/preview.py` turns the
plan into the reply. Slots come from the receipts (`vending.stock`: product, units, unit cost) and
the latest good poll (price); the closet from the receipts; pending requests from
`purchase_requests`; the catalog from our copy in `catalog_products`, the same rows
`get_catalog` reads, so a bridge outage doesn't stop planning; the spendable balance from the
ledger; and the planner's settings from `config`.

`vending/capacity.py` uses the package size and any SKU override to find [column capacity](capacity.md).
Preview warns when a proposed load leaves space, for example `fills 6 of 8: 2 spaces empty`.
If the planner cannot establish capacity for a load, it returns `unknown_capacity` with instructions
to record a count.

The other keys the planner reads are `price_ceiling_cents` (650), `tax_percent` (8) and
`midweek_fee_cents` (2500). **Only the keys in `KNOWN` can be set**, and each value is checked for
type and range before it is stored (`KNOWN` in `vending/config.py`): a fee under a dollar is
refused as dollars typed by mistake, a misspelt key is refused rather than saved silently, and the
agreed limits are maxima: `price_ceiling_cents` at most 650, `wildcard_ceiling_cents` at most 800 and
`wildcard_purchase_cap_cents` at most 8200 (lower is allowed). A row hand-edited above one stops
planning until a person sets it again. `wildcard_slot` must be one of ours in the stored slot
assignment, checked again even when the same value is set twice. Until the ledger is open, the
tool answers `success: false` with the command to run. Delisted products count as not in the
catalog, and so do products the bridge marks inactive. A copy last synced over 36 hours ago (the
same limit as `get_catalog`) is still used, with a `STALE` note saying to run
`python -m vending.sync_catalog`. Only when we hold no listed product (the sync has never run) is
the catalog read live from the bridge (`GET /api/v1/products`), with a note saying so. In that
case an empty or reshaped reply also answers `success: false` (it would otherwise read as "every
product unapproved"), and the halt switch is checked before the bridge is read, so halted with the
bridge down says to stop, not to check Tailscale. A poll older
than 3 hours adds a `STALE` note, since stock on the rails is overstated by whatever sold since.
A refused plan comes back with every refusal and everything that could still be worked out, so the
agent fixes the whole plan in one turn. In `cost`, `spendable` and `spendable_left_at_max` are the
ledger's spendable balance, the same figure `get_account_balance` calls `spendable`. A product the agent
gives no `sourcing` for is bought the way the team's sourcing table says ([`sourcing.md`](sourcing.md)),
and a table price checked over 7 days ago is named in `warnings`. When the agent gives sourcing
with no `expected_price_per_pack`, the cost borrows the table's expected price for the same store
and pack, and an old borrowed price is named the same way. A slot that needs a purchase with
no sourcing given or in the table is refused `no_sourcing`.

### `submit_desired_state`: the only way to buy

Same input as `preview_desired_state`, plus an optional `expected_plan_hash`: the `plan_hash` the
preview returned. The agent previews, fixes every refusal, then submits the same desired state with
that hash. Inside the write gate (halt lock held) it runs the planner **again** on live data, so the
plan that is sent is the plan that was checked; if the lines it would send, their worst-case cost
or the visit fee changed since the preview (stock, prices, the catalog or the config moved), the
hash differs and it refuses `plan_changed`. A balance that moved but still covers the worst case
doesn't count; the balance gate checks it either way. Without the hash nothing is
compared, and whatever the planner produces now is sent. Every refusal comes at once: the planner's,
`plan_changed`, and `submission_unresolved` (below), which the preview reports too. Then, on one
side connection opened before anything is sent:

1. It commits a `sending` row in `purchase_submissions` (migration 008), with the exact body, the
   directive and the quoted visit fee, **before** the POST. A crash after the POST can't lose the
   fact that we sent something, which matters because the bridge has no route that lists our
   requests.
2. It POSTs `/api/v1/purchase-requests` once, never retried: a retry after a lost answer could order
   twice.
3. It settles the row on that same connection: `accepted` (and the request goes into
   `purchase_requests` in the same transaction, so `python -m vending.sync_requests` reads it back
   from then on), `refused` (a `4xx`; the reply carries the bridge's reason, such as `422
   invalid_lines`), `not_filed` (it never left), or `unknown` (sent, but a timeout, a `5xx` whose
   body is kept in `detail`, or a cut-off answer).

If the bridge accepted the request but storing that fails anyway (the database dropped between the
POST and the settle), the answer is written to the server's stderr as a `LOST ANSWER` line and the
error the agent sees names the request ID and the `resolve` command, so the ID is never only in
memory. The row stays `sending`.

While any row is `sending` or `unknown`, the request may or may not exist on the bridge, so both
tools refuse (`submission_unresolved`) until a person settles it. With the request ID (from the
`LOST ANSWER` line or the row's `response`), the bridge's Swagger page has
`GET /api/v1/purchase-requests/{id}`. **Without it, ask the instructors first**: a resend of a
request that was filed would order twice. Then run one of:

```
python -m vending.submissions list
python -m vending.submissions resolve 7 --request-id pr_abc123 --by kevin   # it was filed
python -m vending.submissions resolve 7 --not-filed --by kevin              # it never was
```

Resolving with a request ID also records it in `purchase_requests`. Both paths carry the midweek
visit fee the plan quoted for an off-cycle request (`visit_fee_cents`, saved on the `sending` row
before the POST), so the ledger charges what was agreed then even if `config.midweek_fee_cents`
changes later. Every change is logged with the `--by` name.

`resolve` takes the same lock as every write, so while a submit is in flight it waits (and says
so) until that submit has settled its row; a person's answer and the bridge's can't overwrite each
other. The settle itself only ever changes a row that is still `sending`.

The reply to an accepted submit carries the request ID, the plan hash, the number of lines, the max
cost and the directive the worker will read, plus the plan's `warnings` and `notes`, the same as
the preview's.

## Adding a tool's logic

1. Give it a module in `vending/tools/` with a function that takes a connection and returns a
   `Reply` subclass. See `machine_state.py`.
2. In `vending/server.py`, point its registration at that function. Keep the docstring short,
   because every tool description is sent, and paid for, on every model request.
3. A write tool returns an action for `_write`. It raises `Refused` with every refusal at once,
   using `vending.pricing.Refusal` and the codes in the tool list. If its reply is a `WriteReply`
   subclass, pass that class to `_write` so a refusal comes back in the same type.
4. Add tests on fixture polls. `tests/conftest.py` gives each test a throwaway schema, and
   `store_poll` stores a synthetic poll through the history sync's own `store`.

## Tests

```
pytest
```

The database tests skip when `DATABASE_URL` isn't set or the database is down; one 3-second probe
decides for the whole run, so a dead database doesn't hang the suite. Start it with
`docker compose up -d db` so they run.

`tests/test_catalog_request.py` runs `request_catalog_product` against a local HTTP server
standing in for the bridge, so no test files a real request.
