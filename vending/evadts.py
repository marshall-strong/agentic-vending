"""Parse an EVA-DTS document from the machine into the counters we store.

A poll is about 210 `*`-separated records, one per CRLF-terminated line, from
`DXS` to `DXE`. We keep only what the agent builds on:

- `ID1`: the machine's serial number (`ID1-01`, or `ID1-02` when `ID1-01` is `>`).
- `ID4-01`: how many decimal places every money figure carries (2 on our machine).
- `PA1`: opens a selection: `PA1*<selection>*<price>`.
- `PA2`: that selection's **lifetime** counters: `PA2*<units>*<value>*...`.
- `VA1`: machine-wide lifetime paid vends: `VA1*<value>*<units>*...`.

Lifetime counters never reset, so sales over a period are the difference between
two polls. The interval counters later in `PA2` and the vendor-specific `MA5`
records are deliberately ignored (see `docs/dex-bridge.md`).

**Checksum.** `G85` is CRC-16/ARC (polynomial 0xA001 reflected, initial value 0)
over every byte from the start of the `ST` record up to, not including, the
`G85` record, CR and LF included, written as four uppercase hex digits. Verified
against real polls on 22 Oct 2026. A document that fails the check is still
stored raw, but none of its counters are trusted.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class ParseError(ValueError):
    """The document is not EVA-DTS we can read."""


@dataclass(frozen=True)
class Selection:
    selection: str
    price_cents: int | None
    lifetime_units: int
    lifetime_sales_cents: int


@dataclass(frozen=True)
class Poll:
    crc_ok: bool
    crc_expected: str | None
    crc_actual: str
    machine_serial: str | None
    decimal_places: int | None  # None when the checksum failed
    total_paid_cents: int | None
    total_paid_units: int | None
    selections: list[Selection] = field(default_factory=list)


def crc16_arc(data: bytes) -> int:
    """CRC-16/ARC: reflected polynomial 0xA001, initial value 0, no final XOR."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _checksum(raw: bytes) -> tuple[str | None, str]:
    """Return (the G85 value in the document, the value we compute)."""
    start = raw.find(b"\r\nST*")
    end = raw.find(b"\r\nG85*")
    if start < 0 or end < 0:
        raise ParseError("no ST or G85 record, so the checksum cannot be checked")
    start += 2  # coverage starts at the S of ST
    end += 2  # the CRLF before G85 is covered
    line_end = raw.find(b"\r\n", end)
    expected = raw[end + 4: line_end if line_end >= 0 else None].decode("ascii", "replace").strip()
    actual = f"{crc16_arc(raw[start:end]):04X}"
    return (expected or None), actual


def _int(value: str) -> int:
    try:
        return int(value) if value.strip() else 0
    except ValueError:
        # A plain ValueError would escape the sync and stop it on this poll for good.
        raise ParseError(f"expected a number, got {value!r}") from None


def _to_cents(value: int, decimal_places: int) -> int:
    if decimal_places > 2:
        raise ParseError(f"ID4 says {decimal_places} decimal places; only 0-2 are supported")
    return value * 10 ** (2 - decimal_places)


def parse(raw: bytes) -> Poll:
    """Parse one raw poll. Raises ParseError if it isn't a whole EVA-DTS document."""
    if not raw.startswith(b"DXS"):
        raise ParseError("document does not start with a DXS record")
    if b"\r\nDXE" not in raw:
        raise ParseError("document has no DXE record, so it is incomplete")

    expected, actual = _checksum(raw)
    if expected != actual:
        # None of the counters can be trusted, and a garbled field must not
        # turn a checksum failure into a crash, so don't read them at all.
        return Poll(crc_ok=False, crc_expected=expected, crc_actual=actual, machine_serial=None,
                    decimal_places=None, total_paid_cents=None, total_paid_units=None)

    machine_serial: str | None = None
    decimal_places = 2
    total_paid: tuple[int, int] | None = None
    prices: dict[str, int | None] = {}
    counters: dict[str, tuple[int, int]] = {}
    current: str | None = None

    for line in raw.decode("ascii", "replace").split("\r\n"):
        fields = line.split("*")
        tag = fields[0]
        if tag == "ID1" and len(fields) > 1:
            # ID1-01 should be the serial, but our machine writes ">" there and
            # the serial ("RVV_NG ...") in the next field.
            machine_serial = next(
                (f.strip() for f in fields[1:3] if f.strip() and f.strip() != ">"), None
            )
        elif tag == "ID4" and len(fields) > 1 and fields[1].strip():
            decimal_places = _int(fields[1])
        elif tag == "VA1" and len(fields) > 2:
            total_paid = (_int(fields[1]), _int(fields[2]))
        elif tag == "PA1" and len(fields) > 1:
            current = fields[1].strip()
            prices[current] = _int(fields[2]) if len(fields) > 2 and fields[2].strip() else None
        elif tag == "PA2" and current is not None and len(fields) > 2:
            counters[current] = (_int(fields[1]), _int(fields[2]))
            current = None
        elif tag not in ("PA3", "PA4", "PA5", "PA7", "PA8"):
            # Any other record ends the selection block, so a stray PA2 can't
            # attach itself to the wrong selection.
            current = None

    selections = [
        Selection(
            selection=sel,
            price_cents=None if prices[sel] is None else _to_cents(prices[sel], decimal_places),
            lifetime_units=units,
            lifetime_sales_cents=_to_cents(value, decimal_places),
        )
        for sel, (units, value) in counters.items()
    ]
    return Poll(
        crc_ok=True,
        crc_expected=expected,
        crc_actual=actual,
        machine_serial=machine_serial,
        decimal_places=decimal_places,
        total_paid_cents=None if total_paid is None else _to_cents(total_paid[0], decimal_places),
        total_paid_units=None if total_paid is None else total_paid[1],
        selections=selections,
    )
