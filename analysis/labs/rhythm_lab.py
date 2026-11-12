"""Build the Campus Rhythm Lab from analysis/out/lab_data.json and our slots' sales events.

    python analysis/labs/rhythm_lab.py [--lab-data PATH] [--data DIR] [--out DIR]

Writes DIR/data.json, DIR/index.html and DIR/standalone.html. Default DIR: analysis/out/labs/rhythm-lab.
Source: rhythm_template.html. Reads DATA/sales_events.csv (our slots only; written by export_sales.py).

Before building it re-derives two things the page recomputes in the browser and stops if they disagree
with the analysis script: each slot-day's empty flag, and the outcome of every rule-simulator flag.
"""
import csv
import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))  # so this works under python -I too
import labkit  # noqa: E402

TZ = ZoneInfo('America/New_York')


def parse(s):
    return dt.datetime.fromisoformat(s)


def tmin(x):  # floor to the minute (lab_data stores visit times as %H:%M, i.e. floored)
    return parse(x).replace(second=0, microsecond=0)


def lab_cut(L, sales_csv):
    problems = []
    dates = L['daily']['date']
    didx = {d: i for i, d in enumerate(dates)}
    T1 = parse(L['meta']['last_poll'])
    slots = list(L['slot_daily']['slots'].keys())
    # empty daytime hours per slot-day (08:00-17:00) from the stock-out windows, same rule as the script
    eh = {s: [0] * len(dates) for s in slots}
    for w in L['stockouts']['windows']:
        a, b = parse(w['start']), parse(w['end'])
        # same rule as sales_analysis.py: a window starts in the hour AFTER the last sale
        h = a.astimezone(dt.timezone.utc).replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
        cnt = {}
        while h < b:
            loc = h.astimezone(TZ)
            if 8 <= loc.hour < 17 and h <= T1:
                k = loc.date().isoformat()
                cnt[k] = cnt.get(k, 0) + 1
            h += dt.timedelta(hours=1)
        for k, c in cnt.items():
            if k in didx:
                i = didx[k]
                eh[w['slot']][i] = max(eh[w['slot']][i], c)
    # any slot-day with a sale counts as in stock
    for s in slots:
        u = L['slot_daily']['slots'][s]['u']
        for i in range(len(dates)):
            if u[i] > 0:
                eh[s][i] = 0
    bad = sum(1 for s in slots for i in range(len(dates)) if (eh[s][i] / 9 >= 0.5) != bool(L['slot_daily']['slots'][s]['e'][i]))
    print(f"  empty-hours reconstruction mismatches vs slot_daily.e: {bad}")
    if bad:
        problems.append(f"{bad} slot-days disagree with slot_daily.e")
    # Friday 3 PM poll-stamp totals: machine-wide aggregates computed by the analysis script (no per-slot data)
    PS = L['hourly']['poll_stamp']
    # ---- rule-sim outcomes per (decision, slot), event level, same as sales_analysis.py section 6 ----
    # our slots' sales events only (the exporter writes no other slot); a visit counts as a refill of slot s when s is in
    # its outcome_refill_slots (capacity proof for s, or a visit graded as a proven or likely restock from the door counter plus
    # our slots, not counting s's own silence; never s's own resume)
    RS = L['rule_sim']
    LPR = L['last_proven_refill']
    VIS = {v['id']: v for v in L['visits']}
    EV = {s: [] for s in slots}
    with open(sales_csv, newline='', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            if r['slot'] in EV:
                EV[r['slot']].append((tmin(r['at_eastern']), int(r['units'])))
    for s in EV:
        EV[s].sort()
    T1m = tmin(L['meta']['last_poll'])
    OUTC = dict(slots=slots, code=[], n7=[], refill_visit=[], t_dec=[])
    for vid in RS['decision_visit_ids']:
        t_dec = tmin(VIS[vid]['start']) if vid is not None else T1m
        known = t_dec + dt.timedelta(days=7) <= T1m
        codes, n7s, rvs = '', [], []
        for s in slots:
            nx = [e for e in EV[s] if t_dec < e[0] <= t_dec + dt.timedelta(days=7)]
            n7 = sum(u for _, u in nx)
            rv = None
            if nx:
                ref = [v['id'] for v in L['visits'] if t_dec <= tmin(v['end']) <= nx[0][0] and s in v['outcome_refill_slots']]
                rv = ref[0] if ref else None
            code = 0 if not known else 2 if not n7 else 3 if rv is not None else 1
            codes += str(code)
            n7s.append(n7 if known else None)
            rvs.append(rv if known else None)
        OUTC['code'].append(codes)
        OUTC['n7'].append(n7s)
        OUTC['refill_visit'].append(rvs)
        OUTC['t_dec'].append(t_dec.isoformat()[:16])
    # cross-check against every flag row the script wrote
    si = {s: j for j, s in enumerate(slots)}
    bad_o = sum(1 for row in RS['flags']['rows'] if int(OUTC['code'][row[1]][si[row[2]]]) != row[-1])
    print(f"  rule outcome mismatches vs script flag rows: {bad_o} of {len(RS['flags']['rows'])}")
    if bad_o:
        problems.append(f"{bad_o} rule outcomes disagree with the script's flag rows")
    dl = L['daily']
    keep = ['date', 'dow', 'units_raw', 'units', 'cents_raw', 'cents', 'class_day', 'gap_day', 'gap_hours', 'lump_units', 'partial', 'regular',
            'empty_slots', 'active_slots', 'term_week', 'classes', 'institute', 'label']
    cal = L['calendar']['days']
    pc = L['photo_check']
    scope = labkit.scope_of(L)
    if set(slots) != set(scope['our_slots']):
        problems.append('slot_daily does not cover exactly the slots in scope.our_slots')
    out = dict(
        scope=scope,
        meta=dict(first_poll=L['meta']['first_poll'], last_poll=L['meta']['last_poll'], polls=L['meta']['polls'], totals=L['meta']['totals'],
                  machine_totals=L['meta']['machine_totals'], our_slots=L['meta']['our_slots'],
                  regular_polling_from=L['meta']['regular_polling_from'], gaps=L['meta']['gaps']),
        poll_stamp=dict(friday_units=PS['friday_units'], friday_15h_units=PS['friday_15h_units'], friday_15h_sep18=PS['friday_15h_largest_lump'],
                        lump_at=PS['lump_at'], lump_hours=PS['lump_hours']),
        calendar=dict(events=L['calendar']['events'],
                      days=dict(date=[c['date'] for c in cal], classes=[c['classes'] for c in cal], institute=[c['institute'] for c in cal],
                                label=[c['label'] for c in cal], dow=[c['dow'] for c in cal])),
        daily={k: dl[k] for k in keep},
        weekly=[{k: w[k] for k in ['week', 'term_week', 'days', 'partial', 'units_raw', 'units', 'cents', 'class_days', 'observed_class_days',
                                   'gap_hours', 'lump_units', 'units_per_class_day', 'active_slots', 'slots_empty_any_day', 'slots_empty_at_end']}
                for w in L['weekly']],
        hourly={k: L['hourly'][k] for k in ['note', 'all', 'class_days', 'no_class_days', 'no_class_weekdays', 'hour_units_midpoint',
                                            'hour_units_poll_stamp']},
        regression=L['regression'],
        dow_index={k: L['dow_index'][k] for k in ['index', 'mean_units_per_class_day', 'n_days']},
        slot_daily={s: dict(u=L['slot_daily']['slots'][s]['u'], e=L['slot_daily']['slots'][s]['e'], eh=eh[s]) for s in slots},
        rule=dict(decision_points=RS['decision_points'], decision_display=RS['decision_display'], decision_kinds=RS['decision_kinds'],
                  decision_visit_ids=RS['decision_visit_ids'], decision_note=RS['decision_note'], definitions=RS['definitions'],
                  asof=[{s: dict(e=[int(c) for c in a['e']], eh=[int(c) for c in a['eh']]) for s, a in dec.items()} for dec in RS['asof']],
                  verdicts=RS['verdicts'],
                  outcome=OUTC,
                  script_summary=[{k: r[k] for k in ['rule', 'flags', 'flags_with_outcome', 'contradicted', 'sold_after_refill', 'silent_next_week',
                                                     'flagged_while_suspected_empty', 'units_sold_next_14d_by_flagged', 'next_14d_partial_windows',
                                                     'flagged_now']} for r in RS['summary']]),
        visits=[{k: v[k] for k in ['id', 'start', 'end', 'first_rise', 'weekday', 'openings', 'evidence', 'proven_slots', 'pair_proof_slots',
                                   'resumed_slots', 'outcome_refill_slots', 'resumed_but_known_not_refill', 'note']} for v in L['visits']],
        # the lab's marker line is the last refill PROVEN at a single visit (later visits refilled only some of our rails)
        last_refill={k: LPR[k] for k in ['visit', 'statement']} | dict(
            max_units_one_slot_after_last_restock=LPR['max_units_one_slot_after'],
            last_restock={k: LPR[k] for k in ['visit', 'start', 'end', 'weekday', 'evidence']},
            pair_visits=[x['visit'] for x in LPR['later_pair']['visits']] if LPR.get('later_pair') else [],
            pair_slots=LPR['later_pair']['slots'] if LPR.get('later_pair') else [], proven_slots=LPR['slots'],
            later_possible_topups=[x['visit'] for x in LPR['later_possible_topups']]),
        supply=[{k: w[k] for k in ['week', 'partial', 'units', 'slots_selling', 'slots_suspected_empty_any_day', 'slots_empty_at_week_end',
                                   'slots_sold_out_not_refilled', 'slots_silent_since_before_last_restock', 'slots_empty_never_refilled', 'visits',
                                   'refill_visits']} for w in L['supply']],
        photo=dict(at=pc['at'], visit=pc['visit'], stocked=pc['photo_stocked'], no_conflict_share=pc['no_conflict_share'],
                   rails=pc['rails'],
                   **{k: pc[k] for k in ['agreement', 'tally', 'stocked_and_flagged_empty', 'empty_and_not_flagged', 'empty_and_flagged_empty']}),
    )
    return out, problems


def main():
    args = labkit.parse_args("rhythm-lab", __doc__.splitlines()[0])
    L = labkit.load_lab_data(args.lab_data)
    out, problems = lab_cut(L, Path(args.data) / "sales_events.csv")
    if problems:
        raise SystemExit("rhythm-lab: the lab's own recomputation disagrees with the analysis: " + "; ".join(problems))
    data = labkit.write_json(Path(args.out) / "data.json", out)
    t = labkit.read("rhythm_template.html", out['scope'])
    assert t.count('/*DATA*/') == 1
    page = t.replace('/*DATA*/', data)
    idx, std = labkit.write_pages(args.out, page)
    print(f"rhythm-lab: {idx} ({len(page)} chars), {std.name}")


if __name__ == "__main__":
    main()
