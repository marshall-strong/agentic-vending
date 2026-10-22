"""The pricing gate: the one function that decides whether a sale price is legal.

`plan_restock`, `preview_desired_state`, `submit_desired_state` and `set_price`
all call `check_price`, so there is exactly one place where the rule lives.
See "The pricing gate" in docs/planning/tool-list.md.

The rule: a sale price must still make money if the worker pays the *max* pack
price, because the worker will not come back for a new price after buying.

    floor = max buy price per unit x 1.08, rounded UP to the next 5 cents

Shelf prices include the 8% sales tax, so a unit sold at price P keeps
P / 1.08. That covers the unit's cost only when P >= cost x 1.08.

Everything here is integer cents (or an exact Fraction of a cent). Floats are
never used: $1.575 must round up to $1.60, and a float can land on either side
of that. Rounding happens once, at the end, and only ever up.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction

PRICE_STEP_CENTS = 5        # course rule: prices end in 0 or 5
DEFAULT_TAX_PERCENT = 8     # config.tax_rate; shelf prices include it


@dataclass(frozen=True)
class Refusal:
    """One reason a plan or price is refused. Codes match the planner's table."""
    code: str
    message: str
    slot: str | None = None


def to_cents(price: str | Decimal | int) -> int:
    """Parse a shelf price like "2.50" into cents, refusing anything inexact.

    Strings and Decimals only: a float such as 2.5 is rejected because it can
    already have lost the cent it was meant to be. An int is taken as cents.
    """
    if isinstance(price, bool) or isinstance(price, float):
        raise ValueError(f"price must be a string, Decimal or int cents, not {price!r}")
    if isinstance(price, int):
        return price
    try:
        dollars = Decimal(price)
    except InvalidOperation:
        raise ValueError(f"not a price: {price!r}") from None
    cents = dollars * 100
    if not dollars.is_finite() or cents != cents.to_integral_value():
        raise ValueError(f"price has fractions of a cent: {price!r}")
    return int(cents)


def round_up_to_step(cents: Fraction, step: int = PRICE_STEP_CENTS) -> int:
    """Round up to the next multiple of `step`. An exact multiple stays put."""
    return step * -(-cents // step)


def floor_cents(unit_cost_cents: Fraction | int,
                tax_percent: int = DEFAULT_TAX_PERCENT) -> int:
    """Lowest legal price for one unit that costs `unit_cost_cents`.

    `unit_cost_cents` may be a fraction of a cent: a 24-pack at $12.00 is
    50 cents a unit, but a 12-pack at $7.50 is 62.5 cents a unit.
    """
    unit_cost = Fraction(unit_cost_cents)
    if unit_cost < 0:
        raise ValueError("unit cost cannot be negative")
    return round_up_to_step(unit_cost * (100 + tax_percent) / 100)


def pack_floor_cents(max_pack_price_cents: int, pack_size: int,
                     tax_percent: int = DEFAULT_TAX_PERCENT) -> int:
    """Floor for one purchase option: max pack price spread over the pack."""
    if pack_size <= 0:
        raise ValueError("pack size must be a positive whole number")
    return floor_cents(Fraction(max_pack_price_cents, pack_size), tax_percent)


def slot_floor_cents(floors: list[int]) -> int:
    """A slot has one sale price, so it must clear every cost it might carry.

    Pass the floor of the primary option, the alternate, and (if the slot keeps
    units already in it) the floor of their actual cost. The worst case wins.
    Rounding up is monotonic, so taking the max of the floors is the same as
    flooring the max cost.
    """
    if not floors:
        raise ValueError("a slot needs at least one cost to have a floor")
    return max(floors)


def check_price(price_cents: int, floor: int, ceiling: int,
                slot: str | None = None) -> list[Refusal]:
    """Every refusal for one price, or an empty list if it is legal.

    Off-step prices are refused, never rounded, so the agent sees its mistake
    and picks a legal price itself. The floor and ceiling refusals name the
    limit so the agent can fix the plan in one turn.
    """
    refusals: list[Refusal] = []
    if price_cents % PRICE_STEP_CENTS != 0:
        refusals.append(Refusal(
            "price_off_step",
            f"{_dollars(price_cents)} is not a multiple of 5 cents; "
            f"pick a price ending in 0 or 5",
            slot))
    if price_cents < floor:
        refusals.append(Refusal(
            "below_floor",
            f"{_dollars(price_cents)} is below the floor of {_dollars(floor)}; "
            f"raise the price or lower the max pack price",
            slot))
    if price_cents > ceiling:
        refusals.append(Refusal(
            "above_ceiling",
            f"{_dollars(price_cents)} is above the ceiling of {_dollars(ceiling)}",
            slot))
    return refusals


def _dollars(cents: int) -> str:
    return f"${cents // 100}.{cents % 100:02d}"
