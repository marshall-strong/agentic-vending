# The MCP server

The agent's 10 tools, served over stdio by `python -m vending.server`. It's plain Python on the
official MCP SDK, in the same package as the history sync, so the tools import the parser, the
pricing gate and the database layer directly. The tools only read our database. Nothing here
polls the machine.

| Tool | State |
| --- | --- |
| `get_machine_state` | **Works.** Per slot: price, lifetime units, units sold and units/day over the last 7 days, from stored polls. Fields that need restock receipts are `null`. Sets `stale: true` and says so when the latest poll is over 3 hours old. |
| `get_sales_history` | **Works.** Per slot over any window: units, revenue (includes the 8% tax) and revenue ex-tax, plus totals. Says which two polls the window actually spans. |
| `get_catalog`, `get_account_balance`, `preview_desired_state`, `get_purchase_requests`, `get_pnl` | Registered stubs: they return a "not built yet" error naming what they wait on |
| `submit_desired_state`, `set_price`, `request_catalog_product` | Write stubs, already behind the write gate: refused while halted, logged either way |

## Run it

You need the local database from the history sync first ([`README.md`](../README.md), and
`DATABASE_URL` in your `.env`):

```
pip install -r requirements.txt
docker compose up -d db
python -m vending.sync_history          # needs the class tailnet; loads real polls
python -m vending.server                # waits silently on stdin: that's correct
```

The server applies any pending migrations when it starts. Stop it with Ctrl+C.

**For the live run, set `VENDING_TOOLS=built` in the real environment** (it is not read from `.env`). Then only working tools are registered, so the
agent doesn't pay tokens on every request to describe stubs. The default, `all`, registers all
10 so the team can see the whole contract in a client.

**The sync needs the class tailnet; the tests don't.** On a laptop also on a personal tailnet,
sign in to the class account once with `tailscale login`, then move between tailnets with
`tailscale switch <tailnet>` (`tailscale switch --list` shows them). Only one tailnet is active
at a time, so your personal machines drop off while you're on the class one. In production the
sync runs on the Azure VM, which stays on the class tailnet.

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
`config` row is missing, it counts as on. Every flip is logged with the `--by` name, which is
required because everyone on the shared VM is the same OS user. If a write is in flight, the
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

## Adding a tool's logic

1. Give it a module in `vending/tools/` with a function that takes a connection and returns a
   `Reply` subclass. See `machine_state.py`.
2. In `vending/server.py`, point its registration at that function. Keep the docstring short,
   because every tool description is sent, and paid for, on every model request.
3. A write tool returns an action for `_write`. It raises `Refused` with every refusal at once,
   using `vending.pricing.Refusal` and the codes in the tool list.
4. Add tests on fixture polls. `tests/conftest.py` gives each test a throwaway schema, and
   `store_poll` stores a synthetic poll through the history sync's own `store`.

## Tests

```
pytest
```

The database tests skip when `DATABASE_URL` isn't set or the database is down. Start it with
`docker compose up -d db` so they run.
