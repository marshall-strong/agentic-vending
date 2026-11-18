"""Silent slots (`vending.wildcard_trial.silent_slots`): where a passing trial may be promoted to."""

from datetime import datetime, timedelta, timezone

from tests.conftest import store_poll
from tests.test_machine_state import fulfilled
from vending import counters, stock
from vending.wildcard_trial import SILENT_DAYS, silent_slots

UTC = timezone.utc
R0 = datetime(2026, 10, 16, 20, tzinfo=UTC)       # a Friday visit, 4 PM Eastern
VISIT = datetime(2026, 10, 23, 20, tzinfo=UTC)    # the last Friday visit
WINDOW = datetime(2026, 10, 23, 4, tzinfo=UTC)    # 00:00 Eastern that Friday
THURSDAY_EVENING = datetime(2026, 10, 29, 22, tzinfo=UTC)   # 6 PM Eastern
NOW = THURSDAY_EVENING + timedelta(hours=1)
STEP = timedelta(hours=4)


def machine(conn, sales=None, last=THURSDAY_EVENING, gaps=(), drop_at=None, edit=None):
    """Our slots on a Friday-to-Thursday cycle, polled every 4 hours from Fri 16 Oct to `last`.

    At the 16 Oct visit: 15 (the wildcard), 16, 17, 18, 25, 26 and 28 are loaded with 10 each. At the 23 Oct
    visit 16 is topped up with 2 and 25 switches product. `sales` maps slot -> [(when, units)]. No poll is
    stored inside a gap `(begin, end)`. `drop_at` is when slot 16's units counter reads low once. `edit(t,
    units)` may change a poll's units per slot before it is stored, or drop a slot from it."""
    sales = sales or {}
    fulfilled(conn, "r0", R0, *[(slot, sku, "primary", paid, 10, 0, 0) for slot, sku, paid in (
        ("15", "AAA", "10.00"), ("16", "BBB", "10.00"), ("17", "CCC", "10.00"), ("18", "FFF", "10.00"),
        ("25", "EEE", "10.00"), ("26", "GGG", "10.00"), ("28", "HHH", "20.00"))])
    fulfilled(conn, "r1", VISIT, ("16", "BBB", "primary", "2.00", 2, 0, 0), ("25", "DDD", "primary", "10.00", 10, 0, 0))
    t, n = datetime(2026, 10, 16, 18, tzinfo=UTC), 0
    while t <= last:
        if not any(a < t < b for a, b in gaps):
            units = {slot: 100 + sum(u for at, u in sales.get(slot, []) if at <= t)
                     for slot in ("15", "16", "17", "18", "25", "26", "28")}
            if drop_at is not None and t == drop_at:
                units["16"] = 3
            if edit is not None:
                edit(t, units)
            store_poll(conn, f"p{n}", t.isoformat(), {slot: (300, u) for slot, u in units.items()})
        t, n = t + STEP, n + 1


def report(conn, now=NOW, wildcard="15"):
    counted = stock.stock(conn, until=now)
    counts = {c.slot: c for c in counted.slots}
    slots = tuple(f"{r}{c}" for r in range(1, 6) for c in range(5, 9))
    return silent_slots(conn, now, counts, stock.loads(conn), counted.sales, slots, wildcard)


def why(r, slot):
    return next((n.why for n in r.not_silent if n.slot == slot), None) or next(
        (u.why for u in r.unconfirmed if u.slot == slot), None)


# What the receipts say each slot holds after the 23 Oct visit.
HELD = {"15": "AAA", "16": "BBB", "17": "CCC", "18": "FFF", "25": "DDD", "26": "GGG", "28": "HHH"}


def count(conn, slot, units, at=datetime(2026, 10, 24, 15, tzinfo=UTC), count_id="c1", product=None):
    """A person's door count of `slot`: `units` of `product`, by default the SKU its receipts name."""
    conn.execute("insert into slot_counts (count_id, counted_at, counted_by, source, device_id, selection, product,"
                 " units, exact) values (%s, %s, 'kevin', 'door', 'vmc1', %s, %s, %s, true)",
                 (count_id, at, slot, product or HELD[slot], units))


