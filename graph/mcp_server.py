"""A dependency-free MCP server (stdio, JSON-RPC 2.0) that answers code-graph queries.

Claude Code launches it from the plugin's ``.mcp.json``. The graph file is
re-read whenever it changes on disk, so the PostToolUse re-index hook keeps the
answers fresh without restarting the server.
"""

from __future__ import annotations

import json
import sys
from collections import deque
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loops"))
import indexer  # noqa: E402
import project  # noqa: E402

PROTOCOL_VERSION = "2025-06-18"
TRAVERSAL = {"calls", "imports", "inherits"}


class CodeGraph:
    def __init__(self, path: Path, root: Path):
        self.path = path
        self.root = root
        self._mtime = -1.0
        self.nodes: Dict[str, dict] = {}
        self.out: Dict[str, List[tuple]] = {}
        self.inc: Dict[str, List[tuple]] = {}
        self.meta: dict = {}

    def ensure_fresh(self) -> None:
        if not self.path.exists():
            indexer.write_graph(indexer.build_graph(self.root), self.path)
        mtime = self.path.stat().st_mtime
        if mtime == self._mtime:
            return
        data = json.loads(self.path.read_text())
        self.nodes = {n["id"]: n for n in data["nodes"]}
        self.out, self.inc = {}, {}
        for e in data["edges"]:
            self.out.setdefault(e["source"], []).append((e["target"], e["kind"]))
            self.inc.setdefault(e["target"], []).append((e["source"], e["kind"]))
        self.meta = {k: data.get(k) for k in ("generated_at", "build_ms", "errors", "root")}
        self.meta["file_count"] = len(data.get("files", {}))
        self._mtime = mtime

    # -- resolution ------------------------------------------------------------
    def resolve(self, ref: str) -> List[str]:
        """Accept an id, a file path, a qualname or a bare name; return matching ids."""
        ref = ref.strip()
        if ref in self.nodes:
            return [ref]
        path = ref[:-1] if ref.endswith("/") else ref
        by_file = [n["id"] for n in self.nodes.values() if n["kind"] == "module" and n["file"] == path]
        if by_file:
            return by_file
        hits = [n["id"] for n in self.nodes.values() if n["qualname"] == ref or n["id"].endswith(":" + ref)]
        if hits:
            return sorted(hits)
        return sorted(n["id"] for n in self.nodes.values() if n["name"] == ref)

    def _one(self, ref: str) -> str:
        ids = self.resolve(ref)
        if not ids:
            raise ToolError(f"No symbol matches {ref!r}. Try the `search` tool first.")
        if len(ids) > 1:
            raise ToolError(f"{ref!r} is ambiguous; use a full id:\n" + "\n".join(f"- {i}" for i in ids[:20]))
        return ids[0]

    def loc(self, node_id: str) -> str:
        n = self.nodes[node_id]
        return f"{n['file']}:{n['line']}"

    def describe(self, node_id: str) -> str:
        n = self.nodes[node_id]
        sig = n.get("signature") or ""
        doc = f" — {n['doc']}" if n.get("doc") else ""
        return f"[{n['kind']}] {node_id}{sig}  ({self.loc(node_id)}){doc}"

    def walk(self, start: Iterable[str], edges: Dict[str, List[tuple]], kinds: Set[str], depth: int) -> Dict[str, int]:
        seen: Dict[str, int] = {s: 0 for s in start}
        queue = deque(seen)
        while queue:
            cur = queue.popleft()
            if seen[cur] >= depth:
                continue
            for nxt, kind in edges.get(cur, []):
                if kind in kinds and nxt not in seen:
                    seen[nxt] = seen[cur] + 1
                    queue.append(nxt)
        return seen

    def members(self, node_id: str) -> List[str]:
        """A module/class plus everything it (transitively) defines."""
        return list(self.walk([node_id], self.out, {"defines"}, 99))


class ToolError(Exception):
    pass


