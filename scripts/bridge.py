#!/usr/bin/env python3
"""Talk to the DEX Bridge -- the Raspberry Pi that reads our vending machine.

One command per question you might have about the machine or our account:

    python scripts/bridge.py health                 is the bridge up? (no key needed)
    python scripts/bridge.py slots                  which selections are ours
    python scripts/bridge.py products               what we can order (the catalog)
    python scripts/bridge.py history                stored polls for today (UTC)
    python scripts/bridge.py history --start 2026-10-01 --end 2026-10-07
    python scripts/bridge.py raw SESSION_ID         one stored poll, as raw EVA-DTS text
    python scripts/bridge.py latest                 the newest stored poll, raw
    python scripts/bridge.py latest --summary       price and lifetime sales for our slots
    python scripts/bridge.py request REQUEST_ID     where a purchase request has got to
    python scripts/bridge.py poll --live            read the machine right now (see below)

Settings come from `.env` at the repository root (copy `.env.example`), or from
the environment: `DEX_BASE_URL` and `DEX_API_KEY`. You must be on Tailscale --
the Pi is not on the public internet. The key is never printed.

Two things this script deliberately does not do:

- **It does not read the machine unless you ask twice.** A live poll is about
  8.5 seconds of serial conversation with a machine that is in service and
  taking people's money. The bridge stores every poll anyone makes, and David's
  poller reads the machine every hour, so `latest` is almost always what you
  want. `poll` refuses to run without `--live`.
- **It cannot submit a purchase request.** Submitting spends real money. The
  agent's `submit_purchase_request` tool is meant to be the only way to buy,
  because that is where the balance, price and halt checks live. Reading a
  request's status is fine, so `request` is here.

Standard library only, so it runs anywhere Python 3.11 does with nothing to
install.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Exit codes, so a wrapper script can tell failures apart.
EXIT_OK = 0
EXIT_CONFIG = 2         # .env missing or incomplete
EXIT_UNREACHABLE = 3    # no answer at all -- usually Tailscale is not connected
EXIT_AUTH = 4           # the bridge rejected our key
EXIT_BRIDGE = 5         # the bridge answered with some other error
EXIT_REFUSED = 6        # this script declined to do it (for example `poll` without `--live`)

# A live poll takes about 8.5 s; give it room. Everything else is quick.
TIMEOUT_SECONDS = 15
POLL_TIMEOUT_SECONDS = 30


class BridgeError(Exception):
    def __init__(self, message: str, exit_code: int):
        super().__init__(message)
        self.exit_code = exit_code


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def repo_root() -> Path:
    """Found relative to this file, so the script works from any directory."""
    return Path(__file__).resolve().parent.parent


def load_env(env_path: Path) -> dict[str, str]:
    """Read `KEY=value` lines from a .env file. Same rules as `poll_snapshot.py`."""
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def settings() -> tuple[str, str]:
    """Return (base_url, api_key). Real environment variables win over .env."""
    env = load_env(repo_root() / ".env")
    base_url = os.environ.get("DEX_BASE_URL") or env.get("DEX_BASE_URL", "")
    api_key = os.environ.get("DEX_API_KEY") or env.get("DEX_API_KEY", "")
    if not base_url:
        raise BridgeError(
            "DEX_BASE_URL is not set. Copy .env.example to .env and fill it in.",
            EXIT_CONFIG,
        )
    return base_url.rstrip("/"), api_key


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def call(method: str, path: str, *, params: dict[str, str] | None = None,
         needs_key: bool = True, timeout: float = TIMEOUT_SECONDS) -> tuple[bytes, dict[str, str]]:
    """Make one request and return (body, headers). Raises BridgeError on failure.

    Error messages say what to check next, because "connection refused" on its
    own does not tell a new teammate that Tailscale is the usual cause.
    """
    base_url, api_key = settings()
    if needs_key and not api_key:
        raise BridgeError(
            "DEX_API_KEY is not set. It is in the Postman environment course staff "
            "emailed; ask a teammate if you do not have it.",
            EXIT_CONFIG,
        )

    url = base_url + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, method=method)
    if needs_key:
        request.add_header("X-API-Key", api_key)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(), dict(response.headers.items())
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        detail = body
        try:
            parsed = json.loads(body)
            detail = parsed.get("error") or parsed.get("detail") or body
        except (json.JSONDecodeError, AttributeError):
            pass
        if error.code in (401, 403):
            raise BridgeError(
                f"{error.code}: the bridge rejected our key ({detail}). "
                "Check DEX_API_KEY in .env.",
                EXIT_AUTH,
            ) from None
        if error.code == 409:
            raise BridgeError(
                f"409: someone else is reading the machine right now ({detail}). "
                "Wait a minute, or use `latest` instead.",
                EXIT_BRIDGE,
            ) from None
        if error.code == 429:
            raise BridgeError(f"429: too many requests ({detail}). Slow down.", EXIT_BRIDGE) from None
        raise BridgeError(f"{error.code}: {detail}", EXIT_BRIDGE) from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise BridgeError(
            f"Could not reach {base_url} ({reason}). Is Tailscale connected? "
            "Run `tailscale status` and look for aihub-pi.",
            EXIT_UNREACHABLE,
        ) from None


def get_json(path: str, **kwargs) -> object:
    body, _ = call("GET", path, **kwargs)
    return json.loads(body)


def print_json(value: object) -> None:
    print(json.dumps(value, indent=2))


# --------------------------------------------------------------------------
# EVA-DTS, just enough for `latest --summary`
# --------------------------------------------------------------------------

def summarize(dex_text: str, our_slots: list[str]) -> list[dict[str, object]]:
    """Price and lifetime units sold for each of our slots.

    Records are `*`-separated, one per line. `PA1*<selection>*<price>` opens a
    selection, and the `PA2*<units>*<value>*...` line after it holds that
    selection's lifetime counters. Currency is an integer; `ID4-01` (the first
    field after the tag, `ID4*2*...`) says how many decimal places it carries.

    This does not validate the `G85` checksum, so treat it as a quick look, not
    as data to build on. The real parser belongs in the history sync.
    """
    decimals = 2
    rows: dict[str, dict[str, object]] = {}
    current: str | None = None
    for line in dex_text.splitlines():
        fields = line.strip().split("*")
        tag = fields[0]
        if tag == "ID4" and len(fields) > 1 and fields[1].isdigit():
            decimals = int(fields[1])
        elif tag == "PA1" and len(fields) > 2:
            current = fields[1]
            rows[current] = {"slot": current, "price_cents": int(fields[2] or 0)}
        elif tag == "PA2" and current is not None and len(fields) > 2:
            rows[current]["lifetime_units"] = int(fields[1] or 0)
            rows[current]["lifetime_value_cents"] = int(fields[2] or 0)
            current = None

    scale = 10 ** decimals
    result = []
    for slot in our_slots:
        row = rows.get(slot, {"slot": slot})
        result.append({
            "slot": slot,
            "price": f"${row['price_cents'] / scale:.2f}" if "price_cents" in row else None,
            "lifetime_units": row.get("lifetime_units"),
            "lifetime_sales": f"${row['lifetime_value_cents'] / scale:,.2f}"
            if "lifetime_value_cents" in row else None,
        })
    return result


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def cmd_health(args: argparse.Namespace) -> None:
    print_json(get_json("/health", needs_key=False))


def cmd_slots(args: argparse.Namespace) -> None:
    print_json(get_json("/api/v1/slots"))


def cmd_products(args: argparse.Namespace) -> None:
    print_json(get_json("/api/v1/products"))


def cmd_history(args: argparse.Namespace) -> None:
    # The bridge takes UTC days. "Today" here means today in UTC, which starts
    # at 8 pm the evening before in Rochester (EDT).
    start = args.start or utc_today()
    end = args.end or start
    print_json(get_json("/api/v1/history",
                        params={"start": start, "end": end, "limit": str(args.limit)}))


def cmd_raw(args: argparse.Namespace) -> None:
    body, _ = call("GET", f"/api/v1/history/{urllib.parse.quote(args.session_id)}/raw")
    write_text(body, args.out)


def cmd_latest(args: argparse.Namespace) -> None:
    # History is oldest first. Yesterday and today together always include the
    # newest poll, since David's poller runs every hour.
    today = datetime.now(timezone.utc)
    yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    listing = get_json("/api/v1/history",
                       params={"start": yesterday, "end": today.strftime("%Y-%m-%d"), "limit": "500"})
    polls = listing.get("polls", [])
    if not polls:
        raise BridgeError("No stored polls in the last two UTC days.", EXIT_BRIDGE)
    if listing.get("truncated"):
        print("warning: the listing was truncated, so this may not be the newest poll",
              file=sys.stderr)
    newest = polls[-1]
    print(f"session {newest['session_id']}, taken {newest['started_at']}", file=sys.stderr)

    body, _ = call("GET", f"/api/v1/history/{newest['session_id']}/raw")
    if args.summary:
        our_slots = get_json("/api/v1/slots").get("slots", [])
        print_json(summarize(body.decode("utf-8", errors="replace"), our_slots))
    else:
        write_text(body, args.out)


def cmd_request(args: argparse.Namespace) -> None:
    print_json(get_json(f"/api/v1/purchase-requests/{urllib.parse.quote(args.request_id)}"))


def cmd_poll(args: argparse.Namespace) -> None:
    if not args.live:
        raise BridgeError(
            "Refusing to read the machine without --live. A live poll is 8.5 seconds "
            "of serial traffic on a machine that is selling drinks. `latest` gives you "
            "the newest stored poll (at most an hour old) without touching it.",
            EXIT_REFUSED,
        )
    body, headers = call("POST", "/api/v1/poll", timeout=POLL_TIMEOUT_SECONDS)
    cached = {k.lower(): v for k, v in headers.items()}.get("x-poll-cached")
    if cached == "true":
        print("note: someone polled in the last 30 seconds; this is their read", file=sys.stderr)
    write_text(body, args.out)


def write_text(body: bytes, out: str | None) -> None:
    """Print a raw document, or save it byte for byte so its checksum survives."""
    if out:
        Path(out).write_bytes(body)
        print(f"wrote {len(body)} bytes to {out}", file=sys.stderr)
    else:
        sys.stdout.write(body.decode("utf-8", errors="replace"))


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Talk to the DEX Bridge (the vending machine's Raspberry Pi).",
        epilog="See docs/dex-bridge.md for what each route returns.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("health", help="is the bridge up? (no key needed)").set_defaults(run=cmd_health)
    commands.add_parser("slots", help="the selections our team runs").set_defaults(run=cmd_slots)
    commands.add_parser("products", help="the catalog we can order from").set_defaults(run=cmd_products)

    history = commands.add_parser("history", help="list stored polls (UTC days, inclusive)")
    history.add_argument("--start", help="YYYY-MM-DD or an ISO timestamp; default today (UTC)")
    history.add_argument("--end", help="YYYY-MM-DD or an ISO timestamp; default same as --start")
    history.add_argument("--limit", type=int, default=500)
    history.set_defaults(run=cmd_history)

    raw = commands.add_parser("raw", help="one stored poll as raw EVA-DTS text")
    raw.add_argument("session_id")
    raw.add_argument("--out", help="save to this file instead of printing")
    raw.set_defaults(run=cmd_raw)

    latest = commands.add_parser("latest", help="the newest stored poll")
    latest.add_argument("--summary", action="store_true",
                        help="print price and lifetime sales for our slots instead of raw text")
    latest.add_argument("--out", help="save the raw text to this file")
    latest.set_defaults(run=cmd_latest)

    request = commands.add_parser("request", help="status of one of our purchase requests")
    request.add_argument("request_id")
    request.set_defaults(run=cmd_request)

    poll = commands.add_parser("poll", help="read the machine live (needs --live)")
    poll.add_argument("--live", action="store_true", help="yes, really read the machine now")
    poll.add_argument("--out", help="save to this file instead of printing")
    poll.set_defaults(run=cmd_poll)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.run(args)
    except BridgeError as error:
        print(f"error: {error}", file=sys.stderr)
        return error.exit_code
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
