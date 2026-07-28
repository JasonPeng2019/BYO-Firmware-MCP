#!/usr/bin/env bash
set -euo pipefail

cd "/mnt/c/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP"
export CL_RUNTIME_DIR=.change-loop/fresh-suite/A20-sleeping-symbol-read
export DOER_MODEL=gpt-5.6-terra
export CL_REASONING_EFFORT=medium
export CL_CODEX_FLAGS='--sandbox danger-full-access --ignore-user-config --config service_tier="priority"'
export CL_CODEX_BIN=/mnt/c/Users/Jason/AppData/Local/Programs/OpenAI/Codex/bin/codex.exe

bash ../.codex/skills/change-loop/scripts/agent.sh \
  doer \
  .change-loop/fresh-suite/A20-sleeping-symbol-read/DOER_POST_DIFF_REVIEW_PROMPT.policy-bound.md
