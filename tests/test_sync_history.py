"""Tests for storing polls. Each test runs in a throwaway schema in the local
database, and is skipped when psycopg isn't installed, DATABASE_URL isn't set
or the database is down."""

import threading
import time
import uuid
from datetime import date

import pytest

psycopg = pytest.importorskip("psycopg")

# These import psycopg too, so they come after the skip.
from tests.test_bridge import DROP, HEALTHY, HISTORY, Answer, listing, serve  # noqa: E402
from tests.test_evadts import GARBLED, SAMPLE, build  # noqa: E402
from tests.conftest import CONNECT_TIMEOUT  # noqa: E402
from vending import db  # noqa: E402
from vending.bridge import Bridge, BridgeError, BridgeUnreachable  # noqa: E402
from vending.sync_history import HISTORY_START, default_since, main, store, sync  # noqa: E402


@pytest.fixture
def conn(database_url):
    connection = psycopg.connect(database_url, autocommit=True, connect_timeout=CONNECT_TIMEOUT)
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


def poll_routes(session_ids, **answers):
    """Routes for a one-day listing of `session_ids`, each serving a good poll
    unless `answers` says otherwise."""
    polls = [{"session_id": s, "started_at": f"2026-10-01T{hour:02}:00:00+00:00"} for hour, s in enumerate(session_ids)]
    raws = {f"{HISTORY}/{s}/raw": answers.get(s, Answer(body=build(SAMPLE))) for s in session_ids}
    return {HISTORY: listing(*polls), **HEALTHY, **raws}


@pytest.fixture
def no_waits(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _: None)


@pytest.fixture
def run_main(conn, database_url, no_waits, monkeypatch):
    """`run_main(routes, *args)` is the real `main` in this test's schema, against a
    real client and a fake bridge serving `routes`. Returns its exit code."""
    schema = conn.execute("select current_schema()").fetchone()[0]
    monkeypatch.setattr(db, "connect", lambda: psycopg.connect(
        database_url, autocommit=True, connect_timeout=CONNECT_TIMEOUT, options=f"-c search_path={schema}"))

    def run(routes, *args):
        with serve(routes) as server:
            monkeypatch.setenv("DEX_BASE_URL", server.url)
            monkeypatch.setenv("DEX_API_KEY", "made-up-key")
            return main([*args, "--until", "2026-10-01"])

    return run


def test_a_poll_whose_connection_always_drops_does_not_stall_every_run(conn, no_waits):
    day = date(2026, 10, 1)
    with serve(poll_routes(["before", "drops", "after"], drops=DROP)) as server:
        bridge = Bridge(base_url=server.url, api_key="made-up-key")
        first = sync(conn, bridge, day, day, log=lambda _: None)
        second = sync(conn, bridge, day, day, log=lambda _: None)
    assert (first.new, first.failed_polls) == (2, 1)
    assert (second.new, second.failed_polls) == (0, 1)
    assert {r[0] for r in conn.execute("select session_id from polls")} == {"before", "after"}


def test_a_run_with_a_poll_that_always_drops_finishes_and_exits_1(conn, run_main):
    assert run_main(poll_routes(["drops", "after"], drops=DROP), "--since", "2026-10-01") == 1
    assert [r[0] for r in conn.execute("select session_id from polls")] == ["after"]


def test_a_rejected_key_on_a_poll_still_stops_the_run(conn, run_main):
    # `/health` needs no key, so it can't vouch for a rejected one.
    assert run_main(poll_routes(["rejected", "after"], rejected=Answer(status=401)), "--since", "2026-10-01") == 3
    assert conn.execute("select count(*) from polls").fetchone()[0] == 0


def test_an_outage_on_a_poll_still_stops_the_run(conn, run_main):
    assert run_main({**poll_routes(["drops", "after"], drops=DROP), "/health": DROP}, "--since", "2026-10-01") == 3
    assert conn.execute("select count(*) from polls").fetchone()[0] == 0


