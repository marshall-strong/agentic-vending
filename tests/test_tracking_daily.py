"""The worker's daily tracking check (`vending.tracking_daily`, #218): when it sends, that it never sends twice
in a day (a worker restart included), the quiet skip before the ledger opening, and where an `outside` verdict
shows: the dashboard, the incident log and the agent's briefing. No network: the bridge is a fake."""

from __future__ import annotations

import threading
from datetime import datetime, time, timedelta, timezone

import pytest
from psycopg.types.json import Jsonb

from tests.conftest import TEST_SLOTS, open_in
from tests.test_tracking import FakeBridge, fill, open_ledger, poll, refused
from vending import settings, tracking, tracking_daily
from vending.bridge import BridgeNoAnswer
from vending.start import EASTERN

SETTINGS = (tracking_daily.MODE_SETTING, tracking_daily.AT_SETTING, tracking.DAILY_SETTING)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    """Every setting at its default, whatever a developer's `.env` says; then the daily send on."""
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(settings, "_FILE", {k: v for k, v in settings._FILE.items() if k not in SETTINGS})
    monkeypatch.setenv(tracking_daily.MODE_SETTING, "on")


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(timezone.utc)


# Fri 4 Dec 2026: 06:05 Eastern is 11:05 UTC. The 05:00 Eastern poll is in the bridge's books by 06:00.
SETTLED = utc("2026-12-04T10:00:00+00:00")
NOW = utc("2026-12-04T11:05:00+00:00")


def answer(as_of="p1", outside=(), checked_at=NOW):
    """A 200 of the spec's shape, checked at `checked_at`; the next check from the following midnight Eastern."""
    day = checked_at.astimezone(EASTERN).date() + timedelta(days=1)
    next_from = datetime.combine(day, time.min, EASTERN)
    return {"as_of": as_of, "as_of_time": SETTLED.isoformat(), "checked_at": checked_at.isoformat(),
            "results": {k: "outside" if k in outside else "within" for k in tracking.RESULTS},
            "next_check_from": next_from.isoformat()}


def books(conn):
    """The opening, a fill of our slots, a poll before it all, the settled poll and one this hour."""
    open_ledger(conn)
    fill(conn)
    poll(conn, "p0", "2026-11-02T18:00:00+00:00")
    poll(conn, "p1", SETTLED.isoformat(), sold_in_15=2)
    poll(conn, "p2", "2026-12-04T11:00:00+00:00", sold_in_15=5)     # this hour: not folded in yet


def sends(conn):
    return conn.execute("select id, sent_by, as_of, status, used_check, eastern_day from tracking_checks"
                        " order by id").fetchall()


def run(conn, bridge, now=NOW):
    lines: list[str] = []
    code = tracking_daily.run(conn, lambda: bridge, now=now, log=lines.append)
    return code, lines


# --- When it sends ----------------------------------------------------------------------------------

def test_it_is_off_by_default_and_then_never_opens_the_database(monkeypatch, capsys):
    from vending import db

    monkeypatch.delenv(tracking_daily.MODE_SETTING)
    monkeypatch.setattr(db, "connect", lambda: pytest.fail("off must not connect"))
    assert tracking_daily.main([]) == 0
    assert "off" in capsys.readouterr().out
    monkeypatch.setenv(tracking_daily.MODE_SETTING, "Off")
    assert tracking_daily.main([]) == 0


def test_a_wrong_setting_fails_the_job(conn, monkeypatch):
    monkeypatch.setenv(tracking_daily.MODE_SETTING, "yes")
    assert run(conn, FakeBridge())[0] == 2
    monkeypatch.setenv(tracking_daily.MODE_SETTING, "on")
    for wrong in ("6am", "22:00", "23:30", "06:00+01:00"):
        monkeypatch.setenv(tracking_daily.AT_SETTING, wrong)
        assert run(conn, FakeBridge())[0] == 2, wrong


def test_it_sends_once_as_of_the_newest_poll_already_in_the_bridges_books(conn):
    books(conn)
    bridge = FakeBridge(answer())
    code, lines = run(conn, bridge)
    assert code == 0
    assert [b["as_of"] for b in bridge.sent] == ["p1"]          # not p2, taken this hour
    assert set(bridge.sent[0]["coil"]) == set(TEST_SLOTS)
    [(_, by, as_of, status, used, day)] = sends(conn)
    assert (by, as_of, status, used, str(day)) == (tracking_daily.WORKER, "p1", 200, True, "2026-12-04")
    assert "cash within" in lines[-1]


def test_a_worker_restart_never_sends_twice_in_one_day(schema_url):
    """Each round below is a fresh connection, as after a restart: the only memory is the database."""
    with open_in(*schema_url) as conn:
        books(conn)
    bridge = FakeBridge(answer(), answer())
    later = [NOW, NOW + timedelta(hours=1), NOW + timedelta(hours=9), utc("2026-12-05T01:55:00+00:00")]
    codes = []
    for moment in later:            # 06:05, 07:05, 15:05 and 20:55 Eastern on Fri 4 Dec
        with open_in(*schema_url) as conn:
            codes.append(run(conn, bridge, moment))
    assert [c for c, _ in codes] == [0, 0, 0, 0]
    assert len(bridge.sent) == 1
    assert all("used or may be" in lines[-1] for _, lines in codes[1:])
    with open_in(*schema_url) as conn:
        assert len(sends(conn)) == 1


