"""Our purchase requests and their receipts, in our database.

The bridge has no route that lists a team's requests, so a request we don't
record here is one we can never read back. `record` is for
`submit_desired_state` to call the moment the bridge accepts a request;
`store` saves each answer `python -m vending.sync_requests` reads back; and
`get_purchase_requests` is the logic behind tool #7.

Receipt money arrives as exact decimal strings and is stored as `numeric`, so
nothing here ever goes through a float.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import psycopg
from psycopg.types.json import Jsonb

# Every receipt field we keep a column for, in the migration's order.
RECEIPT_COLUMNS = (
    "outcome", "sku", "store", "packs_bought", "units_per_pack", "units_bought",
    "total_paid", "unit_cost_paid", "units_loaded", "units_lost", "loss_note",
    "units_to_back_stock", "units_from_back_stock", "units_pulled", "pulled_sku",
    "slot_reassigned", "resolved_at",
)
_MONEY = {"total_paid", "unit_cost_paid"}
# No receipt comes near a billion dollars. Postgres `numeric` can't hold some finite Decimals (1e999999),
# and the money readers round to the cent, which raises for an amount past Decimal's 28 digits.
_TOO_MUCH = Decimal(10) ** 9


class _NotReal(ValueError):
    """A well-formed amount that can't be a real one: infinite, NaN, or a billion dollars or more."""


@dataclass(frozen=True)
class Stored:
    """What one read changed: the status now, later losses we hadn't seen, and what to tell a person."""
    status: str
    new_losses: int = 0
    warnings: tuple[str, ...] = ()


def record(conn: psycopg.Connection, request_id: str, submitted_at: datetime, off_cycle: bool,
           body: dict, plan_hash: str | None = None, visit_fee_cents: int | None = None) -> None:
    """Remember a request the bridge has just accepted, as `pending`.

    `visit_fee_cents` is the midweek visit fee as it stood when an off-cycle
    request was submitted, so the ledger charges what was agreed then even if
    the config fee changes later.

    Safe to call twice for the same ID: the second call changes nothing, so a
    retry after a dropped connection can't overwrite what the sync has learned.
    """
    conn.execute(
        "insert into purchase_requests (request_id, submitted_at, off_cycle, plan_hash, request_body, visit_fee_cents)"
        " values (%s, %s, %s, %s, %s, %s) on conflict (request_id) do nothing",
        (request_id, submitted_at, off_cycle, plan_hash, Jsonb(body), visit_fee_cents),
    )


def store(conn: psycopg.Connection, request_id: str, response: dict, checked_at: datetime) -> Stored:
    """Save one answer from `GET /api/v1/purchase-requests/{id}`.

    A fulfilled answer replaces the request's lines and later losses as a
    whole set, in one transaction with the status. Re-reading a request
    therefore never counts a loss twice, and a receipt the bridge corrects
    simply overwrites the old one. An answer we can't make sense of raises
    `ValueError` before anything is written, so a bad read never wipes a good one.
    That includes door times with close before open (`door_times`). Once a request is
    `fulfilled` or `rejected`, a `pending` answer is ignored and the stored status returned.

    An answer whose `updated_at` is older than the stored one is ignored too, with a warning: a slow read
    can arrive after the bridge corrected the receipt, and it must not put the old receipt back. The check
    is part of the same UPDATE, so two reads that race can't both win.

    An amount that can't be real (1e30 dollars) is stored as unknown, with a warning, and the rest of the
    receipt is kept: the fill happened, and the stock count needs its lines. `last_response` keeps the
    answer as the bridge gave it.
    """
    if response.get("id") not in (None, request_id):
        raise ValueError(f"asked for {request_id}, the bridge answered about {response.get('id')}")
    # Pending and rejected answers have no lines; anything with lines is the
    # full listing the bridge only gives once every line is done.
    if "lines" in response:
        status = "fulfilled"
        lines, losses, unreal = _line_rows(request_id, response["lines"])
    elif response.get("status") in ("pending", "rejected"):
        status, lines, losses, unreal = response["status"], [], [], []
    else:
        raise ValueError(f"unexpected answer for {request_id}: status {response.get('status')!r} and no lines")
    opened, closed = door_times(request_id, response)
    total_paid = _kept_money(response.get("total_paid"), f"{request_id}: total_paid", unreal)
    warnings = tuple(unreal) + (() if opened is not None else tuple(_door_time_warnings(request_id, response)))
    changed_at = _when(response.get("updated_at"))

    with conn.transaction():
        updated = conn.execute(
            """
            update purchase_requests
               set status = %s, decision_note = %s, total_paid = %s, fulfilled_at = %s,
                   updated_at = %s, last_checked = %s, last_response = %s,
                   door_opened_at = %s, door_closed_at = %s
             where request_id = %s and not (%s = 'pending' and status <> 'pending')
               and (%s::timestamptz is null or updated_at is null or updated_at <= %s)
            """,
            (status, response.get("decision_note"), total_paid,
             _when(response.get("fulfilled_at")), changed_at,
             checked_at, Jsonb(response), opened, closed, request_id, status, changed_at, changed_at),
        ).rowcount
        if not updated:
            stored = conn.execute("select status, updated_at from purchase_requests where request_id = %s",
                                  (request_id,)).fetchone()
            if stored is None:
                raise ValueError(f"{request_id} is not in purchase_requests; record it when it is submitted")
            # A request only ever leaves `pending`. A `pending` answer that arrives after a final one
            # (a slow read that outlived its timeout) is stale, so it must not clear the receipt or door times.
            if status == "pending" and stored[0] != "pending":
                return Stored(stored[0])
            return Stored(stored[0], warnings=(
                f"{request_id}: the bridge's answer last changed {changed_at.isoformat()}, older than the one "
                f"stored ({stored[1].isoformat()}); kept the stored receipt",))
        if status != "fulfilled":
            return Stored(status, warnings=warnings)
        before = conn.execute(
            "select count(*) from purchase_line_losses where request_id = %s", (request_id,)).fetchone()[0]
        conn.execute("delete from purchase_lines where request_id = %s", (request_id,))
        with conn.cursor() as cur:
            cur.executemany(
                f"insert into purchase_lines (request_id, line_no, slot, source, asked, {', '.join(RECEIPT_COLUMNS)})"
                f" values ({', '.join(['%s'] * (5 + len(RECEIPT_COLUMNS)))})",
                lines,
            )
            cur.executemany(
                "insert into purchase_line_losses (request_id, line_no, position, location, sku, units, note,"
                " recorded_at) values (%s, %s, %s, %s, %s, %s, %s, %s)",
                losses,
            )
    return Stored(status, max(len(losses) - before, 0), warnings)


