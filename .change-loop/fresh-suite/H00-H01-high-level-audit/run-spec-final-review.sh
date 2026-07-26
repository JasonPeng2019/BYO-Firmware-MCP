#!/usr/bin/env bash
set -euo pipefail

cd /mnt/c/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP

export CL_RUNTIME_DIR=.change-loop/fresh-suite/H00-H01-high-level-audit
export CL_CODEX_BIN=/mnt/c/Users/Jason/AppData/Local/Programs/OpenAI/Codex/bin/codex.exe
export SPEC_TESTER_MODEL=gpt-5.6-terra
export CL_REASONING_EFFORT=medium
export CL_CODEX_FLAGS='--sandbox danger-full-access --ignore-user-config'

exec bash ../.codex/skills/change-loop/scripts/agent.sh \
  spec_tester \
  .change-loop/fresh-suite/H00-H01-high-level-audit/state/spec_tester.final_review_prompt.md