def test_the_next_day_it_sends_again(conn):
    books(conn)
    poll(conn, "p3", "2026-12-05T10:00:00+00:00")
    bridge = FakeBridge(answer(), answer("p3", checked_at=NOW + timedelta(days=1)))
    run(conn, bridge)
    assert run(conn, bridge, NOW + timedelta(days=1))[0] == 0
    assert [b["as_of"] for b in bridge.sent] == ["p1", "p3"]


@pytest.mark.parametrize("moment", ["2026-12-04T10:55:00+00:00",      # 05:55 Eastern
                                    "2026-12-05T02:00:00+00:00",      # 21:00 Eastern: the dispatch deadline
                                    "2026-12-05T03:00:00+00:00",      # 22:00 Eastern
                                    "2026-12-05T04:59:00+00:00"])     # 23:59 Eastern
def test_it_sends_only_from_six_to_the_deadline_eastern(conn, moment):
    books(conn)
    bridge = FakeBridge()
    code, lines = run(conn, bridge, utc(moment))
    assert (code, bridge.sent, sends(conn)) == (0, [], [])
    assert "not due" in lines[-1]


def test_the_time_is_a_setting(conn, monkeypatch):
    books(conn)
    monkeypatch.setenv(tracking_daily.AT_SETTING, "07:30")
    bridge = FakeBridge(answer("p2"))
    assert run(conn, bridge)[0] == 0 and bridge.sent == []
    # 07:35 Eastern: the 06:00 Eastern poll is in the bridge's books now.
    assert run(conn, bridge, NOW + timedelta(hours=1, minutes=30))[0] == 0
    assert [b["as_of"] for b in bridge.sent] == ["p2"]


def test_a_person_who_sent_today_already_blocks_the_worker(conn):
    books(conn)
    tracking.send(conn, FakeBridge(answer()), tracking.figures(conn, "p1"), "kevin", now=NOW - timedelta(minutes=30))
    bridge = FakeBridge()
    assert run(conn, bridge)[0] == 0
    assert bridge.sent == []


def test_the_cutoff_is_enforced_again_when_the_send_is_ready(conn):
    """A job that starts inside the window but is only ready to send at the dispatch deadline does not
    send. The window and the day are re-checked under the send's lock, with the clock of that moment."""
    books(conn)
    poll(conn, "p3", "2026-12-05T00:30:00+00:00")     # 19:30 Eastern, so the newest poll isn't stale
    stamps = iter([utc("2026-12-05T01:59:50+00:00"),      # 20:59:50 Eastern: due, and the window is open
                   utc("2026-12-05T02:00:00+00:00"),      # 21:00 Eastern: the reservation, 10 s later
                   utc("2026-12-05T02:10:00+00:00")])     # 21:10 Eastern: the send is only ready now
    bridge = FakeBridge(answer())
    code, lines = run(conn, bridge, now=lambda: next(stamps))
    assert (code, bridge.sent, sends(conn)) == (1, [], [])
    assert "window" in lines[-1]


def test_the_deadline_is_checked_again_just_before_the_post(conn, monkeypatch):
    """A run started at 20:59:50 Eastern whose figures take until 21:00:10 does not post: the window
    and the Eastern day are checked again just before the request goes out, and the reservation
    already committed is marked unused, not sent, so it blocks nothing and is no try."""
    books(conn)
    poll(conn, "p3", "2026-12-05T00:30:00+00:00")     # 19:30 Eastern, so the newest poll isn't stale
    clock = {"now": utc("2026-12-05T01:59:50+00:00")}  # 20:59:50 Eastern: due, the window is open
    real_slots = tracking.our_slots

    def slow_figures(c, *args, **kwargs):              # read inside figures(): the clock moves on
        clock["now"] = utc("2026-12-05T02:00:10+00:00")   # 21:00:10 Eastern
        return real_slots(c, *args, **kwargs)

    monkeypatch.setattr(tracking, "our_slots", slow_figures)
    bridge = FakeBridge(answer("p3"))
    code, lines = run(conn, bridge, now=lambda: clock["now"])
    assert (code, bridge.sent) == (1, [])
    stored = conn.execute("select status, used_check, error_type, answered_at from tracking_checks").fetchall()
    assert stored == [(None, False, "not_sent", utc("2026-12-05T02:00:10+00:00"))]
    day = utc("2026-12-05T01:59:50+00:00").astimezone(EASTERN).date()
    assert tracking.blocking(conn, day) == [] and tracking_daily.tries_today(conn, day)[0] == 0
    assert "window" in lines[-1]


def test_a_tied_poll_committed_after_the_daily_tie_check_does_not_change_the_sent_figures(schema_url,
                                                                                        monkeypatch):
    """The worker's send reads the poll, the tie check, the ledger and the stock in one repeatable-read
    snapshot, with the reservation in the same transaction: a tied poll another connection commits
    right after the tie check is not in the figures. It used to make slot 15 read 3, not 6."""
    from tests.test_tracking import a_tie_lands_after_the_tie_check

    with open_in(*schema_url) as setup:
        books(setup)
    a_tie_lands_after_the_tie_check(schema_url, monkeypatch, SETTLED.isoformat())
    bridge = FakeBridge(answer("p1"))
    with open_in(*schema_url) as job:
        code = tracking_daily.run(job, lambda: bridge, now=NOW, log=lambda line: None)
    assert code == 0 and len(bridge.sent) == 1
    assert bridge.sent[0]["as_of"] == "p1"
    assert bridge.sent[0]["coil"]["15"] == 6


