#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec uv run pyocd-zephyr-build \
  --app-dir "${SCRIPT_DIR}/src" \
  --build-dir "${SCRIPT_DIR}/build" \
  --board "nrf52840dk/nrf52840" \
  "$@"
