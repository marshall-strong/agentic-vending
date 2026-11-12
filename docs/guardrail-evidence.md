# Guardrail evidence, 12 Nov 2026

The brief's guardrails (Project Brief (Revised), section 4) must be "enforced in code ... not in a
prompt". Unit tests already cover the rules. This page records what happened when each guardrail
was tried **against the real system**: the Azure test database `ai-vend-2`, the live catalog from the
bridge, and the real agent. It backs the "controls" part of Summative 2.

**Setup.**

- The planner and tools ran from `main` at `ba14a6d`.
- The agent loop ran from PR #58 (`add_agent_loop`), which was then on the test branch `development`
  at `b1812e5`. It has since merged into `main`.
- The Azure database holds test data that will be reset before the live run: the ledger is open at
  $600.00 less $0.40 of model spend, and test request `pr_v8hB_h3gQatc` is pending.
- Nothing in these tests sent anything to the bridge's purchase route.

| Guardrail | Test | Result |
|---|---|---|
| Spend only what you have | A plan costing up to $635.00 against $599.60 spendable | Refused: `insufficient_balance` |
| Price bounds | $6.50 over the ceiling, $1.50 under the floor, $3.33 off the 5-cent step | Refused, one refusal per slot: `above_ceiling`, `below_floor`, `price_off_step` |
| Halt switch, by a person | The card's one-line SQL, run against a running agent | The run stopped about a second later, before its second turn; $0.02 spent |
| Halt switch, at the start | The agent started while halted | Recorded as `halted`; no model call, $0.00 |
| Halt switch, at the tools | A plan previewed while halted | Refused: `halted`, and nothing else is checked |
| Halt SQL fails safe | The row updated, deleted, and missing (throwaway database) | Halted in every case |

## 1. Spend only what you have

We asked `preview_desired_state` for ten 24-packs of Coca-Cola for slot 16, with every other slot
`keep: true`. `submit_desired_state` runs the same planner (`planning.plan`), so a plan refused here
can't be sent.

```text
success: False | message: Plan refused: 2 problems (insufficient_balance, request_pending).
  REFUSED insufficient_balance: max cost $635.00 (visit fee $0.00 included) exceeds the spendable
          balance of $599.60; buy less or cheaper
  REFUSED request_pending: request pr_v8hB_h3gQatc is still unresolved; wait for its receipts
  cost: expected 549.50, max 635.00, spendable 599.60, spendable_left_at_max -35.40
```

**The check uses the most the worker may pay (`max`), not the expected cost**, because the worker
buys at any price up to the maximum without asking again.

**Finding: the spendable figure doesn't set aside the pending request.** $599.60 is the whole
balance, although `pr_v8hB_h3gQatc` may still spend up to $270. Today that is safe only because
`request_pending` refuses any second request while one is open. If we ever allow two open requests
(a midweek top-up while Friday's is pending, say), the balance check must subtract open requests'
maximums first.

## 2. Price bounds

Three slots, each with one kind of bad price (all other slots `keep: true`):

```text
success: False | message: Plan refused: 4 problems (above_ceiling, below_floor, price_off_step,
         request_pending).
  REFUSED above_ceiling (slot 16): $6.50 is above the ceiling of $6.00
  REFUSED below_floor (slot 18): $1.50 is below the floor of $2.25; raise the price, or pick a
          cheaper store or pack (a max pack price below what the pack costs would leave the slot empty)
  REFUSED price_off_step (slot 25): $3.33 is not a multiple of 5 cents; pick a price ending in 0 or 5
```

**Celsius's floor of $2.25 is worked out, not typed in:** a $25.00 maximum for a 12-pack is $2.0833
a unit, times 1.08 for sales tax is $2.25, rounded up to the next 5 cents (`vending/pricing.py`). The
ceiling is `config.price_ceiling_cents` (default $6.00), assumption A10. Every problem is reported at
once, so the agent can fix them all in one go.

## 3. Halt switch

### Someone stops a running agent

`docs/halt-for-instructors.md` gives one line of SQL that needs nothing from the agent. It was written
for the instructors, but on 12 Nov David said they don't need a halt switch from us (issue #106), so
**we will be the ones to use it**; instructors may still use it. We tested exactly that line, run as
David's name, as if he had:

1. Halt off. We started a dry run of the agent (`python -m vending.agent`, `claude-opus-5-5` at
   `medium`, run 4).
2. As soon as the run's first tool results were stored (about 15 s in), we ran:

   ```sql
   update config set value = 'true', updated_at = now(), updated_by = 'david' where key = 'halted';
   ```
3. The agent checks the switch before every model request. Its record in `agent_runs`:

```text
run 4 | dry-run | claude-opus-5-5 | status halted
detail: The halt switch was turned on; stopped before turn 2.
started 06:34:24.97 UTC, halt set 06:34:39.99, finished 06:34:41.10 | cost $0.024956
steps:  1 thinking   "I'm starting by checking where things stand."
        1 tool_call  get_machine_state, get_purchase_requests, get_account_balance
        1 tool_result (the three answers)
```

It stopped 1.1 seconds after the switch was set. It had only read data, and spent 2 cents.

### Starting while halted

With the switch still on, we started another run (run 5):

```text
run 5: halted. The halt switch is on: nothing was read, called or sent.
status halted | cost $0 | 0 steps | no new row in model_calls
```

### Tools refuse while halted

While halted, the price-bounds plan from section 2 got a single refusal. The planner checks the
switch before it reads the bridge or the ledger:

```text
success: False | message: Plan refused: 1 problem (halted).
  REFUSED halted: the halt switch is on; stop and do nothing
```

Write tools (`submit_desired_state`, `request_catalog_product`) check the switch inside
`vending/write_gate.py` while holding a lock on its row. A write that started first finishes; one that
starts after the halt is refused.

### The SQL fails safe (throwaway database)

On a new `postgres:17` with migrations 001-011 applied, starting with the switch off:

| Action | `python -m vending.halt status` afterwards |
|---|---|
| `update config set value = 'true' ...` | `halted` (`UPDATE 1`) |
| `python -m vending.halt off --by test` | `running: writes allowed` |
| `delete from config where key = 'halted';` | `halted` |
| The same `update` with the row missing | `halted` (`UPDATE 0`, but a missing row already counts as halted) |

**Afterwards:** we turned the Azure switch back off with `python -m vending.halt off --by
claude-for-marshall`, as it was before the test.

## What these tests don't show

- **A halt doesn't recall a request already sent.** It sits with the instructors' purchaser, who can
  deny it (`docs/halt-for-instructors.md`, section 3).
- **A halt set by SQL isn't in `action_log`.** Only `config.updated_by` and `updated_at`
  record it. Our own CLI flips are logged.
- **Approved channel:** the only route to buy is `submit_desired_state`, which posts to the bridge's
  purchase route. That holds because no other tool can spend money, which these tests don't
  exercise.
- **Logging to the shared log:** the institute's shared log schema hasn't been issued (assumption A11),
  so every call goes to our own `action_log`.
