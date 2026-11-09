"""Units per column from the Package Loading chart, matched on the catalog's `size`."""

import pytest

from vending.capacity import for_product, from_size


@pytest.mark.parametrize("size, units", [
    ("20 oz bottle", 7),
    ("16 oz bottle", 7),        # glass 7 or PET 8; the catalog doesn't say, so the one that always fits
    ("14 oz bottle", 8),
    ("12 oz can", 8),
    ("12 OZ Can", 8),
    ("12 fl oz can", 8),
    ("250 ml can", 11),
    ("240ml can", 11),
    ("8.4 oz can", 11),         # Red Bull's 250 ml, as the US label puts it
])
def test_sizes_on_the_chart(size, units):
    assert from_size(size) == units


@pytest.mark.parametrize("size", [
    "16 oz can",                # Monster: taller and wider than anything on the chart
    "16.9 oz bottle",           # Pure Leaf
    "7.5 oz can",               # Pepsi mini can
    "11 oz carton",             # Premier Protein
    "20 oz",                    # no container
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
