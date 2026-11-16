"""The catalog-request backlog: queueing, ordering, and filing through the agent's write gate
against the fake bridge from test_catalog_request."""

import json
from pathlib import Path

import pytest

from tests.conftest import open_in
from tests.test_catalog_request import fake  # noqa: F401  (fixture)
from tests.test_server import db  # noqa: F401  (fixture)
from vending import request_backlog as rb
from vending.bridge import Bridge
from vending.config import set_halted

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "db" / "product_requests" / "backlog-2026-11-15.json"


def item(name, size="16 oz bottle", **kw):
    return rb.Item(name=name, size=size, **kw)


def names(conn):
    return [r["name"] for r in rb.queued(conn)]


@pytest.fixture
def numbered(fake):  # noqa: F811
    """The fake bridge answers each POST with a new request ID, as the real one does."""
    count = iter(range(1, 1000))
    fake.on_post = lambda: setattr(fake, "answer", (201, {"id": f"np_{next(count)}", "status": "pending"}, 0.0))
    return fake


def filer(fake, schema_url):  # noqa: F811
    return dict(bridge=lambda: Bridge(fake.url, "test-key"), connect=lambda: open_in(*schema_url))


def test_the_seed_file_imports_in_order_with_its_reasons(db):  # noqa: F811
    ids = rb.import_file(db, SEED, "kevin")
    rows = rb.queued(db)
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    assert len(ids) == len(seed) == 14
    assert [r["name"] for r in rows] == [d["name"] for d in seed]
    assert [r["position"] for r in rows] == list(range(1, 15))
    assert all(r["why"] for r in rows) and rows[0]["est_gain_cents"] == 4969


def test_an_import_with_one_bad_item_queues_nothing(db, tmp_path):  # noqa: F811
    path = tmp_path / "bad.json"
    path.write_text(json.dumps([{"name": "Snapple Apple", "size": "16 oz bottle"},
                                {"name": "Mystery", "size": "a big one"}]), encoding="utf-8")
    with pytest.raises(rb.BacklogError, match="item 2"):
        rb.import_file(db, path, "kevin")
    assert rb.queued(db) == []


def test_a_drink_already_queued_or_requested_is_refused(db):  # noqa: F811
    rb.add(db, item("Snapple Apple", flavor="Apple"), "kevin")
    with pytest.raises(rb.BacklogError, match="already_queued"):
        rb.add(db, item("Snapple Apple", flavor="Apple"), "meaghan")
    db.execute("insert into product_requests (name, size, words, bridge_id, status) values"
               " ('Powerade', '20 oz bottle', %s, 'np_x', 'pending')", (["powerade", "20", "oz", "bottle"],))
    with pytest.raises(rb.BacklogError, match="already_requested"):
        rb.add(db, item("Powerade", "20 oz bottle"), "kevin")


def test_move_and_drop_keep_the_queue_numbered(db):  # noqa: F811
    a, b, c = (rb.add(db, item(n), "kevin") for n in ("Alpha Tea", "Beta Juice", "Gamma Water"))
    rb.move(db, c, 1, "kevin")
    assert names(db) == ["Gamma Water", "Alpha Tea", "Beta Juice"]
    rb.drop(db, a, "the team decided against it", "kevin")
    assert [(r["name"], r["position"]) for r in rb.queued(db)] == [("Gamma Water", 1), ("Beta Juice", 2)]
    dropped = rb.summary(db)["recent"][0]
    assert dropped["status"] == "dropped" and "decided against it (kevin)" in dropped["drop_reason"]
    with pytest.raises(rb.BacklogError, match="not queued"):
        rb.move(db, a, 1, "kevin")
    with pytest.raises(rb.BacklogError, match="reason"):
        rb.drop(db, b, " ", "kevin")


def test_file_sends_from_the_top_and_links_each_request(db, numbered, schema_url):  # noqa: F811
    for n in ("Alpha Tea", "Beta Juice", "Gamma Water"):
        rb.add(db, item(n), "kevin")
    out = rb.file_next(db, "kevin", limit=2, **filer(numbered, schema_url))
    assert [(o.name, o.result) for o in out] == [("Alpha Tea", "filed"), ("Beta Juice", "filed")]
    assert [p["name"] for p in numbered.posts] == ["Alpha Tea", "Beta Juice"]
    assert names(db) == ["Gamma Water"]
    filed = {r["name"]: r for r in rb.summary(db)["recent"]}
    assert filed["Alpha Tea"]["bridge_id"] == "np_1" and filed["Alpha Tea"]["request_status"] == "pending"
    assert rb.summary(db)["open_requests"] == 2


def test_file_stops_when_the_bridge_is_full_and_keeps_the_rest(db, fake, schema_url):  # noqa: F811
    for n in ("Alpha Tea", "Beta Juice"):
        rb.add(db, item(n), "kevin")
    fake.answer = (429, {"detail": "too many open requests"}, 0.0)
    out = rb.file_next(db, "kevin", **filer(fake, schema_url))
    assert [(o.name, o.result) for o in out] == [("Alpha Tea", "stopped")]
    assert names(db) == ["Alpha Tea", "Beta Juice"]
    assert db.execute("select count(*) from product_requests").fetchone()[0] == 0


def test_a_drink_already_in_the_catalog_is_dropped_and_filing_goes_on(db, numbered, schema_url):  # noqa: F811
    rb.add(db, item("Coca-Cola", "20 oz bottle", flavor="Original"), "kevin")   # the fake catalog lists it
    rb.add(db, item("Alpha Tea"), "kevin")
    out = rb.file_next(db, "kevin", **filer(numbered, schema_url))
    assert [(o.name, o.result) for o in out] == [("Coca-Cola", "dropped"), ("Alpha Tea", "filed")]
    assert rb.summary(db)["recent"][-1]["drop_reason"] == "already in the catalog"


def test_nothing_is_sent_while_halted(db, fake, schema_url):  # noqa: F811
    rb.add(db, item("Alpha Tea"), "kevin")
    set_halted(db, True, "test")
    out = rb.file_next(db, "kevin", **filer(fake, schema_url))
    assert [o.result for o in out] == ["stopped"] and fake.posts == []
    assert names(db) == ["Alpha Tea"]


def test_every_change_is_logged_with_a_name(db):  # noqa: F811
    row = rb.add(db, item("Alpha Tea"), "kevin")
    rb.move(db, row, 1, "kevin")
    tools = [t for (t,) in db.execute("select tool from action_log where tool like 'request_backlog.%' order by id")]
    assert tools == ["request_backlog.add", "request_backlog.move"]
    with pytest.raises(rb.BacklogError, match="--by"):
        rb.add(db, item("Beta Juice"), " ")