def door_times(request_id: str, response: dict) -> tuple[datetime | None, datetime | None]:
    """The request's `door_opened_at` and `door_closed_at`, as a pair or as `(None, None)`.

    They are to the minute. Older requests have none, and the worker may leave
    them out. With only one of the two, or one that isn't a time (a number, or
    a string like "soon" or a date with no time), neither is kept: the visit needs both ends, and the
    receipt itself is still good. The answer is still kept whole in
    `last_response`. Close before open is a bad receipt: `ValueError`, so
    `store` writes nothing.
    """
    opened, closed = _door_when(response.get("door_opened_at")), _door_when(response.get("door_closed_at"))
    if opened is None or closed is None:
        return None, None
    if closed < opened:
        raise ValueError(f"{request_id}: door closed at {closed.isoformat()}, before it opened at {opened.isoformat()}")
    return opened, closed


def _door_when(value: object) -> datetime | None:
    """A door time, or None when there is none or it isn't a time the bridge could mean.

    A date with no time of day ("2026-11-07") isn't one: read as midnight it would
    put the door's close before its open, and the whole receipt would be refused.
    """
    if not isinstance(value, str) or not any(sep in value.strip() for sep in ("T", "t", " ")):
        return None
    try:
        return _when(value)
    except ValueError:
        return None


def _door_time_warnings(request_id: str, response: dict) -> list[str]:
    """Warnings when the answer has a door time that isn't a time, or one but not the other.

    `door_times` keeps neither then, so the visit is dated by `resolved_at` as
    if the receipt had no door times. A person may want to ask for them.
    """
    raw = {k: response.get(k) for k in ("door_opened_at", "door_closed_at")}
    bad = [k for k, v in raw.items() if v is not None and _door_when(v) is None]
    if bad:
        return [f"{request_id}: {k} is {raw[k]!r}, not a time; neither door time is kept, "
                "so its visit is dated by resolved_at" for k in bad]
    opened, closed = _door_when(raw["door_opened_at"]), _door_when(raw["door_closed_at"])
    if (opened is None) == (closed is None):
        return []
    have, missing = ("door_opened_at", "door_closed_at") if closed is None else ("door_closed_at", "door_opened_at")
    return [f"{request_id}: the receipt has {have} but no {missing}; neither is kept, "
            "so its visit is dated by resolved_at"]


