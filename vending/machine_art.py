"""The machine close-up on the dashboard: each of our lanes drawn top-down, Now and Next Friday.

`vending.dashboard` calls `closeup_html` for "In the machine". It draws no photos: every
package is vector art built from a small shape library, picked by the catalog's `size`
("12 oz can", "20 oz bottle") and coloured from a look table, so a product added to the
catalog today draws correctly with no image and no edit here. Only string building, no
model calls, no network.

 - SHAPES: one entry per package, real millimetre sizes (typical retail figures, not measured
   on our packages), so sizes stay true to each other. Picked with `capacity.parse_size`, the
   same reading of a size that the capacity chart uses; an unreadable size gets the generic
   shape of its container, or the generic can.
 - LOOKS: body, band and cap colours, ink and a short label, by SKU, then "line|flavor", then
   line, all lower case. Anything else gets a steady colour from a hash. Add a row to make a
   product prettier; nothing breaks without one.
 - Capacity: `capacity.for_product` (the loading chart, or a person's count in
   `config.capacity_overrides`). With none, no empty slots are drawn beyond what the plan fills,
   and the lane says "capacity not counted yet".

Only our selections (the stored assignment) are drawn. Every other selection is a grey card
with its number and "Not ours": nothing else about it is in the HTML.
"""

from __future__ import annotations

import re
import zlib
from collections.abc import Iterable, Mapping
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from fractions import Fraction
from html import escape
from typing import Any

from vending import capacity, config

SHELVES, COLUMNS = 5, 8

# --- Package shapes -------------------------------------------------------------------------------
# d = body diameter (or width), h = overall height, in mm. Bottles add body, shoulder, neck and
# cap sizes. `band` is the label band, as fractions of the body height. `word` must match the
# word before the container in the size ("12 oz sleek can"); `tol` is how far, in ounces, a
# size may be from `oz` and still match.
SHAPES: dict[str, dict[str, Any]] = {
    "can-12": {"label": "12 oz can", "container": "can", "oz": 12, "tol": 0.15, "family": "can",
               "d": 66, "h": 122, "band": (0.16, 0.60)},
    "can-12-sleek": {"label": "12 oz sleek can", "container": "can", "oz": 12, "tol": 0.15, "word": "sleek",
                     "family": "can", "d": 58, "h": 146, "band": (0.14, 0.64)},
    "can-16": {"label": "16 oz can", "container": "can", "oz": 16, "tol": 0.15, "family": "can",
               "d": 66, "h": 157, "band": (0.14, 0.64)},
    "can-250ml": {"label": "250 ml can", "container": "can", "oz": 8.45, "tol": 0.4, "family": "can",
                  "d": 53, "h": 134, "band": (0.14, 0.62)},
    "can-7.5": {"label": "7.5 oz mini can", "container": "can", "oz": 7.5, "tol": 0.15, "family": "can",
                "d": 52, "h": 89, "band": (0.16, 0.62)},
    "bottle-20": {"label": "20 oz bottle", "container": "bottle", "oz": 20, "tol": 0.15, "family": "bottle",
                  "d": 66, "h": 211, "body_h": 120, "shoulder_h": 44, "neck_d": 29, "cap_d": 30, "cap_h": 19,
                  "band": (0.12, 0.56)},
    "bottle-16": {"label": "16 oz bottle", "container": "bottle", "oz": 16, "tol": 0.15, "family": "bottle",
                  "d": 62, "h": 196, "body_h": 112, "shoulder_h": 42, "neck_d": 27, "cap_d": 29, "cap_h": 18,
                  "band": (0.12, 0.56)},
    "bottle-16-glass": {"label": "16 oz glass bottle", "container": "bottle", "oz": 16, "tol": 0.15,
                        "word": "glass", "family": "glass", "d": 60, "h": 215, "body_h": 112, "shoulder_h": 50,
                        "neck_d": 25, "cap_d": 28, "cap_h": 8, "band": (0.10, 0.50)},
    "bottle-16.9": {"label": "16.9 oz bottle", "container": "bottle", "oz": 16.9, "tol": 0.15, "family": "bottle",
                    "d": 63, "h": 203, "body_h": 115, "shoulder_h": 42, "neck_d": 28, "cap_d": 29, "cap_h": 18,
                    "band": (0.14, 0.58)},
    "bottle-14": {"label": "14 oz bottle", "container": "bottle", "oz": 14, "tol": 0.15, "family": "bottle",
                  "d": 56, "h": 178, "body_h": 100, "shoulder_h": 36, "neck_d": 25, "cap_d": 26, "cap_h": 16,
                  "band": (0.12, 0.58)},
    "carton-11": {"label": "11 oz carton", "container": "carton", "oz": 11, "tol": 0.6, "family": "carton",
                  "d": 52, "h": 150, "gable_h": 22, "cap_d": 18, "cap_h": 9},
}
# The shape for a size that names only its container, or nothing readable at all ("can").
FALLBACK = {"can": "can-12", "bottle": "bottle-16", "carton": "carton-11"}
# The loading-label packages `package_from_name` reads from a free-text name, as shapes.
_PACKAGE_SHAPE = {"20oz PET": "bottle-20", "16oz glass": "bottle-16-glass", "16oz PET": "bottle-16",
                  "12oz can": "can-12", "240/250ml can": "can-250ml", "14oz PET": "bottle-14", "16oz can": "can-16"}

# --- Product looks --------------------------------------------------------------------------------
# c1 body, c2 label band, c3 cap or stripe, ink the label text. Colours eyeballed from the packages.
LOOKS: dict[str, dict[str, dict[str, str]]] = {
    "by_sku": {},
    "by_line_flavor": {
        "recess|orange peach": {"c1": "#f0a05f", "c2": "#f9dfb8", "c3": "#e67a2e", "ink": "#7a3410", "label": "Recess"},
        "recess|lime": {"c1": "#c3d12c", "c2": "#f0f5a8", "c3": "#8da016", "ink": "#434f0a", "label": "Recess"},
        "bubly|strawberry": {"c1": "#94c93a", "c2": "#f6d3d6", "c3": "#e23b4f", "ink": "#8a1a2a", "label": "bubly"},
        "bubly|paradise paloma": {"c1": "#f2a68a", "c2": "#c8e07a", "c3": "#a3cc3a", "ink": "#6a2f1a", "label": "bubly"},
        "mountain dew|baja blast": {"c1": "#2bb7c3", "c2": "#f9f4e6", "c3": "#f59a30", "ink": "#126a72",
                                    "label": "Baja Blast"},
    },
    "by_line": {
        "coca-cola": {"c1": "#d71920", "c2": "#ffffff", "c3": "#b0141a", "ink": "#d71920", "label": "Coca-Cola"},
        "diet coke": {"c1": "#c9ccd1", "c2": "#ffffff", "c3": "#d71920", "ink": "#d71920", "label": "Diet Coke"},
        "coke zero": {"c1": "#1b1b1b", "c2": "#d71920", "c3": "#d71920", "ink": "#ffffff", "label": "Coke Zero"},
        "pepsi": {"c1": "#1d3f94", "c2": "#ffffff", "c3": "#d81f2a", "ink": "#1d3f94", "label": "Pepsi"},
        "dr pepper": {"c1": "#7a1e2c", "c2": "#f3e6e8", "c3": "#5b1220", "ink": "#7a1e2c", "label": "Dr Pepper"},
        "sprite": {"c1": "#14a04a", "c2": "#f2f7d0", "c3": "#0d6f33", "ink": "#0d5a2a", "label": "Sprite"},
        "mountain dew": {"c1": "#3d9a2f", "c2": "#e9f7d8", "c3": "#c8102e", "ink": "#1f5a17", "label": "Mtn Dew"},
        "red bull": {"c1": "#2b4c9b", "c2": "#c9ccd1", "c3": "#d71920", "ink": "#1d2f63", "label": "Red Bull"},
        "monster": {"c1": "#1b1b1b", "c2": "#2b2b2b", "c3": "#62c32e", "ink": "#62c32e", "label": "Monster"},
        "bubly": {"c1": "#7a2a8c", "c2": "#c9418a", "c3": "#d9367c", "ink": "#ffffff", "label": "bubly"},
        "recess": {"c1": "#e9c9a8", "c2": "#f6e8d8", "c3": "#c58a5a", "ink": "#6b3a1a", "label": "Recess"},
        "gorgie": {"c1": "#47a3da", "c2": "#e8f4fb", "c3": "#1f6fa8", "ink": "#0f4c75", "label": "GORGIE"},
        "vitaminwater": {"c1": "#33262a", "c2": "#e8edf1", "c3": "#dde3e8", "ink": "#2a2f36", "label": "vitamin"},
        "snapple": {"c1": "#f2dd3d", "c2": "#fff8c9", "c3": "#2f60b8", "ink": "#6a5a00", "label": "Snapple"},
        "vita coco": {"c1": "#6cc6d9", "c2": "#f6fdff", "c3": "#2d79bd", "ink": "#1c5f8c", "label": "Vita Coco"},
        "premier protein": {"c1": "#5e3526", "c2": "#f7f1ea", "c3": "#d8321f", "ink": "#4a2418", "label": "Premier"},
        "iced energy": {"c1": "#f3c9b4", "c2": "#fbe7d9", "c3": "#f08a3c", "ink": "#7a3a12", "label": "Iced Energy"},
        "topo chico": {"c1": "#2e8a62", "c2": "#f2f0e6", "c3": "#c9a24a", "ink": "#1d5a3f", "label": "Topo Chico"},
    },
}

