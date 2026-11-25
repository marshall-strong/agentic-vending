# Development spend

The dashboard shows recorded API spend against the team's $85 development cap.
It lists the date of the first charge in the report. Set
`dev_spend_cap_cents` in the database config to change the cap. It does not stop calls.

Development ends at the real ledger opening. The 11 Nov test opening does
not end it. Before the real opening, the Spendable cell shows development
spend. After it, the cell shows the competition balance.

```sh
python -m vending.dev_spend
python -m vending.dev_spend --daily
python -m vending.dev_spend --json
python -m vending.dev_spend --html dev-spend.html
python -m vending.dev_spend --demo --html demo.html
```

Reports read `DATABASE_URL` in read-only mode. Weeks run from Saturday to
Friday in Eastern time. The HTML file works offline. The demo uses run notes.

## What counts

| Category | Source |
| --- | --- |
| Agent runs | Harness calls and older ledger token entries |
| Price search | Search calls and lookup rows without a matching harness call |
| Simulations/bench | Bench and sweep calls and scratch CSV imports |
| Manual | Amounts entered from the provider console or another record |

Price memory stores a rounded copy of a call cost. The report matches old
lookup rows to harness calls by the search label, cost and time. It keeps
the harness charge when it finds a match.

An unconfirmed charge can include the worst-case estimate. Refused calls
cost nothing. Tests after the real opening appear in a separate warning.
Development spend stays outside the competition's $600.

## Record scratch spend

Point `DATABASE_URL` at the team database with write access. Apply the
database migrations before you record spend.

```sh
python -m vending.dev_spend record --from sim-out/rehearsal --label "Opus rehearsal" --by kevin
python -m vending.dev_spend record --usd 1.23 --label "Console adjustment 25 Nov" --by kevin --unconfirmed
```

Use a simulation directory or its `model_calls.csv`. The simulator and bench
print the import command. Skip the import for work already in the team database.

The import uses the time of recording by default. Use `--at` with an ISO 8601
timestamp and a UTC offset to backfill the actual spend date. The simulated
dates in the CSV describe the season.

Reuse the label for repeat imports of an older CSV. Give each separate run
a unique label. A manual label identifies one receipt per recorder. Use
`--unconfirmed` if the amount is an estimate.

## Record paid runs

For a paid run on a scratch database, set `SPEND_DATABASE_URL` to the team
spend database with write access. A run on the team database can use its
writable `DATABASE_URL`. The Container App uses the writer secret for both. The harness records the worst-case charge there before it sends
the request. It updates the charge after the reply. Free scripted runs use
their scratch database. The CLI keeps the estimate if settlement fails.

The ledger counts live charges from the real opening against the $600.
The report lists any tests after that opening separately.
