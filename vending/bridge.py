"""Read-only client for the DEX Bridge, for code that runs unattended.

`scripts/bridge.py` is the command-line tool for people; this is the library the
sync jobs use. It never polls the machine live and cannot submit a purchase
request: it only reads stored history and our purchase requests, and `/health`
to tell a bad poll from an outage.

Read failures are retried, then end in one of two ways. `BridgeUnreachable`
means every later call would fail too, so the caller should stop: the bridge is
down or rejected our key. Plain `BridgeError` means only this day or poll
failed: an error status, a response cut off or unreadable, or a connection that
keeps failing on one poll while `/health` still answers.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from vending import settings

TIMEOUT_SECONDS = 20
RETRIES = 4


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
        """The catalog our team can order from (`GET /api/v1/products`)."""
        return json.loads(self._once("GET", "/api/v1/products")).get("products", [])

    def post(self, path: str, body: dict) -> dict:
        """One POST, never retried: a retry after a lost answer could file it twice.

        Raises `BridgeUnreachable` if the request certainly never arrived,
        `BridgeAnswered` for an HTTP error status, and `BridgeNoAnswer` if it
        was sent but no usable answer came back (it may or may not have run).
        """
        return json.loads(self._once("POST", path, body))

    def _once(self, method: str, path: str, body: dict | None = None) -> bytes:
        headers = {"X-API-Key": self.api_key}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, method=method, headers=headers,
                                         data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                data = response.read()
        except urllib.error.HTTPError as error:
            raise BridgeAnswered(error.code, error.read()[:300].decode(errors="replace")) from None
        except urllib.error.URLError as error:
            # urllib raises URLError only while connecting or sending.
            raise BridgeUnreachable(f"Could not reach {self.base_url} ({error.reason}). Is Tailscale connected?") from None
        except OSError as error:  # includes a timeout waiting for the answer
            raise BridgeNoAnswer(f"{type(error).__name__}: {error}") from None
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
                raise BridgeError(f"{error.code} from {path}: {_start_of_body(error)!r}") from None
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
        A GET only: submitting is `submit_desired_state`'s job, never this client's.
        """
        return json.loads(self._get(f"/api/v1/purchase-requests/{urllib.parse.quote(request_id, safe='')}"))


def _is_listing(body: object) -> bool:
    """Whether `body` is a day's listing, with what the sync needs from every poll."""
    return isinstance(body, dict) and isinstance(body.get("polls"), list) and all(
        isinstance(p, dict) and isinstance(p.get("session_id"), str) and isinstance(p.get("started_at"), str)
        for p in body["polls"]
    )


def _start_of_body(error: urllib.error.HTTPError) -> bytes:
    """The start of an error response, for the message. It can be cut off too."""
    try:
        return error.read(200)
    except (OSError, http.client.HTTPException):
        return b""


def _retry_after(error: urllib.error.HTTPError) -> float | None:
    """The wait a 429 asks for, if it says, capped so one answer can't stall the run."""
    value = error.headers.get("Retry-After") if error.headers else None
    try:
        return min(max(float(value), 0.0), 60.0) if value else None
    except ValueError:
        return None  # an HTTP date; the doubling delay is good enough
