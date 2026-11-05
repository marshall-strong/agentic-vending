"""Read-only client for the DEX Bridge, for code that runs unattended.

`scripts/bridge.py` is the command-line tool for people; this is the library the
sync job uses. It never polls the machine live and cannot submit a purchase
request: it only reads stored history.
"""

from __future__ import annotations

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
                    time.sleep(delay)
                    delay *= 2
                    continue
                if error.code in (401, 403):
                    raise BridgeUnreachable(f"{error.code}: the bridge rejected our key. Check DEX_API_KEY.") from None
                raise BridgeError(f"{error.code} from {path}: {error.read()[:200]!r}") from None
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                if attempt < RETRIES:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise BridgeUnreachable(
                    f"Could not reach {self.base_url} ({getattr(error, 'reason', error)}). "
                    "Is Tailscale connected?"
                ) from None
        raise AssertionError("unreachable")

    def history(self, day: str) -> list[dict]:
        """Stored polls for one UTC day (YYYY-MM-DD), oldest first.

        The bridge returns at most 500 polls per request and marks the answer
        `truncated`, so the sync asks one day at a time and refuses a truncated day.
        """
        body = json.loads(self._get("/api/v1/history", {"start": day, "end": day}))
        if body.get("truncated"):
            raise BridgeError(f"history for {day} was truncated at {body.get('limit')} polls")
        return [dict(p, device_id=body.get("device_id")) for p in body.get("polls", [])]

    def raw(self, session_id: str) -> bytes:
        """One stored poll, byte-for-byte."""
        return self._get(f"/api/v1/history/{urllib.parse.quote(session_id)}/raw")
