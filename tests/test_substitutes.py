"""Substitutes: a line's alternate may be another drink (Kevin, 25 Nov: "buy extra ... of a safe drink and double
stock two slots if needed"). The bridge takes an alternate with its own `sku` (docs/dex-bridge.md). The planner
names it only when the purchase option carries one, so every other request body is unchanged. The day-one fill
reads them from `fill_substitutes`; `python -m vending.alternates` picks them."""

from dataclasses import replace
from datetime import timedelta

import pytest

from tests.test_planner import CONFIG, COKE, PICKLE, TABLE, TUESDAY, codes, current, desired, line, pickle_17
from vending.planner import DesiredSlot, PurchaseOption, SlotState, plan_restock

EMPTY_17 = {"17": SlotState("17", None, None, 0, None)}
SOLD_OUT_COKE_17 = {"17": SlotState("17", COKE, 200, 0, None)}

SUB = PurchaseOption("Walmart", 24, "34.99", expected_price_per_pack="29.99", sku=COKE)


def with_alternate(product, alternate):
    return tuple(replace(s, alternate=alternate) if s.product == product else s for s in TABLE)


# --- The planner (pure) -------------------------------------------------------------------------------

def test_a_substitute_alternate_names_its_own_sku_and_change_product():
    p = plan_restock(current(table=with_alternate(PICKLE, SUB), slots=EMPTY_17), desired(pickle_17()), CONFIG,
                     TUESDAY)
    assert codes(p) == []
    body = line(p, "17").to_api()
    assert body["primary"]["sku"] == PICKLE and body["alternate"]["sku"] == COKE
    assert body["primary"]["change_product"] is True and body["alternate"]["change_product"] is True
    assert body["alternate"]["store"] == "Walmart" and body["alternate"]["sale_price"] == "3.00"
    # The slot last held Coke and is sold out: the Coke substitute is what it holds, so it changes nothing.
    p = plan_restock(current(table=with_alternate(PICKLE, SUB), slots=SOLD_OUT_COKE_17), desired(pickle_17()),
                     CONFIG, TUESDAY)
    assert codes(p) == [] and line(p, "17").to_api()["alternate"]["change_product"] is False


def test_a_substitute_goes_only_into_an_empty_slot():
    """Sol, #257: six units of the substitute already in the column would stay (change_product false), outside
    the floor and the column count."""
    p = plan_restock(current(table=with_alternate(PICKLE, SUB)), desired(pickle_17()), CONFIG, TUESDAY)
    assert any("only into an empty slot" in r.message for r in p.refusals)


def test_a_substitute_needs_a_known_column_capacity():
    config = replace(CONFIG, capacity_overrides={k: v for k, v in CONFIG.capacity_overrides.items() if k != COKE})
    p = plan_restock(current(table=with_alternate(PICKLE, SUB), slots=EMPTY_17), desired(pickle_17()), config,
                     TUESDAY)
    assert any("no known column capacity" in r.message for r in p.refusals)


def test_the_wildcards_drink_stays_in_one_slot_with_substitutes():
    """Sol, #257: the wildcard's backup may not be another slot's drink, nor another slot's backup the
    wildcard's."""
    config = replace(CONFIG, wildcard_slot="17")
    p = plan_restock(current(table=with_alternate(PICKLE, SUB), slots=EMPTY_17), desired(pickle_17()), config,
                     TUESDAY)
    assert any("the wildcard's drink must be in no other slot" in r.message for r in p.refusals)
    # Slot 18, a regular slot, backs Pickle Potion up with the wildcard's own drink.
    empty = {s: SlotState(s, None, None, 0, None) for s in ("17", "18")}
    pickle_sub = PurchaseOption("Walmart", 24, "34.99", expected_price_per_pack="29.99", sku=PICKLE)
    table = with_alternate(COKE, pickle_sub)
    p = plan_restock(current(table=table, slots=empty),
                     desired(pickle_17(), DesiredSlot("18", product=COKE, sale_price="2.50", fill_to=6)),
                     config, TUESDAY)
    assert any("is the wildcard's drink" in r.message for r in p.refusals)


def test_the_shopping_list_names_the_substitute_drink():
    p = plan_restock(current(table=with_alternate(PICKLE, SUB), slots=EMPTY_17), desired(pickle_17()), CONFIG,
                     TUESDAY)
    walmart = p.directive.split("Walmart\n", 1)[1]
    assert walmart.lstrip().startswith(COKE) and f"only if {PICKLE} from Amazon is unavailable" in walmart


def test_without_a_sku_the_alternate_is_the_lines_own_drink():
    same = PurchaseOption("Walmart", 12, "29.99", expected_price_per_pack="27.00")
    p = plan_restock(current(table=with_alternate(PICKLE, same)), desired(pickle_17()), CONFIG, TUESDAY)
    body = line(p, "17").to_api()
    assert body["alternate"]["sku"] == PICKLE and body["alternate"]["change_product"] is True


