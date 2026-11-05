"""The catalog tables (migration 006). The sync that fills them comes next; these
tests pin down what the tables accept, so the sync and `get_catalog` can rely on it."""

import psycopg
import pytest
from psycopg.types.json import Jsonb


COKE = {"sku": "COCA-20BOT", "name": "Coca-Cola 20 oz bottle", "line": "Coca-Cola", "flavor": None,
        "size": "20 oz bottle"}


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
