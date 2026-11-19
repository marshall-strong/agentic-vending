"""The worker's live reads around our Friday visit. The bridge is faked; nothing here goes near the real one.

Time is a fake clock that only `sleep` moves, so a whole week of the loop runs in a moment."""

import socket
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("psycopg")

from tests.conftest import store_poll  # noqa: E402
from tests.test_evadts import SAMPLE, build  # noqa: E402
from tests.test_sync_requests import fulfilled, pending, receipt  # noqa: E402
from vending import settings, visit_polls, worker  # noqa: E402
from vending.bridge import BridgeAnswered, BridgeNoAnswer, BridgeUnreachable  # noqa: E402
from vending.purchase_requests import record, store  # noqa: E402
from vending.visit_polls import READ, VISIT_OVER, Reader, Schedule  # noqa: E402

ET = ZoneInfo("America/New_York")
# Thu 3 Dec 2026 is the first day of the competition, so Fri 4 Dec is the first regular Friday.
THU, FRI, SAT, NEXT_FRI = (datetime(2026, 12, d, tzinfo=ET) for d in (3, 4, 5, 11))
SCHEDULE = Schedule(timedelta(minutes=1), datetime(2026, 1, 1, 14, 45).time(), datetime(2026, 1, 1, 19, 0).time())


def at(day, hour, minute=0, second=0):
    return day.replace(hour=hour, minute=minute, second=second)


class Clock:
    """Time moves only when the loop sleeps. Past `stop` the sleep interrupts, as Ctrl+C would."""

    def __init__(self, start, stop):
        self.now, self.stop = start.astimezone(timezone.utc), stop.astimezone(timezone.utc)

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)
        if self.now >= self.stop:
            raise KeyboardInterrupt


@pytest.fixture(autouse=True)
def _no_slot_lock(monkeypatch):
    """The worker's extra rounds after the slot lock are not what these tests are about."""
    monkeypatch.setattr(worker, "_slot_lock", lambda: None)


def run_worker(clock, reader, job=lambda: 0, sleep=None, port=0):
    """The worker's real loop on the fake clock, with one trivial job. Returns when the clock reaches its stop."""
    return worker.main(["--every", "60", "--port", str(port)], jobs=[("a", job)],
                       sleep=sleep or clock.sleep, log=lambda _: None, reads=reader, clock=clock)


def reader_of(calls, outcome=lambda n: READ, log=lambda _: None, **kw):
    """A reader whose reads are the fake `outcome(n)` (a code, or an exception to raise), noting when each began."""
    def run(now):
        calls.append(now.astimezone(ET))
        result = outcome(len(calls))
        if isinstance(result, BaseException):
            raise result
        return result
    return Reader(SCHEDULE, run, log, **kw)


# --- When the reads happen ---------------------------------------------------------

def test_reads_happen_every_minute_inside_the_window_on_fridays_and_nowhere_else():
    calls = []
    clock = Clock(at(THU, 12), at(SAT, 1))
    run_worker(clock, reader_of(calls))
    # From 2:45 PM to 6:59 PM on the Friday, once a minute: 255 reads. Thursday's afternoon has none, and
    # neither has the Friday morning, the minute the window closes, or the evening after it.
    assert calls == [at(FRI, 14, 45) + timedelta(minutes=n) for n in range(255)]
    assert calls[-1] == at(FRI, 18, 59)


def test_the_window_and_interval_are_settings(monkeypatch):
    monkeypatch.setattr(settings, "_FILE", {})
    assert visit_polls.schedule() == SCHEDULE
    monkeypatch.setenv(visit_polls.EVERY_SETTING, "10")
    monkeypatch.setenv(visit_polls.FROM_SETTING, "13:00")
    monkeypatch.setenv(visit_polls.TO_SETTING, "17:30")
    assert visit_polls.schedule() == Schedule(timedelta(minutes=10), datetime(2026, 1, 1, 13).time(),
                                              datetime(2026, 1, 1, 17, 30).time())
    calls = []
    run_worker(Clock(at(FRI, 12), at(FRI, 20)), Reader(visit_polls.schedule(), lambda now: calls.append(now) or READ,
                                                         lambda _: None))
    assert [c.astimezone(ET).strftime("%H:%M") for c in calls] == [
        f"{h}:{m:02}" for h in range(13, 18) for m in range(0, 60, 10) if (h, m) < (17, 30)]


