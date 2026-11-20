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
import traceback
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
# The bridge's session ids are 32 hex digits (docs/snapshot-archive.md).
SID, SID2, SID3, LIVE = (f"{n:x}" * 32 for n in (1, 2, 3, 4))
HISTORY, SLOTS, PRODUCTS = "/api/v1/history", "/api/v1/slots", "/api/v1/products"
RAW = f"{HISTORY}/{SID}/raw"
POLL_PATH, HEALTH = "/api/v1/poll", "/health"
POLL = {"session_id": SID, "started_at": "2026-10-01T12:00:00+00:00"}


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
    "raw": (lambda c: c.raw(SID), RAW),
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


@pytest.mark.parametrize("value", ["7919", "987", "156386060", "s1", "live1", SID[:-1], SID + "0", "g" * 32, "", PA1])
def test_a_session_id_is_32_hex_digits_and_a_counter_is_not_one(client, value):
    assert not bridge_module.valid_session_id(value)
    read = client({POLL_PATH: Answer(body=build(SAMPLE), headers={"X-Poll-Session-Id": value})}).poll()
    assert read.session_id is None


@pytest.mark.parametrize("value", [SID, "fcf24e12ad214de1b637e195f79468f2", "0" * 32])
def test_a_documented_session_id_is_valid(value):
    assert bridge_module.valid_session_id(value)
    assert not bridge_module.valid_session_id(None) and not bridge_module.valid_session_id(7919)


def test_a_truncated_day_says_so_without_the_listings_own_figures(client):
    body = json.dumps({"device_id": "vmc1", "polls": [POLL], "truncated": True, "limit": 7919})
    with pytest.raises(BridgeError) as raised:
        client({HISTORY: Answer(body=body.encode())}).history(DAY)
    assert "listing truncated" in str(raised.value)
    assert_clean(raised.value)


def test_history_checks_the_listing_and_leaves_each_row_to_the_sync(client):
    # One bad row must not turn the day into an error: the sync counts and skips it.
    rows = client({HISTORY: listing(POLL, {"started_at": "2026-10-01T12:00:00+00:00"}, PA1, None)}).history(DAY)
    assert len(rows) == 4 and rows[0] == dict(POLL, device_id="vmc1")


# --- Tracebacks: a response's text must not come back through an exception's cause or context --------------

def traceback_of(error: BaseException) -> str:
    return "".join(traceback.format_exception(error))


NOT_HTTP = Answer(wire=PA1.encode() + b"\r\n\r\n")
CUT_SHORT = Answer(body=POISON, content_length=2000)


@pytest.mark.parametrize("name", ["raw", "history"])
@pytest.mark.parametrize("answer", [NOT_HTTP, CUT_SHORT, Answer(status=404, body=POISON, reason=PA1)],
                         ids=["not HTTP", "cut short", "404"])
def test_a_formatted_traceback_of_a_bridge_read_holds_none_of_the_reply(client, name, answer):
    call, path = READS[name]
    with pytest.raises(BridgeError) as raised:
        call(client({path: answer, **HEALTHY}))
    assert_clean(traceback_of(raised.value))


@pytest.mark.parametrize("name", ["history", "slots", "purchase_request", "product_request"])
def test_a_formatted_traceback_of_an_answer_that_is_not_json_holds_none_of_it(client, name):
    call, path = READS[name]
    with pytest.raises(BridgeError) as raised:
        call(client({path: Answer(body=b"\x80\x81" + POISON)}))
    assert_clean(traceback_of(raised.value))


@pytest.mark.parametrize("failure", [
    http.client.BadStatusLine(PA1),
    http.client.IncompleteRead(POISON, 2000 - len(POISON)),
    urllib.error.HTTPError("u", 409, "Conflict", {}, io.BytesIO(POISON)),
], ids=["bad status line", "short read", "refused"])
def test_a_formatted_traceback_of_a_live_read_holds_none_of_the_reply(monkeypatch, failure):
    def urlopen(request, timeout):
        raise failure

    monkeypatch.setattr(bridge_module.urllib.request, "urlopen", urlopen)
    with pytest.raises(BridgeError) as raised:
        Bridge("http://bridge.test", KEY).poll()
    assert_clean(traceback_of(raised.value))


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


