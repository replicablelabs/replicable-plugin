"""Load and check a benchmark suite: one dataset at a pinned commit, a set of tasks, and the arms to run.

A suite is a directory (normally its own private repository) laid out as::

    suite.json            dataset, defaults, arms, tiers, delivery
    tasks/<id>/task.json  one task: prompt, hidden tests, FAIL_TO_PASS / PASS_TO_PASS selections
    tasks/<id>/prompt.md        (optional when the task names an issue: the issue is the prompt)
    dataset/issues.jsonl        the dataset's GitHub issues, frozen with the pinned commit (see issues.py)
    tasks/<id>/hidden/    test files copied into the checkout only for grading
    tasks/<id>/solution.patch   reference change, used by `validate`
    experiments/          one record per harness change
    results/runs.jsonl    one row per run, append-only

See docs/benchmarking.md for every field.
"""

from __future__ import annotations

import copy
import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import issues as issues_mod
from common import read_json, tree_digest

SHA = re.compile(r"^[0-9a-f]{40}$")

DEFAULT_ARMS: Dict[str, dict] = {
    "baseline": {"plugin": None, "prompt": "raw", "setup": []},
    "previous-raw": {"plugin": "previous", "prompt": "raw", "setup": ["graphify_integration", "graph", "test_gate"]},
    "candidate-raw": {"plugin": "candidate", "prompt": "raw", "setup": ["graphify_integration", "graph", "test_gate"]},
    "previous-skill": {"plugin": "previous", "prompt": "skill", "setup": ["graphify_integration", "graph", "test_gate"]},
    "candidate-skill": {"plugin": "candidate", "prompt": "skill", "setup": ["graphify_integration", "graph", "test_gate"]},
}
SETUP_STEPS = ("graphify_integration", "graph", "test_gate", "wiki")

DEFAULT_TIERS: Dict[str, dict] = {
    "smoke": {"tasks": 3, "trials": 1, "arms": ["baseline", "candidate-raw"]},
    "standard": {"trials": 3, "arms": list(DEFAULT_ARMS)},
    "release": {"trials": 5, "arms": list(DEFAULT_ARMS)},
}

DEFAULTS = {
    "model": None,              # required in practice: a full model id, not an alias
    "effort": None,
    "trials": 3,
    "budget_usd": 5.0,
    "timeout_s": 1800,
    "permission_mode": "acceptEdits",
    "allowed_tools": ["Read", "Edit", "Write", "Glob", "Grep", "Bash"],
    "disallowed_tools": [],     # added to the runner's own: `git push` and `gh` are always denied
    "skill": "epcc",            # the workflow skill the *-skill arms wrap the prompt in
    "test_command": None,       # how graders run a selection: "{tests}" and "{junit}" are substituted
    "harness_test_command": None,  # what the plugin's test gate runs (LAB_TEST_CMD); default: its own detection
    "grade_timeout_s": 900,
    "env": {},
}


class SuiteError(ValueError):
    pass


@dataclass
class Task:
    id: str
    dir: Path
    version: int
    prompt: str
    fail_to_pass: List[str]
    pass_to_pass: List[str]
    tier: Optional[str] = None  # a free-form descriptive tag; never used to select or score
    skill: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    test_command: Optional[str] = None
    setup: List[str] = field(default_factory=list)
    delivery: List[str] = field(default_factory=list)
    budget_usd: Optional[float] = None
    timeout_s: Optional[int] = None
    issue: Optional[int] = None
    issue_state: Optional[str] = None
    content_hash: str = ""

    @property
    def hidden_dir(self) -> Path:
        return self.dir / "hidden"

    @property
    def solution(self) -> Path:
        return self.dir / "solution.patch"

    def hidden_files(self) -> List[Path]:
        return sorted(p for p in self.hidden_dir.rglob("*") if p.is_file()) if self.hidden_dir.is_dir() else []


@dataclass
class Suite:
    root: Path
    name: str
    dataset: dict
    defaults: dict
    arms: Dict[str, dict]
    tiers: Dict[str, dict]
    delivery: dict
    tasks: Dict[str, Task]
    issues_meta: Optional[dict] = None

    @property
    def results(self) -> Path:
        return self.root / "results" / "runs.jsonl"

    @property
    def labels(self) -> Path:
        return self.root / "results" / "labels.jsonl"

    def select(self, patterns: Optional[List[str]] = None, tier: Optional[str] = None,
               difficulty: Optional[Dict[str, str]] = None) -> List[Task]:
        """The tasks to run: by id patterns, then by the run tier's ``tasks`` (patterns or a count).

        With a count, the pick takes one task from each measured difficulty first (``difficulty``
        maps task id to its bucket, from report.difficulty), so a small smoke set still spans
        difficulties; without measurements it takes the first tasks by id.
        """
        tasks = sorted(self.tasks.values(), key=lambda t: t.id)
        if patterns:
            tasks = [t for t in tasks if any(fnmatch.fnmatch(t.id, p) for p in patterns)]
        tier_cfg = self.tiers.get(tier or "", {})
        if isinstance(tier_cfg.get("tasks"), list):
            tasks = [t for t in tasks if any(fnmatch.fnmatch(t.id, p) for p in tier_cfg["tasks"])]
        elif isinstance(tier_cfg.get("tasks"), int):
            difficulty = difficulty or {}
            picked, seen = [], set()
            for t in tasks:
                bucket = difficulty.get(t.id, "unmeasured")
                if bucket not in seen:
                    picked.append(t)
                    seen.add(bucket)
            picked += [t for t in tasks if t not in picked]
            tasks = sorted(picked[:tier_cfg["tasks"]], key=lambda t: t.id)
        return tasks

    def setting(self, name: str, task: Optional[Task] = None):
        if task is not None and getattr(task, name, None) not in (None, [], ""):
            return getattr(task, name)
        return self.defaults.get(name)


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise SuiteError(message)


