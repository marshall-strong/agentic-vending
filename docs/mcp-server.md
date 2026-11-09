# The MCP server

The agent's 10 tools, served over stdio by `python -m vending.server`. It's plain Python on the
official MCP SDK, in the same package as the history sync, so the tools import the parser, the
pricing gate and the database layer directly. The read tools only read our database; the one
built write tool, `request_catalog_product`, also calls the DEX bridge. Nothing here polls the
machine.

| Tool | State |
| --- | --- |
| `get_machine_state` | **Works.** Per slot: price, lifetime units, units sold and units/day over the last 7 days, from stored polls. Fields that need restock receipts are `null`. Sets `stale: true` and says so when the latest poll is over 3 hours old. |
| `get_sales_history` | **Works.** Per slot over any window: units, revenue (includes the 8% tax) and revenue ex-tax, plus totals. Says which two polls the window actually spans. |
| `get_purchase_requests` | **Works.** Our requests, newest first (or only pending ones): status, decision note, the plan we sent, and each line's receipt with later losses. Reads only our database, so it's as fresh as the last `python -m vending.sync_requests`; pending requests that sync hasn't read in 6 hours (counted from submission if it has never read them) are listed in `not_checked_recently` and named in the message. |
| `get_catalog`, `get_account_balance`, `preview_desired_state`, `get_pnl` | Registered stubs: they return a "not built yet" error naming what they wait on |
| `request_catalog_product` | **Works.** Files a request to add a drink to the catalog (`POST /api/v1/product-requests`), recorded in `product_requests`. See below for what it refuses. |
| `submit_desired_state`, `set_price` | Write stubs, already behind the write gate: refused while halted, logged either way |

## Run it

You need the local database from the history sync first ([`README.md`](../README.md), and
`DATABASE_URL` in your `.env`):

```
pip install -r requirements.txt
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
10 so the team can see the whole contract in a client.

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