@pytest.mark.parametrize("name, value", [
    (visit_polls.EVERY_SETTING, "often"), (visit_polls.EVERY_SETTING, "-1"),
    (visit_polls.FROM_SETTING, "afternoon"), (visit_polls.TO_SETTING, "14:00"),   # the end before the start
])
def test_a_wrong_setting_turns_the_reads_off_and_never_stops_the_worker(monkeypatch, name, value):
    monkeypatch.setattr(settings, "_FILE", {})
    monkeypatch.setenv(name, value)
    said = []
    assert visit_polls.default_reader(said.append) is None
    assert said and "off" in said[0] and name in said[0]


def test_an_interval_of_zero_turns_the_reads_off(monkeypatch):
    monkeypatch.setattr(settings, "_FILE", {})
    monkeypatch.setenv(visit_polls.EVERY_SETTING, "0")
    assert visit_polls.schedule() is None and visit_polls.default_reader(lambda _: None) is None


def test_the_window_follows_eastern_time_through_the_clock_change():
    # Fri 30 Oct is still daylight time and Fri 4 Dec is standard time. 2:45 PM Eastern is 18:45 UTC in
    # the first and 19:45 UTC in the second, and the window follows the wall clock.
    for friday in (datetime(2026, 10, 30, tzinfo=ET), FRI):
        assert not visit_polls.window_open(SCHEDULE, at(friday, 14, 44))
        assert visit_polls.window_open(SCHEDULE, at(friday, 14, 45))
        assert visit_polls.window_open(SCHEDULE, at(friday, 18, 59, 59))
        assert not visit_polls.window_open(SCHEDULE, at(friday, 19))
    assert visit_polls.next_open(SCHEDULE, at(FRI, 19, 1)) == at(NEXT_FRI, 14, 45).astimezone(timezone.utc)


# --- Stopping early -----------------------------------------------------------------

def test_the_reads_stop_for_the_day_once_the_visit_is_over_and_come_back_next_friday():
    calls = []
    clock = Clock(at(FRI, 14), at(NEXT_FRI, 16))
    # The 20th try (3:04 PM) finds our receipt in and a read after the door closed stored.
    run_worker(clock, reader_of(calls, lambda n: VISIT_OVER if n == 20 else READ))
    # No try for the rest of that Friday, and a fresh start the next one (2:45 to 3:59 PM, 75 reads).
    assert [c for c in calls if c.date() == FRI.date()] == [at(FRI, 14, 45) + timedelta(minutes=n) for n in range(20)]
    assert [c for c in calls if c.date() > FRI.date()] == [at(NEXT_FRI, 14, 45) + timedelta(minutes=n)
                                                           for n in range(75)]


# --- A failed read ------------------------------------------------------------------

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_a_failed_read_does_not_fail_the_round_or_health():
    failures = (BridgeAnswered(409, "busy"), BridgeNoAnswer("TimeoutError: timed out"), BridgeUnreachable("down"),
                RuntimeError("the database is missing migrations"))
    calls, said, health, rounds = [], [], [], []
    port = free_port()
    clock = Clock(at(FRI, 14, 40), at(FRI, 16, 45))

    def sleep(seconds):
        """Looks at /health, as a person or Azure would, at every wake-up of the loop."""
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health") as response:
            health.append((response.status, response.read()))
        clock.sleep(seconds)

    # Every read fails, whichever way a read can.
    reader = reader_of(calls, lambda n: failures[n % len(failures)], said.append)
    run_worker(clock, reader, job=lambda: rounds.append(1) or 0, sleep=sleep, port=port)
    assert len(calls) >= 20 and any("409" in line for line in said)    # reads were tried, and failed
    assert len(rounds) == 3                                            # the rounds at 2:40, 3:40 and 4:40 PM all ran
    assert health and all(status == 200 and b'"status": "ok"' in body for status, body in health)
    assert all(b"visit" not in body and b"busy" not in body and b"Bridge" not in body for _, body in health)   # nor does /health


