"""Shared helpers for the benchmark runner (stdlib only, Python 3.9+).

The benchmark measures the plugin; it is not part of it. Claude Code never
loads this directory. State and raw artifacts live under the bench cache
(``REPLICABLE_BENCH_HOME`` > ``$XDG_CACHE_HOME/replicable-bench``), never in
the suite repository, because transcripts contain the dataset's code.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Iterable, List, Optional

BENCH_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = BENCH_DIR.parent
RUNNER_VERSION = "1"

# Environment variables that configure the plugin or this runner: never inherited by a benchmark run.
SCRUBBED_ENV = ("HARNESS_ROOT", "HARNESS_DATA", "LAB_TEST_CMD", "LAB_TEST_GATE", "LAB_TEST_GATE_MAX",
                "REPLICABLE_TRACE", "CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT", "GRAPHIFY_BIN")


def home() -> Path:
    if os.environ.get("REPLICABLE_BENCH_HOME"):
        return Path(os.environ["REPLICABLE_BENCH_HOME"]).expanduser().resolve()
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return (cache / "replicable-bench").resolve()


def claude_bin() -> str:
    return os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude"


def graphify_bin() -> Optional[str]:
    return os.environ.get("BENCH_GRAPHIFY_BIN") or shutil.which("graphify")


def claude_config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def project_slug(path: Path) -> str:
    """The directory name Claude Code uses for a project's transcripts: every non-alphanumeric becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


class Timed:
    def __init__(self, proc: subprocess.CompletedProcess, ms: int, timed_out: bool = False):
        self.proc, self.ms, self.timed_out = proc, ms, timed_out

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.proc.returncode == 0

    @property
    def output(self) -> str:
        return (self.proc.stdout or "") + (self.proc.stderr or "")


def sh(cmd, cwd: Optional[Path] = None, env: Optional[dict] = None, timeout: Optional[float] = None,
       shell: Optional[bool] = None) -> Timed:
    """Run a command, capture its output and time it. A timeout is reported, not raised."""
    shell = isinstance(cmd, str) if shell is None else shell
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=cwd, env=env, shell=shell, capture_output=True, text=True, timeout=timeout)
        return Timed(proc, elapsed_ms(started))
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return Timed(subprocess.CompletedProcess(cmd, 124, out, err), elapsed_ms(started), timed_out=True)


def git(args: List[str], cwd: Path, check: bool = True, env: Optional[dict] = None) -> str:
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)
    if check and res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}: {res.stderr.strip()}")
    return res.stdout.strip()


def elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    tmp.replace(path)


def read_jsonl(path: Path) -> List[dict]:
    rows = []
    try:
        with Path(path).open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        pass
    return rows


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def digest(data) -> str:
    raw = data if isinstance(data, (bytes, str)) else json.dumps(data, sort_keys=True)
    return hashlib.sha256(raw.encode() if isinstance(raw, str) else raw).hexdigest()


def tree_digest(root: Path, files: Iterable[Path]) -> str:
    h = hashlib.sha256()
    for path in sorted(files):
        h.update(str(path.relative_to(root)).encode() + b"\0")
        h.update(path.read_bytes() + b"\0")
    return h.hexdigest()


def clean_env(extra: Optional[dict] = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in SCRUBBED_ENV}
    env.update(extra or {})
    return env


def percentile(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def tool_version(cmd: List[str]) -> Optional[str]:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = (res.stdout or res.stderr).strip().splitlines()
    return out[0].strip() if out else None
