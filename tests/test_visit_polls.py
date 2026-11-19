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
from vending.bridge import BridgeAnswered, BridgeNoAnswer, BridgeUnreachable, LiveRead  # noqa: E402
from vending.purchase_requests import record, store  # noqa: E402
from vending.visit_polls import READ, VISIT_OVER, Reader, Schedule  # noqa: E402

ET = ZoneInfo("America/New_York")
# Thu 3 Dec 2026 is the first day of the competition, so Fri 4 Dec is the first regular Friday.
THU, FRI, SAT, NEXT_FRI = (datetime(2026, 12, d, tzinfo=ET) for d in (3, 4, 5, 11))
SCHEDULE = Schedule(timedelta(minutes=1), datetime(2026, 1, 1, 14, 45).time(), datetime(2026, 1, 1, 19, 0).time())
DATES_SETTING = "VISIT_POLL_DATES"
MON_FILL = datetime(2026, 11, 30, tzinfo=ET)     # the day-one fill is about this day, a Monday
SKIPPED = getattr(visit_polls, "SKIPPED", 3)     # what a step returns when it sent no POST and nothing failed


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
    # Under a minute, or not a finite number: never rounded to zero, never an OverflowError at startup.
    (visit_polls.EVERY_SETTING, "0.5"), (visit_polls.EVERY_SETTING, "1e-9"), (visit_polls.EVERY_SETTING, "inf"),
    (visit_polls.EVERY_SETTING, "nan"), (visit_polls.EVERY_SETTING, "1e30"),
    # Finite and inside timedelta's range, but too big for a datetime to add to: capped at a day (1440 minutes).
    (visit_polls.EVERY_SETTING, "1e12"), (visit_polls.EVERY_SETTING, "1441"),
    (DATES_SETTING, "30 Nov"), (DATES_SETTING, "2026-11-30, soon"), (DATES_SETTING, "2026-13-01"),
])
def test_a_wrong_setting_turns_the_reads_off_and_never_stops_the_worker(monkeypatch, name, value):
    monkeypatch.setattr(settings, "_FILE", {})
    monkeypatch.setenv(name, value)
    said = []
    assert visit_polls.default_reader(said.append) is None
    assert said and "off" in said[0] and name in said[0]


def test_the_longest_interval_is_a_day_and_the_scheduler_can_add_it_to_a_time(monkeypatch):
    monkeypatch.setattr(settings, "_FILE", {})
    monkeypatch.setenv(visit_polls.EVERY_SETTING, "1440")
    sched = visit_polls.schedule()
    assert sched.every == timedelta(days=1)
    reader = Reader(sched, lambda now: READ, lambda _: None)
    reader.tick(at(FRI, 15).astimezone(timezone.utc))        # adds the interval to a datetime: no OverflowError
    assert reader.not_before == at(FRI, 15).astimezone(timezone.utc) + timedelta(days=1)


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


def test_a_read_that_runs_past_the_hourly_round_does_not_take_the_worker_down():
    rounds = []
    clock = Clock(at(FRI, 14, 40, 5), at(FRI, 15, 50))      # the rounds are due at 3:40:05 PM and 4:40:10 PM

    def run(now):
        clock.now += timedelta(seconds=10)                    # every read takes 10 s
        return READ

    def sleep(seconds):
        if seconds < 0:                                       # as `time.sleep` does
            raise ValueError("sleep length must be non-negative")
        clock.sleep(seconds)

    # The read that starts at 3:40:00 PM, five seconds before the round is due, ends five seconds after it.
    run_worker(clock, Reader(SCHEDULE, run, lambda _: None), job=lambda: rounds.append(clock.now) or 0, sleep=sleep)
    assert len(rounds) == 2 and rounds[1] - rounds[0] < timedelta(minutes=61)    # the round ran, right after the read


# --- Extra days for the reads (the day-one fill) ----------------------------------------

def dates_schedule(monkeypatch, dates):
    monkeypatch.setattr(settings, "_FILE", {})
    monkeypatch.setenv(DATES_SETTING, dates)
    return visit_polls.schedule()


