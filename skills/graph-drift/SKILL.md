---
name: graph-drift
description: Re-index the code graph and report what changed structurally since the last check (new/removed symbols, new dependencies, new import cycles, modules with no tests). Designed to run on a schedule with /loop.
---

Call the code-graph `drift` tool.

Report the result in at most 8 lines. If it shows a new import cycle, a new
high fan-in symbol, or new code with no test coverage, say what you would do
about it, but do not edit anything unless the user asked.
