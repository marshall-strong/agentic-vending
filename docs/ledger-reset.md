# Resetting the ledger for the real opening (29 Nov)

The team ledger already has an opening entry from testing: entry 1, "opening capital (test state,
11 Nov; to be reset before launch)". The ledger allows one opening, so `ledger open` refuses the
real one while the test one is there (#215). This page says how to move the test opening out of
the way and record the real one.

**When:** Sun 29 Nov, by 23:59 Eastern, before the day-one fill request goes in. The command refuses
from 00:00 Eastern on Mon 30 Nov (`RESET_CLOSES`). Staff buy and
stock on Mon 30 Nov. An opening recorded after the fill's receipt leaves the fill out of our books
([20 Nov notes](planning/2026-11-20-david-update.md)).

**Who:** one team member, from a laptop with `DATABASE_URL` pointing at the team database (as in
[deploy.md](deploy.md)). Kevin, unless the team names someone else. One person runs every step, in
order, so nothing runs twice.

## What the reset changes

`python -m vending.ledger reset-test-opening` moves the test opening from `ledger_entries` to
`ledger_entries_archive` (migration 022), in one transaction, and logs it in `action_log`. Nothing
is deleted. Nothing else moves. Once the real opening is recorded:

- **Model calls** before it, such as run 2's on 12 Nov, are development spend. They show on the
  [dev-spend page](dev-spend.md), not in the balance (#104).
- **Receipts and midweek visits** before it, such as the 12 Nov test purchase
  (`pr_v8hB_h3gQatc`), are test purchases. They are not in the balance, the P&L or stock
  (`stock.receipts_count_from`).
- **Takings** count from the last poll at or before it.

Every reader of the opening (the balance, the P&L, the money chart, the dashboard's money panel,
`get_account_balance`, `get_pnl` and the dev-spend page) reads `ledger_entries`, so they all move to
the new opening together.

**It moves one entry only:** entry 1, dated 11 Nov (Eastern), with the detail "opening capital
(test state, 11 Nov; to be reset before launch)" exactly (`TEST_OPENING_ID`, `TEST_OPENING_DATE`,
`TEST_OPENING_DETAIL` in `vending/ledger.py`). It doesn't look for the word "test", so the real
opening is never moved, whatever its detail says.

**It refuses**, and changes nothing, when any of these is true:

1. The competition has started (`vending/start.py`, `START`).
2. It is 00:00 Eastern on Mon 30 Nov or later (`RESET_CLOSES`). The fill can be stocked that day, so
   an opening recorded then could fall after the fill's receipt.
3. The archive already holds a reset. The reset runs once, even if a row like the test one comes
   back.
4. There is no opening.
5. The opening isn't that exact entry.

It reads these guards inside its transaction, after taking a lock and with the opening row locked,
so two people running it at once can't both move something. After its last write it reads the
database clock again, still inside the transaction. A write can wait on a lock another connection
holds. If 00:00 Eastern on Mon 30 Nov passes while it waits, the reset refuses with the same
"window closed" message and nothing changes.

## The steps

1. **Migrate**, so the archive table exists. Use the owner login, as for any migration:

   ```
   python -m vending.db migrate
   ```

2. **Dry run.** It changes nothing and prints what it would do. Check it names entry 1, the test
   opening, and nothing else:

   ```
   python -m vending.ledger reset-test-opening --by NAME
   ```

3. **Reset**, then go straight to step 4. Between the two, the ledger has no opening, and the agent
   and the dashboard say so:

   ```
   python -m vending.ledger reset-test-opening --by NAME --yes
   ```

4. **Record the real opening.** The amount is the $600.00 the brief gives
   ([opening capital plan](opening-capital-plan.md)). Leave out `--at`: the opening is now.

   ```
   python -m vending.ledger open --amount 600.00 --by NAME
   ```

5. **Check.**
   - `python -m vending.ledger show`: opening capital 600.00 dated today, receipts 0.00, token
     spend 0.00, and a note that the earlier model calls are development spend.
   - `python -m vending.ledger entries`: the new opening is in the list, and the test opening is
     under "Archived, not in the books".
   - `python -m vending.dev_spend`: run 2 is in development spend, and development ends at the
     new opening.
   - The dashboard's money panel shows the new opening date.

Then the fill request can go in.

## If something goes wrong

- **Step 2 or 3 refuses:** read the message. It names the guard. Don't edit the database by hand.
  Ask the team.
- **Step 4 fails after step 3:** the ledger has no opening. Fix the cause (it names it) and run
  step 4 again. The test opening is safe in the archive.
- **The wrong amount went in at step 4:** the reset refuses the real opening, by design. Record
  the difference as an adjustment with a detail saying why, or ask the team before anything else.

## If the window closed

From 00:00 Eastern on Mon 30 Nov the command refuses. Don't edit the database by hand. What to do
depends on how far the steps got:

- **Step 3 ran, but step 4 didn't, and the fill's receipt is in:** record the real opening dated
  before the receipt, so the fill counts. Give the time with its offset (a time without one is read
  as UTC):

  ```
  python -m vending.ledger open --amount 600.00 --by NAME --at 2026-11-29T23:00:00-05:00
  ```

  Then do step 5. The opening's date decides which receipts, visits and model calls count, so check
  that `ledger show` lists the fill's receipt.
- **Step 3 never ran:** the 11 Nov test opening is still the opening, so the books count from
  11 Nov. Run 2's model spend is in the balance, and the 12 Nov test receipt is in purchases and
  stock. Ask the team before anything else. A fix then is a person's decision, recorded in the
  ledger with a detail saying why, not a reset.

## Example

The CLI on a scratch database seeded like the team ledger (a test opening on 11 Nov, two model
calls, one test receipt). The CLI prints times in Eastern.

```
$ python -m vending.ledger reset-test-opening --by kevin
Dry run: nothing was changed. With --yes this moves one entry out of the books, into ledger_entries_archive:
     1  2026-11-11 14:00  opening         600.00  opening capital (test state, 11 Nov; to be reset before launch)  by marshall
Nothing else moves. Once the real opening is recorded, these fall before it:
  model calls since 2026-11-11: 2 entries, 0.48 spent. Development spend: on the dev-spend page, not in the balance
  receipts since 2026-11-11: 1 lines, 12.37 paid. Test purchases: not in the balance, the P&L or stock
  midweek visits since 2026-11-11: 0, 0.00 in fees. Not in the balance or the P&L
To make the change: python -m vending.ledger reset-test-opening --by kevin --yes
```
