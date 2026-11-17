"""Our slot assignment: read from the inventory server, followed until the lock time, then held, and used by
every tool that reads the database. Only a person changes it; the worker only confirms it. The parsing tests need
no database. "Now" is conftest's TEST_NOW (before the start) unless a test sets the slot clock."""

from datetime import date, datetime, timedelta, timezone

import pytest

from tests.conftest import TEST_NOW, TEST_SLOTS
from vending import settings, slots
from vending.slots import BadAssignment, SlotsNotRecorded, parse

# An answer shaped like the bridge's, for the test assignment.
ANSWER = {"team": "group-b", "device_id": "vmc1", "slots": list(TEST_SLOTS)}
NOW = TEST_NOW
LOCK = datetime.fromisoformat(slots.DEFAULT_LOCK_AT)
AFTER = LOCK + timedelta(hours=1)
# The other half of the machine, columns 1-4: a redrawn split could give us this.
LEFT = tuple(f"{row}{col}" for row in range(1, 6) for col in range(1, 5))
MOVED = {"team": "group-b", "device_id": "vmc1", "slots": list(LEFT)}
WHOLE_MACHINE = [f"{row}{col}" for row in range(1, 6) for col in range(1, 9)]


# --- Parsing (no database) ------------------------------------------------------------------------

def test_the_live_answer_parses_in_the_order_it_came():
    found = parse(ANSWER)
    assert (found.team, found.device_id, found.slots) == ("group-b", "vmc1", TEST_SLOTS)


def test_any_twenty_selections_on_our_machine_parse():
    assert parse(MOVED).slots == LEFT
    assert parse({**ANSWER, "slots": WHOLE_MACHINE[:20]}).slots == tuple(WHOLE_MACHINE[:20])   # shelves 1-2, 3


# Malformed: refused before and after the lock, by `record` and (stored by hand) by `our_slots`.
MALFORMED = [
    (["15", "16"], "isn't a JSON object"),
    ({"team": "group-b", "slots": ["15"]}, "no device_id"),
    ({**ANSWER, "device_id": "vmc2"}, "'vmc2'"),
    ({**ANSWER, "slots": []}, "lists no slots"),
    ({**ANSWER, "slots": "15,16"}, "lists no slots"),
    ({**ANSWER, "slots": ["15", ""]}, "non-empty name"),
    ({**ANSWER, "slots": ["15", " 16"]}, "non-empty name"),
    ({**ANSWER, "slots": ["15", 16]}, "non-empty name"),
    ({**ANSWER, "slots": [*TEST_SLOTS[:-1], "15"]}, "listed twice: 15"),
    ({**ANSWER, "slots": list(TEST_SLOTS[:-1])}, "names 19 different selections, not 20"),
    ({**ANSWER, "slots": [*TEST_SLOTS, "11"]}, "names 21 different selections, not 20"),
    ({**ANSWER, "slots": ["11", "12", "13", "14"]}, "names 4 different selections, not 20"),
    ({**ANSWER, "slots": WHOLE_MACHINE}, "names 40 different"),
    ({**ANSWER, "slots": [*TEST_SLOTS[:-1], "50"]}, "not a selection .*: 50"),
    ({**ANSWER, "slots": [*TEST_SLOTS[:-1], "19"]}, "not a selection .*: 19"),
    ({**ANSWER, "slots": [*TEST_SLOTS[:-1], "68"]}, "not a selection .*: 68"),
    ({**ANSWER, "slots": [*TEST_SLOTS[:-1], "A5"]}, "not a selection .*: A5"),
    ({**ANSWER, "slots": [*TEST_SLOTS[:-1], "155"]}, "not a selection .*: 155"),
    ({"device_id": "vmc1", "slots": list(TEST_SLOTS)}, "names no team; ours is 'group-b'"),
    ({**ANSWER, "team": ""}, "names no team"),
    ({**ANSWER, "team": 2}, "names no team"),
    ({**ANSWER, "team": "group-a"}, r"for team 'group-a', but ours is 'group-b' \(SLOTS_TEAM\)"),
    ({**ANSWER, "team": "Group B"}, "for team 'Group B'"),
    ({**MOVED, "team": "group-a"}, "for team 'group-a'"),          # the other half, under the other team's name
]


