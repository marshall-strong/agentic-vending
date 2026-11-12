# Money

The account balance is the only spending limit: $600 once, sales flow back in, and fees and agent costs come out of the same account.

**The account** (Project Brief (Revised), section 3.3):

- We start with **$600.00** of opening capital, once. Nobody tops it up.
- Every unit sold credits the account, so sales revenue can be spent again.
- The balance is the spending cap. There is no per-week budget, no credit and no overdraft.
- Overhead comes out of the same account: the $25 off-cycle dispatch fee and the cost of running you.

**Get the balance from `get_account_balance`, every time.** Use the `spendable` figure: the balance less the sales tax still owed. Never use a figure you remember. The brief says a team must always know what the account holds and how it got there, and that an order refused for insufficient funds is our own failure (section 3.3).

**Why timing matters** (section 3.3):

- **Capital has timing.** Money comes back only as units sell. A high price with slow sales and a low price with fast sales can give the same margin but very different cash on hand.
- **A stockout costs twice.** We lose the sale and its demand signal, and we lose the cash that sale would have returned for the next restock.
- **The $25 fee competes with stock.** A midweek dispatch is $25 of product we did not buy.
- **Agent cost is working capital too.** Expensive reasoning spends money that would have bought stock.

**The opening purchase** (section 3.3). The first order is made before any sale, on priors alone. How much of the $600 to commit before there is data, and how much to hold back, is a team decision. The brief says both extremes can be defended and neither is free. Follow the team's opening capital plan. If there is none, say so rather than invent one.

`get_pnl` gives profit and loss over a window: revenue ex tax, cost of sales, fees, token spend and net.
