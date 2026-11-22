"""`python -m vending.fill`: the picked first-fill plan, previewed and submitted through the agent's own
planner and submit, against the local fake bridge from `tests/test_desired_state.py`."""

import re
from datetime import datetime, timezone

import pytest

psycopg = pytest.importorskip("psycopg")

from psycopg.types.json import Jsonb  # noqa: E402

from tests.conftest import TEST_SLOTS, open_in  # noqa: E402
from tests.test_desired_state import fake  # noqa: E402,F401  (a fixture)
from tests.test_server import db  # noqa: E402,F401  (a fixture)
from vending import fill, ledger, sourcing  # noqa: E402
from vending.config import set_halted  # noqa: E402
from vending.sync_catalog import sync as catalog_sync  # noqa: E402

# The real opening, after the fall cut, then the fill previewed on Sun 29 Nov at 20:00 Eastern.
OPENED = datetime(2026, 11, 28, 17, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 11, 30, 1, 0, tzinfo=timezone.utc)
CHECKED = datetime(2026, 11, 27, 15, 0, tzinfo=timezone.utc)
COKE, PICKLE = TEST_SLOTS[:10], TEST_SLOTS[10:]
HASH = re.compile(r"Plan hash: ([0-9a-f]{64})")


def plan_json() -> dict:
    """Round 6's shape: Coca-Cola in the first 10 of our slots, Pickle Potion in the rest."""
    return {"day_one": [{"slot": s, "sku": "COKE20", "units": 7, "price_cents": 250} for s in COKE]
            + [{"slot": s, "sku": "PICKLE", "units": 8, "price_cents": 300} for s in PICKLE]}


# The round's own pack list, as `plan_rounds` reads it: what the pick buys (round 2, finding 4 compares).
PROJECTION = {"packs": [
    {"sku": "COKE20", "units_per_pack": 12, "expected_cents": 1237, "max_cents": 1300},
    {"sku": "PICKLE", "units_per_pack": 24, "expected_cents": 1800, "max_cents": 2100}]}


def option(sku, rank="primary", **over) -> sourcing.Option:
    base = {"COKE20": dict(store="Costco", units_per_pack=12, expected_cents=1237, max_cents=1300, url=None),
            "PICKLE": dict(store="Amazon", units_per_pack=24, expected_cents=1800, max_cents=2100,
                           url="https://www.amazon.com/dp/B000TEST")}[sku]
    return sourcing.Option(sku=sku, rank=rank, **{"checked_at": CHECKED, **base, **over}, pack_description=None,
                           checked_by="Test Checker")


def seed(conn, fake, *, opened=OPENED, prices=("COKE20", "PICKLE"), capital=60000) -> int:  # noqa: F811
    """A picked plan, our catalog copy, checked prices for `prices`, the real opening and the halt switch off."""
    if opened is not None:
        ledger.record(conn, "opening", capital, "opening capital", "Test Treasurer", opened)
    set_halted(conn, False, "test")
    catalog_sync(conn, fake.client(), now=NOW, log=lambda _: None)
    for sku in prices:
        sourcing.set_option(conn, option(sku), NOW)
    plan_id = conn.execute(
        "insert into first_fill_plans (loaded_by, label, source_note, plan, projection, slots, picked)"
        " values ('Test Loader', '6', 'round6/plan6.json', %s, %s, %s, true) returning id",
        (Jsonb(plan_json()), Jsonb(PROJECTION), list(TEST_SLOTS))).fetchone()[0]
    return plan_id


def run(schema_url, fake, *argv) -> int:  # noqa: F811
    return fill.main(list(argv), bridge=fake.client, connect=lambda: open_in(*schema_url), now=NOW)


def previewed(schema_url, fake, capsys) -> tuple[str, str | None]:  # noqa: F811
    assert run(schema_url, fake) == 0
    out = capsys.readouterr().out
    found = HASH.search(out)
    return out, found and found.group(1)


def codes(conn) -> list[str]:
    [(detail,)] = conn.execute("select detail from action_log where tool = 'fill.submit' order by id desc limit 1"
                               ).fetchall()
    return sorted(set(re.findall(r"(?:^|; )([a-z_]+):", detail)))


# --- Preview --------------------------------------------------------------------------------------

def test_the_preview_shows_every_line_the_cost_and_the_hash_and_sends_nothing(db, fake, schema_url, capsys):  # noqa: F811
    plan_id = seed(db, fake)
    out, plan_hash = previewed(schema_url, fake, capsys)
    assert f"First-fill plan #{plan_id}, round 6" in out
    # One line per product buys the plan's packs for all its slots; the others load from that purchase.
    assert re.search(rf"^{COKE[0]}\s+COKE20\s+7\s+Costco\s+6 x 12\s+1\.03 / 1\.08\s+\$2\.50", out, re.M)
    assert re.search(rf"^{PICKLE[0]}\s+PICKLE\s+8\s+Amazon\s+4 x 24\s+0\.75 / 0\.88\s+\$3\.00", out, re.M)
    for slot in COKE[1:]:
        assert re.search(rf"^{slot}\s+COKE20\s+7\s+from slot {COKE[0]}\s+-\s+-\s+\$2\.50", out, re.M)
    for slot in PICKLE[1:]:
        assert re.search(rf"^{slot}\s+PICKLE\s+8\s+from slot {PICKLE[0]}\s+-\s+-\s+\$3\.00", out, re.M)
    # As round 6 pools them: 70 Coca-Cola in six 12-packs, 80 Pickle Potion in four 24-packs.
    assert "Total: expected $146.22, at most $162.00. Spendable $600.00; $438.00 left at most." in out
    # The agent's hash for the same desired state, bound to the plan's ID and our slots (finding 5).
    with open_in(*schema_url) as conn:
        p = fill.preview(conn, fake.client(), NOW)
    assert plan_hash == p.approval == fill.approval_hash(p.plan.plan_hash, plan_id, TEST_SLOTS)
    assert f"--submit --hash {plan_hash} --by NAME" in out
    # Sunday evening: Amazon's cutoff for the Monday stocking (Saturday 23:59) has passed, in-store's hasn't.
    assert "past the Amazon cutoff for the Mon 30 Nov stocking (Sat 28 Nov 23:59 ET)" in out
    assert ", ".join(sorted(PICKLE)) in out and "Costco cutoff" not in out
    assert fake.posts == [] and db.execute("select count(*) from purchase_submissions").fetchone() == (0,)


def test_with_no_pick_it_says_how_to_pick(db, fake, schema_url, capsys):  # noqa: F811
    seed(db, fake)
    db.execute("update first_fill_plans set picked = false")
    assert run(schema_url, fake) == 2
    assert "plan_store pick" in capsys.readouterr().err


def test_a_store_cutoff_on_the_stocking_day_is_only_a_warning(db, fake, schema_url, capsys):  # noqa: F811
    seed(db, fake)
    late = datetime(2026, 11, 30, 5, 30, tzinfo=timezone.utc)   # Mon 30 Nov 00:30 Eastern
    with open_in(*schema_url) as conn:
        p = fill.preview(conn, fake.client(), late)
    assert p.ok
    assert [w.split(" for slots")[0] for w in p.warnings if "Mon 30 Nov stocking" in w] == [
        "past the Amazon cutoff for the Mon 30 Nov stocking (Sat 28 Nov 23:59 ET)",
        "past the Costco cutoff for the Mon 30 Nov stocking (Sun 29 Nov 23:59 ET)"]


# --- Submit ---------------------------------------------------------------------------------------

def test_a_submit_records_the_request_as_the_agent_would_with_who_and_which_plan(db, fake, schema_url, capsys):  # noqa: F811
    plan_id = seed(db, fake)
    _, plan_hash = previewed(schema_url, fake, capsys)
    with open_in(*schema_url) as conn:
        request_hash = fill.preview(conn, fake.client(), NOW).plan.plan_hash
    assert run(schema_url, fake, "--submit", "--hash", plan_hash, "--by", "Test Submitter") == 0
    out = capsys.readouterr().out
    assert "Submitted purchase request pr-1 (pending): 20 lines, at most 162.00" in out
    assert "python -m vending.sync_requests" in out
    [posted] = fake.posts
    assert posted["off_cycle"] is False and [ln["slot"] for ln in posted["lines"]] == list(TEST_SLOTS)
    assert posted["lines"][0]["primary"] == {
        "sku": "COKE20", "store": "Costco", "packs": 6, "units_per_pack": 12, "max_price_per_pack": "13.00",
        "sale_price": "2.50", "change_product": True}
    # Where the request sync finds it on 30 Nov, with the planner's hash of the request, as the agent's would be.
    assert db.execute("select request_id, status, plan_hash, request_body from purchase_requests").fetchall() == [
        ("pr-1", "pending", request_hash, posted)]
    assert db.execute("select status, request_id from purchase_submissions").fetchall() == [("accepted", "pr-1")]
    [(arguments, outcome, result)] = db.execute(
        "select arguments, outcome, result from action_log where tool = 'fill.submit'").fetchall()
    assert arguments == {"by": "Test Submitter", "expected_plan_hash": plan_hash, "plan_id": plan_id, "label": "6"}
    assert outcome == "done" and result["request_id"] == "pr-1" and result["submission"] == 1


