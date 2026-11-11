"""Shared helpers for the plugin's command hooks (stdlib only)."""

from __future__ import annotations

import atexit
import json
import os
import sys
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "harness"))
import project  # noqa: E402

ROOT = project.root()
STATE = project.data_dir(ROOT)

_STARTED = time.monotonic()
_HOOK = Path(sys.argv[0]).stem if sys.argv and sys.argv[0] else "unknown"
_EVENT: dict = {}
_TRACED = False


def read_event() -> dict:
    global _EVENT
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        event = {}
    _EVENT = event if isinstance(event, dict) else {}
    return _EVENT


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload))


def context(event: str, text: str) -> None:
    emit({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}})


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes", "on"}


# -- tracing -------------------------------------------------------------------
# Opt-in record of what each hook decided and why, for the benchmark (docs/benchmarking.md).
# REPLICABLE_TRACE=1 appends to <data dir>/trace.jsonl; any other value is the file to append to.
# One JSON line per hook run: reason codes and counts only, never command output or file contents.
OUTCOMES = ("skip", "context", "pass", "block", "gave_up", "error", "untraced")


def trace_path():
    target = os.environ.get("REPLICABLE_TRACE", "").strip()
    if not target or target.lower() in {"0", "false", "no", "off"}:
        return None
    return STATE / "trace.jsonl" if target.lower() in {"1", "true", "yes", "on"} else Path(target)


def trace(outcome: str, reason: str, **fields) -> None:
    """Record this hook run's outcome. Never raises, and does nothing unless REPLICABLE_TRACE is set."""
    global _TRACED
    _TRACED = True
    try:
        path = trace_path()
        if path is None:
            return
        row = {
            "ts": round(time.time(), 3),
            "hook": _HOOK,
            "event": _EVENT.get("hook_event_name"),
            "session_id": _EVENT.get("session_id"),
            "tool_use_id": _EVENT.get("tool_use_id"),
            "outcome": outcome,
            "reason": reason,
            "duration_ms": round((time.monotonic() - _STARTED) * 1000),
            "plugin_version": project.plugin_version(),
        }
        row.update(fields)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    except Exception:
        pass


def _record_crash(kind, value, tb):
    if not _TRACED:
        trace("error", f"exception:{kind.__name__}")  # the type only: messages can hold paths or secrets
    _previous_excepthook(kind, value, tb)


def _record_untraced():
    if not _TRACED:
        trace("untraced", "no_trace_call")


_previous_excepthook = sys.excepthook
sys.excepthook = _record_crash
atexit.register(_record_untraced)
