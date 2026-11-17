# Docker recovery (Windows laptop)

Docker Desktop on Kevin's laptop sometimes wedges. `docker ps` still answers, but new
containers hang or stay in "Created". This page tells you what to run, and records what
we know about the cause.

## When Docker hangs, run this first

```
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\docker_recover.ps1
```

Don't improvise restarts. The script does the whole escalation, and every step has a timeout:

0. **Start.** If the engine doesn't answer and Docker Desktop isn't running at all (after a
   reboot, for example), the script starts it and waits for the engine. With `-CheckOnly`
   it reports this and exits 1 instead.
1. **Probe.** `docker version`, then `docker run --rm alpine:3 true` (60 s). If both work,
   the script stops and restarts nothing.
2. **Recover.** `docker desktop stop --force` (if that hangs, it kills the Docker Desktop
   processes), then `wsl --shutdown`, then `wsl -d docker-desktop -e true` to check that WSL
   can boot a VM, then `docker desktop start` and a wait for the engine.
3. **Check again**, then restart every container that was running before. Not all of them
   restart by themselves: `agentic-vending-local-db` has restart policy `no`. The script also
   runs the `KsaiWslKeepalive` task, because `wsl --shutdown` stops the WSL sshd behind
   `ssh laptop`.
4. **Clean up.** Scratch containers (see below) that have been stuck in "Created" for more
   than 10 minutes are removed. Other stuck containers are listed, not removed.

| Exit code | Meaning | What to do |
|---|---|---|
| 0 | Docker can run containers (healthy, or recovered) | Continue |
| 1 | Still broken after the full restart, or Docker Desktop didn't answer within 120 s of starting | Run `docker desktop diagnose`, tell Kevin |
| 2 | The WSL service itself is wedged | Elevated PowerShell: `Restart-Service WSLService -Force`, run the script again. If that hangs, reboot. |
| 3 | Probe image `alpine:3` is missing and can't be pulled | Network problem, not Docker |

Options: `-CheckOnly` probes and never restarts anything. `-DryRun -SimulateHang` prints the
escalation without stopping anything (use it to test the script). Each run adds one line to
`data\logs\docker-recover.log`, so we can see how often this happens.

A recovery stops every running container for about a minute. Don't run it in a loop or
on a schedule.

## Throwaway containers: label them, let Docker pick the port

Four stuck containers had piled up by 14 November. Two of them never started because their
fixed host port was already taken (`overnight-db2` asked for 55432, which the compose
database holds; `review-pr111-pg` asked for 55499, which `vending-dash-test` holds). For a
scratch database:

```
docker run -d --rm --label agentic-vending.scratch=true -p 127.0.0.1::5432 -e POSTGRES_PASSWORD=x postgres:17
docker port <container> 5432        # the host port Docker picked
docker rm -f -v <container>         # when you are done
```

- `-p 127.0.0.1::5432` lets Docker choose a free host port, so it can't collide.
- `--rm` removes the container (and its anonymous volume) when it stops.
- The label lets `docker_recover.ps1` remove it if its start hangs, which `--rm` can't cover.

The repo's own code doesn't leave containers behind: `docker compose up -d db` is the one
long-lived database, and the tests reuse `DATABASE_URL` or skip.

## What we know about the cause (investigated 15 November 2026)

**Most likely: host network changes break the Docker VM's networking under WSL mirrored
mode.** `%USERPROFILE%\.wslconfig` sets `networkingMode=mirrored`. It is on purpose:
the WSL sshd behind `ssh laptop` needs it (see `kevinsykes-ai/docs/LAPTOP-REMOTE-OPS.md`).
Evidence:

- Docker's backend log shows the host losing its route to the engine
  (`still dialing 192.168.65.7:2376: no route to host`) seven times between 12 and 14 November.
  All seven came within a few minutes of a network change: six on wake from sleep (Wi-Fi
  and Tailscale reconnecting), one on a Tailscale-only reconnect at 17:53 on 14 November.
- The 14 November hang followed that reconnect. At 18:07 a new container's create took 17 s
  and its start failed. By 18:33 `wsl.exe` calls failed with `Wsl/Service/WSAENOBUFS` and
  `ERROR_TIMEOUT`.
- Microsoft's WSL troubleshooting page lists a known issue: under mirrored mode, Docker
  Desktop containers with published ports "will fail to be created"
  ([source](https://learn.microsoft.com/en-us/windows/wsl/troubleshooting)).
- Between 6 and 14 November the WSL VM ran for 8 days without a restart, since the
  idle timeouts are `-1`. The Hyper-V switch logged about 30,000 "NIC connect" events
  for it, rising from about 100 a day to 5,700 a day. `wsl --shutdown` resets all of
  this, which is why the manual fix worked.

**Possible, not proven: kernel memory pressure.** WSAENOBUFS means Windows ran out of socket
buffer space. On 15 November, after 9 days of uptime, nonpaged pool was 1.96 GB, which is
high for 16 GB of RAM, and free memory was 0.4 to 1.5 GB. We have no earlier figure to
compare. A reboot clears both.

**Ruled out:** the system proxy (off), TCP port exhaustion (about 300 connections, no
Tcpip 4227/4231 events), and the Hyper-V result `0xC0370103` in the event log (it means
"operation started", not an error).

**Not known:** why `dbx-explorer-test` (11 Nov) and `overnight-throwaway-db` (12 Nov) never
started. The engine's logs from those days had already rotated away. Something polls the
engine for container stats every few seconds, which fills the VM log in about 19 minutes.
