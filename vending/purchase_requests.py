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

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

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
EASTERN = ZoneInfo("America/New_York")
# `action_log` tool of an answer `store` kept out. One row per request and answer, so each is named once.
IGNORED_TOOL = "purchase_requests.ignored"
# No receipt comes near a billion dollars, or needs more than 10 decimal places (`unit_cost_paid` has four).
# Past these, Postgres `numeric` can't hold some finite Decimals (1e999999, 1e-20000), and the money readers,
# which round to the cent, raise for an amount past Decimal's 28 digits. Both are checked on the digits and
# exponent alone (`_money`), never by Decimal arithmetic, which overflows on 1e1000000.
_MOST_WHOLE_DIGITS = 9
_MOST_PLACES = 10
# Bridge times outside these can't be ours, and the ends of Python's range overflow in another time zone.
_EARLIEST = datetime(2020, 1, 1, tzinfo=timezone.utc)
_LATEST = datetime(2100, 1, 1, tzinfo=timezone.utc)


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
    That includes an answer of the wrong shape (a time that is a number, `lines`
    that isn't a list: `_check_shape`) and door times with close before open
    (`door_times`). Once a request is `fulfilled` or `rejected`, a `pending`
    answer is ignored and the stored status returned.

    The answer's own `status` decides what it is. Only a `fulfilled` answer (or one
    with no status and a `lines` list) carries lines; a `rejected` answer's `lines`
    is ignored. A fulfilled answer that lists no lines is kept out: no request is
    filled with nothing, and stored lines are never replaced by none.

    An answer whose `updated_at` is older than the stored one is ignored too: a slow read can arrive after
    the bridge corrected the receipt, and it must not put the old receipt back. So is a fulfilled answer
    with no `updated_at` once we hold a receipt that has one. A fulfilled answer with the same
    `updated_at` as the stored receipt is the same revision: stored again if it is the same answer, or the same
    answer with more `later_losses` (the bridge adds them to a fulfilled request; the spec doesn't say it
    changes `updated_at`). Any other difference within one revision is kept out: we can't tell which is
    newer, so the stored one stays. A newer revision is kept out too when it drops or changes a later loss
    we stored (`_losses_shrank`): the bridge only adds to that list, and replacing the lines would delete
    the loss and put its units back in the stock count. The request's row is locked while this is decided,
    so two reads that race can't both win.

    An amount that can't be real (1e30 dollars, or more than 10 decimal places) is stored as unknown, with a
    warning, and the rest of the receipt is kept: the fill happened, and the stock count needs its lines.
    `last_response` keeps the answer as the bridge gave it. The warning comes once per revision of the
    receipt, not on every hourly re-read of it.

    A fulfilled receipt is never undone here. The bridge documents rejection only before fulfillment
    (`docs/dex-bridge.md`), so a `rejected` answer after a fulfilled one, with or without a newer
    `updated_at`, is kept out: the receipt, its stock and the prices it set stay, and a person checks with
    the instructors. Each answer kept out is named once: one `action_log` row per request and answer, which
    holds the reason and the warning itself, so a warning the caller fails to log is still on record.
    """
    _check_shape(request_id, response)
    if response.get("id") not in (None, request_id):
        raise ValueError(f"asked for {request_id}, the bridge answered about {response.get('id')}")
    declared = response.get("status")
    empty = False
    if declared in ("pending", "rejected"):
        status, lines, losses, unreal = declared, [], [], []
    elif declared == "fulfilled" or (declared is None and "lines" in response):
        status = "fulfilled"
        lines, losses, unreal = _line_rows(request_id, response.get("lines") or [])
        empty = not lines
    else:
        raise ValueError(f"unexpected answer for {request_id}: status {declared!r}"
                         + ("" if "lines" in response else " and no lines"))
    opened, closed = door_times(request_id, response)
    total_paid = _kept_money(response.get("total_paid"), f"{request_id}: total_paid", unreal)
    changed_at = _when(response.get("updated_at"))
    fulfilled_at = _when(response.get("fulfilled_at"))

    with conn.transaction():
        # Locked, so two syncs reading the same request decide one after the other, and two keeping out the
        # same answer name it once between them.
        stored = conn.execute("select status, updated_at, last_response from purchase_requests"
                              " where request_id = %s for update", (request_id,)).fetchone()
        if stored is None:
            raise ValueError(f"{request_id} is not in purchase_requests; record it when it is submitted")
        was, was_changed, was_answer = stored
        # A request only ever leaves `pending`. A `pending` answer that arrives after a final one
        # (a slow read that outlived its timeout) is stale, so it must not clear the receipt or door times.
        if status == "pending" and was != "pending":
            return Stored(was)
        kept = _kept_out(request_id, status, empty, changed_at, response, was, was_changed, was_answer)
        if kept is None and status == was == "fulfilled":
            kept = _losses_shrank(conn, request_id, changed_at, losses)
        if kept is not None:
            reason, warning = kept
            if not _first_time_kept_out(conn, request_id, response, reason, warning):
                return Stored(was)
            return Stored(was, warnings=(warning,))
        if unreal and was == status and was_answer is not None and (
                (changed_at is not None and was_changed == changed_at) or was_answer == response):
            unreal = []      # named when this revision was first read
        warnings = tuple(unreal) + (() if opened is not None else tuple(_door_time_warnings(request_id, response)))
        conn.execute(
            """
            update purchase_requests
               set status = %(status)s, decision_note = %(note)s, total_paid = %(total)s,
                   fulfilled_at = %(fulfilled)s, updated_at = %(changed)s, last_checked = %(checked)s,
                   last_response = %(response)s, door_opened_at = %(opened)s, door_closed_at = %(closed)s
             where request_id = %(id)s
            """,
            {"status": status, "note": response.get("decision_note"), "total": total_paid,
             "fulfilled": fulfilled_at, "changed": changed_at, "checked": checked_at,
             "response": Jsonb(response), "opened": opened, "closed": closed, "id": request_id},
        )
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


def _kept_out(request_id: str, status: str, empty: bool, changed_at: datetime | None, response: dict,
              was: str, was_changed: datetime | None, was_answer: dict | None) -> tuple[str, str] | None:
    """Why this answer must not replace the stored one, as (reason, warning for a person); None when it may."""
    said = f"last changed {_eastern(changed_at)}" if changed_at else "no updated_at"
    if empty:
        return "no_lines", (f"{request_id}: the bridge says fulfilled ({said}) but lists no lines; kept what we "
                            "had. No request is filled with nothing, so a person should check it with the "
                            "instructors")
    if status == "rejected" and was == "fulfilled":
        return "rejected_after_fill", (
            f"{request_id}: the bridge now says rejected ({said}), after we stored its fulfilled "
            "receipt. Kept the receipt, its stock and its prices. The bridge rejects a request only "
            "before it is filled, so a person should check with the instructors")
    if was_changed is None:
        return None
    if changed_at is None:
        if was != "fulfilled":
            return None
        return "no_updated_at", (f"{request_id}: the bridge sent a receipt with no updated_at, but the stored one "
                                 f"last changed {_eastern(was_changed)}; kept the stored receipt")
    if was_changed > changed_at:
        return "older", (f"{request_id}: the bridge's answer last changed {_eastern(changed_at)}, older than the "
                         f"one stored ({_eastern(was_changed)}); kept the stored receipt")
    if (was_changed == changed_at and was == status == "fulfilled" and was_answer is not None
            and not _same_but_more_losses(was_answer, response)):
        return "same_revision", (
            f"{request_id}: the bridge sent an answer with the same updated_at as the stored one "
            f"({_eastern(changed_at)}) but different contents, so we can't tell which is newer; kept the "
            "stored receipt. A person should check it against the bridge")
    return None


def _losses_shrank(conn: psycopg.Connection, request_id: str, changed_at: datetime | None,
                   losses: list[tuple]) -> tuple[str, str] | None:
    """Why a fulfilled answer must not replace the stored receipt's later losses, or None when it may.

    The bridge only adds to a line's `later_losses`, oldest first, so each stored line's losses must start that
    line's list in the answer. Replacing the lines deletes the stored losses, so an answer that drops or changes
    one would put units back in the count that were broken or missing."""
    def by_line(rows) -> dict[int, list[tuple]]:
        found: dict[int, list[tuple]] = {}
        for line_no, *loss in rows:
            found.setdefault(line_no, []).append(tuple(loss))
        return found

    stored = by_line(conn.execute(
        "select line_no, location, sku, units, note, recorded_at from purchase_line_losses"
        " where request_id = %s order by line_no, position", (request_id,)).fetchall())
    answer = by_line((line_no, location, sku, units, note, at)
                     for _, line_no, _position, location, sku, units, note, at in losses)
    short = sorted(line_no for line_no, old in stored.items() if answer.get(line_no, [])[:len(old)] != old)
    if not short:
        return None
    said = f"last changed {_eastern(changed_at)}" if changed_at else "no updated_at"
    lines = ", ".join(str(n) for n in short)
    return "losses_shrank", (
        f"{request_id}: the bridge's answer ({said}) lists fewer later losses than the stored receipt, or different "
        f"ones, on line{'' if len(short) == 1 else 's'} {lines}; kept the stored receipt. Later losses only grow, "
        "so a person should check it against the bridge")


def _same_but_more_losses(stored: dict, answer: dict) -> bool:
    """Whether `answer` is the stored answer, or the stored answer with later losses added to its lines.

    `later_losses` only grows, oldest first, so each line's stored losses must start the new list."""
    def without_losses(response: dict) -> tuple[dict, list[list]]:
        lines = response.get("lines") if isinstance(response.get("lines"), list) else []
        rest, losses = {**response, "lines": []}, []
        for line in lines:
            if not isinstance(line, dict) or not isinstance(line.get("receipt"), dict):
                rest["lines"].append(line)
                losses.append([])
                continue
            receipt = dict(line["receipt"])
            losses.append(receipt.pop("later_losses", None) or [])
            rest["lines"].append({**line, "receipt": receipt})
        return rest, losses

    stored_rest, stored_losses = without_losses(stored)
    answer_rest, answer_losses = without_losses(answer)
    return stored_rest == answer_rest and all(
        new[:len(old)] == old for old, new in zip(stored_losses, answer_losses, strict=True))