# How a lane is drawn: the camera looks down at about 45 degrees.
VF = 0.70          # heights foreshortened in the top-down view
ER = 0.38          # lids and bases are ellipses this flat
M = 0.92           # mm to lane units at the front of the rail
RAIL = 560.0       # mm of rail a full column spans
L_END = 640.0      # mm of rail drawn
KZ = (1 / 0.44 - 1) / 600.0   # perspective: the back of the rail is drawn at 0.44 of the front
VBW, VBH = 100, 206
Y0, YH = 164.0, -62.0
LEAN = 12.0        # lanes lean toward the middle of the shelf
UNKNOWN_SLOTS = 4  # hatched slots drawn behind an unknown count when nothing says how deep the rail is
REASON_CHARS = 140


def _e(value: Any) -> str:
    return "" if value is None else escape(str(value))


def _f(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


# --- Reading ---------------------------------------------------------------------------------------

def lane_products(snap: Mapping, ours: Iterable[str]) -> list[str]:
    """Every product our lanes hold now or the plan puts in them, for `read_products`. Only ours."""
    ours = set(ours)
    found: set[str] = set()
    stock = snap.get("stock") or {}
    if stock.get("ok"):
        found.update(v.get("product") for s, v in stock["data"]["slots"].items() if s in ours)
    counts = snap.get("counts") or {}
    if counts.get("ok"):
        found.update(v.get("product") for s, v in counts["data"].items() if s in ours)
    plan = snap.get("weekly_plan", snap.get("plan")) or {}
    if plan.get("ok") and plan["data"]:
        found.update((s.get("now") or {}).get("product") for s in plan["data"]["slots"] if s["slot"] in ours)
    return sorted(p for p in found if isinstance(p, str) and p)


def read_products(conn: Any, skus: list[str]) -> dict:
    """The catalog rows and counted capacities for `skus`. Delisted products are kept: an old
    receipt can still name one. Overrides for products we don't hold aren't returned."""
    rows = conn.execute("select sku, name, line, flavor, size from catalog_products where sku = any(%s)",
                        (skus,)).fetchall() if skus else []
    raw = config.get_value(conn, "capacity_overrides", {})
    overrides = {}
    if isinstance(raw, dict):
        overrides = {k: v for k, v in raw.items()
                     if k in skus and isinstance(v, int) and not isinstance(v, bool) and v > 0}
    return {"catalog": {r[0]: {"name": r[1], "line": r[2], "flavor": r[3], "size": r[4]} for r in rows},
            "overrides": overrides}


# --- Packages and looks ---------------------------------------------------------------------------

def shape_for_size(size: str | None) -> tuple[str, bool]:
    """The shape key for a catalog size, and whether it matched a package exactly."""
    parsed = capacity.parse_size(size)
    if parsed is None:
        found = re.search(r"\b(bottle|carton|can)s?\b", (size or "").lower())
        return FALLBACK[found.group(1) if found else "can"], False
    container, oz, word, _ = parsed
    best = None
    for key, sp in SHAPES.items():
        if sp["container"] != container or abs(Fraction(str(sp["oz"])) - oz) > Fraction(str(sp["tol"])):
            continue
        if sp.get("word") and sp["word"] != word:
            continue
        score = (0 if sp.get("word") == word else 1, abs(Fraction(str(sp["oz"])) - oz))
        if best is None or score < best[0]:
            best = (score, key)
    return (best[1], True) if best else (FALLBACK[container], False)


# Units a column holds, by the loading-label package, for a product known only by name. The
# chart is `vending/capacity.py`; the 16 oz can is estimated from its width (A20).
PACKAGE_CAPACITY = {"20oz PET": 7, "16oz glass": 7, "16oz PET": 8, "12oz can": 8, "240/250ml can": 11,
                    "14oz PET": 8, "16oz can": 8}


def package_from_name(product: str | None) -> str | None:
    """The loading-label package a product name states, like `20oz PET`, or None when it doesn't say
    clearly enough. Reads names such as `COKE-20OZ`, `Red Bull 250ml can` or `Jarritos 12.5 oz glass`."""
    if not product:
        return None
    text = product.lower()
    found = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)\s*-?\s*(fl\.?\s*oz|oz|ounces?|ml)(?![a-z])", text)
    if not found:
        return None
    size, unit = Decimal(found.group(1)), found.group(2)
    glass = re.search(r"\bglass\b", text) is not None
    can = re.search(r"\bcans?\b", text) is not None
    pet = re.search(r"\b(pet|plastic)\b", text) is not None
    if unit == "ml":
        return "240/250ml can" if size in (240, 250) and not glass and "bottle" not in text else None
    if size == 20 and not glass and not can:
        return "20oz PET"
    if size == 14 and not glass and not can:
        return "14oz PET"
    if size == 16:
        if can and not glass and not pet:
            return "16oz can"
        return "16oz glass" if glass and not pet else "16oz PET" if pet and not glass else None
    if size == 12 and can:
        return "12oz can"
    return None


def _norm(text: Any) -> str:
    return " ".join(str(text or "").lower().split())


def hash_look(key: str, label: str) -> dict[str, str]:
    """A steady look from a hash of the product, for one the look table doesn't list."""
    hue = zlib.crc32(_norm(key).encode("utf-8")) % 360
    return {"c1": f"hsl({hue} 55% 46%)", "c2": f"hsl({hue} 60% 86%)", "c3": f"hsl({(hue + 40) % 360} 60% 38%)",
            "ink": f"hsl({hue} 65% 20%)", "label": label}


def look_for(sku: str, line: str | None = None, flavor: str | None = None, name: str | None = None) -> dict[str, str]:
    """The look for a product: by SKU, then line and flavor, then line, else from a hash of the SKU."""
    for table, key in (("by_sku", sku), ("by_line_flavor", f"{_norm(line)}|{_norm(flavor)}"),
                       ("by_line", _norm(line))):
        if key in LOOKS[table]:
            return LOOKS[table][key]
    words = (line or name or sku or "?").split()
    label = (words[0] if words else "?")[:11]
    return hash_look(sku, label)


class Item:
    """A product as drawn: its shape, look, name and how many fit."""

    def __init__(self, product: str, products: dict | None, counted: bool):
        catalog = (products or {}).get("catalog", {})
        overrides = (products or {}).get("overrides", {})
        row = catalog.get(product)
        self.product = product
        self.from_count = counted and row is None    # free text from a photo or door count
        self.cap_note = None
        if row is not None:
            self.key, _ = shape_for_size(row["size"])
            self.cap = capacity.for_product(product, row["size"], overrides)
            parsed = capacity.parse_size(row["size"])
            if product in overrides:
                self.cap_note = "counted"
            elif self.cap is not None and parsed and parsed[0] == "can" and parsed[1] == 16:
                self.cap_note = "estimated"
            self.look = look_for(product, row["line"], row["flavor"], row["name"])
            self.name = row["name"]
            self.detail = " · ".join(x for x in (row["flavor"], row["size"]) if x)
        else:
            package = package_from_name(product)
            self.key = _PACKAGE_SHAPE.get(package or "", FALLBACK["can"])
            # Without the catalog read, no counted capacity is known either.
            self.cap = overrides.get(product, PACKAGE_CAPACITY.get(package or "")) if products else None
            if product in overrides:
                self.cap_note = "counted"
            self.look = look_for(product, name=product)
            self.name = product
            self.detail = ""
        self.spec = SHAPES[self.key]

    def same(self, other: Item | None) -> bool:
        """The same product: same SKU. Names only decide when one side is free text from a count,
        since two catalog SKUs can share a name (flavours of one line)."""
        if other is None:
            return False
        if self.product == other.product:
            return True
        if self.from_count or other.from_count:
            return _norm(self.name) == _norm(other.name)
        return False

    def pitch(self, cap: int | None) -> float:
        return max(self.spec["d"], RAIL / cap) if cap else float(self.spec["d"])


# --- Package drawings ------------------------------------------------------------------------------

