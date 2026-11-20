"""From the start (`vending.start.START`) a poll carries all 40 selections, and nothing may show the
other team's per-selection data or a machine-wide total that includes their sales (`docs/integrity.md`).
The end-to-end check is `tests/test_post_start_canary.py`; these test each guard on its own."""

import importlib.util
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("psycopg")

from tests.conftest import store_poll  # noqa: E402
from tests.test_evadts import build  # noqa: E402
from vending import db_explorer, pages  # noqa: E402

# Machine-wide VA1 totals no single selection adds up to, so they can't show by chance.
TOTAL_CENTS, TOTAL_UNITS = 987654, 98765


def test_the_public_explorer_hides_the_machine_wide_totals_of_every_poll(schema_url, conn):
    _, schema = schema_url
    store_poll(conn, "s1", "2026-12-04T15:00:00+00:00", {"15": (250, 3)})
    conn.execute("update polls set total_paid_cents = %s, total_paid_units = %s", (TOTAL_CENTS, TOTAL_UNITS))
    request = db_explorer.Request(schema, "polls", 0)
    status, page = db_explorer.render(conn, request, pages.HIDE)
    assert status == 200 and "(hidden)" in page and "s1" in page
    assert str(TOTAL_CENTS) not in page and str(TOTAL_UNITS) not in page
    # Without the rule they would be there: the test sees the page it means to.
    assert str(TOTAL_CENTS) in db_explorer.render(conn, request)[1]


# --- scripts/bridge.py ----------------------------------------------------------------------------

# Assignments the inventory server could give: 20 of the 40, four in each of rows 1-5. They are test
# data, not ours. Our real slots come only from the server (`vending.slots.our_slots`), and they will
# change at least once before the start. Columns 5-8 is the assignment of 12 Nov 2026, which `SLOTS`
# on main still holds. The others aren't columns 5-8, so a test with one of them fails if code
# still assumes columns 5-8: it would drop some of ours or show some of theirs.
ALL_SELECTIONS = tuple(f"{row}{col}" for row in range(1, 6) for col in range(1, 9))


def by_row(*columns: str) -> tuple[str, ...]:
    """An assignment from the four columns each of rows 1-5 has, e.g. by_row("1234", ...)."""
    assert len(columns) == 5 and all(len(c) == 4 for c in columns)
    return tuple(f"{row}{col}" for row, cols in enumerate(columns, start=1) for col in cols)


ASSIGNMENTS = {
    "columns 5-8": by_row("5678", "5678", "5678", "5678", "5678"),
    "columns 1-4": by_row("1234", "1234", "1234", "1234", "1234"),
    "odd columns": by_row("1357", "1357", "1357", "1357", "1357"),
    "mixed": by_row("1278", "3456", "2367", "1458", "1238"),
}
COLUMNS_5_TO_8 = ASSIGNMENTS["columns 5-8"]


def others(ours: tuple[str, ...]) -> tuple[str, ...]:
    """The other team's 20: every selection that isn't in `ours`."""
    return tuple(s for s in ALL_SELECTIONS if s not in ours)


@pytest.fixture(params=list(ASSIGNMENTS.values()), ids=list(ASSIGNMENTS))
def assignment(request) -> tuple[str, ...]:
    """Our selections in this test: each assignment in `ASSIGNMENTS` in turn."""
    return request.param


# The other team's selections in a poll from the start: a price and counters nothing of ours has.
SENTINEL_PRICE, SENTINEL_UNITS = 987, 7919
SENTINEL_CENTS = SENTINEL_PRICE * SENTINEL_UNITS
OUR_PRICE = 250


def machine_poll(ours: tuple[str, ...], our_units: int, their_price: int, their_units: int, door: int) -> bytes:
    """A whole-machine poll as the bridge sends it from the start: all 40 selections, the VA1
    totals over all of them, and the door counter. `ours` sell at `OUR_PRICE`."""
    sold = [(s, OUR_PRICE, our_units) for s in ours] + [(s, their_price, their_units) for s in others(ours)]
    records = ["ID1*TEST-MACHINE*0000**EVS3.0*", "ID4*2*1840*5",
               f"VA1*{sum(p * u for _, p, u in sold)}*{sum(u for *_, u in sold)}*0*0",
               "CA2*1234*56*0*0", f"EA2*DO*{door}*{door + 100}"]
    for slot, price, units in sorted(sold):
        records += [f"PA1*{slot}*{price}*", f"PA2*{units}*{price * units}*0*0*0*0"]
    return build(records)


