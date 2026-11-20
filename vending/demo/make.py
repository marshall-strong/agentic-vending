"""Make the demo dashboard's made-up snapshot (`vending/demo/snapshot.json`).

    DATABASE_URL=postgresql://...@127.0.0.1:PORT/postgres python -m vending.demo.make
    python -m vending.demo.make --check      # is the committed file what this makes?

It builds a throwaway schema in a LOCAL Postgres (it refuses any other host), fills it with made-up
data, and runs the dashboard's own `snapshot()` on it as the public page does. So the file has exactly
the shape the live page has, and it can't drift from it: change the snapshot's shape, run this
again, and the diff shows what moved. Then the schema is dropped.

Everything here is invented: the products, prices, sales, runs, tool calls, requests and the 20
selections (`SELECTIONS`, a scattered set that is not the team's assignment and isn't read from it).
The fixed moment is Wed 9 Dec 2026 10:00 Eastern, a week into the competition. Nothing reads the
clock, the team's database or the bridge, so the same run makes the same file.

Developer tool: it isn't imported by the pages, and needs a database the pages never touch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest import mock

UTC = timezone.utc
# Wed 9 Dec 2026 10:00 Eastern (UTC-5).
AS_OF = datetime(2026, 12, 9, 15, 0, tzinfo=UTC)
# Opening capital: Wed 2 Dec, 10:00 Eastern. The first fill is loaded the day the competition starts.
OPENED = datetime(2026, 12, 2, 15, 0, tzinfo=UTC)
LOAD = datetime(2026, 12, 3, 15, 0, tzinfo=UTC)
# A midweek visit (Saturday noon) that tops up the three fastest sellers.
VISIT = datetime(2026, 12, 5, 17, 0, tzinfo=UTC)
LAST_POLL = datetime(2026, 12, 9, 13, 30, tzinfo=UTC)

# Twenty made-up selections, scattered over the machine (not whole columns, and not the team's own).
SELECTIONS = ("11", "13", "16", "17", "22", "24", "25", "28", "31", "33",
              "36", "37", "41", "44", "46", "47", "52", "54", "55", "58")

MODEL = "claude-sonnet-5-5"

# sku: (name, line, flavor, size, units per pack, price per pack in dollars). Made-up SKUs.
PRODUCTS: dict[str, tuple[str, str, str | None, str, int, str]] = {
    "COCA-ORIGINAL-20BOT": ("Coca-Cola 20 oz", "Coca-Cola", None, "20 oz bottle", 12, "17.40"),
    "COCA-DIET-20BOT": ("Diet Coke 20 oz", "Diet Coke", None, "20 oz bottle", 12, "17.40"),
    "COCA-ZERO_SUG-20BOT": ("Coke Zero 20 oz", "Coke Zero", None, "20 oz bottle", 12, "17.40"),
    "SPRITE-ORIGINAL-20BOT": ("Sprite 20 oz", "Sprite", None, "20 oz bottle", 12, "16.80"),
    "PEPSI-ORIGINAL-20BOT": ("Pepsi 20 oz", "Pepsi", None, "20 oz bottle", 12, "16.20"),
    "MOUNTAIN-ORIGINAL-20BOT": ("Mountain Dew 20 oz", "Mountain Dew", None, "20 oz bottle", 12, "16.20"),
    "DRPEPPER-ORIGINAL-20BOT": ("Dr Pepper 20 oz", "Dr Pepper", None, "20 oz bottle", 12, "16.80"),
    "MOUNTAIN-BAJA_BLA-20BOT": ("Mountain Dew Baja Blast 20 oz", "Mountain Dew", "Baja Blast", "20 oz bottle", 12, "16.80"),
    "BUBLY-STRAWBER-12CAN": ("bubly Strawberry 12 oz", "bubly", "Strawberry", "12 oz can", 12, "7.50"),
    "BUBLY-PARADISE-12CAN": ("bubly Paradise Paloma 12 oz", "bubly", "Paradise Paloma", "12 oz can", 12, "7.50"),
    "RECESS-LIME-12CAN": ("Recess Lime 12 oz", "Recess", "Lime", "12 oz can", 12, "11.40"),
    "TOPOCHICO-ORIGINAL-16BOT": ("Topo Chico 16 oz glass", "Topo Chico", None, "16 oz glass bottle", 12, "14.40"),
    "VITAMINWATER-DARK-20BOT": ("vitaminwater dark 20 oz", "vitaminwater", "dark", "20 oz bottle", 12, "15.60"),
    "REDBULL-ORIGINAL-8_4CAN": ("Red Bull 250 ml", "Red Bull", None, "250 ml can", 12, "20.40"),
    "MONSTER-ORIGINAL-16CAN": ("Monster 16 oz", "Monster", None, "16 oz can", 12, "21.00"),
    "SNAPPLE-ORIGINAL-16BOT": ("Snapple 16 oz", "Snapple", None, "16 oz bottle", 12, "13.80"),
    "PREMIER-CHOCOLAT-11CAR": ("Premier Protein Chocolate 11 oz", "Premier Protein", "Chocolate", "11 oz carton", 12, "31.20"),
    "GORGIE-ORIGINAL-20BOT": ("GORGIE Electrolyte Water 20 oz", "GORGIE", None, "20 oz bottle", 12, "15.00"),
}

# Day one: (slot, sku, fill to, tag price, units a day, the agent's reason). `slot` is one of SELECTIONS.
DAY_ONE = (
    ("11", "COCA-ORIGINAL-20BOT", 7, "2.50", 3.7, "Coca-Cola is the safest seller in the fall figures. Fill the lane."),
    ("13", "COCA-DIET-20BOT", 7, "2.50", 1.1, "Diet Coke followed Coca-Cola in the fall figures."),
    ("16", "COCA-ZERO_SUG-20BOT", 7, "2.50", 0.8, "Coke Zero sold steadily in the fall."),
    ("17", "SPRITE-ORIGINAL-20BOT", 7, "2.50", 0.7, "A lemon-lime to sit beside the colas."),
    ("22", "BUBLY-STRAWBER-12CAN", 8, "1.75", 0.9, "Cans are cheap to buy, so a lower price should keep them moving."),
    ("24", "BUBLY-PARADISE-12CAN", 8, "1.75", 0.5, "A second bubly flavour, to see which one wins."),
    ("25", "TOPOCHICO-ORIGINAL-16BOT", 7, "2.75", 0.6, "Glass sparkling water, priced up for the bottle."),
    ("28", "VITAMINWATER-DARK-20BOT", 7, "2.50", 0.4, "A non-fizzy choice."),
    ("31", "PEPSI-ORIGINAL-20BOT", 7, "2.50", 2.0, "Pepsi sold well in the fall figures."),
    ("33", "MOUNTAIN-ORIGINAL-20BOT", 7, "2.50", 1.8, "Mountain Dew is the strongest of the greens."),
    ("36", "DRPEPPER-ORIGINAL-20BOT", 7, "2.50", 0.7, "Dr Pepper, for people who want neither cola."),
    ("37", "REDBULL-ORIGINAL-8_4CAN", 11, "3.00", 5.9, "The energy drink with the best margin, and the small can fits 11."),
    ("41", "MONSTER-ORIGINAL-16CAN", 8, "3.25", 1.3, "A second energy drink, in the bigger can."),
    ("44", "SNAPPLE-ORIGINAL-16BOT", 7, "2.50", 0.3, "Tea, to cover a different taste."),
    ("46", "PREMIER-CHOCOLAT-11CAR", 9, "4.00", 0.5, "A protein shake for the gym crowd, at a price that pays for the carton."),
    ("47", "RECESS-LIME-12CAN", 8, "2.25", 0.6, "A new-wave drink that has a following."),
    ("52", "MOUNTAIN-BAJA_BLA-20BOT", 7, "2.50", 0.9, "A colourful flavour for variety."),
    ("54", "COCA-ORIGINAL-20BOT", 7, "2.50", 0.8, "A second Coca-Cola lane, since it sells best."),
    ("55", "TOPOCHICO-ORIGINAL-16BOT", 7, "2.75", 0.0, "More sparkling water, if it can be bought."),
    ("58", "MOUNTAIN-BAJA_BLA-20BOT", 7, "2.50", 0.0, "A second Baja Blast lane, if it can be bought."),
)
# Not on the shelves on day one: the day-one order found no seller for them.
NOT_BOUGHT = {"55", "58"}
# Topped up by the midweek visit.
TOPPED_UP = ("11", "33", "37")
# Where the agent's next plan differs from the one before it: slot -> (sku, fill to, price, reason).
NEXT_CHANGES = {
    "11": (None, None, "2.75", "Coca-Cola ran out on Saturday morning before the top-up, and is down to 1 again. "
                               "One more quarter should still sell it through."),
    "31": (None, None, "2.75", "Pepsi sold out on Sunday night, three and a half days after the fill. "
                               "Raising the price a quarter."),
    "37": (None, None, "3.25", "Red Bull sold out on Saturday morning and again early on Monday at $3.00. "
                               "$3.25 is still under the ceiling."),
    "24": (None, None, "1.50", "bubly Paradise Paloma sold 1 since Thursday, a third of what Strawberry sold at the same "
                               "price. A quarter off, to see if it moves."),
    "28": ("GORGIE-ORIGINAL-20BOT", None, None, "vitaminwater sold 1 since Thursday, one of the slowest lanes. GORGIE's request "
                                                "was approved, so swap it in."),
    "44": (None, 0, None, "Snapple sold 1 since Thursday. Pull it to the closet now, so the lane is free for a better "
                          "seller: the next plan swaps one in and fills it to its column."),
}
# Next Friday's order (pending): slot -> (sku, fill to, price, a new product for the slot). The swap, the two
# lanes that found no seller, and the lanes the closet can't fill to their column by Friday.
FRIDAY_ORDER = {
    "31": ("PEPSI-ORIGINAL-20BOT", 7, "2.75", False), "41": ("MONSTER-ORIGINAL-16CAN", 8, "3.25", False),
    "28": ("GORGIE-ORIGINAL-20BOT", 7, "2.50", True), "55": ("TOPOCHICO-ORIGINAL-16BOT", 7, "2.75", True),
    "58": ("MOUNTAIN-BAJA_BLA-20BOT", 7, "2.50", True), "37": ("REDBULL-ORIGINAL-8_4CAN", 11, "3.25", False),
    "13": ("COCA-DIET-20BOT", 7, "2.50", False), "22": ("BUBLY-STRAWBER-12CAN", 8, "1.75", False),
    "46": ("PREMIER-CHOCOLAT-11CAR", 9, "4.00", False),
}
# Reworded reasons on slots the plan leaves as they were: not a change.
STILL = "Unchanged from last week: the numbers don't argue for a move."

# Units per column for the packages the loading chart doesn't cover: a made-up count, as a person would
# record it. Every lane is filled to its column at every restock.
CAPACITY_OVERRIDES = {"PREMIER-CHOCOLAT-11CAR": 9}

# Visitors tile: made-up counts, with browsers named by made-up ids.
BROWSERS = tuple(f"00000000000000{n:02x}" for n in range(1, 6))


def _utc(day: int, hour: int, minute: int = 0, month: int = 12) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


def _cents(price: str) -> int:
    return int(Decimal(price) * 100)


# --- The week's sales, worked out before anything is stored ---------------------------------------------

def simulate() -> dict:
    """Hour by hour from the first load: units on hand and sold per selection, and what the midweek visit adds.

    A selection sells `rate` units a day while it has stock; `VISIT` brings the topped-up three back to
    their fill. Returns `sold[hour_index][slot]` (cumulative), `on_hand_at_visit[slot]`, `end[slot]`.
    """
    plan = {s: (fill, rate) for s, sku, fill, price, rate, why in DAY_ONE if s not in NOT_BOUGHT}
    on_hand = {s: fill for s, (fill, _) in plan.items()}
    owed = dict.fromkeys(plan, 0.0)
    sold = dict.fromkeys(plan, 0)
    hours: dict[datetime, dict[str, int]] = {}
    at_visit: dict[str, int] = {}
    t = LOAD
    while t < LAST_POLL + timedelta(hours=1):
        t += timedelta(hours=1)
        if t == VISIT:
            for s in TOPPED_UP:
                at_visit[s] = on_hand[s]
                on_hand[s] = plan[s][0]
        for s, (fill, rate) in plan.items():
            owed[s] += rate / 24
            sell = min(on_hand[s], math.floor(owed[s]))
            on_hand[s] -= sell
            sold[s] += sell
            owed[s] -= sell
            if on_hand[s] == 0:
                owed[s] = min(owed[s], 0.99)
        hours[t] = dict(sold)
    return {"sold": hours, "at_visit": at_visit, "end": dict(on_hand)}


def poll_times() -> list[datetime]:
    """When the machine was polled: before the load, just after, every six hours, and 90 minutes before `AS_OF`."""
    times = [_utc(1, 12), _utc(3, 5), _utc(3, 13, 30), _utc(3, 15, 30)]
    t = _utc(3, 15, 30) + timedelta(hours=6)
    while t < LAST_POLL:
        times.append(t)
        t += timedelta(hours=6)
    times.append(LAST_POLL)
    return times


# --- Seeding ----------------------------------------------------------------------------------------------

def _document(records: list[str]) -> bytes:
    """A DEX document around `records`, with a correct checksum: the same framing `tests/test_evadts.py` builds."""
    from vending.evadts import crc16_arc

    head = b"DXS*0000000000*VA*V1/1*1**\r\n"
    body = ("\r\n".join(["ST*001*0001", *records]) + "\r\n").encode("latin-1")
    tail = f"G85*{crc16_arc(body):04X}\r\nSE*{len(records) + 3}*0001\r\nDXE*1*1\r\n".encode("ascii")
    return head + body + tail


def seed_polls(conn, sim: dict) -> None:
    from vending.slots import DEVICE_ID
    from vending.sync_history import store

    price = {s: _cents(p) for s, _, _, p, _, _ in DAY_ONE}
    every = [f"{shelf}{column}" for shelf in "12345" for column in "12345678"]
    sold_by_hour = sim["sold"]
    for number, at in enumerate(poll_times()):
        hour = at.replace(minute=0, second=0, microsecond=0)
        cumulative = sold_by_hour.get(hour, {})      # nothing sold before the first load
        records = ["ID1*DEMO-MACHINE*0000**EVS3.0*", "ID4*2*1840*5"]
        total_cents = total_units = 0
        lines = []
        for index, slot in enumerate(every):
            if slot in price:
                base = 100 + 7 * index
                units = base + cumulative.get(slot, 0)
                cents_each = price[slot]
            else:  # a selection that isn't one of the demo's twenty: made-up counters, never shown
                base = 60 + 3 * index
                units = base + int(0.7 * max((at - LOAD).total_seconds(), 0) / 86400)
                cents_each = 200
            cents = units * cents_each
            total_cents, total_units = total_cents + cents, total_units + units
            lines += [f"PA1*{slot}*{cents_each}*", f"PA2*{units}*{cents}*0*0*0*0"]
        records.append(f"VA1*{total_cents}*{total_units}*0*0")
        records += lines
        meta = {"session_id": f"demo-{number:03d}", "device_id": DEVICE_ID, "started_at": at.isoformat()}
        assert store(conn, meta, _document(records)) == "ok"


def _ask(sku: str, packs: int, sale: str | None = None, store: str = "Walmart", change: bool = False) -> dict:
    name, line, flavor, size, upp, pack_price = PRODUCTS[sku]
    ask = {"sku": sku, "store": store, "packs": packs, "units_per_pack": upp,
           "max_price_per_pack": str(Decimal(pack_price) + Decimal("1.50")), "change_product": change}
    if sale:
        ask["sale_price"] = sale
    return ask


def _line_row(request_id: str, number: int, slot: str, sku: str | None, outcome: str, loaded: int, lost: int,
              back: int, at: datetime, asked: dict, store_name: str = "Walmart", note: str | None = None) -> tuple:
    if sku is None:
        return (request_id, number, slot, "purchase", asked, "unavailable", None, None, 0, None, 0, Decimal(0), None, 0, 0,
                None, 0, 0, 0, None, False, at)
    packs = 1
    _, _, _, _, upp, pack_price = PRODUCTS[sku]
    return (request_id, number, slot, "purchase", asked, outcome, sku, store_name, packs, upp, upp * packs,
            Decimal(pack_price) * packs, None, loaded, lost, note, back, 0, 0, None, False, at)


def seed_purchases(conn, sim: dict) -> dict:
    """pr-001 (day one, fulfilled), pr-003 (the midweek visit, fulfilled) and pr-002 (next Friday, pending)."""
    from psycopg.types.json import Jsonb

    rows = {s: (sku, fill, price) for s, sku, fill, price, _, _ in DAY_ONE}
    day_one = []
    body_lines = []
    for number, (slot, sku, fill, price, _, _) in enumerate(DAY_ONE):
        asked = _ask(sku, 1, sale=price, change=True)
        body_lines.append({"slot": slot, "source": "purchase", "primary": asked})
        if slot in NOT_BOUGHT:
            day_one.append(_line_row("pr-001", number, slot, None, "unavailable", 0, 0, 0, LOAD, asked))
        else:
            upp = PRODUCTS[sku][4]
            lost = 1 if slot == "22" else 0
            outcome, shop = ("alternate", "Amazon") if slot == "28" else ("primary", "Walmart")
            day_one.append(_line_row("pr-001", number, slot, sku, outcome, fill, lost, upp - fill - lost, LOAD, asked,
                                     shop, "one can dented in the box" if lost else None))
    paid_one = sum(r[11] or 0 for r in day_one)
    conn.execute(
        "insert into purchase_requests (request_id, submitted_at, off_cycle, request_body, status, decision_note,"
        " total_paid, fulfilled_at) values ('pr-001', %s, false, %s, 'fulfilled', 'Filled; 2 of 20 selections had"
        " no seller', %s, %s)",
        (_utc(2, 19, 30), Jsonb({"off_cycle": False, "lines": body_lines}), paid_one, LOAD))

    visit_lines, visit_body = [], []
    for number, slot in enumerate(TOPPED_UP):
        sku, fill, price = rows[slot]
        loaded = fill - sim["at_visit"][slot]
        asked = _ask(sku, 1, sale=price)
        visit_body.append({"slot": slot, "source": "purchase", "primary": asked})
        visit_lines.append(_line_row("pr-003", number, slot, sku, "primary", loaded, 0, PRODUCTS[sku][4] - loaded,
                                     VISIT, asked))
    paid_visit = sum(r[11] for r in visit_lines)
    conn.execute(
        "insert into purchase_requests (request_id, submitted_at, off_cycle, request_body, status, decision_note,"
        " total_paid, fulfilled_at, visit_fee_cents) values ('pr-003', %s, true, %s, 'fulfilled', 'Midweek visit done',"
        " %s, %s, 2500)",
        (_utc(4, 21), Jsonb({"off_cycle": True, "lines": visit_body}), paid_visit, VISIT))

    # Next Friday: what the 7 Dec plan asks to buy. Pending, so it shows as committed in Money. A lane the
    # closet can fill to its column by Friday (Coca-Cola, Mountain Dew and most others) isn't bought again.
    pending = []
    for slot, (sku, fill, price, retry) in FRIDAY_ORDER.items():
        line = {"slot": slot, "source": "purchase",
                "primary": _ask(sku, 1, sale=price, store="Amazon", change=retry)}
        if retry:
            line["alternate"] = _ask(sku, 1, sale=price, store="Walmart", change=retry)
        pending.append(line)
    conn.execute(
        "insert into purchase_requests (request_id, submitted_at, off_cycle, request_body, status)"
        " values ('pr-002', %s, false, %s, 'pending')",
        (_utc(7, 14, 40), Jsonb({"off_cycle": False, "lines": pending})))

    with conn.cursor() as cur:
        cur.executemany(
            "insert into purchase_lines (request_id, line_no, slot, source, asked, outcome, sku, store, packs_bought,"
            " units_per_pack, units_bought, total_paid, unit_cost_paid, units_loaded, units_lost, loss_note,"
            " units_to_back_stock, units_from_back_stock, units_pulled, pulled_sku, slot_reassigned, resolved_at)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [(*r[:4], Jsonb(r[4]), *r[5:]) for r in day_one + visit_lines])
    return {"paid_one": paid_one, "paid_visit": paid_visit}


PLAN_ONE_REASON = "Day-one fill, from the fall sales and the loading chart."


def _entry(slot: str, sku: str, fill: int, price: str, reason: str) -> dict:
    return {"slot": slot, "product": sku, "fill_to": fill, "sale_price": price, "reason": reason}


def plan_one() -> dict:
    return {"visit": "scheduled", "reason": PLAN_ONE_REASON,
            "slots": [_entry(s, sku, fill, price, why) for s, sku, fill, price, _, why in DAY_ONE]}


def plan_next() -> dict:
    slots = []
    for s, sku, fill, price, _, why in DAY_ONE:
        change = NEXT_CHANGES.get(s)
        if change:
            new_sku, new_fill, new_price, reason = change
            slots.append(_entry(s, new_sku or sku, fill if new_fill is None else new_fill, new_price or price, reason))
        else:
            slots.append(_entry(s, sku, fill, price, STILL))
    return {"visit": "scheduled", "reason": "Raise three prices on sell-outs, lower one, swap the slowest lane, "
                                            "and pull one for a swap to come.",
            "slots": slots}


def seed_runs(conn) -> dict[str, int]:
    """Six runs: the day-one run, a failure, a midweek top-up, a replay, Monday's run and one in progress."""
    from vending import agent_runs

    ids = {}

    def run(key: str, mode: str, start: datetime, end: datetime | None, status: str, **fields) -> None:
        run_id = agent_runs.start(conn, mode=mode, model=MODEL)
        if status != "running":
            agent_runs.finish(conn, run_id, status, **fields)
        conn.execute("update agent_runs set started_at = %s, finished_at = %s where id = %s", (start, end, run_id))
        ids[key] = run_id

    run("one", "live", _utc(2, 19), _utc(2, 19, 6), "submitted", plan=plan_one(), cost_usd=Decimal("0.46"),
        request_id="pr-001",
        update="First fill is ordered: 18 of the 20 selections have a seller. Two selections found none.",
        notes="Selections 55 and 58 had no seller on day one. Try them again next Friday.")
    run("failed", "live", _utc(4, 14), _utc(4, 14, 3), "failed", cost_usd=Decimal("0.12"),
        detail="The model call timed out after 120 s")
    midweek = plan_one()
    midweek["visit"] = "off_cycle"
    midweek["reason"] = ("Coca-Cola and Red Bull are down to 1 each, with the weekend ahead. A visit costs $25 "
                         "and the lost sales cost more; Mountain Dew is topped up on the same trip.")
    run("visit", "live", _utc(5, 14), _utc(5, 14, 9), "submitted", plan=midweek, cost_usd=Decimal("0.37"),
        request_id="pr-003",
        update="Midweek visit requested: Coca-Cola, Mountain Dew and Red Bull to be topped up to full.",
        notes="Check on Monday whether the three lanes sold enough to pay for the visit.")
    run("replay", "replay", _utc(6, 12), _utc(6, 12, 2), "skipped", cost_usd=Decimal("0.00"),
        detail="Replay of the week of 23 Nov with the same rules; nothing to submit")
    run("friday", "live", _utc(7, 14), _utc(7, 14, 12), "submitted", plan=plan_next(), cost_usd=Decimal("0.58"),
        request_id="pr-002",
        update="Next Friday's order: three prices up, one down, one swap, one lane pulled for a swap to come. "
               "Selections 55 and 58 get another "
               "try at a seller. Coca-Cola and Mountain Dew refill from the closet; Red Bull, Diet Coke, "
               "bubly Strawberry and the protein shake are bought, since the closet can't fill them.",
        notes="Pepsi sold out on Sunday: if it sells out again at $2.75, price it at $3.00. "
              "Check whether GORGIE outsells vitaminwater, and pick the product for 44.")
    run("now", "live", _utc(9, 14, 50), None, "running")
    return ids


