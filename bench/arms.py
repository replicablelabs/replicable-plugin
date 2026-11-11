"""Arms: which plugin version runs, how the task is prompted, and what setup happens first.

Plugin references:
    None / "none"   no plugin (the baseline)
    "candidate"     the version under test: this repository's working tree by default
    "previous"      the most recent v* tag before the candidate
    any git ref     a tag, branch or commit of this repository

Each version is materialised once into the bench cache (``plugins/<id>``), so a
run never sees edits made to the working tree while a benchmark is running.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from common import PLUGIN_ROOT, Timed, git, graphify_bin, home, sh, tree_digest

VERSION_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def _tags(repo: Path) -> List[Tuple[Tuple[int, int, int], str, str]]:
    out = []
    for line in git(["tag", "--list", "v*", "--format=%(refname:short) %(objectname) %(*objectname)"], repo).splitlines():
        parts = line.split()
        m = VERSION_TAG.match(parts[0]) if parts else None
        if m:
            sha = parts[2] if len(parts) > 2 and parts[2] else parts[1]  # annotated tags point at a tag object
            out.append(((int(m[1]), int(m[2]), int(m[3])), parts[0], sha))
    return sorted(out, reverse=True)


def _manifest_version(root: Path) -> str:
    try:
        return json.loads((root / ".claude-plugin" / "plugin.json").read_text())["version"]
    except (OSError, ValueError, KeyError):
        return "unknown"


def _tracked_files(repo: Path) -> List[Path]:
    names = git(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], repo).split("\0")
    return [repo / n for n in names if n and (repo / n).is_file()]


class PluginVersion(dict):
    """{ref, sha, version, dirty, content_hash, path} — what a results row records about a plugin."""

    @property
    def path(self) -> Path:
        return Path(self["path"])


def materialise(ref: str, repo: Path = PLUGIN_ROOT, cache: Optional[Path] = None) -> PluginVersion:
    cache = cache or home() / "plugins"
    head = git(["rev-parse", "HEAD"], repo)
    if ref == "candidate" or ref == "worktree":
        dirty = bool(git(["status", "--porcelain", "--untracked-files=normal"], repo))
        files = _tracked_files(repo)
        content = tree_digest(repo, files)
        dest = cache / f"tree-{content[:16]}"
        if not dest.exists():
            tmp = dest.with_name(dest.name + ".tmp")
            shutil.rmtree(tmp, ignore_errors=True)
            for path in files:
                target = tmp / path.relative_to(repo)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
            tmp.replace(dest)
        return PluginVersion(ref="candidate", sha=head, version=_manifest_version(dest), dirty=dirty,
                             content_hash=content, path=str(dest))
    if ref == "previous":
        ref = previous_tag(repo)
    sha = git(["rev-parse", f"{ref}^{{commit}}"], repo)
    dest = cache / sha
    if not dest.exists():
        tmp = dest.with_name(dest.name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        archive = subprocess.run(["git", "archive", sha], cwd=repo, capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", str(tmp)], input=archive, check=True)
        tmp.replace(dest)
    files = [p for p in dest.rglob("*") if p.is_file()]
    return PluginVersion(ref=ref, sha=sha, version=_manifest_version(dest), dirty=False,
                         content_hash=tree_digest(dest, files), path=str(dest))


def previous_tag(repo: Path = PLUGIN_ROOT) -> str:
    """The newest v* tag that is not the candidate itself (a clean HEAD that carries the tag is the candidate)."""
    head = git(["rev-parse", "HEAD"], repo)
    dirty = bool(git(["status", "--porcelain", "--untracked-files=normal"], repo))
    for _, name, sha in _tags(repo):
        if dirty or sha != head:
            return name
    raise RuntimeError("no earlier v* tag to compare against; pass --previous <ref>")


def resolve(arms: Dict[str, dict], names: List[str], candidate: str = "candidate",
            previous: str = "previous") -> Dict[str, dict]:
    """Arm configs with their plugin versions materialised (shared between arms that use the same ref)."""
    resolved, cache = {}, {}
    for name in names:
        if name not in arms:
            raise SystemExit(f"unknown arm {name!r}; defined: {', '.join(arms)}")
        arm = dict(arms[name], name=name)
        ref = arm.get("plugin")
        ref = {"candidate": candidate, "previous": previous}.get(ref, ref) if ref else None
        if ref and ref != "none":
            if ref not in cache:
                cache[ref] = materialise(ref)
            arm["plugin_version"] = cache[ref]
        else:
            arm["plugin_version"] = None
        resolved[name] = arm
    return resolved


def prompt_for(arm: dict, task_prompt: str, skill: str) -> str:
    if arm.get("prompt") == "skill":
        return f"/replicable:{skill} {task_prompt}"
    return task_prompt


def setup(arm: dict, repo: Path, env: dict, harness_test_command: Optional[str]) -> List[dict]:
    """Do what /replicable:setup would do, as fixed steps rather than through the agent. Timed per step."""
    steps = []
    graphify = graphify_bin()
    plugin = arm.get("plugin_version")
    for step in arm.get("setup", []):
        if step == "graphify_integration":
            res = sh([graphify, "claude", "install"], cwd=repo, env=env, timeout=120) if graphify else None
        elif step == "graph":
            res = sh([graphify, "update", "."], cwd=repo, env=env, timeout=900) if graphify else None
        elif step == "wiki" and plugin:
            res = sh(["python3", str(plugin.path / "harness" / "scaffold.py"), "--root", str(repo), "--apply"],
                     cwd=repo, env=env, timeout=120)
        elif step == "test_gate":
            steps.append({"step": step, "ok": True, "ms": 0})  # applied through LAB_TEST_GATE in the run's env
            continue
        else:
            res = None
        steps.append(_step(step, res, graphify))
    if harness_test_command and plugin:
        config = repo / ".claude" / "replicable.json"
        data = json.loads(config.read_text()) if config.exists() else {}
        data["test"] = harness_test_command
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps(data, indent=2) + "\n")
    return steps


def _step(name: str, res: Optional[Timed], graphify: Optional[str]) -> dict:
    if res is None:
        return {"step": name, "ok": False, "ms": 0, "error": "graphify not installed" if not graphify else "not applicable"}
    row = {"step": name, "ok": res.ok, "ms": res.ms}
    if not res.ok:
        row["error"] = "timeout" if res.timed_out else f"exit {res.proc.returncode}"
    return row
