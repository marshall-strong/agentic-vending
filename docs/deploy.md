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
`python -m vending.sync_requests`, `python -m vending.sync_catalog` and
`python -m vending.sync_product_requests` (which catalog requests were approved or rejected),
each with a 30-minute
limit (`JOB_TIMEOUT_MINUTES`), and it answers `GET /health` on port 8080 with the last round's
outcome per job (a `503` means the last round failed). Its root, `GET /`, is the same status as a
page for people: whether the last round passed, each job's outcome, when the last and next rounds
run (Eastern time), and links to the docs. It reloads itself every minute and works on a phone.
Both are public, so they show exit codes and outcomes only, never error text (a database error
names the host and user); the errors are in the log. The page is built from the same data as
`/health`, so it can't show more. `docker/entrypoint.sh` joins the tailnet first and
routes plain-http traffic (the bridge) through Tailscale's local proxy; HTTPS (the Anthropic API)
and Postgres go straight out, which is why `DEX_BASE_URL` must be `http://`.

**Friday reads around our visit** (`vending/visit_polls.py`, #172). Besides the hourly rounds, the
worker asks the bridge to read the machine live (`POST /api/v1/poll`, about 8.5 s) on Fridays, once a
minute from 2:45 PM to 7 PM Eastern. Why: the machine's counters are cumulative and are read only when
polled, so a sale between the last read before our visit and the first one after it can't be told apart
(old product or new) when the price didn't change. A read taken **while the door is open** settles it:
no unit sells then, so everything before that read is the old product and everything after it is the
new one, and the receipt's door times prove the read fell inside the visit. A visit can take only a few
minutes, so the reads have to be close together for one to land inside it.

| Setting | Default | |
|---|---|---|
| `VISIT_POLL_EVERY_MINUTES` | `1` | Minutes between reads: `0` turns them off, otherwise a number from `1` up. Under a minute, `inf` or text is a wrong value |
| `VISIT_POLL_FROM` | `14:45` | First read, Eastern time (the window follows the wall clock through daylight saving) |
| `VISIT_POLL_TO` | `19:00` | No read from this time on |
| `VISIT_POLL_DATES` | empty | Comma-separated ISO dates (Eastern), such as `2026-11-30`. These days get the reads too, with the same window and the same stop rule as a Friday. For a visit of ours that isn't on a Friday, the day-one fill. Remove the date afterwards (a past date does nothing) |

A wrong value turns the reads off, with a line in the log; it never stops the worker. The bridge stores
every read in its history, so after each read the worker copies the two latest UTC days of history into
our database (`vending.sync_history`, idempotent) and the read reaches the stock replay like the hourly
ones. The worker keeps the session id the bridge names in the `X-Poll-Session-Id` header of the read and
checks that the copy stored a usable row for it (checksum passed, our device). If not (the history
couldn't be listed or fetched, the checksum failed, or the poll isn't in the listing), the read counts
as a failure, and the log says "not stored usable". When it is stored, the log says "visit reads: stored
read <session id>". That the header's id is the id the history lists is what `docs/snapshot-archive.md`
suggests ("identifies the serial conversation"), and no one has checked it against the live bridge yet. If
every read is reported "not stored usable" while polls do appear in the history, that is the first suspect.
Without a header, the check falls back to any usable poll of ours taken since the read was sent.

The window and the spacing are checked again when the read is sent, after the receipts have been read
back: no read goes out after `VISIT_POLL_TO`, and none within an interval of the last one actually sent.

A read holds the bridge's one serial line, and anyone else's poll gets a `409` while it runs, so:

- one read at a time: a read is never started while the last one is still running;
- one try each: a `409`, a timeout or a bridge that is down is logged ("visit reads: skipped ...")
  and skipped, with no retry, and the next try is a whole interval later;
- after **3 failures in a row**, 5 minutes pass before the next try (and again after each further
  failure, until one works);
- **a failed read never fails the round or `/health`.** Reads are not jobs of the round and appear in
  neither. Only the log shows them.

Each step first re-reads our pending purchase requests (plain GETs, not machine reads), so a receipt
is seen within a minute. The reads stop for the day once a request of ours was fulfilled that day and a
read taken after its door closed (the close minute is over) is stored. A receipt with no door times is
dated by `fulfilled_at`. David set no limit on how often: on 12 Nov he said a read about every 10 minutes "won't break
anything", and named a poll every 15 seconds as what would get our container switched off. A read
every minute is Kevin's choice (19 Nov). Only Fridays and the dates in `VISIT_POLL_DATES`: a midweek
visit has no day the data can tell us, since a pending request can be filled on any day, so those get
the hourly reads only unless a person lists the day.

**Never point an HTTP liveness probe at `/health`.** Its `503` means a sync failed, for example
while the bridge is unreachable, not that the worker is stuck, so Azure would restart a healthy
container every round and the restarts would fix nothing. The deploy script sets no probes, so
Azure uses its default TCP probes on port 8080, which pass whenever the worker is listening.
Watch `/health` yourself, or alert on it, instead.

The image runs as a normal user (`vending`, uid 10001), with pinned base images and no test
packages (`pytest` is in `requirements-dev.txt`). It runs **Python 3.14**, the newest version any
of us develops on, so code that passes on a laptop also runs in Azure; bump the `FROM` line when
the team's venvs move. `tini` is PID 1: it passes Azure's `SIGTERM` to the worker and reaps the
entrypoint's background loops when they end.

To run the tests inside the image, with the image's own Python, before a deploy that changes it:

```
docker build -t agentic-vending:dev .
docker run --rm --user root --entrypoint sh -v "${PWD}/tests:/app/tests:ro" -v "${PWD}/scripts:/app/scripts:ro" -v "${PWD}/requirements-dev.txt:/app/requirements-dev.txt:ro" agentic-vending:dev -c "pip install -q -r requirements-dev.txt && python -m pytest -q -p no:cacheprovider tests"
```

The database tests skip there (no `DATABASE_URL`); the rest run.

**The Tailscale node is ephemeral.** It keeps no state, so every start, restart or new revision
joins again with `TS_AUTHKEY`, and Tailscale removes the node once it goes offline. That needs a
**reusable** key (ephemeral and tagged is best); a single-use key works for one start only. If
joining fails, the worker runs anyway, `/health` reports the bridge as unreachable, and the
entrypoint keeps retrying every minute in the background. `tailscaled`'s own log is
`/tmp/tailscaled.log` inside the container (the entrypoint prints its last 20 lines when a join
fails); it is cut to its last 500 lines whenever it passes 1 MB, so an always-on replica's log
can't fill the disk.

**The agent loop does not run yet.** When the harness exists it starts `python -m vending.server`
over stdio from the same image, with `VENDING_TOOLS=built` so only working tools are offered.
Until then the container gets **no Anthropic key**, since nothing in it calls the model; the change
that runs the agent loop here adds the secret to `$Secrets` in the deploy script, under the name the
agent reads (`VENDING_ANTHROPIC_API_KEY`, per the README). The deploy of `c1505c2` on 11 Nov, from
the script before this change, did set `ANTHROPIC_API_KEY` in the container. `--set-env-vars` only
adds, so the script now passes `--remove-env-vars` for it when the app still has it (the list is
`$Retired`). The `anthropic-api-key` secret itself stays in the app, unused by any variable; to
delete it:
`az containerapp secret remove --name ai-group-2-vend --resource-group data_systems_resource_group --secret-names anthropic-api-key`.

**Nothing in the image migrates.** The worker's syncs, the server, the halt switch and the ledger
CLI all refuse to run on a database that is behind and name the command. After a merge that adds
a migration, a person runs it against Azure, from a laptop with `DATABASE_URL` pointing there:

```
python -m vending.db migrate
```

**After the migration that adds `slot_assignment` (013, from #69), record our slots once.** Every
agent tool, `preview_desired_state` and the ledger CLI read which selections are ours from that
table, and until it has a row they refuse and name this command (the dashboard says our slots
aren't recorded, names the same command and leaves the machine, prices and stock blank). It
reads `GET /api/v1/slots` from the bridge, so run it from a laptop on the class tailnet, with the
same `DATABASE_URL`:

```
python -m vending.slots record
```

It should print `recorded: team group-b, device vmc1, 20 slots: ...` with the server's answer,
and `python -m vending.slots` shows what is stored. Running it again is safe: the same answer only
updates when it was last checked.

**Only a person stores or changes our slots.** The worker runs `python -m vending.slots check`
first in every round (`slots_check`). It only confirms: the same answer updates when it was last
checked; a different answer, or nothing stored, changes nothing and fails the round, so `/health`
answers 503 until a person looks. The instructors change our assignment exactly once, through the
server, and that change is what the competition uses (David, 16 Nov; assumption A16). So a failed
`slots_check` before the start most likely means the change has come: check it with the
instructors, then run `python -m vending.slots record`, and replan the day-one fill if it is
already bought.

**Our team.** Every answer must name our team, `SLOTS_TEAM` (unset: `group-b`, the name the server
has given since 12 Nov). An answer for another team, or with no team, is refused, so a swapped key
or a misconfigured bridge can't hand us the other half of the machine.

**The lock.** The assignment is fixed from the start. The lock time is the setting `SLOTS_LOCK_AT`,
an ISO 8601 time with its offset; unset, it is `2026-12-03T00:00:00-05:00` (midnight Eastern,
Thu 3 Dec, the first day of Winter Quarter on the Institute's calendar and the start David gave on
12 Nov). If the instructors move the start (to Wed 2 Dec, say), set it on the Container App, for
example `2026-12-02T00:00:00-05:00`. Times are taken from the database clock, not the host's.
Each process reads these settings itself: when you run `record` or `confirm` from a laptop, set the
same `SLOTS_LOCK_AT` and `SLOTS_TEAM` as the Container App. Every `record`, `check` and `confirm`
prints the lock and team it used, so a difference shows.

- **Before the lock time**, `record` takes a different answer in. It prints `changed: ...`, and
  `action_log` (tool `slots.record`) says what it was and what it is now.
- **At the lock**, the worker runs extra rounds 1, 5 and 15 minutes after it. If the server still gives the
  stored answer, that check confirms it for the competition, and `action_log` says so. A check
  counts from when it asked the server, so one that asked before the lock and got its answer after
  it confirms nothing.
- **Until something confirms it**, every reader refuses: the agent's tools, the planner, the
  ledger, replay and the analysis ask for `python -m vending.slots check`, and the dashboard shows
  no assigned selections (back stock still shows, from the receipts).
- **After the start**, a confirmed assignment is never changed by `record` alone (exit 1). If the
  instructors say the confirmed one is wrong, a person runs `python -m vending.slots record --replace`.
- **Always refused**, before or after: an answer that isn't 20 different selections on `vmc1`,
  each a shelf 1-5 then a column 1-8 (11 to 58), or isn't for our team.

**On the morning of the start** (just after midnight Eastern, or first thing before the 7:30 AM
opening), one of us checks `/health`. `slots_check` passed at an extra round: nothing to do. It
failed with a different assignment: ask the instructors, then run `record` (nothing has been
confirmed since the lock, so `record` takes the server's answer then). The bridge is down: run
`python -m vending.slots confirm` from a laptop with `DATABASE_URL`, which confirms the stored
assignment without the server; run `check` once the bridge is back.

**A change after the day-one fill.** Stock follows the receipts, but sales are read only for our
current slots. If our slots change after the fill is loaded, the fill's receipts on the old slots
stay counted as stock while their sales stop being read, so the P&L and the stock count go wrong.
Plan on that not happening (the change comes before the start), but if it does, record a ledger
adjustment for the units on the old slots (`docs/pnl-report.md`) and say so in the week's notes.

The dashboard and `/db` read the stored assignment on every page, so they follow a change at
once. With nothing stored, the dashboard lights no selection and says so, and `/db` shows no
row of `poll_selections` or `slot_counts`. The sales analysis reads the stored assignment when it
exports, so after a change the next `python analysis/rebuild.py` follows it. No code holds a fixed
list of our slots.

## The public pages

The same port serves two pages for the team and the instructors (#53), so they can look without
SQL:

```
https://ai-group-2-vend.bravegrass-0fe81ba9.eastus.azurecontainerapps.io/dashboard
https://ai-group-2-vend.bravegrass-0fe81ba9.eastus.azurecontainerapps.io/db
```

`/dashboard` is the agent dashboard ([`dashboard.md`](dashboard.md)), with the same data as JSON
at `/dashboard/state.json`. `/db` is the DB explorer ([`db-explorer.md`](db-explorer.md)). The
home page links to both once they are set up. They are `vending/pages.py`.

**One shared password** (`vending/gate.py`, since 15 Nov 2026). Every page except `/health`
asks for it first, at `/login`; a browser that gives it stays signed in for 30 days, and
`/logout` forgets it in that browser only. The sign-in is a signed cookie the server keeps
no list of, so a copied cookie works until it expires; the only ways to sign a browser out
remotely are a new password or a new `READONLY_DATABASE_URL` (the cookie key is made from both). Kevin has the password and shares it with the team and the instructors.
The code holds only its scrypt hash, so nobody has to store a secret in Azure, and after a
wrong guess the same client waits a second before its next one is checked. A client has one check
at a time, so guesses sent all at once, or queued while the server is busy, still get one check a
second. The delay is per address: someone behind the same shared network address as a teammate
could keep that teammate waiting. To change it, run `python -m vending.gate hash`, paste the
line it prints into `vending/gate.py` and deploy; that signs every browser out. Microsoft
sign-in (Container Apps' built-in one) was tried first, but the institute's tenant makes an
administrator approve each app, so it would need IT.

**Visits are counted** (`vending/visits.py`, migrations 016 and 018). Each right password is a
sign-in, and each browser opening `/dashboard` or `/db` is a visit, at most once per page per half
hour; the dashboard's own 30-second refresh never counts, with or without scripts (the no-script
reload goes to `?refresh=1`). A browser is a random id in a cookie of its own (`vend_browser`), so
there are no names, and no IP address or browser string is kept; the browser counts are the ones
that opened a page. The dashboard's **Visitors** tile shows the counts, and
`python -m vending.visits` prints them. The worker writes them in the background with the syncs'
login (the pages' login stays read-only), on one of the pages' own two connections and only while
one is free, so the pages still use at most two. That connection has the pages' 5-second statement
timeout and keepalives. If no connection comes free within 30 seconds, the tables aren't there yet
or the database is down, the counts are dropped with a line in the log and the pages work as
before, so run `python -m vending.db migrate` before or after the deploy that brings them. The
tile reads running totals and the last week's rows only, gives up after a second, and if it can't
read them it says "Unavailable" while the rest of the dashboard shows as usual. No model is called.

**Nobody can change anything through them, in three layers:**

1. **A login that can only read.** The pages connect with their own secret,
   `READONLY_DATABASE_URL`, never with the syncs' `DATABASE_URL`. Before serving, they ask
   Postgres what that login may do. **If it can write to any table or create one, the pages
   switch themselves off** and say why in the log, so putting the syncs' login in that secret
   doesn't work. Without the secret they answer "not set up yet", and everything else runs.
2. **A read-only session**, and each page is one `repeatable read, read only` transaction.
3. **Nothing to send:** GET only, no SQL box. The one form is the password page, which only
   checks the password.

**Load limits.** The database server allows 50 connections for both teams. The pages use at most
2 at once, a request that waits more than 5 seconds for one is told the page is busy, and every
statement is cancelled after 5 seconds. The role's own `connection limit` (below) caps it again
on the server.

**What anyone with the password sees:** every table, including the plans, the purchase requests and the ledger. No
table holds a password or a key. **Left out:** the selections not assigned to us (the machine
stores every selection, and `/db` shows only those in the stored assignment in `poll_selections`
and `slot_counts`, and none while nothing is stored; the dashboard's JSON counts likewise), the dev machines' hostnames in `model_calls.host`, and the error text of failed tool calls and failed
runs (`action_log.detail` and `result`, `agent_runs.detail`, where the row failed), because a
database error names the host and the user. Refusals keep their text. The pages' own errors show
one plain sentence; the detail is in the container's log.
`/db` also hides the machine-wide totals in `polls` (`total_paid_cents` and `total_paid_units`) in
every row: from the start they include the other team's sales, and the total minus ours is theirs.

### Setting up the read-only login (once)

`g2_admin` can't create roles, and the database lets only named logins connect, so the first two
statements are for whoever administers the server (David or Graham):

```sql
create role vending_reader login password '<a long random password>' connection limit 4;
grant connect on database "ai-vend-2" to vending_reader;
```

Then one of us, with `DATABASE_URL` pointing at Azure, lets it look inside our schema (`usage`) and
read our tables, including tables later migrations add:

```
python -m vending.pages grant vending_reader --by NAME
```

Put the new login in your `.env`, under its own name, and check it:

```
READONLY_DATABASE_URL=postgresql://vending_reader:<password>@<host>:5432/ai-vend-2?sslmode=require
python -m vending.pages check
```

`check` connects the way the pages do and must say **read-only** and **can read every table**.
It also names a schema the login may not look inside, for example `schema public (no usage)`: if
`grant` printed a "no privileges were granted" warning for the schema, our login doesn't own it,
and the administrator runs `grant usage on schema public to vending_reader;`.
Then deploy with `-SetSecrets`, which loads the secret and gives the container the variable. The
deploy script treats this one secret as optional: without it in `.env` a deploy still works, and
says the pages aren't set up.

**To change the password:** the administrator runs
`alter role vending_reader password '<new>';`, you update `READONLY_DATABASE_URL` in `.env`, and
`.\scripts\deploy_container_app.ps1 -SetSecrets` restarts the revision with it.

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
`DEX_API_KEY` and `TS_AUTHKEY` (and `READONLY_DATABASE_URL`, when it is there) from your `.env` into Container App secrets,
sets `DEX_BASE_URL`, points ingress at port 8080, and rolls the app to the image with one replica
always on (`minReplicas` 1, the 29 Oct decision: it stays 0 only while the hello-world image is
deployed). If the roll fails, it points ingress back at the port it had.

It builds only from a clean checkout of `main` that matches `origin/main` (it fetches to check),
because the tag names the commit; `-SkipBuild` without `-Tag` checks the same, since the tag is
then the current commit. `-Force` deploys anyway, and uncommitted changes then add `-dirty` to
the tag. Untracked files count as changes only under the paths the `Dockerfile` copies
(`requirements.txt`, `vending/`, `db/`, `docs/agent/`, `docker/`), because nothing else reaches the image: a
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
https://ai-group-2-vend.bravegrass-0fe81ba9.eastus.azurecontainerapps.io/
https://ai-group-2-vend.bravegrass-0fe81ba9.eastus.azurecontainerapps.io/health
https://ai-group-2-vend.bravegrass-0fe81ba9.eastus.azurecontainerapps.io/dashboard
https://ai-group-2-vend.bravegrass-0fe81ba9.eastus.azurecontainerapps.io/db
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
- **Once Tailscale drops the node, the container can't rejoin by itself.** The entrypoint deletes
  its copy of the key after the first join and the worker never sees `TS_AUTHKEY`, so if the
  ephemeral node is removed (for example after a long outage on either side), every hourly round
  reports the bridge unreachable on `/health` until someone restarts the revision, which joins
  again with the `ts-authkey` secret:
  `az containerapp revision restart --name ai-group-2-vend --resource-group data_systems_resource_group --revision <latest revision>`.
- `tailscaled`'s log is not in the Container App log. To read it, open a shell with
  `az containerapp exec --name ai-group-2-vend --resource-group data_systems_resource_group --command sh`
  and run `tail -n 100 /tmp/tailscaled.log`.
- One replica, so a deploy restarts the worker; a sync round in flight is cut off and simply
  runs again next hour.
- A job that passes its time limit can't be stopped, only left to finish in the background;
  `/health` shows it as `timed out`, then `still running` in later rounds until it ends. If it
  never ends, restart the revision (`az containerapp revision restart`).
- The base images are pinned by digest, so they get no security fixes until someone bumps them
  in `Dockerfile`.
- Hosting costs are covered by Graham and David (19 Oct), not our $600.
