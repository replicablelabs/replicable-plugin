"""SessionStart: make sure graphify can serve this project; say so briefly if it cannot.

The graph-first behaviour itself (CLAUDE.md rules, PreToolUse search/read guards)
comes from graphify's own Claude Code integration, not from this plugin. This
hook only checks the prerequisites and builds the graph the first time, using
`graphify update` (local tree-sitter, no LLM, nothing leaves the machine).
"""

from _common import ROOT, context, project, read_event

read_event()
if not project.graphify():
    context("SessionStart", "replicable-harness: graphify is not installed, so there is no code graph. "
            f"Tell the user once: `{project.GRAPHIFY_INSTALL}`, then `/replicable-harness:setup`.")
    raise SystemExit(0)

notes = []
if not project.graph_file(ROOT).exists():
    try:
        res = project.run_graphify(["update", "."], ROOT, timeout=120)
        notes.append("built graphify-out/graph.json (code only, no LLM)" if res.returncode == 0
                     else f"`graphify update .` failed: {(res.stderr or res.stdout).strip()[:200]}")
    except Exception as exc:  # never block a session on this
        notes.append(f"could not build the graph: {exc}")
if not project.graphify_claude_integration(ROOT):
    notes.append("graphify's Claude Code rules and hooks are not installed for this project; "
                 "suggest `/replicable-harness:setup` to the user once")
if notes:
    context("SessionStart", "replicable-harness: " + "; ".join(notes) + ".")
