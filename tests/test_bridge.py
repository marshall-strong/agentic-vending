"""Tests for the real `Bridge` client, against a real HTTP server on this machine.
Retries are instant because `time.sleep` is patched, and nothing here goes near
the real bridge. The two tests at the end fake the network at `urlopen` instead."""

import io
import json
import threading
import time
import urllib.error
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from vending import bridge as bridge_module
from vending.bridge import RETRIES, Bridge, BridgeError, BridgeRateLimited, BridgeUnreachable

KEY = "made-up-key"
DAY = "2026-10-01"
HISTORY = "/api/v1/history"
RAW = "/api/v1/history/s1/raw"
POLL = {"session_id": "s1", "started_at": "2026-10-01T12:00:00+00:00", "bytes": 4}


@dataclass
class Answer:
    status: int = 200
    body: bytes = b""
    content_length: int | None = None  # What to claim, when it isn't the real length.
    wire: bytes | None = None  # Send exactly this and hang up, instead of a proper response.


DROP = Answer(wire=b"")
HEALTHY = {"/health": Answer(body=b'{"status": "ok"}')}


def listing(*polls, **fields):
    return Answer(body=json.dumps({"device_id": "vmc1", "polls": list(polls), **fields}).encode())


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        self.server.requests.append(path)
        self.server.keys.add(self.headers.get("X-API-Key"))
        answers = self.server.routes.get(path, Answer(status=404, body=b"not found"))
        # A list plays out in order, then its last answer repeats.
        if isinstance(answers, list):
            answer = answers.pop(0) if len(answers) > 1 else answers[0]
        else:
            answer = answers
        self.close_connection = True
        if answer.wire is not None:
            self.wfile.write(answer.wire)
            return
        self.send_response(answer.status)
        self.send_header("Content-Length", str(answer.content_length or len(answer.body)))
        self.end_headers()
        self.wfile.write(answer.body)

    def log_message(self, *args):
        pass


@contextmanager
def serve(routes):
    """A fake bridge on a free local port. `routes` maps a path to its answers."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.routes, server.requests, server.keys = routes, [], set()
    server.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def sleeps(monkeypatch):
    """Makes retries instant and records how long each one would have waited."""
    delays = []
    monkeypatch.setattr(time, "sleep", delays.append)
    return delays


@pytest.fixture
def bridge(sleeps):
    """`bridge(routes)` is a real client for a fake bridge serving `routes`."""
    with ExitStack() as cleanup:

        def make(routes):
            server = cleanup.enter_context(serve(routes))
            client = Bridge(base_url=server.url + "/", api_key=KEY)
            client.server = server
            return client

        yield make


def refuses_with_plain_error(call, match=""):
    """`call` raises `BridgeError` itself, the kind that doesn't stop a run."""
    with pytest.raises(BridgeError, match=match) as raised:
        call()
    assert type(raised.value) is BridgeError


def test_raw_returns_the_stored_bytes_and_sends_the_key(bridge, sleeps):
    client = bridge({RAW: Answer(body=b"DXS*\r\n")})
    assert client.raw("s1") == b"DXS*\r\n"
    assert client.server.keys == {KEY}
    assert sleeps == []


def test_raw_quotes_the_session_id(bridge):
    client = bridge({"/api/v1/history/a%20b/raw": Answer(body=b"ok")})
    assert client.raw("a b") == b"ok"


def test_history_returns_the_polls_with_the_device_id(bridge):
    client = bridge({HISTORY: listing(POLL)})
    assert client.history(DAY) == [dict(POLL, device_id="vmc1")]


def test_a_day_with_no_polls_is_an_empty_list(bridge):
    assert bridge({HISTORY: listing()}).history(DAY) == []


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_key_stops_at_once(bridge, sleeps, status):
    client = bridge({RAW: Answer(status=status), **HEALTHY})
    with pytest.raises(BridgeUnreachable, match="DEX_API_KEY"):
        client.raw("s1")
    assert client.server.requests == [RAW]
    assert sleeps == []


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_a_busy_or_failing_bridge_is_retried_with_a_growing_wait(bridge, sleeps, status):
    client = bridge({RAW: [Answer(status=status), Answer(status=status), Answer(body=b"ok")]})
    assert client.raw("s1") == b"ok"
    assert sleeps == [2.0, 4.0]