@pytest.mark.parametrize("answer, why", MALFORMED)
def test_an_answer_it_cant_trust_is_refused(answer, why):
    with pytest.raises(BadAssignment, match=why):
        parse(answer)


def test_our_team_comes_from_the_setting(monkeypatch):
    assert slots.our_team() == "group-b"
    monkeypatch.setenv(slots.TEAM_SETTING, "Group B")
    assert parse({**ANSWER, "team": "Group B"}).team == "Group B"
    with pytest.raises(BadAssignment, match="for team 'group-b', but ours is 'Group B'"):
        parse(ANSWER)


# --- The lock time --------------------------------------------------------------------------------

def test_the_lock_defaults_to_the_start_and_reads_the_setting(monkeypatch):
    monkeypatch.delenv(slots.LOCK_SETTING, raising=False)
    monkeypatch.setattr(settings, "_FILE", {})
    assert slots.lock_at() == datetime(2026, 12, 3, 5, tzinfo=timezone.utc)      # midnight Eastern
    monkeypatch.setenv(slots.LOCK_SETTING, "2026-12-03T09:00:00-05:00")
    assert slots.lock_at() == datetime(2026, 12, 3, 14, tzinfo=timezone.utc)


@pytest.mark.parametrize("value", ["2026-12-03T00:00:00", "3 Dec", "2026-12-03"])
def test_a_lock_time_without_an_offset_is_refused(monkeypatch, value):
    monkeypatch.setenv(slots.LOCK_SETTING, value)
    with pytest.raises(settings.SettingsError, match="SLOTS_LOCK_AT"):
        slots.lock_at()


# --- Storing it -----------------------------------------------------------------------------------

@pytest.fixture
def empty(conn):
    """A database with no assignment stored (conftest stores one by default)."""
    conn.execute("delete from slot_assignment")
    return conn


def _log(conn):
    return conn.execute("select tool, outcome, detail from action_log order by id").fetchall()


def test_nothing_stored_refuses_and_says_what_to_run(empty):
    with pytest.raises(SlotsNotRecorded, match="python -m vending.slots record"):
        slots.our_slots(empty)


def test_the_first_read_is_recorded(empty):
    assert slots.record(empty, ANSWER, NOW) == "recorded"
    assert slots.our_slots(empty) == TEST_SLOTS
    row = empty.execute("select team, first_read_at, last_checked_at, response from slot_assignment").fetchone()
    assert row == ("group-b", NOW, NOW, ANSWER)
    assert [r[:2] for r in _log(empty)] == [("slots.record", "done")]


def test_a_first_answer_other_than_slots_is_recorded(empty):
    """No fixed list bounds the first answer: any well-formed one is ours."""
    assert slots.record(empty, MOVED, NOW) == "recorded"
    assert slots.our_slots(empty) == LEFT


def test_a_first_read_after_the_lock_is_recorded_too(empty):
    """The lock holds an assignment already stored. With none stored there is nothing to hold."""
    assert slots.record(empty, MOVED, AFTER) == "recorded"
    assert slots.our_slots(empty) == LEFT


def test_the_same_answer_again_only_moves_last_checked(empty):
    slots.record(empty, ANSWER, NOW)
    later = NOW + timedelta(days=3)
    reordered = {**ANSWER, "slots": list(reversed(TEST_SLOTS))}   # same set: not a change
    assert slots.record(empty, reordered, later) == "unchanged"
    assert empty.execute("select first_read_at, last_checked_at from slot_assignment").fetchone() == (NOW, later)
    assert slots.our_slots(empty) == TEST_SLOTS                    # the first order stays
    assert slots.record(empty, ANSWER, AFTER) == "unchanged"        # after the lock as well
    assert empty.execute("select last_checked_at from slot_assignment").fetchone() == (AFTER,)


