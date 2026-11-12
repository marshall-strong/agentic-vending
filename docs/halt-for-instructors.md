# How to stop our agent (for instructors and operations)

The project brief requires that an authorized instructor or operations staff member can pause or stop
our system immediately, "with no dependency on your agent's cooperation" (Project Brief (Revised),
section 4). Any one of these three works without us. Use the first one unless you need more.

## 1. Turn on the halt switch (recommended)

Connect to our database, `ai-vend-2`, on the instructors' Postgres server, as any login that can
update our tables, and run:

```sql
update config set value = 'true', updated_at = now(), updated_by = 'your name' where key = 'halted';
```

- **What it does:** every tool that writes refuses with `halted`, and the agent stops before its next
  model request. Read-only tools and the hourly data sync keep working, so the dashboard stays current.
- **How fast:** at once. A write already in progress finishes first, because it holds a lock on this
  row, then nothing else gets through.
- **It fails safe.** If the row is missing, or holds anything other than `false`, we count as halted.
  So `delete from config where key = 'halted';` also stops us.
- **Check it:** `select value, updated_by, updated_at from config where key = 'halted';` shows `true`.
- **Tested 12 Nov 2026:** on a throwaway database, and then on `ai-vend-2` against a running agent,
  which stopped about a second later, before its second turn. Details are in `docs/guardrail-evidence.md`.

## 2. Stop the container

Our sync worker runs in the Container App `ai-group-2-vend` (resource group
`data_systems_resource_group`). In the Azure portal, open it and deactivate the active revision under
Revisions, or from the Azure CLI:

```
az containerapp revision list --name ai-group-2-vend --resource-group data_systems_resource_group --query "[?properties.active].name" -o tsv
az containerapp revision deactivate --name ai-group-2-vend --resource-group data_systems_resource_group --revision <name from the line above>
```

- **What it does:** stops everything we run in Azure, including the hourly data sync.
- **Note:** as of 12 Nov the agent itself does not run in the container yet, so today this stops the
  sync only. Use the halt switch to stop the agent.
- **Not tested by us:** we haven't deactivated our own revision, because it would stop the sync.

## 3. Deny the request at the bridge

A purchase request we have already sent is with you, not with us. **The halt switch does not recall
it.** Deny or cancel it in the purchaser portal, as with any request.

## What happens next

The halt stays on until one of us turns it off with `python -m vending.halt off --by NAME`, which is
logged with the name. Please tell us why you stopped us, so we can fix it before turning it back on.
