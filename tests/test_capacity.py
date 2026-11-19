"""Units per column from the Package Loading chart, matched on the catalog's `size`."""

import pytest

from fractions import Fraction

from vending.capacity import ML_PER_OZ, for_product, from_size, parse_size


@pytest.mark.parametrize("size, units", [
    ("20 oz bottle", 7),
    ("16 oz bottle", 7),        # glass 7 or PET 8; the catalog doesn't say, so the one that always fits
    ("14 oz bottle", 8),
    ("12 oz can", 8),
    ("12 OZ Can", 8),
    ("12 fl oz can", 8),
    ("16 oz can", 8),           # not on the chart: the 12 oz can's width, taller (A20)
    ("16 fl oz can", 8),
    ("250 ml can", 11),
    ("240ml can", 11),
    ("8.4 oz can", 11),         # Red Bull's 250 ml, as the US label puts it
    ("20-oz bottles", 7),
    ("20 oz. bottle", 7),
    ("20 ounce bottle", 7),
    ("20 oz plastic bottle", 7),
    ("12 oz sleek can", 8),
    ("355 ml can", 8),          # the 12 oz can, labelled in metric
    ("591 ml bottle", 7),       # the 20 oz bottle
    ("16 oz glass bottle", 7),  # on the chart as glass
])
def test_sizes_on_the_chart(size, units):
    assert from_size(size) == units


@pytest.mark.parametrize("size", [
    "16.9 oz bottle",           # Pure Leaf
    "7.5 oz can",               # Pepsi mini can
    "11 oz carton",             # Premier Protein
    "20 oz",                    # no container
    "20 oz glass bottle",       # the chart's 20 oz is PET
    "14 oz glass bottle",
    "500 ml bottle",            # 16.9 oz
    "",
    None,
])
def test_sizes_off_the_chart_have_no_capacity(size):
    assert from_size(size) is None


def test_a_count_wins_over_the_chart_and_fills_its_gaps():
    counted = {"COCA-20BOT": 8, "MONSTER-WHITE-16CAN": 6}
    assert for_product("COCA-20BOT", "20 oz bottle", counted) == 8
    assert for_product("MONSTER-WHITE-16CAN", "16 oz can", counted) == 6
    assert for_product("BUBLY-LIME-12CAN", "12 oz can", counted) == 8
    assert for_product("PREMIER-CHOCOLAT-11CAR", "11 oz carton", counted) is None


@pytest.mark.parametrize("size, parsed", [
    ("12 oz can", ("can", Fraction(12), None, False)),
    ("12 fl oz sleek can", ("can", Fraction(12), "sleek", False)),
    ("16 OZ Glass Bottle", ("bottle", Fraction(16), "glass", False)),
    ("20-oz. bottles", ("bottle", Fraction(20), None, False)),
    ("11 oz carton", ("carton", Fraction(11), None, False)),
    ("250 ml can", ("can", 250 / ML_PER_OZ, None, True)),
    ("16.9 oz bottle", ("bottle", Fraction("16.9"), None, False)),
])
def test_parse_size_reads_container_ounces_and_word(size, parsed):
    assert parse_size(size) == parsed


@pytest.mark.parametrize("size", [None, "", "1 L bottle", "large", "12 oz", "can"])
def test_parse_size_gives_none_for_what_it_cannot_read(size):
    assert parse_size(size) is None
