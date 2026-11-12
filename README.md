# agentic-vending

An AI agent that runs part of a real vending machine as a business.

Golisano Institute, Applied AI (AI Implementation, AI-R01-12627), Fall 2026.
Meaghan Gartland, Sean Kelley, Kevin Sykes and Marshall Strong. Group B.

## What we are building

There is a Royal Vendors RVV NG drink machine in the building. It has 40 selections:
5 rows, with 8 columns in each row. Our team is assigned some of those slots, and
another team is assigned others. We do not get to know which slots are theirs.

We start with **$600** of institute credit, once. From that we buy drinks, set prices,
and ask for the machine to be restocked. Everything the machine sells credits the same
account. **This quarter we build and test. Next quarter the agent runs live for a
quarter** and the team whose slots make the most profit by week 9 wins.

Profit is:

```
revenue from units sold
  - cost of the drinks we bought
  - $25 for each midweek service visit
  - what it costs to run the agent itself (AI tokens)
```

**That last line is the one people forget.** Every time the agent asks the AI model to
think, the tokens are charged to the same $600 that buys drinks. A dollar spent on
tokens is a dollar not spent on stock. That is why this project keeps arithmetic in
plain Python, calls the model only where real judgment is needed, and logs token usage
from day one.

## How the pieces fit together

```
 vending machine
      |  serial cable
 Raspberry Pi "DEX Bridge"  (aihub-pi, reachable over Tailscale only)
      |  HTTP
 our sync job  ->  database  ->  agent tools  ->  the agent
                                                      |
                                              purchase order
                                                      |
                                        a student worker buys and restocks
```

- **The machine keeps its own books.** Every sale, on every selection, since its control
  board was installed. It publishes them in an industry format called EVA-DTS.
- **The Pi reads the machine and hands us that report unchanged.** It also keeps a
  history of every read, so we can fetch past readings without disturbing the machine.
- **We turn those readings into data**, work out what sold, and decide what to charge and
  what to buy.
- **We cannot buy anything directly.** The agent submits a purchase order by Thursday
  midnight. On Friday a student worker buys the drinks, loads the machine, changes the
  price tags, and reports back what actually happened. They can substitute an item or
  refuse an order, so every order carries a backup choice.
- **Friday's visit is free. Any midweek visit costs $25**, whether it is a restock, a
  product swap, or a price change. Prices are physical tags, so they only change when
  someone visits. **There are no remote price changes:** the worker changes them by hand,
  on the free Friday visit or a $25 midweek one, so the agent sets prices in its order.

## Planning documents

| File | What it is |
|---|---|
| [`docs/planning/tool-list.md`](docs/planning/tool-list.md) | **Start here.** The agent's proposed tools, what the bridge API actually offers, and the open questions. A snapshot of the [live Claude doc](https://claude.ai/code/artifact/82ae22ab-53a2-48c0-b4d5-c636efcc877d), where edits and comments go |
| [`docs/artifacts.md`](docs/artifacts.md) | **The team's main live project pages in one place:** the roadmap, the team summary, the sales and data labs, and the agent walkthroughs |
| [`docs/planning/build-plan.md`](docs/planning/build-plan.md) | The 21 September build plan: architecture, guardrails, order of work |
| [`docs/planning/2026-10-19-david-api-qa.md`](docs/planning/2026-10-19-david-api-qa.md) | What David answered on 19 October about the API he is still building. These answers outrank the live spec |
| [`docs/planning/2026-09-21-design-brief-meeting.md`](docs/planning/2026-09-21-design-brief-meeting.md) | What Graham and David answered in the Design Brief meeting, and what they still owe us |
| [`docs/dex-api-quickstart.md`](docs/dex-api-quickstart.md) | **New to the API?** From Tailscale and a key to your first calls, plus troubleshooting |
| [`docs/dex-bridge.md`](docs/dex-bridge.md) | How to talk to the Pi and its API with `scripts/bridge.py` |
| [`docs/mcp-server.md`](docs/mcp-server.md) | The MCP server: run it, connect Claude Desktop or the Inspector, the halt switch and the write gate |
| [`docs/db-explorer.md`](docs/db-explorer.md) | The DB explorer: a read-only page that lists every table and shows its columns and rows, so you don't need psql |
| [`docs/week-replayer.md`](docs/week-replayer.md) | The week replayer: score a decision-maker on past weeks, its hard checks, test cases and demand assumptions |
| [`docs/pnl-report.md`](docs/pnl-report.md) | The weekly P&L report: a printable page of profit and loss by week, from the ledger |
| [`docs/integrity.md`](docs/integrity.md) | The team integrity rule, why our pricing ignores the other team's prices for now, and the guardrails |
| [`docs/assumptions.md`](docs/assumptions.md) | The assumptions register: what our code and scores rely on that we haven't confirmed |
| [`docs/dashboard.md`](docs/dashboard.md) | The read-only dashboard for watching the agent: status, plan and what changed, activity, requests |