def _band(rx: float, ry: float, y_hi: float, y_lo: float, var: str) -> str:
    return (f'<path style="fill:var({var})" d="M{_f(-rx)} {_f(y_hi)}A{_f(rx)} {_f(ry)} 0 0 0 {_f(rx)} {_f(y_hi)}'
            f'V{_f(y_lo)}A{_f(rx)} {_f(ry)} 0 0 1 {_f(-rx)} {_f(y_lo)}Z"/>')


def _can(sp: dict, vf: float, er: float, detail: bool) -> str:
    rx, height = sp["d"] / 2, sp["h"] * vf
    ry = rx * er
    b0, b1 = sp.get("band", (0.16, 0.6))
    body = f"M{_f(-rx)} {_f(-height)}V0A{_f(rx)} {_f(ry)} 0 0 0 {_f(rx)} 0V{_f(-height)}Z"
    out = [f'<path style="fill:var(--c1)" d="{body}"/>', _band(rx, ry, -height * b1, -height * b0, "--c2"),
           _band(rx, ry, -height * b0, -height * (b0 - 0.07), "--c3"), f'<path fill="url(#ma-cyl)" d="{body}"/>']
    t = rx / 32
    lid = ('<ellipse rx="32" ry="11" style="fill:var(--c1)"/><ellipse rx="32" ry="11" fill="#000" fill-opacity=".18"/>'
           '<ellipse cy="-2" rx="29.5" ry="10" fill="#b5bac2" stroke="#6d737c" stroke-width="1.2"/>')
    if detail:
        lid += ('<ellipse cy="-2" rx="24.5" ry="8" fill="#d4d8de" stroke="#8d939c" stroke-width=".9"/>'
                '<ellipse cy="-2.5" rx="19" ry="6" fill="#bfc4cb"/>'
                '<path d="M-11 -1.5H9L12 -8H-8Z" fill="#9aa0a9" stroke="#6d737c" stroke-width=".8"/>'
                '<ellipse cx="-2" cy="-4" rx="5" ry="1.8" fill="#4a4f57"/>'
                '<ellipse cx="10" cy="2" rx="6.5" ry="2.6" fill="#555b64"/>'
                '<path d="M-26 -4Q-14 -13 2 -12" fill="none" stroke="#fff" stroke-opacity=".6" stroke-width="1.4"'
                ' stroke-linecap="round"/>')
    out.append(f'<g transform="translate(0 {_f(-height)}) scale({t:.3f} {t * er / 0.344:.3f})">{lid}</g>')
    return "".join(out)


def _bottle(sp: dict, vf: float, er: float, detail: bool, glass: bool) -> str:
    rx = sp["d"] / 2
    ry = rx * er
    bh, sh = sp["body_h"] * vf, sp["shoulder_h"] * vf
    rn, rc, ch, height = sp["neck_d"] / 2, sp["cap_d"] / 2, sp["cap_h"] * vf, sp["h"] * vf
    b0, b1 = sp.get("band", (0.12, 0.56))
    top = bh + sh
    body = f"M{_f(-rx)} {_f(-bh)}V0A{_f(rx)} {_f(ry)} 0 0 0 {_f(rx)} 0V{_f(-bh)}Z"
    shoulder = (f"M{_f(-rx)} {_f(-bh)}C{_f(-rx)} {_f(-(bh + .6 * sh))} {_f(-rn)} {_f(-(bh + .4 * sh))} {_f(-rn)} "
                f"{_f(-top)}H{_f(rn)}C{_f(rn)} {_f(-(bh + .4 * sh))} {_f(rx)} {_f(-(bh + .6 * sh))} {_f(rx)} {_f(-bh)}"
                f"A{_f(rx)} {_f(ry)} 0 0 1 {_f(-rx)} {_f(-bh)}Z")
    out = [f'<path style="fill:var(--c1)" d="{body}"/>', _band(rx, ry, -bh * b1, -bh * b0, "--c2"),
           f'<path fill="url(#ma-cyl)" d="{body}"/>', f'<path style="fill:var(--c1)" d="{shoulder}"/>',
           f'<path fill="url(#ma-cyl)" d="{shoulder}"/>']
    neck_top = height - ch
    neck = f'x="{_f(-rn)}" y="{_f(-neck_top)}" width="{_f(2 * rn)}" height="{_f(neck_top - top + 0.5)}"'
    out.append(f'<rect {neck} fill="#fff" fill-opacity="{".12" if glass else ".35"}"/>')
    if glass:
        out.append(f'<rect x="{_f(-rn)}" y="{_f(-neck_top + 4)}" width="{_f(2 * rn)}" '
                   f'height="{_f(.45 * (neck_top - top))}" style="fill:var(--c2)"/><rect {neck} fill="url(#ma-cyl)"/>')
    else:
        out.append(f'<rect x="{_f(-rn * 1.25)}" y="{_f(-top - 4 * vf)}" width="{_f(2 * rn * 1.25)}" '
                   f'height="{_f(3 * vf)}" rx="1" fill="#fff" fill-opacity=".45"/>')
    cap = f'x="{_f(-rc)}" y="{_f(-height)}" width="{_f(2 * rc)}" height="{_f(ch)}" rx="1"'
    out.append(f'<rect {cap} style="fill:var(--c3)"/>')
    if detail and not glass:
        out.extend(f'<path d="M{_f(k * rc)} {_f(-height)}V{_f(-height + ch)}" stroke="#000" stroke-opacity=".28"'
                   f' stroke-width=".9"/>' for k in (-.6, -.2, .2, .6))
    out.append(f'<rect {cap} fill="url(#ma-cyl)"/>')
    lid = f'cy="{_f(-height)}" rx="{_f(rc)}" ry="{_f(rc * er)}"'
    out.append(f'<ellipse {lid} style="fill:var(--c3)"/><ellipse {lid} fill="#fff" fill-opacity=".4"/>')
    if detail:
        shine = (f"M{_f(-rx * .72)} {_f(-bh * .9)}C{_f(-rx * .72)} {_f(-bh - sh * .35)} {_f(-rn * .5)} "
                 f"{_f(-bh - sh * .5)} {_f(-rn * .45)} {_f(-top - 2)}")
        out.append(f'<path d="{shine}" fill="none" stroke="#fff" stroke-opacity="{".7" if glass else ".5"}" '
                   f'stroke-width="{"2.2" if glass else "1.6"}" stroke-linecap="round"/>')
        if not glass:
            out.extend(f'<path d="M{_f(-rx)} {_f(-bh * fy)}A{_f(rx)} {_f(ry)} 0 0 0 {_f(rx)} {_f(-bh * fy)}" '
                       f'fill="none" stroke="#000" stroke-opacity=".22"/>' for fy in (.08, .62))
    return "".join(out)


def _carton(sp: dict, vf: float, er: float, detail: bool) -> str:
    rx, gable = sp["d"] / 2, sp["gable_h"] * vf
    hb = sp["h"] * vf - gable
    rc, ch = sp["cap_d"] / 2, sp["cap_h"] * vf
    b0, b1 = sp.get("band", (0.12, 0.62))
    roof = f"M{_f(-rx)} {_f(-hb)}L{_f(-rx * .55)} {_f(-hb - gable)}H{_f(rx * .55)}L{_f(rx)} {_f(-hb)}Z"
    cy = -hb - gable - ch + 1
    return (f'<rect x="{_f(-rx)}" y="{_f(-hb)}" width="{_f(2 * rx)}" height="{_f(hb)}" style="fill:var(--c1)"/>'
            f'<rect x="{_f(-rx)}" y="{_f(-hb * b1)}" width="{_f(2 * rx)}" height="{_f(hb * (b1 - b0))}" '
            f'style="fill:var(--c2)"/><rect x="{_f(-rx)}" y="{_f(-hb)}" width="{_f(2 * rx)}" height="{_f(hb)}" '
            f'fill="url(#ma-box)"/><path style="fill:var(--c1)" d="{roof}"/><path fill="#fff" fill-opacity=".24" '
            f'd="{roof}"/><rect x="{_f(-rc)}" y="{_f(cy)}" width="{_f(2 * rc)}" height="{_f(ch)}" rx="1" '
            f'style="fill:var(--c3)"/><ellipse cy="{_f(cy)}" rx="{_f(rc)}" ry="{_f(rc * er)}" style="fill:var(--c3)"/>')


def _draw(sp: dict, vf: float, er: float, detail: bool) -> str:
    family = sp["family"]
    if family == "can":
        return _can(sp, vf, er, detail)
    if family == "carton":
        return _carton(sp, vf, er, detail)
    return _bottle(sp, vf, er, detail, glass=family == "glass")


