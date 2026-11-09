# Hooks for coding agents

Hooks are commands (or prompts) that the harness runs at fixed points in the
agent loop. Unlike instructions, they run **every time**, which makes them the
main tool for enforcing verification, protecting context and keeping work
safe. Background on loops and harness layers is in
[harness-engineering.md](harness-engineering.md).

> Hook events and their input fields evolve with Claude Code. The core events
> below are long-standing; run `/hooks` in your version for the exact list.

## Where hooks fire

```
 SessionStart ──► session begins (also on resume, /clear, and after compaction)
                      │
 UserPromptSubmit ──► prompt sent, before the model sees it
                      │
              ┌──► model thinks
 PreToolUse ──┼──► tool about to run   ← allow, deny or rewrite the call
              │    tool runs
 PostToolUse ─┼──► tool finished       ← add context ("this edit affects X")
              └──── repeat while the model keeps calling tools
                      │ model stops calling tools
 Stop ──────────► about to end the turn ← block: "keep going, because …"

 also: SubagentStop, PreCompact, Notification, SessionEnd
```

## Metrics

### Outcome metrics: is the agent getting better?

| Metric | Definition |
| --- | --- |
| **Escaped regressions** | Bugs found after the agent said done (CI, review, production), per task |
| **False-done rate** | Share of turns where the agent claimed done but checks fail |
| **First-pass green rate** | Share of tasks whose first "done" already passes every check |
| **Iterations to green** | Edit → check cycles before tests pass |
| **Human interventions per task** | Corrections, approvals, "no, not that" messages |
| **Tokens per task** | Input plus output tokens to finish |
| **Context peak and compactions per task** | Largest context reached; how often it was summarised |
| **Time to a verified change** | From prompt to green checks |
| **Rework rate** | Reverts or follow-up fixes to agent code within N days |

### Hook-health metrics: is this hook pulling its weight?

| Metric | Definition | Target |
| --- | --- | --- |
| **Precision** | Share of blocks or warnings that were justified | High: false alarms train everyone to ignore the hook |
| **Catch rate** | Share of real problems caught before they escaped | High for gates; measure with seeded bugs |
| **Latency** | p50 and p95 run time | Under 1 s for PreToolUse and PostToolUse; seconds acceptable at Stop |
| **Injected tokens** | Tokens added to the context per firing | As few as possible (about 10–15 lines) |
| **Fire rate** | How often it runs, and how often it acts | A hook that always fires and never acts is pure cost |
| **Loop-outs** | How often a blocking hook hit its retry cap | Frequent means the agent cannot satisfy the check |
| **Override rate** | How often a human bypasses or disables it | High means it is in the way |
| **Crash rate** | Errors in the hook itself | Near zero, and it should fail safely when it does |

### Measuring

- **SessionEnd** appends one row per session: tokens, cost, turns, blocks per
  hook, final check status, files changed.
- **Every hook logs** its latency, its decision and its output size.
- **A fixed task set** of 5–10 real tasks runs with and without the harness
  (`claude plugin eval` can drive it).
- **Seeded bugs** measure catch rates.

## Practices

| Practice | Moves |
| --- | --- |
| **Enforce checks with hooks rather than asking for them** | False-done ↓, escaped regressions ↓ |
| **Fast, specific feedback, at the edit, scoped to what changed** | Iterations ↓, time ↓ |
| **Actionable messages:** the failing test, the line, the likely cause, the next step | Iterations ↓, loop-outs ↓ |
| **Short injections:** only what is needed to act, with pointers to the rest | Injected tokens ↓, context peak ↓ |
| **Cheap checks early, expensive late:** lint and types after edits, the full suite at Stop, an LLM judge last | Latency ↓, tokens ↓ |
| **Precision first:** start strict about when a hook acts; widen it with evidence | Precision ↑, overrides ↓ |
| **Cap every blocking loop and escalate to a human** | Bounded loop-outs and cost |
| **Fail open on helper hooks, fail closed on safety hooks** | Crash impact ↓ |
| **Keep state outside the context:** plans and progress in files, restored on demand | Compactions ↓, coherence ↑ |
| **Keep derived data fresh:** re-index and regenerate on change; flag drift | Precision ↑ |
| **Measure before and after each hook; remove the ones that don't move a metric** | All of them, by keeping the harness lean |

## Catalogue