def leaked(text: str, *extra: int) -> list[str]:
    """Every sentinel in `text`: the other team's price, counters and sales, and any `extra`
    figure (a machine-wide total), as a number or as dollars."""
    figures = (SENTINEL_PRICE, SENTINEL_UNITS, SENTINEL_CENTS, *extra)
    patterns = [rf"(?<![\w.]){n}(?![\w])" for n in figures]
    # Dollars stand alone too: "59.878255" in a clock time is not $9.87.
    patterns += [rf"(?<![\w.,]){re.escape(f'{c / 100:,.2f}')}(?![\w])" for c in (SENTINEL_PRICE, SENTINEL_CENTS, *extra)]
    patterns += [rf"(?<![\w.,]){re.escape(f'{c / 100:.2f}')}(?![\w])" for c in (SENTINEL_PRICE, SENTINEL_CENTS, *extra)]
    return [p for p in patterns if re.search(p, text)]


def load_script():
    spec = importlib.util.spec_from_file_location(
        "bridge_script", Path(__file__).resolve().parent.parent / "scripts" / "bridge.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module



# Poll times on each side of the fall cut (FALL_CUTOFF, 18 Nov 2026 00:00 Eastern when unset) and the
# start. scripts/bridge.py prints every one of them the same way: only our selections and the door counter
# (docs/integrity.md). Pre-cut fall data for all 40 comes from `analysis/export_sales.py --fall-all`.
AFTER, BEFORE = "2026-12-04T15:00:00+00:00", "2026-11-10T15:00:00+00:00"
BETWEEN, LAST_FALL_DAY = "2026-11-20T15:00:00+00:00", "2026-12-02T15:00:00+00:00"
JUST_BEFORE_THE_CUT = "2026-11-18T04:59:50+00:00"    # 17 Nov, 23:59:50 Eastern
EVERY_TIME = (BEFORE, JUST_BEFORE_THE_CUT, BETWEEN, LAST_FALL_DAY, AFTER)


def http_date(moment: str) -> str:
    """`moment` as an HTTP `Date` header gives it (RFC 9110: GMT, to the second)."""
    from email.utils import format_datetime

    return format_datetime(datetime.fromisoformat(moment).astimezone(timezone.utc), usegmt=True)


@pytest.fixture
def script(monkeypatch, assignment):
    """scripts/bridge.py, answering from a fake bridge with one stored poll, `script.poll`, taken
    at `script.taken` by every account the bridge gives: the listing's `started_at`, a `polls` table
    lookup, and a live read's `Date` header. The script must print the same whatever they say.
    `script.listing` replaces the whole listing. `script.ours` is our assignment. "Now", the
    client's clock, is `script.now`, the day after the start unless a test moves it. The fake
    answers only the history and poll routes, so a command that asks `/api/v1/slots` which
    selections are ours fails: ours come from the stored assignment only."""
    module = load_script()
    module.ours = assignment
    module.poll, module.taken = machine_poll(assignment, 13, SENTINEL_PRICE, SENTINEL_UNITS, 7), AFTER
    module.calls = []
    module.now = datetime(2026, 12, 4, 15, 0, tzinfo=timezone.utc)
    module.listing = module.stored = module.live_headers = None

    def call(method, path, **kwargs):
        module.calls.append(path)
        assert path.startswith(module.POLL_ROUTES), f"the script asked {path} during a poll command"
        if path == "/api/v1/poll":
            headers = module.live_headers
            return module.poll, ({"Date": http_date(module.taken)} if headers is None else headers)
        return module.poll, {}

    def get_json(path, **kwargs):
        module.calls.append(path)
        assert path == "/api/v1/history", f"the script asked {path} during a poll command"
        if module.listing is not None:
            return module.listing
        return {"polls": [{"session_id": "s1", "started_at": module.taken}]}

    def stored_poll_time(session_id):
        module.calls.append(f"polls table: {session_id}")
        if module.stored is not None:
            return module.stored
        return datetime.fromisoformat(module.taken), None

    class Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return module.now

    monkeypatch.setattr(module, "call", call)
    monkeypatch.setattr(module, "get_json", get_json)
    monkeypatch.setattr(module, "datetime", Now)
    module.stored_selections = module.our_selections   # the real one, for tests that store an assignment
    module.real_stored_poll_time = getattr(module, "stored_poll_time", None)   # the real one, likewise
    monkeypatch.setattr(module, "stored_poll_time", stored_poll_time, raising=False)
    monkeypatch.setattr(module, "our_selections", lambda: (assignment, None))
    return module


def rebuilt(ours: tuple[str, ...], door: str = "EA2*DO*7*107") -> str:
    """Exactly what the script prints of `machine_poll(ours, 13, ...)` from the start: ID4's decimal
    places, the door counter, then each of ours in the poll's order, rebuilt from its fields."""
    lines = ["ID4*2", door] + [r for s in sorted(ours) for r in (f"PA1*{s}*250", "PA2*13*3250*0*0*0*0")]
    return "".join(line + "\r\n" for line in lines)


@pytest.mark.parametrize("argv", [["latest"], ["raw", "s1"], ["poll", "--live"]])
def test_the_bridge_script_prints_only_our_selections_of_a_poll_from_the_start(script, capsys, argv):
    assert script.main(argv) == 0
    out, err = capsys.readouterr()
    assert out == rebuilt(script.ours)
    assert not leaked(out) and not leaked(err)
    assert "20 of our selections" in err and "left out" in err and "--out" in err


@pytest.mark.parametrize("argv", [["latest", "--unredacted"], ["raw", "s1", "--unredacted"],
                                  ["poll", "--live", "--unredacted"]])
def test_nothing_prints_the_whole_of_a_poll_from_the_start(script, capsys, argv):
    with pytest.raises(SystemExit) as refused:
        script.main(argv)
    out, err = capsys.readouterr()
    assert refused.value.code == 2 and out == "" and "unrecognized arguments: --unredacted" in err
    assert script.calls == []


POLL_ARGVS = [["latest"], ["raw", "s1"], ["poll", "--live"]]


def at(script, taken: str) -> None:
    """The stored poll was taken at `taken`, by every account of it, and the client's clock says so too."""
    script.taken, script.now = taken, datetime.fromisoformat(taken)


def http_calls(script) -> list[str]:
    """The requests the script sent to the bridge (not the `polls` table lookups)."""
    return [c for c in script.calls if c.startswith("/")]


def machine_totals(script) -> tuple[int, int]:
    """The VA1 totals of `script.poll`: the machine-wide cents and units."""
    va1 = next(line for line in script.poll.decode().splitlines() if line.startswith("VA1*"))
    return int(va1.split("*")[1]), int(va1.split("*")[2])


@pytest.mark.parametrize("taken", EVERY_TIME)
@pytest.mark.parametrize("argv", POLL_ARGVS)
def test_every_poll_prints_only_our_selections_whatever_its_time(script, capsys, argv, taken):
    """The bridge's times can be wrong (a slow or proxied `Date`, a listing time with no offset), so
    the script never decides from them that a poll may print whole: before the cut, between the cut
    and the start, and after, it prints only ours and the door counter."""
    at(script, taken)
    assert script.main(argv) == 0
    out, err = capsys.readouterr()
    assert out == rebuilt(script.ours)
    assert not leaked(out, *machine_totals(script)) and not leaked(err, *machine_totals(script))
    assert "20 of our selections" in err and "analysis/export_sales.py --fall-all" in err


@pytest.mark.parametrize("value", ["18 Nov", "invalid", "2026-11-01T00:00:00-05:00", "2026-12-20T00:00:00-05:00"])
@pytest.mark.parametrize("argv", POLL_ARGVS)
def test_fall_cutoff_changes_nothing_the_script_prints(script, capsys, monkeypatch, argv, value):
    """The script no longer reads the cut, so no FALL_CUTOFF, moved or malformed, opens a poll."""
    monkeypatch.setenv("FALL_CUTOFF", value)
    at(script, BEFORE)
    assert script.main(argv) == 0
    out, err = capsys.readouterr()
    assert out == rebuilt(script.ours) and not leaked(out, *machine_totals(script))


@pytest.mark.parametrize("taken", EVERY_TIME)
def test_out_takes_only_a_regular_file_whatever_the_polls_time(script, capsys, tmp_path, monkeypatch, taken):
    at(script, taken)
    monkeypatch.chdir(tmp_path)
    assert script.main(["latest", "--out", "-"]) == script.EXIT_REFUSED
    out, err = capsys.readouterr()
    assert out == "" and "regular file" in err and not leaked(err) and list(tmp_path.iterdir()) == []


def test_the_help_says_where_whole_fall_polls_come_from(capsys):
    with pytest.raises(SystemExit):
        load_script().main(["--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert "never prints a whole poll" in help_text and "analysis/export_sales.py --fall-all" in help_text


# --- What a reply may put on the screen ---------------------------------------------------------------

def test_history_prints_only_the_listings_known_fields(script, capsys):
    import json

    whole = script.poll.decode()
    script.listing = {"polls": [{"session_id": "s1", "started_at": BETWEEN, "bytes": 4341, "raw": whole},
                                {"session_id": "s2", "started_at": BETWEEN, "bytes": "PA1*11*987*"},
                                {"session_id": "PA1*11*987*", "started_at": BETWEEN},
                                {"session_id": "s3", "started_at": "VA1*987654"}],
                      "truncated": False, "raw": whole, "note": f"VA1*{machine_totals(script)[0]}"}
    assert script.main(["history"]) == 0
    out, err = capsys.readouterr()
    assert not leaked(out, *machine_totals(script)) and not leaked(err, *machine_totals(script))
    assert "PA1*" not in out + err and "VA1" not in out + err
    assert json.loads(out) == {"polls": [
        {"session_id": "s1", "started_at": "2026-11-20T15:00:00+00:00", "bytes": 4341},
        {"session_id": "s2", "started_at": "2026-11-20T15:00:00+00:00", "bytes": None},
        {"session_id": "s3", "started_at": None, "bytes": None}], "truncated": False}
    assert "1 entry left out" in err


@pytest.mark.parametrize("listing", [["PA1*11*987*"], "VA1*987654", {"polls": "VA1*987654"}, {"polls": [1]}])
def test_history_refuses_a_listing_of_the_wrong_shape(script, capsys, listing):
    script.listing = listing
    assert script.main(["history"]) == script.EXIT_BRIDGE
    out, err = capsys.readouterr()
    assert out == "" and "PA1*" not in err and "VA1" not in err and not leaked(err, 987654)


@pytest.mark.parametrize("entry", [
    {"session_id": f"PA1*11*{SENTINEL_PRICE}*", "started_at": BETWEEN},
    {"session_id": "s1\nVA1*987654", "started_at": BETWEEN},
    {"session_id": ["s1"], "started_at": BETWEEN},
    {"started_at": BETWEEN}])
def test_latest_refuses_a_listing_whose_newest_session_isnt_an_id(script, capsys, entry):
    script.listing = {"polls": [entry]}
    assert script.main(["latest"]) == script.EXIT_BRIDGE
    out, err = capsys.readouterr()
    assert out == "" and not leaked(err, 987654) and "PA1*" not in err and "VA1" not in err
    assert http_calls(script) == ["/api/v1/history"]


@pytest.mark.parametrize("started_at", [f"PA1*11*{SENTINEL_PRICE}*", "2026-11-10T15:00:00", None, 7919])
def test_latest_with_an_unreadable_time_prints_no_raw_time(script, capsys, started_at):
    script.listing = {"polls": [{"session_id": "s1", "started_at": started_at}]}
    assert script.main(["latest"]) == 0
    out, err = capsys.readouterr()
    assert out == rebuilt(script.ours)
    assert not leaked(out, *machine_totals(script)) and not leaked(err, *machine_totals(script))
    assert "PA1*" not in err and "session s1, taken at an unknown time" in err


def test_latest_prints_the_session_and_time_it_checked(script, capsys):
    at(script, BETWEEN)
    assert script.main(["latest"]) == 0
    assert "session s1, taken 2026-11-20T15:00:00+00:00" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["latest"], ["raw", "s1"], ["poll", "--live"]])
def test_out_saves_the_raw_poll_byte_for_byte(script, capsys, tmp_path, monkeypatch, argv):
    saved = tmp_path / "poll.dex"
    saved.write_bytes(b"x" * (len(script.poll) + 100))   # a longer file there already is replaced whole
    monkeypatch.chdir(tmp_path)   # named relative to it: "wrote ... to" prints the name, never the temp folder
    assert script.main([*argv, "--out", "poll.dex"]) == 0
    out, err = capsys.readouterr()
    assert saved.read_bytes() == script.poll and out == "" and not leaked(err)


TARGETS = ("-", "CON", "con.txt", "nul", "COM1", "lpt3.dex", "CONOUT$", "a device path", "NUL in a folder",
           "a directory", "/dev/stdout", "/dev/tty", "a pipe", "a link to a device")


def _target(name: str, tmp_path: Path) -> str:
    """The `--out` target `name` stands for, made in `tmp_path` where it has to be made, and named
    relative to it (the test runs from there): an absolute path would print the temp folder, and
    any folder above could hold a number `leaked` reads as a sentinel."""
    import os

    if name == "a device path":
        return "\\\\.\\CON"
    if name == "NUL in a folder":
        return "NUL.dex"
    if name == "a directory":
        return "."
    if name == "a pipe":
        if not hasattr(os, "mkfifo"):
            pytest.skip("this system can't make a named pipe")
        os.mkfifo(tmp_path / "pipe")
        return "pipe"
    if name == "a link to a device":
        if os.name != "posix" or not Path("/dev/null").exists():
            pytest.skip("this system has no /dev/null to link to")
        (tmp_path / "link").symlink_to("/dev/null")
        return "link"
    return name


def test_an_out_target_names_no_temp_folder(tmp_path):
    """pytest numbers its temp folders by run, and a run named pytest-987 printed the folder in an
    --out error, so `leaked` matched the sentinel price and failed these tests at random (20 in one
    run). Any folder above could hold such a number, so a target names no folder at all."""
    folder = tmp_path / f"run-{SENTINEL_PRICE}"
    folder.mkdir()
    for name in ("NUL in a folder", "a directory"):
        assert not leaked(_target(name, folder)), _target(name, folder)


@pytest.mark.parametrize("name", TARGETS)
def test_out_refuses_anything_but_a_regular_file(script, capsys, tmp_path, monkeypatch, name):
    target = _target(name, tmp_path)
    monkeypatch.chdir(tmp_path)   # a relative name that got through would land here, not in the repo
    before = sorted(p.name for p in tmp_path.iterdir())
    assert script.main(["latest", "--out", target]) == script.EXIT_REFUSED
    out, err = capsys.readouterr()
    assert out == "" and not leaked(err) and "regular file" in err, err
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_a_failed_assignment_read_prints_no_error_text(script, capsys, monkeypatch):
    """A database error can quote what it was given; the note names the error's kind only."""
    from vending import db

    def broken():
        raise RuntimeError(f"PA1*11*{SENTINEL_PRICE}* VA1*987654")

    monkeypatch.setattr(script, "our_selections", script.stored_selections)
    monkeypatch.setattr(db, "connect", broken)
    assert script.main(["latest"]) == 0
    out, err = capsys.readouterr()
    assert out == "ID4*2\r\nEA2*DO*7*107\r\n" and "RuntimeError" in err
    assert "PA1*" not in err and "VA1" not in err and not leaked(err, 987654)


def test_with_no_stored_assignment_no_selection_is_printed(script, capsys, monkeypatch):
    monkeypatch.setattr(script, "our_selections", lambda: ((), "SlotsNotRecorded: nothing stored"))
    assert script.main(["latest"]) == 0
    out, err = capsys.readouterr()
    assert out == "ID4*2\r\nEA2*DO*7*107\r\n"
    assert "no selection is printed" in err and "0 of our selections" in err and not leaked(err)


def test_the_summary_reads_only_our_selections_from_the_stored_assignment(script, capsys, monkeypatch):
    import json

    read = []   # every field `summarize` turns into a number
    real_number = script.number
    monkeypatch.setattr(script, "number", lambda field: read.append(field) or real_number(field))
    assert script.main(["latest", "--summary"]) == 0
    out, err = capsys.readouterr()
    rows = json.loads(out)
    assert [r["slot"] for r in rows] == list(script.ours)
    assert {(r["price"], r["lifetime_units"], r["lifetime_sales"]) for r in rows} == {("$2.50", 13, "$32.50")}
    assert not leaked(out) and not leaked(err)
    # Not one field of theirs was read: only ID4's decimal places, and our prices and counters.
    assert sorted(set(read)) == ["13", "2", "250", "3250"]
    assert "/api/v1/slots" not in script.calls


def test_the_summary_with_no_stored_assignment_reads_no_poll(script, capsys, monkeypatch):
    monkeypatch.setattr(script, "our_selections", lambda: ((), "SlotsNotRecorded: nothing stored"))
    assert script.main(["latest", "--summary"]) == 0
    out, err = capsys.readouterr()
    assert out.strip() == "[]" and "no selection is printed" in err and script.calls == []


def test_summarize_skips_a_selection_not_ours_before_reading_its_fields():
    text = "ID4*2\r\nPA1*11*not-a-price*\r\nPA2*x*y\r\nPA1*15*250*\r\nPA2*3*750*0\r\nPA1*16*\r\nPA2*\r\n"
    assert load_script().summarize(text, ("15", "16", "17")) == [
        {"slot": "15", "price": "$2.50", "lifetime_units": 3, "lifetime_sales": "$7.50"},
        {"slot": "16", "price": None, "lifetime_units": None, "lifetime_sales": None},
        {"slot": "17", "price": None, "lifetime_units": None, "lifetime_sales": None},
    ]


def test_rebuild_writes_only_checked_fields_of_ours_and_the_door():
    text = ("DXS*X*VA*V0/6*1\nST*001*0001\nID1*SERIAL*MODEL\nID4*2*1840*5\nVA1*98765*432\n"
            "PA1*15*250*cap*PA1-04\nPA2*3*750*1*2*note*9\nPA3*9*9\nPA5*20261204*1200\n"
            "PA1*11*987*\nPA2*7919*7815*0\nZZ9*15*987\nPA2*4*4\nEA2*DO*7*107*12/04\nEA2*CA*1\n"
            "PA1*16*x\nPA2*y\nG85*ABCD\nSE*20*0001\nDXE*1*1\n")
    shown, printed, left_out = load_script().rebuild(text, ("15", "16"))
    # A field that isn't a number ends its record there; a PA2 outside our block, PA3 and the
    # rest are gone. `\n` in, `\n` out.
    assert shown == "ID4*2\nPA1*15*250\nPA2*3*750*1*2\nEA2*DO*7*107\nPA1*16\n"
    assert (printed, left_out) == (2, 15)


# --- Errors from the bridge -----------------------------------------------------------------------

def _failing(monkeypatch, code: int, body: bytes):
    """The real `call`, against a bridge that answers every request with `code` and `body`."""
    import io
    import urllib.error
    import urllib.request

    module = load_script()
    monkeypatch.setenv("DEX_BASE_URL", "http://bridge.test")
    monkeypatch.setenv("DEX_API_KEY", "key")

    def urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, code, "Reason From The Bridge", {}, io.BytesIO(body))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return module


