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



AFTER, BEFORE = "2026-12-04T15:00:00+00:00", "2026-12-02T15:00:00+00:00"


@pytest.fixture
def script(monkeypatch, assignment):
    """scripts/bridge.py, answering from a fake bridge with one stored poll, `script.poll`, taken
    at `script.taken`. `script.ours` is our assignment. "Now" is the day after the start. The
    fake answers only the history and poll routes, so a command that asks `/api/v1/slots` which
    selections are ours fails: ours come from the stored assignment only."""
    module = load_script()
    module.ours = assignment
    module.poll, module.taken = machine_poll(assignment, 13, SENTINEL_PRICE, SENTINEL_UNITS, 7), AFTER
    module.calls = []

    def call(method, path, **kwargs):
        module.calls.append(path)
        assert path.startswith(module.POLL_ROUTES), f"the script asked {path} during a poll command"
        return module.poll, {}

    def get_json(path, **kwargs):
        module.calls.append(path)
        assert path == "/api/v1/history", f"the script asked {path} during a poll command"
        return {"polls": [{"session_id": "s1", "started_at": module.taken}]}

    class Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 12, 4, 15, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(module, "call", call)
    monkeypatch.setattr(module, "get_json", get_json)
    monkeypatch.setattr(module, "datetime", Now)
    module.stored_selections = module.our_selections   # the real one, for tests that store an assignment
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


def test_a_poll_from_before_the_start_is_printed_whole(script, capsys):
    script.taken = BEFORE
    assert script.main(["latest"]) == 0
    assert capsys.readouterr().out == script.poll.decode()


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


def test_out_before_the_start_is_as_it_was(script, capsys, tmp_path):
    script.taken = BEFORE
    saved = tmp_path / "poll.dex"
    assert script.main(["latest", "--out", str(saved)]) == 0
    assert saved.read_bytes() == script.poll


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


def test_other_routes_still_say_what_the_bridge_said(monkeypatch, capsys):
    module = _failing(monkeypatch, 422, b'{"detail": "no such request"}')
    assert module.main(["request", "r1"]) == module.EXIT_BRIDGE
    assert "422: no such request" in capsys.readouterr().err
