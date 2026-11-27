"""`python -m vending.alternates`: a backup store for every line of the picked plan, from the price lookups the
four-store compare logged, such that the fill's own checks still pass (no `pick_deviation`, the $400 cap, the
spendable floor). Uses the picked plan and fake bridge from `tests/test_fill.py`."""

from datetime import timedelta
from decimal import Decimal

import pytest

psycopg = pytest.importorskip("psycopg")

from tests.conftest import open_in  # noqa: E402
from tests.test_desired_state import fake  # noqa: E402,F401  (a fixture)
from tests.test_fill import NOW, option, seed  # noqa: E402
from tests.test_server import db  # noqa: E402,F401  (a fixture)
from vending import alternates, fill, price_search, sourcing  # noqa: E402

LOOKED = NOW - timedelta(hours=3)          # Sun 29 Nov 17:00 Eastern: in-store cutoffs not passed, Amazon's has
SONNET, HAIKU = "claude-sonnet-5-5", "claude-haiku-5-5"
URLS = {"Walmart": "https://www.walmart.com/ip/test-coke/123", "BJs": "https://www.bjs.com/product/test/1",
        "Costco": "https://www.costco.com/test.product.1.html", "Amazon": "https://www.amazon.com/dp/B000ALT1"}


def lookup(conn, sku, store, units, price, *, at=LOOKED, model=SONNET, kind="search", outcome="found",
           stock="in_stock", asked=None, url=None):
    price_search.remember(conn, sku=sku, store=store, kind=kind, asked=asked or (units, units), outcome=outcome,
                          model=model, cost=Decimal(0), by="Test Runner", no_store=False, at=at,
                          seen={"units": units, "price": price, "url": url or URLS[store], "stock": stock})


def run(schema_url, fake, *argv) -> int:  # noqa: F811
    return alternates.main(list(argv), bridge=fake.client, connect=lambda: open_in(*schema_url), now=NOW)


def rows(conn) -> dict:
    return {(o.sku, o.rank): o for o in sourcing.options(conn)}


def preview(schema_url, fake):  # noqa: F811
    with open_in(*schema_url) as conn:
        return fill.preview(conn, fake.client(), NOW)


def test_a_line_without_an_alternate_gets_one_from_a_fresh_checked_price_at_another_store(
        db, fake, schema_url, capsys):  # noqa: F811
    seed(db, fake)
    lookup(db, "COKE20", "Walmart", 12, 1150)
    before = rows(db)
    assert run(schema_url, fake) == 0                  # dry run by default
    out = capsys.readouterr().out
    assert "COKE20" in out and "Walmart 12-pack $11.50" in out and "dry run" in out.lower()
    assert "PICKLE" in out and "Lines with a backup: 0 of 2 now, 1 of 2 after" in out
    assert rows(db) == before
    assert db.execute("select count(*) from action_log where tool = %s", (alternates.TOOL,)).fetchone() == (0,)

    assert run(schema_url, fake, "--write", "--by", "Test Runner") == 0
    alt = rows(db)[("COKE20", "alternate")]
    # The pick allows 6 x $13.00, so the alternate's max is the agent's +25% held to $13.00 a pack.
    assert (alt.store, alt.units_per_pack, alt.expected_cents, alt.max_cents) == ("Walmart", 12, 1150, 1300)
    assert alt.checked_at == LOOKED and alt.checked_by == sourcing.AGENT_BY and alt.url == URLS["Walmart"]
    assert "Test Runner" in alt.note
    p = preview(schema_url, fake)
    assert p.ok and p.deviations == ()


def test_an_alternate_a_cent_over_the_pick_gets_its_max_lowered(db, fake, schema_url, capsys):  # noqa: F811
    """C4's case on 25 Nov: the alternate's most-we-pay is $0.02 over the pick's, so the plain submit is refused
    (`pick_deviation`). Its price is well under, so its max comes down to the pick's."""
    seed(db, fake)
    sourcing.set_option(db, option("COKE20", "alternate", store="Walmart", expected_cents=1150, max_cents=1302), NOW)
    assert preview(schema_url, fake).deviations != ()
    assert run(schema_url, fake, "--write", "--by", "Test Runner") == 0
    out = capsys.readouterr().out
    assert "max $13.02 -> $13.00" in out
    alt = rows(db)[("COKE20", "alternate")]
    assert (alt.store, alt.expected_cents, alt.max_cents) == ("Walmart", 1150, 1300)
    assert preview(schema_url, fake).deviations == ()


