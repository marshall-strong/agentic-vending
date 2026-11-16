"""The dashboard's money panel (#48): the balance, where the $600 has gone, what is committed but not
yet paid, and the profit and loss since the opening.

Every figure comes from `vending.ledger` (`balance`, `pnl`), except one: *committed*, the most the
pending purchase requests can still cost. The ledger counts a purchase only once it is fulfilled, so
a submitted day-one order isn't in the balance until the worker reports it. That figure is read from
each pending request's own body: for each line, the dearer of its primary and alternate at
`packs x max_price_per_pack`, plus the visit fee stored on an off-cycle request. That is the same
worst case the planner checks against the spendable balance.

The split of the $600 and the $53 floor are the team's *proposed* capital plan (#113), not agreed:
they are reference lines, never a limit. Nothing here enforces them.

The competition counts from the ledger's opening entry. Everything before it is development and
test spend, not from the $600: the panel never counts it in a competition figure, and says so where
it shows. An opening entry dated in the future (recorded ahead with `--at`) isn't open yet: the
panel keeps its pre-opening view and says when the ledger opens. Stock on hand and cost of sales
replay every receipt, including any from before the opening, so the panel notes such receipts
when there are any.

Reads only, inside the dashboard's one read-only snapshot. Money is exact: cents as Fractions in,
dollar strings out (`ledger.money`), never a float. No model calls.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from html import escape
from typing import Any

import psycopg

from vending import config, ledger
from vending.slots import SlotsNotRecorded

# Proposed in #113 ("Opening capital plan"), not yet agreed: how the $600 is split.
PROPOSED_SPLIT: tuple[tuple[str, int], ...] = (
    ("Day-one fill (most-we-pay)", 36_600),
    ("Midweek visits (2 x $25)", 5_000),
    ("Model spend (9 weeks x $1)", 900),
    ("Product-switch reserve", 6_800),
    ("Safety margin", 10_700),
)
MODEL_BUDGET_CENTS = 900
VISIT_BUDGET_CENTS = 5_000
FLOOR_CENTS = 5_300           # one midweek fee + 7 days at the $4.00 daily model cap (#113)
PLAN_ISSUE = "https://github.com/marshall-strong/agentic-vending/pull/113"


def _cents(text: Any) -> Fraction | None:
    """A dollar amount from a request body ("40.50") to exact cents, or None if it isn't one."""
    if isinstance(text, (bool, float)) or text is None:
        return None
    try:
        value = Decimal(str(text))
    except InvalidOperation:
        return None
    return Fraction(value) * 100 if value.is_finite() else None


def _worst_case(body: Any) -> tuple[Fraction, int]:
    """The most one request body can cost in drinks, and how many purchase lines had no usable price."""
    total, unpriced = Fraction(0), 0
    lines = body.get("lines") if isinstance(body, dict) else None
    for line in lines if isinstance(lines, list) else []:
        if not isinstance(line, dict) or line.get("source") != "purchase":
            continue
        worst = None
        for key in ("primary", "alternate"):
            choice = line.get(key)
            if not isinstance(choice, dict):
                continue
            price, packs = _cents(choice.get("max_price_per_pack")), choice.get("packs")
            if price is None or price < 0 or not isinstance(packs, int) or isinstance(packs, bool) or packs < 0:
                continue
            worst = max(worst or 0, price * packs)
        if worst is None:
            unpriced += 1
        else:
            total += worst
    return total, unpriced


def committed(conn: psycopg.Connection) -> dict:
    """The most the pending purchase requests can still cost, and how many there are.

    A submission left `sending` or `unknown` (sent, no usable answer: it may exist at the bridge)
    counts too, at its worst case, since the worker may fill it without our knowing its ID.
    `max` is None when any purchase line has no usable price: the most is then unknown, never
    the sum of the lines that do have one.
    """
    pending, _ = _committed(conn)
    return pending


