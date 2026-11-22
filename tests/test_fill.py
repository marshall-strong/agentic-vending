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
    return sourcing.Option(sku=sku, rank=rank, **{**base, **over}, pack_description=None, checked_at=CHECKED,
                           checked_by="Test Checker")


def seed(conn, fake, *, opened=OPENED, prices=("COKE20", "PICKLE")) -> int:  # noqa: F811
    """A picked plan, our catalog copy, checked prices for `prices`, the real opening and the halt switch off."""
    if opened is not None:
        ledger.record(conn, "opening", 60000, "opening capital", "Test Treasurer", opened)
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
    fake.answer = (201, {"id": "pr-2", "status": "pending"})
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
    assert "- COKE20: the pick buys 6 x 12 (72 units, at most $78.00); this request buys 6 x 12 (72 units, at most " \
           "$90.00)" in out
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
    real_balance = ledger.balance
    monkeypatch.setattr(ledger, "balance", lambda conn, as_of=None: replace(
        real_balance(conn, as_of), opening_at=TEST_OPENING_AT))
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
