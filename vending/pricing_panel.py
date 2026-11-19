"""The dashboard's pricing panel: for each of our selections, what it charges, what a unit cost us,
the lowest legal price, what a sale keeps, how fast it sells, what the plan does to the price next,
and how the price has moved over the last few weeks.

Where each figure comes from:

- **Now**: the shelf price at the latest poll, as `get_machine_state` reports it (the snapshot's
  `sales` panel).
- **Unit cost**: what the units in the slot cost us, from the receipts (`vending.stock`, the
  snapshot's `stock` panel, exact). With no receipt cost, the expected cost of the primary sourcing
  option (`sourcing_options`, `vending.sourcing`: the pack price someone saw over the pack size),
  labelled *expected*. The product is the receipts' one, else the latest plan's.
- **Floor**: `vending.pricing`'s rule, as the planner applies it to a slot it restocks: the floor of
  the receipt cost (`floor_cents`) and of each sourcing option at its most-we-pay pack price
  (`pack_floor_cents`); the highest wins (`slot_floor_cents`). **Legal** is `check_price` against
  that floor and the ceiling (`config.price_ceiling_cents`, default $6.50). With no cost known, the
  floor is unknown: a price is then only checked for its 5-cent step and the ceiling.
- **Margin**: per unit, what a sale keeps after the sales tax inside the shelf price
  (`config.tax_percent`, default 8%), less the unit cost.
- **Sold**: units in the last 7 days and per class day, from `get_machine_state` (the per class day
  rate is the slot's own once it has 5 in-stock class days, else the fall prior for its price).
- **Next**: the latest plan's price for the slot when it differs from the shelf, with the agent's
  reason, checked the same way against the floor of the product the plan puts in.
- **Sparkline**: the shelf price at every good poll over the last `HISTORY_DAYS` days
  (`machine_state.price_history`). A change shows at the poll that first saw it.

Only our selections (the stored assignment the snapshot read) appear, and every read is filtered to
them or to the products they hold. Nothing here reads another team's or the Interloper's prices
(`docs/integrity.md`). Reads only, inside the dashboard's read-only snapshot. Money is integer cents
or exact Fractions, shown as dollars rounded half up to the cent; never a float.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from html import escape
from typing import Any

import psycopg

from vending import config
from vending.pricing import (DEFAULT_TAX_PERCENT, check_price, floor_cents, pack_floor_cents,
                             slot_floor_cents, to_cents)
from vending.tools import machine_state

# How far back the sparkline reaches.
HISTORY_DAYS = 28
# The planner's default ceiling when `config.price_ceiling_cents` is unset (`vending.planning.planner_config`).
DEFAULT_CEILING_CENTS = 650
RANKS = ("primary", "alternate")
# The sparkline's drawing box, in px.
SPARK_W, SPARK_H, SPARK_PAD = 112, 26, 3


# --- Data ---------------------------------------------------------------------------------------

def products(snap: dict, slots: tuple[str, ...]) -> list[str]:
    """The products our selections hold (receipts) or the latest plan puts in them: the sourcing rows to read."""
    held = _data(snap, "stock", {}).get("slots", {})
    planned = _planned(snap)
    found = {held[s]["product"] for s in slots if (held.get(s) or {}).get("product")}
    found |= {e["product"] for s in slots if isinstance(e := planned.get(s), dict) and isinstance(e.get("product"), str)}
    return sorted(found)


def read(conn: psycopg.Connection, now: datetime, slots: tuple[str, ...], skus: list[str]) -> dict:
    """The panel's own reads: the ceiling and tax rate, the sourcing rows for `skus`, and the shelf price
    of `slots` at every good poll since `HISTORY_DAYS` ago."""
    # Here, not at the top: `vending.sourcing` needs time-zone data, and the page starts without it.
    from vending import sourcing

    wanted = set(skus)
    options: dict[str, dict[str, Any]] = {}
    for o in sourcing.options(conn):
        if o.sku in wanted:
            options.setdefault(o.sku, {})[o.rank] = o
    since = now - timedelta(days=HISTORY_DAYS)
    return {"ceiling_cents": _setting(conn, "price_ceiling_cents", DEFAULT_CEILING_CENTS),
            "tax_percent": _setting(conn, "tax_percent", DEFAULT_TAX_PERCENT),
            "options": options, "since": since,
            "history": machine_state.price_history(conn, slots, now, since) if slots else []}


def _setting(conn: psycopg.Connection, key: str, default: int) -> int:
    """A whole-number setting as the planner reads it: its default unless the stored value is an int."""
    value = config.get_value(conn, key)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def build(snap: dict, slots: tuple[str, ...], raw: dict, now: datetime) -> dict:
    """The panel's rows, one per selection in `slots`, from the snapshot's panels and `read`'s rows. Pure."""
    held = _data(snap, "stock", {}).get("slots", {})
    sales = _data(snap, "sales", {}).get("slots", {})
    planned = _planned(snap)
    tax, ceiling, options = raw["tax_percent"], raw["ceiling_cents"], raw["options"]
    history: dict[str, list[tuple[datetime, int]]] = {}
    for at, slot, cents in raw["history"]:
        history.setdefault(slot, []).append((at, cents))
    rows = []
    for slot in slots:
        h, sale = held.get(slot) or {}, sales.get(slot) or {}
        entry = planned.get(slot) if isinstance(planned.get(slot), dict) else None
        keep = bool(entry and entry.get("keep"))
        product, product_from = h.get("product"), "receipt" if h.get("product") else None
        if product is None and entry and not keep and isinstance(entry.get("product"), str):
            product, product_from = entry["product"], "plan"
        price = _cents(sale.get("price"))
        receipt = Fraction(h["unit_cost_cents"]) if h.get("unit_cost_cents") else None
        cost, basis = (receipt, "receipt") if receipt is not None else (_expected(options, product), "expected")
        if cost is None:
            basis = None
        floor, parts = _floor(receipt, options.get(product), tax)
        legal, problems = _check(price, floor, ceiling)
        rows.append({
            "slot": slot, "product": product, "product_from": product_from,
            "price": sale.get("price"),
            "unit_cost": _dollars(cost), "cost_basis": basis,
            "floor": _dollars(floor), "floor_from": parts, "legal": legal, "problems": problems,
            "margin": None if price is None or cost is None else _dollars(Fraction(price * 100, 100 + tax) - cost),
            "sold_7d": sale.get("sold"), "per_class_day": sale.get("per_class_day"),
            "rate_basis": _rate_basis(sale.get("rate_basis")),
            "next": _next(entry, keep, price, product, receipt, options, tax, ceiling),
            "history": _history(history.get(slot, [])),
        })
    return {"ceiling": _dollars(ceiling), "tax_percent": tax, "history_days": HISTORY_DAYS,
            "since": raw["since"], "until": now, "rows": rows}