def test_a_poll_that_ages_past_the_limit_before_the_send_is_refused(conn):
    """A delayed worker: the job's checks pass at 06:05 Eastern on a poll of 05:00, but the send is
    only ready at 13:05, when even the newest settled poll (06:00 Eastern) is over the six-hour
    limit. The poll is picked again under the send's reservation lock, with the clock of that
    moment, and its age is checked there, so nothing is sent."""
    books(conn)
    stamps = iter([NOW, NOW + timedelta(hours=7), NOW + timedelta(hours=7, minutes=5)])
    bridge = FakeBridge(answer("p1"))
    code, lines = run(conn, bridge, now=lambda: next(stamps))
    assert (code, bridge.sent, sends(conn)) == (1, [], [])
    assert "history sync may have stopped" in lines[-1]


def test_fridays_figures_are_not_sent_on_saturday_morning(conn):
    """A job ready to send on Friday evening whose send is only ready on Saturday morning: the day
    and the poll's age are decided again under the send's lock, with the clock of that moment, so
    Friday's poll cannot spend Saturday's check."""
    books(conn)
    poll(conn, "p3", "2026-12-05T00:30:00+00:00")     # 19:30 Eastern Friday, fresh that evening
    stamps = iter([utc("2026-12-05T01:55:00+00:00"),       # 20:55 Eastern Friday: due, the poll is fresh
                   utc("2026-12-06T11:05:00+00:00"),       # 06:05 Eastern Saturday: the reservation
                   utc("2026-12-06T11:15:00+00:00")])
    bridge = FakeBridge(answer("p3"))
    code, lines = run(conn, bridge, now=lambda: next(stamps))
    assert (code, bridge.sent, sends(conn)) == (1, [], [])
    assert "history sync may have stopped" in lines[-1]


