# Neutral test gate

## Spec suite: PASS

- Command: `uv run --locked --no-sync pytest -q tests/test_h01_strict_mcp_boundary.py`
- Exit code: `0`

```text
.........                                                         [100%]
9 passed, 7 subtests passed in 2.78s

```

## Regression suite: PASS

- Command: `uv run --locked --no-sync pytest -q tests/test_h01_strict_mcp_regressions.py`
- Exit code: `0`

```text
....                                                                     [100%]
4 passed in 1.41s

```