def test_the_request_buys_the_plans_packs_and_names_each_slots_load(db, fake, schema_url, capsys):  # noqa: F811
    """Review blocker 1: the plan pools units by product before rounding to packs, so the request must too.

    A pack per slot bought 20 packs at a $340.00 max where the plan buys 10 at $162.00. The bridge's purchase
    line has no load target, so the per-slot loads go in the request: back-stock lines carry their units, and
    the note names every slot's load, the purchase lines' too."""
    seed(db, fake)
    _, plan_hash = previewed(schema_url, fake, capsys)
    assert run(schema_url, fake, "--submit", "--hash", plan_hash, "--by", "Test Submitter") == 0
    assert "at most 162.00" in capsys.readouterr().out
    [posted] = fake.posts
    lines = {ln["slot"]: ln for ln in posted["lines"]}
    for first, rest, sku, units, price, packs in ((COKE[0], COKE[1:], "COKE20", 7, "2.50", 6),
                                                   (PICKLE[0], PICKLE[1:], "PICKLE", 8, "3.00", 4)):
        assert lines[first]["source"] == "purchase" and lines[first]["primary"]["packs"] == packs
        for slot in rest:
            assert lines[slot] == {"slot": slot, "source": "back_stock", "load": {
                "sku": sku, "units": units, "sale_price": price, "change_product": True}}
    assert sum(ln["primary"]["packs"] for ln in posted["lines"] if ln["source"] == "purchase") == 10
    note = posted["note"]
    assert len(note) <= 500
    for slot in COKE:
        assert f"{slot}={7}" in note
    for slot in PICKLE:
        assert f"{slot}={8}" in note


def test_a_changed_load_changes_the_request_and_its_hash(db, fake, schema_url, capsys):  # noqa: F811
    """A load target is part of the request, so a pick changed from 7 to 6 can't reuse the old hash."""
    seed(db, fake)
    _, before = previewed(schema_url, fake, capsys)
    plan = plan_json()
    plan["day_one"][1]["units"] = 6
    db.execute("update first_fill_plans set plan = %s", (Jsonb(plan),))
    _, after = previewed(schema_url, fake, capsys)
    assert after and after != before
    assert run(schema_url, fake, "--submit", "--hash", before, "--by", "Test Submitter") == 1
    assert "plan_changed" in capsys.readouterr().err and fake.posts == []
    assert run(schema_url, fake, "--submit", "--hash", after, "--by", "Test Submitter") == 0
    [posted] = fake.posts
    assert f"{COKE[1]}=6" in posted["note"]
    assert {ln["slot"]: ln for ln in posted["lines"]}[COKE[1]]["load"]["units"] == 6


def test_an_opening_dated_after_now_authorizes_nothing(db, fake, schema_url, capsys):  # noqa: F811
    """Review blocker 2: an opening at 23:59 ET on 29 Nov put $600 in a 20:00 ET submit, and it posted."""
    seed(db, fake, opened=datetime(2026, 11, 30, 4, 59, tzinfo=timezone.utc))
    out, plan_hash = previewed(schema_url, fake, capsys)
    assert plan_hash is None and "Can't plan yet" in out and "after now" in out and "$600.00" not in out
    assert run(schema_url, fake, "--submit", "--hash", "0" * 64, "--by", "Test Submitter") == 1
    capsys.readouterr()
    assert fake.posts == [] and db.execute("select count(*) from purchase_submissions").fetchone() == (0,)


def test_a_fill_sent_before_the_ledgers_opening_still_counts_as_sent(db, fake, schema_url, capsys):  # noqa: F811
    """Review blocker 2: the duplicate check looked only at requests since the opening, so an opening recorded
    again after the fill went out hid it. The fill's own record is checked too."""
    seed(db, fake)
    _, plan_hash = previewed(schema_url, fake, capsys)
    assert run(schema_url, fake, "--submit", "--hash", plan_hash, "--by", "Test Submitter") == 0
    db.execute("update purchase_requests set status = 'fulfilled'")
    later = datetime(2026, 11, 30, 2, 0, tzinfo=timezone.utc)
    db.execute("update ledger_entries set at = %s where kind = 'opening'", (datetime(2026, 11, 30, 1, 30,
                                                                                         tzinfo=timezone.utc),))
    capsys.readouterr()
    assert fill.main([], bridge=fake.client, connect=lambda: open_in(*schema_url), now=later) == 0
    out = capsys.readouterr().out
    assert "fill_already_sent: request pr-1 (fulfilled)" in out and "Plan hash: none" in out
    assert fill.main(["--submit", "--hash", "0" * 64, "--by", "Test Submitter"], bridge=fake.client,
                     connect=lambda: open_in(*schema_url), now=later) == 1
    capsys.readouterr()
    assert codes(db) == ["fill_already_sent"] and len(fake.posts) == 1


def test_a_hash_that_does_not_match_the_preview_is_refused(db, fake, schema_url, capsys):  # noqa: F811
    seed(db, fake)
    _, plan_hash = previewed(schema_url, fake, capsys)
    # A price changed after the preview: the plan now costs more, so it hashes differently.
    sourcing.set_option(db, option("COKE20", max_cents=1400), NOW)
    assert run(schema_url, fake, "--submit", "--hash", plan_hash, "--by", "Test Submitter") == 1
    err = capsys.readouterr().err
    assert "Refused; nothing was sent" in err and "plan_changed" in err
    assert fake.posts == [] and db.execute("select count(*) from purchase_submissions").fetchone() == (0,)
    assert db.execute("select outcome from action_log where tool = 'fill.submit'").fetchall() == [("refused",)]


def test_submit_needs_the_hash_and_a_name(db, fake, schema_url, capsys):  # noqa: F811
    seed(db, fake)
    for argv in (["--submit", "--by", "Test Submitter"], ["--submit", "--hash", "abc"]):
        with pytest.raises(SystemExit):
            run(schema_url, fake, *argv)
    assert fake.posts == []


@pytest.mark.parametrize("missing", ["no row", "no price"])
def test_a_product_without_a_checked_price_is_refused_with_nothing_sent(db, fake, schema_url, capsys, missing):  # noqa: F811
    seed(db, fake, prices=("COKE20",) if missing == "no row" else ("COKE20", "PICKLE"))
    if missing == "no price":
        sourcing.set_option(db, option("PICKLE", expected_cents=None), NOW)
    out, plan_hash = previewed(schema_url, fake, capsys)
    assert plan_hash is None and "Plan hash: none" in out
    assert out.count("- no_checked_price: slot ") == len(PICKLE) and "#202" in out
    assert "no_sourcing" not in out           # the planner's own refusal for the same slots isn't repeated
    assert run(schema_url, fake, "--submit", "--hash", "0" * 64, "--by", "Test Submitter") == 1
    capsys.readouterr()
    assert codes(db) == ["no_checked_price"] and fake.posts == []


def test_a_plan_picked_for_another_slot_assignment_is_refused(db, fake, schema_url, capsys):  # noqa: F811
    seed(db, fake)
    db.execute("update first_fill_plans set slots = %s", (["11", *TEST_SLOTS[1:]],))
    out, plan_hash = previewed(schema_url, fake, capsys)
    assert plan_hash is None and "plan_assignment: plan #" in out and "COKE20" not in out
    assert run(schema_url, fake, "--submit", "--hash", "0" * 64, "--by", "Test Submitter") == 1
    capsys.readouterr()
    assert codes(db) == ["plan_assignment"] and fake.posts == []


def test_without_an_opening_nothing_can_be_planned_or_sent(db, fake, schema_url, capsys):  # noqa: F811
    seed(db, fake, opened=None)
    out, plan_hash = previewed(schema_url, fake, capsys)
    assert plan_hash is None and "Can't plan yet" in out and "vending.ledger open" in out
    assert run(schema_url, fake, "--submit", "--hash", "0" * 64, "--by", "Test Submitter") == 1
    assert "Can't submit yet" in capsys.readouterr().out
    assert fake.posts == [] and db.execute("select count(*) from purchase_submissions").fetchone() == (0,)


def test_the_test_opening_from_before_the_fall_cut_is_refused(db, fake, schema_url, capsys):  # noqa: F811
    seed(db, fake, opened=datetime(2026, 11, 11, 17, tzinfo=timezone.utc))
    out, plan_hash = previewed(schema_url, fake, capsys)
    assert plan_hash is None and "test_opening: the ledger's opening is dated 2026-11-11" in out
    assert run(schema_url, fake, "--submit", "--hash", "0" * 64, "--by", "Test Submitter") == 1
    capsys.readouterr()
    assert codes(db) == ["test_opening"] and fake.posts == []


