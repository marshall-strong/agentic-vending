# The weekly P&L report

A page that shows our profit and loss week by week, to read or print (issue #56). Open it in a
browser and use Print, or Save as PDF. It reads the ledger and never writes.

```
python -m vending.pnl_report              # http://127.0.0.1:8767
python -m vending.pnl_report --port 9000
```

You need the local database with the ledger opened, as in [`mcp-server.md`](mcp-server.md):
`docker compose up -d db`, `python -m vending.db migrate`, then
`python -m vending.ledger open --amount 600.00 --by NAME`. Without an opening entry the page says
how to record one.

## What it shows

One row per week. A week runs **Saturday to Friday, Eastern time**, the ledger's week: it turns
over after the Friday visit, and the sales tax is rounded per week (the team's 19 Oct decision).
The first and last weeks are cut to the range you chose.

| Column | Where it comes from |
|---|---|
| Units sold | Rises in our 20 slots' unit counters |
| Takings (incl. tax) | Rises in the sales counters, tax included |
| Sales tax set aside | Takings x 8/108, rounded half up per week |
| Revenue ex tax | Takings less the tax set aside |
| Cost of sales | The units sold, at weighted-average cost from the receipts |
| Shrinkage | The cost of units lost without a sale |
| Visit fees | Fulfilled midweek visits, $25 each unless the request stored another fee |
| Token spend | Model calls recorded in the ledger. $0 until the harness records them (#51) |
| Adjustments | Corrections recorded in the ledger |
| Profit | Revenue ex tax less cost of sales, shrinkage, visit fees and token spend, plus adjustments |

Under the weeks are the totals for the range, then the balance **now**, the sales tax still owed and
the spendable balance. Those three don't depend on the range. The notes from the ledger come last.

**The page computes nothing.** Each week comes from `ledger.pnl_by_week`, the totals from
`get_pnl`, and the balance from `get_account_balance`. The exact weeks add up to the exact totals.
Each figure on the page is rounded on its own, so a column can differ from its total by a cent.
The page says so under the table.

**Money is exact.** Figures stay as exact fractions of a cent until they are shown, then round
half up to the cent.

**Unknown stays unknown.** When a unit sold has no known cost (sold before we first loaded the
slot, or from a slot we never loaded), cost of sales and profit show "unknown", with how many
units were costed. The same goes for shrinkage with no known cost. `get_pnl` gives a figure that
leaves the unknown cost out; the page doesn't show that figure as profit.

## The date range

Choose a start and an end day, both Eastern and both included. Leave them empty for the whole
run: the opening entry to today. **Whole run** clears them. The form doesn't print.

Dates must fall between 2000-01-01 and 2099-12-31, and one page shows at most 157 weeks (three
years). A longer range is refused with a message.

## Safety

- It listens on 127.0.0.1 and answers only a Host of `127.0.0.1` or `localhost`.
- The session is read-only (`default_transaction_read_only`), so Postgres refuses any write.
- Each page is one repeatable-read, read-only transaction, so every figure comes from the same
  snapshot. A query that runs past 15 seconds is cancelled.
- It never migrates. Every value on the page is HTML-escaped.
