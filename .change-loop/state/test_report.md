# Neutral test gate

## Spec suite: PASS

- Command: `PYTHONPATH=src .venv/Scripts/python.exe -m unittest discover -s tests -p 'test_change_loop_stale_setup_allowance.py'`
- Exit code: `0`

```text
.......
----------------------------------------------------------------------
Ran 7 tests in 0.002s

OK

```

## Regression suite: PASS

- Command: `PYTHONPATH=src .venv/Scripts/python.exe -m unittest tests.test_regression_stale_setup_allowance`
- Exit code: `0`

```text
...
----------------------------------------------------------------------
Ran 3 tests in 0.001s

OK

```
