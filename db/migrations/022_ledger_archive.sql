-- Ledger entries taken out of the books, kept rather than deleted (#215).
--
-- The team ledger holds a test opening from 11 Nov. `ledger_entries` allows one
-- opening (`ledger_entries_one_opening`), so the real one can't be recorded
-- beside it. `python -m vending.ledger reset-test-opening` moves the test
-- opening here in one transaction, and then `ledger open` records the real one.
-- Every reader of the opening reads `ledger_entries` only, so nothing reads
-- this table to work out a figure; it is the record of what was taken out.
--
-- `id` is the id the row had in `ledger_entries`. Numbered 022 because PR #204
-- uses 021; the migrator applies any file not yet applied.

create table ledger_entries_archive (
    id            bigint      primary key,
    at            timestamptz not null,
    kind          text        not null,
    amount_cents  numeric     not null,
    detail        text        not null,
    reference     text,
    recorded_by   text        not null,
    recorded_at   timestamptz not null,
    archived_at   timestamptz not null default now(),
    archived_by   text        not null,
    reason        text        not null
);