def test_the_reads_also_run_on_the_dates_in_the_setting_and_nowhere_else(monkeypatch):
    sched = dates_schedule(monkeypatch, "2026-11-30, 2026-12-14")
    assert visit_polls.window_open(sched, at(MON_FILL, 15)) and not visit_polls.window_open(sched, at(MON_FILL, 14, 44))
    assert not visit_polls.window_open(sched, at(MON_FILL, 19))
    assert not visit_polls.window_open(sched, at(MON_FILL + timedelta(days=7), 15))     # a Monday not in the list
    assert visit_polls.window_open(sched, at(FRI, 15))                                   # Fridays stay
    # Eastern dates: 7 PM on the 30th is already the 1st in UTC, and it is no more in the window than 7 PM on a Friday.
    assert not visit_polls.window_open(sched, at(MON_FILL, 19).astimezone(timezone.utc))
    assert visit_polls.next_open(sched, at(datetime(2026, 11, 27, tzinfo=ET), 19, 1)) == \
        at(MON_FILL, 14, 45).astimezone(timezone.utc)
    assert visit_polls.next_open(sched, at(MON_FILL, 19, 1)) == at(FRI, 14, 45).astimezone(timezone.utc)
    monkeypatch.setenv(DATES_SETTING, "")
    assert visit_polls.schedule().dates == frozenset() and not visit_polls.window_open(visit_polls.schedule(), at(MON_FILL, 15))


def test_a_listed_date_gets_the_same_reads_and_the_same_stop_rule(monkeypatch):
    sched = dates_schedule(monkeypatch, "2026-11-30")
    calls = []

    def run(now):
        calls.append(now.astimezone(ET))
        return VISIT_OVER if len(calls) == 20 else READ

    run_worker(Clock(at(MON_FILL, 12) - timedelta(days=1), at(MON_FILL, 12) + timedelta(days=2)),
               Reader(sched, run, lambda _: None))
    # Sunday has none; Monday's reads run from 2:45 PM until the 20th finds the visit over; Tuesday has none.
    assert calls == [at(MON_FILL, 14, 45) + timedelta(minutes=n) for n in range(20)]


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


class Now:
    """The clock `step` looks at. Only the fake bridge moves it, the way a slow answer would."""

    def __init__(self, moment):
        self.now = moment

    def __call__(self):
        return self.now


class Bridge:
    """The bridge as `step` uses it: a request that is pending, then fulfilled; reads that land in history.
    Each call takes the time in `takes` (a receipt read) or 8.5 s (the machine read) on the fake clock."""

    def __init__(self, answers, clock):
        self.answers, self.polls, self.stored, self.asked = answers, 0, [], []
        self.clock, self.refresh_takes, self.sends_session_id = clock, timedelta(0), True
        self.header_prefix = "live"             # what the header's id starts with; the history's ids start with "live"
        self.cached, self.age_seconds = False, 0.0

    def purchase_request(self, request_id):
        self.asked.append(request_id)
        self.clock.now += self.refresh_takes
        return self.answers[request_id].pop(0) if len(self.answers[request_id]) > 1 else self.answers[request_id][0]

    def poll(self):
        self.polls += 1
        session_id = f"live{self.polls}"
        self.stored.append((session_id, self.clock.now))
        self.clock.now += timedelta(seconds=8.5)
        named = f"{self.header_prefix}{self.polls}" if self.sends_session_id else None
        read = LiveRead(b"DXS*", named)
        if "cached" in LiveRead._fields:
            read = read._replace(cached=self.cached, age_seconds=self.age_seconds)
        return read

    def history(self, day):
        return [{"session_id": s, "started_at": t.isoformat(), "device_id": "vmc1"} for s, t in self.stored]

    def raw(self, session_id):
        return build(SAMPLE)


@pytest.fixture(autouse=True)
def _fresh_post_record(monkeypatch):
    """When the machine was last asked to read is process-wide state: every test starts with none."""
    monkeypatch.setattr(visit_polls, "_last_post", None, raising=False)


