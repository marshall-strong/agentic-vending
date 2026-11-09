-- The ledger: the money the other tables don't already record.
--
-- The balance is computed, never stored:
--
--     opening capital
--   + takings           (the polls' lifetime sales counters, tax included)
--   - receipts          (purchase_lines.total_paid on fulfilled requests)
--   - midweek visit fees (fulfilled off-cycle requests x config midweek_fee_cents)
--   - token spend       (one row here per model call)
--   +/- adjustments     (corrections a person records, with a reason)
--
-- Takings, receipts and fees are read from their own tables each time, so a
-- corrected receipt or a re-synced poll changes the balance by itself. This
-- table holds only what has no other record: the one opening entry, every
-- model call's cost, and adjustments. `vending/ledger.py` is the only writer.
--
-- Numbered 007 because Sean's catalog branch uses 006; the migrator applies
-- any file not yet applied, so the order they merge in doesn't matter.

create table ledger_entries (
    id            bigserial   primary key,
    -- When the money moved, which may differ from when it was recorded.
    at            timestamptz not null default now(),
    kind          text        not null check (kind in ('opening', 'tokens', 'adjustment')),
    -- Signed cents: a credit is positive, a debit negative. `numeric`, not an
    -- integer, because a model call costs a fraction of a cent.
    amount_cents  numeric     not null,
    detail        text        not null,
    -- What it refers to: a model request ID, a purchase request ID, an email.
    reference     text,
    recorded_by   text        not null,
    recorded_at   timestamptz not null default now()
);

create index ledger_entries_at on ledger_entries (at);

-- The run starts once. A second opening entry is a mistake, not a top-up;
-- a top-up is an adjustment.
create unique index ledger_entries_one_opening on ledger_entries (kind) where kind = 'opening';
