# Talking to the DEX Bridge

The DEX Bridge is the Raspberry Pi (`aihub-pi`) wired to the vending machine. It reads
the machine's sales report, stores every read, and runs the purchasing API. The quickest
way to use it is `scripts/bridge.py`, which wraps every route we are allowed to call.

The full, live API reference is the bridge's own Swagger page:
<http://aihub-pi:8000/docs> (Tailscale only). This page covers what we have learned using it,
checked against the live spec on 29 Oct.

**The bridge's version number does not change when the API does.** It said `0.1.0` on
15 Oct and still says `0.1.0` on 29 Oct, after David reshaped purchase requests and added
product requests. To see whether anything changed, compare
<http://aihub-pi:8000/openapi.json> with the last copy you saved, not the version.

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
| `products` | `GET /api/v1/products` | What we can order, as single items: `sku`, `name`, `line`, `flavor`, `size`. No prices and no pack sizes. Three products on 29 Oct, one of them a test entry |
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
  money. The agent's `submit_desired_state` tool will be the only way to buy, because
  that is where the balance, price-bound and halt checks live. Use Postman for one-off
  experiments only if the whole team agrees first.
- **The script cannot submit or check a product request either.** Check one with
  `GET /api/v1/product-requests/{id}` on the Swagger page. Submitting one belongs to the
  agent's `request_catalog_product` tool, or to a team decision.
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

As of 29 Oct. **A request describes the machine we want after the restock: for each slot,
what goes in it and what it sells for.** The body is `off_cycle`, an optional `note` (up
to 500 characters) and `lines`, one per slot.

- **`off_cycle: true` asks for a midweek restock.** There is no separate visit route. The
  spec does not mention the $25 fee.
- **A purchase line** (`"source": "purchase"`) has a `primary` choice and an optional
  `alternate`, bought if the primary can't be found. Each choice gives the `sku`, the
  `store` (`Walmart`, `Costco`, `Amazon` or `BJs`, spelled exactly), `packs` (1-20),
  `units_per_pack` (1-60), an optional `pack_description` for the shelf, the
  `max_price_per_pack` the worker may pay, and the `sale_price`. An Amazon choice needs the
  Amazon `url`. Purchases are tax-exempt, so the max is the shelf price.
- **A back-stock line** (`"source": "back_stock"`) loads `units` (1-200) of a `sku` from our
  closet, with its `sale_price`. The bridge doesn't check that we have them.
- **Every choice carries a `sale_price`:** tax included, steps of 0.05, at most $10.00.
  Money can be a number or a string (`2.95` or `"2.95"`).
- **The alternate has its own `sku` and `sale_price`,** so it can be a different product
  at a different price.
- **`change_product` must be `true` exactly when the choice isn't what the slot holds now.**
  An empty slot accepts either.
- **Every line is checked at once.** A `422 invalid_lines` lists every problem, each with
  an `error_type`: `duplicate_slot`, `unknown_slot`, `slot_not_owned`, `unknown_sku`,
  `product_not_orderable`, `alternate_same_as_primary`, `unacknowledged_product_change`,
  `unexpected_product_change`.
- **`429 too_many_open_requests`** means we must wait for earlier requests to be decided.
  The spec doesn't say how many may be open.

The request stays `pending` (or becomes `rejected`, with a `decision_note`) until the worker
has handled every line. Then each line gets a receipt: `outcome` (`primary`, `alternate`,
`loaded` or `unavailable`), the `store` actually used, `packs_bought`, `units_per_pack` as
actually bought, `total_paid` (exact, authoritative), `unit_cost_paid` (to four places),
`units_lost` with a `loss_note`, `units_loaded`, `units_to_back_stock`,
`units_from_back_stock`, `units_pulled`, `pulled_sku` and `slot_reassigned`.

**A fulfilled request can still change.** Units later found broken or missing, in the
coil or the closet, are added to the line's `later_losses`. Re-read recent requests to keep
our stock count right. There is still no route that lists our requests, so save every ID.

**What the API still can't do:** change a price without loading anything (every line loads
at least one pack or unit), leave a slot empty, or list our requests.

## Product requests

`POST /api/v1/product-requests` proposes a drink for the catalog. It takes a `name` and
`size` (both required) and an optional `line`, `flavor` and `note`. It orders nothing. Check
it with `GET /api/v1/product-requests/{id}`: `pending`, `rejected` with a `decision_note`, or
`approved` with the `sku` it was added under. The name and SKU may differ from what we
proposed. Too many open product requests also returns `429`.

Details, and the questions still open with David, are in
[`docs/planning/tool-list.md`](planning/tool-list.md).
