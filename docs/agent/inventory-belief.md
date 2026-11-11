# Inventory belief

Stock on hand is an estimate with uncertainty, built from restocks as reset points; demand is censored when a slot runs out, and stockouts shift demand to substitutes.

**Only one thing is guaranteed** (Project Brief (Revised), section 3.1): the message saying which slots are ours. It arrives once and does not change. Any other input (camera, sales signal, restock record, manual count) may or may not exist. Do not assume vision.

**Design for these** (section 3.1):

- **Noisy, partial signals.** No input is ground truth. Say how sure you are.
- **Censored demand.** When a slot empties before the next restock, the sales it missed are invisible. Units sold is a floor on demand, not demand. Treating units restocked as units demanded biases estimates low, worst at low prices, where slots empty soonest.
- **Restocks are reset points.** At a restock we know exactly what went in. Estimate cycle by cycle, from one restock to the next, not as one long series.
- **Stockouts cause substitution.** When our slot and a similar slot empty at different times, one's stockout inflates the other's apparent demand. The similar slot may belong to a team we cannot name.

**How this works for us** (assumption A13 in `docs/assumptions.md`, waiting on the instructional team to confirm the observation setup). We assume the machine never reports stock on hand. Stock is units loaded (from restock receipts) minus units sold (from the machine's sales counters). Get slot state from `get_machine_state`, sales over a window from `get_sales_history`, and receipts from `get_purchase_requests`. If a slot sat empty for part of a period, treat its demand for that period as unknown, not as what it sold.

**When data is missing or stale**, say so in the reason and prefer the cautious action. `get_machine_state` flags a stale poll.
