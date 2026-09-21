#!/usr/bin/env python3
"""Poll the DEX Bridge once and archive the raw response to disk.

This script does exactly one thing: it asks the vending machine for its audit
document and writes what came back, unchanged, into `data/snapshots/`. It does
not parse EVA-DTS, it does not write to a database, and it does not analyse
anything. Those are separate jobs. Keeping the raw text is what makes them
possible later.

Why this matters: the machine reports where its lifetime counters stand *right
now*. There is no "sales last week" endpoint. Period sales only exist as the
difference between two archived reads, so a day with no archive is a day of
demand data that cannot be recovered afterwards.

Each successful run writes two files that share a timestamp:

    data/snapshots/20260920T213234Z.dex       the response body, byte for byte
    data/snapshots/20260920T213234Z.headers   the status line and all headers

The timestamp is UTC, taken at the moment the request is sent.

Run it by hand:

    python scripts/poll_snapshot.py
    python scripts/poll_snapshot.py --dry-run    # checks only, does not poll

Be aware of what a poll costs. A read is about 8.5 seconds of live serial
conversation with a machine that is in service and taking people's money, and
results are shared class-wide for 30 seconds. The API documentation says it
plainly: polling in a loop gains you nothing and is unkind to the machine. This
script therefore makes exactly one request per run, and retrying is off by
default.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------
# Exit codes
#
# The scheduler records these, so each failure gets its own number. When you
# are looking at Task Scheduler history a week later, "last result: 3" should
# tell you what went wrong without needing the log.
# --------------------------------------------------------------------------
EXIT_OK = 0             # a snapshot was archived (fresh or cached)
EXIT_CONFIG = 2         # .env is missing or incomplete -- our fault, not the machine's
EXIT_TAILSCALE = 3      # the tailnet is down, or the Pi is not on it
EXIT_UNREACHABLE = 4    # network or service failure -- /health did not answer
EXIT_AUTH = 5           # /health answered but our key was rejected
EXIT_BRIDGE = 6         # the bridge answered with an error status
EXIT_INVALID = 7        # we got a 200, but the document is not a usable DEX read
EXIT_WRITE = 8          # could not write to disk

# The Tailscale machine name of the Pi that hosts the bridge. Override with
# DEX_TAILSCALE_PEER in .env if it is ever renamed.
DEFAULT_PEER = "aihub-pi"

# A healthy read is about 210 lines and roughly 4 KB. These bounds are
# deliberately loose -- they are here to catch a truncated or empty body, not
# to validate the document's contents.
MIN_LINES = 50

# Rotate the log once it passes this size. At about 200 bytes per run and a few
# dozen runs a day, this takes years to reach; it exists so the file cannot
# grow without limit if something starts logging in a tight loop.
MAX_LOG_BYTES = 5 * 1024 * 1024


class PollError(Exception):
    """A failure we understand well enough to name.

    `reason` is a short machine-readable token that goes into the log line;
    `exit_code` is what the process returns to the scheduler.
    """

    def __init__(self, reason: str, detail: str, exit_code: int):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.exit_code = exit_code


# --------------------------------------------------------------------------
# Paths, configuration and logging
# --------------------------------------------------------------------------

def repo_root() -> Path:
    """The repository root, found relative to this file rather than the cwd.

    The scheduler may start us from anywhere, so nothing here may depend on
    the current working directory.
    """
    return Path(__file__).resolve().parent.parent


def load_env(env_path: Path) -> dict[str, str]:
    """Read a .env file into a dictionary.

    Deliberately minimal: `KEY=value` lines, `#` comments, blank lines ignored,
    and surrounding quotes stripped. We parse it ourselves so the script has no
    third-party dependencies at all -- see the note in docs/snapshot-archive.md
    about why that matters for a scheduled task.
    """
    if not env_path.exists():
        raise PollError(
            "no_env_file",
            f"{env_path} does not exist. Copy .env.example to .env and fill it in.",
            EXIT_CONFIG,
        )

    values: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def log_line(log_path: Path, message: str) -> None:
    """Append one line to the log and echo it to stdout.

    Every run writes exactly one of these, success or failure. That is the
    whole point: a log with no line for 10:07 means the scheduler did not run,
    which is a different problem from a machine that sold nothing.
    """
    print(message, flush=True)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if log_path.exists() and log_path.stat().st_size > MAX_LOG_BYTES:
            log_path.replace(log_path.with_suffix(log_path.suffix + ".1"))
        with log_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(message + "\n")
    except OSError as error:
        # Never let a logging problem hide the result of the poll itself.
        print(f"warning: could not write to {log_path}: {error}", file=sys.stderr)


def quote(value: str) -> str:
    """Wrap a value in quotes if it contains anything that breaks key=value logs."""
    text = str(value).replace('"', "'").replace("\n", " ").replace("\r", " ")
    return f'"{text}"' if (" " in text or not text) else text


def utc_stamp(moment: datetime) -> str:
    """Format a UTC time the way the archive names its files: 20260920T213234Z."""
    return moment.strftime("%Y%m%dT%H%M%SZ")


# --------------------------------------------------------------------------
# Preflight checks
# --------------------------------------------------------------------------

def check_tailscale(peer_name: str) -> str:
    """Confirm the tailnet is up and the Pi is on it.

    The bridge is not on the public internet, so a dropped tailnet looks
    exactly like a dead service if you only try the HTTP call. Checking first
    means the log says "tailscale_down" instead of a generic timeout.

    If the Tailscale CLI is not on PATH we log a note and carry on -- a missing
    command is not evidence that the network is broken, and /health will catch
    a genuine outage a moment later.
    """
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except FileNotFoundError:
        return "tailscale CLI not found on PATH; skipped the check"
    except (subprocess.SubprocessError, OSError) as error:
        return f"tailscale status did not run ({error}); skipped the check"

    if result.returncode != 0 or not result.stdout.strip():
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise PollError(
            "tailscale_down",
            detail[0] if detail else "tailscale status returned a failure",
            EXIT_TAILSCALE,
        )

    try:
        status = json.loads(result.stdout)
    except json.JSONDecodeError:
        return "tailscale status returned output we could not parse; skipped the check"

    backend = status.get("BackendState", "unknown")
    if backend != "Running":
        raise PollError(
            "tailscale_down",
            f"Tailscale backend state is {backend!r}, expected 'Running'. Run: tailscale up",
            EXIT_TAILSCALE,
        )

    # Find the Pi among the peers. If we cannot see it by name we say so but do
    # not stop -- the machine may have been renamed, and DEX_BASE_URL may point
    # at its address directly.
    for peer in (status.get("Peer") or {}).values():
        if peer.get("HostName") == peer_name:
            if peer.get("Online") is False:
                raise PollError(
                    "peer_offline",
                    f"Tailscale is up but peer {peer_name!r} is offline. The Pi is not reachable.",
                    EXIT_TAILSCALE,
                )
            return f"tailnet up, {peer_name} online"
    return f"tailnet up, but no peer named {peer_name!r} was listed"


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def http_request(
    method: str,
    url: str,
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, str, list[tuple[str, str]], bytes]:
    """Make one HTTP request and return (status, reason, headers, body).

    Error statuses are returned like any other response rather than raised, so
    the caller can decide what a 409 or a 502 means. Only a genuine transport
    failure -- no route, refused connection, timeout -- raises.
    """
    data = b"" if method == "POST" else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return (
                response.status,
                response.reason,
                list(response.headers.items()),
                response.read(),
            )
    except urllib.error.HTTPError as error:
        # An HTTPError is still a response: it has a status, headers and a body.
        return error.code, error.reason, list(error.headers.items()), error.read()
    except urllib.error.URLError as error:
        raise PollError("unreachable", f"{url} could not be reached: {error.reason}", EXIT_UNREACHABLE)
    except TimeoutError:
        raise PollError("timeout", f"{url} did not respond within {timeout:.0f}s", EXIT_UNREACHABLE)
    except OSError as error:
        raise PollError("unreachable", f"{url} could not be reached: {error}", EXIT_UNREACHABLE)


def get_health(base_url: str, timeout: float) -> dict:
    """Call GET /health, which needs no API key.

    This is how we tell "the service is down" apart from "our key is wrong".
    If /health answers and the poll is then rejected, the problem is the key.
    """
    status, reason, _, body = http_request("GET", f"{base_url}/health", {}, timeout)
    if status != 200:
        raise PollError(
            "health_not_ok",
            f"GET /health returned {status} {reason}",
            EXIT_UNREACHABLE,
        )
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise PollError("health_not_ok", "GET /health did not return JSON", EXIT_UNREACHABLE)


def poll_machine(
    base_url: str,
    api_key: str,
    timeout: float,
) -> tuple[datetime, float, list[tuple[str, str]], bytes]:
    """Call POST /api/v1/poll once and return the response.

    Returns the UTC time the request was sent (which becomes the archive
    filename), how long it took, the response headers and the raw body.
    """
    url = f"{base_url}/api/v1/poll"
    headers = {"X-API-Key": api_key, "Accept": "text/plain"}

    sent_at = datetime.now(timezone.utc)
    started = time.monotonic()
    status, reason, response_headers, body = http_request("POST", url, headers, timeout)
    elapsed = time.monotonic() - started

    if status == 200:
        return sent_at, elapsed, response_headers, body

    # Anything else: say what happened in terms the next person can act on.
    snippet = body.decode("utf-8", errors="replace").strip()[:200]
    if status in (401, 403):
        raise PollError(
            "auth_rejected",
            f"POST /api/v1/poll returned {status} {reason}. /health answered, so the "
            f"service is up and DEX_API_KEY is the problem. Response: {snippet}",
            EXIT_AUTH,
        )
    if status == 409:
        raise PollError(
            "bridge_busy",
            f"POST /api/v1/poll returned 409 {reason} -- another caller holds the serial "
            f"line. Skipping this slot; the next scheduled run will pick it up. Response: {snippet}",
            EXIT_BRIDGE,
        )
    raise PollError(
        "bridge_error",
        f"POST /api/v1/poll returned {status} {reason}. Response: {snippet}",
        EXIT_BRIDGE,
    )


# --------------------------------------------------------------------------
# Validation and archiving
# --------------------------------------------------------------------------

def validate_document(body: bytes, response_headers: dict[str, str]) -> list[str]:
    """Check the body looks like a whole DEX read. Returns a list of problems.

    This is a shape check, not a parse. A real EVA-DTS document runs from a
    `DXS` record to a `DXE` record; if the first and last records are there and
    the byte count matches what the server promised, we have the whole thing.
    Whether its contents make sense is the parser's job, later.
    """
    problems: list[str] = []

    if not body:
        return ["response body was empty"]

    declared = response_headers.get("content-length")
    if declared and declared.isdigit() and int(declared) != len(body):
        problems.append(f"truncated: content-length said {declared}, we received {len(body)}")

    text = body.decode("utf-8", errors="replace")
    lines = [line for line in text.splitlines() if line.strip()]

    if not lines:
        return ["response body contained no records"]
    if not lines[0].startswith("DXS"):
        problems.append(f"first record is {lines[0][:24]!r}, expected it to start with DXS")
    if not lines[-1].startswith("DXE"):
        problems.append(f"last record is {lines[-1][:24]!r}, expected it to start with DXE")
    if len(lines) < MIN_LINES:
        problems.append(f"only {len(lines)} records, expected at least {MIN_LINES}")

    return problems


def render_headers(
    status: int,
    reason: str,
    response_headers: list[tuple[str, str]],
) -> bytes:
    """Build the `.headers` file: the status line, the headers, a blank line.

    Written with CRLF to match how they arrived on the wire, and to match the
    snapshot that was archived by hand before this script existed.
    """
    lines = [f"HTTP/1.1 {status} {reason}"]
    lines.extend(f"{name.lower()}: {value}" for name, value in response_headers)
    lines.append("")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def unique_paths(directory: Path, stamp: str) -> tuple[Path, Path]:
    """Pick a `.dex`/`.headers` pair that does not already exist.

    Two runs in the same second would otherwise overwrite each other. This
    should never happen on a 30-minute schedule, but overwriting an archived
    snapshot is the one thing this script must never do.
    """
    candidate = stamp
    suffix = 0
    while (directory / f"{candidate}.dex").exists():
        suffix += 1
        candidate = f"{stamp}-{suffix:02d}"
    return directory / f"{candidate}.dex", directory / f"{candidate}.headers"


def write_atomically(path: Path, payload: bytes) -> None:
    """Write bytes to a temporary file, then move it into place.

    If the machine loses power mid-write, the archive is left holding either
    the old state or the complete new file -- never a half-written snapshot
    that looks real.
    """
    temporary = path.with_name(path.name + ".part")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def archive(
    directory: Path,
    stamp: str,
    body: bytes,
    header_blob: bytes,
) -> Path:
    """Write the body and headers into `directory`. Returns the `.dex` path."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        dex_path, headers_path = unique_paths(directory, stamp)
        write_atomically(dex_path, body)
        write_atomically(headers_path, header_blob)
        return dex_path
    except OSError as error:
        raise PollError("write_failed", f"could not write to {directory}: {error}", EXIT_WRITE)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Poll the DEX Bridge once and archive the raw response.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the Tailscale and /health checks and stop. Does not read the machine.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Seconds to wait for the poll. A read is about 8.5s, but it may queue "
             "behind another caller. Default: 60.",
    )
    parser.add_argument(
        "--retry-on-conflict",
        action="store_true",
        help="If the bridge is busy (409), wait 45s and try once more. Off by default: "
             "missing one slot is cheap, because the machine's counters are cumulative.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Where to write snapshots. Default: data/snapshots under the repo root.",
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    root = repo_root()
    log_path = root / "data" / "logs" / "poll.log"
    snapshot_dir = args.out_dir if args.out_dir else root / "data" / "snapshots"
    now = lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731

    config = load_env(root / ".env")
    # A real environment variable beats the .env file. This is the usual
    # convention, and it is how the failure paths get tested without bothering
    # the machine -- see docs/snapshot-archive.md.
    for key in ("DEX_BASE_URL", "DEX_API_KEY", "DEX_TAILSCALE_PEER"):
        if os.environ.get(key):
            config[key] = os.environ[key]

    base_url = config.get("DEX_BASE_URL", "").rstrip("/")
    api_key = config.get("DEX_API_KEY", "")
    peer_name = config.get("DEX_TAILSCALE_PEER") or DEFAULT_PEER

    if not base_url:
        raise PollError("no_base_url", "DEX_BASE_URL is not set in .env", EXIT_CONFIG)
    if not api_key:
        raise PollError("no_api_key", "DEX_API_KEY is not set in .env", EXIT_CONFIG)

    # Preflight 1: is the tailnet up? The bridge is not on the public internet.
    tailscale_note = check_tailscale(peer_name)

    # Preflight 2: is the service alive? This needs no key, so if it answers and
    # the poll is then rejected, we know the key is the problem.
    health = get_health(base_url, timeout=20.0)
    if not health.get("transmit_enabled", True):
        raise PollError(
            "transmit_disabled",
            "The bridge reports transmit_enabled=false: it can listen but cannot drive "
            "the serial line, so a poll would not reach the machine.",
            EXIT_BRIDGE,
        )

    if args.dry_run:
        log_line(
            log_path,
            f"{now()} level=INFO event=dry_run tailscale={quote(tailscale_note)} "
            f"health={quote(health.get('status', '?'))} "
            f"version={quote(health.get('version', '?'))} "
            f"uptime={quote(health.get('uptime', '?'))} "
            f"transmit_enabled={health.get('transmit_enabled')} "
            f"note={quote('no poll was made')}",
        )
        return EXIT_OK

    # The one and only request that touches the machine.
    try:
        sent_at, elapsed, response_headers, body = poll_machine(base_url, api_key, args.timeout)
    except PollError as error:
        if not (error.reason == "bridge_busy" and args.retry_on_conflict):
            raise
        log_line(
            log_path,
            f"{now()} level=WARN event=poll_retry reason=bridge_busy "
            f"detail={quote('waiting 45s for the other caller to finish, then trying once more')}",
        )
        time.sleep(45)
        sent_at, elapsed, response_headers, body = poll_machine(base_url, api_key, args.timeout)

    header_map = {name.lower(): value for name, value in response_headers}
    cached = header_map.get("x-poll-cached", "?")
    age = header_map.get("x-poll-age-seconds", "?")
    session = header_map.get("x-poll-session-id", "?")
    digest = hashlib.sha256(body).hexdigest()[:16]
    line_count = len(body.decode("utf-8", errors="replace").splitlines())

    problems = validate_document(body, header_map)

    # Archive first, judge second. A body we already paid 8.5 seconds of the
    # machine's time for is never thrown away -- but a suspect one goes in its
    # own directory so that `data/snapshots/*.dex` stays trustworthy.
    target_dir = snapshot_dir if not problems else snapshot_dir / "invalid"
    stamp = utc_stamp(sent_at)
    written = archive(target_dir, stamp, body, render_headers(200, "OK", response_headers))
    relative = written.relative_to(root) if written.is_relative_to(root) else written

    common = (
        f"file={quote(str(relative))} bytes={len(body)} lines={line_count} "
        f"cached={cached} age_seconds={age} session={session} sha256={digest} "
        f"elapsed={elapsed:.1f}s"
    )

    if problems:
        log_line(
            log_path,
            f"{now()} level=ERROR event=invalid_snapshot {common} "
            f"problems={quote('; '.join(problems))} exit={EXIT_INVALID}",
        )
        return EXIT_INVALID

    # Record the newest good snapshot so other tools do not have to scan the
    # directory. Machine-local and gitignored.
    try:
        (root / ".last_snapshot").write_text(stamp + "\n", encoding="utf-8", newline="\n")
    except OSError as error:
        print(f"warning: could not update .last_snapshot: {error}", file=sys.stderr)

    log_line(log_path, f"{now()} level=INFO event=archived {common} exit={EXIT_OK}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    log_path = repo_root() / "data" / "logs" / "poll.log"
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        return run(args)
    except PollError as error:
        log_line(
            log_path,
            f"{timestamp} level=ERROR event=poll_failed reason={error.reason} "
            f"detail={quote(error.detail)} exit={error.exit_code}",
        )
        return error.exit_code
    except Exception as error:  # noqa: BLE001 -- the scheduler must always get a logged reason
        log_line(
            log_path,
            f"{timestamp} level=ERROR event=poll_failed reason=unexpected "
            f"detail={quote(f'{type(error).__name__}: {error}')} exit=1",
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
