# Deploying to Azure

The agent runs in an Azure Container App that David created on 26 Oct 2026: `ai-group-2-vend`,
in resource group `data_systems_resource_group`, environment `data-systems-containers`,
subscription "AI Innovation Hub", East US, Consumption plan. Each team member has Contributor on
it, which is enough to deploy revisions, set secrets and change scale. The database is the
instructors' Azure Postgres (`ai-vend-2`), reached over the public endpoint with TLS, so the
container needs no network setup for it. **The bridge is reachable only on the class tailnet**,
so the container joins it with Tailscale in userspace mode.

## What runs in the container

`Dockerfile` builds one image with the package and the Tailscale binaries. Its default command
is `python -m vending.worker`: every hour it runs `python -m vending.sync_history` and
`python -m vending.sync_requests`, and it answers `GET /health` on port 80 with the last round's
outcome (a `503` means the last round failed). `docker/entrypoint.sh` joins the tailnet first and
routes plain-http traffic (the bridge) through Tailscale's local proxy; HTTPS (the Anthropic API)
and Postgres go straight out.

**The agent loop does not run yet.** When the harness exists it starts `python -m vending.server`
over stdio from the same image, with `VENDING_TOOLS=built` so only working tools are offered.

**Nothing in the image migrates.** The worker's syncs, the server, the halt switch and the ledger
CLI all refuse to run on a database that is behind and name the command. After a merge that adds
a migration, a person runs it against Azure, from a laptop with `DATABASE_URL` pointing there:

```
python -m vending.db migrate
```

## One-time setup, per person

1. `az login --tenant golisanoinstitute.org` (your institute account; the Cognivue account sees nothing).
2. `docker login ghcr.io -u <your GitHub user>`, password a GitHub token with `write:packages`.
3. After the first push, make the package public on GitHub (`marshall-strong` → Packages →
   `agentic-vending` → Package settings → Change visibility), so the Container App can pull it
   without registry credentials. The repository is public already, and the image holds no secrets.

## Deploy

From the repository root, on a machine with Docker running:

```
.\scripts\deploy_container_app.ps1 -SetSecrets
```

That builds the image tagged with the current commit, pushes it, loads `DATABASE_URL`,
`DEX_API_KEY`, `TS_AUTHKEY` and `ANTHROPIC_API_KEY` from your `.env` into Container App secrets,
sets `DEX_BASE_URL`, and rolls the app to the image with one replica always on (`minReplicas` 1,
the 29 Oct decision: it stays 0 only while the hello-world image is deployed). Later deploys that
change only code:

```
.\scripts\deploy_container_app.ps1
```

Then check:

```
https://ai-group-2-vend.bravegrass-0fe81ba9.eastus.azurecontainerapps.io/health
az containerapp logs show --name ai-group-2-vend --resource-group data_systems_resource_group --follow
```

The log shows `tailscale:` with the node's status, then each sync's output. The first round
tells you whether Azure can reach the bridge at all, which has never been tested: the auth key
worked from a laptop on 2 Nov, over a direct local path.

## Rolling back

Container Apps keeps earlier revisions. List them and activate the previous one:

```
az containerapp revision list --name ai-group-2-vend --resource-group data_systems_resource_group --output table
az containerapp revision activate --name ai-group-2-vend --resource-group data_systems_resource_group --revision <name>
```

Or deploy a known tag without rebuilding: `.\scripts\deploy_container_app.ps1 -Tag <sha> -SkipBuild`.

## Trying the image locally

```
docker build -t agentic-vending:dev .
docker run --rm -p 8080:80 -e DATABASE_URL=postgresql://vending:<password>@host.docker.internal:55432/vending agentic-vending:dev python -m vending.worker --once --port 0
```

Without `TS_AUTHKEY` the entrypoint says so and the syncs report the bridge as unreachable, which
is the expected outcome off the tailnet. With `--env-file .env` it joins the tailnet from your
laptop too; the node shows up in the admin console as `agentic-vending`, so remove it afterwards.

## Known limits

- Tailscale's default key expiry is 90 days, and the key's expiry is unknown; if the node drops
  off the tailnet, ask David for a new key and run the deploy with `-SetSecrets`.
- One replica, so a deploy restarts the worker; a sync round in flight is cut off and simply
  runs again next hour.
- Hosting costs are covered by Graham and David (19 Oct), not our $600.
