"""Logs, exceptions and job output carry no poll bytes (`docs/integrity.md`, "Logs carry no poll bytes").

From the fall cut, nothing may show per-selection sales, prices or stock outside our assignment, or the
machine-wide VA1 totals. The worker's logs and /health are read by people, so what the bridge sent back
(an error body, a malformed document, a short read, a bad status line, a header) must never reach them.
What may reach them is the status, a session id that passes validation, sizes, times and a reason the
code wrote itself ("HTTP 502", "short read: 30 of 2000 bytes").

Every test here answers with a reply holding the other team's records: `PA1*11*987*`, `PA2*7919*...` and
`VA1*156386060*...`. It runs the real code (the real `Bridge` against a fake bridge on this machine, the
real jobs, the real script) and checks every line it logged, raised or printed.
"""

import http.client
import importlib.util
import io
import json
import re
import threading
import time
import urllib.error
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("psycopg")

from tests.conftest import open_in  # noqa: E402
from tests.test_evadts import SAMPLE, build  # noqa: E402
from vending import bridge as bridge_module  # noqa: E402
from vending import db, evadts, slots, sync_catalog, sync_history, sync_requests, visit_polls, worker  # noqa: E402
from vending.bridge import Bridge, BridgeAnswered, BridgeError, BridgeNoAnswer, BridgeUnreachable  # noqa: E402
from vending.purchase_requests import record  # noqa: E402
from vending.visit_polls import Reader, Schedule  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# What the other team's records look like in a reply. None of the figures can show by chance.
PA1, PA2, VA1 = "PA1*11*987*", "PA2*7919*7816053*0*0*0*0", "VA1*156386060*9999*0*0"
POISON = "\r\n".join((PA1, PA2, VA1)).encode() + b"\r\n"
FIGURES = (987, 7919, 7816053, 156386060)


def leaked(text: str) -> list[str]:
    """Every sentinel in `text`: a figure of those records, a record tag, or any fragment of a record
    six characters long or more."""
    found = [f"figure {n}" for n in FIGURES if re.search(rf"(?<![\w.]){n}(?![\w])", text)]
    found += [f"tag {tag}" for tag in ("PA1", "PA2", "VA1") if re.search(rf"{tag}\*", text)]
    for record_text in (PA1, PA2, VA1):
        found += [f"fragment {record_text[i:i + 6]!r}" for i in range(len(record_text) - 5)
                  if record_text[i:i + 6] in text]
    return found


def assert_clean(*texts) -> None:
    for text in texts:
        assert not leaked(str(text)), f"poll bytes in {str(text)!r}: {leaked(str(text))}"


def test_the_check_sees_what_it_looks_for():
    # Without this a `leaked` that found nothing would pass every test below.
    assert leaked(f"HTTP 502: {PA1}") and leaked(f"saw {PA2!r}") and leaked(f"saw {VA1[:9]}")
    assert leaked("987") and leaked("total 156386060zz") and leaked(POISON.decode())
    assert not leaked("HTTP 502: bad gateway") and not leaked("short read: 30 of 2000 bytes, session abc123")


# --- A fake bridge ---------------------------------------------------------------------------------

KEY = "made-up-key"
DAY = "2026-10-01"
HISTORY, RAW, SLOTS, PRODUCTS = "/api/v1/history", "/api/v1/history/s1/raw", "/api/v1/slots", "/api/v1/products"
POLL_PATH, HEALTH = "/api/v1/poll", "/health"
POLL = {"session_id": "s1", "started_at": "2026-10-01T12:00:00+00:00"}


@dataclass
class Answer:
    status: int = 200
    body: bytes = b""
    reason: str | None = None               # the status line's reason phrase
    headers: dict = field(default_factory=dict)
    content_length: int | None = None       # what to claim, when it isn't the real length
    wire: bytes | None = None               # send exactly this and hang up, instead of a proper response


HEALTHY = {HEALTH: Answer(body=b'{"status": "ok", "transmit_enabled": true}')}


