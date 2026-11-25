"""The dashboard's price-check panel: what the four stores charge for the drinks in our plan, and the weekly
price checks that keep those prices fresh.

Where each figure comes from:

- **The weekly checks**: `price_check_runs` (migration 034, `vending.price_schedule`): when each was due, whether
  it compared every store or re-checked the stores we buy from, how many drinks it looked up, how many prices it
  changed, how many drinks it left without a usable price, and what it cost. The model is read from its lookups in
  `price_lookups` (migration 032), which the weekly check logs under its own name (`price_schedule.BY`).
- **Each store's latest price**: the newest of two records for that store: our sourcing row there
  (`sourcing_options`: a person's price, a receipt's, or one a price check stored) and the newest verified price a
  lookup saw there (`price_lookups`). The lookup logs a find before it verifies it, so verification is derived the
  way `price_search._verified` decides (`_verify`): a check that agreed, a model that needs no check, a find the
  sourcing row took, or one within `price_check_percent` of a confirmed price. Any other find (a check that
  disagreed, or one that never ran because the run's searches ran out) is shown as unverified: never a price, never
  the cheapest.
- **What we would buy**: the primary and alternate rows in `sourcing_options`, the same rows the fill and the
  planner read.
- **Flags**: *stale* (a primary or alternate checked more than 7 days ago: the fill's own rule,
  `sourcing.STALE_AFTER`), *no price* (no primary with a price), *not found* (the newest lookup at the store we buy
  from, after its price was checked, found no usable price), *over the max* (the price the store's cell shows, for
  a store we buy from, is above that row's max for its pack), *changed* (a store's verified unit price, at the price
  it was stored at, moved more than `price_check_percent` from the verified one before it) and *unverified* (the
  newest find at a store didn't clear verification).
- **Lookup spend**: the cost of every lookup in `price_lookups` this week (Saturday to Friday, Eastern, the ledger's
  weeks) and since the ledger's opening. Benchmark runs (`--no-store`) are left out: they are test spend.
- **Before the first weekly check**: when the first is due and whether it is switched on, from the worker's own
  report when it serves the page (its `/health` `price_check`), else from the settings as this server reads them,
  and `price_check_mode` in the database.

Which drinks: the plan "The plan" shows, cut to our selections (the stored assignment the snapshot read): before
the slot lock the first-fill plan, from the lock the agent's latest live plan, where a slot the plan keeps or
restocks holds what our receipts say it holds. A slot the plan pulls buys nothing and isn't listed. Nothing here
reads or shows another team's selections, products or prices, or the Interloper's (`docs/integrity.md`).

Reads only, inside the dashboard's read-only snapshot. Money is integer cents or exact Fractions, shown as dollars
rounded half up to the cent; never a float. No model calls.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from html import escape
from typing import Any

import psycopg

from vending import ledger

# The stores the purchase API accepts, in the order the lookups try them (`price_search.STORE_ORDER`).
STORES = ("Amazon", "Walmart", "Costco", "BJs")
# A store's unit price that moved more than this since the reading before is flagged (`price_check_percent`'s default).
CHANGE_PERCENT = 5
# A check is matched to the find it checked when it was logged this soon after it.
CHECK_WITHIN = timedelta(hours=1)
# Weekly checks listed.
RUNS_SHOWN = 8
# Newest lookups read per drink and store: enough for the newest reading, the verified one before it and the
# confirmed prices a find may skip its check against.
LOOKUPS_PER_STORE = 20
# The lookup's defaults (`price_search.DEFAULT_CHECK_MODEL`, `CONFIRMED_FOR`), here so the page needn't import it.
DEFAULT_CHECK_MODEL = "claude-sonnet-5-5"
CONFIRMED_FOR = timedelta(days=28)
# Lookup outcomes that saw a usable price on the store's page.
PRICED = ("found",)
# The lookups' outcome codes (`price_search.REASONS`), in plain words.
OUTCOME_WORDS = {
    "found": "found", "not_found": "not found", "not_carried": "not carried", "out_of_stock": "out of stock",
    "check_disagrees": "the second check didn't confirm the price", "over_max": "over the most we pay",
    "over_ceiling": "too dear to price under the ceiling", "implausible_price": "too far from our price to trust",
    "pack_mismatch": "only another pack size", "past_cutoff": "past that store's order cutoff",
    "no_usable_price": "no usable price", "search_cap": "the lookup cap was used up",
    "no_credit": "the model API key had no credit",
}
RUN_OUTCOMES = {"running": "running", "done": "done", "partial": "done, some drinks without a price",
                "failed": "failed", "skipped": "skipped", "halted": "halted (the halt switch)",
                "missed": "missed: no run started in time"}
RUN_TONE = {"running": "info", "done": "ok", "partial": "warn", "failed": "bad", "skipped": "muted",
            "halted": "warn", "missed": "bad"}
MODE_WORDS = {"live": "live", "dry-run": "dry run", "off": "off", "invalid": "off (a setting is wrong)",
              "unknown": "unknown"}


class _Gone(Exception):
    """A table this part needs isn't there yet."""


