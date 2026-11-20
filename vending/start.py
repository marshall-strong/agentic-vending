"""When the competition starts, and when the fall data ends: the one definition every reader uses.

The machine goes live with our products on Thu 3 Dec 2026 (12 Nov notes, row 2;
`docs/planning/2026-11-17-david-answers.md (PR #164)`, not on this branch yet). `START` is the start of that day,
Eastern: 2026-12-03 00:00 America/New_York, 05:00 UTC.

The fall cut comes before it (`docs/integrity.md`, "Fall data"): the setting
`FALL_CUTOFF`, 18 Nov 2026 00:00 Eastern when unset, and never later than `START`.
The rule turns on when a poll was taken:

- **Before the fall cut**, no selection is either team's, so every selection's
  data, and the machine-wide totals, are fair to read.
- **From the fall cut on**, nothing reads, shows or uses per-selection data
  outside our assignment, and nothing shows a machine-wide total (the total minus
  ours is theirs). Storing the raw poll is still fine.

`START` itself is when the competition runs: the machine goes live with the
teams' products and the weekly catalog limits count from it.

This is not the ledger's opening, which comes before the day-one fill
(`vending/usage.py` calls that "the competition start").

Standard library only, so the stdlib-only scripts (`scripts/bridge.py`) and the
pure helpers can import it.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from vending import settings

EASTERN = ZoneInfo("America/New_York")

START = datetime(2026, 12, 3, tzinfo=EASTERN)

# The fall data ends before the day-one fill goes in (staff "basically turn it off" to load it, David, 12 Nov;
# loading falls about 30 Nov to 2 Dec). Default: 18 Nov 00:00 Eastern, the first day of the break after Fall
# Quarter classes (golisano_calendar_2026_27.json), safely before any loading. A setting, so the team can move it.
FALL_CUTOFF_SETTING = "FALL_CUTOFF"
DEFAULT_FALL_CUTOFF = "2026-11-18T00:00:00-05:00"


def on_or_after_start(moment: datetime) -> bool:
    """True for an aware moment at or after `START`. A naive one is refused: its zone is a guess."""
    if moment.utcoffset() is None:
        raise ValueError(f"{moment!r} has no time zone, so it can't be placed before or after the start")
    return moment >= START


def fall_cut() -> datetime:
    """The end of the fall data: the earlier of `FALL_CUTOFF` (default `DEFAULT_FALL_CUTOFF`) and `START`.

    Read each call, so a changed setting counts at once. A value that isn't a time with its offset raises
    ValueError naming the setting: a guessed cut could show data from after it."""
    value = settings.get(FALL_CUTOFF_SETTING) or DEFAULT_FALL_CUTOFF
    try:
        cut = datetime.fromisoformat(value)
    except ValueError:
        cut = None
    if cut is None or cut.utcoffset() is None:
        raise ValueError(f"{FALL_CUTOFF_SETTING} is {value!r}; give a time with its offset, like {DEFAULT_FALL_CUTOFF}")
    return min(cut, START)


def on_or_after_fall_cut(moment: datetime) -> bool:
    """True for an aware moment at or after `fall_cut()`. A naive one is refused: its zone is a guess."""
    if moment.utcoffset() is None:
        raise ValueError(f"{moment!r} has no time zone, so it can't be placed before or after the fall cut")
    return moment >= fall_cut()
