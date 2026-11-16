"""The one-page infographic of the simulated seasons, built from docs/simulations/data/.

    python -m vending.simulate.infographic            # writes docs/simulations/images/infographic.{html,png}
    python -m vending.simulate.infographic --no-png   # the HTML only

Every number comes from a season's `season.json` and `weeks.json`. The words (which seasons to
show, the lessons, what's next) are the constants below: edit them when a season or a fix lands,
then rerun.

The layout is fixed, because the final deck crops the PNG (2400 px wide, 2x): the numbers half
(tiles, chart, table) is y 440-1640 and the lessons half y 1790-2690. Each section has a set height,
and the PNG step measures the sections in Chrome and fails if one has moved. If you change the
layout on purpose, update `SECTIONS` and tell whoever keeps the deck.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "docs" / "simulations" / "data"
OUT = REPO / "docs" / "simulations" / "images"

# --- What to show (edit these) --------------------------------------------------------------------
HERO = "2026-11-16-opus-medium-round3"         # the agent as it stands now
BEFORE = "2026-11-16-opus-medium"              # the agent as first built (round 1)
RULE = "2026-11-16-baseline-wide"              # the fair bar: the rule with the hero's drinks and run day
RULE_NOTE = "the same 15 drinks"               # what makes RULE a fair comparison, for the tiles and notes
# (season, label in the chart and table, colour, dashed). The chart draws the first four; the table all five.
SHOW = [
    (HERO, "Opus, round 3 (Wed)", "var(--s1)", False),
    ("2026-11-16-opus-medium-fixed", "Opus, round 2 (Wed)", "var(--s2)", False),
    (BEFORE, "Opus, as built (Thu)", "var(--s3)", False),
    (RULE, "Rule, 15 drinks (Wed)", "var(--ref)", True),
    ("2026-11-16-baseline-wednesday", "Rule, 5 drinks (Wed)", "#b4b3ad", True),
]
PILOT_SPEND = 0.45 + 4.32     # not archived: round 1's one-week pilot, and round 3's first Opus season,
                              # which ran out of its $4.50 cap on 20 Jan (kept in the write-up)
APPROVED_BUDGET = 25          # dollars: $10 for rounds 1-2, $15 more for round 3 (Kevin, 16 Nov)

LESSONS = [  # (status, tag, title, text); six fit, three a row
    ("fixed", "Fixed · round 2", "It skipped the day-one fill",
     'No receipts looked like "contents unknown", so week 1 sold nothing. The rules card now says the '
     "machine starts empty, and the season ends Wed 3 Feb."),
    ("fixed", "Fixed · round 2", "Thursday missed Amazon's cutoff",
     "Restocks landed Monday. The prompt now gives each store's cutoff, and the runs moved to Wednesday."),
    ("fixed", "Fixed · round 3", "It refilled only once slots were empty",
     "The new top-up rule refills a slot before it would run out by the visit after next. Every slot sold."),
    ("fixed", "Fixed · round 3 (#137)", "Only five drinks could be bought",
     "With the team's 15 Nov sourcing it stocked 13 drinks in 20 slots, at most 2 slots of any one."),
    ("open", "Open · budget", "Runs cost more with more drinks",
     "$0.31 to $0.77 a run. A $4.50 season cap ran out on 20 Jan and lost 121 units; a season needs about $5."),
    ("open", "Open · team decision", "The wildcard slot",
     "One slot up to $8.00 for a riskier bet. Built and reviewed on add_wildcard_slot; #110 item 1."),
]
NEXT = [  # (title, text); three cards
    ("Why the agent won",
     "Same drinks as the rule, but it topped up before slots ran out, tested two $0.25 rises, and "
     "skipped buying on the last day."),
    ("The caveat",
     "Each new drink's demand is borrowed from the fall. More drinks may help less than the simulator says, "
     "so read the gap over the rule, not the total."),
    ("For the team to decide",
     "The wildcard slot (#110 item 1), and the run day: Wednesday meets Amazon's cutoff; keep a run on "
     "go-live day, Thu 3 Dec."),
]

# CSS pixel [top, bottom] of each section at 1200 px wide; the PNG is 2x. The deck's crops depend on these.
SECTIONS = {"tiles": (225, 366), "grid2": (390, 814), "lessons": (830, 1183), "next": (1199, 1326)}
WIDTH, HEIGHT = 1200, 1365
DAY_NAMES = {"Wed": "Wednesday", "Thu": "Thursday"}


# --- The data -------------------------------------------------------------------------------------
@dataclass
class Season:
    id: str
    season: dict
    weeks: list[dict]
    slots_sold: int

    @property
    def profit(self) -> float:
        return float(self.season["profit_after_model"])

    @property
    def is_model(self) -> bool:
        return self.season["decider"] == "model"

    @property
    def day(self) -> str:
        return self.season.get("run_day") or "Thu"      # round 1 predates the field; it ran Thursdays

    @property
    def model_name(self) -> str:
        m = self.season.get("model", "")
        return "Opus" if "opus" in m else "Sonnet" if "sonnet" in m else "Haiku" if "haiku" in m else m

    @property
    def cumulative(self) -> list[float]:
        return [float(w["cumulative_profit"]) for w in self.weeks]


def load(season_id: str) -> Season:
    d = DATA / season_id
    with (d / "daily_slots.csv").open(encoding="utf-8", newline="") as f:
        sold = {r["slot"] for r in csv.DictReader(f) if int(r["sold"] or 0) > 0}
    return Season(season_id, json.loads((d / "season.json").read_text(encoding="utf-8")),
                  json.loads((d / "weeks.json").read_text(encoding="utf-8")), len(sold))


def money(v: float, cents: bool = True) -> str:
    sign = "−" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}" if cents else f"{sign}${abs(v):,.0f}"


def total_spend() -> float:
    return PILOT_SPEND + sum(float(json.loads((d / "season.json").read_text(encoding="utf-8"))["model_spend"])
                             for d in sorted(DATA.iterdir()) if (d / "season.json").exists())


# --- The page -------------------------------------------------------------------------------------
def build() -> str:
    hero, before, rule = load(HERO), load(BEFORE), load(RULE)
    gap = hero.profit - rule.profit
    e = html.escape

    if gap >= 0:
        headline = (f"With the same drinks, our agent beat the simple refill rule by {money(gap, False)} "
                    f"in a full simulated season.")
    else:
        headline = (f"Our agent made {money(hero.profit, False)}, but the simple refill rule with the same "
                    f"drinks still won by {money(-gap, False)}.")

    tiles = [
        (f"{hero.model_name}, round 3", money(hero.profit),
         f"profit after model spend, {hero.day} runs; {money(before.profit)} as first built"),
        (f"Simple rule, {RULE_NOTE}", money(rule.profit),
         f"no model, {rule.day} runs; the agent is {money(abs(gap))} {'ahead' if gap >= 0 else 'behind'}"),
        (f"Units sold, {hero.model_name}", f"{before.season['units_sold']} → {hero.season['units_sold']}",
         f"slots that sold: {before.slots_sold} → {hero.slots_sold} of 20"),
        ("Total API spend", money(total_spend()), f"of ${APPROVED_BUDGET} approved, all seasons and the pilot"),
    ]
    tiles_html = "".join(f'<div class="tile"><div class="label">{e(a)}</div><div class="num">{e(b)}</div>'
                         f'<div class="note">{e(c)}</div></div>' for a, b, c in tiles)

    shown = [(load(sid), name, color, dashed) for sid, name, color, dashed in SHOW]
    drawn = shown[:4]
    legend = "".join(
        f'<span><i class="sw{" dash" if dashed else ""}" style="{"" if dashed else f"background:{c}"}"></i>'
        f'{e(n.rsplit(" (", 1)[0])}</span>' for _, n, c, dashed in drawn)
    series = [{"v": s.cumulative, "color": c, "dash": "6 5" if dashed else "", "w": 3 if s.id == hero.id else 2,
               "end": money(s.cumulative[-1], False)} for s, _, c, dashed in reversed(drawn)]
    rows = []
    for s, name, color, _ in shown:
        spend = float(s.season["model_spend"])
        rows.append(
            f'<tr{" class=\"hero\"" if s.id == hero.id else ""}><td><i class="dot" style="background:{color}"></i>'
            f'{e(name)}</td><td class="r">{money(s.profit)}</td><td class="r">{s.season["units_sold"]}</td>'
            f'<td class="r">{round(float(s.season["units_lost"]))}</td>'
            f'<td class="r">{money(spend) if spend >= 0.005 else "$0"}</td></tr>')
    seasons_n = sum(1 for d in DATA.iterdir() if (d / "season.json").exists())

    lessons_html = "".join(f'<div class="lesson"><span class="tag {st}">{e(tag)}</span><b>{e(t)}</b><p>{e(x)}</p></div>'
                           for st, tag, t, x in LESSONS)
    next_html = "".join(f'<div class="card"><h2>{e(t)}</h2><p>{e(x)}</p></div>' for t, x in NEXT)
    rule_note = (f"The fair comparison is the rule with {RULE_NOTE} and {DAY_NAMES[rule.day]} runs: "
                 f"{money(rule.profit)}. With only the first five drinks, the rule made "
                 f"{money(shown[4][0].profit)}.")
    return TEMPLATE.format(
        headline=e(headline), tiles=tiles_html, legend=legend, rows="".join(rows),
        table_title=f"{len(SHOW)} of {seasons_n} seasons", rule_note=e(rule_note), lessons=lessons_html,
        next=next_html, series=json.dumps(series), w=WIDTH, h=HEIGHT)


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Simulated Season Results</title>
<!-- Generated by python -m vending.simulate.infographic from docs/simulations/data/. Don't edit by hand. -->
<style>
  :root {{
    --surface: #fcfcfb; --card: #ffffff; --line: #e6e5e0;
    --ink: #0b0b0b; --ink-2: #52514e; --ink-3: #7a7975;
    --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a; --ref: #52514e;
    --good: #0a7a3d; --warn: #9a5b00;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--surface); color: var(--ink);
         font: 15px/1.45 "Segoe UI", system-ui, -apple-system, sans-serif; }}
  .page {{ width: {w}px; height: {h}px; padding: 40px 48px 0; overflow: hidden; }}
  .head {{ height: 185px; overflow: hidden; }}
  .kicker {{ font-size: 13px; letter-spacing: .08em; text-transform: uppercase; color: var(--ink-3); margin: 0 0 6px; }}
  h1 {{ font-size: 34px; line-height: 1.15; margin: 0 0 8px; font-weight: 700; }}
  .sub {{ color: var(--ink-2); margin: 0; font-size: 16px; max-width: 900px; }}
  .tiles {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; height: 141px; margin-bottom: 24px; }}
  .tile {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 16px 18px; overflow: hidden; }}
  .tile .label {{ font-size: 13px; color: var(--ink-2); }}
  .tile .num {{ font-size: 32px; font-weight: 700; margin: 2px 0; font-variant-numeric: tabular-nums; }}
  .tile .note {{ font-size: 13px; color: var(--ink-3); }}
  .grid2 {{ display: grid; grid-template-columns: 1.55fr 1fr; gap: 16px; height: 424px; margin-bottom: 16px; }}
  .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 18px 20px; overflow: hidden; }}
  .card h2 {{ font-size: 17px; margin: 0 0 2px; }}
  .card .cap {{ font-size: 13px; color: var(--ink-3); margin: 0 0 10px; }}
  .legend {{ display: flex; gap: 18px; font-size: 13px; color: var(--ink-2); margin: 0 0 6px; flex-wrap: wrap; }}
  .legend span {{ display: inline-flex; align-items: center; gap: 6px; }}
  .sw {{ width: 18px; height: 3px; border-radius: 2px; display: inline-block; }}
  .sw.dash {{ background: repeating-linear-gradient(90deg, var(--ref) 0 5px, transparent 5px 9px); }}
  table {{ width: 100%; border-collapse: collapse; font-size: 14px; font-variant-numeric: tabular-nums; }}
  th {{ text-align: left; font-weight: 600; color: var(--ink-2); font-size: 12px; padding: 0 6px 6px; border-bottom: 1px solid var(--line); }}
  td {{ padding: 7px 6px; border-bottom: 1px solid var(--line); }}
  td.r, th.r {{ text-align: right; }}
  tr.hero td {{ font-weight: 700; }}
  .dot {{ display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 6px; vertical-align: 1px; }}
  .page > .card {{ height: 353px; margin-bottom: 16px; }}
  .lessons {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }}
  .lesson {{ border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px; background: #fafaf8; height: 127px; overflow: hidden; }}
  .lesson b {{ display: block; font-size: 14px; margin-bottom: 3px; }}
  .lesson p {{ margin: 0; font-size: 13px; color: var(--ink-2); }}
  .tag {{ display: inline-block; font-size: 11px; font-weight: 600; padding: 1px 7px; border-radius: 99px; margin-bottom: 6px; }}
  .tag.fixed {{ color: var(--good); background: #e5f4ea; }}
  .tag.open {{ color: var(--warn); background: #fbf0dc; }}
  .next {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; height: 127px; }}
  .next .card p {{ margin: 4px 0 0; font-size: 14px; color: var(--ink-2); }}
  .foot {{ margin-top: 18px; font-size: 12px; color: var(--ink-3); }}
</style>
</head>
<body>
<div class="page">
  <div class="head">
  <p class="kicker">Simulated data · Thu 3 Dec 2026 – Wed 3 Feb 2027 · 20 slots · one seed per season</p>
  <h1>{headline}</h1>
  <p class="sub">We ran the real agent, tools, planner and ledger against a simulated machine, customers and student worker. Nothing touched the DEX bridge or the team's database.</p>
  </div>

  <div class="tiles">{tiles}</div>

  <div class="grid2">
    <div class="card">
      <h2>Profit so far, by ledger week</h2>
      <p class="cap">Cumulative profit after model spend. Weeks run Saturday to Friday. Weeks 4 and 5 are Winter Break.</p>
      <div class="legend">{legend}</div>
      <svg id="chart" width="640" height="300" role="img" aria-label="Cumulative profit by week"></svg>
    </div>
    <div class="card">
      <h2>{table_title}</h2>
      <p class="cap">Profit after model spend, units sold, units lost to empty slots, model spend.</p>
      <table>
        <tr><th>Season</th><th class="r">Profit</th><th class="r">Units</th><th class="r">Lost</th><th class="r">Spend</th></tr>
        {rows}
      </table>
      <p class="cap" style="margin-top:10px">{rule_note}</p>
    </div>
  </div>

  <div class="card">
    <h2>What we learned, and what we changed</h2>
    <p class="cap">Lessons for the prompt, rules and tools.</p>
    <div class="lessons">{lessons}</div>
  </div>

  <div class="next">{next}</div>

  <p class="foot">Source: docs/simulations/2026-11-16-full-competition.md and docs/simulations/data/ (built by python -m vending.simulate.infographic). Simulated seasons, one seed each; don't pick a model on these alone.</p>
</div>
<script>
  const series = {series};
  const W = 640, H = 300, m = {{ l: 44, r: 54, t: 10, b: 30 }};
  const all = series.flatMap(s => s.v), hi = Math.max(100, Math.ceil(Math.max(...all) / 100) * 100);
  const low = Math.min(...all), bot = low < -20 ? Math.floor(low / 100) * 100 : 0, n = series[0].v.length;
  const x = i => m.l + i * (W - m.l - m.r) / (n - 1);
  const y = v => m.t + (hi - v) * (H - m.t - m.b) / (hi - bot + 20);
  const ns = "http://www.w3.org/2000/svg", svg = document.getElementById("chart");
  const el = (t, a, txt) => {{ const e = document.createElementNS(ns, t); for (const k in a) e.setAttribute(k, a[k]); if (txt != null) e.textContent = txt; svg.appendChild(e); return e; }};
  el("rect", {{ x: x(3) - 14, y: m.t, width: x(4) - x(3) + 28, height: H - m.t - m.b, fill: "#f2f1ec" }});
  el("text", {{ x: (x(3) + x(4)) / 2, y: m.t + 14, "text-anchor": "middle", "font-size": 11, fill: "#7a7975" }}, "Winter Break");
  for (let v = bot; v <= hi; v += 100) {{
    el("line", {{ x1: m.l, x2: W - m.r, y1: y(v), y2: y(v), stroke: v === 0 ? "#b4b3ad" : "#ecebe6", "stroke-width": 1 }});
    el("text", {{ x: m.l - 8, y: y(v) + 4, "text-anchor": "end", "font-size": 11, fill: "#7a7975" }}, "$" + v);
  }}
  for (let i = 0; i < n; i++) el("text", {{ x: x(i), y: H - 10, "text-anchor": "middle", "font-size": 11, fill: "#7a7975" }}, "W" + (i + 1));
  const ends = [];
  for (const s of series) {{
    el("polyline", {{ points: s.v.map((v, i) => x(i) + "," + y(v)).join(" "), fill: "none", stroke: s.color, "stroke-width": s.w, "stroke-dasharray": s.dash, "stroke-linejoin": "round", "stroke-linecap": "round" }});
    el("circle", {{ cx: x(n - 1), cy: y(s.v[n - 1]), r: 4, fill: s.color, stroke: "#fff", "stroke-width": 2 }});
    ends.push({{ y: y(s.v[n - 1]), t: s.end }});
  }}
  ends.sort((a, b) => a.y - b.y);
  for (let i = 1; i < ends.length; i++) if (ends[i].y - ends[i - 1].y < 14) ends[i].y = ends[i - 1].y + 14;
  ends.forEach(e => el("text", {{ x: x(n - 1) + 9, y: e.y + 4, "font-size": 12, "font-weight": 600, fill: "#0b0b0b" }}, e.t));
</script>
</body>
</html>
"""

