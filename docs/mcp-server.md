# The MCP server

The agent's 11 tools, served over stdio by `python -m vending.server`. It's plain Python on the
official MCP SDK, in the same package as the history sync, so the tools import the parser, the
pricing gate and the database layer directly. The read tools only read our database, except `get_project_rules`, which reads our notes in `docs/agent/`; the one
built write tool, `request_catalog_product`, also calls the DEX bridge. Nothing here polls the
machine.

| Tool | State |
| --- | --- |
| `get_machine_state` | **Works.** Per slot: price, lifetime units, units sold and units/day over the last 7 days, from stored polls; and from the receipts (`vending.stock`), the product, stock on hand (since the product went in), units sold and days since the last load, and days of stock left. The closet per product with its value at cost, and any stock warnings. A slot with no receipts has those fields `null` and is named in the message. Where receipts don't know a slot, or a person's count is newer than its last receipt, `counted_product` and `counted_on_hand` say what the latest count in `slot_counts` saw (units less sales since; an estimate for a photo), with `counted_at` and `count_source`. Sets `stale: true` and says so when the latest poll is over 3 hours old. |
| `get_sales_history` | **Works.** Per slot over any window: units, revenue (includes the 8% tax) and revenue ex-tax, plus totals. Says which two polls the window actually spans. |
| `get_account_balance` | **Works.** The balance from the ledger (opening and later capital, plus revenue, minus receipts, midweek visit fees, token spend and sales tax remitted, plus or minus adjustments), the sales tax set aside and still owed, and the spendable balance after it. Fails with the command to run until a person has recorded the opening capital. |
| `get_pnl` | **Works.** Over any window of Eastern calendar days: revenue ex tax, units sold and how many of them could be costed, cost of sales at weighted-average cost, shrinkage, visit fees, token spend, adjustments, net, purchases, and inventory at cost in the machine and the closet. Capital and tax remittances are shown but are not in net. |
| `preview_desired_state` | **Works.** Runs the restock planner on live data: the request lines, the worker's directive, cost and balance left, each slot's price floor and how it was worked out, warnings, and every refusal at once. `success` is whether the plan could be submitted as it stands. Says what a person must do first when the ledger isn't open, the catalog can't be read, or `capacity_overrides` in config is invalid. |
| `get_purchase_requests` | **Works.** Our requests, newest first (or only pending ones): status, decision note, the plan we sent, and each line's receipt with later losses. Reads only our database, so it's as fresh as the last `python -m vending.sync_requests`; pending requests that sync hasn't read in 6 hours (counted from submission if it has never read them) are listed in `not_checked_recently` and named in the message. |
| `get_catalog` | **Works, version 1.** The single-item products the bridge lists (sku, name, line, flavor, size), searchable by text, with when the catalog was last synced. Says so when that was over 36 hours ago. Our last unit cost, sourcing rows and pending catalog requests are not in it yet. |
| `get_project_rules` | **Works.** Our own notes on the project brief, from [`agent/`](agent/README.md). With no topic, the list of topics and their one-line summaries; with a topic, that file's text. A topic is a plain name from the list: paths, `..` and unknown names are refused with the list. Logged like any read. |
| `request_catalog_product` | **Works.** Files a request to add a drink to the catalog (`POST /api/v1/product-requests`), recorded in `product_requests`. See below for what it refuses. |
| `submit_desired_state` | **Works.** Plans the desired state again on live data inside the write gate, refuses with every problem at once, otherwise POSTs it once and records the request. Saves what it is about to send before sending, so a lost answer leaves a row a person settles with `python -m vending.submissions resolve`. |
| `set_price` | **Dropped.** Prices change only at a worker visit (free Friday, $25 midweek), through `submit_desired_state`. The stub stays so a call gets that answer. |

## Run it

You need the local database from the history sync first ([`README.md`](../README.md), and
`DATABASE_URL` in your `.env`):

```
pip install -r requirements-dev.txt
docker compose up -d db
python -m vending.db migrate            # creates or updates the tables
python -m vending.sync_history          # needs the class tailnet; loads real polls
python -m vending.server                # waits silently on stdin: that's correct
```

