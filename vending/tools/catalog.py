"""`get_catalog` (tool #3): the single-item products our team can order.

Reads the copy of the bridge's `/products` list that the catalog sync keeps in
our database; it never calls the bridge. A product the bridge stopped listing
is kept with `delisted_at` set, and is left out here.

Version 1 returns what the bridge gives us (sku, name, line, flavor, size) and
says when the catalog was last synced. Our last unit cost, sourcing rows and
pending catalog requests are not here yet: they wait on the sourcing table and
the receipts join (see "Tool #3" in docs/planning/tool-list.md).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psycopg
from pydantic import BaseModel

from vending.tools import Reply

# The sync runs daily, so a catalog older than a day and a half means a missed run.
STALE_AFTER = timedelta(hours=36)

SYNC_COMMAND = "python -m vending.sync_catalog"


class CatalogProduct(BaseModel):
    sku: str
    name: str
    line: str | None
    flavor: str | None
    size: str | None


class Catalog(Reply):
    synced_at: datetime | None = None
    data: list[CatalogProduct] = []


def get_catalog(conn: psycopg.Connection, search: str | None, now: datetime | None = None) -> Catalog:
    """Listed products, optionally narrowed by `search`. `now` is for tests."""
    now = now or datetime.now(timezone.utc)
    synced_at = conn.execute("select max(synced_at) from catalog_syncs").fetchone()[0]
    if synced_at is None:
        return Catalog(success=False, message=(
            f"The catalog has never been synced, so there is nothing to show. Run: {SYNC_COMMAND}"))

    words = (search or "").lower().split()
    # strpos is a plain substring test: a "%" or "_" typed in a search is just a character.
    where = " and ".join(["strpos(lower(concat_ws(' ', name, line, flavor, size)), %s) > 0"] * len(words))
    rows = conn.execute(
        "select sku, name, line, flavor, size from catalog_products"
        f" where delisted_at is null{' and ' + where if words else ''} order by name, sku", words).fetchall()
    products = [CatalogProduct(sku=sku, name=name, line=line, flavor=flavor, size=size)
                for sku, name, line, flavor, size in rows]

    seen = f"Catalog last synced {synced_at:%Y-%m-%d %H:%M} UTC."
    if now - synced_at > STALE_AFTER:
        seen += f" That is stale, so products may be missing or gone. Run: {SYNC_COMMAND}"
    if products:
        what = f"{len(products)} product{'s' if len(products) != 1 else ''}"
        what += f" matching '{' '.join(words)}'." if words else "."
        return Catalog(success=True, message=f"{what} {seen}", count=len(products),
                       synced_at=synced_at, data=products)
    if not words:
        return Catalog(success=True, message=f"The catalog has no listed products. {seen}", synced_at=synced_at)
    return Catalog(success=True, message=(
        f"No listed product matches '{' '.join(words)}'. Search again with fewer words, or omit search to list "
        f"everything. If the product really isn't here, ask for it with request_catalog_product. {seen}"),
        synced_at=synced_at)
