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


def server_assignment_supported():
    """True once vending.slots can read the stored server assignment (#69). Before that, no run read one."""
    try:
        from vending.slots import our_slots  # noqa: F401
    except (ImportError, AttributeError):
        return False
    return True


def ours_for(source, slots):
    """The list a recorded scope (data/slots.json, lab_data.json) may cover: always vending.slots.SLOTS.

    A recorded list can't be checked against the server without the database, so it may only ever be
    vending.slots.SLOTS or part of it, whatever source it names. Only the export, which reads the server's
    stored answer itself, can use a list beyond SLOTS (resolve_slots). Unknown sources are refused, and so is
    a list recorded as the server's answer while this code can't read one (#69).
    """
    if not (source in (SOURCE_CLI, SOURCE_SERVER) or is_default(source)):
        raise SystemExit(f"slot list recorded with an unknown source {source!r}: re-export (python analysis/rebuild.py)")
    if source == SOURCE_SERVER and not server_assignment_supported():
        raise SystemExit("slot list recorded as the inventory server's answer, but this code can't read one yet (#69): "
                         "re-export (python analysis/rebuild.py)")
    return SLOTS


def check_scope(device, slots, source, where):
    """Refuse a recorded scope for another machine or with a slot that isn't ours. Returns the slots in shelf order."""
    if device != DEVICE_ID:
        raise SystemExit(f"{where}: recorded for device {device!r}, but ours is {DEVICE_ID!r}")
    return check(slots, where, ours_for(source, slots))


def resolve_run(cli_value, exported, where, now):
    """(device, slots, source, resolved_at) for the analysis, given --slots and the export's data/slots.json.

    `exported` is that file's contents, or None. The CSVs must have been exported for the same list this run
    uses; otherwise it stops and says to re-export.
    """
    recorded = None
    if exported:
        recorded = check_scope(exported["device"], exported["slots"], exported["source"], where)
        if is_default(exported["source"]):
            exported = {**exported, "source": SOURCE_DEFAULT}       # older files carry a longer wording
    ours = ours_for(exported["source"], recorded) if exported else SLOTS
    if cli_value is not None:
        device, slots, source, at = DEVICE_ID, parse_slots(cli_value, ours), SOURCE_CLI, now
        if recorded and set(recorded) == set(slots):
            source, at = exported["source"], exported.get("resolved_at")   # same list as exported: keep where it came from
    elif exported:
        device, slots, source, at = DEVICE_ID, recorded, exported["source"], exported.get("resolved_at")
    else:
        device, slots, source, at = DEVICE_ID, check(SLOTS, "vending.slots.SLOTS"), SOURCE_DEFAULT, None
    if recorded and set(recorded) != set(slots):
        raise SystemExit(
            f"SLOTS: the CSVs were exported for {len(recorded)} slots on {DEVICE_ID} ({ranges(recorded)}), "
            f"but this run asks for {len(slots)} ({ranges(slots)}). Re-export first: "
            f"python analysis/export_sales.py --slots {','.join(slots)}  (or python analysis/rebuild.py --slots ...)")
    return device, slots, source, at


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
