#!/usr/bin/env python3
"""A stand-in for `claude -p` for the benchmark tests: no API calls.

It behaves like a tiny agent run: edits calc.py according to FAKE_CLAUDE_MODE
(solve | noop | break), runs the plugin's real hooks when --plugin-dir is given
(so REPLICABLE_TRACE is exercised), writes a transcript where Claude Code would
(CLAUDE_CONFIG_DIR/projects/<slug>/<session>.jsonl, with one subagent) and
prints stream-json events.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

if "--version" in sys.argv:
    print("9.9.9 (Claude Code)")
    raise SystemExit(0)

args = sys.argv[1:]


def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default


prompt, sid, plugin_dir = opt("-p", ""), opt("--session-id"), opt("--plugin-dir")
model = opt("--model", "fake-model")
cwd = Path.cwd().resolve()
mode = os.environ.get("FAKE_CLAUDE_MODE", "solve")
t0 = time.time()


def iso(offset):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0 + offset)) + ".%03dZ" % int(((t0 + offset) % 1) * 1000)


def usage(inp, cr, cw, out):
    return {"input_tokens": inp, "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw, "output_tokens": out,
            "cache_creation": {"ephemeral_1h_input_tokens": cw, "ephemeral_5m_input_tokens": 0}}


def hook(name, event):
    if not plugin_dir:
        return None
    payload = dict(event, session_id=sid)
    res = subprocess.run([sys.executable, str(Path(plugin_dir) / "hooks" / name)], input=json.dumps(payload),
                         capture_output=True, text=True, cwd=cwd, env=dict(os.environ, CLAUDE_PROJECT_DIR=str(cwd)))
    return res.stdout.strip()


calc = cwd / "calc.py"
if mode == "solve":
    calc.write_text(calc.read_text() + "\n\ndef double(x):\n    return 2 * x\n")
elif mode == "break":
    calc.write_text("def add(a, b):\n    return a - b\n")

hook("session_start.py", {"hook_event_name": "SessionStart"})
edit_out = hook("post_edit.py", {"hook_event_name": "PostToolUse", "tool_use_id": "toolu_edit", "tool_name": "Edit",
                                 "tool_input": {"file_path": str(calc)}}) if mode != "noop" else None
hook("test_gate.py", {"hook_event_name": "Stop"})

user_content = prompt
m = re.match(r"^/(\S+)\s+(.*)$", prompt, re.S)
if m:
    user_content = f"<command-message>{m.group(1)}</command-message>\n<command-name>/{m.group(1)}</command-name>\n<command-args>{m.group(2)}</command-args>"

entries = [
    {"type": "attachment", "timestamp": iso(0.0), "attachment": {"type": "hook_success", "hookName": "SessionStart:startup",
                                                                 "hookEvent": "SessionStart", "durationMs": "40", "exitCode": "0"}},
    {"type": "user", "timestamp": iso(0.1), "message": {"role": "user", "content": user_content}},
    {"type": "api-request", "timestamp": iso(0.2), "id": "r1"},
    {"type": "assistant", "timestamp": iso(1.0), "message": {"id": "msg_1", "model": model, "usage": usage(2, 1000, 500, 100),
     "content": [{"type": "tool_use", "id": "toolu_agent", "name": "Agent", "input": {"subagent_type": "replicable:graph-explorer"}}]}},
    {"type": "user", "timestamp": iso(3.0), "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_agent", "content": "x" * 400}]}},
    {"type": "api-request", "timestamp": iso(3.1), "id": "r2"},
    {"type": "assistant", "timestamp": iso(4.0), "message": {"id": "msg_2", "model": model, "usage": usage(2, 1500, 200, 200),
     "content": [{"type": "tool_use", "id": "toolu_edit", "name": "Edit", "input": {"file_path": str(calc)}},
                 {"type": "tool_use", "id": "toolu_bash", "name": "Bash", "input": {"command": "cd x && python3 -m unittest -q"}}]}},
    {"type": "attachment", "timestamp": iso(4.6), "attachment": {"type": "hook_success", "hookName": "PostToolUse:Edit",
     "hookEvent": "PostToolUse", "toolUseID": "toolu_edit", "durationMs": "300", "exitCode": "0"}},
    {"type": "user", "timestamp": iso(4.6), "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_edit", "content": "e" * 100}]}},
    {"type": "user", "timestamp": iso(5.0), "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_bash", "content": "b" * 300}]}},
    {"type": "api-request", "timestamp": iso(5.1), "id": "r3"},
    {"type": "assistant", "timestamp": iso(6.0), "message": {"id": "msg_3", "model": model, "usage": usage(2, 2100, 100, 50),
     "content": [{"type": "text", "text": "Done."}]}},
    {"type": "system", "timestamp": iso(6.1), "subtype": "stop_hook_summary",
     "hookInfos": [{"command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/test_gate.py\"", "durationMs": 25}],
     "hookErrors": [], "preventedContinuation": False},
]
sub = [
    {"type": "user", "timestamp": iso(1.1), "isSidechain": True, "message": {"role": "user", "content": "find add"}},
    {"type": "assistant", "timestamp": iso(2.0), "isSidechain": True, "message": {"id": "msg_s1", "model": "fake-small",
     "usage": usage(2, 0, 800, 60), "content": [{"type": "tool_use", "id": "toolu_s1", "name": "Bash",
                                                 "input": {"command": "graphify explain add"}}]}},
    {"type": "user", "timestamp": iso(2.5), "isSidechain": True, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_s1", "content": "g" * 200}]}},
    {"type": "assistant", "timestamp": iso(2.9), "isSidechain": True, "message": {"id": "msg_s2", "model": "fake-small",
     "usage": usage(2, 800, 100, 40), "content": [{"type": "text", "text": "add is in calc.py"}]}},
]
config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
project = config / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
(project / sid / "subagents").mkdir(parents=True, exist_ok=True)
(project / f"{sid}.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))
(project / sid / "subagents" / "agent-a1.jsonl").write_text("".join(json.dumps(e) + "\n" for e in sub))
(project / sid / "subagents" / "agent-a1.meta.json").write_text(json.dumps(
    {"agentType": "replicable:graph-explorer", "toolUseId": "toolu_agent"}))

events = [
    {"type": "system", "subtype": "init", "claude_code_version": "9.9.9", "model": model, "permissionMode": "acceptEdits",
     "plugins": [{"name": "replicable", "version": "x"}] if plugin_dir else [], "mcp_servers": [], "skills": [], "agents": []},
    {"type": "assistant", "message": {"id": "msg_3", "content": [{"type": "text", "text": "Done."}]}},
    {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 6000, "duration_api_ms": 5000, "num_turns": 3,
     "total_cost_usd": 0.05, "stop_reason": "end_turn", "terminal_reason": "completed", "permission_denials": [],
     "credential_expires_at": "never-store-me",
     "usage": {"input_tokens": 6, "cache_read_input_tokens": 4600, "cache_creation_input_tokens": 800, "output_tokens": 350},
     "modelUsage": {model: {"costUSD": 0.04}, "fake-small": {"costUSD": 0.01}}},
]
for e in events:
    print(json.dumps(e), flush=True)
