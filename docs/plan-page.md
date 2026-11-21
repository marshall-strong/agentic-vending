# The day-one planning page

One page that shows the agent's day-one plan round by round:
- what goes in each of our 20 slots, and how each round changed from the one before
- what the fill costs against the proposed $366 cap
- what each slot is projected to sell and earn
- which catalog requests the plan needs, and how far each has got
- where the Interloper (#116) sells the same drink

```
python -m vending.plan_page --rounds C:\Users\KevinSykes\vending-day1          # http://127.0.0.1:8768
python -m vending.plan_page --rounds DIR --no-db                               # no catalog-request status (our slots still from DATABASE_URL)
python -m vending.plan_page --rounds DIR --write plan.html                      # one file, no server
```

The planning page runs on your own machine. Use `--write` to make a copy for the team, as listed in
[`artifacts.md`](artifacts.md).

## Put a plan on the dashboard

The Container App reads the first-fill plan from `first_fill_plans`. Migration 020 creates this
table. Run these commands from a laptop with the round files and the database writer login.

```
python -m vending.plan_store load DIR --round 3 --by NAME
python -m vending.plan_store list
python -m vending.plan_store pick ID --by NAME
```

`DIR` can be the folder with the plan file or the folder with the round folders. The loader checks
the plan and projection against the current server assignment. It saves those files' JSON and
the assignment. Load and pick each record a write in `action_log`.

Before the slot lock, **The plan** on the dashboard shows the picked plan. With no pick, it
shows the latest plan and labels it "latest, not picked". After a redraw, load and pick a plan
for the new assignment. At the lock, the dashboard switches to the agent's Friday plan.

| Whole page (seeded test data, light) | Top of the page (dark) | Phone (390 px) |
|---|---|---|
| ![The whole page](plan-page/whole-page-1280-light.png) | ![Rounds, tiles and the machine](plan-page/top-1280-dark.png) | ![Phone](plan-page/top-390-light.png) |

The screenshots show the real round 1, 2a and 2b files. The catalog requests come from a throwaway local database seeded with sample requests, not from the team's database.

## Where each number comes from

| On the page | Source | Computed here? |
|---|---|---|
| Product, units, shelf price, the agent's reason | `roundN/plan*.json` (`day_one`) | read |
| Floor, legal price, whether the units fit a column | the pack list in the projection, through `vending.pricing` and `vending.capacity` | **yes**, with the planner's own rules |
| Fill cost (expected and most-we-pay), packs, closet units | the same pack list: whole packs per product, leftovers to the closet. Each slot's cost is its share of its product's packs, closet included, so the slots add up to the fill | **yes**, and checked against the projection's own totals |
| Units per class day, class days of cover, sell-out date, margin, gross profit by week and to the end of the window, low/base/high | `roundN/projection*.json` | read, never computed |
| The projection's assumptions | the projection's `assumptions` block, highlighted where they differ from the round before | read |
| Catalog request status | `product_requests` and `catalog_products` in our database (read-only) | matched by the same words rule as the duplicate check |
| The Interloper's prices | `analysis/interloper/YYYY-MM-DD.json`, one file per dated observation | read |

When the page's own count disagrees with a file, it says so under **Notes**: for example a fill total, a pack count, a price, a unit count or a SKU that differs between the plan and its projection (so the forecast was made for something else). A file with an unexpected shape is refused with its name, not shown half-read.

## Rounds

A round is one plan file. Pair files by their round ID, for example `plan2a.json` with
`projection2a.json` or `r4.plan.json` with `projection4.json`. The loader ignores other files
such as `plan1_sku.json`, raw model answers and briefs.

**Plan files** may use either round shape:
- Round 1: the full plan, with floats for prices.
- Round 2: `day_one` with `price_cents` and `sku`, plus `catalog_requests`.

The page reads float prices through their decimal text and refuses any that isn't a whole number of cents. A round that names any slot outside our slots (the inventory server's assignment, read from the database by `vending.slots.our_slots`) isn't shown slot by slot, so the page can't show a value for any other selection. It is set aside and listed under **Rounds compared** with its projection's own fill and gross profit, read as written. Rounds made before the 20 Nov redraw end up there. Any other problem with a file still stops the page. With nothing recorded, or no database, the page refuses and says to run `python -m vending.slots record`.

**Projection files** use the `vending-day1/projection/v1` schema. Money is integer cents in every `*_cents` field, and the old float fields are ignored. Projection weeks run Thursday to Wednesday from 3 Dec, but the ledger's weeks run Saturday to Friday, so the weekly chart won't line up with the P&L report.

## Rules the page keeps

- **Only our assigned slots have values.** The other 20 selections are drawn hatched with their number only. That is because the plan places products in our 20 slots, not because the rest belong to anyone yet: the inventory server changed the split once, on 20 Nov, and it is final for the quarter (David, 20 Nov). `python -m vending.slots record` stored the new split, and the plan was placed again in the new slots as a new round; rounds made before it are set aside. Fall sales from all 40 selections, from polls before the fall cut, may be used for planning (Kevin's decision, 15 Nov; `docs/integrity.md`).
- **No model calls.** Everything is computed in code or read from the files.
- **No script.** The round tabs and the "Changed slots only" switch are HTML inputs styled with CSS. The server sends `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'`.
- **Local and read-only.** The server listens on 127.0.0.1 and answers only `localhost` or `127.0.0.1`. The database session is read-only, with a statement timeout. With no database, or if it can't be reached, the page refuses, as it does with nothing recorded, because it can't tell which selections are ours. `--no-db` skips only the catalog check, and the catalog panel then says the requests weren't checked.
- **Interloper prices are display only.** The Interloper is an outside seller, not the other team. No pricing code reads these files, and none may: David said on 17 Nov that the Interloper's prices are not a pricing input. Prices are compared only when both sizes are known and match; otherwise the row says "different size" or "size unknown". The **I** flag on a slot uses the same match as the table.
- **The $366 cap and the $600 split are proposed (#113), not agreed.** They are reference lines only (`plan_rounds.FILL_CAP_CENTS`).
- **A request's status isn't synced back yet.** Nothing reads it from the bridge. So a drink counts as approved once it appears in our copy of the catalog. The team's list is #119.

## Not here yet

- **The money panel (#48)** on `/dashboard`: balance, spend and token spend against $600. This is next.
- **Forecast against actual** once the run starts: needs the projection stored with its `plan_hash`.