def test_prices_the_fill_could_not_use_are_not_backups(db, fake, schema_url, capsys):  # noqa: F811
    seed(db, fake)
    lookup(db, "COKE20", "Walmart", 24, 2000)                                  # another pack than the pick
    lookup(db, "COKE20", "BJs", 12, 1100, model=HAIKU)                         # a cheaper model's unchecked find
    lookup(db, "COKE20", "Walmart", 12, 1100, at=NOW - timedelta(hours=30))    # older than --max-age 24
    lookup(db, "COKE20", "Costco", 12, 1100)                                   # the primary's own store
    lookup(db, "COKE20", "Amazon", 12, 1100)                                   # past Amazon's cutoff for Monday
    lookup(db, "COKE20", "BJs", 12, 1000, outcome="out_of_stock", stock="out_of_stock")
    before = rows(db)
    assert run(schema_url, fake, "--write", "--by", "Test Runner") == 0
    out = capsys.readouterr().out
    assert rows(db) == before
    assert "past its cutoff" in out and "Lines with a backup: 0 of 2 now, 0 of 2 after" in out


def test_a_check_models_own_reading_counts_when_it_disagreed_with_a_cheaper_find(
        db, fake, schema_url, capsys):  # noqa: F811
    seed(db, fake)
    lookup(db, "COKE20", "Walmart", 12, 1000, model=HAIKU)
    lookup(db, "COKE20", "Walmart", 12, 1150, kind="check", outcome="check_disagrees", at=LOOKED + timedelta(seconds=9))
    assert run(schema_url, fake, "--write", "--by", "Test Runner") == 0
    assert rows(db)[("COKE20", "alternate")].expected_cents == 1150


def test_a_price_above_what_the_pick_allows_leaves_the_line_without_one(db, fake, schema_url, capsys):  # noqa: F811
    seed(db, fake)
    lookup(db, "COKE20", "Walmart", 12, 1350)
    assert run(schema_url, fake, "--write", "--by", "Test Runner") == 0
    out = capsys.readouterr().out
    assert ("COKE20", "alternate") not in rows(db)
    assert "$13.50 a pack, above the $13.00 the pick allows" in out


def test_a_backup_that_would_pass_the_cap_is_not_set(db, fake, schema_url, capsys, monkeypatch):  # noqa: F811
    """The fill counts each line at its dearer choice. With the primary under the pick's max, a backup at the pick's
    max raises the fill's most-we-pay: 6 x $12.50 + 4 x $21.00 = $159.00 becomes $162.00."""
    seed(db, fake)
    sourcing.set_option(db, option("COKE20", max_cents=1250), NOW)
    lookup(db, "COKE20", "Walmart", 12, 1150)
    monkeypatch.setattr(alternates, "FILL_CAP_CENTS", 16100)
    assert run(schema_url, fake, "--write", "--by", "Test Runner") == 0
    out = capsys.readouterr().out
    assert ("COKE20", "alternate") not in rows(db)
    assert "$162.00, over the $161.00 cap" in out
    monkeypatch.setattr(alternates, "FILL_CAP_CENTS", 16200)
    assert run(schema_url, fake, "--write", "--by", "Test Runner") == 0
    assert rows(db)[("COKE20", "alternate")].max_cents == 1300
    assert "Cap room after: $0.00" in capsys.readouterr().out


def test_writing_needs_a_name(db, fake, schema_url, capsys):  # noqa: F811
    seed(db, fake)
    with pytest.raises(SystemExit):
        run(schema_url, fake, "--write")


# --- Sol's review of #255 ----------------------------------------------------------------------------

def test_a_later_out_of_stock_reading_supersedes_an_earlier_price(db, fake, schema_url, capsys):  # noqa: F811
    seed(db, fake)
    lookup(db, "COKE20", "Walmart", 12, 1150)
    lookup(db, "COKE20", "Walmart", 12, 1150, at=LOOKED + timedelta(hours=1), outcome="out_of_stock",
           stock="out_of_stock")
    assert run(schema_url, fake, "--write", "--by", "Test Runner") == 0
    assert ("COKE20", "alternate") not in rows(db)
    assert "out of stock in a later lookup" in capsys.readouterr().out


