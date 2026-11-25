"""What a rehearsal season starts from, read from the team database read-only, and loaded into a scratch one.

    python -m vending.simulate.export --plan 8 --out sim-out/export.json     # reads DATABASE_URL, writes nothing there

The export is our own rows only: our stored slot assignment (`slot_assignment`: device and slots, not the
server's raw answer), one first-fill plan (`first_fill_plans`), our sourcing table with its four-store prices
(`sourcing_options`), the price memory (`price_lookups`) and the shared catalog's product fields for the drinks
those name (`catalog_products`). No poll, sale or price of any selection is read, so nothing outside our
assignment can be in it (docs/integrity.md).

**Read-only, twice.** The connection starts with `default_transaction_read_only=on`, and the export opens one
`repeatable read, read only` transaction, checks that Postgres says it is read only, and refuses to go on
otherwise. Any write in it would fail. The URL is never printed.

`load` puts an export into a scratch season (`python -m vending.simulate --from-export FILE`): the slots, the
plan (stored and picked through `vending.plan_store.pick`), the sourcing rows and the price memory as they were,
and the drinks the world needs to sell them (`world.product_for`).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from vending import slots as slots_mod
from psycopg.types.json import Jsonb

VERSION = 1
# The columns read from each table: no raw server answer (`response`, `last_response`).
SLOT_COLUMNS = ("device_id", "slots", "first_read_at", "last_checked_at")
PLAN_COLUMNS = ("id", "loaded_at", "loaded_by", "label", "source_note", "plan", "projection", "slots", "picked")
SOURCING_COLUMNS = ("sku", "rank", "store", "units_per_pack", "expected_cents", "max_cents", "url",
                    "pack_description", "checked_at", "checked_by", "note", "updated_at", "stock")
LOOKUP_COLUMNS = ("id", "at", "sku", "store", "kind", "asked_min", "asked_max", "units_per_pack", "price_cents",
                  "url", "stock", "outcome", "model", "cost_usd", "searches", "no_store", "by", "detail")
CATALOG_COLUMNS = ("sku", "name", "line", "flavor", "size", "delisted_at")
# The team's settings the planner, the fill and the price memory read. Not the switches (the halt switch, the
# schedule's and price check's modes) or the spend caps, which a season sets itself (`live.configure`, `--run-cap`).
CONFIG_KEYS = ("capacity_overrides", "product_aliases", "price_ceiling_cents", "wildcard_slot", "wildcard_ceiling_cents",
               "wildcard_purchase_cap_cents", "tax_percent", "midweek_fee_cents", "spendable_floor_cents",
               "price_search_searches_per_run", "price_search_model", "price_check_model", "price_check_percent",
               "price_check_weekly_cap_cents", "price_compare_every_days", "price_dead_end_days")
NOT_READ_ONLY = "the export's transaction isn't read only, so it stopped before reading anything"


def _plain(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _select(conn: psycopg.Connection, table: str, columns: tuple[str, ...], where: str = "",
            params: tuple = ()) -> list[dict]:
    query = sql.SQL("select {} from {} " + where).format(
        sql.SQL(", ").join(map(sql.Identifier, columns)), sql.Identifier(table))
    return [{c: _plain(v) for c, v in zip(columns, row)} for row in conn.execute(query, params).fetchall()]


def read(conn: psycopg.Connection, plan_id: int) -> dict:
    """The export, in one read-only snapshot. Raises RuntimeError if the transaction isn't read only."""
    with conn.transaction():
        conn.execute("set transaction isolation level repeatable read, read only")
        if conn.execute("show transaction_read_only").fetchone()[0] != "on":
            raise RuntimeError(NOT_READ_ONLY)
        # Our slots as every reader gets them (`vending.slots.our_slots`: a well-formed assignment for our team,
        # confirmed from the lock on); a plan loaded for any other assignment isn't read at all.
        ours = slots_mod.our_slots(conn)
        slots = _select(conn, "slot_assignment", SLOT_COLUMNS, "where id = 1")
        found = conn.execute("select slots from first_fill_plans where id = %s", (plan_id,)).fetchone()
        if found is None:
            raise LookupError(f"no first-fill plan #{plan_id}")
        if set(found[0]) != set(ours):
            raise LookupError(f"plan #{plan_id} was loaded for another slot assignment than ours now; "
                              "export a plan loaded for the current one")
        plans = _select(conn, "first_fill_plans", PLAN_COLUMNS, "where id = %s", (plan_id,))
        slots[0]["slots"] = list(ours)
        options = _select(conn, "sourcing_options", SOURCING_COLUMNS, "order by sku, rank")
        lookups = _select(conn, "price_lookups", LOOKUP_COLUMNS, "order by id")
        skus = sorted({r["sku"] for r in options} | {s.get("sku") for s in plans[0]["plan"].get("day_one") or []
                                                      if isinstance(s, dict) and s.get("sku")})
        catalog = _select(conn, "catalog_products", CATALOG_COLUMNS, "where sku = any(%s) order by sku", (skus,))
        settings = _select(conn, "config", ("key", "value"), "where key = any(%s) order by key", (list(CONFIG_KEYS),))
        at = conn.execute("select pg_catalog.now()").fetchone()[0]
    return {"version": VERSION, "exported_at": at.isoformat(), "assignment": slots[0], "plan": plans[0],
            "sourcing": options, "price_lookups": lookups, "catalog": catalog, "config": settings}


