"""What our Anthropic keys have been used for: every model call, from `model_calls`.

    python -m vending.usage                      # today, the last 7 days, and all time
    python -m vending.usage --since 2026-11-01   # from a date (Eastern)
    python -m vending.usage --recent 30          # the last 30 calls, one line each
    python -m vending.usage --json               # the same, for the dashboard

Every request the harness sends is a row in `model_calls`, live or test, with
its model, effort, tokens, cost, latency and which key paid. This only reads.

Only calls made through `vending/harness.py` from this version on are in
`model_calls`. Code that predates it (such as an agent branch not yet rebased)
writes only the ledger; the report counts that spend separately, and it never
counts toward the test cap.

Costs are what the harness priced from each answer's `usage` at list prices
(`vending/model_prices.json`). A row still `pending` or `error` counts at its
worst case, so these figures can overstate spend but never understate it.
Anthropic's own record, for the workspace the key belongs to, is the Usage and
Cost pages of the Claude Console; the two should agree to within rounding.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import psycopg

from vending import db
from vending.harness import EASTERN, day_start

GROUPS = {
    "purpose": ("purpose",),
    "model": ("model", "coalesce(effort, '-')"),
    "key": ("key_env",),
    "machine": ("host", "recorded_by"),
}


def _since(day: date) -> datetime:
    return datetime.combine(day, time(0), EASTERN).astimezone(timezone.utc)


def totals(conn: psycopg.Connection, since: datetime | None) -> dict:
    row = conn.execute(
        "select count(*), coalesce(sum(cost_usd), 0),"
        " count(*) filter (where status = 'ok'), count(*) filter (where status = 'pending'),"
        " count(*) filter (where status = 'error'), count(*) filter (where status = 'refused'),"
        " coalesce(sum(cost_usd) filter (where purpose = 'live'), 0),"
        " coalesce(sum(cost_usd) filter (where purpose = 'test'), 0),"
        " coalesce(sum(coalesce(input_tokens, 0) + coalesce(cache_write_tokens, 0) + coalesce(cache_read_tokens, 0)), 0),"
        " coalesce(sum(output_tokens), 0)"
        " from model_calls where (%s::timestamptz is null or at >= %s)", (since, since)).fetchone()
    keys = ("calls", "cost_usd", "ok", "pending", "error", "refused", "live_usd", "test_usd", "tokens_in", "tokens_out")
    return dict(zip(keys, row))


def grouped(conn: psycopg.Connection, by: str, since: datetime | None) -> list[dict]:
    cols, width = ", ".join(GROUPS[by]), len(GROUPS[by])
    rows = conn.execute(
        f"select {cols}, count(*), coalesce(sum(cost_usd), 0), avg(cost_usd) filter (where status = 'ok'),"
        " coalesce(sum(output_tokens), 0), avg(output_tokens) filter (where status = 'ok'),"
        " avg(seconds) filter (where status = 'ok'), max(at)"
        f" from model_calls where (%s::timestamptz is null or at >= %s) group by {cols}",
        (since, since)).fetchall()
    out = []
    for r in rows:
        out.append({"group": " ".join(str(v) for v in r[:width]), "calls": r[width], "cost_usd": r[width + 1],
                    "avg_cost_usd": r[width + 2], "tokens_out": r[width + 3], "avg_tokens_out": r[width + 4],
                    "avg_seconds": r[width + 5], "last": r[width + 6]})
    return sorted(out, key=lambda g: g["cost_usd"], reverse=True)


def recent(conn: psycopg.Connection, limit: int) -> list[dict]:
    rows = conn.execute(
        "select id, at, purpose, label, model, effort, key_env, status, stop_reason, input_tokens, output_tokens,"
        " cost_usd, seconds, host from model_calls order by at desc, id desc limit %s", (limit,)).fetchall()
    keys = ("id", "at", "purpose", "label", "model", "effort", "key_env", "status", "stop_reason", "tokens_in",
            "tokens_out", "cost_usd", "seconds", "host")
    return [dict(zip(keys, r)) for r in rows]


def report(conn: psycopg.Connection, now: datetime, since: date | None = None, recent_n: int = 10) -> dict:
    week_ago = now.astimezone(EASTERN).date() - timedelta(days=6)   # calendar days, so DST can't shift it
    windows = ({"from " + since.isoformat(): _since(since)} if since else
               {"today": day_start(now), "last 7 days": _since(week_ago), "all time": None})
    window = list(windows.values())[-1]
    return {
        "as_of": now.astimezone(EASTERN).isoformat(timespec="seconds"),
        "windows": {name: totals(conn, start) for name, start in windows.items()},
        "by": {by: grouped(conn, by, window) for by in GROUPS},
        "recent": recent(conn, recent_n),
        "outside": outside(conn),
    }


def outside(conn: psycopg.Connection) -> dict:
    """Token spend in the ledger with no model_calls row: made by code that predates model_calls."""
    row = conn.execute(
        "select count(*), coalesce(sum(-amount_cents), 0) / 100 from ledger_entries e where kind = 'tokens'"
        " and not exists (select 1 from model_calls c where c.ledger_entry = e.id)").fetchone()
    return {"entries": row[0], "cost_usd": row[1]}


def _money(value) -> str:
    return "-" if value is None else f"${Decimal(value):.4f}"


def _num(value, places: int = 0) -> str:
    return "-" if value is None else f"{float(value):,.{places}f}"


def _table(header: tuple, rows: list[tuple]) -> list[str]:
    widths = [max(len(str(r[i])) for r in (header, *rows)) for i in range(len(header))]
    return ["  ".join(str(v).ljust(w) for v, w in zip(r, widths)).rstrip() for r in (header, *rows)]


def render(rep: dict) -> str:
    out = [f"Model calls on our Anthropic keys, as of {rep['as_of']} (Eastern)", ""]
    out += _table(("Window", "Calls", "Spend", "Live", "Test", "Tokens in", "Tokens out", "Not settled"),
                  [(name, t["calls"], _money(t["cost_usd"]), _money(t["live_usd"]), _money(t["test_usd"]),
                    _num(t["tokens_in"]), _num(t["tokens_out"]),
                    f"{t['pending']} pending, {t['error']} error" if t["pending"] or t["error"] else "")
                   for name, t in rep["windows"].items()])
    titles = {"purpose": "By purpose", "model": "By model and effort", "key": "By key (the variable it came from)",
              "machine": "By machine"}
    for by, title in titles.items():
        rows = rep["by"][by]
        if not rows:
            continue
        out += ["", f"{title}, {list(rep['windows'])[-1]}"]
        out += _table(("", "Calls", "Spend", "$/call", "Avg out tok", "Avg secs"),
                      [(g["group"], g["calls"], _money(g["cost_usd"]), _money(g["avg_cost_usd"]),
                        _num(g["avg_tokens_out"]), _num(g["avg_seconds"], 1)) for g in rows])
    if rep["recent"]:
        out += ["", "Most recent calls"]
        out += _table(("When", "Purpose", "Model", "Effort", "Status", "Cost", "Secs", "What"),
                      [(f"{r['at'].astimezone(EASTERN):%d %b %H:%M}", r["purpose"], r["model"], r["effort"] or "-",
                        r["status"] if r["status"] != "ok" else (r["stop_reason"] or "ok"), _money(r["cost_usd"]),
                        _num(r["seconds"], 1), r["label"][:48]) for r in rep["recent"]])
    if rep["outside"]["entries"]:
        out += ["", f"Also {rep['outside']['entries']} token entries in the ledger, {_money(rep['outside']['cost_usd'])}, "
                "with no model_calls row: spent by code that doesn't record calls yet. Not in the figures above."]
    if not any(t["calls"] for t in rep["windows"].values()):
        out += ["", "No model calls recorded yet."]
    out += ["", "Pending and error rows count at their worst case. Anthropic's own figures: the Claude Console's "
            "Usage and Cost pages for the key's workspace."]
    return "\n".join(out)


def _jsonable(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def main(argv: list[str] | None = None, conn: psycopg.Connection | None = None,
         now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.usage", description=__doc__.split("\n")[0])
    parser.add_argument("--since", type=date.fromisoformat, help="from this date, Eastern (such as 2026-11-01)")
    parser.add_argument("--recent", type=int, default=10, help="how many recent calls to list (0 for none)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of tables")
    args = parser.parse_args(argv)
    if not 0 <= args.recent <= 1000:
        print("--recent takes 0 to 1000.", file=sys.stderr)
        return 2
    own = conn is None
    conn = conn or db.connect()
    try:
        conn.execute("set default_transaction_read_only = on")
        if conn.execute("select to_regclass('model_calls')").fetchone()[0] is None:
            print("This database has no model_calls table yet. Run `python -m vending.db migrate`.", file=sys.stderr)
            return 1
        rep = report(conn, now or datetime.now(timezone.utc), args.since, args.recent)
    finally:
        if own:
            conn.close()
    print(json.dumps(rep, default=_jsonable, indent=1) if args.json else render(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
