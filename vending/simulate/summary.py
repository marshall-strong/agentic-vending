"""Compact summaries of simulated seasons: the only season data kept in git, and what the demo pages show.

    python -m vending.simulate.summary DIR --id ID [--name ...] [--strategy ...] [--source ...] [--out PATH]
    python -m vending.simulate.summary --check        # load every committed summary, or say what is wrong

A season's full detail (`vending.simulate.report`: every run, request, receipt and day of every slot)
runs to thousands of lines, so it stays on the machine that ran the season. `build` cuts it to what a
person compares across seasons: each week's profit, cash and stock-outs, its report card, and the
agent's decisions with their reasons. `--out` defaults to `DIR/summary.json`; to show a season on the
dashboard's demo copy, write it to `SEASONS/<id>.json` and commit only that file.

Every summary says `"simulated": true`. Our slots in a season are the assignment that season recorded
(`assignment`, from its own data), never a list in the code; a season whose assignment wasn't kept has
none. `load` refuses a summary that breaks any of this, so a page can't show one.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vending.slots import DEVICE_ID, problems

SEASONS = Path(__file__).resolve().parent / "seasons"
VERSION = 1
ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")
RULES = ("stock_outs", "slow_sellers", "margin", "spend_vs_plan", "refusals", "guardrails")
GRADES = ("good", "watch", "bad")
REASON_CHARS = 320          # a decision's reason, cut at a sentence where it can be
TOTALS = ("profit_after_model", "units_sold", "units_lost", "revenue_ex_tax", "gross_profit", "model_spend",
          "balance_end", "inventory_at_cost_end", "runs", "submitted", "failed", "qualifying_units_directed")
WEEK = ("week", "saturday", "class_days", "units_sold", "units_lost", "stock_out_slots", "revenue_ex_tax",
        "gross_profit", "model_spend", "profit", "cumulative_profit", "balance", "spendable", "inventory_at_cost")
NUMBERS = ("class_days", "units_sold", "units_lost", "stock_out_slots", "revenue_ex_tax", "gross_profit",
           "model_spend", "profit", "cumulative_profit", "balance", "spendable")


class BadSummary(ValueError):
    """A summary file that can't be shown: unreadable, or missing what every summary must say."""


# --- building one from a season's full detail ---------------------------------------------------

def _short(text: str | None, limit: int = REASON_CHARS) -> str | None:
    """`text` on one line, cut to `limit` characters at the last full sentence (or word) that fits."""
    if not text:
        return None
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("; "))
    return cut[:end + 1] if end > limit // 2 else cut[:cut.rfind(" ")] + " ..."


def _assignment(detail: Path, season: dict) -> list[str] | None:
    """The slots the season ran with: `season.json` names them since the port to main; before it, the
    season's own day-by-day record (`daily_slots.csv`) does, in the order it first lists them."""
    if season.get("assignment"):
        return list(season["assignment"])
    daily = detail / "daily_slots.csv"
    if not daily.exists():
        return None
    seen: dict[str, None] = {}
    with daily.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            seen.setdefault(row["slot"], None)
    return list(seen) or None


def _week(w: dict) -> dict:
    slots = w.get("slots") or {}
    out = {k: w.get(k) for k in WEEK if k != "stock_out_slots"}
    out["stock_out_slots"] = sum(1 for s in slots.values() if (s.get("lost") or 0) >= 0.5)
    out["report_card"] = {k: {"grade": v["grade"], "why": _short(v.get("why"), 200)}
                          for k, v in (w.get("report_card") or {}).items()}
    return {k: out[k] for k in (*WEEK, "report_card")}


def _decision(r: dict) -> dict:
    return {"at": r.get("at"), "kind": r.get("kind"), "status": r.get("status"), "decision": r.get("decision"),
            "cost_usd": round(float(r.get("cost_usd") or 0), 2),
            "reason": _short(r.get("reason") or r.get("detail"))}


def build(detail: Path, season_id: str, name: str | None = None, strategy: str | None = None,
          source: str | None = None, notes: list[str] | tuple[str, ...] = ()) -> dict:
    """The summary of the season whose full detail is in `detail` (what `report.write` wrote there)."""
    season = json.loads((detail / "season.json").read_text(encoding="utf-8"))
    meta_file = detail / "meta.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
    weeks = json.loads((detail / "weeks.json").read_text(encoding="utf-8"))
    runs = json.loads((detail / "runs.json").read_text(encoding="utf-8"))
    model = season.get("model")
    summary = {
        "version": VERSION, "id": season_id, "simulated": True,
        "name": name or meta.get("name") or (f"{model}, {season.get('effort')}" if model else season.get("decider")),
        "strategy": strategy if strategy is not None else meta.get("strategy", ""),
        "built": meta.get("built") or season.get("built"),
        "source": source or str(detail),
        "decider": season.get("decider"), "model": model, "effort": season.get("effort"),
        "seed": season.get("seed"), "run_day": season.get("run_day", "Thu"),
        "assumptions": season.get("assumptions") or {},
        "assignment": _assignment(detail, season),
        "totals": {k: season.get(k) for k in TOTALS},
        "weeks": [_week(w) for w in weeks],
        "decisions": [_decision(r) for r in runs],
        "notes": list(notes),
    }
    check(summary)
    return summary


# --- writing: one line per small object, so a summary stays short ------------------------------

