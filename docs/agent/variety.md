# Variety

Each slot should earn its own sales: a second slot of the same drink adds stock, not sales; keep testing new drinks, and lean toward a new category or brand.

These are Kevin's rules of 22 Nov (#230). They are principles, not counts: the catalog keeps growing, and you add products during the run. None of them is a refusal. Break one when the evidence says to, and say why in `slot_reasons`.

**Duplicates add stock, not sales.** Buyers who want a drink find it in one slot. A second slot of the same drink earns its place only when the first would sell out before the Friday restock, so that a buyer would find it empty. Show that with sales: the slot ran out before Friday, or its sales per class day times the class days to the next visit is more than the column holds. Otherwise give the slot a different drink. The preview warns when two of our slots get the same product ("adds stock, not sales"). Read the warning and either change the plan or give the reason in `slot_reasons`.

**No duplicates on the first fill.** On day one we have no sales of our own, so nothing shows a slot selling out. Every slot gets a different drink.

**Mixed-flavor slots** are allowed when you judge them worth it, for example two flavors of one line that each sell too slowly to fill a slot alone.

- All flavors in the slot share one price.
- The request's note lists each flavor's units for that slot.
- The slot's sales count as demand for the mix. The buyer still chooses whether to buy from that slot, but its counter can't tell the flavors apart, so you lose per-flavor data there.
- Your plan can't express one yet: `preview_desired_state` takes one product per slot, and you can't write the request's note. So plan the slot as one flavor, and propose the mix in the `update`: the slot, each flavor and its units, the shared price, and why. The team decides how to send it.

**Testing new drinks.**

- One regular slot always rotates new drinks, besides the wildcard (`wildcard`). You may open a second test slot, and you may swap any weak slot (`restocking`, "Slow sellers").
- Check every week. Drop a test drink early only if it clearly trails the slots around it in sales per in-stock class day. Otherwise give it a second week.
- Weeks in the Winter Holiday Break don't count as test weeks: the machine sells nothing then (class days are in `restocking`).
- Requests are due Wednesday for the Friday restock (Amazon items by Wednesday 23:59), so each decision rests on about three class days of the new drink's sales. Treat a small difference as unclear, not as a loss.

**When you add a drink, lean toward a new category or brand** over another flavor of a brand we already stock. Close line extensions take most of their sales from the flavors next to them (Ipsos 2011, https://www.ipsos.com/en-us/are-you-confident-your-line-extension-will-pay-out). A flavor is still fine when our sales support it, for example when that line's slots sell out, or the flavor is a known best seller where the category is thin.

**What still decides.** Profit per slot. Variety is how we find more of it, not a goal of its own.
