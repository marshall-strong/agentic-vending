# Memo: what our vending agent will ask of operations

**To:** Golisano Institute operations team
**From:** Group B (Sean Kelley, Marshall Strong, Kevin Sykes, Meaghan Gartland)
**Date:** 16 November 2026
**About:** Our 20 slots in the drinks machine, for the live run starting 3 December

## What the agent does

Once a week, our agent looks at the last week of sales from our 20 slots, what stock we hold, and how much money we have. It then decides, for each slot, what product should be in it, how many to load, and what price goes on the tag. It sends that to you as **one purchase request a week**.

The request is a plain list: for each slot, whether anything changes, the product, how many to load and the price. A shopping list follows, grouped by store, with a link and the most you may pay per pack. Its first test request, on 12 November, put new products in six slots: $233 expected and $270 at most, against a balance of $600.

The agent never touches the machine and never spends money itself. Everything goes through you.

## The limits it cannot break

These are enforced by our software, not left to the agent's judgment. A request that breaks one is refused before it reaches you.

- **It can't overspend.** It checks the *most* the order could cost, not the expected cost, against the money we have.
- **Prices stay within set limits.** No price below what the product costs us plus tax, none above $6.50, and always in 5-cent steps.
- **It can only order products on the approved catalog, and never loads more than a slot holds.** The rest of a pack goes to our closet.
- **One request at a time.** It can't send a new request while one of ours is still waiting with you.
- **It can be switched off.** See "How to stop us" below.

On top of ours, the institute's system accepts prices only up to $10.00 in 5-cent steps, and your purchaser reviews every request before anything is bought.

## What we ask of operations, and how often

| When | What |
|---|---|
| Mon 30 Nov, once | Buy and fill each of our 20 slots to capacity from our day-one request, which we send by 23:59 on Sunday 29 November. |
| Once a week | Review our request, which we send by Thursday midnight for the Friday visit. Approve it, or deny it with a short reason (the agent reads the reason next week). |
| After approval | Buy what the shopping list says, at the named store, at or under the most-per-pack price. |
| Friday visit | Load each slot as listed, change the price tags, and put any extra units in our closet. |
| After the visit | Report what happened: units loaded, units to the closet, price paid. Our records of what was loaded and what went to the closet change only from this report. |

A midweek visit costs us $25, so the agent will ask for one only when it has a reason to.

**If an Amazon delivery hasn't arrived by the visit,** please load it at the next scheduled visit and use the prices on the request. **If a slot is switching to a new product,** take out whatever is in it and put it in our closet. We plan to add both instructions to the request itself (see below).

## How to stop us

You don't need the agent's cooperation to stop it. Either of the first two works on its own:

1. **Deny the request** in the purchaser portal. Nothing is bought.
2. **Ask us or an instructor to turn on our halt switch.** It takes one step for anyone with access to our database. We tested it on 12 November: the agent stopped 1.1 seconds later, having spent two cents. The switch does not recall a request already with you, so deny that one as well.
3. **Switch off our software** on the institute's cloud account. Today this stops only our hourly data updates; the agent itself doesn't run there yet, so use 1 or 2 to stop it.

Please tell us why you stopped us, so we can fix the cause before turning it back on.

## What we recommend before the live run

1. **Tell us if the institute wants a different price ceiling** from our $6.50.
2. **Decide how leftover stock is valued** at the end of the run, and how the bottle deposit is handled, so every team is scored the same way.
3. **Confirm how and when the machine is observed** (counts or photos), so we know what stock is really in each slot.
4. **Let us fix three things in our requests first:** product names instead of internal codes, a clear "remove what's there" instruction when a slot changes product, and a short note field for things like late deliveries.

Questions about this memo: any of the four of us.