def test_a_check_that_read_another_listing_is_not_that_drinks_price(db, fake, schema_url, capsys):  # noqa: F811
    seed(db, fake)
    lookup(db, "COKE20", "Walmart", 12, 1000, model=HAIKU)
    lookup(db, "COKE20", "Walmart", 12, 1150, kind="check", outcome="check_disagrees",
           at=LOOKED + timedelta(seconds=9), url="https://www.walmart.com/ip/another-drink/999")
    assert run(schema_url, fake, "--write", "--by", "Test Runner") == 0
    assert ("COKE20", "alternate") not in rows(db)


def test_nothing_is_written_while_the_fill_is_over_the_cap(db, fake, schema_url, capsys, monkeypatch):  # noqa: F811
    """A change that lowers the most-we-pay is still found, but nothing is written while the fill is over the
    cap after it: the cap is a hard limit (Sol, #255)."""
    seed(db, fake)
    sourcing.set_option(db, option("COKE20", "alternate", store="Walmart", expected_cents=1150, max_cents=1302), NOW)
    monkeypatch.setattr(alternates, "FILL_CAP_CENTS", 16000)        # the fill is $162.00 at the most
    before = rows(db)
    assert run(schema_url, fake, "--write", "--by", "Test Runner") == 1
    assert "over_limit" in capsys.readouterr().err and rows(db) == before


def test_a_dearer_alternate_of_another_pack_is_removed_while_the_fill_is_over_the_cap(
        db, fake, schema_url, capsys, monkeypatch):  # noqa: F811
    """Plan #9 on 27 Nov: a stored Walmart 24-pack alternate for an 8-pack line, max $26.14 against the pick's $8.28,
    put the fill's most-we-pay at $419.60. Its max can't come down (another pack), no other store fitted, so the write
    was refused (over_limit) and the fill's preview refused the day one (over_fill_cap). It is removed: the fill can
    then pass the cap, and lines before it in SKU order are judged under the cap."""
    seed(db, fake)
    # 70 units in 24-packs: 3 x $30.00 = $90.00 for the line, against the pick's 6 x $13.00 = $78.00.
    sourcing.set_option(db, option("COKE20", "alternate", store="Walmart", units_per_pack=24, expected_cents=2000,
                                   max_cents=3000), NOW)
    monkeypatch.setattr(alternates, "FILL_CAP_CENTS", 16500)        # the pick is $162.00 at the most; $174.00 now
    assert run(schema_url, fake, "--write", "--by", "Test Runner") == 0, capsys.readouterr().err
    out = capsys.readouterr().out
    assert ("COKE20", "alternate") not in rows(db)
    assert "removed" in out and "Fill most-we-pay: $174.00 now, $162.00 after" in out
    assert db.execute("select count(*) from action_log where tool = 'sourcing.remove'").fetchone() == (1,)
    assert db.execute("select count(*) from action_log where tool = %s", (alternates.TOOL,)).fetchone() == (1,)


def test_a_cheaper_alternate_of_another_pack_is_kept(db, fake, schema_url, capsys, monkeypatch):  # noqa: F811
    """Only a backup that raises the fill's most-we-pay goes, and only while the fill is over the cap."""
    seed(db, fake)
    sourcing.set_option(db, option("COKE20", "alternate", store="Walmart", units_per_pack=24, expected_cents=2000,
                                   max_cents=2500), NOW)         # 3 x $25.00 = $75.00, under the pick's $78.00
    before = rows(db)
    assert run(schema_url, fake, "--write", "--by", "Test Runner") == 0
    assert rows(db) == before


def test_a_plan_row_changed_after_the_trials_refuses_the_write(db, fake, schema_url):  # noqa: F811
    seed(db, fake)
    lookup(db, "COKE20", "Walmart", 12, 1150)
    with open_in(*schema_url) as conn:
        result = alternates.backups(conn, fake.client(), NOW)
    sourcing.set_option(db, option("PICKLE", expected_cents=1700), NOW)     # another line's primary
    with open_in(*schema_url) as conn, pytest.raises(alternates.Refused) as refused:
        alternates.write(conn, result, "Test Runner", NOW)
    assert refused.value.refusals[0].code == alternates.CHANGED
    assert ("COKE20", "alternate") not in rows(db)