def _part(conn: psycopg.Connection, read: Callable[[], Any]) -> Any:
    """`read` in its own savepoint; `_Gone` when its table or column doesn't exist yet."""
    try:
        with conn.transaction():
            return read()
    except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn) as error:
        raise _Gone(str(error).splitlines()[0]) from error


# --- Which drinks -------------------------------------------------------------------------------

def plan_drinks(snap: dict) -> dict:
    """The drinks in the plan the page shows, by SKU, each with the selections of ours it is planned for.

    Read from the snapshot's plan panel, which holds our selections only (`dashboard._plan`, `_first_fill`). A weekly
    entry with no product keeps the slot's product, as our receipts name it; a pull buys nothing."""
    plan = snap.get("plan") or {}
    data = plan.get("data") if plan.get("ok") else None
    ours = set(_ours(snap))
    out: dict[str, list[str]] = {}
    source = None
    if isinstance(data, dict) and isinstance(data.get("slots"), list):
        weekly = "weekly_plan" not in snap
        source = "weekly" if weekly else "first fill"
        stock = snap.get("stock") or {}
        held = (stock.get("data") or {}).get("slots", {}) if stock.get("ok") else {}
        for row in data["slots"]:
            if not isinstance(row, dict) or row.get("slot") not in ours:
                continue
            if weekly:
                entry = row.get("now")
                if not isinstance(entry, dict) or entry.get("fill_to") == 0:
                    continue
                sku = entry.get("product") or (held.get(row["slot"]) or {}).get("product")
            else:
                sku = row.get("sku")
            if isinstance(sku, str) and sku:
                out.setdefault(sku, []).append(row["slot"])
    return {"source": source, "drinks": {sku: sorted(slots) for sku, slots in sorted(out.items())}}


def _ours(snap: dict) -> tuple[str, ...]:
    ours = snap.get("ours") or {}
    return tuple(ours["data"]) if ours.get("ok") else ()


# --- Reading ------------------------------------------------------------------------------------

@dataclass
class _Reading:
    """One price a lookup saw at a store, with the second model's check of it, if one ran. `verified` says whether
    the find cleared the lookup's verification (`_verify`), and `why`."""
    at: datetime
    price_cents: int
    units: int
    model: str
    kind: str
    check: dict | None = None
    verified: bool = False
    why: str | None = None

    @property
    def trusted(self) -> bool:
        return self.verified

    @property
    def shown_cents(self) -> int:
        """The price a confirmed find is stored at: the check's."""
        if self.check and self.check["outcome"] == "found" and self.check["price_cents"]:
            return self.check["price_cents"]
        return self.price_cents

    def unit(self) -> Fraction:
        return Fraction(self.shown_cents, self.units)


def _dollars(cents: Fraction | int | None) -> str | None:
    """Exact cents as dollars text, rounded half up to the cent. Never a float."""
    if cents is None:
        return None
    value = Fraction(cents)
    return str((Decimal(value.numerator) / Decimal(value.denominator) / 100).quantize(Decimal("0.01"), ROUND_HALF_UP))


def _source(checked_by: str) -> str:
    from vending import sourcing

    if checked_by == sourcing.RECEIPT_BY:
        return "receipt"
    if checked_by == sourcing.AGENT_BY:
        return "price check"
    return "person"


def read(conn: psycopg.Connection, now: datetime, drinks: dict, worker: dict | None = None) -> dict:
    """Everything the panel shows. `drinks` is `plan_drinks(snapshot)`: SKUs of our plan only, so every sourcing and
    lookup read is filtered to them in the query. `worker` is the worker's `/health` `price_check`, when the worker
    serves the page. The opening is the ledger's real one (`ledger.real_opening`: never the 11 Nov test opening),
    once it is in the past."""
    skus = list(drinks["drinks"])
    missing: list[str] = []

    def part(label: str, read_: Callable[[], Any], default: Any) -> Any:
        # A missing table is named in our words: the database's own text stays out of the page and the JSON.
        try:
            return _part(conn, read_)
        except _Gone:
            missing.append(label)
            return default

    opened = part("ledger", lambda: ledger.real_opening(conn), None)
    opened_at = opened.at if opened is not None and opened.at <= now else None
    rules = part("settings", lambda: _rules(conn), _rules(None))
    runs = part("weekly checks", lambda: _runs(conn, now), None)
    schedule = part("settings", lambda: _schedule(conn, now, worker), None)
    spend = part("lookup log", lambda: _spend(conn, now, opened_at), None)
    names = part("catalog", lambda: _names(conn, skus), {})
    options = part("sourcing", lambda: _options(conn, skus), None)
    lookups = part("lookup log", lambda: _lookups(conn, skus, now), None) if "lookup log" not in missing else None
    rows = [_drink(sku, drinks["drinks"][sku], names.get(sku), (options or {}).get(sku, {}),
                   _verify((lookups or {}).get(sku, {}), (options or {}).get(sku, {}), rules), now, rules)
            for sku in skus] if options is not None else []
    return {"plan": drinks["source"], "runs": runs, "schedule": schedule, "spend": spend,
            "drinks": rows, "sourcing_missing": options is None, "lookups_missing": lookups is None,
            "missing": list(dict.fromkeys(missing)), "change_percent": rules["percent"],
            "check_model": rules["check_model"]}


