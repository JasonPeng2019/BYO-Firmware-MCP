#!/usr/bin/env bash
set -euo pipefail

export CL_RUNTIME_DIR=.change-loop/fresh-suite/H01-batch-strict
export CL_CODEX_BIN=/mnt/c/Users/Jason/AppData/Local/Programs/OpenAI/Codex/bin/codex.exe
export CL_CODEX_FLAGS="--sandbox danger-full-access --ignore-user-config"
exec bash ../.codex/skills/change-loop/scripts/agent.sh \
  spec_tester \
  .change-loop/fresh-suite/H01-batch-strict/state/spec_tester_coverage_followup.prompt.md
