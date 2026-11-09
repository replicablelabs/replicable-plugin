---
name: graph-first
description: Use when answering structural questions about this repository — where something is defined, who calls it, how A reaches B, what breaks if X changes, or how the codebase is organised. Query the code-graph MCP tools before grepping or reading whole files.
---

# Graph-first codebase navigation

The `code-graph` MCP server holds a static graph of this repo's Python code
(modules, classes, functions; `defines`, `imports`, `calls`, `inherits`
edges). It is re-indexed automatically after every edit. For code in other
languages it has nothing to say: use Grep/Glob and LSP tools for those.

Pick the cheapest tool that answers the question:

| Question | Tool |
| --- | --- |
| "What is in this repo / where do I start?" | `overview` |
| "Where is X defined?" | `search` → `symbol` |
| "Who uses X?" / "What does X depend on?" | `callers` / `callees` (raise `depth` for transitive) |
| "How does A end up calling B?" | `path` |
| "What breaks if I change X?" / "Which tests cover this?" | `impact` |
| "Draw it" | `mermaid` (module map, or `focus` on a symbol) |

Then read **only** the line ranges the graph points you to (`file:line`).

Rules:
1. Static analysis misses dynamic dispatch, `getattr`, callbacks and string
   imports. When the graph says "no callers", confirm with one targeted `Grep`
   before concluding something is dead.
2. Before editing a symbol with callers, run `impact` and list the affected
   tests in your plan.
3. Cite locations as `path:line` so the user can click them.