def test_the_fill_can_not_be_sent_twice(db, fake, schema_url, capsys):  # noqa: F811
    seed(db, fake)
    _, plan_hash = previewed(schema_url, fake, capsys)
    assert run(schema_url, fake, "--submit", "--hash", plan_hash, "--by", "Test Submitter") == 0
    # Pending: the planner's guard and ours both refuse.
    assert run(schema_url, fake, "--submit", "--hash", plan_hash, "--by", "Test Submitter") == 1
    assert codes(db) == ["fill_already_sent", "request_pending"]
    # Fulfilled on 30 Nov: the planner no longer minds, but the fill has gone.
    db.execute("update purchase_requests set status = 'fulfilled'")
    assert run(schema_url, fake, "--submit", "--hash", plan_hash, "--by", "Test Submitter") == 1
    assert codes(db) == ["fill_already_sent"]
    out, again = previewed(schema_url, fake, capsys)
    assert again is None and "fill_already_sent: request pr-1 (fulfilled)" in out
    assert len(fake.posts) == 1
    # Rejected by the bridge: the fill may be sent again.
    db.execute("update purchase_requests set status = 'rejected'")
    fake.answer = (201, {"id": "pr-2", "status": "pending", "created_at": "2026-11-30T01:00:00Z"})
    _, plan_hash = previewed(schema_url, fake, capsys)
    assert run(schema_url, fake, "--submit", "--hash", plan_hash, "--by", "Test Submitter") == 0
    assert len(fake.posts) == 2


def test_an_unsettled_submission_blocks_the_preview_and_the_submit(db, fake, schema_url, capsys):  # noqa: F811
    seed(db, fake)
    _, plan_hash = previewed(schema_url, fake, capsys)
    fake.answer = (503, {"detail": "down"})
    assert run(schema_url, fake, "--submit", "--hash", plan_hash, "--by", "Test Submitter") == 1
    assert "Don't submit again" in capsys.readouterr().out
    out, again = previewed(schema_url, fake, capsys)
    assert again is None and "submission_unresolved" in out
    assert len(fake.posts) == 1


# --- Review findings 3-6 --------------------------------------------------------------------------

def load_plan(conn, plan=None, label="7") -> int:
    """Another stored plan for our slots, unpicked."""
    return conn.execute(
        "insert into first_fill_plans (loaded_by, label, source_note, plan, projection, slots)"
        " values ('Test Loader', %s, 'round/plan.json', %s, %s, %s) returning id",
        (label, Jsonb(plan or plan_json()), Jsonb(PROJECTION), list(TEST_SLOTS))).fetchone()[0]


def picked_id(conn) -> int:
    return conn.execute("select id from first_fill_plans where picked").fetchone()[0]


def test_an_unchecked_alternate_is_refused_with_nothing_sent(db, fake, schema_url, capsys):  # noqa: F811
    """Finding 3: a checked Costco primary with an unchecked Walmart alternate posted both."""
    seed(db, fake)
    sourcing.set_option(db, option("COKE20", "alternate", store="Walmart", expected_cents=None, max_cents=1500), NOW)
    out, plan_hash = previewed(schema_url, fake, capsys)
    assert plan_hash is None and "Plan hash: none" in out
    assert "- no_checked_price: COKE20's alternate (Walmart) has no checked price" in out
    assert run(schema_url, fake, "--submit", "--hash", "0" * 64, "--by", "Test Submitter") == 1
    capsys.readouterr()
    assert codes(db) == ["no_checked_price"] and fake.posts == []
    # Checked, it goes in the request. At $15.00 a pack it raises the worst case above the pick's, so the
    # preview lists that and the send needs --accept-deviations (round 2, finding 4).
    sourcing.set_option(db, option("COKE20", "alternate", store="Walmart", expected_cents=1400, max_cents=1500), NOW)
    out, plan_hash = previewed(schema_url, fake, capsys)
    assert "- COKE20 (alternate, Walmart): the pick buys 6 x 12 (72 units, at most $78.00); the alternate buys " \
           "6 x 12 (72 units, at most $90.00)" in out
    assert "- COKE20: " not in out   # the primary buys what the pick buys
    assert run(schema_url, fake, "--submit", "--hash", plan_hash, "--by", "Test Submitter",
               "--accept-deviations") == 0
    [posted] = fake.posts
    assert posted["lines"][0]["alternate"]["store"] == "Walmart"


def test_a_repick_waits_for_the_send_and_can_not_slip_in_before_it(db, fake, schema_url, capsys):  # noqa: F811
    """Finding 4: picking round 7 while the submit was sending round 6 went through, so round 6 was sent while
    round 7 was the pick. The submit now holds the pick for its whole transaction: a repick waits until it ends."""
    import threading

    from vending import plan_store

    six = seed(db, fake)
    seven = load_plan(db)
    _, plan_hash = previewed(schema_url, fake, capsys)
    fake.hold = threading.Event()
    result = {}
    sender = threading.Thread(target=lambda: result.update(code=run(
        schema_url, fake, "--submit", "--hash", plan_hash, "--by", "Test Submitter")))
    sender.start()
    try:
        assert fake.received.wait(30)
        with open_in(*schema_url) as other:
            other.execute("set lock_timeout = '1s'")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                plan_store.pick(other, seven, "Test Picker")
    finally:
        fake.hold.set()
        sender.join(30)
    assert result == {"code": 0} and len(fake.posts) == 1 and picked_id(db) == six
    [(arguments,)] = db.execute("select arguments from action_log where tool = 'fill.submit'").fetchall()
    assert arguments["plan_id"] == six
    # Once the send is over, the repick goes through.
    with open_in(*schema_url) as other:
        plan_store.pick(other, seven, "Test Picker")
    assert picked_id(db) == seven


def test_a_pick_changed_inside_the_submit_is_refused_before_the_post(db, fake, schema_url, capsys, monkeypatch):  # noqa: F811
    """Finding 4: the picked plan is read again right before the POST, in the same transaction, and must be the
    one the submit planned."""
    from vending import planning

    seed(db, fake)
    seven = load_plan(db)
    _, plan_hash = previewed(schema_url, fake, capsys)
    real = planning.plan

    def repicked(conn, *args, **kwargs):
        conn.execute("update first_fill_plans set picked = (id = %s)", (seven,))
        return real(conn, *args, **kwargs)

    monkeypatch.setattr(planning, "plan", repicked)
    assert run(schema_url, fake, "--submit", "--hash", plan_hash, "--by", "Test Submitter") == 1
    assert "pick_changed" in capsys.readouterr().err and fake.posts == []


def test_a_hash_is_bound_to_the_plans_id(db, fake, schema_url, capsys):  # noqa: F811
    """Finding 5: the hash covered only the request, so a plan with the same lines under another ID reused it."""
    seed(db, fake)
    _, six_hash = previewed(schema_url, fake, capsys)
    seven = load_plan(db)
    db.execute("update first_fill_plans set picked = (id = %s)", (seven,))
    _, seven_hash = previewed(schema_url, fake, capsys)
    assert seven_hash and seven_hash != six_hash
    assert run(schema_url, fake, "--submit", "--hash", six_hash, "--by", "Test Submitter") == 1
    assert "plan_changed" in capsys.readouterr().err and fake.posts == []
    assert run(schema_url, fake, "--submit", "--hash", seven_hash, "--by", "Test Submitter") == 0


def test_a_hash_is_bound_to_our_slot_assignment():
    """Finding 5: the same plan and request under another assignment hashes differently."""
    base = fill.approval_hash("a" * 64, 6, TEST_SLOTS)
    assert base == fill.approval_hash("a" * 64, 6, tuple(TEST_SLOTS))
    assert base != fill.approval_hash("a" * 64, 6, (*TEST_SLOTS[:-1], "99"))
    assert base != fill.approval_hash("a" * 64, 7, TEST_SLOTS)
    assert base != fill.approval_hash("b" * 64, 6, TEST_SLOTS)


SUN_LAST_SECOND = datetime(2026, 11, 30, 4, 59, 59, tzinfo=timezone.utc)   # Sun 29 Nov 23:59:59 Eastern
MON_0030 = datetime(2026, 11, 30, 5, 30, tzinfo=timezone.utc)              # Mon 30 Nov 00:30 Eastern


