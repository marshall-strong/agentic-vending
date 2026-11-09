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


@dataclass(frozen=True)
class Stored:
    """What one read changed: the status now, and later losses we hadn't seen."""
    status: str
    new_losses: int = 0


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
    """
    if response.get("id") not in (None, request_id):
        raise ValueError(f"asked for {request_id}, the bridge answered about {response.get('id')}")
    # Pending and rejected answers have no lines; anything with lines is the
    # full listing the bridge only gives once every line is done.
    if "lines" in response:
        status = "fulfilled"
        lines, losses = _line_rows(request_id, response["lines"])
    elif response.get("status") in ("pending", "rejected"):
        status, lines, losses = response["status"], [], []
    else:
        raise ValueError(f"unexpected answer for {request_id}: status {response.get('status')!r} and no lines")

    with conn.transaction():
        updated = conn.execute(
            """
            update purchase_requests
               set status = %s, decision_note = %s, total_paid = %s, fulfilled_at = %s,
                   updated_at = %s, last_checked = %s, last_response = %s
             where request_id = %s
            """,
            (status, response.get("decision_note"), _money(response.get("total_paid")),
             _when(response.get("fulfilled_at")), _when(response.get("updated_at")),
             checked_at, Jsonb(response), request_id),
        ).rowcount
        if not updated:
            raise ValueError(f"{request_id} is not in purchase_requests; record it when it is submitted")
        if status != "fulfilled":
            return Stored(status)
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
    return Stored(status, max(len(losses) - before, 0))


def _line_rows(request_id: str, lines: list[dict]) -> tuple[list[tuple], list[tuple]]:
    """Rows for `purchase_lines` and `purchase_line_losses`, or `ValueError`.

    A fulfilled request has a receipt on every line. If one is missing we
    store none of them: a half-stored request would make the stock count wrong
    without saying so.
    """
    line_rows, loss_rows = [], []
    for line in lines:
        receipt = line.get("receipt")
        if receipt is None:
            raise ValueError(f"{request_id} line {line.get('line')} is fulfilled but has no receipt")
        try:
            asked = {k: line.get(k) for k in ("primary", "alternate", "load") if line.get(k) is not None}
            values = [
                _money(receipt[c]) if c in _MONEY else _when(receipt[c]) if c == "resolved_at" else receipt[c]
                for c in RECEIPT_COLUMNS
            ]
            line_rows.append((request_id, line["line"], line["slot"], line["source"], Jsonb(asked), *values))
            for position, loss in enumerate(receipt.get("later_losses") or []):
                loss_rows.append((request_id, line["line"], position, loss["location"], loss["sku"],
                                  loss["units"], loss.get("note"), _when(loss["recorded_at"])))
        except KeyError as missing:
            raise ValueError(f"{request_id} line {line.get('line')}: receipt has no {missing}") from None
    return line_rows, loss_rows


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
    if not amount.is_finite():
        raise ValueError(f"not an amount: {value!r}")
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