def _rules(conn: psycopg.Connection | None) -> dict:
    """The lookup's verification settings, as `price_search.Memory.read` reads them: the check model
    (`price_check_model`) and the percent a find may be from a confirmed price and skip the check
    (`price_check_percent`, also the change flag's threshold). Defaults without a connection."""
    model, percent = DEFAULT_CHECK_MODEL, CHANGE_PERCENT
    if conn is not None:
        from vending import config

        value = config.get_value(conn, "price_check_model", None)
        if isinstance(value, str) and value.strip():
            model = value.strip()
        value = config.get_value(conn, "price_check_percent", None)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100:
            percent = value
    return {"check_model": model, "percent": percent, "trusted": _trusted_models(model)}


def _trusted_models(check_model: str) -> Callable[[str], bool]:
    """Whether a model's find needs no check: the check model, or one at least as dear (`price_search._needs_check`,
    priced from `model_prices.json`). An unpriced model is checked, so its find isn't trusted."""
    try:
        from vending.harness import load_prices

        prices = load_prices()
    except Exception:   # noqa: BLE001 - with no price table, only the check model's own finds are trusted
        prices = None
    known: dict[str, bool] = {}

    def trusted(model: str) -> bool:
        if model == check_model:
            return True
        if model not in known:
            try:
                known[model] = not prices.rates(model, 0).input < prices.rates(check_model, 0).input
            except Exception:   # noqa: BLE001 - an unpriced model gets a check
                known[model] = False
        return known[model]
    return trusted


def _verify(cells: dict, options: dict, rules: dict) -> dict:
    """Mark each reading verified or not, the way the lookup decides it stores a find (`price_search._verified`):
    a check that agreed; a model that needs no check; a find the sourcing row took (stored after it at that price and
    pack); or a find within `percent` of a verified price for that store and pack from the last `CONFIRMED_FOR`, so
    it skipped the check. Anything else (a check that disagreed, or a check that never ran because the run's searches
    ran out) is unverified: never a price, never the cheapest."""
    for store, cell in cells.items():
        rows = [o for o in options.values() if o is not None and o.store == store and o.expected_cents]
        done: list[_Reading] = []
        for r in cell["readings"]:
            if r.kind == "check":
                r.verified, r.why = True, "check model"
            elif r.check is not None:
                r.verified = r.check["outcome"] == "found"
                r.why = "confirmed" if r.verified else None
            elif rules["trusted"](r.model):
                r.verified, r.why = True, "model needs no check"
            elif any(o.units_per_pack == r.units and o.expected_cents == r.price_cents and o.checked_at >= r.at
                     for o in rows):
                r.verified, r.why = True, "stored"
            elif any(p.units == r.units and timedelta(0) <= r.at - p.at <= CONFIRMED_FOR
                     and abs(r.price_cents - p.shown_cents) * 100 <= rules["percent"] * p.shown_cents for p in done):
                r.verified, r.why = True, f"within {rules['percent']}% of a confirmed price"
            if r.verified:
                done.append(r)
    return cells


def _runs(conn: psycopg.Connection, now: datetime) -> list[dict]:
    from vending import price_schedule

    found = conn.execute(
        "select due_at, mode, compare, claimed_at, finished_at, outcome, drinks, changed, failures, cost_usd"
        " from price_check_runs where due_at <= %s order by due_at desc limit %s", (now, RUNS_SHOWN)).fetchall()
    out = []
    for due, mode, compare, claimed, finished, outcome, n, changed, failures, cost in found:
        out.append({"due_at": due, "mode": mode, "compare": compare, "finished_at": finished, "outcome": outcome,
                    "drinks": n, "changed": changed, "failures": failures, "cost_usd": cost,
                    "claimed_at": claimed, "models": []})
    if out:
        try:
            with conn.transaction():
                for run in out:
                    run["models"] = [m for (m,) in conn.execute(
                        "select distinct model from price_lookups where by = %s and at >= %s and at <= %s"
                        " order by model", (price_schedule.BY, run["claimed_at"], run["finished_at"] or now))]
        except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
            pass        # the models stay unknown; the lookups' own part says the table is missing
    for run in out:
        del run["claimed_at"]
    return out