def test_a_slot_loaded_at_the_visit_that_sold_nothing_over_four_watched_class_days_is_silent(conn):
    machine(conn, sales={"18": [(datetime(2026, 10, 27, 15, tzinfo=UTC), 1)]})
    r = report(conn)
    assert r.window_start == WINDOW and r.reason is None
    assert [s.slot for s in r.silent] == ["16"]
    s16 = r.silent[0]
    assert (s16.product, s16.class_days_seen) == ("BBB", SILENT_DAYS)
    assert s16.evidence == "loaded 2 of BBB at the Friday visit of 2026-10-23"
    assert why(r, "18") == "sold 1 since the window began"
    assert why(r, "25") == "its product changed on 2026-10-23"
    assert why(r, "27") == "no receipts say what it holds"
    assert "15" not in {n.slot for n in r.not_silent} | {s.slot for s in r.silent}   # the wildcard never is


def test_without_a_load_at_the_visit_or_a_count_since_a_slot_is_unconfirmed_not_silent(conn):
    machine(conn)
    r = report(conn)
    assert {u.slot for u in r.unconfirmed} == {"17", "18", "26", "28"}
    assert "may be empty" in why(r, "17")
    # A person's count since the visit confirms it; one that saw it empty says so.
    count(conn, "17", 10)
    count(conn, "26", 0)
    r = report(conn)
    assert "17" in [s.slot for s in r.silent]
    assert next(s for s in r.silent if s.slot == "17").evidence == "door count c1 on 2026-10-24: 10 units of CCC"
    assert why(r, "26") == "a person counted it empty on 2026-10-24"
    # A count from before the visit says nothing about now.
    count(conn, "28", 10, at=datetime(2026, 10, 22, 15, tzinfo=UTC), count_id="c0")
    assert "28" in {u.slot for u in report(conn).unconfirmed}


def test_silent_slots_rank_silent_longest_then_lowest_margin(conn):
    machine(conn, sales={"16": [(datetime(2026, 10, 21, 15, tzinfo=UTC), 1)]})
    count(conn, "17", 10)
    count(conn, "28", 10)
    # 17 and 28 have been quiet since the 16 Oct load; 28 cost $2.00 a unit, 17 $1.00, so 28 earns less.
    # 16 was loaded at the 23 Oct visit, so it has been quiet the shortest.
    assert [s.slot for s in report(conn).silent] == ["28", "17", "16"]


def test_a_thursday_morning_run_hasnt_seen_four_class_days(conn):
    machine(conn, last=datetime(2026, 10, 29, 14, tzinfo=UTC))      # 10 AM Thursday
    r = report(conn, now=datetime(2026, 10, 29, 15, tzinfo=UTC))
    assert r.silent == () and why(r, "16").startswith("3 of 4 in-stock class days")


def test_a_day_the_polls_didnt_watch_doesnt_count(conn):
    # 11 hours without a poll on Tuesday: the stretch wasn't watched, so silence is unknown.
    machine(conn, gaps=[(datetime(2026, 10, 27, 1, tzinfo=UTC), datetime(2026, 10, 27, 12, tzinfo=UTC))])
    r = report(conn)
    assert r.silent == () and why(r, "16").startswith("unknown: no poll read it from 2026-10-26 22:00")


def test_a_sale_inside_a_gap_still_counts(conn):
    gap = (datetime(2026, 10, 27, 1, tzinfo=UTC), datetime(2026, 10, 27, 12, tzinfo=UTC))
    machine(conn, gaps=[gap], sales={"16": [(datetime(2026, 10, 27, 6, tzinfo=UTC), 1)]})
    assert why(report(conn), "16") == "sold 1 since the window began"


def test_a_sale_in_the_poll_window_spanning_the_fridays_midnight_counts(conn):
    # The poll before midnight Thursday and the first after: a sale between them may be Friday's.
    machine(conn, sales={"16": [(datetime(2026, 10, 23, 3, tzinfo=UTC), 1)]})   # 11 PM Thursday, seen at 2 AM Friday
    assert why(report(conn), "16") == "sold 1 since the window began"


def test_an_unsettled_drop_in_the_counter_is_unknown_not_silent(conn):
    machine(conn, drop_at=THURSDAY_EVENING)
    readings = [(datetime(2026, 10, 29, 18, tzinfo=UTC), 100), (THURSDAY_EVENING, 3)]
    assert counters.unsettled(readings) and not counters.unsettled(readings + [(NOW, 100)])
    assert why(report(conn), "16").startswith("unknown: its units counter")


def test_no_poll_since_the_window_began_is_unknown(conn):
    machine(conn, last=datetime(2026, 10, 22, 22, tzinfo=UTC))
    assert why(report(conn), "16") == "unknown: no good poll since the window began"


