"""Where am I, where do I keep state, how does this project verify itself, and is graphify set up?

Everything project-specific is resolved here so the rest of the plugin stays
generic.

Project root
    ``HARNESS_ROOT`` > ``CLAUDE_PROJECT_DIR`` (set by Claude Code) >
    the git top-level of the current directory > the current directory.

Plugin state (test-gate markers, loop worktrees, progress notes)
    ``HARNESS_DATA`` > ``$XDG_CACHE_HOME/replicable-harness/<project>-<hash>``
    (default ``~/.cache/...``), outside the repository.

Code graph
    Built and owned by graphify (https://github.com/Graphify-Labs/graphify) in
    ``<root>/graphify-out/``. This plugin only calls graphify's CLI.

Per-project configuration (optional, commit it with the project)
    ``<root>/.claude/replicable-harness.json``::

        { "test": "npm test" }

    Without it the test command is detected from the project's files.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

PLUGIN = "replicable-harness"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
CONFIG_NAME = f"{PLUGIN}.json"
GRAPHIFY_INSTALL = 'uv tool install "graphifyy[mcp]"   # or: pipx install graphifyy'


def root() -> Path:
    for var in ("HARNESS_ROOT", "CLAUDE_PROJECT_DIR"):
        if os.environ.get(var):
            return Path(os.environ[var]).resolve()
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True).stdout.strip()
        if top:
            return Path(top).resolve()
    except (OSError, subprocess.CalledProcessError):
        pass
    return Path.cwd().resolve()


def data_dir(project: Optional[Path] = None) -> Path:
    if os.environ.get("HARNESS_DATA"):
        return Path(os.environ["HARNESS_DATA"]).resolve()
    project = (project or root()).resolve()
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    digest = hashlib.sha1(str(project).encode()).hexdigest()[:10]
    return cache / PLUGIN / f"{project.name}-{digest}"


def config(project: Optional[Path] = None) -> dict:
    path = (project or root()) / ".claude" / CONFIG_NAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path} is not valid JSON: {exc}")
    return data if isinstance(data, dict) else {}


def plugin_version() -> str:
    try:
        return json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text())["version"]
    except (OSError, ValueError, KeyError):
        return "unknown"


# -- graphify ------------------------------------------------------------------
def graphify() -> Optional[str]:
    """Path to the graphify CLI, or None when it is not installed."""
    return os.environ.get("GRAPHIFY_BIN") or shutil.which("graphify")


def graph_file(project: Optional[Path] = None) -> Path:
    return (project or root()) / "graphify-out" / "graph.json"


def graphify_claude_integration(project: Optional[Path] = None) -> bool:
    """True when `graphify claude install` has been run for this project."""
    project = project or root()
    settings = project / ".claude" / "settings.json"
    claude_md = project / "CLAUDE.md"
    has_hooks = settings.exists() and "graphify hook-guard" in settings.read_text(errors="ignore")
    has_rules = claude_md.exists() and "## graphify" in claude_md.read_text(errors="ignore")
    return has_hooks and has_rules


def run_graphify(args: List[str], project: Optional[Path] = None, timeout: int = 60) -> subprocess.CompletedProcess:
    exe = graphify()
    if not exe:
        raise FileNotFoundError("graphify is not installed: " + GRAPHIFY_INSTALL)
    return subprocess.run([exe, *args], cwd=project or root(), capture_output=True, text=True, timeout=timeout)


# -- verification ----------------------------------------------------------------
def _python(project: Path) -> str:
    for candidate in (project / ".venv" / "bin" / "python", project / "venv" / "bin" / "python"):
        if candidate.exists():
            return str(candidate)
    return "python3"


def detect_test_command(project: Path) -> Optional[str]:
    """Best-effort guess from the files a project usually has. Config wins over this."""
    makefile = project / "Makefile"
    if makefile.exists() and any(line.startswith("test:") for line in makefile.read_text(errors="ignore").splitlines()):
        return "make test"
    package = project / "package.json"
    if package.exists():
        try:
            scripts = json.loads(package.read_text()).get("scripts", {})
        except json.JSONDecodeError:
            scripts = {}
        if "test" in scripts:
            runner = next((r for r, lock in (("pnpm", "pnpm-lock.yaml"), ("yarn", "yarn.lock"), ("bun", "bun.lockb"))
                           if (project / lock).exists()), "npm")
            return f"{runner} test"
    if (project / "go.mod").exists():
        return "go test ./..."
    if (project / "Cargo.toml").exists():
        return "cargo test"
    if any((project / f).exists() for f in ("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini")) \
            or (project / "tests").is_dir():
        py = _python(project)
        pyproject = project / "pyproject.toml"
        uses_pytest = (project / "pytest.ini").exists() or (
            pyproject.exists() and "pytest" in pyproject.read_text(errors="ignore"))
        if uses_pytest:
            return f"{py} -m pytest -q"
        return f"{py} -m unittest discover -s tests" if (project / "tests").is_dir() else f"{py} -m unittest"
    return None


def test_command(project: Optional[Path] = None) -> Optional[str]:
    project = project or root()
    return os.environ.get("LAB_TEST_CMD") or config(project).get("test") or detect_test_command(project)


def info(project: Optional[Path] = None) -> dict:
    project = project or root()
    return {
        "plugin": PLUGIN,
        "plugin_root": str(PLUGIN_ROOT),
        "project_root": str(project),
        "data_dir": str(data_dir(project)),
        "config_file": str(project / ".claude" / CONFIG_NAME),
        "config": config(project),
        "test_command": test_command(project),
        "test_gate": (data_dir(project) / "test-gate").exists() or os.environ.get("LAB_TEST_GATE", "") in {"1", "true", "on"},
        "graphify": {
            "cli": graphify(),
            "graph": str(graph_file(project)),
            "graph_exists": graph_file(project).exists(),
            "claude_integration": graphify_claude_integration(project),
            "install": GRAPHIFY_INSTALL,
        },
        "harness_version": {"project": config(project).get("harness_version"), "plugin": plugin_version()},
        "scripts": {
            "headless_loop": str(PLUGIN_ROOT / "loops" / "headless_loop.py"),
            "wiki": str(PLUGIN_ROOT / "harness" / "wiki.py"),
            "scaffold": str(PLUGIN_ROOT / "harness" / "scaffold.py"),
        },
    }
