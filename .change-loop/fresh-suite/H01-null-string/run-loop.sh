#!/usr/bin/env bash
set -euo pipefail

cd /mnt/c/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP

export CL_RUNTIME_DIR=.change-loop/fresh-suite/H01-null-string
export CL_CODEX_BIN=/mnt/c/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP/.change-loop/fresh-suite/H01-null-string/codex-on-request.sh
export DOER_MODEL=gpt-5.6-terra
export SPEC_TESTER_MODEL=gpt-5.6-terra
export REGRESSION_TESTER_MODEL=gpt-5.6-terra
export CL_REASONING_EFFORT=medium
export CL_CODEX_FLAGS='--sandbox danger-full-access --ignore-user-config'

exec bash ../.codex/skills/change-loop/scripts/run_loop.sh </dev/null \
  >>.change-loop/fresh-suite/H01-null-string/run-controller.log 2>&1
