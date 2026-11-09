# replicable-claude

A Claude Code plugin (`replicable-harness`) that extends the harness with a
code-graph MCP server, verification hooks, subagents, skills and loops. It must
work in **any** repository, so nothing here may assume a particular project.

## Commands

- Test: `python3 -m unittest discover -s tests` (stdlib only; must pass on Python 3.9+)
- Validate the plugin: `claude plugin validate .`
- Inventory and token cost: `claude --plugin-dir . plugin details replicable-harness`
- Dogfood on this repo: `claude --plugin-dir .`

## Rules

- Standard library only. No third-party imports anywhere (users install nothing).
- Everything project-specific (root, state dir, config, test command) goes
  through `graph/project.py`. Never hard-code a path to a repository.
- Plugin state lives in the data dir (`~/.cache/replicable-harness/...`), never
  inside the user's repository.
- Hooks must be fast (<1 s typical), exit 0 on anything unexpected, and never
  print secrets. A Stop hook that blocks must have a retry cap.
- Skills reach plugin scripts through the `harness_info` MCP tool, not paths.
- Every behaviour change gets a test in `tests/test_harness.py`.
