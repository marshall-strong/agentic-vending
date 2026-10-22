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


class Bridge:
    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self.base_url = (base_url or settings.require(
            "DEX_BASE_URL", "Copy .env.example to .env and fill it in.")).rstrip("/")
        self.api_key = api_key or settings.require(
            "DEX_API_KEY", "It is in the Postman environment course staff emailed.")

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
                    raise BridgeError(f"{error.code}: the bridge rejected our key. Check DEX_API_KEY.") from None
                raise BridgeError(f"{error.code} from {path}: {error.read()[:200]!r}") from None
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                if attempt < RETRIES:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise BridgeError(
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