def _schedule(conn: psycopg.Connection, now: datetime, worker: dict | None) -> dict:
    """Whether the weekly check is switched on and when the next one is due."""
    from vending import agent_schedule, price_schedule, settings

    problem = None
    if isinstance(worker, dict) and worker.get("mode"):
        worker_mode, from_worker = str(worker["mode"]), True
        problem = worker.get("problem") if isinstance(worker.get("problem"), str) else None
        next_at = _moment(worker.get("next_at"))
    else:
        from_worker, next_at = False, None
        try:
            sched = price_schedule.schedule()
            worker_mode = "off" if sched is None else sched.mode
            problem = sched.problem if sched is not None else None
        except settings.SettingsError as error:
            worker_mode, sched, problem = "invalid", None, str(error)
        try:
            if sched is None:   # off here: when the first would be due, at the settings' times
                entries = tuple(agent_schedule._entry(p.strip()) for p in settings.get(
                    price_schedule.TIMES_SETTING, price_schedule.DEFAULT_TIMES).split(",") if p.strip())
                first = date.fromisoformat(settings.get(price_schedule.FROM_SETTING, price_schedule.DEFAULT_FROM).strip())
                sched = agent_schedule.Schedule("dry-run", entries, first)
            next_at = agent_schedule.next_time(sched, now) if sched.entries else None
        except (ValueError, settings.SettingsError):
            next_at = None
    db_mode = price_schedule.database_mode(conn)
    runs_as = agent_schedule.lower(worker_mode, db_mode) if worker_mode in agent_schedule.RANK else "off"
    return {"worker_mode": worker_mode, "db_mode": db_mode, "runs_as": runs_as, "next_at": next_at,
            "from_worker": from_worker, "problem": problem}


def _moment(text: Any) -> datetime | None:
    if isinstance(text, datetime):
        return text
    try:
        return datetime.fromisoformat(text) if isinstance(text, str) else None
    except ValueError:
        return None


def week_start(now: datetime) -> datetime:
    """Saturday 00:00 Eastern of the ledger's week holding `now` (`ledger.week_of`)."""
    eastern = _eastern_zone()
    if eastern is None:
        local = now.astimezone(timezone.utc)
        return datetime.combine(ledger.week_of(local), time.min, timezone.utc)
    return datetime.combine(ledger.week_of(now.astimezone(eastern)), time.min, eastern)


def _spend(conn: psycopg.Connection, now: datetime, opened_at: datetime | None) -> dict:
    """Lookup spend this week and since the opening; before the opening, this week's is test spend."""
    start = week_start(now)
    week_from = max(start, opened_at) if opened_at is not None else start
    query = ("select coalesce(sum(cost_usd), 0), count(*) from price_lookups"
             " where not no_store and at >= %s and at <= %s")
    week, week_n = conn.execute(query, (week_from, now)).fetchone()
    since, since_n = conn.execute(query, (opened_at, now)).fetchone() if opened_at is not None else (None, None)
    return {"week_start": start, "week_from": week_from, "week": week, "week_lookups": week_n,
            "opened": opened_at is not None, "since_opening": since, "since_lookups": since_n}


def _names(conn: psycopg.Connection, skus: list[str]) -> dict:
    if not skus:
        return {}
    return {sku: {"name": name, "size": size} for sku, name, size in conn.execute(
        "select sku, name, size from catalog_products where sku = any(%s)", (skus,))}


def _options(conn: psycopg.Connection, skus: list[str]) -> dict:
    from vending import sourcing

    out: dict[str, dict] = {}
    for o in sourcing.options_for(conn, skus) if skus else []:
        out.setdefault(o.sku, {})[o.rank] = o
    return out


def _lookups(conn: psycopg.Connection, skus: list[str], now: datetime) -> dict:
    """Each drink's lookups by store, oldest first: the readings (prices seen, each with its check) and the newest
    lookup of any outcome. Only our plan's drinks; benchmark runs left out."""
    if not skus:
        return {}
    found = conn.execute(
        "select sku, store, kind, at, units_per_pack, price_cents, outcome, model from ("
        " select *, row_number() over (partition by sku, store order by at desc, id desc) as n from price_lookups"
        " where sku = any(%s) and not no_store and at <= %s) newest"
        " where n <= %s order by sku, store, at, id", (skus, now, LOOKUPS_PER_STORE)).fetchall()
    out: dict[str, dict] = {}
    for sku, store, kind, at, units, price, outcome, model in found:
        cell = out.setdefault(sku, {}).setdefault(store, {"readings": [], "last": None})
        readings: list[_Reading] = cell["readings"]
        last_find = readings[-1] if readings else None
        if (kind == "check" and last_find is not None and last_find.check is None and last_find.kind != "check"
                and timedelta(0) <= at - last_find.at <= CHECK_WITHIN):
            last_find.check = {"model": model, "outcome": outcome, "price_cents": price, "at": at}
            cell["last"] = {"at": at, "outcome": outcome, "model": model, "kind": kind}
            continue
        if outcome in PRICED and price and units:
            readings.append(_Reading(at, price, units, model, kind))
        cell["last"] = {"at": at, "outcome": outcome, "model": model, "kind": kind}
    return out


