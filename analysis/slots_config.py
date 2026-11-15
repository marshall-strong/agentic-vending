"""Which slots the analysis covers: one setting, three sources, first one found wins.

    1. --slots on the command line ("15,16,17" or "15 16 17")
    2. the assignment stored from the inventory server (GET /api/v1/slots), read with
       vending.slots.stored(conn), when a row is stored
    3. vending.slots.SLOTS, the default list, when none is

Our assignment is whatever the inventory server gave, as stored (vending.slots.stored), not vending.slots.SLOTS:
the split may be redrawn once before the start (#75). The stored answer is checked by its shape only
(check_assignment: exactly 20 different selections of the 40-selection machine) and must be on our device;
anything malformed stops the run. With nothing stored (no table or row yet) the assignment is vending.slots.SLOTS.
The run's list is checked: 1 to 40 different codes, each a shelf digit 1-5 then a column digit 1-8, and every
code must be in our assignment. --slots can only narrow the list, never add a slot that isn't ours
(docs/integrity.md): the export writes per-slot prices and sales for every slot on the list.
The export records the assignment next to the list (data/slots.json, `assigned_slots`), and the analysis, the labs
and rebuild.py check it from there; a recorded scope without one is refused.
Slots come back in shelf order (shelf 1 first, then by column).

The fall run (--fall-all) is the one exception, and it is bounded by date, not by slot. Before the competition
starts (START, 3 Dec 2026 00:00 Eastern) no selection is either team's: course staff stocked the whole machine,
and the inventory server may redraw the split before the start (docs/integrity.md). So a fall run may cover all
40 selections, but only polls before START. check_before_start() refuses any per-selection row, for a selection
outside our assignment, from START on; in a fall run it refuses every row from START on, ours too
(exempt_from_start). The export and the analysis both call it.
"""
import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vending.slots import DEVICE_ID, SLOTS  # noqa: E402  (plain constants; no database needed)

SOURCE_CLI = "command line"
SOURCE_SERVER = "inventory server (stored)"
SOURCE_DEFAULT = "default list in vending/slots.py"
SOURCE_FALL = "all 40 selections, polls before the start only (fall run)"

# The competition starts Thu 3 Dec 2026 (Winter classes start; golisano_calendar_2026_27.json). From then on the
# other team's selections are theirs: nothing reads their per-selection sales, prices or stock.
START = dt.datetime(2026, 12, 3, tzinfo=ZoneInfo("America/New_York"))
ALL_SLOTS = tuple(f"{row}{col}" for row in range(1, 6) for col in range(1, 9))
ASSIGNMENT_SIZE = len(ALL_SLOTS) // 2      # two teams split the machine: 20 selections each


def check_assignment(codes, where):
    """Our assignment in shelf order, checked by shape only, or exit. Missing counts as malformed.

    Exactly ASSIGNMENT_SIZE different codes of the 40-selection machine. It is not bounded by vending.slots.SLOTS:
    the inventory server may redraw the split before the start, and then SLOTS is out of date (#75).
    """
    if not isinstance(codes, (list, tuple)):
        raise SystemExit(f"{where}: no slot assignment recorded ({codes!r}): re-export (python analysis/rebuild.py)")
    problems = []
    bad = [c for c in codes if not isinstance(c, str) or c not in ALL_SLOTS]
    if bad:
        problems.append(f"not a selection of the 40-selection machine: {', '.join(map(repr, bad))}")
    different = len(set(map(str, codes)))
    if len(codes) != ASSIGNMENT_SIZE or different != len(codes):
        problems.append(f"{len(codes)} codes, {different} different; an assignment is "
                        f"{ASSIGNMENT_SIZE} different selections")
    if problems:
        raise SystemExit(f"{where}: the slot assignment is malformed: " + "; ".join(problems))
    return tuple(sorted(codes))


