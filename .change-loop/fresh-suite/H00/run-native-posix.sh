#!/usr/bin/env bash
set -euo pipefail

repo='/mnt/c/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP'
runtime="$repo/.change-loop/fresh-suite/H00"
manifest="$runtime/FINAL_CANDIDATE_MANIFEST.json"
manifest_sha="$runtime/FINAL_CANDIDATE_MANIFEST.sha256"
candidate_root='/tmp/mcp-trial-3-h00-native-candidates'
log_root="$runtime/native-gates"
baseline='6f3da0a9a0bb97fb535c8c0ba11a4d2b31f5e876'

mkdir -p "$candidate_root" "$log_root"

cleanup() {
  case "$candidate_root" in
    /tmp/mcp-trial-3-h00-native-candidates) rm -rf -- "$candidate_root" ;;
    *) printf 'Refusing unsafe cleanup path: %s\n' "$candidate_root" >&2 ;;
  esac
}
trap cleanup EXIT

materialize() {
  local candidate="$1"
  case "$candidate" in
    "$candidate_root"/*) ;;
    *) printf 'Refusing candidate outside root: %s\n' "$candidate" >&2; return 2 ;;
  esac
  rm -rf -- "$candidate"
  git clone --no-local --quiet "$repo" "$candidate"
  [[ "$(git -C "$candidate" rev-parse HEAD)" == "$baseline" ]]

  while IFS=$'\t' read -r relative expected; do
    mkdir -p "$(dirname "$candidate/$relative")"
    cp -- "$repo/$relative" "$candidate/$relative"
    actual="$(sha256sum "$candidate/$relative" | awk '{print $1}')"
    [[ "$actual" == "$expected" ]] || {
      printf 'Candidate hash mismatch for %s\n' "$relative" >&2
      return 2
    }
  done < <(jq -r '.files | to_entries[] | [.key, .value] | @tsv' "$manifest")

  cp -- "$manifest" "$candidate/H00_FINAL_CANDIDATE_MANIFEST.json"
  cp -- "$manifest_sha" "$candidate/H00_FINAL_CANDIDATE_MANIFEST.sha256"
  (
    cd "$candidate"
    sha256sum -c <(
      printf '%s  H00_FINAL_CANDIDATE_MANIFEST.json\n' \
        "$(tr -d '\r\n' < H00_FINAL_CANDIDATE_MANIFEST.sha256)"
    )
  )
}

run_gate() {
  local candidate="$1"
  local log="$2"
  shift 2
  {
    printf '\n=== uv'
    printf ' %q' "$@"
    printf ' ===\n'
  } >>"$log"
  (
    cd "$candidate"
    UV_LINK_MODE=copy uv "$@"
  ) 2>&1 | tee -a "$log"
}

run_candidate() {
  local name="$1"
  local directory="$2"
  local candidate="$candidate_root/$directory"
  local log="$log_root/$name.log"
  printf 'platform=Debian-WSL2-ext4\ncandidate=%s\nmanifest_sha256=%s\n' \
    "$candidate" "$(tr -d '\r\n' < "$manifest_sha")" >"$log"
  materialize "$candidate"
  run_gate "$candidate" "$log" sync --locked
  run_gate "$candidate" "$log" lock --check
  run_gate "$candidate" "$log" build
  run_gate "$candidate" "$log" run --locked --no-sync ruff check .
  run_gate "$candidate" "$log" run --locked --no-sync pyright
  run_gate "$candidate" "$log" run --locked --no-sync pytest --collect-only -q
  run_gate "$candidate" "$log" run --locked --no-sync pytest -q
  printf '\nRESULT=PASS\n' >>"$log"
}

run_candidate posix-ordinary ordinary
run_candidate posix-space-unicode 'candidate spaces µ'

printf 'Debian WSL2 ext4 native candidate gates: PASS\n'
