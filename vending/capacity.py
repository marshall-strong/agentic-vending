"""How many units of a product fit in one column, from the machine's Package Loading chart.

A column holds packages in a row in front of a pusher, so how many fit depends
on the package, not the slot: David said on 21 Sep there is no fixed capacity,
"as many as physically fit". The chart on the inside of the door (part
931,650,021, photographed whole on 9 Nov 2026) gives the manufacturer's
numbers, which it calls approximate:

    20oz PET 7, 16oz glass 7, 16oz PET 8, 12oz can 8, 240/250ml can 11, 14oz PET 8

The catalog's `size` ("20 oz bottle", "12 oz can") is matched against that.
The catalog says only "bottle", never glass or PET, so a 16 oz bottle gets the
glass figure, 7, which fits either way. Anything not on the chart (a 16 oz can,
a 16.9 oz bottle, a carton) has no capacity until a person counts it and
records it in `config.capacity_overrides`, which also wins over the chart:

    python -m vending.config set capacity_overrides '{"MONSTER-WHITE-16CAN": 8}' --by NAME
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from fractions import Fraction

ML_PER_OZ = Fraction("29.5735")

# (container, smallest oz, largest oz, units per column). 240 ml is 8.12 oz and 250 ml is 8.45 oz.
CHART: tuple[tuple[str, Fraction, Fraction, int], ...] = (
    ("bottle", Fraction(20), Fraction(20), 7),
    ("bottle", Fraction(16), Fraction(16), 7),
    ("bottle", Fraction(14), Fraction(14), 8),
    ("can", Fraction(12), Fraction(12), 8),
    ("can", 240 / ML_PER_OZ, 250 / ML_PER_OZ, 11),
)

OVERRIDE_HINT = ("count how many fit in one column, then add it to the others in config with: "
                 "python -m vending.config set capacity_overrides '{\"SKU\": 8}' --by NAME")

# "20 oz bottle", "20-oz. bottles", "12 fl oz sleek can", "591 ml plastic bottle": an amount, a unit,
# at most one word about the package, then the container.
_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*-?\s*(fl\.?\s*oz\.?|oz\.?|ounces?|ml)\s+(?:(\w+)\s+)?(bottle|can|carton)s?\b",
                   re.IGNORECASE)
# A metric label is the same package rounded: 355 ml is the 12 oz can, 591 ml the 20 oz bottle.
ML_SLACK = Fraction(1, 100)


def from_size(size: str | None) -> int | None:
    """Units per column for a catalog `size`, or None when the chart doesn't cover it."""
    match = _SIZE.match(size or "")
    if not match:
        return None
    amount, unit, word, container = match.groups()
    metric = unit.lower() == "ml"
    oz = Fraction(amount) / ML_PER_OZ if metric else Fraction(amount)
    slack = ML_SLACK if metric else 0
    # The chart's 20 oz and 14 oz bottles are PET; a glass one may not fit as many.
    if word and word.lower() == "glass" and container.lower() == "bottle" and oz != 16:
        return None
    for kind, low, high, units in CHART:
        if container.lower() == kind and low * (1 - slack) <= oz <= high * (1 + slack):
            return units
    return None


def for_product(product: str, size: str | None, overrides: Mapping[str, int]) -> int | None:
    """A person's count for `product` if there is one, else the chart's figure for its size."""
    return overrides.get(product, from_size(size))
