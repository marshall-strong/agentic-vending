"""The Products page (#55): every product we can order, how we'd buy it, and what is in back stock.

    /dashboard/products          on the Container App, behind the team password (`vending.pages`)
    /dashboard/demo/products     the same page from made-up data (`vending/demo/products.json`); no database
    /products                    beside the page `python -m vending.dashboard` serves on 127.0.0.1

David asked on 11 Nov for one page that shows "all the different products that we can order", plus
back stock, so nobody has to query the tables. One row per product the catalog lists
(`catalog_products`, kept by `vending.sync_catalog`), with:

- **Sourcing**: our rows in `sourcing_options` (`vending.sourcing`): store, pack, the pack's price when
  someone looked, the unit cost that makes, and the day it was checked. A price checked more than
  `sourcing.STALE_AFTER` (7 days) ago is marked STALE.
- **Day-one plan**: whether the picked first-fill plan (`vending.plan_store`) puts it in a slot, and how
  many units. With no plan picked, the latest stored plan, labelled "latest, not picked", as the
  dashboard's The plan panel does (it is the same read, `dashboard._first_fill`).
- **Back stock**: units in the closet and what each cost us, the dashboard's own Back stock figures
  (`vending.stock`, from receipts). Like the dashboard, it counts from the ledger's opening: before it,
  receipts are test purchases.

Above the table: a warning when the catalog's last sync is more than 36 hours old or never happened
(the rule `get_catalog` uses), and every product the plan picks that has no sourcing row, since the
planner can't price or buy it (on 20 Nov, 8 of round 6's drinks had no checked price). Below it, a
plan or closet product the catalog doesn't list, and our catalog requests, drawn by the dashboard's
own Requests code.

Read-only like the dashboard: one `repeatable read, read only` transaction, on whichever read-only
connection the server hands over. Money stays exact (integer cents and `Fraction`s, written as text),
every value is escaped, and the page runs no script.
"""

from __future__ import annotations

import json
from datetime import datetime
from fractions import Fraction
from pathlib import Path

import psycopg

from vending import dashboard, sourcing
from vending import stock as stock_mod
from vending.tools.catalog import STALE_AFTER as CATALOG_STALE_AFTER

ROUTE = "/dashboard/products"
DEMO_ROUTE = "/dashboard/demo/products"
LOCAL_ROUTE = "/products"
TITLE = "Products"
# The demo's data: made by `python -m vending.demo.make`, beside the dashboard's demo snapshot.
DEMO_FILE = Path(__file__).with_name("demo") / "products.json"
SYNC_COMMAND = "python -m vending.sync_catalog"


# --- Data ---------------------------------------------------------------------------------------

def _catalog(conn: psycopg.Connection) -> dict:
    synced = conn.execute("select max(synced_at) from catalog_syncs").fetchone()[0]
    rows = dashboard._rows(conn, "select sku, name, line, flavor, size from catalog_products"
                                 " where delisted_at is null order by lower(name), sku")
    return {"synced_at": synced, "products": rows}


def _sourcing(conn: psycopg.Connection) -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {}
    for o in sourcing.options(conn):
        found.setdefault(o.sku, []).append(
            {"rank": o.rank, "store": o.store, "units_per_pack": o.units_per_pack, "expected_cents": o.expected_cents,
             "max_cents": o.max_cents, "pack_description": o.pack_description, "checked_at": o.checked_at,
             "checked_by": o.checked_by})
    return found


def _closet(conn: psycopg.Connection, now: datetime, ours: dict) -> list[dict] | None:
    """The dashboard's Back stock, read the dashboard's way; None before the ledger's opening."""
    opened = stock_mod.opening_at(conn)
    if opened is None or opened > now:
        return None
    return (dashboard._stock(conn, now) if ours["ok"] else dashboard._closet_only(conn))["closet"]