# --- One drink ----------------------------------------------------------------------------------

def _option(o: Any, now: datetime) -> dict:
    return {"store": o.store, "units": o.units_per_pack, "price_cents": o.expected_cents,
            "unit": _dollars(Fraction(o.expected_cents, o.units_per_pack)) if o.expected_cents else None,
            "max_cents": o.max_cents, "checked_at": o.checked_at, "source": _source(o.checked_by),
            "by": o.checked_by if _source(o.checked_by) == "person" else None,
            "stale": o.is_stale(now), "stock": o.stock}


def _drink(sku: str, slots: list[str], name: dict | None, options: dict, lookups: dict, now: datetime,
           rules: dict) -> dict:
    primary, alternate = options.get("primary"), options.get("alternate")
    stores, flags = [], []
    units: dict[str, Fraction] = {}
    for store in STORES:
        cell = lookups.get(store, {"readings": [], "last": None})
        readings: list[_Reading] = cell["readings"]
        trusted = [r for r in readings if r.trusted]
        reading = trusted[-1] if trusted else None
        rows = [o for o in (primary, alternate) if o is not None and o.store == store and o.expected_cents]
        row = max(rows, key=lambda o: o.checked_at, default=None)
        price = None
        if row is not None and (reading is None or row.checked_at >= reading.at):
            price = {"price_cents": row.expected_cents, "units": row.units_per_pack, "at": row.checked_at,
                     "source": _source(row.checked_by), "model": None, "confirmed_by": None}
        elif reading is not None:
            price = {"price_cents": reading.shown_cents, "units": reading.units, "at": reading.at,
                     "source": "price check", "model": reading.model,
                     "confirmed_by": reading.check["model"] if reading.check and reading.verified else None}
        if price is not None:
            unit = Fraction(price["price_cents"], price["units"])
            price["unit"] = _dollars(unit)
            price["stale"] = now - price["at"] > _stale_after()
            if not price["stale"]:
                units[store] = unit
        last = cell["last"]
        tried = None
        if last is not None and last["outcome"] not in PRICED and (price is None or last["at"] > price["at"]):
            tried = {"at": last["at"], "outcome": last["outcome"], "words": _words(last["outcome"])}
        # The newest find, when it didn't clear verification and is newer than the price shown: shown as unverified.
        unverified = None
        if readings and not readings[-1].verified and (price is None or readings[-1].at > price["at"]):
            r = readings[-1]
            unverified = {"at": r.at, "price_cents": r.price_cents, "units": r.units,
                          "unit": _dollars(Fraction(r.price_cents, r.units)), "model": r.model,
                          "check": None if r.check is None else {"model": r.check["model"],
                                                                 "words": _words(r.check["outcome"])}}
            flags.append({"code": "unverified", "store": store, **unverified})
        stores.append({"store": store, "price": price, "last_try": tried, "unverified": unverified})
        flags += _changed(store, readings, rules["percent"])
    cheapest = min(units, key=lambda s: (units[s], STORES.index(s))) if units else None
    # The flags about what we would buy.
    if primary is None or primary.expected_cents is None:
        flags.insert(0, {"code": "no_price"})
    for o in (primary, alternate):
        if o is None:
            continue
        if o.is_stale(now):
            flags.append({"code": "stale", "rank": o.rank, "store": o.store, "checked_at": o.checked_at,
                          "days": (now - o.checked_at).days})
        cell = lookups.get(o.store, {"readings": [], "last": None})
        last = cell["last"]
        # A check that didn't confirm a find is the changed flag's, not a miss.
        if (o.rank == "primary" and last is not None and last["at"] > o.checked_at and last["outcome"] not in PRICED
                and last["kind"] != "check"):
            flags.append({"code": "not_found", "rank": o.rank, "at": last["at"],
                          "stores": [{"store": o.store, "words": _words(last["outcome"])}]})
        # The same price the store's cell shows, against this row's max for its pack.
        shown = next(c["price"] for c in stores if c["store"] == o.store)
        if shown is not None and Fraction(shown["price_cents"], shown["units"]) * o.units_per_pack > o.max_cents:
            flags.append({"code": "over_max", "rank": o.rank, "store": o.store, "at": shown["at"],
                          "price_cents": shown["price_cents"], "units": shown["units"], "unit": shown["unit"],
                          "max_cents": o.max_cents, "pack": o.units_per_pack})
    missed = [c for c in stores if c["last_try"] is not None and c["price"] is None]
    if primary is None and missed:
        flags.append({"code": "not_found", "rank": None, "at": max(c["last_try"]["at"] for c in missed),
                      "stores": [{"store": c["store"], "words": c["last_try"]["words"]} for c in missed]})
    return {"sku": sku, "name": (name or {}).get("name"), "size": (name or {}).get("size"), "slots": slots,
            "stores": stores, "cheapest": cheapest,
            "primary": _option(primary, now) if primary is not None else None,
            "alternate": _option(alternate, now) if alternate is not None else None, "flags": flags}