def _data(snap: dict, key: str, default: Any) -> Any:
    panel = snap.get(key) or {}
    return panel["data"] if panel.get("ok") and panel.get("data") is not None else default


def _planned(snap: dict) -> dict[str, Any]:
    """The latest plan's entry for each of our slots (the plan panel keeps only ours)."""
    plan = _data(snap, "plan", None)
    return {s["slot"]: s.get("now") for s in plan["slots"]} if plan else {}


def _cents(text: Any) -> int | None:
    """A shelf or plan price ("2.50") to cents, or None when there is none or it isn't a whole-cent price."""
    if text is None or isinstance(text, (bool, float)):
        return None
    try:
        return to_cents(str(text))   # an int is dollars here (JSON 3 is $3), never cents
    except ValueError:
        return None


def _expected(options: dict, product: str | None) -> Fraction | None:
    """The primary option's expected price per unit, exact; None without one or without a price."""
    o = (options.get(product) or {}).get("primary") if product else None
    return None if o is None or o.expected_cents is None else Fraction(o.expected_cents, o.units_per_pack)


def _floor(receipt: Fraction | None, opts: dict | None, tax: int) -> tuple[int | None, list[str]]:
    """The slot's floor as the planner sets it for a restock, and what each part is. None when no cost is known."""
    parts: list[tuple[str, int]] = []
    if receipt is not None:
        parts.append((f"receipt cost {_dollars(receipt)}", floor_cents(receipt, tax)))
    for rank in RANKS:
        o = (opts or {}).get(rank)
        if o is not None:
            parts.append((f"{rank} {o.store} {o.units_per_pack}-pack at most {_dollars(o.max_cents)}",
                          pack_floor_cents(o.max_cents, o.units_per_pack, tax)))
    if not parts:
        return None, []
    return slot_floor_cents([cents for _, cents in parts]), [f"{what}: floor {_dollars(c)}" for what, c in parts]


