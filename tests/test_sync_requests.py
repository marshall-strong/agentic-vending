"""Tests for storing purchase requests and their receipts. Synthetic answers only,
shaped like the bridge's `PurchaseRequestPending` and `PurchaseRequestDetail`.
Skipped like `test_sync_history` when there is no database."""

from datetime import datetime, timedelta, timezone
from fractions import Fraction

import pytest

pytest.importorskip("psycopg")

# These import psycopg too, so they come after the skip.
from tests.conftest import conn  # noqa: E402,F401 - migrated schema with a server assignment
from vending.bridge import BridgeError, BridgeRateLimited, BridgeUnreachable  # noqa: E402
from vending.purchase_requests import get_purchase_requests, record, store  # noqa: E402
from vending.stock import stock  # noqa: E402
from vending.sync_requests import sync  # noqa: E402

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
BODY = {"off_cycle": False, "lines": [{"slot": "15", "source": "purchase", "primary": {
    "sku": "COKE20", "store": "Costco", "packs": 1, "units_per_pack": 12,
    "max_price_per_pack": "13.00", "sale_price": "2.50", "change_product": False}}]}


def submit(conn, request_id, days_ago=2):
    record(conn, request_id, NOW - timedelta(days=days_ago), False, BODY, plan_hash="abc123")


def pending(request_id):
    return {"id": request_id, "status": "pending", "created_at": "2026-11-07T14:00:00Z"}


def receipt(**overrides):
    fields = {
        "outcome": "primary", "sku": "COKE20", "store": "Costco", "packs_bought": 1, "units_per_pack": 12,
        "units_bought": 12, "total_paid": "12.37", "unit_cost_paid": "1.1245", "units_loaded": 8,
        "units_lost": 1, "loss_note": "one can split", "later_losses": [], "units_to_back_stock": 3,
        "units_from_back_stock": 0, "units_pulled": 0, "pulled_sku": None, "slot_reassigned": False,
        "resolved_at": "2026-11-07T16:00:00Z",
    }
    return {**fields, **overrides}


def fulfilled(request_id, *receipts, fulfilled_at="2026-11-07T16:30:00Z", **fields):
    """A fulfilled answer. `fields` are more request-level fields, such as the door times."""
    return {
        "id": request_id, "status": "fulfilled", "device_id": "vmc1", "created_at": "2026-11-07T14:00:00Z",
        "updated_at": fulfilled_at, "fulfilled_at": fulfilled_at, "off_cycle": False, "note": None,
        "decision_note": None, "total_paid": "12.37", **fields,
        "lines": [{"line": i, "slot": "15", "source": "purchase", "primary": BODY["lines"][0]["primary"],
                   "alternate": None, "load": None, "receipt": r} for i, r in enumerate(receipts)],
    }


def later(units, location="coil", at="2026-11-08T10:00:00Z"):
    return {"location": location, "sku": "COKE20", "units": units, "note": "dented", "recorded_at": at}


class FakeBridge:
    """Answers from a dict. A value that is an exception is raised; a list is answered in turn."""

    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def purchase_request(self, request_id):
        self.asked.append(request_id)
        answer = self.answers[request_id]
        if isinstance(answer, list):
            answer = answer.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def quiet(_):
    pass


def doors(said):
    """What was logged, less the receipt-price lines (`vending.receipt_prices`): these tests have no sourcing rows."""
    return [s for s in said if "sourcing table" not in s]


def test_pending_then_fulfilled(conn):
    submit(conn, "r1")
    bridge = FakeBridge({"r1": [pending("r1"), fulfilled("r1", receipt())]})
    report = sync(conn, bridge, NOW, log=quiet)
    assert (report.checked, report.pending) == (1, 1)
    assert conn.execute("select status, last_checked from purchase_requests").fetchone() == ("pending", NOW)

    report = sync(conn, bridge, NOW, log=quiet)
    assert report.fulfilled == 1
    row = conn.execute("select outcome, total_paid, unit_cost_paid, units_loaded, units_to_back_stock"
                       " from purchase_lines").fetchone()
    assert row[0] == "primary" and str(row[1]) == "12.37" and str(row[2]) == "1.1245" and row[3:] == (8, 3)