def _line_rows(request_id: str, lines: list[dict]) -> tuple[list[tuple], list[tuple], list[str]]:
    """Rows for `purchase_lines` and `purchase_line_losses`, and warnings for amounts kept as unknown;
    or `ValueError`.

    A fulfilled request has a receipt on every line. If one is missing we
    store none of them: a half-stored request would make the stock count wrong
    without saying so.
    """
    line_rows, loss_rows, unreal = [], [], []
    for line in lines:
        receipt = line.get("receipt")
        if receipt is None:
            raise ValueError(f"{request_id} line {line.get('line')} is fulfilled but has no receipt")
        try:
            asked = {k: line.get(k) for k in ("primary", "alternate", "load") if line.get(k) is not None}
            values = [
                _kept_money(receipt[c], f"{request_id} line {line.get('line')}: {c}", unreal) if c in _MONEY
                else _when(receipt[c]) if c == "resolved_at" else receipt[c]
                for c in RECEIPT_COLUMNS
            ]
            line_rows.append((request_id, line["line"], line["slot"], line["source"], Jsonb(asked), *values))
            for position, loss in enumerate(receipt.get("later_losses") or []):
                loss_rows.append((request_id, line["line"], position, loss["location"], loss["sku"],
                                  loss["units"], loss.get("note"), _when(loss["recorded_at"])))
        except KeyError as missing:
            raise ValueError(f"{request_id} line {line.get('line')}: receipt has no {missing}") from None
    return line_rows, loss_rows, unreal


def _kept_money(value: str | int | None, what: str, unreal: list[str]) -> Decimal | None:
    """`_money`, but an amount that can't be real is kept as unknown (None) and named in `unreal`."""
    try:
        return _money(value)
    except _NotReal:
        unreal.append(f"{what} is {value!r}, which can't be a real amount; stored as unknown")
        return None


def _money(value: str | int | None) -> Decimal | None:
    """An exact amount from the bridge. A float would already have lost a cent, so it is refused."""
    if value is None:
        return None
    if isinstance(value, (float, bool)):
        raise ValueError(f"money must be an exact decimal string, not {value!r}")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"not an amount: {value!r}") from None
    if not amount.is_finite() or abs(amount) >= _TOO_MUCH:
        raise _NotReal(f"not an amount: {value!r}")
    return amount


def _when(value: str | None) -> datetime | None:
    """A bridge timestamp. One without a zone is taken as UTC, which is what the bridge stores."""
    if value is None:
        return None
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def get_purchase_requests(conn: psycopg.Connection, pending_only: bool = False) -> list[dict]:
    """Tool #7: our requests, newest first, with the plan we sent and every receipt.

    Everything is JSON-ready: times are ISO 8601 and money stays the exact
    decimal string the bridge gave, so the agent never sees a rounded float.
    `pending_only` keeps the ones the worker hasn't finished, which are the
    ones the planner must not plan around twice.
    """
    requests = conn.execute(
        """
        select request_id, status, submitted_at, off_cycle, plan_hash, decision_note,
               total_paid, fulfilled_at, last_checked, request_body
          from purchase_requests
         where not %s or status = 'pending'
         order by submitted_at desc, request_id
        """,
        (pending_only,),
    ).fetchall()
    ids = [r[0] for r in requests]
    lines: dict[str, list[dict]] = {i: [] for i in ids}
    by_line: dict[tuple[str, int], dict] = {}
    for row in conn.execute(
        f"select request_id, line_no, slot, source, asked, {', '.join(RECEIPT_COLUMNS)}"
        " from purchase_lines where request_id = any(%s) order by request_id, line_no",
        (ids,),
    ):
        request_id, line_no, slot, source, asked, *receipt = row
        entry = {"line": line_no, "slot": slot, "source": source, **asked,
                 "receipt": {**{c: _json(v) for c, v in zip(RECEIPT_COLUMNS, receipt)}, "later_losses": []}}
        lines[request_id].append(entry)
        by_line[(request_id, line_no)] = entry
    for request_id, line_no, location, sku, units, note, recorded_at in conn.execute(
        "select request_id, line_no, location, sku, units, note, recorded_at from purchase_line_losses"
        " where request_id = any(%s) order by request_id, line_no, position",
        (ids,),
    ):
        by_line[(request_id, line_no)]["receipt"]["later_losses"].append(
            {"location": location, "sku": sku, "units": units, "note": note, "recorded_at": _json(recorded_at)})
    return [
        {
            "request_id": request_id, "status": status, "submitted_at": _json(submitted_at),
            "off_cycle": off_cycle, "plan_hash": plan_hash, "decision_note": decision_note,
            "total_paid": _json(total_paid), "fulfilled_at": _json(fulfilled_at),
            "last_checked": _json(last_checked), "plan": body, "lines": lines[request_id],
        }
        for request_id, status, submitted_at, off_cycle, plan_hash, decision_note,
            total_paid, fulfilled_at, last_checked, body in requests
    ]


def _json(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value