## Getting set up

You need: **Python 3.11 or newer**, **Git**, and **Tailscale**.

### 1. Tailscale

The Pi is not on the public internet. Tailscale is the only way to reach it, on campus or
off. Install it, sign in with the account your invitation was sent to, and check:

```
tailscale status
```

You should see `aihub-pi` in the list. Nothing else here works until you do.

### 2. Clone the repository

```
git clone https://github.com/marshall-strong/agentic-vending.git
cd agentic-vending
```

### 3. Create your `.env`

`.env.example` lists every setting the project needs, with the values left blank. Copy it
and fill yours in:

```
cp .env.example .env
```

**Where the values come from:** the DEX Bridge URL and our group API key were emailed to
the team by course staff, and they are also in the Postman environment file they sent.
Ask a teammate if you do not have them.

**`.env` is gitignored and must stay that way.** So are the Postman environment exports,
because they contain the same key in plain text. Two things worth knowing:

- GitHub can automatically block well-known secrets such as AWS or Anthropic keys, but it
  has no idea what our DEX key looks like. **The `.gitignore` is the real protection.**
- If a key ever does get pushed, editing or amending the commit does **not** remove it.
  The fix is to tell course staff so the key can be replaced. Say something immediately;
  it is a small problem caught early and a large one caught late.

### The Anthropic API key is for the agent only

**Building the project never needs it.** Writing code, running tests and using Claude Code
all run on your own Claude subscription.

**The team's Anthropic API key pays only for the agent's own model calls:** test runs,
model comparisons and the live run. Kevin keeps it in his Postman Vault; ask him for it,
and only set it up if you run those. Kevin's understanding (4 Nov) is that this key is free
for building and testing, and that the $600 budget applies only once the competition starts
next quarter. Even so, every run that uses it has a dollar cap and is logged.

**Keep it in a user environment variable named `VENDING_ANTHROPIC_API_KEY`:**

- **Not in `.env`.** That file belongs to one copy of the repository, so every worktree
  would need its own copy of the key. And if your copy of the repository is inside OneDrive
  or iCloud, the file gets synced to the cloud.
