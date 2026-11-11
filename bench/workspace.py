"""Checkouts of the dataset: one cached mirror, and a fresh detached worktree per run.

Hidden tests are never in a worktree while the agent works; grading copies
them in afterwards (see grade.py). Every run gets a unique path, so Claude
Code's per-project state (transcripts, auto-memory) never carries over.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import List, Optional

from common import git, home, sh

BENCH_IDENTITY = {
    "GIT_AUTHOR_NAME": "replicable-bench", "GIT_AUTHOR_EMAIL": "bench@replicable.invalid",
    "GIT_COMMITTER_NAME": "replicable-bench", "GIT_COMMITTER_EMAIL": "bench@replicable.invalid",
}
# Never part of the agent's change: graphify's output, and bytecode written when tests run in the worktree.
IGNORED_BY_BENCH = ("graphify-out/", "__pycache__/", "*.py[cod]")
NO_PUSH = "file:///dev/null/replicable-bench-never-pushes"


def mirror(repo: str, sha: str, name: str) -> Path:
    """A bare mirror of the dataset that contains ``sha``; cloned once, fetched only when the commit is missing."""
    path = home() / "datasets" / f"{name}.git"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        res = sh(["git", "clone", "--mirror", "--quiet", repo, str(path)], timeout=3600)
        if not res.ok:
            shutil.rmtree(path, ignore_errors=True)
            raise RuntimeError(f"could not clone {repo}: {res.output.strip()[-400:]}")
    if sh(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=path).proc.returncode != 0:
        sh(["git", "fetch", "--quiet", "origin", "+refs/*:refs/*"], cwd=path, timeout=3600)
    if sh(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=path).proc.returncode != 0:
        raise RuntimeError(f"commit {sha} is not in {repo}")
    # Worktrees share this repository's config: make every push from a run fail, whatever the agent tries.
    # (A mirror's `git push` would otherwise push all refs to the real repository.)
    sh(["git", "config", "remote.origin.pushurl", NO_PUSH], cwd=path)
    sh(["git", "config", "remote.origin.mirror", "false"], cwd=path)
    exclude = path / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    current = exclude.read_text() if exclude.exists() else ""
    missing = [p for p in IGNORED_BY_BENCH if p not in current.split()]
    if missing:
        exclude.write_text(current + "".join(f"{p}\n" for p in missing))
    return path


def checkout(mirror_path: Path, sha: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    git(["worktree", "add", "--detach", "--force", str(dest), sha], mirror_path)
    return dest


def remove(mirror_path: Path, dest: Path) -> None:
    sh(["git", "worktree", "remove", "--force", str(dest)], cwd=mirror_path)
    shutil.rmtree(dest, ignore_errors=True)
    sh(["git", "worktree", "prune"], cwd=mirror_path)


def run_setup(commands: List[str], repo: Path, env: dict, timeout: Optional[int] = 1800) -> List[dict]:
    steps = []
    for cmd in commands:
        res = sh(cmd, cwd=repo, env=env, timeout=timeout)
        steps.append({"step": cmd, "ok": res.ok, "ms": res.ms,
                      **({} if res.ok else {"error": "timeout" if res.timed_out else f"exit {res.proc.returncode}",
                                             "tail": res.output.strip()[-600:]})})
        if not res.ok:
            break
    return steps


def commit_all(repo: Path, message: str, env: dict) -> str:
    """Commit whatever setup wrote, so the agent's diff is measured from here. Returns the new HEAD."""
    git(["add", "-A"], repo, env=env)
    if git(["status", "--porcelain"], repo, env=env):
        git(["-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", message], repo, env=env)
    return git(["rev-parse", "HEAD"], repo, env=env)


def diff_stats(repo: Path, base: str, env: dict, patch_out: Optional[Path] = None) -> dict:
    """What the agent changed since ``base``, committed or not (IGNORED_BY_BENCH excluded)."""
    git(["add", "-A"], repo, env=env)
    numstat = git(["diff", "--cached", "--numstat", base], repo, env=env)
    added = removed = 0
    files = []
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        a, r, name = parts
        added += int(a) if a.isdigit() else 0
        removed += int(r) if r.isdigit() else 0
        files.append(name)
    if patch_out is not None:
        patch_out.write_text(git(["diff", "--cached", "--binary", base], repo, env=env) + "\n")
    commits = git(["rev-list", "--count", f"{base}..HEAD"], repo, env=env)
    return {"files_changed": len(files), "lines_added": added, "lines_removed": removed,
            "agent_commits": int(commits or 0),
            "test_files_changed": sum(1 for f in files if _is_test(f))}


def _is_test(name: str) -> bool:
    lowered = name.lower()
    return any(part in lowered for part in ("/tests/", "tests/", "/test/", "__tests__", "_test.", ".test.", ".spec.", "test_"))
