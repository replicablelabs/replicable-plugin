Directories starting with `_` are ignored by the runner: copy this one to
`tasks/<id>/` to start a task.

Write the prompt the way a teammate would file the work: the behaviour wanted
and why, not the solution. Never name the hidden tests.

- `hidden/` holds the acceptance tests at their repository paths (here
  `hidden/tests/test_new_behaviour.py`). They are copied in only for grading.
- `fail_to_pass` selects tests that fail at the pinned commit and must pass.
- `pass_to_pass` selects nearby tests that must keep passing.
- `solution.patch` is a reference change (`git diff` from the pinned commit,
  without the hidden tests). `validate --grade` proves the task with it.
