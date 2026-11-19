"""When the competition starts: the one definition every reader uses.

The machine goes live with our products on Thu 3 Dec 2026 (12 Nov notes, row 2;
`docs/planning/2026-11-17-david-answers.md (PR #164)`, not on this branch yet). `START` is the start of that day,
Eastern: 2026-12-03 00:00 America/New_York, 05:00 UTC.

Two rules turn on it (`docs/integrity.md`):

- **Before the start**, no selection is either team's, so every selection's data
  is fair to read.
- **From the start**, nothing reads, shows or uses the other team's
  per-selection data, and nothing shows a machine-wide total that includes their
  sales (the total minus ours is theirs). Storing the raw poll is still fine.

This is not the ledger's opening, which comes before the day-one fill
(`vending/usage.py` calls that "the competition start").

Standard library only, so the stdlib-only scripts (`scripts/bridge.py`) and the
pure helpers can import it.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

START = datetime(2026, 12, 3, tzinfo=EASTERN)


def on_or_after_start(moment: datetime) -> bool:
    """True for an aware moment at or after `START`. A naive one is refused: its zone is a guess."""
    if moment.utcoffset() is None:
        raise ValueError(f"{moment!r} has no time zone, so it can't be placed before or after the start")
    return moment >= START
