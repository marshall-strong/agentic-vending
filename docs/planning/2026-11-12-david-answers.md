# David's answers to our questions, 12 Nov 2026

Kevin went through the questions in #102 with David in an 18-minute talk on 12 Nov. These notes come from a speech-to-text transcript, so they're paraphrased; where the transcript was unclear, the row says so. Answers that change code or docs are listed at the end.

## The answers

| # | Question (#102) | David's answer | Status |
|---|---|---|---|
| 1 | **Order cutoff** | **In-store purchases: Thursday at midnight.** **Amazon: no hard deadline.** What matters is that it arrives on campus in time to be loaded on Friday, so it depends on the shipping speed of the link we give (two-day, overnight). An Amazon order is placed as soon as the request goes through, so order early enough. | Answered |
| 2 | **Competition start** | **The machine goes live with our products on Thursday 3 Dec** (the first day of Winter Quarter), for a clean start. Before that, course staff stop selling, empty the machine and stock it from our requests. Each of our slots is filled **to full capacity** with the product we choose. We get the response like any purchase request. **We must have our day-one choices in by about 27 Nov** (the transcript says "the 27th"; confirm the exact day and time). | Answered; confirm the 27 Nov deadline |
| 3 | **Test stock and the $600** | What's in the machine now **doesn't count against us**. **The day-one stock we ask for comes out of the $600**: we start with $600 minus the cost of filling our slots. Costs are tracked from then on. David wasn't sure he'd understood the question, and said he'd check. | Answered; David to confirm |
| 4 | **What `fulfilled_at` means** | **The end of the visit.** The worker approves the request, goes out and buys, then fills in the receipt in the worker portal: what was paid, what was loaded where, what went to back stock, and anything broken in transit. They submit it **after loading**, and that sets `fulfilled_at`. Until then the request reads as pending. There's **no push or webhook**: we find out by polling the request by its ID. | Answered |
| 5 | **Shared-log deliverable** | **Not a separate deliverable.** Log what the agent decides and why in our own database. David will `pg_dump` both teams' databases for the final roll-up at the end of next quarter, so the reasoning has to be in there. | Answered |
| 6 | **Process map format** | Anything is fine; the tool doesn't matter. It's part of showing our infrastructure. | Answered |
| 7 | **Door times on the receipt** | **Likely yes.** The receipt would carry door-open and door-closed times: no sale of the new setup before the door opened, none of the old one after it closed. Not built yet. | Planned |
| 8 | **Faster reads around a visit** | Polling about every 10 minutes **won't break anything**. Hourly is probably fine, and he wouldn't want it much rarer. He named the exact gap we found: a poll at 1:00, a sale at 1:03, a restock with a product change at 1:10, and the 1:03 sale is read as the new product at the next poll. He'll think about how to close it. | Answered (allowed); the gap is open on his side too |
| 9 | **"Door open now" flag** | Not discussed. | Open |
| 10 | **Leave a slot empty** | **No.** It makes no sense to him; put something in it. | Answered |
| 11 | **Cancel a request** | **No.** Depending on where it is in the process, Amazon may already have been ordered. If one we don't want gets approved, we just don't use it. | Answered |
| 12 | **Open-request limit** | One open at a time today (a rejected one can be resent). David said he could set it up so we can send as many as we want. | Possible change |
| 13 | **Instructor halt** | **Not needed by the instructors.** A person reviews every purchase request, so the agent can't spend on its own. If something goes badly wrong (say it starts polling the bridge every 15 seconds), David deactivates our container, and we restart it once it's fixed. | Answered |
| 14 | **Visit time** | **A standard restock time: Fridays around 3 PM**, which keeps it away from the busy morning and the 1 PM class change. The student worker shops after class, so it may slip toward 4 PM. The receipt will still carry the door times. If a sale slips through, it can be found and fixed by hand. | Answered |
| 15 | **Unsold stock at the end** | Discussed earlier (19 Oct: at what we paid, David verifying). No new detail in the transcript. | As before |
| 16 | **Bottle deposit** | Not a concern. | Answered |
| 17 | **Promotions** | No bundles. | Answered |
| 18 | **Catalog shared?** | **Shared with the other team.** Everyone sees every product that could go in the machine, and which product requests were made and approved, but not the other team's plan or prices. A team's poll messages only ever carry its own slots. | Answered |

## What changes for us

- **Day-one order (urgent).** Our first real request fills all 20 slots to capacity, comes out of the $600, and is due about **27 Nov** for a **3 Dec** start. That moves the capital plan (#90), the restock policy (#91) and the first-products decision up front.
- **Competition start = 3 Dec.** That's when the ledger's opening should be recorded (#104), and the cutoff for whole-machine totals in the sales analysis (#84).
- **Cutover (#97, #105):** plan for a standard Friday visit at 3 to 4 PM. `fulfilled_at` marks the end of the visit, and the door times will narrow it when they're added. Our plan still handles a visit at any time. Polling about every 10 minutes around a visit is allowed.
- **Halt switch (#82):** keep it for our own use. The instructors' backstop is the human review of every request, plus deactivating our container.
- **Shared log:** our `action_log` and agent run records are the log. Make sure the agent's reasons are stored.
- **Integrity:** the shared catalog means we can see the other team's product requests. Per `docs/integrity.md`, we don't use them.
- **The API can't leave a slot empty or cancel a request,** so the planner's switch-only design is right, and the agent should treat a submitted request as final.
