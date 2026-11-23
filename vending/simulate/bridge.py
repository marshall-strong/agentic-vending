"""A stand-in for the DEX bridge, and the student worker who fulfils our requests.

`SimBridge` answers the calls our code makes (`products`, `post`, `purchase_request`, `slots`,
`check_books`) the way docs/dex-bridge.md says the real one does, including its 422 and 429 refusals. It
never touches the network.

**The tracking check** (`check_books`, `POST /api/v1/tracking/check`) compares our figures with the
books recorded for a poll (`record_books`): the true cash and tax owed, which the caller supplies (the
simulated bridge keeps no money of its own), and the machine's coil and closet at that moment. Tolerance,
from David on 20 Nov: within $1 for money (`MONEY_TOLERANCE_CENTS`), within 2 units for counts
(`UNIT_TOLERANCE`). Whether 2 units is per slot and product or across all of them isn't said;
`count_tolerance` is `"per_entry"` (each slot and each product within 2, the default) or `"total"` (the
differences added up, without sign, within 2, the stricter reading). The order of the refusals is our
guess; the real bridge's isn't documented.

`Worker.fulfil` is the Friday (or paid midweek) visit: it buys, loads, pulls and reprices
from the request and its directive, and writes the receipts the bridge then hands back.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from typing import Any

from vending import tracking
from vending.bridge import TRACKING_PATH, BridgeAnswered, PostAnswer
from vending.simulate import world
from vending.simulate.world import BY_SKU, Machine
from vending.slots import DEVICE_ID, our_team

STORES = ("Walmart", "Costco", "Amazon", "BJs")

# The tracking check's tolerance (David, 20 Nov): $1 for money, 2 units for counts.
MONEY_TOLERANCE_CENTS = 100
UNIT_TOLERANCE = 2
COUNT_TOLERANCES = ("per_entry", "total")
TRACKING_FIELDS = ("as_of", "cash", "sales_tax_owed", "coil", "back_stock")


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _money(cents: int | Fraction) -> str:
    return f"{Decimal(int(cents)) / 100:.2f}"


def _cents(value: Any) -> int:
    return int((Decimal(str(value)) * 100).to_integral_value())


@dataclass
class Books:
    """The bridge's books as of one poll: what a tracking check is compared with."""
    session_id: str
    at: datetime
    cash_cents: int | Fraction
    tax_owed_cents: int | Fraction
    coil: dict[str, int]
    back_stock: dict[str, int]
    reconciled: bool = True        # False: the poll isn't folded into the books yet (`not_reconciled_yet`)


@dataclass
class Filed:
    request_id: str
    body: dict
    submitted_at: datetime
    status: str = "pending"
    fulfilled_at: datetime | None = None
    lines: list[dict] = field(default_factory=list)
    total_paid_cents: int = 0
    decision_note: str | None = None