def _committed(conn: psycopg.Connection) -> tuple[dict, Fraction | None]:
    """`committed`, and its maximum as exact cents (None when unknown)."""
    fee = config.get_value(conn, "midweek_fee_cents", ledger.DEFAULT_MIDWEEK_FEE_CENTS)
    if not isinstance(fee, int) or isinstance(fee, bool) or fee < 0:
        fee = ledger.DEFAULT_MIDWEEK_FEE_CENTS
    rows = conn.execute("select off_cycle, request_body, visit_fee_cents from purchase_requests"
                        " where status = 'pending' order by submitted_at").fetchall()
    unsettled = []
    if conn.execute("select to_regclass('purchase_submissions')").fetchone()[0] is not None:
        # An off-cycle submission carries the fee it quoted; a null fee means a Friday request.
        unsettled = [(fee_c is not None, body, fee_c) for body, fee_c in conn.execute(
            "select body, visit_fee_cents from purchase_submissions where status in ('sending', 'unknown')"
            " order by at").fetchall()]
    total, unpriced = Fraction(0), 0
    for off_cycle, body, visit_fee in list(rows) + unsettled:
        drinks, missing = _worst_case(body)
        total += drinks
        unpriced += missing
        if off_cycle:
            total += visit_fee if isinstance(visit_fee, int) and not isinstance(visit_fee, bool) else fee
    most = None if unpriced else total
    return {"requests": len(rows) + len(unsettled), "unsettled": len(unsettled),
            "max": ledger.money(most), "unpriced_lines": unpriced}, most


def early_receipts(conn: psycopg.Connection, opening_at: datetime) -> tuple[int, Fraction]:
    """(receipt lines, cents paid) for fulfilled lines resolved before the opening.

    The balance leaves them out (`ledger._receipts` is windowed), but `vending.stock` replays every
    fulfilled request, so they are in stock on hand and can be in cost of sales.
    """
    lines, total = conn.execute(
        "select count(*), coalesce(sum(l.total_paid), 0) from purchase_lines l join purchase_requests r"
        " using (request_id) where r.status = 'fulfilled' and l.resolved_at < %s", (opening_at,)).fetchone()
    return lines, Fraction(Decimal(total)) * 100


def read(conn: psycopg.Connection, now: datetime) -> dict:
    """Everything the panel shows, as exact dollar strings.

    `opened` is False before the opening entry, and while the opening entry is dated after `now`
    (then `opens_at` says when).
    """
    pending, most = _committed(conn)
    start = ledger.opening(conn)
    if start is not None and start.at > now:
        return {"opened": False, "opens_at": start.at, "committed": pending}
    try:
        bal = ledger.balance(conn, as_of=now)
        p = ledger.pnl(conn, bal.opening_at, now) if bal is not None else None
    except (ledger.LedgerError, SlotsNotRecorded) as error:
        # Our text, not a driver's: it says what to do (for example, record our slot assignment).
        return {"opened": False, "problem": str(error), "committed": pending}
    if bal is None or p is None:
        return {"opened": False, "committed": pending}
    m = ledger.money
    after_pending = None if most is None else bal.spendable_cents - most
    early_lines, early_cents = early_receipts(conn, bal.opening_at)
    early_notes = [] if not early_lines else [
        f"Stock on hand and cost of sales count every receipt, including {early_lines} receipt "
        f"line{'' if early_lines == 1 else 's'} totalling ${m(early_cents)} dated before the opening on "
        f"{bal.opening_at:%Y-%m-%d}: development or test purchases, not from the $600 and not in the balance. "
        "If that isn't real stock, stock on hand is overstated."]
    return {
        "opened": True,
        "opening_at": bal.opening_at,
        "opening": m(bal.opening_cents), "capital": m(bal.capital_cents),
        "takings": m(bal.takings.cents), "tax_set_aside": m(bal.takings.tax_cents),
        "receipts": m(bal.receipts_cents), "receipt_lines": bal.receipt_lines,
        "visit_fees": m(bal.visit_fees_cents), "visits": bal.visits,
        "tokens": m(bal.token_spend_cents), "tax_remitted": m(bal.tax_remitted_cents),
        # Drinks, visits and model together, summed exact and rounded once: the three rounded parts can
        # add up to a cent less or more.
        "spent": m(bal.receipts_cents + bal.visit_fees_cents + bal.token_spend_cents),
        "adjustments": m(bal.adjustments_cents),
        "balance": m(bal.balance_cents), "tax_owed": m(bal.tax_owed_cents), "spendable": m(bal.spendable_cents),
        "spendable_after_pending": m(after_pending),
        "notes": list(bal.notes) + [n for n in p.notes if n not in bal.notes] + early_notes,
        "early_receipts": {"lines": early_lines, "paid": m(early_cents)},
        "committed": pending,
        "pnl": {
            "units_sold": p.units_sold, "units_costed": p.units_costed,
            "revenue_ex_tax": m(p.revenue_ex_tax_cents),
            "cost_of_sales": m(p.cost_of_sales_cents) if p.cost_of_sales_complete else None,
            "gross_margin": m(p.gross_margin_cents) if p.cost_of_sales_complete else None,
            "shrinkage": m(p.shrinkage_cents), "shrinkage_complete": p.shrinkage_complete,
            "visit_fees": m(p.visit_fees_cents), "tokens": m(p.token_spend_cents),
            "net": m(p.net_cents) if p.cost_of_sales_complete and p.shrinkage_complete else None,
            "inventory": m(p.inventory_cents) if p.inventory_cents is not None else None,
            "inventory_complete": p.inventory_complete,
        },
    }


