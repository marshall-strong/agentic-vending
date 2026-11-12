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
is `python -m vending.worker`: every hour it runs `python -m vending.sync_history`,
`python -m vending.sync_requests` and `python -m vending.sync_catalog`, each with a 30-minute
limit (`JOB_TIMEOUT_MINUTES`), and it answers `GET /health` on port 8080 with the last round's
outcome per job (a `503` means the last round failed). `/health` is public, so it shows exit codes
and outcomes only; the error text is in the log. `docker/entrypoint.sh` joins the tailnet first and
routes plain-http traffic (the bridge) through Tailscale's local proxy; HTTPS (the Anthropic API)
and Postgres go straight out, which is why `DEX_BASE_URL` must be `http://`.

**Never point an HTTP liveness probe at `/health`.** Its `503` means a sync failed, for example
while the bridge is unreachable, not that the worker is stuck, so Azure would restart a healthy
container every round and the restarts would fix nothing. The deploy script sets no probes, so
Azure uses its default TCP probes on port 8080, which pass whenever the worker is listening.
Watch `/health` yourself, or alert on it, instead.

The image runs as a normal user (`vending`, uid 10001), with pinned base images and no test
packages (`pytest` is in `requirements-dev.txt`).

**The Tailscale node is ephemeral.** It keeps no state, so every start, restart or new revision
joins again with `TS_AUTHKEY`, and Tailscale removes the node once it goes offline. That needs a
**reusable** key (ephemeral and tagged is best); a single-use key works for one start only. If
joining fails, the worker runs anyway, `/health` reports the bridge as unreachable, and the
entrypoint keeps retrying every minute in the background.

**The agent loop does not run yet.** When the harness exists it starts `python -m vending.server`
over stdio from the same image, with `VENDING_TOOLS=built` so only working tools are offered.
Until then the container gets **no Anthropic key**, since nothing in it calls the model; the change
that runs the agent loop here adds the secret to `$Secrets` in the deploy script, under the name the
agent reads (`VENDING_ANTHROPIC_API_KEY`, per the README).

**Nothing in the image migrates.** The worker's syncs, the server, the halt switch and the ledger
CLI all refuse to run on a database that is behind and name the command. After a merge that adds
a migration, a person runs it against Azure, from a laptop with `DATABASE_URL` pointing there:

```
python -m vending.db migrate
```

## One-time setup, per person

1. `az login --tenant golisanoinstitute.org` (your institute account; the Cognivue account sees nothing).
2. `docker login ghcr.io -u <your GitHub user>`, password a GitHub token with `write:packages`.
3. Once, after the first push: make the package public on GitHub (`marshall-strong` → Packages →
   `agentic-vending` → Package settings → Change visibility), so the Container App can pull it
   without registry credentials. The repository is public already, and the image holds no secrets.
   A new package starts private, so the first deploy pushes the image and then **stops before
   changing anything in Azure**, saying so; make the package public, then rerun with the
   `-Tag <tag> -SkipBuild` it names. Every deploy makes the same check with an anonymous
   `docker manifest inspect`; `-SkipPullCheck` skips it.

## Deploy

From the repository root, on a machine with Docker running:

```
.\scripts\deploy_container_app.ps1 -SetSecrets
```

That builds the image tagged with the current commit, pushes it, loads `DATABASE_URL`,
`DEX_API_KEY` and `TS_AUTHKEY` from your `.env` into Container App secrets,
sets `DEX_BASE_URL`, points ingress at port 8080, and rolls the app to the image with one replica
always on (`minReplicas` 1, the 29 Oct decision: it stays 0 only while the hello-world image is
deployed). If the roll fails, it points ingress back at the port it had.

It builds only from a clean checkout of `main` that matches `origin/main` (it fetches to check),
because the tag names the commit; `-SkipBuild` without `-Tag` checks the same, since the tag is
then the current commit. `-Force` deploys anyway, and uncommitted changes then add `-dirty` to
the tag. Untracked files count as changes only under the paths the `Dockerfile` copies
(`requirements.txt`, `vending/`, `db/`, `docker/`), because nothing else reaches the image: a
stray file at the repository root doesn't block a deploy, but a new `.py` file in `vending/` that
isn't committed does. The list is `$ImagePaths` in the script; keep it in step with the
`Dockerfile`'s `COPY` lines.

The secrets are read by `scripts/write_secret_files.py`, which parses `.env` exactly as the app
does, into temporary files that `az` reads itself (`database-url=@<file>`), so no value is ever on
a command line. Without `-SetSecrets`, the script stops if any of the three secrets doesn't exist
yet. **A revision reads its secrets only when it starts, and changing secrets alone makes no new
revision**, so when `-SetSecrets` leaves the latest revision as it was (same image and settings,
as when only `TS_AUTHKEY` changed), the script restarts it with `az containerapp revision
restart`. After changing a secret any other way (`az containerapp secret set`, the portal),
restart the revision yourself:

```
az containerapp revision restart --name ai-group-2-vend --resource-group data_systems_resource_group --revision <latest revision>
```

Later deploys that change only code:

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
docker run --rm -e DATABASE_URL=postgresql://vending:<password>@host.docker.internal:55432/vending -e DEX_BASE_URL=http://aihub-pi:8000 -e DEX_API_KEY=<key> agentic-vending:dev python -m vending.worker --once --port 0
```

Without `TS_AUTHKEY` the entrypoint says so and the syncs report the bridge as unreachable
(exit 3), which is the expected outcome off the tailnet. Leave out `DEX_BASE_URL` or
`DEX_API_KEY` and the jobs show `crashed` instead, because the settings check fails before
the bridge is tried. With `TS_AUTHKEY` as well it joins the tailnet from your laptop, as an
ephemeral node named `agentic-vending` that Tailscale removes once the container stops.

**Don't pass `.env` itself with `--env-file`** unless every value in it is bare. Docker takes
each line literally, so quotes and a trailing ` # comment` become part of the value, where the
app's own parser (`vending/settings.py`) strips them; its `DATABASE_URL` also names
`127.0.0.1`, which inside the container is the container. Pass the few settings with `-e` as
above, or write a separate file of bare `KEY=value` lines for `--env-file`.

## Known limits

- Tailscale's default key expiry is 90 days, and the key's expiry is unknown; if the node drops
  off the tailnet, ask David for a new key, put it in `.env` and run the deploy with
  `-SetSecrets`, which restarts the revision so it joins with the new key.
- One replica, so a deploy restarts the worker; a sync round in flight is cut off and simply
  runs again next hour.
- A job that passes its time limit can't be stopped, only left to finish in the background;
  `/health` shows it as `timed out`, then `still running` in later rounds until it ends. If it
  never ends, restart the revision (`az containerapp revision restart`).
- The base images are pinned by digest, so they get no security fixes until someone bumps them
  in `Dockerfile`.
- Hosting costs are covered by Graham and David (19 Oct), not our $600.
