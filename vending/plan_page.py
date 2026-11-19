"""The day-one planning page: each round of the agent's day-one plan, what it costs, what it is
projected to earn, which catalog requests it needs, and where the Interloper sells the same drink.

    python -m vending.plan_page --rounds C:\\Users\\KevinSykes\\vending-day1      # http://127.0.0.1:8768
    python -m vending.plan_page --rounds DIR --write plan.html                    # one file, no server

The rounds come from a folder of round files (see `vending.plan_rounds`); our slots (the inventory
server's assignment, `vending.slots.our_slots`) and the catalog requests come from our database
(`slot_assignment`, `product_requests` and `catalog_products`, read-only), and the Interloper's prices
from the dated files in `analysis/interloper/`. With nothing recorded in `slot_assignment`, or no
database, the page refuses and says what to run: it can't tell which slots are ours. `--no-db` skips
only the catalog requests, and the catalog panel says they weren't checked.

What the page computes and what it only shows:
- Floors, legal prices, column fit and the fill's cost are recomputed by `vending.plan_rounds` with
  `vending.pricing` and `vending.capacity`, the same rules the planner uses.
- Every forecast figure (units per class day, class days of cover, sell-out date, margin, gross
  profit by week and to the end of the window, for the low, base and high scenarios) is read from the
  round's projection file as written by the projection script, and shown next to that file's
  `assumptions`. The page has no demand model and makes no model calls.

Only the selections in our current assignment (the inventory server's split, read from the database by
`vending.slots.our_slots`) have values; the rest are drawn hatched with their number only. Before the
3 Dec start no selection is either team's and the split may be redrawn once; if it is, a round's
products must be placed again (docs/integrity.md).
The Interloper is an outside seller, so its prices may be shown; nothing here
feeds them to pricing.

The page runs no script (CSP `default-src 'none'`): the round tabs and the "changed slots only"
switch are plain HTML inputs styled with CSS, so the written file works the same as a published page.
It listens on 127.0.0.1 and answers only a Host of 127.0.0.1 or localhost. Standard library and
psycopg only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

from vending import db, settings
from vending import plan_rounds as pr
from vending.slots import DEVICE_ID, SlotsNotRecorded, our_slots
from vending.tools.catalog_request import same_drink, words

HOST = "127.0.0.1"
PORT = 8768
LOCAL_HOSTS = ("127.0.0.1", "localhost")
OPTIONS = "-c default_transaction_read_only=on -c statement_timeout=10000"
EASTERN = ZoneInfo("America/New_York")
SHELVES, COLUMNS = 5, 8
REPO = Path(__file__).resolve().parent.parent
INTERLOPER_DIR = REPO / "analysis" / "interloper"
CATALOG_ISSUE = "https://github.com/marshall-strong/agentic-vending/issues/119"
INTERLOPER_ISSUE = "https://github.com/marshall-strong/agentic-vending/issues/116"
CAP_ISSUE = "https://github.com/marshall-strong/agentic-vending/pull/113"

# Fill colours for a product's category, readable on the dark cabinet in both themes.
CATEGORY_COLOUR = {
    "energy": "#e0a526", "soda20": "#d64545", "sodacan": "#e46a5a", "sparkling": "#3fb6c9",
    "water": "#6aa8ff", "juice": "#f08c3a", "protein": "#a77bd9", "coffee": "#9a6b4a",
    "tea": "#5fb36b", "sports": "#4f7fe0",
}
OTHER_COLOUR = "#8a94a3"


# --- Data ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Request:
    """One catalog request in the pipeline: wanted by a round, filed, or both."""
    stage: str                  # "wanted", "filed", "approved", "rejected"
    name: str
    size: str | None
    rank: int | None            # the selected round's rank, if it wants it
    why: str | None
    status: str | None          # product_requests.status, or "in catalog", or None
    request_id: int | None = None
    requested_at: datetime | None = None
    decision_note: str | None = None
    team_only: bool = False     # filed, but not on the round's list


@dataclass(frozen=True)
class Pipeline:
    checked: bool               # False when there was no database to read
    note: str
    by_round: dict[str, list[Request]] = field(default_factory=dict)


@dataclass(frozen=True)
class Observation:
    observed: date
    source: str
    file: str
    drinks: list[dict]


@dataclass(frozen=True)
class Page:
    rounds: list[pr.Round]
    pipeline: Pipeline
    interloper: list[Observation]
    root: str
    made_at: datetime
    errors: list[str] = field(default_factory=list)


def _clean_size(size: str | None) -> str:
    return re.sub(r"\(.*?\)", " ", size or "")


def _requests(conn: psycopg.Connection) -> tuple[list[dict], list[dict]]:
    """Our catalog requests and the drinks in our copy of the catalog, from one read-only snapshot."""
    with conn.transaction():
        conn.execute("set transaction isolation level repeatable read, read only")
        with conn.cursor(row_factory=dict_row) as cur:
            requests = cur.execute(
                "select id, requested_at, name, size, line, flavor, status, decision_note, sku"
                " from product_requests order by requested_at, id").fetchall()
            catalog = cur.execute(
                "select sku, name, line, flavor, size from catalog_products where delisted_at is null").fetchall()
    return requests, catalog


def pipeline(rounds: list[pr.Round], requests: list[dict] | None, catalog: list[dict] | None) -> Pipeline:
    """Each round's wanted drinks matched to what has been filed and what is in the catalog.

    A request's status never moves past `pending` on its own (no sync reads it back from the
    bridge yet), so a drink counts as approved when it shows up in our copy of the catalog."""
    if requests is None or catalog is None:
        return Pipeline(False, "No database was read, so nothing shows as filed or approved.",
                        {r.id: [Request("wanted", c.get("name") or c.get("sku") or "?", c.get("size"),
                                        pr._opt_int(c.get("rank")), c.get("why"), None) for c in r.catalog_requests]
                         for r in rounds})
    req_words = [(q, words(q["name"], q.get("flavor"), _clean_size(q.get("size")))) for q in requests]
    cat_words = [words(c["name"], c.get("flavor"), _clean_size(c.get("size"))) for c in catalog]
    by_round: dict[str, list[Request]] = {}
    for r in rounds:
        rows: list[Request] = []
        used: set[int] = set()
        for c in r.catalog_requests:
            name, size = c.get("name") or c.get("sku") or "?", c.get("size")
            want = words(name, _clean_size(size))
            in_catalog = any(same_drink(want, w) for w in cat_words)
            filed = next((q for q, w in req_words if same_drink(want, w)), None)
            if filed:
                used.add(filed["id"])
            status = filed["status"] if filed else None
            if in_catalog:
                stage, status = "approved", "in catalog" if not filed else f"{status}; in catalog"
            elif filed and filed["status"] == "approved":
                stage = "approved"
            elif filed and filed["status"] == "rejected":
                stage = "rejected"
            elif filed:
                stage = "filed"
            else:
                stage = "wanted"
            rows.append(Request(stage, name, size, pr._opt_int(c.get("rank")), c.get("why"), status,
                                filed["id"] if filed else None, filed["requested_at"] if filed else None,
                                filed["decision_note"] if filed else None))
        for q, w in req_words:
            if q["id"] in used:
                continue
            in_catalog = any(same_drink(w, cw) for cw in cat_words)
            stage = ("approved" if in_catalog or q["status"] == "approved" else
                     "rejected" if q["status"] == "rejected" else "filed")
            rows.append(Request(stage, q["name"], q.get("size"), None, None,
                                q["status"] + ("; in catalog" if in_catalog else ""), q["id"],
                                q["requested_at"], q.get("decision_note"), team_only=True))
        by_round[r.id] = rows
    return Pipeline(True, f"Read {len(requests)} catalog request(s) and {len(catalog)} catalog drink(s) "
                          "from our database.", by_round)


def interloper(folder: Path = INTERLOPER_DIR) -> list[Observation]:
    """Every dated observation of the Interloper's drinks, newest first."""
    out = []
    if not folder.is_dir():
        return out
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            observed = date.fromisoformat(data["observed"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        drinks = [d for d in data.get("drinks") or [] if isinstance(d, dict) and isinstance(d.get("price_cents"), int)]
        out.append(Observation(observed, data.get("observed_by") or "", path.name, drinks))
    return sorted(out, key=lambda o: o.observed, reverse=True)


def overlap(rnd: pr.Round, observations: list[Observation]) -> list[dict]:
    """Interloper drinks that one of our slots also sells in this round (same brand line, by the
    file's `match` words), with every dated price seen for it. Drinks we don't carry are left out."""
    if not observations:
        return []
    latest = observations[0]
    rows = []
    for drink in latest.drinks:
        match = [m.lower() for m in drink.get("match") or [] if isinstance(m, str)]
        if not match:
            continue
        ours = [s for s in rnd.slots.values() if all(m in s.product.lower().replace("-", " ") for m in match)]
        if not ours:
            continue
        history = [(o.observed, d["price_cents"]) for o in observations for d in o.drinks
                   if d.get("name") == drink.get("name")]
        rows.append({"drink": drink, "ours": ours, "history": history, "observed": latest.observed})
    return rows


def overlapping_slots(rnd: pr.Round, observations: list[Observation]) -> set[str]:
    """Our slots in this round whose drink the Interloper also sells (the overlap table's rule)."""
    return {s.slot for row in overlap(rnd, observations) for s in row["ours"]}


def build(root: Path, ours: tuple[str, ...], conn_factory: Callable[[], psycopg.Connection] | None,
          interloper_dir: Path = INTERLOPER_DIR) -> Page:
    """The page for the rounds under `root`, checked against `ours` (our slots, from `read_our_slots`).
    `conn_factory` reads the catalog requests; None skips them."""
    rounds = pr.load_all(root, ours)
    errors = []
    requests = catalog = None
    if conn_factory is not None:
        try:
            with conn_factory() as conn:
                requests, catalog = _requests(conn)
        except settings.SettingsError as error:
            errors.append(f"Catalog requests weren't read: {error}")
        except psycopg.Error as error:
            # Only the error's kind: a driver message can quote DATABASE_URL, password included.
            errors.append(f"Catalog requests weren't read ({type(error).__name__}). Check DATABASE_URL and "
                          "that the database is up, or pass --no-db.")
    pipe = pipeline(rounds, requests, catalog)
    return Page(rounds, pipe, interloper(interloper_dir), str(root), datetime.now(timezone.utc), errors)


# --- Formatting ---------------------------------------------------------------------------------

def _e(value: Any) -> str:
    return "" if value is None else escape(str(value))


def _c(cents: int | Fraction | None) -> int | None:
    """Whole cents, rounded half up, for showing a Fraction of a cent."""
    if cents is None:
        return None
    if isinstance(cents, int):
        return cents
    return int((Decimal(cents.numerator) / Decimal(cents.denominator)).quantize(Decimal(1), ROUND_HALF_UP))


def money(cents: int | Fraction | None, whole: bool = False) -> str:
    c = _c(cents)
    if c is None:
        return "&mdash;"
    sign, c = ("&minus;" if c < 0 else ""), abs(c)
    if whole:
        return f"{sign}${(c + 50) // 100:,}"
    return f"{sign}${c // 100:,}.{c % 100:02d}"


def _num(value: float | None, places: int = 2) -> str:
    return "&mdash;" if value is None else f"{value:.{places}f}"


def _day(d: date | None) -> str:
    return "&mdash;" if d is None else f"{d:%a} {d.day} {d:%b}"


def _tab(round_id: str) -> str:
    return re.sub(r"[^a-z0-9]", "", round_id.lower()) or "x"


def _short(product: str) -> str:
    """A cabinet label: the product without size words, at most about 14 characters."""
    text = re.sub(r"\b(\d+(\.\d+)?\s*oz|can|bottle|edition|sparkling|energy drink)\b", "", product, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" ,()")
    return text if len(text) <= 16 else text[:15].rstrip() + "…"


def _colour(category: str | None) -> str:
    return CATEGORY_COLOUR.get(category or "", OTHER_COLOUR)


def _pill(text: str, tone: str, title: str | None = None) -> str:
    tip = f' title="{_e(title)}"' if title else ""
    return f'<span class="pill {tone}"{tip}>{_e(text)}</span>'


# --- Charts (inline SVG, no script) ---------------------------------------------------------------

def _px(v: float | Fraction) -> str:
    return f"{float(v):.1f}"


def rounds_chart(rounds: list[pr.Round]) -> str:
    """One row per round: the fill's most-we-pay cost against the $366 cap (left), and projected gross
    profit to the end of the window, base with a low-to-high line (right)."""
    if not rounds:
        return ""
    row, top, width, mid = 34, 26, 760, 380
    height = top + row * len(rounds) + 8
    lx, lw = 60, mid - 60 - 24                 # left plot
    rx, rw = mid + 16, width - mid - 16 - 30    # right plot, room for the last scale label
    cap_scale = max([pr.FILL_CAP_CENTS * 11 // 10] + [r.fill_most_cents or 0 for r in rounds])
    gp_scale = max([1] + [t.gross_profit_cents or 0 for r in rounds for t in r.totals.values()])
    gp_scale = ((gp_scale // 25_000) + 1) * 25_000
    out = [f'<text class="axis" x="{lx}" y="12">Day-one fill, most-we-pay, against the {money(pr.FILL_CAP_CENTS, True)} cap</text>',
           f'<text class="axis" x="{rx}" y="12">Projected gross profit to the end of the window (low · base · high)</text>']
    capx = lx + lw * pr.FILL_CAP_CENTS / cap_scale
    out.append(f'<line class="cap" x1="{_px(capx)}" x2="{_px(capx)}" y1="18" y2="{height - 4}"></line>')
    for tick in range(0, gp_scale + 1, 25_000 if gp_scale <= 200_000 else 50_000):
        x = rx + rw * tick / gp_scale
        out.append(f'<line class="grid" x1="{_px(x)}" x2="{_px(x)}" y1="18" y2="{height - 4}"></line>'
                   f'<text class="scale" x="{_px(x)}" y="{height}" text-anchor="middle">{money(tick, True)}</text>')
    for i, r in enumerate(rounds):
        y = top + row * i
        out.append(f'<text class="rlabel" x="4" y="{y + 14}">Round {_e(r.id)}</text>')
        if r.fill_most_cents is not None:
            w = lw * r.fill_most_cents / cap_scale
            kind = "over" if r.fill_most_cents > pr.FILL_CAP_CENTS else "under"
            out.append(f'<rect class="fill {kind}" x="{lx}" y="{y + 3}" width="{_px(w)}" height="16">'
                       f'<title>Round {_e(r.id)}: most-we-pay {money(r.fill_most_cents)}, expected '
                       f'{money(r.fill_expected_cents)}</title></rect>'
                       f'<text class="val" x="{_px(lx + w + 4)}" y="{y + 15}">{money(r.fill_most_cents)}</text>')
            if r.fill_expected_cents is not None:
                ex = lx + lw * r.fill_expected_cents / cap_scale
                out.append(f'<line class="expected" x1="{_px(ex)}" x2="{_px(ex)}" y1="{y + 1}" y2="{y + 21}"></line>')
        else:
            out.append(f'<text class="val" x="{lx}" y="{y + 15}">cost unknown</text>')
        t = {k: v.gross_profit_cents for k, v in r.totals.items() if v.gross_profit_cents is not None}
        if "base" in t:
            lo, hi = t.get("low", t["base"]), t.get("high", t["base"])
            x0, x1, xb = (rx + rw * v / gp_scale for v in (lo, hi, t["base"]))
            out.append(f'<line class="range" x1="{_px(x0)}" x2="{_px(x1)}" y1="{y + 11}" y2="{y + 11}"></line>'
                       f'<line class="range" x1="{_px(x0)}" x2="{_px(x0)}" y1="{y + 6}" y2="{y + 16}"></line>'
                       f'<line class="range" x1="{_px(x1)}" x2="{_px(x1)}" y1="{y + 6}" y2="{y + 16}"></line>'
                       f'<circle class="base" cx="{_px(xb)}" cy="{y + 11}" r="5"><title>Round {_e(r.id)}: low '
                       f'{money(lo, True)}, base {money(t["base"], True)}, high {money(hi, True)}</title></circle>'
                       f'<text class="val" x="{_px(xb)}" y="{y + 2}" text-anchor="middle">{money(t["base"], True)}</text>')
        else:
            out.append(f'<text class="val" x="{rx}" y="{y + 15}">no projection</text>')
    desc = "; ".join(f"round {r.id}: most-we-pay {money(r.fill_most_cents)}, base gross profit "
                     f"{money(r.totals['base'].gross_profit_cents, True) if 'base' in r.totals else 'not projected'}"
                     for r in rounds)
    return (f'<svg class="chart" viewBox="0 0 {width} {height + 4}" role="img" aria-labelledby="rc-t rc-d">'
            f'<title id="rc-t">Rounds compared</title><desc id="rc-d">{_e(desc)}</desc>' + "".join(out) + "</svg>")


def weekly_chart(rnd: pr.Round) -> str:
    """Projected gross profit by week over our 20 slots: base bars, a low-to-high line on each."""
    base = rnd.weeks.get("base")
    if not base:
        return ""
    low, high = dict(rnd.weeks.get("low", [])), dict(rnd.weeks.get("high", []))
    width, height, top, bottom, left = 720, 190, 14, 24, 64
    plot = height - top - bottom
    peak = max([1] + [v for _, v in base] + list(high.values()))
    step = 5_000 if peak <= 30_000 else 10_000
    peak = ((peak // step) + 1) * step
    slot = (width - left) / len(base)
    out = []
    for tick in range(0, peak + 1, step):
        y = top + plot - plot * tick / peak
        out.append(f'<line class="grid" x1="{left}" x2="{width}" y1="{_px(y)}" y2="{_px(y)}"></line>'
                   f'<text class="scale" x="{left - 6}" y="{_px(y)}" text-anchor="end" dominant-baseline="middle">'
                   f'{money(tick, True)}</text>')
    for i, (start, gp) in enumerate(base):
        x = left + slot * i + slot * 0.15
        w = slot * 0.7
        h = plot * gp / peak
        tip = f"Week of {_day(start)}: base {money(gp)}"
        if start in low or start in high:
            tip += f" (low {money(low.get(start))}, high {money(high.get(start))})"
        out.append(f'<rect class="bar pos" x="{_px(x)}" y="{_px(top + plot - h)}" width="{_px(w)}" height="{_px(max(h, 0.5))}">'
                   f'<title>{tip}</title></rect>')
        if start in low and start in high:
            y0, y1 = (top + plot - plot * v / peak for v in (low[start], high[start]))
            cx = x + w / 2
            out.append(f'<line class="range" x1="{_px(cx)}" x2="{_px(cx)}" y1="{_px(y0)}" y2="{_px(y1)}"></line>'
                       f'<line class="range" x1="{_px(cx - 4)}" x2="{_px(cx + 4)}" y1="{_px(y1)}" y2="{_px(y1)}"></line>'
                       f'<line class="range" x1="{_px(cx - 4)}" x2="{_px(cx + 4)}" y1="{_px(y0)}" y2="{_px(y0)}"></line>')
        if gp == 0:
            out.append(f'<text class="axis" x="{_px(x + w / 2)}" y="{top + plot - 4}" text-anchor="middle">break</text>')
        out.append(f'<text class="axis" x="{_px(x + w / 2)}" y="{height - 6}" text-anchor="middle">{start:%b} {start.day}</text>')
    out.append(f'<line class="zero" x1="{left}" x2="{width}" y1="{top + plot}" y2="{top + plot}"></line>')
    return (f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="Projected gross profit by week, '
            f'round {_e(rnd.id)}">' + "".join(out) + "</svg>")


# --- Page parts ---------------------------------------------------------------------------------

def _tiles(r: pr.Round) -> str:
    tiles = []
    if r.fill_most_cents is not None:
        left = pr.FILL_CAP_CENTS - r.fill_most_cents
        pct = min(100, 100 * r.fill_most_cents / pr.FILL_CAP_CENTS)
        tone = "bad" if left < 0 else "warn" if left < 1_000 else "ok"
        closet = sum(p.closet for p in r.purchases)
        tiles.append(
            f'<div class="tile"><div class="label">Day-one fill, most-we-pay</div><div class="big">{money(r.fill_most_cents)}</div>'
            f'<div class="meter"><i class="{tone}" style="width:{pct:.1f}%"></i></div>'
            f'<div class="sub"><span class="{tone}-text">{money(abs(left))} {"over" if left < 0 else "under"}</span> the '
            f'<a href="{CAP_ISSUE}">{money(pr.FILL_CAP_CENTS, True)} cap</a> (proposed) &middot; expected '
            f'{money(r.fill_expected_cents)} &middot; {sum(p.packs for p in r.purchases)} packs, {closet} units to the closet</div></div>')
    else:
        tiles.append('<div class="tile"><div class="label">Day-one fill</div><div class="big">unknown</div>'
                     '<div class="sub">No pack prices for this round, so its cost can\'t be counted.</div></div>')
    t = r.totals
    if "base" in t:
        b = t["base"]
        lo, hi = t.get("low"), t.get("high")
        tiles.append(
            f'<div class="tile"><div class="label">Gross profit to the end of the window</div>'
            f'<div class="big">{money(b.gross_profit_cents, True)}</div>'
            f'<div class="sub">base &middot; low {money(lo.gross_profit_cents if lo else None, True)}, high '
            f'{money(hi.gross_profit_cents if hi else None, True)} &middot; {_e(r.window or "")}</div></div>')
        tiles.append(
            f'<div class="tile"><div class="label">Week 1 (Thu 3 to Wed 9 Dec)</div>'
            f'<div class="big">{money(b.week1_gp_cents, True)}</div>'
            f'<div class="sub">base gross profit &middot; {_num(b.units_sold, 0)} units sold over the window</div></div>')
        if b.profit_cents is not None:
            end = pr.START_CENTS + b.profit_cents
            tiles.append(
                f'<div class="tile"><div class="label">Against the $600</div><div class="big">{money(end, True)}</div>'
                f'<div class="sub">$600 + base profit {money(b.profit_cents, True)} (gross profit less '
                f'{money(b.model_spend_cents, True)} model spend). Unsold stock counted at cost; no midweek visits.'
                f'{" Low " + money(pr.START_CENTS + lo.profit_cents, True) if lo and lo.profit_cents is not None else ""}'
                f'{", high " + money(pr.START_CENTS + hi.profit_cents, True) if hi and hi.profit_cents is not None else ""}.</div></div>')
    else:
        tiles.append('<div class="tile"><div class="label">Projection</div><div class="big">none</div>'
                     '<div class="sub">This round has no projection file yet.</div></div>')
    return f'<div class="tiles">{"".join(tiles)}</div>'


def _face(r: pr.Round, changed: dict[str, pr.Change], next_door: set[str]) -> str:
    shelves = []
    for row in range(1, SHELVES + 1):
        cells = []
        for col in range(1, COLUMNS + 1):
            slot = f"{row}{col}"
            s = r.slots.get(slot)
            if slot not in r.ours:
                cells.append(f'<span class="cell theirs" title="{slot}: not in our current assignment"><span class="num">{slot}</span></span>')
                continue
            if s is None:
                cells.append(f'<span class="cell ours empty"><span class="num">{slot}</span><span class="lbl">empty</span></span>')
                continue
            flags = ""
            if s.needs_request:
                flags += '<i class="flag req" title="needs a catalog request">R</i>'
            if slot in next_door:
                flags += '<i class="flag int" title="the Interloper sells this drink">I</i>'
            if s.refusals:
                flags += '<i class="flag bad" title="' + _e("; ".join(s.refusals)) + '">!</i>'
            dot = f'<i class="chg" title="{_e(", ".join(changed[slot].labels))} since the round before"></i>' if slot in changed else ""
            tip = f"{slot}: {s.product} {s.size or ''}, {s.units} units at {money(s.price_cents)} (floor {money(s.floor_cents)})"
            cells.append(
                f'<a class="cell ours{" changed" if slot in changed else ""}" href="#{_tab(r.id)}-s{slot}" title="{_e(tip)}">'
                f'<span class="num">{slot}</span>{dot}<span class="can" style="background:{_colour(s.category)}">'
                f'<b>{s.units}</b></span><span class="lbl">{_e(_short(s.product))}</span>'
                f'<span class="tag">{money(s.price_cents)}<s>{money(s.floor_cents)}</s></span>'
                f'<span class="flags">{flags}</span></a>')
        shelves.append(f'<div class="shelf">{"".join(cells)}</div>')
    cats = sorted({s.category for s in r.slots.values() if s.category})
    legend = "".join(f'<span><i style="background:{_colour(c)}"></i>{_e(c)}</span>' for c in cats)
    return (f'<div class="pcab"><div class="brand">{_e(DEVICE_ID.upper())} &middot; ROUND {_e(r.id.upper())}</div>'
            f'<div class="glass">{"".join(shelves)}</div></div>'
            f'<div class="legend">{legend}</div>'
            '<p class="sub">Striped selections aren\'t in our current assignment, which the inventory server changes once before the 3 Dec start; the day-one products would then move to the new slots. Each lit slot shows units loaded, the product, the '
            'shelf price and, struck through, its floor. <b class="flag req">R</b> needs a catalog request, '
            '<b class="flag int">I</b> the Interloper sells the same drink, <b class="flag bad">!</b> a price or '
            'column problem. A blue dot: changed since the round before.</p>')


def _change_list(r: pr.Round, before: pr.Round | None, changed: dict[str, pr.Change]) -> str:
    if before is None:
        return '<p class="dim">The first round: nothing earlier to compare with.</p>'
    if not changed:
        return f'<p class="dim">No slot changed since round {_e(before.id)}.</p>'
    items = []
    for slot, c in changed.items():
        a, b = c.after, c.before
        if a and b:
            parts = []
            if "new product" in c.labels:
                parts.append(f"{_e(b.product)} &rarr; <b>{_e(a.product)}</b>")
            if "price" in c.labels:
                parts.append(f"{money(b.price_cents)} &rarr; <b>{money(a.price_cents)}</b>")
            if "units" in c.labels:
                parts.append(f"{b.units} &rarr; <b>{a.units}</b> units")
            text = "; ".join(parts)
        else:
            text = _e(", ".join(c.labels))
        items.append(f'<li><a href="#{_tab(r.id)}-s{slot}"><b>{slot}</b></a> {text}</li>')
    same = (f'<p class="sub">Projected with the same assumptions as round {_e(before.id)}, so the totals compare.</p>'
            if pr.same_assumptions(r, before) else
            f'<p class="sub warn-text">The projection\'s assumptions differ from round {_e(before.id)}\'s, so the totals '
            'don\'t compare like for like. See the assumptions below.</p>' if r.assumptions and before.assumptions else "")
    return (f'<div class="label">{len(changed)} of {len(r.ours)} slots changed since round {_e(before.id)}</div>'
            f'<ul class="changes">{"".join(items)}</ul>{same}')


def _slot_table(r: pr.Round, changed: dict[str, pr.Change], next_door: set[str]) -> str:
    tid = _tab(r.id)
    gp_peak = max([1] + [s.projected["base"].gross_profit_cents or 0 for s in r.slots.values() if "base" in s.projected])
    rows = []
    for slot in r.ours:
        s = r.slots.get(slot)
        if s is None:
            rows.append(f'<tr id="{tid}-s{slot}" class="{"changed" if slot in changed else "same"}"><td>{slot}</td><td colspan="11" class="dim">No product.</td></tr>')
            continue
        b, lo, hi = (s.projected.get(k) for k in ("base", "low", "high"))
        flags = []
        if s.needs_request:
            flags.append(_pill("request", "info", "needs a catalog request"))
        if slot in next_door:
            flags.append(_pill("Interloper", "warn", "the Interloper sells this drink"))
        if s.capacity_assumed:
            flags.append(_pill(f"{s.capacity}/column assumed", "muted", "no one has counted a column of this size"))
        for problem in s.refusals:
            flags.append(_pill("problem", "bad", problem))
        if b and b.sellout_date:
            sells = f'<span class="bad-text">{_day(b.sellout_date)}</span>'
        elif b:
            sells = '<span class="dim">refilled first</span>'
        else:
            sells = "&mdash;"
        bar = ""
        if b and b.gross_profit_cents is not None:
            bar = f'<i class="gpbar" style="width:{max(2, 100 * b.gross_profit_cents / gp_peak):.0f}%"></i>'
        gp = (f'{bar}<b>{money(b.gross_profit_cents, True)}</b>'
              f'<span class="sub"> {money(lo.gross_profit_cents if lo else None, True)}&ndash;{money(hi.gross_profit_cents if hi else None, True)}</span>'
              if b else "&mdash;")
        what = ", ".join(changed[slot].labels) if slot in changed else ""
        rows.append(
            f'<tr id="{tid}-s{slot}" class="{"changed" if slot in changed else "same"}">'
            f'<td class="slotno">{slot}</td>'
            f'<td><i class="sw" style="background:{_colour(s.category)}"></i><b>{_e(s.product)}</b>'
            f'<span class="sub"> {_e(s.size or "")}</span> {"".join(flags)}'
            + (f'<div class="why">{_e(s.why)}</div>' if s.why else "") + "</td>"
            f'<td class="n">{s.units}</td><td class="n">{money(s.price_cents)}</td><td class="n dim">{money(s.floor_cents)}</td>'
            f'<td class="n">{money(s.most_cents)}</td>'
            f'<td class="n">{_num(b.units_per_class_day if b else None)}</td>'
            f'<td class="n">{_num(b.class_days_to_sellout if b else None, 1)}</td>'
            f'<td class="n">{sells}</td>'
            f'<td class="n">{money(b.margin_cents if b else None)}</td>'
            f'<td class="gp">{gp}</td><td class="dim">{_e(what)}</td></tr>')
    head = ("<tr><th>Slot</th><th>Product</th><th>Units</th><th>Price</th><th>Floor</th><th>Fill cost<br>(most)</th>"
            "<th>Units /<br>class day</th><th>Class days<br>of cover</th><th>Sells out<br>(base)</th>"
            "<th>Margin<br>/ unit</th><th>Gross profit to window end<br>base (low&ndash;high)</th><th>Changed</th></tr>")
    toggle = (f'<input type="checkbox" class="onlychg" id="{tid}-only"><label class="toggle" for="{tid}-only">'
              'Changed slots only</label>') if changed else ""
    return (toggle +
            f'<div class="scroll"><table class="slots"><thead>{head}</thead><tbody>{"".join(rows)}</tbody></table></div>'
            '<p class="sub">Units, price, floor and fill cost come from the plan and its pack prices, checked here with '
            '<code>vending.pricing</code> and <code>vending.capacity</code>. Fill cost is the slot\'s share of its product\'s '
            'packs at the most-we-pay price, closet leftovers included, so the column adds up to the fill. The forecast columns are read from the projection file, base scenario: '
            '<i>class days of cover</i> is a full column divided by the rate; <i>sells out</i> is the first class day it '
            'runs empty before a Friday refill. Margin per unit is price less 8% tax less expected unit cost.</p>')


def _purchases(r: pr.Round) -> str:
    if not r.purchases:
        return '<p class="empty">No pack prices for this round.</p>'
    rows = "".join(f'<tr><td>{_e(p.product)}</td><td class="n">{p.packs} &times; {p.units_per_pack}</td>'
                   f'<td class="n">{p.units_loaded}</td><td class="n">{p.closet}</td><td class="n">{money(p.expected_cents)}</td>'
                   f'<td class="n">{money(p.most_cents)}</td></tr>' for p in r.purchases)
    total = (f'<tr class="total"><td>Total</td><td class="n">{sum(p.packs for p in r.purchases)} packs</td>'
             f'<td class="n">{sum(p.units_loaded for p in r.purchases)}</td><td class="n">{sum(p.closet for p in r.purchases)}</td>'
             f'<td class="n">{money(r.fill_expected_cents)}</td><td class="n">{money(r.fill_most_cents)}</td></tr>')
    return (f'<div class="scroll"><table class="list"><thead><tr><th>Product</th><th>Packs</th><th>Loaded</th><th>Closet</th>'
            f'<th>Expected</th><th>Most-we-pay</th></tr></thead><tbody>{rows}{total}</tbody></table></div>')


_STAGES = (("wanted", "Wanted, not filed"), ("filed", "Filed, waiting"), ("approved", "Approved / in catalog"),
           ("rejected", "Rejected"))


def _pipeline(p: Pipeline, r: pr.Round) -> str:
    rows = p.by_round.get(r.id, [])
    cols = []
    for stage, title in _STAGES:
        cards = []
        for q in sorted((q for q in rows if q.stage == stage), key=lambda q: (q.team_only, q.rank or 999, q.name)):
            meta = []
            if q.rank:
                meta.append(f"#{_e(q.rank)} in round {_e(r.id)}")
            if q.team_only:
                meta.append("filed, not on this round's list")
            if q.request_id:
                meta.append(f"request {q.request_id}" + (f", {q.requested_at.astimezone(EASTERN):%a %d %b}" if q.requested_at else ""))
            if q.status:
                meta.append(_e(q.status))
            cards.append(f'<li><b>{_e(q.name)}</b> <span class="sub">{_e(q.size or "")}</span>'
                         f'<div class="sub">{" &middot; ".join(meta)}</div>'
                         + (f'<div class="why">{_e(q.why)}</div>' if q.why else "")
                         + (f'<div class="why">Note: {_e(q.decision_note)}</div>' if q.decision_note else "") + "</li>")
        body = f'<ul>{"".join(cards)}</ul>' if cards else '<p class="dim">none</p>'
        cols.append(f'<div class="stage {stage}"><div class="label">{title} &middot; {len(cards)}</div>{body}</div>')
    note = (f'<p class="sub">{_e(p.note)} A request\'s status isn\'t synced back from the bridge yet, so a drink counts '
            'as approved once it shows up in our copy of the catalog. The team\'s list and discussion: '
            f'<a href="{CATALOG_ISSUE}">#119</a>. The other team sees every request; that\'s expected.</p>')
    return f'<div class="stages">{"".join(cols)}</div>{note}'


def _interloper(r: pr.Round, obs: list[Observation]) -> str:
    if not obs:
        return '<p class="empty">No Interloper observations recorded (<code>analysis/interloper/</code>).</p>'
    rows = overlap(r, obs)
    latest = obs[0]
    if not rows:
        body = f'<p class="dim">None of round {_e(r.id)}\'s drinks are sold in the Interloper.</p>'
    else:
        lines = []
        for row in rows:
            d = row["drink"]
            ours = row["ours"]
            our_prices = sorted({s.price_cents for s in ours})
            mine, theirs = _ounces(ours[0].size), _ounces(d.get("size"))
            same_size = mine is not None and mine == theirs
            diff = our_prices[0] - d["price_cents"] if len(our_prices) == 1 and same_size else None
            tone = "" if diff is None else "bad-text" if diff > 0 else "ok-text" if diff < 0 else "dim"
            gap = ("size unknown" if mine is None or theirs is None else "different size" if not same_size else
                   "" if diff is None else
                   "same price" if diff == 0 else f'{money(abs(diff))} {"dearer" if diff > 0 else "cheaper"}')
            hist = ", ".join(f"{money(c)} on {_day(o)}" for o, c in row["history"])
            slots = ", ".join(s.slot for s in ours)
            lines.append(
                f'<tr><td><b>{_e(ours[0].product)}</b><span class="sub"> {_e(ours[0].size or "")} &middot; slot{"s" if len(ours) > 1 else ""} {slots}</span></td>'
                f'<td class="n">{" / ".join(money(c) for c in our_prices)}</td>'
                f'<td><b>{_e(d.get("name"))}</b><span class="sub"> {_e(d.get("size") or "")}</span></td>'
                f'<td class="n">{money(d["price_cents"])}</td><td class="{tone}">{gap}</td>'
                f'<td class="sub">{_e(hist)}</td></tr>')
        body = (f'<div class="scroll"><table class="list"><thead><tr><th>Ours, round {_e(r.id)}</th><th>Our price</th>'
                f'<th>The Interloper</th><th>Its price</th><th>We are</th><th>Seen</th></tr></thead>'
                f'<tbody>{"".join(lines)}</tbody></table></div>')
    return (body + f'<p class="sub">Overlapping drinks only: same brand line, matched by the words in '
            f'<code>analysis/interloper/{_e(latest.file)}</code>. Prices observed {_day(latest.observed)} by '
            f'{_e(latest.source)}. An outside seller, not the other team (<a href="{INTERLOPER_ISSUE}">#116</a>). '
            'Shown for comparison only; our pricing doesn\'t read these (#102 question 14 is still open).</p>')


def _ounces(size: str | None) -> str | None:
    """The fluid ounces in a size ("12 oz can" -> "12"), or None when the size gives none."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:fl\.?\s*)?oz", size or "", re.I)
    return m.group(1) if m else None


def _value(v: Any) -> str:
    if isinstance(v, dict):
        return ", ".join(f"{_e(k)} {_value(x)}" for k, x in v.items())
    if isinstance(v, list):
        return "&ndash;".join(_value(x) for x in v) if all(isinstance(x, (int, float)) for x in v) else ", ".join(_value(x) for x in v)
    return _e(v)


def _assumptions(r: pr.Round, before: pr.Round | None) -> str:
    if not r.assumptions:
        return '<p class="empty">No projection, so no assumptions.</p>'
    differs = set()
    if before and before.assumptions:
        differs = {k for k in set(r.assumptions) | set(before.assumptions) if r.assumptions.get(k) != before.assumptions.get(k)}
    rows = "".join(f'<tr class="{"diff" if k in differs else ""}"><th>{_e(k)}</th><td>{_value(v)}'
                   + (f'<div class="sub">round {_e(before.id)}: {_value(before.assumptions.get(k, "(not set)"))}</div>' if k in differs else "")
                   + "</td></tr>" for k, v in r.assumptions.items())
    head = (f'<p class="sub warn-text">{len(differs)} assumption(s) differ from round {_e(before.id)}, highlighted.</p>'
            if differs else "")
    return (head + f'<div class="scroll"><table class="assume"><tbody>{rows}</tbody></table></div>'
            '<p class="honest">These are the projection script\'s inputs, not measurements. Every forecast figure on this '
            'page follows from them; a round that scores well may only be fitting them.</p>')


def _notes(r: pr.Round) -> str:
    parts = []
    labels = {"checks": "What to check (the agent)", "asks": "What to find out (the agent)", "expect": "The agent's own estimate",
              "fallbacks": "Fallbacks", "cap_note": "Cap note", "note": "Note", "truncated": "Cut short",
              "problems": "Projection problems", "needs_hand_count": "Needs a hand count", "needs_new_source": "Needs a new source"}
    for key, title in labels.items():
        v = r.notes.get(key)
        if not v:
            continue
        if isinstance(v, list):
            items = "".join(f"<li>{_e(x.get('slot', ''))} {('if ' + _e(x['if']) + ': ' + _e(x.get('then', ''))) if isinstance(x, dict) and 'if' in x else _e(x)}</li>"
                            if isinstance(x, dict) else f"<li>{_e(x)}</li>" for x in v)
            parts.append(f'<div class="label">{title}</div><ul class="notes">{items}</ul>')
        else:
            parts.append(f'<div class="label">{title}</div><p>{_e(v)}</p>')
    if r.warnings:
        parts.append('<div class="label">This page found</div><ul class="notes">'
                     + "".join(f'<li class="warn-text">{_e(w)}</li>' for w in r.warnings) + "</ul>")
    src = (f'<p class="sub">Files: <code>{_e(r.plan_file)}</code>'
           + (f', <code>{_e(r.projection_file)}</code>' if r.projection_file else ", no projection")
           + f'; plan saved {r.saved_at.astimezone(EASTERN):%a %d %b %H:%M} ET.</p>')
    return "".join(parts) + src


def _round(r: pr.Round, before: pr.Round | None, page: Page) -> str:
    tid = _tab(r.id)
    changed = pr.changes(r, before)
    next_door = overlapping_slots(r, page.interloper)
    return (f'<div class="round" id="round-{tid}">'
            + _tiles(r)
            + f'<section><h2>The machine, round {_e(r.id)} <span class="hint">lit selections are our current assignment</span></h2>'
              f'<div class="vm-wrap"><div>{_face(r, changed, next_door)}</div><div class="card">{_change_list(r, before, changed)}</div></div></section>'
            + f'<section><h2>Slot by slot <span class="hint">top shelf first &middot; forecast from the projection, base '
              f'scenario</span></h2>{_slot_table(r, changed, next_door)}</section>'
            + '<div class="grid2">'
            + f'<section><h2>Projected gross profit by week <span class="hint">our 20 slots &middot; bars base, lines low '
              f'to high &middot; weeks Thu&ndash;Wed</span></h2><div class="scroll">{weekly_chart(r) or "<p class=empty>No projection.</p>"}</div>'
              '<p class="sub">The projection\'s weeks start on Thursday 3 Dec; the ledger\'s weeks run Saturday to '
              'Friday, so these won\'t line up with the P&amp;L report week for week.</p></section>'
            + f'<section><h2>What to buy <span class="hint">whole packs, leftovers to the closet</span></h2>{_purchases(r)}</section>'
            + "</div>"
            + f'<section><h2>Catalog requests <span class="hint">what round {_e(r.id)} wants, and how far each has got</span></h2>'
              f'{_pipeline(page.pipeline, r)}</section>'
            + f'<section><h2>Next door: the Interloper <span class="hint">drinks we both sell</span></h2>{_interloper(r, page.interloper)}</section>'
            + '<div class="grid2">'
            + f'<section><h2>Projection assumptions <span class="hint">round {_e(r.id)}</span></h2>{_assumptions(r, before)}</section>'
            + f'<section><h2>Notes <span class="hint">from the plan and the projection</span></h2>{_notes(r)}</section>'
            + "</div></div>")


def render(page: Page) -> str:
    rounds = page.rounds
    if not rounds:
        body = '<p class="empty">No rounds found under ' + _e(page.root) + ".</p>"
        return _wrap(body, "", "Day-one plan")
    latest = rounds[-1]
    radios = "".join(f'<input type="radio" name="round" class="tabin" id="tab-{_tab(r.id)}"'
                     f'{" checked" if r is latest else ""}>' for r in rounds)
    labels = "".join(
        f'<label for="tab-{_tab(r.id)}" class="tab">Round {_e(r.id)}'
        + (f' <span class="sub">{money(r.totals["base"].gross_profit_cents, True)}</span>' if "base" in r.totals else "")
        + "</label>" for r in rounds)
    tab_css = "".join(
        f"#tab-{_tab(r.id)}:checked ~ .rounds #round-{_tab(r.id)} {{ display:block }}"
        f"#tab-{_tab(r.id)}:checked ~ .tabs label[for=tab-{_tab(r.id)}] {{ background:var(--accent); color:var(--surface); border-color:var(--accent) }}"
        f"#tab-{_tab(r.id)}:checked ~ .tabs label[for=tab-{_tab(r.id)}] .sub {{ color:inherit }}"
        f"#tab-{_tab(r.id)}:focus-visible ~ .tabs label[for=tab-{_tab(r.id)}] {{ outline:2px solid var(--accent); outline-offset:2px }}"
        for r in rounds)
    errors = "".join(f'<p class="empty bad-text">{_e(e)}</p>' for e in page.errors)
    overview = (f'<section><h2>Rounds compared <span class="hint">most-we-pay against the cap, and projected gross profit'
                f'</span></h2><div class="scroll">{rounds_chart(rounds)}</div>{_overview_table(rounds)}</section>')
    head = (f'<header class="top"><h1>Day-one plan</h1>'
            f'<span class="meta">Read-only &middot; made {page.made_at.astimezone(EASTERN):%a %d %b %Y %H:%M} ET'
            f' &middot; {len(rounds)} round{"s" if len(rounds) != 1 else ""} from <code>{_e(page.root)}</code></span></header>')
    body = (radios + head + errors
            + f'<nav class="tabs" aria-label="Rounds">{labels}</nav>'
            + overview
            + '<div class="rounds">'
            + "".join(_round(r, rounds[i - 1] if i else None, page) for i, r in enumerate(rounds))
            + "</div>"
            + '<footer>Computed in code from the round files: no model calls. The $366 day-one cap and the $600 split are '
              f'proposed in <a href="{CAP_ISSUE}">#113</a>, not yet agreed. Forecasts are the projection script\'s, under '
              'its assumptions; they are estimates, not promises.</footer>')
    return _wrap(body, tab_css, f"Day-one plan, round {latest.id}")


def _overview_table(rounds: list[pr.Round]) -> str:
    rows = []
    for i, r in enumerate(rounds):
        before = rounds[i - 1] if i else None
        t = r.totals
        n = len(pr.changes(r, before)) if before else None
        rows.append(
            f'<tr><td><label for="tab-{_tab(r.id)}" class="link">Round {_e(r.id)}</label></td>'
            f'<td class="sub">{r.saved_at.astimezone(EASTERN):%a %d %b %H:%M}</td>'
            f'<td class="n">{money(r.fill_expected_cents)}</td><td class="n">{money(r.fill_most_cents)}</td>'
            f'<td class="n">{money(None if r.fill_most_cents is None else pr.FILL_CAP_CENTS - r.fill_most_cents)}</td>'
            + "".join(f'<td class="n">{money(t[k].gross_profit_cents, True) if k in t else "&mdash;"}</td>' for k in pr.SCENARIOS)
            + f'<td class="n">{money(t["base"].week1_gp_cents, True) if "base" in t else "&mdash;"}</td>'
            f'<td class="n">{"&mdash;" if n is None else n}</td>'
            f'<td>{"" if before is None or not r.assumptions else ("same" if pr.same_assumptions(r, before) else "<span class=warn-text>changed</span>")}</td></tr>')
    return ('<div class="scroll"><table class="list"><thead><tr><th>Round</th><th>Saved (ET)</th><th>Fill expected</th>'
            '<th>Fill most</th><th>Under cap</th><th>GP low</th><th>GP base</th><th>GP high</th><th>Week 1 base</th>'
            '<th>Slots changed</th><th>Assumptions</th></tr></thead><tbody>' + "".join(rows) + "</tbody></table></div>")


STYLE = """
:root { color-scheme:light dark;
  --bg:#f3f5f8; --surface:#fff; --sunk:#f7f8fa; --ink:#18202a; --muted:#5d6977; --line:#dfe4ea;
  --accent:#1f5fd1; --accent-soft:#e6efff; --ok:#16794a; --ok-soft:#e3f5ea; --warn:#9a5b00; --warn-soft:#fff2d9;
  --bad:#c0262d; --bad-soft:#fde7e7; --info:#1f5fd1; --info-soft:#e6efff; --bar:#e7ebf0; --shadow:0 1px 2px rgba(16,24,40,.06);
  --cab:#262b34; --cab-2:#1e222a; --cab-edge:#3a414d; --cab-brand:linear-gradient(90deg, #b4232a, #d9363e); }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg:#0e1116; --surface:#161b22; --sunk:#11151b; --ink:#e6eaf0; --muted:#94a0ae; --line:#2a313b;
  --accent:#6ea8ff; --accent-soft:#16294a; --ok:#4cc38a; --ok-soft:#12301f; --warn:#f0b450; --warn-soft:#3a2a10;
  --bad:#ff6b6b; --bad-soft:#3d1a1c; --info:#6ea8ff; --info-soft:#16294a; --bar:#262d36; --shadow:none;
  --cab:#1f242c; --cab-2:#181c22; --cab-edge:#363d48; --cab-brand:linear-gradient(90deg, #8f1d23, #b02a31); } }
:root[data-theme="dark"] {
  --bg:#0e1116; --surface:#161b22; --sunk:#11151b; --ink:#e6eaf0; --muted:#94a0ae; --line:#2a313b;
  --accent:#6ea8ff; --accent-soft:#16294a; --ok:#4cc38a; --ok-soft:#12301f; --warn:#f0b450; --warn-soft:#3a2a10;
  --bad:#ff6b6b; --bad-soft:#3d1a1c; --info:#6ea8ff; --info-soft:#16294a; --bar:#262d36; --shadow:none;
  --cab:#1f242c; --cab-2:#181c22; --cab-edge:#363d48; --cab-brand:linear-gradient(90deg, #8f1d23, #b02a31); }
* { box-sizing:border-box }
html { -webkit-text-size-adjust:100% }
body { margin:0; background:var(--bg); color:var(--ink);
  font:14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; font-variant-numeric:tabular-nums }
main { max-width:1240px; margin:0 auto; padding:16px }
h1 { font-size:20px; margin:0 } h2 { font-size:15px; margin:0 }
code { font:12px ui-monospace, "Cascadia Mono", Consolas, monospace; background:var(--sunk); border:1px solid var(--line);
  border-radius:4px; padding:0 4px; overflow-wrap:anywhere }
a, label.link { color:var(--accent); cursor:pointer } label.link { text-decoration:underline }
.dim { color:var(--muted) } .ok-text { color:var(--ok) } .warn-text { color:var(--warn) } .bad-text { color:var(--bad) }
header.top { display:flex; flex-wrap:wrap; align-items:center; gap:8px 14px; margin-bottom:12px }
header.top .meta { color:var(--muted); font-size:12px; margin-left:auto }
.tabin { position:absolute; opacity:0; pointer-events:none }
.tabs { display:flex; flex-wrap:wrap; gap:6px; margin:0 0 12px }
.tab { border:1px solid var(--line); background:var(--surface); border-radius:999px; padding:5px 14px; font-weight:600;
  cursor:pointer; user-select:none }
.round { display:none }
.tiles { display:grid; grid-template-columns:repeat(auto-fit, minmax(210px, 1fr)); gap:10px; margin:0 0 14px }
.tile { background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:12px 14px; box-shadow:var(--shadow); min-width:0 }
.label { font-size:11px; font-weight:600; letter-spacing:.05em; text-transform:uppercase; color:var(--muted) }
.big { display:inline-block; font-size:22px; font-weight:700; margin:2px 0 }
.sub { color:var(--muted); font-size:12px; font-weight:400; overflow-wrap:anywhere }
.meter { height:8px; background:var(--bar); border-radius:4px; overflow:hidden; margin:2px 0 6px }
.meter i { display:block; height:100% } .meter i.ok { background:var(--ok) } .meter i.warn { background:var(--warn) }
.meter i.bad { background:var(--bad) }
section { background:var(--surface); border:1px solid var(--line); border-radius:12px; padding:14px 16px; margin:14px 0;
  box-shadow:var(--shadow); min-width:0 }
section > h2 { margin-bottom:10px; display:flex; align-items:baseline; gap:8px; flex-wrap:wrap }
section > h2 .hint { font-weight:400; font-size:12px; color:var(--muted) }
.grid2 { display:grid; grid-template-columns:minmax(0, 1fr); gap:0 14px }
@media (min-width:980px) { .grid2 { grid-template-columns:minmax(0, 1fr) minmax(0, 1fr) } }
.empty { color:var(--muted); background:var(--sunk); border:1px dashed var(--line); border-radius:8px; padding:10px 12px; margin:0 0 10px }
.honest { font-size:12px; color:var(--muted); font-style:italic; margin:6px 0 0 }
.pill { display:inline-block; font-size:11px; font-weight:600; line-height:18px; padding:0 7px; border-radius:999px;
  white-space:nowrap; vertical-align:1px; margin-left:2px }
.pill.ok { background:var(--ok-soft); color:var(--ok) } .pill.warn { background:var(--warn-soft); color:var(--warn) }
.pill.bad { background:var(--bad-soft); color:var(--bad) } .pill.info { background:var(--info-soft); color:var(--info) }
.pill.muted { background:var(--sunk); color:var(--muted); border:1px solid var(--line) }
.scroll { overflow-x:auto; max-width:100% }
table { border-collapse:collapse; width:100% }
th, td { text-align:left; vertical-align:top; padding:6px 8px; border-bottom:1px solid var(--line) }
th { font-size:11px; font-weight:600; color:var(--muted); text-transform:uppercase; letter-spacing:.03em }
td.n, th.n { text-align:right; white-space:nowrap }
table.slots td.n { text-align:right } table.slots th { white-space:nowrap }
table.slots { min-width:1000px } table.list { min-width:560px } table.assume { min-width:420px }
svg.chart { min-width:520px }
tr.total td { font-weight:700; border-top:2px solid var(--line) }
td.slotno { font:700 13px ui-monospace, Consolas, monospace }
.why { color:var(--muted); font-size:12px; margin-top:2px; max-width:60ch }
.sw { display:inline-block; width:10px; height:10px; border-radius:3px; margin-right:6px; vertical-align:0 }
td.gp { min-width:150px; position:relative; white-space:nowrap }
.gpbar { display:block; height:4px; border-radius:2px; background:var(--ok); margin-bottom:3px; opacity:.7 }
.onlychg { position:absolute; opacity:0 }
.toggle { display:inline-block; font-size:12px; font-weight:600; border:1px solid var(--line); border-radius:999px;
  padding:3px 12px; margin:0 0 8px; cursor:pointer; color:var(--muted) }
.toggle::before { content:"\\2610  " }
.onlychg:checked + .toggle { background:var(--accent); color:var(--surface); border-color:var(--accent) }
.onlychg:checked + .toggle::before { content:"\\2611  " }
.onlychg:checked ~ .scroll tr.same { display:none }
.onlychg:focus-visible + .toggle { outline:2px solid var(--accent) }
table.assume th { width:28%; text-transform:none; font-family:ui-monospace, Consolas, monospace; letter-spacing:0 }
table.assume tr.diff td, table.assume tr.diff th { background:var(--warn-soft) }
ul.changes, ul.notes { margin:6px 0; padding-left:18px } ul.changes li { margin:2px 0 }
.card { background:var(--sunk); border:1px solid var(--line); border-radius:10px; padding:10px 12px; min-width:0 }
.vm-wrap { display:grid; grid-template-columns:minmax(0, 600px) minmax(0, 1fr); gap:14px; align-items:start }
@media (max-width:900px) { .vm-wrap { grid-template-columns:minmax(0, 1fr) } }
.pcab { background:linear-gradient(90deg, var(--cab), var(--cab-2) 60%, var(--cab)); border-radius:14px 14px 6px 6px;
  padding:10px; box-shadow:inset 0 0 0 2px var(--cab-edge) }
.brand { color:#fff; font-weight:800; letter-spacing:.14em; font-size:11px; text-align:center; padding:5px 0;
  background:var(--cab-brand); border-radius:6px; margin-bottom:8px }
.glass { background:linear-gradient(160deg, rgba(255,255,255,.08), rgba(255,255,255,.02) 35%, rgba(255,255,255,.05));
  border-radius:8px; padding:5px 5px 0; box-shadow:inset 0 0 0 1px rgba(255,255,255,.1) }
.shelf { display:grid; grid-template-columns:repeat(8, minmax(0, 1fr)); gap:3px; border-bottom:3px solid #505968;
  padding-bottom:2px; margin-bottom:3px }
.cell { position:relative; display:flex; flex-direction:column; justify-content:flex-end; align-items:center; min-width:0;
  height:104px; padding:12px 1px 2px; border-radius:4px 4px 0 0; color:#cfd6df; text-decoration:none }
.cell .num { position:absolute; top:1px; left:0; right:0; text-align:center; font:600 9px ui-monospace, Consolas, monospace; color:#9aa5b2 }
.theirs { background:repeating-linear-gradient(45deg, rgba(255,255,255,.05) 0 5px, transparent 5px 10px) }
.theirs .num { color:rgba(255,255,255,.3); top:40% }
.ours { background:rgba(255,255,255,.04) } a.ours:hover, a.ours:focus-visible { background:rgba(255,255,255,.12); outline:none }
.ours.changed { box-shadow:inset 0 0 0 2px var(--accent) }
.can { width:62%; max-width:24px; flex:1 1 auto; max-height:44px; border-radius:6px 6px 3px 3px; display:flex; align-items:center;
  justify-content:center; margin-top:4px; box-shadow:inset 0 0 0 1px rgba(255,255,255,.25) }
.can b { font:700 10px ui-monospace, Consolas, monospace; color:#fff; text-shadow:0 1px 2px #000 }
.lbl { width:100%; margin-top:2px; font-size:8px; line-height:10px; text-align:center; white-space:nowrap; overflow:hidden;
  text-overflow:ellipsis; color:#cfd6df }
.tag { margin-top:2px; width:100%; background:#f3f1e7; color:#1d232b; border-radius:2px; font:700 9px/11px ui-monospace, Consolas, monospace;
  text-align:center; white-space:nowrap; overflow:hidden }
.tag s { display:block; font-weight:400; opacity:.55 }
.flags { position:absolute; top:10px; left:1px; display:flex; flex-direction:column; gap:1px }
.flag { display:inline-block; font:700 8px/11px ui-monospace, Consolas, monospace; font-style:normal; width:11px; text-align:center;
  border-radius:2px; color:#fff }
.flag.req { background:var(--info) } .flag.int { background:#b26b00 } .flag.bad { background:var(--bad) }
b.flag { width:auto; padding:0 3px; font-size:10px }
.chg { position:absolute; top:-2px; right:-1px; width:9px; height:9px; border-radius:50%; background:var(--info);
  box-shadow:0 0 0 2px var(--cab-2) }
.legend { display:flex; flex-wrap:wrap; gap:4px 12px; margin:8px 0 4px; font-size:12px; color:var(--muted) }
.legend i { display:inline-block; width:10px; height:10px; border-radius:3px; margin-right:4px; vertical-align:-1px }
.stages { display:grid; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); gap:10px }
.stage { background:var(--sunk); border:1px solid var(--line); border-radius:10px; padding:10px; min-width:0 }
.stage ul { list-style:none; margin:6px 0 0; padding:0; display:grid; gap:6px }
.stage li { background:var(--surface); border:1px solid var(--line); border-left:4px solid var(--muted); border-radius:6px; padding:6px 8px }
.stage.filed li { border-left-color:var(--info) } .stage.approved li { border-left-color:var(--ok) }
.stage.rejected li { border-left-color:var(--bad) }
svg.chart { width:100%; height:auto; display:block; margin:4px 0 8px }
svg .grid { stroke:var(--line); stroke-width:1 } svg .zero { stroke:var(--muted); stroke-width:1 }
svg .scale, svg .axis, svg .val, svg .rlabel { fill:var(--muted); font-size:11px } svg .rlabel { fill:var(--ink); font-weight:600; font-size:12px }
svg .val { fill:var(--ink) }
svg .bar.pos { fill:var(--ok); opacity:.8 } svg .range { stroke:var(--ink); stroke-width:1.5; opacity:.6 }
svg .fill.under { fill:var(--accent); opacity:.75 } svg .fill.over { fill:var(--bad) }
svg .cap { stroke:var(--bad); stroke-width:2; stroke-dasharray:4 3 } svg .expected { stroke:var(--ink); stroke-width:2 }
svg .base { fill:var(--ok); stroke:var(--surface); stroke-width:2 }
footer { color:var(--muted); font-size:12px; margin:18px 0 8px }
@media print { .tabs, .toggle { display:none } }
"""


def _wrap(body: str, tab_css: str, title: str) -> str:
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{_e(title)}</title><style>{STYLE}{tab_css}</style></head><body><main>{body}</main></body></html>')


# --- Server -------------------------------------------------------------------------------------

def _connect() -> psycopg.Connection:
    url = settings.require("DATABASE_URL", "Set DATABASE_URL to read catalog requests, or pass --no-db.")
    return psycopg.connect(url, autocommit=True, connect_timeout=db.CONNECT_TIMEOUT,
                           options=db.session_options(url, OPTIONS))


# Tests swap this to point at the local test database.
connect: Callable[[], psycopg.Connection] = _connect


class NoSlots(RuntimeError):
    """Our slots couldn't be read, so the page can't tell which are ours. The message says what to do."""


def read_our_slots() -> tuple[str, ...]:
    """Our slots (the inventory server's assignment) from the database, or NoSlots saying what to do."""
    try:
        with connect() as conn:
            return our_slots(conn)
    except SlotsNotRecorded as error:
        raise NoSlots(str(error)) from None
    except settings.SettingsError as error:
        raise NoSlots(f"Our slots weren't read: {error}") from None
    except psycopg.Error as error:
        # Only the error's kind: a driver message can quote DATABASE_URL, password included.
        raise NoSlots(f"Our slots weren't read ({type(error).__name__}). They are the inventory server's "
                      "assignment, stored in our database: check DATABASE_URL and that the database is up.") from None


def _handler(root: Path, use_db: bool) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "vending-plan-page"

        def do_GET(self) -> None:  # noqa: N802 (the stdlib's name)
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            if host not in LOCAL_HOSTS:
                return self._send(403, "text/plain; charset=utf-8", "open this page as 127.0.0.1 or localhost")
            if self.path.split("?", 1)[0] != "/":
                return self._send(404, "text/plain; charset=utf-8", "not found")
            try:
                page = build(root, read_our_slots(), connect if use_db else None)
            except NoSlots as error:
                return self._send(500, "text/plain; charset=utf-8", f"can't tell which slots are ours: {error}")
            except pr.RoundError as error:
                return self._send(500, "text/plain; charset=utf-8", f"can't read the rounds: {error}")
            self._send(200, "text/html; charset=utf-8", render(page))

        def _send(self, code: int, content_type: str, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 (the stdlib's name)
            pass
    return Handler


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.plan_page", description=__doc__.splitlines()[0])
    parser.add_argument("--rounds", required=True, type=Path, help="folder holding round1/, round2/, ...")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--no-db", action="store_true",
                        help="don't read catalog requests (our slots still come from DATABASE_URL)")
    parser.add_argument("--write", type=Path, help="write the page to this file and exit, instead of serving it")
    args = parser.parse_args(argv)
    use_db = not args.no_db
    if args.write:
        try:
            page = build(args.rounds, read_our_slots(), connect if use_db else None)
        except NoSlots as error:
            print(f"can't tell which slots are ours: {error}", file=sys.stderr)
            return 1
        except pr.RoundError as error:
            print(f"can't read the rounds: {error}", file=sys.stderr)
            return 1
        args.write.write_text(render(page), encoding="utf-8")
        print(f"wrote {args.write} ({len(page.rounds)} rounds)" + "".join(f"\n  {e}" for e in page.errors))
        return 0
    httpd = ThreadingHTTPServer((HOST, args.port), _handler(args.rounds, use_db))
    print(f"day-one plan on http://{HOST}:{args.port}  (read-only; Ctrl+C to stop)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
