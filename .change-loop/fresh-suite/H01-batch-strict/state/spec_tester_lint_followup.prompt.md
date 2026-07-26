Authorized local firmware validation. This is a host-only follow-up in the named BYO-Firmware-MCP workspace; no board, remote target, or hardware action is in scope.

Resume your persistent H01 spec-tester role. Main-model verification found exactly one test-owned lint defect after the neutral behavioral gate passed: `tests/test_h01_strict_mcp_boundary.py` imports `BatchChild` but never uses it, and Ruff reports F401 at line 17. Make only the minimal test-owned correction by removing that unused import. Do not edit production, any other test, dependencies, lock state, or runtime control files except your required manifest/command. Do not change or weaken any assertion.

Read the complete `../.codex/design_charter.md` before analysis, immediately before the edit, before verification, and before your final verdict; report all checkpoints. Verify with this exact host-only command so no repository `.venv` is created or mutated:
`wsl -d H00-POSIX --cd /mnt/c/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP -- env UV_PROJECT_ENVIRONMENT=/root/mcp-trial-3-h01-gate-venv uv run --locked --no-sync ruff check tests/test_h01_strict_mcp_boundary.py`
Then, if feasible, run your existing isolated spec suite using the same WSL/UV environment prefix.

Keep `.change-loop/fresh-suite/H01-batch-strict/state/spec_test_cmd` exactly as the portable repository command `uv run --locked --no-sync pytest -q tests/test_h01_strict_mcp_boundary.py` and keep the manifest to only `tests/test_h01_strict_mcp_boundary.py`.