# Talking to the DEX Bridge

The DEX Bridge is the Raspberry Pi (`aihub-pi`) wired to the vending machine. It reads
the machine's sales report, stores every read, and runs the purchasing API. The quickest
way to use it is `scripts/bridge.py`, which wraps every route we are allowed to call.

The full, live API reference is the bridge's own Swagger page:
<http://aihub-pi:8000/docs> (Tailscale only). This page covers what we have learned using it.

## Before you start

You need the three things from the setup section of the [`README.md`](../README.md):
Tailscale connected, the repo cloned, and a `.env` with `DEX_BASE_URL` and `DEX_API_KEY`.
Then check:

```
python scripts/bridge.py health
```

`"status": "ok"` means you are set. "Could not reach ... Is Tailscale connected?" means
the tailnet is down, not the bridge.

## Commands

| Command | Route | What you get |
|---|---|---|
| `health` | `GET /health` | Bridge status. The only route that needs no key |
| `slots` | `GET /api/v1/slots` | Our 20 selections on `vmc1`: 15-18, 25-28, 35-38, 45-48, 55-58 |
| `products` | `GET /api/v1/products` | What we can order: `sku`, `name`, `pack_size`. No prices. Empty as of 15 Oct |
| `history [--start D] [--end D]` | `GET /api/v1/history` | Stored polls, oldest first: `session_id`, `started_at`, `bytes` |
| `raw SESSION_ID [--out FILE]` | `GET /api/v1/history/{id}/raw` | One stored poll, as raw EVA-DTS text |
| `latest [--summary]` | history + raw | The newest stored poll. `--summary` prints price and lifetime sales for our slots |
| `request REQUEST_ID` | `GET /api/v1/purchase-requests/{id}` | `pending`, `rejected` with a `decision_note`, or the full receipts |
| `poll --live [--out FILE]` | `POST /api/v1/poll` | Reads the machine right now. Refuses without `--live` |

Examples:

```
python scripts/bridge.py latest --summary
python scripts/bridge.py history --start 2026-10-01 --end 2026-10-07
python scripts/bridge.py latest --out data/latest.dex
```

## Rules of the road

- **Read history, not the machine.** The bridge stores every poll anyone makes, back to
  28 August, and David's poller reads the machine every hour on the hour. Reading
  history costs nothing and never touches the machine. A live poll is about 8.5 seconds
  of serial conversation with a machine that is selling drinks to real people, so
  `poll` only runs with `--live`, and you should rarely need it.
- **History dates are UTC days.** `--start 2026-10-15` starts at 8 pm on 14 October in
  Rochester (EDT). Both ends are inclusive.
- **The script cannot submit a purchase request, on purpose.** Submitting spends real
  money. The agent's `submit_purchase_request` tool will be the only way to buy, because
  that is where the balance, price-bound and halt checks live. Use Postman for one-off
  experiments only if the whole team agrees first.
- **Never commit `.env` or a Postman environment export.** Both hold our key in plain
  text, and the repository is public.

## Reading a raw poll

A poll is an EVA-DTS document: about 210 lines, one record per line, fields separated by
`*`. The records we use most:

| Record | Example | Meaning |
|---|---|---|
| `ID4` | `ID4*2*1840*5` | Field 1 is the number of decimal places in every money figure (2, so `450` = $4.50) |
| `PA1` | `PA1*15*450*` | Selection 15 is priced at $4.50 |
| `PA2` | `PA2*231*85045*23*10350*0*0` | The selection above it: 231 units sold for $850.45 since the board was installed |
| `VA1` | `VA1*...` | Machine-wide lifetime totals; the `PA2` lines sum to it exactly |
| `G85` | `G85*....` | CRC-16 checksum of the document |

What the machine does **not** tell us:

- **Stock on hand.** `PA1` stops after the price; the stock-level field (`PA1-08`) is
  empty. Stock has to be worked out as units loaded (from purchase-request receipts)
  minus units sold (from polls).
- **Sales over a period.** Every counter is a lifetime total. Sales for a week are the
  difference between two polls.

`latest --summary` is a quick look only: it does not check the `G85` checksum. The real
parser belongs in the history sync.

## Purchase requests, in short

A request has one line per slot. A purchase line asks for `packs` (1-20 whole store packs)
of a catalog `sku`, with an optional alternate. A back-stock line loads `units` from our
closet stock. `change_product` must be `true` exactly when the line puts a different
product in the slot. The request stays `pending` until the worker has handled every line,
then each line gets a receipt with `total_paid`, `units_loaded`, `units_to_back_stock`,
`units_pulled` and more. There is no route that lists our requests, so save every ID.

Details and the open questions about prices, midweek visits and catalog requests are in
[`docs/planning/tool-list.md`](planning/tool-list.md).
