# Sales analysis

What the machine's sales history says about demand for **our 20 slots** (15-18, 25-28, 35-38, 45-48, 55-58), the academic calendar, slot position, price, products, and when a slot is really a slow seller. It uses standard forecasting practice: seasonal naive as the bar to beat, walk-forward backtests, and MAE as the headline error.

**Read [REPORT.md](REPORT.md) first.** It has the findings, the forecast backtest, the products our slots held, the slow-seller rule simulation and the guideline we recommend for the agent.

## Scope

Anything per slot (products, prices, position, segments, stock-outs, refill evidence, slow-seller flags, experiments) uses only our slots, read from `vending.slots.SLOTS` on `vending.slots.DEVICE_ID`, and no slot is ever judged by another slot's state (`docs/integrity.md`). Machine-wide data is used only as totals over the whole machine, and only for the calendar and day-off effects, the time-of-day and day-of-week profiles, the overall trend, the machine-total forecast and dating door visits. That history is from before the competition, when course staff stocked every slot, so it reflects no rival strategy. The export never writes a per-slot value of another slot, and the analysis stops if its lab data would carry one.

## The interactive labs

Three pages built on this analysis, as step-by-step labs: step tabs, toggles, sliders, a run log and live recalculation. Ask a project maintainer for access. Rails that are not ours are drawn hatched, labelled "Not our turf", with no values.

| Lab | Question it answers |
|---|---|
| Vending Forecast Lab | How many drinks next week, and how sure are we? Splits the machine-wide series into trend, weekly pattern and noise, shows why you can't split time-series data randomly, compares models live, backtests them and forecasts 4 weeks ahead over the calendar. |
| Campus Rhythm Lab | How do class days, breaks and the time of day drive sales? Includes the slow-seller rule simulator for our slots. |
| Slot and Price Lab | Where in the machine and at what price do our drinks sell? Our slots adjusted for stock-outs, shelf and column effects with price held fixed, price changes, slot segments, and experiments to try next. |

## Files

| File | What it is |
|---|---|
| `export_sales.py` | Reads the database (read-only) and writes `analysis/data/*.csv`: our slots' poll readings and sales events (filtered like `vending.ledger._counter_rows`), every poll time, `machine_hourly.csv` (units and cents the whole machine sold since the previous poll; totals only) and `door_events.csv` (the EVA-DTS `EA2*DO` door-open counter at every poll and its rise). |
| `sales_analysis.py` | The whole analysis in one script with fixed seeds. It writes `analysis/out/lab_data.json` (the labs' data) and `analysis/out/analysis_report.md`. |
| `visit_windows.py` | Dark periods: when the door opened, how long each visit window was, how often polls ran late, and how many of our sales fell inside a window. Reads the exported CSVs (and the visit grades in `lab_data.json` if present), standard library only, and writes `analysis/out/visit_windows.json`. See `docs/cutover.md`. |
| `golisano_calendar_2026_27.json` | The Golisano Institute's 2026-27 academic calendar, one entry per day from Aug 24 2026 to Jan 31 2027, with class days, breaks and closures, its sources, and the date it was last checked against the official page (`checked`). |
| `planograms_photos.json`, `planogram_2026-10-08.json` | What our slots held in the dated team photos (other slots are listed as `{"slot": ..., "ours": false}` with no values). The Oct 8 file also describes the snack machine next to ours. |
| `product_history.md` | Our slots' products over time, read from those photos. |
| `requirements.txt` | Exact package versions the analysis was last run with. |
| `REPORT.md` | The report from the last run, data through Nov 11 2026 6 PM. |

`analysis/data/` and `analysis/out/` are gitignored.

## Rerun it

You need read access to the database: a `.env` file in the repo root with `DATABASE_URL=...` (ask a project maintainer for the value; never commit it). Without it, step 1 fails and you can only rerun step 2 on CSVs someone shares with you in `analysis/data/`.

```powershell
# From the repo root.
# 1. Export (the project's venv; reads DATABASE_URL from .env). Writes analysis\data\*.csv
.venv\Scripts\python.exe analysis\export_sales.py

# 2. Analyse (its own venv with pinned versions, so the project's requirements stay small)
py -3 -m venv analysis\.venv
analysis\.venv\Scripts\python.exe -m pip install -r analysis\requirements.txt
analysis\.venv\Scripts\python.exe -I analysis\sales_analysis.py

# 3. Keep the report
Copy-Item analysis\out\analysis_report.md analysis\REPORT.md
```

The last line it prints is `all_ok: True` when everything reconciles: daily, weekly, slot-day, per-slot and product totals all equal our slots' sales events; machine-wide daily and weekly totals equal `machine_hourly.csv`; and, slot by slot, the last-minus-first of each lifetime counter (units and cents) in `poll_readings.csv`, with restarts and misreads handled as `vending.counters.rises` does, equals the slot's event totals. Any failure stops the script with `CHECKS FAILED`.

## Choices worth knowing

- **Weeks run Saturday to Friday**, as in the ledger and the P&L page (`vending/ledger.py`, `WEEK_STARTS = 5`). Forecast plan dates are Saturdays. Comparisons such as "slots selling, best week to now" use full weeks only, never the partial current week.
- **Polling gaps are missing data, not zero sales.** Before Sep 21 the machine was polled now and then, so part of the early sales landed on whichever poll ended a gap (the report gives the share and the largest lump). The script keeps a raw series and a series with those lumps spread over the gap.
- **Visits come from the door counter.** Every poll reports how many times the door has been opened; a rise between two polls is a visit (rises within 3 hours are one visit). Each visit is graded from our slots only: *proven refill* when one of our slots sold more than 11 units (the largest rail on the loading label) over a stretch with no other visit, *likely refill* when 2+ of our slots that looked empty started selling again within 4 days, *possible top-up* for 1, otherwise no sign. The counter says when the door opened, not what was loaded.
- **Regular refilling stopped in late October.** The operators stopped refilling to prepare for the project, so the late-term decline is mostly supply; the report reads demand from in-stock rates (the restocked scenario). By our slots' own counters some later visits still put product back in a few rails.
- **Stock-outs are inferred.** The machine reports what it sold, not what it holds. A slot counts as "suspected empty" once its silence is unlikely at its own rate on a calendar-time clock (P < 0.05). The window ends at a door visit, never at the slot's own next sale; if the slot sells again with no door opening in between, it was stocked. In the slow-seller simulation every door visit is a decision point (chosen without looking at later sales), and the empty check uses only what was known when the door opened. Logging units loaded at each visit would replace the guess with a fact. The Nov 9 photo is used as a check.
- **Class days, not calendar days.** The quarter break (Nov 18 to Dec 2), Thanksgiving and the winter holiday break are expected to sell almost nothing. They must not count toward "nothing moved for a week".
- **Prices.** The price paid on a sale is the listed price in effect when its poll interval began; the price on a day is the listed price at noon.
