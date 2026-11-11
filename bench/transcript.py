"""Normalise a Claude Code session transcript into the events the metrics need.

The transcript format is internal to Claude Code and changes between versions,
so this parser reads only the fields it needs, skips anything it does not
recognise, and never fails on a bad line. Fields used (Claude Code 2.1.x):

    assistant   message.id, message.model, message.usage, message.content[tool_use]
    user        message.content[tool_result] (tool_use_id, content, is_error);
                a string content with <command-name>/x</command-name> is a slash command
    attachment  hook_* (hookName, hookEvent, toolUseID, durationMs, exitCode, content)
    system      stop_hook_summary (hookInfos[].durationMs, preventedContinuation), *compact*
    api-request timestamp (when a model request started)

Subagents live next to the session: ``subagents/agent-<id>.jsonl`` and
``agent-<id>.meta.json`` (agentType, toolUseId).

Only names and sizes are kept: never file contents, commands' arguments or messages.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

PARSER_VERSION = "1"
COMMAND = re.compile(r"<command-name>/?([^<\s]+)</command-name>")
SCRIPT = re.compile(r"([\w.-]+\.(?:py|sh|js|ts|mjs|rb))")
MULTI = {"graphify", "git", "npm", "pnpm", "yarn", "uv", "cargo", "go", "docker", "make", "gh", "npx", "bun", "just"}


def ts(value) -> Optional[float]:
    if not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _lines(path: Path):
    try:
        with path.open() as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    yield entry
    except OSError:
        return


def bash_detail(command: str) -> str:
    """A privacy-safe label for a shell command: the program and, for multi-command tools, its subcommand."""
    if not isinstance(command, str):
        return "?"
    line = command.strip().splitlines()[0] if command.strip() else ""
    for sep in ("&&", ";", "|"):
        parts = [p.strip() for p in line.split(sep)]
        if parts and parts[0].startswith("cd ") and len(parts) > 1:
            line = sep.join(parts[1:]).strip()
    tokens = [t for t in line.split() if not re.match(r"^[A-Z_][A-Z0-9_]*=", t)]
    if not tokens:
        return "?"
    program = tokens[0].rsplit("/", 1)[-1]
    if program.startswith("python") and len(tokens) > 2 and tokens[1] == "-m":
        return f"python -m {tokens[2]}"
    if program in MULTI and len(tokens) > 1 and re.match(r"^[a-z][\w:-]*$", tokens[1]):
        return f"{program} {tokens[1]}"
    return program


def tool_detail(name: str, tool_input) -> Optional[str]:
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if name == "Skill":
        return str(tool_input.get("skill") or tool_input.get("command") or "?")
    if name in ("Agent", "Task"):
        return str(tool_input.get("subagent_type") or "general-purpose")
    if name == "Bash":
        return bash_detail(tool_input.get("command", ""))
    if name in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        suffix = Path(path).suffix
        return suffix or "(none)"
    return None


def _content_chars(content) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, dict):
                total += len(block.get("text") or "") if block.get("type") == "text" else 0
            elif isinstance(block, str):
                total += len(block)
        return total
    return 0


def _parse_file(path: Path, agent: Optional[str], out: dict) -> None:
    last_api_start: Optional[float] = None
    last_ts: Optional[float] = None
    requests: Dict[str, dict] = out["_requests"]
    for entry in _lines(path):
        kind = entry.get("type")
        t = ts(entry.get("timestamp"))
        if t is not None:
            out["first_ts"] = t if out["first_ts"] is None else min(out["first_ts"], t)
            out["last_ts"] = t if out["last_ts"] is None else max(out["last_ts"], t)
        if kind == "api-request":
            last_api_start = t
            continue
        if kind == "assistant":
            msg = entry.get("message") if isinstance(entry.get("message"), dict) else {}
            mid = msg.get("id") or entry.get("uuid")
            req = requests.get(mid)
            if req is None:
                req = requests[mid] = {"id": mid, "agent": agent, "model": msg.get("model"),
                                       "start_ts": last_api_start if last_api_start is not None else (last_ts or t),
                                       "end_ts": t, "tool_use_ids": [], "usage": {}}
                last_api_start = None
            if t is not None:
                req["end_ts"] = max(req["end_ts"] or t, t)
            if isinstance(msg.get("usage"), dict):
                req["usage"] = msg["usage"]
            for block in msg.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = block.get("name") or "?"
                    call = {"id": block.get("id"), "name": name, "detail": tool_detail(name, block.get("input")),
                            "agent": agent, "request_id": mid, "start_ts": t, "end_ts": None,
                            "result_chars": 0, "is_error": False}
                    out["tool_calls"].append(call)
                    out["_calls"][call["id"]] = call
                    req["tool_use_ids"].append(call["id"])
                    if name == "Skill" and agent is None:
                        out["skills"].append({"skill": call["detail"], "start_ts": t, "via": "tool",
                                              "tool_use_id": call["id"]})
        elif kind == "user":
            msg = entry.get("message") if isinstance(entry.get("message"), dict) else {}
            content = msg.get("content")
            text = content if isinstance(content, str) else " ".join(
                b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")
            m = COMMAND.search(text or "")
            if m and agent is None:
                out["skills"].append({"skill": m.group(1), "start_ts": t, "via": "slash", "tool_use_id": None})
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    call = out["_calls"].get(block.get("tool_use_id"))
                    if call is not None:
                        call["end_ts"] = t
                        call["result_chars"] = _content_chars(block.get("content"))
                        call["is_error"] = bool(block.get("is_error"))
        elif kind == "attachment":
            att = entry.get("attachment") if isinstance(entry.get("attachment"), dict) else {}
            atype = str(att.get("type") or "")
            if atype.startswith("hook_"):
                name = str(att.get("hookName") or att.get("hookEvent") or "?")
                if atype in ("hook_additional_context", "hook_system_message"):
                    out["hook_context"].append({"name": name, "tool_use_id": att.get("toolUseID"), "agent": agent,
                                                "chars": _content_chars(att.get("content")), "kind": atype, "ts": t})
                else:
                    try:
                        ms = int(float(att.get("durationMs"))) if att.get("durationMs") is not None else None
                    except (TypeError, ValueError):
                        ms = None
                    out["hooks"].append({"name": name, "event": att.get("hookEvent"), "kind": atype,
                                         "tool_use_id": att.get("toolUseID"), "agent": agent, "ms": ms,
                                         "exit_code": att.get("exitCode"), "command": _script(att.get("command")),
                                         "ts": t})
            if "compact" in atype:
                out["compactions"] += 1
        elif kind == "system":
            sub = str(entry.get("subtype") or "")
            if sub == "stop_hook_summary":
                infos = entry.get("hookInfos") or []
                for info in infos if isinstance(infos, list) else []:
                    if isinstance(info, dict):
                        out["hooks"].append({"name": f"Stop:{_script(info.get('command')) or '?'}", "event": "Stop",
                                             "kind": "stop_summary", "tool_use_id": None, "agent": agent,
                                             "ms": info.get("durationMs"), "exit_code": None,
                                             "command": _script(info.get("command")), "ts": t})
                if entry.get("preventedContinuation"):
                    out["stop_blocks_prevented"] += 1
                errors = entry.get("hookErrors") or []
                out["stop_hook_errors"] += len(errors) if isinstance(errors, list) else 0
            if "compact" in sub:
                out["compactions"] += 1
        if t is not None:
            last_ts = t


def _script(command) -> Optional[str]:
    if not isinstance(command, str):
        return None
    m = SCRIPT.findall(command)
    return m[-1] if m else command.split()[0] if command.split() else None


def parse(session: Path) -> dict:
    out = {"parser_version": PARSER_VERSION, "first_ts": None, "last_ts": None, "tool_calls": [], "hooks": [],
           "hook_context": [], "skills": [], "subagents": [], "compactions": 0, "stop_blocks_prevented": 0,
           "stop_hook_errors": 0, "_requests": {}, "_calls": {}}
    if session is None or not Path(session).exists():
        out["missing"] = True
        return _finish(out)
    session = Path(session)
    _parse_file(session, None, out)
    # A collected run keeps them beside the copy; Claude Code keeps them in <session-id>/subagents/.
    sub_dir = next((d for d in (session.parent / "subagents", session.with_suffix("") / "subagents") if d.is_dir()), None)
    if sub_dir is not None:
        for path in sorted(sub_dir.glob("agent-*.jsonl")):
            agent_id = path.stem[len("agent-"):]
            meta = {}
            try:
                meta = json.loads(path.with_name(path.stem + ".meta.json").read_text())
            except (OSError, ValueError):
                pass
            before = (out["first_ts"], out["last_ts"])
            out["first_ts"], out["last_ts"] = None, None
            _parse_file(path, agent_id, out)
            start, end = out["first_ts"], out["last_ts"]
            out["subagents"].append({"agent_id": agent_id, "type": meta.get("agentType") or "?",
                                     "tool_use_id": meta.get("toolUseId"), "start_ts": start, "end_ts": end})
            firsts = [x for x in (before[0], start) if x is not None]
            lasts = [x for x in (before[1], end) if x is not None]
            out["first_ts"], out["last_ts"] = (min(firsts) if firsts else None), (max(lasts) if lasts else None)
    return _finish(out)


def _finish(out: dict) -> dict:
    requests = []
    for req in out.pop("_requests").values():
        u = req.pop("usage") or {}
        cache = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
        req.update(input=int(u.get("input_tokens") or 0), cache_read=int(u.get("cache_read_input_tokens") or 0),
                   cache_write=int(u.get("cache_creation_input_tokens") or 0),
                   cache_write_1h=int(cache.get("ephemeral_1h_input_tokens") or 0),
                   output=int(u.get("output_tokens") or 0))
        requests.append(req)
    out.pop("_calls", None)
    out["requests"] = sorted(requests, key=lambda r: (r["start_ts"] or 0))
    out["skills"].sort(key=lambda s: s["start_ts"] or 0)
    return out
