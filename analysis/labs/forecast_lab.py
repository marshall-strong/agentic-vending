"""Build the Vending Forecast Lab from analysis/out/lab_data.json.

    python analysis/labs/forecast_lab.py [--lab-data PATH] [--out DIR]

Writes DIR/data.json (the lab's cut of the data), DIR/index.html and DIR/standalone.html.
Default DIR: analysis/out/labs/forecast-lab. Sources: fl_template.html, fl_core.js, fl_ui.js.

The lab forecasts MACHINE-WIDE totals (an allowed aggregate). Anything per slot in it is ours only.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # so this works under python -I too
import labkit  # noqa: E402


def lab_cut(d):
    dl = d['daily']
    keep = ['date', 'dow', 'units', 'units_raw', 'cents', 'cents_raw', 'class_day', 'classes', 'institute', 'label', 'gap_day',
            'lump_units', 'empty_slots', 'active_slots', 'regular']
    bt = d['backtests']
    fc = d['forecasts']
    pc = d['photo_check']
    A = {c['term']: c for c in d['regression']['calendar']['models']['A']['coefs']}   # model A: machine-wide totals
    lpr = d['last_proven_refill']
    return dict(
        scope=labkit.scope_of(d),
        # this lab forecasts MACHINE-WIDE totals (an aggregate); 'ours' = our slots
        meta=dict(generated_at=d['meta']['generated_at'], device=d['meta']['device'], first_poll=d['meta']['first_poll'],
                  last_poll=d['meta']['last_poll'], polls=d['meta']['polls'],
                  totals=dict(d['meta']['machine_totals'], slots=None), ours=d['meta']['totals'], our_slots=d['meta']['our_slots'],
                  last_full_week=d['meta']['last_full_week'], regular_from=d['meta']['regular_polling_from'][:10],
                  gaps=len([g for g in d['meta']['gaps'] if g['hours'] >= 24])),
        daily={k: dl[k] for k in keep},
        decomp={k: d['decomposition'][k] for k in ['date', 'trend', 'seasonal', 'remainder', 'strength_trend', 'strength_seasonal']},
        dow=dict(index=d['dow_index']['index'], per_class_day=d['dow_index']['mean_units_per_class_day'], n=d['dow_index']['n_days']),
        weekly=[{k: w[k] for k in ['week', 'term_week', 'days', 'partial', 'units', 'units_raw', 'cents', 'cents_spread', 'class_days',
                                   'active_slots', 'slots_empty_at_end', 'gap_hours', 'lump_units']} for w in d['weekly']],
        supply=[{k: w[k] for k in ['week', 'units', 'slots_selling', 'slots_sold_out_not_refilled', 'slots_silent_since_before_last_restock',
                                   'refill_visits']} for w in d['supply']],
        bt=dict(origins=bt['origins'], models=bt['models'],
                per_origin=[{k: p[k] for k in ['origin', 'test_dates', 'actual', 'contains_no_class_weekday', 'ses_alpha', 'forecasts',
                                               'history_start', 'history']} for p in bt['per_origin']],
                average={m: {k: v[k] for k in ['mae', 'rmse', 'bias', 'wape', 'mae_no_break_windows']} for m, v in bt['average'].items()},
                mae_by_k=bt['mae_by_k_sma'], intervals=bt['intervals'],
                weekly_summary=bt['weekly']['summary'], partial=bt['weekly']['current_partial_week']),
        fc=dict(dates=fc['dates'], dow=fc['dow'], classes=fc['classes'], institute=fc['institute'], label=fc['label'],
                models=fc['models'], creg=fc['calendar_regression_coefficients'],
                restocked=dict(rates=fc['restocked_scenario']['recent_rates'], per_class_day=fc['restocked_scenario']['per_class_day_recent'],
                               per_class_day_era=fc['restocked_scenario']['per_class_day_regular_era']),
                supply_warning=fc['supply_warning'],
                weekly=fc['weekly'], interval_horizon_days=fc['interval_horizon_days']),
        reg=dict(no_class=A['no_class'], week=A['week_of_term'], r2=d['regression']['calendar']['models']['A']['r2'],
                 trend=d['regression']['trend_tests'][0]),
        events=[e for e in d['calendar']['events'] if e['end'] >= '2026-08-27'],
        visits=[dict(id=v['id'], day=v['end'][:10], start=v['start'], end=v['end'], weekday=v['weekday'], openings=v['openings'],
                     evidence=v['evidence'], proven=v['proven_slots'], pair=v['pair_proof_slots'], resumed=v['resumed_slots'], note=v['note'])
                for v in d['visits']],
        # the marker is the last refill PROVEN at a single visit; later visits refilled only some of our rails
        refill=dict(statement=lpr['statement'],
                    last_restock={k: lpr[k] for k in ['visit', 'start', 'end', 'weekday', 'evidence']},
                    last_single_proof=dict(visit=lpr['visit'], start=lpr['start'], end=lpr['end'], weekday=lpr['weekday'], slots=lpr['slots']),
                    max_after=lpr['max_units_one_slot_after'],
                    later_topups=lpr['later_possible_topups'], visits_source=d['meta']['visits_source'],
                    later_pair=lpr['later_pair']),
        rule7={k: next(r for r in d['rule_sim']['summary'] if r['rule'] == 'a_7cal')[k]
               for k in ['flags', 'flags_with_outcome', 'contradicted', 'sold_after_refill', 'silent_next_week', 'flagged_while_suspected_empty']},
        rule5={k: next(r for r in d['rule_sim']['summary'] if r['rule'] == 'c_5cd_notempty')[k]
               for k in ['flags', 'flags_with_outcome', 'contradicted', 'sold_after_refill']},
        rule5_verdict=next(v for v in d['rule_sim']['verdicts'] if v['filtered'] == 'c_5cd_notempty')['verdict'],
        photo=dict(at=pc['at'], visit=pc['visit'], rails=len(pc['slots']),
                   stocked=pc['photo_stocked'], tally=pc['tally'], missed=pc['empty_and_not_flagged'], conflict=pc['stocked_and_flagged_empty'],
                   flagged=sorted(pc['empty_and_flagged_empty'] + pc['stocked_and_flagged_empty'], key=int)),
    )


def main():
    args = labkit.parse_args("forecast-lab", __doc__.splitlines()[0])
    d = labkit.load_lab_data(args.lab_data)
    data = labkit.write_json(Path(args.out) / "data.json", lab_cut(d), allow_nan=False)
    sc = labkit.scope_of(d)
    page = (labkit.read("fl_template.html", sc).replace('/*DATA*/', data, 1)
            .replace('/*CORE*/', labkit.read("fl_core.js", sc), 1).replace('/*UI*/', labkit.read("fl_ui.js", sc), 1))
    assert '/*DATA*/' not in page and '</script' not in data
    idx, std = labkit.write_pages(args.out, page)
    print(f"forecast-lab: {idx} ({len(page)} chars), {std.name}")


if __name__ == "__main__":
    main()