def listing(*polls):
    return Answer(body=json.dumps({"device_id": "vmc1", "polls": list(polls)}).encode())


class Handler(BaseHTTPRequestHandler):
    def _answer(self):
        path = self.path.split("?")[0]
        self.server.requests.append((self.command, path))
        self.rfile.read(int(self.headers.get("Content-Length") or 0))    # a client that is still sending would be reset
        # A route nobody planned for answers with the poison too, so a stray request can't hide a leak.
        answer = self.server.routes.get(path, Answer(status=404, body=POISON))
        self.close_connection = True
        if answer.wire is not None:
            self.wfile.write(answer.wire)
            return
        self.send_response(answer.status, answer.reason)
        self.send_header("Content-Length", str(answer.content_length or len(answer.body)))
        for name, value in answer.headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(answer.body)

    do_GET = do_POST = _answer

    def log_message(self, *args):
        pass


@contextmanager
def serve_routes(routes):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.routes, server.requests = routes, []
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
def serve():
    """`serve(routes)` is a fake bridge on a free local port, shut down after the test."""
    with ExitStack() as cleanup:
        yield lambda routes: cleanup.enter_context(serve_routes(routes))


@pytest.fixture(autouse=True)
def _no_waits(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _: None)


@pytest.fixture
def client(serve):
    """`client(routes)` is a real `Bridge` for a fake bridge serving `routes`."""
    return lambda routes: Bridge(base_url=serve(routes).url + "/", api_key=KEY)


# --- vending/bridge.py -----------------------------------------------------------------------------

READS = {
    "raw": (lambda c: c.raw("s1"), RAW),
    "history": (lambda c: c.history(DAY), HISTORY),
    "slots": (lambda c: c.slots(), SLOTS),
    "purchase_request": (lambda c: c.purchase_request("pr1"), "/api/v1/purchase-requests/pr1"),
    "product_request": (lambda c: c.product_request("np1"), "/api/v1/product-requests/np1"),
    "products": (lambda c: c.products(), PRODUCTS),
}


@pytest.mark.parametrize("name", READS)
@pytest.mark.parametrize("status", [404, 503])
def test_a_read_that_is_answered_with_an_error_names_the_status_and_not_the_body(client, name, status):
    call, path = READS[name]
    with pytest.raises(BridgeError) as raised:
        call(client({path: Answer(status=status, body=POISON, reason=PA1), **HEALTHY}))
    assert str(status) in str(raised.value)
    assert_clean(raised.value, repr(raised.value), getattr(raised.value, "detail", ""))


@pytest.mark.parametrize("name", [n for n in READS if n != "products"])
def test_an_outage_message_names_the_status_and_not_the_body(client, name):
    call, path = READS[name]
    bad = Answer(status=502, body=POISON)
    with pytest.raises(BridgeUnreachable) as raised:
        call(client({path: bad, HEALTH: bad}))
    assert "502" in str(raised.value)
    assert_clean(raised.value)


@pytest.mark.parametrize("name, body", [
    ("history", POISON),                                        # not JSON
    ("history", json.dumps(POISON.decode()).encode()),          # JSON, but not a listing
    ("slots", POISON),
    ("purchase_request", POISON),
    ("product_request", POISON),
    ("products", POISON),
])
def test_a_reply_that_is_not_what_was_asked_for_is_not_quoted(client, name, body):
    call, path = READS[name]
    with pytest.raises(BridgeError) as raised:
        call(client({path: Answer(body=body)}))
    assert_clean(raised.value, repr(raised.value))


@pytest.mark.parametrize("name", ["raw", "history"])
def test_a_reply_that_is_not_http_names_no_part_of_it(client, name):
    call, path = READS[name]
    with pytest.raises(BridgeError) as raised:
        call(client({path: Answer(wire=PA1.encode() + b"\r\n\r\n"), **HEALTHY}))
    assert "broken response" in str(raised.value)
    assert_clean(raised.value, repr(raised.value))


