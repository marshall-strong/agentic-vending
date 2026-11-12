# DB explorer

A web page for looking at what is in our database, without psql, VS Code, or asking Claude to run SQL. When the page opens, it lists every schema and table. Choose a table to see:
- its **columns**: type, whether the column can be null, its default, and whether it is part of the primary key;
- its **rows**, 100 at a time, highest primary key first. For tables keyed by a numeric `id`, that is the newest rows first.

It changes nothing. It also knows nothing about vending: it shows only what Postgres says is there.

## Run it

```
python -m vending.db_explorer              # then open http://127.0.0.1:8766
python -m vending.db_explorer --port 9000  # if 8766 is taken
```

It reads whichever database `DATABASE_URL` points at in your `.env`:

- **Your local Docker database:** start it with `docker compose up -d db`.
- **The team's Azure database:** use the Azure `DATABASE_URL`. You need the same network access you'd need for psql.

Press Ctrl+C to stop it.

## What you see

| Value | Shown as |
|---|---|
| Null | *NULL*, in grey (an empty string shows as *(empty)*) |
| Long text or JSON | The first 200 characters. Click to see the rest, up to 20,000 characters. Postgres cuts anything longer before it is sent. |
| `bytea` | Its size only, for example *1,234 bytes* |
| `timestamptz` | Eastern time. Hover to see the stored UTC value. |
| Money and `numeric` | Exactly as stored, never rounded |

Each table shows its row count. If a table has more than 100,000 rows, the page says "more than 100,000" so it doesn't count them all. Paging stops after row 1,000,000: Postgres reads every row it skips, so a deeper page would load the database for nothing. Nothing in our database is that big.

## Why it is safe to leave open

- **It can't write.** The connection is read-only, so Postgres itself refuses any insert, update or delete, whatever the code asks for. Each page is one read-only transaction.
- **It can't hang the database.** A query that runs longer than 5 seconds is cancelled, and the page says so.
- **The URL can't run SQL.** A table name from the URL is used only if Postgres lists that table. Any other name gets a 404 and never reaches a query. There is no SQL box.
- **It never migrates.** If a table is missing, run `python -m vending.db migrate` yourself.
- **Run this way, it answers only `127.0.0.1` or `localhost`**, so another website can't read it through your browser. It uses the same rule as the agent dashboard.

## On the container

The Container App serves the same page to anyone at `/db` (#53), next to the agent dashboard at `/dashboard`. There it connects as a login that can only read, shares a few connections with the dashboard, and leaves out the error text of failed tool calls and failed runs, the other team's selections (`poll_selections` and `slot_counts` show only ours) and the hostnames in `model_calls`. [`deploy.md`](deploy.md#the-public-pages) has the rules and the setup.

Every link on the page is relative (`?schema=...&table=...`), so the page works under any path.