def test_retries_run_out_as_an_error_for_this_poll_alone(bridge, sleeps):
    client = bridge({RAW: Answer(status=503, body=b"busy")})
    refuses_with_plain_error(lambda: client.raw("s1"), match="503.*busy")
    assert client.server.requests == [RAW] * (RETRIES + 1)
    assert sleeps == [2.0, 4.0, 8.0, 16.0]


def test_not_found_is_not_retried(bridge, sleeps):
    client = bridge({})
    refuses_with_plain_error(lambda: client.raw("s1"), match="404.*not found")
    assert client.server.requests == [RAW]
    assert sleeps == []


def test_an_error_response_cut_off_is_still_a_plain_error(bridge):
    client = bridge({RAW: Answer(status=404, body=b"not", content_length=1000)})
    refuses_with_plain_error(lambda: client.raw("s1"), match="404")


def test_an_error_response_cut_off_mid_chunk_is_still_a_plain_error(bridge):
    # Unlike a short `Content-Length`, this raises even on a partial read.
    cut_off = b"HTTP/1.1 404 Not Found\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nno"
    client = bridge({RAW: Answer(wire=cut_off)})
    refuses_with_plain_error(lambda: client.raw("s1"), match="404")


def test_a_reply_that_is_not_http_is_an_error_for_that_request_alone(bridge):
    client = bridge({RAW: Answer(wire=b"not http\r\n\r\n"), **HEALTHY})
    refuses_with_plain_error(lambda: client.raw("s1"), match="broken response")
    assert client.server.requests == [RAW] * (RETRIES + 1)


def test_a_truncated_day_is_refused(bridge):
    client = bridge({HISTORY: listing(POLL, truncated=True, limit=500)})
    refuses_with_plain_error(lambda: client.history(DAY), match="truncated at 500")


def test_a_dropped_connection_is_retried(bridge, sleeps):
    client = bridge({RAW: [DROP, DROP, Answer(body=b"ok")]})
    assert client.raw("s1") == b"ok"
    assert sleeps == [2.0, 4.0]


def test_a_day_whose_connection_keeps_dropping_stops_the_run(bridge):
    client = bridge({HISTORY: DROP, **HEALTHY})
    with pytest.raises(BridgeUnreachable, match="Tailscale"):
        client.history(DAY)


def test_nothing_listening_is_an_outage(sleeps, monkeypatch):
    # Windows takes about two seconds to give up on each refused connection.
    monkeypatch.setattr("vending.bridge.RETRIES", 0)
    with serve({}) as server:
        url = server.url
    with pytest.raises(BridgeUnreachable, match="Tailscale"):
        Bridge(base_url=url, api_key=KEY).raw("s1")


def test_a_short_body_that_recovers_is_retried(bridge, sleeps):
    client = bridge({RAW: [Answer(body=b"DXS", content_length=1000), Answer(body=b"ok")]})
    assert client.raw("s1") == b"ok"
    assert sleeps == [2.0]


def test_a_poll_with_a_short_body_is_an_error_for_that_poll_alone(bridge):
    client = bridge({RAW: Answer(body=b"DXS", content_length=1000), **HEALTHY})
    refuses_with_plain_error(lambda: client.raw("s1"), match="broken response")
    assert client.server.requests == [RAW] * (RETRIES + 1)


def test_a_day_with_a_short_body_is_an_error_for_that_day_alone(bridge):
    client = bridge({HISTORY: Answer(body=b'{"polls"', content_length=1000)})
    refuses_with_plain_error(lambda: client.history(DAY), match="broken response")


# --- Faked at `urlopen`: the purchase-request read and the 429 back-off -------

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
