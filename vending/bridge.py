"""Client for the DEX Bridge, for code that runs unattended.

`scripts/bridge.py` is the command-line tool for people; this is the library the
sync jobs and the write tools use. Only `poll`, which `vending.visit_polls` calls
around a visit, reads the machine live. The sync
jobs read stored history and our purchase requests through it, with retries;
`submit_desired_state` and `request_catalog_product` send their one POST through
`post`, and `vending.tracking` sends our books through `check_books`, each never
retried, and `/health` tells a bad poll from an outage.

Read failures are retried, then end in one of two ways. `BridgeUnreachable`
means every later call would fail too, so the caller should stop: the bridge is
down or rejected our key. Plain `BridgeError` means only this day or poll
failed: an error status, a response cut off or unreadable, or a connection that
keeps failing on one poll while `/health` still answers. A 5xx that outlasts
the retries is checked against `/health` the same way, because Tailscale's
proxy can answer `502` to every request until the node has joined the tailnet.

**No poll bytes in a message.** What the bridge sends back can hold the machine's whole document, and the
worker's logs and `/health` are read by people (`docs/integrity.md`, "Logs carry no poll bytes"). So no
error here quotes a body, a snippet, a bad status line or a header. A message holds the status, sizes and
a reason this module wrote ("short read: 1234 of 2000 bytes"). The exceptions are `post`, whose answer to
our own purchase or catalog request is the reason the write tool shows the agent, and `check_books`, whose
answer is about our own books.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http import HTTPStatus
from typing import Any, NamedTuple

from vending import settings

TIMEOUT_SECONDS = 20
# What a session id looks like before a log line or a URL names it: the bridge's ids are 32 hex digits
# (docs/snapshot-archive.md). A record (`PA1*11*987*`), a bare counter or a line break is not one.
_SESSION_ID = re.compile(r"[0-9a-fA-F]{32}")
# A live read takes about 8.5 s, and may wait its turn for the serial line first.
POLL_TIMEOUT_SECONDS = 30
# The header a live read names its session in (docs/snapshot-archive.md).
SESSION_HEADER = "X-Poll-Session-Id"
# `true` means another caller's recent document, `X-Poll-Age-Seconds` how old it was (docs/snapshot-archive.md).
CACHED_HEADER, AGE_HEADER = "X-Poll-Cached", "X-Poll-Age-Seconds"
RETRIES = 4
# The tracking check (`check_books`), and how much of an error answer it keeps: enough for a list of items.
TRACKING_PATH = "/api/v1/tracking/check"
TRACKING_ERROR_BYTES = 8192


class LiveRead(NamedTuple):
    """What `Bridge.poll` got: the document, the session id the bridge named (None if it named none), and
    whether it was another caller's cached document (None if the header was missing or not true/false) and
    how old it was (None if not given or not a number)."""
    document: bytes
    session_id: str | None
    cached: bool | None = None
    age_seconds: float | None = None


class ListedPoll(NamedTuple):
    """One row of a day's listing that can be fetched and stored: its session id, and its start as a UTC time."""
    session_id: str
    started_at: datetime


def valid_session_id(value: object) -> bool:
    """Whether `value` is a session id: 32 hex digits, so safe to log and to put in a URL."""
    return isinstance(value, str) and _SESSION_ID.fullmatch(value) is not None


def parse_start_time(value: object) -> datetime | None:
    """A listed poll's start as a UTC `datetime`, or None unless it is a real time with an offset.
    "2026-02-30T12:00:00+00:00" is not one, and neither is a time with no offset."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        moment = datetime.fromisoformat(value)
        return None if moment.utcoffset() is None else moment.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def listed_poll(row: object) -> ListedPoll | None:
    """A row of a day's listing as a `ListedPoll`, or None when it has no usable session id or start.
    The row is the bridge's word: nothing from a row that fails is logged, fetched or stored."""
    if not isinstance(row, dict) or not valid_session_id(row.get("session_id")):
        return None
    started = parse_start_time(row.get("started_at"))
    return None if started is None else ListedPoll(row["session_id"], started)


def status_phrase(status: int) -> str:
    """The standard name of an HTTP status ("Bad Gateway"), written here and not taken from the reply."""
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return ""


def _why_it_broke(error: http.client.HTTPException) -> str:
    """A reason of our own for a reply that was cut off or was not HTTP. Never the bytes that came."""
    if isinstance(error, http.client.IncompleteRead):
        got = len(error.partial)
        return f"short read: {got} of {got + error.expected} bytes" if isinstance(error.expected, int) \
            else f"short read: {got} bytes, then the connection ended"
    return f"the reply wasn't a whole HTTP answer ({type(error).__name__})"


class BridgeError(RuntimeError):
    """The bridge could not be reached, rejected us, or answered with an error."""


