"""What a simulated season did, week by week, and how it did by the team's rules.

    python -m vending.simulate.report --database-url ... --out DIR      # rebuild from a finished season

`write` reads the season's database (runs, steps, tool calls, model calls, requests, receipts, the
ledger) and the season's own record of what customers did (`State.days`), and writes small files a
person or a script can read: `weeks.json` (with the report cards), `runs.json`, `requests.json`,
`model_calls.csv`, `daily_slots.csv`, `season.json`, `tables.md` and `weekly.svg`.

**The report card** grades each Sat-Fri ledger week on fixed rules, so two seasons can be compared:

| Rule | Good | Watch | Bad |
|---|---|---|---|
| Stock-outs: sales lost to empty slots, as a share of demand | under 5% | 5 to 15% | over 15% |
| Slow sellers: a stocked slot with no sale for 5+ class days in a row, not swapped | none | | any |
| Gross margin: (revenue ex tax - cost of sales - shrinkage) / revenue ex tax | 40% or more | 25 to 40% | under 25% |
| Spend vs plan: spendable at week end against the $53 floor; model spend against $1 a run | both inside | either outside | spendable below $0 |
| Refusals: preview refusals and refused submits in the week's runs | none | fixed in the run | a run ended without its plan |
| Guardrails: a price off the floor-to-ceiling band, a cap reached, the halt hit | none | | any |

Weeks with no class days (the Winter Break) are graded on money and guardrails only.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import psycopg

from vending import ledger
from vending.simulate import world
from vending.simulate.world import EASTERN, Calendar

FLOOR_CENTS = 5300          # docs/opening-capital-plan.md: spendable floor
RUN_PLAN_USD = Decimal("1.00")
CEILING_CENTS = 650


def _d(cents: Any) -> float:
    return round(float(cents) / 100, 2)


def _weeks() -> list[tuple[date, datetime, datetime]]:
    """(Saturday, start, end) for each ledger week the season touches, clipped to the season."""
    out = []
    sat = ledger.week_of(world.LEDGER_OPENS.astimezone(EASTERN))
    while datetime.combine(sat, datetime.min.time(), EASTERN) < world.COMPETITION_END:
        start = max(datetime.combine(sat, datetime.min.time(), EASTERN), world.LEDGER_OPENS)
        end = min(datetime.combine(sat + timedelta(days=7), datetime.min.time(), EASTERN), world.COMPETITION_END)
        out.append((sat, start, end))
        sat += timedelta(days=7)
    return out


def _runs(conn: psycopg.Connection, st) -> list[dict]:
    rows = conn.execute(
        "select id, started_at, status, detail, cost_usd, plan, team_update, notes, request_id from agent_runs"
        " order by id").fetchall()
    by_id = {r.get("run_id"): r for r in st.runs if r.get("run_id")}
    out = []
    for rid, started, status, detail, cost, plan, update, notes, request_id in rows:
        steps = conn.execute("select turn, kind, tool, content, is_error from agent_steps where run_id = %s"
                             " order by id", (rid,)).fetchall()
        calls = [{"turn": t, "tool": tool, "arguments": c[:600]} for t, k, tool, c, e in steps if k == "tool_call"]
        previews = []
        for t, k, tool, c, e in steps:
            if k == "tool_result" and tool == "preview_desired_state":
                previews.append(_preview_summary(c))
        tokens = conn.execute(
            "select count(*), coalesce(sum(input_tokens),0), coalesce(sum(cache_write_tokens),0),"
            " coalesce(sum(cache_read_tokens),0), coalesce(sum(output_tokens),0) from model_calls"
            " where label like %s", (f"agent run {rid} %",)).fetchone()
        sim = by_id.get(rid, {})
        answer = sim.get("answer") or {}
        out.append({
            "run_id": rid, "kind": sim.get("kind"), "at": started.astimezone(EASTERN).isoformat(),
            "status": status, "detail": detail, "cost_usd": float(cost or 0), "request_id": request_id,
            "model_calls": tokens[0], "input_tokens": tokens[1], "cache_write_tokens": tokens[2],
            "cache_read_tokens": tokens[3], "output_tokens": tokens[4],
            "tool_calls": [c["tool"] for c in calls], "previews": previews,
            "decision": answer.get("decision"), "reason": answer.get("reason"), "update": update, "notes": notes,
            "slot_reasons": answer.get("slot_reasons") or [], "plan": plan,
            "thinking": [c[:1500] for t, k, tool, c, e in steps if k == "thinking"],
        })
    for r in st.runs:
        if r.get("status") == "not_run":
            out.append({"run_id": None, "kind": r["kind"], "at": r["at"], "status": "not_run", "detail": r["detail"],
                        "cost_usd": 0.0, "tool_calls": [], "previews": []})
    return sorted(out, key=lambda r: r["at"])


def _preview_summary(text: str) -> dict:
    """What a preview said, from its stored reply (cut at 4,000 characters, so it may be partial)."""
    try:
        reply = json.loads(text)
    except ValueError:
        codes = sorted({c for c in ("insufficient_balance", "below_floor", "past_cutoff", "request_pending",
                                    "over_capacity", "bad_sourcing", "product_unknown", "price_off_step",
                                    "empty_slot", "unknown_capacity", "nothing_to_load") if c in text})
        ok = '"ok":true' in text.replace(" ", "")
        return {"ok": ok, "refusals": codes, "partial": True}
    return {"ok": bool(reply.get("ok")), "refusals": sorted({r.get("code") for r in reply.get("refusals") or []}),
            "max_cost": (reply.get("cost") or {}).get("max"), "warnings": len(reply.get("warnings") or [])}


def _requests(conn: psycopg.Connection, st) -> list[dict]:
    out = []
    for rid, submitted, off, status, total, fulfilled, body in conn.execute(
            "select request_id, submitted_at, off_cycle, status, total_paid, fulfilled_at, request_body"
            " from purchase_requests order by submitted_at"):
        lines = [{"slot": slot, "source": src, "outcome": outcome, "sku": sku, "store": store, "packs": packs,
                  "units_bought": bought, "paid": str(paid) if paid is not None else None, "loaded": loaded,
                  "lost": lost, "to_closet": closet, "from_closet": from_closet, "pulled": pulled}
                 for slot, src, outcome, sku, store, packs, bought, paid, loaded, lost, closet, from_closet, pulled
                 in conn.execute(
                     "select slot, source, outcome, sku, store, packs_bought, units_bought, total_paid, units_loaded,"
                     " units_lost, units_to_back_stock, units_from_back_stock, units_pulled from purchase_lines"
                     " where request_id = %s order by line_no", (rid,))]
        prices = {}
        for line in body.get("lines") or []:
            choice = line.get("primary") or line.get("load") or {}
            prices[str(line["slot"])] = choice.get("sale_price")
        out.append({"request_id": rid, "submitted_at": submitted.astimezone(EASTERN).isoformat(),
                    "off_cycle": off, "status": status, "total_paid": str(total) if total is not None else None,
                    "fulfilled_at": fulfilled.astimezone(EASTERN).isoformat() if fulfilled else None,
                    "sale_prices": prices, "lines": lines, "body": body})
    return out


def _price_problems(requests: list[dict]) -> list[str]:
    """Prices outside the floor (max unit cost x 1.08, up to 5 cents) or over the $6.50 ceiling."""
    found = []
    for r in requests:
        for line in r["body"].get("lines") or []:
            for rank in ("primary", "alternate", "load"):
                c = line.get(rank)
                if not c:
                    continue
                price = int(round(float(c["sale_price"]) * 100))
                if price > CEILING_CENTS or price % 5:
                    found.append(f"{r['request_id']} slot {line['slot']}: {c['sale_price']}")
                if rank != "load":
                    unit = Fraction(int(round(float(c["max_price_per_pack"]) * 100)), int(c["units_per_pack"]))
                    floor = math.ceil(unit * Fraction(108, 100) / 5) * 5
                    if price < floor:
                        found.append(f"{r['request_id']} slot {line['slot']}: {c['sale_price']} under {floor / 100:.2f}")
    return found


def _slow_sellers(st, cal: Calendar) -> dict[str, list[dict]]:
    """Per Saturday: slots that held stock and sold nothing for 5+ class days in a row, and were still
    the same product after that week's visit."""
    by_slot: dict[str, list[dict]] = defaultdict(list)
    for rec in sorted(st.days.values(), key=lambda r: r["day"]):
        by_slot[rec["slot"]].append(rec)
    flags: dict[str, list[dict]] = defaultdict(list)
    for slot, recs in by_slot.items():
        run, sku = 0, None
        for rec in recs:
            day = date.fromisoformat(rec["day"])
            if not cal.is_class_day(day):
                continue
            if rec["sku"] != sku:
                run, sku = 0, rec["sku"]
            if rec["start_units"] > 0 and rec["sold"] == 0:
                run += 1
            else:
                run = 0
            if run == 5:
                flags[ledger.week_of(datetime.combine(day, datetime.min.time(), EASTERN)).isoformat()].append(
                    {"slot": slot, "sku": sku, "flagged_on": rec["day"]})
    return flags