SECRET = f"PA1*11*{SENTINEL_PRICE}*".encode()


@pytest.mark.parametrize("argv", [["raw", "s1"], ["poll", "--live"], ["latest"], ["history"]])
@pytest.mark.parametrize("code", [400, 401, 404, 409, 429, 500, 599])
def test_a_poll_routes_error_shows_the_status_and_a_short_reason_only(monkeypatch, capsys, argv, code):
    module = _failing(monkeypatch, code, b'{"detail": "' + SECRET + b'"}')
    assert module.main(argv) != 0
    out, err = capsys.readouterr()
    assert out == "" and str(code) in err
    assert SECRET.decode() not in err and "Reason From The Bridge" not in err and not leaked(err)


@pytest.mark.parametrize("argv", [["health"], ["slots"], ["products"], ["request", "r1"]])
@pytest.mark.parametrize("code", [400, 401, 404, 409, 422, 429, 500])
def test_no_routes_error_body_is_printed(monkeypatch, capsys, argv, code):
    """Any route's error reply could carry a poll's records, so none is echoed: the status, its
    standard phrase and a reason written here."""
    body = b'{"detail": "' + SECRET + b'", "error": "VA1*987654*98765"}'
    module = _failing(monkeypatch, code, body)
    assert module.main(argv) != 0
    out, err = capsys.readouterr()
    assert out == "" and str(code) in err
    assert SECRET.decode() not in err and "VA1" not in err and "Reason From The Bridge" not in err
    assert not leaked(err, 987654, 98765)


