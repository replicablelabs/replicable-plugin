# Stages and universal skills (design)

> **Status: design, for review (U1 of #2).** Nothing on this page is built
> yet. It fixes what U2-U6 build: the stages, every universal skill, the
> plan's estimate and route, and the handoff between stages. The
> [decisions](#decisions-for-review) at the end need the maintainer's answer
> before any skill is written (U3).

A coding agent's work is opaque: a session succeeds or not and costs what it
costs. This design makes the work visible and steerable. The agent works
through **named, universal skills** grouped into **stages**, each skill
invocation is a measured span with a recorded outcome, and stages hand over
through a **validated handoff** instead of a growing transcript.

- [Stages](#stages)
- [How the user works with the harness](#how-the-user-works-with-the-harness)
- [Verification: the project's procedure](#verification-the-projects-procedure)
- [Anatomy of a skill](#anatomy-of-a-skill)
- [Which skills each stage allows](#which-skills-each-stage-allows)
- [The skills](#the-skills)
- [The plan's estimate and route](#the-plans-estimate-and-route)
- [Transitions, loops and caps](#transitions-loops-and-caps)
- [Handoffs](#handoffs)
- [Project slots and the project's skills](#project-slots-and-the-projects-skills)
- [Mechanisms (confirmed in Claude Code's docs)](#mechanisms-confirmed-in-claude-codes-docs)
- [What gets measured](#what-gets-measured)
- [Existing skills, agents and hooks](#existing-skills-agents-and-hooks)
- [Decisions for review](#decisions-for-review)

## Stages

```
 interactive                headless
issue ─► Understand ─► Plan ─► Execution ─────────────────────────────────► (Learn)
                                 single-stage: one session, every execution skill
                                 multi-stage:  Build ⇄ Verify ─► Deliver
```

| Stage | Runs | Input | Output | Decides |
| --- | --- | --- | --- | --- |
| **Understand** | Interactive, with the user | A request: an issue, a message | The issue, refined into a spec (problem, goals, design, required design choices, non-goals, acceptance criteria) | What "done" means |
| **Plan** | Headless, always | The refined issue, the code graph | A plan comment on the issue: units, files, tests, checks, the measured estimate and the route, in a `replicable-execution` block | **The route**, and nothing else about execution |
| **Build** | Headless | The plan (and Verify's findings when sent back) | A change in the worktree that the unit checks pass | How each unit is implemented |
| **Verify** | Headless | The whole change, the spec | A verdict: every acceptance criterion met, or findings for Build | Whether the change is done |
| **Deliver** | Headless | The verified change | Commits on the work branch; the results of any project skills it ran | How the change is recorded |
| **Learn** | Optional, after delivery | The session traces | Proposals: new skills, slots, wiki entries. A human approves each | What to improve |

Single-stage runs Build, Verify and Deliver's skills in one session. Multi-stage
runs one loop per stage, with a handoff between them. A run can **re-route**
from single-stage to multi-stage, or **escalate** to a human, when reality
diverges from the plan's estimate ([signals](#re-route-and-escalation)).

Two continuity skills, `handoff` and `status`, are available in every
headless stage.

## How the user works with the harness

```
/replicable:refine 123   (interactive; from a shell: claude "/replicable:refine 123")
   refine with the user ─► spec on the issue ─► the runner starts, detached
                                                       │
            Plan ─► Execution   (headless)             │
                                                       ▼
   questions and results ─► the issue (always) and the refine session (while it is open)
```

- **One entry point.** `/replicable:refine <issue>` is a user-only skill that
  runs Understand with the user. When the user approves the spec, it writes
  the spec to the issue and starts the runner for that issue, **detached**,
  so closing the session or the terminal does not stop the run. All the
  run's state is on disk (handoffs, check records), never in a session.
- **The runner never waits on a person.** When it needs an answer (the
  verification procedure is not in place, a gap Plan found, a cap hit, an
  escalation), it stops the run and writes the question into the handoff.
- **The issue is the durable channel.** Every question, and the final result
  (the branch, or the PR when the runner delivers), is posted as a comment,
  so GitHub's notifications reach the user wherever they are.
- **The refine session is the live channel.** While it is open, it watches
  the runner's event log and surfaces each question there; the user answers
  in place.
- **Answering later.** `/replicable:resume <issue>`, in any session, reads the
  open question, discusses it with the user, records the answer, and
  restarts the run from its last handoff.
- **The benchmark runs the same headless path** with no session at all;
  questions go to a file.

## Verification: the project's procedure

The project guarantees that its changes can be verified, and says how, in
one place: a `## Verification` section in `AGENTS.md` (or a file the
`verification` slot points to). The harness trusts that procedure exactly.
It never invents verification, and never builds what a check needs:
Dockerfiles, compose files, CI workflows and deploy configuration are off
limits unless the spec itself asks for them. A login feature never comes
with a Dockerfile.

The procedure lists the project's steps, each with the command it runs and
when it applies (U2 fixes the format). For example:

```markdown
## Verification

| Step | Command | Applies when |
| --- | --- | --- |
| Build backend, MCP and webapp | `python3 scripts/compose-session.py --slot 1 --profile hosted up -d --build` | always |
| Test the webapp | `…` | the change touches `src/replicable/web/**` |
| Test desktop against the local backend | `…` | the change touches `desktop/**` |
| Check CI | `gh pr checks {pr} --watch --fail-fast` | after the runner opens the PR |
| Test the production webapp | `…` | after merge: listed for the user, never run |
```

The runner reads the section itself and runs its steps through the `check`
tool, so it works whether or not Claude Code loads `AGENTS.md`.

| When | What happens |
| --- | --- |
| **Setup** (and the first `refine` in a repository) | Checks that the section exists, shows it to the user, and says once: "I'll trust this procedure exactly; if it's wrong, I'll stop and ask you to fix it." Without one, the user writes it before any staged run; the harness can draft it from the project's docs for the user to correct |
| **Understand** | The acceptance criteria say what must be true; the procedure says how the project checks things. The spec states verification only where this change departs from the procedure ("a unit test is enough here", "also run on k3s") |
| **Plan** | `select-checks` turns the steps that apply to this change, plus a check for every acceptance criterion (usually a new test), into the plan's checks. They are data in the plan, fixed before Build starts. A criterion that no step can check is a question for the user, never a reason to build infrastructure |
| **Preflight** (the runner, before Build) | Runs the applicable steps once at the starting commit. Existing checks must pass; a new criterion check must run and fail, since the feature does not exist yet. If a step cannot run, the run stops before anything is spent: "the verification procedure is not in place: `<step>`: `<error>`". The result is cached per commit and procedure |
| **Verify** | Runs the plan's checks. A failing step is re-run at the starting commit, without the change. **Fails there too:** the procedure is not in place; the run stops, names the step and the error, and asks the user to fix the procedure. **Passes there:** the change broke it, and the unit goes back to Build |
| **After the fix** | `/replicable:resume <issue>` re-runs the preflight and continues at Verify. The Build work is kept |

**What the harness answers for:** following the procedure exactly, and
finding out, before it spends anything, that a step cannot run. **What it
does not answer for:** a procedure that is missing, wrong, or needs what the
machine lacks (no Dockerfile, no Docker engine, no credentials).

**Stages shrink to the procedure.** With only a unit-test step, Verify runs
the unit tests and the review. Deliver commits on a branch, and the runner
opens a PR when there is a remote; the delivery tier runs only when the
procedure has delivery steps and the spec asks for them. Every run records
each step as passed, failed, or not run and why ("after merge"), so its
result says exactly what was verified and what was not.

**Checks are data.** A check is a one-off command, or a service check that
starts something, waits until it is ready, probes it, and stops it:

```json
{"name": "login-smoke", "criterion": "AC2", "step": "Build backend, MCP and webapp",
 "start": "npm run dev", "ready": "http://localhost:3000",
 "probe": "curl -fsS -X POST localhost:3000/login -d @fixtures/user.json | grep -q dashboard"}
```

The `test` setting (`.claude/replicable.json`) stays for the interactive test
gate, as today; staged runs use the procedure.

## Anatomy of a skill

Every universal skill is defined by six fields. U3 writes them into each
`SKILL.md`; the hooks and the runner check what can be checked.

| Field | Meaning |
| --- | --- |
| **Outcome** | The one thing that is true when the skill succeeds |
| **Inputs** | What it reads: the issue, a handoff, earlier reports, files |
| **Actions** | What it may do, from the [action classes](#action-classes). Anything else is outside its boundary |
| **Exit check** | How success is decided, and by whom: **(M)** mechanical, checked by a hook, an MCP tool or the runner against recorded facts; **(J)** judgement, the skill's own self-check, recorded as such |
| **Report** | The fields it ends with. A report is the skill's outcome as data: it ends the skill's span and feeds the handoff |
| **Slots** | The project-specific values it reads ([slots](#project-slots-and-the-projects-skills)) |

Skills are universal: no project names, paths or commands in them. Anything
project-specific is a slot, or one of the project's own skills.

### Action classes

| Class | Tools and commands |
| --- | --- |
| `read` | `Read`, `Glob`, `Grep`; graphify `query`, `explain`, `path`, `affected`, `god-nodes`; git `status`, `diff`, `log`, `show`, `blame`; the plugin's `info` tool |
| `explore` | `read`, plus the `graph-explorer` subagent |
| `edit-code` | `Edit`, `Write`, `NotebookEdit` on files outside the test paths |
| `edit-tests` | The same tools on files inside the project's test paths |
| `run-checks` | The plan's checks, through the `check` tool: the procedure's steps and the criterion checks. Nothing else counts as passing |
| `git-commit` | git `add`, `commit`, `restore`, and `reset --soft` back to the run's base: only commits this run made |
| `tracker-read` | Reading the issue and its comments (`gh issue view`) |
| `tracker-write` | Editing the issue body or commenting. **Understand only**, and only after the user approves the text |
| `review` | `read`, plus the `reviewer` subagent |
| `handoff` | The `report` and `handoff` tools, which write only to the plugin's data dir |

**Never, in any headless stage:** `git push`, any write through `gh`,
network access other than reading the issue, installing dependencies other
than through the procedure, editing outside the worktree, changing hook or
permission settings, `--no-verify`, and creating or changing the
verification procedure or what it relies on (Dockerfiles, compose files, CI
workflows, deploy configuration) unless the spec asks for it. Pushing, opening the PR and
deploying belong to the runner, never the agent ([D2](#decisions-for-review)).
The benchmark already enforces this for its runs.

## Which skills each stage allows

`REPLICABLE_STAGE` names the stage a session is in. The runner sets it; it is
unset in ordinary interactive sessions, where nothing is restricted and the
trace (when on) only records.

| Skill | understand | plan | build | verify | deliver | execute (single-stage) | learn |
| --- | :-: | :-: | :-: | :-: | :-: | :-: | :-: |
| `assess-task` | ● | | | | | | |
| `explore-code` | ● | ● | | | | | |
| `write-spec` | ● | | | | | | |
| `assess-change` | | ● | | | | | |
| `plan-change` | | ● | | | | | |
| `select-checks` | | ● | | | | | |
| `implement-unit` | | | ● | | | ● | |
| `write-tests` | | | ● | | | ● | |
| `run-checks` | | | ● | ● | | ● | |
| `diagnose-failure` | | | ● | ○ | | ● | |
| `fix-failure` | | | ● | | | ● | |
| `review-change` | | | | ● | | ● | |
| `commit-change` | | | | | ● | ● | |
| `handoff` | | ● | ● | ● | ● | ● | |
| `status` | ● | ● | ● | ● | ● | ● | ● |
| `retrospect` | | | | | | | ● |
| `wiki-ingest` (existing) | | | | | ● | ● | ● |
| The project's own skills | ● | ● | ● | ● | ● | ● | |

○ = proposed addition ([D5](#decisions-for-review)). Each headless session
sees only its own stage's universal skills ([how](#how-a-stage-gets-its-skills)).
The project's own skills are visible in every stage unless they name their
stages ([the project's skills](#the-projects-own-skills)); they cannot disable
or replace a universal skill.

### How a stage gets its skills

Only Understand is interactive, so only Understand's skills are installed
for interactive use. Every other universal skill reaches a session only
through the runner, which gives each headless stage exactly the skills its
column allows.

| Where | What |
| --- | --- |
| **The installed plugin** (`replicable`) | Understand's skills (`assess-task`, `explore-code`, `write-spec`), the skills that exist today, the hooks and the MCP server. The stage hook and the `check`, `report` and `handoff` tools do nothing unless `REPLICABLE_STAGE` is set |
| **A per-stage plugin the runner assembles** | A copy of the stage's other universal skills. It is loaded with a second `--plugin-dir` (repeatable; checked on Claude Code 2.1.322) under a fixed name, so skill names are the same in every run. Skills the installed plugin already has, such as `explore-code` in Plan, are used from it, not copied |
| **The universal skills' sources** | In this repository (`stages/skills/`), outside the `skills/` directory the plugin loads, so installing the plugin never lists them |

The project's own skills load from the repository as they always do
([the project's skills](#the-projects-own-skills)). The runner hides only the
ones that name other stages: project skills with `skillOverrides` through
`--settings`, and skills of plugins the repository enables with permission
deny rules, because `skillOverrides` does not apply to plugin skills. The
stage hook still checks every `Skill` call and traces it. It also covers
the installed plugin's own skills (`epcc`, `tdd`, the wiki skills), which
stay listed because that plugin is loaded for its hooks and MCP server.

A stage's set is the same in every run, never trimmed to the plan, so runs
stay comparable. A skill the plan does not need, such as `write-tests` when
there are no tests to write, is simply not called.

To verify in U4 and U6: that a copied skill tree loads (plugin paths may not
leave the plugin's directory, so the runner copies rather than links), and
that hiding project skills through `--settings` works in headless sessions.

## The skills

### Understand (interactive)

#### `assess-task`

- **Outcome.** The request is understood well enough to spec or to drop: what kind of change it is, whether it is still wanted and its code still live, whether the project's verification procedure exists, and what is unclear.
- **Inputs.** The request (issue number or text), the wiki index, the code graph.
- **Actions.** `read`, `tracker-read`.
- **Exit check.** (M) Every path and symbol the report names resolves at HEAD (file exists, `graphify explain` exits 0). (J) Every open question is either answered by the user or listed.
- **Report.** `kind` (feature, bug, refactor, chore, docs, investigation), `live`: evidence the code is still used (callers, recent commits), `still_wanted` (yes, no, ask), `verification` (the procedure's location and steps, or missing), `open_questions`, `next` (explore-code, write-spec, stop).
- **Slots.** `tracker`, `verification`.

#### `explore-code` (Understand and Plan)

- **Outcome.** A localisation: the files, symbols and tests the change concerns, and what they affect, each claim backed by `path:line`.
- **Inputs.** A question, or the spec.
- **Actions.** `explore`. Graph first, then only the line ranges it points to; a targeted search to confirm "no callers".
- **Exit check.** (M) Every file named exists at the base commit; every symbol resolves in the graph or is marked unresolved; INFERRED edges are marked.
- **Report.** `answer`, `core_files` (the files the change is expected to edit), `affected` (files and symbols, with relation), `tests` (test files that exercise the core files), `uncertain` (INFERRED edges, unresolved names).
- **Slots.** None.

#### `write-spec`

- **Outcome.** The issue holds a spec the Plan stage can run on: problem, goals, design, required design choices (each with the choice made), non-goals, and acceptance criteria, each one testable under the project's verification procedure, or under verification the spec states where this change departs from it.
- **Inputs.** The `assess-task` and `explore-code` reports, the conversation.
- **Actions.** `read`, `tracker-read`; `tracker-write` only after the user approves the final text.
- **Exit check.** (M) The issue body validates against the spec template (U2): sections present, criteria written as a checklist, no required design choice left open. (J) The user approved it.
- **Report.** `issue` (URL), `criteria` (count), `design_choices` (count), `verification` (departures from the procedure, if any), `open` (must be empty).
- **Slots.** `spec.template`, `tracker`, `verification`.

### Plan (headless)

Plan runs `explore-code`, then the three skills below, then `handoff`. The
runner runs the [plan gate](#validation-by-the-runner) and posts the plan
comment (U5); the agent never writes to the issue.

#### `assess-change`

- **Outcome.** The [measured estimate](#the-plans-estimate-and-route) and the route it supports.
- **Inputs.** The spec, the localisation.
- **Actions.** `read`.
- **Exit check.** (M) Every graph field equals what the runner recomputes with `graphify affected` over the plan's files at `measured_at`; the route follows the project's route rule, or `reason` says why not.
- **Report.** The estimate fields (`units` is filled by `plan-change`), `route`, `reason`.
- **Slots.** `surfaces`, `risks`, `route`.

#### `plan-change`

- **Outcome.** A plan the gate accepts: units in dependency order, each with what changes, its files, the tests that prove it, its checks, and the acceptance criteria it covers. Every criterion is covered by a unit, and every unit serves a criterion: no work beyond the spec.
- **Inputs.** The spec, the localisation, the estimate.
- **Actions.** `read`, `handoff`.
- **Exit check.** (M) The plan gate: every criterion maps to a unit and a check, and every unit to a criterion; every file exists at the base or is marked new; unit ids unique, dependencies acyclic, `units` equals the number of units.
- **Report.** `units` (id, what, files with `new`, tests, checks, depends_on, criteria), `criteria` (id, text, units), and the `replicable-execution` block.
- **Slots.** None.

#### `select-checks`

- **Outcome.** The plan's checks, fixed before Build starts: the procedure's steps that apply to this change, and a check for every acceptance criterion (usually a new test, or a service check), each with the command it runs.
- **Inputs.** The spec, the plan's units, the localisation, the verification procedure.
- **Actions.** `read`.
- **Exit check.** (M) Every check is a procedure step, or a criterion check that uses only what the procedure provides; every criterion has a check; no check needs infrastructure the procedure lacks. A criterion no step can check makes Plan stop and ask.
- **Report.** Per unit: `checks`; for the change: `full` (the checks Verify runs); `post_merge` (steps listed for the user, never run).
- **Slots.** `verification`.

### Build (headless)

#### `implement-unit`

- **Outcome.** One unit's change is in the worktree, within the unit's files, and its checks were run after the last edit.
- **Inputs.** The unit, the localisation; Verify's findings when the unit was reopened.
- **Actions.** `read`, `edit-code`, `edit-tests` (for tests the unit lists), `run-checks` (the unit's selection).
- **Exit check.** (M) The diff touches the unit's files, and any other file is listed as a deviation with a reason; a `check` record of the unit's checks exists for the current tree.
- **Report.** `unit`, `files_changed`, `deviations`, `checks` (record ids, pass or fail), `notes`.
- **Slots.** `verification`.

#### `write-tests`

- **Outcome.** Tests that pin a unit's acceptance behaviour exist, are collected by the test command, and, when written before the code, fail for the right reason.
- **Inputs.** The unit, its criteria and checks.
- **Actions.** `read`, `edit-tests`, `run-checks` (the new tests).
- **Exit check.** (M) The new tests run; written first, they fail with an assertion, not an import or syntax error (recorded by the `check` tool).
- **Report.** `tests` (path, name, criterion), `status` (failing-as-expected, passing), `notes`.
- **Slots.** `verification` (its test steps).

#### `run-checks` (Build and Verify)

- **Outcome.** A recorded result for a set of checks on the current tree.
- **Inputs.** A selection: the unit's checks in Build, `full` in Verify.
- **Actions.** `run-checks`. No edits.
- **Exit check.** (M) A `check` record per check: command, exit code, duration, failing test names, tree hash.
- **Report.** `records` (ids), `failing` (check, tests), `tree`.
- **Slots.** `verification`.

#### `diagnose-failure`

- **Outcome.** A root cause for one failing check: a located hypothesis, with evidence, that predicts what a fix changes, classified as the change, the test, or the procedure (the step also fails at the starting commit).
- **Inputs.** A failing `check` record, the diff.
- **Actions.** `read`; re-running the failing test; running it at the base commit to tell a regression from a pre-existing failure.
- **Exit check.** (J) The hypothesis names a `path:line` and predicts the fix's effect. (M) "The procedure" is backed by a failing record of the same step at the starting commit.
- **Report.** `check`, `cause`, `class`, `evidence`, `fix` (proposed; none for "the procedure", which goes to the user), `confidence`.
- **Slots.** `verification`.

#### `fix-failure`

- **Outcome.** The diagnosed cause is fixed: the failing check passes and none of the unit's checks got worse.
- **Inputs.** The diagnosis.
- **Actions.** `read`, `edit-code`, `run-checks`. `edit-tests` only when the diagnosis class is "the test", and then it is a deviation Verify must review. Never deleting, skipping or weakening a test to make it pass, and never changing the procedure or what it relies on.
- **Exit check.** (M) A passing record of the failing check, and records of the unit's checks no worse than before, on the same tree.
- **Report.** `files_changed`, `records`, `deviations`.
- **Slots.** `verification`.

### Verify (headless)

#### `review-change`

- **Outcome.** An independent review of the whole change against the spec: every acceptance criterion judged with evidence, and findings ranked by severity.
- **Inputs.** The spec, the plan, the diff from the base, the `full` check records.
- **Actions.** `review` (the `reviewer` subagent reviews in a clean context).
- **Exit check.** (M) Every criterion has a verdict (met, not met, unverifiable) and evidence (a test, or `path:line`); every finding has `path:line` and a failure scenario; a passing `full` record exists for the reviewed tree.
- **Report.** `verdict` (pass, changes-needed), `criteria`, `findings` (severity, path, line, scenario, fix), `reopen` (units to send back).
- **Slots.** `review.guide` (a project document reviewers follow).

### Deliver (headless)

#### `commit-change`

- **Outcome.** The verified change is on the work branch as reviewable commits whose messages say why and reference the issue; the worktree is clean; nothing is pushed.
- **Inputs.** The verified tree, the spec, the plan, the Verify report.
- **Actions.** `read`, `git-commit`.
- **Exit check.** (M) HEAD's tree is the verified tree; the worktree is clean (ignored files aside); messages follow `commit`; the remote is unchanged.
- **Report.** `commits` (sha, subject), `branch`.
- **Slots.** `commit` (style, issue reference), `branch`.

The project's own skills extend Deliver and the other stages with project
operations: a local release build, a migration check, a preview environment.
They never push or deploy to shared environments: the runner does that, with
the project's environment commands (U9).

### Continuity (every headless stage)

#### `handoff`

- **Outcome.** The stage's handoff is written and valid against its schema.
- **Inputs.** The stage's reports.
- **Actions.** `handoff`.
- **Exit check.** (M) The `handoff` tool validates the schema when it is written; the runner re-validates against the facts before the next stage starts.
- **Report.** `path`, `status`, `next`.
- **Slots.** None.

#### `status`

- **Outcome.** A short, current account of the work: stage, unit, last check results, budget used against caps, blockers. Used on resume, after compaction, or before escalating.
- **Inputs.** Handoffs, `check` records, the trace, git.
- **Actions.** `read`.
- **Exit check.** (J) Every claim comes from those files, not from memory.
- **Report.** `stage`, `unit`, `checks`, `budget`, `blockers`.
- **Slots.** `caps`.

### Learn (optional)

#### `retrospect`

- **Outcome.** Proposals from the session traces: recurring work done outside any skill, skills that looped past their caps, slots that were missing, lessons for the wiki.
- **Inputs.** `analyze-session` output (U7), the handoffs.
- **Actions.** `read`; writes proposals to the data dir. Never creates or edits a skill.
- **Exit check.** (M) Every proposal cites the spans it comes from (session id, start and end).
- **Report.** `proposals` (kind: skill, slot, wiki, CLAUDE.md; evidence; status `pending`).
- **Slots.** `learn.upstream` (off by default: sending proposals upstream is opt-in).

## The plan's estimate and route

The route is the only execution decision Plan makes: **single-stage or
multi-stage**. The estimate is the evidence for it, as fields the run can
measure again afterwards, so estimates can be compared with actual effort.
There are no complexity labels.

| Field | Estimated by Plan as | Measured afterwards as |
| --- | --- | --- |
| `units` | The plan's units | Units completed, plus units added by re-plans |
| `components` | Top-level directories of the files the plan changes, tests excluded | The same over the final diff |
| `surfaces` | The `surfaces` slot entries whose globs match the files the plan changes | The same over the final diff |
| `blast_radius_files` | Distinct files in `graphify affected --depth 3` over the plan's existing files, plus those files | The same over the diff's files, on the graph at `measured_at` |
| `max_fan_in` | The most distinct files that depend directly (`--depth 1`) on any of the plan's files | The same over the diff's files |
| `tests_near_change` | Test files inside the blast radius | The same over the diff; compared with the tests the run changed and ran |
| `risks` | From a fixed list: `migration`, `security`, `release`, `data`, `performance`, `concurrency`, `external` | The `risks` slot globs the diff matches, and failures attributed to a risk |
| `route` | `single-stage` or `multi-stage` | The route actually executed, after any re-route |
| `reason` | One line: why this route | — |
| `measured_at` | The commit the graph fields were measured at | — |

Actual effort, compared with the estimate: sessions and iterations, fix
cycles, Verify-to-Build returns, cost, wall time, tokens, re-routes and
escalations (U8).

`surfaces` and `risks` are measurable only when a project maps them to paths
(slots). Without the slots they are the planner's judgement, and the
benchmark compares them by hand.

**The route rule** is a project slot with a default, and a starting point the
benchmark tunes: multi-stage when the plan has 4 or more units, 3 or more
components, or any of the risks `migration`, `security` or `release`. The gate
accepts a route against the rule only with a `reason` that says why.

Example, from the plan comment on #2:

```replicable-execution
units: 11
components: bench, docs, harness, hooks, loops, skills
surfaces: hooks, skills, mcp-tools, project-config, cli
blast_radius_files: 19
max_fan_in: 10
tests_near_change: 2
risks: release, security
route: multi-stage
reason: 11 units over 6 components and 5 surfaces, with release and security risks; ...
measured_at: 170dac5
```

## Transitions, loops and caps

Each stage has an expected order. A **loop** is an expected cycle; any other
return to an earlier skill, and any return to an earlier stage, is a
**backward jump**. Both are counted.

```
Plan:    explore-code ─► assess-change ─► plan-change ─► select-checks ─► handoff
Preflight (runner): the applicable procedure steps at the starting commit ─► Build
Build, per unit:
         [write-tests] ─► implement-unit ─► run-checks ─┬─► handoff (next unit, or Verify)
                                              ▲         └─► diagnose-failure ─► fix-failure ─┐
                                              └──────────────────── loop ────────────────────┘
Verify:  run-checks ─► review-change ─┬─► handoff ─► Deliver           (pass)
                                      └─► handoff ─► Build            (changes needed: backward jump)
Deliver: commit-change ─► [project skills] ─► handoff ─► done
```

Caps (defaults in the `caps` slot, to be tuned by the benchmark). When a
skill's invocation would pass a cap, the stage hook denies it with the reason,
and the agent's only way on is `handoff` with `status: escalate`.

| Cap | Default | Counts |
| --- | --- | --- |
| `fix_cycles` | 3 per failing check | diagnose → fix → run-checks |
| `verify_returns` | 2 per run | Verify → Build |
| `same_skill` | 4 in a row | Consecutive invocations of one skill |
| `plan_gate` | 2 retries | Plan rejected by the gate |
| `handoff_rejects` | 2 per stage | Handoffs the runner rejects |
| `reroutes` | 1 per run | Single-stage → multi-stage |

### Re-route and escalation

The runner compares the run so far with the estimate, using the same
measures:

- the diff's `blast_radius_files` passes twice the estimate
- files outside the plan pass 3, or half the planned files
- a unit uses more than its share of the budget (cost or iterations)

A single-stage run that shows a signal re-routes once: the agent writes a
handoff, and multi-stage continues from it. A second signal, any cap, or a
multi-stage run that shows a signal escalates: the run stops, and the runner
posts the handoff and the reason on the issue (or writes a file offline or in
the benchmark).

## Handoffs

A handoff is a JSON document in the data dir,
`<data dir>/runs/<run id>/handoffs/<nn>-<stage>[-<unit>].json`, written
through the `handoff` tool. It carries what the next stage needs and nothing
it could recompute. The runner overwrites every field it can derive itself
(git, budget, skills), so a handoff never asks the next stage to trust a
claim it could check.

### Envelope (every stage)

```json
{
  "schema": "replicable.handoff/1",
  "run": "r-20261111-0001",
  "issue": {"repo": "owner/name", "number": 2, "spec_sha256": "…"},
  "stage": "build",
  "unit": "U3",
  "status": "complete",
  "next": "verify",
  "summary": "≤ 600 characters",
  "git": {"base": "…", "head": "…", "tree": "…", "branch": "replicable/2-staged-harness"},
  "checks": ["chk-0007", "chk-0009"],
  "deviations": [{"kind": "out_of_plan_file", "path": "harness/project.py", "reason": "…"}],
  "risks": ["release"],
  "skills": {"implement-unit": 2, "run-checks": 3},
  "budget": {"cost_usd": 1.42, "wall_s": 610, "turns": 38}
}
```

| Field | Written by | Rule |
| --- | --- | --- |
| `schema`, `run`, `issue` | Runner | `spec_sha256` pins the spec the stage worked from |
| `stage`, `unit` | Agent | `unit` only in Build |
| `status` | Agent | `complete`, `partial`, `blocked`, `escalate` |
| `next` | Agent | A request; the runner decides |
| `summary` | Agent | Free text, capped |
| `git` | Runner | Recomputed from the worktree |
| `checks` | Agent, verified by the runner | Ids of `check` records, which hold the facts |
| `deviations` | Agent | `out_of_plan_file`, `changed_test`, `skipped_check`, `new_dependency`, `other` |
| `skills`, `budget` | Runner | From the trace and the session |

### Per stage

| Stage | Extra fields |
| --- | --- |
| **Understand → Plan** | No file: the refined issue is the handoff, validated against the spec template |
| **Plan → Execution** | `plan` (units), `criteria` (id, text, units, checks), `localisation` (core files, affected tests), `estimate` (the [fields](#the-plans-estimate-and-route)), `execution` (the block as posted) |
| **Build → Verify** | `units` (id, status `done` or `blocked`, files changed, check records), `claims` (criterion → evidence) |
| **Verify → Build** | `findings`, failing records, `reopen` (unit ids) |
| **Verify → Deliver** | `verdict: pass`, `criteria` (all met, with evidence), the `full` record for `git.tree` |
| **Deliver → done** | `commits`, the project skills it ran (`project_skills`: skill, status), and `verification`: each procedure step passed, failed, or not run and why |
| **Single-stage → done** | Build's, Verify's and Deliver's fields together |

### Validation by the runner

Before the next stage starts, the runner checks the handoff against the
schema and against the facts. A rejected handoff sends the stage back with
the errors as context; it counts towards `handoff_rejects`.

| Transition | The runner checks |
| --- | --- |
| Every transition | Schema; `git` recomputed; every check record exists, and a check claimed as passing ran on `git.tree` |
| **Plan → Execution** (the plan gate) | Every criterion has a unit and a check, and every unit a criterion; every check is a procedure step or a criterion check that uses only what the procedure provides; every file exists at the base or is marked new; ids unique, dependencies acyclic; `units` matches; the estimate's graph fields match a recomputation at `measured_at`; the route follows the rule or `reason` says why |
| **Preflight** (before Build) | The applicable procedure steps run at the starting commit: existing checks pass, new criterion checks run and fail. Otherwise the run stops: the procedure is not in place |
| **Build → Verify** | Every unit `done` or `blocked`; changed files within the plan plus deviations; each done unit's checks passing on the final tree |
| **Verify → Build** | `changes-needed` with at least one finding, or a failing step that passes at the starting commit; `reopen` not empty |
| **Verify → the user** | A failing step that also fails at the starting commit: the procedure is not in place. The run stops and asks for it to be fixed, then resumes at Verify |
| **Verify → Deliver** | Verdict pass; every criterion met (or marked manual in the plan); `full` passing on `git.tree` |
| **Deliver → done** | Worktree clean; HEAD's tree equals the verified tree; at least one commit since the base; the remote unchanged |

### Context at the start of a stage

The runner, not the agent, assembles each stage's opening context (U6),
injected with SessionStart `additionalContext`: the spec, the plan's part for
this stage, the previous handoff, and fresh facts the runner computes (the
diff stat from the base, the failing records, `graphify affected` for the
changed files, the budget left). It is capped (8,000 characters by default);
the rest is given as paths.

## Project slots and the project's skills

The one thing a project must provide is its
[verification procedure](#verification-the-projects-procedure). Everything
else lives in `.claude/replicable.json`, resolved by `harness/project.py`
and served to skills by the `info` tool, and every slot has a default.
Proposed shape (U3 fixes it):

```json
{
  "verification": "AGENTS.md#verification",
  "surfaces": {"cli": ["src/cli/**"], "api": ["src/api/**"], "schema": ["migrations/**"]},
  "risks": {"migration": ["migrations/**"], "security": ["src/auth/**"], "release": ["pyproject.toml"]},
  "route": {"multi_stage_when": {"units": 4, "components": 3, "risks": ["migration", "security", "release"]}},
  "caps": {"fix_cycles": 3, "verify_returns": 2},
  "commit": {"style": "Imperative subject; body says why", "issue_ref": "Part of #{issue}"},
  "branch": "replicable/{issue}-{slug}",
  "review": {"guide": "CONTRIBUTING.md"}
}
```

| Slot | Used by | Default |
| --- | --- | --- |
| `verification` | Every stage, the runner | `AGENTS.md`, section `## Verification` |
| `surfaces`, `risks` | `assess-change`, the runner's measures | None: judged, not measured |
| `route` | `assess-change`, the plan gate | The rule above |
| `caps` | The stage hook, the runner | The table above |
| `commit`, `branch` | `commit-change`, the runner | Plain messages that reference the issue; `replicable/{issue}-{slug}` |
| `review.guide` | `review-change` | None |
| `spec.template` | `write-spec`, the gate | The plugin's template (U2) |
| `tracker` | `assess-task`, `write-spec`, the runner | GitHub through `gh` |

### The project's own skills

The project extends the harness with its own skills, for what is specific
to it: local environments, release builds, deploy previews, the house way to
add a migration. Nothing in the plugin's config lists them.

- **They come from the repository:** `.claude/skills/`, and the plugins the
  repository enables in its committed `.claude/settings.json` (each installed
  once per clone; setup flags one that is not). Personal skills and
  plugins (`~/.claude/`) never reach a headless run: the runner starts
  Claude Code with `--setting-sources project`, so a teammate, CI and the
  benchmark get the same run.
- **The agent chooses them by description,** as with any skill. They are
  visible in every headless stage. A skill meant for some stages only says
  so in its own frontmatter, and the runner hides it elsewhere. Claude Code
  passes `metadata` through without acting on it:

  ```yaml
  metadata:
    replicable:
      stages: [deliver]
  ```

- **Safety does not depend on the choice.** The headless rules (never push,
  never write through `gh`, never deploy) are permission rules, applied
  whichever skill runs.
- **Supported, not guaranteed.** Their calls are traced like any skill call,
  so they appear in the run's record and in `analyze-session`; one that ends
  with the `report` tool also gets an exact span end and outcome. The
  benchmark never runs them: it measures the harness as shipped, and its
  datasets are assumed to carry no project skills.
- **Names never clash.** Universal skills are namespaced (`replicable:<name>`,
  or the per-stage plugin's prefix), so a project skill with the same short
  name is simply another skill, chosen by its description like the rest. The
  stage hook matches full names. Nothing overrides a universal skill: they
  cannot be disabled or replaced, and a project adapts them through slots.
  That keeps every staged run on the harness we guarantee, plus the
  project's additions.

## Mechanisms (confirmed in Claude Code's docs)

Checked against the [hooks](https://code.claude.com/docs/en/hooks) and
[skills](https://code.claude.com/docs/en/skills) documentation (2026-11-11),
and against the transcripts of real sessions on this machine.

| Question | Answer | Consequence |
| --- | --- | --- |
| Can a PreToolUse hook match the `Skill` tool and see which skill? | Yes. PreToolUse matches tool names exactly, and the docs name "a `PreToolUse` hook matching the `Skill` tool". Its `tool_input` is `{"skill": "<name>", "args": "…"}` (`args` optional): 9 of 9 `Skill` calls in local transcripts, and the documented permission form `Skill(skill:deploy)` | The stage hook is a PreToolUse hook on `Skill` that allows or denies by `tool_input.skill`, with `permissionDecision: "deny"` and a reason |
| What bypasses it? | Typing `/name` "bypasses `PreToolUse`"; `UserPromptExpansion` fires on that path instead, matched on the command name | Headless stage prompts never start with a slash command. Interactive sessions are unrestricted; a `UserPromptExpansion` hook records slash-command starts for the trace |
| How does `disable-model-invocation` interact with in-stage invocation? | `true` removes the skill's description from Claude's context, and "if Claude tries anyway, Claude Code blocks the call"; it also blocks preloading into subagents | Universal skills must leave it `false`: the agent picks them itself. It stays on user-only skills (`setup`, `test-gate`, `headless-loop`). Stage restriction comes from the hook, not from this field |
| Can settings hide plugin skills per stage? | No: "plugin skills are not affected by `skillOverrides`". Project skills are | A plugin's skills are listed in every session that loads it, so the universal skills are not installed for interactive use: the runner gives each stage its own ([how](#how-a-stage-gets-its-skills)), and hides out-of-stage project skills with `skillOverrides`. Permission rules (`Skill(name)` allow and deny) apply to both, as a second layer |
| Which plugins and skills does a headless run load? | `claude -p` loads installed plugins. A repository enables plugins for its team in its committed `.claude/settings.json`; each collaborator installs them once per clone. Plugin skills are namespaced, so a same-named skill elsewhere "both load" | The project's skills come from the repository, and their names cannot clash with ours ([the project's skills](#the-projects-own-skills)) |
| Can a session load more than one plugin directory? | Yes: `--plugin-dir` is repeatable, and a folder of plugins loads each child (`claude --help`, 2.1.322) | The installed plugin and the runner's per-stage plugin load side by side |
| Which fields carry the ids the trace needs? | Every hook gets `session_id`, `transcript_path`, `cwd`, `hook_event_name`, and `prompt_id` after the first input; PreToolUse and PostToolUse add `tool_name`, `tool_input` and `tool_use_id`; inside a subagent, `agent_id` and `agent_type` | The trace keys each row by `session_id` and `tool_use_id` (as it does today) and adds `agent_id`, so a skill invoked inside a subagent is attributed to it |
| Can a skill restrict its own tools? | `allowed-tools` only pre-approves ("it does not restrict which tools are available"). `disallowed-tools` removes tools "while this skill is active", and clears on the user's next message | Per-skill boundaries: the hooks record first ([D7](#decisions-for-review)). `disallowed-tools` is a native candidate, but when it clears in a headless session that invokes several skills must be tested in U4 |
| Do hooks in a skill's frontmatter end with the skill? | No: they stay registered "for the rest of the session" | Skill-scoped hooks cannot mark a skill's end or scope a boundary. The stage hook is a plugin hook that tracks the active skill itself |

## What gets measured

| Measure | Source |
| --- | --- |
| A skill's span | From its `Skill` call (the stage hook's trace row, `tool_use_id`) to its `report` call. Today the bench ends a span at the next skill's start, so it cannot see work done between skills |
| Its outcome | The `report` tool's `status` and fields |
| Cost, time, tokens per skill and per stage | The bench's existing span attribution (`bench/metrics.py`), with spans from the trace; stage from `REPLICABLE_STAGE`, recorded on every trace row |
| The transition graph | The ordered skill rows per session: paths, loops, backward jumps, repeated subgraphs |
| Work outside any skill | Time and tokens outside every span, `(no skill)` |
| Caps and denials | Stage hook rows with `outcome: block` and a reason (`not_in_stage`, `cap:fix_cycles`, …) |
| Plan measures | The gate's result, the estimate against the same fields measured on the diff, re-routes, escalations; recall of the reference's core files (`localisation.core_files`) and of the hidden tests' areas (`criteria[].checks`); the checks Plan chose against the task's reference verification, recall and precision (U8) |

`analyze-session` (U7) computes the same tables for any saved session. Without
a trace, as in an interactive session with tracing off, spans come from
`Skill` calls and slash commands in the transcript.

In the benchmark, the suite writes each dataset's verification procedure for
its pinned commit, the same for every arm, and `validate` proves its steps
run on the bench machine. A task whose procedure cannot run there is
invalid, like a task whose reference solution fails: it never counts as a
harness failure.

## Existing skills, agents and hooks

| Piece | In the staged harness |
| --- | --- |
| `graph-explorer` subagent | Used by `explore-code` |
| `reviewer` subagent | Used by `review-change`, so Verify reviews in a clean context |
| `post_edit` hook | Unchanged; its affected-tests list feeds `implement-unit` and `run-checks` |
| `wiki_flag` hook, `wiki-ingest` | `wiki-ingest` is allowed in Deliver and Learn, so a change that a wiki page describes updates the page in the same change |
| `wiki-lint` | Maintenance, outside the stages |
| `test_gate` hook | Off in staged runs: handoff validation is the stage-aware version ([D8](#decisions-for-review)). Unchanged in interactive sessions |
| `epcc`, `tdd` | Interactive workflows, outside the stages and unchanged; the benchmark's `*-skill` arms use `epcc` ([D9](#decisions-for-review)) |
| `setup`, `test-gate`, `headless-loop` | User-only (`disable-model-invocation: true`), outside the stages. `headless-loop` gives way to the execution runner (U6) |

## Decisions for review

Each has a recommendation; the maintainer decides before U3.

| # | Decision | Options | Recommendation |
| --- | --- | --- | --- |
| D1 | **Where the universal skills ship.** `skillOverrides` cannot hide plugin skills, so installed skills are listed in every session | — | **Decided:** Understand's skills in the installed plugin; every other universal skill only in the per-stage plugin the runner assembles ([how](#how-a-stage-gets-its-skills)). The project's own skills come from the repository and are supported, not benchmarked; universal skills are not overridable |
| D2 | **Who pushes and opens the PR** | A Deliver skill; the runner | The runner. The agent never needs push or `gh` write access, and the benchmark already denies both |
| D3 | **Commits during Build** | The agent commits as it goes; the runner checkpoints at each handoff and `commit-change` rewrites the run's checkpoints into final commits | Runner checkpoints. Verify then reviews a fixed tree, and `commit-change` only touches this run's commits |
| D4 | **Build granularity in multi-stage** | One session for all units; one session per unit | One per unit: small contexts, and per-unit cost and iterations |
| D5 | **`diagnose-failure` in Verify** | Not allowed (as in #2's plan); allowed, read-only | Allowed: Verify-to-Build handoffs then carry a cause, not only a failing check |
| D6 | **How checks run** | — | **Decided:** checks are data in the plan, run only through the `check` MCP tool, which records exit code, duration, failing tests and tree hash, and supports service checks (start, wait until ready, probe, stop). MCP tool timeouts must cover the slowest check |
| D7 | **Per-skill tool boundaries** | Record only; enforce behind a flag; native `disallowed-tools` | Record first, enforce behind `REPLICABLE_BOUNDARIES=enforce` (as #2's plan says); test `disallowed-tools` in U4 and adopt it where it holds |
| D8 | **Test gate in staged runs** | Keep it; turn it off | Off: handoff validation is stricter and knows the stage. A unit that ends red to escalate must not be blocked |
| D9 | **`epcc` and `tdd`** | Keep; rewrite as compositions of universal skills; retire | Keep unchanged until U8 compares them with the staged harness |
| D10 | **How a skill reports** | A fenced block in the reply; a `report` MCP tool | The tool: validated when called, and an exact span end. A staged run needs the plugin's MCP server anyway (`info`, `check`, `handoff`), so the runner treats a server that failed to start (the stream's `init` event lists each server's status) as an infrastructure error, not as a run |