def _first_time_kept_out(conn: psycopg.Connection, request_id: str, response: dict, reason: str,
                         warning: str) -> bool:
    """Record that `store` kept this answer out, with the reason and the warning a person acts on. False when
    the same answer was kept out before.

    The caller holds the request's row lock, so two reads of one answer can't both see it as new. The row
    commits with the read, before the caller logs the warning, so it is the record that lasts."""
    digest = hashlib.sha256(json.dumps(response, sort_keys=True, separators=(",", ":"), default=str)
                            .encode()).hexdigest()
    seen = conn.execute(
        "select 1 from action_log where tool = %s and arguments->>'request_id' = %s"
        " and arguments->>'answer_sha256' = %s limit 1", (IGNORED_TOOL, request_id, digest)).fetchone()
    if seen:
        return False
    conn.execute(
        "insert into action_log (kind, tool, arguments, outcome, detail) values ('read', %s, %s, 'done', %s)",
        (IGNORED_TOOL, Jsonb({"request_id": request_id, "answer_sha256": digest, "reason": reason}), warning))
    return True


def _eastern(moment: datetime) -> str:
    return f"{moment.astimezone(EASTERN):%Y-%m-%d %H:%M} ET"


# The types an answer's fields must have before anything reads them. A JSON number where a time belongs, or a
# string where a list belongs, would otherwise raise TypeError or AttributeError past the sync's per-request
# handling and stop the run.
_TEXT, _WHOLE, _FLAG = (str,), (int,), (bool,)
_ANSWER_FIELDS = {"id": _TEXT, "status": _TEXT, "decision_note": _TEXT, "updated_at": _TEXT,
                  "fulfilled_at": _TEXT}