# --- The metadata routes: only the fields they are documented to have ----------------------------

HIDDEN = f"PA1*11*{SENTINEL_PRICE}*\r\nPA2*{SENTINEL_UNITS}*{SENTINEL_CENTS}\r\nVA1*987654*98765"


@pytest.fixture
def bridge(monkeypatch):
    """scripts/bridge.py with the real `get_json`, over a fake `call` that answers each path from
    `bridge.replies` (JSON) and records it in `bridge.calls`."""
    import json

    module = load_script()
    module.replies, module.calls = {}, []

    def call(method, path, **kwargs):
        module.calls.append(path)
        return json.dumps(module.replies[path]).encode(), {}

    monkeypatch.setattr(module, "call", call)
    return module


def shown(capsys) -> tuple[object, str]:
    """(stdout as JSON, stderr), after checking neither carries a hidden record."""
    import json

    out, err = capsys.readouterr()
    for text in (out, err):
        assert "PA1*" not in text and "PA2*" not in text and "VA1" not in text, text
        assert not leaked(text, 987654, 98765), text
    return json.loads(out), err


def test_health_prints_only_its_status(bridge, capsys):
    bridge.replies["/health"] = {"status": "ok", "raw": HIDDEN, "detail": {"last": HIDDEN}}
    assert bridge.main(["health"]) == 0
    assert shown(capsys)[0] == {"status": "ok"}
    bridge.replies["/health"] = {"status": HIDDEN}
    assert bridge.main(["health"]) == 0
    assert shown(capsys)[0] == {"status": None}


