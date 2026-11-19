# The agent dashboard

A read-only web page for watching the agent while it runs: is it halted, what is it doing, what
did it plan, and what changed since last week.

```
python -m vending.dashboard              # then open http://127.0.0.1:8765
python -m vending.dashboard --port 9000
```

It reads whatever database `DATABASE_URL` points at: your local one, or the team's Azure
database. The page refreshes every 30 seconds. `/state.json` returns the same data as JSON, for a
script or another agent.

![The dashboard with sample data](dashboard/current.png)

*Sample data from a throwaway database, not a real run. **The highlighted columns in this picture are not our real slots.** For the screenshot our slots were set to columns 2, 4, 6 and 8, to show the page follows `vending.slots`. Our real slots are the ones the stored server assignment names (`python -m vending.slots`).*

The money panel (#48), with seeded test data rendered at a fixed time (9 Dec): an opening of $600, two fulfilled
requests, three model calls and one pending request. Light, dark and phone:

| Light | Dark | Phone |
|---|---|---|
| ![Money panel, light](dashboard/money-1280-light.png) | ![Money panel, dark](dashboard/money-1280-dark.png) | ![Money panel, phone](dashboard/money-390-light.png) |

## What it shows

The page answers four questions, top to bottom: is it OK, what is it doing, what did it plan, and
what changed and why.

| Part | What's in it | Where it comes from |
|---|---|---|
| Verdict | **OK**, **Check** or **Problem** next to the title (and in the browser tab), from the worst item in Needs attention | everything below |
| Tiles | The halt switch and who last changed it; the latest run and how long it has run; a countdown to the Thursday 23:59 Eastern cutoff; how old the last machine poll is and what our slots sold in the 7 days to it (the window's start date isn't shown, since it read like an update time); what has been spent on drinks (fulfilled requests) and on the model (run costs), labelled as development and test spend before the opening, not from the \$600; once the ledger's opening entry is in the past, what has been spent since the opening, from the ledger, so the page gives one figure; on the container, who has looked: browsers, visits and sign-ins, all time and this week (`vending.visits`; see [`deploy.md`](deploy.md#the-public-pages)) | `config`, `agent_runs`, `polls`, `purchase_requests`, `vending.ledger`, `page_visits`, `page_visit_totals` |
| Needs attention | One sentence per thing a person should look at, worst first. **Problem:** the latest run failed, or has said `running` for over 2 hours; on the Container App, a job in the worker's latest sync round didn't exit 0 (see [Sync health](#sync-health)). **Check:** refused or failed tool calls in the last 24 hours; polls over 3 hours old; a catalog request stuck in `sending` or `unknown`; the latest purchase request rejected; and within 24 hours of the cutoff, still halted or no Friday request sent. **Info** (doesn't change the verdict): halted, no polls yet, stock warnings from the receipts, a panel whose table is missing | the snapshot, and the worker's latest round |
| In the machine | A close-up of our selections, one shelf at a time, beside a small drawing of the machine's front (5 shelves of 8; row r, column c is selection "rc") that picks the shelf. Which selections are ours comes only from the stored assignment (`vending.slots.our_slots`, kept by `python -m vending.slots record`), read on every page; with none stored the panel says so and draws nothing. Each of ours is a lane seen from above the front edge: the packages on the rail, dashed empty slots, its number plate and price tag, then the product, the count and what Friday does to it. Every other selection is a grey card with its number and "Not ours", and nothing else about it is in the page or `/state.json`. **Now** is the tag price from the latest poll and the product and units on hand from the receipts, or from a person's count where the receipts don't know the slot or the count is newer (see below). **Next Friday** is the latest plan: the empty slots fill in, a swap draws the new product's package and colours, a pull empties the lane, a new price shows the old one struck through, and the agent's reason sits under the lane on one line. Beside it, each change with the agent's reason. "plan row" opens the selection in the plan | `vending.stock` (receipts and polls), `get_machine_state` (polls), `agent_runs`, `catalog_products`, `config.capacity_overrides` |
| The plan | "N of 20 slots changed since run #X", a chip per change that jumps to the slot and opens its reason, a "Show changed slots only" switch, then one line per slot (product, price, fill, change tags) that opens to the reason, what it was, and what the slot holds and what the slot sold in the 7 days to the last poll. That count is the slot's, not the product's: a swap at a visit inside the window adds the old product's sales (#172), and the page says so. Under it, the weekly update for the team and the notes to itself | `agent_runs`, `vending.stock`, `get_machine_state` |
| Runs | The last 10 runs as a timeline: mode, model, how long, cost, request, how it ended, and chips for every slot its plan changed from the plan before it. Under each, **How it decided** opens the run's steps in order: the model's reasoning (a summary the API returns, never the raw chain of thought), anything it wrote, each tool call and what came back, with failed replies in red. A run in progress shows the steps so far | `agent_runs`, `agent_steps` |
| Tool calls | Counts for the last 24 hours, then the last 50 calls. Writes are marked W and shaded; refused and failed calls are red. Filter to writes or to refused and failed; each call's arguments open on a click | `action_log` |
| Money | **Balance**, **spendable** (balance less the sales tax set aside) and **profit since the opening**. One bar shows where the money in (opening, capital, takings) has gone: drinks bought, midweek visits, model spend, tax handed over, tax set aside, and what is still spendable. Then model spend against the proposed \$9 and visit fees against the proposed \$50, the profit and loss since the opening, the proposed split of the \$600 (#113), and the ledger's notes, such as development spend from before the opening. **Committed:** what the pending purchase requests could still cost at most (the dearer of each line's primary and alternate at `packs x max_price_per_pack`, plus an off-cycle request's visit fee), since the ledger counts a purchase only once it is fulfilled. If any pending line has no price we can read, the most is unknown, never counted as \$0, and so is what stays spendable after it. The competition counts from the ledger's opening entry; anything before it is development and test spend, not from the \$600, and no figure here counts it. Before the opening entry, it says how to record it; an opening entry dated in the future (recorded ahead with `--at`) keeps that view and says when the ledger opens. Receipts and visit fees from before the opening are test purchases: once the opening is recorded, stock on hand and cost of sales leave them out too (`stock.receipts_count_from`, #150). A submission sent with no usable answer (`sending` or `unknown` in `purchase_submissions`) counts as committed too, since it may exist at the bridge. Needs attention adds: **Problem** if spendable is below zero, or if pending requests could cost more than is spendable; **Check** if spendable would fall under the proposed \$53 floor, if requests are pending with no opening entry or before an opening dated ahead (the date in Eastern), if a submission is unsettled, if a pending line has no price we can read, or if the ledger can't give a balance (for example, our slot assignment isn't recorded); **Info** if model spend passes \$9. If reading the ledger passes the statement timeout, only this panel says it isn't available; the rest of the page still shows | `vending.ledger` (`balance`, `pnl`), `purchase_requests`, `purchase_submissions` |
| Requests | Our last 10 purchase requests (status, what was paid, lines loaded, unavailable and alternates). Then every catalog request, newest first: counts by status, the open (`pending`) ones against the bridge's cap and the places left, and each request's status, its SKU once it has one (an approved product may be added under a SKU we wouldn't guess), and the decision note | `purchase_requests`, `purchase_lines`, `product_requests` |
| Back stock | What the receipts say is in the closet, and what each unit cost us | `vending.stock` |

### The machine close-up

![In the machine, dark theme](dashboard/machine-closeup-dark.png)

| | |
|---|---|
| ![Light theme](dashboard/machine-closeup-light.png) | ![Next Friday](dashboard/machine-closeup-next-friday.png) |
| Light theme | Next Friday: a swap (12), a price change (13) |

![A 390 px phone](dashboard/machine-closeup-phone-390.png)

*Sample data in a throwaway local database, not a real run: the products, prices, plan and reasons
are made up, and our selections were set to columns 1 to 4 for the picture. Seeded the evening of
15 Nov 2026.*

How to read a lane:

- **Count.** The big number is units on hand (Now) or after the plan's fill (Next Friday). **~4**
  is from a photo count: units seen down the rail, less sales since, so at least that many. **?**
  means no receipt or count yet; the rail behind the front item is then hatched, never drawn full or
  empty.
- **+N back Friday** is the plan's `fill_to` less the units on hand, when both are known. With
  units unknown it is **+?**. With no plan there is no number, only "no plan yet". It is never
  capacity less units.
- **Dashed slots** are empty room in the column. How many fit comes from
  `capacity.for_product`: a person's count in `config.capacity_overrides`, else the machine's
  loading chart for the catalog size. With neither (a carton, a 16.9 oz bottle), the lane draws no
  empty slots beyond what the plan fills and says "capacity not counted yet".
- **Swap, pull, price.** On Next Friday a swap draws the new product, a pull shows the lane empty,
  and a price change strikes the old tag through. The agent's reason is cut to one line; the full
  text is in the plan row.
- **Named from a count.** Photo and door counts name a product in free text, not a SKU, so it
  can't be looked up in the catalog. The package is then read from the name ("Coke 20 oz bottle"),
  with a hash colour, and the lane says "named from a photo count".

Where the drawing comes from (`vending/machine_art.py`; no photos, no images, no model calls):

- **Package shape**: `SHAPES`, one entry per package with real millimetre sizes (typical retail
  figures, not measured on our packages), picked by the catalog `size` through
  `capacity.parse_size`, the same reading the capacity chart uses. A size it can't read gets the
  generic bottle, can or carton (`FALLBACK`).
- **Colours and label**: `LOOKS`, by SKU, then `line|flavor`, then line, all lower case. Anything
  else gets a steady colour from a hash of the SKU and the first word of its line as its label, so
  a product added to the catalog today draws without any change here.
- **The one extra read**: the snapshot's `products` entry reads `catalog_products` (sku, name,
  line, flavor, size) for the products our lanes hold now or next, and the capacity overrides for
  them, in the same read-only, repeatable-read snapshot. If that read fails, the page still draws:
  packages are guessed from names, capacities are unknown, and the legend says so.

To make a product look right, add a row to `LOOKS` in `vending/machine_art.py`, keyed by its line
and flavor as the catalog spells them, lower case (`"bubly|strawberry"`), or by its line alone for
every flavor:

```python
"bubly|strawberry": {"c1": "#94c93a", "c2": "#f6d3d6", "c3": "#e23b4f", "ink": "#8a1a2a", "label": "bubly"},
```

`c1` is the body, `c2` the label band, `c3` the cap or stripe, `ink` the label text, and `label` a
word or two printed on the front package. A new package size (a 1 L bottle, say) is a new `SHAPES`
entry with its diameter and height; anything else in the page needs no change.

On a phone the shelf is a strip that shows two lanes at a time: swipe, drag its scroll bar, use the
arrows on each lane, or focus it and use the arrow keys. From 820 px wide the whole shelf of 8
shows at once. Every lane has an aria label that reads it out (product, count, Friday, price,
reason); with reduced motion set, the Friday fill doesn't animate.

A slot counts as changed when what the plan asks for changes (product, fill, price, keep). A
reworded reason alone is not a change. Status and the machine follow `live` and `dry-run` runs
only, and a plan is compared only with the last plan of the same mode; replays of past weeks show
under Runs, dimmed. Model spend counts `live` and `dry-run` runs.

The page refreshes itself in place every 30 seconds, keeping your scroll position, any open
slot or arguments, the tool-call filter, the Now / Next Friday choice, the shelf you picked and the
changed-only switch, and the verdict in the tab title. Without JavaScript the machine shows Now
(the shelf links still work), the plan shows every slot, and the switches are hidden. If a refresh fails (the database is down) it keeps the last
page and says how old it is. Without JavaScript it reloads the whole page instead.

## Catalog requests

The instructors must approve every day-one product by Wed 25 Nov 2026, so the panel lists all of
our catalog requests, not the newest few. They are our own team's records, so the public page
shows them too. Above the list:

- **Counts by status:** pending, approved, rejected, and any sending or unknown (a request we
  sent but can't tell whether the bridge filed; settle it with `python -m vending.product_requests
  resolve`).
- **Open: N of about 25, and the places left.** The bridge answers `429` once a team has about 25
  open (pending) requests. That number is observed, not documented: it refused a filing at 25 open
  on 17 Nov, and at 24 on 15 Nov, though that day's count was inferred. It is
  `OPEN_REQUEST_CAP` in `vending/dashboard.py`. A sending or unknown request may hold
  a place too, so while places are left the page says they may be fewer by that many. With no
  requests yet the counts still show, all zero.

Statuses and SKUs are what `product_requests` last stored, so on `main` an approval made on the
bridge still shows as pending until someone stores it. Today a person or the tool writes
them; the request sync in PR #147 (`vending/sync_product_requests.py`) fills in `status`, `sku` and
`decision_note` from the bridge, and the panel reads those same columns, so it needs no change
when that merges. `/state.json` carries the rows in `catalog_requests` and the counts in
`catalog_counts`.

## Sync health

The worker (`vending/worker.py`) runs the three syncs every hour and keeps each round's outcome
and exit code in memory, not in the database: `/health` shows them, and says `failing` when any
job didn't exit 0. The dashboard on the Container App runs in that same process, so the worker
hands the same dict to it (`snapshot(..., syncs=...)`). When a job in the latest round didn't exit
0, Needs attention says so as a **Problem**, naming each job with its exit code (or `crashed`,
`timed out`, `still running`) and what the code means:

| Exit | Meaning |
|---|---|
| 1 | finished with problems; the container's log has them |
| 2 | the database is missing migrations; run `python -m vending.db migrate` |
| 3 | a bridge error: unreachable, or it refused our key or answered with an error |

Like `/health`, it shows no error text. `/state.json` carries the round as `syncs`. Run on its own
(`python -m vending.dashboard`), the dashboard has no worker beside it, so `syncs` is `null` and
the page says nothing about the syncs; look at the Container App's `/health`. The poll-age
warning still covers a history sync that has stopped.

## Honesty rules

The page shows only what something recorded, and says so when nothing did.

- **Stock is estimated from receipts and polls, not a sensor.** Units on hand are what the
  fulfilled receipts loaded, minus what the polls' counters say sold, minus recorded losses.
- **A plan is a proposal, not a restock receipt.** Next Friday is what the agent asked for, not
  what a worker loaded.
- **Unknown stays unknown.** A selection with no receipt shows `?` and "unknown", never a guess
  from the plan. A price with no poll shows a dash. Without a plan, Next Friday says "No plan yet".

**Money is the ledger's, exact to the cent.** The balance counts a purchase only once it is
fulfilled, so the panel adds what pending requests could still cost, read from their own bodies.
The \$600 split and the \$53 floor are the team's *proposed* capital plan (#113), shown as reference
lines only.

## What is in the machine: counts

The polls never say which product is in a selection, and receipts only name what we stocked
ourselves. So a person records what they see, and the dashboard takes it from there:

```
python -m vending.slot_counts record db/counts/2026-11-09-photo.json --by kevin --dry-run   # check it
python -m vending.slot_counts record db/counts/2026-11-09-photo.json --by kevin             # record it
python -m vending.slot_counts show
```

A count file lists every selection looked at, ours or not: the product as seen (or null when
empty), the units on the rail and the price on the tag. `source` is `photo` (an estimate: a
picture can't see how deep a rail goes) or `door` (counted with the door open, exact). Recording
writes the whole count in one transaction and logs it to `action_log`; a count id can be recorded
once, and the command never migrates. It isn't gated by the halt switch, because a count changes
nothing in the machine.

On the page, a counted slot shows units counted less what sold since, from the polls: sales
start at the first poll after the count, so nothing sold before it is taken off twice. A photo
count shows as "~3"; a slot the count saw empty shows "0 · empty". A restock receipt newer than
the count replaces it. `db/counts/2026-11-09-photo.json` is the first count, read from photos of
the machine on 9 Nov; its units are estimates and brands that couldn't be read are kept as the
visible description.

## Safety

- **It can't write.** Its connection sets `default_transaction_read_only`, so Postgres refuses
  any write, and it never migrates. A table that doesn't exist yet blanks only its own panel.
- **On the container it is behind the team password (#136).** `/dashboard` on the Container App,
  with `/dashboard/state.json` beside it, shows this page to anyone who signs in with the team's
  shared password; only `/health` is open without it. There it connects as a
  login that can only read, and the error text of failed tool calls and failed runs is left out,
  on the page and in the JSON. So are the selections not assigned to us: the JSON's counts hold only ours. [`deploy.md`](deploy.md#the-public-pages) has the rules.
- **Run locally, it listens on 127.0.0.1 only, and has no login.** It answers only requests addressed to
  `127.0.0.1` or `localhost`, so another website can't point its own name at your machine and
  read the page. A local copy still shows the error text the public page leaves out, so don't
  put a local copy on a public address.
- **It costs nothing to run.** No model calls, no new services, no new packages.
- **It never writes the visit counts itself.** The container's worker counts sign-ins and visits
  with the syncs' login; the page only reads `page_visit_totals` and the last week of
  `page_visits`, and its refresh says it is one (a header, or `?refresh=1` without scripts), so
  an open tab isn't counted again every 30 seconds.

## The run record

The plan, update and notes come from `agent_runs` (`db/migrations/009_agent_runs.sql`). Nothing
writes it yet: the agent loop (#39) should call `vending.agent_runs` at the start and end of each
run, and the agent replay can do the same with `mode="replay"`:

```python
from vending import agent_runs

run_id = agent_runs.start(conn, mode="dry-run", model="claude-sonnet-5-5")
# ... the run ...
agent_runs.finish(conn, run_id, "submitted", plan=plan, update=update, notes=notes,
                  cost_usd=Decimal("0.42"), request_id=request_id)
```

`status` is one of `submitted`, `skipped`, `halted` or `failed`. `plan` is the replayer's decider
shape (`docs/week-replayer.md` on #28). `cost_usd` must be a `Decimal`, and a `Decimal` price in the plan is stored as exact text
(`"2.50"`). A run finishes once; a second `finish` raises.

Times on the page show in your browser's time zone. On Windows, the cutoff date needs the
`tzdata` package (`pip install tzdata`); without it the page says "Thursday 23:59 Eastern".

## Where it could go next

Ideas for the team to react to. None of this is built.

**A. The week's profit.** The Money panel shows profit since the opening; a week-by-week view
(`ledger.pnl_by_week`, as `python -m vending.pnl_report` prints it) isn't on the page yet.

**B. Plan history.** One card per week: the update, the slots that changed, the notes, what it
cost. Everything it needs is in `agent_runs` once #39 writes it.

**C. A live run.** Each tool call as it happens, the draft plans as they change, and the run's
budget. Each run's steps, its reasoning included, are now in `agent_steps` and show under
How it decided, so what is left is a page that follows one run live and the run's budget.