def test_an_unchanged_check_is_logged_only_when_it_confirms_the_assignment(empty):
    """The worker checks every hour; only the first check since the start says something new."""
    slots.record(empty, ANSWER, NOW)
    logged = len(_log(empty))
    slots.record(empty, ANSWER, NOW + timedelta(hours=1))
    assert len(_log(empty)) == logged                                # before the start: no row
    slots.record(empty, ANSWER, AFTER)
    assert len(_log(empty)) == logged + 1 and "confirmed" in _log(empty)[-1][2]
    slots.record(empty, ANSWER, AFTER + timedelta(hours=1))
    assert len(_log(empty)) == logged + 1                            # confirmed already: no row


def test_a_change_before_the_lock_replaces_it_and_is_logged(empty):
    slots.record(empty, ANSWER, NOW)
    later = NOW + timedelta(days=5)
    assert slots.record(empty, MOVED, later) == "changed"
    assert slots.our_slots(empty) == LEFT
    row = empty.execute("select team, device_id, first_read_at, last_checked_at, response"
                        " from slot_assignment").fetchone()
    assert row == ("group-b", "vmc1", later, later, MOVED)
    tool, outcome, detail = _log(empty)[-1]
    assert (tool, outcome) == ("slots.record", "done")
    assert detail.startswith("slot assignment changed: vmc1 11, 12, 13, 14, 21")
    assert "(was team group-b, vmc1 15, 16, 17, 18, 25" in detail and LOCK.isoformat() in detail


def test_a_change_just_before_the_lock_is_still_followed(empty):
    slots.record(empty, ANSWER, NOW)
    assert slots.record(empty, MOVED, LOCK - timedelta(seconds=1)) == "changed"
    assert slots.our_slots(empty) == LEFT


@pytest.mark.parametrize("at", [LOCK, AFTER, LOCK + timedelta(days=60)])
def test_a_change_from_the_lock_on_is_refused_and_changes_nothing(empty, at):
    slots.record(empty, ANSWER, NOW)
    slots.record(empty, ANSWER, LOCK)
    logged = len(_log(empty))
    with pytest.raises(BadAssignment, match="different assignment, but ours locked at 2026-12-03T00:00:00-05:00"):
        slots.record(empty, MOVED, at)
    assert slots.our_slots(empty) == TEST_SLOTS
    assert empty.execute("select last_checked_at from slot_assignment").fetchone() == (LOCK,)
    assert len(_log(empty)) == logged


def test_the_lock_can_be_moved(empty):
    slots.record(empty, ANSWER, NOW)
    slots.record(empty, ANSWER, NOW + timedelta(hours=1), lock=NOW + timedelta(hours=1))
    with pytest.raises(BadAssignment, match="locked at"):
        slots.record(empty, MOVED, NOW + timedelta(hours=2), lock=NOW + timedelta(hours=1))
    assert slots.record(empty, MOVED, AFTER, lock=AFTER + timedelta(hours=1)) == "changed"


def test_a_pre_start_observation_waiting_on_a_lock_does_not_confirm_the_final_assignment(empty, schema_url):
    """A delayed write cannot turn a pre-start answer into a post-start observation."""
    import threading
    import time

    from tests.conftest import open_in

    slots.record(empty, ANSWER, NOW)
    lock = datetime.now(timezone.utc) + timedelta(seconds=0.5)
    outcome = {}

    def waiting():
        with open_in(*schema_url) as other:
            try:
                outcome["result"] = slots.record(other, MOVED, now=lock - timedelta(seconds=0.1), lock=lock)
            except BadAssignment as e:
                outcome["result"] = e

    with open_in(*schema_url) as holder, holder.transaction():
        holder.execute("select 1 from slot_assignment where id = 1 for update")
        caller = threading.Thread(target=waiting)
        caller.start()
        time.sleep(1.0)            # the caller is now queued on the row, and the deadline has passed
    caller.join(timeout=10)
    assert not caller.is_alive()
    assert outcome.get("result") == "changed", outcome
    assert slots.our_slots(empty) == LEFT
    assert empty.execute("select last_checked_at < %s from slot_assignment", (lock,)).fetchone() == (True,)
    assert slots.record(empty, MOVED, now=lock, lock=lock) == "unchanged"
    with pytest.raises(BadAssignment):
        slots.record(empty, ANSWER, lock=lock)


