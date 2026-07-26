# Neutral test gate

## Spec suite: PASS

- Command: `uv run --isolated --with pytest python -m pytest -q tests/test_h04_pack_index_repair_spec.py`
- Exit code: `0`

```text
Installed 62 packages in 607ms
......                                                                   [100%]
6 passed in 0.87s

```

## Regression suite: PASS

- Command: `uv run --locked pytest tests/test_regression_h04_pack_index_repair.py -q`
- Exit code: `0`

```text
..                                                                       [100%]
2 passed in 1.76s

```
