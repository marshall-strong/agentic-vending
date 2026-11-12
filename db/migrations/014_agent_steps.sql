-- What the agent did on each turn of a run, in order: its reasoning, any text it wrote, each tool
-- call and what came back. The agent loop (`vending.agent`) writes a row as each step happens, so a
-- run that dies part-way still shows how far it got, and the dashboard can show a run in progress.
--
-- `thinking` is the model's reasoning as the API returns it: a readable summary, never the raw chain
-- of thought (`thinking: {display: "summarized"}`). `text` is anything it wrote outside a tool call,
-- including its final JSON answer. `tool_call` holds the tool's arguments as JSON, and
-- `tool_result` the reply the model was given, cut to `vending.agent_runs.MAX_STEP_TEXT`
-- characters. `is_error` marks a tool result the model was told had failed.
--
-- Numbered 014 because open PRs use 012 (#66) and 013 (#69); the migrator applies any file not yet
-- applied, so the order they merge in doesn't matter.

create table agent_steps (
    id        bigserial   primary key,
    run_id    bigint      not null references agent_runs (id) on delete cascade,
    turn      integer     not null check (turn >= 1),
    at        timestamptz not null default now(),
    kind      text        not null check (kind in ('thinking', 'text', 'tool_call', 'tool_result')),
    tool      text,
    content   text        not null,
    is_error  boolean     not null default false,
    check ((tool is null) = (kind in ('thinking', 'text')))
);

create index agent_steps_run on agent_steps (run_id, id);
