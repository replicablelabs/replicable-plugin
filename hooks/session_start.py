"""SessionStart: make sure graphify can serve this project; say so briefly if it cannot.

The graph-first behaviour itself (CLAUDE.md rules, PreToolUse search/read guards)
comes from graphify's own Claude Code integration, not from this plugin. This
hook only checks the prerequisites and builds the graph the first time, using
`graphify update` (local tree-sitter, no LLM, nothing leaves the machine).
"""

from _common import ROOT, context, project, read_event, trace

read_event()
if not project.graphify():
    text = ("replicable: graphify is not installed, so there is no code graph. "
            f"Tell the user once: `{project.GRAPHIFY_INSTALL}`, then `/replicable:setup`.")
    context("SessionStart", text)
    trace("context", "no_graphify", chars=len(text))
    raise SystemExit(0)

notes, codes = [], []
if not project.graph_file(ROOT).exists():
    try:
        res = project.run_graphify(["update", "."], ROOT, timeout=120)
        ok = res.returncode == 0
        notes.append("built graphify-out/graph.json (code only, no LLM)" if ok
                     else f"`graphify update .` failed: {(res.stderr or res.stdout).strip()[:200]}")
        codes.append("built" if ok else "build_failed")
    except Exception as exc:  # never block a session on this
        notes.append(f"could not build the graph: {exc}")
        codes.append(f"build_error:{type(exc).__name__}")
integration = project.graphify_claude_integration(ROOT)
if not integration:
    notes.append("graphify's Claude Code rules and hooks are not installed for this project; "
                 "suggest `/replicable:setup` to the user once")
    codes.append("no_integration")
if notes:
    text = "replicable: " + "; ".join(notes) + "."
    context("SessionStart", text)
    trace("context", ",".join(codes), chars=len(text), integration=integration)
else:
    trace("skip", "ready", integration=integration)
