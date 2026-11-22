"""A scripted stand-in for the model: the baseline rule, played through the real agent loop.

It answers `messages.create` the way the API would, with tool calls and then a final JSON answer, so
`vending.agent.run`, the MCP tools, the planner and the submit path all run exactly as for the model.
It costs nothing: its `usage` reports one token each way, so the harness records about $0.

The rule (the bar the model should clear; deliberately simple):
- `scripted-wide` differs only on day one: one sourced drink per slot, as many different drinks as
  there are, best expected profit a class day first (see `wide_day_one`). It separates what having
  more drinks is worth in the simulator from what the agent's choices are worth;
- day one: the five products with a sourcing row, an equal share of our slots each, filled
  round-robin, as many as fit the opening plan's $366 fill cap and $53 spendable floor
  (docs/opening-capital-plan.md);
- every run after: if nothing is pending, refill every slot to its column with what is in it, and put
  the closet's biggest product in an empty slot when it fills a column, inside the same floor;
- our slots are the ones `get_machine_state` lists, which come from the stored assignment, as for
  the model; the rule names no selection of its own;
- price: the product's reference price (docs/agent/price-history.md), raised to the preview's floor
  where the floor is higher, the same in every slot of a product; never a price test, never a switch.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from vending.simulate import world

# The five products with a sourcing row on 11 Nov (db/sourcing/2026-11-11-amazon.csv), in the order the
# day-one fill gives them our slots.
FIRST_FIVE = ("REDBULL-ORIGINAL-8_4CAN", "CELSIUS-SPARKLING_ORANGE-12CAN", "MOUNTAIN-ORIGINAL-20BOT",
              "COCA-ORIGINAL-20BOT", "COCA-DIET-20BOT")


def day_one(slots: list[str] | tuple[str, ...]) -> dict[str, str]:
    """The baseline's day one: `slots` in their listed order, split into five equal runs, one product each."""
    size = -(-len(slots) // len(FIRST_FIVE))
    return {slot: FIRST_FIVE[min(i // size, len(FIRST_FIVE) - 1)] for i, slot in enumerate(slots)}


def fill_order(plan: dict[str, str]) -> list[str]:
    """The slots of a day-one `plan` round-robin by product, so a fill cut short by money still carries
    every product."""
    by_product: dict[str, list[str]] = {}
    for slot, sku in plan.items():
        by_product.setdefault(sku, []).append(slot)
    runs = list(by_product.values())
    return [run[i] for i in range(max(map(len, runs), default=0)) for run in runs if i < len(run)]


def _block(**fields: Any) -> SimpleNamespace:
    return SimpleNamespace(**fields)


def _tool(i: int, name: str, args: dict | None = None) -> SimpleNamespace:
    return _block(type="tool_use", id=f"toolu_{i}_{name}", name=name, input=args or {})


class Scripted:
    """A fake Anthropic client. One instance per run."""

    def __init__(self, wide: bool = False):
        self.wide = wide
        self.slots: list[str] = []
        self.messages = self
        self.step = 0
        self.desired: dict | None = None
        self.wish: list[dict] = []
        self.day_one = False

    def with_options(self, **_):
        return self

    def count_tokens(self, **_):
        return SimpleNamespace(input_tokens=10)

    def create(self, **body):
        content, stop = self._next(body["messages"])
        self.step += 1
        usage = SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0,
                                cache_creation_input_tokens=0, cache_creation=None, service_tier="standard")
        return SimpleNamespace(id=f"msg_scripted_{self.step}", model=body["model"], content=content,
                               stop_reason=stop, usage=usage, _request_id=None)

    # --- the rule ------------------------------------------------------------------------------------
    def _results(self, messages: list) -> dict[str, dict]:
        """The latest reply of each tool, parsed."""
        found: dict[str, dict] = {}
        names: dict[str, str] = {}
        for m in messages:
            if m["role"] == "assistant":
                for b in m["content"]:
                    if getattr(b, "type", None) == "tool_use":
                        names[b.id] = b.name
            elif isinstance(m["content"], list):
                for r in m["content"]:
                    if isinstance(r, dict) and r.get("type") == "tool_result":
                        try:
                            found[names[r["tool_use_id"]]] = json.loads(r["content"])
                        except (ValueError, KeyError):
                            found[names.get(r["tool_use_id"], "?")] = {"text": r["content"]}
        return found

    def _answer(self, decision: str, reason: str, slot_reasons=()) -> tuple[list, str]:
        text = json.dumps({"decision": decision, "reason": reason, "update": f"Baseline rule: {reason}",
                           "notes": "Baseline rule: refill every slot to its column at reference price.",
                           "slot_reasons": list(slot_reasons)})
        return [_block(type="text", text=text)], "end_turn"

    def _next(self, messages: list) -> tuple[list, str]:
        seen = self._results(messages)
        if self.step == 0:
            return [_tool(0, "get_machine_state"), _tool(0, "get_purchase_requests", {"pending_only": True})], "tool_use"
        if self.step == 1:
            if (seen.get("get_purchase_requests") or {}).get("pending"):
                return self._answer("skip", "a request is still pending")
            state = {s["slot"]: s for s in (seen.get("get_machine_state") or {}).get("data", [])}
            self.slots = list(state)
            self.day_one = not any(s.get("product") for s in state.values())
            plan = (wide_day_one if self.wide else day_one)(self.slots)
            refills, fills = [], []
            for name in fill_order(plan):
                s = state.get(name, {})
                product = s.get("product") or (plan[name] if self.day_one else self._closet_pick(seen))
                if product is None:
                    continue
                if s.get("product") and (s.get("stock_on_hand") or 0) >= world.BY_SKU[product].column:
                    continue
                (refills if s.get("product") else fills).append(
                    {"slot": name, "product": product, "sale_price": _price(world.BY_SKU[product].reference_cents)})
            self.wish = refills + fills
            if not self.wish:
                return self._answer("skip", "every slot is full")
            return [_tool(1, "preview_desired_state", {"desired": self._desired(self.wish)})], "tool_use"
        preview = seen.get("preview_desired_state") or {}
        if self.step == 2:
            cost = preview.get("cost") or {}
            spendable = _cents(cost.get("spendable") or "0")
            limit = min(spendable - FLOOR, DAY_ONE_CAP) if self.day_one else spendable - FLOOR
            line_max = {c["slot"]: _cents(c["max"]) for c in cost.get("lines", [])}
            floors = {f["slot"]: _cents(f["floor"]) for f in preview.get("floors", [])}
            chosen, total = [], 0
            for s in self.wish:
                extra = line_max.get(s["slot"], 0)
                if total + extra <= limit:
                    chosen.append(s)
                    total += extra
            if not chosen:
                return self._answer("skip", f"nothing fits the spendable balance less ${FLOOR / 100:.2f}")
            price: dict[str, int] = {}
            for s in chosen:
                price[s["product"]] = max(price.get(s["product"], 0), floors.get(s["slot"], 0),
                                          world.BY_SKU[s["product"]].reference_cents)
            for s in chosen:
                s["sale_price"] = _price(price[s["product"]])
            self.desired = self._desired(chosen)
            return [_tool(2, "preview_desired_state", {"desired": self.desired})], "tool_use"
        if self.step == 3 and preview.get("ok"):
            return [_tool(3, "submit_desired_state", {"desired": self.desired,
                                                      "expected_plan_hash": preview.get("plan_hash")})], "tool_use"
        submitted = seen.get("submit_desired_state") or {}
        if submitted.get("request_id"):
            return self._answer("submit", f"refilled to the column; request {submitted['request_id']}")
        codes = sorted({r.get("code") for r in preview.get("refusals", [])})
        return self._answer("skip", f"the plan was refused ({', '.join(codes) or submitted.get('message', '')})")

    def _desired(self, chosen: list[dict]) -> dict:
        given = {s["slot"]: s for s in chosen}
        return {"slots": [given.get(name, {"slot": name, "keep": True}) for name in self.slots]}

    @staticmethod
    def _closet_pick(seen: dict) -> str | None:
        """For an empty slot after day one: the product the closet holds most of, if it fills a column."""
        closet = (seen.get("get_machine_state") or {}).get("closet") or []
        best = max(closet, key=lambda c: c["units"], default=None)
        if best and best["product"] in world.BY_SKU and best["units"] >= world.BY_SKU[best["product"]].column:
            return best["product"]
        return None


def wide_day_one(slots: list[str] | tuple[str, ...]) -> dict[str, str]:
    """Day one with as many different drinks as there are: every drink with a store price and a column
    on the loading chart, ranked by expected profit a class day at its reference price (rate x (price
    / 1.08 - cheapest unit cost)), one per slot of `slots` in the day-one fill order; the best repeat if
    there are fewer drinks than slots."""
    def profit(sku: str) -> float:
        p = world.BY_SKU[sku]
        cost = min(float(c) for (s, _), c in world.STORE_UNIT_PRICE.items() if s == sku)
        return p.rate * (p.reference_cents / 1.08 - cost)
    skus = sorted({s for s, _ in world.STORE_UNIT_PRICE if s in world.BY_SKU}, key=profit, reverse=True)
    skus = [s for s in skus if profit(s) > 0]
    return {slot: skus[i % len(skus)] for i, slot in enumerate(fill_order(day_one(slots)))}


FLOOR = 5300          # the spendable floor, $53.00 (docs/opening-capital-plan.md)
DAY_ONE_CAP = 36600   # the day-one fill cap, $366.00 most (docs/opening-capital-plan.md)


def _price(cents: int) -> str:
    return f"{cents / 100:.2f}"


def _cents(text: str) -> int:
    return int(round(float(text) * 100))