def test_two_replicas_racing_cannot_bypass_the_backoff(schema_url):
    """Two replicas pass their quiet checks together, before either has stored a transient refusal,
    and race to reserve. The reservation's lock is taken without waiting, so exactly one of them
    sends; the other skips its round with a log line and tries again next round (two database
    connections, released together by a barrier)."""
    barrier = threading.Barrier(2)
    bridges = [FakeBridge(refused(409, "tracking_not_started")), FakeBridge(refused(409, "tracking_not_started"))]
    with open_in(*schema_url) as setup:
        books(setup)
    codes, lines = [], []

    def replica(n: int):
        with open_in(*schema_url) as conn:
            def gated():                       # both replicas passed every quiet check before either sent
                barrier.wait(10)
                return bridges[n]
            codes.append(tracking_daily.run(conn, gated, now=NOW, log=lines.append))

    threads = [threading.Thread(target=replica, args=(n,)) for n in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    with open_in(*schema_url) as conn:
        rows = sends(conn)
    assert sorted(codes) == [0, 0]
    assert len(bridges[0].sent) + len(bridges[1].sent) == 1 and len(rows) == 1
    assert rows[0][2] == "p1" and rows[0][4] is False
    assert sum("holds the reservation" in line for line in lines) == 1
    assert not any("backing off" in line or "still being recorded" in line for line in lines)


def test_a_correction_committed_before_the_lock_is_in_the_sent_figures(schema_url):
    """The figures are computed under the reservation's lock, in its own transaction: a correction a
    second connection commits after the job prepared its figures but before it took the lock is in
    the books the check reports."""
    from vending import ledger

    with open_in(*schema_url) as setup:
        books(setup)
    other = open_in(*schema_url)                    # a second connection, open across the job
    prepared = utc("2026-12-04T11:05:00+00:00")     # 06:05 Eastern: the job's own checks
    under_lock = utc("2026-12-04T11:06:00+00:00")   # 06:06 Eastern: the reservation, a minute later
    steps = iter([prepared, "commit", under_lock, under_lock + timedelta(seconds=1),
                  under_lock + timedelta(seconds=5)])

    def clock():
        when = next(steps)
        if when == "commit":                        # the job has prepared; the lock is next
            ledger.record(other, "adjustment", 12345, "counted the closet: $123.45 more", "marshall",
                          utc("2026-12-04T08:00:00+00:00"))
            other.commit()
            return next(steps)
        return when

    bridge = FakeBridge(answer())
    with open_in(*schema_url) as job:
        lines: list[str] = []
        code = tracking_daily.run(job, lambda: bridge, now=clock, log=lines.append)
    other.close()
    body = bridge.sent[0]
    expected = None
    with open_in(*schema_url) as conn2:
        expected = tracking.figures(conn2, "p1").body()["cash"]
    assert code == 0 and len(bridge.sent) == 1
    assert body["cash"] == expected and body["cash"] != "357.23"


def test_a_poll_committed_before_the_lock_is_the_one_sent(schema_url):
    """The newest settled poll is picked under the reservation's lock, too: a poll a second connection
    commits after the job picked its poll but before the lock is the one the check is as of."""
    with open_in(*schema_url) as setup:
        books(setup)
    other = open_in(*schema_url)
    prepared = utc("2026-12-04T11:05:00+00:00")
    under_lock = utc("2026-12-04T11:06:00+00:00")
    steps = iter([prepared, "commit", under_lock, under_lock + timedelta(seconds=1),
                  under_lock + timedelta(seconds=5)])

    def clock():
        when = next(steps)
        if when == "commit":                        # a newer settled poll lands before the lock
            poll(other, "p9", "2026-12-04T10:30:00+00:00")   # 05:30 Eastern, settled by the 06:00 hour
            other.commit()
            return next(steps)
        return when

    bridge = FakeBridge(answer("p9"))
    with open_in(*schema_url) as job:
        code = tracking_daily.run(job, lambda: bridge, now=clock, log=lambda line: None)
    other.close()
    assert code == 0 and [b["as_of"] for b in bridge.sent] == ["p9"]


def test_figures_prepared_the_evening_before_are_not_sent_the_next_morning(schema_url):
    """A job that prepared its figures on Friday evening and only got the lock on Saturday morning
    sends Saturday's figures - computed at the send, from the books of that moment - not the
    previous evening's."""
    from vending import ledger

    with open_in(*schema_url) as setup:
        books(setup)
    other = open_in(*schema_url)
    evening = utc("2026-12-05T01:55:00+00:00")      # 20:55 Eastern Friday: the job prepares here
    morning = utc("2026-12-06T11:05:00+00:00")      # 06:05 Eastern Saturday: the reservation
    steps = iter([evening, "commit", morning, morning + timedelta(seconds=1), morning + timedelta(seconds=5)])

    def clock():
        when = next(steps)
        if when == "commit":                        # overnight: a correction, and the sync brought a poll
            ledger.record(other, "adjustment", -250, "a damaged coil, per David", "marshall",
                          utc("2026-12-05T01:00:00+00:00"))
            poll(other, "p9", "2026-12-06T10:00:00+00:00")   # 05:00 Eastern Saturday, settled by 06:00
            other.commit()
            return next(steps)
        return when

    bridge = FakeBridge(answer("p9"))
    with open_in(*schema_url) as job:
        code = tracking_daily.run(job, lambda: bridge, now=clock, log=lambda line: None)
    other.close()
    assert code == 0 and [b["as_of"] for b in bridge.sent] == ["p9"]
    expected = None
    with open_in(*schema_url) as conn2:
        expected = tracking.figures(conn2, "p9").body()["cash"]
    assert bridge.sent[0]["cash"] == expected and bridge.sent[0]["cash"] != "357.23"


def test_a_stuck_holder_of_the_reservation_lock_skips_the_round(schema_url):
    """A holder that never releases the reservation's lock must not strand every other replica's
    round: the send tries the lock without waiting, the round is skipped with a log line, and the
    next round tries again. The holder below is a plain transaction on its own connection."""
    import psycopg

    from vending import db as vendordb

    with open_in(*schema_url) as setup:
        books(setup)
    holder = psycopg.connect(schema_url[0], options=vendordb.session_options(
        schema_url[0], f"-c search_path={schema_url[1]}"))
    holder.execute("select pg_advisory_xact_lock(%s)", (tracking.TRACKING_LOCK,))
    codes, lines = [], []

    def replica():
        with open_in(*schema_url) as conn:
            codes.append(tracking_daily.run(conn, lambda: FakeBridge(answer()), now=NOW, log=lines.append))

    thread = threading.Thread(target=replica, daemon=True)
    thread.start()
    thread.join(30)
    holder.rollback()
    holder.close()
    with open_in(*schema_url) as conn:
        assert sends(conn) == []
    assert codes == [0]
    assert any("holds the reservation" in line for line in lines)


def test_the_answer_is_stamped_when_it_is_stored(conn):
    """The reservation and the answer carry their own times: a job that runs long does not record the
    answer under the time it started."""
    books(conn)
    stamps = iter([NOW, NOW + timedelta(minutes=2), NOW + timedelta(minutes=3), NOW + timedelta(minutes=4)])
    run(conn, FakeBridge(answer()), now=lambda: next(stamps))
    sent_at, answered_at = conn.execute("select sent_at, answered_at from tracking_checks").fetchone()
    assert (sent_at, answered_at) == (NOW + timedelta(minutes=2), NOW + timedelta(minutes=4))


def test_a_reply_lost_after_midnight_blocks_the_next_morning(conn):
    """A job ready at 20:59:55 Eastern - inside the window, before the 21:00 dispatch deadline -
    whose reply is lost at 00:01 records the attempt under Friday, with the real answer time, so
    the bridge may have checked it on Saturday: no second send."""
    books(conn)
    poll(conn, "p3", "2026-12-05T00:30:00+00:00")     # 19:30 Eastern, so the newest poll isn't stale
    stamps = iter([utc("2026-12-05T01:59:50+00:00"), utc("2026-12-05T01:59:55+00:00"),
                   utc("2026-12-05T01:59:56+00:00"),     # the dispatch check, just before the request
                   utc("2026-12-05T05:01:00+00:00")])
    bridge = FakeBridge(BridgeNoAnswer("timed out"))
    assert run(conn, bridge, now=lambda: next(stamps))[0] == 3
    [(_, by, as_of, status, used, day)] = sends(conn)
    assert (str(day), status, used) == ("2026-12-04", None, None)
    sent_at, answered_at = conn.execute("select sent_at, answered_at from tracking_checks").fetchone()
    assert (sent_at, answered_at) == (utc("2026-12-05T01:59:55+00:00"), utc("2026-12-05T05:01:00+00:00"))
    assert run(conn, bridge, utc("2026-12-05T11:05:00+00:00"))[0] == 0    # Sat 06:05 Eastern
    assert len(bridge.sent) == 1 and len(sends(conn)) == 1
    assert "used or may be" in run(conn, bridge, utc("2026-12-05T11:05:00+00:00"))[1][-1]


def test_a_request_held_past_midnight_means_one_post_for_the_night(conn, monkeypatch):
    """A send started before the dispatch deadline whose request is held past midnight - the reply
    never comes, the row stays without an answer - may still be checked by the bridge after
    midnight, on the next Eastern day. Until the morning window opens (06:00 Eastern) such a row
    blocks the next day's check, so one request reaches the bridge for that night, not two. After
    06:00 the next day's check goes out: a request started by 21:00 and given up by the client
    within seconds is hours old by then, and the bridge no longer reads it."""
    monkeypatch.setenv(tracking_daily.AT_SETTING, "00:00")
    books(conn)
    poll(conn, "p3", "2026-12-05T00:30:00+00:00")     # 19:30 Eastern Friday, fresh at 20:59
    stamps = iter([utc("2026-12-05T01:59:50+00:00"), utc("2026-12-05T01:59:55+00:00"),
                   utc("2026-12-05T01:59:56+00:00"),     # the dispatch check, just before the request
                   utc("2026-12-05T02:00:15+00:00")])

    class Held(FakeBridge):
        def check_books(self, body):        # the request is on the wire; the bridge holds it: no answer
            raise BridgeNoAnswer("timed out")

    run(conn, Held(), now=lambda: next(stamps))   # the client gives up after 20 s, before midnight
    [row] = sends(conn)
    assert (row[2], row[3], row[4]) == ("p3", None, None)
    # Saturday 00:05 Eastern: the held request may reach the bridge after midnight, so no second send.
    bridge = FakeBridge()
    code, lines = run(conn, bridge, utc("2026-12-05T05:05:00+00:00"))
    assert (code, bridge.sent) == (0, [])
    assert "used or may be" in lines[-1]
    saturday = utc("2026-12-05T05:05:00+00:00").astimezone(EASTERN).date()
    assert tracking.blocking(conn, saturday, now=utc("2026-12-05T05:05:00+00:00")) != []
    assert tracking.blocking(conn, saturday, now=utc("2026-12-05T11:05:00+00:00")) == []
    # Saturday 06:05 Eastern: the morning window opens; the day's check goes out.
    poll(conn, "p6", "2026-12-05T10:00:00+00:00")     # 05:00 Eastern Saturday
    bridge = FakeBridge(answer("p6", checked_at=utc("2026-12-05T11:10:00+00:00")))
    code, lines = run(conn, bridge, utc("2026-12-05T11:05:00+00:00"))
    assert code == 0 and [b["as_of"] for b in bridge.sent] == ["p6"]


# --- Skips and failures -----------------------------------------------------------------------------

def test_with_no_ledger_opening_it_skips_quietly(conn):
    fill(conn)
    poll(conn, "p1", SETTLED.isoformat())
    bridge = FakeBridge()
    code, lines = run(conn, bridge)
    assert (code, bridge.sent, sends(conn)) == (0, [], [])
    assert "skipped" in lines[-1] and "no opening" in lines[-1]


def test_an_opening_after_now_is_no_opening_yet(conn):
    open_ledger(conn, at=NOW + timedelta(days=1))
    assert run(conn, FakeBridge())[0] == 0 and sends(conn) == []


def test_with_no_poll_since_the_opening_it_skips_quietly(conn):
    open_ledger(conn, at=NOW - timedelta(minutes=30))
    poll(conn, "p1", SETTLED.isoformat())
    code, lines = run(conn, FakeBridge())
    assert code == 0 and sends(conn) == [] and "skipped" in lines[-1]


def test_figures_it_cannot_work_out_fail_the_job_and_send_nothing(conn):
    open_ledger(conn)
    fill(conn)
    conn.execute("update purchase_lines set total_paid = null where slot = '15'")   # cash can't be known
    poll(conn, "p1", SETTLED.isoformat())
    bridge = FakeBridge()
    code, lines = run(conn, bridge)
    assert (code, bridge.sent, sends(conn)) == (1, [], [])
    assert "not sent" in lines[-1]
    assert skips(conn) == []


def skips(conn):
    return conn.execute("select eastern_day, reason, slots, rounds from tracking_skips order by id").fetchall()


def test_a_slot_no_receipt_has_loaded_is_a_skip_with_its_reason_until_it_is_loaded(conn):
    """#254: a day-one line came back unavailable, so a slot has no counted receipt. Each round is a skip with its
    reason (exit 0, nothing sent, no guess), and the dashboard shows a Check line, not a failed job. Once the slot
    is loaded, the round sends and the line goes."""
    from datetime import date

    from vending import dashboard

    open_ledger(conn)
    fill(conn, slots=TEST_SLOTS[:-1])
    poll(conn, "p0", "2026-11-02T18:00:00+00:00")
    poll(conn, "p1", SETTLED.isoformat())
    bridge = FakeBridge()
    code, lines = run(conn, bridge)
    assert (code, bridge.sent, sends(conn)) == (0, [], [])
    assert lines[-1].startswith("tracking_daily: skipped") and TEST_SLOTS[-1] in lines[-1]
    later = NOW + timedelta(hours=1)
    assert run(conn, bridge, later)[0] == 0
    assert skips(conn) == [(date(2026, 12, 4), "not_loaded", [TEST_SLOTS[-1]], 2)]
    data = tracking_daily.panel(conn, later)
    [(level, text)] = tracking_daily.attention(data)
    assert level == "warn" and f"slot {TEST_SLOTS[-1]}" in text and "loaded" in text
    assert "skipped" in tracking_daily.panel_html({"ok": True, "data": data})
    snap, _ = dashboard._snapshot(conn, later)
    assert any(a["level"] == "warn" and "tracking check" in a["text"].lower() for a in dashboard.attention(snap))

    fill(conn, slots=TEST_SLOTS[-1:], request_id="late")
    sent = FakeBridge(answer())
    assert run(conn, sent, NOW + timedelta(hours=2))[0] == 0 and len(sent.sent) == 1
    assert tracking_daily.attention(tracking_daily.panel(conn, NOW + timedelta(hours=2))) == []


def test_a_stale_newest_poll_fails_the_job(conn):
    books(conn)
    code, lines = run(conn, FakeBridge(), NOW + timedelta(hours=7))
    assert code == 1 and sends(conn) == [] and "history sync may have stopped" in lines[-1]


def test_a_send_with_no_usable_answer_blocks_the_rest_of_the_day(conn):
    books(conn)
    bridge = FakeBridge(BridgeNoAnswer("timed out"))
    assert run(conn, bridge)[0] == 3
    assert run(conn, bridge, NOW + timedelta(hours=1))[0] == 0
    assert len(bridge.sent) == 1


def test_a_send_left_unanswered_by_a_crash_blocks_the_day(conn):
    """The worker died between storing the request and the answer: the row has no answer at all."""
    books(conn)
    conn.execute("insert into tracking_checks (sent_at, eastern_day, sent_by, as_of, as_of_time, request)"
                 " values (%s, '2026-12-04', %s, 'p1', %s, %s)",
                 (NOW - timedelta(minutes=10), tracking_daily.WORKER, SETTLED, Jsonb({})))
    bridge = FakeBridge()
    assert run(conn, bridge)[0] == 0 and bridge.sent == []


def test_non_transient_refusals_stop_at_three(conn):
    """A refusal that will not settle on its own is retried next round, at most MAX_TRIES a day."""
    books(conn)
    bridge = FakeBridge(*[refused(422, "invalid_coil")] * 3)
    codes = [run(conn, bridge, NOW + timedelta(hours=h))[0] for h in range(5)]
    assert codes == [1, 1, 1, 1, 1]
    assert len(bridge.sent) == 3
    assert [used for _, _, _, _, used, _ in sends(conn)] == [False] * 3
    assert "no more until tomorrow" in run(conn, bridge, NOW + timedelta(hours=5))[1][-1]


def test_two_replicas_racing_cannot_exceed_max_tries(schema_url):
    """Two replicas pass the pre-check together (two tries each) and race to reserve: the lock goes
    to one of them without waiting, it makes the day's third and last try, and the other skips its
    round (LockBusy) instead of queueing behind the holder."""
    barrier = threading.Barrier(2)
    bridge = FakeBridge(*[refused(422, "invalid_coil")] * 3)
    with open_in(*schema_url) as conn:
        books(conn)
        run(conn, bridge)
        run(conn, bridge)                      # two refusals already stored from earlier rounds
    codes, lines = [], []

    def replica():
        with open_in(*schema_url) as conn:
            def gated():                       # both replicas pass every pre-check before either sends
                barrier.wait(10)
                return bridge
            codes.append(tracking_daily.run(conn, gated, now=NOW + timedelta(hours=1), log=lines.append))

    threads = [threading.Thread(target=replica) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    with open_in(*schema_url) as conn:
        rows = sends(conn)
    assert sorted(codes) == [0, 1]
    assert len(bridge.sent) == 3 and len(rows) == 3
    assert [used for *_, used, _ in rows] == [False] * 3
    assert sum("holds the reservation" in line for line in lines) == 1


def test_transient_refusals_back_off_but_do_not_forfeit_the_day(conn):
    """`tracking_not_started` settles at the competition start, so early refusals are retried on a
    backoff across the send window instead of using up the three tries that a real refusal needs."""
    books(conn)
    bridge = FakeBridge(*[refused(409, "tracking_not_started")] * 5)
    # A fresh poll each stretch, as the syncs bring them in, so the newest one isn't stale:
    # p4 at 11:00 Eastern for the 13:05 try, p5 at 16:00 for 17:05.
    poll(conn, "p4", "2026-12-04T16:00:00+00:00")
    poll(conn, "p5", "2026-12-04T21:00:00+00:00")
    # 06:05, 07:05 (1 h later), 09:05 (2 h), 13:05 (4 h) and 17:05 (4 h) Eastern: each round waits out
    # the backoff and stays inside the window, before the 21:00 dispatch deadline.
    for moment in (NOW, NOW + timedelta(hours=1), NOW + timedelta(hours=3), NOW + timedelta(hours=7),
                   NOW + timedelta(hours=11)):
        assert run(conn, bridge, moment)[0] == 0, moment
    assert len(bridge.sent) == 5
    assert [used for _, _, _, _, used, _ in sends(conn)] == [False] * 5


def test_a_transient_refusal_waits_out_its_backoff(conn):
    books(conn)
    bridge = FakeBridge(refused(409, "tracking_not_started"), refused(409, "tracking_not_started"))
    assert run(conn, bridge, NOW)[0] == 0 and len(bridge.sent) == 1
    first = list(bridge.sent)
    code, lines = run(conn, bridge, NOW + timedelta(minutes=30))   # 06:35: the 1 h backoff is not over
    assert (code, bridge.sent) == (0, first)
    assert "backing off" in lines[-1]
    assert run(conn, bridge, NOW + timedelta(hours=1))[0] == 0 and len(bridge.sent) == 2


def test_a_refusal_that_will_not_settle_on_its_own_fails_the_job(conn):
    books(conn)
    assert run(conn, FakeBridge(refused(422, "invalid_coil")))[0] == 1


def test_two_workers_cannot_both_send_before_the_daily_limit_starts(conn):
    """Before 3 Dec `send` alone allows a second send; the worker's `once_a_day` refuses it under the lock."""
    books(conn)
    early = utc("2026-11-25T11:05:00+00:00")
    found = tracking.figures(conn, "p0")
    checked = answer("p0", checked_at=early)
    tracking.send(conn, FakeBridge(checked), found, tracking_daily.WORKER, now=early, once_a_day=True)
    with pytest.raises(tracking.AlreadyChecked):
        tracking.send(conn, FakeBridge(checked), found, tracking_daily.WORKER, now=early, once_a_day=True)
    tracking.send(conn, FakeBridge(checked), found, "kevin", now=early)     # by hand: testing, no limit


# --- Where the verdicts show ------------------------------------------------------------------------

def test_an_outside_verdict_reaches_the_dashboard_and_the_incident_log(conn):
    from vending import dashboard, report_card

    books(conn)
    run(conn, FakeBridge(answer(outside=("coil",))))
    later = NOW + timedelta(hours=1)
    data = tracking_daily.panel(conn, later)
    [check] = data["checks"]
    assert check["outside"] == ["coil"] and data["on"] and data["at"] == "06:00"
    [(level, text)] = tracking_daily.attention(data)
    assert level == "bad" and "coil" in text
    html = tracking_daily.panel_html({"ok": True, "data": data})
    assert html.count('class="pill bad"') == 1 and html.count('class="pill ok"') == 3
    incidents = report_card.incidents(conn, later)
    [row] = [r for r in incidents["rows"] if r["kind"] == "books_outside"]
    assert "the coil" in row["note"] and not row["error"]
    assert "Books outside" in report_card.incidents_html({"ok": True, "data": incidents})
    snap, _ = dashboard._snapshot(conn, later)
    assert snap["tracking"]["ok"] and snap["tracking"]["data"]["checks"][0]["outside"] == ["coil"]
    assert any("tracking check" in a["text"] for a in dashboard.attention(snap))


def test_a_check_within_on_every_figure_is_no_incident(conn):
    from vending import report_card

    books(conn)
    run(conn, FakeBridge(answer()))
    assert not [r for r in report_card.incidents(conn, NOW + timedelta(hours=1))["rows"]
                if r["kind"].startswith(("books", "check"))]
    assert tracking_daily.attention(tracking_daily.panel(conn, NOW)) == []


def test_a_check_with_no_usable_answer_is_an_incident_and_shows_no_verdicts(conn):
    from vending import report_card

    books(conn)
    run(conn, FakeBridge(BridgeNoAnswer("timed out")))
    [row] = [r for r in report_card.incidents(conn, NOW + timedelta(hours=1))["rows"]
             if r["kind"] == "check_unanswered"]
    assert "may have used" in row["note"]
    html = tracking_daily.panel_html({"ok": True, "data": tracking_daily.panel(conn, NOW)})
    assert "No verdicts" in html and "pill" not in html


def test_a_reservation_left_unanswered_by_a_crash_becomes_an_incident(conn):
    """The worker crashed after the reservation committed and before any answer was recorded: the
    day is blocked, and nothing showed in the incident log. Past a grace period the log shows it:
    a tracking check may have been used; check with the instructors."""
    from vending import report_card

    books(conn)
    sent = NOW - timedelta(hours=1)
    conn.execute("insert into tracking_checks (sent_at, eastern_day, sent_by, as_of, as_of_time, request)"
                 " values (%s, '2026-12-04', %s, 'p1', %s, '{}')",
                 (sent, tracking_daily.WORKER, SETTLED))
    data = report_card.incidents(conn, NOW)
    [row] = [r for r in data["rows"] if r["kind"].startswith("check")]
    assert row["kind"] == "check_unresolved" and not row["error"]
    assert "may have been used" in row["note"] and "instructors" in row["note"]
    assert data["total"] >= 1
    assert "Check unresolved" in report_card.incidents_html({"ok": True, "data": data})


def test_a_fresh_reservation_is_not_yet_an_incident(conn):
    """A send still being recorded (the answer within the grace period) is not an incident."""
    from vending import report_card

    books(conn)
    conn.execute("insert into tracking_checks (sent_at, eastern_day, sent_by, as_of, as_of_time, request)"
                 " values (%s, '2026-12-04', %s, 'p1', %s, '{}')",
                 (NOW - timedelta(minutes=5), tracking_daily.WORKER, SETTLED))
    assert not [r for r in report_card.incidents(conn, NOW)["rows"] if r["kind"].startswith("check")]


def test_the_panel_says_when_the_daily_send_is_off(conn, monkeypatch):
    monkeypatch.setenv(tracking_daily.MODE_SETTING, "off")
    html = tracking_daily.panel_html({"ok": True, "data": tracking_daily.panel(conn, NOW)})
    assert "The daily send is off" in html and "No checks sent yet" in html


def test_an_outside_verdict_is_in_the_agents_next_briefing(conn):
    from tests.test_briefing import NOW as BRIEF_NOW, sample
    from vending import briefing

    sample(conn)
    as_of = BRIEF_NOW - timedelta(hours=1)
    conn.execute("insert into tracking_checks (sent_at, eastern_day, sent_by, as_of, as_of_time, request,"
                 " answered_at, status, answer, used_check, results)"
                 " values (%s, '2026-11-12', 'worker (daily)', 'poll-3', %s, '{}', %s, 200, %s, true, %s)",
                 (BRIEF_NOW - timedelta(minutes=30), as_of, BRIEF_NOW - timedelta(minutes=30),
                  Jsonb({"checked_at": (BRIEF_NOW - timedelta(minutes=30)).isoformat()}),
                  Jsonb({"cash": "outside", "sales_tax_owed": "within", "coil": "within", "back_stock": "within"})))
    b = briefing.build(conn, BRIEF_NOW)
    assert b.tracking is not None and b.tracking.results["cash"] == "outside"
    assert [a.lookup for a in b.alerts if a.code == "books_outside_cash"] == ["get_account_balance"]
    text = briefing.render(b)
    assert "cash OUTSIDE" in text and "coil within" in text


def test_the_panel_shows_the_last_answered_check_when_seven_refusals_follow_it(conn):
    """Seven refusals fill the panel's recent list, so the last answered check falls off it. It is
    rendered separately, with its four verdicts: an outside verdict must stay visible on the page."""
    books(conn)
    run(conn, FakeBridge(answer(outside=("coil",))))
    for minute in (10, 20, 30, 40, 50, 60, 70):        # seven refusals after the answered check
        at = NOW + timedelta(minutes=minute)
        conn.execute(
            "insert into tracking_checks (sent_at, eastern_day, sent_by, as_of, as_of_time, request,"
            " answered_at, status, error_type, used_check, results)"
            " values (%s, '2026-12-04', 'kevin', 'p1', %s, '{}', %s, 422, 'invalid_coil', false, null)",
            (at, SETTLED, at))
    data = tracking_daily.panel(conn, NOW + timedelta(hours=2))
    assert data["answered"]["id"] not in [c["id"] for c in data["checks"]]
    html = tracking_daily.panel_html({"ok": True, "data": data})
    assert html.count('class="pill bad"') == 1 and html.count('class="pill ok"') == 3
    assert "Last answered" in html


def test_the_briefing_says_when_no_check_has_been_answered(conn):
    from tests.test_briefing import NOW as BRIEF_NOW, sample
    from vending import briefing

    sample(conn)
    b = briefing.build(conn, BRIEF_NOW)
    assert b.tracking is None and not [a for a in b.alerts if a.code.startswith("books_outside")]
    assert "Tracking check (bridge's books vs ours): none answered yet." in briefing.render(b)


def test_the_briefing_keeps_the_latest_answered_check_past_seven_refusals(conn):
    """The newest answered check is queried for itself, not looked for among the 7 newest sends:
    refusals after it cannot hide an outside verdict from the briefing."""
    from tests.test_briefing import NOW as BRIEF_NOW, sample
    from vending import briefing

    sample(conn)
    answered = BRIEF_NOW - timedelta(hours=3)
    conn.execute(
        "insert into tracking_checks (sent_at, eastern_day, sent_by, as_of, as_of_time, request,"
        " answered_at, status, answer, used_check, results)"
        " values (%s, '2026-11-12', 'worker (daily)', 'poll-3', %s, '{}', %s, 200, %s, true, %s)",
        (answered, answered, answered,
         Jsonb({"checked_at": answered.isoformat()}),
         Jsonb({"cash": "outside", "sales_tax_owed": "within", "coil": "within", "back_stock": "within"})))
    for minute in (170, 145, 120, 95, 70, 45, 20):
        at = BRIEF_NOW - timedelta(minutes=minute)
        conn.execute(
            "insert into tracking_checks (sent_at, eastern_day, sent_by, as_of, as_of_time, request,"
            " answered_at, status, error_type, used_check)"
            " values (%s, '2026-11-12', 'kevin', 'poll-3', %s, '{}', %s, 409, 'not_reconciled_yet', false)",
            (at, answered, at))
    b = briefing.build(conn, BRIEF_NOW)
    assert b.tracking is not None and b.tracking.id > 0 and b.tracking.results["cash"] == "outside"
    assert b.tracking.unanswered_since == 7
    assert [a.lookup for a in b.alerts if a.code == "books_outside_cash"] == ["get_account_balance"]
    assert "cash OUTSIDE" in briefing.render(b)


def test_an_outside_verdict_survives_seven_later_refusals(conn):
    """Needs attention reads the latest answered check, not the newest answered one among the last
    7 sends: after one `cash: outside` and 7 refusals, the warning is still shown."""
    from vending import dashboard

    books(conn)
    run(conn, FakeBridge(answer(outside=("cash",))))
    for hour in range(1, 8):
        at = NOW + timedelta(hours=hour)
        conn.execute(
            "insert into tracking_checks (sent_at, eastern_day, sent_by, as_of, as_of_time, request,"
            " answered_at, status, error_type, used_check)"
            " values (%s, '2026-12-04', 'kevin', 'p1', %s, '{}', %s, 409, 'not_reconciled_yet', false)",
            (at, SETTLED, at))
    later = NOW + timedelta(hours=8)
    found = tracking_daily.latest_answered(conn, later)
    assert found is not None and found["verdicts"]["cash"] == "outside"
    panel = tracking_daily.panel(conn, later)
    [(level, text)] = tracking_daily.attention(panel)
    assert level == "bad" and "cash" in text
    snap, _ = dashboard._snapshot(conn, later)
    assert any("tracking check" in a["text"] for a in dashboard.attention(snap))