def _stale_after() -> timedelta:
    from vending import sourcing

    return sourcing.STALE_AFTER


def _words(outcome: str) -> str:
    return OUTCOME_WORDS.get(outcome, "no usable price")


def _changed(store: str, readings: list[_Reading], percent: int) -> list[dict]:
    """A flag when the newest verified price at `store` is more than `percent` from the verified one before it, both
    at the price they were stored at (a confirmed find at the check's). An unverified find is its own flag."""
    verified = [r for r in readings if r.verified]
    if len(verified) < 2:
        return []
    newest, before = verified[-1], verified[-2]
    old, new = before.unit(), newest.unit()
    if old <= 0 or abs(new - old) * 100 <= percent * old:
        return []
    pct = (new - old) * 100 / old
    return [{"code": "changed", "store": store, "at": newest.at, "before_at": before.at,
             "from_unit": _dollars(old), "to_unit": _dollars(new),
             "percent": f"{(Decimal(pct.numerator) / Decimal(pct.denominator)).quantize(Decimal('0.1'), ROUND_HALF_UP):+}",
             "model": newest.model, "verified_by": newest.check["model"] if newest.check else None,
             "why": newest.why}]


# --- Page ---------------------------------------------------------------------------------------

def _eastern_zone():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except Exception:   # Windows without the tzdata package
        return None


def _e(value: Any) -> str:
    return "" if value is None else escape(str(value))


def et_day(value: datetime | None) -> str:
    """A day in Eastern, the machine's zone: "Tue 8 Dec"."""
    if value is None:
        return "unknown"
    zone = _eastern_zone()
    local = value.astimezone(zone) if zone else value.astimezone(timezone.utc)
    return f"{local:%a} {local.day} {local:%b}" + ("" if zone else " (UTC)")


def et_moment(value: datetime | None) -> str:
    """A day and time in Eastern: "Tue 8 Dec, 12:00 PM ET"."""
    if value is None:
        return "unknown"
    zone = _eastern_zone()
    if zone is None:
        return f"{value.astimezone(timezone.utc):%a %d %b, %H:%M} UTC"
    local = value.astimezone(zone)
    return f"{local:%a} {local.day} {local:%b}, {local.hour % 12 or 12}:{local:%M %p} ET"


def _usd(cents: int | Fraction | None) -> str:
    return '<span class="dim">unknown</span>' if cents is None else f"${_dollars(cents)}"


def _money(value: Any) -> str:
    """A dollar amount from the database (a Decimal), to the cent; unknown when there is none."""
    if value is None:
        return '<span class="dim">unknown</span>'
    return f"${Decimal(value).quantize(Decimal('0.01'), ROUND_HALF_UP)}"


def _pill(text: str, tone: str) -> str:
    return f'<span class="pill {tone}">{_e(text)}</span>'


def _facts(rows: list[tuple[str, str]]) -> str:
    return '<dl class="pc-facts">' + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows) + "</dl>"


def _schedule_rows(s: dict | None, has_runs: bool) -> list[tuple[str, str]]:
    if s is None:
        return [("Weekly check", '<span class="dim">unknown: table missing</span>')]
    runs_as = s["runs_as"]
    state = _pill(MODE_WORDS.get(runs_as, runs_as), "ok" if runs_as == "live" else "info" if runs_as == "dry-run"
                  else "muted")
    source = "worker" if s["from_worker"] else "this server's settings"
    rows = [("Weekly check", f'{state} <span class="sub">PRICE_CHECK_MODE {_e(s["worker_mode"])} ({source}) &middot; '
                             f'price_check_mode {_e(s["db_mode"])} (database)</span>')]
    label = "Next due" if has_runs else "First due"
    rows.append((label, _e(et_moment(s["next_at"])) if s["next_at"] else '<span class="dim">unknown</span>'))
    if not has_runs:
        rows.append(("Runs so far", "none"))
    if s.get("problem"):
        rows.append(("Problem", f'<span class="bad-text">{_e(s["problem"])}</span>'))
    return rows


def _spend_rows(sp: dict | None) -> list[tuple[str, str]]:
    if sp is None:
        return [("Lookup spend", '<span class="dim">unknown: table missing</span>')]
    week = (f'{_money(sp["week"])} <span class="sub">{sp["week_lookups"]} lookups from {_e(et_day(sp["week_from"]))}'
            + ("" if sp["opened"] else " &middot; test spend: before the opening") + "</span>")
    since = (f'{_money(sp["since_opening"])} <span class="sub">{sp["since_lookups"]} lookups</span>' if sp["opened"]
             else '<span class="dim">not opened</span>')
    return [("Lookup spend, this week", week), ("Lookup spend, since opening", since)]


