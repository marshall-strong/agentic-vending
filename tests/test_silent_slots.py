"""Silent slots (`vending.wildcard_trial.silent_slots`): where a passing trial may be promoted to."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from tests.conftest import store_poll
from tests.test_machine_state import fulfilled
from vending import counters, stock
from vending.wildcard_trial import (CONFLICT, PASS, Silent, SilentReport, SlotRecord, Trial, SILENT_DAYS,
                                    _stock_evidence, propose, pulls, silent_slots)

UTC = timezone.utc
R0 = datetime(2026, 10, 16, 20, tzinfo=UTC)       # a Friday visit, 4 PM Eastern
VISIT = datetime(2026, 10, 23, 20, tzinfo=UTC)    # the last Friday visit
WINDOW = datetime(2026, 10, 23, 4, tzinfo=UTC)    # 00:00 Eastern that Friday
THURSDAY_EVENING = datetime(2026, 10, 29, 22, tzinfo=UTC)   # 6 PM Eastern
NOW = THURSDAY_EVENING + timedelta(hours=1)
STEP = timedelta(hours=4)


def machine(conn, sales=None, last=THURSDAY_EVENING, gaps=(), drop_at=None, edit=None, full=False, takings=None,
            prices=None):
    """Our slots on a Friday-to-Thursday cycle, polled every 4 hours from Fri 16 Oct to `last`.

    At the 16 Oct visit: 15 (the wildcard), 16, 17, 18, 25, 26 and 28 are loaded with 10 each. At the 23 Oct
    visit 16 is topped up with 2 and 25 switches product. `sales` maps slot -> [(when, units)]. No poll is
    stored inside a gap `(begin, end)`. `drop_at` is when slot 16's units counter reads low once. `edit(t,
    units)` may change a poll's units per slot before it is stored, or drop a slot from it. `full` loads our
    other 13 slots at the 16 Oct visit too (no poll reads them), so a receipt names what every slot holds.
    `takings` maps slot -> [(when, cents)] added to its takings counter with no unit counted. `prices` maps
    slot -> price(t), the price a poll at `t` reads (300 cents otherwise); the takings stay at 300 a unit."""
    from tests.conftest import TEST_SLOTS as SLOTS

    sales = sales or {}
    lines = [(slot, sku, "primary", paid, 10, 0, 0) for slot, sku, paid in (
        ("15", "AAA", "10.00"), ("16", "BBB", "10.00"), ("17", "CCC", "10.00"), ("18", "FFF", "10.00"),
        ("25", "EEE", "10.00"), ("26", "GGG", "10.00"), ("28", "HHH", "20.00"))]
    if full:
        lines += [(slot, f"X{slot}", "primary", "5.00", 5, 0, 0) for slot in SLOTS if slot not in {l[0] for l in lines}]
    fulfilled(conn, "r0", R0, *lines)
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
            extra = {slot: sum(c for at, c in (takings or {}).get(slot, []) if at <= t) for slot in units}
            price = {slot: (prices or {}).get(slot, lambda t: 300)(t) for slot in units}
            store_poll(conn, f"p{n}", t.isoformat(),
                       {slot: (price[slot], u, 300 * u + extra[slot]) for slot, u in units.items()})
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


def test_the_old_products_sale_split_at_a_swap_doesnt_make_the_slot_quiet_later(conn):
    # Review round 1, item 5. Slot 17 held OLD ($2.00) until the 16 Oct visit swapped it for CCC. The
    # window from 18:00 to 22:00 holds the visit, and its cents say the 1 unit sold was OLD: booked to the
    # 22:00 poll, but taken at the swap. CCC has sold nothing, so 17 has been quiet since 20:00, as 28 has.
    # On a tie the lower margin goes first: 17's CCC cost $3.00 a unit, 28's HHH $2.00.
    fulfilled(conn, "rp", datetime(2026, 10, 9, 20, tzinfo=UTC), ("17", "OLD", "primary", "10.00", 10, 0, 0))
    sold = datetime(2026, 10, 16, 19, tzinfo=UTC)
    machine(conn, sales={"17": [(sold, 1)]}, takings={"17": [(sold, -100)]},
            prices={"17": lambda t: 200 if t < R0 else 300})
    conn.execute("update purchase_lines set units_pulled = 9, pulled_sku = 'OLD', slot_reassigned = true,"
                 " total_paid = 30 where request_id = 'r0' and slot = '17'")
    count(conn, "17", 10)
    count(conn, "28", 10)
    old = [s for s in stock.stock(conn, until=NOW).sales if s.slot == "17"]
    assert [(s.product, s.at, s.taken_at) for s in old] == [("OLD", datetime(2026, 10, 16, 22, tzinfo=UTC), R0)]
    silent = report(conn).silent
    assert [(s.slot, s.quiet_since) for s in silent[:2]] == [("17", R0), ("28", R0)]


def test_a_sale_booked_at_the_runs_first_load_empties_the_slot(conn):
    # Review round 3, case (h). A poll at the 16 Oct load's own moment reads all 10 of slot 17's units sold. The
    # replay applies the load, then the sale, so 17 stood empty until 5 more went in on Tue 27 Oct. Since the
    # window began it held stock as Wed 28 and Thu 29 began, so 2 of 4 days: not silent after 4.
    machine(conn, sales={"17": [(R0, 10)]})
    store_poll(conn, "at-load", R0.isoformat(), {"17": (300, 110, 33000)})
    fulfilled(conn, "rt", datetime(2026, 10, 27, 15, tzinfo=UTC), ("17", "CCC", "primary", "5.00", 5, 0, 0))
    count(conn, "17", 5, at=datetime(2026, 10, 28, 15, tzinfo=UTC))
    assert [(s.at, s.product, s.units) for s in stock.stock(conn, until=NOW).sales if s.slot == "17"] == [
        (R0, "CCC", 10)]
    assert why(report(conn), "17") == f"2 of {SILENT_DAYS} in-stock class days seen since the window began; not silent yet"


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


def test_takings_that_rise_with_no_unit_counted_are_not_silence(conn):
    """16 took $2.50 on Tuesday, but its units counter stayed flat: the counters disagree, so it isn't silent."""
    machine(conn, takings={"16": [(datetime(2026, 10, 27, 15, tzinfo=UTC), 250)]})
    r = report(conn)
    assert "16" not in [s.slot for s in r.silent]
    assert why(r, "16").startswith("unknown: its takings rose $2.50 since the window began")


def test_a_pull_since_the_window_began_is_not_silence(conn):
    """An unavailable switch pulls all 12 BBB from 16 on Sunday; BBB goes back in on Thursday evening. The
    receipts say it holds stock again, but it stood empty through class days, so it isn't silent."""
    machine(conn)
    sunday, thursday = datetime(2026, 10, 25, 16, tzinfo=UTC), datetime(2026, 10, 29, 21, tzinfo=UTC)
    conn.execute("insert into purchase_requests (request_id, submitted_at, off_cycle, request_body, status,"
                 " fulfilled_at) values ('sun', %s, true, '{}', 'fulfilled', %s),"
                 " ('thu', %s, true, '{}', 'fulfilled', %s)",
                 (sunday - timedelta(days=1), sunday, thursday - timedelta(days=1), thursday))
    conn.execute(
        "insert into purchase_lines (request_id, line_no, slot, source, asked, outcome, sku, packs_bought,"
        " units_bought, total_paid, units_loaded, units_lost, units_to_back_stock, units_from_back_stock,"
        " units_pulled, pulled_sku, slot_reassigned, resolved_at) values"
        " ('sun', 0, '16', 'purchase', '{}', 'unavailable', 'ZZZ', 0, 0, null, 0, 0, 0, 0, 12, 'BBB', false, %s),"
        " ('thu', 0, '16', 'back_stock', '{}', 'loaded', 'BBB', 0, 0, null, 10, 0, 0, 10, 0, null, false, %s)",
        (sunday, thursday))
    r = report(conn)
    assert "16" not in [s.slot for s in r.silent]
    assert why(r, "16") == ("unknown: a receipt pulled 12 units from it on 2026-10-25, since the window began, so "
                            "it may have stood empty")


def test_a_dated_request_with_no_lines_since_the_window_began_blocks_every_silent_slot(conn):
    """After the opening, fulfilled, dated, and no lines: nothing says what that visit did, so no slot is silent."""
    from tests.test_ledger import open_ledger

    open_ledger(conn, at=datetime(2026, 10, 1, tzinfo=UTC))
    machine(conn)
    assert [s.slot for s in report(conn).silent] == ["16"]
    for request_id, off_cycle in (("r9", False), ("m9", True)):
        conn.execute("delete from purchase_requests where request_id in ('r9', 'm9')")
        conn.execute("insert into purchase_requests (request_id, submitted_at, off_cycle, request_body, status,"
                     " fulfilled_at) values (%s, %s, %s, '{}', 'fulfilled', %s)",
                     (request_id, VISIT, off_cycle, datetime(2026, 10, 27, 15, tzinfo=UTC)))
        r = report(conn)
        assert r.silent == () and request_id in r.reason and "no lines" in r.reason, r
        assert all(n.why == r.reason for n in r.not_silent)


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



def test_an_undated_friday_request_with_no_lines_still_blocks_after_the_opening(conn):
    """A request with no lines can't be dated by its lines; with no fulfilled_at, the visit stays unknown."""
    from tests.test_ledger import open_ledger

    open_ledger(conn, at=datetime(2026, 10, 1, tzinfo=UTC))
    machine(conn)
    conn.execute("insert into purchase_requests (request_id, submitted_at, off_cycle, request_body, status)"
                 " values ('r9', %s, false, '{}', 'fulfilled')", (VISIT,))
    r = report(conn)
    assert r.window_start is None and r.silent == () and "r9" in r.reason


def test_a_test_purchase_before_the_ledger_opened_is_no_friday_visit(conn):
    """Main's rule (stock._receipts): receipts from before the ledger's opening don't count, so an old test
    request with no fulfilled_at neither hides the last Friday visit nor counts as a load at one."""
    from tests.test_ledger import open_ledger

    opened = datetime(2026, 10, 1, tzinfo=UTC)
    open_ledger(conn, at=opened)
    machine(conn, sales={"18": [(datetime(2026, 10, 27, 15, tzinfo=UTC), 1)]})
    fulfilled(conn, "t0", datetime(2026, 9, 21, 15, tzinfo=UTC), ("16", "ZZZ", "primary", "5.00", 5, 0, 0))
    conn.execute("update purchase_requests set fulfilled_at = null where request_id = 't0'")
    r = report(conn)
    assert r.reason is None and r.window_start == WINDOW and [s.slot for s in r.silent] == ["16"]
    assert stock.request_loads(conn, ["t0"]) == {}
    assert all(at >= opened for slot_loads in stock.loads(conn).values() for at, _ in slot_loads)



def test_loads_and_pulls_are_on_the_receipt_clock(conn):
    """With door times the stock replay pulls at door open and loads at door close (#168), not at resolved_at.
    The trial's loads and pulls read the same clock, so a swap's pull can't land after its load, and a load
    after `until` hasn't happened yet."""
    machine(conn)
    opened, closed = VISIT - timedelta(hours=2), VISIT - timedelta(hours=1, minutes=30)
    conn.execute("update purchase_requests set door_opened_at = %s, door_closed_at = %s where request_id = 'r1'",
                 (opened, closed))
    conn.execute("update purchase_lines set units_pulled = 10, pulled_sku = 'EEE' where request_id = 'r1' and slot = '25'")
    assert stock.loads(conn)["25"] == [(R0, 10), (closed, 10)]
    assert stock.loads(conn, until=closed - timedelta(minutes=1))["25"] == [(R0, 10)]
    assert pulls(conn)["25"] == [(opened, 10)]

def test_a_slot_emptied_by_a_recorded_loss_isnt_in_stock_until_it_is_refilled(conn):
    """16 holds 12 BBB after the 23 Oct visit, loses all 12 the next day (a recorded coil loss), and gets 6 more
    on Thursday evening. The polls see no sale, but it held nothing for most of the week: not silent."""
    machine(conn, sales={"18": [(datetime(2026, 10, 27, 15, tzinfo=UTC), 1)]})
    conn.execute("insert into purchase_line_losses (request_id, line_no, position, location, sku, units, recorded_at)"
                 " values ('r1', 0, 0, 'coil', 'BBB', 12, %s)", (datetime(2026, 10, 24, 15, tzinfo=UTC),))
    fulfilled(conn, "r2", THURSDAY_EVENING - timedelta(hours=1), ("16", "BBB", "primary", "6.00", 6, 0, 0))
    conn.execute("update purchase_requests set off_cycle = true where request_id = 'r2'")
    r = report(conn)
    assert r.window_start == WINDOW and "16" not in [s.slot for s in r.silent]
    assert why(r, "16").endswith("in-stock class days seen since the window began; not silent yet")

# --- What the agent and the planner see ----------------------------------------------------------------

def trial_sales(extra=None):
    """The wildcard, 15, sells one a day on 19-22 and 26 Oct: 5 over 9 in-stock class days, a first-look pass."""
    days = [datetime(2026, 10, d, 15, tzinfo=UTC) for d in (19, 20, 21, 22, 26)]
    return {"15": [(at, 1) for at in days], **(extra or {})}


def test_a_pass_with_a_silent_slot_comes_with_a_proposal(conn):
    from vending import config
    from vending.tools.machine_state import get_machine_state

    machine(conn, sales=trial_sales(), full=True)
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

    machine(conn, sales=trial_sales({"16": [(datetime(2026, 10, 27, 15, tzinfo=UTC), 1)]}), full=True)
    config.set_value(conn, "wildcard_slot", "15", "test")
    t = get_machine_state(conn, now=NOW).wildcard_trial
    assert t.verdict == "pass" and t.silent_slots == [] and t.proposal is None
    assert "keep it in the wildcard" in t.reason


def test_no_proposal_unless_it_passes(conn):
    from vending import config
    from vending.tools.machine_state import get_machine_state

    machine(conn, sales={"15": [(datetime(2026, 10, 19, 15, tzinfo=UTC), 3)]}, full=True)
    config.set_value(conn, "wildcard_slot", "15", "test")
    t = get_machine_state(conn, now=NOW).wildcard_trial
    assert t.verdict == "extend" and [s.slot for s in t.silent_slots] == ["16"] and t.proposal is None


def test_the_planner_gets_the_verdict_and_the_silent_slots(conn):
    from tests.test_ledger import open_ledger
    from vending import config, planner, planning, wildcard_trial

    assert planner.PASS_VERDICT == wildcard_trial.PASS
    open_ledger(conn, at=datetime(2026, 10, 1, tzinfo=UTC))  # before the October receipts, which it would drop
    machine(conn, sales=trial_sales(), full=True)
    gathered = planning.gather(conn, [], NOW)
    assert (gathered.current.wildcard_verdict, gathered.current.silent) == (None, frozenset())   # no wildcard set
    config.set_value(conn, "wildcard_slot", "15", "test")
    gathered = planning.gather(conn, [], NOW)
    assert (gathered.current.wildcard_verdict, gathered.current.silent) == ("pass", frozenset({"16"}))


# 18 was 3 units of OLD; at a swap it took 6 of FFF, and 3 units sold in a window the replay can't split
# (#173): on hand is 3 to 6 of FFF.
SPLIT = stock.SlotCount(slot="18", product="FFF", units=3, unit_cost=None, loaded=6, sold=0, lost=0,
                        since=R0, unattributed=3, at_most=6)


def _look(units, product="FFF"):
    return {"count_id": "c1", "counted_at": VISIT + timedelta(days=1), "source": "door", "product": product,
            "units_counted": units}


def test_a_count_anywhere_in_the_range_unattributed_sales_leave_agrees_with_the_receipts():
    for units in (3, 5, 6):
        evidence, doubt = _stock_evidence(SPLIT, WINDOW, [], [(R0, 6)], _look(units))
        assert doubt is None and evidence.endswith(f"{units} units of FFF"), (units, evidence)
    evidence, doubt = _stock_evidence(SPLIT, WINDOW, [], [(R0, 6)], _look(7))
    assert evidence == CONFLICT and "holds 3 to 6 of FFF" in doubt


def test_a_slot_unattributed_sales_may_have_emptied_is_unconfirmed_not_empty():
    maybe = replace(SPLIT, units=0)
    evidence, doubt = _stock_evidence(maybe, WINDOW, [], [(R0, 6)], None)
    assert evidence is None and "holds 0 to 6 of FFF" in doubt and "may be empty" in doubt
    gone = replace(SPLIT, units=0, at_most=0)
    assert _stock_evidence(gone, WINDOW, [], [(R0, 6)], None) == (None, "the receipts say it is empty")
    assert _stock_evidence(maybe, WINDOW, [], [(R0, 6)], _look(2))[1] is None   # a count settles it


def test_a_promotion_pulls_the_range_the_receipts_allow():
    trial = Trial(SlotRecord("15", "AAA", R0, 5, 5, None), "15", False, PASS, "passes", 300)
    report = SilentReport(WINDOW, (Silent("18", "FFF", 4, R0, None, "loaded"),), (), ())
    assert propose(trial, report, {"18": SPLIT}, 650).pulls == "FFF (3 to 6 units to the closet)"
    exact = replace(SPLIT, unattributed=0, at_most=None)
    assert propose(trial, report, {"18": exact}, 650).pulls == "FFF (3 units to the closet)"