def test_a_submit_after_the_sunday_deadline_is_refused_without_late(db, fake, schema_url, capsys):  # noqa: F811
    """Finding 6: a hash from Sunday evening still posted at 00:30 ET on Mon 30 Nov."""
    seed(db, fake)
    _, plan_hash = previewed(schema_url, fake, capsys)
    argv = ["--submit", "--hash", plan_hash, "--by", "Test Submitter"]
    assert fill.main(argv, bridge=fake.client, connect=lambda: open_in(*schema_url), now=MON_0030) == 1
    err = capsys.readouterr().err
    assert "fill_deadline: the fill request was due by 23:59 ET Sun 29 Nov" in err and "--late" in err
    assert fake.posts == [] and codes(db) == ["fill_deadline"]
    # With --late, a person sends it anyway; the log says so.
    assert fill.main([*argv, "--late"], bridge=fake.client, connect=lambda: open_in(*schema_url), now=MON_0030) == 0
    assert len(fake.posts) == 1
    [(arguments,)] = db.execute("select arguments from action_log where tool = 'fill.submit' and outcome = 'done'"
                                ).fetchall()
    assert arguments["late"] is True


def test_the_last_second_of_sunday_is_still_on_time(db, fake, schema_url, capsys):  # noqa: F811
    seed(db, fake)
    _, plan_hash = previewed(schema_url, fake, capsys)
    assert fill.main(["--submit", "--hash", plan_hash, "--by", "Test Submitter"], bridge=fake.client,
                     connect=lambda: open_in(*schema_url), now=SUN_LAST_SECOND) == 0
    assert len(fake.posts) == 1


def test_a_preview_after_the_deadline_warns_and_says_to_add_late(db, fake, schema_url, capsys):  # noqa: F811
    seed(db, fake)
    assert fill.main([], bridge=fake.client, connect=lambda: open_in(*schema_url), now=MON_0030) == 0
    out = capsys.readouterr().out
    assert "past the fill request deadline (23:59 ET Sun 29 Nov)" in out
    assert re.search(r"--submit --hash [0-9a-f]{64} --late --by NAME", out)
    assert fill.main([], bridge=fake.client, connect=lambda: open_in(*schema_url), now=SUN_LAST_SECOND) == 0
    assert "fill request deadline" not in capsys.readouterr().out


# --- Sol review, round 2 --------------------------------------------------------------------------

TEST_OPENING_AT = datetime(2026, 11, 11, 17, tzinfo=timezone.utc)


def _clear_checked_price(conn):
    sourcing.set_option(conn, option("COKE20", expected_cents=None), NOW)


def _test_opening(conn):
    conn.execute("update ledger_entries set at = %s where kind = 'opening'", (TEST_OPENING_AT,))


@pytest.mark.parametrize("change", [_clear_checked_price, _test_opening], ids=["clear price", "test opening"])
def test_inputs_can_not_change_between_the_checks_and_the_planner(db, fake, schema_url, capsys, monkeypatch,  # noqa: F811
                                                                   change):
    """Round 2, blocker 1: a second connection cleared COKE20's checked price (cap unchanged), or re-dated the
    opening to the 11 Nov test date, after the fill's checks and before the planner read them again. The hash
    didn't change, so the old approval posted. The submit now holds the sourcing table and the ledger, so the
    change waits for the send."""
    from vending import planning

    seed(db, fake)
    _, plan_hash = previewed(schema_url, fake, capsys)
    real, seen = planning.plan, {}

    def changed_then_planned(conn, *args, **kwargs):
        if "changed" not in seen:
            with open_in(*schema_url) as other:
                other.execute("set lock_timeout = '1s'")
                try:
                    change(other)
                    seen["changed"] = True
                except psycopg.errors.LockNotAvailable:
                    seen["changed"] = False
        return real(conn, *args, **kwargs)

    monkeypatch.setattr(planning, "plan", changed_then_planned)
    run(schema_url, fake, "--submit", "--hash", plan_hash, "--by", "Test Submitter")
    capsys.readouterr()
    # The change could not land mid-submit, so what was sent is what was checked.
    assert seen == {"changed": False}
    [posted] = fake.posts
    assert posted["lines"][0]["primary"]["store"] == "Costco"
    assert sourcing.options_for(db, ["COKE20"])[0].expected_cents == 1237
    assert ledger.opening(db).at == OPENED


def test_the_guards_run_on_the_rows_the_planner_used(db, fake, schema_url, capsys, monkeypatch):  # noqa: F811
    """Round 2, blocker 1: the guards read the database again rather than what the planner was given. If the
    planner's sourcing rows lack a checked price, or its opening is the test one, nothing is sent."""
    from dataclasses import replace

    seed(db, fake)
    _, plan_hash = previewed(schema_url, fake, capsys)
    real_rows = sourcing.listed_options
    monkeypatch.setattr(sourcing, "listed_options", lambda conn: [
        replace(o, expected_cents=None) if (o.sku, o.rank) == ("COKE20", "primary") else o for o in real_rows(conn)])
    assert run(schema_url, fake, "--submit", "--hash", plan_hash, "--by", "Test Submitter") == 1
    assert "no_checked_price" in capsys.readouterr().err and fake.posts == []
    monkeypatch.setattr(sourcing, "listed_options", real_rows)
    # The planner was given the test opening's entry (round 3: the entry, not its date, tells it).
    from vending import planning

    real_gather = planning.gather
    monkeypatch.setattr(planning, "gather", lambda *a, **k: (lambda g: replace(g, opening=replace(
        g.opening, id=ledger.TEST_OPENING_ID, at=TEST_OPENING_AT, detail=ledger.TEST_OPENING_DETAIL)))(
        real_gather(*a, **k)))
    assert run(schema_url, fake, "--submit", "--hash", plan_hash, "--by", "Test Submitter") == 1
    assert "test_opening" in capsys.readouterr().err and fake.posts == []
    assert db.execute("select count(*) from purchase_submissions").fetchone() == (0,)


LATER = datetime(2026, 11, 30, 2, 0, tzinfo=timezone.utc)


def _move_opening_past_the_fill(conn):
    conn.execute("update purchase_requests set status = 'fulfilled'")
    conn.execute("update ledger_entries set at = %s where kind = 'opening'",
                 (datetime(2026, 11, 30, 1, 30, tzinfo=timezone.utc),))


def _second_try(schema_url, fake, capsys) -> str:  # noqa: F811
    """A preview, then a submit, after the opening moved past the fill. Returns the preview."""
    assert fill.main([], bridge=fake.client, connect=lambda: open_in(*schema_url), now=LATER) == 0
    out = capsys.readouterr().out
    assert fill.main(["--submit", "--hash", "0" * 64, "--by", "Test Submitter"], bridge=fake.client,
                     connect=lambda: open_in(*schema_url), now=LATER) == 1
    capsys.readouterr()
    return out