@pytest.fixture
def machine(conn, monkeypatch):
    from contextlib import nullcontext

    monkeypatch.setattr(visit_polls.db, "connect", lambda: nullcontext(conn))
    record(conn, "r1", at(THU, 20).astimezone(timezone.utc), False, {"off_cycle": False, "lines": []})
    clock = Now(POLL_AT(15, 0))
    bridge = Bridge({"r1": [pending("r1"), fulfilled("r1", receipt(slot_reassigned=False), fulfilled_at="2026-12-04T20:40:00Z",
                                                  **DOOR)]}, clock)
    monkeypatch.setattr(visit_polls, "Bridge", lambda: bridge)
    # `raising=False`: the clock is what `step` asks for the time of its POST, after the receipts are read.
    monkeypatch.setattr(visit_polls, "_utcnow", clock, raising=False)
    return bridge


def step(bridge, now):
    bridge.clock.now = now + timedelta(seconds=5)   # the machine is read just after the step starts
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


# --- The window and the spacing are checked at the POST, not at the start of the step ------

def test_no_read_is_sent_once_the_window_has_closed_while_the_receipts_were_read(machine):
    machine.refresh_takes = timedelta(seconds=10)
    machine.clock.now = at(FRI, 18, 59, 55).astimezone(timezone.utc)
    result = visit_polls.step(machine.clock.now, lambda _: None)       # began inside the window; 19:00:05 at the POST
    assert machine.polls == 0
    assert result == SKIPPED


def test_two_reads_are_never_sent_closer_than_the_interval_whatever_the_receipts_took(machine):
    said = []
    machine.refresh_takes = timedelta(seconds=50)
    machine.clock.now = POLL_AT(15, 20, 0)
    visit_polls.step(machine.clock.now, said.append)                    # slow: the POST goes at 3:20:50 PM
    machine.refresh_takes = timedelta(seconds=30)
    machine.clock.now = POLL_AT(15, 21, 0)
    result = visit_polls.step(machine.clock.now, said.append)           # the next step's POST would be at 3:21:30 PM
    assert machine.polls == 1                                           # 40 s after the last one: not sent
    assert result == SKIPPED and any("skipped" in line for line in said)
    machine.refresh_takes = timedelta(0)
    machine.clock.now = POLL_AT(15, 21, 50)
    assert visit_polls.step(machine.clock.now, said.append) == READ and machine.polls == 2    # a minute after 3:20:50
    gaps = [b - a for (_, a), (_, b) in zip(machine.stored, machine.stored[1:])]
    assert all(gap >= timedelta(minutes=1) for gap in gaps)


def test_a_step_that_sent_nothing_is_not_a_failure_and_the_next_try_is_an_interval_after_the_last_post(monkeypatch):
    calls = []
    first = at(FRI, 15, 0).astimezone(timezone.utc)

    def run(now):
        calls.append(now)
        monkeypatch.setattr(visit_polls, "_last_post", now + timedelta(seconds=20), raising=False)
        return SKIPPED if len(calls) == 1 else READ

    reader = Reader(SCHEDULE, run, lambda _: None)
    reader.tick(first)
    assert reader.failures == 0 and reader.not_before is not None
    assert reader.next_due(first) == first + timedelta(seconds=20) + timedelta(minutes=1)
    reader.tick(first + timedelta(minutes=1))                            # too soon after the POST at 3:00:20
    assert len(calls) == 1
    reader.tick(first + timedelta(minutes=1, seconds=20))
    assert len(calls) == 2 and reader.failures == 0


def test_the_back_off_counts_from_the_post_that_failed_not_from_the_start_of_the_step(conn, machine, monkeypatch):
    said = []
    # Reading the receipts takes 45 s, so each POST goes out 45 s into its step, and each is a busy answer.
    monkeypatch.setattr(visit_polls, "refresh_pending",
                        lambda *a: setattr(machine.clock, "now", machine.clock.now + timedelta(seconds=45)))
    machine.poll = lambda: (_ for _ in ()).throw(BridgeAnswered(409, "busy"))
    reader = Reader(SCHEDULE, lambda now: visit_polls.step(now, said.append), said.append)
    for minute in (25, 27, 29):
        machine.clock.now = POLL_AT(15, minute, 0)
        reader.tick(POLL_AT(15, minute, 0))
    assert reader.failures == 3
    assert reader.not_before == POLL_AT(15, 29, 45) + timedelta(minutes=5)      # five whole minutes after the POST


