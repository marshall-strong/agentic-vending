"""`set_wildcard_slot`: the agent picks the one wildcard slot, once (`docs/agent/wildcard.md`).

The wildcard is fixed: config `wildcard_slot`, which `vending.config.set_value`
refuses to change once set. A person sets it with
`python -m vending.config set wildcard_slot '"37"' --by NAME`; the agent sets it
with this tool, after the redraw, by the rule in the doc: an ordinary
middle-shelf slot of ours, not beside the machine's centre line (column 4 or 5).
The code checks what it can (ours, shelf 2 to 4, not column 4 or 5); the agent
checks that the column takes cans and bottles.

It goes through the write gate like every write tool: refused while halted,
and logged with its reason.
"""

from __future__ import annotations

from collections.abc import Callable

import psycopg

from vending import config
from vending.pricing import Refusal
from vending.slots import SlotsNotRecorded, our_slots
from vending.tools import WriteReply
from vending.write_gate import Refused

Action = Callable[[psycopg.Connection], WriteReply]

MIDDLE_SHELVES = ("2", "3", "4")   # not the top or bottom shelf
CENTRE_COLUMNS = ("4", "5")        # the two columns either side of the machine's centre line (8 columns)


def set_wildcard_slot(slot: str, reason: str) -> Action:
    def action(conn: psycopg.Connection) -> WriteReply:
        # Every check comes first, so asking again for a slot that is set but no longer ours, or never was an
        # allowed spot, is refused rather than confirmed.
        refusals = problems(conn, slot, reason)
        now = config.get_value(conn, "wildcard_slot")
        if now is not None and now != slot:
            refusals.insert(0, Refusal("wildcard", (
                f"the wildcard is slot {now} and never moves; only a person can change it"), slot))
        if refusals:
            raise Refused(refusals)
        if now == slot:
            return WriteReply(success=True, message=f"Slot {slot} is already the wildcard; nothing changed.")
        config.set_value(conn, "wildcard_slot", slot, "agent")
        return WriteReply(success=True, message=(
            f"Slot {slot} is the wildcard from now on, and never moves. It may sell up to the wildcard ceiling; "
            "every other slot keeps the ceiling. See get_machine_state's wildcard_trial."))
    return action


def problems(conn: psycopg.Connection, slot: str, reason: str) -> list[Refusal]:
    """Why `slot` can't be the wildcard: not ours in the stored assignment, not an ordinary spot, or no reason."""
    try:
        ours = our_slots(conn)
    except SlotsNotRecorded as error:
        return [Refusal("wildcard", str(error), slot)]
    refusals = []
    if slot not in ours:
        refusals.append(Refusal("wildcard", f"slot {slot} isn't one of ours ({', '.join(ours)})", slot))
    else:
        shelf, column = slot[0], slot[1:]
        if shelf not in MIDDLE_SHELVES:
            refusals.append(Refusal("wildcard", (
                f"slot {slot} is on shelf {shelf}; the wildcard goes on a middle shelf "
                f"({', '.join(MIDDLE_SHELVES)}), an ordinary position"), slot))
        if column in CENTRE_COLUMNS:
            refusals.append(Refusal("wildcard", (
                f"slot {slot} is in column {column}, beside the machine's centre line, the best-seen "
                "position; pick another column so a trial isn't flattered"), slot))
    if not reason.strip():
        refusals.append(Refusal("wildcard", "give a reason: why this slot", slot))
    return refusals
