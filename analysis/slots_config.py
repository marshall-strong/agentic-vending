"""Which slots the analysis covers: one setting, three sources, first one found wins.

    1. --slots on the command line ("15,16,17" or "15 16 17")
    2. the assignment stored from the inventory server (GET /api/v1/slots), read with
       vending.slots.our_slots(conn), when that function exists and a row is stored
    3. vending.slots.SLOTS, the default list

The result is checked: 1 to 40 different codes, each a shelf digit 1-5 then a column digit 1-8, and every
code must be one of OUR slots: vending.slots.SLOTS. A stored server assignment can narrow that list but never
widen it, so when the assignment changes (#69), SLOTS changes with it.
--slots can only narrow the list, never add a slot that isn't ours (docs/integrity.md): the export writes
per-slot prices and sales for every slot on the list.
Slots come back in shelf order (shelf 1 first, then by column).

The fall run (--fall-all) is the one exception, and it is bounded by date, not by slot. Before the competition
starts (START, 3 Dec 2026 00:00 Eastern) no selection is either team's: course staff stocked the whole machine,
and the inventory server may redraw the split before the start (docs/integrity.md). So a fall run may cover all
40 selections, but only polls before START. check_before_start() refuses any per-selection row, for a selection
outside our assignment, from START on; the export and the analysis both call it.
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


def check_before_start(rows, assigned=SLOTS, where="fall run"):
    """Refuse per-selection data for a selection outside `assigned` from START on. Returns the rows' count.

    `rows` gives (selection, time) pairs; each time must carry its time zone. Our own selections may have any
    time. Every other selection's row must be from before START, or the whole run stops: no file and no figure
    may carry the other team's per-selection sales, prices or stock from the competition.
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


def resolve_slots(cli_value, conn=None, fall=False):
    """(device_id, slots, source) for this run. See the module docstring for the order.

    fall=True is the fall run: all 40 selections (or --slots narrowed within them). The caller must keep its data
    to polls before START and pass every per-selection row through check_before_start.
    """
    if fall:
        if cli_value is not None:
            return DEVICE_ID, parse_slots(cli_value, ALL_SLOTS), SOURCE_FALL
        return DEVICE_ID, ALL_SLOTS, SOURCE_FALL
    stored = from_server(conn)
    # The stored answer is bounded by SLOTS as well: when #69 lands, SLOTS must be updated with it.
    ours = check(stored, "stored server assignment", SLOTS) if stored else SLOTS
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
    vending.slots.SLOTS or part of it, whatever source it names (the export's own list, from resolve_slots,
    is held to SLOTS as well). Unknown sources are refused, and so is
    a list recorded as the server's answer while this code can't read one (#69).
    """
    if source == SOURCE_FALL:
        return ALL_SLOTS                 # bounded by date instead: check_before_start
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
