# replicable-claude

A Claude Code plugin (`replicable-harness`) that extends the harness with
graph-aware verification hooks, a test gate, subagents, skills and loops. The
code graph comes from graphify (https://github.com/Graphify-Labs/graphify);
this plugin must work in **any** repository and never assume a particular
project.

## Commands

- Test: `python3 -m unittest discover -s tests` (stdlib only; tests that need
  graphify are skipped when it is not installed)
- Validate the plugin: `claude plugin validate .`
- Inventory and token cost: `claude --plugin-dir . plugin details replicable-harness`
- Dogfood on this repo: `claude --plugin-dir .`
- Project wiki CLI (design: `docs/wiki.md`): `python3 harness/wiki.py --root <project> check|index|stale|new`

## Rules

- **Use graphify, don't rebuild it.** Graph building, queries, and the
  graph-first rules and search/read hooks belong to graphify (`graphify claude
  install`). This plugin only adds what graphify lacks, and calls graphify's CLI
  to do it. Before adding a feature, check `graphify --help` for an existing one.
- Hooks run graphify in code-only mode (`graphify update`): local, no LLM.
  Never trigger an LLM extraction from a hook.
- Standard library only in this plugin's own code (Python 3.9+).
- Everything project-specific (root, state dir, config, test command, graphify
  status) goes through `harness/project.py`. Never hard-code a repository path.
- Plugin state lives in the data dir (`~/.cache/replicable-harness/...`), never
  inside the user's repository. (graphify's `graphify-out/` is graphify's.)
- Hooks must be fast, exit 0 on anything unexpected, and never print secrets.
  A Stop hook that blocks must have a retry cap.
- Skills reach plugin scripts through the `harness_info` MCP tool, not paths.
- Every behaviour change gets a test in `tests/test_harness.py`.
