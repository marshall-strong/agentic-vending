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
  someone visits.

The detailed build plan lives in the team's shared folder in Teams, not in this repo.

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

### 4. Check that you can reach the machine

```
curl http://aihub-pi:8000/health
```

A `200` response means you are set. If the name will not resolve at all, Tailscale is not
connected, and that is a network problem rather than an API one.

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

## Current state

`main` has the repository's setup: `.gitignore`, `.env.example` and `.gitattributes`.
The snapshot archiver, which records the machine's readings over time, is on its way in a
separate pull request. Until it lands, there is nothing here to run.
