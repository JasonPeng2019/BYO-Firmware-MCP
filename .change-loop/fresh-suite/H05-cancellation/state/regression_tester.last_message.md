Updated the owned regression test only:

- `anyio.sleep(0)` → `anyio.sleep(0.001)`
- `BaseException` → `Exception`, with a line-local justified `BLE001` suppression while retaining the `OperationCancelledError` assertion.

Results:

- `uvx ruff check tests/test_regression_h05_wait_cancellation.py` — passed: `All checks passed!`
- `./.h01-venv-batchstrict/Scripts/python.exe -m unittest -q tests.test_regression_h05_wait_cancellation` — passed: 2 tests.

Regression command and manifest unchanged. Charter checkpoints recorded before analysis, edits, verification, and final report.