_LINE_FIELDS = {"line": _WHOLE, "slot": _TEXT, "source": _TEXT}
_RECEIPT_FIELDS = {
    "outcome": _TEXT, "sku": _TEXT, "store": _TEXT, "packs_bought": _WHOLE, "units_per_pack": _WHOLE,
    "units_bought": _WHOLE, "units_loaded": _WHOLE, "units_lost": _WHOLE, "loss_note": _TEXT,
    "units_to_back_stock": _WHOLE, "units_from_back_stock": _WHOLE, "units_pulled": _WHOLE,
    "pulled_sku": _TEXT, "slot_reassigned": _FLAG, "resolved_at": _TEXT,
}
_LOSS_FIELDS = {"location": _TEXT, "sku": _TEXT, "units": _WHOLE, "note": _TEXT, "recorded_at": _TEXT}


def _check_shape(request_id: str, response: object) -> None:
    """`ValueError` unless the answer and its lines, receipts and losses are objects and lists where the spec
    has them, and each field we read is null or of its type. Money is checked by `_money`, door times by
    `_door_when`, and the choices we sent (`primary`, `alternate`, `load`) are kept as they come."""
    if not isinstance(response, dict):
        raise ValueError(f"the answer is {type(response).__name__}, not an object")
    _check_fields(request_id, response, _ANSWER_FIELDS)
    if response.get("lines") is None:
        return
    if not isinstance(response["lines"], list):
        raise ValueError(f"{request_id}: lines is {response['lines']!r}, not a list")
    for index, line in enumerate(response["lines"]):
        if not isinstance(line, dict):
            raise ValueError(f"{request_id} line {index}: {line!r} is not an object")
        where = f"{request_id} line {line.get('line', index)}"
        _check_fields(where, line, _LINE_FIELDS)
        receipt = line.get("receipt")
        if receipt is None:
            continue
        if not isinstance(receipt, dict):
            raise ValueError(f"{where}: receipt is {receipt!r}, not an object")
        _check_fields(where, receipt, _RECEIPT_FIELDS)
        losses = receipt.get("later_losses")
        if losses is None:
            continue
        if not isinstance(losses, list):
            raise ValueError(f"{where}: later_losses is {losses!r}, not a list")
        for loss in losses:
            if not isinstance(loss, dict):
                raise ValueError(f"{where}: a later loss is {loss!r}, not an object")
            _check_fields(f"{where} later loss", loss, _LOSS_FIELDS)