def test_a_fill_interrupted_before_its_log_still_counts_as_sent(db, fake, schema_url, capsys, monkeypatch):  # noqa: F811
    """Round 2, blocker 2: the fill was found by its action_log row. An interruption after the submission was
    settled but before that row committed left an accepted request with no fill marker; once it was fulfilled
    and the opening moved past it, a fresh approval posted again. The marker is now on the submission row,
    saved before the POST."""
    from vending.tools import submit as submit_tool

    seed(db, fake)
    _, plan_hash = previewed(schema_url, fake, capsys)
    real = submit_tool._send

    def interrupted(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError("the process died here")

    monkeypatch.setattr(submit_tool, "_send", interrupted)
    with pytest.raises(RuntimeError):
        run(schema_url, fake, "--submit", "--hash", plan_hash, "--by", "Test Submitter")
    monkeypatch.setattr(submit_tool, "_send", real)
    assert db.execute("select status, request_id, purpose from purchase_submissions").fetchall() == [
        ("accepted", "pr-1", "fill")]
    assert db.execute("select count(*) from action_log where tool = 'fill.submit'"
                      " and result->>'request_id' is not null").fetchone() == (0,)
    _move_opening_past_the_fill(db)
    out = _second_try(schema_url, fake, capsys)
    assert "fill_already_sent: request pr-1 (fulfilled)" in out and "Plan hash: none" in out
    assert codes(db) == ["fill_already_sent"] and len(fake.posts) == 1


def test_a_fill_settled_by_hand_keeps_its_marker(db, fake, schema_url, capsys):  # noqa: F811
    """Round 2, blocker 2: the marker survives a person's `submissions resolve`, and a request the bridge
    rejected doesn't count."""
    from vending import submissions

    seed(db, fake)
    _, plan_hash = previewed(schema_url, fake, capsys)
    fake.answer = (503, {"detail": "down"})
    assert run(schema_url, fake, "--submit", "--hash", plan_hash, "--by", "Test Submitter") == 1
    [(row, purpose)] = db.execute("select id, purpose from purchase_submissions").fetchall()
    assert purpose == "fill"
    submissions.resolve(db, row, "pr-9", "Test Resolver")
    assert db.execute("select status, purpose from purchase_submissions").fetchall() == [("accepted", "fill")]
    _move_opening_past_the_fill(db)
    assert "fill_already_sent: request pr-9 (fulfilled)" in _second_try(schema_url, fake, capsys)
    assert len(fake.posts) == 1
    # Rejected by the bridge: the marker no longer blocks a new fill.
    db.execute("update purchase_requests set status = 'rejected'")
    assert fill.main([], bridge=fake.client, connect=lambda: open_in(*schema_url), now=LATER) == 0
    assert "fill_already_sent" not in capsys.readouterr().out


def test_the_deadline_is_checked_again_after_the_attempt_is_saved(db, fake, schema_url, capsys, monkeypatch):  # noqa: F811
    """Round 2, major 3: the deadline was checked before the side connection opened and the attempt was saved.
    A send that crosses midnight there is now stopped before the POST, and its attempt settled as never filed."""
    from vending import submissions

    seed(db, fake)
    clock = {"now": NOW}
    monkeypatch.setattr(ledger, "db_now", lambda conn: clock["now"])
    real_save = submissions.save_sending

    def saved_at_midnight(*args, **kwargs):
        row = real_save(*args, **kwargs)
        clock["now"] = MON_0030
        return row

    monkeypatch.setattr(submissions, "save_sending", saved_at_midnight)

    def submit(*extra) -> int:
        clock["now"] = NOW
        assert fill.main([], bridge=fake.client, connect=lambda: open_in(*schema_url)) == 0
        found = HASH.search(capsys.readouterr().out)
        return fill.main(["--submit", "--hash", found.group(1), "--by", "Test Submitter", *extra],
                         bridge=fake.client, connect=lambda: open_in(*schema_url))

    assert submit() == 1
    assert "fill_deadline" in capsys.readouterr().err and fake.posts == []
    [(status, detail)] = db.execute("select status, detail from purchase_submissions").fetchall()
    assert status == "not_filed" and "fill_deadline" in detail
    assert codes(db) == ["fill_deadline"]
    # With --late, the same crossing still sends.
    assert submit("--late") == 0
    assert len(fake.posts) == 1


DEVIATION = ("COKE20: the pick buys 6 x 12 (72 units, at most $78.00); this request buys 5 x 16 (80 units, at "
             "most $85.00)")


def test_a_request_that_buys_other_packs_than_the_pick_needs_approval(db, fake, schema_url, capsys):  # noqa: F811
    """Round 2, major 4: only the slots' loads went to the planner. The pick buys 70 Coca-Cola as 6 x 12 (72);
    with a checked 16-pack now in the table the request bought 5 x 16 (80), and nothing said so."""
    seed(db, fake)
    sourcing.set_option(db, option("COKE20", units_per_pack=16, expected_cents=1500, max_cents=1700), NOW)
    out, plan_hash = previewed(schema_url, fake, capsys)
    assert plan_hash
    assert "Differs from the pick (1):" in out and f"- {DEVIATION}" in out
    assert "--accept-deviations" in out
    argv = ["--submit", "--hash", plan_hash, "--by", "Test Submitter"]
    assert run(schema_url, fake, *argv) == 1
    assert "pick_deviation" in capsys.readouterr().err and fake.posts == []
    assert codes(db) == ["pick_deviation"]
    assert run(schema_url, fake, *argv, "--accept-deviations") == 0
    [posted] = fake.posts
    assert posted["lines"][0]["primary"]["packs"] == 5
    [(arguments,)] = db.execute("select arguments from action_log where tool = 'fill.submit' and outcome = 'done'"
                                ).fetchall()
    assert arguments["accept_deviations"] is True and arguments["deviations"] == [DEVIATION]


def test_a_request_that_buys_what_the_pick_buys_needs_no_approval(db, fake, schema_url, capsys):  # noqa: F811
    seed(db, fake)
    out, _ = previewed(schema_url, fake, capsys)
    assert "Differs from the pick" not in out and "--accept-deviations" not in out


# --- Sol review, round 3 --------------------------------------------------------------------------

def _no_input_locks(monkeypatch):
    """The submit's locks off, so a second connection's change lands mid-submit: a writer the locks miss. What
    stops the send then is the full check right before the POST, not the locks."""
    monkeypatch.setattr(fill, "hold_inputs", lambda conn: None, raising=False)


def _changed_once_saved(monkeypatch, schema_url, change):  # noqa: F811
    """Runs `change` on a second connection right after the attempt is saved, before the POST."""
    from vending import submissions

    real_save = submissions.save_sending

    def saved_then_changed(*args, **kwargs):
        row = real_save(*args, **kwargs)
        with open_in(*schema_url) as other:
            change(other)
        return row

    monkeypatch.setattr(submissions, "save_sending", saved_then_changed)


def _lower_the_ceiling(conn):
    from vending import config

    config.set_value(conn, "price_ceiling_cents", 200, "Test Changer")


def _old_request(conn):
    """A request from before the opening, fulfilled, whose one line (dated after the opening, nothing loaded)
    costs $0.00 so far.

    `store` takes a corrected amount on the same lines, never lines added to a receipt that had none (#225), so
    the sync's change below is a correction of this receipt."""
    from tests.test_sync_requests import BODY, fulfilled
    from vending.purchase_requests import record, store

    record(conn, "pr-old", datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc), False, BODY)
    store(conn, "pr-old", fulfilled("pr-old", _old_line("0.00"), fulfilled_at="2026-11-29T15:30:00Z",
                                    total_paid="0.00"), NOW)


def _old_line(paid: str) -> dict:
    """`pr-old`'s line: nothing bought or loaded, so it leaves the stock alone and only its cost counts."""
    from tests.test_sync_requests import receipt

    return receipt(outcome="unavailable", packs_bought=0, units_bought=0, units_loaded=0, units_lost=0,
                   loss_note=None, units_to_back_stock=0, unit_cost_paid=None, total_paid=paid,
                   resolved_at="2026-11-29T15:00:00Z")


def _receipt_sync_spends_the_cash(conn):
    """The receipt sync reads `pr-old` again: the bridge corrected its receipt to $590.00."""
    from tests.test_sync_requests import fulfilled
    from vending.purchase_requests import store

    store(conn, "pr-old", fulfilled("pr-old", _old_line("590.00"), fulfilled_at="2026-11-29T15:30:00Z",
                                    updated_at="2026-11-29T16:00:00Z", total_paid="590.00"), NOW)


@pytest.mark.parametrize("change", [_lower_the_ceiling, _receipt_sync_spends_the_cash],
                         ids=["lower ceiling", "receipt sync"])
def test_a_change_after_the_attempt_is_saved_stops_the_post(db, fake, schema_url, capsys, monkeypatch,  # noqa: F811
                                                            change):
    """Round 3, blocker 1: once the attempt was saved, only the deadline was checked again. A lowered price
    ceiling, or a receipt sync that left less cash than the request's total, still posted. The whole preview
    now runs again right before the POST, and the body must be the one approved."""
    seed(db, fake)
    _old_request(db)
    _, plan_hash = previewed(schema_url, fake, capsys)
    assert plan_hash
    _no_input_locks(monkeypatch)
    _changed_once_saved(monkeypatch, schema_url, change)
    rc = run(schema_url, fake, "--submit", "--hash", plan_hash, "--by", "Test Submitter")
    err = capsys.readouterr().err
    assert (rc, len(fake.posts)) == (1, 0)
    assert ("above_ceiling" if change is _lower_the_ceiling else "insufficient_balance") in err
    [(status, detail)] = db.execute("select status, detail from purchase_submissions").fetchall()
    assert status == "not_filed" and detail.startswith("nothing sent: ")


def test_a_changed_body_after_the_attempt_is_saved_stops_the_post(db, fake, schema_url, capsys, monkeypatch):  # noqa: F811
    """Round 3, blocker 1: the body re-planned before the POST must be byte-identical to the approved one. A
    load changed mid-submit gives another body, so nothing is sent."""
    seed(db, fake)
    _, plan_hash = previewed(schema_url, fake, capsys)
    _no_input_locks(monkeypatch)
    plan = plan_json()
    plan["day_one"][1]["units"] = 6
    _changed_once_saved(monkeypatch, schema_url,
                        lambda other: other.execute("update first_fill_plans set plan = %s", (Jsonb(plan),)))
    rc = run(schema_url, fake, "--submit", "--hash", plan_hash, "--by", "Test Submitter")
    err = capsys.readouterr().err
    assert (rc, len(fake.posts)) == (1, 0)
    assert "plan_changed" in err and "pick_changed" in err
    assert db.execute("select status from purchase_submissions").fetchall() == [("not_filed",)]


