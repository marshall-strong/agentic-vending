"""Tests for `get_catalog`, on a hand-filled catalog."""

from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from vending.tools.catalog import STALE_AFTER, get_catalog

NOW = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)


def product(conn, sku, name, line=None, flavor=None, size=None, delisted=False):
    conn.execute(
        "insert into catalog_products (sku, name, line, flavor, size, delisted_at, last_response)"
        " values (%s, %s, %s, %s, %s, %s, %s)",
        (sku, name, line, flavor, size, NOW if delisted else None, Jsonb({"sku": sku})))


def synced(conn, at=NOW - timedelta(hours=2)):
    conn.execute("insert into catalog_syncs (synced_at, product_count) values (%s, 0)", (at,))


def fill(conn, synced_at=NOW - timedelta(hours=2)):
    product(conn, "CC20", "Coca-Cola", "Coca-Cola", None, "20 oz")
    product(conn, "CCC20", "Coca-Cola Cherry", "Coca-Cola", "Cherry", "20 oz")
    product(conn, "DW20", "Dasani Water", "Dasani", None, "20 oz")
    product(conn, "OLD1", "Retired Orange Soda", "Fanta", "Orange", "12 oz", delisted=True)
    synced(conn, synced_at)


def skus(reply):
    return [p.sku for p in reply.data]


def test_lists_every_listed_product_and_hides_delisted(conn):
    fill(conn)
    reply = get_catalog(conn, None, now=NOW)
    assert reply.success and reply.count == 3
    assert skus(reply) == ["CC20", "CCC20", "DW20"]  # by name; the delisted one is gone
    first = reply.data[0]
    assert (first.name, first.line, first.flavor, first.size) == ("Coca-Cola", "Coca-Cola", None, "20 oz")


def test_says_when_the_catalog_was_last_synced(conn):
    fill(conn)
    reply = get_catalog(conn, None, now=NOW)
    assert reply.synced_at == NOW - timedelta(hours=2)
    assert "2026-11-08 10:00 UTC" in reply.message
    assert "stale" not in reply.message.lower()


def test_a_stale_catalog_is_called_out(conn):
    fill(conn, synced_at=NOW - STALE_AFTER - timedelta(hours=1))
    reply = get_catalog(conn, None, now=NOW)
    assert reply.success and reply.count == 3
    assert "stale" in reply.message.lower()
    assert "python -m vending.sync_catalog" in reply.message


def test_never_synced_is_a_failure_that_says_how_to_fix_it(conn):
    product(conn, "CC20", "Coca-Cola")  # rows without a recorded sync can't be called current
    reply = get_catalog(conn, None, now=NOW)
    assert not reply.success and reply.count == 0
    assert "python -m vending.sync_catalog" in reply.message


def test_search_matches_name_line_flavor_and_size_ignoring_case(conn):
    fill(conn)
    assert skus(get_catalog(conn, "dasani", now=NOW)) == ["DW20"]       # name and line
    assert skus(get_catalog(conn, "CHERRY", now=NOW)) == ["CCC20"]      # flavor
    assert skus(get_catalog(conn, "20 oz", now=NOW)) == ["CC20", "CCC20", "DW20"]  # size
    assert skus(get_catalog(conn, "  coca-cola  ", now=NOW)) == ["CC20", "CCC20"]  # trimmed


def test_every_word_of_a_search_must_match(conn):
    fill(conn)
    assert skus(get_catalog(conn, "coca cherry", now=NOW)) == ["CCC20"]
    assert skus(get_catalog(conn, "cherry water", now=NOW)) == []


def test_search_characters_are_plain_text_not_patterns(conn):
    fill(conn)
    assert skus(get_catalog(conn, "%", now=NOW)) == []
    assert skus(get_catalog(conn, "_", now=NOW)) == []


def test_a_blank_search_lists_everything(conn):
    fill(conn)
    assert get_catalog(conn, "   ", now=NOW).count == 3


def test_a_search_with_no_match_is_an_answer_not_a_failure(conn):
    fill(conn)
    reply = get_catalog(conn, "kombucha", now=NOW)
    assert reply.success and reply.count == 0 and reply.data == []
    assert "request_catalog_product" in reply.message


def test_a_delisted_product_is_not_found_by_search(conn):
    fill(conn)
    assert get_catalog(conn, "orange", now=NOW).count == 0