def seed_steps(conn, ids: dict[str, int]) -> None:
    steps = {
        "one": [
            (1, _utc(2, 19, 0), "thinking", None, "It is the first fill. I need what the machine holds, what the "
             "catalog sells and what the fall figures say.", False),
            (1, _utc(2, 19, 1), "tool_call", "get_catalog", "{}", False),
            (1, _utc(2, 19, 1), "tool_result", "get_catalog", "18 products, 17 with a price per pack", False),
            (2, _utc(2, 19, 3), "thinking", None, "Twenty lanes and a $600 budget. Cans are cheap and sell at $1.75; "
             "the big bottles carry the margin.", False),
            (2, _utc(2, 19, 4), "tool_call", "preview_desired_state", '{"slots": 20}', False),
            (2, _utc(2, 19, 4), "tool_result", "preview_desired_state", "20 lines, 0 refusals", False),
            (3, _utc(2, 19, 5), "tool_call", "submit_desired_state", '{"slots": 20}', False),
            (3, _utc(2, 19, 6), "text", None, "Submitted request pr-001 for 20 selections.", False),
        ],
        "failed": [
            (1, _utc(4, 14, 0), "thinking", None, "Checking Friday's stock before deciding whether a visit is worth it.", False),
            (1, _utc(4, 14, 1), "tool_call", "get_machine_state", "{}", False),
            (1, _utc(4, 14, 3), "tool_result", "get_machine_state", "the call timed out", True),
        ],
        "friday": [
            (1, _utc(7, 14, 0), "thinking", None, "Monday. Last week's plan is the baseline. I want sales per lane, "
             "what sold out, and what the ledger says I can spend.", False),
            (1, _utc(7, 14, 1), "tool_call", "get_machine_state", "{}", False),
            (1, _utc(7, 14, 1), "tool_result", "get_machine_state", "20 lanes; 31 and 37 empty, 11 down to 1; 11, 33 and 37 "
             "topped up on Saturday", False),
            (1, _utc(7, 14, 2), "tool_call", "get_account_balance", "{}", False),
            (1, _utc(7, 14, 2), "tool_result", "get_account_balance", "balance and spendable both well above the $53 floor", False),
            (2, _utc(7, 14, 4), "thinking", None, "Coca-Cola and Red Bull ran out before Saturday's top-up and are "
             "empty or nearly again, and Pepsi ran out on Sunday. Their price is too low for the demand, and a "
             "quarter more should still clear them.", False),
            (2, _utc(7, 14, 5), "tool_call", "get_sales_history", '{"slot": "31"}', False),
            (2, _utc(7, 14, 5), "tool_result", "get_sales_history", "7 sold by Sunday night, empty since", False),
            (3, _utc(7, 14, 7), "thinking", None, "vitaminwater sold 1 since Thursday. GORGIE was approved in "
             "November; try it there. Snapple sold 1, so pull it and swap a new product in next week.", False),
            (3, _utc(7, 14, 8), "tool_call", "preview_desired_state", '{"slots": 20}', False),
            (3, _utc(7, 14, 8), "tool_result", "preview_desired_state", "most we pay $173.40, 0 refusals", False),
            (4, _utc(7, 14, 10), "tool_call", "submit_desired_state", '{"slots": 20}', False),
            (4, _utc(7, 14, 12), "text", None, "Submitted request pr-002.", False),
        ],
        "now": [
            (1, _utc(9, 14, 50), "thinking", None, "Wednesday check: is anything wrong before Thursday's cutoff?", False),
            (1, _utc(9, 14, 51), "tool_call", "get_machine_state", "{}", False),
            (1, _utc(9, 14, 51), "tool_result", "get_machine_state", "20 lanes; last poll 1 h 21 min ago", False),
            (2, _utc(9, 14, 53), "tool_call", "submit_desired_state", '{"slots": 20}', False),
            (2, _utc(9, 14, 53), "tool_result", "submit_desired_state", "refused: a request for Friday is already pending (pr-002)", True),
        ],
    }
    with conn.cursor() as cur:
        for key, rows in steps.items():
            cur.executemany(
                "insert into agent_steps (run_id, turn, at, kind, tool, content, is_error) values (%s, %s, %s, %s, %s, %s, %s)",
                [(ids[key], *row) for row in rows])


