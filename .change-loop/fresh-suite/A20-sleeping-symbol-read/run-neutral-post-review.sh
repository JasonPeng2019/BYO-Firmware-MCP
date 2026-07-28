#!/usr/bin/env bash
set -euo pipefail

cd "/mnt/c/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP"
state=".change-loop/fresh-suite/A20-sleeping-symbol-read/state"

for role in spec_tester regression_tester; do
  manifest="$state/$role.manifest"
  snapshot="$state/$role.manifest.snapshot"
  : >"$snapshot.tmp"
  count=0
  while IFS= read -r rel || [[ -n "$rel" ]]; do
    rel="${rel%$'\r'}"
    [[ -n "$rel" && "$rel" != \#* ]] || continue
    [[ "$rel" != /* && "$rel" != *".."* && -f "$rel" ]]
    printf '%s\t%s\n' "$(git hash-object -- "$rel")" "$rel" >>"$snapshot.tmp"
    count=$((count + 1))
  done <"$manifest"
  [[ "$count" -gt 0 ]]
  mv "$snapshot.tmp" "$snapshot"
done

CL_RUNTIME_DIR=.change-loop/fresh-suite/A20-sleeping-symbol-read \
  bash ../.codex/skills/change-loop/scripts/run_tests.sh
