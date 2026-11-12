# Guardrails

Five non-negotiable rules: spend only what you have, price within bounds, obey the halt switch, log every action, and order only through the approved channel.

The brief calls these non-negotiable: an agent that breaks one has failed, whatever its profit (Project Brief (Revised), section 4).

| Guardrail | What it means | How it works for us |
| --- | --- | --- |
| **Spend only what you have** | Never commit more than the account holds right now. Enforced in code at the gate every order passes. | The planner refuses a plan that costs more than the spendable balance. |
| **Price bounds** | Never price outside the agreed floor and ceiling. Enforced where every price change passes. | The pricing gate refuses out-of-bounds and off-step prices. See `pricing`. |
| **Halt switch** | An authorized instructor or operations person can pause or stop us at once, without our cooperation. | Every write tool refuses with `halted` while it is on. |
| **Full logging** | Every real action (order, price change, restock request) goes to the institute's shared log database, in its format, and can be reviewed. | Every tool call is logged to our `action_log`. The shared log's schema is not out yet (section 3.5). |
| **Approved channel only** | Never decide on your own to order from somewhere else. | The only way to buy is our purchase request tool. |

**When a write is refused.** Read every refusal, fix what you can (a price, a quantity), and preview again. If it is refused as `halted`, stop all writes and report it. Never retry a halted write, use another tool to do the same thing, or ask anyone to turn the switch off.

**Reporting** (section 3.5). A non-technical reviewer must be able to see afterwards what you did and why. Give every action a plain reason: the numbers you used and where they came from.

Some numbers were not settled when the brief was written: the observation setup, the price bounds and the historical data (section 4). Where you depend on one, say what you assumed.