def seed_actions(conn) -> None:
    """The tool-call log: the real writes made while seeding are replaced by made-up ones at made-up times."""
    from psycopg.types.json import Jsonb

    conn.execute("delete from action_log")
    done, refused, failed = "done", "refused", "failed"
    rows = [
        (_utc(2, 13, 40), "write", "submit_desired_state", {"slots": 20}, refused, "The agent is halted, so no write tool can run."),
        (_utc(2, 14, 0), "write", "halt", {"state": "off", "by": "kevin"}, done, "halt switch turned off by kevin"),
        (_utc(2, 14, 30), "write", "config.set", {"key": "capacity_overrides", "value": CAPACITY_OVERRIDES}, done,
         f"config capacity_overrides set to {json.dumps(CAPACITY_OVERRIDES)} by kevin"),
        (_utc(2, 15, 0), "write", "ledger.opening", {"amount": "600.00"}, done, "opening 600.00 recorded by kevin: opening capital"),
        (_utc(2, 19, 0), "read", "get_project_rules", {"topic": "pricing"}, done, None),
        (_utc(2, 19, 1), "read", "get_catalog", {}, done, None),
        (_utc(2, 19, 2), "read", "get_account_balance", {}, done, None),
        (_utc(2, 19, 4), "read", "preview_desired_state", {"slots": 20}, done, None),
        (_utc(2, 19, 5), "write", "submit_desired_state", {"slots": 20}, done, "request pr-001 submitted"),
        (_utc(4, 14, 1), "read", "get_machine_state", {}, failed, "the call timed out"),
        (_utc(5, 14, 1), "read", "get_machine_state", {}, done, None),
        (_utc(5, 14, 3), "read", "get_sales_history", {"slot": "11"}, done, None),
        (_utc(5, 14, 4), "read", "get_sales_history", {"slot": "33"}, done, None),
        (_utc(5, 14, 5), "read", "get_sales_history", {"slot": "37"}, done, None),
        (_utc(5, 14, 7), "read", "preview_desired_state", {"slots": 20, "off_cycle": True}, done, None),
        (_utc(5, 14, 9), "write", "submit_desired_state", {"slots": 20, "off_cycle": True}, done, "request pr-003 submitted"),
        (_utc(7, 14, 1), "read", "get_machine_state", {}, done, None),
        (_utc(7, 14, 2), "read", "get_account_balance", {}, done, None),
        (_utc(7, 14, 3), "read", "get_pnl", {}, done, None),
        (_utc(7, 14, 5), "read", "get_sales_history", {"slot": "31"}, done, None),
        (_utc(7, 14, 6), "read", "get_sales_history", {"slot": "28"}, done, None),
        (_utc(7, 14, 7), "read", "get_purchase_requests", {}, done, None),
        (_utc(7, 14, 8), "read", "preview_desired_state", {"slots": 20}, done, None),
        (_utc(7, 14, 9), "write", "submit_desired_state", {"slots": 20},
         refused, "Selection 37: $3.50 is above the price ceiling of $3.25. Price it at or under the ceiling."),
        (_utc(7, 14, 11), "write", "submit_desired_state", {"slots": 20}, done, "request pr-002 submitted"),
        (_utc(8, 16, 0), "read", "get_machine_state", {}, done, None),
        (_utc(9, 14, 51), "read", "get_machine_state", {}, done, None),
        (_utc(9, 14, 52), "read", "get_pnl", {}, failed, "the bridge answered 503"),
        (_utc(9, 14, 53), "write", "submit_desired_state", {"slots": 20}, refused,
         "A request for this Friday is already pending (pr-002). Wait for it to be filled or rejected."),
    ]
    with conn.cursor() as cur:
        cur.executemany(
            "insert into action_log (at, kind, tool, arguments, outcome, detail) values (%s, %s, %s, %s, %s, %s)",
            [(at, kind, tool, Jsonb(args), outcome, detail) for at, kind, tool, args, outcome, detail in rows])


