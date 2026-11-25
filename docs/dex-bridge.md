# Talking to the DEX Bridge

The DEX Bridge is the Raspberry Pi (`aihub-pi`) wired to the vending machine. It reads
the machine's sales report, stores every read, and runs the purchasing API. The quickest
way to use it is `scripts/bridge.py`, which wraps every route we are allowed to call.

The full, live API reference is the bridge's own Swagger page:
<http://aihub-pi:8000/docs> (Tailscale only). This page covers what we have learned using it,
checked against the live spec on 29 Oct.

**Don't rely on the bridge's version number to tell you the API changed.** It said `0.1.0` on
15 Oct and still said `0.1.0` on 29 Oct, after David reshaped purchase requests and added
product requests. On 20 Nov it said `0.4.0`, with the tracking check added. To see whether anything
changed, compare <http://aihub-pi:8000/openapi.json> with the last copy saved, not the version. The
20 Nov copy is [`bridge-openapi-2026-11-20.json`](bridge-openapi-2026-11-20.json), as fetched (one
line; `python -m json.tool` it before a diff).

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
| `health` | `GET /health` | Bridge status (`status` only). The only route that needs no key |
| `slots` | `GET /api/v1/slots` | The selections the server currently assigns us on `vmc1`; it may change before 3 Dec. Checked as `python -m vending.slots record` checks it: a reply for another team, another machine or not 20 selections is refused, and none of it is printed |
| `products` | `GET /api/v1/products` | What we can order, as single items: `sku`, `name`, `line`, `flavor`, `size`, and nothing else the reply holds. No prices and no pack sizes. Three products on 29 Oct, one of them a test entry |
| `history [--start D] [--end D]` | `GET /api/v1/history` | Stored polls, oldest first: `session_id`, `started_at`, `bytes` and `truncated`, each checked, and nothing else the reply holds. An entry whose session ID isn't one is left out, and stderr counts it. **At most 500 per request**: a longer range comes back with `truncated: true`, so ask a day or a week at a time |
| `raw SESSION_ID [--out FILE]` | `GET /api/v1/history/{id}/raw` | One stored poll, rebuilt from our selections and the door counter (see [below](#what-raw-latest-and-poll-print)) |
| `latest [--summary] [--out FILE]` | history + raw | The newest stored poll. `--summary` prints price and lifetime sales for our slots, from the stored assignment |
| `request REQUEST_ID` | `GET /api/v1/purchase-requests/{id}` | `pending`, `rejected`, or the full receipts: the documented fields only, each checked. A `note`, `decision_note` or `loss_note` prints as its length, `(note omitted, N characters)` |
| `poll --live [--out FILE]` | `POST /api/v1/poll` | Reads the machine right now. Refuses without `--live` |
| (not in this script) | `POST /api/v1/tracking/check` | Our books checked against the bridge's, once a day. Use `python -m vending.tracking check`; see [below](#the-tracking-check) for why it isn't here |

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
  `poll` only runs with `--live`, and you should rarely need it. The one automatic exception is our
  worker, which reads the machine every minute on Friday afternoons around our visit
  (`vending/visit_polls.py`, `docs/deploy.md`); a person's `--live` poll then gets a `409` while one runs.
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

## What raw, latest and poll print

**This script never prints a whole poll.** All 40 selections' per-selection data, and the
machine-wide totals, may be used only from polls taken before the fall cut: the setting
`FALL_CUTOFF`, 18 Nov 2026 00:00 Eastern when unset, and never later than the 3 Dec 2026 00:00
Eastern start (`docs/integrity.md`, "Fall data"). The only times the script could judge that by
are the bridge's (a listing's `started_at`, a reply's `Date`), and those can be wrong: a slow or
proxied clock, or a time with no offset. So it doesn't judge: `raw`, `latest` and `poll --live`
print every poll the same way, whatever its time. Pre-cut fall data for all 40 selections comes
from `analysis/export_sales.py --fall-all`, which stops at the cut. The script doesn't read
`FALL_CUTOFF`.

From the start the raw poll carries all 40 selections, the other team's 20 too. Storing it is fine.
"Ours" means the stored assignment (`vending.slots.our_slots`, so the script needs
`DATABASE_URL`), never a fresh `slots` answer.

| Command | What it prints, for any poll |
|---|---|
| `raw SESSION_ID` | A rebuilt view (below) on stdout, and a note on stderr saying how many lines were left out |
| `latest` | The session and the time it checked on stderr, then the rebuilt view and the note |
| `poll --live` | The rebuilt view, and the note |
| `latest --summary` | JSON price and lifetime sales for our selections only. The stored assignment is read first, before any poll. A selection that isn't ours is skipped at its `PA1`, before any of its fields are read. With no readable assignment it prints `[]`, reads no poll, and says why on stderr |
| `--out FILE` (with `raw`, `latest` or `poll --live`) | Archival storage: saves the whole poll byte for byte, to a regular file only. It refuses `-`, a device (`/dev/stdout`, `/dev/tty`, `CON`, `NUL`, `COM1`, `\\.\...`), a pipe, a directory, or a link to any of them, exits 6 and writes nothing. Nothing is printed on stdout |

**The rebuilt view** is written anew from checked fields. No line is copied from the poll:

- `ID4*<decimal places>`, so a price can be read, when they are 0 to 2;
- for each of our selections, `PA1*<selection>*<price>` and `PA2*<lifetime units>*<lifetime value>`;
- the door counter, `EA2*DO*<since reset>*<lifetime>`.

Only those fields are kept. A `PA2` belongs to a selection by the same block rules the history
sync uses (`vending.evadts.records`): across `PA3`-`PA5`, `PA7` and `PA8` only. A selection listed
twice prints none of its figures, and stderr says so. `latest --summary` reads the same way.

A field counts only if it is a plain number. The first field that isn't ends its record.
Everything else is left out: the other team's selections, the machine-wide totals (`VA1`, the
cash and card records), `ID1`, the other `PA` records, the envelope and `G85` (its checksum
wouldn't match). With no readable assignment, the view has no `PA` records and stderr says
"no selection is printed".

**Nothing prints the whole of a poll.** The `--unredacted` flag is gone. `--out FILE` is archival
storage: it keeps the raw bytes in a regular file, because storing the poll is fine. It is not a
way to read them. Reading a saved poll taken from the cut on follows the same rule as printing it:
only our selections, and no machine-wide total.

**What this guards against.** The script keeps poll content off the screen. It doesn't verify
catalog or status text against a server that deliberately disguises it: a product's name prints if
it is one short line with no `*`, and `health` prints only a known status.

**Errors.** From every route, an error shows only the status and its standard phrase, for example
`404: Not Found`, never the bridge's reply: an error reply could carry a poll's records. A reply
that isn't valid HTTP (a malformed status line, say) shows only the kind of fault, and a failed
connection only the operating system's reason. A reply that isn't JSON where JSON is expected, or
isn't the shape the script knows, isn't printed either. A failed read of the stored assignment
names the error's kind, not its text.

## Purchase requests, in short

As of 29 Oct. **A request describes the machine we want after the restock: for each slot,
what goes in it and what it sells for.** The body is `off_cycle`, an optional `note` (up
to 500 characters) and `lines`, one per slot.

- **`off_cycle: true` asks for a visit on any day but Friday, and costs $25.** That covers
  a restock, a product switch or a price change. There is no separate visit route. The spec
  doesn't mention the fee; the team confirmed it on 29 Oct.
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

**Prices change only at a visit.** There are no remote price changes: the student worker changes the tags by hand, on the free Friday visit or a $25 midweek visit, from the sale price on each line.

**What the API still can't do:** change a price without loading anything (every line loads
at least one pack or unit), leave a slot empty, or list our requests.

## The tracking check

As of 20 Nov (v0.4.0). `POST /api/v1/tracking/check` takes our figures as of one poll and says,
for each, `within` or `outside`. It never says by how much, which way, or which slot.

- **Body** (no other keys): `as_of`, a poll's `session_id` from `GET /api/v1/history` (everything up to
  and including that poll counts); `cash` and `sales_tax_owed`, in dollars (number or string); `coil`,
  units per slot for **every slot we own and only those**; `back_stock`, units per SKU (a SKU left out
  counts as 0).
