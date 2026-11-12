"""Rebuild everything in one go: export, analysis, report, the three labs.

    python analysis/rebuild.py                          # normal run: our slots as stored/default
    python analysis/rebuild.py --slots 15,16,17,18,...  # only part of our slots (never another team's)
    python analysis/rebuild.py --no-export              # reuse the CSVs in analysis/data/

Run it with the analysis venv's python (pandas, statsmodels, ...). The export needs the project's
venv (psycopg) and DATABASE_URL in .env. It uses .venv in the repo root when there is one; point at
another python with --export-python.

Steps, each stops the run if it fails:
    1. analysis/export_sales.py        (skipped with --no-export)
    2. analysis/sales_analysis.py      writes analysis/out/lab_data.json and analysis_report.md
    3. analysis/labs/*_lab.py          write analysis/out/labs/<lab>/index.html and standalone.html
    4. copy analysis_report.md to analysis/REPORT.md (only when every check passed and the run covers all our slots)

Full output of every step goes to analysis/out/rebuild_log.txt. Exits 1 if a step fails or all_ok is false.
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ANALYSIS = Path(__file__).resolve().parent
REPO = ANALYSIS.parent
OUT = ANALYSIS / "out"
LABS = [("forecast-lab", "forecast_lab.py"), ("rhythm-lab", "rhythm_lab.py"), ("slot-lab", "slot_lab.py")]
TURF = "Not our turf"
TURF_PARTIAL = "Not in this run"
sys.path.insert(0, str(ANALYSIS))
from slots_config import SLOTS as ASSIGNED  # noqa: E402


def project_python():
    """The project's venv python (it has psycopg), or this python if there is no .venv."""
    for p in (REPO / ".venv" / "Scripts" / "python.exe", REPO / ".venv" / "bin" / "python"):
        if p.exists():
            return str(p)
    return sys.executable


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--slots", help="our slot codes, comma or space separated; passed to the export and the analysis")
    ap.add_argument("--no-export", action="store_true", help="skip the database export and use the CSVs already in analysis/data/")
    ap.add_argument("--export-python", default=project_python(),
                    help="python with psycopg for the export (default: the project's .venv if there is one, else this one)")
    ap.add_argument("--no-report-copy", action="store_true", help="leave analysis/REPORT.md as it is")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    log = (OUT / "rebuild_log.txt").open("w", encoding="utf-8")
    slots_arg = ["--slots", args.slots] if args.slots is not None else []
    steps = []
    if not args.no_export:
        steps.append(("export", [args.export_python, str(ANALYSIS / "export_sales.py")] + slots_arg))
    steps.append(("analysis", [sys.executable, "-I", str(ANALYSIS / "sales_analysis.py")] + slots_arg))
    steps += [(name, [sys.executable, "-I", str(ANALYSIS / "labs" / script)]) for name, script in LABS]

    results = []
    for name, cmd in steps:
        t0 = time.time()
        p = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace")
        log.write(f"===== {name}: {' '.join(Path(c).name if i < 2 else c for i, c in enumerate(cmd))}\n{p.stdout}{p.stderr}\n")
        log.flush()
        ok = p.returncode == 0
        results.append((name, ok, time.time() - t0))
        print(f"{'ok  ' if ok else 'FAIL'} {name} ({time.time() - t0:.0f} s)")
        if not ok:
            tail = (p.stdout + p.stderr).strip().splitlines()[-8:]
            print("\n".join("     " + line for line in tail))
            print(f"stopped. Full output: {OUT / 'rebuild_log.txt'}")
            return 1

    lab = json.loads((OUT / "lab_data.json").read_text(encoding="utf-8"))
    sc, checks = lab["scope"], lab["meta"]["checks"]
    all_ok = bool(checks.get("all_ok"))
    n = len(sc["our_slots"])
    rival = sc["machine"]["shelves"] * sc["machine"]["columns"] - n
    partial = set(sc["our_slots"]) != set(ASSIGNED)     # a narrowed --slots run
    turf = TURF_PARTIAL if partial else TURF
    print()
    print(f"slots     {n} on {sc['device']}: {sc['slots_text']}")
    print(f"source    {sc['source']}")
    print(f"          {sc['source_line']}")
    print(f"all_ok    {all_ok}")
    print(f"data      {lab['meta']['first_poll']} to {lab['meta']['last_poll']}, {lab['meta']['polls']} polls")
    if partial:
        print(f"narrowed  {n} of our {len(ASSIGNED)} slots; the other {rival} rails are hatched as '{turf}' with no values")
    else:
        print(f"not ours  {rival} rails, drawn hatched as '{turf}' with no values")
    for name, _ in LABS:
        page = (OUT / "labs" / name / "index.html").read_text(encoding="utf-8")
        print(f"{name:13s} {OUT / 'labs' / name / 'index.html'}  (+ standalone.html)  '{turf}' in source: {page.count(turf)}")
    if partial:
        print(f"report    left as is: a narrowed run's report stays in {OUT / 'analysis_report.md'}")
    elif all_ok and not args.no_report_copy:
        shutil.copyfile(OUT / "analysis_report.md", ANALYSIS / "REPORT.md")
        print(f"report    copied to {ANALYSIS / 'REPORT.md'}")
    print(f"log       {OUT / 'rebuild_log.txt'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