def _check(price: int | None, floor: int | None, ceiling: int) -> tuple[bool | None, list[str]]:
    """`check_price`'s verdict: False with its messages when refused; True when legal; None when unknown
    (no price, or no floor and nothing else wrong)."""
    if price is None:
        return None, []
    refusals = check_price(price, floor or 0, ceiling)
    if refusals:
        return False, [r.message for r in refusals]
    return (True if floor is not None else None), []


def _next(entry: dict | None, keep: bool, price: int | None, product: str | None, receipt: Fraction | None,
          options: dict, tax: int, ceiling: int) -> dict | None:
    """The plan's new price for the slot, when it sets one that differs from the shelf; else None."""
    if entry is None or keep or entry.get("sale_price") is None:
        return None
    new = _cents(entry["sale_price"])
    if new is not None and new == price:
        return None
    goes_in = entry.get("product") if isinstance(entry.get("product"), str) else product
    # The units already in the slot count only while the plan keeps the same product.
    floor, _ = _floor(receipt if goes_in == product else None, options.get(goes_in), tax)
    legal, problems = _check(new, floor, ceiling) if new is not None else (False, ["not a whole-cent price"])
    return {"price": _dollars(new) if new is not None else str(entry["sale_price"]), "product": goes_in,
            "floor": _dollars(floor), "legal": legal, "problems": problems, "reason": entry.get("reason")}


def _rate_basis(text: str | None) -> str | None:
    """`own` or `fall prior`, from `vending.demand`'s wording; None otherwise."""
    if not text:
        return None
    return "own" if text.startswith("own") else "fall prior" if text.startswith("fall prior") else None


def _history(seen: list[tuple[datetime, int]]) -> dict:
    """The polls' prices as steps: the first price seen, then each change. `last_at` is the latest poll."""
    points: list[dict] = []
    for at, cents in seen:
        if not points or points[-1]["cents"] != cents:
            points.append({"at": at, "cents": cents, "price": _dollars(cents)})
    return {"points": points, "changes": max(len(points) - 1, 0), "last_at": seen[-1][0] if seen else None}


def _dollars(cents: Fraction | int | None) -> str | None:
    """Exact cents as dollars text, rounded half up to the cent. Never a float."""
    if cents is None:
        return None
    cents = Fraction(cents)
    value = Decimal(cents.numerator) / Decimal(cents.denominator) / 100
    return str(value.quantize(Decimal("0.01"), ROUND_HALF_UP))


# --- Page ---------------------------------------------------------------------------------------

def _e(value: Any) -> str:
    return "" if value is None else escape(str(value))


def _usd(text: str | None) -> str:
    if text is None:
        return '<span class="dim">&mdash;</span>'
    return f"&minus;${_e(text[1:])}" if text.startswith("-") else f"${_e(text)}"


def _when(at: datetime) -> str:
    return f"{at:%d %b}".lstrip("0")


def sparkline(history: dict, since: datetime, until: datetime) -> str:
    """The shelf price over the window as a step line, with a dot at each change. Inline SVG, no script."""
    points = history["points"]
    if not points:
        return '<span class="pr-nospark dim">no polls</span>'
    span = max((until - since).total_seconds(), 1)
    lo, hi = min(p["cents"] for p in points), max(p["cents"] for p in points)
    inner_w, inner_h = SPARK_W - 2 * SPARK_PAD, SPARK_H - 2 * SPARK_PAD

    def x(at: datetime) -> float:
        return SPARK_PAD + inner_w * min(max((at - since).total_seconds() / span, 0), 1)

    def y(cents: int) -> float:
        return SPARK_PAD + inner_h / 2 if hi == lo else SPARK_PAD + inner_h * (hi - cents) / (hi - lo)

    end = x(history["last_at"] or until)
    d = f"M{x(points[0]['at']):.1f} {y(points[0]['cents']):.1f}"
    for p in points[1:]:
        d += f" H{x(p['at']):.1f} V{y(p['cents']):.1f}"
    d += f" H{end:.1f}"
    dots = "".join(f'<circle cx="{x(p["at"]):.1f}" cy="{y(p["cents"]):.1f}" r="2.4"/>' for p in points[1:])
    steps = [f"${points[0]['price']} at {_when(max(points[0]['at'], since))}"]
    steps += [f"${p['price']} from {_when(p['at'])}" for p in points[1:]]
    label = f"Shelf price, last {HISTORY_DAYS} days: " + ", ".join(steps)
    return (f'<svg class="pr-spark" viewBox="0 0 {SPARK_W} {SPARK_H}" width="{SPARK_W}" height="{SPARK_H}" '
            f'role="img" aria-label="{_e(label)}"><title>{_e(label)}</title>'
            f'<line class="pr-base" x1="{SPARK_PAD}" x2="{SPARK_W - SPARK_PAD}" y1="{SPARK_H - 1}" y2="{SPARK_H - 1}"/>'
            f'<path d="{d}"/>{dots}</svg>')