def read(conn: psycopg.Connection, now: datetime | None = None, public: bool = False) -> dict:
    """Everything the page shows, from one repeatable-read, read-only transaction. A missing table
    blanks only its own part. `public` hides read errors as the public dashboard does."""
    with conn.transaction():
        conn.execute("set transaction isolation level repeatable read, read only")
        now = now or conn.execute("select transaction_timestamp()").fetchone()[0]
        ours = dashboard._our_slots(conn)
        slots = tuple(ours["data"]) if ours["ok"] else ()
        assigned = (dashboard._optional(conn, "select first_read_at from slot_assignment where id = 1")
                    if slots else None)
        catalog_requests = dashboard._panel(lambda: dashboard._rows(conn, dashboard.CATALOG_REQUESTS_SQL))
        data = {
            "as_of": now,
            "catalog": dashboard._panel(lambda: dashboard._guarded(conn, lambda: _catalog(conn))),
            "sourcing": dashboard._panel(lambda: dashboard._guarded(conn, lambda: _sourcing(conn))),
            "plan": dashboard._panel(lambda: dashboard._isolated(conn, lambda: dashboard._first_fill(
                conn, slots, assigned["first_read_at"] if assigned else None))),
            "closet": dashboard._panel(lambda: dashboard._guarded(conn, lambda: _closet(conn, now, ours))),
            "catalog_requests": catalog_requests,
            "catalog_counts": dashboard._catalog_counts(catalog_requests),
        }
    if public:
        dashboard.hide_panel_errors(data, "products")
    return data


# --- What the data says ---------------------------------------------------------------------------

def catalog_alert(data: dict) -> dict | None:
    """The catalog's age as a warning, or None when it is fresh. The 36-hour rule is `get_catalog`'s."""
    catalog = data["catalog"]
    if not catalog["ok"]:
        return None
    synced = catalog["data"]["synced_at"]
    if synced is None:
        return {"level": "bad", "text": f"The catalog has never been synced, so this list is empty. Run `{SYNC_COMMAND}`."}
    if data["as_of"] - synced > CATALOG_STALE_AFTER:
        return {"level": "bad", "text": (f"The catalog was last synced {dashboard._span(data['as_of'] - synced)} ago, "
                                         "over 36 hours: products may be missing or gone. The worker syncs it every "
                                         f"hour, so its sync may be failing. Run `{SYNC_COMMAND}`.")}
    return None


def planned(data: dict) -> dict[str, dict]:
    """The plan's products by SKU (a planned line with no SKU is keyed by its product name, in brackets):
    the slots it fills and the units it loads."""
    plan = data["plan"]
    if not plan["ok"] or not plan["data"]:
        return {}
    found: dict[str, dict] = {}
    for row in plan["data"].get("slots") or []:
        key = row["sku"] or f"[{row['product']}]"
        entry = found.setdefault(key, {"sku": row["sku"], "product": row["product"], "slots": [], "units": 0})
        entry["slots"].append(row["slot"])
        entry["units"] += row["units"] or 0
    return found


def _names(data: dict) -> dict[str, str]:
    """The catalog's name for each listed SKU."""
    return {p["sku"]: p["name"] for p in data["catalog"]["data"]["products"]} if data["catalog"]["ok"] else {}


def _label(p: dict, names: dict[str, str]) -> str:
    """A planned product as words: the catalog's name and its SKU, or the plan's name and "no SKU yet"."""
    if p["sku"] is None:
        return f"{p['product']} (no SKU yet)"
    name = names.get(p["sku"]) or p["product"]
    return p["sku"] if name == p["sku"] else f"{name} ({p['sku']})"


def _sourcing_rows(data: dict) -> dict[str, list[dict]]:
    return data["sourcing"]["data"] if data["sourcing"]["ok"] else {}


def unsourced(data: dict) -> list[dict]:
    """Planned products with no sourcing row, in plan order. Empty when the sourcing table can't be read,
    since then nobody knows."""
    if not data["sourcing"]["ok"]:
        return []
    rows = _sourcing_rows(data)
    return [p for p in planned(data).values() if p["sku"] is None or p["sku"] not in rows]


def is_stale(option: dict, now: datetime) -> bool:
    return now - option["checked_at"] > sourcing.STALE_AFTER