# -- tools ---------------------------------------------------------------------
def tool_overview(g: CodeGraph, args: dict) -> str:
    modules = [n for n in g.nodes.values() if n["kind"] == "module"]
    fan_in = sorted(
        ((sum(1 for _, k in g.inc.get(i, []) if k == "calls"), i) for i, n in g.nodes.items() if n["kind"] != "module"),
        reverse=True,
    )[: int(args.get("top", 10))]
    lines = [f"Graph of {g.meta['root']} built {g.meta['generated_at']} "
             f"({g.meta['file_count']} files, {len(g.nodes)} nodes)", "", "Modules:"]
    for m in sorted(modules, key=lambda m: m["file"]):
        defs = sum(1 for _, k in g.out.get(m["id"], []) if k == "defines")
        deps = [t for t, k in g.out.get(m["id"], []) if k == "imports"]
        tag = " [test]" if m.get("is_test") else ""
        lines.append(f"- {m['file']}{tag}: {defs} top-level defs" + (f"; imports {', '.join(deps)}" if deps else ""))
    lines += ["", "Most-called symbols (change these carefully):"]
    lines += [f"- {count} callers: {g.describe(i)}" for count, i in fan_in if count]
    if g.meta.get("errors"):
        lines += ["", "Files that failed to parse:"] + [f"- {e['file']}:{e['line']} {e['error']}" for e in g.meta["errors"]]
    return "\n".join(lines)


def tool_search(g: CodeGraph, args: dict) -> str:
    q = str(args["query"]).lower()
    kind = args.get("kind")
    scored = []
    for n in g.nodes.values():
        if kind and n["kind"] != kind:
            continue
        hay = f"{n['id']} {n.get('doc', '')}".lower()
        if q in n["name"].lower():
            scored.append((0 if n["name"].lower() == q else 1, n["id"]))
        elif q in hay:
            scored.append((2, n["id"]))
    scored.sort()
    hits = [g.describe(i) for _, i in scored[: int(args.get("limit", 25))]]
    return "\n".join(hits) if hits else f"No symbols match {args['query']!r}."


def tool_symbol(g: CodeGraph, args: dict) -> str:
    node_id = g._one(args["name"])
    n = g.nodes[node_id]
    lines = [g.describe(node_id), f"lines {n['line']}-{n['end_line']}"]
    for title, edges, kinds in (
        ("Defines", g.out, {"defines"}), ("Calls", g.out, {"calls"}), ("Called by", g.inc, {"calls"}),
        ("Imports", g.out, {"imports"}), ("Imported by", g.inc, {"imports"}),
        ("Inherits from", g.out, {"inherits"}), ("Subclassed by", g.inc, {"inherits"}),
    ):
        related = sorted({t for t, k in edges.get(node_id, []) if k in kinds})
        if related:
            lines.append(f"{title}:")
            lines += [f"  - {t}  ({g.loc(t)})" for t in related]
    return "\n".join(lines)


def _traverse(g: CodeGraph, args: dict, edges, verb: str) -> str:
    node_id = g._one(args["name"])
    depth = max(1, min(int(args.get("depth", 1)), 10))
    found = g.walk([node_id], edges, {"calls"}, depth)
    found.pop(node_id)
    if not found:
        return f"No {verb} found for {node_id} (static analysis; dynamic dispatch is not tracked)."
    return "\n".join(f"{'  ' * (d - 1)}- [d{d}] {g.describe(i)}" for i, d in sorted(found.items(), key=lambda x: (x[1], x[0])))


def tool_callers(g: CodeGraph, args: dict) -> str:
    return _traverse(g, args, g.inc, "callers")


def tool_callees(g: CodeGraph, args: dict) -> str:
    return _traverse(g, args, g.out, "callees")


