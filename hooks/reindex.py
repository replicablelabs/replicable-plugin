"""PostToolUse(Edit|Write): re-index, then tell Claude the blast radius of what it just changed.

This closes the loop "edit -> know what to verify" without Claude having to ask.
"""

from pathlib import Path

from _common import ROOT, emit, load_graph, mcp_server, read_event, rebuild

event = read_event()
tool_input = event.get("tool_input") or {}
file_path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
if not file_path.endswith(".py"):
    raise SystemExit(0)

try:
    rel = Path(file_path).resolve().relative_to(ROOT).as_posix()
except ValueError:
    raise SystemExit(0)  # edited something outside this repository

graph = rebuild()
errors = [e for e in graph["errors"] if e["file"] == rel]
if errors:
    err = errors[0]
    emit({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext":
          f"code-graph: {rel} no longer parses ({err['error']} at line {err['line']}); fix this first."}})
    raise SystemExit(0)

g = load_graph()
try:
    report = mcp_server.tool_impact(g, {"targets": rel, "depth": 3})
except mcp_server.ToolError:
    raise SystemExit(0)
summary = "\n".join(report.splitlines()[:12])
emit({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext":
      f"code-graph re-indexed after editing {rel}.\n{summary}"}})