@pytest.mark.parametrize("name", ["raw", "history"])
def test_a_read_cut_short_says_how_much_arrived_and_nothing_of_it(client, name):
    call, path = READS[name]
    with pytest.raises(BridgeError) as raised:
        call(client({path: Answer(body=POISON[:30], content_length=2000), **HEALTHY}))
    assert "short read: 30 of 2000 bytes" in str(raised.value)
    assert_clean(raised.value, repr(raised.value))


@pytest.mark.parametrize("status, reason", [(409, "Conflict"), (502, PA1), (500, None)])
def test_a_live_read_that_is_refused_names_the_status_and_not_the_body(client, status, reason):
    with pytest.raises(BridgeAnswered) as raised:
        client({POLL_PATH: Answer(status=status, body=POISON, reason=reason)}).poll()
    assert raised.value.status == status and str(status) in str(raised.value)
    assert_clean(raised.value, repr(raised.value), raised.value.detail, raised.value.args)


def test_a_live_read_refused_by_a_cut_off_urlopen_error_names_no_body(monkeypatch):
    def urlopen(request, timeout):
        raise urllib.error.HTTPError("u", 409, "Conflict", {}, io.BytesIO(POISON))

    monkeypatch.setattr(bridge_module.urllib.request, "urlopen", urlopen)
    with pytest.raises(BridgeAnswered) as raised:
        Bridge("http://bridge.test", KEY).poll()
    assert_clean(raised.value, raised.value.detail)


@pytest.mark.parametrize("failure", [
    http.client.BadStatusLine(PA1),
    http.client.IncompleteRead(POISON, 2000 - len(POISON)),
    http.client.LineTooLong(PA2),
])
def test_a_live_read_that_breaks_off_names_no_part_of_the_reply(monkeypatch, failure):
    def urlopen(request, timeout):
        raise failure

    monkeypatch.setattr(bridge_module.urllib.request, "urlopen", urlopen)
    with pytest.raises(BridgeNoAnswer) as raised:
        Bridge("http://bridge.test", KEY).poll()
    assert_clean(raised.value, repr(raised.value))
    if isinstance(failure, http.client.IncompleteRead):
        assert f"short read: {len(POISON)} of 2000 bytes" in str(raised.value)


def test_a_live_read_cut_short_says_how_much_arrived(client):
    with pytest.raises(BridgeNoAnswer) as raised:
        client({POLL_PATH: Answer(body=POISON[:30], content_length=2000)}).poll()
    assert "short read: 30 of 2000 bytes" in str(raised.value)
    assert_clean(raised.value)


def test_a_session_id_that_does_not_validate_is_not_passed_on(client):
    odd = {"X-Poll-Session-Id": PA1, "X-Poll-Cached": VA1, "X-Poll-Age-Seconds": PA2}
    read = client({POLL_PATH: Answer(body=build(SAMPLE), headers=odd)}).poll()
    assert read.session_id is None and read.cached is None and read.age_seconds is None
    named = {"X-Poll-Session-Id": "fcf24e12ad214de1b637e195f79468f2"}
    assert client({POLL_PATH: Answer(body=build(SAMPLE), headers=named)}).poll().session_id == named["X-Poll-Session-Id"]


def test_a_write_still_carries_the_servers_reason(client):
    # Not a poll: the answer to our own purchase request, which the agent needs to read.
    answer = Answer(status=422, body=b'{"error": "invalid_lines"}')
    with pytest.raises(BridgeAnswered) as raised:
        client({"/api/v1/purchase-requests": answer}).post("/api/v1/purchase-requests", {})
    assert "invalid_lines" in raised.value.detail


# --- vending/evadts.py and vending/slots.py ----------------------------------------------------------

def test_a_garbled_counter_names_no_part_of_the_document():
    records = [r for r in SAMPLE if not r.startswith(("VA1", "PA1*15", "PA2*30"))]
    with pytest.raises(evadts.ParseError) as raised:
        evadts.parse(build([*records, "VA1*156386060zz*9999*0*0", PA1, PA2]))
    assert_clean(raised.value)