MEASURE = """<script>window.addEventListener("load", () => {
  const q = {tiles: ".tiles", grid2: ".grid2", lessons: ".page > .card", next: ".next"}, o = {};
  for (const [k, s] of Object.entries(q)) { const r = document.querySelector(s).getBoundingClientRect(); o[k] = [Math.round(r.top), Math.round(r.bottom)]; }
  const p = document.createElement("pre"); p.id = "rects"; p.textContent = JSON.stringify(o); document.body.appendChild(p);
});</script></body>"""


def chrome() -> str:
    for c in (shutil.which("chrome"), shutil.which("google-chrome"), shutil.which("msedge"),
              r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"):
        if c and Path(c).exists():
            return c
    raise SystemExit("no Chrome or Edge found for the PNG; use --no-png")


def check_layout(page: str, browser: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "measure.html"
        probe.write_text(page.replace("</body>", MEASURE), encoding="utf-8")
        dom = subprocess.run([browser, "--headless=new", "--disable-gpu", f"--window-size={WIDTH},{HEIGHT}",
                              "--virtual-time-budget=2000", "--dump-dom", probe.as_uri()],
                             capture_output=True, text=True, timeout=120).stdout
    found = re.search(r'<pre id="rects">([^<]*)</pre>', dom)
    if not found:
        raise SystemExit("couldn't measure the page in Chrome")
    rects = {k: tuple(v) for k, v in json.loads(found.group(1)).items()}
    moved = {k: (SECTIONS[k], rects[k]) for k in SECTIONS if any(abs(a - b) > 2 for a, b in zip(SECTIONS[k], rects[k]))}
    if moved:
        raise SystemExit(f"the layout moved, so the deck's crops would be wrong: {moved}")


def render_png(html_path: Path, png_path: Path, browser: str) -> None:
    subprocess.run([browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=2",
                    "--virtual-time-budget=2000", f"--window-size={WIDTH},{HEIGHT}", f"--screenshot={png_path}", html_path.as_uri()],
                   check=True, capture_output=True, timeout=120)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.simulate.infographic", description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--no-png", action="store_true")
    args = parser.parse_args(argv)
    page = build()
    args.out.mkdir(parents=True, exist_ok=True)
    html_path = args.out / "infographic.html"
    html_path.write_text(page, encoding="utf-8", newline="\n")
    print(f"wrote {html_path}")
    if not args.no_png:
        browser = chrome()
        check_layout(page, browser)
        render_png(html_path, args.out / "infographic.png", browser)
        print(f"wrote {args.out / 'infographic.png'} ({WIDTH * 2} px wide; sections unchanged)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