def _plain(text: str) -> str:
    """A dollar string for a sentence: "-5.00" reads "-$5.00", not "$-5.00"."""
    return f"-${text[1:]}" if text.startswith("-") else f"${text}"


def attention(data: dict) -> list[tuple[str, str]]:
    """(level, sentence) for the dashboard's Needs attention."""
    out = []
    pending = data["committed"]
    if data.get("problem"):
        out.append(("warn", f"The ledger can't give a balance: {data['problem']}"))
        return out
    if not data["opened"]:
        if pending["requests"] and data.get("opens_at"):
            out.append(("warn", f"{pending['requests']} purchase request(s) are pending, but the ledger doesn't "
                                f"open until {data['opens_at'].astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC, so "
                                "nothing can say whether we can pay, and a purchase fulfilled before the opening "
                                "isn't counted against the $600."))
        elif pending["requests"]:
            out.append(("warn", f"{pending['requests']} purchase request(s) are pending, but the ledger has no "
                                "opening entry, so nothing can say whether we can pay. Record it with "
                                "`python -m vending.ledger open --amount 600.00 --by NAME`."))
        return out
    spendable = Decimal(data["spendable"])
    after = None if data["spendable_after_pending"] is None else Decimal(data["spendable_after_pending"])
    if pending["unpriced_lines"]:
        n = pending["unpriced_lines"]
        out.append(("warn", f"{n} pending purchase line{' has' if n == 1 else 's have'} no price we can read, so "
                            "the most the pending requests can cost is unknown and we can't say whether we can pay "
                            "for them. Check each pending request's `max_price_per_pack` and `packs` in "
                            "`purchase_requests`."))
    if pending.get("unsettled"):
        out.append(("warn", f"{pending['unsettled']} purchase submission(s) were sent with no usable answer, so "
                            "they may exist at the bridge; they are counted as committed. Check the bridge, then run "
                            "`python -m vending.submissions resolve`."))
    if spendable < 0:
        out.append(("bad", f"Spendable is {_plain(data['spendable'])}: more has gone out than we have."))
    elif after is not None and after < 0:
        out.append(("bad", f"Pending purchase requests could cost up to {_plain(pending['max'])}, more than the "
                           f"{_plain(data['spendable'])} we can spend."))
    elif after is not None and after * 100 < FLOOR_CENTS and pending["requests"]:
        out.append(("warn", f"If the pending requests cost their most ({_plain(pending['max'])}), "
                            f"{_plain(data['spendable_after_pending'])} stays spendable, under the proposed $53.00 "
                            "floor (#113)."))
    elif spendable * 100 < FLOOR_CENTS:
        out.append(("warn", f"Spendable is {_plain(data['spendable'])}, under the proposed $53.00 floor (#113)."))
    if Decimal(data["tokens"]) * 100 > MODEL_BUDGET_CENTS:
        out.append(("info", f"Model spend since the opening is {_plain(data['tokens'])}, over the proposed $9.00 (#113)."))
    return out


# --- Page ---------------------------------------------------------------------------------------

def _e(value: Any) -> str:
    return "" if value is None else escape(str(value))


def _usd(text: str | None) -> str:
    if text is None:
        return '<span class="dim">unknown</span>'
    return f"&minus;${_e(text[1:])}" if text.startswith("-") else f"${_e(text)}"


def _day(value: datetime) -> str:
    """A date the page's script shows in the viewer's zone (`data-f="day"`); UTC text until it runs."""
    utc = value.astimezone(timezone.utc)
    return (f'<time datetime="{utc.isoformat()}" data-f="day" title="{utc:%Y-%m-%d %H:%M} UTC">'
            f'{utc:%a %d %b}</time>')


def _c(text: str | None) -> int:
    return int(Decimal(text or "0") * 100)


def _meter(label: str, used: str, budget_cents: int, note: str) -> str:
    used_c = _c(used)
    pct = 0 if budget_cents <= 0 else min(100, 100 * used_c / budget_cents)
    tone = "bad" if used_c > budget_cents else "warn" if used_c * 5 > budget_cents * 4 else "ok"
    return (f'<div class="mrow"><div class="mlabel">{_e(label)}</div><div class="mbar"><i class="{tone}" '
            f'style="width:{pct:.1f}%"></i></div><div class="mval">{_usd(used)} of {_usd(ledger.money(budget_cents))}'
            f'</div><div class="sub">{note}</div></div>')


