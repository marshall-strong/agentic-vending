"""Tests for `vending.evadts`.

The synthetic documents below are built in the test, so no machine data is
committed (the repo is public). `test_real_snapshots` also runs every `*.dex`
file under `data/` when you have the snapshot archive locally.
"""

from pathlib import Path

import pytest

from vending.evadts import ParseError, crc16_arc, parse

REPO = Path(__file__).resolve().parent.parent


def build(records: list[str], *, corrupt: bool = False) -> bytes:
    """Wrap records in DXS/ST ... G85/SE/DXE with a correct (or broken) checksum."""
    head = b"DXS*0000000000*VA*V1/1*1**\r\n"
    body = ("\r\n".join(["ST*001*0001", *records]) + "\r\n").encode("ascii")
    crc = crc16_arc(body) ^ (1 if corrupt else 0)
    tail = f"G85*{crc:04X}\r\nSE*{len(records) + 3}*0001\r\nDXE*1*1\r\n".encode("ascii")
    return head + body + tail


SAMPLE = [
    "ID1*TEST-MACHINE*0000**EVS3.0*",
    "ID4*2*1840*5",
    "VA1*12345*42*0*0",
    "PA1*15*450*",
    "PA2*30*13500*0*0*0*0",
    "PA1*16**",
    "PA2*12*0*0*0*0*0",
]


def test_crc16_arc_check_value():
    # The standard check value for CRC-16/ARC.
    assert crc16_arc(b"123456789") == 0xBB3D


def test_parses_counters_and_prices():
    poll = parse(build(SAMPLE))
    assert poll.crc_ok
    assert poll.machine_serial == "TEST-MACHINE"
    assert poll.decimal_places == 2
    assert poll.total_paid_cents == 12345
    assert poll.total_paid_units == 42
    by_slot = {s.selection: s for s in poll.selections}
    assert by_slot["15"].price_cents == 450
    assert by_slot["15"].lifetime_units == 30
    assert by_slot["15"].lifetime_sales_cents == 13500
    assert by_slot["16"].price_cents is None


def test_serial_after_a_placeholder_field():
    # Our machine writes ">" in ID1-01 and the serial in ID1-02.
    records = [r if not r.startswith("ID1") else "ID1*>*RVV_NG 00000-00*0000**EVS3.0*" for r in SAMPLE]
    assert parse(build(records)).machine_serial == "RVV_NG 00000-00"


def test_bad_checksum_is_reported_not_raised():
    poll = parse(build(SAMPLE, corrupt=True))
    assert not poll.crc_ok
    assert poll.crc_expected != poll.crc_actual


def test_decimal_places_scale_to_cents():
    records = [r.replace("ID4*2*", "ID4*1*") for r in SAMPLE]
    poll = parse(build(records))
    assert poll.decimal_places == 1
    assert poll.total_paid_cents == 123450


def test_truncated_document_is_rejected():
    raw = build(SAMPLE)
    with pytest.raises(ParseError):
        parse(raw[: raw.index(b"DXE")])


def test_not_evadts_is_rejected():
    with pytest.raises(ParseError):
        parse(b"<html>nope</html>")


@pytest.mark.parametrize(
    "path",
    sorted((REPO / "data").rglob("*.dex")) or [pytest.param(None, marks=pytest.mark.skip("no local snapshots"))],
)
def test_real_snapshots(path):
    """Every archived poll passes its checksum, and per-selection sales sum to VA1."""
    poll = parse(path.read_bytes())
    assert poll.crc_ok, f"{path.name}: G85 {poll.crc_expected} but computed {poll.crc_actual}"
    assert sum(s.lifetime_sales_cents for s in poll.selections) == poll.total_paid_cents
    assert sum(s.lifetime_units for s in poll.selections) == poll.total_paid_units