def test_an_assignment_that_is_not_an_object_names_no_part_of_the_answer():
    with pytest.raises(slots.BadAssignment) as raised:
        slots.parse([PA1, PA2, VA1])
    assert_clean(raised.value)


# --- vending/sync_history.py -------------------------------------------------------------------------

@pytest.fixture
def own_connection(schema_url, monkeypatch):
    """What each job opens for itself is the test's schema."""
    monkeypatch.setattr(db, "connect", lambda: open_in(*schema_url))


def synced(conn, routes, serve, **kwargs):
    server = serve(routes)
    lines = []
    report = sync_history.sync(conn, Bridge(base_url=server.url, api_key=KEY), datetime(2026, 10, 1).date(),
                               datetime(2026, 10, 1).date(), log=lines.append, **kwargs)
    return report, "\n".join(lines)


@pytest.mark.parametrize("routes, field_, says", [
    ({HISTORY: Answer(body=POISON)}, "failed_days", "not JSON"),
    ({HISTORY: listing(POLL), RAW: Answer(status=404, body=POISON, reason=PA1)}, "failed_polls", "404"),
    ({HISTORY: listing(POLL), RAW: Answer(status=503, body=POISON), **HEALTHY}, "failed_polls", "503"),
    ({HISTORY: listing(POLL), RAW: Answer(wire=PA1.encode() + b"\r\n\r\n"), **HEALTHY}, "failed_polls", "broken response"),
    ({HISTORY: listing(POLL), RAW: Answer(body=POISON[:30], content_length=2000), **HEALTHY}, "failed_polls",
     "short read: 30 of 2000 bytes"),
], ids=["day not JSON", "poll 404", "poll 503", "poll not HTTP", "poll cut short"])
def test_a_day_or_poll_the_sync_cannot_read_is_logged_with_a_reason_of_our_own(conn, serve, routes, field_, says):
    report, log = synced(conn, routes, serve)
    assert getattr(report, field_) == 1 and says in log
    assert_clean(log)


@pytest.mark.parametrize("meta", [
    {"session_id": PA1, "started_at": VA1},                                 # nothing usable
    {"session_id": "ok1", "started_at": PA2},                               # a start that isn't a time
    {"session_id": "ok1\r\n" + VA1, "started_at": "2026-10-01T12:00:00+00:00"},
])
def test_a_listed_poll_with_an_unusable_id_or_time_is_skipped_and_never_logged(conn, serve, meta):
    from urllib.parse import quote

    routes = {HISTORY: listing(meta), f"{HISTORY}/{quote(meta['session_id'])}/raw": Answer(body=POISON),
              f"{HISTORY}/ok1/raw": Answer(body=build(SAMPLE))}
    report, log = synced(conn, routes, serve)
    assert report.failed_polls == 1 and report.new == 0
    assert conn.execute("select count(*) from polls").fetchone()[0] == 0
    assert_clean(log)


@pytest.mark.parametrize("routes, code", [
    ({HISTORY: Answer(body=POISON)}, 1),
    ({HISTORY: Answer(status=502, body=POISON), HEALTH: Answer(status=502, body=POISON)}, 3),
    ({HISTORY: listing(POLL), RAW: Answer(status=401, body=POISON)}, 3),
])
def test_the_sync_command_prints_no_poll_bytes(own_connection, serve, monkeypatch, capsys, routes, code):
    monkeypatch.setenv("DEX_BASE_URL", serve(routes).url)
    monkeypatch.setenv("DEX_API_KEY", KEY)
    assert sync_history.main(["--since", DAY, "--until", DAY]) == code
    said = capsys.readouterr()
    assert said.out + said.err
    assert_clean(said.out, said.err)


# --- The worker's jobs, as the round runs them, and what /health shows -----------------------------------

