# Benchmark suite

Tasks, hidden tests, experiment records and results for benchmarking the
replicable plugin against one dataset at a pinned commit. The runner lives in
the plugin repository (`bench/run.py`); its documentation is
`docs/benchmarking.md` there.

**Keep this repository private when the dataset is private.** Tasks, hidden tests
and reference solutions are derived from the dataset's code. Raw transcripts
are never committed: they stay in the bench cache (`~/.cache/replicable-bench/`).

```
suite.json            dataset (repo, pinned sha, setup), defaults, arms, tiers, delivery
tasks/<id>/           task.json, prompt.md, hidden/, solution.patch, rubric.md (optional)
experiments/          NNNN-slug.md per harness change; README.md is the log
results/runs.jsonl    one row per run, append-only (aggregates only, no code)
results/labels.jsonl  manual failure-label corrections, append-only
results/plans/        which runs belong to which experiment
results/reports/      generated reports and dashboards
```

Common commands (from the plugin repository):

```bash
python3 bench/run.py validate --suite <this dir> --grade
python3 bench/run.py new-experiment --suite <this dir> my-change --hypothesis "..."
python3 bench/run.py run --suite <this dir> --tier standard --experiment NNNN-my-change
python3 bench/run.py report --suite <this dir> --experiment NNNN-my-change --html results/reports/NNNN.html
```

Tasks are frozen once used: to change one, bump its `version` (results of
different versions are never mixed).