def unit_cost(option: dict) -> str | None:
    """The pack's price over its units, in dollars, rounded half up to the cent; exact until then."""
    if option["expected_cents"] is None:
        return None
    return dashboard._dollars(Fraction(option["expected_cents"], option["units_per_pack"]))


def alerts(data: dict) -> list[dict]:
    """What a person should look at first, worst first."""
    found = []
    if (catalog := catalog_alert(data)) is not None:
        found.append(catalog)
    gaps = unsourced(data)
    if gaps:
        names = ", ".join(_label(p, _names(data)) for p in gaps)
        found.append({"level": "bad", "text": (
            f"{dashboard._plural(len(gaps), 'product')} in the day-one plan {'has' if len(gaps) == 1 else 'have'} "
            f"no sourcing row, so the planner has no store, pack or price to buy {'it' if len(gaps) == 1 else 'them'} "
            f"with: {names}. Look each up and record it with `python -m vending.sourcing set`.")})
    rows, now = _sourcing_rows(data), data["as_of"]
    old = [p for p in planned(data).values()
           if p["sku"] in rows and any(is_stale(o, now) for o in rows[p["sku"]])]
    if old:
        found.append({"level": "warn", "text": (
            f"{dashboard._plural(len(old), 'product')} in the day-one plan {'has' if len(old) == 1 else 'have'} a price "
            f"checked more than 7 days ago: {', '.join(_label(p, _names(data)) for p in old)}. Check {'it' if len(old) == 1 else 'them'} again.")})
    return found


# --- The page -----------------------------------------------------------------------------------

_e = dashboard._e
_pill = dashboard._pill


def _money_cents(cents: int | None) -> str:
    return "price unknown" if cents is None else sourcing._money(cents)


def _day(value: datetime) -> str:
    local = dashboard._eastern(value)
    return f"{local:%a} {local.day} {local:%b}"


def _sourcing_cell(options: list[dict] | None, now: datetime, panel_ok: bool) -> str:
    if not panel_ok:
        return '<span class="dim">unknown</span>'
    if not options:
        return '<span class="dim">none</span>'
    out = []
    for o in options:
        stale = " " + _pill("STALE", "warn") if is_stale(o, now) else ""
        unit = unit_cost(o)
        rank = "" if o["rank"] == "primary" else '<span class="dim">alternate: </span>'
        out.append(
            f'<div class="src">{rank}<b>{_e(o["store"])}</b> &middot; {o["units_per_pack"]} a pack at '
            f'{_e(_money_cents(o["expected_cents"]))}'
            + (f' &middot; <b>${_e(unit)}</b> a unit' if unit is not None else "")
            + f'<div class="dim">max {_e(sourcing._money(o["max_cents"]))} &middot; checked '
              f'{_e(_day(o["checked_at"]))} by {_e(o["checked_by"])}{stale}</div></div>')
    return "".join(out)


def _plan_cell(entry: dict | None, plan_ok: bool) -> str:
    if not plan_ok:
        return '<span class="dim">unknown</span>'
    if entry is None:
        return '<span class="dim">&mdash;</span>'
    slots = ", ".join(entry["slots"])
    return (f'{_pill("in plan", "info")} <span class="dim">{dashboard._plural(entry["units"], "unit")} &middot; '
            f'slot{"s" if len(entry["slots"]) > 1 else ""} {_e(slots)}</span>')


def _closet_cell(item: dict | None, closet: dict) -> str:
    if not closet["ok"]:
        return '<span class="dim">unknown</span>'
    if item is None:
        return '<span class="dim">&mdash;</span>'
    cost = "cost unknown" if item["unit_cost"] is None else f'${_e(item["unit_cost"])} each'
    return f'<b>{item["units"]}</b> <span class="dim">&times; {cost}</span>'