def _silhouette(sp: dict) -> str:
    """The dashed outline of a package, for an empty slot (stroke and fill come from CSS)."""
    rx = sp["d"] / 2
    ry, height = rx * ER, sp["h"] * VF
    nss = 'vector-effect="non-scaling-stroke"'
    if sp["family"] == "can":
        return (f'<path {nss} d="M{_f(-rx)} {_f(-height)}V0A{_f(rx)} {_f(ry)} 0 0 0 {_f(rx)} 0V{_f(-height)}"/>'
                f'<ellipse {nss} cy="{_f(-height)}" rx="{_f(rx)}" ry="{_f(ry)}"/>')
    if sp["family"] == "carton":
        hb = height - sp["gable_h"] * VF
        return (f'<path {nss} d="M{_f(-rx)} {_f(-hb)}V0H{_f(rx)}V{_f(-hb)}L{_f(rx * .55)} {_f(-height)}'
                f'H{_f(-rx * .55)}Z"/>')
    bh, sh = sp["body_h"] * VF, sp["shoulder_h"] * VF
    rn, rc = sp["neck_d"] / 2, sp["cap_d"] / 2
    top = bh + sh
    return (f'<path {nss} d="M{_f(-rx)} {_f(-bh)}V0A{_f(rx)} {_f(ry)} 0 0 0 {_f(rx)} 0V{_f(-bh)}'
            f'C{_f(rx)} {_f(-(bh + .6 * sh))} {_f(rn)} {_f(-(bh + .4 * sh))} {_f(rn)} {_f(-top)}V{_f(-height)}'
            f'M{_f(-rx)} {_f(-bh)}C{_f(-rx)} {_f(-(bh + .6 * sh))} {_f(-rn)} {_f(-(bh + .4 * sh))} {_f(-rn)} '
            f'{_f(-top)}V{_f(-height)}"/><ellipse {nss} cy="{_f(-height)}" rx="{_f(rc)}" ry="{_f(rc * ER)}"/>')


_GRADIENTS = (
    '<linearGradient id="ma-cyl" x1="0" x2="1" y1="0" y2="0"><stop offset="0" stop-color="#000" stop-opacity=".5"/>'
    '<stop offset=".16" stop-color="#000" stop-opacity=".12"/><stop offset=".3" stop-color="#fff" stop-opacity=".34"/>'
    '<stop offset=".42" stop-color="#fff" stop-opacity=".06"/><stop offset=".78" stop-color="#000" stop-opacity=".2"/>'
    '<stop offset="1" stop-color="#000" stop-opacity=".55"/></linearGradient>'
    '<linearGradient id="ma-box" x1="0" x2="1" y1="0" y2="0"><stop offset="0" stop-color="#000" stop-opacity=".28"/>'
    '<stop offset=".12" stop-color="#fff" stop-opacity=".12"/><stop offset=".5" stop-color="#fff" stop-opacity="0"/>'
    '<stop offset="1" stop-color="#000" stop-opacity=".34"/></linearGradient>'
    '<linearGradient id="ma-fog" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#05070a" stop-opacity=".62"/>'
    '<stop offset=".5" stop-color="#05070a" stop-opacity=".16"/><stop offset="1" stop-color="#05070a" stop-opacity="0"/>'
    '</linearGradient><linearGradient id="ma-ledge-g" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#9aa2ad"/>'
    '<stop offset=".12" stop-color="#5c636d"/><stop offset="1" stop-color="#23272d"/></linearGradient>'
    '<linearGradient id="ma-glass" x1="0" x2="1" y1="0" y2="1"><stop offset="0" stop-color="#26303a"/>'
    '<stop offset="1" stop-color="#0d1115"/></linearGradient>'
    '<pattern id="ma-hatch" width="4" height="4" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
    '<rect width="4" height="4" fill="#f3bb52" fill-opacity=".14"/><line y2="4" stroke="#f3bb52" stroke-width="1.5"/>'
    '</pattern><pattern id="ma-grey" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
    '<rect width="5" height="5" fill="#1b2027"/><line y2="5" stroke="#2c333d" stroke-width="1.6"/></pattern>')


def _defs(keys: Iterable[str], columns: Iterable[int]) -> str:
    """Gradients, the rails of the columns drawn and the ledge, and the drawings of only the packages
    this page uses, once each, for `<use>`."""
    parts = [_GRADIENTS, f'<g id="ma-ledge">{_LEDGE}</g>']
    parts += [f'<g id="ma-rail-{c}">{_rail_art(c)}</g>' for c in sorted(set(columns))]
    for key in sorted(set(keys)):
        sp = SHAPES[key]
        parts.append(f'<g id="ma-p-{key}">{_draw(sp, VF, ER, True)}</g><g id="ma-f-{key}">{_draw(sp, 1.0, 0.10, False)}'
                     f'</g><g id="ma-s-{key}">{_silhouette(sp)}</g>')
    return (f'<svg class="ma-defs" width="0" height="0" aria-hidden="true" focusable="false"><defs>{"".join(parts)}'
            '</defs></svg>')


# --- One lane ---------------------------------------------------------------------------------------

def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def money(value: Any) -> str | None:
    """A price as exact dollars text, "$1.25"; None when there is none. Never a float."""
    if value is None:
        return None
    try:
        if isinstance(value, Fraction):
            with localcontext() as ctx:
                ctx.prec = 40
                amount = Decimal(value.numerator) / Decimal(value.denominator)
            if amount != amount.quantize(Decimal("0.01")):
                amount = amount.quantize(Decimal("0.01"), ROUND_HALF_UP)
        else:
            amount = Decimal(str(value))
    except InvalidOperation:
        return f"${value}"
    if not amount.is_finite():
        return f"${value}"
    return f"${amount.quantize(Decimal('0.01'))}" if amount == amount.quantize(Decimal("0.01")) else f"${amount}"


def _day(when: Any) -> str:
    """A count's date in Eastern, the machine's zone, like "14 Nov"."""
    try:
        from zoneinfo import ZoneInfo
        when = when.astimezone(ZoneInfo("America/New_York"))
    except Exception:   # no IANA database, or not an aware datetime: the date as stored
        pass
    return f"{when:%d %b}".lstrip("0") if hasattr(when, "strftime") else str(when)


def _cut(text: Any, limit: int = REASON_CHARS) -> str:
    """One line of at most `limit` characters."""
    line = " ".join(str(text).split())
    return line if len(line) <= limit else line[:limit - 1].rstrip() + "…"


class Lane:
    """One of our selections, from `machine_view`'s entry for it and the products read."""

    def __init__(self, slot: str, view: dict, products: dict | None):
        now, nxt = view["now"], view["next"]
        self.slot, self.view, self.change = slot, view, view.get("change")
        self.shelf, self.col = (int(slot[0]), int(slot[1])) if len(slot) == 2 and slot.isdigit() else (0, 0)
        self.source = now["source"]
        self.counted = self.source in ("photo", "door")
        self.units = _int(now["units"])
        self.counted_empty = self.counted and now["product"] is None and now["units"] == 0
        self.now_item = Item(now["product"], products, self.counted) if now["product"] else None
        self.price = now["price"]
        self.next = nxt
        self.fill = _int(view["fill_to"])
        self.next_item = self.now_item
        self.swap = False      # Friday replaces the product in the lane with another
        self.new = False       # Friday puts a product into a lane with none on record
        self.pulled = bool(nxt and nxt["pulled"])
        self.next_units = self.units
        if nxt is not None and nxt["in_plan"]:
            self.next_units = 0 if self.pulled else _int(nxt["units"])
            if nxt["product"] and not self.pulled:
                planned = Item(nxt["product"], products, False) if nxt["product"] != now["product"] else self.now_item
                if planned is not self.now_item and not planned.same(self.now_item):
                    self.swap, self.new = self.now_item is not None, self.now_item is None
                    self.next_item = planned
        self.cap = self._cap(self.now_item, self.units)
        self.next_cap = self._cap(self.next_item, self.next_units)

    @staticmethod
    def _cap(item: Item | None, units: int | None) -> int | None:
        if item is None or item.cap is None:
            return None
        return max(item.cap, units or 0)

    @property
    def back(self) -> int | None:
        """How many go back Friday: the plan's fill less the units on hand, when both are known."""
        if self.fill is None or self.units is None or self.swap or self.pulled:
            return None
        return max(self.fill - self.units, 0)

    @property
    def added(self) -> int | None:
        """Units that go into the lane Friday: the refill, or all of a swap's or a new product's."""
        if self.swap or self.new:
            return self.next_units
        return self.back

    def count_text(self, units: int | None, cap: int | None, item: Item | None, estimate: bool) -> str:
        if item is None and not units:
            return "<b>0</b> empty" if units == 0 else "<b>?</b> no record yet"
        shown = "?" if units is None else (f"~{units}" if estimate else str(units))
        tail = f" of {cap}" if cap else " on hand" if units is not None else " on hand, count unknown"
        return f"<b>{shown}</b>{tail}"

    def aria(self) -> str:
        parts = [f"Selection {self.slot}"]
        item = self.now_item
        if self.counted_empty:
            parts.append("counted empty")
        elif item is None:
            parts.append("no product on record")
        else:
            parts.append(item.name)
            if self.units is None:
                parts.append("units unknown")
            else:
                about = "about " if self.source == "photo" else ""
                parts.append(f"{about}{self.units} on hand" + (f" of {self.cap}" if self.cap else ""))
        parts.append(f"price {money(self.price)}" if self.price is not None else "price unknown")
        nxt = self.next
        if nxt is None:
            parts.append("no plan yet")
        elif not nxt["in_plan"]:
            parts.append("not in the plan")
        elif self.pulled:
            parts.append("pulled on Friday")
        elif (self.swap or self.new) and self.next_item:
            parts.append(f"{'swapped' if self.swap else 'loaded'} Friday to {self.next_item.name}"
                         + (f", {self.next_units} loaded" if self.next_units is not None else ""))
        elif self.fill is not None:
            back = self.back
            parts.append(f"filled to {self.fill} on Friday"
                         + ("" if back is None else f", {back} go back" if back else ", nothing to add"))
        if nxt and nxt["old_price"] is not None:
            parts.append(f"price {money(nxt['old_price'])} to {money(nxt['price'])} Friday")
        if self.change and self.change.get("reason"):
            parts.append(f"reason: {_cut(self.change['reason'])}")
        return ". ".join(parts) + "."