class SimBridge:
    """The bridge, in memory. `now` is the simulated clock.

    Its inventory server assigns us the machine's slots, which are the season's recorded assignment
    (`State.assignment`), under our team's name (`vending.slots.our_team`), so `vending.slots` stores
    and checks them as it would the real server's answer."""

    def __init__(self, machine: Machine, now, catalog: list[dict] | None = None, team: str | None = None,
                 count_tolerance: str = "per_entry", daily_limit_from: datetime | None | str = "default"):
        if count_tolerance not in COUNT_TOLERANCES:
            raise ValueError(f"count_tolerance is one of {COUNT_TOLERANCES}, not {count_tolerance!r}")
        self.machine, self.now, self.team = machine, now, team
        self.catalog = catalog if catalog is not None else [p.catalog_entry() for p in world.PRODUCTS]
        self.requests: dict[str, Filed] = {}
        self.product_requests: list[dict] = []
        self.refused: list[dict] = []           # every 4xx it answered, for the report
        self.calls: list[tuple[str, str]] = []  # (method, path) in order
        # The tracking check: the books per poll, when they began (None: not yet), and from when one a day:
        # by default from the client's own setting (`vending.tracking.daily_from`, the competition start),
        # so a test that forgets it still meets the limit; None means no limit.
        self.books: dict[str, Books] = {}
        self.tracking_from: datetime | None = None
        self.daily_limit_from: datetime | None = (tracking.daily_from() if daily_limit_from == "default"
                                                  else daily_limit_from)
        self.count_tolerance = count_tolerance
        self.checks: list[dict] = []            # every check made: at, as_of, results

    # --- reads ---------------------------------------------------------------------------------------
    def products(self) -> list[dict]:
        self.calls.append(("GET", "/api/v1/products"))
        return [dict(p) for p in self.catalog]

    def slots(self) -> dict:
        self.calls.append(("GET", "/api/v1/slots"))
        return {"team": self.team or our_team(), "device_id": DEVICE_ID, "slots": list(self.machine.slots)}

    def purchase_request(self, request_id: str) -> dict:
        self.calls.append(("GET", f"/api/v1/purchase-requests/{request_id}"))
        r = self.requests.get(request_id)
        if r is None:
            raise BridgeAnswered(404, "not found")
        if r.status != "fulfilled":
            return {"id": r.request_id, "status": r.status, "created_at": _iso(r.submitted_at),
                    **({"decision_note": r.decision_note} if r.decision_note else {})}
        return {"id": r.request_id, "status": "fulfilled", "device_id": DEVICE_ID,
                "created_at": _iso(r.submitted_at), "updated_at": _iso(r.fulfilled_at),
                "fulfilled_at": _iso(r.fulfilled_at), "off_cycle": bool(r.body.get("off_cycle")),
                "note": r.body.get("note"), "decision_note": None, "total_paid": _money(r.total_paid_cents),
                "lines": r.lines}

    def history(self, day: str) -> list[dict]:     # not used: polls are stored directly
        raise BridgeAnswered(501, "the simulation stores polls itself")

    # --- writes --------------------------------------------------------------------------------------
    def post(self, path: str, body: dict) -> dict:
        self.calls.append(("POST", path))
        if path == "/api/v1/purchase-requests":
            answer = self._purchase(body)
            if answer.status != 201:
                raise BridgeAnswered(answer.status, answer.text)
            return answer.body
        if path == TRACKING_PATH:
            self.calls.pop()                    # `check_books` notes its own call
            return self.check_books(body)
        if path == "/api/v1/product-requests":
            request_id = f"prq_sim_{len(self.product_requests) + 1:03d}"
            self.product_requests.append({"id": request_id, "body": body, "at": self.now().isoformat()})
            return {"id": request_id, "status": "pending"}
        raise BridgeAnswered(404, f"no route {path}")

    def post_answer(self, path: str, body: dict, *, ready=None) -> PostAnswer:
        """A purchase POST's whole answer, as the real bridge's `post_answer` gives it: the documented `201`, or
        the bridge's documented refusal (`422`, `429`), which `submit_desired_state` leaves `unknown`. `ready` runs
        first, as the real one runs it once connected: whatever it raises goes up, and nothing is filed."""
        if ready is not None:
            ready()
        if path != "/api/v1/purchase-requests":
            return PostAnswer(200, self.post(path, body), "", "")
        self.calls.append(("POST", path))
        return self._purchase(body)

    def _purchase(self, body: dict) -> PostAnswer:
        if any(r.status == "pending" for r in self.requests.values()):
            self.refused.append({"at": self.now().isoformat(), "status": 429, "body": body})
            return _json_answer(429, {"error": "too many open requests", "error_type": "too_many_open_requests"})
        problems = self._check(body)
        if problems:
            self.refused.append({"at": self.now().isoformat(), "status": 422, "problems": problems, "body": body})
            lines = body.get("lines") or []
            items = [{"index": p["line"], "slot": str(lines[p["line"]].get("slot")), "choice": None,
                      "sku": p.get("sku"), "error_type": p["error_type"], "reason": p["error_type"].replace("_", " ")}
                     for p in problems]
            return _json_answer(422, {"error": "invalid lines", "error_type": "invalid_lines", "items": items})
        request_id = f"pr_sim_{len(self.requests) + 1:03d}"
        self.requests[request_id] = Filed(request_id, body, self.now())
        return _json_answer(201, {"id": request_id, "status": "pending", "created_at": _iso(self.now())})

    def _check(self, body: dict) -> list[dict]:
        """The bridge's line checks (docs/dex-bridge.md): every problem at once."""
        listed = {p["sku"] for p in self.catalog if p.get("active", True)}
        problems, seen = [], set()
        for i, line in enumerate(body.get("lines") or []):
            slot = str(line.get("slot"))
            if slot in seen:
                problems.append({"line": i, "error_type": "duplicate_slot"})
            seen.add(slot)
            if slot not in self.machine.slots:
                problems.append({"line": i, "error_type": "slot_not_owned"})
                continue
            held = self.machine.slots[slot]
            choices = [line.get(k) for k in ("primary", "alternate", "load") if line.get(k)]
            for choice in choices:
                sku = choice.get("sku")
                if sku not in listed:
                    problems.append({"line": i, "error_type": "unknown_sku", "sku": sku})
                if held.units > 0 and held.sku:       # an empty slot accepts either
                    differs = sku != held.sku
                    if differs and not choice.get("change_product"):
                        problems.append({"line": i, "error_type": "unacknowledged_product_change"})
                    if not differs and choice.get("change_product"):
                        problems.append({"line": i, "error_type": "unexpected_product_change"})
            if line.get("alternate") and line.get("primary") and line["alternate"] == line["primary"]:
                problems.append({"line": i, "error_type": "alternate_same_as_primary"})
        return problems


    # --- the tracking check ----------------------------------------------------------------------------
    def record_books(self, session_id: str, at: datetime, cash_cents: int | Fraction, tax_owed_cents: int | Fraction,
                     *, coil: dict[str, int] | None = None, back_stock: dict[str, int] | None = None,
                     reconciled: bool = True) -> Books:
        """The books as of the poll `session_id`: the true money, given, and the machine's coil and closet
        now unless given. The first books recorded start the tracking, unless `tracking_from` is set."""
        books = Books(session_id, at, cash_cents, tax_owed_cents,
                      dict(coil) if coil is not None else {s: st.units for s, st in self.machine.slots.items()},
                      dict(back_stock) if back_stock is not None
                      else {sku: n for sku, n in self.machine.closet.items() if n}, reconciled)
        self.books[session_id] = books
        if self.tracking_from is None:
            self.tracking_from = at
        return books

    def check_books(self, body: Any) -> dict:
        """`POST /api/v1/tracking/check`: within or outside for each figure, as the real bridge answers."""
        self.calls.append(("POST", TRACKING_PATH))
        now = self.now()
        invalid = _schema_problems(body)
        if invalid:
            self._refuse(422, {"detail": invalid}, body)
        if self.tracking_from is None:
            self._refuse_tracking(409, "tracking_not_started", "nothing to check against until the machine is "
                                  "stocked for the project", body)
        books = self.books.get(body["as_of"])
        if books is None:
            self._refuse_tracking(404, "unknown_poll", "no such poll", body)
        if books.at < self.tracking_from:
            self._refuse_tracking(422, "before_tracking_start", "that poll is before the books began", body)
        if not books.reconciled:
            self._refuse_tracking(409, "not_reconciled_yet", "polls are folded in on the hour", body)
        owned = set(self.machine.slots)
        given = set(body["coil"])
        wrong = sorted(given - owned) + sorted(owned - given)
        if wrong:
            self._refuse_tracking(422, "invalid_coil", "every slot you own, and only those", body,
                                  [f"{s}: not yours" if s in given else f"{s}: missing" for s in wrong])
        listed = {p["sku"] for p in self.catalog}
        unknown = sorted(set(body["back_stock"]) - listed)
        if unknown:
            self._refuse_tracking(422, "unknown_sku", "not in the catalog", body, unknown)
        day = now.astimezone(world.EASTERN).date()
        next_from = datetime.combine(day + timedelta(days=1), time(0), world.EASTERN)
        if self.daily_limit_from is not None and now >= self.daily_limit_from and any(
                c["at"].astimezone(world.EASTERN).date() == day for c in self.checks):
            self._refuse_tracking(429, "daily_check_used", "one check per team per day", body,
                                  next_check_from=_iso(next_from))
        results = {
            "cash": _verdict(abs(_dollars_cents(body["cash"]) - books.cash_cents) <= MONEY_TOLERANCE_CENTS),
            "sales_tax_owed": _verdict(abs(_dollars_cents(body["sales_tax_owed"]) - books.tax_owed_cents)
                                       <= MONEY_TOLERANCE_CENTS),
            "coil": _verdict(self._counts_within(body["coil"], books.coil)),
            "back_stock": _verdict(self._counts_within(body["back_stock"], books.back_stock)),
        }
        self.checks.append({"at": now, "as_of": books.session_id, "results": results})
        return {"as_of": books.session_id, "as_of_time": _iso(books.at), "checked_at": _iso(now),
                "results": results, "next_check_from": _iso(next_from)}

    def _counts_within(self, ours: dict[str, int], truth: dict[str, int]) -> bool:
        """A missing key counts as 0 on either side."""
        gaps = [abs(ours.get(k, 0) - truth.get(k, 0)) for k in set(ours) | set(truth)]
        if self.count_tolerance == "total":
            return sum(gaps) <= UNIT_TOLERANCE
        return all(g <= UNIT_TOLERANCE for g in gaps)

    def _refuse_tracking(self, status: int, error_type: str, detail: str, body: Any, items: list[str] = (),
                         next_check_from: str | None = None) -> None:
        answer = {"error": error_type.replace("_", " "), "error_type": error_type, "detail": detail,
                  "items": list(items)}
        if next_check_from is not None:
            answer["next_check_from"] = next_check_from
        self._refuse(status, answer, body)

    def _refuse(self, status: int, answer: dict, body: Any) -> None:
        self.refused.append({"at": self.now().isoformat(), "status": status, "answer": answer, "body": body})
        raise BridgeAnswered(status, json.dumps(answer))


