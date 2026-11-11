"""Run Claude Code headless on one task and keep everything it leaves behind.

Saved in the run directory (bench cache only; it holds the dataset's code):
    stream.jsonl      every stream-json event, wrapped as {"t": <receive time>, "e": <event>}
    transcript/       Claude Code's session transcript and its subagent transcripts
    trace.jsonl       the plugin's hook trace (REPLICABLE_TRACE)
    stderr.txt
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import List, Optional

from common import claude_bin, claude_config_dir, clean_env, project_slug


ALWAYS_DISALLOWED = ["Bash(git push:*)", "Bash(gh:*)"]


def command(prompt: str, session_id: str, *, model: Optional[str], effort: Optional[str], budget_usd: float,
            permission_mode: str, allowed_tools: List[str], plugin_dir: Optional[Path],
            disallowed_tools: Optional[List[str]] = None) -> List[str]:
    cmd = [claude_bin(), "-p", prompt, "--session-id", session_id, "--output-format", "stream-json", "--verbose",
           "--setting-sources", "project", "--permission-mode", permission_mode,
           "--max-budget-usd", str(budget_usd)]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    if allowed_tools:
        cmd += ["--allowedTools", *allowed_tools]
    cmd += ["--disallowedTools", *dict.fromkeys(ALWAYS_DISALLOWED + list(disallowed_tools or []))]
    if plugin_dir:
        cmd += ["--plugin-dir", str(plugin_dir)]
    return cmd


def environment(run_dir: Path, extra: dict) -> dict:
    """The run's environment: nothing from the caller's plugin settings, its own plugin state and trace.

    GitHub access is cut: no token variables and an empty gh config, so an agent cannot open PRs or
    issues on the dataset's repository (pushes are blocked separately, in the mirror's config).
    """
    gh_config = run_dir / "gh-config"
    gh_config.mkdir(parents=True, exist_ok=True)
    env = clean_env({
        "HARNESS_DATA": str(run_dir / "harness-data"),
        "REPLICABLE_TRACE": str(run_dir / "trace.jsonl"),
        "ENABLE_CLAUDEAI_MCP_SERVERS": "false",  # claude.ai connectors would differ by account, not by arm
        "GH_CONFIG_DIR": str(gh_config),
        "GIT_TERMINAL_PROMPT": "0",
        **extra,
    })
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
        env.pop(name, None)
    return env


def run(cmd: List[str], cwd: Path, env: dict, run_dir: Path, timeout_s: float) -> dict:
    """Start claude, stamp each stream event on arrival, enforce the wall-clock limit."""
    run_dir.mkdir(parents=True, exist_ok=True)
    stream_path, stderr_path = run_dir / "stream.jsonl", run_dir / "stderr.txt"
    started_wall, started = time.time(), time.monotonic()
    timed_out = False
    with stream_path.open("w") as stream, stderr_path.open("w") as stderr:
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=stderr, text=True,
                                    stdin=subprocess.DEVNULL)
        except OSError as exc:
            return {"status": "infra_error", "error": f"could not start claude: {type(exc).__name__}",
                    "wall_ms": 0, "started": started_wall}

        def kill_after():
            nonlocal timed_out
            timed_out = True
            proc.kill()

        timer = threading.Timer(timeout_s, kill_after)
        timer.start()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    event = {"type": "unparsed", "text": line[:2000]}
                stream.write(json.dumps({"t": round(time.time(), 3), "e": event}) + "\n")
                stream.flush()
            proc.wait()
        finally:
            timer.cancel()
    wall_ms = round((time.monotonic() - started) * 1000)
    status = "timeout" if timed_out else ("ok" if proc.returncode == 0 else "agent_error")
    return {"status": status, "exit_code": proc.returncode, "wall_ms": wall_ms, "started": started_wall}


def collect_transcript(session_id: str, worktree: Path, run_dir: Path, remove_project: bool = True) -> Optional[Path]:
    """Copy the session transcript (and subagents) into the run dir; drop Claude's per-project folder for this run."""
    projects = claude_config_dir() / "projects"
    candidates = list(projects.glob(f"*/{session_id}.jsonl"))
    if not candidates:
        return None
    source = candidates[0]
    dest = run_dir / "transcript"
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest / "session.jsonl")
    sub = source.with_suffix("") / "subagents"
    if sub.is_dir():
        shutil.copytree(sub, dest / "subagents", dirs_exist_ok=True)
    # Only ever delete the folder Claude created for *this run's* unique worktree path.
    project_dir = source.parent
    if remove_project and project_dir.name == project_slug(worktree.resolve()) and project_dir.parent == projects:
        shutil.rmtree(project_dir, ignore_errors=True)
    return dest / "session.jsonl"


def new_session_id() -> str:
    return str(uuid.uuid4())


def stream_events(run_dir: Path) -> List[dict]:
    out = []
    path = run_dir / "stream.jsonl"
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        try:
            wrapped = json.loads(line)
        except ValueError:
            continue
        if isinstance(wrapped, dict) and isinstance(wrapped.get("e"), dict):
            out.append(dict(wrapped["e"], _t=wrapped.get("t")))
    return out


def summary(events: List[dict]) -> dict:
    """The parts of the init and result events that a results row keeps (no secrets, no code)."""
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
    result = next((e for e in reversed(events) if e.get("type") == "result"), {})
    keep = ("subtype", "is_error", "duration_ms", "duration_api_ms", "num_turns", "total_cost_usd", "stop_reason",
            "terminal_reason", "api_error_status", "safety_stops")
    out = {k: result.get(k) for k in keep if k in result}
    out["permission_denials"] = len(result.get("permission_denials") or [])
    if isinstance(result.get("usage"), dict):
        u = result["usage"]
        out["usage"] = {k: u.get(k) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
                                             "output_tokens") if k in u}
        if isinstance(u.get("output_tokens_details"), dict):
            out["usage"]["thinking_tokens"] = u["output_tokens_details"].get("thinking_tokens")
    model_usage = result.get("modelUsage") or {}
    out["models"] = {m: {k: v.get(k) for k in ("inputTokens", "outputTokens", "cacheReadInputTokens",
                                                  "cacheCreationInputTokens", "costUSD")}
                     for m, v in model_usage.items() if isinstance(v, dict)}
    stats = result.get("subagent_stats") or {}
    out["subagents_spawned"] = stats.get("spawned")
    out["init"] = {
        "claude_code_version": init.get("claude_code_version"),
        "model": init.get("model"),
        "permission_mode": init.get("permissionMode"),
        "plugins": sorted(f"{p.get('name')}@{p.get('version') or p.get('source')}" for p in init.get("plugins", [])
                          if isinstance(p, dict)),
        "mcp_servers": sorted(f"{s.get('name')}:{s.get('status')}" for s in init.get("mcp_servers", [])
                              if isinstance(s, dict)),
        "skills": len(init.get("skills") or []),
        "agents": sorted(a for a in init.get("agents", []) if isinstance(a, str)),
    }
    return out


def plugin_env(test_gate: bool, harness_test_command: Optional[str]) -> dict:
    env = {}
    if test_gate:
        env["LAB_TEST_GATE"] = "1"
    if harness_test_command:
        env["LAB_TEST_CMD"] = harness_test_command
    return env


def managed_settings_hash() -> Optional[str]:
    """Organisation policy applies to every arm alike; record which one was in force."""
    for path in (Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
                 Path("/etc/claude-code/managed-settings.json")):
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        except OSError:
            continue
    return None