def test_a_delayed_observation_cannot_overwrite_a_newer_answer_or_unconfirm_it(empty):
    slots.record(empty, ANSWER, NOW)
    with pytest.raises(BadAssignment, match="older"):
        slots.record(empty, MOVED, NOW - timedelta(seconds=1))
    slots.record(empty, ANSWER, AFTER)
    assert slots.record(empty, ANSWER, NOW) == "unchanged"
    assert empty.execute("select last_checked_at from slot_assignment").fetchone() == (AFTER,)
    with pytest.raises(BadAssignment, match="locked at"):
        slots.record(empty, MOVED, NOW)


def test_another_teams_answer_is_refused_even_with_our_slots(empty):
    """A swapped key or a misconfigured bridge answers for the other team: never ours, before or after the lock."""
    slots.record(empty, ANSWER, NOW)
    for at in (NOW + timedelta(hours=1), AFTER):
        for answer in ({**ANSWER, "team": "group-a"}, {**MOVED, "team": "group-a"}):
            with pytest.raises(BadAssignment, match="for team 'group-a'"):
                slots.record(empty, answer, at)
            with pytest.raises(BadAssignment, match="for team 'group-a'"):
                slots.check(empty, answer, at)
    assert slots.our_slots(empty) == TEST_SLOTS
    assert empty.execute("select team, last_checked_at from slot_assignment").fetchone() == ("group-b", NOW)


def test_a_stored_row_for_another_team_is_refused(conn, monkeypatch):
    conn.execute("update slot_assignment set team = 'group-a'")
    with pytest.raises(SlotsNotRecorded, match="can't be ours: it is for team 'group-a'"):
        slots.our_slots(conn)
    conn.execute("update slot_assignment set team = null")
    with pytest.raises(SlotsNotRecorded, match="names no team"):
        slots.our_slots(conn)
    conn.execute("update slot_assignment set team = 'group-b'")
    monkeypatch.setenv(slots.TEAM_SETTING, "group-a")
    with pytest.raises(SlotsNotRecorded, match="for team 'group-b', but ours is 'group-a'"):
        slots.our_slots(conn)


# --- The worker only confirms ---------------------------------------------------------------------

def test_the_worker_confirms_an_unchanged_answer(empty):
    slots.record(empty, ANSWER, NOW)
    logged = len(_log(empty))
    reordered = {**ANSWER, "slots": list(reversed(TEST_SLOTS))}
    assert slots.check(empty, reordered, NOW + timedelta(hours=1)) == "unchanged"
    assert len(_log(empty)) == logged
    assert slots.check(empty, ANSWER, AFTER) == "unchanged"
    assert empty.execute("select last_checked_at from slot_assignment").fetchone() == (AFTER,)
    tool, outcome, detail = _log(empty)[-1]
    assert (tool, outcome) == ("slots.record", "done") and "confirmed by the first check since the start" in detail
    assert slots.our_slots(empty, now=AFTER) == TEST_SLOTS


@pytest.mark.parametrize("at", [NOW + timedelta(hours=1), LOCK, AFTER])
def test_the_worker_never_takes_in_a_change(empty, at):
    """Before the start, at the first check after it (a wrong half from a bad deploy, say) and after it: the worker
    changes nothing, and its round fails until a person runs record."""
    slots.record(empty, ANSWER, NOW)
    logged = _log(empty)
    with pytest.raises(BadAssignment, match="different assignment.*only confirms.*python -m vending.slots record"):
        slots.check(empty, MOVED, at)
    assert empty.execute("select slots, last_checked_at from slot_assignment").fetchone() == (list(TEST_SLOTS), NOW)
    assert _log(empty) == logged
    assert slots.our_slots(empty) == TEST_SLOTS


def test_the_worker_doesnt_store_a_first_assignment(empty):
    with pytest.raises(BadAssignment, match="only confirms one.*python -m vending.slots record"):
        slots.check(empty, ANSWER, NOW)
    assert empty.execute("select count(*) from slot_assignment").fetchone() == (0,)