def _legal_pill(legal: bool | None, floor: str | None) -> str:
    if legal is True:
        return '<span class="pill ok">legal</span>'
    if legal is False:
        return '<span class="pill bad">not legal</span>'
    return '<span class="pill muted">floor unknown</span>' if floor is None else ""


def _row(r: dict, since: datetime, until: datetime) -> str:
    cost = (f'{_usd(r["unit_cost"])} <span class="pill {"muted" if r["cost_basis"] == "receipt" else "warn"}">'
            f'{"receipt" if r["cost_basis"] == "receipt" else "expected"}</span>' if r["unit_cost"] is not None
            else '<span class="dim">unknown</span>')
    floor_title = "; ".join(r["floor_from"]) or "no receipt or sourcing cost, so no floor"
    margin = r["margin"]
    margin_html = (f'<span class="{"bad-text" if margin.startswith("-") else ""}">{_usd(margin)}</span>'
                   if margin is not None else _usd(None))
    rate = (f'{_e(r["per_class_day"])}/class day'
            + (f' <span class="dim">({_e(r["rate_basis"])})</span>' if r["rate_basis"] else "")
            if r["per_class_day"] is not None else '<span class="dim">no rate</span>')
    sold = (f'{_e(r["sold_7d"])} in 7 d' if r["sold_7d"] is not None else '<span class="dim">&mdash; in 7 d</span>')
    n = r["next"]
    if n is None:
        nxt = '<span class="dim">no change</span>'
    else:
        flag = "" if n["legal"] is not False else ' <span class="pill bad">not legal</span>'
        nxt = (f'<b>&rarr; ${_e(n["price"])}</b>{flag}'
               + (f' <span class="pr-reason" title="{_e(n["reason"])}">{_e(n["reason"])}</span>' if n["reason"] else ""))
    hist = r["history"]
    moved = (f'{hist["changes"]} change{"" if hist["changes"] == 1 else "s"}' if hist["changes"]
             else "no change" if hist["points"] else "")
    problems = r["problems"] + ([f'Plan: {m}' for m in n["problems"]] if n else [])
    why = (f'<div class="pr-why bad-text">{"<br>".join(_e(m) for m in problems)}</div>' if problems else "")
    product = (f'{_e(r["product"])}' + (' <span class="dim">(plan)</span>' if r["product_from"] == "plan" else "")
               if r["product"] else '<span class="dim">unknown</span>')
    return (f'<div class="pr-row{" pr-illegal" if r["legal"] is False else ""}" id="price-{_e(r["slot"])}">'
            f'<div class="pr-slot">{_e(r["slot"])}</div>'
            f'<div class="pr-prod">{product}</div>'
            f'<div class="pr-now"><span class="k">Now</span><b>{_usd(r["price"])}</b></div>'
            f'<div class="pr-stats">'
            f'<div><span class="k">Unit cost</span>{cost}</div>'
            f'<div title="{_e(floor_title)}"><span class="k">Floor</span>{_usd(r["floor"])} '
            f'{_legal_pill(r["legal"], r["floor"])}</div>'
            f'<div><span class="k">Margin</span>{margin_html}</div>'
            f'<div><span class="k">Sold</span>{sold} &middot; {rate}</div>'
            f'</div>'
            f'<div class="pr-next"><span class="k">Plan</span>{nxt}</div>'
            f'<div class="pr-hist">{sparkline(hist, since, until)}<span class="pr-moved dim">{moved}</span></div>'
            f'{why}</div>')