def _split_bar(d: dict) -> str:
    """Where the money in has gone: one stacked bar that adds up to opening + capital + takings
    (+ any positive adjustment), so every segment is a share of everything we have had."""
    adj, remitted, owed = _c(d["adjustments"]), _c(d["tax_remitted"]), _c(d["tax_owed"])
    if owed < 0:
        # More tax handed over than is due: spendable already counts the overpayment back, so the bar
        # shows only the tax that was due, and the key says what was overpaid.
        tax = [(f"Tax handed over (net of {_plain(ledger.money(-owed))} overpaid)", remitted + owed, "remitted")]
    else:
        tax = [("Tax handed over", remitted, "remitted"), ("Tax set aside", owed, "owed")]
    parts = [("Drinks bought", _c(d["receipts"]), "drinks"), ("Midweek visits", _c(d["visit_fees"]), "visits"),
             ("Model", _c(d["tokens"]), "model"), *tax, ("Spendable", _c(d["spendable"]), "spend")]
    if adj < 0:
        parts.insert(3, ("Adjustments", -adj, "adjust"))
    total = sum(max(c, 0) for _, c, _ in parts) or 1
    segs = "".join(f'<i class="seg-{k}" style="width:{100 * max(c, 0) / total:.2f}%" '
                   f'title="{_e(label)}: {_usd(ledger.money(c))}"></i>' for label, c, k in parts if c > 0)
    keys = "".join(f'<span><i class="seg-{k}"></i>{_e(label)} {_usd(ledger.money(c))}</span>'
                   for label, c, k in parts if c != 0 or k == "spend" or owed < 0 and k == "remitted")
    money_in = (f'{_usd(d["opening"])} opening'
                + (f' + {_usd(d["capital"])} capital' if _c(d["capital"]) else "")
                + f' + {_usd(d["takings"])} takings' + (f' + {_usd(d["adjustments"])} adjustments' if adj > 0 else ""))
    return (f'<div class="mbar split" role="img" aria-label="Where the money has gone">{segs}</div>'
            f'<div class="mkeys">{keys}</div><div class="sub">Money in: {money_in}.</div>')


