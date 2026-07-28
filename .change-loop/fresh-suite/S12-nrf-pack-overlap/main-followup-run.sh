#!/usr/bin/env bash
set -euo pipefail

repo="/mnt/c/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP"
runtime=".change-loop/fresh-suite/S12-nrf-pack-overlap"
state="$repo/$runtime/state"

cd "$repo"
export CL_RUNTIME_DIR="$runtime"
export CL_CODEX_FLAGS='--sandbox danger-full-access --ignore-user-config --config service_tier="priority"'
export CL_REASONING_EFFORT=medium
export DOER_MODEL=gpt-5.6-terra
export SPEC_TESTER_MODEL=gpt-5.6-terra
export REGRESSION_TESTER_MODEL=gpt-5.6-terra
export CL_CODEX_BIN=/mnt/c/Users/Jason/.codex/packages/standalone/current/bin/codex.exe

bash ../.codex/skills/change-loop/scripts/agent.sh \
  doer "$state/main_followup_doer.prompt.md"
bash ../.codex/skills/change-loop/scripts/agent.sh \
  spec_tester "$state/main_followup_spec.prompt.md"
bash ../.codex/skills/change-loop/scripts/agent.sh \
  regression_tester "$state/main_followup_regression.prompt.md"
bash ../.codex/skills/change-loop/scripts/run_tests.sh
