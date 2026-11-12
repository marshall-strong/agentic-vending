-- The standing benchmark: every run of `python -m vending.bench`, and every answer in it.
--
-- The team keeps changing the prompt, the team rules, the test cases and the
-- model. Each change should be checked against the same yardstick, and the
-- answer kept, so that a slow slide shows up as a trend and a sudden one as a
-- regression, instead of living in someone's terminal scrollback or a
-- gitignored results folder. So every bench run is a row here, with what it ran
-- on (the git commit, a hash of the prompt and one of the cases, so a change in
-- score can be put down to a change in the inputs), and every answer is a row in
-- `bench_results` with its hard checks, its outcome numbers, its cost and the
-- judge's grade. `python -m vending.bench report` reads both.
--
-- The model calls themselves are in `model_calls` (011), labelled "bench <id> ...";
-- `spent_usd` here is the bench's own total of them.
--
-- `vending/bench/` is the only writer.

create table bench_runs (
    id          bigserial   primary key,
    started_at  timestamptz not null default now(),
    finished_at timestamptz,
    -- A run left `running` was killed before it could say so (a power cut, a closed window).
    status      text        not null default 'running' check (status in ('running', 'done', 'stopped')),
    git_sha     text,
    git_branch  text,
    git_dirty   boolean,                  -- uncommitted changes: the sha alone doesn't say what ran
    -- sha256 of the model's system prompt and plan schema: a new hash means the prompt changed.
    prompt_hash text,
    -- sha256 of every case's question, good answer and weeks, in order: a new hash restarts the trend.
    cases_hash  text,
    cases       text[]      not null default '{}',
    configs     text[]      not null default '{}',   -- "haiku@medium", ...
    repeats     integer     not null default 1,
    judge       text        not null default 'none',  -- "claude-opus-5-5@medium", "claude-code-blind", "none"
    -- sha256 of the judge's rubric and output schema: a new hash means the yardstick itself moved.
    judge_hash  text,
    -- What the judge said about each case, or why it couldn't judge it: {case: {"note"|"problem": text}}.
    judge_notes jsonb       not null default '{}',
    budget_usd  numeric,
    spent_usd   numeric     not null default 0,
    note        text        not null default '',
    source      text        not null default 'bench' check (source in ('bench', 'import')),
    results_dir text,
    recorded_by text        not null default '',
    host        text        not null default ''
);

create index bench_runs_started on bench_runs (started_at);
-- An imported folder is imported once.
create unique index bench_runs_imported_dir on bench_runs (results_dir) where source = 'import';

create table bench_results (
    id            bigserial primary key,
    run_id        bigint    not null references bench_runs (id) on delete cascade,
    config        text      not null,              -- "haiku@medium", or "baseline"
    model         text      not null,
    effort        text      not null,
    repeat        integer   not null default 1,
    case_name     text      not null,
    -- The baseline's answer to the same case: the judge must fail it on the trap cases.
    control       boolean   not null default false,
    status        text      not null check (status in ('scored', 'dropped', 'error')),
    valid         boolean,
    failed        text[]    not null default '{}',  -- hard checks that failed
    checks        jsonb     not null default '{}',  -- {check: pass|fail|skip}
    slots_scored  integer,
    short         integer,
    unsold        integer,
    margin        numeric,                          -- dollars, against the case's next week
    cost_usd      numeric   not null default 0,
    input_tokens  integer,
    output_tokens integer,
    seconds       numeric,
    max_tokens    integer,
    plan          jsonb,                            -- the plan as given; {"not_json": text} if it wasn't JSON
    decision      smallint  check (decision between 0 and 3),   -- null: not judged
    reasoning     smallint  check (reasoning between 0 and 2),
    invented      boolean,
    judge_why     text,
    -- null: not judged. false: the judge failed its control on this case, so the grade doesn't count.
    judge_ok      boolean,
    unique (run_id, config, repeat, case_name)
);

create index bench_results_config on bench_results (config, case_name);