@pytest.mark.parametrize("case, sku, code_text", [
    ("inactive", "OLD-SODA", "not active in the catalog"),
    ("itself", PICKLE, "is the line's own drink"),
])
def test_a_substitute_the_bridge_would_refuse_is_refused(case, sku, code_text):
    p = plan_restock(current(table=with_alternate(PICKLE, replace(SUB, sku=sku)), slots=EMPTY_17),
                     desired(pickle_17()), CONFIG, TUESDAY)
    assert "bad_sourcing" in codes(p)
    assert any(code_text in r.message for r in p.refusals)


def test_a_substitute_that_does_not_fit_the_column_is_refused():
    config = replace(CONFIG, capacity_overrides={**CONFIG.capacity_overrides, COKE: 4})
    p = plan_restock(current(table=with_alternate(PICKLE, SUB), slots=EMPTY_17), desired(pickle_17()), config,
                     TUESDAY)
    assert "bad_sourcing" in codes(p)
    assert any("holds at most 4" in r.message for r in p.refusals)


def test_a_substitute_on_a_pooled_line_is_refused():
    two = (pickle_17(), DesiredSlot("18", product=PICKLE, sale_price="3.00", fill_to=6))
    p = plan_restock(current(table=with_alternate(PICKLE, SUB)), replace(desired(*two), pool=True), CONFIG, TUESDAY)
    assert "bad_sourcing" in codes(p)
    assert any("pooled" in r.message for r in p.refusals)


# --- The fill and the backup step (database) ----------------------------------------------------------

psycopg = pytest.importorskip("psycopg")

from psycopg.types.json import Jsonb  # noqa: E402

from tests.conftest import TEST_SLOTS, open_in  # noqa: E402
from tests.test_alternates import lookup  # noqa: E402
from tests.test_desired_state import fake  # noqa: E402,F401  (a fixture)
from tests.test_fill import NOW, OPENED, option  # noqa: E402
from tests.test_server import db  # noqa: E402,F401  (a fixture)
from vending import alternates, fill, ledger, sourcing  # noqa: E402
from vending.config import set_halted  # noqa: E402
from vending.sync_catalog import sync as catalog_sync  # noqa: E402

LONE, POOLED = TEST_SLOTS[0], TEST_SLOTS[1:]
PICKLE12 = dict(store="Walmart", units_per_pack=12, expected_cents=900, max_cents=1000, url=None)


def seed(conn, fake) -> int:  # noqa: F811
    """Coca-Cola in one slot (1 x 12, at most $13.00), Pickle Potion pooled in the other 19 (Walmart 12-packs)."""
    ledger.record(conn, "opening", 60000, "opening capital", "Test Treasurer", OPENED)
    set_halted(conn, False, "test")
    catalog_sync(conn, fake.client(), now=NOW, log=lambda _: None)
    sourcing.set_option(conn, option("COKE20"), NOW)
    sourcing.set_option(conn, option("PICKLE", **PICKLE12, checked_at=NOW - timedelta(hours=1)), NOW)
    plan = {"day_one": [{"slot": LONE, "sku": "COKE20", "units": 7, "price_cents": 250}]
            + [{"slot": s, "sku": "PICKLE", "units": 8, "price_cents": 300} for s in POOLED]}
    projection = {"packs": [
        {"sku": "COKE20", "units_per_pack": 12, "expected_cents": 1237, "max_cents": 1300},
        {"sku": "PICKLE", "units_per_pack": 12, "expected_cents": 900, "max_cents": 1000}]}
    return conn.execute(
        "insert into first_fill_plans (loaded_by, label, source_note, plan, projection, slots, picked)"
        " values ('Test Loader', '8', 'round8/plan8.json', %s, %s, %s, true) returning id",
        (Jsonb(plan), Jsonb(projection), list(TEST_SLOTS))).fetchone()[0]


def substitute(conn, plan_id, sku, sub, max_cents):
    conn.execute("insert into fill_substitutes (plan_id, sku, substitute_sku, max_cents, set_by)"
                 " values (%s, %s, %s, %s, 'Test Runner')", (plan_id, sku, sub, max_cents))


def preview(schema_url, fake):  # noqa: F811
    with open_in(*schema_url) as conn:
        return fill.preview(conn, fake.client(), NOW)


def lone_line(p):
    return next(ln for ln in p.plan.lines if ln.slot == LONE).to_api()


def test_the_fill_sends_a_stored_substitute_as_the_alternate(db, fake, schema_url, capsys):  # noqa: F811
    plan_id = seed(db, fake)
    assert preview(schema_url, fake).ok
    substitute(db, plan_id, "COKE20", "PICKLE", 1000)
    p = preview(schema_url, fake)
    assert p.ok and p.deviations == ()
    body = lone_line(p)
    assert body["primary"]["sku"] == "COKE20"
    assert body["alternate"] == {"sku": "PICKLE", "store": "Walmart", "packs": 1, "units_per_pack": 12,
                                 "max_price_per_pack": "10.00", "sale_price": "2.50", "change_product": True}
    assert fill.main(["--submit", "--hash", p.approval, "--by", "Test Submitter"], bridge=fake.client,
                     connect=lambda: open_in(*schema_url), now=NOW) == 0
    [posted] = fake.posts
    assert next(ln for ln in posted["lines"] if ln["slot"] == LONE)["alternate"]["sku"] == "PICKLE"


