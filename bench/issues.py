"""The GitHub issues a suite's tasks are taken from, frozen alongside the dataset's pinned commit.

Issues are edited, commented on and closed over time; a task that says "resolve
#826" must keep meaning the same thing. Only the issues the benchmark uses are
frozen: ``snapshot`` fetches each chosen issue and its comments, read-only
through the ``gh`` CLI, and adds it to the suite:

    dataset/issues.jsonl       one issue per line, sorted by number, each with its fetched_at
    dataset/issues.meta.json   repository, count, sha256 of the file, last update

An issue already in the snapshot is kept as it is unless ``refresh`` is asked for.
A task refers to an issue with ``"issue": <number>``. Without a ``prompt.md``,
its prompt is the issue as it reads in the snapshot (title, body, comments).
Issue text is the dataset owner's data: it lives in the suite, which must be as
private as the dataset.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Callable, Dict, List, Optional

from common import now_iso, write_json

GITHUB = re.compile(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")


def repo_slug(url: str) -> Optional[str]:
    m = GITHUB.search(url or "")
    return f"{m.group(1)}/{m.group(2)}" if m else None


def gh_api(path: str, paginate: bool = False):
    """GET from the GitHub API with the gh CLI (read-only)."""
    cmd = ["gh", "api", *(["--paginate", "--slurp"] if paginate else []), path]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if res.returncode != 0:
        raise RuntimeError(f"gh api {path} failed: {res.stderr.strip()[:300]}")
    data = json.loads(res.stdout or "null")
    if paginate:
        return [item for page in data or [] for item in (page if isinstance(page, list) else [page])]
    return data


DIAGNOSTICS = re.compile(r"(?:^|\n)(?:-{3,}\s*\n\s*)?#{2,4}\s*Report diagnostics\b.*\Z", re.S | re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def sanitize(text: str) -> str:
    """Drop the diagnostics block in-app reports append (reporter identity, environment) and redact emails.

    The snapshot is task input, not a copy of the tracker: personal data stays on GitHub.
    """
    text = DIAGNOSTICS.sub("\n\n(Report diagnostics removed from the benchmark snapshot.)", text or "")
    return EMAIL.sub("[email removed]", text).strip()


def _issue(raw: dict, comments: List[dict]) -> dict:
    return {
        "number": raw["number"],
        "title": raw.get("title") or "",
        "body": sanitize(raw.get("body") or ""),
        "state": raw.get("state"),
        "state_reason": raw.get("state_reason"),
        "labels": sorted(l.get("name") for l in raw.get("labels") or [] if isinstance(l, dict)),
        "author": (raw.get("user") or {}).get("login"),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "closed_at": raw.get("closed_at"),
        "comments": [{"author": (c.get("user") or {}).get("login"), "created_at": c.get("created_at"),
                      "body": sanitize(c.get("body") or "")}
                     for c in sorted(comments, key=lambda c: c.get("created_at") or "")],
    }


def snapshot(repo: str, suite_root: Path, numbers: List[int], refresh: bool = False,
             fetch: Callable = gh_api) -> dict:
    """Freeze the given issues into the suite. Returns {"added": [...], "refreshed": [...], "kept": [...], "meta": {...}}."""
    frozen = load(suite_root)
    added, refreshed, kept = [], [], []
    for n in sorted(set(int(x) for x in numbers)):
        if n in frozen and not refresh:
            kept.append(n)
            continue
        raw = fetch(f"repos/{repo}/issues/{n}")
        if not isinstance(raw, dict) or "number" not in raw:
            raise RuntimeError(f"#{n}: not found in {repo}")
        if "pull_request" in raw:
            raise RuntimeError(f"#{n} is a pull request, not an issue")
        comments = fetch(f"repos/{repo}/issues/{n}/comments?per_page=100", paginate=True) if raw.get("comments") else []
        (refreshed if n in frozen else added).append(n)
        frozen[n] = dict(_issue(raw, comments), fetched_at=now_iso())
    path = suite_root / "dataset" / "issues.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(frozen[n], sort_keys=True, ensure_ascii=False) + "\n" for n in sorted(frozen))
    path.write_text(text)
    meta = {"repo": repo, "updated_at": now_iso(), "issues": len(frozen),
            "open": sum(1 for i in frozen.values() if i["state"] == "open"),
            "sha256": hashlib.sha256(text.encode()).hexdigest()}
    write_json(path.with_name("issues.meta.json"), meta)
    return {"added": added, "refreshed": refreshed, "kept": kept, "meta": meta}


def referenced(suite_root: Path) -> List[int]:
    """Issue numbers named by the suite's tasks (read straight from task.json, before the suite loads)."""
    out = []
    for path in sorted((suite_root / "tasks").glob("*/task.json")):
        if path.parent.name.startswith(("_", ".")):
            continue
        try:
            n = json.loads(path.read_text()).get("issue")
        except ValueError:
            continue
        if isinstance(n, int):
            out.append(n)
    return out


def digest(issue: dict) -> str:
    """What identifies an issue's content: everything but when it was fetched."""
    content = {k: v for k, v in issue.items() if k != "fetched_at"}
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load(suite_root: Path) -> Dict[int, dict]:
    path = suite_root / "dataset" / "issues.jsonl"
    out = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                issue = json.loads(line)
                out[int(issue["number"])] = issue
    return out


def prompt(issue: dict, with_comments: bool = True) -> str:
    """The issue as a developer would hand it over: title, body, then the discussion."""
    parts = [f"Resolve GitHub issue #{issue['number']}: {issue['title']}".rstrip()]
    if issue.get("labels"):
        parts.append("Labels: " + ", ".join(issue["labels"]))
    parts.append(issue.get("body", "").strip() or "(no description)")
    if with_comments and issue.get("comments"):
        lines = ["Comments on the issue:"]
        for c in issue["comments"]:
            lines.append(f"- {c.get('author') or 'someone'} ({(c.get('created_at') or '')[:10]}): "
                         + (c.get("body") or "").strip())
        parts.append("\n".join(lines))
    return "\n\n".join(parts)
