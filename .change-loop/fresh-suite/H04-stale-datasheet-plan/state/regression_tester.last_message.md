Fixed the neutral-harness failure: the recorded command now uses Bash-safe forward slashes.

Verified under both PowerShell and Bash:

```sh
./.h01-venv-batchstrict/Scripts/python.exe -m unittest -v tests.test_regression_h04_stale_datasheet_plan
```

Regression coverage remains focused on stale/missing PDF invalidation before setup, paired repair, permissions, or hardware workflow.

Charter checkpoints completed this iteration: before analysis, before state edit, before verification, and before final verdict.
