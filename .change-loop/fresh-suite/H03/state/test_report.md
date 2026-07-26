# Neutral test gate

## Spec suite: PASS

- Command: `uv run pytest -q tests/test_h03_manifest_canonical.py`
- Exit code: `0`

```text
Using CPython 3.12.13
Removed virtual environment at: .venv
Creating virtual environment at: .venv
warning: Failed to hardlink files; falling back to full copy. This may lead to degraded performance.
         If the cache and target directories are on different filesystems, hardlinking may not be supported.
         If this is intentional, set `export UV_LINK_MODE=copy` or use `--link-mode=copy` to suppress this warning.
Installed 61 packages in 5m 55s
..                                                                       [100%]
2 passed in 0.64s

```

## Regression suite: PASS

- Command: `uv run --locked --no-sync pytest tests/test_h03_manifest_regressions.py`
- Exit code: `0`

```text
============================= test session starts ==============================
platform linux -- Python 3.12.13, pytest-9.1.1, pluggy-1.6.0
rootdir: /mnt/c/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP
configfile: pyproject.toml
plugins: anyio-4.14.2
collected 2 items

tests/test_h03_manifest_regressions.py ..                                [100%]

============================== 2 passed in 12.78s ==============================

```
