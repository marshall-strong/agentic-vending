"""The midweek wake-up (#219): the projection, the planner's plan, the weekly cap, the job and the surfaces.

The decision reuses the planner. `window` and `at_risk` are tested pure; `decide` is
tested pure on a hand-built `planning.Gathered`, so the planner's real `plan_restock`
prices the candidate visit: whole packs, the fee, the closet's same-product lots, and
every gate. The database tests build a full machine from synthetic receipts, polls,
a synced catalog and the sourcing table, and walk the worker's pass (`check`),
`--check` and the halt switch.
"""

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction

import pytest

from tests.conftest import TEST_SLOTS, store_poll
from tests.test_dashboard import fulfilled
from vending import config, db, ledger, midweek_wake, planner, planning, settings, sourcing, submissions
from vending.midweek_wake import Outlook, at_risk, decide, window

# Monday 7 Dec 2026, noon Eastern (EST, UTC-5): inside the term, week of Sat 5 Dec.
MONDAY = datetime(2026, 12, 7, 17, tzinfo=timezone.utc)
MONDAY_0730 = datetime(2026, 12, 7, 12, 30, tzinfo=timezone.utc)   # 7:30 AM Eastern
MONDAY_2000 = datetime(2026, 12, 8, 1, tzinfo=timezone.utc)        # 8 PM Eastern, Monday
TUESDAY = MONDAY + timedelta(days=1, hours=1)          # Tue 8 Dec, 1pm Eastern
WEDNESDAY = MONDAY + timedelta(days=2, hours=2)        # Wed 9 Dec, 2pm Eastern
SATURDAY_BEFORE = MONDAY - timedelta(days=2)           # Sat 5 Dec, noon Eastern
BREAK_MONDAY = datetime(2026, 12, 21, 17, tzinfo=timezone.utc)  # winter break week: no class days
# The briefing fixture's week (tests.test_briefing's NOW, Thu 12 Nov 2026, is in this week).
BRIEFING_WEEK = "2026-11-07"

SKU, CHEAP = "WAKE-SKU", "CHEAP-SKU"
PRICE = 450                                            # $4.50: the mid-premium fall prior, 1.354 a class day
MARGIN = Fraction(PRICE) / Fraction(108, 100) - Fraction(1014, 6)   # $4.50 ex tax less $1.69 a can: 247.67 cents
SAVED_A_SLOT = Fraction("1.354") * 2 * MARGIN          # empty at noon Monday: Thursday and Friday saved


# --- the pure parts: a hand-built machine for the real planner ----------------------


def table(max_cents=1014, expected_cents=1014):
    """The sourcing table as the planner gets it: WAKE-SKU at Walmart, a 6-pack."""
    return sourcing.to_planner([sourcing.Option(
        sku=SKU, rank="primary", store="Walmart", units_per_pack=6, expected_cents=expected_cents,
        max_cents=max_cents, url=None, pack_description=None, checked_at=MONDAY, checked_by="tester")])


def machine(empty=(), *, low=None, closet=(), balance=60000, pending=(), max_cents=1014, fee=2500,
            halted=False, unknown=(), now=MONDAY, on_hand_cost=Fraction(169)):
    """Every test slot holds WAKE-SKU (12 oz cans, 8 a column) at $4.50: 8 on hand, or none
    for `empty`, or `low[slot]`. Slots in `unknown` have no receipt. Returns the gathered
    facts the planner plans on."""
    low = low or {}
    slots = []
    for s in TEST_SLOTS:
        units = 0 if s in empty else low.get(s, 8)
        if s in unknown:
            slots.append(planner.SlotState(s, None, None, 0, None, contents_known=False))
        else:
            slots.append(planner.SlotState(s, SKU, PRICE, units, on_hand_cost))
    catalog = (planner.CatalogProduct(SKU, True, "12 fl oz can"),
               planner.CatalogProduct(CHEAP, True, "12 fl oz can"))
    current = planner.CurrentState(tuple(slots), tuple(closet), tuple(pending), catalog,
                                   table(max_cents), balance)
    cfg = planner.Config(slots=TEST_SLOTS, midweek_fee_cents=fee, halted=halted)
    return planning.Gathered(current, cfg, now, None, ())


