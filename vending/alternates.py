"""A backup store for every line of the picked plan, from the latest four-store prices (Kevin, 25 Nov).

    python -m vending.alternates                         # dry run: what each line would get; writes nothing
    python -m vending.alternates --write --by NAME       # store them
    python -m vending.alternates --max-age 24 --visit 2026-11-30

The worker buys a line's primary if the store has it at or under its max per pack, else the alternate, else
nothing. So a line with no alternate stays empty when its primary is sold out. This step gives each line of the
picked plan without one an alternate: the same drink at another store, from the price lookups the four-store
compare logged (`price_lookups`, `python -m vending.price_search --plan picked --compare`). It never searches.

A price counts when:

- it was looked up in the last `--max-age` hours (default 24), at a store other than the primary's, whose cutoff
  for the stocking (`--visit`, default the day-one stocking) hasn't passed;
- it is the pick's pack size, so the fill buys the same packs and units (`fill.deviations`);
- a trusted model read it on the store's own product page, with every check a lookup makes passed: a find by
  the check model or a dearer one (`price_search.needs_check`), or the check model's own reading when it
  disagreed with a cheaper model's find. A cheaper model's unchecked find never counts. Not out of stock;
- its price per unit is within half and twice the primary's (`price_search.PLAUSIBLE`);
- its price is at most the pick's most-we-pay per pack. The alternate's max is the agent's (`price_search.
  agent_max`, the price plus 25%) held to that, so the fill's `pick_deviation` check passes.

The cheapest per unit wins (then in stock, then Amazon, Walmart, Costco, BJs). An existing alternate whose max
alone is over the pick's, with a price under it, gets its max lowered to the pick's (C4 on 25 Nov). An alternate
that differs from the pick in packs or units is replaced when a price fits, else left and listed. While the fill is
over the cap or under the floor, a stored alternate of another pack size whose removal lowers the fill's most-we-pay is
removed first, in SKU order, until it isn't: its max can't come down to the pick's, and it would keep every line's
trial over the cap (plan #9 on 27 Nov: a 24-pack alternate for an 8-pack line put the fill at $419.60).

A line still without one gets a substitute: another drink of the plan (Kevin, 25 Nov: "double stock two slots
if needed"), at its own fresh primary row, stored in `fill_substitutes` for the picked plan. The fill sends it as
the line's alternate, with its own `sku` (`vending.fill.with_substitutes`). Its row must be checked within
`--max-age`, at a store whose cutoff hasn't passed. Its max per pack is held so its packs cost at most the pick's
most-we-pay for the line. The cheapest per unit is tried first. The planner refuses one on a pooled line, one that
isn't active in the catalog and one that doesn't fit the column, so those lines are listed without one. A
same-drink alternate always wins: the fill uses a substitute only for a line with none.

Each new alternate is tried on the fill's own planner first (`planning.plan_gathered`, pure): it is kept only if
the fill gains no refusal and no deviation, and its most-we-pay (each line at its dearer choice) stays within the
$400 day-one cap (`plan_rounds.FILL_CAP_CENTS`) and leaves the spendable floor out of the $600 opening. Lines are
tried in SKU order, each on top of the ones before. A line that gets none is listed with each store's reason.

`--write` stores them with `sourcing.set_option` inside the write gate, after checking the rows didn't change
since they were read (`changed_meanwhile`). `checked_by` is `agent-search` (the lookup found the price), dated
when it was looked up; the note names the person. `action_log` keeps what each replaced.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from fractions import Fraction

import psycopg

from vending import db, fill, harness, plan_rounds, planner, planning, price_search, settings, slots, sourcing
from vending.bridge import Bridge
from vending.pricing import Refusal
from vending.start import EASTERN
from vending.write_gate import Refused, run_write

TOOL = "alternates.set"
FILL_CAP_CENTS = plan_rounds.FILL_CAP_CENTS
START_CENTS = plan_rounds.START_CENTS
DEFAULT_MAX_AGE_HOURS = 24
MOST_MAX_AGE_HOURS = 144            # under the 7 days after which the fill refuses a price as stale
CHANGED = "changed_meanwhile"
OVER_LIMIT = "over_limit"


class NoPlan(RuntimeError):
    """Nothing to give backups to: no pick, a pick for another assignment, or a planner that can't run."""


@dataclass(frozen=True)
class Price:
    """One usable price from the lookup log."""
    store: str
    units: int
    cents: int
    url: str
    stock: str
    at: datetime

    @property
    def per_unit(self) -> Fraction:
        return Fraction(self.cents, self.units)


