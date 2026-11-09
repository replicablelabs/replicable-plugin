"""Re-index and report structural drift since the last snapshot.

Built to be driven by a loop, e.g. inside Claude Code:

    /loop 15m /replicable-harness:graph-drift

Each run compares the fresh graph with ``snapshot.json`` in the plugin's data
directory for this project (see ``graph/project.py``) and then
replaces the snapshot, so every report covers only what changed since the
previous tick.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Set

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "graph"))
import indexer  # noqa: E402

STRUCTURAL = {"calls", "imports", "inherits"}


def import_cycles(graph: dict) -> List[List[str]]:
    adj: Dict[str, Set[str]] = {}
    for e in graph["edges"]:
        if e["kind"] == "imports":
            adj.setdefault(e["source"], set()).add(e["target"])
    index, low, stack, on_stack, out = {}, {}, [], set(), []

    def strongconnect(v: str) -> None:  # Tarjan's SCC
        index[v] = low[v] = len(index)
        stack.append(v)
        on_stack.add(v)
        for w in adj.get(v, ()):
            if w not in index:
                strongconnect(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            component = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                component.append(w)
                if w == v:
                    break
            if len(component) > 1:
                out.append(sorted(component))

    for v in list(adj):
        if v not in index:
            strongconnect(v)
    return sorted(out)


def untested_modules(graph: dict) -> List[str]:
    nodes = {n["id"]: n for n in graph["nodes"]}
    out: Dict[str, List[str]] = {}
    for e in graph["edges"]:
        if e["kind"] in STRUCTURAL | {"defines"}:
            out.setdefault(e["source"], []).append(e["target"])
    frontier = [i for i, n in nodes.items() if nodes[n["module"]].get("is_test")]
    seen = set(frontier)
    while frontier:
        cur = frontier.pop()
        for nxt in out.get(cur, []):
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    covered = {nodes[i]["module"] for i in seen}
    has_defs = {e["source"] for e in graph["edges"] if e["kind"] == "defines"}
    return sorted(n["file"] for i, n in nodes.items()
                  if n["kind"] == "module" and not n.get("is_test") and i not in covered and i in has_defs)


def fan_in(graph: dict) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for e in graph["edges"]:
        if e["kind"] == "calls":
            counts[e["target"]] = counts.get(e["target"], 0) + 1
    return counts


def diff(old: dict, new: dict, hot: int = 5) -> dict:
    old_ids = {n["id"] for n in old["nodes"]}
    new_ids = {n["id"] for n in new["nodes"]}
    old_edges = {(e["source"], e["target"], e["kind"]) for e in old["edges"] if e["kind"] in STRUCTURAL}
    new_edges = {(e["source"], e["target"], e["kind"]) for e in new["edges"] if e["kind"] in STRUCTURAL}
    old_fan, new_fan = fan_in(old), fan_in(new)
    return {
        "changed_files": sorted(f for f, h in new["files"].items() if old["files"].get(f) != h),
        "removed_files": sorted(set(old["files"]) - set(new["files"])),
        "added_symbols": sorted(new_ids - old_ids),
        "removed_symbols": sorted(old_ids - new_ids),
        "added_dependencies": sorted(f"{s} --{k}--> {t}" for s, t, k in new_edges - old_edges if k == "imports"),
        "added_calls": len([1 for _, _, k in new_edges - old_edges if k == "calls"]),
        "removed_calls": len([1 for _, _, k in old_edges - new_edges if k == "calls"]),
        "new_hotspots": sorted(i for i, c in new_fan.items() if c >= hot and old_fan.get(i, 0) < hot),
        "new_cycles": [c for c in import_cycles(new) if c not in import_cycles(old)],
        "newly_untested": sorted(set(untested_modules(new)) - set(untested_modules(old))),
        "untested": untested_modules(new),
    }


def render(report: dict, first_run: bool) -> str:
    if first_run:
        return ("graph-drift: baseline snapshot saved. Untested modules: "
                + (", ".join(report["untested"]) or "none"))
    keys = [k for k, v in report.items() if v and k != "untested"]
    if not keys:
        return "graph-drift: no structural change since the last tick."
    lines = ["graph-drift since last tick:"]
    for key in keys:
        value = report[key]
        if isinstance(value, list):
            shown = value[:10]
            more = f" (+{len(value) - 10} more)" if len(value) > 10 else ""
            lines.append(f"- {key.replace('_', ' ')}: " + ", ".join(map(str, shown)) + more)
        else:
            lines.append(f"- {key.replace('_', ' ')}: {value}")
    return "\n".join(lines)


def run(root: Path, graph_path: Path) -> str:
    """Re-index ``root``, diff against the previous snapshot, store the new one, return the report."""
    snapshot = graph_path.parent / "snapshot.json"
    new = indexer.build_graph(root)
    indexer.write_graph(new, graph_path)
    first_run = not snapshot.exists()
    old = json.loads(snapshot.read_text()) if not first_run else new
    indexer.write_graph(new, snapshot)
    return render(diff(old, new), first_run)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Report structural drift in the code graph.")
    parser.add_argument("--root", type=Path, default=None)
    args = parser.parse_args(argv)
    root = (args.root or indexer.default_root()).resolve()
    print(run(root, indexer.default_out(root)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
