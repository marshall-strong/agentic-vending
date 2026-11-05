-- Settings the code reads at run time, and the log every write goes through.
--
-- `config` holds one JSON value per key, so the planner's settings (cutoff,
-- slot capacity, price ceiling) can join it later without a migration each.
-- The halt flag starts ON: nothing writes until a person turns it off with
-- `python -m vending.halt off`. If the row is ever missing, the write gate
-- treats the machine as halted.

create table config (
    key         text        primary key,
    value       jsonb       not null,
    updated_at  timestamptz not null default now(),
    updated_by  text        not null default current_user
);

insert into config (key, value) values ('halted', 'true');

-- One row per tool call, reads included, whatever happened. A write's row is
-- written in the same transaction as the write itself. `arguments` is what the
-- agent sent; `result` is what the tool returned. Readable by a non-technical
-- reviewer: `select at, tool, outcome, detail from action_log where kind = 'write' order by at`.
create table action_log (
    id         bigserial   primary key,
    at         timestamptz not null default now(),
    kind       text        not null check (kind in ('read', 'write')),
    tool       text        not null,
    arguments  jsonb       not null,
    outcome    text        not null check (outcome in ('done', 'refused', 'failed')),
    detail     text,
    result     jsonb
);

create index action_log_at on action_log (at);