def _row(sku: str | None, name: str, size: str | None, data: dict, plan: dict, closet: dict[str, dict]) -> str:
    rows = _sourcing_rows(data)
    entry = plan.get(sku) if sku else None
    gap = entry is not None and data["sourcing"]["ok"] and sku not in rows
    flag = " " + _pill("no sourcing", "bad") if gap else ""
    return (f'<tr{' class="gap"' if gap else ""}><td><b>{_e(name)}</b>{flag}'
            + (f'<div><code>{_e(sku)}</code></div>' if sku else '<div class="dim">no SKU yet</div>')
            + f'</td><td>{_e(size) if size else "&mdash;"}</td>'
            f'<td>{_sourcing_cell(rows.get(sku) if sku else None, data["as_of"], data["sourcing"]["ok"])}</td>'
            f'<td>{_plan_cell(entry, data["plan"]["ok"])}</td>'
            f'<td>{_closet_cell(closet.get(sku) if sku else None, data["closet"])}</td></tr>')


_HEAD = ('<thead><tr><th>Product</th><th>Size</th><th>Our sourcing</th><th>Day-one plan</th>'
         '<th>Back stock</th></tr></thead>')


def _plan_lede(data: dict) -> str:
    plan = data["plan"]
    if not plan["ok"]:
        return f'<p class="lede warn-text">The day-one plan can&rsquo;t be read: {_e(plan["why"])}</p>'
    p = plan["data"]
    if p is None:
        return '<p class="lede dim">No day-one plan is stored yet, so no product is marked as in it.</p>'
    which = "the picked day-one plan" if p["picked"] else "the latest day-one plan (latest, not picked)"
    if p.get("message"):
        return f'<p class="lede warn-text">Plan #{int(p["id"])}: {_e(p["message"])}</p>'
    return f'<p class="lede">&ldquo;Day-one plan&rdquo; is {which}, #{int(p["id"])}.</p>'


def _closet_lede(data: dict) -> str:
    closet = data["closet"]
    if not closet["ok"]:
        return f'<p class="lede warn-text">Back stock can&rsquo;t be read: {_e(closet["why"])}</p>'
    if closet["data"] is None:
        return ('<p class="lede dim">Back stock counts from the ledger&rsquo;s opening, as on the dashboard; '
                'receipts before it are test purchases. No opening yet, so no back stock is shown.</p>')
    total = sum(c["units"] for c in closet["data"])
    return (f'<p class="lede">Back stock: {dashboard._plural(total, "unit")} of '
            f'{dashboard._plural(len(closet["data"]), "product")} in the closet, by the receipts, at what they cost us.</p>')


