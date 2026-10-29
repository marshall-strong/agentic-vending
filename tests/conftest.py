"""Shared fixtures. Database tests each run in a throwaway schema in the local
database, and are skipped when DATABASE_URL isn't set or the database is down."""

import uuid

import psycopg
import pytest

from tests.test_evadts import build
from vending import db, settings
from vending.sync_history import store


@pytest.fixture
def schema_url():
    """(url, schema) for a fresh, migrated schema, dropped after the test."""
    url = settings.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set")
    try:
        admin = psycopg.connect(url, autocommit=True)
    except psycopg.OperationalError:
        pytest.skip("database not reachable (docker compose up -d db)")
    schema = f"test_{uuid.uuid4().hex[:8]}"
    admin.execute(f"create schema {schema}")
    with open_in(url, schema) as conn:
        db.migrate(conn)
    yield url, schema
    admin.execute(f"drop schema {schema} cascade")
    admin.close()


def open_in(url: str, schema: str) -> psycopg.Connection:
    return psycopg.connect(url, autocommit=True, options=f"-c search_path={schema}")


@pytest.fixture
def conn(schema_url):
    with open_in(*schema_url) as connection:
        yield connection


def store_poll(conn, session_id: str, started_at: str, selections: dict[str, tuple[int | None, int]],
               device_id: str = "vmc1") -> None:
    """Store a synthetic poll through the history sync's own `store`.
    `selections` maps slot -> (price_cents or None, lifetime_units)."""
    records = ["ID1*TEST-MACHINE*0000**EVS3.0*", "ID4*2*1840*5", "VA1*0*0*0*0"]
    for slot, (price, units) in selections.items():
        records += [f"PA1*{slot}*{'' if price is None else price}*", f"PA2*{units}*{units * (price or 0)}*0*0*0*0"]
    meta = {"session_id": session_id, "device_id": device_id, "started_at": started_at}
    assert store(conn, meta, build(records)) == "ok"