def _grade(value: str, why: str) -> dict:
    return {"grade": value, "why": why}


def build(conn: psycopg.Connection, st) -> dict:
    cal = Calendar()
    runs, requests = _runs(conn, st), _requests(conn, st)
    spend = defaultdict(Decimal)
    calls = conn.execute("select at, cost_usd from model_calls").fetchall()
    for at, cost in calls:
        spend[ledger.week_of(at.astimezone(EASTERN))] += Decimal(cost)
    slow = _slow_sellers(st, cal)
    price_problems = _price_problems(requests)
    weeks = []
    running = Fraction(0)
    for n, (sat, start, end) in enumerate(_weeks(), start=1):
        p = ledger.pnl(conn, start, end)
        bal = ledger.balance(conn, end)
        days = [r for r in st.days.values() if start.date() <= date.fromisoformat(r["day"]) < end.date()]
        sold = sum(r["sold"] for r in days)
        lost = sum(r["unmet"] + r["lost_expected"] for r in days)
        demand = sold + lost
        class_days = cal.class_days(start.date(), end.date())
        empty_hours = sum(r["empty_class_hours"] for r in days)
        per_slot = defaultdict(lambda: {"sold": 0, "lost": 0.0, "revenue_cents": 0, "sku": None, "price_cents": None})
        for r in sorted(days, key=lambda r: r["day"]):
            s = per_slot[r["slot"]]
            s["sold"] += r["sold"]
            s["lost"] += r["unmet"] + r["lost_expected"]
            s["revenue_cents"] += r["revenue_cents"]
            s["sku"], s["price_cents"] = r["sku"], r["price_cents"]
        revenue = Fraction(p.takings.cents - p.takings.tax_cents)
        gross = revenue - p.cost_of_sales_cents - p.shrinkage_cents
        model = spend.get(sat, Decimal(0))
        profit = gross - p.visit_fees_cents - Fraction(model * 100)
        running += profit
        week_runs = [r for r in runs if start <= datetime.fromisoformat(r["at"]) < end]
        week_requests = [r for r in requests if start <= datetime.fromisoformat(r["submitted_at"]) < end]
        visits = [r for r in requests if r["fulfilled_at"] and start <= datetime.fromisoformat(r["fulfilled_at"]) < end]
        card = {}
        if class_days:
            share = lost / demand if demand else 0
            card["stock_outs"] = _grade("good" if share < .05 else "watch" if share < .15 else "bad",
                                        f"{lost:.0f} of {demand:.0f} units of demand lost to empty slots ({share:.0%}); "
                                        f"{empty_hours:.0f} slot-hours empty while customers came")
            flags = slow.get(sat.isoformat(), [])
            card["slow_sellers"] = _grade("bad" if flags else "good",
                                          f"{len(flags)} stocked slots with no sale for 5 class days: "
                                          + (", ".join(f"{f['slot']} ({f['sku']})" for f in flags) or "none"))
            margin = gross / revenue if revenue else None
            card["margin"] = _grade("watch" if margin is None else "good" if margin >= .4 else
                                    "watch" if margin >= .25 else "bad",
                                    "no revenue" if margin is None else
                                    f"gross margin {float(margin):.0%} on ${float(revenue) / 100:,.2f} revenue ex tax")
        spendable = bal.spendable_cents
        run_cost = [Decimal(str(r["cost_usd"])) for r in week_runs if r["run_id"]]
        over_runs = [c for c in run_cost if c > RUN_PLAN_USD]
        card["spend_vs_plan"] = _grade(
            "bad" if spendable < 0 else "watch" if spendable < FLOOR_CENTS or over_runs else "good",
            f"spendable ${float(spendable) / 100:,.2f} at week end (floor $53); model ${model:.2f} over "
            f"{len(run_cost)} runs (plan $1.00 a run)")
        refusals = sorted({c for r in week_runs for pv in r["previews"] for c in pv["refusals"]})
        unresolved = [r for r in week_runs if r["status"] == "failed"]
        card["refusals"] = _grade("bad" if unresolved else "watch" if refusals else "good",
                                  (f"preview refusals: {', '.join(refusals)}" if refusals else "no preview refusals")
                                  + (f"; {len(unresolved)} run(s) failed: " +
                                     "; ".join((r['detail'] or '')[:120] for r in unresolved) if unresolved else ""))
        caps = [r for r in week_runs if "cap" in (r.get("detail") or "").lower() and r["status"] == "failed"]
        halted = [r for r in week_runs if r["status"] == "halted"]
        week_prices = [x for x in price_problems if any(x.startswith(r["request_id"]) for r in week_requests)]
        card["guardrails"] = _grade("bad" if caps or halted or week_prices or spendable < 0 else "good",
                                    "none hit" if not (caps or halted or week_prices) else
                                    f"caps {len(caps)}, halted {len(halted)}, prices {week_prices}")
        weeks.append({
            "week": n, "saturday": sat.isoformat(), "start": start.isoformat(), "end": end.isoformat(),
            "class_days": class_days, "units_sold": p.units_sold, "units_lost": round(lost, 1),
            "revenue_ex_tax": _d(revenue), "cost_of_sales": _d(p.cost_of_sales_cents), "shrinkage": _d(p.shrinkage_cents),
            "visit_fees": _d(p.visit_fees_cents), "visits": p.visits, "purchases": _d(p.receipts_cents),
            "gross_profit": _d(gross), "model_spend": float(model), "profit": _d(profit), "cumulative_profit": _d(running),
            "balance": _d(bal.balance_cents), "spendable": _d(spendable),
            "inventory_at_cost": _d(p.inventory_cents) if p.inventory_cents is not None else None,
            "runs": [{k: r.get(k) for k in ("run_id", "kind", "at", "status", "cost_usd", "decision", "reason",
                                            "update", "slot_reasons", "tool_calls", "previews", "request_id")}
                     for r in week_runs],
            "requests": [r["request_id"] for r in week_requests],
            "visits_done": [{"request_id": r["request_id"], "at": r["fulfilled_at"], "paid": r["total_paid"],
                             "lines": len(r["lines"]),
                             "unavailable": sum(1 for x in r["lines"] if x["outcome"] == "unavailable"),
                             "prices": r["sale_prices"]} for r in visits],
            "slots": {s: {**v, "lost": round(v["lost"], 1)} for s, v in sorted(per_slot.items(), key=lambda x: int(x[0]))},
            "report_card": card,
        })
    end = world.COMPETITION_END
    total = ledger.pnl(conn, world.LEDGER_OPENS, end)
    total_model = sum(Decimal(c) for _, c in calls)
    season = {
        "decider": st.decider, "model": st.model if st.decider == "model" else None,
        "effort": st.effort if st.decider == "model" else None, "seed": st.seed,
        "run_day": "Mon Tue Wed Thu Fri Sat Sun".split()[getattr(st, "run_day", 3)],
        "units_sold": total.units_sold, "revenue_ex_tax": _d(total.takings.cents - total.takings.tax_cents),
        "gross_profit": _d(total.gross_margin_cents), "visit_fees": _d(total.visit_fees_cents),
        "model_spend": float(total_model), "profit_after_model": round(sum(w["profit"] for w in weeks), 2),
        "inventory_at_cost_end": _d(total.inventory_cents) if total.inventory_cents is not None else None,
        "balance_end": _d(ledger.balance(conn, end).balance_cents),
        "units_lost": round(sum(w["units_lost"] for w in weeks), 1),
        "runs": len([r for r in runs if r["run_id"]]), "submitted": sum(r["status"] == "submitted" for r in runs),
        "failed": sum(r["status"] == "failed" for r in runs), "midweek_visits": total.visits,
        "qualifying_units_directed": sum(x["loaded"] for r in requests for x in r["lines"]),
        "price_problems": price_problems,
    }
    return {"season": season, "weeks": weeks, "runs": runs, "requests": requests}


