"""Copy the products we can order from the bridge into `catalog_products`.

    python -m vending.sync_catalog

The worker (`vending.worker`) runs it every hour with the other syncs. Once a
day would do, since the catalog changes rarely, but this is one small read that
never touches the machine, so it isn't worth a schedule of its own. `get_catalog` reads the copy, so the agent never
waits on the bridge and a bridge outage doesn't hide the catalog.

Each run lists every product once. New products are added; listed ones get
their fields and `last_seen_at` updated, and are relisted if they had been
delisted. A stored product missing from the answer is marked delisted, not
deleted, so old requests and receipts still name it. The whole run is one
transaction and records one `catalog_syncs` row.

A product without a usable `sku` or `name` is logged and skipped. An empty
answer while we still hold listed products is treated as a bad answer, not as
the catalog being emptied: nothing changes and the run fails, because one
glitch would otherwise delist everything the agent can order. An unreachable
bridge or a rejected key stops the run, as in `sync_history`.

Skipped products are logged but don't fail the run: the worker runs this on a
schedule, and one malformed row from the bridge shouldn't mark every round as
failed. The run fails only when the bridge listed products and none was usable.

It never migrates. On a database that is behind it refuses to run (exit 2) and
names `python -m vending.db migrate`, like the other syncs, so a run started
from a branch can't change the shared database's schema.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Jsonb

from vending import db
from vending.bridge import Bridge, BridgeError

FIELDS = ("sku", "name", "line", "flavor", "size")


class EmptyCatalog(Exception):
    """The bridge listed no products while we still hold listed ones."""


@dataclass
class Report:
    listed: int = 0
    added: int = 0
    updated: int = 0
    relisted: int = 0
    delisted: int = 0
    invalid: int = 0


def usable(products: list, log) -> dict[str, dict]:
    """The products worth storing, by SKU. Logs and drops the rest."""
    found: dict[str, dict] = {}
    for product in products:
        if not isinstance(product, dict) or not all(
                isinstance(product.get(key), str) and product[key].strip() for key in ("sku", "name")):
            log(f"skipped a product without a usable sku and name: {product!r}")
            continue
        sku = product["sku"]
        if sku in found:
            log(f"{sku}: listed twice; kept the first")
            continue
        found[sku] = product
    return found


def sync(conn: psycopg.Connection, bridge: Bridge, now: datetime | None = None, log=print) -> Report:
    now = now or datetime.now(timezone.utc)
    answer = bridge.products()
    report = Report()
    products = usable(answer, log)
    report.invalid = len(answer) - len(products)
    with conn.transaction():
        stored = {row[0]: row[1:] for row in conn.execute(
            f"select {', '.join(FIELDS)}, delisted_at from catalog_products")}
        if not products and any(row[-1] is None for row in stored.values()):
            raise EmptyCatalog("the bridge listed no products; kept the catalog as it was")
        for sku, product in products.items():
            fields = tuple(product.get(key) for key in FIELDS)
            before = stored.get(sku)
            if before is None:
                report.added += 1
            elif before[-1] is not None:
                report.relisted += 1
            elif before[:-1] != fields[1:]:
                report.updated += 1
            conn.execute(
                """
                insert into catalog_products (sku, name, line, flavor, size, first_seen_at, last_seen_at, last_response)
                values (%s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (sku) do update
                   set name = excluded.name, line = excluded.line, flavor = excluded.flavor,
                       size = excluded.size, last_seen_at = excluded.last_seen_at,
                       last_response = excluded.last_response, delisted_at = null
                """,
                (*fields, now, now, Jsonb(product)),
            )
        gone = [sku for sku, row in stored.items() if row[-1] is None and sku not in products]
        if gone:
            conn.execute("update catalog_products set delisted_at = %s where sku = any(%s)", (now, gone))
            log(f"delisted: {', '.join(sorted(gone))}")
        report.delisted = len(gone)
        report.listed = len(products)
        conn.execute(
            "insert into catalog_syncs (synced_at, product_count, added, delisted) values (%s, %s, %s, %s)",
            (now, report.listed, report.added, report.delisted),
        )
    return report


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args(argv)
    try:
        with db.connect() as conn:
            # Never migrates: in production DATABASE_URL is the shared Azure
            # database, and a schema change there should be a person's decision.
            behind = db.pending(conn)
            if behind:
                print(f"The database is missing migrations: {', '.join(behind)}. "
                      "Check DATABASE_URL points where you mean, then run: python -m vending.db migrate",
                      file=sys.stderr)
                return 2
            report = sync(conn, Bridge())
    except BridgeError as error:
        print(f"bridge error: {error}", file=sys.stderr)
        return 3
    except EmptyCatalog as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1
    print(f"done: {report.listed} listed, {report.added} new, {report.updated} changed, "
          f"{report.relisted} relisted, {report.delisted} delisted, {report.invalid} skipped as unusable")
    # Skipped rows are already logged; fail only when nothing the bridge listed was usable.
    return 1 if report.invalid and not report.listed else 0


if __name__ == "__main__":
    sys.exit(main())
