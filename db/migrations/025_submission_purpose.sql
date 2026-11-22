-- What a submission was for, saved with the `sending` row before the POST.
--
-- `fill`: the day-one fill (`python -m vending.fill`, #214). The fill refuses to
-- send a second time while a request linked to a `fill` row is accepted and not
-- rejected by the bridge. The mark is on the row saved before the POST, so an
-- interruption after the bridge accepts (and before `action_log` commits) can't
-- lose it. `settle` and `submissions resolve` never change it.
--
-- Null: an agent's request. Numbered 025: 022 to 024 are on other open branches.

-- A submit in flight reads purchase_submissions in its gate transaction, then
-- inserts its attempt on a second connection. If this ALTER queued behind the
-- read, that insert would queue behind the ALTER, and neither could finish.
-- So give up fast instead: the migration fails with "lock timeout" and nothing
-- changes. Run it again once no submit is running (docs/deploy.md).
set local lock_timeout = '2s';

alter table purchase_submissions add column purpose text check (purpose in ('fill'));
