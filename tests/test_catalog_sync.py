"""The catalog tables (migration 006) and the sync that fills them from
`GET /api/v1/products`. Synthetic answers only, shaped like the live catalog."""

from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from psycopg.types.json import Jsonb

from vending.bridge import BridgeUnreachable
from vending.sync_catalog import EmptyCatalog, sync


COKE = {"sku": "COCA-20BOT", "name": "Coca-Cola 20 oz bottle", "line": "Coca-Cola", "flavor": None,
        "size": "20 oz bottle"}
HINT = {"sku": "HINT-GRAPE-16BOT", "name": "Hint Grape 16 oz bottle", "line": "Hint", "flavor": "Grape",
        "size": "16 oz bottle"}
NOW = datetime(2026, 11, 5, 9, 10, tzinfo=timezone.utc)
LATER = NOW + timedelta(days=1)


class FakeBridge:
    """Answers `products()` with each answer in turn. An answer that is an exception is raised."""

    def __init__(self, *answers):
        self.answers = list(answers)

    def products(self):
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def quiet(_):
    pass


def catalog(conn):
    return conn.execute("select sku, name, flavor, delisted_at from catalog_products order by sku").fetchall()


def syncs(conn):
    return conn.execute("select product_count, added, delisted from catalog_syncs order by id").fetchall()


def add(conn, product: dict) -> None:
    conn.execute(
        "insert into catalog_products (sku, name, line, flavor, size, last_response)"
        " values (%(sku)s, %(name)s, %(line)s, %(flavor)s, %(size)s, %(raw)s)",
        {**product, "raw": Jsonb(product)})


def test_a_product_without_a_flavor_is_stored_as_listed(conn):
    add(conn, COKE)
    row = conn.execute("select sku, flavor, delisted_at, first_seen_at = last_seen_at from catalog_products").fetchone()
    assert row == ("COCA-20BOT", None, None, True)


def test_the_bridge_answer_is_kept_as_it_came(conn):
    add(conn, COKE)
    assert conn.execute("select last_response from catalog_products").fetchone() == (COKE,)


def test_a_sku_is_stored_once(conn):
    add(conn, COKE)
    with pytest.raises(psycopg.errors.UniqueViolation):
        add(conn, COKE)


@pytest.mark.parametrize("field", ["sku", "name"])
def test_a_blank_sku_or_name_is_refused(conn, field):
    with pytest.raises(psycopg.errors.CheckViolation):
        add(conn, {**COKE, field: ""})


def test_a_sync_cannot_record_a_negative_count(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("insert into catalog_syncs (product_count) values (-1)")


def test_the_first_sync_adds_every_product_and_records_the_run(conn):
    report = sync(conn, FakeBridge([COKE, HINT]), NOW, log=quiet)
    assert (report.listed, report.added) == (2, 2)
    assert catalog(conn) == [("COCA-20BOT", "Coca-Cola 20 oz bottle", None, None),
                             ("HINT-GRAPE-16BOT", "Hint Grape 16 oz bottle", "Grape", None)]
    assert syncs(conn) == [(2, 2, 0)]


def test_a_changed_product_is_updated_and_keeps_when_we_first_saw_it(conn):
    renamed = {**COKE, "name": "Coca-Cola Classic 20 oz bottle"}
    sync(conn, FakeBridge([COKE]), NOW, log=quiet)
    report = sync(conn, FakeBridge([renamed]), LATER, log=quiet)
    assert (report.added, report.updated) == (0, 1)
    assert conn.execute("select name, first_seen_at, last_seen_at, last_response from catalog_products").fetchone() == (
        "Coca-Cola Classic 20 oz bottle", NOW, LATER, renamed)


def test_an_unchanged_product_is_not_counted_as_changed(conn):
    sync(conn, FakeBridge([COKE]), NOW, log=quiet)
    report = sync(conn, FakeBridge([COKE]), LATER, log=quiet)
    assert (report.listed, report.added, report.updated) == (1, 0, 0)


def test_a_product_dropped_by_the_bridge_is_delisted_not_deleted(conn):
    sync(conn, FakeBridge([COKE, HINT]), NOW, log=quiet)
    report = sync(conn, FakeBridge([COKE]), LATER, log=quiet)
    assert report.delisted == 1
    assert catalog(conn)[1] == ("HINT-GRAPE-16BOT", "Hint Grape 16 oz bottle", "Grape", LATER)
    assert syncs(conn)[-1] == (1, 0, 1)


def test_a_delisted_product_listed_again_is_relisted(conn):
    sync(conn, FakeBridge([COKE, HINT]), NOW, log=quiet)
    sync(conn, FakeBridge([COKE]), LATER, log=quiet)
    report = sync(conn, FakeBridge([COKE, HINT]), LATER + timedelta(days=1), log=quiet)
    assert (report.relisted, report.added) == (1, 0)
    assert catalog(conn)[1][3] is None


def test_a_delisted_product_is_not_delisted_again(conn):
    sync(conn, FakeBridge([COKE, HINT]), NOW, log=quiet)
    sync(conn, FakeBridge([COKE]), LATER, log=quiet)
    report = sync(conn, FakeBridge([COKE]), LATER + timedelta(days=1), log=quiet)
    assert report.delisted == 0
    assert catalog(conn)[1][3] == LATER


@pytest.mark.parametrize("bad", [{"name": "No SKU"}, {**HINT, "name": "  "}, {**HINT, "sku": 42}, "COCA-20BOT", None])
def test_an_unusable_product_is_skipped_and_the_rest_are_stored(conn, bad):
    logged = []
    report = sync(conn, FakeBridge([COKE, bad]), NOW, log=logged.append)
    assert (report.listed, report.invalid) == (1, 1)
    assert [row[0] for row in catalog(conn)] == ["COCA-20BOT"]
    assert "skipped" in logged[0]


def test_a_sku_listed_twice_keeps_the_first(conn):
    report = sync(conn, FakeBridge([COKE, {**COKE, "name": "Second copy"}]), NOW, log=quiet)
    assert (report.listed, report.invalid) == (1, 1)
    assert catalog(conn)[0][1] == "Coca-Cola 20 oz bottle"


def test_an_empty_answer_never_delists_the_whole_catalog(conn):
    sync(conn, FakeBridge([COKE, HINT]), NOW, log=quiet)
    with pytest.raises(EmptyCatalog):
        sync(conn, FakeBridge([]), LATER, log=quiet)
    assert [row[3] for row in catalog(conn)] == [None, None]
    assert len(syncs(conn)) == 1


def test_an_empty_answer_is_fine_when_we_hold_nothing_listed(conn):
    report = sync(conn, FakeBridge([]), NOW, log=quiet)
    assert report.listed == 0
    assert syncs(conn) == [(0, 0, 0)]


def test_an_unreachable_bridge_stops_the_sync_and_writes_nothing(conn):
    with pytest.raises(BridgeUnreachable):
        sync(conn, FakeBridge(BridgeUnreachable("Is Tailscale connected?")), NOW, log=quiet)
    assert catalog(conn) == [] and syncs(conn) == []
