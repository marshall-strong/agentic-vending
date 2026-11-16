"""Database-enforced checks for counter reads after a server reassignment."""

from datetime import datetime, timezone

import pytest

from tests.conftest import TEST_SLOTS, store_poll
from tests.test_analysis_slots import _run_export
from tests.test_dashboard import fulfilled
from vending import slot_counts, slots, stock


@pytest.fixture
def guarded(conn):
    """A real database view refuses forbidden counter values before Python sees them."""
    _polls(conn)
    conn.execute("""
        create function checked_units(sid text, sel text, units integer) returns integer
        language plpgsql volatile as $$
        begin
            if not (sel = any((select s.slots from slot_assignment s where id = 1)::text[]))
               and (select started_at from polls where session_id = sid) >= '2026-12-03T05:00:00Z' then
                raise exception 'forbidden counter read: %', sel;
            end if;
            return units;
        end $$
    """)
    columns = [r[0] for r in conn.execute("select * from poll_selections limit 0").description]
    conn.execute("alter table poll_selections rename to source_selections")
    from psycopg import sql
    projection = [sql.SQL("checked_units(session_id, selection, lifetime_units) as lifetime_units")
                  if c == "lifetime_units" else sql.Identifier(c) for c in columns]
    conn.execute(sql.SQL("create view poll_selections as select {} from source_selections")
                 .format(sql.SQL(", ").join(projection)))
    return conn


def _polls(conn):
    store_poll(conn, "before", "2026-11-20T12:00:00Z", {"15": (250, 100), "11": (250, 200)})
    store_poll(conn, "after", "2026-12-04T12:00:00Z", {"15": (250, 102), "11": (250, 205)})


def test_boundary_instrument_allows_ours_and_fall_but_rejects_later_others(conn, guarded):
    assert conn.execute("select lifetime_units from poll_selections where selection = '15'").fetchall()
    assert conn.execute("select lifetime_units from poll_selections where session_id = 'before'").fetchall()
    with pytest.raises(Exception, match="forbidden counter read: 11"):
        conn.execute("select lifetime_units from poll_selections where selection = '11'").fetchall()


def test_export_never_reads_post_start_counters_outside_assignment(conn, schema_url, tmp_path, guarded):
    result = _run_export(schema_url, tmp_path / "ours")
    assert result.returncode == 0, result.stdout + result.stderr
    # A fall export must still read all selections before the start.
    # The volatile test projection can run before a join's timestamp filter;
    # use the real table to check the fall export's output instead.
    conn.execute("drop view poll_selections")
    conn.execute("alter table source_selections rename to poll_selections")
    result = _run_export(schema_url, tmp_path / "fall", "--fall-all")
    assert result.returncode == 0, result.stdout + result.stderr
    import csv
    with (tmp_path / "fall" / "poll_readings.csv").open(newline="") as source:
        rows = list(csv.DictReader(source))
    assert {r["slot"] for r in rows} == {"11", "15"}
    assert all(r["at_eastern"].startswith("2026-11-20") for r in rows)


def test_receipt_for_a_removed_selection_does_not_authorize_its_counters(conn, guarded):
    at = datetime(2026, 11, 19, tzinfo=timezone.utc)
    fulfilled(conn, "removed", "11", "OLD", units_loaded=10, paid="5.00", at=at)
    fulfilled(conn, "current", "15", "CURRENT", units_loaded=10, paid="5.00", at=at)
    found = stock.stock(conn)
    assert {s.slot for s in found.sales} == {"15"}
    assert sum(s.units for s in found.sales) == 2


def test_counts_filter_in_sql_before_reading_counters(conn, guarded):
    slot_counts.record(conn, slot_counts.parse({
        "count_id": "mixed", "device_id": "vmc1", "source": "photo",
        "counted_at": "2026-11-19T12:00:00Z",
        "selections": [{"selection": s, "product": "fixture", "units": 10} for s in ("11", "15")],
    }), "test")
    found = slot_counts.latest(conn)
    assert set(found) == {"15"}
    assert found["15"]["sold_since"] == 2


@pytest.mark.parametrize("reader", [stock.stock, slot_counts.latest])
@pytest.mark.parametrize("assignment", [None, ["11"]])
def test_invalid_assignment_refuses_before_counters(conn, guarded, reader, assignment):
    if assignment is None:
        conn.execute("delete from slot_assignment")
    else:
        conn.execute("update slot_assignment set slots = %s", (assignment,))
    with pytest.raises(slots.SlotsNotRecorded):
        reader(conn)


def test_first_post_start_check_captures_the_servers_final_assignment(conn):
    conn.execute("update slot_assignment set last_checked_at = '2026-12-03T04:59:00Z'")
    final_slots = tuple(s for s in slot_counts.SELECTIONS if s not in TEST_SLOTS)
    answer = {"device_id": "vmc1", "slots": list(final_slots)}
    at = datetime(2026, 12, 3, 5, 1, tzinfo=timezone.utc)
    assert slots.record(conn, answer, now=at) == "changed"
    assert slots.our_slots(conn) == final_slots
    with pytest.raises(slots.BadAssignment):
        slots.record(conn, {**answer, "slots": list(TEST_SLOTS)}, now=at)


def test_readers_refuse_until_the_first_post_start_confirmation(conn, schema_url, monkeypatch, capsys):
    from vending import dashboard, pages
    from vending import db
    from tests.conftest import open_in
    import slots_config

    start = slots.lock_at()
    conn.execute("update slot_assignment set last_checked_at = %s", (start.replace(year=start.year - 1),))

    class AfterStart(datetime):
        @classmethod
        def now(cls, tz=None):
            return start

    monkeypatch.setattr(slots, "datetime", AfterStart)
    for read in (slots.our_slots, stock.stock, slot_counts.latest):
        with pytest.raises(slots.SlotsNotRecorded, match="final server assignment"):
            read(conn)
    with pytest.raises(SystemExit, match="final server assignment"):
        slots_config.resolve_slots(None, conn)
    assert len(slots_config.resolve_slots(None, conn, fall=True)[1]) == 40
    monkeypatch.setattr(db, "connect", lambda: open_in(*schema_url))
    assert slots.main(["show"]) == 1
    assert "final server assignment" in capsys.readouterr().err
    assert pages.stored_slots(conn) == ()
    snap = dashboard.snapshot(conn)
    assert snap["ours"]["ok"] is False
    assert snap["counts"]["data"] == {}
    assert slots.record(conn, {"team": "group-b", "device_id": "vmc1", "slots": list(TEST_SLOTS)}, now=start) == "unchanged"
    assert slots.our_slots(conn) == TEST_SLOTS


def test_simultaneous_initial_records_both_succeed(conn, schema_url):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from tests.conftest import open_in

    conn.execute("delete from slot_assignment")
    # Keep the first insert open long enough for the other recorder to observe
    # an empty table unless initialization itself is serialized.
    conn.execute("""
        create function slow_insert() returns trigger language plpgsql as $$
        begin perform pg_sleep(0.3); return new; end $$;
        create trigger slow_insert before insert on slot_assignment
        for each row execute function slow_insert()
    """)
    start = threading.Barrier(2)
    answer = {"device_id": "vmc1", "slots": list(TEST_SLOTS)}

    def record():
        with open_in(*schema_url) as other:
            start.wait(timeout=5)
            return slots.record(other, answer)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: record(), range(2)))
    assert sorted(results) == ["recorded", "unchanged"]
    assert conn.execute("select count(*) from slot_assignment").fetchone() == (1,)