def test_later_losses_are_picked_up_without_double_counting(conn):
    submit(conn, "r1")
    bridge = FakeBridge({"r1": [
        fulfilled("r1", receipt(later_losses=[later(1)])),
        fulfilled("r1", receipt(later_losses=[later(1)])),
        fulfilled("r1", receipt(later_losses=[later(1), later(2, "back_stock", "2026-11-08T11:00:00Z")])),
    ]})
    assert [sync(conn, bridge, NOW, log=quiet).new_losses for _ in range(3)] == [1, 0, 1]
    assert conn.execute("select count(*), sum(units) from purchase_line_losses").fetchone() == (2, 3)
    [slot] = stock(conn).slots
    assert (slot.units, slot.lost) == (7, 1)


def test_rejected_is_stored_and_not_read_again(conn):
    submit(conn, "r1")
    bridge = FakeBridge({"r1": {**pending("r1"), "status": "rejected", "decision_note": "Costco is closed"}})
    assert sync(conn, bridge, NOW, log=quiet).rejected == 1
    assert sync(conn, bridge, NOW, log=quiet).checked == 0
    assert get_purchase_requests(conn)[0]["decision_note"] == "Costco is closed"


def test_one_bad_request_does_not_stop_the_others(conn):
    for request_id in ("gone", "odd", "nolines", "ok"):
        submit(conn, request_id)
    bridge = FakeBridge({
        "gone": BridgeError("404 from /api/v1/purchase-requests/gone"),
        "odd": {"id": "odd", "status": "cancelled"},
        "nolines": fulfilled("nolines", receipt(), receipt()) | {"lines": [
            {"line": 0, "slot": "15", "source": "purchase", "receipt": None}]},
        "ok": fulfilled("ok", receipt()),
    })
    report = sync(conn, bridge, NOW, log=quiet)
    assert (report.failed, report.fulfilled) == (3, 1)
    statuses = dict(conn.execute("select request_id, status from purchase_requests").fetchall())
    assert statuses == {"gone": "pending", "odd": "pending", "nolines": "pending", "ok": "fulfilled"}


def test_a_bad_reread_keeps_the_receipts_we_had(conn):
    submit(conn, "r1")
    good = fulfilled("r1", receipt())
    broken = fulfilled("r1", receipt()) | {"lines": [{"line": 0, "slot": "15", "source": "purchase", "receipt": None}]}
    sync(conn, FakeBridge({"r1": good}), NOW, log=quiet)
    assert sync(conn, FakeBridge({"r1": broken}), NOW, log=quiet).failed == 1
    assert conn.execute("select count(*) from purchase_lines").fetchone()[0] == 1


def test_rate_limit_stops_the_run_and_leaves_the_rest(conn):
    for days_ago, request_id in enumerate(("c", "b", "a")):
        submit(conn, request_id, days_ago=days_ago + 1)
    bridge = FakeBridge({"a": pending("a"), "b": BridgeRateLimited("429"), "c": pending("c")})
    report = sync(conn, bridge, NOW, log=quiet)
    assert (report.checked, report.skipped) == (1, 2)
    assert bridge.asked == ["a", "b"]


def test_unreachable_bridge_stops_the_sync(conn):
    submit(conn, "r1")
    with pytest.raises(BridgeUnreachable):
        sync(conn, FakeBridge({"r1": BridgeUnreachable("Is Tailscale connected?")}), NOW, log=quiet)


def test_only_recently_fulfilled_requests_are_read_again(conn):
    submit(conn, "old", days_ago=60)
    submit(conn, "new")
    sync(conn, FakeBridge({"old": fulfilled("old", receipt(), fulfilled_at="2026-09-10T16:00:00Z"),
                           "new": fulfilled("new", receipt())}), NOW, log=quiet)
    bridge = FakeBridge({"old": fulfilled("old", receipt()), "new": fulfilled("new", receipt())})
    sync(conn, bridge, NOW, log=quiet)
    assert bridge.asked == ["new"]


def test_get_purchase_requests_shows_plan_receipts_and_losses(conn):
    submit(conn, "r1", days_ago=3)
    submit(conn, "r2", days_ago=1)
    sync(conn, FakeBridge({"r1": fulfilled("r1", receipt(later_losses=[later(1)])), "r2": pending("r2")}),
         NOW, log=quiet)
    everything = get_purchase_requests(conn)
    assert [r["request_id"] for r in everything] == ["r2", "r1"]
    r1 = everything[1]
    assert (r1["status"], r1["total_paid"], r1["plan"], r1["plan_hash"]) == ("fulfilled", "12.37", BODY, "abc123")
    [line] = r1["lines"]
    assert line["primary"]["sku"] == "COKE20"
    assert line["receipt"]["total_paid"] == "12.37" and line["receipt"]["unit_cost_paid"] == "1.1245"
    assert line["receipt"]["later_losses"] == [
        {"location": "coil", "sku": "COKE20", "units": 1, "note": "dented", "recorded_at": "2026-11-08T10:00:00+00:00"}]
    assert [r["request_id"] for r in get_purchase_requests(conn, pending_only=True)] == ["r2"]