def _rail(col: int) -> tuple:
    def s(z: float) -> float:
        return 1 / (1 + KZ * z)

    def xc(z: float) -> float:
        return 50 + (3.5 - (col - 1)) * LEAN * (1 - s(z))

    def yb(z: float) -> float:
        return YH + (Y0 - YH) * s(z)
    return s, xc, yb


def _rail_art(col: int) -> str:
    s, xc, _ = _rail(col)
    z0 = -25.0

    def edge(z: float, half: float) -> str:
        return f"{xc(z) + half * s(z):.1f},{YH + (Y0 + 12 - YH) * s(z):.1f}"
    out = [f'<path d="M{edge(z0, -48)} L{edge(L_END, -48)} L{edge(L_END, 48)} L{edge(z0, 48)}Z" fill="#15191f"/>']
    out += [f'<path d="M{edge(z0, off)} L{edge(L_END, off)}" stroke="#fff" stroke-opacity=".09" stroke-width="1.2"/>'
            for off in (-16, 16)]
    for side in (-1, 1):
        x0, y0 = xc(z0) + side * 47 * s(z0), YH + (Y0 + 12 - YH) * s(z0)
        x1, y1 = xc(L_END) + side * 47 * s(L_END), YH + (Y0 + 12 - YH) * s(L_END)
        out.append(f'<path d="M{x0:.1f},{y0:.1f}L{x1:.1f},{y1:.1f}L{x1:.1f},{y1 - 42 * s(L_END):.1f}'
                   f'L{x0:.1f},{y0 - 42 * s(z0):.1f}Z" fill="#cfe4ff" fill-opacity=".05" stroke="#cfe4ff" '
                   f'stroke-opacity=".28" stroke-width=".8"/>')
    return "".join(out)


# The front of every lane: the retainer ears, the metal ledge and the number plate.
_LEDGE = "".join(
    f'<path d="M{50 + side * 47},176V132Q{50 + side * 47},127 {50 + side * 42},127H{50 + side * 38}" fill="none" '
    f'stroke="#cfe4ff" stroke-opacity=".5" stroke-width="1.6" stroke-linecap="round"/>' for side in (-1, 1)
) + (f'<rect y="176" width="{VBW}" height="{VBH - 176}" fill="url(#ma-ledge-g)"/>'
     '<rect x="6" y="183" width="27" height="16" rx="2.5" fill="#07080a" stroke="#6b727c" stroke-width=".8"/>')


def _label_text(item: Item) -> str:
    sp, label = item.spec, item.look["label"]
    d = sp["d"]
    size = min(d * 0.23, d * 0.84 / (0.56 * max(len(label), 1)))
    b0, b1 = sp.get("band", (0.14, 0.6))
    if sp["family"] in ("bottle", "glass"):
        base = sp["body_h"] * VF
    elif sp["family"] == "carton":
        base = (sp["h"] - sp["gable_h"]) * VF
    else:
        base = sp["h"] * VF
    y = -base * (b0 + b1) / 2 + size * 0.34
    return (f'<text y="{y:.1f}" text-anchor="middle" font-size="{size:.1f}" font-weight="800" '
            f'fill="{_e(item.look["ink"])}" font-family="Arial Black,Arial,sans-serif">{_e(label)}</text>')


def _style(item: Item) -> str:
    v = item.look
    return _e(f"--c1:{v['c1']};--c2:{v['c2']};--c3:{v['c3']}")


def _badge(x: float, y: float, text: str, cls: str, fill: str, ink: str) -> str:
    r = 7.5 if len(text) < 2 else 9.5
    return (f'<g class="{cls}"><circle cx="{x:.0f}" cy="{y:.1f}" r="{r}" fill="{fill}" stroke="#05070a"/>'
            f'<text x="{x:.0f}" y="{y + 3.6:.1f}" class="ma-tx" font-size="10.5" fill="{ink}">{_e(text)}</text></g>')


def _layer(lane: Lane, item: Item, have: int | None, want: int | None, cap: int | None, estimate: bool,
           animate: bool) -> str:
    """The packages on one rail. `have` is on the rail now (None: unknown), `want` after Friday
    (None: the same as now). With `animate`, the slots Friday fills drop in on the Next Friday view."""
    s, xc, yb = _rail(lane.col)
    pitch = item.pitch(cap)
    unknown = have is None
    after = have if want is None else want
    n = max(cap or 0, after or 0, have or 0, 1, UNKNOWN_SLOTS if unknown and not cap and want is None else 0)
    style = _style(item)

    def at(z: float) -> str:
        return f"translate({xc(z):.1f} {yb(z):.1f}) scale({M * s(z):.4f})"
    out = []
    for i in range(n - 1, -1, -1):
        z = i * pitch
        if i == 0:
            out.append(f'<rect y="-24" width="{VBW}" height="200" fill="url(#ma-fog)"/>')
        now_there = (i == 0) if unknown else i < have
        next_there = (i < after) if after is not None else now_there
        if not animate:
            next_there = now_there
        label = _label_text(item) if i == 0 else ""
        pkg = f'<g transform="{at(z)}"><use href="#ma-p-{item.key}" style="{style}"/>{label}</g>'
        if now_there and next_there:
            out.append(pkg)
        elif now_there:
            out.append(f'<g class="ma-out">{pkg}</g>')
        else:
            if unknown and (cap is None or i < cap):
                out.append(f'<use class="ma-unk{" ma-go" if next_there else ""}" href="#ma-s-{item.key}" '
                           f'transform="{at(z)}"/>')
            elif cap is None or i < cap or next_there:
                out.append(f'<use class="ma-ghost{" ma-go" if next_there else ""}" href="#ma-s-{item.key}" '
                           f'transform="{at(z)}"/>')
            if next_there:
                out.append(f'<g class="ma-fill" style="--i:{i - (0 if unknown else have)}">{pkg}</g>')

    def top(i: int) -> float:
        return max(yb(i * pitch) - item.spec["h"] * VF * M * s(i * pitch), -14)
    if unknown:
        out.append(f'<text class="{"ma-out" if animate and want is not None else ""}" x="{xc(2.2 * pitch):.1f}" '
                   f'y="{yb(2.2 * pitch) - 8:.1f}" text-anchor="middle" font-size="34" font-weight="900" '
                   f'fill="#f3bb52" stroke="#05070a" stroke-width="3" paint-order="stroke" '
                   f'font-family="Arial Black,Arial,sans-serif">?</text>')
    elif have:
        cls = "ma-nb v-now" if animate else "ma-nb"
        out.append(_badge(89, top(have - 1), f"~{have}" if estimate else str(have), cls, "#e3e9f0", "#10151b"))
    if animate and after is not None and after > 0:
        out.append(_badge(89, top(after - 1), str(after), "ma-nb v-next", "#52d6a4", "#05281c"))
    return "".join(out)


def _tag(price: Any, x: float = 54, w: float = 40) -> str:
    text = money(price)
    if text is None:
        return (f'<rect x="{x}" y="183" width="{w}" height="16" rx="2.5" fill="url(#ma-hatch)" stroke="#f3bb52" '
                f'stroke-width=".9"/><text x="{x + w / 2}" y="195.4" class="ma-tx" font-size="12" fill="#f3bb52">$?</text>')
    size = 11 if len(text) <= 5 else 9.5
    return (f'<rect x="{x}" y="183" width="{w}" height="16" rx="2.5" fill="#f4f3ee" stroke="#8b8f95" '
            f'stroke-width=".8"/><text x="{x + w / 2}" y="195" class="ma-tx" font-size="{size}" fill="#10151b">'
            f'{_e(text)}</text>')