def test_the_window_is_the_last_friday_visit_not_a_midweek_one(conn):
    machine(conn)
    conn.execute("insert into purchase_requests (request_id, submitted_at, off_cycle, request_body, status,"
                 " fulfilled_at) values ('mid', %s, true, '{}', 'fulfilled', %s)",
                 (datetime(2026, 10, 26, tzinfo=UTC), datetime(2026, 10, 27, 15, tzinfo=UTC)))
    assert report(conn).window_start == WINDOW


def test_with_no_friday_visit_no_slot_is_silent(conn):
    r = report(conn)
    assert r.window_start is None and r.silent == () and r.reason == "no Friday visit is recorded yet"


def test_a_friday_visit_with_no_time_leaves_every_slot_unknown(conn):
    machine(conn)
    conn.execute("insert into purchase_requests (request_id, submitted_at, off_cycle, request_body, status)"
                 " values ('r9', %s, false, '{}', 'fulfilled')", (VISIT,))
    r = report(conn)
    assert r.window_start is None and r.silent == ()
    assert "r9" in r.reason and "python -m vending.sync_requests" in r.reason
    assert all(n.why == r.reason for n in r.not_silent)


# --- What the agent and the planner see ----------------------------------------------------------------

def trial_sales(extra=None):
    """The wildcard, 15, sells one a day on 19-22 and 26 Oct: 5 over 9 in-stock class days, a first-look pass."""
    days = [datetime(2026, 10, d, 15, tzinfo=UTC) for d in (19, 20, 21, 22, 26)]
    return {"15": [(at, 1) for at in days], **(extra or {})}


def test_a_pass_with_a_silent_slot_comes_with_a_proposal(conn):
    from vending import config
    from vending.tools.machine_state import get_machine_state

    machine(conn, sales=trial_sales())
    config.set_value(conn, "wildcard_slot", "15", "test")
    reply = get_machine_state(conn, now=NOW)
    t = reply.wildcard_trial
    assert (t.verdict, t.units_sold, t.class_days_in_stock) == ("pass", 5, 9)
    assert t.silent_since == WINDOW and [s.slot for s in t.silent_slots] == ["16"]
    assert {u.slot for u in t.unconfirmed_slots} == {"17", "18", "26", "28"}
    p = t.proposal
    assert (p.into_slot, p.product, p.trial_price) == ("16", "AAA", "3.00")
    assert p.pulls == "BBB (12 units to the closet)"
    assert "switch slot 15 to the next product" in p.wildcard_next
    assert "at most $6.50" in p.why and "Fri 23 Oct" in p.why
    assert "Proposal for the next Friday plan: switch slot 16 to AAA" in reply.message
    assert "maybe empty" in reply.message


def test_a_pass_with_no_silent_slot_stays_in_the_wildcard(conn):
    from vending import config
    from vending.tools.machine_state import get_machine_state

    machine(conn, sales=trial_sales({"16": [(datetime(2026, 10, 27, 15, tzinfo=UTC), 1)]}))
    config.set_value(conn, "wildcard_slot", "15", "test")
    t = get_machine_state(conn, now=NOW).wildcard_trial
    assert t.verdict == "pass" and t.silent_slots == [] and t.proposal is None
    assert "keep it in the wildcard" in t.reason


def test_no_proposal_unless_it_passes(conn):
    from vending import config
    from vending.tools.machine_state import get_machine_state

    machine(conn, sales={"15": [(datetime(2026, 10, 19, 15, tzinfo=UTC), 3)]})
    config.set_value(conn, "wildcard_slot", "15", "test")
    t = get_machine_state(conn, now=NOW).wildcard_trial
    assert t.verdict == "extend" and [s.slot for s in t.silent_slots] == ["16"] and t.proposal is None


def test_the_planner_gets_the_verdict_and_the_silent_slots(conn):
    from tests.test_ledger import open_ledger
    from vending import config, planner, planning, wildcard_trial

    assert planner.PASS_VERDICT == wildcard_trial.PASS
    open_ledger(conn)
    machine(conn, sales=trial_sales())
    gathered = planning.gather(conn, [], NOW)
    assert (gathered.current.wildcard_verdict, gathered.current.silent) == (None, frozenset())   # no wildcard set
    config.set_value(conn, "wildcard_slot", "15", "test")
    gathered = planning.gather(conn, [], NOW)
    assert (gathered.current.wildcard_verdict, gathered.current.silent) == ("pass", frozenset({"16"}))
