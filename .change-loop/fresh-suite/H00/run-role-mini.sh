#!/usr/bin/env bash
set -euo pipefail

export CL_CODEX_BIN=/mnt/c/Users/Jason/AppData/Local/Programs/OpenAI/Codex/bin/codex.exe
export CL_RUNTIME_DIR=.change-loop/fresh-suite/H00
export DOER_MODEL=gpt-5.4-mini
export SPEC_TESTER_MODEL=gpt-5.4-mini
export REGRESSION_TESTER_MODEL=gpt-5.4-mini
export CL_CODEX_FLAGS='--sandbox danger-full-access --ignore-user-config'

exec bash ../.codex/skills/change-loop/scripts/agent.sh "$@"