def _price_change(old: Any, new: Any) -> str:
    """Friday's tag: the old price struck through, then the new one, outlined."""
    return (f'<rect x="35" y="183" width="28" height="16" rx="3" fill="#c9ccd1"/><text x="49" y="194.6" '
            f'text-anchor="middle" font-size="8" font-weight="800" fill="#4a5059" font-family="Arial,sans-serif">'
            f'{_e(money(old))}</text><line x1="37" y1="191.6" x2="61" y2="191.6" stroke="#d6232a" stroke-width="1.6"/>'
            f'<rect x="64" y="183" width="34" height="16" rx="3" fill="#f4f3ee" stroke="#52d6a4" stroke-width="1.8"/>'
            f'<text x="81" y="195" text-anchor="middle" font-size="{10 if len(money(new) or "") <= 5 else 8.5}" '
            f'font-weight="800" fill="#10151b" font-family="Arial,sans-serif">{_e(money(new))}</text>')


def lane_svg(lane: Lane) -> str:
    """The lane seen from above the front edge: rail, packages, ledge, number plate and tag."""
    out = [f'<use href="#ma-rail-{lane.col}"/>']
    estimate = lane.source == "photo"
    item, nxt = lane.now_item, lane.next
    if item is None:
        out.append(f'<rect y="-24" width="{VBW}" height="200" fill="url(#ma-fog)"/>')
        words = ("EMPTY",) if lane.counted_empty else ("NO RECORD", "YET")
        text = "".join(f'<text x="50" y="{110 + 11 * k}" text-anchor="middle" font-size="9" font-weight="800" '
                       f'fill="#8a95a5" font-family="Arial,sans-serif">{w}</text>' for k, w in enumerate(words))
        if lane.new and lane.next_item is not None:
            loaded = _layer(lane, lane.next_item, lane.next_units or 0, None, lane.next_cap, False, False)
            out.append(f'<g class="v-now">{text}</g><g class="v-next">{loaded}</g>')
        else:
            out.append(text)
    elif lane.swap and lane.next_item is not None:
        old = _layer(lane, item, lane.units, None, lane.cap, estimate, False)
        new_units = lane.next_units if lane.next_units is not None else 0
        new = _layer(lane, lane.next_item, new_units, None, lane.next_cap, False, False)
        out.append(f'<g class="v-now">{old}</g><g class="v-next ma-swapin">{new}</g>')
    else:
        want = None
        if nxt is not None and nxt["in_plan"]:
            want = 0 if lane.pulled else lane.next_units
        out.append(_layer(lane, item, lane.units, want, lane.cap, estimate, nxt is not None))
    out.append(f'<use href="#ma-ledge"/><text class="ma-tx" x="19.5" y="195.2" font-size="12.5" fill="#fff">'
               f'{_e(lane.slot)}</text>')
    if nxt is not None and nxt["old_price"] is not None:
        out.append(f'<g class="v-now">{_tag(lane.price)}</g><g class="v-next">{_price_change(nxt["old_price"], nxt["price"])}</g>')
    elif nxt is not None and nxt["price"] is not None and lane.price is None:
        out.append(f'<g class="v-now">{_tag(None)}</g><g class="v-next">{_tag(nxt["price"])}</g>')
    else:
        out.append(_tag(lane.price))
    return (f'<svg viewBox="0 -24 {VBW} {VBH + 24}" aria-hidden="true" focusable="false">' + "".join(out) + "</svg>")


def _chip(text: str, tone: str = "") -> str:
    return f'<span class="ma-chip{" " + tone if tone else ""}">{text}</span>'


def lane_card(lane: Lane, prev: str | None, nxt_slot: str | None, linked: bool) -> str:
    item, nxt = lane.now_item, lane.next
    estimate = lane.source == "photo" and not lane.counted_empty
    now_count = lane.count_text(lane.units, lane.cap, item, estimate)
    # Now: what Friday does to the lane.
    if nxt is None:
        now_chip = _chip("no plan yet")
    elif not nxt["in_plan"]:
        now_chip = _chip("not in plan")
    elif lane.pulled:
        now_chip = _chip("pulled Friday", "bad")
    elif lane.swap and lane.next_item:
        now_chip = _chip("swap Friday", "info")
    elif lane.new and lane.next_item:
        now_chip = _chip("new product Friday", "info")
    elif lane.fill is None:
        now_chip = _chip("price change" if nxt["old_price"] is not None else "keep as is")
    elif lane.units is None:
        now_chip = _chip("+? back Friday", "unk")
    elif lane.back:
        now_chip = _chip(f"+{lane.back} back Friday", "fri")
    else:
        now_chip = _chip(f"plan {lane.fill}, nothing to add")
    if lane.units == 0:
        now_chip = _chip("empty", "bad") + " " + now_chip
    # Next Friday.
    next_item = lane.next_item
    if nxt is None or not nxt["in_plan"]:
        next_count, next_chip = now_count, now_chip
    elif lane.pulled:
        next_count, next_chip = "<b>0</b> pulled", _chip("pulled", "bad")
    else:
        next_count = lane.count_text(lane.next_units, lane.next_cap, next_item, False)
        if lane.swap:
            next_chip = _chip("swap", "info")
        elif lane.new:
            next_chip = _chip("new", "info")
        elif lane.fill is None:
            next_chip = _chip("price change" if nxt["old_price"] is not None else "keep as is")
        elif lane.units is None:
            next_chip = _chip(f"filled to {lane.fill}", "fri")
        elif lane.back:
            next_chip = _chip(f"+{lane.back} added", "fri")
        else:
            next_chip = _chip("nothing to add")
    name_now = ("Empty" if lane.counted_empty else "No record yet") if item is None else item.name
    detail_now = item.detail if item else ""
    notes_now, notes_next = [], []
    if item is not None and item.from_count:
        notes_now.append(f'<p class="ma-nt">named from a {_e(lane.source)} count</p>')
    if lane.counted and lane.view["now"].get("counted_at") is not None:
        when = lane.view["now"]["counted_at"]
        notes_now.append(f'<p class="ma-nt">counted {_e(_day(when))}'
                         + (", less sales since" if lane.source == "photo" else "") + "</p>")
    for target, it, cap in ((notes_now, item, lane.cap), (notes_next, next_item, lane.next_cap)):
        if it is None:
            continue
        if cap is None:
            target.append('<p class="ma-nt unk">capacity not counted yet</p>')
        elif it.cap_note:
            target.append(f'<p class="ma-nt">capacity {cap} ({it.cap_note})</p>')
    if nxt is not None and nxt["old_price"] is not None:
        notes_next.append(f'<p class="ma-nt ma-price"><s>{_e(money(nxt["old_price"]))}</s> &rarr; '
                          f'<b>{_e(money(nxt["price"]))}</b> from Friday</p>')
    reason = (lane.change or {}).get("reason")
    if reason:
        notes_next.append(f'<p class="ma-why" title="{_e(" ".join(str(reason).split()))}">{_e(_cut(reason))}</p>')
    head_next = ""
    if (lane.swap or lane.new) and next_item is not None:
        head_next = (f'<p class="ma-nm v-next">{_e(next_item.name)}'
                     + (f"<span>{_e(next_item.detail)}</span>" if next_item.detail else "") + "</p>")
    head_now = (f'<p class="ma-nm{" v-now" if head_next else ""}">{_e(name_now)}'
                + (f"<span>{_e(detail_now)}</span>" if detail_now else "") + "</p>")
    nav = ""
    if prev or nxt_slot:
        nav = ('<div class="ma-nav">'
               + (f'<a href="#lane-{_e(prev)}" data-lane="{_e(prev)}" aria-label="Previous lane, selection {_e(prev)}">'
                  '&lsaquo;</a>' if prev else '<span></span>')
               + (f'<a href="#lane-{_e(nxt_slot)}" data-lane="{_e(nxt_slot)}" aria-label="Next lane, selection '
                  f'{_e(nxt_slot)}">&rsaquo;</a>' if nxt_slot else '<span></span>') + '</div>')
    plan_link = (f'<a class="ma-plan" href="#slot-{_e(lane.slot)}">plan row</a>' if linked else "")
    classes = ["ma-lane"] + (["ma-changed"] if lane.change else [])
    return (f'<article class="{" ".join(classes)}" id="lane-{_e(lane.slot)}" role="group" '
            f'aria-label="{_e(lane.aria())}">{nav}{lane_svg(lane)}<div class="ma-meta">{head_now}{head_next}'
            f'<p class="ma-ct"><span class="v-now">{now_count}</span><span class="v-next">{next_count}</span></p>'
            f'<p class="ma-chips"><span class="v-now">{now_chip}</span><span class="v-next">{next_chip}</span></p>'
            + (f'<div class="v-now">{"".join(notes_now)}</div>' if notes_now else "")
            + (f'<div class="v-next">{"".join(notes_next)}</div>' if notes_next else "")
            + f"{plan_link}</div></article>")


