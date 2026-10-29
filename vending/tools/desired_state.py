"""Input types for `preview_desired_state` and `submit_desired_state`.

Field names follow "The desired-state purchase request" in docs/planning/tool-list.md.
They are ours until David publishes his schema; rename them here when he does.
Prices are strings ("2.50") so no float ever carries money.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SlotTarget(BaseModel):
    slot: str
    keep: bool = False
    product: str | None = None
    sale_price: str | None = Field(None, description='Shelf price incl. tax, e.g. "2.50"')
    fill_to: int | None = Field(None, ge=0, description="Units on the rail after the visit; omit to fill to capacity")


class Purchase(BaseModel):
    store: Literal["WALMART", "AMAZON", "BJS", "COSTCO"]
    link: str | None = None
    pack_size: int = Field(gt=0)
    max_pack_price: str


class Sourcing(BaseModel):
    product: str
    primary: Purchase
    alternate: Purchase | None = None
    extra_units_to_closet: int = Field(0, ge=0)


class DesiredState(BaseModel):
    visit: Literal["scheduled", "midweek"] = "scheduled"
    slots: list[SlotTarget]
    sourcing: list[Sourcing] = []