def test_three_failures_in_a_row_wait_five_minutes_and_each_skip_is_logged():
    calls, said = [], []
    # Fail, fail, fail, fail, then it works again: a 409, a timeout, the bridge down, a 409.
    errors = [BridgeAnswered(409, "busy"), BridgeNoAnswer("TimeoutError"), BridgeUnreachable("down"),
              BridgeAnswered(409, "busy")]
    reader = reader_of(calls, lambda n: errors[n - 1] if n <= 4 else READ, said.append)
    run_worker(Clock(at(FRI, 14, 40), at(FRI, 15, 5)), reader)
    minutes = [c.strftime("%H:%M") for c in calls]
    # A minute apart for the first three tries; 5 minutes after the third failure (14:47 -> 14:52),
    # 5 more after the fourth (14:52 -> 14:57), then a minute apart again once one read works.
    assert minutes[:8] == ["14:45", "14:46", "14:47", "14:52", "14:57", "14:58", "14:59", "15:00"]
    skips = [line for line in said if "skipped" in line]
    assert len(skips) == 4 and all(line.startswith("visit reads: skipped") for line in skips)
    assert "1 failed in a row, next try in 1 min" in skips[0] and "409" in skips[0]
    assert "3 failed in a row, next try in 5 min" in skips[2]
    assert "4 failed in a row, next try in 5 min" in skips[3]


def test_a_read_still_running_is_never_started_again_and_the_tick_isnt_retried_in_a_loop():
    calls, said, release = [], [], threading.Event()

    def run(now):
        calls.append(now)
        release.wait(5)
        return READ

    reader = Reader(SCHEDULE, run, said.append, timeout=0.05)
    first = at(FRI, 14, 45).astimezone(timezone.utc)
    reader.tick(first)                                    # the read hangs and is left to finish
    assert len(calls) == 1 and "left to finish" in said[-1]    # a skipped minute, not a retry
    assert reader.next_due(first) == first + timedelta(minutes=1)
    reader.tick(first + timedelta(seconds=30))            # too soon: nothing happens
    reader.tick(first + timedelta(minutes=1))
    assert len(calls) == 1 and "still running" in said[-1]
    release.set()
    reader.running["visit_read"].join(5)
    reader.tick(first + timedelta(minutes=3))
    assert len(calls) == 2


def test_a_hung_read_makes_the_loop_skip_a_minute_at_a_time_not_spin():
    calls, said, release, sleeps = [], [], threading.Event(), []

    def run(now):
        calls.append(now)
        release.wait(10)
        return READ

    clock = Clock(at(FRI, 14, 50), at(FRI, 15, 0))

    def sleep(seconds):
        sleeps.append(seconds)
        assert len(sleeps) < 100, "the loop is spinning"
        clock.sleep(seconds)

    reader = Reader(SCHEDULE, run, said.append, timeout=0.02)
    try:
        run_worker(clock, reader, sleep=sleep)
    finally:
        release.set()
    # One try, which hung, and then a skipped minute each: the earlier read is never started again.
    assert len(calls) == 1
    assert sum("still running" in line for line in said) >= 8
    assert min(s for s in sleeps if s) >= 60


def test_a_tick_outside_the_window_reads_nothing():
    calls = []
    reader = reader_of(calls)
    for moment in (at(FRI, 14, 44, 59), at(FRI, 19), at(THU, 16), at(SAT, 16)):
        reader.tick(moment.astimezone(timezone.utc))
    assert calls == []


# --- What a step does, against a real database and a fake bridge ----------------------------

POLL_AT = lambda h, m, s=5: at(FRI, h, m, s).astimezone(timezone.utc)   # noqa: E731
DOOR = {"door_opened_at": "2026-12-04T20:12:00+00:00", "door_closed_at": "2026-12-04T20:31:00+00:00"}   # 15:12-15:31 ET


class Bridge:
    """The bridge as `step` uses it: a request that is pending, then fulfilled; reads that land in history."""

    def __init__(self, answers):
        self.answers, self.polls, self.stored, self.asked = answers, 0, [], []

    def purchase_request(self, request_id):
        self.asked.append(request_id)
        return self.answers[request_id].pop(0) if len(self.answers[request_id]) > 1 else self.answers[request_id][0]

    def poll(self):
        self.polls += 1
        self.stored.append((f"live{self.polls}", self.taken))
        return b"DXS*"

    def history(self, day):
        return [{"session_id": s, "started_at": t.isoformat(), "device_id": "vmc1"} for s, t in self.stored]

    def raw(self, session_id):
        return build(SAMPLE)


@pytest.fixture
def machine(conn, monkeypatch):
    from contextlib import nullcontext

    monkeypatch.setattr(visit_polls.db, "connect", lambda: nullcontext(conn))
    record(conn, "r1", at(THU, 20).astimezone(timezone.utc), False, {"off_cycle": False, "lines": []})
    bridge = Bridge({"r1": [pending("r1"), fulfilled("r1", receipt(slot_reassigned=False), fulfilled_at="2026-12-04T20:40:00Z",
                                                  **DOOR)]})
    monkeypatch.setattr(visit_polls, "Bridge", lambda: bridge)
    return bridge