def html(data: dict, *, nav: tuple[tuple[str, str], ...] = (), demo: bool = False) -> str:
    """The page from `read`'s data alone, so made-up data draws the same way."""
    now = data["as_of"]
    catalog, plan_rows = data["catalog"], planned(data)
    closet = {c["product"]: c for c in (data["closet"]["data"] or [])} if data["closet"]["ok"] else {}
    links = " &middot; ".join(f'<a href="{_e(href)}">{_e(label)}</a>' for label, href in nav)
    synced = (catalog["data"]["synced_at"] if catalog["ok"] else None)
    head = (f'<header class="top"><h1>Products</h1><span class="meta">Read-only &middot; as of '
            f'{dashboard._time(now, "etday")} &middot; catalog synced '
            + (dashboard._time(synced, "etday") if synced else "never")
            + (f" &middot; {links}" if links else "") + "</span></header>")
    items = alerts(data)
    body = head + (dashboard._attention_html(items) if items else "")
    if not catalog["ok"]:
        table = f'<p class="empty">The catalog can&rsquo;t be read: {_e(catalog["why"])}</p>'
        listed = set()
    else:
        products = catalog["data"]["products"]
        listed = {p["sku"] for p in products}
        rows = [_row(p["sku"], p["name"], p["size"], data, plan_rows, closet) for p in products]
        sourced = sum(1 for p in products if p["sku"] in _sourcing_rows(data))
        lede = (f'<p class="lede">{dashboard._plural(len(products), "product")} the catalog lists'
                + (f", {sourced} with a sourcing row" if data["sourcing"]["ok"] else "")
                + f'. A sourcing price checked more than 7 days ago is marked {_pill("STALE", "warn")}; '
                  f'a product in the day-one plan with no sourcing row is marked {_pill("no sourcing", "bad")}. '
                  'Unit cost is the pack price over its units, to the cent.</p>')
        if not data["sourcing"]["ok"]:
            lede += f'<p class="lede warn-text">Sourcing can&rsquo;t be read: {_e(data["sourcing"]["why"])}</p>'
        table = lede + (f'<div class="scroll"><table class="products">{_HEAD}<tbody>{"".join(rows)}</tbody></table></div>'
                        if rows else '<p class="empty">The catalog lists no products.</p>')
    # Planned or closet products the catalog doesn't list: delisted, or a plan line still waiting for its SKU.
    extra_keys = [k for k, p in plan_rows.items() if p["sku"] is None or (catalog["ok"] and p["sku"] not in listed)]
    extra_keys += [sku for sku in closet if catalog["ok"] and sku not in listed and sku not in plan_rows]
    extra = ""
    if extra_keys:
        rows = [_row(plan_rows[k]["sku"] if k in plan_rows else k,
                     plan_rows[k]["product"] if k in plan_rows else k, None, data, plan_rows, closet)
                for k in extra_keys]
        extra = ('<section id="unlisted"><h2>Not in the catalog <span class="hint">in the plan or the closet, '
                 'but not listed now: delisted, or waiting for an approval and its SKU</span></h2>'
                 f'<div class="scroll"><table class="products">{_HEAD}<tbody>{"".join(rows)}</tbody></table></div></section>')
    body += (f'<section id="products"><h2>What we can order <span class="hint">the catalog, our sourcing, the plan '
             f'and the closet</span></h2>{_plan_lede(data)}{_closet_lede(data)}{table}</section>' + extra
             + '<section id="catalog-requests"><h2>Catalog requests <span class="hint">products we asked the '
               f'instructors to add</span></h2>{dashboard.catalog_requests_html(data["catalog_requests"], now)}</section>'
             + ('<footer>Made-up data, shown with the live page&rsquo;s own code. Nothing here is read from our '
                f'database. The live page is <a href="{ROUTE}">{ROUTE}</a>.</footer>' if demo else
                '<footer>This page only reads. Sourcing is changed with <code>python -m vending.sourcing</code>; '
                'the catalog is synced from the bridge every hour.</footer>'))
    banner = (f'<div class="demo-banner" role="note">DEMO: made-up data, not our products. The live page is '
              f'<a href="{ROUTE}">{ROUTE}</a></div>' if demo else "")
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            + ('<meta name="robots" content="noindex">' if demo else "")
            + f'<title>{dashboard.DEMO_TITLE if demo else ""}{TITLE}</title>'
            f'<style>{dashboard.STYLE}{STYLE}</style></head><body>{banner}<main>{body}</main></body></html>')


STYLE = """
.scroll { overflow-x:auto; -webkit-overflow-scrolling:touch }
table.products { width:100%; border-collapse:collapse; font-size:14px }
table.products th { text-align:left; font-weight:600; color:var(--muted); border-bottom:1px solid var(--line);
  padding:6px 8px; white-space:nowrap }
table.products td { vertical-align:top; padding:8px; border-top:1px solid var(--line) }
table.products td:first-child { min-width:14rem }
table.products td:nth-child(3) { min-width:18rem }
table.products tr.gap td { background:var(--bad-soft) }
table.products .src + .src { margin-top:6px }
"""


# --- Demo -----------------------------------------------------------------------------------------

def demo_data(path: Path = DEMO_FILE) -> dict:
    """The made-up data, decoded as `read` made it. Refuses a file not marked `"demo": true`."""
    from vending.demo import DemoError, decode

    try:
        data = decode(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as error:
        raise DemoError(f"the demo products file can't be read: {type(error).__name__}") from error
    if not isinstance(data, dict) or data.pop("demo", None) is not True:
        raise DemoError("the demo products file isn't marked demo: true")
    return data


def demo_page() -> str:
    from vending.demo import DEMO

    return html(demo_data(), nav=(("Demo dashboard", DEMO), ("Live page", ROUTE)), demo=True)


def render_file(data: dict) -> str:
    """The demo file's text, as `vending.demo.make` writes it."""
    from vending.demo import encode

    return json.dumps(encode({"demo": True, **data}), indent=1, ensure_ascii=False) + "\n"
