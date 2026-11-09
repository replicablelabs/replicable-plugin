---
name: setup
description: Set up or upgrade replicable-harness for this project. Installs graphify's own Claude Code integration (CLAUDE.md rules + PreToolUse search/read hooks), builds the code graph locally, records the test command, and creates or upgrades the project wiki (skeleton, CLAUDE.md schema section, Obsidian settings, CI check, harness_version).
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
   checkout).
7. Wiki and upgrades: run `python3 <scripts.scaffold>` (a dry run: it writes
   nothing). It prints each planned change with a diff, after any WARNING
   about credentials in committed settings files; relay those warnings
   verbatim and never write credentials yourself. Summarise the changes
   (create/update, one line each; show a diff only for files that already
   exist, such as `CLAUDE.md` and `.gitignore`). **Ask before applying**, then
   run it again with `--apply`, and run `python3 <scripts.wiki> check`.
   Existing wiki pages and `raw/` are never modified; only the plugin's own
   files and marked sections are.
8. Finish with one line saying what changed, and that the new files
   (`wiki/`, `CLAUDE.md`, `.gitignore`, `.github/workflows/wiki.yml`,
   `.claude/replicable-harness.json`) are meant to be committed.
