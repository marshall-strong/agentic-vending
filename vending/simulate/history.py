"""The dashboard's Simulations section: every archived season, read from files, no database.

    /simulations                     every season side by side: profit, units, spend, report cards
    /simulations/ID/                 the agent dashboard as it looked at a moment (?at=...), with a time bar
    /simulations/ID/pnl              the P&L page at the same moment
    /simulations/ID/report           week by week: the table, the report cards, every run's reasons
    /simulations/ID/weeks.json       the season's weekly data, for a script or an agent

A season is a directory under `docs/simulations/data/` holding `meta.json`, `season.json`,
`weeks.json`, `runs.json` and `archive.jsonl.xz` (see `vending.simulate.archive`). `vending.pages`
serves this section on the Container App and `vending.dashboard` locally. Every page says it is
simulated data. Nothing here reads or writes the team's database.
"""

from __future__ import annotations

import html
import json
import lzma
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from vending.simulate import world
from vending.simulate.world import EASTERN

DATA = world.REPO / "docs" / "simulations" / "data"
ROOT = "/simulations"
HTML, JSON = "text/html; charset=utf-8", "application/json"
BANNER = ("Simulated data: our real agent, tools, planner and ledger on a made-up machine, customers and "
          "worker. Not the competition.")


@dataclass(frozen=True)
class Season:
    id: str
    meta: dict
    season: dict
    weeks: list
    runs: list


def seasons(root: Path | None = None) -> list[Season]:
    root = root or DATA
    found = []
    for d in sorted(root.glob("*/")) if root.exists() else []:
        try:
            read = lambda name: json.loads((d / name).read_text(encoding="utf-8"))
            found.append(Season(d.name, read("meta.json"), read("season.json"), read("weeks.json"), read("runs.json")))
        except (OSError, ValueError):
            continue
    return found


@lru_cache(maxsize=4)
def _moments(path: str, mtime: float) -> tuple[list[datetime], list[dict]]:
    rows = [json.loads(line) for line in lzma.open(path, "rt", encoding="utf-8")]
    return [datetime.fromisoformat(r["at"]) for r in rows], rows


def moments(season_dir: Path) -> tuple[list[datetime], list[dict]]:
    path = season_dir / "archive.jsonl.xz"
    return _moments(str(path), path.stat().st_mtime)


def _e(x) -> str:
    return html.escape(str(x))


def _page(title: str, body: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{_e(title)}</title><style>"
            "body{font:14px/1.45 system-ui,sans-serif;margin:24px auto;max-width:1150px;padding:0 16px;color:#18202a}"
            "table{border-collapse:collapse;margin:8px 0 18px}td,th{border:1px solid #dfe4ea;padding:4px 8px;"
            "text-align:right;vertical-align:top}td.l,th.l{text-align:left}.banner{background:#fef3c7;"
            "border:1px solid #d97706;padding:6px 10px;border-radius:6px}.good{background:#e3f5ea}"
            ".watch{background:#fff2d9}.bad{background:#fde7e7;font-weight:600}small{color:#5d6977}"
            "h2{margin-top:28px}a{color:#1f5fd1}"
            "@media (prefers-color-scheme:dark){body{background:#111418;color:#e8eaed}td,th{border-color:#333}"
            ".good{background:#12301f}.watch{background:#3a2c0b}.bad{background:#3d1416}.banner{background:#3a2c0b}"
            "a{color:#8ab4ff}}</style></head>"
            f'<body><p class="banner">{_e(BANNER)}</p>{body}</body></html>')


def _money(v) -> str:
    return "unknown" if v is None else f"${v:,.2f}"


