"""Tests for the write gate and the halt switch."""

import psycopg
import pytest

from tests.conftest import open_in
from vending import halt

from vending.config import is_halted, set_halted
from vending.pricing import Refusal
from vending.tools import WriteReply
from vending.write_gate import Refused, run_write


def write_marker(conn):
    """A stand-in write: one row in a scratch table."""
    conn.execute("insert into scratch values (1)")
    return WriteReply(success=True, message="written", count=1)


@pytest.fixture
def scratch(conn):
    conn.execute("create table scratch (n int)")
    return conn


def log(conn):
    return conn.execute("select tool, outcome, detail from action_log where tool != 'halt' order by id").fetchall()


def rows(conn):
    return conn.execute("select count(*) from scratch").fetchone()[0]


def test_halted_from_the_start(conn):
    assert is_halted(conn)


def test_halted_write_is_refused_logged_and_not_run(scratch):
    with pytest.raises(Refused) as caught:
        run_write(scratch, "t", {"a": 1}, write_marker)
    assert [r.code for r in caught.value.refusals] == ["halted"]
    assert rows(scratch) == 0
    assert log(scratch) == [("t", "refused", "halted: " + caught.value.refusals[0].message)]


def test_write_and_log_commit_together(scratch):
    set_halted(scratch, False, "test")
    assert run_write(scratch, "t", {"a": 1}, write_marker).success
    assert rows(scratch) == 1
    assert log(scratch) == [("t", "done", None)]
    assert scratch.execute("select result->>'message' from action_log where tool = 't'").fetchone()[0] == "written"


def test_refusal_rolls_back_the_write(scratch):
    set_halted(scratch, False, "test")

    def refuse_after_writing(conn):
        write_marker(conn)
        raise Refused([Refusal("below_floor", "too cheap", "15"), Refusal("price_off_step", "odd", "16")])

    with pytest.raises(Refused):
        run_write(scratch, "t", {}, refuse_after_writing)
    assert rows(scratch) == 0
    assert log(scratch) == [("t", "refused", "below_floor: too cheap; price_off_step: odd")]


def test_crash_rolls_back_and_is_logged(scratch):
    set_halted(scratch, False, "test")

    def crash(conn):
        write_marker(conn)
        raise RuntimeError("bridge timed out")

    with pytest.raises(RuntimeError):
        run_write(scratch, "t", {}, crash)
    assert rows(scratch) == 0
    assert log(scratch) == [("t", "failed", "RuntimeError: bridge timed out")]


def test_missing_halt_row_counts_as_halted(scratch):
    set_halted(scratch, False, "test")
    scratch.execute("delete from config where key = 'halted'")
    with pytest.raises(Refused):
        run_write(scratch, "t", {}, write_marker)


def test_halt_waits_visibly_for_an_in_flight_write(schema_url, conn, capsys):
    set_halted(conn, False, "test")
    with open_in(*schema_url) as writer, writer.transaction():
        assert not is_halted(writer, lock=True)  # a write holding the lock
        with pytest.raises(psycopg.errors.LockNotAvailable):
            set_halted(conn, True, "kevin", lock_timeout_ms=100)
    halt.flip(conn, True, "kevin")  # lock released: goes straight through
    assert is_halted(conn) and capsys.readouterr().out == ""


def test_halt_needs_a_name_to_flip(capsys):
    with pytest.raises(SystemExit):
        halt.main(["on"])
    assert "--by NAME is required" in capsys.readouterr().err


def test_halt_flip_is_logged(conn):
    set_halted(conn, False, "kevin")
    set_halted(conn, True, "kevin")
    assert is_halted(conn)
    assert conn.execute("select detail from action_log where tool = 'halt' order by id").fetchall() == [
        ("halt turned off by kevin",), ("halt turned on by kevin",)]