def outlooks(empty=(), *, rate="1.354", low=None, unknown_rate=()):
    """The projection for `machine`: empty and low slots at `rate`; the rest hold 8 at the
    mid-premium prior (1.354), which no visit this week needs to touch."""
    low = low or {}

    def one(s):
        if s in unknown_rate:
            return Outlook(s, None, 0 if s in empty else low.get(s, 8))
        if s in empty or s in low:
            return Outlook(s, Decimal(rate), 0 if s in empty else low[s])
        return Outlook(s, Decimal("1.354"), 8)
    return [one(s) for s in TEST_SLOTS]


def wake_for(empty, now=MONDAY, rate="1.354", **kw):
    return decide(now, outlooks(empty, rate=rate), machine(empty, now=now, **kw))


def cents(value: Fraction) -> int:
    return midweek_wake._whole_cents(value)


FOUR = TEST_SLOTS[:4]


def test_monday_fast_sellers_wake_with_the_docs_numbers():
    # 4 slots empty at noon Monday at $4.50, cans at most $1.69: each saves Thursday and
    # Friday, 1.354 x 2 = 2.708 units x $2.4767 = $6.71; 4 slots $26.83 > the $25 fee.
    wake = wake_for(FOUR)
    assert wake is not None
    assert wake.week_start.isoformat() == "2026-12-05"          # the Saturday that starts the week
    assert wake.slots == FOUR
    assert wake.saved_margin_cents == cents(4 * SAVED_A_SLOT) == 2683
    assert wake.visit_cost_cents == 4 * 2 * 1014 + 2500           # the planner: 2 whole 6-packs a slot, plus the fee
    assert "empty now" in wake.reason and "Wednesday afternoon" in wake.reason
    assert "$26.83" in wake.reason and "$25.00" in wake.reason and "$106.12" in wake.reason


def test_one_and_three_empty_slots_never_pay():
    assert wake_for(FOUR[:1]) is None
    assert wake_for(FOUR[:3]) is None                             # 3 x $6.71 = $20.12 < $25
    assert wake_for(FOUR) is not None


def test_a_730_am_projection_counts_mondays_demand():
    """Sol, round 2: the check after the 7 AM sync must count the rest of Monday, apart from
    the two class days the visit takes to land (Tuesday and Wednesday)."""
    early = window(MONDAY_0730)
    assert early.before_landing == 2 + Fraction(99, 100)          # 0.99 of Monday is still to sell
    assert early.after_landing == 2                               # Thursday and Friday
    assert window(MONDAY_2000).before_landing == 2                # nothing of Monday is left at 8 PM
    # 4 cans at 1 a class day: by Wednesday's landing 2.99 have sold at 7:30, leaving 1.01 for
    # the 2 class days after it, so the slot is at risk; counted from 8 PM it is not.
    slot = [Outlook(TEST_SLOTS[0], Decimal(1), 4)]
    [risk] = at_risk(early, slot)
    assert risk.short == Fraction(99, 100)
    assert at_risk(window(MONDAY_2000), slot) == []


def test_sixteen_unrelated_cheap_cans_in_the_closet_dont_trigger_a_wake():
    """Sol, round 2: the closet is drawn only for the same product (the planner's allocation),
    so 16 cheap cans of something else never price three WAKE-SKU loads under $25 of fee."""
    cheap = (planner.ClosetStock(CHEAP, 16, 50),)
    assert wake_for(FOUR[:3], closet=cheap) is None
    # The same 16 cans of WAKE-SKU do count: two slots load from them at 50 cents, oldest first.
    same = wake_for(FOUR[:3], closet=(planner.ClosetStock(SKU, 16, 50),))
    assert same is not None
    saved = 2 * Fraction("2.708") * (Fraction(PRICE) / Fraction(108, 100) - 50) + Fraction("2.708") * MARGIN
    assert same.saved_margin_cents == cents(saved)
    assert same.visit_cost_cents == 2 * 1014 + 2500               # only the third slot buys


