# {id}

- **Date:** {date}
- **Status:** open  <!-- open | keep | revert | inconclusive -->
- **Candidate:** replicable {plugin_version} @ {plugin_sha} (fill in the PR or commit range)
- **Compared with:** previous release and the no-plugin baseline

## Hypothesis

{hypothesis}

## Change

What changed in the harness, and why it should move the metrics below.
Link the PR or commits.

## Metrics

| Metric | Expected | Guardrail (must not get worse) |
| --- | --- | --- |
| pass@1 | | |
| median cost per run | | |
| median time per run | | |
| | | |

## Run

```bash
python3 bench/run.py run --suite . --tier standard --experiment {id}
python3 bench/run.py report --suite . --experiment {id} --md results/reports/{id}.md --html results/reports/{id}.html
```

Pinned for this experiment: model, effort, Claude Code version, graphify version
and dataset commit are recorded on every row; the report lists them.

## Results

Paste or link `results/reports/{id}.md`. Note every regression, not only the gains.

## Failure analysis

Read the transcripts of the runs whose label changed between arms
(`run_dir` in each row). Correct wrong automatic labels with
`bench/run.py label`, and say here what you learned.

## Verdict

keep / revert / inconclusive, in one or two sentences, and what to try next.
