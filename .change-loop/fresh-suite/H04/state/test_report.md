# Neutral test gate

## Spec suite: PASS

- Command: `./.h01-venv-batchstrict/Scripts/python.exe -m unittest tests/test_h04_attachment_cache_spec.py`
- Exit code: `0`

```text
........
----------------------------------------------------------------------
Ran 8 tests in 1.338s

OK

```

## Regression suite: PASS

- Command: `PYTHONPATH=src ./.h01-venv-batchstrict/Scripts/python.exe -m unittest tests/test_h04_attachment_cache_regressions.py`
- Exit code: `0`

```text
...
----------------------------------------------------------------------
Ran 3 tests in 1.145s

OK

```