def exempt_from_start(source, assigned):
    """The selections whose rows may be from START on: our assignment, or none at all in a fall run.

    A fall run covers every selection and is bounded by date, so all of its rows must be from before START,
    ours included.
    """
    return () if source == SOURCE_FALL else tuple(assigned)


def check_before_start(rows, assigned=(), where="fall run"):
    """Refuse per-selection data for a selection outside `assigned` from START on. Returns the rows' count.

    `rows` gives (selection, time) pairs; each time must carry its time zone. Selections in `assigned` (our
    own; see exempt_from_start) may have any time; with none given, no row may be from START on. Every other
    selection's row must be from before START, or the whole run stops: no file and no figure may carry the
    other team's per-selection sales, prices or stock from the competition.
    """
    assigned = {str(s) for s in assigned}
    late, n = {}, 0
    for sel, at in rows:
        n += 1
        sel = str(sel)
        if sel in assigned:
            continue
        if getattr(at, "tzinfo", None) is None:
            raise SystemExit(f"{where}: a row for selection {sel} has a time with no time zone ({at!r}); "
                             "can't tell whether it is before the start")
        if at >= START:
            late[sel] = min(at, late.get(sel, at))
    if late:
        first = min(late.values())
        raise SystemExit(f"{where}: per-selection data from the competition for selections that aren't ours "
                         f"({ranges(tuple(sorted(late)))}; first at {first.isoformat()}). Before the start "
                         f"({START:%Y-%m-%d %H:%M} Eastern) no selection is either team's; from it, the other team's "
                         "selections are theirs (docs/integrity.md). Nothing was used.")
    return n


def parse_slots(value, ours=SLOTS):
    """Slot codes from "15,16 17", checked against `ours`. Exits with a message on anything else."""
    codes = check([c for c in str(value).replace(",", " ").split() if c], "--slots", ours)
    if set(codes) != set(ours) and (len({c[0] for c in codes}) < 2 or len({c[1] for c in codes}) < 2):
        raise SystemExit(f"--slots: {ranges(codes)} covers one shelf or one column; the analysis's shelf and column "
                         "models need at least two of each. Narrow to two shelves or more, such as 15,16,17,18,25,26,27,28.")
    return codes


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
    """The stored server assignment, unchecked, or None when there is none (no function, no table, no row).

    It reads the row itself (vending.slots.stored), not vending.slots.our_slots: our_slots refuses an assignment
    outside SLOTS, and this code would then fall back to SLOTS, the old split. resolve_slots checks the shape.
    """
    if conn is None:
        return None
    try:
        from vending.slots import stored
    except (ImportError, AttributeError):
        return None                       # vending.slots can't read a stored assignment yet
    try:
        found = stored(conn)
    except Exception as error:            # e.g. psycopg.errors.UndefinedTable before the migration runs
        if type(error).__name__ not in ("UndefinedTable", "UndefinedColumn", "InFailedSqlTransaction"):
            raise
        _rollback(conn)
        return None
    if found is None:
        return None
    if found.device_id != DEVICE_ID:
        raise SystemExit(f"stored server assignment: recorded for device {found.device_id!r}, but ours is {DEVICE_ID!r}")
    return tuple(found.slots)


def _rollback(conn):
    try:
        conn.rollback()
    except Exception:
        pass


def resolve_slots(cli_value, conn=None, fall=False):
    """(device_id, slots, source, assigned) for this run. See the module docstring for the order.

    `assigned` is our assignment: the stored server answer, checked by shape, else vending.slots.SLOTS.
    fall=True is the fall run: all 40 selections (or --slots narrowed within them). The caller must keep its data
    to polls before START and pass every per-selection row through check_before_start (exempt_from_start).
    """
    stored = from_server(conn)
    if stored is not None:
        assigned = check_assignment(stored, "stored server assignment")
    else:
        assigned = check_assignment(SLOTS, "vending.slots.SLOTS")
    if fall:
        if cli_value is not None:
            return DEVICE_ID, parse_slots(cli_value, ALL_SLOTS), SOURCE_FALL, assigned
        return DEVICE_ID, ALL_SLOTS, SOURCE_FALL, assigned
    if cli_value is not None:
        return DEVICE_ID, parse_slots(cli_value, assigned), SOURCE_CLI, assigned   # only ever a part of our list
    return DEVICE_ID, assigned, SOURCE_DEFAULT if stored is None else SOURCE_SERVER, assigned


