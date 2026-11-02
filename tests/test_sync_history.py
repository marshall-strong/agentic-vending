"""Tests for storing polls. Each test runs in a throwaway schema in the local
database, and is skipped when psycopg isn't installed, DATABASE_URL isn't set
or the database is down."""

import threading
import uuid
from datetime import date

import pytest

psycopg = pytest.importorskip("psycopg")

# These import psycopg too, so they come after the skip.
from tests.test_evadts import GARBLED, SAMPLE, build  # noqa: E402
from vending import db, settings  # noqa: E402
from vending.bridge import BridgeError, BridgeUnreachable  # noqa: E402
from vending.sync_history import HISTORY_START, default_since, store, sync  # noqa: E402


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


class FakeBridge:
    """Serves stored history from a dict. A value that is an exception is raised."""

    def __init__(self, days, raws):
        self.days, self.raws = days, raws

    def history(self, day):
        result = self.days.get(day, [])
        if isinstance(result, Exception):
            raise result
        return [dict(m, device_id="vmc1") for m in result]

    def raw(self, session_id):
        result = self.raws[session_id]
        if isinstance(result, Exception):
            raise result
        return result


def poll_meta(session_id, started_at):
    return {"session_id": session_id, "started_at": started_at}


def test_one_bad_day_or_poll_does_not_stop_the_sync(conn):
    bridge = FakeBridge(
        days={
            "2026-10-01": [poll_meta("a", "2026-10-01T12:00:00+00:00"), poll_meta("gone", "2026-10-01T13:00:00+00:00")],
            "2026-10-02": BridgeError("history for 2026-10-02 was truncated at 500 polls"),
            "2026-10-03": [poll_meta("b", "2026-10-03T12:00:00+00:00")],
        },
        raws={"a": build(SAMPLE), "gone": BridgeError("404 from /raw"), "b": build(SAMPLE)},
    )
    report = sync(conn, bridge, date(2026, 10, 1), date(2026, 10, 3), log=lambda _: None)
    assert (report.days, report.new, report.failed_days, report.failed_polls) == (3, 2, 1, 1)
    assert {r[0] for r in conn.execute("select session_id from polls")} == {"a", "b"}


def test_unreachable_bridge_stops_the_sync(conn):
    bridge = FakeBridge(days={"2026-10-01": BridgeUnreachable("Is Tailscale connected?")}, raws={})
    with pytest.raises(BridgeUnreachable):
        sync(conn, bridge, date(2026, 10, 1), date(2026, 10, 2), log=lambda _: None)


def test_garbled_poll_is_stored_not_raised(conn):
    raw = build(GARBLED)
    assert store(conn, META, raw) == "bad"
    assert conn.execute("select crc_ok from polls").fetchall() == [(None,)]


def test_not_evadts_is_stored_with_no_checksum_result(conn):
    assert store(conn, META, b"<html>502 Bad Gateway</html>") == "bad"
    assert conn.execute("select crc_ok, raw from polls").fetchone() == (None, b"<html>502 Bad Gateway</html>")


def test_two_runs_migrating_at_once_do_not_collide():
    url = settings.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set")
    schema = f"test_{uuid.uuid4().hex[:8]}"
    connections = [psycopg.connect(url, autocommit=True) for _ in range(2)]
    connections[0].execute(f"create schema {schema}")
    for connection in connections:
        connection.execute(f"set search_path to {schema}")
    start = threading.Barrier(2)
    results, errors = [], []

    def run(connection):
        start.wait()
        try:
            results.append(db.migrate(connection))
        except Exception as error:  # noqa: BLE001 - the test reports any failure
            errors.append(error)

    threads = [threading.Thread(target=run, args=(c,)) for c in connections]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        # Each migration ran exactly once, in one of the two runs.
        assert sorted(sum(results, [])) == sorted(p.name for p in db.MIGRATIONS.glob("*.sql"))
    finally:
        connections[0].execute(f"drop schema {schema} cascade")
        for connection in connections:
            connection.close()


def test_default_since_rechecks_the_day_before(conn):
    assert default_since(conn) == HISTORY_START
    store(conn, {**META, "started_at": "2026-10-02T00:05:00+00:00"}, build(SAMPLE))
    assert default_since(conn) == date(2026, 10, 1)


def test_migrate_twice_is_a_no_op(conn):
    assert db.migrate(conn) == []