def test_config_and_receipt_writes_wait_for_the_post(db, fake, schema_url, capsys):  # noqa: F811
    """Round 3, blocker 1: from the check before the POST through the POST, a config change and a receipt sync
    wait for the submit, so what was checked is what is sent."""
    import threading

    from tests.test_sync_requests import fulfilled, receipt
    from vending import config
    from vending.purchase_requests import store

    seed(db, fake)
    _old_request(db)
    _, plan_hash = previewed(schema_url, fake, capsys)
    fake.hold = threading.Event()
    result, blocked = {}, {}
    sender = threading.Thread(target=lambda: result.update(code=run(
        schema_url, fake, "--submit", "--hash", plan_hash, "--by", "Test Submitter")))
    sender.start()
    try:
        assert fake.received.wait(30)
        with open_in(*schema_url) as other:
            other.execute("set lock_timeout = '500ms'")
            for name, write in (
                    ("config", lambda: config.set_value(other, "price_ceiling_cents", 200, "Test Changer")),
                    ("receipts", lambda: store(other, "pr-old", fulfilled("pr-old", receipt(), total_paid="12.37"),
                                               NOW))):
                try:
                    write()
                    blocked[name] = False
                except psycopg.errors.LockNotAvailable:
                    blocked[name] = True
    finally:
        fake.hold.set()
        sender.join(30)
    assert blocked == {"config": True, "receipts": True}
    assert result == {"code": 0} and len(fake.posts) == 1


def test_the_11_nov_test_opening_is_refused_whatever_the_fall_cut(db, fake, schema_url, capsys, monkeypatch):  # noqa: F811
    """Round 3, blocker 2: with FALL_CUTOFF moved to 10 Nov, the 11 Nov test opening was after the cut, so the
    fill took it for the real one. The test opening is now told by its ledger entry (`ledger.is_test_opening`)."""
    monkeypatch.setenv("FALL_CUTOFF", "2026-11-10T00:00:00-05:00")
    seed(db, fake, opened=None)
    detail = "opening capital (test state, 11 Nov; to be reset before launch)"
    entry = ledger.record(db, "opening", 60000, detail, "Test Treasurer", TEST_OPENING_AT)
    db.execute("update ledger_entries set id = 1 where id = %s", (entry.id,))
    out, plan_hash = previewed(schema_url, fake, capsys)
    assert plan_hash is None and "test_opening: " in out
    rc = run(schema_url, fake, "--submit", "--hash", plan_hash or "0" * 64, "--by", "Test Submitter")
    capsys.readouterr()
    assert (rc, len(fake.posts)) == (1, 0)
    assert "test_opening" in codes(db)


def test_is_test_opening_matches_the_entry_and_nothing_else():
    """The same names and rule as PR #222's: id, Eastern date and detail, all three."""
    from datetime import date
    from fractions import Fraction

    assert (ledger.TEST_OPENING_ID, ledger.TEST_OPENING_DATE, ledger.TEST_OPENING_DETAIL) == (
        1, date(2026, 11, 11), "opening capital (test state, 11 Nov; to be reset before launch)")

    def entry(**over):
        base = dict(id=ledger.TEST_OPENING_ID, at=TEST_OPENING_AT, kind="opening", amount_cents=Fraction(60000),
                    detail=ledger.TEST_OPENING_DETAIL, reference=None, recorded_by="Test Treasurer")
        return ledger.Entry(**{**base, **over})

    assert ledger.is_test_opening(entry())
    assert not ledger.is_test_opening(None)
    assert not ledger.is_test_opening(entry(id=2))
    assert not ledger.is_test_opening(entry(detail="opening capital"))
    assert not ledger.is_test_opening(entry(at=OPENED))


ALT16 = dict(store="Walmart", units_per_pack=16, expected_cents=1400, max_cents=1500)


def test_an_alternate_with_other_packs_than_the_pick_needs_approval(db, fake, schema_url, capsys):  # noqa: F811
    """Round 3, major 3: only the primary's packs were compared. A Walmart 16-pack alternate buys 5 x 16 (80
    units) where the pick buys 6 x 12 (72); its $75.00 worst case is under the pick's $78.00, so nothing said so
    and it posted. Every choice the stocker may use is compared now."""
    seed(db, fake)
    sourcing.set_option(db, option("COKE20", "alternate", **ALT16), NOW)
    out, plan_hash = previewed(schema_url, fake, capsys)
    assert plan_hash
    argv = ["--submit", "--hash", plan_hash, "--by", "Test Submitter"]
    rc = run(schema_url, fake, *argv)
    capsys.readouterr()
    assert (rc, len(fake.posts)) == (1, 0)
    assert codes(db) == ["pick_deviation"]
    assert ("- COKE20 (alternate, Walmart): the pick buys 6 x 12 (72 units, at most $78.00); the alternate buys "
            "5 x 16 (80 units, at most $75.00)") in out
    assert run(schema_url, fake, *argv, "--accept-deviations") == 0
    [posted] = fake.posts
    assert posted["lines"][0]["alternate"]["units_per_pack"] == 16


def test_migration_025_gives_up_fast_rather_than_queue_behind_a_submit(schema_url):  # noqa: F811
    """Round 3, major 4: 025's ALTER waited behind a submit's open read of purchase_submissions, and the submit's
    insert of its attempt then queued behind the ALTER: neither could finish. 025 now gives up after its short
    lock_timeout, and a retry once nothing is submitting applies it."""
    import time

    from vending import db as vending_db

    with open_in(*schema_url) as conn, open_in(*schema_url) as submit:
        # Without 025's own limit this would wait forever: fail the test instead of hanging it.
        conn.execute("set statement_timeout = '20s'")
        conn.execute("alter table purchase_submissions drop column purpose")
        conn.execute("delete from schema_migrations where version = '025_submission_purpose.sql'")
        with submit.transaction():
            # What a submit in flight holds: its gate transaction has read purchase_submissions.
            submit.execute("select count(*) from purchase_submissions").fetchone()
            began = time.monotonic()
            with pytest.raises(psycopg.errors.LockNotAvailable):
                vending_db.migrate(conn)
            assert time.monotonic() - began < 10
        assert vending_db.migrate(conn) == ["025_submission_purpose.sql"]
        assert conn.execute("select count(*) from information_schema.columns where table_name ="
                            " 'purchase_submissions' and column_name = 'purpose' and table_schema = current_schema()"
                            ).fetchone() == (1,)


# --- Sol review, round 4 --------------------------------------------------------------------------

def test_a_halt_turned_on_during_the_submit_wins_and_nothing_is_sent(db, fake, schema_url, capsys, monkeypatch):  # noqa: F811
    """Round 4, blocker 1: the write gate holds the halt row, so a `halt on` started then waits for it, holding a
    lock on `config`. The fill's SHARE lock on `config` then closed a deadlock; Postgres cancelled the halt, the
    flag stayed off and the fill posted. The fill now takes that lock without waiting, before it saves anything,
    and refuses instead: the halt goes through and nothing is sent."""
    import threading
    import time

    from vending import config

    seed(db, fake)
    _, plan_hash = previewed(schema_url, fake, capsys)
    real_hold = fill.hold_inputs
    halt: dict = {}
    halter = open_in(*schema_url)
    pid = halter.info.backend_pid

    def turn_halt_on():
        try:
            set_halted(halter, True, "Test Halter")
            halt["error"] = None
        except psycopg.Error as error:
            halt["error"] = type(error).__name__

    thread = threading.Thread(target=turn_halt_on)

    def halt_waiting_then_hold(conn):
        thread.start()
        deadline = time.monotonic() + 20
        while not db.execute("select cardinality(pg_blocking_pids(%s)) > 0", (pid,)).fetchone()[0]:
            assert time.monotonic() < deadline, "the halt never waited on the submit"
            time.sleep(0.05)
        real_hold(conn)

    monkeypatch.setattr(fill, "hold_inputs", halt_waiting_then_hold)
    try:
        rc = run(schema_url, fake, "--submit", "--hash", plan_hash, "--by", "Test Submitter")
    finally:
        thread.join(30)
        halter.close()
    err = capsys.readouterr().err
    assert halt == {"error": None} and config.is_halted(db)
    assert (rc, len(fake.posts)) == (1, 0)
    assert "settings_busy" in err and codes(db) == ["settings_busy"]
    assert db.execute("select count(*) from purchase_submissions").fetchone() == (0,)


def _takings_since_the_opening(conn):
    """37 Coca-Colas sold at $2.50 since the opening: $92.50 of takings, by the polls."""
    from tests.conftest import store_poll

    store_poll(conn, "before", "2026-11-28T16:00:00+00:00", {COKE[0]: (250, 100)})
    store_poll(conn, "after", "2026-11-29T12:00:00+00:00", {COKE[0]: (250, 137)})