def seed_catalog_and_requests(conn) -> None:
    from psycopg.types.json import Jsonb

    with conn.cursor() as cur:
        cur.executemany(
            "insert into catalog_products (sku, name, line, flavor, size, last_response) values (%s, %s, %s, %s, %s, '{}')",
            [(sku, name, line, flavor, size) for sku, (name, line, flavor, size, _, _) in PRODUCTS.items()])
        requests = [
            (_utc(18, 14, month=11), "GORGIE Electrolyte Water", "20 oz bottle", "GORGIE", None, "approved", "GORGIE-ORIGINAL-20BOT", "Approved."),
            (_utc(19, 15, month=11), "Topo Chico Sparkling Water", "16 oz glass bottle", "Topo Chico", None, "approved", "TOPOCHICO-ORIGINAL-16BOT", "Approved."),
            (_utc(19, 15, month=11), "Premier Protein Chocolate", "11 oz carton", "Premier Protein", "Chocolate", "approved", "PREMIER-CHOCOLAT-11CAR", "Approved."),
            (_utc(20, 13, month=11), "Red Bull", "250 ml can", "Red Bull", None, "approved", "REDBULL-ORIGINAL-8_4CAN", "Approved."),
            (_utc(21, 16, month=11), "Mountain Dew Baja Blast", "20 oz bottle", "Mountain Dew", "Baja Blast", "approved", "MOUNTAIN-BAJA_BLA-20BOT", "Approved."),
            (_utc(22, 17, month=11), "Recess Lime", "12 oz can", "Recess", "Lime", "approved", "RECESS-LIME-12CAN", "Approved."),
            (_utc(23, 12, month=11), "Vita Coco Coconut Water", "11.1 oz carton", "Vita Coco", None, "rejected", None, "Not available from the distributor."),
            (_utc(1, 15), "Iced Energy Peach", "12 oz can", "Iced Energy", "Peach", "pending", None, None),
            (_utc(3, 16), "Monster Ultra White", "16 oz can", "Monster", "Ultra White", "pending", None, None),
            (_utc(8, 14), "Snapple Peach Tea", "16 oz bottle", "Snapple", "Peach", "pending", None, None),
        ]
        cur.executemany(
            "insert into product_requests (requested_at, name, size, line, flavor, words, bridge_id, status, decision_note,"
            " sku, last_response) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, '{}')",
            [(at, name, size, line, flavor, [w.lower() for w in f"{name} {size}".split()], f"demo-req-{n:02d}",
              status, note, sku) for n, (at, name, size, line, flavor, status, sku, note) in enumerate(requests, 1)])


