#!/usr/bin/env bash
set -euo pipefail

export CL_JQ_BIN='C:/Users/Jason/AppData/Local/Temp/change-loop-jq-compat/jq'
export CL_CODEX_FLAGS='--sandbox danger-full-access --ignore-user-config'
exec bash '../.codex/skills/change-loop/scripts/run_loop.sh'