# --- output --------------------------------------------------------------------------------------------

GRADE_MARK = {"good": "good", "watch": "watch", "bad": "**bad**"}


def tables(data: dict) -> str:
    rows = ["| Week | Class days | Units | Lost | Revenue ex tax | Gross profit | Fees | Model | Profit | Cumulative | Spendable |",
            "|---|---|---|---|---|---|---|---|---|---|---|"]
    for w in data["weeks"]:
        rows.append(f"| W{w['week']} (Sat {w['saturday'][5:]}) | {w['class_days']} | {w['units_sold']} | {w['units_lost']:.0f} | "
                    f"${w['revenue_ex_tax']:,.2f} | ${w['gross_profit']:,.2f} | ${w['visit_fees']:,.2f} | "
                    f"${w['model_spend']:.2f} | ${w['profit']:,.2f} | ${w['cumulative_profit']:,.2f} | ${w['spendable']:,.2f} |")
    cards = ["", "| Week | Stock-outs | Slow sellers | Margin | Spend vs plan | Refusals | Guardrails |",
             "|---|---|---|---|---|---|---|"]
    for w in data["weeks"]:
        c = w["report_card"]
        cell = lambda k: GRADE_MARK[c[k]["grade"]] if k in c else "n/a (no classes)"
        cards.append(f"| W{w['week']} | {cell('stock_outs')} | {cell('slow_sellers')} | {cell('margin')} | "
                     f"{cell('spend_vs_plan')} | {cell('refusals')} | {cell('guardrails')} |")
    detail = [""]
    for w in data["weeks"]:
        detail.append(f"### W{w['week']}: Sat {w['saturday']} ({w['class_days']} class days)\n")
        for k, v in w["report_card"].items():
            detail.append(f"- **{k.replace('_', ' ')}: {v['grade']}.** {v['why']}")
        for r in w["runs"]:
            detail.append(f"- Run {r['run_id']} ({r['kind']}, {r['at'][:16]}): {r['status']}, ${r['cost_usd']:.2f}. "
                          f"{r.get('reason') or ''}")
            for sr in (r.get("slot_reasons") or [])[:20]:
                detail.append(f"  - slot {sr['slot']}: {sr['reason']}")
        for v in w["visits_done"]:
            prices = ", ".join(f"{s} {p}" for s, p in sorted(v["prices"].items(), key=lambda x: int(x[0])))
            detail.append(f"- Visit {v['request_id']} at {v['at'][:16]}: paid ${v['paid']}, {v['lines']} lines, "
                          f"{v['unavailable']} unavailable. Prices: {prices}")
        detail.append("")
    return "\n".join(rows + cards + detail)


