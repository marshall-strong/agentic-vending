"""Tests for the bridge client's purchase-request read, with the network faked out."""

import io
import urllib.error

import pytest

from vending import bridge as bridge_module
from vending.bridge import Bridge, BridgeRateLimited


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_purchase_request_is_a_get_with_the_id_quoted(monkeypatch):
    seen = []

    def urlopen(request, timeout):
        seen.append(request)
        return FakeResponse(b'{"id": "pr/1", "status": "pending", "created_at": "2026-11-02T14:00:00Z"}')

    monkeypatch.setattr(bridge_module.urllib.request, "urlopen", urlopen)
    answer = Bridge("http://bridge.test", "key").purchase_request("pr/1")
    assert answer["status"] == "pending"
    assert seen[0].get_method() == "GET" and seen[0].data is None
    assert seen[0].full_url == "http://bridge.test/api/v1/purchase-requests/pr%2F1"


def test_429_backs_off_as_asked_then_gives_up(monkeypatch):
    sleeps = []

    def urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {"Retry-After": "7"}, io.BytesIO())

    monkeypatch.setattr(bridge_module.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(bridge_module.time, "sleep", sleeps.append)
    with pytest.raises(BridgeRateLimited):
        Bridge("http://bridge.test", "key").purchase_request("pr1")
    assert sleeps == [7.0] * bridge_module.RETRIES