def _not_ours(slot: str) -> str:
    return (f'<article class="ma-lane ma-notours" id="lane-{_e(slot)}" aria-label="Selection {_e(slot)}: not ours">'
            f'<div><span class="ma-n">{_e(slot)}</span><span class="ma-t">Not ours</span></div></article>')


# --- The machine front, to pick a shelf ----------------------------------------------------------------

FW, FH = 250, 476
GX, GY, GW, PITCH = 14, 24, 222, 78
FS = 0.245   # mm to front-map units: packages keep their true sizes to each other


def _front(lanes: dict[str, Lane], ours: set[str]) -> str:
    out = [f'<svg class="ma-front" viewBox="0 0 {FW} {FH}" aria-hidden="true" focusable="false">'
           f'<rect width="{FW}" height="{FH}" rx="12" fill="#16181c" stroke="#2c3037" stroke-width="2"/>'
           f'<rect x="{GX}" y="{GY}" width="{GW}" height="{PITCH * SHELVES + 8}" rx="3" fill="url(#ma-glass)" '
           f'stroke="#444a54" stroke-width="2"/>']
    width = GW / COLUMNS
    for r in range(1, SHELVES + 1):
        y_top = GY + 4 + PITCH * (r - 1)
        base = y_top + 54
        out.append(f'<rect x="{GX}" y="{base + 1}" width="{GW}" height="6" fill="url(#ma-ledge-g)"/>')
        for c in range(1, COLUMNS + 1):
            slot, x = f"{r}{c}", GX + (c - 1) * width
            if slot not in ours:
                out.append(f'<rect x="{x + 1:.1f}" y="{y_top}" width="{width - 2:.1f}" height="{PITCH - 12}" '
                           f'fill="url(#ma-grey)" opacity=".9"/>')
                continue
            lane = lanes[slot]
            cx = x + width / 2
            at = f"translate({cx:.1f} {base - 1}) scale({FS})"
            for cls, item, units in (("v-now", lane.now_item, lane.units),
                                     ("v-next", lane.next_item if not lane.pulled else None, lane.next_units)):
                if item is None:
                    continue
                if units == 0:
                    out.append(f'<use class="ma-ghost {cls}" href="#ma-s-{item.key}" transform="{at}"/>')
                else:
                    out.append(f'<use class="{cls}" href="#ma-f-{item.key}" style="{_style(item)}" transform="{at}"/>')
            gx, gw, gy = x + 3, width - 6, base + 10
            # A bar needs a known size: the capacity, else the plan's fill. Without either, the
            # lane draws an outlined, empty gauge rather than a bar that would read as full.
            denom = max(lane.cap or 0, lane.fill or 0)
            sized = denom > 0
            denom = max(denom, lane.units or 0, 1)
            out.append(f'<rect x="{gx:.1f}" y="{gy}" width="{gw:.1f}" height="5" rx="2" '
                       + ('fill="url(#ma-hatch)" stroke="#f3bb52" stroke-width=".8"/>' if lane.units is None
                          else 'fill="#fff" fill-opacity=".14"/>' if sized
                          else 'fill="none" stroke="#fff" stroke-opacity=".35" stroke-dasharray="2 2"/>'))
            if lane.units and sized:
                out.append(f'<rect class="v-now" x="{gx:.1f}" y="{gy}" width="{gw * min(lane.units, denom) / denom:.1f}" '
                           f'height="5" rx="2" fill="#e3e9f0"/>')
            after = lane.next_units
            after_size = max(lane.next_cap or 0, lane.fill or 0)
            if after and after_size:
                after_denom = max(after_size, after, 1)
                out.append(f'<rect class="v-next" x="{gx:.1f}" y="{gy}" width="{gw * after / after_denom:.1f}" '
                           f'height="5" rx="2" fill="#52d6a4"/>')
        out.append(f'<rect class="ma-hl ma-hl{r}" x="{GX - 3}" y="{y_top - 3}" width="{GW + 6}" height="{PITCH - 2}" rx="5"/>')
    out.append(f'<path d="M{GX + 10} {GY + 8}L{GX + 70} {GY + 8}L{GX + 20} {GY + PITCH * SHELVES}L{GX + 4} '
               f'{GY + PITCH * SHELVES}Z" fill="#fff" fill-opacity=".05"/>'
               f'<rect x="{GX}" y="{GY + PITCH * SHELVES + 26}" width="{GW}" height="40" rx="5" fill="#07080a" '
               f'stroke="#3a3f48"/></svg>')
    return "".join(out)


# --- The whole close-up --------------------------------------------------------------------------------

def closeup_html(view: dict[str, dict], products: dict | None, linked: bool) -> str:
    """Our lanes, one shelf at a time, beside the machine's front for picking the shelf.

    `view` is `dashboard.machine_view`: our selections only. `products` is the products read
    (None when it failed: packages are then guessed from names and capacity is unknown).
    `linked` puts a link to each lane's plan row.
    """
    ours = set(view)
    lanes = {slot: Lane(slot, v, products) for slot, v in view.items()}
    keys = {i.key for lane in lanes.values() for i in (lane.now_item, lane.next_item) if i is not None}
    rows, panels = [], []
    for r in range(1, SHELVES + 1):
        mine = [f"{r}{c}" for c in range(1, COLUMNS + 1) if f"{r}{c}" in ours]
        top = 100 * (GY + 4 + PITCH * (r - 1) - 3) / FH
        pos = f"top:{top:.2f}%;height:{100 * (PITCH - 2) / FH:.2f}%"
        where = {1: " (top)", SHELVES: " (bottom)"}.get(r, "")
        if mine:
            known = [lanes[s].units for s in mine if lanes[s].units is not None]
            seen = f"{sum(known)}{'+?' if len(known) < len(mine) else ''}" if known else "?"
            backs = [lanes[s].added for s in mine if lanes[s].added is not None]
            open_ = any(lanes[s].fill is not None and lanes[s].added is None and not lanes[s].pulled for s in mine)
            planned = any(lanes[s].fill is not None or lanes[s].swap or lanes[s].new or lanes[s].pulled for s in mine)
            back = f"+{sum(backs)}{'+?' if open_ else ''}" if backs else "+?" if open_ else "+0" if planned else ""
            friday = f'<span class="ma-fr">{back}</span> Friday' if back else "no Friday plan"
            line = f'{seen} on hand &middot; {friday}'
            rows.append(f'<a class="ma-row ma-pick{r}" href="#shelf-{r}" data-shelf="{r}" style="{pos}" '
                        f'aria-label="Shelf {r}: {len(mine)} of {COLUMNS} selections ours">'
                        f'<b>Shelf {r}</b><small>{line}</small></a>')
            cards = []
            for c in range(1, COLUMNS + 1):
                slot = f"{r}{c}"
                if slot not in ours:
                    cards.append(_not_ours(slot))
                    continue
                k = mine.index(slot)
                cards.append(lane_card(lanes[slot], mine[k - 1] if k else None,
                                       mine[k + 1] if k + 1 < len(mine) else None, linked))
            body = (f'<p class="ma-hint">Two lanes at a time: swipe, drag the scroll bar, use &lsaquo; &rsaquo;, '
                    f'or focus the strip and use the arrow keys.</p><div class="ma-stage"><div class="ma-strip" '
                    f'tabindex="0" role="region" aria-label="Shelf {r} lanes, scrolls sideways">{"".join(cards)}'
                    '</div></div>')
            head = f"{len(mine)} of {COLUMNS} selections ours"
        else:
            rows.append(f'<a class="ma-row ma-pick{r} ma-none" href="#shelf-{r}" data-shelf="{r}" style="{pos}" '
                        f'aria-label="Shelf {r}: none ours"><b>Shelf {r}</b><small>Not ours</small></a>')
            body = f'<p class="ma-nonote">None of selections {r}1&ndash;{r}{COLUMNS} are ours.</p>'
            head = "none ours"
        panels.append(f'<div class="ma-shelf" id="shelf-{r}" data-n="{r}" role="group" aria-label="Shelf {r}">'
                      f'<div class="ma-sh"><h3>Shelf {r}{where} &middot; {r}1&ndash;{r}{COLUMNS}</h3>'
                      f'<span class="dim">{head}</span></div>{body}</div>')
    return (f'{_defs(keys, (lane.col for lane in lanes.values()))}<div class="ma">'
            f'<nav class="ma-fm" aria-label="The machine\'s front: pick a shelf"><div class="ma-fmgrid"><div>'
            f'{_front(lanes, ours)}</div><div class="ma-rows">{"".join(rows)}</div></div></nav>'
            f'<div class="ma-shelves">{"".join(panels)}</div></div>')


