-- Which slot each submitted plan named as the wildcard (#110 item 1).
--
-- The wildcard is a testing ground: a product on trial there is judged against
-- our other slots and promoted into one if it earns more per in-stock class
-- day (`docs/agent/wildcard.md`). A trial priced at or under the ceiling looks
-- like any other slot in the polls, so the slot the plan named is kept here,
-- and `get_machine_state` reads the latest accepted one to find the trial.
-- Null: the plan named no wildcard, so no trial is running.
--
-- Numbered 019: 016 and 018 are add_visit_counting, 017 add_request_backlog.

alter table purchase_submissions add column wildcard text;
