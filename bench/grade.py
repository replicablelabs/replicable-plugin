"""Grade a run with the task's hidden tests, SWE-bench style.

FAIL_TO_PASS  selections that fail at the pinned commit and must pass after the change.
PASS_TO_PASS  selections that pass at the pinned commit and must still pass.
resolved      every selection in both groups passes.

The suite's ``test_command`` runs one group at a time, with ``{tests}`` replaced
by the group's selections (shell-quoted) and ``{junit}`` by a JUnit XML path, e.g.
``.venv/bin/python -m pytest -q -p no:cacheprovider {tests} --junitxml={junit}``.
Per-test counts come from the JUnit file when the runner writes one; otherwise
only the exit code is known.
"""

from __future__ import annotations

import shlex
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Optional

from common import sh


def install_hidden(task, repo: Path) -> List[str]:
    """Copy the hidden tests over the checkout (they win over anything the agent wrote at the same path)."""
    copied = []
    for path in task.hidden_files():
        rel = path.relative_to(task.hidden_dir)
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied.append(rel.as_posix())
    return copied


def junit_counts(path: Path) -> Optional[dict]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    total = failures = errors = skipped = 0
    for suite in suites:
        total += int(suite.get("tests", 0) or 0)
        failures += int(suite.get("failures", 0) or 0)
        errors += int(suite.get("errors", 0) or 0)
        skipped += int(suite.get("skipped", 0) or 0)
    return {"total": total, "failed": failures + errors, "skipped": skipped,
            "passed": max(total - failures - errors - skipped, 0)}


def run_group(name: str, selections: List[str], command: str, repo: Path, out_dir: Path, env: dict,
              timeout_s: int) -> dict:
    if not selections:
        return {"selections": 0, "ok": True, "exit_code": 0, "ms": 0, "skipped_group": True}
    junit = out_dir / f"junit-{name}.xml"
    if junit.exists():
        junit.unlink()
    cmd = command.replace("{tests}", " ".join(shlex.quote(s) for s in selections)).replace("{junit}", shlex.quote(str(junit)))
    res = sh(cmd, cwd=repo, env=env, timeout=timeout_s)
    (out_dir / f"grade-{name}.log").write_text(res.output[-200_000:])
    counts = junit_counts(junit)
    ok = res.ok
    if counts is not None and counts["passed"] == 0:
        # Selected nothing, or every selected test was skipped (e.g. it needs a database the run lacks):
        # a selection that proves nothing must never count as passing.
        ok = False
    row = {"selections": len(selections), "ok": ok, "exit_code": res.proc.returncode, "ms": res.ms,
           "timed_out": res.timed_out}
    if counts is not None:
        row.update(counts)
    return row


def grade(task, repo: Path, command: str, out_dir: Path, env: dict, timeout_s: int = 900) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    hidden = install_hidden(task, repo)
    f2p = run_group("f2p", task.fail_to_pass, command, repo, out_dir, env, timeout_s)
    p2p = run_group("p2p", task.pass_to_pass, command, repo, out_dir, env, timeout_s)
    f2p_rate = None
    if "total" in f2p and f2p["total"]:
        f2p_rate = round(f2p["passed"] / f2p["total"], 4)
    elif not f2p.get("skipped_group"):
        f2p_rate = 1.0 if f2p["ok"] else 0.0
    return {
        "resolved": bool(f2p["ok"] and p2p["ok"]),
        "f2p": f2p,
        "p2p": p2p,
        "f2p_pass_rate": f2p_rate,
        "p2p_broken": p2p.get("failed", 0 if p2p["ok"] else None),
        "hidden_files": len(hidden),
        "grader_error": "grader timed out" if (f2p.get("timed_out") or p2p.get("timed_out")) else None,
    }


def apply_patch(patch: Path, repo: Path, env: dict) -> bool:
    return sh(["git", "apply", "--whitespace=nowarn", str(patch)], cwd=repo, env=env).ok