def index() -> str:
    from vending.simulate.report import chart_svg
    found = seasons()
    if not found:
        return _page("Simulations", "<h1>Simulations</h1><p>No archived seasons yet. Run "
                     "<code>python -m vending.simulate</code>, then <code>python -m vending.simulate.archive</code>.</p>")
    rows = "".join(
        f'<tr><td class=l><a href="{ROOT}/{quote(s.id)}/">{_e(s.meta.get("name", s.id))}</a><br>'
        f'<small>{_e(s.meta.get("strategy", ""))}</small></td>'
        f'<td class=l>{_e(s.season.get("model") or "scripted rule")}{" at " + _e(s.season["effort"]) if s.season.get("effort") else ""}</td>'
        f'<td><b>{_money(s.season["profit_after_model"])}</b></td><td>{s.season["units_sold"]}</td>'
        f'<td>{s.season["units_lost"]:.0f}</td><td>{_money(s.season["model_spend"])}</td>'
        f'<td>{_money(s.season["balance_end"])}</td><td>{_money(s.season.get("inventory_at_cost_end"))}</td>'
        f'<td>{s.season["submitted"]} of {s.season["runs"]}</td>'
        f'<td class=l><a href="{ROOT}/{quote(s.id)}/report">report</a> &middot; '
        f'<a href="{ROOT}/{quote(s.id)}/pnl">P&amp;L</a></td></tr>' for s in found)
    n = max(len(s.weeks) for s in found)
    weekly = "".join(
        f"<tr><td class=l>W{i + 1}</td>" + "".join(
            f"<td>{_money(s.weeks[i]['profit'])}</td>" if i < len(s.weeks) else "<td></td>" for s in found) + "</tr>"
        for i in range(n))
    grid = ""
    for s in found:
        cells = "".join(
            "<tr><td class=l>W{}</td>{}</tr>".format(w["week"], "".join(
                f'<td class="{w["report_card"][k]["grade"]}" title="{_e(w["report_card"][k]["why"])}">'
                f'{w["report_card"][k]["grade"]}</td>' if k in w["report_card"] else "<td><small>no classes</small></td>"
                for k in CARD)) for w in s.weeks)
        grid += (f'<h3>{_e(s.meta.get("name", s.id))}</h3><table><tr><th></th>'
                 + "".join(f"<th>{_e(k.replace('_', ' '))}</th>" for k in CARD) + f"</tr>{cells}</table>")
    chart = chart_svg({s.meta.get("name", s.id): {"weeks": s.weeks} for s in found})
    return _page("Simulations", f"""<h1>Simulations</h1>
<p>Each season runs our real weekly agent through a simulated competition, Mon 30 Nov to Wed 3 Feb.
Open one to step through the agent dashboard and the P&amp;L as they would have looked at any moment.
Method and assumptions: <code>docs/simulations/</code>.</p>
<table><tr><th class=l>Season</th><th class=l>Decider</th><th>Profit after model</th><th>Units</th>
<th>Units lost</th><th>Model spend</th><th>Balance at end</th><th>Stock at cost</th><th>Runs submitted</th><th></th></tr>
{rows}</table>
<div style="overflow-x:auto">{chart}</div>
<h2>Profit by week</h2><table><tr><th></th>{''.join(f'<th>{_e(s.meta.get("name", s.id))}</th>' for s in found)}</tr>
{weekly}</table>
<h2>Report cards</h2><p><small>Hover a grade for the reason. The rules are in
<code>vending/simulate/report.py</code>.</small></p>{grid}""")


CARD = ("stock_outs", "slow_sellers", "margin", "spend_vs_plan", "refusals", "guardrails")


