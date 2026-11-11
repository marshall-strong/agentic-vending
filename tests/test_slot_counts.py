"""Counts of what is in the machine, and sales since each count."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from tests.conftest import store_poll
from vending import slot_counts
from vending.slot_counts import CountError, parse

PHOTO = Path(__file__).resolve().parent.parent / "db" / "counts" / "2026-11-09-photo.json"


def count(**changes):
    data = {"count_id": "c1", "counted_at": "2026-11-09T11:30:00-05:00", "source": "photo", "device_id": "vmc1",
            "selections": [{"selection": "25", "product": "Premier Protein", "units": 4, "price_on_tag": "4.00"},
                           {"selection": "26", "product": None, "units": 0, "price_on_tag": "4.00"}]}
    data.update(changes)
    return data


def test_the_committed_photo_count_is_valid_and_covers_the_whole_machine():
    parsed = parse(json.loads(PHOTO.read_text(encoding="utf-8")))
    assert {s.selection for s in parsed.selections} == set(slot_counts.SELECTIONS)
    assert parsed.exact is False
    assert all(s.price_on_tag is None or isinstance(s.price_on_tag, Decimal) for s in parsed.selections)


def test_every_problem_is_listed_at_once():
    bad = count(counted_at="2026-11-09T11:30:00", source="guess", selections=[
        {"selection": "59", "product": "x", "units": 1},
        {"selection": "25", "product": None, "units": 3},
        {"selection": "26", "product": "y", "units": -1, "price_on_tag": 2.5},
        {"selection": "27", "product": "z", "units": 1, "price_on_tag": "2.505"},
        {"selection": "27", "product": "z", "units": 1}])
    with pytest.raises(CountError) as error:
        parse(bad)
    message = str(error.value)
    for expected in ("time zone", "source", "59", "25: units are counted but no product",
                     "26: units", "26: price_on_tag must be exact text", "27: price_on_tag must be dollars",
                     "27 appears more than once"):
        assert expected in message


def test_record_writes_once_and_logs(conn):
    assert slot_counts.record(conn, parse(count()), "kevin") == 2
    rows = conn.execute("select selection, product, units, exact, price_on_tag, counted_by from slot_counts"
                        " order by selection").fetchall()
    assert rows == [("25", "Premier Protein", 4, False, Decimal("4.00"), "kevin"),
                    ("26", None, 0, False, Decimal("4.00"), "kevin")]
    assert conn.execute("select tool, outcome from action_log").fetchall() == [("slot_counts", "done")]
    with pytest.raises(CountError, match="already recorded"):
        slot_counts.record(conn, parse(count()), "kevin")
    assert conn.execute("select count(*) from slot_counts").fetchone()[0] == 2


def test_sales_before_the_count_are_not_taken_off_again(conn):
    # Poll at 10:00 ET reads 100; 3 sell; the count at 11:30 sees 4; polls after read 103, then 105.
    store_poll(conn, "a", "2026-11-09T15:00:00+00:00", {"25": (400, 100)})
    store_poll(conn, "b", "2026-11-09T17:00:00+00:00", {"25": (400, 103)})
    store_poll(conn, "c", "2026-11-10T15:00:00+00:00", {"25": (400, 105)})
    slot_counts.record(conn, parse(count()), "kevin")
    seen = slot_counts.latest(conn)["25"]
    assert (seen["sold_since"], seen["on_hand"]) == (2, 2)


def test_no_poll_before_the_count_still_counts_sales_after_it(conn):
    store_poll(conn, "b", "2026-11-09T17:00:00+00:00", {"25": (400, 100)})
    store_poll(conn, "c", "2026-11-10T15:00:00+00:00", {"25": (400, 103)})
    slot_counts.record(conn, parse(count()), "kevin")
    assert slot_counts.latest(conn)["25"]["on_hand"] == 1


def test_a_counter_reset_starts_from_its_new_value(conn):
    store_poll(conn, "b", "2026-11-09T17:00:00+00:00", {"25": (400, 100)})
    store_poll(conn, "c", "2026-11-10T15:00:00+00:00", {"25": (400, 1)})     # board swapped
    store_poll(conn, "d", "2026-11-11T15:00:00+00:00", {"25": (400, 2)})
    slot_counts.record(conn, parse(count()), "kevin")
    # `vending.counters.rises`, like the ledger: one low poll could be a misread, so the restart
    # is confirmed by the next poll and counted from there. 1 to 2 is one sale.
    assert slot_counts.latest(conn)["25"]["sold_since"] == 1


def test_no_poll_since_the_count_keeps_the_counted_units(conn):
    store_poll(conn, "a", "2026-11-09T15:00:00+00:00", {"25": (400, 100)})
    slot_counts.record(conn, parse(count()), "kevin")
    seen = slot_counts.latest(conn)["25"]
    assert (seen["sold_since"], seen["on_hand"]) == (None, 4)


def test_missing_poll_tables_keep_the_count(conn):
    slot_counts.record(conn, parse(count()), "kevin")
    conn.execute("drop table poll_selections")
    seen = slot_counts.latest(conn)["25"]
    assert (seen["product"], seen["sold_since"], seen["on_hand"]) == ("Premier Protein", None, 4)


def test_more_sales_than_counted_never_goes_negative(conn):
    store_poll(conn, "b", "2026-11-09T17:00:00+00:00", {"25": (400, 100)})
    store_poll(conn, "c", "2026-11-12T15:00:00+00:00", {"25": (400, 110)})
    slot_counts.record(conn, parse(count()), "kevin")
    assert slot_counts.latest(conn)["25"]["on_hand"] == 0


def test_a_count_for_another_device_or_with_a_bad_note_is_refused():
    with pytest.raises(CountError, match="device_id"):
        parse(count(device_id="vmc-1"))
    with pytest.raises(CountError, match="25: note must be text"):
        parse(count(selections=[{"selection": "25", "product": "x", "units": 1, "note": {"a": 1}}]))


def test_a_later_count_replaces_an_earlier_one(conn):
    slot_counts.record(conn, parse(count()), "kevin")
    slot_counts.record(conn, parse(count(count_id="c2", counted_at="2026-11-13T09:00:00-05:00", source="door",
                                         selections=[{"selection": "25", "product": "Snapple", "units": 7}])), "kevin")
    seen = slot_counts.latest(conn)
    assert (seen["25"]["product"], seen["25"]["exact"], seen["26"]["count_id"]) == ("Snapple", True, "c1")


def test_dry_run_writes_nothing(tmp_path, capsys):
    path = tmp_path / "c.json"
    path.write_text(json.dumps(count()), encoding="utf-8")
    assert slot_counts.main(["record", str(path), "--by", "kevin", "--dry-run"]) == 0
    assert "nothing written" in capsys.readouterr().out


def _at(hour):
    from datetime import datetime, timezone
    return datetime(2026, 11, 10, hour, tzinfo=timezone.utc)


def test_sales_since_a_count_use_the_same_counter_rule_as_the_ledger():
    # Review on #47: one misread poll, then a board swap. `vending.counters.rises` says 250 and 4.
    misread = [(_at(1), 100000), (_at(2), 500), (_at(3), 100250)]
    assert slot_counts._sold_since(misread, _at(1)) == 250
    swap = [(_at(1), 1000), (_at(2), 5), (_at(3), 7), (_at(4), 9)]
    assert slot_counts._sold_since(swap, _at(1)) == 4
    assert slot_counts._sold_since(swap, _at(5)) is None