def load_task(path: Path, issues: Optional[Dict[int, dict]] = None) -> Task:
    data = read_json(path / "task.json")
    _require(isinstance(data, dict), f"{path / 'task.json'}: missing or not a JSON object")
    tid = data.get("id") or path.name
    _require(tid == path.name, f"{path}: task id {tid!r} must match its directory name")
    _require(re.fullmatch(r"[a-z0-9][a-z0-9._-]*", tid) is not None, f"{tid}: ids are lowercase, digits, . _ -")
    tier = data.get("tier")
    _require(tier is None or isinstance(tier, str), f"{tid}: tier is an optional free-form tag (a string)")
    version = data.get("version", 1)
    _require(isinstance(version, int) and version >= 1, f"{tid}: version must be a positive integer")
    issue_no = data.get("issue")
    issue = None
    if issue_no is not None:
        _require(isinstance(issue_no, int), f"{tid}: issue must be an issue number")
        issue = (issues or {}).get(issue_no)
        _require(issue is not None, f"{tid}: issue #{issue_no} is not frozen in dataset/issues.jsonl "
                                    "(run `bench/run.py snapshot-issues --suite <dir>` to freeze it)")
    prompt_file = path / data.get("prompt_file", "prompt.md")
    if prompt_file.is_file():
        prompt = prompt_file.read_text().strip()
    elif issue is not None:
        prompt = issues_mod.prompt(issue, with_comments=data.get("issue_comments", True))
    else:
        raise SuiteError(f"{tid}: {prompt_file.name} is missing (or name an issue)")
    _require(bool(prompt), f"{tid}: the prompt is empty")
    f2p, p2p = data.get("fail_to_pass", []), data.get("pass_to_pass", [])
    _require(isinstance(f2p, list) and f2p and all(isinstance(x, str) for x in f2p),
             f"{tid}: fail_to_pass must be a non-empty list of test selections")
    _require(isinstance(p2p, list) and all(isinstance(x, str) for x in p2p),
             f"{tid}: pass_to_pass must be a list of test selections")
    files = [p for p in path.rglob("*") if p.is_file()]
    content_hash = tree_digest(path, files)
    if issue is not None:  # the issue text is part of the task: a refreshed issue that changed is a different task
        content_hash += ":" + issues_mod.digest(issue)[:16]
    return Task(
        id=tid, dir=path, version=version, tier=tier, prompt=prompt, fail_to_pass=f2p, pass_to_pass=p2p,
        skill=data.get("skill"), tags=list(data.get("tags", [])), test_command=data.get("test_command"),
        setup=list(data.get("setup", [])), delivery=list(data.get("delivery", [])),
        budget_usd=data.get("budget_usd"), timeout_s=data.get("timeout_s"),
        issue=issue_no, issue_state=(issue or {}).get("state"), content_hash=content_hash,
    )


def load(root: Path) -> Suite:
    root = Path(root).resolve()
    data = read_json(root / "suite.json")
    _require(isinstance(data, dict), f"{root / 'suite.json'}: missing or not a JSON object")
    dataset = data.get("dataset") or {}
    _require(bool(dataset.get("repo")), "suite.json: dataset.repo is required (a git URL or path)")
    _require(bool(SHA.match(str(dataset.get("sha", "")))), "suite.json: dataset.sha must be a full 40-character commit")
    defaults = copy.deepcopy(DEFAULTS)
    defaults.update(data.get("defaults") or {})
    arms = copy.deepcopy(DEFAULT_ARMS)
    arms.update(data.get("arms") or {})
    for name, arm in arms.items():
        _require(arm.get("prompt", "raw") in ("raw", "skill"), f"arm {name}: prompt must be raw or skill")
        bad = [s for s in arm.get("setup", []) if s not in SETUP_STEPS]
        _require(not bad, f"arm {name}: unknown setup steps {bad}; known: {', '.join(SETUP_STEPS)}")
        _require(not (arm.get("prompt") == "skill" and not arm.get("plugin")),
                 f"arm {name}: a skill prompt needs a plugin")
    tiers = copy.deepcopy(DEFAULT_TIERS)
    tiers.update(data.get("tiers") or {})
    tasks: Dict[str, Task] = {}
    issues = issues_mod.load(root)
    tasks_dir = root / "tasks"
    if tasks_dir.is_dir():
        for path in sorted(p for p in tasks_dir.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))):
            task = load_task(path, issues)
            tasks[task.id] = task
    for task in tasks.values():
        _require(bool(task.test_command or defaults.get("test_command")),
                 f"{task.id}: no test_command (set it in the task or in suite.json defaults)")
        missing = [d for d in task.delivery if d not in (data.get("delivery") or {}).get("suites", {})]
        _require(not missing, f"{task.id}: delivery suites {missing} are not defined in suite.json")
    return Suite(root=root, name=data.get("name") or root.name, dataset=dataset, defaults=defaults, arms=arms,
                 tiers=tiers, delivery=data.get("delivery") or {}, tasks=tasks,
                 issues_meta=read_json(root / "dataset" / "issues.meta.json"))
