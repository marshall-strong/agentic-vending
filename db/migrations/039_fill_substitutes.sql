-- Substitutes for the day-one fill (Kevin, 25 Nov): another drink a line's alternate buys when its own drink has
-- no backup at another store, so the slot isn't left empty. The bridge takes an alternate with its own `sku`
-- (docs/dex-bridge.md). `python -m vending.alternates --write` stores them; `python -m vending.fill` reads them
-- for the picked plan only, and only for a line with no same-drink alternate.
--
-- substitute_sku: the drink to buy, at its own primary sourcing row (store, pack, link, price).
-- max_cents:      the most per pack for it here, held to the line's most-we-pay in the pick, so the fill's
--                 $400 cap and its pick_deviation check hold. The row's own max applies when it is lower.
create table fill_substitutes (
    plan_id        bigint      not null references first_fill_plans (id) on delete cascade,
    sku            text        not null,
    substitute_sku text        not null check (substitute_sku <> sku),
    max_cents      integer     not null check (max_cents between 1 and 20000),
    set_by         text        not null check (length(trim(set_by)) > 0),
    set_at         timestamptz not null default now(),
    primary key (plan_id, sku)
);
