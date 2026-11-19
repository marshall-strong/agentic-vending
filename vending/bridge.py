"""Client for the DEX Bridge, for code that runs unattended.

`scripts/bridge.py` is the command-line tool for people; this is the library the
sync jobs and the write tools use. Only `poll`, which `vending.visit_polls` calls
around a visit, reads the machine live. The sync
jobs read stored history and our purchase requests through it, with retries;
`submit_desired_state` and `request_catalog_product` send their one POST through
`post`, never retried, and `/health` tells a bad poll from an outage.

Read failures are retried, then end in one of two ways. `BridgeUnreachable`
means every later call would fail too, so the caller should stop: the bridge is
down or rejected our key. Plain `BridgeError` means only this day or poll
failed: an error status, a response cut off or unreadable, or a connection that
keeps failing on one poll while `/health` still answers. A 5xx that outlasts
the retries is checked against `/health` the same way, because Tailscale's
proxy can answer `502` to every request until the node has joined the tailnet.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, NamedTuple

from vending import settings

TIMEOUT_SECONDS = 20
# A live read takes about 8.5 s, and may wait its turn for the serial line first.
POLL_TIMEOUT_SECONDS = 30
# The header a live read names its session in (docs/snapshot-archive.md).
SESSION_HEADER = "X-Poll-Session-Id"
RETRIES = 4


class LiveRead(NamedTuple):
    """What `Bridge.poll` got: the document, and the session id the bridge named (None if it named none)."""
    document: bytes
    session_id: str | None


class BridgeError(RuntimeError):
    """The bridge could not be reached, rejected us, or answered with an error."""


class BridgeAnswered(BridgeError):
    """An HTTP error status from a single, unretried call (`products`, `post`)."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"{status}: {detail}")
        self.status, self.detail = status, detail


class BridgeNoAnswer(BridgeError):
    """Sent, but no usable answer came back: a timeout, a dropped connection, or not JSON."""


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
        return json.loads(self._once("POST", path, body))

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
        return LiveRead(document, headers.get(SESSION_HEADER.lower()) or None)

    def _once(self, method: str, path: str, body: dict | None = None, expect_json: bool = True,
              timeout: float | None = None, headers_out: dict[str, str] | None = None) -> bytes:
        headers = {"X-API-Key": self.api_key}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if not expect_json:
            headers["Accept"] = "text/plain"
        request = urllib.request.Request(self.base_url + path, method=method, headers=headers,
                                         data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(request, timeout=timeout or TIMEOUT_SECONDS) as response:
                data = response.read()
                if headers_out is not None:    # names lower-cased: HTTP header names have no case
                    headers_out.update({k.lower(): v for k, v in response.headers.items()})
        except urllib.error.HTTPError as error:
            # The status is the answer; a body cut off or slow to arrive mustn't turn it into a crash.
            raise BridgeAnswered(error.code, _start_of_body(error, 300).decode(errors="replace")) from None
        except urllib.error.URLError as error:
            # urllib raises URLError only while connecting or sending.
            raise BridgeUnreachable(f"Could not reach {self.base_url} ({error.reason}). Is Tailscale connected?") from None
        except (OSError, http.client.HTTPException) as error:
            # A timeout waiting for the answer, or an answer cut off part way
            # (`IncompleteRead`): either way it was sent and may have run.
            raise BridgeNoAnswer(f"{type(error).__name__}: {error}") from None
        if expect_json:
            try:
                json.loads(data)
            except ValueError as error:
                raise BridgeNoAnswer(f"the answer wasn't JSON ({error})") from None
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
                detail = f"{error.code} from {path}: {_start_of_body(error)!r}"
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
                    raise BridgeError(f"broken response from {path}: {error!r}") from None
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
            raise BridgeError(f"/api/v1/slots was not JSON: {payload[:200]!r}") from None

    def history(self, day: str) -> list[dict]:
        """Stored polls for one UTC day (YYYY-MM-DD), oldest first.

        The bridge returns at most 500 polls per request and marks the answer
        `truncated`, so the sync asks one day at a time and refuses a truncated day.
        """
        payload = self._get("/api/v1/history", {"start": day, "end": day})
        try:
            body = json.loads(payload)
        except ValueError:  # Not `JSONDecodeError`: bytes that aren't text raise `UnicodeDecodeError`.
            raise BridgeError(f"history for {day} was not JSON: {payload[:200]!r}") from None
        if not _is_listing(body):
            raise BridgeError(f"history for {day} was not a listing of polls: {payload[:200]!r}")
        if body.get("truncated"):
            raise BridgeError(f"history for {day} was truncated at {body.get('limit')} polls")
        return [dict(p, device_id=body.get("device_id")) for p in body["polls"]]

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
            raise BridgeError(f"answer from {path} isn't JSON: {body[:200]!r}") from None


def _is_listing(body: object) -> bool:
    """Whether `body` is a day's listing, with what the sync needs from every poll."""
    return isinstance(body, dict) and isinstance(body.get("polls"), list) and all(
        isinstance(p, dict) and isinstance(p.get("session_id"), str) and isinstance(p.get("started_at"), str)
        for p in body["polls"]
    )


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