@dataclass(frozen=True)
class Line:
    sku: str
    primary: sourcing.Option | None
    before: sourcing.Option | None      # the alternate as stored
    after: sourcing.Option | None       # the alternate this step would store; `before` when it changes nothing
    action: str                         # kept, set, replaced, lowered, substitute, removed, none
    why: str                            # what changed, or why there is none
    drop_sub: bool = False              # its stored substitute is removed (`fill_substitutes`)
    drop_alt: bool = False              # its stored alternate is removed (`before`), as it kept the fill over the cap

    @property
    def changed(self) -> bool:
        return self.action in ("set", "replaced", "lowered", "substitute", "removed") or self.drop_sub or self.drop_alt

    @property
    def substitutes(self) -> bool:
        """The backup is another drink (`after.sku` differs), stored in `fill_substitutes`."""
        return self.after is not None and self.after.sku != self.sku and self.action != "removed"


@dataclass(frozen=True)
class Trial:
    plan: planner.Plan
    deviations: frozenset[str]
    refusals: frozenset[tuple[str, str | None]]
    max_cents: int


@dataclass(frozen=True)
class Result:
    picked: fill.Picked
    since: datetime
    visit: date
    lines: tuple[Line, ...]
    before: Trial
    after: Trial
    floor_cents: int
    read: dict                          # (sku, rank) -> the row as read, for the write's check
    subs_read: dict = field(default_factory=dict)   # the picked plan's substitutes as read (`fill.substitutes`)
    pooled: tuple[str, ...] = ()        # the plan's drinks in more than one slot

    def with_backup(self, when: str) -> int:
        """Lines whose alternate the fill takes without a `pick_deviation`: now (`before`) or `after`."""
        trial = self.before if when == "now" else self.after
        count = 0
        for ln in self.lines:
            alt = ln.before if when == "now" else ln.after
            if alt is not None and not _alternate_deviates(trial, ln.sku) and ln.action != "none":
                count += 1
        return count


def usable(row: tuple, store: str, trusted: Callable[[str], bool]) -> bool:
    """Whether a `price_lookups` row is a reading every lookup check passed, by a trusted model: a find, or the check
    model's own reading when it disagreed with a cheaper model's find (its fields are kept only as far as its checks
    passed, so all of them set, in stock or unknown, a pack it was asked for and a plausible price means it passed
    them all)."""
    kind, outcome, units, cents, url, stock, model, asked_min, asked_max = row[2:11]
    if not (outcome == "found" or (kind == "check" and outcome == "check_disagrees")):
        return False
    if None in (units, cents, url, stock) or stock not in ("in_stock", "unknown"):
        return False
    if not asked_min <= units <= asked_max:
        return False
    if cents > planner.MAX_PRICE_PER_PACK_CENTS or cents > units * price_search.MOST_PER_UNIT_CENTS:
        return False
    return (trusted(model) and price_search.on_store(url, store) and price_search.product_page(url, store))


def _same_listing_check(row: tuple, mine: list[tuple]) -> bool:
    """A `check_disagrees` row counts only as a price disagreement: the check read the same listing and pack as the
    find it checked (the newest earlier search or listing lookup at that store). A check that read another listing
    (another ASIN, say) is not that product's price."""
    if row[2] != "check":
        return True
    checked = next((r for r in mine if r[0] < row[0] and r[2] in ("search", "listing")), None)
    return (checked is not None and checked[4] == row[4]
            and price_search.same_listing(row[1], checked[6], row[6]))


