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

-- The reset closes at 00:00 Eastern on Mon 30 Nov (`vending.ledger.RESET_CLOSES`), when staff can stock
-- the day-one fill. The reset checks the clock in Python, but a pause after its last check could still
-- commit after the cutoff. So the database checks too, at commit: a deferred constraint trigger refuses
-- an archive row whose transaction commits at or after the cutoff. It reads `clock_timestamp()`, the
-- time at commit, not `now()`, the transaction's start. The reset refuses to run inside a caller's
-- transaction, so no caller can hold the archive row past the cutoff (or check it early with
-- `SET CONSTRAINTS ALL IMMEDIATE`) and commit later.
--
-- The cutoff is fixed here and reads nothing else, so no role that can write a table can move it.
-- Tests replace this function in their own disposable schema (tests/test_ledger_reset.py).

create function ledger_reset_closes() returns timestamptz language sql stable as $$
    select timestamptz '2026-11-30 00:00:00 America/New_York'
$$;

create function ledger_entries_archive_before_cutoff() returns trigger language plpgsql as $$
declare
    closes timestamptz := ledger_reset_closes();
begin
    if clock_timestamp() >= closes then
        raise exception 'the reset window closed at % Eastern (Mon 30 Nov, when the day-one fill can be stocked), '
                        'so the reset was not committed. Nothing was changed. See docs/ledger-reset.md, '
                        '''If the window closed''',
                        to_char(closes at time zone 'America/New_York', 'YYYY-MM-DD HH24:MI');
    end if;
    return null;
end
$$;

create constraint trigger ledger_entries_archive_before_cutoff
    after insert on ledger_entries_archive deferrable initially deferred
    for each row execute function ledger_entries_archive_before_cutoff();