def test_a_spendable_balance_under_the_planners_cost_is_no_wake():
    """Sol, round 2: $54.44 spendable against the planner's $65.56 (two slots, two 6-packs
    each at $10.14, plus the $25 fee) is the planner's insufficient_balance: no wake."""
    two = FOUR[:2]
    logged = []
    assert decide(MONDAY, outlooks(two, rate="3"), machine(two, balance=5444), log=logged.append) is None
    assert any("insufficient_balance" in line and "$65.56" in line for line in logged)
    wake = decide(MONDAY, outlooks(two, rate="3"), machine(two, balance=6556))
    assert wake is not None and wake.visit_cost_cents == 6556
    assert wake.saved_margin_cents == cents(2 * 6 * MARGIN)       # 3 a class day x 2 days, under a column


def test_raising_only_a_pack_max_lowers_the_saved_margin():
    """Sol, round 2: the unit cost basis is the planner's most we pay, so a higher max on the
    same pack (expected price unchanged) lowers the margin each saved unit keeps."""
    base = wake_for(FOUR, fee=0)
    dearer = wake_for(FOUR, fee=0, max_cents=1200)
    assert dearer.saved_margin_cents < base.saved_margin_cents
    assert dearer.saved_margin_cents == cents(4 * Fraction("2.708") * (Fraction(PRICE) / Fraction(108, 100) - 200))


def test_saved_units_are_capped_at_what_the_plan_loads():
    # 4.5 a class day empty now: 9 units short after the landing, but the plan fills the
    # column, 8 cans, so each slot saves 8.
    wake = wake_for(FOUR, rate="4.5")
    assert wake.saved_margin_cents == cents(4 * 8 * MARGIN)
    # A slot with 2 left at 3 a class day sells out before the landing; the plan loads 6.
    low = {TEST_SLOTS[0]: 2}
    wake = decide(MONDAY, outlooks(rate="3", low=low), machine(low=low, fee=0))
    assert wake.saved_margin_cents == cents(6 * MARGIN)


def test_units_staying_on_the_shelf_dont_set_the_basis():
    # 2 cans already in the slot cost $3.00 each; the visit loads 6 new ones at $1.69 at most.
    # The basis is what the visit loads, not the dearer cans that stay.
    low = {TEST_SLOTS[0]: 2}
    wake = decide(MONDAY, outlooks(rate="3", low=low), machine(low=low, fee=0, on_hand_cost=Fraction(300)))
    assert wake.saved_margin_cents == cents(6 * MARGIN)


def test_units_lost_before_the_landing_count_nothing():
    # Empty at noon Monday at 1.354: about 3 units are lost before Wednesday's landing either
    # way; only Thursday's and Friday's 2.708 are saved.
    [risk] = at_risk(window(MONDAY), [Outlook(TEST_SLOTS[0], Decimal("1.354"), 0)])
    assert risk.short == Fraction("2.708")


def test_any_planner_refusal_records_nothing():
    pending = (planner.PendingRequest("still-open"),)
    logged = []
    assert decide(MONDAY, outlooks(FOUR), machine(FOUR, pending=pending), log=logged.append) is None
    assert any("request_pending" in line for line in logged)
    assert wake_for(FOUR, halted=True) is None                    # the planner's halted refusal


def test_unknown_projection_or_contents_is_skipped_not_guessed():
    # A fifth empty slot with no rate is not at risk; one with no receipt is left out of the plan.
    five = TEST_SLOTS[:5]
    assert decide(MONDAY, outlooks(five, unknown_rate=five[4:]), machine(five)).slots == FOUR
    logged = []
    wake = decide(MONDAY, outlooks(five), machine(five, unknown=five[4:]), log=logged.append)
    assert wake is None                                           # the unknown slot is kept: price_unknown
    assert any("doesn't know its product or price" in line for line in logged)


def test_back_stock_of_unknown_cost_counts_nothing():
    closet = (planner.ClosetStock(SKU, 8, None),)
    logged = []
    wake = decide(MONDAY, outlooks(FOUR), machine(FOUR, closet=closet), log=logged.append)
    assert wake is None                                           # one slot's saving is unknown: 3 x $6.71
    assert any("cost of what the visit loads is unknown" in line for line in logged)