- **Never under the name `ANTHROPIC_API_KEY`.** When that variable is set, Claude Code
  uses it *instead of your subscription*, even while you're logged in
  ([Claude Code docs](https://code.claude.com/docs/en/env-vars)). The team key would then pay
  for everyone's development work. The agent's code reads `VENDING_ANTHROPIC_API_KEY` and
  passes it to the Anthropic client itself.

Set it once, then open a new terminal. On Windows, run this in PowerShell and paste the key
when it asks. It's read as a prompt, so the key doesn't end up in your command history:

```
[Environment]::SetEnvironmentVariable('VENDING_ANTHROPIC_API_KEY', (Read-Host 'Key'), 'User')
```

On macOS or Linux, add `export VENDING_ANTHROPIC_API_KEY='<the key>'` to `~/.zshrc` or
`~/.bashrc`. In production it's a secret on our Azure Container App.

**Every use of the key is recorded.** All our model calls go through `vending/harness.py`, which
writes each one to the `model_calls` table before it is sent: what asked for it, the model and
effort, which key paid, tokens, cost and time. Live calls also go in the ledger, because their
cost comes out of the $600; test runs, replays and sweeps don't, but they're in `model_calls` all
the same. See it all with:

```
python -m vending.usage
```

Test calls stop at a daily cap shared by the whole team (`model_test_daily_cap_cents`, $20 by
default); live calls have their own (`model_daily_cap_cents`). Anthropic's own record is the
Claude Console's Usage and Cost pages for the key's workspace.

### 4. Check that you can reach the machine

```
python scripts/bridge.py health
```

`"status": "ok"` means you are set. If it says it could not reach the bridge, Tailscale is
not connected, and that is a network problem rather than an API one. Then try
`python scripts/bridge.py latest --summary` to see our slots' prices and lifetime sales.
[`docs/dex-bridge.md`](docs/dex-bridge.md) covers every command, and [`docs/dex-api-quickstart.md`](docs/dex-api-quickstart.md) walks through setup step by step.

### 5. Set up the database and sync the machine's history

You also need **Docker Desktop**. The database is Postgres 17, the same version as our
Azure database; locally it runs in Docker. Set `VENDING_DB_PASSWORD` (any password) and
`DATABASE_URL` in `.env` as `.env.example` shows, then:

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
docker compose up -d db
python -m vending.db migrate
python -m vending.sync_history --since 2026-08-28
```

`migrate` creates or updates the tables. The sync then copies every stored poll since
28 August into `polls` and `poll_selections` (about 830 polls on 22 October, two minutes).
Run it again any time to pick up new polls; it only fetches what it doesn't have. It never
touches the machine itself: it reads the bridge's stored history. Neither the sync nor the
server migrates on its own: on a database that is behind, each refuses to run and names
`migrate`, so nothing started from a branch can change the shared Azure database's schema.
Run the tests with `pytest`.

Before the live run starts, a person records the opening capital once with
`python -m vending.ledger open --amount 600.00 --by NAME`; until then `get_account_balance`
refuses and names that command. See "the ledger" in `docs/mcp-server.md`.

`python -m vending.sync_requests` does the same for our purchase requests: it reads back
every request still pending, and every one fulfilled in the last 30 days, and stores the
receipts the stock counts come from. It only reads; it never submits a request.

## Deploying

The agent runs in an Azure Container App; `docs/deploy.md` has the one-time setup, the deploy
script (`scripts/deploy_container_app.ps1`), how to check `/health` and the logs, and how to roll
back. Nothing in the image migrates the database: a person runs `python -m vending.db migrate`.

## Working on this repo

- **Everything goes through a branch and a pull request.** Nobody pushes to `main`.
- **Commit messages explain why, not what.** The diff already shows what changed.
- **Line endings are handled for you** by `.gitattributes`, so you do not need to change
  any Git settings. Text is stored with LF, Python and shell scripts stay LF, PowerShell
  scripts stay CRLF, and raw machine snapshots are treated as binary so their checksums
  survive.
- **Your commits should be linked to your GitHub account.** If a commit shows a generic
  avatar instead of your picture, your Git email is not one GitHub recognises. Fix it
  with your GitHub noreply address, which you can find at
  <https://github.com/settings/emails>:

```
git config --global user.email "12345678+yourusername@users.noreply.github.com"
```

## Rules the code has to respect

These are not style preferences. Breaking one is a failed agent, no matter how good its
decisions are.

- **Never spend more than the account holds.** Checked in code, at the point the spending
  happens, not in a prompt.
- **Never price outside the floor and ceiling we set.** Same rule, same place.
- **Prices must end in 0 or 5.** $2.00 and $2.05 are legal, $1.63 is not.
- **Every real action is logged** where a non-technical reviewer can read it afterwards.
- **There must be a halt switch** that works without the agent's cooperation.
- **Never poll the machine in a loop.** A read is about 8.5 seconds of live conversation
  over a slow serial line, results are shared across the whole class for 30 seconds, and
  **the machine is in service selling drinks to real people.** Read the stored history
  instead wherever you can; it is free and does not touch the machine.

## Words you will see

| Term | What it means |
|---|---|
| **DEX / EVA-DTS** | The vending industry's format for a machine's own sales report. Plain text, one record per line |
| **DEX Bridge** | The Raspberry Pi that reads the machine and serves the report over HTTP |
| **Slot / selection** | One numbered spot in the machine. `23` means row 2, column 3 |
| **Lifetime counters** | Totals since the control board was installed. They cannot be reset, so we trust them |
| **PO (purchase order)** | What the agent submits to ask for drinks to be bought and loaded |
| **Service visit** | When the worker restocks, swaps products, or changes prices. Free on Friday, $25 midweek |

## Snapshot archive

The machine reports lifetime counters as they stand right now -- there is no
"sales this week" endpoint. Period sales only exist as the difference between
two stored reads.

`scripts/poll_snapshot.py` polls the DEX Bridge and archives the raw response.
Start here:

```
python scripts\poll_snapshot.py --dry-run
```

That checks Tailscale and the bridge without reading the machine. Full setup,
the scheduling, and the reasoning behind the polling cadence are in
[`docs/snapshot-archive.md`](docs/snapshot-archive.md).

Note: the bridge now stores every poll from anyone, and David's own poller reads the
machine hourly (`GET /api/v1/history`). The plan is to replace this live poller with a
history sync; until then, this is what runs.
