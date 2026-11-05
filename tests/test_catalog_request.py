"""`request_catalog_product` through the MCP client, the write gate and Postgres,
against a local HTTP server standing in for the bridge's two routes."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.conftest import open_in
from tests.test_server import call, db  # noqa: F401  (db is a fixture)
from vending import bridge as bridge_module
from vending import server
from vending.bridge import Bridge
from vending.config import set_halted
from vending.tools import catalog_request

CATALOG = [
    {"sku": "COKE-20", "name": "Coca-Cola", "flavor": "Original", "size": "20 oz bottle"},
    {"sku": "DIET-12", "name": "Diet Coke", "flavor": None, "size": "12 fl oz can"},
]


class FakeBridge:
    """Records each request; `answer` sets the POST reply as (status, body, delay)."""

    def __init__(self):
        self.posts: list[dict] = []
        self.gets = 0
        self.catalog_status = 200
        self.answer = (201, {"id": "req-1", "status": "pending"}, 0.0)
        # Called as each POST arrives, before it is answered.
        self.on_post = lambda: None
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                assert self.path == "/api/v1/products" and self.headers["X-API-Key"] == "test-key"
                fake.gets += 1
                self._reply(fake.catalog_status, {"products": CATALOG})

            def do_POST(self):
                assert self.path == "/api/v1/product-requests" and self.headers["X-API-Key"] == "test-key"
                fake.posts.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                fake.on_post()
                status, body, delay = fake.answer
                time.sleep(delay)
                try:
                    self._reply(status, body)
                except OSError:
                    pass  # the client gave up waiting

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def fake(db, monkeypatch):  # noqa: F811
    set_halted(db, False, "test")
    bridge = FakeBridge()
    monkeypatch.setattr(server, "bridge", lambda: Bridge(bridge.url, "test-key"))
    monkeypatch.setattr(bridge_module, "RETRIES", 0)
    yield bridge
    bridge.server.shutdown()
    bridge.server.server_close()


def ask(**arguments):
    result = call("request_catalog_product", {"name": "Coca-Cola Cherry", "size": "20 oz bottle", **arguments})
    assert not result.isError, result.content[0].text
    return result.structuredContent


def codes(reply):
    return sorted(r["code"] for r in reply["refusals"])


def test_sends_the_request_and_records_it(db, fake):  # noqa: F811
    reply = ask(line="Coca-Cola", flavor="", note="students keep asking")
    assert reply["success"] and (reply["request_id"], reply["status"]) == ("req-1", "pending")
    # The empty flavor is left out: the bridge rejects an empty optional field.
    assert fake.posts == [{"name": "Coca-Cola Cherry", "size": "20 oz bottle", "line": "Coca-Cola",
                           "note": "students keep asking"}]
    assert db.execute("select name, bridge_id, status, words from product_requests").fetchall() == [
        ("Coca-Cola Cherry", "req-1", "pending", ["20", "cherry", "coca", "cola", "oz"])]
    assert db.execute("select outcome from action_log where tool = 'request_catalog_product'").fetchall() == [
        ("done",)]


def test_the_same_product_again_is_refused_without_sending(db, fake):  # noqa: F811
    ask()
    reply = ask(name="coca-cola cherry", size="20oz")
    assert not reply["success"] and codes(reply) == ["already_requested"]
    assert "req-1" in reply["refusals"][0]["message"]
    assert len(fake.posts) == 1
    assert db.execute("select outcome from action_log where tool = 'request_catalog_product' order by id").fetchall() == [("done",), ("refused",)]


def test_a_more_specific_product_is_not_a_duplicate(db, fake):  # noqa: F811
    ask(name="Cherry", size="20 oz")
    fake.answer = (201, {"id": "req-2", "status": "pending"}, 0.0)
    assert ask(name="Cherry Zero", size="20 oz")["success"]
    assert len(fake.posts) == 2


def test_a_product_already_in_the_catalog_is_refused(db, fake):  # noqa: F811
    reply = ask(name="Diet Coke", size="12 oz can")
    assert codes(reply) == ["already_in_catalog"] and "DIET-12" in reply["refusals"][0]["message"]
    assert fake.posts == []


def test_every_field_problem_is_refused_at_once(db, fake):  # noqa: F811
    reply = ask(name="Hard Seltzer Lime", size="one can", flavor="x" * 101)
    assert codes(reply) == ["alcohol_or_thc", "bad_field", "not_a_drink"]
    assert fake.posts == []


def test_root_beer_is_not_alcohol(db, fake):  # noqa: F811
    assert ask(name="A&W Root Beer", size="20 oz bottle")["success"]


def test_too_many_open_requests_is_a_refusal(db, fake):  # noqa: F811
    fake.answer = (429, {"detail": "too many open requests"}, 0.0)
    reply = ask()
    assert codes(reply) == ["too_many_open_requests"]
    assert db.execute("select count(*) from product_requests").fetchone() == (0,)


def test_a_rejected_request_stores_nothing(db, fake):  # noqa: F811
    fake.answer = (422, {"detail": "bad size"}, 0.0)
    reply = ask()
    assert not reply["success"] and "422" in reply["message"]
    assert db.execute("select count(*) from product_requests").fetchone() == (0,)
    assert db.execute("select outcome from action_log where tool = 'request_catalog_product'").fetchall() == [("failed",)]


@pytest.mark.parametrize("answer", [(503, {"detail": "down"}, 0.0), (201, {"id": "late"}, 2.0)])
def test_no_answer_is_stored_as_unknown_and_blocks_a_resend(db, fake, monkeypatch, answer):  # noqa: F811
    monkeypatch.setattr(bridge_module, "TIMEOUT_SECONDS", 0.5)
    fake.answer = answer
    reply = ask()
    assert not reply["success"] and reply["status"] == "unknown"
    assert db.execute("select bridge_id, status from product_requests").fetchall() == [(None, "unknown")]
    fake.answer = (201, {"id": "req-9", "status": "pending"}, 0.0)
    again = ask()
    assert codes(again) == ["already_requested"] and "a person must check" in again["refusals"][0]["message"]
    assert len(fake.posts) == 1


def test_the_request_is_saved_before_it_is_sent(db, fake, schema_url):  # noqa: F811
    seen = []

    def look():
        # A separate connection sees only committed rows.
        with open_in(*schema_url) as other:
            seen.extend(other.execute("select status from product_requests").fetchall())

    fake.on_post = look
    assert ask()["success"]
    assert seen == [("sending",)]
    assert db.execute("select bridge_id, status from product_requests").fetchall() == [("req-1", "pending")]


def test_a_crash_before_the_answer_is_stored_keeps_the_row_and_blocks_a_resend(db, fake, monkeypatch):  # noqa: F811
    def crash(*args):
        raise RuntimeError("simulated crash before the answer was stored")

    original = catalog_request._record_answer
    monkeypatch.setattr(catalog_request, "_record_answer", crash)
    assert call("request_catalog_product", {"name": "Coca-Cola Cherry", "size": "20 oz bottle"}).isError
    assert db.execute("select bridge_id, status from product_requests").fetchall() == [(None, "sending")]
    monkeypatch.setattr(catalog_request, "_record_answer", original)
    again = ask()
    assert codes(again) == ["already_requested"] and "a person must check" in again["refusals"][0]["message"]
    assert len(fake.posts) == 1


def test_the_answer_is_kept_when_the_gate_fails_after_it(db, fake, monkeypatch):  # noqa: F811
    original = catalog_request._record_answer

    def store_then_crash(*args):
        original(*args)
        raise RuntimeError("simulated failure after the answer was stored, e.g. the log insert")

    monkeypatch.setattr(catalog_request, "_record_answer", store_then_crash)
    assert call("request_catalog_product", {"name": "Coca-Cola Cherry", "size": "20 oz bottle"}).isError
    assert db.execute("select bridge_id, status from product_requests").fetchall() == [("req-1", "pending")]
    assert db.execute("select outcome from action_log where tool = 'request_catalog_product'").fetchall() == [
        ("failed",)]


def test_an_accepted_answer_without_an_id_is_unknown(db, fake):  # noqa: F811
    fake.answer = (201, {"status": "pending"}, 0.0)
    reply = ask()
    assert not reply["success"] and reply["status"] == "unknown" and "no request ID" in reply["message"]
    assert db.execute("select bridge_id, status, last_response from product_requests").fetchall() == [
        (None, "unknown", {"status": "pending"})]
    assert codes(ask()) == ["already_requested"]
    assert len(fake.posts) == 1


@pytest.mark.parametrize("first, again", [
    ("20 oz bottle", "20 oz"),
    ("20 oz", "20 oz bottle"),
    ("20 oz", "20 ounce"),
    ("20 fl oz", "20oz"),
])
def test_the_same_size_written_differently_is_a_duplicate(db, fake, first, again):  # noqa: F811
    assert ask(size=first)["success"]
    assert codes(ask(size=again)) == ["already_requested"]
    assert len(fake.posts) == 1


@pytest.mark.parametrize("name, size", [("Coca-Cola", "20 ounce"), ("Diet Coke", "12 oz"), ("Coca-Cola", "20 oz can")])
def test_the_catalog_check_ignores_how_the_size_is_written(db, fake, name, size):  # noqa: F811
    assert codes(ask(name=name, size=size)) == ["already_in_catalog"]
    assert fake.posts == []


@pytest.mark.parametrize("name", ["Brut Champagne", "Prosecco", "Red Sangria", "Lime Margarita", "Mojito",
                                  "Peach Bellini", "Pinot Grigio"])
def test_wine_and_cocktails_are_refused(db, fake, name):  # noqa: F811
    assert codes(ask(name=name)) == ["alcohol_or_thc"]


@pytest.mark.parametrize("name", ["Virgin Mojito Sparkling Water", "Ocean Spray Cranberry Juice Cocktail",
                                  "Sangria Senorial", "Sangría Señorial", "Kola Champagne"])
def test_soft_drinks_named_like_alcohol_are_allowed(db, fake, name):  # noqa: F811
    assert ask(name=name)["success"]


def test_a_more_general_request_is_not_the_catalogs_flavor(db, fake, monkeypatch):  # noqa: F811
    # The fake bridge serves the module's CATALOG, read at request time.
    monkeypatch.setitem(globals(), "CATALOG", [{"sku": "SPR-CH", "name": "Sprite Cherry", "flavor": None,
                                                "size": "20 oz bottle"}])
    assert ask(name="Sprite")["success"]


def test_a_more_general_request_is_not_an_earlier_flavor(db, fake):  # noqa: F811
    assert ask(name="Sprite Cherry")["success"]
    fake.answer = (201, {"id": "req-2", "status": "pending"}, 0.0)
    assert ask(name="Sprite")["success"]
    assert len(fake.posts) == 2


def test_an_unreadable_catalog_sends_nothing(db, fake):  # noqa: F811
    fake.catalog_status = 500
    reply = ask()
    assert not reply["success"] and "Couldn't read the catalog" in reply["message"]
    assert fake.posts == []


def test_halted_never_calls_the_bridge(db, fake):  # noqa: F811
    set_halted(db, True, "test")
    assert codes(ask()) == ["halted"]
    assert fake.gets == 0 and fake.posts == []


def test_the_weekly_limit_is_higher_while_the_catalog_is_small(db, fake, monkeypatch):  # noqa: F811
    monkeypatch.setattr(catalog_request, "STARTUP_WEEKLY_LIMIT", 3)
    monkeypatch.setattr(catalog_request, "WEEKLY_LIMIT", 1)
    for i, drink in enumerate(["Sprite", "Fanta", "Pibb"]):
        fake.answer = (201, {"id": f"req-{i}", "status": "pending"}, 0.0)
        assert ask(name=drink)["success"]
    reply = ask(name="Fresca")
    assert codes(reply) == ["request_limit"] and "start-up limit" in reply["refusals"][0]["message"]
    assert len(fake.posts) == 3


def test_the_normal_limit_applies_once_the_catalog_is_full(db, fake, monkeypatch):  # noqa: F811
    monkeypatch.setattr(catalog_request, "SMALL_CATALOG", len(CATALOG))
    monkeypatch.setattr(catalog_request, "WEEKLY_LIMIT", 1)
    assert ask(name="Sprite")["success"]
    reply = ask(name="Fanta")
    assert codes(reply) == ["request_limit"] and "start-up" not in reply["refusals"][0]["message"]


def test_requests_older_than_a_week_do_not_count(db, fake, monkeypatch):  # noqa: F811
    monkeypatch.setattr(catalog_request, "SMALL_CATALOG", len(CATALOG))
    monkeypatch.setattr(catalog_request, "WEEKLY_LIMIT", 1)
    assert ask(name="Sprite")["success"]
    db.execute("update product_requests set requested_at = now() - interval '8 days'")
    fake.answer = (201, {"id": "req-2", "status": "pending"}, 0.0)
    assert ask(name="Fanta")["success"]
