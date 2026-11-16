"""Save a finished season as pages, so the dashboard can show it with no database.

    python -m vending.simulate.archive --database-url postgresql://...@127.0.0.1:PORT/agent \\
        --data docs/simulations/data/2026-11-16-opus --name "Opus 5.5, medium" --strategy "the live agent as built"

It cuts the season's scratch database at each moment worth seeing (`viewer.as_of`), renders the real
agent dashboard and the real P&L page there, and writes them all into one compressed file,
`archive.jsonl.xz`, beside the season's data files. `vending.simulate.history` serves those files
under `/simulations` in the dashboard app, read-only, on the Container App as well as locally.

The moments: the end of each day (18:00 Eastern, after the day's sales and any visit), 07:00 and 11:00
on weekdays (before and after the morning rush), and just after every agent run and every visit. The dashboard is rendered as the public pages show it
(`dashboard.snapshot(public=True)`): no error text, our selections only.
"""

from __future__ import annotations

import argparse
import json
import lzma
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

from vending.simulate import world
from vending.simulate.world import EASTERN

ARCHIVE = "archive.jsonl.xz"
META = "meta.json"


def moments(url: str) -> list[tuple[datetime, str]]:
    from vending.simulate import viewer
    found: dict[datetime, str] = {}
    day = world.SIM_START.date()
    while datetime.combine(day, time(0), EASTERN) < world.COMPETITION_END:
        found[datetime.combine(day, time(18), EASTERN)] = f"end of {day:%a %d %b}"
        if day.weekday() < 5:       # a weekday's morning rush and midday too
            found[datetime.combine(day, time(11), EASTERN)] = f"{day:%a} after the morning rush"
            found[datetime.combine(day, time(7), EASTERN)] = f"{day:%a} before classes"
        day += timedelta(days=1)
    for at, label in viewer.milestones(url):
        found[at.astimezone(EASTERN) + timedelta(minutes=10)] = f"after {label}"
    end = world.COMPETITION_END - timedelta(minutes=1)
    found[end] = "end of the season"
    return sorted((t, label) for t, label in found.items() if world.SIM_START < t <= end)


def build(url: str, out: Path, meta: dict, say=print) -> Path:
    from psycopg.types.json import set_json_loads  # noqa: F401 - imported by the viewer's page builder

    from vending import dashboard
    from vending.simulate import viewer
    points = moments(url)
    path = out / ARCHIVE
    with lzma.open(path, "wt", encoding="utf-8", preset=9) as f:
        for i, (at, label) in enumerate(points):
            with viewer.as_of(url, at) as conn:
                from functools import partial
                from decimal import Decimal
                set_json_loads(partial(json.loads, parse_float=Decimal), context=conn._conn)
                snap = dashboard._snapshot(conn, at)
                dashboard.hide_errors(snap)
                dashboard.only_ours(snap)
                snap["attention"] = dashboard.attention(snap)
            page = dashboard.render(snap, refresh=86400, json_href="#", nav=(("P&L", "@PNL@"), ("Simulations", "@SIMS@")))
            pnl = viewer.pnl_page_bare(url, at)
            f.write(json.dumps({"at": at.isoformat(), "label": label, "dashboard": page, "pnl": pnl}) + "\n")
            if i % 20 == 0:
                say(f"  {i + 1}/{len(points)} {at:%a %d %b %H:%M} {label}")
    (out / META).write_text(json.dumps({**meta, "moments": len(points)}, indent=1), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    from vending.simulate.season import check_scratch
    p = argparse.ArgumentParser(prog="python -m vending.simulate.archive", description=__doc__.splitlines()[0])
    p.add_argument("--database-url", required=True)
    p.add_argument("--data", type=Path, required=True, help="the season's data directory (season.json, weeks.json)")
    p.add_argument("--name", required=True, help="how the dashboard names this season")
    p.add_argument("--strategy", default="", help="one line: what was different about it")
    args = p.parse_args(argv)
    check_scratch(args.database_url)
    path = build(args.database_url, args.data, {"name": args.name, "strategy": args.strategy,
                                                "built": datetime.now(EASTERN).isoformat(timespec="minutes")})
    print(f"wrote {path} ({path.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
