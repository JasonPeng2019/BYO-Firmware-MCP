# Neutral test gate

## Spec suite: PASS

- Command: `env PYTHONPATH=src uvx --from pytest --with pyelftools --with pyyaml pytest -q tests/test_a23_signed_hex_spec.py`
- Exit code: `0`

```text
............                                                     [100%]
12 passed, 8 subtests passed in 1.09s

```

## Regression suite: PASS

- Command: `PYTHONPATH=src uvx --from pytest --with pyelftools --with pyyaml pytest -q tests/test_a23_signed_hex_regression.py`
- Exit code: `0`

```text
.                                                                        [100%]
1 passed in 0.93s

```
