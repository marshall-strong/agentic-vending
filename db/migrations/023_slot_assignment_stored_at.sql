-- When the current slot assignment was stored, not when the server was asked for it (#216).
--
-- `python -m vending.slots record` asks the bridge, then stores the answer. `first_read_at` is when
-- it asked, so an agent run that starts while the request waits looks newer than the assignment,
-- though it only ever saw the old slots. `stored_at` is the database clock when the row was written
-- or changed (`vending.slots._store`), and `vending.slots.assigned_since` takes the later of the two,
-- so that run's notes are left out.
--
-- Null for a row written before this migration or by hand (the tests, the demo): then
-- `first_read_at` alone counts, as before. Numbered 023 because two open branches use 022.

alter table slot_assignment add column stored_at timestamptz;