def test_slots_prints_only_our_teams_checked_assignment(bridge, capsys):
    ours = list(COLUMNS_5_TO_8)
    bridge.replies["/api/v1/slots"] = {"team": "group-b", "device_id": "vmc1", "slots": ours, "raw": HIDDEN}
    assert bridge.main(["slots"]) == 0
    assert shown(capsys)[0] == {"team": "group-b", "device_id": "vmc1", "slots": sorted(ours)}


@pytest.mark.parametrize("reply", [
    {"team": "group-a", "device_id": "vmc1", "slots": list(COLUMNS_5_TO_8)},
    {"team": HIDDEN, "device_id": "vmc1", "slots": list(COLUMNS_5_TO_8)},
    {"team": "group-b", "device_id": HIDDEN, "slots": list(COLUMNS_5_TO_8)},
    {"team": "group-b", "device_id": "vmc1", "slots": [*COLUMNS_5_TO_8[:19], HIDDEN]},
    {"team": "group-b", "device_id": "vmc1"}, [HIDDEN], HIDDEN])
def test_slots_refuses_any_other_reply_and_prints_none_of_it(bridge, capsys, reply):
    bridge.replies["/api/v1/slots"] = reply
    assert bridge.main(["slots"]) == bridge.EXIT_BRIDGE
    out, err = capsys.readouterr()
    assert out == "" and "PA1*" not in err and "VA1" not in err and not leaked(err, 987654)
    assert "group-a" not in err