def html(panel: dict) -> str:
    """The panel's inner HTML: a dashboard `_panel` dict, ok or not."""
    if not panel["ok"]:
        return f'<p class="empty">Not available: {_e(panel["why"])}</p>'
    d = panel["data"]
    pending = d["committed"]
    pend = ""
    if pending["requests"]:
        pend = (f'<p class="sub"><b>{pending["requests"]} pending purchase request'
                f'{"" if pending["requests"] == 1 else "s"}</b> could cost up to {_usd(pending["max"])}; the ledger '
                'counts a purchase only once it is fulfilled.'
                + (f' {pending["unsettled"]} of them sent with no usable answer.' if pending.get("unsettled") else "")
                + (f' {pending["unpriced_lines"]} line(s) have no price we can read, so the most is unknown.'
                   if pending["unpriced_lines"] else "")
                + "</p>")
    split = "".join(f"<li>{_e(label)} <b>{_usd(ledger.money(c))}</b></li>" for label, c in PROPOSED_SPLIT)
    plan = (f'<details class="plan113"><summary>The proposed split of the $600 (<a href="{PLAN_ISSUE}">#113</a>, '
            f'not agreed)</summary><ul>{split}</ul><p class="sub">Keep at least $53.00 spendable after any order: '
            'one midweek fee and a week of the daily model cap.</p></details>')
    if d.get("problem"):
        return f'<p class="empty bad-text">No balance: {_e(d["problem"])}</p>' + pend + plan
    if not d["opened"] and d.get("opens_at"):
        return (f'<p class="empty">The ledger opens {_day(d["opens_at"])}: its opening entry is recorded ahead '
                'of time, so there is no balance until then. Spend before the opening is development, not from '
                'the $600.</p>' + pend + plan)
    if not d["opened"]:
        return ('<p class="empty">No opening entry in the ledger yet, so there is no balance. Record the '
                '$600 before the day-one fill (#107): <code>python -m vending.ledger open --amount 600.00 '
                '--by NAME</code>. Spend before the opening is development, not from the $600.</p>' + pend + plan)
    p = d["pnl"]
    tiles = (
        f'<div class="mtile"><div class="label">Balance</div><div class="big">{_usd(d["balance"])}</div>'
        f'<div class="sub">since the opening {_day(d["opening_at"])}</div></div>'
        f'<div class="mtile"><div class="label">Spendable</div><div class="big">{_usd(d["spendable"])}</div>'
        f'<div class="sub">balance less {_usd(d["tax_owed"])} sales tax set aside'
        + (f'<br>{_usd(d["spendable_after_pending"])} if pending requests cost their most' if pending["requests"] else "")
        + '</div></div>'
        f'<div class="mtile"><div class="label">Profit since the opening</div><div class="big">{_usd(p["net"])}</div>'
        f'<div class="sub">{p["units_sold"]} units sold &middot; revenue {_usd(p["revenue_ex_tax"])} without tax'
        + ("" if p["net"] is not None else " &middot; unknown until every unit sold has a receipt cost") + "</div></div>")
    meters = (_meter("Model spend", d["tokens"], MODEL_BUDGET_CENTS, "live calls, from the ledger")
              + _meter("Midweek visits", d["visit_fees"], VISIT_BUDGET_CENTS,
                       f'{d["visits"]} visit{"" if d["visits"] == 1 else "s"}'))
    pnl_rows = "".join(f'<tr><td>{label}</td><td class="n">{value}</td></tr>' for label, value in (
        ("Revenue without tax", _usd(p["revenue_ex_tax"])),
        ("Cost of the units sold", _usd(p["cost_of_sales"])),
        ("Gross margin", _usd(p["gross_margin"])),
        ("Shrinkage", _usd(p["shrinkage"]) + ("" if p["shrinkage_complete"] else " (some costs unknown)")),
        ("Midweek visit fees", _usd(p["visit_fees"])),
        ("Model spend", _usd(p["tokens"])),
        ("<b>Profit</b>", f'<b>{_usd(p["net"])}</b>'),
        ("Stock on hand, at cost" + (
            f'<div class="sub warn-text">all receipts, including {_usd(d["early_receipts"]["paid"])} dated before the '
            'opening (see the ledger notes)</div>' if d["early_receipts"]["lines"] else ""),
         _usd(p["inventory"]) + ("" if p["inventory_complete"] else " (some costs unknown)")),
    ))
    notes = "".join(f"<li>{_e(n)}</li>" for n in d["notes"])
    return (f'<div class="mtiles">{tiles}</div>'
            f'<h3>Where the money has gone</h3>{_split_bar(d)}{pend}'
            f'<h3>Against the proposed budget</h3>{meters}'
            f'<div class="grid2 mlow"><div><h3>Profit and loss since the opening</h3><table class="mpnl">{pnl_rows}</table>'
            '<p class="sub">Weekly detail: <code>python -m vending.pnl_report</code>.</p></div>'
            f'<div>{plan}' + (f'<h3>Ledger notes</h3><ul class="mnotes">{notes}</ul>' if notes else "") + "</div></div>")


STYLE = """
.mtiles { display:grid; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); gap:10px; margin-bottom:6px }
.mtile { background:var(--sunk); border:1px solid var(--line); border-radius:10px; padding:10px 12px; min-width:0 }
.mbar { height:12px; background:var(--bar); border-radius:6px; overflow:hidden; display:flex }
.mbar i { display:block; height:100% }
.mbar i.ok { background:var(--ok) } .mbar i.warn { background:var(--warn) } .mbar i.bad { background:var(--bad) }
.mbar.split { height:18px; margin:4px 0 6px }
.seg-drinks { background:#3b82c4 } .seg-visits { background:#c77d1a } .seg-model { background:#8b5cc6 }
.seg-remitted { background:#7a8794 } .seg-owed { background:#a9b4bf } .seg-adjust { background:#c0262d }
.seg-spend { background:var(--ok) }
.mkeys { display:flex; flex-wrap:wrap; gap:4px 14px; font-size:12px; color:var(--muted) }
.mkeys i { display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:4px; vertical-align:-1px }
.mrow { display:grid; grid-template-columns:140px minmax(0, 1fr) 150px; gap:4px 10px; align-items:center; margin:6px 0 }
.mrow .sub { grid-column:2 / -1; margin-top:-2px }
.mlabel { font-weight:600; font-size:13px } .mval { font-size:12px; text-align:right; white-space:nowrap }
@media (max-width:560px) { .mrow { grid-template-columns:minmax(0, 1fr) auto } .mrow .mbar { grid-column:1 / -1; order:3 }
  .mrow .sub { grid-column:1 / -1; order:4 } }
table.mpnl { border-collapse:collapse; width:100% } table.mpnl td { padding:4px 6px; border-bottom:1px solid var(--line) }
table.mpnl td.n { text-align:right; white-space:nowrap }
.plan113 { margin-top:28px; font-size:13px } .plan113 ul { margin:6px 0; padding-left:18px }
ul.mnotes { margin:6px 0; padding-left:18px; font-size:12px; color:var(--muted) }
"""
