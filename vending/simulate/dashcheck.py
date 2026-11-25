"""The dashboard against a season's scratch database: the real snapshot and page, timed panel by panel.

`check` opens the scratch database as the dashboard opens the team's (`vending.pages.OPTIONS`: read only, a
5-second statement timeout), with the season's clock (`dbclock`), takes `dashboard.snapshot(public=True)` and
`dashboard.render` as `/dashboard` does, and `run_page.read` and `run_page.html` as `/dashboard/run/<id>` does for
each run asked for. It records the seconds each panel took, which panels came back unavailable or missing and
why, and the sizes of the pages. Timings are this machine's against a local database: the deployed page reads
the team database over the network (issue #250), so they show the queries' own cost, not the page's latency.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import psycopg


def _connection(url: str) -> psycopg.Connection:
    from vending import db, pages
    from vending.simulate import dbclock

    return psycopg.connect(url, autocommit=True, connect_timeout=db.CONNECT_TIMEOUT,
                           options=db.session_options(url, pages.OPTIONS, dbclock.search_path(_schema(url))))


def _schema(url: str) -> str:
    """The schema a scratch URL's own options name (`-c search_path=...`), else public."""
    from psycopg.conninfo import conninfo_to_dict

    for part in (conninfo_to_dict(url).get("options") or "").split("-c "):
        key, _, value = part.strip().partition("=")
        if key == "search_path" and value:
            return value.split(",")[0].strip()
    return "public"


def timed_snapshot(conn: psycopg.Connection) -> tuple[dict, list[dict], float]:
    """`dashboard.snapshot(public=True)` with each panel's seconds: (snapshot, panels, total seconds). A panel is
    one `dashboard._panel` call, named by the key it lands under."""
    from vending import dashboard

    seen: list[tuple[int, float]] = []
    real: Callable = dashboard._panel

    def panel(build):
        start = time.perf_counter()
        out = real(build)
        seen.append((id(out), time.perf_counter() - start))
        return out

    dashboard._panel = panel
    try:
        start = time.perf_counter()
        snap = dashboard.snapshot(conn, public=True)
        total = time.perf_counter() - start
    finally:
        dashboard._panel = real
    names = {id(v): k for k, v in snap.items() if isinstance(v, dict)}
    panels = []
    for ident, seconds in seen:
        name = names.get(ident, "(nested)")
        value = snap.get(name) if name in snap else None
        ok = bool(value.get("ok")) if isinstance(value, dict) else True
        panels.append({"panel": name, "seconds": round(seconds, 3), "ok": ok,
                       "why": None if ok else str(value.get("why"))[:200]})
    return snap, panels, total


def check(url: str, run_ids: list[int], *, label: str = "") -> dict:
    """The dashboard and the run pages of `run_ids`, timed. Never raises: a failure is part of what it finds."""
    from vending import dashboard, run_page

    out: dict[str, Any] = {"label": label, "panels": [], "runs": [], "errors": []}
    try:
        with _connection(url) as conn:
            snap, panels, total = timed_snapshot(conn)
            out["as_of"] = str(snap.get("as_of"))
            out["snapshot_seconds"] = round(total, 3)
            out["panels"] = panels
            out["unavailable"] = [p["panel"] for p in panels if not p["ok"]]
            out["attention"] = [str(a.get("text") or a.get("title") or a)[:160] for a in snap.get("attention") or []]
            start = time.perf_counter()
            page = dashboard.render(snap)
            out["render_seconds"] = round(time.perf_counter() - start, 3)
            out["page_bytes"] = len(page.encode())
            for run_id in run_ids:
                row: dict[str, Any] = {"run_id": run_id}
                try:
                    start = time.perf_counter()
                    data = run_page.read(conn, run_id, public=True)
                    row["read_seconds"] = round(time.perf_counter() - start, 3)
                    start = time.perf_counter()
                    html = run_page.html(data)
                    row["render_seconds"] = round(time.perf_counter() - start, 3)
                    row["page_bytes"] = len(html.encode())
                    row["steps"] = len(data.get("steps") or [])
                except Exception as error:      # noqa: BLE001 - recorded, the check goes on
                    row["error"] = f"{type(error).__name__}: {str(error).splitlines()[0][:160] if str(error) else ''}"
                out["runs"].append(row)
    except Exception as error:      # noqa: BLE001 - recorded
        out["errors"].append(f"{type(error).__name__}: {str(error).splitlines()[0][:200] if str(error) else ''}")
    return out


def table(found: dict) -> str:
    """A Markdown table of one check: the slowest panels first."""
    lines = [f"Dashboard check {found.get('label', '')} as of {found.get('as_of')}: snapshot "
             f"{found.get('snapshot_seconds')} s, render {found.get('render_seconds')} s, "
             f"{found.get('page_bytes')} bytes", "", "| Panel | Seconds | Shows |", "|---|---:|---|"]
    for p in sorted(found.get("panels") or [], key=lambda p: -p["seconds"]):
        lines.append(f"| {p['panel']} | {p['seconds']:.3f} | {'ok' if p['ok'] else 'unavailable: ' + str(p['why'])} |")
    for r in found.get("runs") or []:
        lines.append(f"| run page {r['run_id']} | {r.get('read_seconds', '-')} + {r.get('render_seconds', '-')} | "
                     f"{r.get('error') or str(r.get('steps')) + ' steps'} |")
    for e in found.get("errors") or []:
        lines.append(f"| (error) | | {e} |")
    return "\n".join(lines)