@pytest.mark.parametrize("wrapped", [True, False])
def test_products_prints_only_the_catalog_fields(bridge, capsys, wrapped):
    items = [{"sku": "SKU-1", "name": "Cola", "line": "Classic", "flavor": "Cherry", "size": "12 oz",
              "price": SENTINEL_PRICE, "raw": HIDDEN},
             {"sku": "SKU-2", "name": HIDDEN, "line": None, "flavor": "Lime\nVA1*987654", "size": 12},
             HIDDEN]
    bridge.replies["/api/v1/products"] = {"products": items, "raw": HIDDEN} if wrapped else items
    assert bridge.main(["products"]) == 0
    printed, err = shown(capsys)
    assert printed == [
        {"sku": "SKU-1", "name": "Cola", "line": "Classic", "flavor": "Cherry", "size": "12 oz"},
        {"sku": "SKU-2", "name": None, "line": None, "flavor": None, "size": None}]
    assert "1 entry left out" in err


def test_request_prints_no_free_text(bridge, capsys):
    bridge.replies["/api/v1/purchase-requests/r1"] = {
        "id": "r1", "status": "rejected", "created_at": "2026-11-20T15:00:00+00:00",
        "decision_note": HIDDEN, "raw": HIDDEN}
    assert bridge.main(["request", "r1"]) == 0
    printed, _ = shown(capsys)
    assert printed == {"id": "r1", "status": "rejected", "created_at": "2026-11-20T15:00:00+00:00",
                       "decision_note": f"(note omitted, {len(HIDDEN)} characters)"}


