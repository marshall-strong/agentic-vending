"""Tests for storing polls. Each test runs in a throwaway schema in the local
database, and is skipped when DATABASE_URL isn't set or the database is down."""

import uuid

import psycopg
import pytest

from tests.test_evadts import SAMPLE, build
from vending import db, settings
from vending.sync_history import store


@pytest.fixture
def conn():
    url = settings.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set")
    try:
        connection = psycopg.connect(url, autocommit=True)
    except psycopg.OperationalError:
        pytest.skip("database not reachable (docker compose up -d db)")
    schema = f"test_{uuid.uuid4().hex[:8]}"
    connection.execute(f"create schema {schema}")
    connection.execute(f"set search_path to {schema}")
    db.migrate(connection)
    yield connection
    connection.execute(f"drop schema {schema} cascade")
    connection.close()


META = {"session_id": "s1", "device_id": "vmc1", "started_at": "2026-10-01T12:00:00+00:00"}


def test_store_is_idempotent(conn):
    raw = build(SAMPLE)
    assert store(conn, META, raw) == "ok"
    assert store(conn, META, raw) == "ok"
    assert conn.execute("select count(*) from polls").fetchone()[0] == 1
    rows = conn.execute(
        "select selection, price_cents, lifetime_units, lifetime_sales_cents"
        " from poll_selections order by selection").fetchall()
    assert rows == [("15", 450, 30, 13500), ("16", None, 12, 0)]


def test_checksum_failure_keeps_raw_but_no_counters(conn):
    raw = build(SAMPLE, corrupt=True)
    assert store(conn, META, raw) == "crc"
    crc_ok, stored, total = conn.execute("select crc_ok, raw, total_paid_cents from polls").fetchone()
    assert crc_ok is False and bytes(stored) == raw and total is None
    assert conn.execute("select count(*) from poll_selections").fetchone()[0] == 0


def test_migrate_twice_is_a_no_op(conn):
    assert db.migrate(conn) == []
