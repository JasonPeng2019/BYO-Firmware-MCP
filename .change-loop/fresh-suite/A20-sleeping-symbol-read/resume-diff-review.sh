#!/usr/bin/env bash
set -euo pipefail

server_root="/mnt/c/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP"
runtime="$server_root/.change-loop/fresh-suite/A20-sleeping-symbol-read"
codex_bin="/mnt/c/Users/Jason/AppData/Local/Programs/OpenAI/Codex/bin/codex.exe"

cd "$server_root"
prompt="$(cat "$runtime/DIFF_REVIEW_FOLLOWUP_PROMPT.policy-bound.md")"
"$codex_bin" exec \
  --model gpt-5.6-terra \
  --config 'model_reasoning_effort="medium"' \
  --config 'service_tier="priority"' \
  --sandbox read-only \
  --ignore-user-config \
  --json \
  --output-last-message ".change-loop/fresh-suite/A20-sleeping-symbol-read/diff-review-followup.last-message.md" \
  resume 019fa770-05b1-7e80-bcb3-56bf1d0ca3b1 "$prompt" \
  >"$runtime/diff-review-followup.codex.jsonl" \
  2>"$runtime/diff-review-followup.codex.stderr.log"