@pytest.mark.parametrize("job", ["sync_history", "sync_requests", "sync_catalog", "slots_check"])
def test_a_job_that_meets_a_poisoned_bridge_logs_prints_and_shows_no_poll_bytes(
        conn, own_connection, serve, monkeypatch, capsys, job):
    routes = {HISTORY: Answer(body=POISON), PRODUCTS: Answer(status=502, body=POISON, reason=PA2),
              "/api/v1/purchase-requests/r1": Answer(body=POISON), SLOTS: Answer(status=502, body=POISON)}
    monkeypatch.setenv("DEX_BASE_URL", serve(routes).url)
    monkeypatch.setenv("DEX_API_KEY", KEY)
    record(conn, "r1", datetime(2026, 12, 3, 20, tzinfo=timezone.utc), False, {"off_cycle": False, "lines": []})
    run = {"sync_history": lambda: sync_history.main(["--since", DAY, "--until", DAY]),
           "sync_requests": lambda: sync_requests.main([]),
           "sync_catalog": lambda: sync_catalog.main([]),
           "slots_check": lambda: slots.main(["check"])}[job]
    logged = []
    state = worker.State()
    state.results = worker.run_round([(job, run)], logged.append)
    said = capsys.readouterr()
    assert state.results[0].outcome == "finished" and state.results[0].exit_code != 0
    assert said.out + said.err
    assert_clean("\n".join(logged), said.out, said.err, json.dumps(state.public()), state.results[0].error or "")


def test_a_job_that_crashes_on_a_bridge_error_leaves_no_poll_bytes_in_the_log(serve, monkeypatch):
    def job():
        Bridge(base_url=serve({POLL_PATH: Answer(wire=PA1.encode() + b"\r\n\r\n")}).url, api_key=KEY).poll()
        return 0

    logged = []
    [result] = worker.run_round([("visit_read", job)], logged.append)
    assert result.outcome == "crashed"
    assert_clean("\n".join(logged), result.error)


# --- vending/visit_polls.py --------------------------------------------------------------------------

ET = ZoneInfo("America/New_York")
SCHEDULE = Schedule(timedelta(minutes=1), datetime(2026, 1, 1, 14, 45).time(), datetime(2026, 1, 1, 19, 0).time())
FRIDAY_3_25 = datetime(2026, 12, 4, 15, 25, tzinfo=ET).astimezone(timezone.utc)
PENDING = Answer(body=json.dumps({"id": "r1", "status": "pending", "created_at": "2026-11-07T14:00:00Z"}).encode())
REQUEST = "/api/v1/purchase-requests/r1"


def visit(conn, serve, monkeypatch, routes):
    """One tick of the visit reader against a fake bridge, with a request of ours pending. Returns its log."""
    said = []
    record(conn, "r1", datetime(2026, 12, 3, 20, tzinfo=timezone.utc), False, {"off_cycle": False, "lines": []})
    server = serve(routes)
    monkeypatch.setattr(visit_polls.db, "connect", lambda: nullcontext(conn))
    monkeypatch.setattr(visit_polls, "Bridge", lambda: Bridge(base_url=server.url, api_key=KEY))
    monkeypatch.setattr(visit_polls, "_utcnow", lambda: FRIDAY_3_25 + timedelta(seconds=5))
    monkeypatch.setattr(visit_polls, "_last_post", None)
    monkeypatch.setattr(visit_polls, "_last_post_ok", None)
    Reader(SCHEDULE, lambda now: visit_polls.step(now, said.append, SCHEDULE), said.append).tick(FRIDAY_3_25)
    return "\n".join(said)


POLLED = Answer(body=build(SAMPLE), headers={"X-Poll-Session-Id": "live1", "X-Poll-Cached": "false"})


