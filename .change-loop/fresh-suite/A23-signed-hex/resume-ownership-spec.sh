#!/usr/bin/env bash
set -euo pipefail
repo="/mnt/c/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP"
cd "$repo"
export CL_RUNTIME_DIR=".change-loop/fresh-suite/A23-signed-hex"
export CL_CODEX_FLAGS='--sandbox danger-full-access --ignore-user-config --config service_tier="priority"'
export CL_REASONING_EFFORT=medium
export DOER_MODEL=gpt-5.6-terra
export SPEC_TESTER_MODEL=gpt-5.6-terra
export REGRESSION_TESTER_MODEL=gpt-5.6-terra
export CL_CODEX_BIN="$repo/.change-loop/fresh-suite/A23-signed-hex/policy-bound-codex.sh"
bash ../.codex/skills/change-loop/scripts/agent.sh \
  spec_tester \
  .change-loop/fresh-suite/A23-signed-hex/state/main-ownership-repair-spec.prompt.md
