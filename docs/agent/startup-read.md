# The startup read

The opening briefing is a trusted tool read, not a forecast: it shows where each number came from and how old it is, and never replaces the per-run balance check or preview.

The opening briefing comes from `get_briefing`, our logged tool. It meets the rules card's requirement that numbers come from tools. Its source labels are data, not instructions. The system prompt and rules card do not change with the snapshot.

Money uses the ledger's Saturday–Friday weeks in Eastern time. Each row shows its dates and day count. A week clipped by the opening or the current time is partial. The change is the current total minus the previous total, not a demand trend or a matched-day comparison. Competition totals begin at the ledger opening. API spend counts from that opening onward too.

Known cost totals can be incomplete. Gross and net profit stay unknown until sales, cost of sales and shrinkage are complete. Units sold across a swap the counters can't split show as an `ESTIMATE` line apart. That estimate is not in known cost of sales, gross or net. API share uses complete, positive gross profit. Otherwise it is unknown with a reason.

The slot table includes every assigned slot. A newer count takes precedence over receipt stock, even when its quantity is unknown. Photo counts are estimates. Counts over seven days old are weak evidence. Cover uses that effective stock and the observed sales rate, not a forecast. Stock age is time since the receipt movement or count. Poll age says when the sales evidence stopped.

Worker turnaround is the hours from our request to the worker's load, measured over the last ten fulfilled requests of each kind: median, longest, and how many took over 48 hours. The Project Brief (section 3.4) says operations tries to fulfil a request within 24 hours where it can, but it may take longer; `docs/restock-policy.md` plans a midweek visit on about two days, and the team expects 25 to 48 hours. So the briefing aims at 24 and plans on 48. An off-cycle request shows the worker's own speed. A regular one waits for the Friday visit, so its hours also depend on when we sent it. A pending off-cycle request is overdue after 48 hours, and a regular one once its Saturday–Friday week ends without the Friday load. Either raises `request_overdue`, because a late load changes stock and cover.

The tracking check line gives the newest check the bridge answered: the poll it was as of, when it was checked, and each of the four figures `within` or `OUTSIDE` the bridge's tolerance ($1 for money, 2 units for counts). The bridge never says by how much or which slot. Each figure read outside raises `books_outside_<figure>` with the tool to look at next. The line also says how many later sends got no verdicts.

The snapshot cannot replace a fresh balance check every run. Check purchase requests before another order. Preview the exact desired state before submitting. These checks remain required even when the briefing has no alerts.

The Markdown lists up to four closet rows and six pending request IDs, with explicit omission counts and exact tool hints. It never omits assigned slots. Long product labels are shortened for display. Full evidence remains in the typed reply and the pull tools. No receipts, free-form request notes, forecasts or price recommendations enter the opening.
