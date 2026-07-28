#!/usr/bin/env bash
set -euo pipefail

repo="/mnt/c/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP"
runtime=".change-loop/fresh-suite/S12-nrf-pack-overlap"
runtime_abs="$repo/$runtime"
stdout_log="$runtime_abs/manager-run-loop.stdout.log"
stderr_log="$runtime_abs/manager-run-loop.stderr.log"
exit_file="$runtime_abs/manager-run-loop.exitcode"
finished_file="$runtime_abs/manager-run-loop.finished-at"

rm -f "$exit_file" "$finished_file"

run_manager() {
  cd "$repo"
  export CL_RUNTIME_DIR="$runtime"
  export CL_CODEX_FLAGS='--sandbox danger-full-access --ignore-user-config --config service_tier="priority"'
  export CL_REASONING_EFFORT=medium
  export DOER_MODEL=gpt-5.6-terra
  export SPEC_TESTER_MODEL=gpt-5.6-terra
  export REGRESSION_TESTER_MODEL=gpt-5.6-terra
  export CL_CODEX_BIN=/mnt/c/Users/Jason/.codex/packages/standalone/current/bin/codex.exe

  set +e
  bash ../.codex/skills/change-loop/scripts/run_loop.sh
  rc=$?
  set -e
  printf '%s\n' "$rc" >"$exit_file"
  date --iso-8601=seconds >"$finished_file"
  return "$rc"
}

if [[ "${1:-}" == "--foreground" ]]; then
  set +e
  run_manager >"$stdout_log" 2>"$stderr_log" </dev/null
  rc=$?
  set -e
  exit "$rc"
fi

run_manager >"$stdout_log" 2>"$stderr_log" </dev/null &

printf '%s\n' "$!"