def prices(conn: psycopg.Connection, sku: str, primary: sourcing.Option, units: int, now: datetime,
           since: datetime, visit: date, trusted: Callable[[str], bool]) -> tuple[list[Price], dict[str, str]]:
    """The newest usable price of `sku` in `units`-packs at each store but the primary's, looked up after `since`;
    and for each store without one, why. A later out-of-stock reading at that store supersedes it."""
    rows = conn.execute(
        "select id, store, kind, outcome, units_per_pack, price_cents, url, stock, model, asked_min, asked_max, at"
        " from price_lookups where sku = %s and at > %s and at <= %s order by at desc, id desc",
        (sku, since, now)).fetchall()
    found: list[Price] = []
    why: dict[str, str] = {}
    hours = f"{(now - since).total_seconds() / 3600:g}"
    for store in price_search.STORE_ORDER:
        if store == primary.store:
            continue
        if price_search.past_cutoff(store, visit, now):
            why[store] = f"past its cutoff for the {visit:%a %d %b} stocking"
            continue
        mine = [r for r in rows if r[1] == store]
        good = [r for r in mine if usable(r, store, trusted) and _same_listing_check(r, mine)]
        fit = [r for r in good if r[4] == units]
        # P1 (Sol): newer evidence wins. An out-of-stock reading at this store after the newest usable price, for
        # this pack or with no pack read, means the store may not have it now.
        gone = fit and any(r[0] != fit[0][0] and (r[11], r[0]) > (fit[0][11], fit[0][0])
                           and (r[3] == "out_of_stock" or r[7] == "out_of_stock") and r[4] in (None, units)
                           for r in mine)
        if gone:
            why[store] = "out of stock in a later lookup"
        elif fit:
            r = fit[0]
            price = Price(store, r[4], r[5], sourcing.plain_link(store, r[6]), r[7], r[11])
            if primary.expected_cents and not (price_search.PLAUSIBLE[0] <= price.per_unit / Fraction(
                    primary.expected_cents, primary.units_per_pack) <= price_search.PLAUSIBLE[1]):
                why[store] = f"{_usd(price.cents)} for {units} is implausible against the primary"
                continue
            found.append(price)
        elif not mine:
            why[store] = f"no lookup in the last {hours} hours"
        elif good:
            why[store] = f"only other pack sizes ({', '.join(str(n) for n in sorted({r[4] for r in good}))})"
        elif any(r[3] == "out_of_stock" for r in mine):
            why[store] = "out of stock"
        elif any(r[2] == "check" and r[3] == "check_disagrees" for r in mine):
            why[store] = "a cheaper model's find the check couldn't confirm"
        elif any(r[3] == "found" for r in mine):
            why[store] = "only a cheaper model's unchecked find"
        elif all(r[3] == "not_found" for r in mine):
            why[store] = "not carried"
        else:
            why[store] = "no usable price (" + ", ".join(sorted({r[3] for r in mine})) + ")"
    found.sort(key=lambda p: (p.per_unit, p.stock != "in_stock", price_search.STORE_ORDER.index(p.store)))
    return found, why


def _ordered(rows: dict) -> tuple[sourcing.Option, ...]:
    """The rows as `sourcing.listed_options` orders them: by SKU, primary first."""
    return tuple(sorted(rows.values(), key=lambda o: (o.sku, sourcing.RANKS.index(o.rank))))


def trial(gathered: planning.Gathered, checked: plan_rounds.Round, rows: dict,
          subs: dict | None = None) -> Trial:
    """The fill planned on `rows` instead of the table, with `subs` as the fill applies them
    (`fill.with_substitutes`): its deviations, refusals and most-we-pay. Pure."""
    table = _ordered(rows)
    given = replace(gathered, sourcing_rows=table, current=replace(gathered.current,
                                                                    sourcing_table=sourcing.to_planner(list(table))))
    given, swapped = fill.with_substitutes(given, checked, subs or {})
    planned = planning.plan_gathered(given, fill.desired_state(checked), pool=True)
    refusals = frozenset(_key(r) for r in (*swapped, *planned.refusals, *fill.input_refusals(checked, given)))
    cost = planned.cost
    most = 0 if cost is None else cost.max_cents - cost.visit_fee_cents
    return Trial(planned, frozenset(fill.deviations(planned, checked)), refusals, most)


# Slot-less refusals whose message carries the plan's cost: keyed by code alone, or every trial would look new.
COST_CODES = ("insufficient_balance", "below_spendable_floor")


def _key(r: Refusal) -> tuple[str, str | None, str | None]:
    """A refusal as `trial` compares them: by code and slot, and by message when it names no slot (two stores'
    cutoffs are two refusals)."""
    return (r.code, r.slot, None if r.slot is not None or r.code in COST_CODES else r.message)


def _alternate_deviates(t: Trial, sku: str) -> bool:
    return any(d.startswith((f"{sku} (alternate", f"{sku} (substitute")) for d in t.deviations)


def _worse(now: Trial, then: Trial, floor_cents: int) -> str | None:
    """Why `then` is worse for the fill than `now`, or None."""
    if added := sorted(then.refusals - now.refusals, key=lambda r: (r[0], r[1] or "", r[2] or "")):
        return f"the fill would refuse it ({', '.join(sorted({key[0] for key in added}))})"
    if added := sorted(then.deviations - now.deviations):
        return f"it differs from the pick ({'; '.join(added)})"
    lowers = then.max_cents < now.max_cents
    if then.max_cents > FILL_CAP_CENTS and not lowers:
        return (f"the fill's most-we-pay would be {_usd(then.max_cents)}, over the {_usd(FILL_CAP_CENTS)} cap")
    if START_CENTS - then.max_cents < floor_cents and not lowers:
        return (f"the fill's most-we-pay would be {_usd(then.max_cents)}, leaving under the {_usd(floor_cents)} "
                f"floor of the {_usd(START_CENTS)} opening")
    return None


