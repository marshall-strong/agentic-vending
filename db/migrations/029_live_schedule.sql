-- Live scheduled agent runs (#213): `agent_schedule` may now hold a `live` run, and its `submitted` ending.
--
-- Migration 024 allowed only dry runs. A scheduled run is live only when the container's
-- AGENT_SCHEDULE_MODE and `config.agent_schedule_mode` both say `live`, from the competition's start
-- (`vending.start.START`), and only after its checks before a send pass (`vending/agent_schedule.py`).
-- A run claimed as live that a check turns into a dry run is stored as `dry-run`, with why in `detail`.
-- A live run calls `vending.agent.run(live=True)`, so it sends at most one purchase request; when it
-- does, its ending is `submitted` and the request is in `agent_runs.request_id` (through `run_id`).
alter table agent_schedule
    drop constraint agent_schedule_mode_check,
    add constraint agent_schedule_mode_check check (mode in ('dry-run', 'live', 'off')),
    drop constraint agent_schedule_outcome_check,
    add constraint agent_schedule_outcome_check
        check (outcome in ('running', 'submitted', 'skipped', 'halted', 'failed', 'missed'));