def seed_counts(conn) -> None:
    """A photo count of three selections: two of ours, and one that isn't (the page drops it)."""
    from vending import slot_counts
    from vending.slots import DEVICE_ID

    slot_counts.record(conn, slot_counts.parse({
        "count_id": "demo-photo-1", "counted_at": _utc(8, 15).isoformat(), "source": "photo", "device_id": DEVICE_ID,
        "selections": [
            {"selection": "55", "product": "Sparkling water 16 oz (brand not readable)", "units": 4, "price_on_tag": "2.00"},
            {"selection": "44", "product": "Snapple 16 oz", "units": 6, "price_on_tag": "2.50"},
            {"selection": "12", "product": "Not one of the demo's twenty", "units": 3, "price_on_tag": "2.00"}]}),
        "demo")


def seed_ledger(conn) -> None:
    from vending import ledger

    ledger.record(conn, "opening", 60000, "opening capital", "demo", at=OPENED)
    # Made-up development spend from before the opening: it is outside the balance, and the panel says so.
    for n, (day, cost) in enumerate(((24, "0.31"), (27, "0.44"), (30, "0.29")), 1):
        ledger.record_tokens(conn, cost, MODEL, 21000 + n * 900, 1800 + n * 120, f"demo_dev_{n}", at=_utc(day, 16, month=11))
    for key, (at, cost) in {"one": (_utc(2, 19, 6), "0.46"), "failed": (_utc(4, 14, 3), "0.12"),
                            "visit": (_utc(5, 14, 9), "0.37"), "friday": (_utc(7, 14, 12), "0.58"),
                            "now": (_utc(9, 14, 53), "0.09")}.items():
        ledger.record_tokens(conn, cost, MODEL, 40000, 2500, f"demo_run_{key}", at=at)