def default_shelf(view: Mapping[str, Any]) -> int:
    """The first shelf with one of ours on it, shown until a viewer picks another."""
    for r in range(1, SHELVES + 1):
        if any(f"{r}{c}" in view for c in range(1, COLUMNS + 1)):
            return r
    return 1


# The close-up's styles, added to the dashboard's. The cabinet's inside is dark in both themes
# (it is a black machine); the text around it follows the page's theme variables.
STYLE = """
.ma-defs { position:absolute; width:0; height:0; overflow:hidden }
.ma-tx { font-family:Arial, sans-serif; font-weight:800; text-anchor:middle }
.ma { display:grid; grid-template-columns:minmax(0, 1fr); gap:14px; min-width:0 } .ma > * { min-width:0 }
.ma-fmgrid { display:grid; grid-template-columns:minmax(0, 46%) minmax(0, 1fr); gap:8px; max-width:440px }
.ma-front { display:block; width:100%; height:auto }
.ma-rows { position:relative }
.ma-row { position:absolute; left:0; right:0; display:flex; flex-direction:column; justify-content:center; padding:2px 8px;
  border-radius:8px; text-decoration:none; color:var(--ink); border:2px solid transparent; line-height:1.15; overflow:hidden }
.ma-row b { font-size:13px } .ma-row small { font-size:11px; color:var(--muted) } .ma-row .ma-fr { color:var(--ok); font-weight:700 }
.ma-row.ma-none b { color:var(--muted); font-weight:600 }
.ma-row:hover, .ma-row:focus-visible { border-color:var(--line) }
.ma-hl { fill:none; stroke:#52d6a4; stroke-width:2.5; opacity:0 }
.ma-shelf { display:none; min-width:0 }
.ma-sh { display:flex; flex-wrap:wrap; gap:2px 10px; align-items:baseline; margin:0 0 6px }
.ma-sh h3 { margin:0; font-size:15px; color:var(--ink); text-transform:none; letter-spacing:0 }
.ma-hint { display:none; font-size:12px; color:var(--muted); margin:0 0 4px }
.ma-nonote { color:var(--muted); background:var(--sunk); border:1px dashed var(--line); border-radius:10px; padding:16px; margin:0 }
.ma-stage { position:relative; border-radius:12px; overflow:hidden; background:#0b0e12; border:1px solid var(--line) }
.ma-strip { display:flex; overflow-x:auto; scroll-snap-type:x mandatory; overscroll-behavior-x:contain; scrollbar-width:auto;
  scrollbar-color:#6b7684 #1a1f26; background:radial-gradient(120% 90% at 50% 0, #222a33 0, #0b0e12 70%) }
.ma-strip::-webkit-scrollbar { height:12px } .ma-strip::-webkit-scrollbar-track { background:#1a1f26 }
.ma-strip::-webkit-scrollbar-thumb { background:#6b7684; border-radius:6px; border:2px solid #1a1f26 }
.ma-strip:focus-visible, .ma-lane:focus-visible { outline:3px solid #4d8dff; outline-offset:-3px }
.ma-lane { flex:0 0 43%; scroll-snap-align:start; position:relative; border-right:1px solid rgba(255,255,255,.07); color:#e8ecf2;
  min-width:0; scroll-margin:0 }
.ma-lane svg { display:block; width:100%; height:auto }
.ma-notours { flex:0 0 26%; display:flex; align-items:center; justify-content:center; text-align:center; color:#8a95a5;
  background:repeating-linear-gradient(135deg, #1a1f26 0 9px, #12161b 9px 18px); min-height:220px }
.ma-n { display:block; font-size:22px; font-weight:800; color:#a8b2c0 }
.ma-t { font-size:11px; font-weight:700; letter-spacing:.04em; text-transform:uppercase }
.ma-nav { display:none }
.ma-nav a { display:inline-flex; align-items:center; justify-content:center; width:44px; height:44px; border-radius:50%;
  background:rgba(10,13,17,.72); border:1px solid rgba(255,255,255,.35); color:#fff; font-size:26px; line-height:1;
  text-decoration:none; font-weight:700 }
.ma-nav a:hover, .ma-nav a:focus-visible { background:#52d6a4; color:#04221a; outline:none }
.ma-meta { padding:8px 8px 12px; background:rgba(0,0,0,.35); min-height:112px }
.ma-nm { margin:0; font-weight:700; font-size:13px; line-height:1.2; overflow-wrap:anywhere }
.ma-nm span { display:block; font-weight:500; color:#aab4c3; font-size:12px }
.ma-ct { margin:5px 0 4px; font-size:12px; color:#aab4c3 }
.ma-ct b { font-size:24px; line-height:1; color:#fff }
.ma-chips { margin:0 }
.ma-chip { display:inline-block; border-radius:999px; padding:2px 8px; font-size:11px; font-weight:800; background:#24303b; color:#e8ecf2;
  margin:0 2px 2px 0 }
.ma-chip.fri { background:#0f4a37; color:#7ff0c4 } .ma-chip.unk { background:#4a3508; color:#f8cd75 }
.ma-chip.bad { background:#4b1c1f; color:#ffb0a8 } .ma-chip.info { background:#16294a; color:#9cc3ff }
.ma-nt { margin:4px 0 0; font-size:11px; color:#98a4b4 } .ma-nt.unk { color:#f8cd75 }
.ma-price s { color:#ff9187 } .ma-price b { color:#7ff0c4 }
.ma-why { margin:4px 0 0; font-size:11px; color:#d7dee8; font-style:italic; white-space:nowrap; overflow:hidden; text-overflow:ellipsis }
.ma-plan { display:inline-block; margin-top:5px; font-size:11px; color:#9cc3ff }
.vm[data-view="next"] .ma-changed .ma-meta { box-shadow:inset 0 3px 0 #6ea8ff }
.ma-ghost, .ma-unk { stroke-width:1.4; transition:opacity .35s }
.ma-ghost { fill:none; stroke:rgba(200,222,245,.62); stroke-dasharray:3 3 }
.ma-unk { fill:url(#ma-hatch); stroke:#f3bb52 }
.ma-fill { opacity:0; transform:translateY(-22px); transition:opacity .35s calc(var(--i) * 110ms),
  transform .55s cubic-bezier(.2, .9, .3, 1.15) calc(var(--i) * 110ms) }
.ma-out { transition:opacity .35s }
.vm[data-view="next"] .ma-fill { opacity:1; transform:none }
.vm[data-view="next"] .ma-go, .vm[data-view="next"] .ma-out { opacity:0 }
.vm[data-shelf="1"] .ma-shelf[data-n="1"], .vm[data-shelf="2"] .ma-shelf[data-n="2"], .vm[data-shelf="3"] .ma-shelf[data-n="3"],
.vm[data-shelf="4"] .ma-shelf[data-n="4"], .vm[data-shelf="5"] .ma-shelf[data-n="5"], .vm:not([data-js]) .ma-shelf:target { display:block }
.vm:not([data-js]):has(.ma-shelf:target) .ma-shelf:not(:target) { display:none }
.vm[data-shelf="1"] .ma-hl1, .vm[data-shelf="2"] .ma-hl2, .vm[data-shelf="3"] .ma-hl3, .vm[data-shelf="4"] .ma-hl4,
.vm[data-shelf="5"] .ma-hl5 { opacity:1 }
.vm[data-shelf="1"] .ma-pick1, .vm[data-shelf="2"] .ma-pick2, .vm[data-shelf="3"] .ma-pick3, .vm[data-shelf="4"] .ma-pick4,
.vm[data-shelf="5"] .ma-pick5 { border-color:var(--ok); background:var(--surface) }
@media (max-width:819px) {
  .ma-nav { display:flex; position:absolute; top:6px; left:4px; right:4px; justify-content:space-between; z-index:2; pointer-events:none }
  .ma-nav a, .ma-nav span { pointer-events:auto } .ma-nav span { width:44px }
  .ma-fmgrid { max-width:340px } .ma-hint { display:block } .ma-stage { margin:0 -16px; border-radius:0; border-left:0; border-right:0 } }
@media (min-width:820px) {
  .ma-strip { overflow:visible; scroll-snap-type:none } .ma-lane, .ma-notours { flex:1 1 0; scroll-snap-align:none }
  .ma-ct b { font-size:22px } .ma-meta { padding:8px 6px 12px } }
@media (min-width:1180px) { .ma { grid-template-columns:300px minmax(0, 1fr); align-items:start } .ma-fmgrid { max-width:none } }
@media (prefers-reduced-motion:reduce) { .ma-fill, .ma-ghost, .ma-unk, .ma-out { transition:none !important } }
"""
