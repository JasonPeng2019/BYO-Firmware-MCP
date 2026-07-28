#!/usr/bin/env bash
set -euo pipefail
repo="/mnt/c/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP"
runtime=".change-loop/fresh-suite/A23-signed-hex"
cd "$repo"
read -r spec <"$runtime/state/spec_tester.manifest"
read -r regression <"$runtime/state/regression_tester.manifest"
printf '%s\t%s\n' "$(git hash-object -- "$spec")" "$spec" \
  >"$runtime/state/spec_tester.manifest.snapshot"
printf '%s\t%s\n' "$(git hash-object -- "$regression")" "$regression" \
  >"$runtime/state/regression_tester.manifest.snapshot"
CL_RUNTIME_DIR="$runtime" bash ../.codex/skills/change-loop/scripts/run_tests.sh
