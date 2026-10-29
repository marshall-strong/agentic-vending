# DEX API quickstart

Once you're on Tailscale and have our group key in `.env`, `python scripts/bridge.py health` answers in seconds. This page gets you there, then shows the read-only calls you'll actually use.

## Before you start

**You need four things; a teammate can help with the last one.**

| You need | Check it with | If it's missing |
| --- | --- | --- |
| Tailscale, signed in with the account the tailnet invite went to | `tailscale status` lists `aihub-pi` | The bridge is not on the public internet. Ask a teammate or the course staff for an invite. |
| The repo, cloned | `git clone https://github.com/marshall-strong/agentic-vending.git` | Ask a teammate to add your GitHub account as a collaborator if the clone is refused |
| Python 3.11 or newer | `python --version` | Install from python.org. The script uses only the standard library, so there's nothing to `pip install`. |
| Our group API key | A teammate gives it to you directly | Never paste it in chat, a doc, an issue or a commit |

## Step 1: Reach the bridge

**`/health` needs no key, so it tells you whether the network is right before you touch the key.** With Tailscale connected, open this in a browser:

http://aihub-pi:8000/health

You should see `"status": "ok"`. If the page won't load, it's Tailscale, not the API: run `tailscale status` and look for `aihub-pi`.

## Step 2: Put the key in `.env`

**The script reads its settings from a file named `.env` in the repo's top folder** (`agentic-vending/.env`, next to `README.md`). From that folder:

```
cp .env.example .env
```

Then open `.env` and fill in these two lines:

```
DEX_BASE_URL=http://aihub-pi:8000
DEX_API_KEY=<our group key>
```

Leave the rest blank for now. `.env` is gitignored, so Git won't pick it up; keep it that way. If a key is ever pushed, deleting the commit doesn't remove it: tell the team right away so course staff can replace it.

## Step 3: Make your first calls

**`scripts/bridge.py` wraps every route we're allowed to use, and checks your setup as it goes.** Run these from the repo's top folder, in order:

```
python scripts/bridge.py health
python scripts/bridge.py slots
python scripts/bridge.py latest --summary
```

- `health` should print `"status": "ok"` again, this time through the script.
- `slots` proves the key works: it lists our 20 slots, 15-18, 25-28, 35-38, 45-48 and 55-58.
- `latest --summary` shows each of our slots' current price and lifetime sales from the newest stored poll.

If all three work, you're set up. The rest of the commands:

| Command | What you get |
| --- | --- |
| `history --start 2026-10-20 --end 2026-10-21` | Stored polls in that range: `session_id`, `started_at`, size. Dates are UTC days. |
| `raw SESSION_ID --out poll.dex` | One stored poll as the raw EVA-DTS text the machine sent |
| `latest --out latest.dex` | The newest stored poll, raw |
| `products` | The catalog we can order from: single items with `line`, `flavor` and `size`, no prices or pack sizes (three products on 29 Oct, one a test entry) |
| `request REQUEST_ID` | Where one of our purchase requests has got to |

`python scripts/bridge.py --help` lists them all. [`dex-bridge.md`](dex-bridge.md) explains each one in more detail.

## Other ways in

**The Swagger page is the best way to see what each route takes and returns.** Open http://aihub-pi:8000/docs (Tailscale on), click **Authorize**, paste the key into `X-API-Key`, then use **Try it out** on any `GET` route.

**From PowerShell**, read the key out of `.env` and call any `GET` route. Each line is one statement:

```
$key = (Get-Content .env | Where-Object { $_ -like 'DEX_API_KEY=*' }) -replace '^DEX_API_KEY=', ''
Invoke-RestMethod -Uri 'http://aihub-pi:8000/api/v1/slots' -Headers @{ 'X-API-Key' = $key }
Invoke-RestMethod -Uri 'http://aihub-pi:8000/api/v1/history?start=2026-10-21&end=2026-10-21' -Headers @{ 'X-API-Key' = $key }
```

**Skip the course Postman collection.** It dates from September and only has `/health`, the live poll and an old parse route; the history and purchasing routes aren't in it. If you prefer Postman anyway, import the routes from http://aihub-pi:8000/openapi.json, and keep any Postman environment file out of the repo: it holds the key in plain text.

## Rules

- **Read history, not the machine.** The bridge stores every poll anyone makes, back to 28 August, and the course staff's poller reads the machine every hour. A live poll (`POST /api/v1/poll`) ties up a machine that's selling drinks for about 9 seconds, so the script refuses to do one without `--live`. You'll almost never need it.
- **Never submit a purchase request.** `POST /api/v1/purchase-requests` spends real money from our $600. Nothing in the script can do it, on purpose; leave that `POST` alone in Swagger too.
- **Don't submit a product request on your own either.** `POST /api/v1/product-requests` asks the course staff to add a drink to the catalog. It costs nothing, but each one is a team decision, and too many open requests get a `429`.
- **The version number won't tell you the API changed.** `/health` still says `0.1.0` after several changes. Check the Swagger page and [`dex-bridge.md`](dex-bridge.md), which notes the date it was last checked against the live spec.
- **History comes in UTC days, at most 500 polls per request.** A longer range comes back with `truncated: true` and silently misses the rest, so ask a few days at a time.
- **The key stays in `.env`.** It never goes in code, commits, docs, chat or screenshots. The repo is public.

## Troubleshooting

| You see | It means | Do this |
| --- | --- | --- |
| "Python was not found; run without arguments to install from the Microsoft Store" | Windows is running its Store placeholder, not a real Python | Install Python from python.org (or `winget install --id Python.Python.3.13 -e`), turn off the `python.exe` and `python3.exe` App execution aliases in Settings, and open a new terminal. If you use uv, `uv run scripts/bridge.py health` works without either. |
| "Could not reach ... Is Tailscale connected?" | The network can't see the Pi | `tailscale status`; reconnect, and check you're signed in to the right account |
| "`DEX_BASE_URL` is not set" | No `.env`, or it's in the wrong folder | Put `.env` in the repo's top folder, next to `README.md` |
| `401` or `403` | The bridge rejected the key | Recopy `DEX_API_KEY` exactly: no quotes, no spaces |
| `409` | Someone else is polling the machine live | Wait a minute, or use `latest` instead |
| `429` | Too many requests too fast | Slow down |
| `"truncated": true` in history | More than 500 polls in your range | Ask for fewer days at a time |

## Reading a poll

**A raw poll is about 210 lines of EVA-DTS, one record per line, fields split by `*`.** Most of it you can ignore. These are the records we use:

| Record | Example | Meaning |
| --- | --- | --- |
| `ID4` | `ID4*2*1840*5` | The first field is the number of decimal places in every money figure: 2, so `450` is $4.50 |
| `PA1` | `PA1*15*450*` | Selection 15 is priced at $4.50 |
| `PA2` | `PA2*240*...` | The line after a `PA1`: that selection's lifetime units sold, then lifetime sales |
| `VA1` | `VA1*...` | Machine-wide lifetime totals; all the `PA2` lines add up to it exactly |
| `G85` | `G85*5CDB` | A checksum of the document |

Two things the machine doesn't tell us:

- **Stock on hand.** We work it out as units loaded, from purchase-request receipts, minus units sold.
- **Sales for a period.** Every counter is a lifetime total, so a week's sales are the difference between two polls.

**Our slots are columns 5-8 of every row:** 15-18, 25-28, 35-38, 45-48 and 55-58. Columns 1-4 belong to group A.
