-- What a submission was for, saved with the `sending` row before the POST.
--
-- `fill`: the day-one fill (`python -m vending.fill`, #214). The fill refuses to
-- send a second time while a request linked to a `fill` row is accepted and not
-- rejected by the bridge. The mark is on the row saved before the POST, so an
-- interruption after the bridge accepts (and before `action_log` commits) can't
-- lose it. `settle` and `submissions resolve` never change it.
--
-- Null: an agent's request. Numbered 025: 022 to 024 are on other open branches.

alter table purchase_submissions add column purpose text check (purpose in ('fill'));