def test_stock_reads_receipts_and_poll_counters(conn):
    submit(conn, "r1")
    sync(conn, FakeBridge({"r1": fulfilled("r1", receipt(total_paid="12.00"))}), NOW, log=quiet)
    for session_id, started_at, lifetime in (("p1", "2026-11-07T15:00:00Z", 40), ("p2", "2026-11-08T15:00:00Z", 43)):
        conn.execute("insert into polls (session_id, device_id, started_at, raw, byte_count, crc_ok)"
                     " values (%s, 'vmc1', %s, '', 0, true)", (session_id, started_at))
        conn.execute("insert into poll_selections (session_id, selection, price_cents, lifetime_units,"
                     " lifetime_sales_cents) values (%s, '15', 250, %s, 0)", (session_id, lifetime))
    result = stock(conn)
    [slot] = result.slots
    assert (slot.product, slot.units, slot.sold, slot.unit_cost) == ("COKE20", 5, 3, Fraction(1200, 11))
    assert [(c.product, c.units, c.unit_cost) for c in result.closet] == [("COKE20", 3, Fraction(1200, 11))]
    assert result.warnings == ()


def test_stock_ignores_counters_from_another_device(conn):
    # The sync stores every device's polls, with the same selection names.
    submit(conn, "r1")
    sync(conn, FakeBridge({"r1": fulfilled("r1", receipt(total_paid="12.00"))}), NOW, log=quiet)
    for session_id, device, started_at, lifetime in (
            ("p1", "vmc1", "2026-11-07T15:00:00Z", 40), ("p2", "vmc1", "2026-11-08T15:00:00Z", 43),
            ("x1", "vmc2", "2026-11-07T15:30:00Z", 100), ("x2", "vmc2", "2026-11-08T15:30:00Z", 106)):
        conn.execute("insert into polls (session_id, device_id, started_at, raw, byte_count, crc_ok)"
                     " values (%s, %s, %s, '', 0, true)", (session_id, device, started_at))
        conn.execute("insert into poll_selections (session_id, selection, price_cents, lifetime_units,"
                     " lifetime_sales_cents) values (%s, '15', 250, %s, 0)", (session_id, lifetime))
    [slot] = stock(conn).slots
    assert (slot.units, slot.sold) == (5, 3)


def test_record_twice_keeps_what_the_sync_learned(conn):
    submit(conn, "r1")
    sync(conn, FakeBridge({"r1": fulfilled("r1", receipt())}), NOW, log=quiet)
    submit(conn, "r1")
    assert conn.execute("select status from purchase_requests").fetchone() == ("fulfilled",)


@pytest.mark.parametrize("final", ["fulfilled", "rejected"])
def test_a_late_pending_answer_does_not_overwrite_a_final_one(conn, final):
    """A visit step that outlived its timeout can read a request as pending, and write it after the hourly
    sync stored the receipt. Pending is where a request starts; it never comes back."""
    submit(conn, "r1")
    door = {"door_opened_at": "2026-11-09T11:00:00+00:00", "door_closed_at": "2026-11-09T11:20:00+00:00"}
    answer = (fulfilled("r1", receipt(), fulfilled_at="2026-11-09T11:30:00Z", **door) if final == "fulfilled"
              else {"id": "r1", "status": "rejected", "decision_note": "no", "created_at": "2026-11-07T14:00:00Z"})
    assert store(conn, "r1", answer, NOW).status == final
    before = conn.execute("select status, decision_note, fulfilled_at, door_opened_at, door_closed_at, last_checked,"
                          " last_response from purchase_requests").fetchone()
    lines = conn.execute("select count(*) from purchase_lines").fetchone()[0]

    assert store(conn, "r1", pending("r1"), NOW + timedelta(minutes=5)).status == final     # told what is stored
    assert conn.execute("select status, decision_note, fulfilled_at, door_opened_at, door_closed_at, last_checked,"
                        " last_response from purchase_requests").fetchone() == before
    assert conn.execute("select count(*) from purchase_lines").fetchone()[0] == lines
    # The other way round still works, and a request that was never recorded is still refused.
    with pytest.raises(ValueError, match="not in purchase_requests"):
        store(conn, "nope", pending("nope"), NOW)


