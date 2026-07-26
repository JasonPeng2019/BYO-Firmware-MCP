#!/usr/bin/env bash
set -euo pipefail

cd /mnt/c/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP

export CL_RUNTIME_DIR=.change-loop/fresh-suite/H00-H01-high-level-audit
export CL_CODEX_BIN=/mnt/c/Users/Jason/AppData/Local/Programs/OpenAI/Codex/bin/codex.exe
export DOER_MODEL=gpt-5.6-terra
export SPEC_TESTER_MODEL=gpt-5.6-terra
export REGRESSION_TESTER_MODEL=gpt-5.6-terra
export CL_REASONING_EFFORT=medium
# The Windows Codex executable is launched from WSL on a Windows-mounted repo.
# The built-in Windows workspace sandbox is known to fail CreateProcessWithLogonW
# in this setup, so retain config isolation while using the documented fallback.
export CL_CODEX_FLAGS='--sandbox danger-full-access --ignore-user-config'

exec bash ../.codex/skills/change-loop/scripts/run_loop.sh </dev/null \
  >>.change-loop/fresh-suite/H00-H01-high-level-audit/run-controller.log 2>&1
