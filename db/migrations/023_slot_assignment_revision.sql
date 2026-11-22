-- Which slot assignment each agent run read, so its notes are reused only on that assignment (#216).
--
-- `slot_assignment.revision` is a number that changes whenever our slots change: a new row takes the
-- next value, and the trigger gives an update the next value when it changes `team`, `device_id` or
-- `slots`. Moving `last_checked_at` keeps it. The sequence only goes up, so an old revision never
-- comes back, even after the row is cleared and recorded again.
--
-- `agent_runs.slots_revision` is the revision a run read when it started (`vending.agent_runs.start`),
-- in that statement's snapshot. A change that `slots record` has written but not yet committed is not
-- in it, so a run that starts then records the old revision. `vending.agent.last_notes` reuses notes
-- only from a run whose revision is the current one. A run with none (it ran before this migration, or
-- with no assignment stored) never carries notes once a revision exists.
--
-- Numbered 023 because two open branches use 022. 024 to 026 are taken by other open PRs.

create sequence slot_assignment_revisions;

alter table slot_assignment add column revision bigint;
update slot_assignment set revision = nextval('slot_assignment_revisions');
alter table slot_assignment alter column revision set not null,
                            alter column revision set default nextval('slot_assignment_revisions');

create function slot_assignment_new_revision() returns trigger language plpgsql as $$
begin
    if (new.team, new.device_id, new.slots) is distinct from (old.team, old.device_id, old.slots) then
        new.revision := nextval('slot_assignment_revisions');
    end if;
    return new;
end
$$;

create trigger slot_assignment_new_revision before update on slot_assignment
    for each row execute function slot_assignment_new_revision();

alter table agent_runs add column slots_revision bigint;
