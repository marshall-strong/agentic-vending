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
    patterns += [re.escape(f"{c / 100:,.2f}") for c in (SENTINEL_PRICE, SENTINEL_CENTS, *extra)]
    patterns += [re.escape(f"{c / 100:.2f}") for c in (SENTINEL_PRICE, SENTINEL_CENTS, *extra)]
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
    at `script.taken`. `script.ours` is our assignment. "Now" is the day after the start."""
    module = load_script()
    module.ours = assignment
    module.poll, module.taken = machine_poll(assignment, 13, SENTINEL_PRICE, SENTINEL_UNITS, 7), AFTER

    def call(method, path, **kwargs):
        return module.poll, {}

    def get_json(path, **kwargs):
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


@pytest.mark.parametrize("argv", [["latest"], ["raw", "s1"], ["poll", "--live"]])
def test_the_bridge_script_prints_only_our_selections_of_a_poll_from_the_start(script, capsys, argv):
    assert script.main(argv) == 0
    out, err = capsys.readouterr()
    assert not leaked(out)
    for ours in script.ours:
        assert f"PA1*{ours}*250*\r\nPA2*13*3250*" in out
    for theirs in others(script.ours):
        assert f"PA1*{theirs}*" not in out
    assert "EA2*DO*7*107" in out and "VA1" not in out and "CA2" not in out
    assert out.startswith("DXS*") and out.rstrip().endswith("DXE*1*1") and "\r\n" in out
    assert "left out" in err and "--out" in err


@pytest.mark.parametrize("argv", [["latest", "--unredacted"], ["raw", "s1", "--unredacted"]])
def test_unredacted_prints_the_whole_poll(script, capsys, argv):
    assert script.main(argv) == 0
    out = capsys.readouterr().out
    assert out == script.poll.decode() and leaked(out)   # so `leaked` does find the sentinels


def test_a_poll_from_before_the_start_is_printed_whole(script, capsys):
    script.taken = BEFORE
    assert script.main(["latest"]) == 0
    assert capsys.readouterr().out == script.poll.decode()


def test_out_saves_the_raw_poll_byte_for_byte(script, capsys, tmp_path):
    saved = tmp_path / "poll.dex"
    assert script.main(["latest", "--out", str(saved)]) == 0
    assert saved.read_bytes() == script.poll and capsys.readouterr().out == ""


def test_with_no_stored_assignment_no_selection_is_printed(script, capsys, monkeypatch):
    monkeypatch.setattr(script, "our_selections", lambda: ((), "SlotsNotRecorded: nothing stored"))
    assert script.main(["latest"]) == 0
    out, err = capsys.readouterr()
    assert "PA1" not in out and "PA2" not in out and "EA2*DO*7*107" in out
    assert "no selection is printed" in err


def test_redact_keeps_a_block_only_while_it_is_ours():
    text = "ID4*2\r\nPA1*15*250*\r\nPA2*1*250\r\nPA1*11*987*\r\nPA2*2*1974\r\nPA3*9\r\nVA1*5*1\r\nEA2*DO*1*2\r\n"
    kept, left_out = load_script().redact(text, ("15",))
    assert kept == "ID4*2\r\nPA1*15*250*\r\nPA2*1*250\r\nEA2*DO*1*2\r\n" and left_out == 4
