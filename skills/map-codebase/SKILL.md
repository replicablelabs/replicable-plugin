---
name: map-codebase
description: Generate an architecture document for this repo by fanning out one subagent per module (seeded with code-graph facts) and synthesising the results. Uses the Workflow tool, so only run it when the user explicitly asks for it.
argument-hint: "[output path, default docs/ARCHITECTURE.md]"
disable-model-invocation: true
---

# Map the codebase with a workflow

1. Call the code-graph `overview` tool. From it, build a JSON array with one
   entry per non-test module that has definitions:
   `{"file": "...", "module": "...", "imports": [...], "hot_symbols": [...]}`
   (`hot_symbols`: that module's entries from "Most-called symbols").
2. Call the code-graph `harness_info` tool and run the Workflow tool with
   `scriptPath` set to its `scripts.map_codebase_workflow`
   and `args: {"modules": <that array>, "out": "$ARGUMENTS" or "docs/ARCHITECTURE.md"}`.
3. When it finishes, show the user the document's path and its table of
   contents. Do not commit it.
