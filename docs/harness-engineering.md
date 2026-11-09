# Harness engineering

The **harness** is the software around a model that turns it into an agent: it
runs the loop, provides tools, manages the context window, enforces
permissions and runs hooks. Claude Code is a harness. **Harness engineering**
is customising it so an agent produces better work for less effort: more
precise context, cheaper and safer actions, and verification it cannot skip.

This plugin is harness engineering on top of Claude Code. See
[hooks.md](hooks.md) for the hook catalogue and metrics.

## The layers

| Layer | Question it answers | Typical mechanisms |
| --- | --- | --- |
| **Context** | What does the model see? | `CLAUDE.md`, SessionStart and UserPromptSubmit hooks, the code graph, the wiki index |
| **Tools** | What can it do? | Built-in tools, MCP servers |
| **Verification** | How does it find out it is wrong? | PostToolUse and Stop hooks, tests, type checks |
| **Control flow** | When may it stop, and what runs next? | Stop hook, outer loops, workflows |
| **Memory** | What survives across contexts? | Progress and plan files, git, the wiki |
| **Orchestration** | Who does what, in what order? | Subagents, workflows, worktrees, the plan graph |
| **Guardrails** | What is it not allowed to do, and at what cost? | Permissions, PreToolUse hooks, budgets, isolation |

Verification is the layer most often missing. An agent with perfect context
and no fast check still ships regressions. An agent with average context and a
check that runs in seconds and must pass corrects itself.

## Loops

### The inner loop (owned by Claude Code)

```
prompt ─► model ─► tool call ─► result ─► model ─► … ─► model stops calling tools ─► turn ends
```

The loop ends when **the model believes** it is done, not when the work is
verifiably done. Closing that gap is the point of most harness engineering.

The inner loop is not replaced; it is extended at three points:

| Point | What changes | Mechanism |
| --- | --- | --- |
| What goes **in** | The context the model sees | SessionStart, UserPromptSubmit, PostToolUse `additionalContext` |
| What it can **do** | The actions available, and which are allowed | MCP tools, permissions, PreToolUse |
| When it may **stop** | The exit condition | The Stop hook: "done" becomes "checks pass" |

Other ways to improve it:

| Lever | Deterministic? |
| --- | --- |
| Hooks | Yes: the harness runs them every time |
| Permissions (allow and deny rules, permission mode) | Yes |
| Tools (MCP) | The tool is; whether the model uses it is not |
| Instructions (`CLAUDE.md`, skills, system prompt) | No: they are guidance |
| Subagents | The structure is; their contents are not |
| Model and effort | — |

> **Rule of thumb:** if something must happen every time, make it a hook. If
> it is guidance that needs judgement, make it an instruction.

### Outer loops (built around Claude Code)

Outer loops decide **when a whole inner loop runs and with what context**.
Hooks act inside a run; outer loops act between runs.