def test_a_float_amount_is_refused(conn):
    submit(conn, "r1")
    with pytest.raises(ValueError):
        store(conn, "r1", fulfilled("r1", receipt(total_paid=12.37)), NOW)


def test_main_refuses_a_schema_missing_migrations(database_url, monkeypatch, capsys):
    import uuid

    import pytest

    from tests.conftest import open_in
    from vending import db, sync_requests

    schema = f"test_{uuid.uuid4().hex[:8]}"
    with open_in(database_url, "public") as admin:
        admin.execute(f"create schema {schema}")
        try:
            monkeypatch.setattr(db, "connect", lambda: open_in(database_url, schema))
            monkeypatch.setattr(sync_requests, "Bridge", lambda: pytest.fail("ran on an unmigrated schema"))
            assert sync_requests.main([]) == 2
            err = capsys.readouterr().err
            assert "001_polls.sql" in err and "python -m vending.db migrate" in err
            # It only looked: nothing was created.
            with open_in(database_url, schema) as conn:
                assert conn.execute("select to_regclass('schema_migrations')").fetchone() == (None,)
        finally:
            admin.execute(f"drop schema {schema} cascade")


def test_main_runs_on_a_migrated_schema(schema_url, monkeypatch, capsys):
    from tests.conftest import open_in
    from vending import db, sync_requests

    monkeypatch.setattr(db, "connect", lambda: open_in(*schema_url))
    # Nothing is due on an empty schema, so the bridge is never asked for anything.
    monkeypatch.setattr(sync_requests, "Bridge", lambda: object())
    assert sync_requests.main([]) == 0
    assert "0 read" in capsys.readouterr().out


def test_stock_preserves_receipts_but_does_not_count_removed_selections_sales(conn):
    # Historical receipts stay on the books, but do not authorize reads of a removed selection.
    from tests.conftest import TEST_SLOTS
    conn.execute("update slot_assignment set slots = %s", ([s for s in TEST_SLOTS if s != "15"] + ["11"],))
    submit(conn, "r1")
    sync(conn, FakeBridge({"r1": fulfilled("r1", receipt(total_paid="12.00"))}), NOW, log=quiet)
    for session_id, started_at, lifetime in (("p1", "2026-11-07T15:00:00Z", 40), ("p2", "2026-11-08T15:00:00Z", 43)):
        conn.execute("insert into polls (session_id, device_id, started_at, raw, byte_count, crc_ok)"
                     " values (%s, 'vmc1', %s, '', 0, true)", (session_id, started_at))
        conn.execute("insert into poll_selections (session_id, selection, price_cents, lifetime_units,"
                     " lifetime_sales_cents) values (%s, '15', 250, %s, 0)", (session_id, lifetime))
    [slot] = stock(conn).slots
    assert (slot.units, slot.sold) == (8, 0)


# --- Door times (17 Nov): request-level, to the minute, null on older requests -------------

OPENED, CLOSED = "2026-11-07T15:34:00+00:00", "2026-11-07T16:34:00+00:00"


def door_row(conn, request_id="r1"):
    return conn.execute("select door_opened_at, door_closed_at from purchase_requests where request_id = %s",
                        (request_id,)).fetchone()


def test_door_times_are_stored_with_the_receipt(conn):
    submit(conn, "r1")
    store(conn, "r1", fulfilled("r1", receipt(), door_opened_at=OPENED, door_closed_at=CLOSED), NOW)
    assert door_row(conn) == (datetime(2026, 11, 7, 15, 34, tzinfo=timezone.utc),
                              datetime(2026, 11, 7, 16, 34, tzinfo=timezone.utc))