def server_assignment_supported():
    """True once vending.slots can read the stored server assignment (#69). Before that, no run read one."""
    try:
        from vending.slots import our_slots  # noqa: F401
    except (ImportError, AttributeError):
        return False
    return True


def ours_for(source, assigned):
    """The list a recorded scope (data/slots.json, lab_data.json) may cover: the assignment recorded with it.

    A fall run may cover all 40 (it is bounded by date instead). Unknown sources are refused, and so is
    a list recorded as the server's answer while this code can't read one (#69).
    """
    if source == SOURCE_FALL:
        return ALL_SLOTS                 # bounded by date instead: check_before_start
    if not (source in (SOURCE_CLI, SOURCE_SERVER) or is_default(source)):
        raise SystemExit(f"slot list recorded with an unknown source {source!r}: re-export (python analysis/rebuild.py)")
    if source == SOURCE_SERVER and not server_assignment_supported():
        raise SystemExit("slot list recorded as the inventory server's answer, but this code can't read one yet (#69): "
                         "re-export (python analysis/rebuild.py)")
    return assigned


def check_scope(device, slots, source, where, assigned):
    """Refuse a recorded scope for another machine, with a missing or malformed assignment, or with a slot that
    isn't ours. Returns (slots, assigned), each in shelf order."""
    if device != DEVICE_ID:
        raise SystemExit(f"{where}: recorded for device {device!r}, but ours is {DEVICE_ID!r}")
    assigned = check_assignment(assigned, f"{where}: assigned_slots")
    if is_default(source) and set(assigned) != set(SLOTS):
        raise SystemExit(f"{where}: recorded as the default list, but its assignment isn't vending.slots.SLOTS: "
                         "re-export (python analysis/rebuild.py)")
    return check(slots, where, ours_for(source, assigned)), assigned


def resolve_run(cli_value, exported, where, now):
    """(device, slots, source, resolved_at, assigned) for the analysis, given --slots and the export's data/slots.json.

    `exported` is that file's contents, or None. The CSVs must have been exported for the same list this run
    uses; otherwise it stops and says to re-export. `assigned` is the assignment recorded there (refused when
    missing or malformed); with no file it is vending.slots.SLOTS.
    """
    recorded = None
    if exported:
        recorded, assigned = check_scope(exported["device"], exported["slots"], exported["source"], where,
                                         exported.get("assigned_slots"))
        if is_default(exported["source"]):
            exported = {**exported, "source": SOURCE_DEFAULT}       # older files carry a longer wording
    else:
        assigned = check_assignment(SLOTS, "vending.slots.SLOTS")
    ours = ours_for(exported["source"], assigned) if exported else assigned
    if cli_value is not None:
        device, slots, source, at = DEVICE_ID, parse_slots(cli_value, ours), SOURCE_CLI, now
        if recorded and set(recorded) == set(slots):
            source, at = exported["source"], exported.get("resolved_at")   # same list as exported: keep where it came from
    elif exported:
        device, slots, source, at = DEVICE_ID, recorded, exported["source"], exported.get("resolved_at")
    else:
        device, slots, source, at = DEVICE_ID, assigned, SOURCE_DEFAULT, None
    if recorded and set(recorded) != set(slots):
        raise SystemExit(
            f"SLOTS: the CSVs were exported for {len(recorded)} slots on {DEVICE_ID} ({ranges(recorded)}), "
            f"but this run asks for {len(slots)} ({ranges(slots)}). Re-export first: "
            f"python analysis/export_sales.py --slots {','.join(slots)}  (or python analysis/rebuild.py --slots ...)")
    return device, slots, source, at, assigned


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
