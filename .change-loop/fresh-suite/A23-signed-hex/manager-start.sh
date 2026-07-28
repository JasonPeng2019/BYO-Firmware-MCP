#!/usr/bin/env bash
set -euo pipefail

repo="/mnt/c/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP"
runtime=".change-loop/fresh-suite/A23-signed-hex"
runtime_abs="$repo/$runtime"
stdout_log="$runtime_abs/manager-run-loop.stdout.log"
stderr_log="$runtime_abs/manager-run-loop.stderr.log"
exit_file="$runtime_abs/manager-run-loop.exitcode"
finished_file="$runtime_abs/manager-run-loop.finished-at"

rm -f "$exit_file" "$finished_file"
cd "$repo"
export CL_RUNTIME_DIR="$runtime"
export CL_CODEX_FLAGS='--sandbox danger-full-access --ignore-user-config --config service_tier="priority"'
export CL_REASONING_EFFORT=medium
export DOER_MODEL=gpt-5.6-terra
export SPEC_TESTER_MODEL=gpt-5.6-terra
export REGRESSION_TESTER_MODEL=gpt-5.6-terra
export CL_CODEX_BIN="$runtime_abs/policy-bound-codex.sh"

set +e
bash ../.codex/skills/change-loop/scripts/run_loop.sh \
  > >(tee "$stdout_log") \
  2> >(tee "$stderr_log" >&2)
rc=$?
set -e
printf '%s\n' "$rc" >"$exit_file"
date --iso-8601=seconds >"$finished_file"
exit "$rc"