def chart_svg(seasons: dict[str, dict]) -> str:
    """Weekly profit as bars and cumulative profit as lines, one colour per season. Static SVG."""
    width, height, left, right, top, bottom = 820, 360, 64, 24, 36, 48
    colors = ("#2a6fdb", "#e07b1a", "#2f9e66", "#8b5cf6", "#c026d3", "#64748b")
    n = max(len(s["weeks"]) for s in seasons.values())
    values = [v for s in seasons.values() for w in s["weeks"] for v in (w["profit"], w["cumulative_profit"])] + [0]
    low, high = min(values), max(values)
    span = (high - low) or 1
    low, high = low - span * .05, high + span * .08
    y = lambda v: top + (high - v) * (height - top - bottom) / (high - low)
    band = (width - left - right) / n
    bar = band * 0.7 / len(seasons)
    ends: list[tuple[float, str, str]] = []
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
           'font-family="system-ui, sans-serif" font-size="12">', '<rect width="100%" height="100%" fill="#fff"/>',
           f'<text x="{left}" y="20" font-size="14" font-weight="600" fill="#222">Simulated profit by week '
           '(bars) and season to date (lines), after model spend</text>']
    step = 10 ** math.floor(math.log10(span)) if span > 0 else 1
    if span / step < 4:
        step /= 2
    t = math.ceil(low / step) * step
    while t <= high:
        out.append(f'<line x1="{left}" x2="{width - right}" y1="{y(t):.1f}" y2="{y(t):.1f}" stroke="#e5e7eb"/>')
        out.append(f'<text x="{left - 6}" y="{y(t) + 4:.1f}" text-anchor="end" fill="#555">${t:,.0f}</text>')
        t += step
    out.append(f'<line x1="{left}" x2="{width - right}" y1="{y(0):.1f}" y2="{y(0):.1f}" stroke="#888"/>')
    for i in range(n):
        out.append(f'<text x="{left + band * (i + .5):.1f}" y="{height - bottom + 16}" text-anchor="middle" fill="#555">W{i + 1}</text>')
    for k, (name, s) in enumerate(seasons.items()):
        c = colors[k % len(colors)]
        for i, w in enumerate(s["weeks"]):
            x0 = left + band * i + band * .15 + bar * k
            y0, y1 = sorted((y(0), y(w["profit"])))
            out.append(f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{bar - 2:.1f}" height="{max(y1 - y0, 1):.1f}" '
                       f'fill="{c}" fill-opacity="0.45"><title>{name} W{i + 1}: ${w["profit"]:,.2f}</title></rect>')
        pts = " ".join(f"{left + band * (i + .5):.1f},{y(w['cumulative_profit']):.1f}" for i, w in enumerate(s["weeks"]))
        out.append(f'<polyline points="{pts}" fill="none" stroke="{c}" stroke-width="2.5"/>')
        last = s["weeks"][-1]
        ends.append((y(last["cumulative_profit"]), c, f'{name}: ${last["cumulative_profit"]:,.0f}'))
        out.append(f'<rect x="{left + 10 + 170 * k}" y="{height - 18}" width="12" height="12" fill="{c}"/>'
                   f'<text x="{left + 26 + 170 * k}" y="{height - 8}" fill="#333">{name}</text>')
    return "\n".join(out + ["</svg>"])


