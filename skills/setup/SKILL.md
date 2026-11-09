---
name: setup
description: Set up replicable-harness for this project. Installs graphify's own Claude Code integration (CLAUDE.md rules + PreToolUse search/read hooks), builds the code graph locally, and records the test command.
disable-model-invocation: true
---

# Set up the harness for this project

1. Call the harness `harness_info` tool and show the user the result in
   4-6 lines: project root, detected test command, test gate, and the
   `graphify` block.
2. If `graphify.cli` is null, stop and give the user the install command
   from `graphify.install`. Do not install it yourself.
3. If `graphify.claude_integration` is false, explain that
   `graphify claude install` adds a `## graphify` section to `CLAUDE.md` and
   PreToolUse hooks to `.claude/settings.json` (both project files the user
   may want to commit). **Ask before running it**, then run it.
4. If `graphify.graph_exists` is false, run `graphify update .`. It parses
   code locally with tree-sitter and sends nothing to an LLM. Do **not** run
   the `/graphify` skill's full extraction unless the user asks: that pass
   sends documents and images to an LLM backend.
5. If `test_command` is null or wrong, ask the user for the right command
   and write it to `.claude/replicable-harness.json` as `{"test": "..."}`.
6. Optionally offer `graphify hook install` (rebuilds the graph on commit and
   checkout) and adding `graphify-out/cache/` to `.gitignore`.
7. Finish with one line saying what changed.