def seed_visits(conn) -> None:
    """The Visitors tile: made-up counts. A browser is a made-up id, as the counter's cookie would be."""
    conn.execute("update page_visit_totals set browsers = 5, sign_ins = 9, visits = 31, since = %s", (_utc(2, 16),))
    rows = [(_utc(day, hour), "view", "/dashboard", BROWSERS[number % 4])
            for number, (day, hour) in enumerate(((3, 14), (3, 20), (4, 15), (4, 22), (5, 13), (6, 17),
                                                  (7, 15), (7, 21), (8, 14), (8, 19), (9, 12), (9, 14)))]
    with conn.cursor() as cur:
        cur.executemany("insert into page_visits (at, kind, page, browser) values (%s, %s, %s, %s)", rows)
        # (page_visits has an index on `at`; the writer's own bookkeeping isn't needed to read the counts.)


def seed(conn) -> None:
    """Fill a migrated, empty schema with the made-up week."""
    from vending import config, slots

    conn.execute("insert into slot_assignment (team, device_id, slots, first_read_at, last_checked_at, response)"
                 " values (%s, %s, %s, %s, %s, '{}')",
                 (slots.DEFAULT_TEAM, slots.DEVICE_ID, list(SELECTIONS), _utc(1, 15, month=11), _utc(3, 10)))
    seed_catalog_and_requests(conn)
    config.set_value(conn, "capacity_overrides", CAPACITY_OVERRIDES, "kevin")
    sim = simulate()
    seed_ledger(conn)
    seed_polls(conn, sim)
    seed_purchases(conn, sim)
    ids = seed_runs(conn)
    seed_steps(conn, ids)
    seed_counts(conn)
    seed_visits(conn)
    config.set_halted(conn, False, "kevin")
    conn.execute("update config set updated_at = %s where key = 'halted'", (_utc(2, 14),))
    seed_actions(conn)


