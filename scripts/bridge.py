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

For a poll taken from the fall cut on (`FALL_CUTOFF`, 18 Nov 2026 00:00 Eastern
when unset), `raw`, `latest` and `poll` print a view rebuilt from our selections
and the door counter only (see "From the fall cut" below).

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

**From the fall cut (`vending.start.fall_cut`).** All 40 selections' data, and
the machine-wide totals, may be used only from polls taken before the fall cut:
the setting `FALL_CUTOFF`, 18 Nov 2026 00:00 Eastern when unset, and never later
than the 3 Dec 2026 start (`docs/integrity.md`, "Fall data"). From the cut on,
nothing may show per-selection data outside our assignment or a machine-wide
total. From the start the raw poll carries all 40 selections, the other team's
20 too. Our selections are the stored assignment (`vending.slots.our_slots`,
which needs `DATABASE_URL`), never a fresh `/api/v1/slots` answer. For a poll
from the fall cut on:

- `raw`, `latest` and `poll --live` print a view rebuilt from checked fields,
  not lines copied from the poll: `ID4` with its decimal places, `PA1` (the
  selection and its price) and `PA2` (its counters) for each of our selections,
  and the door counter `EA2*DO` with its counts. Nothing else: no envelope, no
  `ID1`, no `G85` checksum, no other team's selection and no machine-wide total
  (`VA1`, the cash and card records, the rest). A note on stderr says how many
  lines were left out. When the assignment can't be read, no selection is
  printed, and stderr says so. Nothing prints the whole poll.
- `--out FILE` saves the raw bytes, because storing the poll is fine. It
  writes only a regular file: it refuses `-`, a device (`/dev/stdout`, `CON`,
  `NUL`), a pipe, a directory, or anything else that isn't a regular file.
- `latest --summary` reads the stored assignment first, and skips every
  selection that isn't ours before reading any of its fields.

