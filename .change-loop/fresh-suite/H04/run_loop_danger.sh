#!/usr/bin/env bash
set -euo pipefail

export CL_RUNTIME_DIR=.change-loop/fresh-suite/H04
export CL_CODEX_BIN=/mnt/c/Users/Jason/AppData/Local/Programs/OpenAI/Codex/bin/codex.exe
export CL_CODEX_FLAGS="--sandbox danger-full-access --ignore-user-config"

exec ../.codex/skills/change-loop/scripts/run_loop.sh
