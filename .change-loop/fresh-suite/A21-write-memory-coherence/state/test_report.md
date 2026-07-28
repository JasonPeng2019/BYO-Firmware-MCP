# Neutral test gate

## Spec suite: PASS

- Command: `./.venv/Scripts/python.exe -m pytest -q tests/test_a21_write_memory_coherence_spec.py`
- Exit code: `0`

```text
.........                                                    [100%]
9 passed, 12 subtests passed in 3.89s

```

## Regression suite: PASS

- Command: `./.venv/Scripts/python.exe -m pytest -q tests/test_regression_a21_write_memory_coherence.py tests/test_a20_sleeping_symbol_read_spec.py`
- Exit code: `0`

```text
..............                                                     [100%]
14 passed, 6 subtests passed in 3.63s

```
