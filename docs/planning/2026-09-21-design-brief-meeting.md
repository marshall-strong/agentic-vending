# Design Brief meeting — decisions and answers

Monday 21 September 2026, with Graham Anthony and David Robinson. Extracted from the
meeting transcript (kept in the team's shared folder, not in this repo); timestamps are
`[mm:ss]` into the recording. Where the audio was garbled, the entry says so and gives our
best reading. Those items should be confirmed rather than built on.

## The headline changes

1. **Full sales history is available now, back to 28 August.** Two new endpoints,
   `GET /api/v1/history` and `GET /api/v1/history/{session_id}/raw`, are live and working
   with our key (verified after the meeting). The bridge stores **every** live poll from
   anyone, including ours, and David's own poller reads the machine every hour
   `[34:02]`. Reading history does not touch the machine.
2. **Purchasing is a purchase-order workflow with a human in it.** The agent submits a
   PO; a student worker buys it and restocks it; the result comes back through a status
   endpoint `[05:51]`, `[21:36]`, `[24:06]`. We don't place orders with a retailer
   directly.
3. **The restock report tells us what was loaded into each slot** `[22:48]`: "we
   purchased 24 of this item, 8 of them were loaded into slot A, the rest are in stock."
   So stock on hand = loaded quantity − units sold since. That largely solves the missing
   `PA1-08` product-level problem.
4. **There is no price floor or ceiling from them** `[28:40]`: "there's not really any
   rules between pricing, just whatever works." That's a change in reply to our question,
   but the project brief still requires price bounds as a guardrail, so **we set our own**.

## Answers to our questions

| # | Our question | Answer | Confidence |
|---|---|---|---|
| 1 | Retailer, lead time, order cutoff | **No fixed vendor**; it's the purchaser's discretion. Target or Walmart is easiest; Amazon works but you have to allow for delivery delay `[04:25]`. The PO is due by **Thursday midnight**; the worker buys and restocks on Friday `[19:00]`. The Friday timing still has to be confirmed with the worker. | High |
| 2 | Can the agent see the whole machine? | **This quarter: yes, everything** `[02:42]`. **Next quarter (the live run): restricted.** The audio suggests the agent will not see the other team's prices, possibly only some stock information `[03:01]`–`[03:31]`. | Medium: garbled, confirm |
| 3 | Price floor and ceiling | **None from them.** "Whatever works" `[28:40]`. Rules on product: it must be a drink, no alcohol, no THC; caffeine is fine `[28:50]`–`[29:28]`. | High |
| 4 | The $600: credit, card, or bank? | **Institute scrip ("Golisano bucks") on a ledger they keep.** The purchaser knows our balance `[10:32]`–`[11:08]`. We keep our own books too. | High |
| 5 | How does the agent learn its rows? | Not discussed. | Open |
| 6 | Group 1 or group 2? | Not discussed in the meeting. **Settled afterwards in class: Group A = group 1, Group B = group 2. We are group 2.** Readings for Discussion 1 are due Thu 1 Oct; we lead it Thu 8 Oct. Which of the two topics is ours is still open. | High (David, in class) |
| 7 | Restocker interface: ours or theirs? | **Theirs.** "Our worker is using the portal that we built" `[21:36]`. Kevin was right. | High |
| 8 | Does a midweek price change cost $25? | Not clearly answered in the recording (`[03:38]` is garbled). **Answered in class discussions: yes.** The worker does three jobs on a visit: add stock, remove a product, change prices (physical tags). Friday's visit is free; **any midweek visit costs $25**, whatever it's for. | High (class discussion, per Marshall) |
| 9 | NY bottle deposit | Not discussed. | Open |
| 10 | "Cost of goods ordered" vs sold, unsold stock at the end | **Unresolved:** "we'll get back to you on this" `[09:41]`. Team argued unsold stock is still an asset; Graham doesn't want to reward "$600 of Red Bull at the end." | Owed by them |
| 11 | Bulk buying and storing stock off-machine | **Allowed in practice.** Their example has 24 bought, 8 loaded, 16 held in stock `[22:48]`. How it scores is item 10. | Medium |
| 12 | Units per spot | **No fixed number.** Spots are slanted rails with no individual slots, so **as many as physically fit**, set by container size `[20:40]`. The loading chart (7 / 7 / 8) is "probably close to right." David offered to help test-fit new products `[25:11]`. | High |
| 13 | Reopen the machine to re-photograph the chart | Superseded by 12. David will help check fit for specific products. | Closed |
| 14 | Can operations decline or modify a PO? | **Yes, both.** The purchaser may find the price changed and decide, or deny a PO from a shady site. Either way they send a note back to the agent `[05:57]`–`[07:08]`. **Always include one or two alternates** `[17:26]`–`[18:18]`. | High |
| 15 | Compute: self-reported or metered? | Not discussed directly. They mentioned **fireworks.ai / open-weight models** as a way to get model access `[01:16]`–`[01:46]` (garbled). | Low: confirm |
| 16 | Visio for the process map | Not discussed. | Open |
| 17 | Are tools meant to be MCP servers? | Not discussed. | Open |
| 18 | Promotion or marketing | Not discussed. | Open |
| — | Repo public vs private | "Let me think about that. I think it's a fair question" `[27:05]`. | Owed by them |
| — | Historical data | **Yes:** the history endpoints (headline 1). Early history is sporadic test polls; hourly since 21 September `[34:13]`. | High, verified |

## What Graham and David owe us

- [ ] **Purchase order template/schema** and the endpoint to submit it, which returns a
  PO ID `[17:05]`, `[24:13]`
- [ ] **PO status endpoint**: pending, or processed with what was bought, at what cost,
  and what went into which slot `[24:25]`–`[24:44]`
- [ ] **Balance/cost check endpoint**: we send what we think our figures are; it says
  "accurate" or "off", with a tolerance around $0.25 `[11:38]`–`[12:39]`. It's a guide
  for this quarter, not an answer key.
- [ ] **Exact sales-tax method**: arithmetic rounding (not banker's rounding) is
  confirmed; still to come is per-sale vs aggregated `[13:15]`–`[14:21]`
- [ ] **Product table we can add to**, so both sides mean the same product `[29:54]`
- [ ] **Ruling on unsold inventory** at the end of the run (item 10)
- [ ] **Ruling on repo visibility**
- [ ] **Confirmation of the Thursday-midnight / Friday timeline** with the worker

## Other things said worth keeping

- **Deterministic pricing is encouraged.** "There could be a deterministic tool out there
  that helps you decide what the price is." The team replied that anything that can be
  deterministic should be, to save tokens `[00:33]`–`[00:39]`.
- **The trade-off of ordering early:** the earlier the PO, the staler the sales data it
  was based on `[04:57]`–`[05:21]`.
- **Sales tax, a discrepancy to resolve:** at `[16:18]` the example was spoken as "8% of
  that $3 goes to sales tax" ($0.24). The accounting lecture says the $3.00 is
  tax-inclusive, so the tax is $3.00 − $3.00 ÷ 1.08 = $0.22. The two methods differ on
  every sale. That's exactly what the owed tax method will settle, and we should
  **not** hard-code either until it arrives.
- **Prices must end in 0 or 5** ($2.00 or $2.05, never $1.63). This is a course rule from class discussion, not from this recording. It means the "undercut by a cent" idea at `[03:32]` isn't possible; the smallest undercut is 5 cents.
- **Novel and local drinks are allowed** if sealed, shelf-stable, need no assembly, and
  vend reliably, **and are cleared with them before ordering** `[31:07]`–`[33:28]`.
- On agents: "if you provide it with evidence that what it thought was going to happen
  didn't happen, it can reason its way around. How good it does is a different
  discussion" `[19:50]`.