def test_an_unused_lot_of_unknown_cost_leaves_the_drawn_lots_priced():
    """Sol, round 3: two slots draw 16 known cans at 50 cents; 4 more WAKE-SKU of unknown cost
    can't cover the third slot's 8, so it buys and those 4 stay unused. Only the lots a line
    draws decide whether its cost is known, so the wake keeps its $26.57."""
    closet = (planner.ClosetStock(SKU, 16, 50), planner.ClosetStock(SKU, 4, None))
    wake = wake_for(FOUR[:3], closet=closet)
    assert wake is not None
    saved = 2 * Fraction("2.708") * (Fraction(PRICE) / Fraction(108, 100) - 50) + Fraction("2.708") * MARGIN
    assert wake.saved_margin_cents == cents(saved) == 2657
    plan = planning.plan_gathered(machine(FOUR[:3], closet=closet),
                                  midweek_wake.candidate(at_risk(window(MONDAY), outlooks(FOUR[:3])),
                                                         machine(FOUR[:3], closet=closet))[0])
    assert [f.units_loaded_cost_unknown for f in plan.floors if f.slot in FOUR[:3]] == [0, 0, 0]


def test_no_decision_after_tuesday_or_before_the_week():
    assert wake_for(FOUR, now=WEDNESDAY) is None                  # the landing is Friday: nothing to save
    assert wake_for(FOUR, now=SATURDAY_BEFORE) is None            # Monday decides, on fresher data
    assert wake_for(FOUR, now=TUESDAY, rate="3") is not None      # Tuesday's visit saves Friday


def test_break_week_finds_no_class_days_to_save():
    assert wake_for(FOUR, now=BREAK_MONDAY) is None


def test_naive_clock_is_a_guess_and_is_refused():
    with pytest.raises(ValueError):
        window(MONDAY.replace(tzinfo=None))


# --- the week cap and the request, on the database -------------------------------


def week_row(week="2026-12-05", slots=("15", "16")):
    return midweek_wake.Wake(datetime.fromisoformat(week).date(), "Slots 15 and 16 would sell out.",
                             slots, 2600)


def test_the_cap_is_the_unique_index(conn):
    assert midweek_wake.record(conn, week_row()) is not None
    assert midweek_wake.record(conn, week_row()) is None        # same week: refused
    assert conn.execute("select count(*) from midweek_wakeups").fetchone()[0] == 1
    assert midweek_wake.record(conn, week_row(week="2026-12-12")) is not None   # a later week is free
    assert conn.execute("select count(*) from midweek_wakeups").fetchone()[0] == 2


def test_request_midweek_run_records_once_and_logs(conn, capsys):
    assert not midweek_wake.request_midweek_run(conn, "Slots 15 and 16 would sell out.", now=MONDAY)
    midweek_wake.record(conn, week_row())
    assert midweek_wake.request_midweek_run(conn, "Slots 15 and 16 would sell out.", by="tester", now=MONDAY)
    row = midweek_wake.current(conn, MONDAY)
    assert row["requested_at"] is not None and row["requested_by"] == "tester"
    first = row["requested_at"]
    assert not midweek_wake.request_midweek_run(conn, "again", by="other", now=MONDAY)
    row = midweek_wake.current(conn, MONDAY)
    assert row["requested_at"] == first and row["requested_by"] == "tester"
    assert "midweek run requested" in capsys.readouterr().out


def test_current_reads_this_week_only(conn):
    assert midweek_wake.current(conn, MONDAY) is None
    midweek_wake.record(conn, week_row())
    row = midweek_wake.current(conn, MONDAY)
    assert row["week_start"].isoformat() == "2026-12-05"
    assert row["slots"] == ["15", "16"] and row["saved_margin_cents"] == 2600
    assert midweek_wake.current(conn, MONDAY + timedelta(days=8)) is None   # next week


# --- the worker's job, on stored facts -------------------------------------------