def test_a_substitute_dearer_than_the_pick_is_a_deviation(db, fake, schema_url):  # noqa: F811
    plan_id = seed(db, fake)
    sourcing.set_option(db, option("PICKLE", **{**PICKLE12, "max_cents": 1400}), NOW)
    substitute(db, plan_id, "COKE20", "PICKLE", 1400)
    p = preview(schema_url, fake)
    assert any(d.startswith("COKE20 (substitute PICKLE, Walmart)") and "at most $14.00" in d for d in p.deviations)


def test_a_substitute_on_a_pooled_line_refuses_the_fill(db, fake, schema_url):  # noqa: F811
    plan_id = seed(db, fake)
    substitute(db, plan_id, "PICKLE", "COKE20", 1300)
    p = preview(schema_url, fake)
    assert not p.ok and any(r.code == "bad_sourcing" and "pooled" in r.message for r in p.refusals)


def test_a_stale_substitute_price_refuses_the_fill(db, fake, schema_url):  # noqa: F811
    plan_id = seed(db, fake)
    sourcing.set_option(db, option("PICKLE", **PICKLE12, checked_at=NOW - timedelta(days=8)), NOW)
    substitute(db, plan_id, "COKE20", "PICKLE", 1000)
    p = preview(schema_url, fake)
    assert any(r.code == "stale_price" and "PICKLE" in r.message for r in p.refusals)


def test_the_backup_step_gives_a_line_with_no_other_store_a_substitute(db, fake, schema_url, capsys):  # noqa: F811
    plan_id = seed(db, fake)
    run = lambda *argv: alternates.main(list(argv), bridge=fake.client,  # noqa: E731
                                        connect=lambda: open_in(*schema_url), now=NOW)
    assert run() == 0
    out = capsys.readouterr().out
    assert "COKE20" in out and "substitute: PICKLE Walmart 12-pack $9.00 (max $10.00)" in out
    assert "Pooled lines left without a backup (1): PICKLE" in out
    assert db.execute("select count(*) from fill_substitutes").fetchone() == (0,)          # a dry run
    assert run("--write", "--by", "Test Runner") == 0
    assert db.execute("select plan_id, sku, substitute_sku, max_cents, set_by from fill_substitutes").fetchall() == [
        (plan_id, "COKE20", "PICKLE", 1000, "Test Runner")]
    p = preview(schema_url, fake)
    assert p.ok and p.deviations == () and lone_line(p)["alternate"]["sku"] == "PICKLE"


def test_a_same_drink_backup_wins_over_a_substitute(db, fake, schema_url, capsys):  # noqa: F811
    seed(db, fake)
    lookup(db, "COKE20", "Walmart", 12, 1150)
    assert alternates.main(["--write", "--by", "Test Runner"], bridge=fake.client,
                           connect=lambda: open_in(*schema_url), now=NOW) == 0
    assert db.execute("select count(*) from fill_substitutes").fetchone() == (0,)
    assert sourcing.options_for(db, ["COKE20"])[-1].store == "Walmart"


def test_a_stored_substitute_that_no_longer_fits_is_removed(db, fake, schema_url, capsys):  # noqa: F811
    """Sol, #257: a stored substitute repriced over its max refused the whole fill, and a rerun left it. Now it is
    removed when nothing else fits."""
    plan_id = seed(db, fake)
    substitute(db, plan_id, "COKE20", "PICKLE", 1000)
    sourcing.set_option(db, option("PICKLE", **{**PICKLE12, "expected_cents": 1400, "max_cents": 1500},
                                   checked_at=NOW - timedelta(hours=1)), NOW)
    assert not preview(schema_url, fake).ok
    assert alternates.main(["--write", "--by", "Test Runner"], bridge=fake.client,
                           connect=lambda: open_in(*schema_url), now=NOW) == 0
    out = capsys.readouterr().out
    assert "removed" in next(ln for ln in out.splitlines() if ln.startswith("COKE20"))
    assert "stored substitute: PICKLE, " in out
    assert db.execute("select count(*) from fill_substitutes").fetchone() == (0,)
    assert preview(schema_url, fake).ok


def test_a_same_drink_backup_replaces_a_stored_substitute(db, fake, schema_url):  # noqa: F811
    plan_id = seed(db, fake)
    substitute(db, plan_id, "COKE20", "PICKLE", 1000)
    lookup(db, "COKE20", "Walmart", 12, 1150)
    assert alternates.main(["--write", "--by", "Test Runner"], bridge=fake.client,
                           connect=lambda: open_in(*schema_url), now=NOW) == 0
    assert db.execute("select count(*) from fill_substitutes").fetchone() == (0,)
    assert sourcing.options_for(db, ["COKE20"])[-1].store == "Walmart"