**Code** = code graph · **Wiki** = code wiki · **Plan** = feature planning graph
(see [harness-engineering.md](harness-engineering.md#three-graphs-that-feed-the-harness)).
**Status:** ✅ implemented · ◐ partly implemented · ○ proposed. **G** = provided by graphify's own Claude Code integration or CLI (`graphify claude install`), **P** = provided by this plugin

| # | Hook | Event | What it does | Graph | Main metrics | Status |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Repo map | SessionStart | Compact module map and hot spots | Code | Tokens ↓, first-pass ↑ | ◐ G: `CLAUDE.md` rules point to `GRAPH_REPORT.md` and `graphify query`; no SessionStart injection |
| 2 | Wiki index | SessionStart | Load only the wiki's index page | Wiki | Tokens ↓, context ↓ | ◐ G: `graphify export wiki`, and graphify's rules say to use `graphify-out/wiki/index.md` when it exists |
| 3 | Plan restore | SessionStart (compact, resume) | Re-inject the current plan node, its status and its acceptance check | Plan | Survives compaction | ○ |
| 4 | Prompt enrichment | UserPromptSubmit | Symbols named in the prompt bring their graph summary, linked wiki pages and affected tests | Code + Wiki | Tokens ↓, first-pass ↑ | ○ |
| 5 | Plan binding | UserPromptSubmit | Tie the prompt to a plan node, or ask for one | Plan | Interventions ↓, rework ↓ | ○ |
| 6 | Read guard | PreToolUse (Read, Grep) | Steer searches and reads to the graph; deny generated, lock and huge files | Code | Tokens ↓, context ↓ | ◐ G: PreToolUse guards on Grep, Read and Glob steer to the graph first |
| 7 | Impact-first | PreToolUse (Edit) | Editing a high fan-in symbol requires an `affected` check first | Code | Escaped regressions ↓ | ○ (graphify `affected` is the query it would use) |
| 8 | Scope guard | PreToolUse (Edit) | Warn on edits outside the current plan node's files | Plan | Rework ↓, parallel conflicts ↓ | ○ |
| 9 | Safety guard | PreToolUse (Bash) | Deny force-push, destructive commands and deploys | — | Safety | ○ |
| 10 | Noise reducer | PreToolUse (Bash) | Rewrite test and build commands to quiet output with the tail only (`updatedInput`) | — | Tokens ↓ | ○ |
| 11 | Blast radius + affected tests | PostToolUse (Edit) | Re-index and report "affects X; run tests Y" | Code | Iterations ↓, regressions ↓ | ◐ P: `graphify update` + `graphify affected`; reports tests, does not run them |
| 12 | Parse guard | PostToolUse (Edit) | "This file no longer parses" at the moment of the edit | Code | Iterations ↓ | ○ (removed with the `ast` indexer) |
| 13 | Lint and type feedback | PostToolUse (Edit) | Format silently; report remaining lint and type errors only | — | Iterations ↓ | ○ |
| 14 | Wiki staleness flag | PostToolUse (Edit) | Editing code a wiki page links to: "page Z may be outdated" | Wiki + Code | Wiki precision ↑ | ○ |
| 15 | Test gate + definition of done | Stop | Tests green (with a retry cap); also no debug prints, no skipped tests | — | False-done ↓ | ◐ P: tests only |
| 16 | Coverage gate | Stop | Changed symbols with no covering test, according to the graph | Code | Escaped regressions ↓ | ○ |
| 17 | Plan acceptance | Stop | The current node's check must pass; mark it done and unlock dependents | Plan | First-pass ↑, orchestration | ○ |
| 18 | Subagent contract | SubagentStop | Enforce output format, evidence and size | — | Context ↓ | ○ |
| 19 | State checkpoint | PreCompact | Write the plan, decisions and files touched to disk (pairs with #3) | Plan | Survives compaction | ○ |
| 20 | Attention ping | Notification | Desktop or phone notification when input is needed | — | Time ↓ | ○ |
| 21 | Metrics + learnings | SessionEnd | Log metrics; suggest a wiki or `CLAUDE.md` entry for traps found | Wiki | Makes everything above measurable | ○ |

What each graph contributes:
- **Code graph (graphify):** what a change touches. It makes edit → verify precise (1, 4, 6, 7, 11, 12, 16).
- **Wiki:** what a change must respect. Intent and constraints (2, 4, 14, 21).
- **Plan graph:** what a change is for and when it is done. Scope, exit
  conditions and the order of work for orchestration (3, 5, 8, 17, 19).

## Hooks that call LLMs

Hooks can use models in two ways:

1. **Prompt-based hooks.** Built into Claude Code: the hook is a prompt a
   model evaluates, returning a decision with a reason. Which events support
   them depends on the Claude Code version; check the hooks documentation.
2. **Command hooks that call a model themselves,** through the API or `claude
   -p`. This is flexible: summarise a huge log, classify a diff. Guard against
   **recursion**: a nested `claude -p` loads your hooks too. Check an
   environment variable, or start the inner run with hooks disabled.

| | Script hook | LLM hook |
| --- | --- | --- |
| Determinism | Same input, same answer | May vary between runs |
| Latency | Milliseconds | Seconds |
| Cost | Free | Tokens on every firing |
| Good for | Anything checkable: tests, types, paths, patterns, graph queries | Judgement: intent, completeness, contextual risk |

Good uses for an LLM hook: "did the response do everything the user asked?"
(Stop), "is this command risky in this context?" (PreToolUse), "does every
review finding have evidence?" (SubagentStop).

**Rule:** script what can be checked, use a model only for what needs
judgement, and put model-based hooks on **low-frequency events** (Stop,
SubagentStop), not on every tool call.

## Mistakes to avoid

- **Hook output costs tokens.** Text added with `additionalContext` enters the
  context. A chatty hook makes context health worse.
- **Slow hooks make the agent sluggish.** PostToolUse runs after every
  matching call. Keep it under 1 s and move slow checks to Stop.
- **Uncapped blocking loops.** Every Stop or SubagentStop block needs a retry
  limit and a hand-back to the human.
- **Vague block reasons.** The reason is the agent's next instruction. "Tests
  failed" is weak; "`test_refresh` fails at `auth.py:42`: expected 200, got
  401; check `refresh_token()`" gets fixed.
- **Crashing safety hooks that fail open.** A guard that errors should deny,
  not allow.
