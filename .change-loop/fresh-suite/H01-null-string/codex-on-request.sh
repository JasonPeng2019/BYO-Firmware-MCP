#!/usr/bin/env bash
set -euo pipefail

exec /mnt/c/Users/Jason/AppData/Local/Programs/OpenAI/Codex/bin/codex.exe \
  -a on-request "$@"