def test_a_failure_before_any_post_backs_off_from_when_it_happened(conn, machine, monkeypatch):
    said = []
    monkeypatch.setattr(settings, "_FILE", {})
    for name in (visit_polls.EVERY_SETTING, visit_polls.FROM_SETTING, visit_polls.TO_SETTING, DATES_SETTING):
        monkeypatch.delenv(name, raising=False)

    def slow_and_broken(conn, now):
        machine.clock.now += timedelta(seconds=45)
        raise RuntimeError("the database went away")

    monkeypatch.setattr(visit_polls, "visit_over", slow_and_broken)
    reader = visit_polls.default_reader(said.append)
    for minute in (25, 27, 29):
        machine.clock.now = POLL_AT(15, minute, 0)
        reader.tick(POLL_AT(15, minute, 0))
    assert machine.polls == 0 and reader.failures == 3                       # no POST was ever sent
    assert reader.not_before == POLL_AT(15, 29, 45) + timedelta(minutes=5)   # from the failure, not the step's start


# --- A read has to be stored usable, or it counts as a failure ----------------------------------

def _history_fails(machine):
    def history(day):
        raise BridgeAnswered(500, "history is down")
    machine.history = history


def _fetch_fails(machine):
    def raw(session_id):
        raise BridgeAnswered(502, "bad gateway")
    machine.raw = raw


@pytest.mark.parametrize("break_it", [
    _history_fails,                                                                    # can't list the day
    _fetch_fails,                                                                      # can't fetch the poll
    lambda m: setattr(m, "raw", lambda session_id: build(SAMPLE, corrupt=True)),       # a bad checksum
    lambda m: setattr(m, "history", lambda day: []),                                   # the poll isn't in the listing
    lambda m: setattr(m, "raw", lambda session_id: b"<html>502 Bad Gateway</html>"),   # not EVA-DTS at all
    lambda m: setattr(m, "history", lambda day: (_ for _ in ()).throw(BridgeUnreachable("down"))),   # down right after
], ids=["history down", "fetch fails", "bad checksum", "not listed", "not EVA-DTS", "history unreachable"])
def test_a_read_whose_storage_is_not_confirmed_is_logged_and_still_a_read(conn, machine, break_it):
    said = []
    break_it(machine)
    machine.clock.now = POLL_AT(15, 25)
    assert visit_polls.step(POLL_AT(15, 25), said.append) == READ       # the POST worked: not a failure
    assert any("storage not confirmed" in line and "live1" in line for line in said)
    assert not any("stored read" in line for line in said)


def test_unconfirmed_storage_never_counts_toward_the_back_off(conn, machine):
    said = []
    _history_fails(machine)
    reader = Reader(SCHEDULE, lambda now: visit_polls.step(now, said.append), said.append)
    for minute in (25, 28, 31, 34):
        machine.clock.now = POLL_AT(15, minute)
        reader.tick(POLL_AT(15, minute))
    assert machine.polls == 4                                  # a try every time it was due, no 5 minute wait
    assert reader.failures == 0 and reader.not_before == POLL_AT(15, 34) + timedelta(minutes=1)
    assert not any("failed in a row" in line for line in said)


def test_real_poll_failures_still_back_off_after_unconfirmed_storage(conn, machine):
    said = []
    _history_fails(machine)
    reader = Reader(SCHEDULE, lambda now: visit_polls.step(now, said.append), said.append)
    machine.clock.now = POLL_AT(15, 25)
    reader.tick(POLL_AT(15, 25))                               # unconfirmed: no failure
    machine.poll = lambda: (_ for _ in ()).throw(BridgeAnswered(409, "busy"))
    for minute in (28, 31, 34):
        machine.clock.now = POLL_AT(15, minute)
        reader.tick(POLL_AT(15, minute))
    assert reader.failures == 3 and "3 failed in a row, next try in 5 min" in said[-1]