def html(panel: dict, ours: dict) -> str:
    """The whole section: the panel's rows, or why there are none."""
    head = ('<section id="pricing"><h2>Pricing <span class="hint">our selections &middot; shelf price from the '
            'latest poll &middot; floor and legality from the pricing gate</span></h2>')
    if not ours.get("ok"):
        return head + f'<p class="empty">No slot assignment is stored, so nothing is priced: {_e(ours.get("why"))}</p></section>'
    if not panel.get("ok"):
        return head + f'<p class="empty">Not available: {_e(panel.get("why"))}</p></section>'
    d = panel["data"]
    rows = d["rows"]
    if not any(r["price"] or r["unit_cost"] or r["next"] or r["history"]["points"] for r in rows):
        return (head + '<p class="empty">Nothing to price yet: no polls, receipts, sourcing or plan for our '
                'selections.</p></section>')
    illegal = sum(1 for r in rows if r["legal"] is False)
    planned = sum(1 for r in rows if r["next"] and r["next"]["legal"] is False)
    flags = ([f'{illegal} shelf price{"" if illegal == 1 else "s"} not legal'] if illegal else []) + (
        [f'{planned} plan price{"" if planned == 1 else "s"} not legal'] if planned else [])
    lede = (f'<p class="lede sub">Ceiling ${_e(d["ceiling"])}. Shelf prices include {_e(d["tax_percent"])}% sales tax; '
            f'margin is what one sale keeps after it, less the unit cost. <b>Expected</b> costs are from sourcing '
            f'(no receipt yet). The line is the shelf price at each poll over the last {_e(d["history_days"])} days.'
            + (f' <b class="bad-text">{"; ".join(flags)}.</b>' if flags else "")
            + "</p>")
    header = ('<div class="pr-head" aria-hidden="true"><div>Slot</div><div>Product</div><div>Now</div>'
              '<div>Unit cost</div><div>Floor</div><div>Margin</div><div>Sold</div><div>Plan</div>'
              f'<div>Last {_e(d["history_days"])} days</div></div>')
    body = "".join(_row(r, d["since"], d["until"]) for r in rows)
    return head + lede + f'<div class="pr-table">{header}{body}</div></section>'


STYLE = """
/* Pricing (`vending.pricing_panel`). */
.pr-table { font-size:13px }
.pr-head, .pr-row { display:grid; gap:2px 12px; align-items:center;
  grid-template-columns:34px minmax(110px, 1.2fr) 58px minmax(96px, .9fr) minmax(108px, 1fr) 62px minmax(150px, 1.2fr)
    minmax(150px, 1.5fr) 124px }
.pr-head { font-size:11px; font-weight:600; letter-spacing:.05em; text-transform:uppercase; color:var(--muted);
  padding:0 6px 6px; border-bottom:1px solid var(--line) }
.pr-row { padding:7px 6px; border-bottom:1px solid var(--line); min-width:0 }
.pr-row:last-child { border-bottom:0 }
.pr-row.pr-illegal { background:var(--bad-soft); border-radius:6px }
.pr-row > div, .pr-stats > div { min-width:0; overflow-wrap:anywhere }
.pr-stats { display:contents }
.pr-row .k { display:none }
.pr-slot { font-weight:700 }
.pr-now b { font-size:14px }
.pr-reason { display:block; color:var(--muted); font-size:12px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis }
.pr-hist { display:flex; flex-direction:column; gap:0 }
.pr-moved { font-size:11px }
.pr-spark { display:block; overflow:visible }
.pr-spark path { fill:none; stroke:var(--accent); stroke-width:1.6; stroke-linejoin:round }
.pr-spark circle { fill:var(--warn); stroke:var(--surface); stroke-width:1 }
.pr-spark .pr-base { stroke:var(--line); stroke-width:1 }
.pr-why { grid-column:2 / -1; font-size:12px }
@media (max-width:820px) {
  .pr-head { display:none }
  .pr-row { grid-template-columns:30px minmax(0, 1fr) auto auto; gap:3px 10px; align-items:start }
  .pr-slot { grid-row:span 3 }
  .pr-hist { grid-column:3; grid-row:1; align-items:flex-end }
  .pr-spark { width:84px; height:22px }
  .pr-now { grid-column:4; grid-row:1; text-align:right }
  .pr-prod { grid-column:2; grid-row:1; font-weight:600 }
  .pr-stats { display:flex; flex-wrap:wrap; gap:2px 14px; grid-column:2 / -1; font-size:12px }
  .pr-next { grid-column:2 / -1; font-size:12px }
  .pr-row .k { display:inline; margin-right:4px; font-size:10px; font-weight:600; letter-spacing:.05em;
    text-transform:uppercase; color:var(--muted) }
  .pr-now .k { display:none }
  .pr-reason { white-space:normal }
  .pr-why { grid-column:2 / -1 } }
"""