class BridgeAnswered(BridgeError):
    """An HTTP error status from a single, unretried call (`products`, `post`)."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"{status}: {detail}")
        self.status, self.detail = status, detail


class BridgeNoAnswer(BridgeError):
    """Sent, but no usable answer came back: a timeout, a dropped connection, or not JSON."""


class BridgeNotJson(BridgeNoAnswer):
    """A success status whose body isn't JSON. `status` is the status (None if the reply gave none);
    `body` is the start of the body, kept only for `post` and `check_books` (else empty). Neither is in
    the message, which says only the size."""

    def __init__(self, status: int | None, size: int, body: bytes = b""):
        super().__init__(f"the answer wasn't JSON ({size} bytes)")
        self.status, self.body = status, body


class BridgeUnreachable(BridgeError):
    """No network or a rejected key: every later call would fail the same way."""


class ConnectionFailed(BridgeUnreachable):
    """Every attempt at one request failed to connect or lost the connection.

    The bridge is probably down, but `Bridge.raw` checks, because one poll can
    drop every time on a bridge that is otherwise fine.
    """


class BridgeRateLimited(BridgeError):
    """Still `429` after every retry. Later calls in the same run would only make it worse."""


class Bridge:
    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self.base_url = (base_url or settings.require(
            "DEX_BASE_URL", "Copy .env.example to .env and fill it in.")).rstrip("/")
        self.api_key = api_key or settings.require(
            "DEX_API_KEY", "It is in the Postman environment course staff emailed.")

    # The two calls below are for write tools, which run while holding the halt
    # lock, so they never retry: one try, then the caller decides.

    def products(self) -> list[dict]:
        """The catalog our team can order from (`GET /api/v1/products`).

        Always a list. A reply of another shape comes back as an empty list, so
        `vending.planning` and the catalog sync can refuse it instead of crashing on it.
        """
        body = json.loads(self._once("GET", "/api/v1/products"))
        products = body.get("products") if isinstance(body, dict) else body
        return products if isinstance(products, list) else []

    def post(self, path: str, body: dict) -> dict:
        """One POST, never retried: a retry after a lost answer could file it twice.

        Raises `BridgeUnreachable` if the request certainly never arrived,
        `BridgeAnswered` for an HTTP error status, and `BridgeNoAnswer` if it
        was sent but no usable answer came back (it may or may not have run).
        """
        return json.loads(self._once("POST", path, body, keep_body=True))

    def check_books(self, body: dict) -> dict:
        """Our books as of one poll (`POST /api/v1/tracking/check`), once, never retried: the bridge
        allows one check a day, and a retry after a lost answer could be refused as the day's second.

        `vending.tracking` builds the body and stores the answer. Raises as `post` does; an error
        answer is the bridge's JSON about our own books (`{error, error_type, detail, items}`), kept
        whole enough to parse in `BridgeAnswered.detail`. A success status whose body isn't JSON raises
        `BridgeNotJson`, with the status and the start of the body.
        """
        return json.loads(self._once("POST", TRACKING_PATH, body, keep_body=True,
                                     kept_bytes=TRACKING_ERROR_BYTES))

    def poll(self) -> LiveRead:
        """Read the machine now (`POST /api/v1/poll`, about 8.5 s): the document, and the session id
        the bridge named in its `X-Poll-Session-Id` header (None if it sent none).

        One try, never retried: the read holds the bridge's serial line, so another caller
        gets a `409` while it runs, and a retry would only keep it busy. The bridge stores
        the read in its history, which is where `vending.sync_history` copies it from.
        Raises `BridgeAnswered` for an error status (`409` when the line is busy),
        `BridgeNoAnswer` for a timeout, and `BridgeUnreachable` when the bridge is down.
        Only `vending.visit_polls` calls it.
        """
        headers: dict[str, str] = {}
        document = self._once("POST", "/api/v1/poll", expect_json=False, timeout=POLL_TIMEOUT_SECONDS,
                              headers_out=headers)
        cached = {"true": True, "false": False}.get(headers.get(CACHED_HEADER.lower(), "").strip().lower())
        try:
            age = float(headers[AGE_HEADER.lower()])
        except (KeyError, ValueError):
            age = None
        if age is not None and age != age:      # nan
            age = None
        session_id = headers.get(SESSION_HEADER.lower())
        # A header that isn't an id (it is read by people in the log) counts as no id.
        return LiveRead(document, session_id if valid_session_id(session_id) else None, cached, age)

    def _once(self, method: str, path: str, body: dict | None = None, expect_json: bool = True,
              timeout: float | None = None, headers_out: dict[str, str] | None = None,
              keep_body: bool = False, kept_bytes: int = 300) -> bytes:
        """One try. `keep_body` puts the first `kept_bytes` of an error answer in `BridgeAnswered.detail`:
        only for `post` and `check_books`, whose answers concern our own request. Any other answer can hold
        a poll, so only its status is kept."""
        headers = {"X-API-Key": self.api_key}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if not expect_json:
            headers["Accept"] = "text/plain"
        request = urllib.request.Request(self.base_url + path, method=method, headers=headers,
                                         data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(request, timeout=timeout or TIMEOUT_SECONDS) as response:
                status = getattr(response, "status", None)
                data = response.read()
                if headers_out is not None:    # names lower-cased: HTTP header names have no case
                    headers_out.update({k.lower(): v for k, v in response.headers.items()})
        except urllib.error.HTTPError as error:
            # The status is the answer; a body cut off or slow to arrive mustn't turn it into a crash.
            detail = (_start_of_body(error, kept_bytes).decode(errors="replace") if keep_body
                      else status_phrase(error.code))
            raise BridgeAnswered(error.code, detail) from None
        except urllib.error.URLError as error:
            # urllib raises URLError only while connecting or sending. Only a failure to connect proves
            # the bridge got nothing; a reset or timeout once connected may come after it read the request.
            if not _before_connected(error.reason):
                raise BridgeNoAnswer(f"{type(error.reason).__name__} while sending: it may have arrived") from None
            raise BridgeUnreachable(f"Could not reach {self.base_url} ({error.reason}). Is Tailscale connected?") from None
        except http.client.HTTPException as error:
            # An answer cut off part way (`IncompleteRead`) or not HTTP at all (`BadStatusLine`, which names
            # the line it got). `RemoteDisconnected` is an `OSError` too and is handled below.
            if not isinstance(error, OSError):
                raise BridgeNoAnswer(_why_it_broke(error)) from None
            raise BridgeNoAnswer(f"{type(error).__name__}: the connection ended with no answer") from None
        except OSError as error:
            # A timeout waiting for the answer: it was sent and may have run.
            raise BridgeNoAnswer(f"{type(error).__name__}: {error}") from None
        if expect_json:
            try:
                json.loads(data)
            except ValueError:      # its message can name a byte of the answer
                raise BridgeNotJson(status, len(data), data[:kept_bytes] if keep_body else b"") from None
        return data

    def _get(self, path: str, params: dict[str, str] | None = None) -> bytes:
        url = self.base_url + path + ("?" + urllib.parse.urlencode(params) if params else "")
        request = urllib.request.Request(url, headers={"X-API-Key": self.api_key})
        delay = 2.0
        for attempt in range(RETRIES + 1):
            try:
                with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                    return response.read()
            except urllib.error.HTTPError as error:
                # 429 (too many requests) and 5xx are worth a retry; anything
                # else is our mistake and retrying won't fix it.
                if (error.code == 429 or error.code >= 500) and attempt < RETRIES:
                    time.sleep(_retry_after(error) or delay)
                    delay *= 2
                    continue
                if error.code == 429:
                    raise BridgeRateLimited(f"429 from {path}: still rate limited after {RETRIES} retries") from None
                if error.code in (401, 403):
                    raise BridgeUnreachable(f"{error.code}: the bridge rejected our key. Check DEX_API_KEY.") from None
                # The status and our own name for it; the body can hold a poll.
                detail = f"{error.code} {status_phrase(error.code)} from {path}".replace("  ", " ")
                # A 5xx from everything, `/health` included, is an outage (or
                # the Tailscale proxy before the node joins), not this request.
                if error.code >= 500 and path != "/health" and not self._is_up():
                    raise BridgeUnreachable(f"{detail}, and /health doesn't answer. Is Tailscale connected?") from None
                raise BridgeError(detail) from None
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as error:
                if attempt < RETRIES:
                    time.sleep(delay)
                    delay *= 2
                    continue
                # A response cut off or unreadable (`IncompleteRead`, `BadStatusLine`)
                # is this request's fault alone. `RemoteDisconnected` is an
                # `HTTPException` as well as an `OSError`, and it is a lost
                # connection, so check `OSError`.
                if not isinstance(error, OSError):
                    raise BridgeError(f"broken response from {path}: {_why_it_broke(error)}") from None
                raise ConnectionFailed(
                    f"Could not reach {self.base_url} ({getattr(error, 'reason', error)}). "
                    "Is Tailscale connected?"
                ) from None
        raise AssertionError("unreachable")

    def _is_up(self) -> bool:
        """Whether `/health` answers. It needs no key and doesn't read the machine."""
        try:
            self._get("/health")
        except BridgeError:
            return False
        return True

    def slots(self) -> Any:
        """Our slot assignment (`GET /api/v1/slots`): `team`, `device_id` and `slots`, as the bridge sent it.
        `vending.slots.parse` checks it."""
        payload = self._get("/api/v1/slots")
        try:
            return json.loads(payload)
        except ValueError:  # Not `JSONDecodeError`: bytes that aren't text raise `UnicodeDecodeError`.
            raise BridgeError(f"/api/v1/slots was not JSON ({len(payload)} bytes)") from None

    def history(self, day: str) -> list[Any]:
        """Stored polls for one UTC day (YYYY-MM-DD), oldest first.

        The bridge returns at most 500 polls per request and marks the answer
        `truncated`, so the sync asks one day at a time and refuses a truncated day.

        Only the listing as a whole is checked here. A row that is not an object, or has no usable
        session id or start (`listed_poll`), comes back as it is, so one bad row can't turn the whole
        day into an error: the sync counts and skips it.
        """
        payload = self._get("/api/v1/history", {"start": day, "end": day})
        try:
            body = json.loads(payload)
        except ValueError:  # Not `JSONDecodeError`: bytes that aren't text raise `UnicodeDecodeError`.
            raise BridgeError(f"history for {day} was not JSON ({len(payload)} bytes)") from None
        if not _is_listing(body):
            raise BridgeError(f"history for {day} was not a listing of polls ({len(payload)} bytes)")
        if body.get("truncated"):
            raise BridgeError(f"history for {day}: listing truncated")   # not its `limit`: a figure of the bridge's
        return [dict(p, device_id=body.get("device_id")) if isinstance(p, dict) else p for p in body["polls"]]

    def raw(self, session_id: str) -> bytes:
        """One stored poll, byte-for-byte.

        If the connection keeps failing but `/health` answers, the fault is this
        poll, so this raises `BridgeError` and the sync can move on to the next one.
        """
        try:
            return self._get(f"/api/v1/history/{urllib.parse.quote(session_id)}/raw")
        except ConnectionFailed:
            if self._is_up():
                raise BridgeError("the connection failed on every attempt, though /health answers") from None
            raise

    def purchase_request(self, request_id: str) -> dict:
        """One of our purchase requests, as the bridge sees it now.

        `{id, status: "pending", created_at}` until the worker has dealt with
        every line, `{..., status: "rejected", decision_note}` if it was turned
        down, and the full listing with a receipt per line once fulfilled.
        A GET only: `submit_desired_state` files requests, through `post`.
        """
        return self._json(f"/api/v1/purchase-requests/{urllib.parse.quote(request_id, safe='')}")

    def product_request(self, request_id: str) -> dict:
        """One of our catalog requests, as the bridge sees it now (`ProductRequestView`).

        `{id, status, created_at, requested, decision_note, product}`: `status` is
        `pending`, `approved` (with `product: {sku, name}`, which may differ from
        what we asked for) or `rejected` (with `decision_note`). A GET only:
        `request_catalog_product` files requests, through `post`.
        """
        return self._json(f"/api/v1/product-requests/{urllib.parse.quote(request_id, safe='')}")

    def _json(self, path: str) -> Any:
        """A GET whose answer must be JSON. One that isn't (a proxy's page, say) is a BridgeError,
        so a sync counts it and goes on instead of crashing."""
        body = self._get(path)
        try:
            return json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise BridgeError(f"answer from {path} isn't JSON ({len(body)} bytes)") from None


