#!/usr/bin/env python3
"""Report on the health of the snapshot archive.

This answers one question: is the archiver still running?

That question is harder than it sounds, and it is the real risk in this
project. A scheduled task that quietly stopped firing three weeks ago looks
exactly like a scheduled task that is working, because both produce silence.
The per-run log in `data/logs/poll.log` cannot tell you either, because a task
that never started never writes a line.

So this script looks at the archive from the outside. It works out when the
last poll *should* have happened, given the schedule, and compares that with
when one actually did.

It reads only filenames, timestamps and the log. It does not open a `.dex`
file or parse anything -- that is the parser's job, later.

    python scripts/check_archive.py

Exit code 0 means the archive is current. Exit code 1 means it has gone stale
and somebody needs to look at the scheduler.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, time as clock_time, timedelta, timezone
from pathlib import Path

# --------------------------------------------------------------------------
# The schedule.
#
# These must match the trigger created by scripts/register_task.ps1. If you
# change the cadence, change it in both places -- and in
# docs/snapshot-archive.md, which explains why these numbers are what they are.
#
# The times are local (institute) time. The :37 is not a typo: starting off the
# hour keeps us from landing on :00 and :30 at the same instant as every other
# group, which is when the bridge is most likely to answer 409.
# --------------------------------------------------------------------------
WINDOW_START = clock_time(7, 37)
WINDOW_END = clock_time(18, 37)
INTERVAL_MINUTES = 30

# How far behind schedule the archive may fall before we call it stale.
# 75 minutes lets two consecutive slots fail without raising an alarm, which is
# the normal cost of somebody else holding the serial line.
DEFAULT_GRACE_MINUTES = 75

SNAPSHOT_NAME = re.compile(r"^(\d{8}T\d{6}Z)(?:-\d+)?\.dex$")


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def snapshot_time(path: Path) -> datetime | None:
    """Recover the UTC time a snapshot was taken from its filename."""
    match = SNAPSHOT_NAME.match(path.name)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def list_snapshots(directory: Path) -> list[tuple[datetime, Path]]:
    """Every well-named snapshot in a directory, oldest first."""
    if not directory.is_dir():
        return []
    found = []
    for path in directory.glob("*.dex"):
        taken = snapshot_time(path)
        if taken is not None:
            found.append((taken, path))
    return sorted(found)


def expected_last_poll(now_local: datetime) -> datetime:
    """The most recent moment the scheduler should have run.

    Inside the daily window this is the latest slot that has already come
    round. Before the window opens it is yesterday's final slot; after it
    closes, today's. Without this, the check would scream every morning about
    the overnight gap, which is by design and not a fault.
    """
    today_start = now_local.replace(
        hour=WINDOW_START.hour, minute=WINDOW_START.minute, second=0, microsecond=0
    )
    today_end = now_local.replace(
        hour=WINDOW_END.hour, minute=WINDOW_END.minute, second=0, microsecond=0
    )

    if now_local < today_start:
        return today_end - timedelta(days=1)
    if now_local >= today_end:
        return today_end

    elapsed = (now_local - today_start).total_seconds() / 60
    slots_passed = int(elapsed // INTERVAL_MINUTES)
    return today_start + timedelta(minutes=slots_passed * INTERVAL_MINUTES)


def describe_age(delta: timedelta) -> str:
    """A human-readable gap, e.g. '2 days 3h 14m' or '41m'."""
    total_minutes = int(delta.total_seconds() // 60)
    if total_minutes < 1:
        return "less than a minute"
    days, remainder = divmod(total_minutes, 60 * 24)
    hours, minutes = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def read_log_tail(log_path: Path, count: int) -> list[str]:
    """The last `count` lines of the poll log, newest last."""
    if not log_path.exists():
        return []
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return [line for line in lines if line.strip()][-count:]


def field(line: str, name: str) -> str:
    """Pull `name=value` out of one of our log lines."""
    match = re.search(rf"\b{name}=(\"[^\"]*\"|\S+)", line)
    return match.group(1).strip('"') if match else "?"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report on snapshot archive health.")
    parser.add_argument(
        "--grace-minutes",
        type=int,
        default=DEFAULT_GRACE_MINUTES,
        help=f"How far behind schedule is acceptable. Default: {DEFAULT_GRACE_MINUTES}.",
    )
    parser.add_argument("--log-lines", type=int, default=25, help="How many recent runs to summarise.")
    args = parser.parse_args(argv)

    root = repo_root()
    snapshot_dir = root / "data" / "snapshots"
    log_path = root / "data" / "logs" / "poll.log"

    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone()

    good = list_snapshots(snapshot_dir)
    invalid = list_snapshots(snapshot_dir / "invalid")

    print(f"Snapshot archive status -- {now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    print()
    print(f"  Archive directory : {snapshot_dir}")
    print(f"  Snapshots total   : {len(good)}")
    print(f"  Rejected as bad   : {len(invalid)}")

    if not good:
        print()
        print("STALE: there are no snapshots at all. Nothing has ever been archived here.")
        return 1

    newest_time, newest_path = good[-1]
    oldest_time, _ = good[0]
    print(f"  Oldest snapshot   : {oldest_time.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    print(f"  Newest snapshot   : {newest_path.name}  ({describe_age(now_utc - newest_time)} ago)")
    print()

    last_24h = [item for item in good if now_utc - item[0] <= timedelta(hours=24)]
    last_7d = [item for item in good if now_utc - item[0] <= timedelta(days=7)]
    slots_per_day = int((
        datetime.combine(now_local.date(), WINDOW_END)
        - datetime.combine(now_local.date(), WINDOW_START)
    ).total_seconds() // 60 // INTERVAL_MINUTES) + 1
    print(f"  Last 24 hours     : {len(last_24h)} snapshots (a full day of the schedule is {slots_per_day})")
    print(f"  Last 7 days       : {len(last_7d)} snapshots")
    print()

    # What the log says about recent runs. This distinguishes "the scheduler is
    # running but every poll fails" from "the scheduler is not running at all".
    tail = read_log_tail(log_path, args.log_lines)
    if not tail:
        print(f"  Log file          : {log_path} (no entries yet)")
    else:
        archived = sum(1 for line in tail if "event=archived" in line)
        failed = len(tail) - archived
        print(f"  Log file          : {log_path}")
        print(f"  Last {len(tail):>2} runs      : {archived} archived, {failed} failed")
        failures = [line for line in tail if "level=ERROR" in line]
        if failures:
            last = failures[-1]
            print(f"  Most recent error : {last.split(' ', 1)[0]}  reason={field(last, 'reason')}")
            print(f"                      {field(last, 'detail')[:100]}")
        cached = sum(1 for line in tail if "cached=true" in line)
        if cached:
            print(f"  Served from cache : {cached} of the last {len(tail)} runs")
    print()

    # The verdict.
    expected = expected_last_poll(now_local)
    behind = expected - newest_time.astimezone()
    if behind > timedelta(minutes=args.grace_minutes):
        print(
            f"STALE: the last poll should have been at {expected.strftime('%Y-%m-%d %H:%M %Z')}, "
            f"but the newest snapshot is {describe_age(behind)} older than that."
        )
        print("Check that the scheduled task is enabled and that its last result was 0:")
        print("  Get-ScheduledTask -TaskName 'DEX snapshot archive' | Get-ScheduledTaskInfo")
        return 1

    print(f"OK: the archive is current. Last expected poll was {expected.strftime('%H:%M %Z')}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