def synced_by(conn, routes, serve, **kwargs):
    """The sync of 1 Oct against a fake bridge: its report, everything it logged and the bridge (`.requests`)."""
    server = serve(routes)
    lines = []
    report = sync_history.sync(conn, Bridge(base_url=server.url, api_key=KEY), datetime(2026, 10, 1).date(),
                               datetime(2026, 10, 1).date(), log=lines.append, **kwargs)
    return report, "\n".join(lines), server


def synced(conn, routes, serve, **kwargs):
    report, log, _ = synced_by(conn, routes, serve, **kwargs)
    return report, log


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


GOOD_TIME = "2026-10-01T12:00:00+00:00"
# One row of a day's listing that can't be fetched and stored, each with a value that must never be logged.
BAD_ROWS = {
    "id is a record": {"session_id": PA1, "started_at": GOOD_TIME},
    "id is a counter": {"session_id": "7919", "started_at": GOOD_TIME},       # not 32 hex digits
    "id has a line break": {"session_id": SID2[:-2] + "\r\n" + VA1, "started_at": GOOD_TIME},
    "no id": {"started_at": GOOD_TIME, "bytes": PA2},
    "id is not a string": {"session_id": 7919, "started_at": GOOD_TIME},
    "no start": {"session_id": SID2, "note": VA1},
    "start is a record": {"session_id": SID2, "started_at": VA1},
    "start is not a string": {"session_id": SID2, "started_at": {"x": PA1}},
    "start is a number": {"session_id": SID2, "started_at": 156386060},
    "start is impossible": {"session_id": SID2, "started_at": "2026-02-30T12:00:00+00:00"},
    "start has no offset": {"session_id": SID2, "started_at": "2026-10-01T12:00:00"},
    "start has a month 13": {"session_id": SID2, "started_at": "2026-13-01T12:00:00+00:00"},
    "row is a string": PA2,
    "row is a list": [SID2, VA1],
    "row is null": None,
}


@pytest.mark.parametrize("meta", BAD_ROWS.values(), ids=BAD_ROWS)
def test_a_listed_poll_with_an_unusable_id_or_time_is_skipped_and_never_logged(conn, serve, meta):
    routes = {HISTORY: listing(meta), f"{HISTORY}/{SID2}/raw": Answer(body=build(SAMPLE))}
    report, log, server = synced_by(conn, routes, serve)
    assert (report.failed_days, report.failed_polls, report.new) == (0, 1, 0)
    assert conn.execute("select count(*) from polls").fetchone()[0] == 0
    assert all("/raw" not in path for _, path in server.requests)         # parsed before anything is fetched
    assert_clean(log)
    for value in ("2026-02-30", "2026-13-01", "7919", "156386060", SID2):
        assert value not in log, f"{value!r} is in the log"


def test_one_bad_row_does_not_stop_the_good_polls_of_its_day(conn, serve):
    good, later = build(SAMPLE), build(SAMPLE, corrupt=True)
    rows = [
        POLL,                                                                        # good
        {"started_at": GOOD_TIME},                                                   # no id
        {"session_id": SID2, "started_at": 12345},                                   # a start that isn't a string
        {"session_id": SID2, "started_at": "2026-02-30T12:00:00+00:00"},             # an impossible date
        PA1,                                                                         # not an object
        {"session_id": SID3, "started_at": "2026-10-01T13:00:00+00:00"},             # good, after the bad ones
    ]
    routes = {HISTORY: listing(*rows), RAW: Answer(body=good), f"{HISTORY}/{SID3}/raw": Answer(body=later),
              f"{HISTORY}/{SID2}/raw": Answer(body=POISON)}
    report, log, server = synced_by(conn, routes, serve)
    assert (report.failed_days, report.failed_polls, report.new, report.crc_failed) == (0, 4, 2, 1)
    stored = {r[0]: bytes(r[1]) for r in conn.execute("select session_id, raw from polls")}
    assert stored == {SID: good, SID3: later}                                        # byte for byte
    assert [p for _, p in server.requests if p.endswith("/raw")] == [RAW, f"{HISTORY}/{SID3}/raw"]
    assert "skipped" in log
    assert_clean(log)
    for bad in ("2026-02-30", "12345", str(SID2)):
        assert bad not in log


