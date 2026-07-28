#!/usr/bin/env bash
set -euo pipefail

cd /mnt/c/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP

export CL_RUNTIME_DIR=.change-loop/fresh-suite/A20-sleeping-symbol-read
export DOER_MODEL=gpt-5.6-terra
export SPEC_TESTER_MODEL=gpt-5.6-terra
export REGRESSION_TESTER_MODEL=gpt-5.6-terra
export CL_REASONING_EFFORT=medium
# Scoped fallback: Windows Codex reported workspace-write but enforced a read-only filesystem for
# both persistent roles. The change-loop skill permits danger-full-access for this exact sandbox
# launch failure; user config remains isolated and the repository/prompt scope remains unchanged.
export CL_CODEX_FLAGS='--sandbox danger-full-access --ignore-user-config --config service_tier="priority"'
export CL_CODEX_BIN=/mnt/c/Users/Jason/AppData/Local/Programs/OpenAI/Codex/bin/codex.exe

exec bash ../.codex/skills/change-loop/scripts/run_loop.sh
