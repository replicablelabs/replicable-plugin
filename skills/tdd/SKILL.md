---
name: tdd
description: Test-driven loop — write failing tests first, confirm they fail, commit them, then implement until green without changing the tests. Use when behaviour can be specified as input/output.
argument-hint: <behaviour to implement>
---

# Test-driven development loop

Behaviour: $ARGUMENTS

1. Use `search`/`impact` on the code-graph to find where the behaviour lives
   and which test module already covers it.
2. Write tests for the expected behaviour (including edge cases). Do **not**
   write implementation code yet, not even stubs that make them pass.
3. Run them and confirm they fail for the right reason. Show the failure.
4. Commit the tests on their own (`test: ...`).
5. Implement the smallest change that makes them pass. Do not modify the
   tests; if a test is wrong, stop and explain why.
6. Iterate: run → read failure → fix, until green. Then run the full suite.
7. Optionally turn on the Stop-hook gate (`/replicable-harness:test-gate on`)
   so you cannot end the turn while tests are red.
