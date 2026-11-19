"""A stand-in for the DEX bridge, and the student worker who fulfils our requests.

`SimBridge` answers the calls our code makes (`products`, `post`, `purchase_request`, `slots`) the way
docs/dex-bridge.md says the real one does, including its 422 and 429 refusals. It never touches the
network. `Worker.fulfil` is the Friday (or paid midweek) visit: it buys, loads, pulls and reprices
from the request and its directive, and writes the receipts the bridge then hands back.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from typing import Any

from vending.bridge import BridgeAnswered
from vending.simulate import world
from vending.simulate.world import BY_SKU, Machine
from vending.slots import DEVICE_ID, our_team

STORES = ("Walmart", "Costco", "Amazon", "BJs")


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _money(cents: int | Fraction) -> str:
    return f"{Decimal(int(cents)) / 100:.2f}"


def _cents(value: Any) -> int:
    return int((Decimal(str(value)) * 100).to_integral_value())


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

    def __init__(self, machine: Machine, now, catalog: list[dict] | None = None, team: str | None = None):
        self.machine, self.now, self.team = machine, now, team
        self.catalog = catalog if catalog is not None else [p.catalog_entry() for p in world.PRODUCTS]
        self.requests: dict[str, Filed] = {}
        self.product_requests: list[dict] = []
        self.refused: list[dict] = []           # every 4xx it answered, for the report
        self.calls: list[tuple[str, str]] = []  # (method, path) in order

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
            return self._purchase(body)
        if path == "/api/v1/product-requests":
            request_id = f"prq_sim_{len(self.product_requests) + 1:03d}"
            self.product_requests.append({"id": request_id, "body": body, "at": self.now().isoformat()})
            return {"id": request_id, "status": "pending"}
        raise BridgeAnswered(404, f"no route {path}")

    def _purchase(self, body: dict) -> dict:
        if any(r.status == "pending" for r in self.requests.values()):
            self.refused.append({"at": self.now().isoformat(), "status": 429, "body": body})
            raise BridgeAnswered(429, '{"error": "too_many_open_requests"}')
        problems = self._check(body)
        if problems:
            self.refused.append({"at": self.now().isoformat(), "status": 422, "problems": problems, "body": body})
            raise BridgeAnswered(422, str({"error": "invalid_lines", "problems": problems}))
        request_id = f"pr_sim_{len(self.requests) + 1:03d}"
        self.requests[request_id] = Filed(request_id, body, self.now())
        return {"id": request_id, "status": "pending", "created_at": _iso(self.now())}

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