def report_page(s: Season) -> str:
    table = "".join(
        f"<tr><td class=l>W{w['week']} <small>Sat {w['saturday'][5:]}</small></td><td>{w['class_days']}</td>"
        f"<td>{w['units_sold']}</td><td>{w['units_lost']:.0f}</td><td>{_money(w['revenue_ex_tax'])}</td>"
        f"<td>{_money(w['gross_profit'])}</td><td>{_money(w['visit_fees'])}</td><td>{_money(w['model_spend'])}</td>"
        f"<td><b>{_money(w['profit'])}</b></td><td>{_money(w['cumulative_profit'])}</td><td>{_money(w['spendable'])}</td></tr>"
        for w in s.weeks)
    weeks = ""
    for w in s.weeks:
        card = "".join(f'<li class="{v["grade"]}"><b>{_e(k.replace("_", " "))}: {_e(v["grade"])}.</b> {_e(v["why"])}</li>'
                       for k, v in w["report_card"].items())
        runs = ""
        for r in w["runs"]:
            reasons = "".join(f"<li>slot {_e(x['slot'])}: {_e(x['reason'])}</li>" for x in r.get("slot_reasons") or [])
            stamp = quote(r["at"][:16])
            runs += (f'<li><a href="{ROOT}/{quote(s.id)}/?at={stamp}">Run {r["run_id"]}</a> ({_e(r["kind"])}, '
                     f'{_e(r["at"][:16].replace("T", " "))}): <b>{_e(r["status"])}</b>, {_money(r["cost_usd"])}. '
                     f'{_e(r.get("reason") or "")}<br><small>{_e(r.get("update") or "")}</small>'
                     f'{"<ul>" + reasons + "</ul>" if reasons else ""}</li>')
        visits = "".join(
            f'<li><a href="{ROOT}/{quote(s.id)}/?at={quote(v["at"][:16])}">Visit {_e(v["request_id"])}</a> '
            f'{_e(v["at"][:16].replace("T", " "))}: paid {_money(float(v["paid"] or 0))}, {v["lines"]} lines, '
            f'{v["unavailable"]} unavailable</li>' for v in w["visits_done"])
        weeks += (f'<h3>W{w["week"]}: from Sat {_e(w["saturday"])} <small>({w["class_days"]} class days)</small></h3>'
                  f'<ul>{card}</ul><ul>{runs}{visits}</ul>')
    return _page(f"{s.meta.get('name', s.id)}: report", f"""
<p><a href="{ROOT}/">All simulations</a> &middot; <a href="{ROOT}/{quote(s.id)}/">Dashboard over time</a></p>
<h1>{_e(s.meta.get("name", s.id))}</h1><p>{_e(s.meta.get("strategy", ""))}</p>
<table><tr><th class=l>Week</th><th>Class days</th><th>Units</th><th>Lost</th><th>Revenue ex tax</th>
<th>Gross profit</th><th>Fees</th><th>Model</th><th>Profit</th><th>Cumulative</th><th>Spendable</th></tr>{table}</table>
<h2>Week by week</h2>{weeks}""")


def _bar(s: Season, times: list[datetime], rows: list[dict], i: int, kind: str) -> str:
    base = f"{ROOT}/{quote(s.id)}/" + ("pnl" if kind == "pnl" else "")
    at = lambda j: quote(rows[j]["at"][:16])
    step = lambda j, label: (f'<a href="{base}?at={at(j)}">{label}</a>' if 0 <= j < len(rows) else
                             f"<span style='opacity:.4'>{label}</span>")
    def day(d: int) -> int:
        """The first moment at least `d` days later (or earlier, for a negative `d`); -1 if none."""
        from datetime import timedelta
        target = times[i] + timedelta(days=d)
        if d > 0:
            return next((j for j in range(i + 1, len(times)) if times[j] >= target), -1)
        return max((j for j in range(i) if times[j] <= target), default=-1)
    options = "".join(f'<option value="{at(j)}"{" selected" if j == i else ""}>'
                      f'{times[j].astimezone(EASTERN):%a %d %b %H:%M} &middot; {_e(r["label"])}</option>'
                      for j, r in enumerate(rows))
    other = (f'<a href="{ROOT}/{quote(s.id)}/?at={at(i)}">Dashboard</a>' if kind == "pnl" else
             f'<a href="{ROOT}/{quote(s.id)}/pnl?at={at(i)}">P&amp;L</a>')
    return (f'<div style="position:sticky;top:0;z-index:999;background:#fef3c7;color:#1f2933;'
            f'border-bottom:2px solid #d97706;padding:6px 12px;font:14px system-ui,sans-serif">'
            f'<b>SIMULATION: {_e(s.meta.get("name", s.id))}</b> &middot; '
            f'<b>{times[i].astimezone(EASTERN):%a %d %b %Y %H:%M} ET</b> ({_e(rows[i]["label"])}) &middot; '
            f'{step(day(-7), "&laquo; week")} {step(day(-1), "&lsaquo; day")} {step(i - 1, "&lsaquo; previous")} '
            f'&middot; {step(i + 1, "next &rsaquo;")} {step(day(1), "day &rsaquo;")} {step(day(7), "week &raquo;")} &middot; '
            f'<select onchange="location.href=\'{base}?at=\'+this.value">{options}</select> &middot; {other} &middot; '
            f'<a href="{ROOT}/{quote(s.id)}/report">season report</a> &middot; <a href="{ROOT}/">all simulations</a>'
            f'<br><input type=range min=0 max={len(rows) - 1} value={i} style="width:100%" '
            f'onchange="location.href=\'{base}?at=\'+[{",".join(repr(r["at"][:16]) for r in rows)}][this.value]"></div>')