def test_a_header_id_that_is_not_the_history_id_keeps_the_one_minute_cadence_and_the_stop_rule(conn, machine):
    said = []
    machine.header_prefix = "header"            # the header's ids are never the history's ids
    reader = Reader(SCHEDULE, lambda now: visit_polls.step(now, said.append), said.append)
    for minute in (25, 26, 27):                 # three successful POSTs, each with a usable row stored
        machine.clock.now = POLL_AT(15, minute)
        reader.tick(POLL_AT(15, minute))
    assert machine.polls == 3 and reader.failures == 0
    assert reader.not_before == POLL_AT(15, 27) + timedelta(minutes=1)      # not five minutes
    assert sum("storage not confirmed" in line for line in said) == 3
    # The rows the bridge stored still end the visit: a read after the close minute (3:32 PM) is in.
    for minute in (40, 41):
        machine.clock.now = POLL_AT(15, minute)
        result = visit_polls.step(POLL_AT(15, minute), said.append)
    assert result == VISIT_OVER


def test_a_usable_row_from_before_the_post_does_not_confirm_a_read_without_a_session_id(conn, machine):
    said = []
    machine.sends_session_id = False
    machine.history = lambda day: []                                    # this read never reaches storage
    store_poll(conn, "earlier", (POLL_AT(15, 25) - timedelta(seconds=59)).isoformat(), {"15": (200, 100)})
    machine.clock.now = POLL_AT(15, 25)
    assert visit_polls.step(POLL_AT(15, 25), said.append) == READ
    assert any("storage not confirmed" in line for line in said)        # the row is its own observation, at its own time
    assert not any("stored read" in line for line in said)


def test_a_read_with_no_session_id_is_confirmed_by_a_row_taken_since_the_post(conn, machine):
    said = []
    machine.sends_session_id = False
    machine.clock.now = POLL_AT(15, 25)
    assert visit_polls.step(POLL_AT(15, 25), said.append) == READ
    assert any("stored read" in line for line in said) and not any("not confirmed" in line for line in said)


def test_every_read_logs_whether_the_bridge_served_a_cached_document_and_how_old(conn, machine):
    said = []
    machine.cached, machine.age_seconds = True, 12.5
    machine.clock.now = POLL_AT(15, 25)
    visit_polls.step(POLL_AT(15, 25), said.append)
    assert any("cached=true" in line and "age_seconds=12.5" in line for line in said)
    said.clear()
    machine.cached, machine.age_seconds = False, 0.0
    machine.clock.now = POLL_AT(15, 27)
    visit_polls.step(POLL_AT(15, 27), said.append)
    assert any("cached=false" in line and "age_seconds=0" in line for line in said)


def test_a_stored_read_is_logged_with_its_session_id(conn, machine):
    said = []
    machine.clock.now = POLL_AT(15, 25)
    assert visit_polls.step(POLL_AT(15, 25), said.append) == READ
    assert any("live1" in line and "stored" in line for line in said)


def test_a_bridge_that_sends_no_session_id_is_checked_by_the_time_of_the_read(conn, machine):
    machine.sends_session_id = False
    machine.clock.now = POLL_AT(15, 25)
    assert visit_polls.step(POLL_AT(15, 25), lambda _: None) == READ          # a usable poll after the POST is stored
    machine.history = lambda day: []
    machine.clock.now = POLL_AT(15, 27)
    said = []
    assert visit_polls.step(POLL_AT(15, 27), said.append) == READ             # the POST worked; the row is not there
    assert any("storage not confirmed" in line for line in said)


# --- Which day a receipt belongs to -------------------------------------------------------------

def test_a_receipt_filed_after_midnight_counts_on_the_day_it_was_fulfilled(conn):
    record(conn, "r1", at(THU, 20).astimezone(timezone.utc), False, {})
    # The door closed at 11:58 PM Thursday (04:58 UTC); the receipt came in at 12:05 AM Friday.
    store(conn, "r1", fulfilled("r1", receipt(), fulfilled_at="2026-12-04T05:05:00Z",
                                door_opened_at="2026-12-04T04:40:00+00:00",
                                door_closed_at="2026-12-04T04:58:00+00:00"), POLL_AT(0, 10))
    store_poll(conn, "s1", POLL_AT(14, 50).isoformat(), {"15": (200, 100)})
    assert visit_polls.visit_over(conn, POLL_AT(15, 0))               # a read after the close is stored: Friday is done
    assert not visit_polls.visit_over(conn, POLL_AT(15, 0) - timedelta(days=1))