def _is_listing(body: object) -> bool:
    """Whether `body` is a day's listing: an object with a list of `polls`. Its rows are checked one by one
    by the sync (`listed_poll`), never all together."""
    return isinstance(body, dict) and isinstance(body.get("polls"), list)


def _before_connected(reason: object) -> bool:
    """Whether a `URLError`'s reason proves no byte of the request left: urllib refused it before
    connecting, the name didn't resolve, the connection was refused, or connecting (TCP, or TLS for
    https) failed or timed out. The request is written only once `HTTPConnection.connect` returns, so a
    failure raised inside it is before; any other (a reset, a broken pipe, a timeout while writing) isn't."""
    if not isinstance(reason, OSError):
        return True
    if isinstance(reason, (socket.gaierror, ConnectionRefusedError)):
        return True
    trace = reason.__traceback__
    while trace is not None:
        code = trace.tb_frame.f_code
        if code.co_name == "connect" and code.co_filename == http.client.__file__:
            return True
        trace = trace.tb_next
    return False


def _start_of_body(error: urllib.error.HTTPError, size: int = 200) -> bytes:
    """The start of an error response, for the message. It can be cut off too."""
    try:
        return error.read(size)
    except (OSError, http.client.HTTPException):
        return b""


def _retry_after(error: urllib.error.HTTPError) -> float | None:
    """The wait a 429 asks for, if it says, capped so one answer can't stall the run."""
    value = error.headers.get("Retry-After") if error.headers else None
    try:
        return min(max(float(value), 0.0), 60.0) if value else None
    except ValueError:
        return None  # an HTTP date; the doubling delay is good enough
