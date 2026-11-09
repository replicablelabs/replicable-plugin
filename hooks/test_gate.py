"""Stop hook: a bounded "don't stop until the tests are green" loop.

Opt-in so it never surprises you: enable with `/replicable-harness:test-gate on`
(the harness `test_gate` tool) or by exporting LAB_TEST_GATE=1.

The command comes from `.claude/replicable-harness.json` ("test"), else it is
detected from the project (Makefile, package.json, go.mod, Cargo.toml, Python).

- Only runs when there are uncommitted changes other than docs.
- Blocks the stop (decision=block) with the failing output so Claude keeps fixing.
- Gives up after LAB_TEST_GATE_MAX attempts (default 3) so it can never spin forever.
"""

import os
import subprocess

from _common import ROOT, STATE, emit, env_flag, project, read_event

event = read_event()
marker = STATE / "test-gate"
if not (marker.exists() or env_flag("LAB_TEST_GATE")):
    raise SystemExit(0)

changed = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=ROOT, capture_output=True, text=True).stdout
DOCS = (".md", ".mdx", ".rst", ".txt", ".png", ".jpg", ".svg")
code_changes = [line for line in changed.splitlines()
                if line.strip() and "graphify-out/" not in line and not line.rstrip().lower().endswith(DOCS)]
if not code_changes:
    raise SystemExit(0)  # only docs or graphify's own output changed: nothing to verify

attempts_file = STATE / "test-gate.attempts"
attempts = int(attempts_file.read_text() or 0) if attempts_file.exists() else 0
limit = int(os.environ.get("LAB_TEST_GATE_MAX", "3"))

command = project.test_command(ROOT)
if not command:
    emit({"systemMessage": "test-gate: no test command found; set \"test\" in .claude/replicable-harness.json"})
    raise SystemExit(0)
result = subprocess.run(command, shell=True, cwd=ROOT, capture_output=True, text=True, timeout=280)
if result.returncode == 0:
    if attempts_file.exists():
        attempts_file.unlink()
    emit({"systemMessage": "test-gate: tests pass ✔"})
    raise SystemExit(0)

if attempts >= limit:
    attempts_file.unlink() if attempts_file.exists() else None
    emit({"systemMessage": f"test-gate: still failing after {limit} attempts; handing back to you."})
    raise SystemExit(0)

attempts_file.parent.mkdir(parents=True, exist_ok=True)
attempts_file.write_text(str(attempts + 1))
tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-40:])
emit({
    "decision": "block",
    "reason": (f"test-gate (attempt {attempts + 1}/{limit}): `{command}` failed. "
               f"Fix the cause (`graphify affected <file>` shows what a change touches), then stop again.\n\n{tail}"),
})
