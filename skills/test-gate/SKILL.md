---
name: test-gate
description: Turn the Stop-hook test gate on or off. When on, Claude cannot finish a turn while the project's tests fail (bounded to 3 retries).
argument-hint: on | off | status
disable-model-invocation: true
---

Call the harness `test_gate` tool with `action` set to `$ARGUMENTS`
(default `status`) and report its answer in one line. If it says no test
command was found, tell the user to add `"test"` to
`.claude/replicable.json`.
