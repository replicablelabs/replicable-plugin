---
name: headless-loop
description: Explain and launch the headless "Ralph" loop — fresh `claude -p` runs on an isolated git worktree until the tests pass and the agent declares the goal done, capped by iterations and budget.
argument-hint: <goal>
disable-model-invocation: true
---

1. Call the harness `harness_info` tool. Note `scripts.headless_loop` and
   `test_command`. If `test_command` is null, stop: the loop needs an objective
   definition of done, so ask the user to set `"test"` in
   `.claude/replicable-harness.json`.
2. Show the user this command with the real script path and their goal, and
   explain the caps (iterations, USD per iteration, isolated worktree branch
   `loop/<name>`). Ask before running it: it spends money and makes commits on
   that new branch.

```bash
python3 <scripts.headless_loop> --goal "$ARGUMENTS" --max-iterations 5 --max-budget-usd 1.50
```