def test_a_wrong_first_check_after_the_start_doesnt_stick(empty):
    """The review's case on #146: the first check after the start gets the wrong half. The worker refuses it, the
    readers keep refusing (nothing confirmed), and the right answer an hour later confirms the assignment."""
    slots.record(empty, ANSWER, NOW)
    with pytest.raises(BadAssignment):
        slots.check(empty, MOVED, AFTER)
    with pytest.raises(SlotsNotRecorded, match="not been confirmed"):
        slots.our_slots(empty, now=AFTER)
    assert slots.check(empty, ANSWER, AFTER + timedelta(hours=1)) == "unchanged"
    assert slots.our_slots(empty, now=AFTER + timedelta(hours=1)) == TEST_SLOTS


def test_a_person_can_take_the_servers_answer_at_the_start_while_nothing_is_confirmed(empty):
    """The one change can land just before the start, with no check in between: a person stores it then."""
    slots.record(empty, ANSWER, NOW)
    assert slots.record(empty, MOVED, AFTER) == "changed"
    assert "the first check since the start" in _log(empty)[-1][2]
    assert slots.our_slots(empty, now=AFTER) == LEFT


def test_a_person_replaces_a_confirmed_assignment_only_with_replace(empty):
    slots.record(empty, ANSWER, NOW)
    slots.check(empty, ANSWER, AFTER)
    with pytest.raises(BadAssignment, match="locked at .*record --replace"):
        slots.record(empty, MOVED, AFTER + timedelta(hours=1))
    assert slots.record(empty, MOVED, AFTER + timedelta(hours=2), replace=True) == "replaced"
    assert "replaced by a person with --replace" in _log(empty)[-1][2]
    assert slots.our_slots(empty, now=AFTER + timedelta(hours=2)) == LEFT
    with pytest.raises(BadAssignment, match="locked at"):          # one --replace doesn't unlock it
        slots.record(empty, ANSWER, AFTER + timedelta(hours=3))


# --- A person confirms without the server -----------------------------------------------------------

def test_confirm_marks_the_stored_assignment_confirmed_after_the_start(empty):
    slots.record(empty, ANSWER, NOW)
    with pytest.raises(BadAssignment, match="nothing to confirm"):
        slots.confirm(empty, now=NOW + timedelta(hours=1))
    assert slots.confirm(empty, now=AFTER) == "confirmed"
    assert "confirmed without the server" in _log(empty)[-1][2]
    assert slots.our_slots(empty, now=AFTER) == TEST_SLOTS
    assert slots.confirm(empty, now=AFTER + timedelta(hours=1)) == "already confirmed"


def test_confirm_refuses_nothing_stored_or_a_bad_row(empty):
    with pytest.raises(BadAssignment, match="python -m vending.slots record"):
        slots.confirm(empty, now=AFTER)
    slots.record(empty, ANSWER, NOW)
    empty.execute("update slot_assignment set team = 'group-a'")
    with pytest.raises(BadAssignment, match="can't be ours"):
        slots.confirm(empty, now=AFTER)


def test_readers_use_the_database_clock_by_default(empty, monkeypatch):
    slots.record(empty, ANSWER, NOW)
    monkeypatch.setattr(slots, "clock", lambda conn: AFTER)
    with pytest.raises(SlotsNotRecorded, match="not been confirmed"):
        slots.our_slots(empty)


def test_the_real_clock_is_the_databases(conn, monkeypatch):
    monkeypatch.undo()                          # conftest's fixed clock off
    db_now = conn.execute("select clock_timestamp()").fetchone()[0]
    assert abs((slots.clock(conn) - db_now).total_seconds()) < 5
    assert slots.clock(conn).utcoffset() == timedelta(0)


def test_a_malformed_lock_setting_is_a_refusal_the_tools_understand(conn, monkeypatch):
    from vending.tools.machine_state import get_machine_state

    monkeypatch.setenv(slots.LOCK_SETTING, "3 Dec")
    with pytest.raises(SlotsNotRecorded, match="SLOTS_LOCK_AT"):
        slots.our_slots(conn)
    reply = get_machine_state(conn, now=NOW)
    assert not reply.success and "SLOTS_LOCK_AT" in reply.message