@pytest.mark.parametrize("fields", [
    {},                                                           # an older request: no such fields
    {"door_opened_at": None, "door_closed_at": None},             # the worker left them out
    {"door_opened_at": OPENED, "door_closed_at": None},           # half a visit is no visit
    {"door_opened_at": None, "door_closed_at": CLOSED},
])
def test_missing_or_null_door_times_are_stored_as_null(conn, fields):
    submit(conn, "r1")
    stored = store(conn, "r1", fulfilled("r1", receipt(), **fields), NOW)
    assert door_row(conn) == (None, None)
    # Only half a pair is worth telling a person about: the receipt has one, so the other went missing.
    half = len([v for v in fields.values() if v is not None]) == 1
    assert len(stored.warnings) == half and all(w.startswith("r1: the receipt has door_") for w in stored.warnings)
    # The answer itself is still kept whole.
    assert conn.execute("select last_response from purchase_requests").fetchone()[0].items() >= fields.items()


def test_half_a_pair_of_door_times_is_logged_naming_the_request(conn):
    submit(conn, "r1")
    said = []
    sync(conn, FakeBridge({"r1": fulfilled("r1", receipt(), door_opened_at=OPENED)}), NOW, log=said.append)
    assert doors(said) == ["r1: the receipt has door_opened_at but no door_closed_at; neither is kept, "
                    "so its visit is dated by resolved_at"]
    assert door_row(conn) == (None, None)


@pytest.mark.parametrize("bad", [1763000000, 17.5, "soon", ["2026-11-07T15:34:00Z"], {"at": OPENED}, True])
def test_a_door_time_that_is_not_a_time_is_logged_and_the_rest_of_the_batch_still_syncs(conn, bad):
    # A number (or anything not a time string) used to raise TypeError past the per-request
    # handler and stop the whole sync. It is treated as missing: logged, neither kept.
    submit(conn, "r1")
    submit(conn, "r2")
    said = []
    report = sync(conn, FakeBridge({
        "r1": fulfilled("r1", receipt(), door_opened_at=bad, door_closed_at=CLOSED),
        "r2": fulfilled("r2", receipt(), door_opened_at=OPENED, door_closed_at=CLOSED),
    }), NOW, log=said.append)
    assert (report.checked, report.failed) == (2, 0)
    assert doors(said) == [f"r1: door_opened_at is {bad!r}, not a time; neither door time is kept, "
                    "so its visit is dated by resolved_at"]
    assert door_row(conn, "r1") == (None, None)
    assert door_row(conn, "r2")[1] == datetime(2026, 11, 7, 16, 34, tzinfo=timezone.utc)
    assert conn.execute("select count(*) from purchase_lines where request_id = 'r1'").fetchone()[0] == 1


def test_a_door_time_with_a_date_and_no_time_is_logged_and_the_receipt_kept(conn):
    # Read as midnight, "2026-11-07" would close the door before its 15:34 open, and the whole
    # receipt would be refused. It isn't a time: neither door time is kept, the receipt is.
    submit(conn, "r1")
    said = []
    report = sync(conn, FakeBridge({"r1": fulfilled("r1", receipt(), door_opened_at=OPENED,
                                                    door_closed_at="2026-11-07")}), NOW, log=said.append)
    assert (report.checked, report.failed) == (1, 0)
    assert doors(said) == ["r1: door_closed_at is '2026-11-07', not a time; neither door time is kept, "
                    "so its visit is dated by resolved_at"]
    assert door_row(conn) == (None, None)
    assert conn.execute("select count(*) from purchase_lines").fetchone()[0] == 1


def test_a_door_closed_before_it_opened_is_logged_and_not_stored(conn):
    submit(conn, "r1")
    said = []
    bad = fulfilled("r1", receipt(), door_opened_at=CLOSED, door_closed_at=OPENED)
    report = sync(conn, FakeBridge({"r1": bad}), NOW, log=said.append)
    assert report.failed == 1 and any("before it opened" in s for s in said)
    assert conn.execute("select status, last_response from purchase_requests").fetchone() == ("pending", None)
    assert conn.execute("select count(*) from purchase_lines").fetchone()[0] == 0
    # A good receipt we already had is kept when a later read is bad.
    store(conn, "r1", fulfilled("r1", receipt(), door_opened_at=OPENED, door_closed_at=CLOSED), NOW)
    sync(conn, FakeBridge({"r1": bad}), NOW, log=quiet)
    assert door_row(conn)[1] == datetime(2026, 11, 7, 16, 34, tzinfo=timezone.utc)
    # The database refuses one too, should anything write past `store`.
    import psycopg
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("update purchase_requests set door_opened_at = door_closed_at + interval '1 minute'")