The server never migrates. If the database is missing a migration, it refuses to start and names
the missing ones; check that `DATABASE_URL` points where you mean (in production, our Azure
Container App, it's the shared Azure database), then run `python -m vending.db migrate`. Stop the server with Ctrl+C.

**For the live run, set `VENDING_TOOLS=built` in the real environment** (it is not read from `.env`). Then only working tools are registered, so the
agent doesn't pay tokens on every request to describe stubs. The default, `all`, registers all
11 so the team can see the whole contract in a client.

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
The limit counts every request filed in the last 7 days, with a higher limit while the bridge
catalog is small, so the first big ask can happen without anyone flipping a switch. **The numbers
are placeholders the team still has to set:** for now 3 per week normally, 15 while the catalog has
fewer than 25 products (`WEEKLY_LIMIT`, `STARTUP_WEEKLY_LIMIT` and `SMALL_CATALOG` in
`vending/tools/catalog_request.py`). A request is a duplicate of an
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
twice. Each change is logged in `action_log` with the `--by` name. Nothing updates `pending` rows
yet. A later sync can read `GET /api/v1/product-requests/{id}`.

### `get_account_balance` and `get_pnl`: the ledger

Both read `vending/ledger.py`. **The balance is computed, never stored:**

```
opening capital + later capital + takings - receipts - midweek visit fees - token spend
- tax remitted +/- adjustments
```

Takings are the rise in the polls' lifetime sales counters for our 20 slots since the opening
entry, tax included; `vending/counters.py` decides what a reading that goes down means (one low
poll between good ones is a misread, two in a row a counter that restarted). Receipts are
`purchase_lines.total_paid` on fulfilled requests, timed by each line's `resolved_at`, the same
clock as the stock they delivered. Every fulfilled off-cycle request costs the midweek visit fee
stored on it when it was submitted (`purchase_requests.visit_fee_cents`), or `config`
`midweek_fee_cents` ($25.00 by default) for requests without one, so a later change to the config
fee never rewrites past visits. The sales tax inside the takings (takings x 8/108, per week,
rounded half up) is set aside; what is still **owed** is the set-aside less what has been
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

`get_pnl` takes its window as Eastern calendar days, inclusive, so a Friday evening sale lands in
Friday. `units_sold` counts every rise in our 20 slots' unit counters; cost of sales covers the
units the receipts can cost (weighted average, `vending.stock`), and `units_costed`,
`cost_of_sales_complete: false` and a note say how many could not be (sold before we first loaded
the slot, or past what we loaded). Cost of sales is never "unknown": a quiet week with a $25 visit
and 40 cents of tokens nets -25.40. **Shrinkage** is the cost of units lost without a sale: later
losses, units still counted in a slot when its product changed, and a purchase line that lost
every unit. It is its own line, taken out of net, so a lost unit's cost never silently disappears.

### `preview_desired_state`: the planner on live data

`vending/planning.py` gathers what `plan_restock` needs and `vending/tools/preview.py` turns the
plan into the reply. Slots come from the receipts (`vending.stock`: product, units, unit cost) and
the latest good poll (price); the closet from the receipts; pending requests from
`purchase_requests`; the catalog live from the bridge (`GET /api/v1/products`, until the catalog
sync lands and it can come from our table); the spendable balance from the ledger; and the
planner's settings from `config`.

**How many units fit in a column depends on the package**, not the slot: each column is a row of
packages in front of a pusher. `vending/capacity.py` matches each product's catalog `size` against
the machine's Package Loading chart ([photographed 9 Nov](images/package-loading-chart-2026-11-09.jpg)): 20 oz bottle 7, 16 oz bottle 7 (the chart
says glass 7, PET 8, and the catalog doesn't say which), 14 oz bottle 8, 12 oz can 8, 240/250 ml can
11. A package the chart doesn't list (a 16 oz can, a 16.9 oz bottle, a carton) is refused as
`unknown_capacity` until a person counts how many fit and records it; a count also wins over the
chart, which calls its own figures approximate:

```
python -m vending.config set capacity_overrides '{"MONSTER-WHITE-16CAN": 6}' --by marshall
python -m vending.config show
```

In Windows PowerShell 5.1 the inner quotes are lost unless escaped:

```
python -m vending.config set capacity_overrides '{\"MONSTER-WHITE-16CAN\": 6}' --by marshall
```

`set` replaces the whole object, so copy the current one from `show` and add to it; `set` names
any count the new value dropped. A count must be 1 to 15 units (the chart's largest figure is 11),
and a SKU with a space in it is refused, since SKUs have none and it would never match. Preview
warns about a count for a SKU that isn't in the catalog, and about one more than 1.5 times the
chart's figure for that package.

The other keys the planner reads are `price_ceiling_cents` (600), `tax_percent` (8) and
`midweek_fee_cents` (2500). **Only those four keys can be set**, and each value is checked for
type and range before it is stored (`KNOWN` in `vending/config.py`): a fee under a dollar is
refused as dollars typed by mistake, a ceiling over the API's $10.00 is refused, and a misspelt key
is refused rather than saved silently. Until the ledger is open, the
tool answers `success: false` with the command to run; so does an empty or reshaped catalog reply
(it would otherwise read as "every product unapproved"), and the halt switch is checked before the
bridge is read, so halted with the bridge down says to stop, not to check Tailscale. A poll older
than 3 hours adds a `STALE` note, since stock on the rails is overstated by whatever sold since.
A refused plan comes back with every refusal and everything that could still be worked out, so the
agent fixes the whole plan in one turn. In `cost`, `spendable` and `spendable_left_at_max` are the
ledger's spendable balance, the same figure `get_account_balance` calls `spendable`. There is no
sourcing table yet: the agent gives `sourcing` for every product it buys, and a slot that needs a
purchase without it is refused `no_sourcing`.

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