def catalog_and_sourcing(conn, at=MONDAY):
    """WAKE-SKU in our synced catalog copy (a 12 fl oz can: 8 a column) and on the sourcing
    table at $10.14 a 6-pack ($1.69 a can). Idempotent."""
    conn.execute("insert into catalog_products (sku, name, size, last_response)"
                 " values (%s, 'Wake Cola', '12 fl oz can', %s) on conflict (sku) do nothing",
                 (SKU, json.dumps({"sku": SKU, "size": "12 fl oz can", "active": True})))
    conn.execute("insert into catalog_syncs (synced_at, product_count) values (%s, 1)", (at - timedelta(hours=1),))
    sourcing.set_option(conn, sourcing.Option(
        sku=SKU, rank="primary", store="Walmart", units_per_pack=6, expected_cents=1014,
        max_cents=1014, url=None, pack_description=None,
        checked_at=at - timedelta(days=2), checked_by="tester"), at)


def fast_week(conn, empty=FOUR, *, tag="a", monday=MONDAY, opening=60000, sales_cents=None, halted=False):
    """A full machine: every test slot loaded Friday evening with WAKE-SKU at $1.69 a can and
    $4.50 on the shelf. Slots in `empty` got 6 and sold all 6 by Monday's poll; the rest got 8
    and sold none. The ledger is opened ($600.00), the halt switch is off unless `halted`.
    At noon Monday each empty slot saves $6.71 at the mid-premium prior, so four make $26.83
    against the $25 fee. `sales_cents` overrides the polls' lifetime sales cents."""
    catalog_and_sourcing(conn, monday)
    config.set_halted(conn, halted, "tester")
    if conn.execute("select 1 from ledger_entries where kind = 'opening'").fetchone() is None:
        ledger.record(conn, "opening", opening, "opening capital", "tester", at=monday - timedelta(days=4))
    friday_evening = monday - timedelta(days=2, hours=19)       # the Friday 22:00 UTC before
    for slot in TEST_SLOTS:
        loaded = 6 if slot in empty else 8
        fulfilled(conn, f"wake-{tag}-{slot}", slot, SKU, units_loaded=loaded,
                  paid=f"{loaded * Decimal('1.69'):.2f}", at=friday_evening)

    def readings(units):
        return (PRICE, units) if sales_cents is None else (PRICE, units, sales_cents)

    store_poll(conn, f"wake-before-{tag}", (friday_evening - timedelta(hours=2)).isoformat(),
               {s: readings(100) for s in TEST_SLOTS})
    store_poll(conn, f"wake-monday-{tag}", (monday - timedelta(hours=1)).isoformat(),
               {s: readings(106 if s in empty else 100) for s in TEST_SLOTS})


def test_fast_seller_week_wakes_exactly_once(conn):
    fast_week(conn)
    assert midweek_wake.check(conn, now=MONDAY) is True
    row = midweek_wake.current(conn, MONDAY)
    assert row["slots"] == list(FOUR)
    assert row["requested_at"] is not None and row["requested_by"] == "midweek_wake"
    assert "Wednesday afternoon" in row["reason"] and "$26.83" in row["reason"]
    assert midweek_wake.check(conn, now=MONDAY + timedelta(hours=1)) is False   # same week: held
    assert midweek_wake.check(conn, now=TUESDAY) is False
    assert conn.execute("select count(*) from midweek_wakeups").fetchone()[0] == 1


def test_the_check_writes_no_purchase_request(conn):
    fast_week(conn)
    before = conn.execute("select count(*) from purchase_requests").fetchone()[0]
    assert midweek_wake.check(conn, now=MONDAY) is True
    assert conn.execute("select count(*) from purchase_requests").fetchone()[0] == before


def test_normal_week_wakes_nothing(conn, capsys):
    fast_week(conn, empty=())                                   # every slot holds 8
    assert midweek_wake.check(conn, now=MONDAY) is False
    assert midweek_wake.current(conn, MONDAY) is None
    assert "no slot is projected" in capsys.readouterr().out


def test_the_halt_switch_records_nothing_and_keeps_the_week(conn, capsys):
    """Sol, round 2: halted, the planner would refuse the visit, so nothing is recorded; once
    a person turns it off, a later round the same week still wakes."""
    fast_week(conn, halted=True)
    assert midweek_wake.check(conn, now=MONDAY) is False
    assert midweek_wake.current(conn, MONDAY) is None
    assert "halt switch is on" in capsys.readouterr().out
    config.set_halted(conn, False, "tester")
    assert midweek_wake.check(conn, now=MONDAY + timedelta(minutes=30)) is True
    assert midweek_wake.current(conn, MONDAY)["slots"] == list(FOUR)


