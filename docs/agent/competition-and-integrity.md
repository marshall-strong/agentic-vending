# Competition and integrity

Our slot assignment is private; we may not know which other slots are a rival's; the team has chosen to ignore other slots' prices, though the brief calls them a pricing input.

**What the brief says.**

- **The assignment is random, fixed and private** (Project Brief (Revised), section 1). The inventory server tells each team which locations are theirs. In practice the instructors change it exactly once, through the server, before the competition starts on Thu 3 Dec 2026, and that assignment is fixed from then on (David, 16 Nov). Our slots are whatever the stored server answer says (`vending.slots.our_slots`); the tools read it for you. It is kept private so nobody can influence the competition from outside it, for example by steering customers toward or away from a row because they know whose it is.
- **We may not know which other slots are a rival's** (section 1), only that they are not ours. The brief asks each team to write down what it assumes it can and cannot infer about the competition from what it sees. Ours is A14 in `docs/assumptions.md`.
- **The other row's price is an input** (section 3.2). At least one other agent prices substitutable products nearby, and the brief says that price is "an input to your pricing logic".
- **The leaderboard is visible** (section 6). Expect other teams to react to it, and expect to be tempted to react to them. Log both.

**What the instructor said in class.** Teams must act with integrity and must not change strategy based on the other team's slots, pricing or strategy.

**The two sources conflict, and the team has chosen.** The brief treats the other row's price as a pricing input; the instructor said not to base strategy on the other team's pricing. The team follows the instructor: we ignore other slots' prices and should not need them to win (team decision, 11 Nov; the team may revisit it).

**So:**

- Do not try to work out which slots belong to the other team.
- Apart from fall data, never use another selection's price, sales or stock in a decision. Price from our own costs, sales and stock.
- Fall data is the exception. Until the fall cut (18 Nov 2026 unless the team moves it; the team keeps it before the day-one fill, and it is never after the 3 Dec start), course staff stocked and priced the whole machine, so no selection is either team's. Data from polls taken before the cut may be used for all 40 selections, for example for day-one sales rates and reference prices: those prices were the course staff's, not a team's. What counts is when the poll was taken, not when you read it. From the cut on, selections outside our assignment are off-limits: their sales, prices and stock.
- Never try to steer customers toward or away from any slot except through our own prices and products.

**This is about the other team in our machine.** The Interloper, the outside seller's machine beside ours, is different: its prices may be used (see `interloper`).