def step(bridge, now):
    bridge.taken = now + timedelta(seconds=5)       # when the machine is read: just after the step starts
    return visit_polls.step(now, lambda _: None)


def test_the_visit_is_over_only_when_a_read_after_the_door_closed_is_stored(conn, machine):
    # 3:25 PM: the request is pending, so the machine is read (and the read is stored by the step).
    assert step(machine, POLL_AT(15, 25)) == READ and machine.polls == 1
    # 3:40 PM: the receipt is in (door closed 3:31), but the last read is from before it. Read again.
    assert step(machine, POLL_AT(15, 40)) == READ and machine.polls == 2
    assert conn.execute("select status, door_closed_at from purchase_requests").fetchone() == (
        "fulfilled", datetime(2026, 12, 4, 20, 31, tzinfo=timezone.utc))
    # 3:41 PM: a read after the close is stored and our request is fulfilled. Done: no more reads.
    assert step(machine, POLL_AT(15, 41)) == VISIT_OVER and machine.polls == 2


@pytest.mark.parametrize("read_at, over", [
    ((15, 31, 30), False),     # inside the close minute: the door may not have closed yet
    ((15, 32, 0), True),       # after the close minute
    ((15, 20, 0), False),      # during the visit
])
def test_only_a_read_after_the_close_minute_ends_the_visit(conn, read_at, over):
    record(conn, "r1", at(THU, 20).astimezone(timezone.utc), False, {})
    store(conn, "r1", fulfilled("r1", receipt(), fulfilled_at="2026-12-04T20:40:00Z", **DOOR), POLL_AT(15, 40))
    store_poll(conn, "s1", POLL_AT(*read_at).isoformat(), {"15": (200, 100)})
    assert visit_polls.visit_over(conn, POLL_AT(16, 0)) is over


def test_what_does_not_end_a_visit(conn):
    record(conn, "r1", at(THU, 20).astimezone(timezone.utc), False, {})
    store_poll(conn, "late", POLL_AT(17, 0).isoformat(), {"15": (200, 100)})
    assert not visit_polls.visit_over(conn, POLL_AT(17, 5))                                  # nothing fulfilled
    store(conn, "r1", pending("r1"), POLL_AT(15, 0))
    assert not visit_polls.visit_over(conn, POLL_AT(17, 5))                                  # still pending
    store(conn, "r1", fulfilled("r1", receipt(), fulfilled_at="2026-12-03T20:40:00Z",
                                door_opened_at="2026-12-03T20:12:00+00:00",
                                door_closed_at="2026-12-03T20:31:00+00:00"), POLL_AT(15, 0))
    assert not visit_polls.visit_over(conn, POLL_AT(17, 5))                                  # yesterday's visit
    store(conn, "r1", fulfilled("r1", receipt(), fulfilled_at="2026-12-04T20:40:00Z", **DOOR), POLL_AT(15, 0))
    assert visit_polls.visit_over(conn, POLL_AT(17, 5))
    conn.execute("update polls set device_id = 'someone-else'")
    assert not visit_polls.visit_over(conn, POLL_AT(17, 5))                                  # another device's read
    conn.execute("update polls set device_id = 'vmc1', crc_ok = false")
    assert not visit_polls.visit_over(conn, POLL_AT(17, 5))                                  # a read that failed its checksum


def test_a_receipt_with_no_door_times_is_dated_by_fulfilled_at(conn):
    record(conn, "r1", at(THU, 20).astimezone(timezone.utc), False, {})
    store(conn, "r1", fulfilled("r1", receipt(), fulfilled_at="2026-12-04T20:40:00Z"), POLL_AT(15, 0))
    store_poll(conn, "s1", POLL_AT(15, 39).isoformat(), {"15": (200, 100)})
    assert not visit_polls.visit_over(conn, POLL_AT(17, 0))
    store_poll(conn, "s2", POLL_AT(15, 41).isoformat(), {"15": (200, 100)})
    assert visit_polls.visit_over(conn, POLL_AT(17, 0))


def test_a_busy_bridge_stops_the_step_before_anything_is_stored(conn, machine):
    machine.poll = lambda: (_ for _ in ()).throw(BridgeAnswered(409, "busy"))
    with pytest.raises(BridgeAnswered):
        step(machine, POLL_AT(15, 25))
    assert conn.execute("select count(*) from polls").fetchone()[0] == 0