When a poll was taken comes from the bridge, never from this computer's clock:
`latest` reads the listing's `started_at`, `raw` the time our `polls` table
holds for the session (copied from the bridge's listing by the history sync),
and `poll --live` the bridge's `Date` header, less `X-Poll-Age-Seconds` when
the read was another caller's. A poll prints whole only when that time is
before the cut; an unknown time counts as one from the cut. The cut is settled
before the bridge is asked anything, so a `FALL_CUTOFF` that isn't a time with
its offset stops the command with no request sent.

Nothing the bridge sends is printed unchecked. `history` prints each entry's
`session_id`, `started_at` and `bytes`, checked, and `truncated`; nothing else.
`latest` prints the session and time it checked. An error from any route shows
the status and its standard phrase, never the bridge's reply: a reply could
carry a poll's records.

Standard library only, except reading the stored assignment, so it runs
anywhere Python 3.11 does with nothing to install.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http import HTTPStatus
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


# Routes whose replies are polls or list them. Their error replies are never printed (see `call`).
POLL_ROUTES = ("/api/v1/history", "/api/v1/poll")


def status_phrase(code: int) -> str:
    """A status's standard phrase, from the standard library, not from the bridge."""
    try:
        return HTTPStatus(code).phrase
    except ValueError:
        return "error"


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


# The one `.env` reader the project has, so this script and the sync jobs agree
# on how a value is quoted or commented. Standard library too.
sys.path.insert(0, str(repo_root()))
from vending import settings as env  # noqa: E402
from vending.start import fall_cut  # noqa: E402


def settings() -> tuple[str, str]:
    """Return (base_url, api_key). Real environment variables win over .env."""
    base_url = env.get("DEX_BASE_URL") or ""
    api_key = env.get("DEX_API_KEY") or ""
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
        # Any route's error reply could carry a poll's records, so none is echoed: the status
        # and its standard phrase only, and the hints below, written here.
        detail = status_phrase(error.code)
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
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise BridgeError(f"the reply from {path} isn't JSON; it isn't printed", EXIT_BRIDGE) from None


def print_json(value: object) -> None:
    print(json.dumps(value, indent=2))


# --------------------------------------------------------------------------
# EVA-DTS, just enough for `latest --summary`
# --------------------------------------------------------------------------

def number(field: str) -> int | None:
    """A field of counts or money as an int: 1 to 12 digits and nothing else, or None."""
    return int(field) if re.fullmatch(r"[0-9]{1,12}", field) else None


def summarize(dex_text: str, ours: tuple[str, ...] | list[str]) -> list[dict[str, object]]:
    """Price and lifetime units sold for each of `ours`, our selections from the stored assignment.

    Records are `*`-separated, one per line. `PA1*<selection>*<price>` opens a
    selection, and the `PA2*<units>*<value>*...` line after it holds that
    selection's lifetime counters. Currency is an integer; `ID4-01` (the first
    field after the tag, `ID4*2*...`) says how many decimal places it carries.

    A selection that isn't one of `ours` is skipped at its `PA1`: only its name is compared, and
    no other field of it, or of its `PA2`, is read. A field that isn't a plain number is None.

    This does not validate the `G85` checksum, so treat it as a quick look, not
    as data to build on. The real parser belongs in the history sync.
    """
    wanted = set(ours)
    decimals = 2
    rows: dict[str, dict[str, int | None]] = {}
    current: str | None = None
    for line in dex_text.splitlines():
        fields = line.strip().split("*")
        tag = fields[0]
        if not tag.startswith("PA"):
            current = None
        if tag == "ID4" and len(fields) > 1:
            places = number(fields[1])
            if places is not None and places <= 6:
                decimals = places
        elif tag == "PA1":
            current = fields[1] if len(fields) > 1 and fields[1] in wanted else None
            if current is not None:
                rows[current] = {"price_cents": number(fields[2]) if len(fields) > 2 else None}
        elif tag == "PA2" and current is not None:
            rows[current]["lifetime_units"] = number(fields[1]) if len(fields) > 1 else None
            rows[current]["lifetime_value_cents"] = number(fields[2]) if len(fields) > 2 else None
            current = None

    scale = 10 ** decimals
    result = []
    for slot in ours:
        row = rows.get(slot, {})
        price, value = row.get("price_cents"), row.get("lifetime_value_cents")
        result.append({
            "slot": slot,
            "price": f"${price / scale:.2f}" if price is not None else None,
            "lifetime_units": row.get("lifetime_units"),
            "lifetime_sales": f"${value / scale:,.2f}" if value is not None else None,
        })
    return result


# --------------------------------------------------------------------------
# From the fall cut: only our selections (see the module docstring)
# --------------------------------------------------------------------------

def counts(fields: list[str]) -> list[str]:
    """The leading fields that are plain numbers (0 to 12 digits), up to the first that isn't."""
    kept = []
    for field in fields:
        if not re.fullmatch(r"[0-9]{0,12}", field):
            break
        kept.append(field)
    return kept


def rebuild(dex_text: str, ours: tuple[str, ...] | list[str]) -> tuple[str, int, int]:
    """What a poll from the fall cut on may show: (text, how many of `ours` it prints, lines left out).

    Nothing is copied from the poll. Each line is written anew from a known tag and fields
    checked to be plain numbers, so a record nobody thought of, or a field that isn't a number,
    can't come through:

    - `ID4*<decimal places>`, so a price can be read;
    - for each `PA1` that names one of `ours`: `PA1*<selection>*<price>`, then `PA2*<counts>`
      from a `PA2` that follows it in the same `PA` block;
    - `EA2*DO*<counts>`, the door counter.

    Every other line is left out, the envelope and `G85` too (its checksum would be wrong). A
    selection that isn't ours is dropped at its `PA1`, before any other field of it is read. A
    field that isn't a number ends its record there. Lines end as the poll's do.
    """
    wanted = set(ours)
    newline = "\r\n" if "\r\n" in dex_text else "\n"
    out: list[str] = []
    printed = left_out = 0
    current: str | None = None   # one of ours, whose PA2 may come next
    for line in dex_text.splitlines():
        if not line.strip():
            continue
        fields = line.strip().split("*")
        tag = fields[0]
        record = None
        if not tag.startswith("PA"):
            current = None
        if tag == "ID4":
            places = counts(fields[1:2])
            if places and places[0]:
                record = f"ID4*{places[0]}"
        elif tag == "PA1":
            current = fields[1] if len(fields) > 1 and fields[1] in wanted else None
            if current is not None:
                record = "*".join(["PA1", current, *counts(fields[2:3])])
                printed += 1
        elif tag == "PA2" and current is not None:
            found = counts(fields[1:])
            if found:
                record = "*".join(["PA2", *found])
            current = None
        elif tag == "EA2" and len(fields) > 1 and fields[1] == "DO":
            record = "*".join(["EA2", "DO", *counts(fields[2:])])
        if record is None:
            left_out += 1
        else:
            out.append(record)
    return "".join(record + newline for record in out), printed, left_out


def our_selections() -> tuple[tuple[str, ...], str | None]:
    """(our selections from the stored assignment, None), or ((), why they can't be read).

    Imported here, not at the top, because only this needs psycopg and `DATABASE_URL`.
    """
    try:
        from vending import db
        from vending.slots import our_slots

        with db.connect() as conn:
            return tuple(our_slots(conn)), None
    except Exception as error:  # noqa: BLE001 -- whatever went wrong, no selection is printed
        return (), f"{type(error).__name__}: {str(error)[:200]}"


def settled_cut() -> datetime:
    """The fall cut (`vending.start.fall_cut`), settled before the bridge is asked for anything. A
    malformed FALL_CUTOFF stops the command here, with no request sent."""
    try:
        return fall_cut()
    except ValueError as error:
        raise BridgeError(str(error), EXIT_CONFIG) from None


def show_poll(body: bytes, out: str | None, taken: datetime | None, cut: datetime,
              unknown: str | None = None) -> None:
    """Print a poll, or save it byte for byte. Only one `taken`, by the bridge's account, before
    `cut` is as it always was. One from the cut on, or taken at an unknown time (`unknown` says
    why), prints only as `rebuild` writes it, and saves only to a regular file (`save`)."""
    whole = taken is not None and taken < cut
    if out:
        if whole:
            write_text(body, out)
        else:
            save(body, out)
        return
    if whole:
        write_text(body, None)
        return
    if taken is None:
        print(f"note: when the poll was taken is unknown ({unknown}), so it counts as one from the "
              "fall cut", file=sys.stderr)
    ours, why = our_selections()
    if why:
        print(f"note: our slot assignment can't be read ({why}), so no selection is printed",
              file=sys.stderr)
    text, printed, left_out = rebuild(body.decode("utf-8", errors="replace"), ours)
    print(f"note: a poll from the fall cut on ({cut.isoformat(timespec='minutes')}, FALL_CUTOFF): "
          f"{printed} of our selections, rebuilt from checked fields; {left_out} lines left out (the "
          "selections that aren't ours, the machine-wide totals, the envelope and G85). --out FILE "
          "saves the whole poll", file=sys.stderr)
    sys.stdout.write(text)


def stored_poll_time(session_id: str) -> tuple[datetime | None, str | None]:
    """(when the poll was taken, None) from our `polls` table, or (None, why it isn't known).

    The table's `started_at` is the bridge's listing, copied by the history sync. Imported here,
    like `our_selections`, because only this and that need psycopg and `DATABASE_URL`.
    """
    try:
        from vending import db

        with db.connect() as conn:
            row = conn.execute("select started_at from polls where session_id = %s", (session_id,)).fetchone()
    except Exception as error:  # noqa: BLE001 -- whatever went wrong, the time is unknown
        return None, f"{type(error).__name__}: {str(error)[:200]}"
    if row is None:
        return None, "not in the polls table; `python -m vending.sync_history` copies the bridge's listing"
    taken = row[0]
    if not callable(getattr(taken, "utcoffset", None)) or taken.utcoffset() is None:
        return None, "the polls table's time has no time zone"
    return taken, None


def live_poll_time(headers: dict[str, str]) -> tuple[datetime | None, str | None]:
    """(the latest the live read can have been taken, None) by the bridge's clock, or (None, why not).

    The bridge's `Date` header is written after the read, so the read can't be later. When the
    read was another caller's (`X-Poll-Cached: true`), it is `X-Poll-Age-Seconds` older still; an
    age that isn't a number of seconds is ignored, which leaves the later time.
    """
    found = {k.lower(): v for k, v in headers.items()}
    try:
        date = parsedate_to_datetime(found.get("date") or "")
    except (TypeError, ValueError, IndexError):
        date = None
    if date is None or date.utcoffset() is None:
        return None, "the bridge's reply has no readable Date header"
    if (found.get("x-poll-cached") or "").strip().lower() == "true":
        try:
            age = float(found.get("x-poll-age-seconds") or "")
        except ValueError:
            age = None
        if age is not None and 0 <= age < 3600:   # also refuses nan and inf
            date -= timedelta(seconds=age)
    return date, None


# A session ID as the bridge gives them: letters, digits, `-` and `_`. Anything else is never printed.
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def checked_session(value: object) -> str | None:
    return value if isinstance(value, str) and SESSION_ID.fullmatch(value) else None


def checked_time(value: object) -> datetime | None:
    """A listing's `started_at` as an aware time, or None. One with no offset is unknown, not a guess."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.utcoffset() is not None else None


def listed_polls(listing: object) -> list[dict]:
    """The listing's `polls`, each a dict, or a BridgeError: a reply of any other shape isn't printed."""
    polls = listing.get("polls") if isinstance(listing, dict) else None
    if not isinstance(polls, list) or not all(isinstance(entry, dict) for entry in polls):
        raise BridgeError("the bridge's listing isn't a list of polls, so nothing of it is printed", EXIT_BRIDGE)
    return polls


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
    listing = get_json("/api/v1/history", params={"start": start, "end": end, "limit": str(args.limit)})
    rows, left_out = [], 0
    for entry in listed_polls(listing):
        session = checked_session(entry.get("session_id"))
        if session is None:
            left_out += 1
            continue
        taken, size = checked_time(entry.get("started_at")), entry.get("bytes")
        rows.append({"session_id": session, "started_at": taken.isoformat() if taken else None,
                     "bytes": size if type(size) is int and size >= 0 else None})
    if left_out:
        print(f"note: {left_out} {'entry' if left_out == 1 else 'entries'} left out: a session ID that "
              "isn't one", file=sys.stderr)
    print_json({"polls": rows, "truncated": listing.get("truncated") is True})


def cmd_raw(args: argparse.Namespace) -> None:
    cut = settled_cut()
    body, _ = call("GET", f"/api/v1/history/{urllib.parse.quote(args.session_id)}/raw")
    # The route doesn't say when the poll was taken; our polls table does, from the bridge's listing.
    taken, unknown = stored_poll_time(args.session_id)
    show_poll(body, args.out, taken, cut, unknown)


def cmd_latest(args: argparse.Namespace) -> None:
    cut = settled_cut()   # first, even for --summary: a malformed setting sends no request
    ours: tuple[str, ...] = ()
    if args.summary:
        # Ours from the stored assignment, read before any poll; never from a fresh /api/v1/slots.
        ours, why = our_selections()
        if why:
            print(f"note: our slot assignment can't be read ({why}), so no selection is printed",
                  file=sys.stderr)
            print_json([])
            return
    # History is oldest first. Yesterday and today together always include the
    # newest poll, since David's poller runs every hour.
    today = datetime.now(timezone.utc)
    yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    listing = get_json("/api/v1/history",
                       params={"start": yesterday, "end": today.strftime("%Y-%m-%d"), "limit": "500"})
    polls = listed_polls(listing)
    if not polls:
        raise BridgeError("No stored polls in the last two UTC days.", EXIT_BRIDGE)
    if listing.get("truncated") is True:
        print("warning: the listing was truncated, so this may not be the newest poll",
              file=sys.stderr)
    session = checked_session(polls[-1].get("session_id"))
    if session is None:
        raise BridgeError("the newest poll in the listing has no session ID this script accepts, so it "
                          "isn't fetched", EXIT_BRIDGE)
    taken = checked_time(polls[-1].get("started_at"))
    print(f"session {session}, taken {taken.isoformat() if taken else 'at an unknown time'}", file=sys.stderr)

    body, _ = call("GET", f"/api/v1/history/{session}/raw")
    if args.summary:
        print_json(summarize(body.decode("utf-8", errors="replace"), ours))
    else:
        show_poll(body, args.out, taken, cut, "the listing's started_at isn't a time with its offset")


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
    cut = settled_cut()
    body, headers = call("POST", "/api/v1/poll", timeout=POLL_TIMEOUT_SECONDS)
    cached = {k.lower(): v for k, v in headers.items()}.get("x-poll-cached")
    if cached == "true":
        print("note: someone polled in the last 30 seconds; this is their read", file=sys.stderr)
    taken, unknown = live_poll_time(headers)
    show_poll(body, args.out, taken, cut, unknown)


def write_text(body: bytes, out: str | None) -> None:
    """Print a raw document, or save it byte for byte so its checksum survives. Before the fall cut only."""
    if out:
        Path(out).write_bytes(body)
        print(f"wrote {len(body)} bytes to {out}", file=sys.stderr)
    else:
        sys.stdout.write(body.decode("utf-8", errors="replace"))


# Windows device names. `CON`, `nul.txt` or `C:\x\COM1` open a device, not a file, in any folder.
DEVICE_NAMES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$",
                *(f"{port}{n}" for port in ("COM", "LPT") for n in (*"0123456789", "\u00b9", "\u00b2", "\u00b3"))}


def device_name(out: str) -> bool:
    """`out` names a Windows device (`CON`, `nul.txt`, `\\\\.\\pipe\\x`). Refused on every system,
    so the rule is the same on each."""
    if out.replace("/", "\\").startswith(("\\\\.\\", "\\\\?\\")):
        return True
    name = re.split(r"[\\/]", out)[-1]
    return name.split(".", 1)[0].rstrip(" :").upper() in DEVICE_NAMES


def save(body: bytes, out: str) -> None:
    """Save a poll from the fall cut on byte for byte, to a regular file and nowhere else.

    `--out` is for archiving, so it can't be a way round the rebuilt view: `-`, a device
    (`/dev/stdout`, `/dev/tty`, `CON`), a pipe, a socket or a directory is refused, and nothing
    is written. The target is checked after following links, and again on the opened file, so
    one swapped in between is refused too.
    """
    def refuse(why: str) -> BridgeError:
        return BridgeError(f"--out {out!r} {why}. A poll from the fall cut on is saved only to a regular "
                           "file, never to the terminal, a device or a pipe.", EXIT_REFUSED)

    if out == "-" or device_name(out):
        raise refuse("names the terminal or a device")
    try:
        found = Path(out).resolve().stat()
    except FileNotFoundError:
        found = None
    except OSError as error:
        raise refuse(f"can't be checked ({error.strerror or type(error).__name__})") from None
    if found is not None and not stat.S_ISREG(found.st_mode):
        raise refuse("isn't a regular file")
    # No O_TRUNC: the file is emptied only once it is known to be a regular one. O_NONBLOCK, so a
    # pipe swapped in with nobody reading fails at once instead of waiting.
    flags = (os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
             | getattr(os, "O_NOCTTY", 0))
    try:
        fd = os.open(out, flags, 0o644)
    except OSError as error:
        raise refuse(f"can't be opened ({error.strerror or type(error).__name__})") from None
    with open(fd, "wb", closefd=True) as file:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise refuse("isn't a regular file")
        file.truncate(0)
        file.write(body)
    print(f"wrote {len(body)} bytes to {out}", file=sys.stderr)


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
