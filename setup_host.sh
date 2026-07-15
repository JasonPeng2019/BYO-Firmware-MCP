#!/usr/bin/env bash
set -euo pipefail

BOARD_CONFIG_DIR="${BOARD_CONFIG_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/boards}"
BOARD_CONFIGS=()
BOARD_IDS=()
SKIP_UV_SYNC=0
SKIP_HOST_BOOTSTRAP=0
ENSURE_ZEPHYR_BUILD_ENV=0
DRY_RUN=0

section() {
  printf '\n============================================================\n'
  printf '  %s\n' "$1"
  printf '============================================================\n'
}

status() {
  printf '  [%s] %s\n' "$1" "$2"
}

run_step() {
  local description="$1"
  shift
  if [[ "$DRY_RUN" -eq 1 ]]; then
    status "INFO" "DRY RUN: ${description}"
    return 0
  fi
  "$@"
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

usage() {
  cat <<'EOF'
Usage: ./setup_host.sh [options]

Options:
  --board-config-dir PATH   Board config directory (default: ./boards)
  --board-config PATH       Extra board config file, repeatable
  --board-id ID             Board id to target, repeatable
  --skip-uv-sync            Skip uv sync
  --skip-host-bootstrap     Skip host_bootstrap.py
  --ensure-zephyr-build-env Provision the local Zephyr workspace/SDK needed for repo firmware rebuilds
  --dry-run                 Print intended actions without executing them
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --board-config-dir)
      BOARD_CONFIG_DIR="$2"
      shift 2
      ;;
    --board-config)
      BOARD_CONFIGS+=("$2")
      shift 2
      ;;
    --board-id)
      BOARD_IDS+=("$2")
      shift 2
      ;;
    --skip-uv-sync)
      SKIP_UV_SYNC=1
      shift
      ;;
    --skip-host-bootstrap)
      SKIP_HOST_BOOTSTRAP=1
      shift
      ;;
    --ensure-zephyr-build-env)
      ENSURE_ZEPHYR_BUILD_ENV=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "setup_host.sh currently supports macOS only." >&2
  exit 1
fi

get_selected_boards_via_loader() {
  section "Board config" >&2

  local cmd=(uv run)
  if [[ "$SKIP_UV_SYNC" -eq 1 || "$DRY_RUN" -eq 1 ]]; then
    cmd+=(--no-sync)
  fi
  cmd+=(
    python
    -m
    pyocd_debug_mcp.board_config_cli
    --board-config-dir
    "$BOARD_CONFIG_DIR"
  )
  local path
  for path in "${BOARD_CONFIGS[@]:-}"; do
    [[ -n "$path" ]] || continue
    cmd+=(--board-config "$path")
  done
  local board_id
  for board_id in "${BOARD_IDS[@]:-}"; do
    [[ -n "$board_id" ]] || continue
    cmd+=(--board-id "$board_id")
  done

  local boards_json
  if ! boards_json="$("${cmd[@]}")"; then
    local hint=""
    if [[ "$SKIP_UV_SYNC" -eq 1 || "$DRY_RUN" -eq 1 ]]; then
      hint=" The repo environment must already exist when using --skip-uv-sync or --dry-run because board metadata is resolved through the shared Python loader."
    fi
    echo "Shared board loader failed.${hint}" >&2
    exit 1
  fi

  if [[ -z "$boards_json" || "$boards_json" == "[]" ]]; then
    echo "No board configs were selected." >&2
    exit 1
  fi

  status "PASS" "Selected boards via shared loader: $(python3 - <<'PY' "$boards_json"
import json
import sys

boards = json.loads(sys.argv[1])
print(", ".join(board["board_id"] for board in boards))
PY
)" >&2
  printf '%s\n' "$boards_json"
}

ensure_homebrew() {
  section "Homebrew"
  if command_exists brew; then
    status "PASS" "Homebrew already found"
    return
  fi
  status "WARN" "Homebrew not found - attempting install"
  run_step "Install Homebrew" /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
}

brew_install() {
  local description="$1"
  shift
  run_step "$description" brew "$@"
}

ensure_uv() {
  section "uv"
  if command_exists uv; then
    status "PASS" "uv already found on PATH"
    return
  fi
  status "WARN" "uv not found - attempting install with Homebrew"
  brew_install "brew install uv" install uv
  if ! command_exists uv; then
    echo "uv install completed but uv was not found on PATH." >&2
    exit 1
  fi
  status "PASS" "uv installed"
}