def test_a_stale_poll_refuses_the_check(conn, capsys):
    fast_week(conn)
    conn.execute("delete from polls where started_at > %s", (MONDAY - timedelta(hours=4),))
    assert midweek_wake.check(conn, now=MONDAY) is False
    assert "stale" in capsys.readouterr().out.lower()


def test_check_without_polls_skips(conn, capsys):
    config.set_halted(conn, False, "tester")
    assert midweek_wake.check(conn, now=MONDAY) is False
    assert "skipped" in capsys.readouterr().out


def test_an_open_request_blocks_the_wake(conn, capsys):
    """A purchase request still pending can't be followed by a visit (the planner's
    request_pending), so nothing is recorded and the week stays open."""
    fast_week(conn)
    conn.execute("insert into purchase_requests (request_id, submitted_at, off_cycle, request_body, status)"
                 " values ('wake-pending', %s, false, '{}', 'pending')", (MONDAY - timedelta(days=1),))
    assert midweek_wake.check(conn, now=MONDAY) is False
    assert midweek_wake.current(conn, MONDAY) is None
    assert "request_pending" in capsys.readouterr().out


def test_a_balance_under_the_visit_blocks_the_wake(conn, capsys):
    """$10.00 in, $256.88 of receipts and no takings: the planner's insufficient_balance."""
    fast_week(conn, tag="poor", opening=1000, sales_cents=0)
    assert midweek_wake.check(conn, now=MONDAY) is False
    assert midweek_wake.current(conn, MONDAY) is None
    assert "insufficient_balance" in capsys.readouterr().out


@pytest.mark.parametrize("status", ["sending", "unknown"])
def test_an_unresolved_submission_blocks_the_wake_until_it_is_settled(conn, capsys, status):
    """Sol, round 3: preview and submit refuse while a submission is sending or unknown
    (`submissions.unresolved`), so the wake must too, recording nothing. Once a person
    settles it, a later round the same week still wakes."""
    fast_week(conn)
    row = conn.execute("insert into purchase_submissions (plan_hash, body, status)"
                       " values ('h', %s::jsonb, %s) returning id",
                       (json.dumps({"off_cycle": False, "lines": []}), status)).fetchone()[0]
    assert midweek_wake.check(conn, now=MONDAY) is False
    assert midweek_wake.current(conn, MONDAY) is None
    assert "submission_unresolved" in capsys.readouterr().out
    submissions.resolve(conn, row, None, "tester")
    assert midweek_wake.check(conn, now=MONDAY + timedelta(minutes=30)) is True
    assert midweek_wake.current(conn, MONDAY)["slots"] == list(FOUR)


def test_an_empty_catalog_copy_skips_without_the_bridge(conn, capsys):
    fast_week(conn)
    conn.execute("delete from catalog_syncs")
    assert midweek_wake.check(conn, now=MONDAY) is False
    out = capsys.readouterr().out
    assert "skipped" in out and "never calls the bridge" in out


def test_no_wake_before_the_competition_starts(conn, capsys):
    """Fall data (before the 3 Dec start) triggers nothing: the decision and the worker
    job are gated on the start before any gathering."""
    fall_monday = datetime(2026, 11, 16, 17, tzinfo=timezone.utc)   # Mon 16 Nov, noon ET
    fast_week(conn, monday=fall_monday, tag="fall")
    assert midweek_wake.check(conn, now=fall_monday) is False
    assert midweek_wake.evaluate(conn, fall_monday) is None
    assert midweek_wake.current(conn, fall_monday) is None
    assert "3 Dec" in capsys.readouterr().out