@pytest.mark.parametrize("routes, says", [
    ({REQUEST: PENDING, POLL_PATH: Answer(status=409, body=POISON, reason=PA1)}, "409"),             # busy, with a body
    ({REQUEST: PENDING, POLL_PATH: Answer(status=502, body=POISON)}, "502"),
    ({REQUEST: PENDING, POLL_PATH: Answer(body=POISON[:30], content_length=2000)},                   # cut short
     "short read: 30 of 2000 bytes"),
    ({REQUEST: PENDING, POLL_PATH: Answer(wire=PA1.encode() + b"\r\n\r\n")}, "skipped"),             # not HTTP
    ({REQUEST: Answer(body=POISON), POLL_PATH: POLLED, HISTORY: listing()}, "could not read r1"),    # receipt read
    ({REQUEST: PENDING, POLL_PATH: Answer(body=build(SAMPLE), headers={"X-Poll-Session-Id": PA1}),   # header named
      HISTORY: listing()}, "visit reads: read"),
    ({REQUEST: PENDING, POLL_PATH: POLLED, HISTORY: Answer(body=POISON)}, "could not list"),
    ({REQUEST: PENDING, POLL_PATH: POLLED, HISTORY: listing({"session_id": VA1, "started_at": PA2}),
      f"{HISTORY}/live1/raw": Answer(body=POISON)}, "visit reads: read live1"),
], ids=["busy", "502", "short read", "not HTTP", "receipt not JSON", "session header", "history not JSON",
        "listing with odd ids"])
def test_a_visit_read_logs_no_poll_bytes(conn, serve, monkeypatch, routes, says):
    log = visit(conn, serve, monkeypatch, routes)
    assert says in log
    assert_clean(log)


# --- scripts/poll_snapshot.py --------------------------------------------------------------------------

@pytest.fixture
def snapshot(monkeypatch, tmp_path, capsys, serve):
    """`snapshot(routes, *argv)` is the real script against a fake bridge, with its repo root in a temporary
    folder. Returns its exit code and everything it logged or printed (the archive itself keeps the document)."""
    spec = importlib.util.spec_from_file_location("poll_snapshot_under_test", ROOT / "scripts" / "poll_snapshot.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    (tmp_path / ".env").write_text("DEX_BASE_URL=http://unused\nDEX_API_KEY=unused\n")
    monkeypatch.setattr(script, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(script, "check_tailscale", lambda peer: "tailnet skipped")

    def run(routes, *argv):
        monkeypatch.setenv("DEX_BASE_URL", serve({**HEALTHY, **routes}).url)
        monkeypatch.setenv("DEX_API_KEY", KEY)
        code = script.main(["--timeout", "5", *argv])
        said = capsys.readouterr()
        log = tmp_path / "data" / "logs" / "poll.log"
        return code, (log.read_text(encoding="utf-8") if log.exists() else "") + said.out + said.err

    run.script = script
    return run


@pytest.mark.parametrize("status", [401, 403, 409, 500, 502])
def test_a_snapshot_the_bridge_refuses_logs_the_status_and_not_the_body(snapshot, status):
    code, text = snapshot({POLL_PATH: Answer(status=status, body=POISON, reason=PA1)})
    assert code != 0 and str(status) in text
    assert_clean(text)


def test_a_document_that_is_not_a_whole_read_logs_no_line_of_it(snapshot):
    document = b"\r\n".join([PA1.encode(), b"ID4*2*1840*5", b"ID4*2*1840*5", VA1.encode()]) + b"\r\n"
    code, text = snapshot({POLL_PATH: Answer(body=document)})
    assert code == snapshot.script.EXIT_INVALID and "invalid_snapshot" in text
    assert_clean(text)


def test_headers_that_do_not_validate_are_not_logged(snapshot):
    odd = {"X-Poll-Session-Id": PA1, "X-Poll-Cached": VA1, "X-Poll-Age-Seconds": PA2}
    code, text = snapshot({POLL_PATH: Answer(body=build(SAMPLE), headers=odd)})
    assert "event=" in text
    assert_clean(text)


def test_a_snapshot_cut_short_says_how_much_arrived_and_nothing_of_it(snapshot):
    code, text = snapshot({POLL_PATH: Answer(body=POISON, content_length=1000)})
    assert code != 0 and f"short read: {len(POISON)} of 1000 bytes" in text
    assert_clean(text)


def test_a_reply_that_is_not_http_logs_no_part_of_it(snapshot):
    code, text = snapshot({POLL_PATH: Answer(wire=PA1.encode() + b"\r\n\r\n")})
    assert code != 0 and "poll_failed" in text
    assert_clean(text)
