# Neutral test gate

## Spec suite: PASS

- Command: `PYTHONPATH=src ./.venv/Scripts/python.exe -m unittest tests/test_h05_marker_unlink_spec.py`
- Exit code: `0`

```text
.....
----------------------------------------------------------------------
Ran 5 tests in 1.658s

OK

```

## Regression suite: PASS

- Command: `./.h01-venv-batchstrict/Scripts/python.exe -m unittest -q tests.test_regression_h05_marker_unlink`
- Exit code: `0`

```text
----------------------------------------------------------------------
Ran 3 tests in 0.004s

OK

```
