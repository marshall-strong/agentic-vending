"""Build the Slot and Price Lab from analysis/out/lab_data.json.

    python analysis/labs/slot_lab.py [--lab-data PATH] [--out DIR]

Writes DIR/data.json, DIR/index.html and DIR/standalone.html. Default DIR: analysis/out/labs/slot-lab.
Source: slotlab_template.html. Every per-slot value in the lab must be one of our slots, or the build stops.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # so this works under python -I too
import labkit  # noqa: E402


def lab_cut(d):
    dl = d['daily']
    days = dict(date=dl['date'], dow=dl['dow'], cd=dl['class_day'], gap=dl['gap_day'], part=dl['partial'], reg=dl['regular'],
                cls=dl['classes'], inst=dl['institute'], lab=dl['label'],
                mu=dl['units'])   # whole-machine units per day: a total (allowed aggregate trend), never a per-slot value
    keep = ['slot', 'row', 'col', 'units', 'cents', 'units_regular', 'rate', 'rate_recent', 'avg_price', 'current_price', 'fri_share',
            'zero_week_share', 'cv_weekly', 'empty_now', 'last_sale', 'days_since_last_sale', 'instock_class_days', 'class_days', 'empty_windows']
    slots = [{k: s[k] for k in keep} for s in d['slots']]
    pc = d['price_changes']
    ck = ['slot', 'when', 'old', 'new', 'units_before', 'instock_days_before', 'units_after', 'instock_days_after', 'rate_before', 'rate_after',
          'rate_ratio']
    changes = [{k: c.get(k) for k in ck} for c in pc['changes']]
    cl = d['clusters']
    byk = {k: dict(inertia=v['inertia'], pct=v['pct_explained'], sil=v['silhouette'], stab=v['stability_pair_agreement'],
                   assign=v['assignments'], profiles=v['profiles']) for k, v in cl['by_k'].items()}
    rs = d['rule_sim']
    cols = rs['flags']['columns']
    last = len(rs['decision_points']) - 1
    want = {'c_5cd_notempty', 'b_5cd'}
    flags = [dict(zip(cols, r)) for r in rs['flags']['rows'] if r[1] == last and r[0] in want]
    flags = [{k: f[k] for k in ('rule', 'slot', 'empty_at_decision', 'p0', 'expected', 'class_days_silent')} for f in flags]
    summ = [{k: s.get(k) for k in ('rule', 'description', 'flags', 'flags_with_outcome', 'contradicted', 'contradicted_share',
                                    'sold_after_refill', 'sold_after_refill_share', 'silent_next_week', 'flagged_now')}
            for s in rs['summary'] if s['rule'] in ('a_7cal', 'b_5cd', 'c_5cd_notempty', 'd_p05_notempty')]
    # Visits (door counter) and refill evidence per slot. Pair proofs (a refill at one of two visits) are added per slot.
    VK = ('id', 'start', 'end', 'weekday', 'evidence', 'openings', 'proven_slots', 'pair_proof_slots', 'resumed_slots', 'note')
    visits = [{k: v.get(k) for k in VK} for v in d['visits']]
    rev = d['refill_evidence']
    per = {sid: [dict(x) for x in lst] for sid, lst in rev['per_slot'].items()}
    for v in d['visits']:
        for sid in v['pair_proof_slots']:
            per.setdefault(sid, []).append({'visit': v['id'], 'kind': 'pair proof'})
    for sid in per:
        per[sid].sort(key=lambda x: (x['visit'], x['kind']))
    lpr = d['last_proven_refill']
    refill = dict(method=rev['method'], capacity=rev['capacity_max'], silent_days=rev['silent_days'], resume_days=rev['resume_days'],
                  likely_min=rev['likely_min'], counts=rev['counts'], per_slot=per,
                  # the reference visit is the last refill PROVEN at a single visit; later visits refilled only some of our rails
                  last=dict(statement=lpr['statement'], proven_visit=lpr['visit'], proven_slots=lpr['slots'],
                            restock_visit=lpr['visit'], max_after_restock=lpr['max_units_one_slot_after'],
                            pair_visits=[x['visit'] for x in (lpr.get('later_pair') or {}).get('visits', [])],
                            pair_slots=(lpr.get('later_pair') or {}).get('slots', []),
                            topups=[v['id'] for v in d['visits'] if v['id'] > lpr['visit']
                                    and v['evidence'] in ('proven refill', 'likely refill', 'possible top-up')]))
    pc_ = d['photo_check']
    photo = dict(note=pc_['note'], visit=pc_['visit'], at=pc_['at'], agreement=pc_['agreement'], no_conflict=pc_['no_conflict_share'],
                 slots={x['slot']: dict(photo=x['photo'], inferred=x['inferred'], verdict=x['verdict'], after=x['units_after_photo_before_next_visit'])
                        for x in pc_['slots']})
    # State at the last poll, from the stock-out windows still open at the end of the data.
    STATE = {'sold out, not refilled': 'out', 'silent since before': 'empty'}
    open_w = {w['slot']: w for w in d['stockouts']['windows'] if w['how'] == 'ongoing'}
    for s in slots:
        w = open_w.get(s['slot'])
        if s['units'] == 0:
            s['state'] = 'never'
        elif w:
            s['state'] = next((v for k, v in STATE.items() if w['status'].startswith(k)), 'empty')
        else:
            s['state'] = 'selling'
        s['state_since'] = w['start'] if w else None
    assert all((s['state'] in ('out', 'empty')) == s['empty_now'] for s in slots), 'state disagrees with empty_now'
    m = d['meta']
    cal = d['calendar']
    last_day = m['last_poll'][:10]
    ahead = dict(
        days=[dict(date=x['date'], dow=x['dow'], cd=int(x['classes'] == 'normal' and x['dow'] < 5), lab=x['label'])
              for x in cal['days'] if x['date'] > last_day],
        breaks=[dict(start=e['start'], end=e['end'], label=e['label'], type=e['type'], institute=e['institute'])
                for e in cal['events'] if e['end'] > last_day and e['type'] in ('break', 'holiday')])
    # Products from the dated photos, our slots only (the analysis script never exports any other slot).
    pr = d['products']
    ours = set(m['our_slots'])
    assert ours == set(d['scope']['our_slots']), 'meta.our_slots and scope.our_slots disagree'
    assert set(pr['timeline']) <= ours and set(pr['runs']) <= ours, 'products carry a slot that is not ours'
    TK = ('photo', 'at', 'earliest', 'latest', 'photo_confidence', 'product', 'brand', 'category', 'caffeine', 'stock_seen', 'read_confidence')
    products = dict(note=pr['note'], caveats=pr['caveats'], photos=pr['photos'], basis=pr['basis_units'], total=pr['units_total'],
                    timeline={sid: [{k: x.get(k) for k in TK} for x in lst] for sid, lst in pr['timeline'].items()},
                    runs=pr['runs'], by_brand=pr['by_brand'], by_category=pr['by_category'], by_caffeine=pr['by_caffeine'])
    out = dict(
        scope=labkit.scope_of(d),
        meta=dict(first_poll=m['first_poll'], last_poll=m['last_poll'], polls=m['polls'], totals=m['totals'], regular_from=m['regular_polling_from'],
                  machine_totals=m['machine_totals'], our_slots=m['our_slots']),
        days=days, slots=slots, sd=d['slot_daily']['slots'], hist=pc['history'], changes=changes,
        pc=dict(event_date=pc['event_date'], machine_trend_ratio=pc['machine_trend_ratio']),
        windows=d['stockouts']['windows'], reg=d['slot_rowcol_regression'],
        clusters=dict(features=cl['features'], notes=cl['feature_notes'], method=cl['method'], k1=cl['k1_inertia'], best=cl['best_k_by_silhouette'],
                      byk=byk, feat=cl['slot_features']),
        rules=dict(decision=rs['decision_points'][last], decision_display=rs['decision_display'][last], decision_note=rs.get('decision_note'),
                   summary=summ, flags=flags),
        ahead=ahead, visits=visits, refill=refill, photo=photo, visits_source=m.get('visits_source'), products=products)
    # Scope guard: every per-slot value in the lab must belong to one of our slots.
    bad = set()
    for s in slots:
        bad |= {s['slot']} - ours
    bad |= set(out['sd']) - ours
    for c in changes:
        bad |= {c['slot']} - ours
    for w in out['windows']:
        bad |= {w['slot']} - ours
    for f in flags:
        bad |= {f['slot']} - ours
    for v in visits:
        for k in ('proven_slots', 'pair_proof_slots', 'resumed_slots'):
            bad |= set(v.get(k) or []) - ours
    bad |= set(per) - ours
    bad |= set(out['hist']) - ours
    bad |= set(photo['slots']) - ours
    bad |= set(cl['slot_features']) - ours if isinstance(cl['slot_features'], dict) else set(x['slot'] for x in cl['slot_features']) - ours
    for b in pr['by_brand'] + pr['by_category'] + pr['by_caffeine']:
        bad |= set(b['slots']) - ours
    if bad:
        raise SystemExit('slot-lab: lab data carries slots that are not ours: %s' % sorted(bad))
    return out


def main():
    args = labkit.parse_args("slot-lab", __doc__.splitlines()[0])
    d = labkit.load_lab_data(args.lab_data)
    data = labkit.write_json(Path(args.out) / "data.json", lab_cut(d))
    t = labkit.read("slotlab_template.html", labkit.scope_of(d))
    assert t.count('__DATA__') == 1
    page = t.replace('__DATA__', data)
    idx, std = labkit.write_pages(args.out, page)
    print(f"slot-lab: {idx} ({len(page)} chars), {std.name}")


if __name__ == "__main__":
    main()
