"""Simulated time for our code, and tripwires that stop anything reaching the real world.

Our code reads "now" in two ways: `ledger.db_now` (the database clock, used by the ledger, the
planner and the harness's caps) and `datetime.now` in a few tool and sync modules. `patched` swaps
both for the simulated clock for as long as the simulation runs, and puts them back after.

The tripwires make the real bridge client and `urllib` raise on use, so a code path that would call
the DEX bridge fails loudly instead of sending a purchase request, a catalog request or a poll. The
model API goes through the Anthropic SDK (httpx), which these leave alone.
"""

from __future__ import annotations

import contextlib
import importlib
import urllib.request
from datetime import datetime, timezone
from typing import Iterator

# Every module that calls `datetime.now(...)` on a path the agent, the tools or the syncs can reach.
NOW_MODULES = ("vending.tools.machine_state", "vending.tools.purchase_requests", "vending.tools.catalog",
               "vending.tools.pnl", "vending.sync_catalog", "vending.sync_requests", "vending.sourcing",
               "vending.submissions", "vending.agent", "vending.tools.catalog_request", "vending.product_requests")


class Clock:
    def __init__(self, start: datetime):
        self._now = start.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._now

    def set(self, moment: datetime) -> None:
        moment = moment.astimezone(timezone.utc)
        if moment < self._now:
            raise ValueError(f"the clock can't go back from {self._now} to {moment}")
        self._now = moment


class RealWorldBlocked(RuntimeError):
    """Something in the simulation tried to reach the real bridge or the network."""


def _sim_datetime(clock: Clock) -> type:
    class SimDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: D102 - the simulated clock, in `tz` like datetime.now
            moment = clock.now()
            return moment.astimezone(tz) if tz is not None else moment.astimezone().replace(tzinfo=None)

        @classmethod
        def utcnow(cls):  # noqa: D102
            return clock.now().replace(tzinfo=None)
    return SimDatetime


@contextlib.contextmanager
def patched(clock: Clock) -> Iterator[None]:
    from vending import bridge, ledger

    saved: list[tuple[object, str, object]] = []

    def swap(target: object, name: str, value: object) -> None:
        saved.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def blocked(*_a, **_k):
        raise RealWorldBlocked("the simulation never calls the real DEX bridge or the network")

    sim_dt = _sim_datetime(clock)
    try:
        swap(ledger, "db_now", lambda conn: clock.now())
        for name in NOW_MODULES:
            module = importlib.import_module(name)
            if getattr(module, "datetime", None) is datetime:
                swap(module, "datetime", sim_dt)
        swap(bridge.Bridge, "__init__", blocked)
        swap(bridge.Bridge, "_once", blocked)
        swap(bridge.Bridge, "_get", blocked)
        swap(urllib.request, "urlopen", blocked)
        yield
    finally:
        for target, name, value in reversed(saved):
            setattr(target, name, value)
