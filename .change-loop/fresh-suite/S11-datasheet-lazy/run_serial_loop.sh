#!/usr/bin/env bash
set -uo pipefail

cd /mnt/c/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP

export CL_RUNTIME_DIR=.change-loop/fresh-suite/S11-datasheet-lazy
export DOER_MODEL=gpt-5.6-terra
export SPEC_TESTER_MODEL=gpt-5.6-terra
export REGRESSION_TESTER_MODEL=gpt-5.6-terra
export CL_REASONING_EFFORT=medium
export MAX_ITERS=8
export CL_CODEX_BIN=/mnt/c/Users/Jason/AppData/Local/Programs/OpenAI/Codex/bin/codex.exe
export CL_CODEX_FLAGS='--sandbox danger-full-access --ignore-user-config --config service_tier="priority"'

printf '{"state":"RUNNING","wsl_pid":%s,"started_at":"%s"}\n' \
  "$$" "$(date --iso-8601=seconds)" \
  > "$CL_RUNTIME_DIR/controller.status.json"

bash ../.codex/skills/change-loop/scripts/run_loop.sh \
  < /dev/null \
  > "$CL_RUNTIME_DIR/controller.stdout.log" \
  2> "$CL_RUNTIME_DIR/controller.stderr.log"
code=$?

printf '{"state":"EXITED","wsl_pid":%s,"exit_code":%s,"ended_at":"%s"}\n' \
  "$$" "$code" "$(date --iso-8601=seconds)" \
  > "$CL_RUNTIME_DIR/controller.status.json"
exit "$code"
