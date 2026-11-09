"""The headless "Ralph" loop: fresh `claude -p` runs until the goal is verifiably done.

Each iteration starts a *new* Claude Code process with a clean context, so
memory between iterations lives in files, not in the transcript:

- ``PROGRESS.md`` in the plugin's data directory (outside the repo, so it never
  lands in your branch): what was tried, what is left. The agent updates it
  every iteration and reads it at the start of the next.
- the git history of the loop branch: one checkpoint commit per iteration.

The loop stops when the tests pass **and** the agent printed the completion
promise ``<promise>DONE</promise>``, or when a cap is hit. Guardrails:

- runs on its own branch in an isolated git worktree (never touches your checkout);
- ``--max-iterations`` and ``--max-budget-usd`` (per iteration) are enforced;
- ``--permission-mode acceptEdits`` plus an allowlist: Claude may edit files and
  run tests/git-read commands, nothing else (no network, no push).
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "graph"))
import project  # noqa: E402

PROMISE = "<promise>DONE</promise>"
ALLOWED_TOOLS = [
    "Read", "Edit", "Write", "Glob", "Grep",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
    f"mcp__plugin_{project.PLUGIN}_code-graph",
]

PROMPT = """You are iteration {i} of {n} of an autonomous loop working toward this goal:

GOAL: {goal}

You start with no memory of earlier iterations. Read {progress} first; it is the
only record of what earlier iterations did. Use the code-graph tools (overview,
symbol, impact) to orient instead of reading whole files.

This iteration:
1. Pick the single most valuable next step toward the goal and do it.
2. Verify with: {test_cmd}
3. Update {progress}: what you did, what you learned, what is left.
4. If and only if the goal is fully met and the tests pass, end your reply with {promise}

{feedback}"""


def run(cmd, cwd: Path, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, **kw)


def make_worktree(root: Path, name: str) -> Path:
    path = project.data_dir(root) / "worktrees" / name
    branch = f"loop/{name}"
    if not path.exists():
        res = run(["git", "worktree", "add", "-b", branch, str(path), "HEAD"], cwd=root)
        if res.returncode != 0:
            raise SystemExit(f"Could not create worktree: {res.stderr.strip()}")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run Claude Code in a bounded verify-until-done loop.")
    parser.add_argument("--goal", required=True, help="what 'done' means, ideally testable")
    parser.add_argument("--max-iterations", type=int, default=5)
    parser.add_argument("--max-budget-usd", type=float, default=1.50, help="cap per iteration")
    parser.add_argument("--name", default=time.strftime("%Y%m%d-%H%M%S"), help="worktree/branch suffix")
    parser.add_argument("--model", default=None, help="e.g. sonnet, opus")
    parser.add_argument("--dry-run", action="store_true", help="print the first command and exit")
    args = parser.parse_args(argv)

    if shutil.which("claude") is None:
        raise SystemExit("The `claude` CLI is required: https://docs.claude.com/claude-code")
    root = project.root()
    test_cmd = project.test_command(root)
    if not test_cmd:
        raise SystemExit('No test command found. Set "test" in .claude/replicable-harness.json: '
                         "the loop needs an objective definition of done.")
    tree = make_worktree(root, args.name) if not args.dry_run else root
    env = dict(os.environ, LAB_TEST_CMD=test_cmd, CODE_GRAPH_ROOT=str(tree))
    if (tree / "src").is_dir():
        # Python editable installs point at the main checkout; make the worktree's code win.
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(tree / "src"), os.environ.get("PYTHONPATH")]))
    progress_dir = project.data_dir(root) / "loops" / args.name
    progress = progress_dir / "PROGRESS.md"
    if not args.dry_run:
        progress_dir.mkdir(parents=True, exist_ok=True)
        if not progress.exists():
            progress.write_text(f"# Loop progress\n\nGoal: {args.goal}\n\n")

    feedback, total_cost = "", 0.0
    for i in range(1, args.max_iterations + 1):
        prompt = PROMPT.format(i=i, n=args.max_iterations, goal=args.goal, test_cmd=test_cmd, progress=progress,
                               promise=PROMISE, feedback=feedback)
        cmd = ["claude", "-p", prompt, "--plugin-dir", str(PLUGIN_ROOT), "--add-dir", str(progress_dir), "--output-format", "json",
               "--permission-mode", "acceptEdits", "--max-budget-usd", str(args.max_budget_usd),
               "--allowedTools", *ALLOWED_TOOLS, f"Bash({test_cmd}:*)"]
        if args.model:
            cmd += ["--model", args.model]
        if args.dry_run:
            print(" ".join(shlex.quote(c) for c in cmd))
            return 0

        print(f"[loop] iteration {i}/{args.max_iterations} in {tree}", flush=True)
        started = time.time()
        res = run(cmd, cwd=tree, env=env)
        try:
            out = json.loads(res.stdout)
        except json.JSONDecodeError:
            out = {"result": res.stdout, "is_error": True}
        cost = float(out.get("total_cost_usd") or 0)
        total_cost += cost
        text = out.get("result") or ""
        tests = subprocess.run(test_cmd, shell=True, cwd=tree, env=env, capture_output=True, text=True)
        green = tests.returncode == 0
        print(f"[loop]   {time.time() - started:.0f}s, ${cost:.2f}, tests {'green' if green else 'RED'}"
              f"{', promise made' if PROMISE in text else ''}", flush=True)

        run(["git", "add", "-A"], cwd=tree)
        run(["git", "commit", "-q", "--no-verify", "-m", f"loop: iteration {i}\n\nGoal: {args.goal}"], cwd=tree)

        if green and PROMISE in text:
            print(f"[loop] DONE after {i} iteration(s), ${total_cost:.2f} total. "
                  f"Review with: git diff main...loop/{args.name}")
            return 0
        if PROMISE in text and not green:
            feedback = "The previous iteration claimed DONE but the tests FAIL. Do not trust PROGRESS.md's claim of completion.\n"
        else:
            feedback = ""
        if not green:
            tail = "\n".join((tests.stdout + tests.stderr).strip().splitlines()[-30:])
            feedback += f"Test output from the end of the previous iteration:\n```\n{tail}\n```"

    print(f"[loop] stopped at the iteration cap, ${total_cost:.2f} total. Branch loop/{args.name} has the work.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
