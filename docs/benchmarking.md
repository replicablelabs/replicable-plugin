# Benchmarking the harness

Does a change to the plugin make Claude Code better at real work? The
benchmark answers that with numbers. It runs a fixed set of tasks against one
pinned commit of a repository (the **dataset**), under several harness
configurations (**arms**), and records **every** run: resolved, unresolved,
looping, crashed.

The runner (`bench/`) lives here and works with any dataset. A dataset's
**suite** (its tasks, hidden tests, experiment records and results) lives in its
own repository, private when the dataset is private.

- [Concepts](#concepts)
- [Metrics](#metrics)
- [How each metric is measured](#how-each-metric-is-measured)
- [Failure labels](#failure-labels)
- [Writing a suite](#writing-a-suite)
- [Running, reporting, recording experiments](#running-reporting-recording-experiments)
- [Isolation and fairness](#isolation-and-fairness)
- [Privacy](#privacy)
- [What the Claude Code transcript gives us (verified)](#what-the-claude-code-transcript-gives-us-verified)
- [Limits](#limits)

## Concepts

| Term | Meaning |
| --- | --- |
| **Dataset** | A git repository at one pinned commit. Every run starts from a fresh checkout of it. |
| **Task** | A change to make, written as a teammate would file it, with hidden acceptance tests and a reference solution. |
| **Arm** | One harness configuration: which plugin version (or none), how the task is prompted, what setup runs first. |
| **Trial** | One run of one task under one arm. Agents vary between runs, so each cell gets several trials. |
| **`candidate`** | The plugin version under test. By default this is the working tree of this repository, copied when the run starts and recorded with its commit and a dirty flag. `--candidate <ref>` picks any ref. |
| **`previous`** | The newest `v*` tag that is not the candidate. A clean HEAD that carries a tag is the candidate, so its predecessor is used. `--previous <ref>` overrides it. |
| **Experiment** | One harness change, one record: hypothesis, change, results, verdict. Runs carry the experiment's id. |

Default arms (`bench/suite.py`, overridable per suite):

| Arm | Plugin | Prompt | Setup before the agent starts |
| --- | --- | --- | --- |
| `baseline` | none | raw | none |
| `previous-raw` / `candidate-raw` | previous / candidate | raw | `graphify claude install`, `graphify update .`, test gate on |
| `previous-skill` / `candidate-skill` | previous / candidate | wrapped: `/replicable:<skill> <prompt>` (default `epcc`, per task `skill`) | same |

The raw arms ask whether the plugin helps when the model is left to choose its
own workflow. The skill arms measure the workflow the plugin intends. Comparing
the two separates what the hooks contribute from what the skills contribute. To
isolate graphify's contribution from the plugin's, add a suite arm such as
`{"plugin": null, "prompt": "raw", "setup": ["graphify_integration", "graph"]}`.

**Grading follows SWE-bench.** Each task's hidden tests are in two groups:

- **FAIL_TO_PASS:** tests that fail at the pinned commit and must pass after the change. They prove the task was done.
- **PASS_TO_PASS:** tests that pass at the pinned commit and must still pass. They prove nothing else broke.

A run is **resolved** when every selection in both groups passes.

## Metrics

Every run appends one row to `results/runs.jsonl`.

**Outcome**

| Metric | Definition |
| --- | --- |
| `resolved` | FAIL_TO_PASS and PASS_TO_PASS all pass |
| pass@1 | Chance one attempt resolves a task: the mean over trials, averaged over tasks |
| pass^k | Chance that **all** k independent attempts resolve it, C(c,k)/C(n,k) per task (reliability) |
| pass@k | Chance that at least one of k attempts resolves it |
| `grade.f2p_pass_rate` | Share of FAIL_TO_PASS tests passing (needs JUnit output) |
| `grade.p2p_broken` | PASS_TO_PASS tests the change broke |
| `false_done` | The agent finished normally, believing it was done, and the task is not resolved |
| `failure_label` | Why an unresolved run failed (see [Failure labels](#failure-labels)) |

**Efficiency, totals** (`totals`)

| Metric | Definition |
| --- | --- |
| `wall_ms` | Time from launching `claude` to its exit, measured by the runner |
| `api_ms`, `tool_ms`, `hook_ms` | Model time, main-thread tool time, time inside hooks |
| `turns`, `requests`, `tool_calls` | Size of the run |
| `tokens` | input, cache read, cache write and output tokens, all threads including subagents; `tokens_main` is the main thread only |
| `cost_usd` | The cost Claude Code reports for the run |
| `context_peak` | Largest main-thread context (input + cache read + cache write) |
| `compactions`, `subagents`, `permission_denials` | Counts |
| `setup_ms` | Dataset setup plus arm setup, kept apart because it is a one-off cost in real use |

**Efficiency, attributed** (method below)

- `tools["<Tool>"]` and `tools["<Tool>:<detail>"]`: calls, errors, time (total, p50, p95), hook time, output tokens, result tokens, re-read ("rent") tokens. The detail is privacy-safe: the skill name, the subagent type, the program and subcommand of a shell command (`git status`, `python -m pytest`), or a file's extension.
- `skills["<skill>"]`: invocations, wall time of its span, requests, tokens, cost, tool calls, subagents. `(no skill)` covers work outside any skill.
- `subagents["<type>"]`: count, wall time, requests, tokens, cost, tool calls.

**Harness behaviour**

- `hooks["<hookName>"]`: every hook Claude Code reported (the plugin's and graphify's), with runs, errors, latency and the characters it added to the context.
- `plugin_hooks["<hook>"]`: every run of the plugin's own hooks from the trace, including silent ones, with latency and `outcome:reason` counts.
- `gate`: test-gate runs, blocks, passes, whether the cap was hit, whether a block was followed by a fix, and time spent in tests.

**Delivery proxy** (`delivery`, only with `--delivery`)

Time and image size for each build, plus pass/fail and time of the integration
suites the task lists. It is reported apart from the harness score. Delivery
happens downstream of the harness, but a harness that writes code which builds
slowly or breaks integration tests is worse, and this is where that shows up.
It is local only: it never pushes and never deploys.

## How each metric is measured

There are three sources, joined by session id.

1. **The Claude Code session transcript.** The runner chooses the session id (`--session-id`) and copies the transcript out of `~/.claude/projects/<slug>/` after the run. It provides tokens and timestamps for every request, every tool call and result, `Skill` and `Agent` calls, subagent transcripts, and every hook that produced output, with its duration.
2. **The plugin's hook trace** (`REPLICABLE_TRACE`, written by `hooks/_common.py`). It writes one line per hook run, **including silent ones**, which the transcript omits, with an outcome (`skip`, `context`, `pass`, `block`, `gave_up`, `error`) and a reason code (`docs`, `no_graphify`, `reported`, `tests_fail`, `cap_hit`, …). It records reason codes and counts only, never output. The trace is off unless the variable is set. `REPLICABLE_TRACE=1` writes to the data dir's `trace.jsonl`; any other value is the file to write.
3. **The grader and the delivery proxy:** exit codes and JUnit XML from the hidden tests, plus build and compose timings.

The stream-json `result` event supplies the authoritative cost, duration, turn
count and stop reason. The `init` event records which plugins, MCP servers,
skills and agents were loaded, which proves what each arm actually ran with.

### Attribution

Measured values and estimates are kept apart. The estimates are consistent
between arms, which is what a comparison needs.

| Quantity | Rule | Kind |
| --- | --- | --- |
| Tool time | `tool_result` timestamp − `tool_use` timestamp, minus the duration of hooks that ran for that call (`toolUseID`) | measured; parallel calls overlap, so per-tool times can add up to more than wall time |
| Tool output tokens | The output tokens of the request that issued the call, split evenly over its tool calls | estimate |
| Tool result tokens | How much the next request's context grew (Δ context − previous output), split over the results and hook context that arrived in between, by size. Falls back to characters / 4 | estimate |
| Tool rent tokens | Result tokens × the number of later requests in the same context: what re-reading a large output costs for the rest of the run | estimate |
| Skill span | From a skill's start (slash command in the prompt, or a `Skill` call) to the next skill's start or the end of the run. Requests, tool calls and subagents inside it belong to it, and a subagent's requests belong to the span it was started in | rule |
| Cost per request / skill / subagent | Each model's reported cost, split over its requests by price-weighted tokens (input 1, 5-minute cache write 1.25, 1-hour cache write 2, cache read 0.1, output 5) | estimate; sums to the reported cost |

### Statistics

- **Trials:** 3 per cell (`standard`), 5 for releases. The report takes the latest row per run key.
- **Comparisons are paired by task:** A − B per task, averaged over tasks, with a 95% bootstrap interval (2,000 resamples of tasks, then of trials within each task). An interval that spans 0 means "no detectable change", not "no change".
- **Infrastructure errors** (setup failed, the runner crashed, the grader timed out) are left out of every score and counted separately.

## Failure labels

Exactly one label per unresolved run: the first rule that matches, in this order.
`bench/run.py label <run-id> <label>` overrides a label after reading the
transcript. Overrides are append-only in `results/labels.jsonl`, and reports
show the corrected label.

| Label | Rule |
| --- | --- |
| `infra_error` | Setup or the runner failed, the transcript is missing, or the grader timed out. Not scored |
| `timeout` | The run hit the wall-clock limit |
| `budget_exhausted` | `--max-budget-usd` stopped it |
| `context_exhausted` | Stopped by context or output limits |
| `hook_timeout` | A hook was cancelled or timed out |
| `hook_error` | A hook errored (transcript, Stop summary or trace) |
| `graphify_unavailable` | A plugin arm ran without graphify |
| `gate_cap_hit` | The test gate gave up after its retry cap |
| `agent_gave_up` | No change to the code at all |
| `broke_other_tests` | FAIL_TO_PASS passes, PASS_TO_PASS does not |
| `incomplete` | Some FAIL_TO_PASS tests pass |
| `wrong_fix` | None of them pass |

`false_done` is recorded beside the label, not as a label, because it applies to
any of the last four.

## Writing a suite

```bash
python3 bench/run.py init-suite ../replicable-bench --repo git@github.com:org/dataset.git --sha <40-char commit>
```

`suite.json` fields:

| Field | Meaning |
| --- | --- |
| `dataset.repo`, `dataset.sha` | Git URL or path, and the full commit every run starts from |
| `dataset.setup` | Commands run in every fresh checkout (e.g. `uv sync --extra test`), timed |
| `defaults.model` | **A full model id**, not an alias, so a model release does not change the benchmark silently |
| `defaults.effort`, `trials`, `budget_usd`, `timeout_s` | Per run; tasks can override budget and timeout |
| `defaults.permission_mode`, `allowed_tools`, `disallowed_tools` | What the agent may do unattended. `git push` and `gh` are always denied |
| `defaults.skill` | The workflow skill for `*-skill` arms (tasks can override with `skill`) |
| `defaults.test_command` | How the grader runs a selection: `{tests}` becomes the shell-quoted selections, `{junit}` a JUnit XML path. Example: `.venv/bin/python -m pytest -q -p no:cacheprovider {tests} --junitxml={junit}` |
| `defaults.harness_test_command` | What the plugin's test gate runs, written to `.claude/replicable.json` and `LAB_TEST_CMD`, as a real project would configure it |
| `defaults.env` | Extra environment for setup, agent and grader |
| `arms`, `tiers` | Added to or overriding the defaults |
| `delivery` | Builds and compose integration suites for the delivery proxy (`bench/delivery.py`) |

A task is a directory `tasks/<id>/` (directories starting with `_` are ignored):

| File | Meaning |
| --- | --- |
| `task.json` | `tier` (`L1`, `L2`, `L3` or `adv`), `version`, `fail_to_pass`, `pass_to_pass`, optional `issue`, `issue_comments` (default true), `skill`, `tags`, `test_command`, `setup`, `delivery`, `budget_usd`, `timeout_s` |
| `prompt.md` | What a teammate would write: the behaviour and why, never the solution, never the hidden tests' names. Optional when the task names an issue |
| `hidden/` | Acceptance tests at their repository paths, copied in only for grading. They overwrite anything the agent wrote at the same path |
| `solution.patch` | A reference change from the pinned commit, without the hidden tests |
| `rubric.md` | Optional: what a reviewer would check beyond the tests |

**Tasks from GitHub issues.** A task can name an issue of the dataset's repository: `"issue": 826` in
`task.json`. The issues a suite uses are frozen into the suite, so the issue's text can't change
under a task when someone edits or comments on it:

```bash
python3 bench/run.py snapshot-issues --suite ../replicable-bench 826 35    # or no numbers: whatever the tasks reference
```

- **Storage.** Each issue, with its comments, goes in `dataset/issues.jsonl`. It is fetched read-only with `gh` and timestamped.
- **Frozen.** An issue already frozen is kept as it is; `--refresh` fetches it again.
- **The prompt.** Without a `prompt.md`, the task's prompt is the issue as frozen: title, labels, body and comments. The issue's content is part of the task's hash, so a refreshed issue whose text changed counts as a different task.
- **Privacy.** The snapshot drops the "Report diagnostics" block that in-app reports append (reporter identity, environment) and redacts email addresses.
- **Images.** Images in issues stay links that the agent cannot open, so describe in `prompt.md` anything the agent must see.

`validate --grade` proves each task is sound. FAIL_TO_PASS must fail and
PASS_TO_PASS must pass at the pinned commit, and both must pass with
`solution.patch` applied. A task that fails either check measures nothing.

**Tasks are frozen once used.** Editing one changes its content hash, so earlier
runs no longer match its run key. To change a task deliberately, bump its
`version`.

**A good task set** stresses the harness on purpose:
- L1 single-file fixes, where harness overhead shows up as pure cost.
- L2 changes across several files, where the graph should help.
- L3 cross-cutting refactors with ripple effects.
- Adversarial tasks: no nearby tests, a flaky test, a misleading description, a change that should stay small.

Mining tasks keeps them realistic. Pin the dataset at commit X, then use pull
requests merged after X that apply to X and carry tests: their tests become the
hidden tests and their diffs the reference solutions.

## Running, reporting, recording experiments

```bash
# 1. Start a record for the change (writes experiments/NNNN-slug.md and a line in experiments/README.md)
python3 bench/run.py new-experiment --suite ../replicable-bench gate-on-commit --hypothesis "..."

# 2. See what would run and what it may cost, then run it
python3 bench/run.py run --suite ../replicable-bench --tier standard --experiment 0003-gate-on-commit --dry-run
python3 bench/run.py run --suite ../replicable-bench --tier standard --experiment 0003-gate-on-commit --max-cost-usd 150

# 3. Report: Markdown for the record, HTML for the dashboard
python3 bench/run.py report --suite ../replicable-bench --experiment 0003-gate-on-commit \
    --md results/reports/0003.md --html results/reports/0003.html
```

| Tier | Tasks | Trials | Arms | Use |
| --- | --- | --- | --- | --- |
| `smoke` | 3 (one per tier first) | 1 | baseline, candidate-raw | Does the pipeline work? |
| `standard` | all | 3 | all five | Every harness change |
| `release` | all | 5 | all five | Before tagging a version |

**Runs are reused.** A run whose key matches an earlier run is not repeated. The key covers the task and its version, the dataset commit and setup, the model, effort, Claude Code and graphify versions, the arm (including the plugin's content hash) and the trial. So `baseline` and `previous` are paid for once until something they depend on changes. `--rerun` forces new runs. Each experiment's plan (`results/plans/<id>.json`) lists every key it covers, reused or not, so its report includes them.

**The dashboard** (`--html`) is one self-contained file with no network access:
- a "compare A with B" filter, with headline tiles (pass@1, pass^k, cost, time, false done, with intervals)
- a per-task dumbbell, regressions first
- a trend of pass@1 and cost across every plugin version ever run, with the baseline as reference
- where time and tokens go: per tool, skill, subagent and hook, A beside B
- failure labels per arm
- every run, infrastructure errors included, with its local run directory

The same structure is used by hosted eval tools (Braintrust and LangSmith experiment comparisons, Langfuse and Phoenix traces). The files are plain JSONL, so they can be loaded into a self-hosted one later. A hosted one would receive the dataset's code inside the transcripts.

**Record every change, not just the wins.**
- The experiment record holds the hypothesis, the change, the expected and guardrail metrics, the generated results, the failure analysis and a verdict (`keep`, `revert` or `inconclusive`).
- Reverted and inconclusive experiments stay in `experiments/README.md`.
- `runs.jsonl` is append-only.
- Every row carries the plugin's commit and content hash, so any number traces back to the exact code.

**Before a release:** run the `release` tier against the candidate, write the experiment record, then bump the version and tag.

## Isolation and fairness

Each run gets:

- **A fresh checkout.** It is a detached worktree of a cached mirror at the pinned commit, at a unique path, so Claude Code's per-project state (transcripts, auto-memory) never carries between runs. The worktree is removed afterwards (keep it with `--keep-worktrees`). The mirror's `info/exclude` keeps `graphify-out/` and Python bytecode out of setup's commit and the agent's measured diff, even when the dataset does not ignore them.
- **A snapshot of the plugin.** Each plugin version is copied into the bench cache once, so edits made while a benchmark runs do not leak into it.
- **Fresh plugin state:** `HARNESS_DATA=<run>/harness-data`.
- **No caller configuration.** The runner removes `HARNESS_*`, `LAB_*`, `REPLICABLE_TRACE` and `GRAPHIFY_BIN` from its environment. The agent runs with `--setting-sources project`, which excludes user settings, user hooks and user-installed plugins, and with `ENABLE_CLAUDEAI_MCP_SERVERS=false`, which excludes the account's claude.ai connectors. The plugin's own MCP server stays. (`--strict-mcp-config` would drop it, and `--bare` would skip all hooks, so neither is used.)
- **A bench git identity** (`replicable-bench <bench@replicable.invalid>`), so agent commits never use yours.
- **No way out to the dataset's repository.** The mirror's push URL points nowhere, so any `git push` from a run fails. This matters because a mirror pushes every ref, and the dataset may deploy on push. The agent is also denied `git push` and `gh`, runs with an empty gh configuration and no GitHub token variables, and git never prompts for credentials.

Fairness between arms:

- **Random interleaving.** The (task, arm, trial) list is shuffled with a recorded seed, so drift in API latency or load hits every arm alike.
- **Concurrency 1 by default,** because wall-clock time is sensitive to contention.
- **Organisation-managed settings** apply to every arm; their hash is recorded.
- **Built-in plugins** Claude Code ships apply to every arm and are listed in the row's `init`.

## Privacy

- **The suite contains the dataset's code.** Tasks, hidden tests and solutions are derived from it, so keep the suite repository as private as the dataset.
- **`runs.jsonl` holds aggregates only.** Counts, timings, token totals, labels, tool names with privacy-safe details, and a short error message for infrastructure errors. No prompts, outputs, file contents or commands' arguments. The stream's credential fields are never copied.
- **Raw artifacts stay on the machine that ran them**, in `~/.cache/replicable-bench/runs/<suite>/<run-id>/` (`REPLICABLE_BENCH_HOME` overrides): stream, transcript, trace, agent patch, grader logs. They are never committed.

## What the Claude Code transcript gives us (verified)

These results come from headless runs (`claude -p --output-format stream-json --verbose`) on Claude Code 2.1.321 with this plugin loaded:

- **Where the transcript lands.** It is written to `~/.claude/projects/<cwd with every non-alphanumeric as "-">/<session-id>.jsonl`. Subagents land in `<session-id>/subagents/agent-<id>.jsonl` with an `agent-<id>.meta.json` naming `agentType` and the parent `toolUseId`.
- **Hooks.** Every hook that produced output appears as a `hook_success` attachment with `hookName`, `toolUseID` and `durationMs`, and its context as `hook_additional_context`. **Silent hook runs do not appear.** Stop hooks always appear in a `stop_hook_summary` with each hook's `durationMs`. This is why the plugin's own trace exists.
- **Requests.** Each `api-request` entry marks when a request started. The assistant entries of one message share `message.id` and carry `usage`; the last one holds the final usage.
- **Slash commands.** A prompt that starts with a slash command, such as `/replicable:epcc …`, appears as `<command-name>/replicable:epcc</command-name>`. No `Skill` tool call is made, so the skill span starts at the prompt.
- **The `result` event.** It carries `total_cost_usd` for all threads, `modelUsage` with per-model cost, `duration_ms`, `num_turns`, `terminal_reason` and `subagent_stats`. Its `usage` covers the main thread only.
- **Test-gate finding.** The `epcc` skill commits its work before stopping, so the test gate, which looks only at uncommitted changes, found nothing to check. The benchmark records this as `test_gate skip:no_code_changes` and it is worth an experiment.

The transcript format is internal to Claude Code and can change. The parser
(`bench/transcript.py`) reads only these fields, ignores everything else, and
every row records the Claude Code version and the parser version.

## Limits

- **Attribution estimates.** Token attribution to tools is an estimate (see [Attribution](#attribution)). Skill spans assume skills run one after another.
- **Counted, not detected.** Per-hook "precision" and "catch rate" ([hooks.md](hooks.md#hook-health-metrics-is-this-hook-pulling-its-weight)) need judgement: whether a block was justified, or whether a bug was caught. The benchmark counts blocks, fixes after blocks and cap hits. Seeded-bug tasks give catch rates.
- **Opt-in model grading.** The model-graded rubric is not part of the score yet. `rubric.md` is kept with each task so it can be added without changing tasks.
- **Delivery is approximate.** The delivery proxy measures a local build and local integration tests. Real deploy times would need a non-production environment and are out of scope.