ensure_uv_sync() {
  section "Repo environment"
  if [[ "$SKIP_UV_SYNC" -eq 1 ]]; then
    status "INFO" "Skipping uv sync by request"
    return
  fi
  run_step "uv sync --locked" uv sync --locked
  status "PASS" "Repo environment synced with 'uv sync --locked'"
}

ensure_libusb() {
  section "libusb"
  brew_install "brew install libusb" install libusb
  status "PASS" "libusb ensured via Homebrew"
}

ensure_nordic_tools() {
  section "Nordic tools (optional helper)"
  if command_exists nrfjprog; then
    status "PASS" "nrfjprog available for Nordic serial auto-detect and fallback flashing"
    return
  fi
  status "WARN" "nrfjprog not found - attempting optional Homebrew cask install"
  brew_install "brew install --cask nordic-nrf-command-line-tools" install --cask nordic-nrf-command-line-tools
  if ! command_exists nrfjprog; then
    status "WARN" "nordic-nrf-command-line-tools install did not produce a usable nrfjprog on PATH. Continuing because this helper is optional after normal probe setup."
    return
  fi
  status "PASS" "nrfjprog available for Nordic serial auto-detect and fallback flashing"
}

ensure_stm32_cubeprogrammer_path() {
  section "STM32CubeProgrammer"
  if command_exists STM32_Programmer_CLI; then
    status "PASS" "STM32_Programmer_CLI already found on PATH"
    return
  fi

  local candidates=(
    "/Applications/STMicroelectronics/STM32Cube/STM32CubeProgrammer/STM32CubeProgrammer.app/Contents/MacOs/bin"
    "/Applications/STMicroelectronics/STM32Cube/STM32CubeProgrammer.app/Contents/MacOs/bin"
    "/Applications/STM32CubeProgrammer.app/Contents/MacOs/bin"
  )

  for candidate in "${candidates[@]}"; do
    if [[ -d "$candidate" ]]; then
      export PATH="$candidate:$PATH"
      if command_exists STM32_Programmer_CLI; then
        status "PASS" "STM32_Programmer_CLI found after PATH repair ($candidate)"
        return
      fi
    fi
  done

  status "WARN" "STM32_Programmer_CLI not found. Install STM32CubeProgrammer manually before using ST-LINK boards, then rerun this script."
}

run_host_bootstrap() {
  if [[ "$SKIP_HOST_BOOTSTRAP" -eq 1 ]]; then
    status "INFO" "Skipping host bootstrap by request"
    return
  fi
  section "Host bootstrap"
  local cmd=(uv run python host_bootstrap.py --install-packs)
  for board_id in "${BOARD_IDS[@]}"; do
    cmd+=(--board-id "$board_id")
  done
  run_step "${cmd[*]}" "${cmd[@]}"
}

run_zephyr_build_bootstrap() {
  if [[ "$ENSURE_ZEPHYR_BUILD_ENV" -ne 1 ]]; then
    status "INFO" "Skipping Zephyr build bootstrap (use --ensure-zephyr-build-env when this host must rebuild firmware locally)"
    return
  fi
  section "Zephyr build bootstrap"
  local cmd=(uv run pyocd-zephyr-build --ensure-only)
  run_step "${cmd[*]}" "${cmd[@]}"
}

section "macOS host setup"

ensure_homebrew
ensure_uv
ensure_uv_sync
boards_json="$(get_selected_boards_via_loader)"
ensure_libusb

needs_nordic="$(python3 - <<'PY' "$boards_json"
import json, sys
boards = json.loads(sys.argv[1])
print("1" if any(board["mcu_family"].startswith("nrf") and board["probe_family"] == "jlink" for board in boards) else "0")
PY
)"
needs_stlink="$(python3 - <<'PY' "$boards_json"
import json, sys
boards = json.loads(sys.argv[1])
print("1" if any(board["probe_family"] == "stlink" for board in boards) else "0")
PY
)"

if [[ "$needs_nordic" == "1" ]]; then
  ensure_nordic_tools
fi

if [[ "$needs_stlink" == "1" ]]; then
  ensure_stm32_cubeprogrammer_path
fi

run_zephyr_build_bootstrap
run_host_bootstrap

section "Done"
status "PASS" "macOS host setup script completed."