# --- Making the file --------------------------------------------------------------------------------------

def sync_round() -> dict:
    """The worker's last sync round as `/health` shows it: made up, and all well."""
    jobs = (("slots_check", 0.4), ("sync_history", 3.1), ("sync_requests", 2.2), ("sync_catalog", 1.6),
            ("sync_product_requests", 1.1))
    start = _utc(9, 14, 0)
    return {"status": "ok", "started_at": _utc(2, 15, 30).isoformat(), "rounds": 162,
            "last_round_at": start.isoformat(), "next_round_at": _utc(9, 15, 0).isoformat(),
            "results": [{"job": job, "started_at": start.isoformat(), "seconds": seconds, "exit_code": 0, "outcome": "ok"}
                        for job, seconds in jobs]}


def make(writer, reader) -> dict:
    """The demo snapshot from a migrated empty schema: `writer` seeds it, `reader` (read-only, like the
    dashboard's own connection) is snapshotted the way the public page is."""
    from vending import dashboard, money_panel, slots

    # Our slots are read as the clock stood at AS_OF, a week after the lock.
    with mock.patch.object(slots, "clock", lambda conn: AS_OF):
        seed(writer)
        snap = dashboard.snapshot(reader, AS_OF, public=True, syncs=sync_round())
        # Retain the replay, dialogue and development spend notes from the fictional seed.
        # The served demo reads the finished file and never calls these readers.
        tags = {slot: row["price"] for slot, row in snap["sales"]["data"]["slots"].items()}
        snap["runs"]["data"] = dashboard._runs(reader, AS_OF, tags, dashboard.our_selections(snap),
                                                modes=("live", "dry-run", "replay"))
        snap["money"]["data"]["notes"] = money_panel.read(reader, AS_OF)["notes"]
    for run in snap["runs"]["data"]:
        if run["status"] == "failed" and run["detail"]:
            run["detail"] = dashboard.HIDDEN
    snap["demo"] = True
    return snap


