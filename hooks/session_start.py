"""SessionStart: make sure the graph exists and hand Claude a compact map of the repo."""

from _common import emit, load_graph, mcp_server, read_event, rebuild

event = read_event()
graph = rebuild()  # ~30 ms here; always start from a fresh index
g = load_graph()
overview = mcp_server.tool_overview(g, {"top": 8})
emit({
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": (
            "The code-graph MCP server indexes this repository (tools: overview, search, symbol, callers, "
            "callees, impact, path, mermaid). Use it for structural questions before grepping, and run "
            "`impact` before editing shared code. Current map:\n\n" + overview
        ),
    }
})