def test_job_gates_on_the_environment(conn, monkeypatch):
    monkeypatch.delenv("MIDWEEK_WAKE", raising=False)
    monkeypatch.setattr(settings, "_FILE", {})

    def no_connection(*a, **k):
        raise AssertionError("the off job must not open a database connection")

    monkeypatch.setattr(db, "connect", no_connection)
    assert midweek_wake.job() == 0
    monkeypatch.setenv("MIDWEEK_WAKE", "off")
    assert midweek_wake.job() == 0
    assert midweek_wake.main(["--check"]) == 0
    assert conn.execute("select count(*) from midweek_wakeups").fetchone()[0] == 0


def _on_with(conn, monkeypatch):
    monkeypatch.setenv("MIDWEEK_WAKE", "on")
    monkeypatch.setattr(midweek_wake.ledger, "db_now", lambda _conn: MONDAY)  # the real clock isn't Dec 2026

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(db, "connect", fake_connect)


def test_job_on_runs_the_check(conn, monkeypatch):
    _on_with(conn, monkeypatch)
    fast_week(conn)
    assert midweek_wake.job() == 0
    assert midweek_wake.current(conn, MONDAY) is not None       # the job recorded the week's wake-up


def test_dash_check_runs_the_workers_path_and_writes_nothing(conn, monkeypatch, capsys):
    """Sol, round 2: `--check` says "would record" only when the worker would: the week's row,
    the halt switch, an open request and the balance all count, and it writes nothing."""
    _on_with(conn, monkeypatch)
    fast_week(conn, halted=True)
    assert midweek_wake.main(["--check"]) == 0
    out = capsys.readouterr().out
    assert "would not record" in out and "halt switch is on" in out
    config.set_halted(conn, False, "tester")
    conn.execute("insert into purchase_requests (request_id, submitted_at, off_cycle, request_body, status)"
                 " values ('wake-pending', %s, false, '{}', 'pending')", (MONDAY - timedelta(days=1),))
    assert midweek_wake.main(["--check"]) == 0
    out = capsys.readouterr().out
    assert "would not record" in out and "request_pending" in out
    conn.execute("delete from purchase_requests where request_id = 'wake-pending'")
    assert midweek_wake.main(["--check"]) == 0
    assert f"would record: Slots {FOUR[0]}, {FOUR[1]}, {FOUR[2]} and {FOUR[3]}" in capsys.readouterr().out
    assert conn.execute("select count(*) from midweek_wakeups").fetchone()[0] == 0   # --check wrote nothing
    assert midweek_wake.check(conn, now=MONDAY) is True
    assert midweek_wake.main(["--check"]) == 0
    out = capsys.readouterr().out
    assert "already has its wake-up" in out and "would not record" in out


def test_a_crashed_request_leaves_the_week_unrecorded(conn, schema_url, monkeypatch):
    """The insert and the run request are one transaction: a crash after the insert rolls
    it back, so a second connection's retry records the week in one piece."""
    from tests.conftest import open_in

    fast_week(conn)

    def crash(*a, **k):
        raise RuntimeError("crashed after the insert")

    monkeypatch.setattr(midweek_wake, "request_midweek_run", crash)
    with pytest.raises(RuntimeError):
        midweek_wake.check(conn, now=MONDAY)
    monkeypatch.undo()                                  # the retry runs the real request
    assert conn.execute("select count(*) from midweek_wakeups").fetchone()[0] == 0
    with open_in(*schema_url) as conn2:
        assert midweek_wake.check(conn2, now=MONDAY) is True
    row = midweek_wake.current(conn, MONDAY)
    assert row["requested_at"] is not None and row["requested_by"] == "midweek_wake"


# --- the surfaces ----------------------------------------------------------------


def test_briefing_carries_the_wake_up(conn):
    from tests.test_briefing import NOW as BRIEF_NOW, sample
    from vending import briefing

    sample(conn)
    assert briefing.build(conn, BRIEF_NOW).midweek_wakeup is None
    midweek_wake.record(conn, week_row(week=BRIEFING_WEEK))
    b = briefing.build(conn, BRIEF_NOW)
    assert b.midweek_wakeup is not None
    assert b.midweek_wakeup.saved_margin == "26.00" and b.midweek_wakeup.slots == ["15", "16"]
    assert not b.midweek_wakeup.requested
    text = briefing.render(b)
    assert "Midweek wake-up:" in text and "would sell out" in text
    assert "has not been requested yet" in text
    midweek_wake.request_midweek_run(conn, week_row().reason, by="tester", now=BRIEF_NOW)
    b = briefing.build(conn, BRIEF_NOW)
    assert b.midweek_wakeup.requested
    assert "was requested" in briefing.render(b)


