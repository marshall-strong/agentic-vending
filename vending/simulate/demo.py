"""Simulated seasons on the dashboard's demo copy: /dashboard/demo/simulations, from files only.

    /dashboard/demo/simulations         every committed season side by side
    /dashboard/demo/simulations/<id>    one season: weeks, report cards, the agent's decisions

`vending.worker` serves these behind the shared password (`vending.gate`), like every page but
/health. They read the compact summaries in `vending/simulate/seasons/` (`vending.simulate.summary`)
and nothing else: this module never imports the database driver or opens a connection, so the live
dashboard's data can't reach a simulation page, and a simulated figure can't reach the live one.

Every page opens with the same banner, `BANNER`, and every title starts with "SIMULATED". GET only:
the worker answers any other method with 405. No scripts, and nothing loaded from anywhere else.
"""

from __future__ import annotations

import sys
from datetime import datetime
from html import escape
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from vending.simulate import summary

ROOT = "/dashboard/demo/simulations"
BANNER = "SIMULATED season: not real sales or money"
HTML = "text/html; charset=utf-8"
CSP = "default-src 'none'; style-src 'unsafe-inline'"
EASTERN = ZoneInfo("America/New_York")

RULE_NAMES = {"stock_outs": "Stock-outs", "slow_sellers": "Slow sellers", "margin": "Margin",
              "spend_vs_plan": "Spend vs plan", "refusals": "Refusals", "guardrails": "Guardrails"}
GRADE_MARK = {"good": "✓ good", "watch": "! watch", "bad": "✕ bad"}


def mounted(path: str) -> bool:
    """Whether `path` (no query, no trailing slash) is one of these pages."""
    return path == ROOT or path.startswith(ROOT + "/")


def respond(target: str) -> tuple[int, str, str, str] | None:
    """(status, content type, body, content security policy) for a GET of `target`, or None when the path
    isn't one of ours. Never raises: a summary that can't be read is a plain error page."""
    path = urlsplit(target).path.rstrip("/")
    if not mounted(path):
        return None
    rest = path[len(ROOT):].strip("/")
    try:
        if not rest:
            return 200, HTML, index(summary.load_all()), CSP
        if "/" in rest or not summary.ID.match(rest):
            return 404, HTML, _not_found(), CSP
        found = summary.SEASONS / f"{rest}.json"
        if not found.is_file():
            return 404, HTML, _not_found(), CSP
        return 200, HTML, season_page(summary.load(found)), CSP
    except summary.BadSummary as error:
        print(f"simulations: {error}", file=sys.stderr, flush=True)
        return 500, HTML, _page("Simulations", "<h1>Simulations</h1><p>A season summary can't be read, so "
                                "these pages are off until it is fixed. The detail is in the container's log.</p>"), CSP
    except Exception as error:  # noqa: BLE001 - a page answers, whatever happened, and never with the error's text
        print(f"simulations: {target!r}: {type(error).__name__}", file=sys.stderr, flush=True)
        return 500, HTML, _page("Simulations", "<h1>Simulations</h1><p>This page hit an error it doesn't handle "
                                "yet. The container's log says which kind.</p>"), CSP


# --- formatting ---------------------------------------------------------------------------------

def _money(value: float | int | None, cents: bool = True) -> str:
    if value is None:
        return "–"
    text = f"${abs(value):,.2f}" if cents else f"${abs(value):,.0f}"
    return ("−" if value < 0 else "") + text


def _num(value: float | int | None, places: int = 0) -> str:
    return "–" if value is None else f"{value:,.{places}f}"


def _when(iso: str | None) -> str:
    if not iso:
        return "–"
    try:
        moment = datetime.fromisoformat(iso).astimezone(EASTERN)
    except ValueError:
        return escape(iso)
    return f"{moment:%a} {moment.day} {moment:%b} {moment:%H:%M}"


