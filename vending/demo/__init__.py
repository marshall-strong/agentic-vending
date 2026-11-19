"""The demo copy of the dashboard: the live page's own code, drawn from a fixed, made-up snapshot.

    /dashboard/demo               the page, with a banner, "DEMO" in the tab title and no refresh
    /dashboard/demo/state.json    the same data as JSON, with `"demo": true`

Nothing here touches a database, and nothing here may. The snapshot is a committed file
(`snapshot.json`, made by `scripts/make_demo_snapshot.py`), read from disk and handed to
`vending.dashboard.render`. That includes our selections: the demo has its own made-up assignment
inside the snapshot (`snapshot["ours"]`), and never reads the team's stored one (`vending.slots`).
So this module imports no database driver and no reader. `tests/test_demo.py` fails the page if a
connection is opened or `vending.slots` is called.

The data file holds JSON, which has no dates, `Decimal`s or `Fraction`s, so `encode` tags them and
`decode` gives `render` back exactly what a live `snapshot()` has.

The copy sits behind the same password as the live page, and its views are not counted as visits
(`vending.visits.PAGES` lists the live pages only; see docs/dashboard.md).
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

from vending import dashboard

DEMO, DEMO_STATE = "/dashboard/demo", "/dashboard/demo/state.json"
LIVE = dashboard.DEMO_LIVE_HREF
SNAPSHOT = Path(__file__).with_name("snapshot.json")
# Links on the demo page: back to the live page first.
NAV = (("Live dashboard", LIVE), ("Home", "/"))

_TAGS = {"$datetime": datetime.fromisoformat, "$decimal": Decimal, "$fraction": Fraction}


class DemoError(Exception):
    """The snapshot file is missing, isn't JSON, or isn't a demo snapshot."""


def encode(value: Any) -> Any:
    """`value` as plain JSON types: dates, Decimals and Fractions become one-key tagged objects.

    Raises TypeError for anything else, so a new kind of value in the snapshot is noticed when the
    file is made, not when the page is served. Tuples become lists, as in the live JSON.
    """
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    if isinstance(value, Fraction):
        return {"$fraction": f"{value.numerator}/{value.denominator}"}
    if isinstance(value, dict):
        if any(not isinstance(k, str) for k in value):
            raise TypeError("a snapshot's keys must be text")
        return {k: encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"can't store {type(value).__name__} in the demo snapshot")


def decode(value: Any) -> Any:
    """The inverse of `encode`: tagged objects become datetimes, Decimals and Fractions again."""
    if isinstance(value, dict):
        if len(value) == 1:
            (tag, raw), = value.items()
            if tag in _TAGS:
                return _TAGS[tag](raw)
        return {k: decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode(v) for v in value]
    return value


def load(path: Path = SNAPSHOT) -> dict:
    """The made-up snapshot, as `dashboard.snapshot` would have made it. A fresh copy every call.

    Refuses a file that isn't marked `"demo": true`, so a real snapshot dumped into this place can
    never be served as the demo (or the other way round).
    """
    try:
        snap = decode(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as error:
        raise DemoError(f"the demo snapshot can't be read: {type(error).__name__}") from error
    if not isinstance(snap, dict) or snap.get("demo") is not True:
        raise DemoError("the demo snapshot isn't marked demo: true")
    return snap


def page() -> str:
    """The demo page: the live page's `render`, in demo mode."""
    return dashboard.render(load(), json_href=DEMO_STATE, nav=NAV, demo=True)


def state_json() -> str:
    """The demo's JSON: the snapshot, with `"demo": true` first. Never a live verdict."""
    return dashboard.to_json({"demo": True, **load()})
