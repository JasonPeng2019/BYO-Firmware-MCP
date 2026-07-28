#!/usr/bin/env bash
set -euo pipefail

real_codex="/mnt/c/Users/Jason/.vscode/extensions/openai.chatgpt-26.715.61943-win32-x64/bin/windows-x86_64/codex.exe"
suite_root="/mnt/c/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3"
runtime="$suite_root/BYO-Firmware-MCP/.change-loop/fresh-suite/A23-signed-hex"

if [[ "${1:-}" == "exec" && "${2:-}" == "--help" && "$#" -eq 2 ]]; then
  exec "$real_codex" "$@"
fi

args=("$@")
if [[ "${#args[@]}" -lt 2 || "${args[0]}" != "exec" ]]; then
  exec "$real_codex" "$@"
fi

last_index=$((${#args[@]} - 1))
raw_prompt="${args[$last_index]}"
unset 'args[last_index]'

sequence_file="$runtime/prompt-policy-sequence"
sequence=0
if [[ -s "$sequence_file" ]]; then
  read -r sequence <"$sequence_file"
fi
sequence=$((sequence + 1))
printf '%s\n' "$sequence" >"$sequence_file"
stem=$(printf '%03d' "$sequence")
raw_file="$runtime/policy-bound-prompt-$stem.raw.md"
composed_file="$runtime/policy-bound-prompt-$stem.composed.md"
printf '%s' "$raw_prompt" >"$raw_file"
python3 "$suite_root/.agent-workspace/prompt_policy.py" "$raw_file" "$composed_file"
printf '%s\t%s\t%s\n' \
  "$stem" \
  "fcb25396d58af7ee6e7ffc931142b830e8a1b28ea3e5c197a1ca1e3d6248aa68" \
  "$(sha256sum "$composed_file" | awk '{print $1}')" \
  >>"$runtime/prompt-policy-bindings.tsv"

exec "$real_codex" "${args[@]}" "$(cat "$composed_file")"