def _day(iso: str | None) -> str:
    if not iso:
        return "–"
    moment = datetime.fromisoformat(iso)
    return f"{moment.day} {moment:%b}"


def _who(data: dict) -> str:
    if data.get("decider") == "model":
        return f"{data.get('model')} at {data.get('effort')} effort"
    return {"scripted": "the simple refill rule, no model", "scripted-wide": "the simple rule with one drink per slot"
            }.get(data.get("decider"), str(data.get("decider")))


# --- the page frame -----------------------------------------------------------------------------

STYLE = """
:root { color-scheme: light dark;
  --bg:#f3f5f8; --surface:#fff; --ink:#18202a; --muted:#5d6977; --line:#dfe4ea; --accent:#1f5fd1;
  --ok:#16794a; --ok-soft:#e3f5ea; --warn:#9a5b00; --warn-soft:#fff2d9; --bad:#c0262d; --bad-soft:#fde7e7;
  --s1:#1f5fd1; --s2:#c25e00; --s3:#0f8a7a; --s4:#7c3aed; --rule:#5d6977; --grid:#e7ebf0; --zero:#8a94a3;
  --banner:#ffd23f; --banner-ink:#141414; }
@media (prefers-color-scheme: dark) { :root {
  --bg:#0e1116; --surface:#161b22; --ink:#e6eaf0; --muted:#94a0ae; --line:#2a313b; --accent:#6ea8ff;
  --ok:#4cc38a; --ok-soft:#12301f; --warn:#f0b450; --warn-soft:#3a2a10; --bad:#ff6b6b; --bad-soft:#3d1a1c;
  --s1:#6ea8ff; --s2:#f59e4c; --s3:#3fc7b3; --s4:#b392f0; --rule:#a3adba; --grid:#262d36; --zero:#6b7480; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
a { color:var(--accent); }
.banner { position:sticky; top:0; z-index:10; background:var(--banner); color:var(--banner-ink);
  border-bottom:4px solid var(--banner-ink); padding:10px 16px; text-align:center; }
.banner strong { display:block; font-size:clamp(17px, 3.6vw, 24px); letter-spacing:.02em; text-transform:none; }
.banner span { font-size:13px; }
main { max-width:1180px; margin:0 auto; padding:16px; }
nav { font-size:14px; margin:4px 0 12px; display:flex; gap:16px; flex-wrap:wrap; }
h1 { font-size:26px; margin:8px 0 4px; } h2 { font-size:18px; margin:28px 0 8px; }
.muted { color:var(--muted); }
.card { background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }
.tiles { display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:10px; margin:14px 0; }
.tile b { display:block; font-size:22px; font-variant-numeric:tabular-nums; }
.tile small { color:var(--muted); }
.scroll { overflow-x:auto; background:var(--surface); border:1px solid var(--line); border-radius:10px; }
table { border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; }
th, td { padding:7px 10px; border-bottom:1px solid var(--line); text-align:right; white-space:nowrap; }
th { font-size:13px; color:var(--muted); font-weight:600; background:var(--surface); }
td.l, th.l { text-align:left; } td.wrap { white-space:normal; min-width:15rem; text-align:left; }
tr:last-child td { border-bottom:0; }
.neg { color:var(--bad); }
.chip { display:inline-block; padding:1px 8px; border-radius:999px; font-size:12px; font-weight:600; }
.good { background:var(--ok-soft); color:var(--ok); } .watch { background:var(--warn-soft); color:var(--warn); }
.bad { background:var(--bad-soft); color:var(--bad); }
.chart { width:100%; height:auto; display:block; }
.chart text { fill:var(--muted); font-size:12px; }
@media (max-width:600px) { .chart text, .chart text.end { font-size:22px; } }
.legend { display:flex; gap:6px 18px; flex-wrap:wrap; font-size:14px; margin:8px 0 0; padding:0; list-style:none; }
.headline { margin:4px 0 8px; } .headline h2 { margin:0 0 2px; font-size:20px; }
.small { font-size:13px; margin:2px 0 6px; }
.chart text.end { font-size:13px; font-weight:700; }
.key.dashed { background:repeating-linear-gradient(90deg, var(--rule) 0 5px, transparent 5px 8px) !important; }
.key { display:inline-block; width:12px; height:12px; border-radius:2px; vertical-align:-1px; margin-right:6px; }
details { margin:8px 0; } summary { cursor:pointer; color:var(--accent); }
ul.decisions { list-style:none; padding:0; margin:0; }
ul.decisions li { padding:10px 0; border-bottom:1px solid var(--line); }
ul.decisions li:last-child { border-bottom:0; }
footer { margin:28px 0 8px; font-size:13px; color:var(--muted); }
"""