def tool_impact(g: CodeGraph, args: dict) -> str:
    """Reverse transitive closure: what could break if `target` changes."""
    seeds: List[str] = []
    for ref in args["targets"] if isinstance(args.get("targets"), list) else [args["targets"]]:
        for node_id in g.resolve(str(ref)) or []:
            seeds += g.members(node_id)
    if not seeds:
        raise ToolError(f"Nothing matches {args['targets']!r}.")
    depth = max(1, min(int(args.get("depth", 4)), 10))
    reached = g.walk(seeds, g.inc, TRAVERSAL, depth)
    seed_set = set(seeds)
    affected = {i: d for i, d in reached.items() if i not in seed_set}
    files = sorted({g.nodes[i]["file"] for i in affected} | {g.nodes[i]["file"] for i in seed_set})
    tests = sorted({g.nodes[i]["file"] for i in reached if g.nodes[g.nodes[i]["module"]].get("is_test")})
    lines = [f"Changing {len(seed_set)} symbol(s) can affect {len(affected)} other symbol(s) across {len(files)} file(s)."]
    lines += ["", "Tests to run:"] + ([f"- {t}" for t in tests] or ["- none found (consider adding one)"])
    lines += ["", "Affected symbols (by distance):"]
    lines += [f"- [d{d}] {g.describe(i)}" for i, d in sorted(affected.items(), key=lambda x: (x[1], x[0]))[:60]]
    if len(affected) > 60:
        lines.append(f"... and {len(affected) - 60} more (raise precision by passing a symbol instead of a file)")
    return "\n".join(lines)


def tool_path(g: CodeGraph, args: dict) -> str:
    src, dst = g._one(args["source"]), g._one(args["target"])
    prev: Dict[str, Optional[tuple]] = {src: None}
    queue = deque([src])
    while queue:
        cur = queue.popleft()
        if cur == dst:
            break
        for nxt, kind in g.out.get(cur, []):
            if nxt not in prev and kind in TRAVERSAL | {"defines"}:
                prev[nxt] = (cur, kind)
                queue.append(nxt)
    if dst not in prev:
        return f"No static path from {src} to {dst}."
    hops, cur = [], dst
    while prev[cur]:
        parent, kind = prev[cur]
        hops.append(f"{parent} --{kind}--> {cur}")
        cur = parent
    return "\n".join(reversed(hops)) or f"{src} is {dst}."


def tool_mermaid(g: CodeGraph, args: dict) -> str:
    """Module-level dependency diagram, optionally a call graph around one symbol."""
    lines = ["flowchart LR"]
    safe = lambda i: "n" + str(abs(hash(i)) % 10**8)  # noqa: E731
    if args.get("focus"):
        center = g._one(args["focus"])
        ids = set(g.walk([center], g.out, {"calls"}, 2)) | set(g.walk([center], g.inc, {"calls"}, 2))
        for i in sorted(ids):
            lines.append(f'  {safe(i)}["{g.nodes[i]["qualname"]}"]')
        for i in sorted(ids):
            for t, k in g.out.get(i, []):
                if k == "calls" and t in ids:
                    lines.append(f"  {safe(i)} --> {safe(t)}")
        return "\n".join(lines)
    for n in g.nodes.values():
        if n["kind"] == "module" and not n.get("is_test"):
            lines.append(f'  {safe(n["id"])}["{n["file"]}"]')
    for n in g.nodes.values():
        if n["kind"] == "module" and not n.get("is_test"):
            for t, k in g.out.get(n["id"], []):
                if k == "imports" and not g.nodes[t].get("is_test"):
                    lines.append(f"  {safe(n['id'])} --> {safe(t)}")
    return "\n".join(lines)


def tool_reindex(g: CodeGraph, args: dict) -> str:
    graph = indexer.build_graph(g.root)
    indexer.write_graph(graph, g.path)
    return f"Re-indexed {len(graph['files'])} files in {graph['build_ms']} ms ({len(graph['nodes'])} nodes)."


def tool_harness_info(g: CodeGraph, args: dict) -> str:
    """Paths and settings the skills need, so nothing in the plugin is hard-coded to one repo."""
    return json.dumps(project.info(g.root), indent=1)


def tool_test_gate(g: CodeGraph, args: dict) -> str:
    action = str(args.get("action", "status")).lower()
    state = project.data_dir(g.root)
    marker = state / "test-gate"
    if action == "on":
        state.mkdir(parents=True, exist_ok=True)
        marker.write_text("on\n")
    elif action == "off":
        for path in (marker, state / "test-gate.attempts"):
            if path.exists():
                path.unlink()
    elif action != "status":
        raise ToolError("action must be on, off or status")
    command = project.test_command(g.root) or "none found: set \"test\" in .claude/replicable-harness.json"
    return f"test-gate is {'ON' if marker.exists() else 'OFF'}; verification command: {command}"