def read_only_connection(url: str) -> psycopg.Connection:
    from vending import db

    return psycopg.connect(url, autocommit=True, connect_timeout=db.CONNECT_TIMEOUT,
                           options=db.session_options(url, "-c default_transaction_read_only=on"))


# --- loading into a scratch season ------------------------------------------------------------------------

def assignment(export: dict) -> tuple[str, ...]:
    """The season's slots: the stored assignment the export carries, checked as `vending.slots` checks one."""
    from vending.slots import problems

    a = export["assignment"]
    wrong = problems(a["device_id"], a["slots"])
    if wrong:
        raise SystemExit("the export's assignment can't be one: " + "; ".join(wrong))
    return tuple(a["slots"])


def catalog(export: dict) -> list[dict]:
    """The simulated bridge's product list: the world's drinks, with each exported drink as the team's catalog has
    it (a delisted one left out, so our catalog copy marks it delisted, as the live planner sees it)."""
    from vending.simulate import world

    out = {p.sku: p.catalog_entry() for p in world.PRODUCTS}
    for c in export["catalog"]:      # the team's catalog wins for its drinks: their fields, and a delisted one is
        if c.get("delisted_at"):     # left out of the answer, which is how `sync_catalog` delists it
            out.pop(c["sku"], None)
            continue
        out[c["sku"]] = {"sku": c["sku"], "name": c["name"], "line": c["line"], "flavor": c["flavor"],
                         "size": c["size"], "active": True}
    return list(out.values())


def products(export: dict) -> tuple[list[dict], list[str]]:
    """World products for the exported drinks the world doesn't have (`world.product_for`), as a season keeps them,
    and a line for each drink left out (no primary price, or a size the loading chart can't size)."""
    from vending.simulate import world

    prices = {p["sku"]: p["price_cents"] for p in export["plan"]["plan"].get("day_one") or [] if isinstance(p, dict)}
    overrides = next((r["value"] for r in export.get("config") or [] if r["key"] == "capacity_overrides"), None) or {}
    primary = {o["sku"]: o for o in export["sourcing"] if o["rank"] == "primary" and o.get("expected_cents")}
    out, left = [], []
    for c in export["catalog"]:
        if c["sku"] in world.BY_SKU:
            continue
        o = primary.get(c["sku"])
        if o is None:
            left.append(f"{c['sku']}: no primary row with a price")
            continue
        p = world.product_for(c, o["units_per_pack"], o["expected_cents"], prices.get(c["sku"]),
                              column=overrides.get(c["sku"]))
        if p is None:
            left.append(f"{c['sku']}: the loading chart can't size {c['size']!r} and no person counted it")
            continue
        out.append(dataclasses.asdict(p))
    return out, left