def _page(title: str, body: str) -> str:
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>SIMULATED · {escape(title)}</title><style>{STYLE}</style></head><body>"
            f'<div class="banner" role="note"><strong>{escape(BANNER)}</strong>'
            "<span>Made-up customers, machine and student worker, run through our real agent code. "
            "Nothing on this page happened.</span></div>"
            f'<main><nav><a href="/">Home</a><a href="{ROOT}">All simulated seasons</a></nav>{body}'
            "<footer>Simulated data from committed season summaries, never from the database. "
            "How these were made and how to rerun one: docs/simulations.md in the repository.</footer>"
            "</main></body></html>")


def _not_found() -> str:
    return _page("Not found", f'<h1>No such simulated season</h1><p><a href="{ROOT}">See every season</a>.</p>')


# --- charts (static SVG, colours from the page's tokens) ----------------------------------------

def _scale(values: list[float]) -> tuple[float, float, float]:
    low, high = min([0.0, *values]), max([0.0, *values])
    span = (high - low) or 1.0
    if -low < span * 0.03:      # a few cents below zero needn't add a whole step to the axis
        low = 0.0
    raw = span / 4
    step = next(s * 10 ** e for e in range(-1, 6) for s in (1, 2, 2.5, 5) if s * 10 ** e >= raw)
    low = (low // step) * step
    high = -((-high) // step) * step
    if high <= low:             # every value the same (a season that made nothing): give the axis one step
        high = low + step
    return low, high, step


def _chart(series: list[tuple[str, str, list[dict], bool]], bars: bool, ends: bool = False) -> str:
    """Cumulative profit as lines (and, with `bars`, each week's profit as bars) for each (name, colour,
    weeks, dashed). With `ends`, each line's last value is written at its end. Weeks run W1 to the
    longest season's last."""
    width, height, left, right, top, bottom = 860, 300, 88, 70 if ends else 16, 14, 34
    n = max(len(w) for _, _, w, _ in series)
    values = [x["cumulative_profit"] for _, _, w, _ in series for x in w]
    if bars:
        values += [x["profit"] for _, _, w, _ in series for x in w]
    low, high, step = _scale(values)
    y = lambda v: top + (high - min(max(v, low), high)) * (height - top - bottom) / (high - low)
    band = (width - left - right) / n
    out = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="Simulated profit by week">']
    t = low
    while t <= high + 1e-9:
        out.append(f'<line x1="{left}" x2="{width - right}" y1="{y(t):.1f}" y2="{y(t):.1f}" style="stroke:var(--grid)"/>'
                   f'<text x="{left - 8}" y="{y(t) + 4:.1f}" text-anchor="end">{_money(t, cents=False)}</text>')
        t += step
    out.append(f'<line x1="{left}" x2="{width - right}" y1="{y(0):.1f}" y2="{y(0):.1f}" style="stroke:var(--zero)"/>')
    for i in range(n):
        out.append(f'<text x="{left + band * (i + .5):.1f}" y="{height - 10}" text-anchor="middle">W{i + 1}</text>')
    labels: list[list] = []
    for name, colour, weeks, dashed in series:
        if bars:
            for i, w in enumerate(weeks):
                y0, y1 = sorted((y(0), y(w["profit"])))
                out.append(f'<rect x="{left + band * i + band * .22:.1f}" y="{y0:.1f}" width="{band * .56:.1f}" '
                           f'height="{max(y1 - y0, 1):.1f}" rx="2" style="fill:{colour};opacity:.35">'
                           f'<title>W{i + 1}: {_money(w["profit"])} profit</title></rect>')
        points = " ".join(f"{left + band * (i + .5):.1f},{y(w['cumulative_profit']):.1f}" for i, w in enumerate(weeks))
        dash = ' stroke-dasharray="7 5"' if dashed else ""
        out.append(f'<polyline points="{points}" fill="none" style="stroke:{colour}" stroke-width="2.5"{dash}/>')
        for i, w in enumerate(weeks):
            out.append(f'<circle cx="{left + band * (i + .5):.1f}" cy="{y(w["cumulative_profit"]):.1f}" r="3.5" '
                       f'style="fill:{colour}"><title>{escape(name)}, W{i + 1}: {_money(w["cumulative_profit"])} '
                       "so far</title></circle>")
        if ends and weeks:
            last = weeks[-1]["cumulative_profit"]
            labels.append([y(last) + 4, colour, _money(last, cents=False), left + band * (len(weeks) - .5) + 8])
    # End labels in value order, pushed apart so none overlaps the next.
    labels.sort(key=lambda label: label[0])
    for k in range(1, len(labels)):
        labels[k][0] = max(labels[k][0], labels[k - 1][0] + 22)
    for at, colour, text, x in labels:
        out.append(f'<text x="{x:.1f}" y="{at:.1f}" class="end" style="fill:{colour}">{text}</text>')
    return "".join(out) + "</svg>"


# --- pages --------------------------------------------------------------------------------------

COLOURS = ("var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)")
RULE_COLOUR = "var(--rule)"      # a season with no model, drawn dashed: the bar the agent should clear


def _ai(t: dict) -> str:
    """A season's AI cost (its model spend), to the cent."""
    return _money(round(t.get("model_spend") or 0, 2))


def headline_chart(seasons: list[summary.Season]) -> str:
    """Slide 13 of the final deck: profit so far, week by week, after the AI cost, for every season with
    weeks, best first. A season with no model is dashed, as the bar the agent should clear."""
    charted = sorted((s for s in seasons if s.weeks), key=lambda s: -s.totals["profit_after_model"])
    if not charted:
        return ""
    series, k = [], 0
    for s in charted:
        rule = s.data.get("decider") != "model"
        series.append((s, RULE_COLOUR if rule else COLOURS[k % len(COLOURS)], rule))
        k += 0 if rule else 1
    legend = "".join(
        f'<li><i class="key{" dashed" if rule else ""}" style="background:{c}"></i>'
        f'<a href="{ROOT}/{s.id}">{escape(s.name)}</a>: <b>{_money(s.totals["profit_after_model"])}</b> after '
        f'<b>{_ai(s.totals)}</b> of AI cost</li>' for s, c, rule in series)
    untracked = [s for s in seasons if not s.weeks]
    missing = ('<p class="muted small">Not charted, totals only: '
               + ", ".join(f'<a href="{ROOT}/{s.id}">{escape(s.name)}</a>' for s in untracked) + ".</p>"
               if untracked else "")
    lines = [(s.name, c, s.weeks, rule) for s, c, rule in series]
    return ('<section class="card headline"><h2>Profit so far, week by week, after AI cost</h2>'
            '<p class="muted small">Cumulative profit of each simulated season, after what its model calls cost. '
            "Dashed: the simple refill rule, no model.</p>"
            f'{_chart(lines, bars=False, ends=True)}<ul class="legend">{legend}</ul>{missing}</section>')


def index(seasons: list[summary.Season]) -> str:
    if not seasons:
        return _page("Simulated seasons", "<h1>Simulated seasons</h1><p>No season summaries are committed yet.</p>")
    rows = []
    for s in sorted(seasons, key=lambda s: -s.totals["profit_after_model"]):
        t = s.totals
        rows.append(
            f'<tr><td class="wrap"><a href="{ROOT}/{s.id}"><b>{escape(s.name)}</b></a>'
            f'<br><small class="muted">{escape(s.data["strategy"])}</small></td>'
            f'<td><b>{_money(t["profit_after_model"])}</b></td><td>{_ai(t)}</td><td>{_num(t.get("units_sold"))}</td>'
            f'<td>{_num(t.get("units_lost"))}</td><td>{len(s.weeks) or "totals only"}</td></tr>')
    body = (
        f"{headline_chart(seasons)}"
        "<h1>Simulated seasons</h1>"
        '<p class="muted">Each season runs our real agent, tools, planner, stock count and ledger from the day-one '
        "fill (Mon 30 Nov) to Wed 3 Feb, on a made-up machine with made-up customers. They compare strategies "
        "with each other. They don't forecast real sales.</p>"
        '<div class="scroll"><table><thead><tr><th class="l">Season and what it tries</th>'
        "<th>Profit after AI cost</th><th>AI cost</th><th>Units sold</th><th>Units lost to empty slots</th>"
        f"<th>Weeks</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>")
    return _page("Simulated seasons", body)


def _tiles(t: dict) -> str:
    tiles = (("Profit after AI cost", _money(t.get("profit_after_model"))),
             ("Units sold", _num(t.get("units_sold"))),
             ("Units lost to empty slots", _num(t.get("units_lost"))),
             ("AI cost (model calls)", _ai(t)),
             ("Cash at the end", _money(t.get("balance_end"))),
             ("Stock at cost at the end", _money(t.get("inventory_at_cost_end"))))
    return '<div class="tiles">' + "".join(
        f'<div class="card tile"><small>{label}</small><b>{value}</b></div>' for label, value in tiles) + "</div>"


def _weeks_table(weeks: list[dict]) -> str:
    rows = []
    for w in weeks:
        neg = lambda v: ' class="neg"' if (v or 0) < 0 else ""
        rows.append(
            f'<tr><td class="l">W{w["week"]} <span class="muted">from Sat {_day(w["saturday"])}</span></td>'
            f'<td>{w["class_days"]}</td><td>{w["units_sold"]}</td><td>{_num(w["units_lost"])}</td>'
            f'<td>{w["stock_out_slots"]}</td><td>{_money(w["revenue_ex_tax"])}</td><td>{_money(w["gross_profit"])}</td>'
            f'<td>{_money(w["model_spend"])}</td><td{neg(w["profit"])}><b>{_money(w["profit"])}</b></td>'
            f'<td{neg(w["cumulative_profit"])}>{_money(w["cumulative_profit"])}</td><td>{_money(w["balance"])}</td>'
            f'<td>{_money(w["spendable"])}</td></tr>')
    return ('<div class="scroll"><table><thead><tr><th class="l">Week</th><th>Class days</th><th>Units sold</th>'
            "<th>Units lost</th><th>Slots that ran out</th><th>Revenue ex tax</th><th>Gross profit</th><th>AI cost</th>"
            "<th>Profit</th><th>So far</th><th>Cash</th><th>Spendable</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>")


def _cards(weeks: list[dict]) -> str:
    head = "".join(f"<th>{name}</th>" for name in RULE_NAMES.values())
    rows, why = [], []
    for w in weeks:
        card = w.get("report_card") or {}
        cells = []
        for rule in RULE_NAMES:
            line = card.get(rule)
            cells.append("<td class=\"muted\">no classes</td>" if line is None else
                         f'<td><span class="chip {line["grade"]}" title="{escape(line.get("why") or "")}">'
                         f'{GRADE_MARK[line["grade"]]}</span></td>')
        rows.append(f'<tr><td class="l">W{w["week"]}</td>{"".join(cells)}</tr>')
        items = "".join(f"<li><b>{RULE_NAMES[r]}, {line['grade']}:</b> {escape(line.get('why') or '')}</li>"
                        for r, line in card.items())
        why.append(f"<li>W{w['week']}<ul>{items}</ul></li>")
    return (f'<div class="scroll"><table><thead><tr><th class="l">Week</th>{head}</tr></thead>'
            f"<tbody>{''.join(rows)}</tbody></table></div>"
            f"<details><summary>Why each grade</summary><ul>{''.join(why)}</ul></details>")


def _decisions(decisions: list[dict]) -> str:
    items = []
    for d in decisions:
        what = d.get("decision") or d.get("status") or ""
        items.append(f'<li><b>{_when(d["at"])}</b> <span class="muted">{escape(str(d.get("kind") or ""))} run, '
                     f'{escape(str(what))}, {_money(d.get("cost_usd"))} of AI cost</span>'
                     f'<br>{escape(d.get("reason") or "No reason recorded.")}</li>')
    return f'<div class="card"><ul class="decisions">{"".join(items)}</ul></div>'


def _sweep(sweep: dict) -> str:
    rows = "".join(f'<tr><td class="l">{r["value"]:g}</td><td>{_money(r["profit_after_model"])}</td>'
                   f'<td>{_num(r.get("units_sold"))}</td><td>{_num(r.get("red_bull"))}</td>'
                   f'<td>{_num(r.get("celsius"))}</td><td>{_num(r.get("mountain_dew_20oz"))}</td></tr>'
                   for r in sweep["rows"])
    return ("<h2>The same rule, four shares</h2>"
            '<div class="scroll"><table><thead><tr><th class="l">Share the Interloper takes</th>'
            "<th>Profit after AI cost</th><th>Units sold</th><th>Red Bull</th><th>Celsius</th>"
            f"<th>Mountain Dew 20 oz</th></tr></thead><tbody>{rows}</tbody></table></div>")


def season_page(s: summary.Season) -> str:
    d = s.data
    assumptions = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in (d.get("assumptions") or {}).items()) or "the defaults"
    slots = (f"{len(d['assignment'])} slots, the assignment this season recorded when it ran"
             if d.get("assignment") else "not kept for this season")
    parts = [f"<h1>{escape(s.name)}</h1>", f'<p class="muted">{escape(d["strategy"])}</p>',
             f'<p class="muted">Decided by {escape(_who(d))}; weekly runs on {escape(str(d.get("run_day")))}; '
             f'seed {escape(str(d.get("seed")))}.</p>', _tiles(s.totals)]
    if s.weeks:
        parts += ['<h2>Profit by week (bars) and so far (line)</h2>',
                  f'<div class="card">{_chart([(s.name, "var(--s1)", s.weeks, False)], bars=True)}</div>',
                  "<h2>Week by week</h2>", _weeks_table(s.weeks),
                  "<h2>Report cards</h2>", _cards(s.weeks)]
    if d.get("sweep"):
        parts.append(_sweep(d["sweep"]))
    if d["decisions"]:
        parts += ["<h2>The agent's decisions, in its words</h2>", _decisions(d["decisions"])]
    notes = "".join(f"<li>{escape(n)}</li>" for n in d["notes"])
    parts.append(f'<h2>About this season</h2><div class="card"><ul>{notes}'
                 f"<li>Assumptions: {escape(assumptions)}.</li><li>Our slots: {escape(slots)}.</li>"
                 f"<li>Built {escape(str(d.get('built') or 'unknown'))}. Full detail: {escape(d['source'])}.</li>"
                 "</ul></div>")
    return _page(s.name, "".join(parts))
