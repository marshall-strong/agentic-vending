-- The model's id for each tool use, on the tool call and on its result (`vending.agent`), so a reply is
-- matched to the call it answers even when the model makes several calls in one turn. The live run page
-- (`vending.run_page`) pairs each `preview_desired_state` draft with its own preview reply by it.
--
-- Null for steps recorded before this migration, for reasoning and text, and for the startup reads the
-- run makes itself (`get_briefing`, `check_submissions`), which no model tool use asked for.
alter table agent_steps add column tool_use_id text;
