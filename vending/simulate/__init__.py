"""A full simulated competition: the real agent, tools, planner, stock and ledger, on a made-up machine.

    python -m vending.simulate --database-url postgresql://...@127.0.0.1:PORT/sim --decider scripted
    python -m vending.simulate --database-url ... --decider model --budget 9.50

Everything the agent touches is our real code: `vending.agent.run` drives the model, the MCP server's
tools answer it in-process, `preview_desired_state` and `submit_desired_state` run the planner, polls go
in through `sync_history.store`, receipts through `sync_requests.sync`, and the money comes from
`vending.ledger`. Only three things are made up, and each lives in this package:

- **the world** (`world.py`): the calendar, the products, what customers buy, and the machine itself;
- **the bridge** (`bridge.py`): a stand-in for the DEX bridge that files our requests, and a student
  worker who turns each accepted request into receipts at the visit;
- **the clock** (`clock.py`): simulated time for the tools, the planner and the ledger, plus tripwires
  that raise if anything tries to reach the real bridge or the network.

It must only ever run against a scratch database: `season.check_scratch` refuses any URL that isn't
on this machine. See `docs/simulations.md` for what it assumes, how to run a season, and what is kept in git.
"""
