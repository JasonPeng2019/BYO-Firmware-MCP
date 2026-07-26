Fixed the neutral-gate command portability issue. The spec command now uses Bash-safe forward slashes:

```sh
./.h01-venv-batchstrict/Scripts/python.exe -m unittest -v tests.test_h04_stale_datasheet_plan_spec
```

It passes under WSL/Bash (4 tests). The manifest remains:

`tests/test_h04_stale_datasheet_plan_spec.py`

CL-001 coverage remains unchanged: binding declaration/payload, stale primary and paired refusal ordering, relocking/budget preservation/replacement, and unchanged-PDF paired control.

The neutral failure was only caused by backslashes being consumed by Bash; no implementation defect was indicated. No hardware action occurred.

Design-charter checkpoints this turn: before analysis, before state-command update, and before final verdict.
