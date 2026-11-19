"""`Bridge.poll`, the one live read of the machine. Faked at `urlopen`: nothing here goes near the real bridge."""

import io
import urllib.error

import pytest

from tests.test_bridge import FakeResponse
from vending import bridge as bridge_module
from vending.bridge import Bridge, BridgeAnswered, BridgeNoAnswer, BridgeUnreachable

DOCUMENT = b"DXS*VA*V0/6*1\r\nDXE*1*1\r\n"      # text, not JSON


class PolledResponse(FakeResponse):
    """What the bridge sends back to a live read: the document, with its session in a header."""
    headers = {"X-Poll-Session-Id": "fcf24e12ad214de1b637e195f79468f2", "X-Poll-Cached": "false"}


def test_poll_posts_once_with_the_key_and_returns_the_document(monkeypatch):
    seen = []

    def urlopen(request, timeout):
        seen.append((request, timeout))
        return PolledResponse(DOCUMENT)

    monkeypatch.setattr(bridge_module.urllib.request, "urlopen", urlopen)
    read = Bridge("http://bridge.test/", "key").poll()
    assert read.document == DOCUMENT and read.session_id == "fcf24e12ad214de1b637e195f79468f2"
    [(request, timeout)] = seen
    assert request.get_method() == "POST" and request.full_url == "http://bridge.test/api/v1/poll"
    assert request.get_header("X-api-key") == "key" and request.data is None
    assert timeout == bridge_module.POLL_TIMEOUT_SECONDS > bridge_module.TIMEOUT_SECONDS     # a read takes ~8.5 s


@pytest.mark.parametrize("failure, expected", [
    (urllib.error.HTTPError("u", 409, "Conflict", {}, io.BytesIO(b"serial line busy")), BridgeAnswered),
    (TimeoutError("timed out"), BridgeNoAnswer),
    (urllib.error.URLError("no route"), BridgeUnreachable),
])
def test_a_busy_slow_or_missing_bridge_is_one_try_and_a_plain_error(monkeypatch, failure, expected):
    tries, sleeps = [], []

    def urlopen(request, timeout):
        tries.append(request)
        raise failure

    monkeypatch.setattr(bridge_module.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(bridge_module.time, "sleep", sleeps.append)
    with pytest.raises(expected) as raised:
        Bridge("http://bridge.test", "key").poll()
    # Never retried or waited on: a retry would keep the serial line busy for the next caller.
    assert len(tries) == 1 and sleeps == []
    if expected is BridgeAnswered:
        assert raised.value.status == 409


def test_poll_names_no_session_when_the_bridge_sent_none(monkeypatch):
    class NoHeaders(FakeResponse):
        headers = {}

    monkeypatch.setattr(bridge_module.urllib.request, "urlopen", lambda request, timeout: NoHeaders(DOCUMENT))
    read = Bridge("http://bridge.test", "key").poll()
    assert read.document == DOCUMENT and read.session_id is None
