# Pricing

Prices stay within the floor and ceiling, must be explainable, may need deliberate variation to learn demand, and come from our own costs and sales only.

**The rule** (Project Brief (Revised), sections 2, 3.2 and 4). Set each product's price within the "agreed floor and ceiling", with logic you can point to and explain. The bound is enforced in code where every price change passes, not by you remembering it.

**Our floor and ceiling.** The instructors have given no numbers, so the team set its own. These are unconfirmed: the brief lists price bounds as not settled yet, and A10 in `docs/assumptions.md` waits on the instructional team to confirm them or give others. The ceiling is a config value (default $6.00; the bridge refuses anything over $10.00). Each slot's floor comes from cost plus tax, rounded up to the next 5 cents. Prices go in **5-cent steps**: $2.05 is legal, $1.63 is not. `preview_desired_state` shows each slot's floor and refuses a price outside the bounds. Never guess a floor; read it from the preview.

**The identification problem** (section 3.2). We set prices and observe the sales that follow. If we only ever set the price we already think is best, we never learn how demand responds to price. Deliberate price variation teaches us something but costs margin while we test. Decide whether to vary, and say so in the reason.

**Not a monopolist** (section 3.2). At least one other agent prices substitutable products nearby, and the brief calls the other row's price an input to pricing. **The team follows the instructor's in-class rule instead: never use another slot's price** (see `competition-and-integrity`).

**Timing.** A price changes only when the worker changes the tag at a visit: free on Friday, $25 midweek. Price for the week ahead, not the next hour.

**Write down the reason.** Every price change says why: the demand evidence, the cost, and any assumed elasticity and how it could be wrong (section 3.2).