def test_the_fills_balance_gate_does_not_count_takings_from_the_polls(db, fake, schema_url, capsys):  # noqa: F811
    """Round 4, major 2: the fill's balance gate counted takings worked out from the polls, which a history
    backfill can change after the last check. It now counts only the opening, capital and adjustments less
    receipts, visit fees, model calls and tax remitted. $100.00 of capital plus $92.50 of takings no longer
    covers a $162.00 request."""
    import math

    seed(db, fake, capital=10000)
    _takings_since_the_opening(db)
    with open_in(*schema_url) as conn:
        assert math.floor(ledger.balance(conn, NOW).spendable_cents) > 16200   # the ledger itself counts them
        p = fill.preview(conn, fake.client(), NOW)
    assert p.plan.cost.balance_cents == 10000
    assert "insufficient_balance" in [r.code for r in p.refusals] and p.approval is None
    rc = run(schema_url, fake, "--submit", "--hash", "0" * 64, "--by", "Test Submitter")
    err = capsys.readouterr().err
    assert (rc, len(fake.posts)) == (1, 0) and "insufficient_balance" in err


def test_a_poll_backfilled_after_the_final_check_does_not_move_the_gate(db, fake, schema_url, capsys,  # noqa: F811
                                                                       monkeypatch):
    """Round 4, major 2: Sol's backfill, committed after the final check and before the POST. A poll just before
    the opening that already reads the 37 sales moves the takings since the opening from $92.50 to nothing. The
    fill's gate never counted them, so it reads the same before and after."""
    seed(db, fake)
    _takings_since_the_opening(db)
    with open_in(*schema_url) as conn:
        before = fill.preview(conn, fake.client(), NOW)
    assert before.ok and before.plan.cost.balance_cents == 60000
    real_final = fill._final_check

    def checked_then_backfilled(*args, **kwargs):
        found = real_final(*args, **kwargs)
        from tests.conftest import store_poll

        with open_in(*schema_url) as other:
            store_poll(other, "backfill", "2026-11-28T16:59:00+00:00", {COKE[0]: (250, 137)})
        return found

    monkeypatch.setattr(fill, "_final_check", checked_then_backfilled)
    assert run(schema_url, fake, "--submit", "--hash", before.approval, "--by", "Test Submitter") == 0
    assert len(fake.posts) == 1
    with open_in(*schema_url) as conn:
        assert ledger.balance(conn, NOW).takings.cents == 0      # the backfill did move the ledger's takings
        after = fill.preview(conn, fake.client(), NOW)
    assert after.plan.cost.balance_cents == before.plan.cost.balance_cents == 60000


def test_quotes_older_than_a_week_are_refused(db, fake, schema_url, capsys):  # noqa: F811
    """Round 4, major 3: a quote over a week old was only a warning, so the fill posted on 9-day-old primary and
    alternate prices. Each choice the stocker may use must have been checked within the week."""
    from datetime import timedelta

    seed(db, fake)
    old = NOW - timedelta(days=9)
    sourcing.set_option(db, option("COKE20", checked_at=old), NOW)
    sourcing.set_option(db, option("COKE20", "alternate", store="Walmart", checked_at=old), NOW)
    out, plan_hash = previewed(schema_url, fake, capsys)
    assert plan_hash is None and out.count("stale_price: ") == 2
    rc = run(schema_url, fake, "--submit", "--hash", plan_hash or "0" * 64, "--by", "Test Submitter")
    capsys.readouterr()
    assert (rc, len(fake.posts)) == (1, 0)
    assert "stale_price" in codes(db)


def test_a_body_changed_after_the_save_is_refused_by_the_byte_check_alone(db, fake, schema_url, capsys,  # noqa: F811
                                                                         monkeypatch):
    """Round 4, minor 4: the earlier body test changed the pick, so other guards refused it too. Here the pick,
    the plan and its hash stay as approved, and only the body about to be sent changes after the real save:
    the byte comparison is the one refusal."""
    from vending import submissions

    seed(db, fake)
    _, plan_hash = previewed(schema_url, fake, capsys)
    real_save = submissions.save_sending

    def saved_then_body_changed(side, saved_hash, body, *args, **kwargs):
        row = real_save(side, saved_hash, body, *args, **kwargs)
        body["lines"][0]["primary"]["packs"] += 1
        return row

    monkeypatch.setattr(submissions, "save_sending", saved_then_body_changed)
    rc = run(schema_url, fake, "--submit", "--hash", plan_hash, "--by", "Test Submitter")
    err = capsys.readouterr().err
    assert (rc, len(fake.posts)) == (1, 0)
    assert codes(db) == ["request_changed"] and "request_changed" in err
    [(status, detail)] = db.execute("select status, detail from purchase_submissions").fetchall()
    assert status == "not_filed" and detail.startswith("nothing sent: ")


# --- Sol review, round 5 --------------------------------------------------------------------------

def _wait_until(check, what: str, seconds: float = 20) -> None:
    import time

    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, what
        time.sleep(0.05)


def _bounded(schema_url, pids: list[int] | None = None, isolation=None):  # noqa: F811
    """Connections for a fill under test: a lock wait gives up after 10 s, so a fill that waits fails the test
    instead of hanging it. `pids` collects their backend IDs, the main connection's first."""
    def connect():
        c = open_in(*schema_url)
        c.execute("set lock_timeout = '10s'")
        if isolation is not None:
            c.isolation_level = isolation
        if pids is not None:
            pids.append(c.info.backend_pid)
        return c
    return connect


def _blocked_or_done(db, pids: list[int], thread) -> bool:  # noqa: F811
    """True once `thread` has ended, or the backend in `pids` waits on a lock."""
    if not thread.is_alive():
        return True
    return bool(pids) and db.execute("select cardinality(pg_blocking_pids(%s)) > 0", (pids[0],)).fetchone()[0]


def test_a_halt_started_while_the_receipt_sync_holds_its_lock_wins(db, fake, schema_url, capsys):  # noqa: F811
    """Round 5, major 1, Sol's case: the receipt sync holds its lock; the fill started and waited for it while
    holding the halt row and `config`; a `halt on` then queued behind the fill, and once the sync let go the
    fill posted before the halt committed. The fill now takes every lock without waiting and refuses
    (`receipts_busy`) before it saves anything, so the halt goes through and nothing is sent."""
    import threading

    from vending import config
    from vending.purchase_requests import RECEIPTS_LOCK

    seed(db, fake)
    _, plan_hash = previewed(schema_url, fake, capsys)
    fill_pids: list[int] = []
    connect = _bounded(schema_url, fill_pids)
    result, halt = {}, {}
    sender = threading.Thread(target=lambda: result.update(code=fill.main(
        ["--submit", "--hash", plan_hash, "--by", "Test Submitter"], bridge=fake.client, connect=connect, now=NOW)))
    with open_in(*schema_url) as sync, open_in(*schema_url) as halter:
        halter_pid = halter.info.backend_pid

        def turn_halt_on():
            try:
                set_halted(halter, True, "Test Halter")
                halt["error"] = None
            except psycopg.Error as error:
                halt["error"] = type(error).__name__

        halting = threading.Thread(target=turn_halt_on)
        try:
            with sync.transaction():
                sync.execute("select pg_advisory_xact_lock(hashtext(%s))", (RECEIPTS_LOCK,))
                sender.start()
                _wait_until(lambda: _blocked_or_done(db, fill_pids, sender), "the fill neither waited nor ended")
                halting.start()
                _wait_until(lambda: _blocked_or_done(db, [halter_pid], halting), "the halt neither waited nor ended")
            # The sync's lock is released here.
        finally:
            for t in (sender, halting):
                if t.ident is not None:
                    t.join(30)
    err = capsys.readouterr().err
    assert halt == {"error": None} and config.is_halted(db)
    assert (result.get("code"), len(fake.posts)) == (1, 0)
    assert "receipts_busy" in err and codes(db) == ["receipts_busy"]
    assert db.execute("select count(*) from purchase_submissions").fetchone() == (0,)


@pytest.mark.parametrize("busy", ["first_fill_plans", "sourcing_options", "slot_assignment"])
def test_a_busy_input_is_refused_at_once_with_nothing_saved(db, fake, schema_url, capsys, busy):  # noqa: F811
    """Round 5, major 1: a writer holding any input the fill locks makes it refuse at once (`inputs_busy`)
    rather than wait while it holds the halt row."""
    seed(db, fake)
    _, plan_hash = previewed(schema_url, fake, capsys)
    with open_in(*schema_url) as writer, writer.transaction():
        if busy == "slot_assignment":
            writer.execute("select 1 from slot_assignment where id = 1 for update")
        else:
            writer.execute(f"lock table {busy} in row exclusive mode")
        rc = fill.main(["--submit", "--hash", plan_hash, "--by", "Test Submitter"], bridge=fake.client,
                       connect=_bounded(schema_url), now=NOW)
    err = capsys.readouterr().err
    assert (rc, len(fake.posts)) == (1, 0)
    assert "inputs_busy" in err and codes(db) == ["inputs_busy"]
    assert db.execute("select count(*) from purchase_submissions").fetchone() == (0,)