def backups(conn: psycopg.Connection, bridge: Bridge, now: datetime | None = None,
            max_age: timedelta = timedelta(hours=DEFAULT_MAX_AGE_HOURS),
            visit: date = fill.DAY_ONE_VISIT) -> Result:
    """What each line of the picked plan would get. Reads only."""
    ours = slots.our_slots(conn)
    pick = fill.picked(conn, ours)
    if pick.round is None:
        raise NoPlan(f"plan #{pick.id} (round {pick.label}) was loaded for another slot assignment than ours now; "
                     "load and pick a plan for the current assignment first")
    try:
        _, gathered = planning.plan(conn, fill.desired_state(pick.round), bridge, now, pool=True,
                                    count_takings=False)
    except planning.NotReady as blocked:
        raise NoPlan(f"the planner can't run: {blocked}") from None
    now = gathered.now
    since = now - max_age
    rows = {(o.sku, o.rank): o for o in gathered.sourcing_rows}
    # The rows the trials use, as the planner read them: the write checks the listed ones haven't changed.
    read = dict(rows)
    table = harness.load_prices()
    check = price_search.Memory.read(conn).check_model
    trusted = lambda model: not price_search.needs_check(table, model, check)  # noqa: E731
    floor_cents = gathered.config.spendable_floor_cents
    subs = fill.substitutes(conn, pick.id)
    subs_read = dict(subs)
    first = current = trial(gathered, pick.round, rows, subs)
    in_slots: dict[str, int] = {}
    for s in pick.round.slots.values():
        if s.sku:
            in_slots[s.sku] = in_slots.get(s.sku, 0) + 1
    pooled = tuple(sorted(sku for sku, n in in_slots.items() if n > 1))
    # While the fill is over the cap or the floor, even with every max the loop below can lower (C4) lowered: drop each
    # stored alternate of another pack size whose removal lowers the most-we-pay. Its max can't be lowered to the
    # pick's, and every trial below would be judged over the cap. A line with a stored substitute is left alone: the
    # removal would bring the substitute in (Sol, #265).
    dropped: dict[str, sourcing.Option] = {}
    lowered = dict(rows)
    for p in pick.round.purchases:
        alt = rows.get((p.sku, "alternate"))
        fit = min(p.most_cents // max(p.packs, 1), planner.MAX_PRICE_PER_PACK_CENTS)
        if (alt is not None and alt.expected_cents is not None and alt.units_per_pack == p.units_per_pack
                and alt.expected_cents <= fit < alt.max_cents):
            lowered[(p.sku, "alternate")] = replace(alt, max_cents=fit)
    best = trial(gathered, pick.round, lowered, subs) if lowered != rows else current
    for p in sorted(pick.round.purchases, key=lambda p: p.sku):
        if not _over(best, floor_cents):
            break
        alt = rows.get((p.sku, "alternate"))
        if (alt is None or p.sku in subs or alt.units_per_pack == p.units_per_pack
                or not _alternate_deviates(current, p.sku)):
            continue
        attempt = {k: v for k, v in lowered.items() if k != (p.sku, "alternate")}
        tried = trial(gathered, pick.round, attempt, subs)
        if tried.max_cents < best.max_cents and not tried.refusals - best.refusals:
            lowered, best = attempt, tried
            dropped[p.sku] = alt
    if dropped:
        rows = {k: v for k, v in rows.items() if not (k[1] == "alternate" and k[0] in dropped)}
        current = trial(gathered, pick.round, rows, subs)
    lines = []
    for p in sorted(pick.round.purchases, key=lambda p: p.sku):
        primary, alt = rows.get((p.sku, "primary")), rows.get((p.sku, "alternate"))
        if primary is None or primary.expected_cents is None:
            lines.append(Line(p.sku, primary, alt, alt, "none", "no checked primary (no row, no price, or the catalog delisted it), so no alternate can be set"))
            continue
        deviates = _alternate_deviates(current, p.sku)
        if alt is not None and alt.expected_cents is not None and not deviates:
            lines.append(Line(p.sku, primary, alt, alt, "kept", "already has one the fill takes"))
            continue
        had = subs.get(p.sku) if alt is None else None
        stored = _substitute_row(rows, had)
        with_sub = current
        if had is not None:
            # Judged without it first: a same-drink backup wins over a stored substitute.
            subs = {k: v for k, v in subs.items() if k != p.sku}
            current = trial(gathered, pick.round, rows, subs)
        fit = min(p.most_cents // max(p.packs, 1), planner.MAX_PRICE_PER_PACK_CENTS)
        tries: list[tuple[str, sourcing.Option, str]] = []
        if (alt is not None and deviates and alt.expected_cents is not None and alt.units_per_pack == p.units_per_pack
                and alt.expected_cents <= fit < alt.max_cents):
            tries.append(("lowered", replace(alt, max_cents=fit),
                          f"{alt.store}: max {_usd(alt.max_cents)} -> {_usd(fit)}, the pick's"))
        found, why = prices(conn, p.sku, primary, p.units_per_pack, now, since, visit, trusted)
        for price in found:
            if price.cents > fit:
                why[price.store] = f"{_usd(price.cents)} a pack, above the {_usd(fit)} the pick allows"
                continue
            if alt is not None and (alt.store, alt.units_per_pack, alt.expected_cents) == (
                    price.store, price.units, price.cents) and tries:
                continue                                  # the lowered row is the same price
            new = sourcing.Option(
                sku=p.sku, rank="alternate", store=price.store, units_per_pack=price.units, expected_cents=price.cents,
                max_cents=min(price_search.agent_max(price.cents), fit), url=price.url,
                pack_description=primary.pack_description if primary.units_per_pack == price.units else None,
                checked_at=price.at, checked_by=sourcing.AGENT_BY, note=None, stock=price.stock)
            tries.append(("set" if alt is None else "replaced", new,
                          f"{price.store} {price.units}-pack {_usd(price.cents)} (max {_usd(new.max_cents)}), "
                          f"looked up {_when(price.at)}"))
        done = False
        for action, new, said in tries:
            attempt = {**rows, (p.sku, "alternate"): new}
            tried = trial(gathered, pick.round, attempt, subs)
            problem = _worse(current, tried, floor_cents)
            if problem is None:
                rows, current = attempt, tried
                lines.append(Line(p.sku, primary, alt, new, action, said, drop_sub=had is not None))
                done = True
                break
            why[new.store] = f"{said.split(' (')[0].split(':')[0]}: {problem}" if action != "lowered" else problem
        if done:
            continue
        # No same-drink backup: another drink of the plan, unless the line keeps a stored same-drink alternate.
        if had is not None:
            why_not = _stored_problem(stored, had, since, visit, now) or _worse(current, with_sub, floor_cents)
            if why_not is None:
                subs, current = {**subs, p.sku: had}, with_sub
                lines.append(Line(p.sku, primary, stored, stored, "kept", f"its stored substitute {had.substitute}"))
                continue
            why["stored substitute"] = f"{had.substitute}, {why_not}"
        if alt is None:
            picked_sub = None
            for option, most in _substitute_candidates(p, rows, pick.round, now, since, visit):
                if option.expected_cents > most:
                    why[option.sku] = (f"{option.sku} {_usd(option.expected_cents)} a pack, above the {_usd(most)} "
                                       "the pick allows")
                    continue
                attempt_subs = {**subs, p.sku: fill.Substitute(p.sku, option.sku, most)}
                tried = trial(gathered, pick.round, rows, attempt_subs)
                problem = _worse(current, tried, floor_cents)
                if problem is None:
                    subs, current = attempt_subs, tried
                    picked_sub = replace(option, sku=option.sku, rank="alternate", max_cents=min(option.max_cents, most))
                    break
                why[option.sku] = f"{option.sku}: {problem}"
            if picked_sub is not None:
                lines.append(Line(p.sku, primary, stored, picked_sub, "substitute",
                                  f"{picked_sub.sku} {_option(picked_sub)}, another drink of the plan",
                                  drop_sub=had is not None and had.substitute != picked_sub.sku))
                continue
        reasons = "; ".join(f"{store}: {text}" for store, text in why.items()) or "no other store"
        if alt is not None:
            reasons = f"the stored {alt.store} alternate differs from the pick and nothing fits. {reasons}"
        if p.sku in pooled:
            reasons = f"pooled over {in_slots[p.sku]} slots, so no substitute. {reasons}"
        if had is not None:
            lines.append(Line(p.sku, primary, stored, None, "removed", reasons, drop_sub=True))
            continue
        lines.append(Line(p.sku, primary, alt, alt, "none", reasons))
    lines = [_dropped(ln, dropped[ln.sku]) if ln.sku in dropped else ln for ln in lines]
    return Result(pick, since, visit, tuple(lines), first, current, floor_cents, read, subs_read, pooled)


def _over(t: Trial, floor_cents: int) -> bool:
    return t.max_cents > FILL_CAP_CENTS or START_CENTS - t.max_cents < floor_cents


def _dropped(ln: Line, alt: sourcing.Option) -> Line:
    """A line whose stored alternate the removal pass dropped: `before` is that alternate, and it goes on `--write`."""
    said = (f"its {alt.store} {alt.units_per_pack}-pack alternate (max {_usd(alt.max_cents)}) is another pack than the "
            "pick and costs more for the line, which kept the fill over the cap: removed")
    if ln.after is None or ln.action == "none":
        return replace(ln, before=alt, after=None, action="removed", why=f"{said}. {ln.why}", drop_alt=True)
    return replace(ln, before=alt, why=f"{said}; then {ln.why}", drop_alt=True)


def _stored_problem(row: sourcing.Option | None, had: fill.Substitute, since: datetime, visit: date,
                    now: datetime) -> str | None:
    """Why a stored substitute can't be kept as it is, or None: its row's price must be checked, fresh and at a
    store whose cutoff hasn't passed, as a new one's would."""
    if row is None:
        return f"{had.substitute} has no checked primary price"
    if row.checked_at <= since:
        return f"its price was checked {_when(row.checked_at)}, before {_when(since)}"
    if price_search.past_cutoff(row.store, visit, now):
        return f"{row.store} is past its cutoff for the {visit:%a %d %b} stocking"
    if row.expected_cents > row.max_cents:
        return f"{_usd(row.expected_cents)} a pack, above its {_usd(row.max_cents)} max"
    return None


def _substitute_row(rows: dict, sub: fill.Substitute | None) -> sourcing.Option | None:
    """A stored substitute as the row it buys from: its drink's primary, held to its max."""
    row = rows.get((sub.substitute, "primary")) if sub is not None else None
    if row is None or row.expected_cents is None:
        return None
    return replace(row, rank="alternate", max_cents=min(row.max_cents, sub.max_cents))


def _substitute_candidates(p: plan_rounds.Purchase, rows: dict, checked: plan_rounds.Round, now: datetime,
                           since: datetime, visit: date) -> list[tuple[sourcing.Option, int]]:
    """The plan's other drinks that could stand in for `p`: a checked primary row looked up since `since`, at a
    store whose cutoff for `visit` hasn't passed. Each with the most per pack that keeps its packs within the pick's
    most-we-pay for `p`. Cheapest per unit first."""
    found = []
    for other in sorted({s.sku for s in checked.slots.values() if s.sku and s.sku != p.sku}):
        row = rows.get((other, "primary"))
        if row is None or row.expected_cents is None or row.checked_at <= since or row.stock == "out_of_stock":
            continue
        if price_search.past_cutoff(row.store, visit, now):
            continue
        packs = planner.packs_for(max(p.units_loaded, 1), row.units_per_pack)
        if packs > planner.MAX_PACKS:
            continue
        most = min(row.max_cents, p.most_cents // packs, planner.MAX_PRICE_PER_PACK_CENTS)
        found.append((row, most))
    found.sort(key=lambda f: (Fraction(f[0].expected_cents, f[0].units_per_pack),
                              price_search.STORE_ORDER.index(f[0].store), f[0].sku))
    return found


def write(conn: psycopg.Connection, result: Result, by: str, now: datetime | None = None) -> list[str]:
    """Store every changed alternate in one write. Refused (nothing written) when the fill's most-we-pay after them
    is over the cap or under the floor, or when the pick or any row of the plan's drinks changed since `backups`
    read them: the trials were run on those."""
    if not by.strip():
        raise ValueError("--write needs --by NAME")
    changed = [ln for ln in result.lines if ln.changed]
    over = []
    if result.after.max_cents > FILL_CAP_CENTS:
        over.append(f"the fill's most-we-pay after these backups is {_usd(result.after.max_cents)}, over the "
                    f"{_usd(FILL_CAP_CENTS)} cap: fix the plan's own lines first")
    if START_CENTS - result.after.max_cents < result.floor_cents:
        over.append(f"the fill's most-we-pay after these backups is {_usd(result.after.max_cents)}, leaving under the "
                    f"{_usd(result.floor_cents)} floor of the {_usd(START_CENTS)} opening")
    if over and changed:
        raise Refused([Refusal(OVER_LIMIT, why) for why in over])
    arguments = {"by": by.strip(), "plan_id": result.picked.id, "label": result.picked.label,
                 "alternates": [{"sku": ln.sku, "action": ln.action, "drop_substitute": ln.drop_sub,
                                 **({"buys": ln.after.sku, "store": ln.after.store,
                                     "units_per_pack": ln.after.units_per_pack, "price_cents": ln.after.expected_cents,
                                     "max_cents": ln.after.max_cents} if ln.after is not None else {})}
                                for ln in changed]}
    said: list[str] = []

    def action(conn: psycopg.Connection) -> price_search.Recorded:
        # The pick, then every row of the plan's drinks, as the trials saw them. `sourcing.set_option` takes the
        # same SKU locks, so a `sourcing set` waits for this write, or this write sees what it stored.
        conn.execute("lock table first_fill_plans in share mode")
        if fill._picked_row(conn) != result.picked.row:
            raise Refused([Refusal(CHANGED, "the picked plan changed since it was read; run it again")])
        conn.execute("lock table fill_substitutes in share row exclusive mode")
        if fill.substitutes(conn, result.picked.id) != result.subs_read:
            raise Refused([Refusal(CHANGED, "the plan's substitutes changed since they were read; run it again")])
        skus = {p.sku for p in result.picked.round.purchases}
        sourcing.lock_skus(conn, skus)
        # The listed drinks' rows, against the very rows the trials used; a delisted drink has no line.
        for sku in sorted(sourcing.in_catalog(conn, sorted(skus))):
            for rank in sourcing.RANKS:
                if sourcing.locked_row(conn, sku, rank) != result.read.get((sku, rank)):
                    raise Refused([Refusal(CHANGED, f"{sku}'s {rank} changed since it was read; run it again")])
        at = now or conn.execute("select now()").fetchone()[0]
        for ln in changed:
            row = ln.after
            if ln.drop_alt:
                sourcing.remove(conn, ln.sku, "alternate", by.strip())
                said.append(f"{ln.sku}: its {sourcing.describe(ln.before)} alternate removed")
            if ln.drop_sub:
                conn.execute("delete from fill_substitutes where plan_id = %s and sku = %s",
                             (result.picked.id, ln.sku))
                if ln.action == "removed":
                    said.append(f"{ln.sku}: its stored substitute removed")
                    continue
            if ln.action == "removed":
                continue
            if ln.substitutes and ln.action != "substitute":
                continue                            # a kept substitute stays as stored (Sol, #265)
            if ln.substitutes:
                conn.execute(
                    "insert into fill_substitutes (plan_id, sku, substitute_sku, max_cents, set_by, set_at)"
                    " values (%s, %s, %s, %s, %s, %s) on conflict (plan_id, sku) do update set"
                    " substitute_sku = excluded.substitute_sku, max_cents = excluded.max_cents,"
                    " set_by = excluded.set_by, set_at = excluded.set_at",
                    (result.picked.id, ln.sku, row.sku, row.max_cents, by.strip(), at))
                said.append(f"{ln.sku}: substitute {row.sku}, {sourcing.describe(row)}")
                continue
            if ln.action != "lowered":
                row = replace(row, note=(f"backup set by {by.strip()} (python -m vending.alternates) from the "
                                         f"{row.store} price looked up {sourcing.day(row.checked_at)}: "
                                         f"{row.url or ''}")[:500])
            sourcing.set_option(conn, row, now=at)
            said.append(f"{ln.sku}: {ln.action} {sourcing.describe(row)}")
        return price_search.Recorded(success=True, message="; ".join(said) or "nothing to change")

    if changed:
        run_write(conn, TOOL, arguments, action)
    return said


# --- Output --------------------------------------------------------------------------------

def render(r: Result, wrote: bool) -> str:
    pick = r.picked
    out = [f"Backups for plan #{pick.id} (round {pick.label}), the {r.visit:%a %d %b} stocking, from prices looked "
           f"up since {_when(r.since)}."]
    out.append("Written." if wrote else "Dry run: nothing written. To store them: python -m vending.alternates "
                                        "--write --by NAME")
    header = ("SKU", "Primary", "Alternate now", "After")
    rows = [header]
    for ln in r.lines:
        rows.append((ln.sku, _option(ln.primary), _option(ln.before, ln.sku),
                     "removed" if ln.action == "removed" else
                     "same" if not ln.changed and ln.after is ln.before else
                     f"{ln.action}: {_option(ln.after, ln.sku)}" if ln.changed else "none"))
    widths = [max(len(row[i]) for row in rows) for i in range(len(header))]
    out += ["", *("  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in rows)]
    changes = [ln for ln in r.lines if ln.changed]
    if changes:
        out += ["", f"Changes ({len(changes)}):", *(f"- {ln.sku}: {ln.action}, {ln.why}" for ln in changes)]
    left = [ln for ln in r.lines if ln.action in ("none", "removed")]
    if left:
        out += ["", f"Left without a backup ({len(left)}):", *(f"- {ln.sku}: {ln.why}" for ln in left)]
    if pooled_left := [ln.sku for ln in left if ln.sku in r.pooled]:
        out += ["", f"Pooled lines left without a backup ({len(pooled_left)}): {', '.join(pooled_left)}"]
    same = sum(1 for ln in r.lines if ln.after is not None and ln.action != "none" and not ln.substitutes)
    other = sum(1 for ln in r.lines if ln.action != "none" and ln.substitutes)
    out += ["", f"Backups after: {same} the same drink at another store, {other} a substitute drink."]
    n = len(r.lines)
    out += ["", f"Lines with a backup: {r.with_backup('now')} of {n} now, {r.with_backup('after')} of {n} after "
                "(alternates the fill takes without pick_deviation).",
            f"Fill most-we-pay: {_usd(r.before.max_cents)} now, {_usd(r.after.max_cents)} after; cap "
            f"{_usd(FILL_CAP_CENTS)}. Cap room after: {_usd(FILL_CAP_CENTS - r.after.max_cents)}."]
    return "\n".join(out)


def _option(o: sourcing.Option | None, line: str | None = None) -> str:
    """A row as text; prefixed with its drink when it is a substitute for `line`."""
    if o is None:
        return "none"
    price = _usd(o.expected_cents) if o.expected_cents is not None else "no price"
    named = f"{o.sku} " if line is not None and o.sku != line else ""
    return f"{named}{o.store} {o.units_per_pack}-pack {price} (max {_usd(o.max_cents)})"


def _usd(cents: int) -> str:
    return plan_rounds.format_cents(cents)


def _when(at: datetime) -> str:
    return f"{at.astimezone(EASTERN):%a %d %b %H:%M} ET"


def main(argv: list[str] | None = None, *, bridge: Callable[[], Bridge] = Bridge,
         connect: Callable[[], psycopg.Connection] = db.connect, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vending.alternates", description=__doc__.split("\n")[0])
    parser.add_argument("--write", action="store_true", help="store the alternates; without it, a dry run")
    parser.add_argument("--by", help="your name, for the log (required with --write)")
    parser.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE_HOURS, metavar="HOURS",
                        help=f"use prices looked up in the last HOURS (default {DEFAULT_MAX_AGE_HOURS}, at most "
                             f"{MOST_MAX_AGE_HOURS})")
    parser.add_argument("--visit", type=date.fromisoformat, default=fill.DAY_ONE_VISIT,
                        help=f"the stocking day, for the store cutoffs (default {fill.DAY_ONE_VISIT})")
    args = parser.parse_args(argv)
    if args.write and not (args.by and args.by.strip()):
        parser.error("--write needs --by NAME")
    if not 0 < args.max_age <= MOST_MAX_AGE_HOURS:
        parser.error(f"--max-age must be above 0 and at most {MOST_MAX_AGE_HOURS} hours")
    try:
        with connect() as conn:
            result = backups(conn, bridge(), now, timedelta(hours=args.max_age), args.visit)
            wrote = False
            if args.write:
                try:
                    write(conn, result, args.by, now)
                except Refused as refused:
                    print(render(result, False), file=sys.stderr)
                    print(f"Refused; nothing was written ({len(refused.refusals)}):", file=sys.stderr)
                    for x in refused.refusals:
                        print(f"- {x.code}: {x.message}", file=sys.stderr)
                    return 1
                wrote = True
            print(render(result, wrote))
            return 0
    except (NoPlan, fill.NoPick, plan_rounds.RoundError, slots.SlotsNotRecorded, settings.SettingsError,
            ValueError) as error:
        print(error, file=sys.stderr)
        return 2
    except psycopg.Error as error:
        print(f"{type(error).__name__}: {str(error).strip().splitlines()[0]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
