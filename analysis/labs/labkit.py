"""Shared helpers for the lab builds: default paths, arguments, and writing the two page forms.

Each lab is one page made from a template plus a compact JSON cut of analysis/out/lab_data.json.
The slot list, the machine name and where the list came from are filled into the templates'
{{TOKENS}} from lab_data's scope, so no template names a slot, a slot count or a column.

Two forms are written next to each other:
    index.html       the page body only, for publishing as a claude.ai artifact (the host adds
                     the doctype, <html>, <head> and <body>)
    standalone.html  the same page wrapped in a full HTML document, to open directly in a browser
"""
import argparse
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent          # analysis/labs
ANALYSIS = HERE.parent                          # analysis
LAB_DATA = ANALYSIS / "out" / "lab_data.json"
DATA = ANALYSIS / "data"
OUT = ANALYSIS / "out" / "labs"


def parse_args(name, description):
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("--lab-data", default=str(LAB_DATA), help="lab_data.json written by sales_analysis.py")
    ap.add_argument("--data", default=str(DATA), help="folder with the exported CSVs")
    ap.add_argument("--out", default=str(OUT / name), help="folder for index.html and standalone.html")
    return ap.parse_args()


def load_lab_data(path):
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"no lab data at {p}: run analysis/sales_analysis.py first")
    return json.loads(p.read_text(encoding="utf-8"))


def read(name, fill_from=None):
    """A source file from this folder; with `fill_from` (lab_data's scope), its {{TOKENS}} filled in."""
    text = (HERE / name).read_text(encoding="utf-8")
    return fill(text, fill_from, name) if fill_from is not None else text


def scope_of(d):
    """The slot scope every lab carries: which slots are ours, on which machine, and where that list came from."""
    sc = d.get("scope") or {}
    for k in ("device", "our_slots", "source", "source_line", "machine"):
        if k not in sc:
            raise SystemExit(f"lab_data.json has no scope.{k}: rerun analysis/sales_analysis.py")
    stray = set((d.get("photo_check") or {}).get("photo_stocked") or []) - set(sc["our_slots"])
    if stray:
        raise SystemExit(f"lab_data.json: photo_check.photo_stocked has slots that are not ours: {sorted(stray)}")
    return {k: sc.get(k) for k in ("device", "our_slots", "n_slots", "slots_text", "source", "source_line", "resolved_at", "machine")}


def _and_list(nums):
    nums = list(nums)
    if len(nums) > 1 and nums == list(range(nums[0], nums[-1] + 1)):
        return f"{nums[0]} to {nums[-1]}"
    return str(nums[0]) if len(nums) == 1 else ", ".join(map(str, nums[:-1])) + f" and {nums[-1]}"


def tokens(scope):
    """Text for the {{TOKENS}} in the templates, all from lab_data's scope."""
    slots = scope["our_slots"]
    n = len(slots)
    rows = sorted({int(s[0]) for s in slots})
    cols = sorted({int(s[1]) for s in slots})
    our = f"our {n} slot" + ("" if n == 1 else "s")
    return {
        "DEVICE": scope["device"],
        "N_SLOTS": str(n),
        "N_OTHER": str(scope["machine"]["shelves"] * scope["machine"]["columns"] - n),
        "OUR": our,
        "OUR_CAP": our.capitalize(),
        "SLOT_RANGES": scope["slots_text"],
        "SOURCE_LINE": scope["source_line"],
        "BASE_ROW": str(rows[0]),
        "BASE_ROW_TEXT": "row 1 = top shelf" if rows[0] == 1 else f"row {rows[0]}",
        "BASE_COL": str(cols[0]),
        "OUR_COLS": _and_list(cols),
    }


# Token text lands in HTML and inside JS strings of every quote style, so it may hold only these characters.
SAFE = re.compile(r"[A-Za-z0-9 ,.:;()/#=-]*")


def fill(text, scope, name="template"):
    vals = tokens(scope)
    for k, v in vals.items():
        if not SAFE.fullmatch(v):
            raise SystemExit(f"{name}: the text for {{{{{k}}}}} has a character that is not safe in HTML or JS: {v!r}")
    out = re.sub(r"\{\{([A-Z_]+)\}\}", lambda m: vals[m.group(1)] if m.group(1) in vals else m.group(0), text)
    left = sorted(set(re.findall(r"\{\{([A-Z_]+)\}\}", out)))
    if left:
        raise SystemExit(f"{name}: unknown tokens {left}")
    return out


def standalone(page, title_fallback="Lab"):
    """Wrap an artifact-form page in a full HTML document.

    The templates start with <title>, <meta>, <link> and one <style> block, then the body.
    Everything up to the end of that first style block goes in <head>; the rest in <body>.
    """
    cut = page.index("</style>") + len("</style>")
    head, body = page[:cut], page[cut:].lstrip("\n")
    base = ("<style>html{-webkit-text-size-adjust:100%}"
            "body{margin:0;font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;font-size:16px;line-height:1.5}</style>")
    if "<title>" not in head:
        head = f"<title>{title_fallback}</title>\n" + head
    return ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"{base}\n{head}\n</head>\n<body>\n{body}</body>\n</html>\n")


def write_pages(out_dir, page):
    """Write index.html (artifact form) and standalone.html. LF line endings on every platform."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    idx, std = out / "index.html", out / "standalone.html"
    idx.write_text(page, encoding="utf-8", newline="\n")
    std.write_text(standalone(page), encoding="utf-8", newline="\n")
    return idx, std


def write_json(path, obj, **kw):
    s = json.dumps(obj, separators=(",", ":"), **kw)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(s, encoding="utf-8", newline="\n")
    return s
