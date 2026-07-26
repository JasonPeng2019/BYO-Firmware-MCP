#!/usr/bin/env bash
set -euo pipefail

cd /mnt/c/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP
runtime=.change-loop/fresh-suite/H01-batch-strict

if pgrep -af 'H01-batch-strict/run-loop.sh|CL_RUNTIME_DIR=.change-loop/fresh-suite/H01-batch-strict' |
  grep -v 'pgrep -af' >/dev/null; then
  echo "Refusing concurrent H01 batch-strict change-loop controller." >&2
  exit 3
fi

nohup "$runtime/run-loop.sh" </dev/null >>"$runtime/run-controller.log" 2>&1 &
controller_pid=$!
printf '%s\n' "$controller_pid" >"$runtime/controller.pid"
printf 'CONTROLLER_PID=%s\n' "$controller_pid"

