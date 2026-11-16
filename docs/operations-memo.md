# Memo: what our vending agent will ask of operations

**To:** Golisano Institute operations team
**From:** Group B (Sean Kelley, Marshall Strong, Kevin Sykes, Meaghan Gartland)
**Date:** 16 November 2026
**About:** Our 20 slots in the drinks machine, for the live run in December

## What the agent does

Once a week, our agent looks at the last week of sales from our 20 slots, what stock we hold, and how much money we have. It then decides, for each slot, what product should be in it, how many to load, and what price goes on the tag. It sends that to you as **one purchase request a week**.

The request is a plain list: for each slot, whether anything changes, the product, how many to load and the price. A shopping list follows, grouped by store, with a link and the most you may pay per pack. Its first real request, on 12 November, asked for six slots to be refilled: $233 expected and $270 at most, against a balance of $600.

The agent never touches the machine and never spends money itself. Everything goes through you.

## The limits it cannot break

These are enforced by our software, not left to the agent's judgment. A request that breaks one is refused before it reaches you.

- **It can't overspend.** It checks the *most* the order could cost, not the expected cost, against the money we have.
- **Prices stay within set limits.** No price below what the product costs us plus tax, none above our ceiling, and always in 5-cent steps.
- **It can only order products on the approved catalog**, and no more than a slot holds.
- **One request at a time.** It can't send a new request while one of ours is still waiting with you.
- **It can be switched off.** See "How to stop us" below.

On top of ours, the institute's system accepts prices only up to $10.00 in 5-cent steps, and your purchaser reviews every request before anything is bought.

## What we ask of operations, and how often

| When | What |
|---|---|
| Once a week | Review our request. Approve it, or deny it with a short reason (the agent reads the reason next week). |
| After approval | Buy what the shopping list says, at the named store, at or under the most-per-pack price. |
| Friday visit | Load each slot as listed, change the price tags, and put any extra units in our closet. |
| After the visit | Report what happened: units loaded, units to the closet, price paid. Our records change only from this report. |

A midweek visit costs us $25, so the agent will ask for one only when it has a reason to.

**If a delivery arrives late,** please load it at the next visit and use the prices on the request. **If a slot is switching to a new product,** take out whatever is in it and put it in our closet. We are adding both instructions to the request itself (see below).

## How to stop us

You don't need our help to stop us. Any one of these works on its own:

1. **Deny the request** in the purchaser portal. Nothing is bought.
2. **Turn on our halt switch.** Our written instructions (`docs/halt-for-instructors.md`) explain it in one step. We tested it on 12 November: the agent stopped about a second later, having spent two cents. The switch does not recall a request already with you, so deny that one as well.
3. **Switch off our software** on the institute's cloud account.

Please tell us why you stopped us, so we can fix the cause before turning it back on.

## What we recommend before the live run

1. **Set the price limits for the run.** We use our own ceiling until the institute gives us one.
2. **Agree an ordering cutoff.** Our 12 November request went in after the Wednesday deadline for Friday delivery. A fixed day and time would let us plan around it.
3. **Decide how leftover stock is valued** at the end of the run, and how the bottle deposit is handled, so every team is scored the same way.
4. **Confirm how and when the machine is observed** (counts or photos), so we know what stock is really in each slot.
5. **Agree a shared log of visits and requests**, so both sides read the same record.
6. **Let us fix three things in our requests first:** product names instead of internal codes, a clear "remove what's there" instruction when a slot changes product, and a short note field for things like late deliveries.

Questions about this memo: any of the four of us.