- **200**: `as_of`, `as_of_time` (UTC), `checked_at`, `results` (`cash`, `sales_tax_owed`, `coil`,
  `back_stock`, each `within` or `outside`) and `next_check_from` (midnight Eastern).
- **Errors** are `{error, error_type, detail, next_check_from?, items[]}`: `403` needs a team key;
  `404 unknown_poll`; `409 not_reconciled_yet` (polls are folded into the books on the hour) or
  `tracking_not_started`; `422 invalid_coil`, `unknown_sku` or `before_tracking_start`; `429
  daily_check_used`. A body off the schema gets FastAPI's own `422`.
- **One check per team per Eastern day.** A request refused before the check is made doesn't use it.
  David, 20 Nov: no limit while testing; once the quarter starts, one a day.
- **Tolerance** (David, 20 Nov): within $1 for money, within 2 units for counts. Whether 2 units is
  per slot and per product or across all of them isn't said yet.
- **Cash** is what we have to spend after the sales tax owed is taken out (David, 20 Nov): the ledger's
  spendable balance.
- On 20 Nov a live probe got `409 tracking_not_started` ("nothing to check against until the machine is
  stocked for the project"). The books start when staff stock the machine, on Mon 30 Nov.

`python -m vending.tracking check` works out our four figures as of a poll (default the newest stored
one) from the ledger and the stock count and prints them; `--send --by NAME` sends them once and
stores the request and the answer in `tracking_checks`. From the competition start it refuses a second
send the same Eastern day unless the earlier one was refused without using the check. The day is the
bridge's: a send reserved at 23:59:59 and checked at 00:00:01 uses the new day's check. The module's
docstring has the details. The simulated bridge answers the same route (`vending/simulate/bridge.py`).

The worker can send it once a day (`vending/tracking_daily.py`, #218), off until the team sets
`TRACKING_DAILY=on`: see [`deploy.md`](deploy.md). It uses the same code and the same guard. A slot no
counted receipt has loaded is refused as `tracking.NotLoaded` (a `CantCompute`): the command refuses to send,
and the worker records the round as a skip (#254).

**Why `scripts/bridge.py` doesn't send it.** That script only reads: it can't submit a purchase request,
and it writes nothing to our database. A tracking check spends the day's one check, and its answer
belongs in `tracking_checks` beside what we sent, behind the once-a-day guard. A send from the script
would skip both. So it lives in `vending.tracking`, like the other writes.

## Product requests

`POST /api/v1/product-requests` proposes a drink for the catalog. It takes a `name` and
`size` (both required) and an optional `line`, `flavor` and `note`. It orders nothing. Check
it with `GET /api/v1/product-requests/{id}`: `pending`, `rejected` with a `decision_note`, or
`approved` with the `sku` it was added under. The name and SKU may differ from what we
proposed. Too many open product requests also returns `429`.

Details, and the questions still open with David, are in
[`docs/planning/tool-list.md`](planning/tool-list.md).