def test_stock_reads_door_times_for_a_line_resolved_before_the_door_opened(conn):
    # As on pr_v8hB_h3gQatc: a line filled in before the door opened. A poll at 15:33,
    # after resolved_at but before door open, sees 3 sales. They are not the new load's.
    submit(conn, "r1")
    sync(conn, FakeBridge({"r1": fulfilled("r1", receipt(total_paid="12.00", resolved_at="2026-11-07T15:31:52Z"),
                                           door_opened_at=OPENED, door_closed_at=CLOSED)}), NOW, log=quiet)
    for session_id, started_at, lifetime in (("p1", "2026-11-07T14:33:00Z", 40), ("p2", "2026-11-07T15:33:00Z", 43),
                                             ("p3", "2026-11-07T17:33:00Z", 44)):
        conn.execute("insert into polls (session_id, device_id, started_at, raw, byte_count, crc_ok)"
                     " values (%s, 'vmc1', %s, '', 0, true)", (session_id, started_at))
        conn.execute("insert into poll_selections (session_id, selection, price_cents, lifetime_units,"
                     " lifetime_sales_cents) values (%s, '15', 250, %s, 0)", (session_id, lifetime))
    [slot] = stock(conn).slots
    assert (slot.since, slot.units, slot.sold) == (datetime(2026, 11, 7, 16, 34, tzinfo=timezone.utc), 7, 1)


def test_migration_019_backfills_door_times_from_stored_answers(database_url):
    import uuid

    from psycopg.types.json import Jsonb

    from tests.conftest import open_in
    from vending import db

    schema = f"test_{uuid.uuid4().hex[:8]}"
    with open_in(database_url, "public") as admin:
        admin.execute(f"create schema {schema}")
        try:
            with open_in(database_url, schema) as conn:
                # Every migration before 019, recorded as applied, as on a database that is behind.
                conn.execute("create table schema_migrations (version text primary key,"
                             " applied_at timestamptz not null default now())")
                for path in sorted(db.MIGRATIONS.glob("*.sql")):
                    if path.name >= "019":
                        break
                    conn.execute(path.read_text(encoding="utf-8"))
                    conn.execute("insert into schema_migrations (version) values (%s)", (path.name,))
                answers = {
                    "both": {"door_opened_at": OPENED, "door_closed_at": CLOSED},
                    "no_zone": {"door_opened_at": "2026-11-07T15:34:00", "door_closed_at": "2026-11-07T16:34:00"},
                    "none": {},
                    "nulls": {"door_opened_at": None, "door_closed_at": None},
                    "half": {"door_opened_at": OPENED},
                    "backwards": {"door_opened_at": CLOSED, "door_closed_at": OPENED},
                    "garbage": {"door_opened_at": "soon", "door_closed_at": "later"},
                    # Passes a prefix regex, but there is no 31 November: casting it would abort 019.
                    "bad_date": {"door_opened_at": "2026-11-31T15:34:00Z", "door_closed_at": CLOSED},
                    "unread": None,
                }
                for request_id, fields in answers.items():
                    conn.execute(
                        "insert into purchase_requests (request_id, submitted_at, off_cycle, request_body, last_response)"
                        " values (%s, %s, false, '{}', %s)",
                        (request_id, NOW, None if fields is None else Jsonb(fulfilled(request_id, receipt(), **fields))))
                conn.execute("set time zone 'America/New_York'")   # the backfill must not depend on the session
                notices = []
                conn.add_notice_handler(lambda d: notices.append(d.message_primary))
                assert db.migrate(conn)[0] == "019_door_times.sql"
                got = dict((r, (o, c)) for r, o, c in conn.execute(
                    "select request_id, door_opened_at, door_closed_at from purchase_requests"))
                visit = (datetime(2026, 11, 7, 15, 34, tzinfo=timezone.utc), datetime(2026, 11, 7, 16, 34, tzinfo=timezone.utc))
                assert got == {"both": visit, "no_zone": visit, **{r: (None, None) for r in answers if r not in ("both", "no_zone")}}
                # The skipped pairs are named; a request with no door times at all is not.
                assert "019: door times filled in on 2 request(s)" in notices
                [skipped] = [n for n in notices if "skipped" in n]
                assert skipped.startswith("019: 4 request(s) skipped") and skipped.endswith(
                    "left null: backwards, bad_date, garbage, half")
        finally:
            admin.execute(f"drop schema {schema} cascade")
