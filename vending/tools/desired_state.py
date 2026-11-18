"""Input types for `preview_desired_state` and `submit_desired_state`.

Field names and limits follow the live purchase-request API (docs/dex-bridge.md)
and match `vending.planner`'s dataclasses field for field (PR #13), so a
validated model converts straight into the planner's input. Prices are strings
("2.50") so no float ever carries money; the planner parses them exactly.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class DesiredSlot(BaseModel):
    slot: str
    keep: bool = False
    product: str | None = None
    sale_price: str | None = Field(None, description='Shelf price incl. tax, e.g. "2.50"')
    fill_to: int | None = Field(None, ge=0, description="Units on the rail after the visit; omit to fill the column. "
                                "The loading chart gives 7 to 11 depending on the package (20 oz bottle 7, 12 oz can 8); "
                                "a product a person counted may differ")


class PurchaseOption(BaseModel):
    store: Literal["Walmart", "Costco", "Amazon", "BJs"]
    units_per_pack: int = Field(gt=0, le=60)
    max_price_per_pack: str
    url: str | None = Field(None, description="Required for Amazon")
    expected_price_per_pack: str | None = None
    pack_description: str | None = Field(None, max_length=100)


class Sourcing(BaseModel):
    product: str
    primary: PurchaseOption
    alternate: PurchaseOption | None = None
    extra_units_to_closet: int = Field(0, ge=0)


class DesiredState(BaseModel):
    slots: list[DesiredSlot]
    off_cycle: bool = Field(False, description="True asks for a midweek visit, which costs $25")
    sourcing: list[Sourcing] = Field([], description="How to buy a product; leave a product out to use the "
                                     "team's sourcing table, which the preview names in warnings when it is old")
    wildcard: str | None = Field(None, description="The one slot that tests a product for the others, and may sell "
                                 "above the ceiling, up to the wildcard ceiling ($8.00 by default), for a price test; it "
                                 "moves only at a Friday visit, and one purchase for it costs at most $82.00. Name it in "
                                 "every plan: a midweek plan names the one in effect, a slot above the ceiling is named "
                                 "until a later poll reads it at or under, and a Friday plan that names none ends the "
                                 "trial when its visit is done (get_machine_state, wildcard_trial)")
