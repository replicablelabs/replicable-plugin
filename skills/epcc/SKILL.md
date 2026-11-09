---
name: epcc
description: Explore → Plan → Code → Commit. Anthropic's recommended workflow for non-trivial changes. Use when the user asks for a feature or refactor that touches more than one function.
argument-hint: <what to build or change>
---

# Explore → Plan → Code → Commit

Task: $ARGUMENTS

1. **Explore (no edits).** Use the `graph-explorer` subagent for broad
   questions so the main context stays clean. Orient with graphify:
   `graphify query "<question>"`, `graphify explain "<symbol>"`, and
   `graphify affected "<file or symbol>"` for everything you expect to touch.
   Read only the line ranges the graph points to.
2. **Plan.** Write a short plan: files and symbols to change, the blast radius
   from `graphify affected`, and the tests that prove it works (existing and
   new). For anything risky, stop and show the plan before writing code.
3. **Code.** Implement in small steps. After each edit the PostToolUse hook
   updates the graph and lists the affected tests. Run them.
4. **Commit.** Run the full suite, review your own diff (or ask the `reviewer`
   subagent), then commit with a message that explains *why*. Only push or
   open a PR if the user asked.