def _require_local(url: str) -> None:
    from psycopg import ProgrammingError
    from psycopg.conninfo import conninfo_to_dict

    try:
        params = conninfo_to_dict(url)
    except ProgrammingError:
        sys.exit("DATABASE_URL can't be parsed, so it can't be shown to be local")
    # The environment can redirect a connection too: PGHOSTADDR wins over the URL's host, and a
    # service file can name any server. Refuse both, and a service named in the URL.
    if params.get("service") or os.environ.get("PGSERVICE") or os.environ.get("PGSERVICEFILE"):
        sys.exit("DATABASE_URL or the environment names a Postgres service, which can point anywhere; "
                 "this tool only runs on a scratch Postgres on this machine")
    hosts = (params.get("host", "").split(",") + params.get("hostaddr", "").split(",")
             + os.environ.get("PGHOSTADDR", "").split(","))
    if any(h and h not in ("127.0.0.1", "localhost", "::1") for h in hosts) or not params.get("host"):
        sys.exit("DATABASE_URL isn't a local database; this tool only runs on a scratch Postgres on this machine")


@contextmanager
def scratch_schema(url: str):
    """(writer, reader) connections in a new schema, migrated; the schema is dropped on the way out."""
    import psycopg

    from vending import dashboard, db

    schema = f"demo_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(url, autocommit=True, connect_timeout=db.CONNECT_TIMEOUT,
                            options=db.session_options(url, "-c search_path="))
    admin.execute(f"create schema {schema}")
    try:
        with psycopg.connect(url, autocommit=True, connect_timeout=db.CONNECT_TIMEOUT,
                             options=db.session_options(url, f"-c search_path={schema}")) as writer:
            db.migrate(writer)
            with psycopg.connect(url, autocommit=True, connect_timeout=db.CONNECT_TIMEOUT,
                                 options=db.session_options(url, dashboard.READ_ONLY, f"-c search_path={schema}")) as reader:
                yield writer, reader
    finally:
        admin.execute(f"drop schema if exists {schema} cascade")
        admin.close()


def render_file(snap: dict) -> str:
    """The file's text: stable (sorted keys would reorder panels, so keep the snapshot's own order)."""
    from vending.demo import encode

    return json.dumps(encode(snap), indent=1, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    from vending import settings
    from vending.demo import SNAPSHOT

    parser = argparse.ArgumentParser(prog="python -m vending.demo.make", description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=SNAPSHOT)
    parser.add_argument("--check", action="store_true", help="say whether the committed file is what this makes")
    args = parser.parse_args(argv)
    url = settings.require("DATABASE_URL", "A scratch local Postgres; see AGENTS.md for how to start one.")
    _require_local(url)
    with scratch_schema(url) as (writer, reader):
        text = render_file(make(writer, reader))
    if args.check:
        same = args.out.read_text(encoding="utf-8") == text
        print("the committed snapshot is what the generator makes" if same else "the committed snapshot is OUT OF DATE; run without --check")
        return 0 if same else 1
    args.out.write_text(text, encoding="utf-8")
    print(f"wrote {args.out} ({len(text):,} bytes, sha256 {hashlib.sha256(text.encode()).hexdigest()[:12]})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