def dumps(value: Any, indent: str = "") -> str:
    """JSON with every object or list that fits in 150 characters on one line."""
    flat = json.dumps(value, ensure_ascii=False)
    if len(flat) + len(indent) <= 150 or not isinstance(value, (dict, list)) or not value:
        return flat
    inner = indent + " "
    if isinstance(value, dict):
        items = [f"{inner}{json.dumps(k)}: {dumps(v, inner)}" for k, v in value.items()]
        return "{\n" + ",\n".join(items) + "\n" + indent + "}"
    return "[\n" + ",\n".join(inner + dumps(v, inner) for v in value) + "\n" + indent + "]"


def write(summary: dict, path: Path) -> None:
    check(summary)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(summary) + "\n", encoding="utf-8", newline="\n")


# --- reading ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Season:
    id: str
    data: dict

    @property
    def name(self) -> str:
        return self.data["name"]

    @property
    def totals(self) -> dict:
        return self.data["totals"]

    @property
    def weeks(self) -> list[dict]:
        return self.data["weeks"]


def check(summary: Any) -> None:
    """Raise `BadSummary` unless `summary` has what every summary must: marked simulated, a known shape,
    numbers where numbers go, and a slot assignment that `vending.slots` would accept (or none)."""
    def bad(why: str) -> BadSummary:
        sid = summary.get("id") if isinstance(summary, dict) else None
        return BadSummary(f"simulation summary {sid or '?'}: {why}")

    if not isinstance(summary, dict):
        raise bad("not a JSON object")
    if summary.get("version") != VERSION:
        raise bad(f"version {summary.get('version')!r}, not {VERSION}")
    if summary.get("simulated") is not True:
        raise bad('it must say "simulated": true')
    if not isinstance(summary.get("id"), str) or not ID.match(summary["id"]):
        raise bad("its id must be lower-case letters, digits and dashes")
    for key in ("name", "strategy", "source"):
        if not isinstance(summary.get(key), str):
            raise bad(f"{key} must be text")
    totals = summary.get("totals")
    if not isinstance(totals, dict) or not isinstance(totals.get("profit_after_model"), (int, float)):
        raise bad("totals must give profit_after_model")
    if summary.get("assignment") is not None:
        wrong = problems(DEVICE_ID, summary["assignment"])
        if wrong:
            raise bad("its assignment isn't one: " + "; ".join(wrong))
    weeks, decisions = summary.get("weeks"), summary.get("decisions")
    if not isinstance(weeks, list) or not isinstance(decisions, list) or not isinstance(summary.get("notes"), list):
        raise bad("weeks, decisions and notes must be lists")
    for w in weeks:
        if not isinstance(w, dict) or not isinstance(w.get("week"), int):
            raise bad("every week needs its number")
        for key in NUMBERS:
            if not isinstance(w.get(key), (int, float)):
                raise bad(f"week {w['week']}: {key} must be a number")
        for rule, card in (w.get("report_card") or {}).items():
            if rule not in RULES or not isinstance(card, dict) or card.get("grade") not in GRADES:
                raise bad(f"week {w['week']}: a report card line is malformed ({rule})")
    for d in decisions:
        if not isinstance(d, dict) or not isinstance(d.get("at"), str):
            raise bad("every decision needs its time")
    sweep = summary.get("sweep")
    if sweep is not None:
        rows = sweep.get("rows") if isinstance(sweep, dict) else None
        if not isinstance(sweep.get("knob") if isinstance(sweep, dict) else None, str) or not isinstance(rows, list) \
                or not all(isinstance(r, dict) and isinstance(r.get("value"), (int, float))
                           and isinstance(r.get("profit_after_model"), (int, float)) for r in rows):
            raise bad("a sweep needs its knob and, per row, a value and profit_after_model")


def load(path: Path) -> Season:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BadSummary(f"simulation summary {path.name}: can't be read ({type(error).__name__})") from error
    check(data)
    if path.stem != data["id"]:
        raise BadSummary(f"simulation summary {path.name}: its id is {data['id']!r}, not the file's name")
    return Season(data["id"], data)


def load_all(directory: Path | None = None) -> list[Season]:
    """Every committed summary, by id. Raises `BadSummary` on the first one that can't be shown."""
    folder = directory or SEASONS
    return [load(p) for p in sorted(folder.glob("*.json"))] if folder.is_dir() else []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.simulate.summary", description=__doc__.splitlines()[0])
    parser.add_argument("detail", nargs="?", type=Path, help="a season's full-detail directory (report.write's)")
    parser.add_argument("--id", help="the season's id, e.g. 2026-11-16-opus-medium-round3")
    parser.add_argument("--name")
    parser.add_argument("--strategy")
    parser.add_argument("--source", help="where the full detail is (default: the directory)")
    parser.add_argument("--note", action="append", default=[], help="a line for the season's notes (repeatable)")
    parser.add_argument("--out", type=Path, help="default DIR/summary.json")
    parser.add_argument("--check", action="store_true", help=f"load every summary in {SEASONS}")
    args = parser.parse_args(argv)
    if args.check:
        try:
            seasons = load_all()
        except BadSummary as error:
            print(error, file=sys.stderr)
            return 1
        for s in seasons:
            print(f"{s.id}: {s.name}, ${s.totals['profit_after_model']:,.2f}, {len(s.weeks)} weeks")
        return 0
    if args.detail is None or not args.id:
        parser.error("give a season's directory and --id (or --check)")
    out = args.out or args.detail / "summary.json"
    write(build(args.detail, args.id, args.name, args.strategy, args.source, args.note), out)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
