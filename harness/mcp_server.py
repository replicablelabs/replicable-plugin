"""A small dependency-free MCP server (stdio, JSON-RPC 2.0) for the harness's own state.

Graph queries are not here: graphify provides them (its CLI, and `graphify-mcp`).
This server only exposes what skills need without hard-coded paths.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project  # noqa: E402

PROTOCOL_VERSION = "2025-06-18"


class ToolError(Exception):
    pass


def tool_info(root: Path, args: dict) -> str:
    return json.dumps(project.info(root), indent=1)


def tool_test_gate(root: Path, args: dict) -> str:
    action = str(args.get("action", "status")).lower()
    state = project.data_dir(root)
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
    command = project.test_command(root) or f'none found: set "test" in .claude/{project.CONFIG_NAME}'
    return f"test-gate is {'ON' if marker.exists() else 'OFF'}; verification command: {command}"


def _schema(props: dict) -> dict:
    return {"type": "object", "properties": props, "additionalProperties": False}


TOOLS: Dict[str, tuple] = {
    "info": (tool_info, "Plugin and project settings: project root, test command, test-gate state, whether graphify is installed and set up, script locations.",
                     _schema({})),
    "test_gate": (tool_test_gate, "Turn the Stop-hook test gate on/off, or show its status and the project's verification command.",
                  _schema({"action": {"type": "string", "enum": ["on", "off", "status"]}})),
}


def handle(root: Path, msg: dict) -> Optional[dict]:
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:  # notification
        return None
    try:
        if method == "initialize":
            result = {
                "protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL_VERSION),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": project.MCP_SERVER, "version": project.plugin_version()},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [{"name": n, "description": d, "inputSchema": s} for n, (_, d, s) in TOOLS.items()]}
        elif method == "tools/call":
            name = msg["params"]["name"]
            if name not in TOOLS:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"Unknown tool {name}"}}
            fn: Callable = TOOLS[name][0]
            try:
                text, is_error = fn(root, msg["params"].get("arguments") or {}), False
            except ToolError as exc:
                text, is_error = f"Error: {exc}", True
            result = {"content": [{"type": "text", "text": text}], "isError": is_error}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    except Exception as exc:  # never crash the server on one bad request
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(exc)}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(stdin=sys.stdin, stdout=sys.stdout) -> None:
    root = project.root()
    for line in stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        reply = handle(root, msg)
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()


if __name__ == "__main__":
    serve()