def test_a_fulfilled_request_prints_its_receipts_checked(bridge, capsys):
    receipt = {"outcome": "loaded", "sku": "SKU-1", "store": "Wegmans", "packs_bought": 2, "units_per_pack": 12,
               "units_bought": 24, "total_paid": "18.50", "unit_cost_paid": "0.7708", "units_loaded": 20,
               "units_lost": 0, "loss_note": HIDDEN, "units_to_back_stock": 4, "units_from_back_stock": 0,
               "units_pulled": 0, "pulled_sku": None, "slot_reassigned": False,
               "resolved_at": "2026-11-20T16:00:00+00:00", "later_losses": [{"note": HIDDEN}], "raw": HIDDEN}
    bridge.replies["/api/v1/purchase-requests/r2"] = {
        "id": "r2", "status": "fulfilled", "device_id": "vmc1", "created_at": "2026-11-20T15:00:00+00:00",
        "updated_at": "2026-11-20T16:00:00+00:00", "fulfilled_at": "2026-11-20T16:00:00+00:00",
        "off_cycle": False, "note": HIDDEN, "decision_note": None, "total_paid": "18.50",
        "door_opened_at": "2026-11-20T15:40:00+00:00", "door_closed_at": "2026-11-20T15:55:00+00:00",
        "lines": [{"line": 1, "slot": "15", "source": "store", "primary": HIDDEN, "receipt": receipt}, HIDDEN]}
    assert bridge.main(["request", "r2"]) == 0
    printed, err = shown(capsys)
    line = printed["lines"][0]
    assert printed["note"] == f"(note omitted, {len(HIDDEN)} characters)" and printed["decision_note"] is None
    assert printed["total_paid"] == "18.50" and printed["door_opened_at"] == "2026-11-20T15:40:00+00:00"
    assert line["slot"] == "15" and "primary" not in line and len(printed["lines"]) == 1
    assert line["receipt"]["units_loaded"] == 20 and line["receipt"]["unit_cost_paid"] == "0.7708"
    assert line["receipt"]["loss_note"] == f"(note omitted, {len(HIDDEN)} characters)"
    assert line["receipt"]["later_losses"] == 1 and "raw" not in line["receipt"]
    assert "1 line left out" in err


