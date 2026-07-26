# Neutral test gate

## Spec suite: PASS

- Command: `uv sync --locked && uv run --locked --no-sync pytest -q tests/test_h00_repository_contract.py`
- Exit code: `0`

```text
Resolved 66 packages in 5ms
Checked 61 packages in 654ms
...................                                               [100%]
19 passed, 7 subtests passed in 139.62s (0:02:19)

```

## Regression suite: PASS

- Command: `uv sync --locked && uv run --locked --no-sync pytest -q tests/test_h00_repository_regressions.py`
- Exit code: `0`

```text
Resolved 66 packages in 5ms
Checked 61 packages in 362ms
.                                                                        [100%]
1 passed in 13.80s

```