def test_briefing_alerts_when_the_table_is_missing(conn):
    from tests.test_briefing import NOW as BRIEF_NOW, sample
    from vending import briefing

    sample(conn)
    conn.execute("drop table midweek_wakeups")
    b = briefing.build(conn, BRIEF_NOW)
    assert b.midweek_wakeup is None
    assert "midweek_wakeup_unavailable" in {a.code for a in b.alerts}


def test_dashboard_attention_carries_the_wake_up(conn):
    from vending import dashboard

    fast_week(conn)
    assert midweek_wake.check(conn, now=MONDAY) is True
    snap = dashboard._snapshot(conn, MONDAY)[0]
    wake = snap["midweek_wakeup"]
    assert wake["ok"] and wake["data"]["slots"] == ["15", "16", "17", "18"]
    items = {i["text"]: i["level"] for i in dashboard.attention(snap)}
    assert next(level for text, level in items.items() if "Midweek wake-up:" in text) == "info"
    dashboard.to_json(snap)                                     # the whole snapshot stays JSON-safe


def test_dashboard_attention_warns_until_requested(conn):
    from vending import dashboard

    fast_week(conn)
    conn.execute("delete from midweek_wakeups")                 # the check recorded a request; take it off
    midweek_wake.record(conn, week_row(week="2026-12-05", slots=("15", "16", "17", "18")))
    snap = dashboard._snapshot(conn, MONDAY)[0]
    items = {i["text"]: i["level"] for i in dashboard.attention(snap)}
    assert next(level for text, level in items.items() if "Midweek wake-up:" in text) == "warn"
    assert "has not been requested yet" in next(text for text in items if "Midweek wake-up:" in text)


def test_dashboard_attention_without_the_table_is_a_missing_panel(conn):
    from vending import dashboard

    conn.execute("drop table midweek_wakeups")
    snap = dashboard._snapshot(conn, MONDAY)[0]
    wake = snap["midweek_wakeup"]
    assert wake["ok"] is False and "migrate" in wake["why"]


def test_worker_carries_the_job():
    from vending import worker

    assert worker.JOB_NAMES["midweek_wake"] == "Midweek wake-up check"
    assert [name for name, _ in worker.default_jobs()][-1] == "midweek_wake"   # after the syncs it reads


def test_the_wake_waits_for_the_slot_check(monkeypatch):
    """Sol, round 3: a round whose slot assignment check fails (exit 1) must not run the wake
    check on slots nobody confirmed; the other syncs run as before."""
    from vending import slots, sync_catalog, sync_history, sync_product_requests, sync_requests, worker

    for module in (sync_history, sync_requests, sync_catalog, sync_product_requests):
        monkeypatch.setattr(module, "main", lambda argv: 0)
    monkeypatch.setenv("MIDWEEK_WAKE", "on")
    ran = []
    monkeypatch.setattr(midweek_wake, "job", lambda: ran.append("wake") or 0)
    monkeypatch.setattr(slots, "main", lambda argv: 1)
    jobs = worker.default_jobs()
    results = worker.run_round(jobs, log=lambda _: None)
    assert ran == []
    assert [(r.job, r.exit_code) for r in results] == [
        ("slots_check", 1), ("sync_history", 0), ("sync_requests", 0), ("sync_catalog", 0),
        ("sync_product_requests", 0), ("midweek_wake", 0)]
    monkeypatch.setattr(slots, "main", lambda argv: 0)
    worker.run_round(jobs, log=lambda _: None)                    # the same job list, next round
    assert ran == ["wake"]
    monkeypatch.setattr(slots, "main", lambda argv: (_ for _ in ()).throw(RuntimeError("boom")))
    worker.run_round(jobs, log=lambda _: None)                    # a crashed check gates it too
    assert ran == ["wake"]