def _late_clock(monkeypatch, later: datetime) -> None:
    """The database clock reads NOW until the attempt is saved; the final check's gather then takes long enough
    that the clock reads `later` once it is done."""
    from vending import planning, submissions

    clock = {"t": NOW, "saved": False}
    monkeypatch.setattr(ledger, "db_now", lambda conn: clock["t"])
    real_save, real_gather = submissions.save_sending, planning.gather

    def saved(*args, **kwargs):
        row = real_save(*args, **kwargs)
        clock["saved"] = True
        return row

    def gathered(*args, **kwargs):
        found = real_gather(*args, **kwargs)
        if clock["saved"]:
            clock["t"] = later
        return found

    monkeypatch.setattr(submissions, "save_sending", saved)
    monkeypatch.setattr(planning, "gather", gathered)


@pytest.mark.parametrize("what", ["quote", "debit"])
def test_the_last_clock_reading_rechecks_quote_age_and_cash(db, fake, schema_url, capsys, monkeypatch, what):  # noqa: F811
    """Round 5, minor 2: the final check read the clock before it gathered. A quote that turned 7 days old,
    or a scheduled debit that came due, during the gather still posted. Quote age and cash are now checked
    again at the clock reading taken just before the POST, the one the deadline uses."""
    from datetime import timedelta

    seed(db, fake)
    later = NOW + timedelta(hours=1)          # 21:00 ET Sun 29 Nov: still before the deadline
    if what == "quote":
        sourcing.set_option(db, option("COKE20", checked_at=NOW - timedelta(days=7) + timedelta(minutes=30)), NOW)
    else:
        ledger.record(db, "adjustment", -59000, "scheduled debit", "Test Treasurer", NOW + timedelta(minutes=30))
    _, plan_hash = previewed(schema_url, fake, capsys)
    assert plan_hash
    _late_clock(monkeypatch, later)
    rc = fill.main(["--submit", "--hash", plan_hash, "--by", "Test Submitter"], bridge=fake.client,
                   connect=_bounded(schema_url), now=None)
    err = capsys.readouterr().err
    assert (rc, len(fake.posts)) == (1, 0)
    assert ("stale_price" if what == "quote" else "insufficient_balance") in err
    [(status, detail)] = db.execute("select status, detail from purchase_submissions").fetchall()
    assert status == "not_filed" and detail.startswith("nothing sent: ")


def _clock_crosses_during_the_ledger_read(monkeypatch, later: datetime) -> None:
    """The database clock reads NOW until the final check's own ledger read (in `_at_the_post`, after the attempt
    is saved); that read takes long enough that the clock reads `later` once it is done."""
    from vending import submissions

    import sys

    clock = {"t": NOW, "saved": False}
    monkeypatch.setattr(ledger, "db_now", lambda conn: clock["t"])
    real_save, real_balance = submissions.save_sending, ledger.balance

    def saved(*args, **kwargs):
        row = real_save(*args, **kwargs)
        clock["saved"] = True
        return row

    def balanced(*args, **kwargs):
        found = real_balance(*args, **kwargs)
        if clock["saved"] and sys._getframe(1).f_code.co_name == "_at_the_post":   # the final check's own read
            clock["t"] = later
        return found

    monkeypatch.setattr(submissions, "save_sending", saved)
    monkeypatch.setattr(ledger, "balance", balanced)


@pytest.mark.parametrize("what", ["deadline", "quote", "debit"])
def test_the_final_clock_reading_is_taken_after_the_ledger_read(db, fake, schema_url, capsys, monkeypatch, what):  # noqa: F811
    """Round 6, finding 1: the final clock reading came before `ledger.balance()`. The deadline, a quote's
    week or a scheduled debit could cross during that read and the fill still posted. The last reading is now
    taken after the ledger read, right before the POST, and all three are checked at it."""
    from datetime import timedelta

    seed(db, fake)
    if what == "deadline":
        later = fill.fill_deadline() + timedelta(minutes=1)
    else:
        later = NOW + timedelta(hours=1)          # 21:00 ET Sun 29 Nov: still before the deadline
    if what == "quote":
        sourcing.set_option(db, option("COKE20", checked_at=NOW - timedelta(days=7) + timedelta(minutes=30)), NOW)
    elif what == "debit":
        ledger.record(db, "adjustment", -59000, "scheduled debit", "Test Treasurer", NOW + timedelta(minutes=30))
    _, plan_hash = previewed(schema_url, fake, capsys)
    assert plan_hash
    _clock_crosses_during_the_ledger_read(monkeypatch, later)
    rc = fill.main(["--submit", "--hash", plan_hash, "--by", "Test Submitter"], bridge=fake.client,
                   connect=_bounded(schema_url), now=None)
    err = capsys.readouterr().err
    assert (rc, len(fake.posts)) == (1, 0)
    assert {"deadline": "fill_deadline", "quote": "stale_price", "debit": "ledger_entry_due"}[what] in err
    [(status, detail)] = db.execute("select status, detail from purchase_submissions").fetchall()
    assert status == "not_filed" and detail.startswith("nothing sent: ")


def test_the_fill_refuses_at_the_start_if_the_halt_is_already_on(db, fake, schema_url, capsys):  # noqa: F811
    """Round 6, finding 2: a halt that is on before the fill starts stops it before anything is saved or sent."""
    seed(db, fake)
    _, plan_hash = previewed(schema_url, fake, capsys)
    set_halted(db, True, "Test Halter")
    rc = fill.main(["--submit", "--hash", plan_hash, "--by", "Test Submitter"], bridge=fake.client,
                   connect=_bounded(schema_url), now=NOW)
    err = capsys.readouterr().err
    assert (rc, len(fake.posts)) == (1, 0)
    assert "halted" in err
    assert db.execute("select count(*) from purchase_submissions").fetchone() == (0,)


def _repeatable_read(schema_url, pids: list[int] | None = None):  # noqa: F811
    return _bounded(schema_url, pids, psycopg.IsolationLevel.REPEATABLE_READ)


def test_the_fills_transaction_is_read_committed_whatever_the_default(db, fake, schema_url, capsys, monkeypatch):  # noqa: F811
    """Round 5, minor 3: the fill's transaction took the connection's isolation level. It is now set to READ
    COMMITTED explicitly, and checked."""
    seed(db, fake)
    _, plan_hash = previewed(schema_url, fake, capsys)
    real_hold, seen = fill.hold_inputs, []

    def noted(conn):
        seen.append(conn.execute("select current_setting('transaction_isolation')").fetchone()[0])
        real_hold(conn)

    monkeypatch.setattr(fill, "hold_inputs", noted)
    rc = fill.main(["--submit", "--hash", plan_hash, "--by", "Test Submitter"], bridge=fake.client,
                   connect=_repeatable_read(schema_url), now=NOW)
    assert (rc, len(fake.posts), seen) == (0, 1, ["read committed"])


@pytest.mark.parametrize("layer", ["gate", "side"])
def test_two_fills_at_once_send_one_request(db, fake, schema_url, capsys, monkeypatch, layer):  # noqa: F811
    """Round 5, minor 3: under REPEATABLE READ, a second fill that started before the first saved its attempt
    waited on the halt row with the snapshot it took then, so it missed the first's attempt and posted again.
    Now the gate's transaction is READ COMMITTED, and the duplicate checks run again on the fresh side
    connection before the POST. `side` turns the first off, to show the second alone stops it."""
    import threading

    from vending import submissions, write_gate

    seed(db, fake)
    _, plan_hash = previewed(schema_url, fake, capsys)
    if layer == "side":
        monkeypatch.setattr(write_gate, "_read_committed", lambda conn, outer: None)
    real_save, saving, go = submissions.save_sending, threading.Event(), threading.Event()

    def first_waits(*args, **kwargs):
        if not saving.is_set():         # the first fill: about to save its attempt
            saving.set()
            assert go.wait(30)
        return real_save(*args, **kwargs)

    monkeypatch.setattr(submissions, "save_sending", first_waits)
    argv = ["--submit", "--hash", plan_hash, "--by", "Test Submitter"]
    first, second, pids = {}, {}, []
    a = threading.Thread(target=lambda: first.update(code=fill.main(
        argv, bridge=fake.client, connect=_repeatable_read(schema_url), now=NOW)))
    b = threading.Thread(target=lambda: second.update(code=fill.main(
        argv, bridge=fake.client, connect=_repeatable_read(schema_url, pids), now=NOW)))
    a.start()
    try:
        assert saving.wait(30)
        b.start()
        _wait_until(lambda: _blocked_or_done(db, pids, b), "the second fill neither waited nor ended")
    finally:
        go.set()
        a.join(30)
        if b.ident is not None:
            b.join(30)
    err = capsys.readouterr().err
    assert (first, second, len(fake.posts)) == ({"code": 0}, {"code": 1}, 1)
    assert "fill_already_sent" in err
    rows = db.execute("select status from purchase_submissions order by id").fetchall()
    assert rows == ([("accepted",)] if layer == "gate" else [("accepted",), ("not_filed",)])