def test_main_refuses_a_schema_missing_migrations(database_url, monkeypatch, capsys):
    schema = f"test_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(database_url, autocommit=True, connect_timeout=CONNECT_TIMEOUT)
    admin.execute(f"create schema {schema}")
    try:
        monkeypatch.setattr(db, "connect", lambda: psycopg.connect(
            database_url, autocommit=True, connect_timeout=CONNECT_TIMEOUT, options=f"-c search_path={schema}"))
        monkeypatch.setattr("vending.sync_history.Bridge", lambda: pytest.fail("reached the bridge on an unmigrated schema"))
        assert main(["--since", "2026-10-01", "--until", "2026-10-01"]) == 2
        message = capsys.readouterr().err
        assert "001_polls.sql" in message and "python -m vending.db migrate" in message
        # It only looked: nothing was created.
        with admin.cursor() as cur:
            cur.execute(f"set search_path to {schema}")
            assert cur.execute("select to_regclass('schema_migrations')").fetchone() == (None,)
    finally:
        admin.execute(f"drop schema {schema} cascade")
        admin.close()


def test_garbled_poll_is_stored_not_raised(conn):
    raw = build(GARBLED)
    assert store(conn, META, raw) == "bad"
    assert conn.execute("select crc_ok from polls").fetchall() == [(None,)]


def test_not_evadts_is_stored_with_no_checksum_result(conn):
    assert store(conn, META, b"<html>502 Bad Gateway</html>") == "bad"
    assert conn.execute("select crc_ok, raw from polls").fetchone() == (None, b"<html>502 Bad Gateway</html>")


def test_an_unparseable_poll_is_fetched_again_only_on_a_retry_pass(conn):
    day = date(2026, 10, 1)
    meta = poll_meta("cut", "2026-10-01T12:00:00+00:00")
    assert store(conn, meta, b"DXS*cut off") == "bad"
    bridge = FakeBridge(days={"2026-10-01": [meta]}, raws={"cut": build(SAMPLE)})

    plain = sync(conn, bridge, day, day, log=lambda _: None)
    assert (plain.new, plain.unparseable) == (0, 0)
    assert conn.execute("select crc_ok from polls").fetchall() == [(None,)]

    retry = sync(conn, bridge, day, day, log=lambda _: None, retry_bad=True)
    assert (retry.new, retry.unparseable) == (1, 0)
    assert conn.execute("select crc_ok, total_paid_units from polls").fetchall() == [(True, 42)]
    assert conn.execute("select count(*) from poll_selections").fetchone()[0] == 2


def test_a_retry_pass_leaves_a_still_bad_poll_and_a_checksum_failure_alone(conn):
    day = date(2026, 10, 1)
    bad = poll_meta("bad", "2026-10-01T12:00:00+00:00")
    crc = poll_meta("crc", "2026-10-01T13:00:00+00:00")
    assert store(conn, bad, build(GARBLED)) == "bad"
    assert store(conn, crc, build(SAMPLE, corrupt=True)) == "crc"
    bridge = FakeBridge(days={"2026-10-01": [bad, crc]},
                        raws={"bad": b"<html>still broken</html>", "crc": BridgeError("must not be fetched")})
    report = sync(conn, bridge, day, day, log=lambda _: None, retry_bad=True)
    assert (report.new, report.unparseable, report.failed_polls) == (0, 1, 0)
    assert conn.execute("select session_id, crc_ok from polls order by session_id").fetchall() == [
        ("bad", None), ("crc", False)]


def test_store_never_replaces_a_readable_poll(conn):
    assert store(conn, META, build(SAMPLE)) == "ok"
    assert store(conn, META, build(GARBLED)) == "bad"
    assert store(conn, META, build(SAMPLE, corrupt=True)) == "crc"
    assert conn.execute("select crc_ok, total_paid_units from polls").fetchall() == [(True, 42)]


def test_two_runs_migrating_at_once_do_not_collide(database_url):
    schema = f"test_{uuid.uuid4().hex[:8]}"
    connections = [psycopg.connect(database_url, autocommit=True, connect_timeout=CONNECT_TIMEOUT)
                   for _ in range(2)]
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
