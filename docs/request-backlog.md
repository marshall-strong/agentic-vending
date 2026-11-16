# The catalog-request backlog

The bridge stops taking our product requests while too many are open: on 15 Nov 2026 it refused the
next one at 24 open. So the drinks we want to ask for next wait in a **backlog**, in priority order,
until the instructors decide some of the open ones. The backlog lives in `product_request_backlog`
(migration 017), so it isn't lost in someone's scratch file and the dashboard can show it.

## Commands

```
python -m vending.request_backlog                                   # the backlog, open requests, recent outcomes
python -m vending.request_backlog add "Snapple Apple" --size "16 oz bottle" --flavor Apple \
    --why "adds about $50 to the day-one plan" --gain 49.69 --by NAME
python -m vending.request_backlog import db/product_requests/backlog-2026-11-15.json --by NAME
python -m vending.request_backlog move 7 --to 1 --by NAME
python -m vending.request_backlog drop 7 --reason "the team decided against it" --by NAME
python -m vending.request_backlog file --check --by NAME            # what would be filed; sends nothing
python -m vending.request_backlog file [--max 5] --by NAME          # file from the top until the bridge is full
```

- **`add` and `import` refuse** a field the bridge would reject (the same checks as
  `request_catalog_product`), a drink we've already requested, and a drink already queued. An import
  is all or nothing.
- **`--why` and `--gain`** say why a drink is on the list and, if someone projected it, how much gross
  profit it adds to the best day-one plan. They're for people and the dashboard; filing ignores them.
- **`file`** sends from the top through the agent's own write gate and `request_catalog_product`, so
  the halt switch, the duplicate checks and the weekly limit (#130) all apply, and every request is in
  `product_requests` and `action_log` like any other.
  - It **stops** at the first `too_many_open_requests`, at a halt, or at the weekly limit, leaving the
    rest queued. It also stops if the bridge's answer is lost (`unknown`): settle that one first with
    `python -m vending.product_requests`.
  - A drink the bridge says is **already in the catalog**, or that we'd **already requested**, is
    dropped with that reason, and filing goes on.
  - A filed row points at its `product_requests` row, so its outcome (pending, approved, rejected)
    shows next to it.
- Every change needs `--by NAME` and is logged in `action_log`. Nothing here migrates.

## When to file

After the instructors approve or reject some open requests. The catalog sync shows approvals as new
products. Rejections don't show there; `GET /api/v1/product-requests/{id}` has them (see
[`mcp-server.md`](mcp-server.md)). Filing needs the bridge, so it runs from a machine on the class
tailnet with `DATABASE_URL`, `DEX_BASE_URL` and `DEX_API_KEY` set.

## The first backlog

`db/product_requests/backlog-2026-11-15.json` holds the 14 drinks left over when the bridge stopped
the 15 Nov batch. They're ordered by the day-one projection:

1. Snapple Apple (+$50) and Powerade (+$40), the only two that add profit in the projection.
2. Reign and AriZona Green Tea, which fit a column now that 16 oz cans count as 8 (#134).
3. The drinks that fit a column but add nothing in the projection.
4. The four whose package still needs a column count.

Load it once on Azure after `migrate`:

```
python -m vending.db migrate
python -m vending.request_backlog import db/product_requests/backlog-2026-11-15.json --by NAME
```

## For the dashboard

`vending.request_backlog.summary(conn)` returns everything a panel needs:
- `queued`: in order, with `why` and `est_gain_cents`
- `open_requests`: how many of ours are open
- `recent`: the latest filed or dropped items, each with its request's status and bridge ID