def test_the_sync_command_with_a_mixed_listing_stores_the_good_poll_and_prints_no_bad_value(
        conn, own_connection, serve, monkeypatch, capsys):
    good = build(SAMPLE)
    rows = [{"session_id": SID2, "started_at": "2026-02-30T12:00:00+00:00"}, POLL, {"started_at": VA1}, PA1]
    monkeypatch.setenv("DEX_BASE_URL", serve({HISTORY: listing(*rows), RAW: Answer(body=good)}).url)
    monkeypatch.setenv("DEX_API_KEY", KEY)
    assert sync_history.main(["--since", DAY, "--until", DAY]) == 1                  # the bad rows are counted
    said = capsys.readouterr()
    assert [(r[0], bytes(r[1])) for r in conn.execute("select session_id, raw from polls")] == [(SID, good)]
    assert "1 new" in said.out and "3 polls the bridge couldn't return" in said.out
    assert_clean(said.out, said.err)
    assert "2026-02-30" not in said.out + said.err


def poll_with_serial(serial: str) -> bytes:
    return build(["ID1*>*" + serial + "*0000**EVS3.0*", *SAMPLE[1:]])


def test_a_poll_that_cannot_be_stored_does_not_stop_the_polls_after_it(conn, serve):
    # A serial with a NUL byte is a good document the database refuses: that used to end the whole run.
    first, last = build(SAMPLE), build(SAMPLE, corrupt=True)
    rows = [POLL, {"session_id": SID2, "started_at": "2026-10-01T13:00:00+00:00"},
            {"session_id": SID3, "started_at": "2026-10-01T14:00:00+00:00"}]
    routes = {HISTORY: listing(*rows), RAW: Answer(body=first),
              f"{HISTORY}/{SID2}/raw": Answer(body=poll_with_serial("RVV\x00NG")),
              f"{HISTORY}/{SID3}/raw": Answer(body=last)}
    report, log, _ = synced_by(conn, routes, serve)
    assert (report.failed_polls, report.new) == (1, 2)
    assert {r[0] for r in conn.execute("select session_id from polls")} == {SID, SID3}
    # A reason of ours, the error's class and its SQLSTATE: never the database's text.
    assert f"could not store {SID2}" in log and "DataError" in log and "NUL" not in log
    assert_clean(log)


def test_a_storage_error_is_logged_as_its_class_and_sqlstate_only(conn, serve, monkeypatch):
    import psycopg

    def refuse(conn, meta, raw):
        raise psycopg.errors.CheckViolation(f'new row violates check constraint\nDETAIL:  Failing row contains ({PA1}, {VA1})')

    monkeypatch.setattr(sync_history, "store", refuse)
    report, log, _ = synced_by(conn, {HISTORY: listing(POLL), RAW: Answer(body=build(SAMPLE))}, serve)
    assert report.failed_polls == 1 and "CheckViolation" in log and "23514" in log
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


POLLED = Answer(body=build(SAMPLE), headers={"X-Poll-Session-Id": LIVE, "X-Poll-Cached": "false"})


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
      f"{HISTORY}/{LIVE}/raw": Answer(body=POISON)}, f"visit reads: read {LIVE}"),
], ids=["busy", "502", "short read", "not HTTP", "receipt not JSON", "session header", "history not JSON",
        "listing with odd ids"])
def test_a_visit_read_logs_no_poll_bytes(conn, serve, monkeypatch, routes, says):
    log = visit(conn, serve, monkeypatch, routes)
    assert says in log
    assert_clean(log)


