"""Which slots the analysis covers: one setting, three sources, first one found wins.

    1. --slots on the command line ("15,16,17" or "15 16 17")
    2. the assignment stored from the inventory server (GET /api/v1/slots), read with
       vending.slots.our_slots(conn), when that function exists and a row is stored
    3. vending.slots.SLOTS, the default list

The result is checked: 1 to 40 different codes, each a shelf digit 1-5 then a column digit 1-8, and every
code must be one of OUR slots: the stored server assignment when there is one, else vending.slots.SLOTS.
--slots can only narrow the list, never add a slot that isn't ours (docs/integrity.md): the export writes
per-slot prices and sales for every slot on the list.
Slots come back in shelf order (shelf 1 first, then by column).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vending.slots import DEVICE_ID, SLOTS  # noqa: E402  (plain constants; no database needed)

SOURCE_CLI = "command line"
SOURCE_SERVER = "inventory server (stored)"
SOURCE_DEFAULT = "default list in vending/slots.py"


def parse_slots(value, ours=SLOTS):
    """Slot codes from "15,16 17", checked against `ours`. Exits with a message on anything else."""
    codes = [c for c in str(value).replace(",", " ").split() if c]
    return check(codes, "--slots", ours)


def is_default(source):
    """True for the default-list source, including the longer wording older slots.json files carry."""
    return str(source).startswith(SOURCE_DEFAULT)


def check(codes, where, ours=SLOTS):
    """The codes in shelf order, or exit with every problem. `ours` is the list every code must belong to."""
    codes = [str(c).strip() for c in codes]
    problems = []
    if not 1 <= len(codes) <= 40:
        problems.append(f"{len(codes)} slots given; give 1 to 40")
    bad = [c for c in codes if not (len(c) == 2 and c[0] in "12345" and c[1] in "12345678")]
    if bad:
        problems.append(f"not a slot code (shelf 1-5 then column 1-8, like 15): {', '.join(bad)}")
    dup = sorted({c for c in codes if codes.count(c) > 1})
    if dup:
        problems.append(f"listed more than once: {', '.join(dup)}")
    ours = {str(c).strip() for c in ours}
    not_ours = [c for c in codes if c not in bad and c not in ours]
    if not_ours:
        problems.append(f"not one of our slots ({ranges(tuple(sorted(ours)))}): {', '.join(not_ours)}; "
                        "the analysis never covers another team's slots")
    if problems:
        raise SystemExit(f"{where}: " + "; ".join(problems))
    return tuple(sorted(codes))


def from_server(conn):
    """The stored server assignment, or None when it can't be read (no function yet, no table, no row)."""
    if conn is None:
        return None
    try:
        from vending.slots import our_slots
    except (ImportError, AttributeError):
        return None                       # vending.slots has no our_slots yet
    try:
        from vending.slots import SlotsNotRecorded
    except (ImportError, AttributeError):
        SlotsNotRecorded = LookupError    # noqa: N806  (never raised; keeps the except below valid)
    try:
        return tuple(our_slots(conn))
    except SlotsNotRecorded:
        _rollback(conn)
        return None
    except Exception as error:            # e.g. psycopg.errors.UndefinedTable before the migration runs
        if type(error).__name__ not in ("UndefinedTable", "UndefinedColumn", "InFailedSqlTransaction"):
            raise
        _rollback(conn)
        return None


def _rollback(conn):
    try:
        conn.rollback()
    except Exception:
        pass


def resolve_slots(cli_value, conn=None):
    """(device_id, slots, source) for this run. See the module docstring for the order."""
    stored = from_server(conn)
    ours = check(stored, "stored server assignment", stored) if stored else SLOTS
    if cli_value is not None:
        return DEVICE_ID, parse_slots(cli_value, ours), SOURCE_CLI   # only ever a part of our list
    if stored:
        return DEVICE_ID, ours, SOURCE_SERVER
    return DEVICE_ID, check(SLOTS, "vending.slots.SLOTS"), SOURCE_DEFAULT


def ranges(slots):
    """'15-18, 25-28' style text for slot codes in shelf order."""
    out, run = [], []
    for s in slots:
        if run and s[0] == run[-1][0] and int(s[1]) == int(run[-1][1]) + 1:
            run.append(s)
            continue
        if run:
            out.append(run[0] if len(run) == 1 else f"{run[0]}-{run[-1]}")
        run = [s]
    if run:
        out.append(run[0] if len(run) == 1 else f"{run[0]}-{run[-1]}")
    return ", ".join(out)