| Loop | Trigger | State | Done when | In this plugin |
| --- | --- | --- | --- | --- |
| `/loop` | A timer | Whatever the prompt reads | Each tick reports | Documented use (e.g. checking graphify's `god-nodes`) |
| Routines (`/schedule`) | A cloud schedule | Repository and outputs | Each run reports | Documented use |
| Stop-hook loop | The end of a turn | The transcript | Checks pass, or the retry cap is hit | `test_gate` |
| Headless "Ralph" loop | The previous iteration ended | `PROGRESS.md` and git commits | Tests pass **and** the completion promise is printed | `loops/headless_loop.py` |
| Workflow fan-out | One request | Structured results | The synthesis is written | Not bundled (graphify's report and wiki export cover the architecture-document case) |

### Loop engineering

There is no standard definition of the term. A working one: **designing the
feedback loop an agent runs in.** Every loop answers seven questions:

| Question | Weak answer | Strong answer |
| --- | --- | --- |
| **Trigger:** what starts an iteration? | A human, by hand | An event, a timer or a failing check |
| **State:** what carries over? | The growing transcript | Files and git: progress notes, the plan, commits |
| **Verification:** how do we know it worked? | The model says so | An objective signal: tests, types, a smoke run |
| **Speed:** how long does one check take? | A deployment (minutes) | Seconds, run locally |
| **Termination:** when does it stop? | When the model feels done, or never | The check passes and done is declared, or a cap is hit |
| **Budget and guardrails:** what limits it? | Nothing | Iterations, money, a worktree, a tool allowlist |
| **Escalation:** when does a human step in? | After the damage | After N failures, or before irreversible steps |

Common symptoms and the weak answer behind each:
- **Regressions:** verification is "the model says so".
- **Slow iteration:** the only check is a deploy.
- **Every session starts from zero:** state lives only in the transcript.

## Three graphs that feed the harness

Each graph provides a different kind of context, and each needs a different
strategy for staying fresh.

### Code graph: what a change touches

A static graph of modules, classes, functions and their `imports`, `calls`,
`inherits` and `defines` edges. It doesn't make code faster to *understand*.
It makes code cheaper to *navigate*: "who calls this?", "what breaks?", "how
does A reach B?" get exact answers, then the agent reads **only the lines
the graph points to**.

- **Freshness:** derived from the code by a machine, so re-index on every edit.
  A stale graph is worse than none, because the agent trusts it.
- **Value scales with size:** marginal on a small repository, essential on a large one.
- **In this plugin** the graph is [graphify](https://github.com/Graphify-Labs/graphify)'s:
  tree-sitter parsing of about 40 languages, edges tagged `EXTRACTED` or
  `INFERRED`, queried with `graphify query`, `path`, `explain` and `affected`.
- **Related approaches:** grep → tree-sitter repository maps → language servers
  (LSP) → a full graph.
- **Limits:** static analysis misses dynamic dispatch, reflection and string
  imports. Confirm "no callers" with a targeted search.

### Code wiki: what a change must respect

Linked pages (Obsidian-style) with one small **index** that the agent always
reads and pages it loads **on demand**. This is progressive disclosure, the
same pattern Claude Code uses for skills (description always loaded, body on
use) and `CLAUDE.md` links.

- **Document why, not what.** The code says what it does. The wiki is the only
  home for decisions, constraints, domain rules and known traps.
- **Freshness is the main way it fails.** Link pages to `path:line`, regenerate
  pages that describe structure, and flag pages whose linked code changed.
  Hand-written pages about intent age slowly; hand-written pages about
  structure age fast.
- **Divide the work with the code graph:** the graph is accurate about
  structure, the wiki is the source of intent. The wiki should link to code
  and leave structure to the graph.

### Feature planning graph: what a change is for, and when it is done

A plan expressed as a dependency graph (a DAG) of work items. It serves the
orchestration layer: independent nodes run **in parallel** (separate
subagents or worktrees), dependent nodes run **in sequence**.

- **Every node needs its own acceptance check** (a test or check that passes).
  Without it you parallelise "the model says done".
- **Fix interfaces first.** Parallel nodes that share an API or schema
  conflict unless the contract is its own first node.
- **The plan is a hypothesis:** re-plan when a node fails.
- **The plan is memory:** stored on disk with each node's status, it lets work
  survive fresh contexts and compaction.
- **Review the plan, not only the diff.** A plan is the cheapest place to
  catch a wrong approach.

### How they work together in the loop

| Graph | Role in the loop | Kept fresh by |
| --- | --- | --- |
| Code graph | Makes edit → verify precise: blast radius, affected tests | Re-indexing on every edit |
| Wiki | Supplies constraints before acting; captures lessons after | Staleness flags and periodic regeneration |
| Plan graph | Gives each inner loop a scope and an exit condition; gives the orchestrator its order | Status updates at each node's acceptance |

## Where to start

In order of return on effort for a team with regressions and slow iteration:

1. **Fast local checks the agent must pass** (tests, types, lint, a smoke
   run), enforced with hooks rather than instructions.
2. **A `CLAUDE.md`** with commands, conventions and known traps.
3. **The plan graph** for multi-module features.
4. **The wiki** for decisions and domain knowledge.
5. **The code graph** once the codebase is too large to hold in your head.

Measure each step (see [hooks.md](hooks.md#metrics)) and keep only what moves a metric.