def _runs_html(runs: list[dict] | None) -> str:
    if not runs:
        return ""
    rows = []
    for r in runs:
        how = ("off" if r["mode"] == "off" else
               ("compare all stores" if r["compare"] else "re-check primary stores")
               + (" &middot; dry run" if r["mode"] == "dry-run" else ""))
        model = _e(", ".join(r["models"])) if r["models"] else "&mdash;"
        nums = (f'<td class="n">{r["drinks"]}</td><td class="n">{r["changed"]}</td><td class="n">{r["failures"]}</td>'
                if r["drinks"] is not None else '<td class="n dim">&mdash;</td>' * 3)
        rows.append(f'<tr><td>{_e(et_moment(r["due_at"]))}</td><td>{how}</td><td>{model}</td>{nums}'
                    f'<td class="n">{_money(r["cost_usd"])}</td>'
                    f'<td>{_pill(RUN_OUTCOMES.get(r["outcome"], r["outcome"]), RUN_TONE.get(r["outcome"], "muted"))}</td></tr>')
    return ('<h3>Weekly checks</h3><div class="pc-scroll"><table class="pc-runs"><thead><tr><th>Due</th><th>Kind</th>'
            '<th>Model</th><th class="n">Checked</th><th class="n">Changed</th><th class="n">Not found</th>'
            f'<th class="n">Cost</th><th>Outcome</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def _store_cell(cell: dict, cheapest: str | None) -> str:
    p, tried = cell["price"], cell["last_try"]
    if p is None:
        body = '<span class="dim">&mdash;</span>'
    else:
        if p["source"] == "price check" and p["model"]:
            source = _e(p["model"])
        else:
            source = _e(p["source"])
        if p.get("confirmed_by"):
            source += f' &#10003; {_e(p["confirmed_by"])}'
        tags = ((' <span class="pill ok">cheapest</span>' if cell["store"] == cheapest else "")
                + (' <span class="pill warn">stale</span>' if p["stale"] else ""))
        body = (f'<b>${_e(p["unit"])}</b>/unit{tags}<span class="sub pc-detail">{p["units"]} for '
                f'{_usd(p["price_cents"])} &middot; {_e(et_day(p["at"]))} &middot; {source}</span>')
    if tried is not None:
        body += f'<span class="sub pc-detail">{_e(et_day(tried["at"]))}: {_e(tried["words"])}</span>'
    if cell.get("unverified"):
        u = cell["unverified"]
        body += (f'<span class="sub pc-detail"><span class="warn-text">unverified</span> ${_e(u["unit"])}/unit '
                 f'&middot; {_e(et_day(u["at"]))}</span>')
    return f'<td class="pc-store"><span class="k">{_e(cell["store"])}</span>{body}</td>'


def _buy(o: dict | None, label: str) -> str:
    if o is None:
        return f'<div><span class="k2">{label}</span> <span class="dim">&mdash;</span></div>'
    price = (f'{o["units"]} for {_usd(o["price_cents"])}' if o["price_cents"] else
             f'{o["units"]}, <span class="dim">no price</span>')
    who = o["source"] if o["source"] != "person" else o["by"]
    return (f'<div><span class="k2">{label}</span> {_e(o["store"])} &middot; {price} &middot; max {_usd(o["max_cents"])}'
            f'<span class="sub pc-detail">{_e(et_day(o["checked_at"]))} &middot; {_e(who)}'
            + (' &middot; <span class="warn-text">stale</span>' if o["stale"] else "") + "</span></div>")


def _flag(f: dict) -> tuple[str, str]:
    """(tone, label) for one flag: a short tag and its figures."""
    code = f["code"]
    if code == "no_price":
        return "bad", "<b>No price</b> no primary with a price"
    if code == "stale":
        return "warn", (f'<b>Stale</b> {_e(f["rank"])} {_e(f["store"])} &middot; {f["days"]} d '
                        f'({_e(et_day(f["checked_at"]))}) &gt; 7 d')
    if code == "not_found":
        where = "; ".join(f'{_e(x["store"])}: {_e(x["words"])}' for x in f["stores"])
        return "warn", f'<b>Not found</b> {where} &middot; {_e(et_day(f["at"]))}'
    if code == "over_max":
        return "bad", (f'<b>Over max</b> {_e(f["store"])} {_usd(f["price_cents"])}/{f["units"]} '
                       f'(${_e(f["unit"])}/unit) &gt; max {_usd(f["max_cents"])}/{f["pack"]} &middot; '
                       f'{_e(et_day(f["at"]))}')
    if code == "changed":
        verdict = (f'confirmed by {_e(f["verified_by"])}' if f["verified_by"] else _e(f["why"] or "verified"))
        return "info", (f'<b>Changed</b> {_e(f["store"])} {_e(f["percent"])}% &middot; ${_e(f["from_unit"])} &rarr; '
                        f'${_e(f["to_unit"])}/unit &middot; {_e(et_day(f["before_at"]))} &rarr; {_e(et_day(f["at"]))} '
                        f'&middot; {verdict}')
    if code == "unverified":
        check = f["check"]
        verdict = (f'{_e(check["model"])}: {_e(check["words"])}' if check else "no check recorded")
        return "warn", (f'<b>Unverified</b> {_e(f["store"])} {_usd(f["price_cents"])}/{f["units"]} '
                        f'(${_e(f["unit"])}/unit) &middot; {_e(f["model"])} &middot; {_e(et_day(f["at"]))} &middot; '
                        f'{verdict} &middot; not used')
    return "muted", _e(code)


def html(panel: dict) -> str:
    """The panel's inner HTML, from a dashboard `_panel` dict, ok or not."""
    if not panel["ok"]:
        return f'<p class="empty">Not available: {_e(panel["why"])}</p>'
    d = panel["data"]
    facts = _schedule_rows(d["schedule"], bool(d["runs"])) + _spend_rows(d["spend"])
    if d["missing"]:
        facts.append(("Tables missing", f'{_e(", ".join(d["missing"]))} &middot; <code>python -m vending.db migrate</code>'))
    out = [_facts(facts), _runs_html(d["runs"])]
    plan = {"weekly": "latest live plan", "first fill": "first-fill plan"}.get(d["plan"], "no plan")
    out.append(f'<h3>Drinks in our plan <span class="hint">{plan} &middot; {len(d["drinks"])} drinks &middot; '
               f'stale &gt; 7 d &middot; changed &gt; {d["change_percent"]}%</span></h3>')
    if d["sourcing_missing"]:
        out.append('<p class="empty">Sourcing table missing</p>')
    elif not d["drinks"]:
        out.append('<p class="empty">No drinks</p>')
    else:
        rows = []
        for drink in d["drinks"]:
            name = drink["name"] or drink["sku"]
            flags = [_flag(f) for f in drink["flags"]]
            flag_html = ("".join(f'<li class="{tone}">{text}</li>' for tone, text in flags)
                         or '<li class="dim">&mdash;</li>')
            stores = "".join(_store_cell(c, drink["cheapest"]) for c in drink["stores"])
            rows.append(f'<tr><td class="pc-drink"><b>{_e(name)}</b><span class="sub pc-detail"><code>'
                        f'{_e(drink["sku"])}</code></span><span class="sub pc-detail">{_e(", ".join(drink["slots"]))}'
                        f'</span></td>{stores}<td class="pc-buy">{_buy(drink["primary"], "Primary")}'
                        f'{_buy(drink["alternate"], "Alternate")}</td><td class="pc-flags"><ul>{flag_html}</ul></td></tr>')
        head = "".join(f"<th>{s}</th>" for s in STORES)
        out.append(f'<div class="pc-scroll"><table class="pc-drinks"><thead><tr><th>Drink</th>{head}<th>Buy</th>'
                   f'<th>Flags</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')
    return "".join(out)


STYLE = """
/* Price checks (`vending.price_check_panel`). */
dl.pc-facts { display:grid; grid-template-columns:max-content minmax(0, 1fr); gap:3px 14px; margin:0 0 10px; font-size:13px }
dl.pc-facts dt { color:var(--muted); font-weight:600 } dl.pc-facts dd { margin:0; overflow-wrap:anywhere }
.pc-scroll { overflow-x:auto; margin:4px 0 8px }
table.pc-runs, table.pc-drinks { border-collapse:collapse; width:100%; font-size:13px }
.pc-runs th, .pc-runs td, .pc-drinks th, .pc-drinks td { padding:6px; border-bottom:1px solid var(--line);
  text-align:left; vertical-align:top }
.pc-runs th, .pc-drinks th { font-size:11px; font-weight:600; letter-spacing:.04em; text-transform:uppercase;
  color:var(--muted) }
.pc-runs td.n, .pc-runs th.n { text-align:right; white-space:nowrap }
.pc-detail { display:block }
.pc-drinks .k { display:none } .pc-drinks .k2 { display:inline-block; min-width:62px; color:var(--muted); font-weight:600 }
.pc-drink { min-width:150px } .pc-store { min-width:110px } .pc-buy { min-width:200px } .pc-flags { min-width:220px }
.pc-flags ul { margin:0; padding-left:16px }
.pc-flags li.bad { color:var(--bad) } .pc-flags li.warn { color:var(--warn) }
.warn-text { color:var(--warn) }
@media (max-width:820px) {
  dl.pc-facts { grid-template-columns:minmax(0, 1fr) } dl.pc-facts dd { margin-bottom:4px }
  .pc-drinks thead { display:none }
  .pc-drinks, .pc-drinks tbody, .pc-drinks tr, .pc-drinks td { display:block; width:auto; min-width:0 }
  .pc-drinks tr { border-bottom:1px solid var(--line); padding:6px 0 }
  .pc-drinks td { border:0; padding:3px 0 }
  .pc-drinks .k { display:inline-block; min-width:64px; font-weight:600; color:var(--muted) }
}
"""