def write(conn: psycopg.Connection, st, out: Path, opts: dict | None = None) -> dict:
    data = build(conn, st)
    data["season"]["assumptions"] = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in (opts or {}).items()}
    (out / "season.json").write_text(json.dumps(data["season"], indent=1, default=str), encoding="utf-8")
    (out / "weeks.json").write_text(json.dumps(data["weeks"], indent=1, default=str), encoding="utf-8")
    (out / "runs.json").write_text(json.dumps(data["runs"], indent=1, default=str), encoding="utf-8")
    (out / "requests.json").write_text(json.dumps(data["requests"], indent=1, default=str), encoding="utf-8")
    with (out / "model_calls.csv").open("w", newline="", encoding="utf-8") as f:
        cols = ("id", "at", "label", "model", "effort", "status", "stop_reason", "input_tokens", "cache_write_tokens",
                "cache_read_tokens", "output_tokens", "max_tokens", "worst_case_usd", "cost_usd", "seconds")
        w = csv.writer(f)
        w.writerow(cols)
        for row in conn.execute(f"select {', '.join(cols)} from model_calls order by id"):
            w.writerow([x.astimezone(EASTERN).isoformat() if isinstance(x, datetime) else x for x in row])
    with (out / "daily_slots.csv").open("w", newline="", encoding="utf-8") as f:
        cols = ("day", "slot", "sku", "price_cents", "start_units", "sold", "revenue_cents", "unmet",
                "lost_expected", "demand_expected", "empty_class_hours", "end_units")
        w = csv.writer(f)
        w.writerow(cols)
        for rec in sorted(st.days.values(), key=lambda r: (r["day"], int(r["slot"]))):
            w.writerow([round(rec[c], 3) if isinstance(rec[c], float) else rec[c] for c in cols])
    (out / "events.json").write_text(json.dumps(st.log, indent=1, default=str), encoding="utf-8")
    (out / "tables.md").write_text(tables(data), encoding="utf-8")
    (out / "weekly.svg").write_text(chart_svg({st.decider: data}), encoding="utf-8")
    return data


def main(argv: list[str] | None = None) -> int:
    from vending.simulate import season
    parser = argparse.ArgumentParser(prog="python -m vending.simulate.report")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--out", type=Path, required=True, help="the season's directory (holds checkpoint.pickle)")
    args = parser.parse_args(argv)
    season.check_scratch(args.database_url)
    st = season.load(args.out / "checkpoint.pickle")
    with psycopg.connect(args.database_url, options="-c TimeZone=UTC") as conn:
        write(conn, st, args.out)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
