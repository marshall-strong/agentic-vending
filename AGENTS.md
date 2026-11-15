# Instructions for AI coding tools

Read [`docs/integrity.md`](docs/integrity.md) before working in this repo. In short:

- Never look for, open or use the other team's dashboard, repo, container, data or strategy.
- Our pricing does not take the other team's prices as an input (the team's current choice; see the doc).

Our slots are the inventory server's assignment, stored by `python -m vending.slots record` and read through `vending.slots.our_slots`. It may change until the competition starts on 3 Dec 2026. Never hard-code a list of selections, and never describe our slots as particular columns.

If Docker hangs, run `scripts/docker_recover.ps1` rather than restarting things by hand. For
throwaway containers, use the label and random port in [`docs/docker-recovery.md`](docs/docker-recovery.md).
