"""Shared helpers for the plugin's command hooks (stdlib only)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "graph"))
import indexer  # noqa: E402
import mcp_server  # noqa: E402
import project  # noqa: E402

ROOT = project.root()
GRAPH = indexer.default_out(ROOT)
STATE = project.data_dir(ROOT)


def read_event() -> dict:
    try:
        return json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return {}


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload))


def load_graph() -> "mcp_server.CodeGraph":
    graph = mcp_server.CodeGraph(GRAPH, ROOT)
    graph.ensure_fresh()
    return graph


def rebuild() -> dict:
    graph = indexer.build_graph(ROOT)
    indexer.write_graph(graph, GRAPH)
    return graph


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes", "on"}