def test_the_tools_work_after_the_start_once_confirmed(conn, monkeypatch):
    """The whole path with a post-start clock: the worker's check confirms, then the tools read our slots."""
    from tests.conftest import store_poll
    from vending.planning import planner_config
    from vending.tools.machine_state import get_machine_state

    later = LOCK + timedelta(days=1)
    monkeypatch.setattr(slots, "clock", lambda c: later)
    store_poll(conn, "a", "2026-12-03T12:00:00+00:00", {"15": (300, 50)})
    store_poll(conn, "b", "2026-12-04T03:00:00+00:00", {"15": (300, 53)})
    assert not get_machine_state(conn, now=later).success
    assert slots.check(conn, ANSWER, LOCK + timedelta(minutes=1)) == "unchanged"
    state = get_machine_state(conn, now=later)
    assert state.success and [s.slot for s in state.data] == list(TEST_SLOTS)
    assert planner_config(conn).slots == TEST_SLOTS


@pytest.mark.parametrize("at", [NOW, AFTER])
@pytest.mark.parametrize("answer, why", MALFORMED)
def test_a_malformed_answer_changes_nothing_before_or_after_the_lock(empty, answer, why, at):
    with pytest.raises(BadAssignment, match=why):
        slots.record(empty, answer, at)
    assert empty.execute("select count(*) from slot_assignment").fetchone() == (0,)
    slots.record(empty, ANSWER, NOW)
    with pytest.raises(BadAssignment, match=why):
        slots.record(empty, answer, at)
    assert slots.our_slots(empty) == TEST_SLOTS
    assert empty.execute("select last_checked_at from slot_assignment").fetchone() == (NOW,)


@pytest.mark.parametrize("names, why", [
    (["15", "16", "11"], "names 3 different selections, not 20"),
    ([*TEST_SLOTS[:-1], "59"], "not a selection .*: 59"),
    (WHOLE_MACHINE, "names 40 different"),
])
def test_a_malformed_stored_row_is_refused_by_every_tool(conn, names, why):
    from vending.planning import NotReady, planner_config
    from vending.tools.machine_state import get_machine_state
    from vending.tools.sales_history import get_sales_history

    conn.execute("update slot_assignment set slots = %s", (names,))     # written by hand, not by record
    with pytest.raises(SlotsNotRecorded, match=f"can't be ours: .*{why}"):
        slots.our_slots(conn)
    for reply in [get_machine_state(conn, now=NOW), get_sales_history(conn, NOW - timedelta(days=7), NOW, None)]:
        assert not reply.success and "can't be ours" in reply.message
    with pytest.raises(NotReady, match="can't be ours"):
        planner_config(conn)


def test_a_stored_row_on_another_device_is_refused(conn):
    conn.execute("update slot_assignment set device_id = 'vmc2'")
    with pytest.raises(SlotsNotRecorded, match="'vmc2'"):
        slots.our_slots(conn)


def test_only_one_assignment_can_be_stored(conn):
    import psycopg
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute("insert into slot_assignment (device_id, slots, response) values ('vmc1', '{15}', '{}')")


# --- The tools use it -----------------------------------------------------------------------------

def test_every_tool_that_reads_slots_refuses_without_an_assignment(empty):
    from vending.planning import NotReady, planner_config
    from vending.tools.account_balance import get_account_balance
    from vending.tools.machine_state import get_machine_state
    from vending.tools.pnl import get_pnl
    from vending.tools.sales_history import get_sales_history

    from tests.test_ledger import open_ledger

    open_ledger(empty)
    replies = [get_machine_state(empty, now=NOW), get_sales_history(empty, NOW - timedelta(days=7), NOW, None),
               get_account_balance(empty), get_pnl(empty, date(2026, 11, 1), date(2026, 11, 7))]
    for reply in replies:
        assert not reply.success and "python -m vending.slots record" in reply.message
    with pytest.raises(NotReady, match="python -m vending.slots record"):
        planner_config(empty)