def moment_page(s: Season, at: str | None, kind: str) -> str:
    times, rows = moments(DATA / s.id)
    if at:
        moment = datetime.fromisoformat(at)
        moment = moment if moment.tzinfo else moment.replace(tzinfo=EASTERN)
        i = max(bisect_right(times, moment) - 1, 0)
    else:
        i = len(rows) - 1
    page = rows[i]["pnl" if kind == "pnl" else "dashboard"]
    stamp = quote(rows[i]["at"][:16])
    page = (page.replace("@PNL@", f"{ROOT}/{quote(s.id)}/pnl?at={stamp}").replace("@SIMS@", f"{ROOT}/")
            .replace('href="#"', f'href="{ROOT}/{quote(s.id)}/weeks.json"'))
    # The dashboard's script refetches its JSON to stay live; an archived moment never changes, so stop it.
    page = page.replace("setInterval(refresh, REFRESH);", "")
    # The dashboard's script ages every time against the browser's clock ("last poll 24 d ago"); an archived
    # page must age them against its own moment instead.
    frozen = f"<script>Date.now=function(){{return {int(times[i].timestamp() * 1000)};}};</script>"
    bar = frozen + _bar(s, times, rows, i, kind)
    start = page.find("<body")
    end = page.find(">", start) + 1
    return page[:end] + bar + page[end:]


def respond(target: str) -> tuple[int, str, str] | None:
    """(status, content type, body) for a path under /simulations, or None if it isn't one."""
    parts = urlsplit(target)
    path = parts.path.rstrip("/")
    if path != ROOT and not path.startswith(ROOT + "/"):
        return None
    bits = [b for b in path[len(ROOT):].split("/") if b]
    at = (parse_qs(parts.query).get("at") or [None])[0]
    try:
        if not bits:
            return 200, HTML, index()
        found = {s.id: s for s in seasons()}
        s = found.get(bits[0])
        if s is None or len(bits) > 2:
            return 404, HTML, _page("Not found", f'<p>No such simulation. <a href="{ROOT}/">All simulations</a></p>')
        rest = bits[1] if len(bits) > 1 else ""
        if rest == "":
            return 200, HTML, moment_page(s, at, "dashboard")
        if rest == "pnl":
            return 200, HTML, moment_page(s, at, "pnl")
        if rest == "report":
            return 200, HTML, report_page(s)
        if rest == "weeks.json":
            return 200, JSON, json.dumps(s.weeks, indent=1)
        if rest == "season.json":
            return 200, JSON, json.dumps(s.season, indent=1)
        return 404, HTML, _page("Not found", f'<p><a href="{ROOT}/">All simulations</a></p>')
    except (OSError, ValueError, KeyError) as error:
        return 500, HTML, _page("Simulations", f"<p>This season's files couldn't be read ({type(error).__name__}).</p>")
