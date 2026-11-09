---
name: epcc
description: Explore → Plan → Code → Commit. Anthropic's recommended workflow for non-trivial changes. Use when the user asks for a feature or refactor that touches more than one function.
argument-hint: <what to build or change>
---

# Explore → Plan → Code → Commit

Task: $ARGUMENTS

1. **Explore (no edits).** Use the `graph-explorer` subagent for broad
   questions so the main context stays clean. Locate the entry points with
   `overview`/`search`, then `symbol` and `impact` on everything you expect to
   touch. Read only the relevant line ranges.
2. **Plan.** Write a short plan: files and symbols to change, the blast radius
   from `impact`, the tests that prove it works (existing ones and new ones).
   For anything risky, stop and show the plan before writing code. Think hard
   about edge cases here, not later.
3. **Code.** Implement in small steps. After each edit the PostToolUse hook
   tells you the new blast radius and tests to run — run them. Keep going
   until they pass.
4. **Commit.** Run the full suite, review your own diff (or ask the `reviewer`
   subagent), then commit with a message that explains *why*. Only push or
   open a PR if the user asked.
