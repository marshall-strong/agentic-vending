# Snapshot archive

A scheduled job that polls the DEX Bridge and writes each response to disk,
unchanged. Nothing else.

## What this is, and what it deliberately is not

The archive stores three things per poll: the raw response body, the time it was
taken, and the response headers. It does **not** parse EVA-DTS, write to a
database, or analyse anything. Those are separate jobs, and keeping the raw text
now is exactly what makes them possible later -- a parser can be rewritten
against archived text, but a poll that was never taken is gone.

That matters more here than in a normal logging setup. The machine reports where
its **lifetime counters** stand at the moment you ask. There is no "sales this
week" endpoint. Period sales only exist as the difference between two archived
reads, so **every day without an archive is baseline demand data that cannot be
recovered afterwards.**

## Quick start

```
python scripts\poll_snapshot.py --dry-run
```

Checks that Tailscale is up and the bridge is alive, then stops. It does not
read the machine, so it costs nothing and is the right thing to run first.

```
python scripts\poll_snapshot.py
```

One poll, one snapshot. This is the whole job.

```
python scripts\check_archive.py
```

Tells you whether the archive is current. Run this when you want to know that
the thing is still working.

```
.\scripts\register_task.ps1
```

Sets up the Windows scheduled tasks. See [Scheduling](#scheduling) below.

You need a `.env` in the repository root with `DEX_BASE_URL` and `DEX_API_KEY`
in it. Copy `.env.example` and fill it in. It is gitignored and must stay that
way -- the key lets anyone on the tailnet talk to a machine that takes real
money.

## What gets written

Each successful poll writes a pair of files named for the UTC time the request
was sent:

```
data/snapshots/20260921T010454Z.dex       the response body, byte for byte
data/snapshots/20260921T010454Z.headers   the status line and every header
```

`data/` is gitignored. The archive is our own data, not source code, and it is
append-only -- back it up somewhere other than this repository.

The `.dex` file is written in binary mode so the machine's CRLF line endings
survive intact. The `.headers` file records everything the server sent; three
headers matter most:

| Header | Why it matters |
|---|---|
| `X-Poll-Cached` | `true` means we were handed another caller's document, not a fresh read |
| `X-Poll-Age-Seconds` | how old that document was when we got it |
| `X-Poll-Session-Id` | identifies the serial conversation, for correlating with the bridge's own logs |

A cached response is still archived. It is a real document and a real
observation, at most 30 seconds stale, and throwing it away would lose data for
no gain. The log line and the `.headers` file both record that it was cached.

### Snapshots that fail the shape check

A response has to look like a whole DEX read: first record starts with `DXS`,
last record starts with `DXE`, at least 50 records, and a byte count matching
the server's `Content-Length`. That is a cheap check that requires parsing
nothing.

A body that fails goes to `data/snapshots/invalid/` instead. It is never
discarded -- we paid 8.5 seconds of the machine's time for it and it may be
worth reading later -- but keeping it out of the main directory means
`data/snapshots/*.dex` stays trustworthy for whatever reads it next.

## Cadence

**The recommendation: every 30 minutes from 07:37 to 18:37, seven days a week.
That is 23 polls a day.**

### Why not 5 minutes

| | every 30 min | every 5 min |
|---|---|---|
| Polls per day | 23 | 133 |
| Serial time per day | 3m 16s | 18m 51s |
| Share of the 11-hour window | 0.5% | 2.9% |

Nearly six times the load on a machine that is in service, sells drinks to
people in the building, and is shared with another team and the rest of the
class. The API documentation is blunt about it: polling in a loop "gains you
nothing and is unkind to a machine that is in service."

The reason it gains little is worth stating precisely, because it is the part
that makes the case:

**The machine's lifetime counters run from installation of the control board
and cannot be reset. They increment whether or not anyone is watching.** So the
interval does not determine what we capture -- it determines only where we can
place a boundary. No sale is ever lost by polling less often. The 07:37 read
minus the previous evening's 18:37 read gives the overnight total exactly, even
though nothing was polled in between.

What a longer interval costs is **timing resolution on demand, not totals.**
Miss a day and the cumulative numbers are still correct; we just cannot say
which day the sales happened.

That reframes the question. It is not "how much data do we get", it is "how
finely do we need to attribute sales to a time of day". Thirty minutes brackets
a class changeover, which is the event anyone actually wants to reason about.
Five minutes is finer than the events being measured, and on a sparse series it
makes things worse rather than better: most five-minute buckets would contain
zero vends, so the result is a noisier series, not a more informative one.

### Answering the question empirically later

The honest position for the instructors is that this is a starting cadence, not
a permanent one. After about two weeks of 30-minute data the team can count the
actual vends per bucket and see whether finer resolution would reveal anything
that 30 minutes hides. If it would, the right response is a **time-boxed**
tightening -- say 10 minutes for a 90-minute window around changeovers, on named
days, announced to the class -- not a standing increase.

Going the other way is cheap to justify and easy to do. Going up to 133 polls a
day on a shared machine, before anyone has shown the data supports it, is not.

### Why seven days a week

The weekend rate is itself a finding. If the machine sells nothing on Sunday,
that is a real fact about demand and worth having in evidence rather than
assuming. Contention on a weekend is also near zero, since nobody else is
polling.

### Why 07:37 and not 07:30

The offset is deliberate. Results are shared class-wide for 30 seconds, so
everyone polling on the hour and half-hour piles onto the same instants -- which
is when the bridge is most likely to answer `409` because someone else already
holds the serial line. Starting at `:37` puts our reads at `:07` and `:37`,
away from the obvious times, at no cost.

### The overnight gap

Between 18:37 and 07:37 nothing is polled. The building is shut and the rate is
near zero, and because the counters are cumulative, the first read of the
morning still captures whatever happened overnight. Nothing is lost but
resolution, during the period where resolution is worth least.

## Scheduling

```
.\scripts\register_task.ps1
```

Registers two Windows scheduled tasks:

- **DEX snapshot archive** -- every 30 minutes, 07:37 to 18:37, daily.
- **DEX snapshot archive check** -- daily at 18:50, reports whether the archive
  is current.

Task Scheduler is the right tool here: it is built in, it survives reboots, and
it keeps its own run history with exit codes. Nothing extra needs installing.

Two settings matter more than the rest:

- `-StartWhenAvailable` runs a missed slot as soon as the machine is available
  again. If the laptop was asleep at 10:07, the poll happens when it wakes
  rather than being skipped. **A late snapshot is worth far more than a missing
  one**, because what we are building is a series of differences.
- `-WakeToRun` lets the machine wake for a scheduled poll.

`register_task.ps1` reads the task back after registering it and fails loudly if
the repetition did not take or the task has no next run time. This is not
paranoia: a `-Once` trigger with a repetition registers perfectly cleanly,
reports "Ready", and then never fires again. That is the exact shape of failure
this archive exists to prevent, so the registration script refuses to report
success without checking.

To remove both tasks:

```
.\scripts\register_task.ps1 -Unregister
```

### Running when nobody is logged in

By default the tasks run as you, which means they only fire while you are logged
on. To run them as SYSTEM instead, so they survive logout and reboot, use an
elevated PowerShell:

```
.\scripts\register_task.ps1 -RunAsSystem
```

No password is stored in either case.

## Telling a stalled scheduler from a quiet machine

This is the failure that actually matters. A task that stopped firing three
weeks ago looks exactly like a task that is working, because both produce
silence. Three things guard against it.

**1. `data/logs/poll.log`** -- one line per run, success or failure:

```
2026-09-21T01:05:02Z level=INFO event=archived file=data\snapshots\20260921T010454Z.dex bytes=4341 lines=210 cached=false age_seconds=0.0 session=fcf24e12ad214de1b637e195f79468f2 sha256=320e4abb364c63c4 elapsed=8.7s exit=0
```

The `sha256` is a prefix of the body's hash. Two consecutive snapshots with the
same hash and a fresh (`cached=false`) read means the machine returned an
identical document -- either genuinely no activity, or a bridge that has stuck.

**2. `data/logs/scheduler.log`** -- written by the PowerShell wrapper, before
and after Python runs. This exists because `poll.log` can only describe runs
that got as far as starting Python. Compare the two:

| `scheduler.log` | `poll.log` | What it means |
|---|---|---|
| has entries | has entries | working normally |
| has entries | no entries | Python is failing to launch |
| no entries | no entries | the task is not firing at all |

**3. `scripts/check_archive.py`** -- works out when the last poll *should* have
happened, given the schedule, and compares it with when one actually did. It
knows about the overnight gap, so it does not raise a false alarm every morning,
and it tolerates two consecutive missed slots before complaining. Exit code 1
means the archive has gone stale.

Task Scheduler's own history is a fourth view, and the exit codes below are
chosen so its "Last Run Result" column is readable on its own.

## Exit codes

| Code | Meaning | What to do |
|---|---|---|
| 0 | A snapshot was archived | Nothing |
| 1 | Unexpected error | Read the `detail=` in the log |
| 2 | `.env` missing or incomplete | Copy `.env.example` to `.env` and fill it in |
| 3 | Tailscale down, or the Pi is not on the tailnet | `tailscale status`, then `tailscale up` |
| 4 | Service unreachable -- `/health` did not answer | The bridge or the Pi is down; tell David |
| 5 | `/health` answered but our key was rejected | `DEX_API_KEY` is wrong; the service itself is fine |
| 6 | The bridge returned an error, including `409` busy | Usually transient; the next slot will pick it up |
| 7 | A `200` whose body is not a usable DEX read | Look in `data/snapshots/invalid/` |
| 8 | Could not write to disk | Check free space and permissions |
| 9 | The wrapper could not find or launch Python | `py -3 --version` under the task's account |

Codes 3, 4 and 5 are deliberately distinct. Because the bridge is reachable only
over Tailscale, a dropped tailnet, a dead service and a bad key all look like
"it doesn't work" if you only try the HTTP call. The script checks Tailscale
first, then `/health` (which needs no key), and only then polls -- so by the
time it fails, it can say which of the three went wrong.

## Retries

There are none by default, on purpose.

A `409` means another caller is mid-read. Waiting and trying again is the start
of a polling loop, and the cost of simply not retrying is close to zero: the
counters are cumulative, so the next scheduled slot recovers everything the
missed one would have seen. The slot is skipped, the log says why, and that is
the end of it.

`--retry-on-conflict` exists for the case where somebody deliberately wants one
extra attempt after a 45-second wait. It is off by default and should stay off
for the scheduled task.

## Known limitations

Worth being upfront about these rather than discovering them in November.

- **The host is a laptop.** `cog-dev-mstrong` sleeps, reboots and travels.
  `-StartWhenAvailable` and `-WakeToRun` cover a lot, but a closed lid on
  battery in a bag will still miss slots. A machine that is always on -- the Pi
  itself, or any always-on box on the tailnet -- is the better long-term home
  for this.
- **The default registration needs a login session.** Tasks run as the current
  user unless `-RunAsSystem` is used, so logging out stops the archive.
- **The health check shares a scheduler with the thing it checks.** If Task
  Scheduler is wholly dead, the check is dead too and stays silent. The real
  backstop is somebody running `check_archive.py` by hand now and then, or
  running it from a different machine.
- **Nothing sends an alert.** The check writes to `data/logs/check.log` and
  returns a non-zero exit code; it does not email anyone. Wiring that up is a
  deliberate next step, not an oversight.

## Design notes

**No third-party dependencies.** `poll_snapshot.py` uses only the Python
standard library, including a nine-line `.env` parser instead of
`python-dotenv`. `requests` would read slightly better, but a scheduled task
runs under a different account with a different `PATH` from your shell, and
"works in my terminal, `ModuleNotFoundError` under the scheduler" is a failure
that hides for exactly as long as nobody checks. There is no virtualenv to
activate and nothing to install.

**Archive first, judge second.** The response is written to disk before it is
validated. A body that cost 8.5 seconds of a working machine's time is never
thrown away because our shape check disliked it.

**Atomic writes.** Each file is written to a `.part` file, flushed, and then
moved into place. If the machine loses power mid-write, the archive holds either
nothing or a complete file -- never a half-written snapshot that looks real.

**Filenames never collide.** Two runs in the same second would otherwise
overwrite each other; the second gets a `-01` suffix instead. Overwriting an
archived snapshot is the one thing this script must never do.
