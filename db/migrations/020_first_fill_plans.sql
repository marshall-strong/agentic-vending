-- 019 is reserved by PR #168. The pages login inherits SELECT from vending.pages grant.
create table first_fill_plans (
    id          bigint      generated always as identity primary key,
    loaded_at   timestamptz not null default now(),
    loaded_by   text        not null,
    label       text        not null,
    source_note text        not null,
    plan        jsonb       not null check (jsonb_typeof(plan) = 'object'),
    projection  jsonb       not null check (jsonb_typeof(projection) = 'object'),
    slots       text[]      not null,
    picked      boolean     not null default false
);

create unique index first_fill_plans_one_pick on first_fill_plans (picked) where picked;