def test_the_tools_use_the_stored_slots_not_the_list_in_the_code(empty):
    """After a change before the lock, every tool follows the new assignment, whatever selections it names."""
    from tests.conftest import store_poll
    from vending.planning import planner_config
    from vending.tools.machine_state import get_machine_state
    from vending.tools.sales_history import get_sales_history

    slots.record(empty, ANSWER, NOW - timedelta(days=10))
    slots.record(empty, MOVED, NOW - timedelta(days=9))
    store_poll(empty, "a", "2026-11-05T12:00:00+00:00", {"11": (200, 10), "12": (250, 5), "15": (300, 50)})
    store_poll(empty, "b", "2026-11-12T12:00:00+00:00", {"11": (200, 17), "12": (250, 5), "15": (300, 60)})

    state = get_machine_state(empty, now=NOW)
    assert [s.slot for s in state.data] == list(LEFT)
    assert {s.slot: s.units_sold_in_window for s in state.data if s.slot in ("11", "12")} == {"11": 7, "12": 0}

    sales = get_sales_history(empty, NOW - timedelta(days=8), NOW, None)
    assert {r.slot for r in sales.data} <= set(LEFT) and "11" in {r.slot for r in sales.data}
    refused = get_sales_history(empty, NOW - timedelta(days=8), NOW, ["15"])
    assert not refused.success and "Not our slots: 15. Ours are 11, 12" in refused.message

    assert planner_config(empty).slots == LEFT


def test_the_ledger_counts_only_the_stored_slots(empty):
    from tests.conftest import store_poll
    from vending import ledger

    slots.record(empty, MOVED, NOW)
    store_poll(empty, "a", "2026-11-05T12:00:00+00:00", {"11": (200, 10), "16": (300, 50)})
    store_poll(empty, "b", "2026-11-06T12:00:00+00:00", {"11": (200, 12), "16": (300, 59)})
    start, end = datetime(2026, 11, 5, tzinfo=timezone.utc), datetime(2026, 11, 7, tzinfo=timezone.utc)
    assert ledger.units_sold(empty, start, end) == 2           # slot 16's 9 aren't in the stored list
    assert ledger.takings(empty, start, end).cents == 400


# --- The command ----------------------------------------------------------------------------------

class FakeBridge:
    def __init__(self, answer):
        self.answer = answer

    def slots(self):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def command(schema_url, monkeypatch):
    """`python -m vending.slots` against the test schema, with the bridge faked and the lock in the future."""
    from tests.conftest import open_in
    from vending import bridge, db

    monkeypatch.setattr(db, "connect", lambda: open_in(*schema_url))
    answers = {"answer": ANSWER}
    monkeypatch.setattr(bridge, "Bridge", lambda: FakeBridge(answers["answer"]))
    monkeypatch.setenv(slots.LOCK_SETTING, "2999-01-01T00:00:00+00:00")
    with open_in(*schema_url) as conn:
        conn.execute("delete from slot_assignment")
    return answers


def test_record_then_show(command, capsys):
    assert slots.main(["show"]) == 1
    assert "python -m vending.slots record" in capsys.readouterr().out
    assert slots.main(["record"]) == 0
    assert capsys.readouterr().out.startswith("recorded: team group-b, device vmc1, 20 slots: 15, 16")
    assert slots.main(["record"]) == 0
    assert capsys.readouterr().out.startswith("unchanged:")
    assert slots.main([]) == 0
    assert "20 slots" in capsys.readouterr().out


def test_record_follows_a_change_before_the_lock(command, capsys):
    slots.main(["record"])
    command["answer"] = MOVED
    assert slots.main(["record"]) == 0
    assert capsys.readouterr().out.splitlines()[-1].startswith("changed: team group-b, device vmc1, 20 slots: 11, 12")


def test_record_refuses_a_change_after_the_lock_with_exit_1(command, monkeypatch, capsys):
    slots.main(["record"])
    monkeypatch.setenv(slots.LOCK_SETTING, "2026-01-01T00:00:00-05:00")
    command["answer"] = MOVED
    assert slots.main(["record"]) == 1
    assert "different assignment, but ours locked at 2026-01-01T00:00:00-05:00" in capsys.readouterr().err