# --- A reply that isn't HTTP ------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [["health"], ["slots"], ["history"], ["raw", "s1"], ["poll", "--live"],
                                  ["request", "r1"]])
@pytest.mark.parametrize("kind", ["BadStatusLine", "LineTooLong", "IncompleteRead", "RemoteDisconnected"])
def test_a_reply_that_isnt_http_prints_a_fixed_message(monkeypatch, capsys, argv, kind):
    import http.client
    import urllib.request

    module = load_script()
    monkeypatch.setenv("DEX_BASE_URL", "http://bridge.test")
    monkeypatch.setenv("DEX_API_KEY", "key")
    errors = {"BadStatusLine": lambda: http.client.BadStatusLine(HIDDEN),
              "LineTooLong": lambda: http.client.LineTooLong(HIDDEN),
              "IncompleteRead": lambda: http.client.IncompleteRead(HIDDEN.encode()),
              "RemoteDisconnected": lambda: http.client.RemoteDisconnected(HIDDEN)}

    def urlopen(request, timeout):
        raise errors[kind]()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(module, "our_selections", lambda: (COLUMNS_5_TO_8, None))
    assert module.main(argv) in (module.EXIT_BRIDGE, module.EXIT_UNREACHABLE)
    out, err = capsys.readouterr()
    assert out == "" and "PA1*" not in err and "VA1" not in err and not leaked(err, 987654, 98765), err