def _verdict(ok: bool) -> str:
    return "within" if ok else "outside"


def _dollars_cents(value: Any) -> Fraction:
    return Fraction(Decimal(str(value))) * 100


def _schema_problems(body: Any) -> list[dict]:
    """What FastAPI would refuse in a tracking body (`TrackingCheckCreate`), in its error shape."""
    if not isinstance(body, dict):
        return [{"type": "dict_type", "loc": ["body"], "msg": "Input should be a valid dictionary"}]
    problems = [{"type": "missing", "loc": ["body", k], "msg": "Field required"}
                for k in TRACKING_FIELDS if k not in body]
    problems += [{"type": "extra_forbidden", "loc": ["body", k], "msg": "Extra inputs are not permitted"}
                 for k in body if k not in TRACKING_FIELDS]
    as_of = body.get("as_of")
    if "as_of" in body and not (isinstance(as_of, str) and 1 <= len(as_of) <= 64):
        problems.append({"type": "string_type", "loc": ["body", "as_of"], "msg": "1 to 64 characters"})
    for k in ("cash", "sales_tax_owed"):
        if k in body and not _amount(body[k]):
            problems.append({"type": "number_type", "loc": ["body", k], "msg": "a number of dollars"})
    for k, longest in (("coil", 8), ("back_stock", 64)):
        if k not in body:
            continue
        v = body[k]
        if not isinstance(v, dict):
            problems.append({"type": "dict_type", "loc": ["body", k], "msg": "Input should be a valid dictionary"})
            continue
        for key, n in v.items():
            if not (isinstance(key, str) and 1 <= len(key) <= longest):
                problems.append({"type": "string_too_long", "loc": ["body", k, str(key)], "msg": "bad key"})
            if isinstance(n, bool) or not isinstance(n, int) or not -10000 <= n <= 100000:
                problems.append({"type": "int_type", "loc": ["body", k, str(key)], "msg": "a whole number of units"})
    return problems