def tool_drift(g: CodeGraph, args: dict) -> str:
    import graph_drift  # lazy: only needed for this tool

    return graph_drift.run(g.root, g.path)


def _schema(props: dict, required: List[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


NAME = {"type": "string", "description": "Symbol id, qualname, bare name, or repo-relative file path"}
DEPTH = {"type": "integer", "minimum": 1, "maximum": 10}
TOOLS: Dict[str, tuple] = {
    "overview": (tool_overview, "Map of the repository: modules, their imports, and the most-called symbols. Call this first when orienting in an unfamiliar codebase.",
                 _schema({"top": {"type": "integer"}}, [])),
    "search": (tool_search, "Find symbols (modules, classes, functions) by name or docstring substring. Cheaper and more precise than grep for definitions.",
               _schema({"query": {"type": "string"}, "kind": {"type": "string", "enum": ["module", "class", "function", "method"]},
                        "limit": {"type": "integer"}}, ["query"])),
    "symbol": (tool_symbol, "Everything about one symbol: location, signature, docstring, what it calls, who calls it, imports, inheritance.",
               _schema({"name": NAME}, ["name"])),
    "callers": (tool_callers, "Who calls this symbol, optionally transitively (depth).", _schema({"name": NAME, "depth": DEPTH}, ["name"])),
    "callees": (tool_callees, "What this symbol calls, optionally transitively (depth).", _schema({"name": NAME, "depth": DEPTH}, ["name"])),
    "impact": (tool_impact, "Blast radius of changing files or symbols: affected symbols, files and the tests to run. Use before editing and before declaring a change done.",
               _schema({"targets": {"oneOf": [NAME, {"type": "array", "items": NAME}]}, "depth": DEPTH}, ["targets"])),
    "path": (tool_path, "Shortest static dependency path from one symbol/module to another (how does A reach B?).",
             _schema({"source": NAME, "target": NAME}, ["source", "target"])),
    "mermaid": (tool_mermaid, "Mermaid diagram of module dependencies, or of the call neighbourhood around `focus`.",
                _schema({"focus": NAME}, [])),
    "reindex": (tool_reindex, "Rebuild the graph now (normally automatic after edits).", _schema({}, [])),
    "drift": (tool_drift, "Re-index and report structural change since the previous call: new/removed symbols, new dependencies, new import cycles, untested modules.",
              _schema({}, [])),
    "test_gate": (tool_test_gate, "Turn the Stop-hook test gate on/off, or show its status and the project's verification command.",
                  _schema({"action": {"type": "string", "enum": ["on", "off", "status"]}}, [])),
    "harness_info": (tool_harness_info, "Plugin and project paths/settings: project root, data dir, config file, detected test command, script locations.",
                     _schema({}, [])),
}


# -- JSON-RPC plumbing ------------------------------------------------------------
def handle(g: CodeGraph, msg: dict) -> Optional[dict]:
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:  # notification (e.g. notifications/initialized)
        return None
    try:
        if method == "initialize":
            result = {
                "protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL_VERSION),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "code-graph", "version": "0.1.0"},
                "instructions": "Static code graph of this repository (Python today). Prefer `overview`, `symbol`, `impact` over broad grep when you need structure (who calls what, what breaks).",
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [{"name": n, "description": d, "inputSchema": s} for n, (_, d, s) in TOOLS.items()]}
        elif method == "tools/call":
            name = msg["params"]["name"]
            if name not in TOOLS:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"Unknown tool {name}"}}
            g.ensure_fresh()
            fn: Callable = TOOLS[name][0]
            try:
                text, is_error = fn(g, msg["params"].get("arguments") or {}), False
            except (ToolError, KeyError) as exc:
                text, is_error = f"Error: {exc}", True
            result = {"content": [{"type": "text", "text": text}], "isError": is_error}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    except Exception as exc:  # never crash the server on one bad request
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(exc)}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(stdin=sys.stdin, stdout=sys.stdout) -> None:
    root = project.root()
    graph = CodeGraph(indexer.default_out(root), root)
    for line in stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        reply = handle(graph, msg)
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()


if __name__ == "__main__":
    serve()