def test_a_read_stored_under_another_device_says_so_without_naming_it(conn, serve, monkeypatch):
    listing_of_another = {"device_id": PA1, "polls": [{"session_id": LIVE, "started_at": "2026-12-04T20:25:30+00:00"}]}
    log = visit(conn, serve, monkeypatch, {
        REQUEST: PENDING, POLL_PATH: POLLED, HISTORY: Answer(body=json.dumps(listing_of_another).encode()),
        f"{HISTORY}/{LIVE}/raw": Answer(body=build(SAMPLE))})
    assert f"storage not confirmed for read {LIVE}: device mismatch" in log
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


@pytest.mark.parametrize("answer", [NOT_HTTP, CUT_SHORT], ids=["not HTTP", "cut short"])
def test_the_snapshot_script_does_not_chain_a_reply_into_its_error(snapshot, serve, answer):
    # `raise PollError(...)` inside an `except` chains the cause, and a formatted traceback prints it.
    server = serve({POLL_PATH: answer})
    with pytest.raises(snapshot.script.PollError) as raised:
        snapshot.script.http_request("POST", server.url + POLL_PATH, {}, 5)
    assert_clean(traceback_of(raised.value))


@pytest.mark.parametrize("body", [
    b"\x80\x81" + POISON,                    # not UTF-8
    POISON,                                  # not JSON
    json.dumps([PA1]).encode(),              # JSON, but not an object
    json.dumps(PA1).encode(),
], ids=["not UTF-8", "not JSON", "a list", "a string"])
def test_a_health_reply_that_is_not_a_json_object_is_a_named_failure(snapshot, body):
    code, text = snapshot({HEALTH: Answer(body=body)})
    assert code == snapshot.script.EXIT_UNREACHABLE and "reason=health_not_ok" in text
    assert "unexpected" not in text
    assert_clean(text)


def test_the_health_error_does_not_chain_the_reply(snapshot, serve):
    server = serve({HEALTH: Answer(body=b"\x80\x81" + POISON)})
    with pytest.raises(snapshot.script.PollError) as raised:
        snapshot.script.get_health(server.url, 5)
    assert_clean(traceback_of(raised.value))


def test_the_dry_run_logs_only_plain_values_of_the_health_fields(snapshot):
    health = {"status": PA1, "version": {"x": VA1}, "uptime": PA2, "transmit_enabled": True, "extra": PA1}
    code, text = snapshot({HEALTH: Answer(body=json.dumps(health).encode())}, "--dry-run")
    assert code == 0 and "event=dry_run" in text
    assert_clean(text)


def test_the_dry_run_still_logs_plain_health_values(snapshot):
    health = {"status": "ok", "version": "1.4.2", "uptime": "3d 2h", "transmit_enabled": True}
    code, text = snapshot({HEALTH: Answer(body=json.dumps(health).encode())}, "--dry-run")
    assert code == 0 and "health=ok" in text and "version=1.4.2" in text and "uptime=" in text
    assert "transmit_enabled=True" in text


# --- scripts/check_archive.py ----------------------------------------------------------------------------

def test_the_archive_check_prints_the_reason_code_and_never_an_old_detail(tmp_path, monkeypatch, capsys):
    # Logs written before the fix can hold a snippet of the reply in `detail`.
    spec = importlib.util.spec_from_file_location("check_archive_under_test", ROOT / "scripts" / "check_archive.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    (tmp_path / "data" / "snapshots").mkdir(parents=True)
    (tmp_path / "data" / "snapshots" / "20261119T120000Z.dex").write_bytes(b"x")
    (tmp_path / "data" / "logs").mkdir(parents=True)
    (tmp_path / "data" / "logs" / "poll.log").write_text(
        '2026-11-19T12:30:00Z level=ERROR event=poll_failed reason=bridge_error '
        f'detail="POST /api/v1/poll returned 502 Bad Gateway. Response: {PA1} {PA2} {VA1}" exit=6\n', encoding="utf-8")
    monkeypatch.setattr(script, "repo_root", lambda: tmp_path)
    script.main(["--log-lines", "5"])
    out = capsys.readouterr().out
    assert "Most recent error" in out and "reason=bridge_error" in out
    assert_clean(out)