def _amount(value: Any) -> bool:
    """A number within +/-100000, or a string the bridge can read as dollars."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return False
    try:
        amount = Decimal(str(value))
    except ArithmeticError:
        return False
    return amount.is_finite() and (isinstance(value, str) or -100000 <= amount <= 100000)


_ROW = re.compile(r"^(\S+)\s{2,}(.+?)\s{2,}(\S+)\s{2,}(\S+)\s{2,}(\S+)\s{2,}(\S+(?: stock)?)\s{2,}(.+)$")


def directive_loads(directive: str | None, slots) -> dict[str, int]:
    """Units to load per slot of `slots`, read off the directive table the worker gets (`planner._directive`)."""
    loads: dict[str, int] = {}
    for line in (directive or "").splitlines():
        cells = re.split(r"\s{2,}", line.strip())
        if len(cells) >= 5 and cells[0] in slots:
            try:
                loads[cells[0]] = int(cells[4])
            except ValueError:
                loads[cells[0]] = 0
    return loads


@dataclass
class Worker:
    """The student worker. Rules (all assumed; the write-up lists them):

    - buys the primary if the store has it at or under the max per pack, else the alternate, else nothing;
    - a store is out of a product on 3% of visits; each unit is broken on arrival with chance 0.5%;
    - loads what the directive says, never past what the column holds, and puts the rest in the closet;
    - pulls the old product to the closet only when a new one goes in;
    - sets the slot's tag to the sale price of what it loaded; a slot it can't fill keeps its old price.
    """
    seed: int

    def fulfil(self, bridge: SimBridge, request_id: str, at: datetime, directive: str | None) -> list[dict]:
        r = bridge.requests[request_id]
        machine = bridge.machine
        loads = directive_loads(directive, machine.slots)
        lines, total = [], 0
        events = []
        for i, line in enumerate(r.body.get("lines") or []):
            slot = str(line["slot"])
            resolved = at + timedelta(minutes=2 * i)
            receipt, paid, event = self._line(machine, request_id, i, slot, line, loads.get(slot), at, resolved)
            total += paid
            events.append(event)
            lines.append({"line": i, "slot": slot, "source": line["source"], "primary": line.get("primary"),
                          "alternate": line.get("alternate"), "load": line.get("load"), "receipt": receipt})
        r.status, r.fulfilled_at, r.lines, r.total_paid_cents = "fulfilled", at + timedelta(
            minutes=2 * len(lines)), lines, total
        return events

    def _line(self, machine: Machine, request_id: str, i: int, slot: str, line: dict, load: int | None,
              visit: datetime, resolved: datetime) -> tuple[dict, int, dict]:
        st = machine.slots[slot]
        r = world.rng(self.seed, "line", request_id, i)
        empty = {"packs_bought": 0, "units_per_pack": None, "units_bought": 0, "total_paid": "0.00",
                 "unit_cost_paid": None, "units_loaded": 0, "units_lost": 0, "loss_note": None, "later_losses": [],
                 "units_to_back_stock": 0, "units_from_back_stock": 0, "units_pulled": 0, "pulled_sku": None,
                 "slot_reassigned": False, "resolved_at": _iso(resolved)}
        event = {"slot": slot, "request_id": request_id, "line": i, "at": resolved.isoformat()}
        if line["source"] == "back_stock":
            choice = line["load"]
            sku, asked = choice["sku"], int(choice["units"])
            have = machine.closet.get(sku, 0)
            pulled, pulled_sku = self._pull(machine, slot, sku) if have else (0, None)
            room = BY_SKU[sku].column - (st.units if st.sku == sku else 0)
            taken = min(asked, have, max(room, 0), load if load is not None else asked)
            if taken <= 0:
                return ({**empty, "outcome": "unavailable", "sku": sku, "store": None},
                        0, {**event, "outcome": "unavailable", "why": "closet short"})
            machine.closet[sku] = have - taken
            st.sku, st.units, st.price_cents = sku, st.units + taken, _cents(choice["sale_price"])
            return ({**empty, "outcome": "loaded", "sku": sku, "store": None, "units_loaded": taken,
                     "units_from_back_stock": taken, "units_pulled": pulled, "pulled_sku": pulled_sku},
                    0, {**event, "outcome": "loaded", "sku": sku, "units_loaded": taken, "price": choice["sale_price"],
                        "pulled": pulled})
        tried = []
        for rank in ("primary", "alternate"):
            choice = line.get(rank)
            if not choice:
                continue
            sku, store, upp, packs = choice["sku"], choice["store"], int(choice["units_per_pack"]), int(choice["packs"])
            if sku not in BY_SKU:
                tried.append(f"{rank}: {sku} unknown to the shops")
                continue
            price = world.market_pack_cents(self.seed, sku, upp, store, visit.astimezone(world.EASTERN).date())
            if r.random() < world.STORE_SOLD_OUT:
                tried.append(f"{rank}: {store} had none")
                continue
            if price > _cents(choice["max_price_per_pack"]):
                tried.append(f"{rank}: {store} wanted {_money(price)} a pack, over the {choice['max_price_per_pack']} max")
                continue
            bought = upp * packs
            lost = sum(r.random() < world.BREAKAGE for _ in range(bought))
            paid = price * packs
            pulled, pulled_sku = self._pull(machine, slot, sku)
            room = BY_SKU[sku].column - (st.units if st.sku == sku else 0)
            want = load if load is not None else room
            loaded = max(min(want, room, bought - lost), 0)
            to_closet = bought - lost - loaded
            machine.closet[sku] = machine.closet.get(sku, 0) + to_closet
            st.sku, st.units, st.price_cents = sku, (st.units if st.sku == sku else 0) + loaded, _cents(choice["sale_price"])
            unit = Fraction(paid, bought) / 100
            receipt = {**empty, "outcome": rank, "sku": sku, "store": store, "packs_bought": packs,
                       "units_per_pack": upp, "units_bought": bought, "total_paid": _money(paid),
                       "unit_cost_paid": f"{float(unit):.4f}", "units_loaded": loaded, "units_lost": lost,
                       "loss_note": f"{lost} broken on arrival" if lost else None,
                       "units_to_back_stock": to_closet, "units_pulled": pulled, "pulled_sku": pulled_sku}
            return receipt, paid, {**event, "outcome": rank, "sku": sku, "store": store, "paid": _money(paid),
                                   "units_loaded": loaded, "to_closet": to_closet, "lost": lost,
                                   "price": choice["sale_price"], "pulled": pulled, "tried": tried}
        primary = line.get("primary") or {}
        return ({**empty, "outcome": "unavailable", "sku": primary.get("sku"), "store": primary.get("store")}, 0,
                {**event, "outcome": "unavailable", "tried": tried})

    @staticmethod
    def _pull(machine: Machine, slot: str, sku: str) -> tuple[int, str | None]:
        """Take the old product out to the closet when a new one goes in."""
        st = machine.slots[slot]
        if st.sku and st.sku != sku and st.units > 0:
            pulled, old = st.units, st.sku
            machine.closet[old] = machine.closet.get(old, 0) + pulled
            st.units = 0
            return pulled, old
        if st.sku != sku:
            st.units = 0
        return 0, None


def _json_answer(status: int, body: dict) -> PostAnswer:
    return PostAnswer(status, body, "", json.dumps(body)[:500])
