-- Which selections are ours, as the inventory server told us (`GET /api/v1/slots`).
--
-- The project brief (section 1) says the assignment comes from the inventory
-- server, once, and is fixed for both quarters. So it is read from the bridge
-- and stored here by `python -m vending.slots record`, and every tool that reads
-- the database takes our slots from this row instead of a list in the code.
--
-- One row only. A later read that matches it just moves `last_checked_at`. A
-- read that differs is refused and changes nothing: the brief says the
-- assignment doesn't change, so a different answer needs a person to look.
--
-- Numbered 013 because 008-012 are taken on open branches; the migrator applies
-- any file not yet applied, so the order they merge in doesn't matter.

create table slot_assignment (
    id               smallint    primary key default 1 check (id = 1),
    team             text,
    device_id        text        not null check (device_id <> ''),
    slots            text[]      not null check (cardinality(slots) > 0),
    first_read_at    timestamptz not null default now(),
    last_checked_at  timestamptz not null default now(),
    response         jsonb       not null      -- the bridge's answer, as it came
);
