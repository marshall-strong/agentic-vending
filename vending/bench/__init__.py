"""The standing benchmark: the same test cases, the same judge, run after every change.

    python -m vending.bench run --dry-run
    python -m vending.bench run --note "prompt: ask for a threshold"
    python -m vending.bench report
    python -m vending.bench report --history 10
    python -m vending.bench list
    python -m vending.bench import --dir sweep-results/20261111-173503 --grades .../grades.json

Every run answers the team's test cases with a few chosen configs (model @
effort) through the model sweep, has a model judge every plan blind against the
case's good answer, and stores all of it in `bench_runs` and `bench_results`,
with the git commit and hashes of the prompt, the cases and the rubric. The
report then says, per config, whether this run is better or worse than the last
comparable one, lists what regressed, and names what changed in between.

See docs/benchmark.md.
"""