def test_an_answer_asked_for_before_the_lock_and_delivered_after_it_confirms_nothing(command, schema_url,
                                                                                      monkeypatch, capsys):
    """The clock crosses the lock while the bridge answers. That answer is the server's pre-start one: it
    mustn't become readable after the start, and the final assignment asked for after the lock still lands."""
    from tests.conftest import open_in

    lock = datetime(2026, 12, 3, 5, tzinfo=timezone.utc)
    monkeypatch.setenv(slots.LOCK_SETTING, lock.isoformat())
    clock = {"now": lock - timedelta(days=1)}
    monkeypatch.setattr(slots, "clock", lambda conn: clock["now"])
    assert slots.main(["record"]) == 0                       # the day before: ANSWER stored

    class SlowBridge:
        def slots(self):
            clock["now"] = lock + timedelta(seconds=1)       # asked a second before the lock, answered after
            return command["answer"]

    from vending import bridge
    clock["now"] = lock - timedelta(seconds=1)
    monkeypatch.setattr(bridge, "Bridge", SlowBridge)
    assert slots.main(["record"]) == 0
    with open_in(*schema_url) as conn:
        with pytest.raises(SlotsNotRecorded, match="final server assignment has not been confirmed"):
            slots.our_slots(conn)

    command["answer"] = MOVED                                 # the final assignment, asked for after the lock
    clock["now"] = lock + timedelta(minutes=5)
    monkeypatch.setattr(bridge, "Bridge", lambda: FakeBridge(command["answer"]))
    capsys.readouterr()
    assert slots.main(["record"]) == 0, capsys.readouterr().err
    with open_in(*schema_url) as conn:
        assert slots.our_slots(conn) == LEFT


def test_record_refuses_a_malformed_answer_with_exit_1(command, capsys):
    command["answer"] = {**ANSWER, "slots": ["11"]}
    assert slots.main(["record"]) == 1
    assert "names 1 different selections, not 20" in capsys.readouterr().err


def test_record_refuses_a_bad_lock_setting_with_exit_1(command, monkeypatch, capsys):
    monkeypatch.setenv(slots.LOCK_SETTING, "3 Dec")
    assert slots.main(["record"]) == 1
    assert "SLOTS_LOCK_AT" in capsys.readouterr().err


def test_record_reports_an_unreachable_bridge_with_exit_3(command, capsys):
    from vending.bridge import BridgeUnreachable

    command["answer"] = BridgeUnreachable("Is Tailscale connected?")
    assert slots.main(["record"]) == 3
    assert "Tailscale" in capsys.readouterr().err


def test_record_never_migrates(schema_url, monkeypatch, capsys):
    from tests.conftest import open_in
    from vending import db

    with open_in(*schema_url) as conn:
        conn.execute("delete from schema_migrations where version = '013_slot_assignment.sql'")
    monkeypatch.setattr(db, "connect", lambda: open_in(*schema_url))
    monkeypatch.setattr(db, "migrate", lambda conn: pytest.fail("record must never migrate"))
    assert slots.main(["record"]) == 2
    assert "python -m vending.db migrate" in capsys.readouterr().err


def test_check_is_the_workers_and_exits_1_on_a_change(command, capsys):
    assert slots.main(["check"]) == 1                          # nothing stored: a person records first
    assert "only confirms one" in capsys.readouterr().err
    assert slots.main(["record"]) == 0
    assert slots.main(["check"]) == 0
    assert capsys.readouterr().out.splitlines()[-1].startswith("unchanged: team group-b")
    command["answer"] = MOVED
    assert slots.main(["check"]) == 1
    assert "only confirms an unchanged one" in capsys.readouterr().err
    assert slots.main(["show"]) == 0 and "15, 16" in capsys.readouterr().out    # nothing changed


def test_confirm_and_replace_from_the_command_line(command, monkeypatch, capsys):
    assert slots.main(["record"]) == 0
    assert slots.main(["confirm"]) == 1                        # the lock is in 2999
    assert "nothing to confirm" in capsys.readouterr().err
    monkeypatch.setenv(slots.LOCK_SETTING, "2026-11-01T00:00:00-05:00")
    capsys.readouterr()
    assert slots.main(["confirm"]) == 0                        # recorded at TEST_NOW, after this lock
    assert capsys.readouterr().out.startswith("already confirmed: team group-b")
    command["answer"] = MOVED
    assert slots.main(["record"]) == 1
    assert "record --replace" in capsys.readouterr().err
    assert slots.main(["record", "--replace"]) == 0
    assert capsys.readouterr().out.splitlines()[-1].startswith("replaced: team group-b, device vmc1, 20 slots: 11")
    with pytest.raises(SystemExit):
        slots.main(["check", "--replace"])