def unit_prices(export: dict) -> list[tuple[str, str, int, int]]:
    """Each exported row with a price, for the world's shop prices (`world.register_prices`)."""
    return [(o["sku"], o["store"], o["expected_cents"], o["units_per_pack"]) for o in export["sourcing"]
            if o.get("expected_cents") and o.get("units_per_pack")]


def _insert(conn: psycopg.Connection, table: str, rows: list[dict]) -> int:
    for row in rows:
        cols = list(row)
        conn.execute(sql.SQL("insert into {} ({}) " + ("overriding system value " if "id" in cols else "")
                             + "values ({})").format(
            sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, cols)),
            sql.SQL(", ").join(sql.Placeholder() * len(cols))),
            [Jsonb(v) if isinstance(v, dict) else v for v in row.values()])     # a list is a text[] column
    return len(rows)


def load_settings(conn: psycopg.Connection, export: dict, by: str) -> list[str]:
    """The team's settings (`CONFIG_KEYS`), through `vending.config.set_value`, so each is checked as a person's is."""
    from vending import config

    done = []
    for row in export.get("config") or []:
        if row["key"] in CONFIG_KEYS:
            config.set_value(conn, row["key"], row["value"], by)
            done.append(row["key"])
    return done


def load_prices(conn: psycopg.Connection, export: dict) -> dict:
    """The sourcing table and the price memory, as the team database holds them."""
    n = _insert(conn, "sourcing_options", export["sourcing"])
    m = _insert(conn, "price_lookups", export["price_lookups"])
    if m:
        conn.execute("select setval(pg_get_serial_sequence('price_lookups', 'id'), (select max(id) from price_lookups))")
    return {"sourcing_rows": n, "price_lookups": m}


def load(conn: psycopg.Connection, export: dict, by: str) -> dict:
    """Everything but the plan: the team's settings, the sourcing table and the price memory."""
    return {**load_prices(conn, export), "settings": load_settings(conn, export, by)}


def load_plan(conn: psycopg.Connection, export: dict, by: str) -> int:
    """Store the exported plan, keeping its id and label, and pick it (`vending.plan_store.pick`)."""
    from vending import plan_store

    plan = dict(export["plan"], picked=False)
    _insert(conn, "first_fill_plans", [plan])
    conn.execute("select setval(pg_get_serial_sequence('first_fill_plans', 'id'),"
                 " (select max(id) from first_fill_plans))")
    plan_store.pick(conn, plan["id"], by)
    return plan["id"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.simulate.export", description=__doc__.splitlines()[0])
    parser.add_argument("--plan", type=int, required=True, help="the first-fill plan's id")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--database-url", help="default DATABASE_URL (never printed)")
    args = parser.parse_args(argv)
    from vending import settings

    url = args.database_url or settings.require("DATABASE_URL", "set DATABASE_URL to the team database")
    try:
        with read_only_connection(url) as conn:
            data = read(conn, args.plan)
    except (LookupError, RuntimeError, slots_mod.SlotsNotRecorded) as error:
        print(error, file=sys.stderr)
        return 2
    except psycopg.Error as error:
        print(f"{type(error).__name__} (the database couldn't be read)", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=1), encoding="utf-8")
    print(f"wrote {args.out}: plan #{data['plan']['id']} (round {data['plan']['label']}), "
          f"{len(data['assignment']['slots'])} slots, {len(data['sourcing'])} sourcing rows, "
          f"{len(data['price_lookups'])} price lookups, {len(data['catalog'])} catalog drinks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