def _check_fields(where: str, found: dict, types: dict[str, tuple[type, ...]]) -> None:
    for key, kinds in types.items():
        value = found.get(key)
        # bool is an int in Python, but true is not a count of units.
        if value is not None and (not isinstance(value, kinds) or (bool not in kinds and isinstance(value, bool))):
            raise ValueError(f"{where}: {key} is {value!r}, not {'text' if kinds == _TEXT else kinds[0].__name__}")


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
    if not amount.is_finite():
        raise _NotReal(f"not an amount: {value!r}")
    # Read the digits and exponent directly: no Decimal arithmetic, so no context can overflow or round.
    sign, digits, exponent = amount.as_tuple()
    if not any(digits):
        return amount if -_MOST_PLACES <= exponent <= 0 else Decimal(0)     # 0.00 stays 0.00; 0e-20000 is 0
    digits = list(digits)
    while exponent < 0 and digits[-1] == 0:      # 12.3700 has two places, not four
        digits.pop()
        exponent += 1
    if len(digits) + exponent > _MOST_WHOLE_DIGITS or exponent < -_MOST_PLACES:
        raise _NotReal(f"not an amount: {value!r}")
    # Kept as given (12.30 stays 12.30), unless only trailing zeros put it past the places numeric may hold.
    return amount if amount.as_tuple().exponent >= -_MOST_PLACES else Decimal((sign, tuple(digits), exponent))


def _when(value: str | None) -> datetime | None:
    """A bridge timestamp. One without a zone is taken as UTC, which is what the bridge stores.

    Anything but text is `ValueError`, so a malformed time fails its own request, never the whole sync. So is a
    time outside 2020-2099 UTC: year 1 or 9999 parses, but can't be written in Eastern time (OverflowError), and
    no receipt of ours is from then."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"not a time: {value!r}")
    moment = datetime.fromisoformat(value)
    moment = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    try:
        utc = moment.astimezone(timezone.utc)
    except OverflowError:
        utc = None
    if utc is None or not _EARLIEST <= utc < _LATEST:
        raise ValueError(f"not a time the bridge could mean: {value!r}")
    return moment


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

