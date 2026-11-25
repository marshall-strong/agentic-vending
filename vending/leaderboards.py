"""Rank competition sales from the stock replay and the ledger's weekly takings."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time
from html import escape
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from vending import ledger, stock
from vending.slots import SlotsNotRecorded, our_slots

if TYPE_CHECKING:
    import psycopg


GROUPS = (("product", "Products"), ("flavor", "Flavors"), ("brand", "Brands"))
PERIODS = (("this_week", "This week"), ("since_opening", "Since the opening"))

# Apportion the tax already rounded by the ledger. Each SKU gets the difference
# between two cumulative shares, so the rounding adds up. Any takings that
# cannot be linked to a SKU keep their share outside the rankings.
RANKINGS = """
with events as (
    select * from unnest(%s::date[], %s::text[], %s::integer[], %s::integer[], %s::integer[], %s::bigint[])
        as e(week, sku, units, loaded, carried, gross)
), takings as (
    select * from unnest(%s::date[], %s::bigint[], %s::bigint[]) as t(week, gross, tax)
), sku_weeks as (
    select week, sku, sum(units) as units, sum(loaded) as loaded, sum(carried) as carried,
           coalesce(sum(gross), 0) as gross,
           bool_or(units > 0 and gross is null) as unknown_revenue
    from events group by week, sku
), shares as (
    select s.*, t.tax, t.gross as week_gross,
           sum(s.gross) over (partition by s.week order by sku rows unbounded preceding) as cumulative
    from sku_weeks s left join takings t using (week)
), product_weeks as (
    select *, gross - coalesce(
        round(cumulative * tax / nullif(week_gross, 0)) -
        round((cumulative - gross) * tax / nullif(week_gross, 0)), 0) as net
    from shares
), totals as (
    select period, kind, key, label, sum(w.units) as units, sum(w.loaded) as loaded,
           sum(w.loaded + case when period = 'this_week' then w.carried else 0 end) as available,
           case when not bool_or(unknown_revenue) then sum(w.net) end as revenue
    from product_weeks w
    left join catalog_products c using (sku)
    cross join lateral (values ('since_opening', true), ('this_week', w.week = %s)) p(period, include_week)
    cross join lateral (values
        ('product', w.sku, coalesce(nullif(c.name, ''), w.sku)),
        ('flavor', nullif(c.flavor, ''), coalesce(nullif(c.flavor, ''), 'Unknown')),
        ('brand', nullif(c.line, ''), coalesce(nullif(c.line, ''), 'Unknown'))
    ) g(kind, key, label)
    where include_week
    group by period, kind, key, label
    having sum(w.units) > 0
), ranked as (
    select *, case when units <= available then round(units * 100 / nullif(available, 0), 1) end as sell_through,
           row_number() over (partition by period, kind order by units desc, label, key) as rank
    from totals
)
select period, kind, key, label, units, loaded, available, revenue, sell_through
from ranked where rank <= 10 order by period, kind, rank
"""


def read(conn: psycopg.Connection, now: datetime) -> dict:
    opened = ledger.opening(conn)
    if opened is None or now < opened.at:
        return {"opened": False}
    try:
        assigned = set(our_slots(conn))
    except SlotsNotRecorded:
        return {"opened": True, "assignment_missing": True}

    tz = ZoneInfo(ledger.TIMEZONE)
    week = ledger.week_of(now.astimezone(tz))
    week_start = datetime.combine(week, time.min, tz)
    # Keep all our receipts in the replay so stock bought for a previously
    # assigned slot can still supply the closet. Rank only our current slots.
    # The opening poll is a baseline. Its rise can precede a load at that same time.
    moves = [m for m in stock.load_movements(conn, until=now) if m.kind != "sale" or m.at > opened.at]
    cents = ledger.opening_rises(ledger._counter_rows(conn, "lifetime_sales_cents", now), opened.at, now)
    _, _, units, _ = ledger.opening_rises(ledger._counter_rows(conn, "lifetime_units", now), opened.at, now)
    gross = {(r.selection, r.at): r.amount for r in cents[2]}
    measured = {(r.selection, r.at): r.amount for r in units}
    sales: dict[tuple[str, datetime], list[stock.SaleCost]] = defaultdict(list)
    for sale in stock.replay(moves, now).sales:
        key = (sale.slot, sale.at)
        if key in measured:
            sales[key].append(sale)

    events: list[tuple[date, str, int, int, int, int | None]] = []
    for key, window in sales.items():
        linked = [s for s in window if s.product is not None and s.attributed
                  and (s.attribution_since is None or s.attribution_since >= opened.at)]
        # The money and unit counters can recover from a reset at different
        # polls. Show revenue only if the replay's split matches the ledger rise.
        amounts = [s.gross_cents for s in linked if s.gross_cents is not None]
        known = len(amounts) == len(window) and sum(amounts) == gross.get(key)
        for sale in linked:
            events.append((ledger.week_of(sale.at.astimezone(tz)), sale.product, sale.units,
                           0, 0, sale.gross_cents if known else None))
    unlinked = max(0, sum(measured.values()) - sum(e[2] for e in events))
    estimated = ledger.summarize_sales(s for window in sales.values() for s in window).units_estimated
    for move in moves:
        # The stock reader already applied receipts_count_from to these loads.
        if move.slot in assigned and move.kind in ("purchase", "load") and move.to_slot and move.at <= now:
            events.append((ledger.week_of(move.at.astimezone(tz)), move.sku, 0, move.to_slot, 0, None))
    for slot in stock.replay(moves, week_start, inclusive=False).slots:
        if slot.slot in assigned and slot.units:
            events.append((week, slot.product, 0, 0, slot.units, None))

    result = {"opened": True, "unlinked_units": unlinked, "units_estimated": estimated,
              **{period: {kind: [] for kind, _ in GROUPS} for period, _ in PERIODS}}
    if not events:
        return result
    weeks = ledger._takings(cents, tz).by_week
    params = (*map(list, zip(*events)), [w.week for w in weeks], [w.takings_cents for w in weeks],
              [w.tax_cents for w in weeks], week)
    for period, kind, key, name, units, loaded, available, revenue, sell_through in conn.execute(
        RANKINGS, params,
    ):
        result[period][kind].append({
            "name": name, "sku": key if kind == "product" else None, "units": int(units), "loaded": int(loaded),
            "available": int(available),
            "revenue": ledger.money(int(revenue)) if revenue is not None else None,
            "sell_through": float(sell_through) if sell_through is not None else None,
        })
    return result


def html(panel: dict) -> str:
    if not panel["ok"]:
        return '<p class="empty">Not available.</p>'
    data = panel["data"]
    if not data["opened"]:
        return '<p class="empty">Leaderboards start at the ledger opening</p>'
    if data.get("assignment_missing"):
        return '<p class="empty">Slot assignment unavailable.</p>'
    out = ['<p class="sub lede">Top 10 by units sold. Revenue excludes tax. Sell-through divides units sold by '
           'available stock. This week includes starting stock and loads. Since the opening uses loads only. '
           'If sales exceed recorded stock, the rate is unavailable.</p>']
    unlinked = data["unlinked_units"]
    if unlinked:
        sales = "sale" if unlinked == 1 else "sales"
        out.append(f'<p class="sub lede">{unlinked} {sales} not linked to a product since the opening.</p>')
    estimated = data["units_estimated"]
    if estimated:
        units = "unit" if estimated == 1 else "units"
        out.append(f'<p class="sub lede">Estimated cost for {estimated} {units} sold across a swap the counters '
                   'cannot split. The units and their revenue stay out of the rankings.</p>')
    for period, label in PERIODS:
        hint = ' <span>Saturday to Friday, Eastern</span>' if period == "this_week" else ""
        out.append(f'<h3 class="leaderboard-period">{label}{hint}</h3>')
        if not data[period]["product"]:
            out.append('<p class="empty">No sales linked to products yet.</p>')
            continue
        out.append('<div class="leaderboard-grid">')
        for kind, title in GROUPS:
            out.append(f'<table aria-label="{label} by {kind}"><caption>{title}</caption><thead><tr>'
                       f'<th scope="col">{kind.title()}</th><th scope="col">Units</th>'
                       '<th scope="col">Revenue</th><th scope="col">Sell-through</th></tr></thead><tbody>')
            for row in data[period][kind]:
                name = escape(row["name"])
                sku = (f'<small>{escape(row["sku"])}</small>'
                       if row["sku"] and row["sku"] != row["name"] else "")
                revenue = "$" + row["revenue"] if row["revenue"] is not None else "—"
                sell = f'{row["sell_through"]:g}%' if row["sell_through"] is not None else "—"
                out.append(f'<tr><th scope="row">{name}{sku}</th><td>{row["units"]}</td>'
                           f'<td>{revenue}</td><td>{sell}</td></tr>')
            out.append('</tbody></table>')
        out.append('</div>')
    return "".join(out)


STYLE = """
.leaderboard-period span { font-weight:400; text-transform:none; letter-spacing:normal; margin-left:4px }
.leaderboard-grid { display:grid; align-items:start; grid-template-columns:repeat(auto-fit,minmax(min(100%,290px),1fr)); gap:12px }
.leaderboard-grid table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; font-size:13px }
.leaderboard-grid caption { text-align:left; font-weight:650; padding:0 6px 5px }
.leaderboard-grid th, .leaderboard-grid td { padding:5px 6px; border-bottom:1px solid var(--line); text-align:right }
.leaderboard-grid th:first-child { text-align:left; overflow-wrap:anywhere }
.leaderboard-grid thead { color:var(--muted); font-size:11px }
.leaderboard-grid td { white-space:nowrap }
.leaderboard-grid small { display:block; color:var(--muted); font-weight:400 }
"""
