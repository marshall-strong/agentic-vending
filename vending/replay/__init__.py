"""The week replayer: test a decision-maker on weeks that have already happened.

1. `state.replay_week` rebuilds a past week from stored polls.
2. A decider (`deciders.py`) turns that week into a plan for each slot.
3. `scoring.py` runs the hard checks on the plan, then scores it against what
   actually sold the following week.

    python -m vending.replay --week 2026-10-02 --decider baseline
    python -m vending.replay --cases --decider baseline

No model calls here, and nothing writes: the replay connection is read-only.
See docs/week-replayer.md.
"""
