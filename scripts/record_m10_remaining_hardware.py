#!/usr/bin/env python3
"""Record Task 20 blocked/reused phases after a fresh read-only identity check."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from pyocd_debug_mcp.probe_inventory import _list_connected_probes_via_pyocd_api
from pyocd_debug_mcp.serial_resolver import list_serial_ports
from pyocd_debug_mcp.services import target_control


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(path)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result-root", type=Path, required=True)
    parser.add_argument("--required-nrf-board-id", required=True)
    parser.add_argument("--required-ficr-part", type=lambda value: int(value, 0), required=True)
    parser.add_argument("--observed-nordic-probe-id", required=True)
    parser.add_argument("--observed-nordic-target", required=True)
    parser.add_argument("--observed-nordic-uart", required=True)
    parser.add_argument("--nucleo-result", type=Path, required=True)
    args = parser.parse_args()
    started = time.perf_counter()
    result_root = args.result_root.resolve()
    exact_command = [sys.executable, *sys.argv]

    probes = [asdict(item) for item in _list_connected_probes_via_pyocd_api()]
    ports = [asdict(item) for item in (list_serial_ports() or [])]
    observed_probe = [
        item for item in probes if item["uid"] == args.observed_nordic_probe_id
    ]
    observed_uart = [
        item for item in ports if item["device"] == args.observed_nordic_uart
    ]
    if len(observed_probe) != 1 or len(observed_uart) != 1:
        raise RuntimeError("The explicitly named observed Nordic probe/UART is not unique")

    handle = target_control.open_session(
        board=None,
        unique_id=args.observed_nordic_probe_id,
        target=args.observed_nordic_target,
    )
    try:
        ficr_part = target_control.read_memory(handle, 0x10000100, 32)
        ficr_variant = target_control.read_memory(handle, 0x10000104, 32)
    finally:
        target_control.close_session(handle)
    if ficr_part == args.required_ficr_part:
        raise RuntimeError("The designated nRF52833 may now be present; do not record it blocked")

    inventory = {
        "schema_version": 1,
        "status": "inventory_complete",
        "recorded_at": _timestamp(),
        "exact_command": exact_command,
        "probes": probes,
        "serial_ports": ports,
        "observed_nordic": {
            "probe_id": args.observed_nordic_probe_id,
            "uart": args.observed_nordic_uart,
            "uart_usb_serial": observed_uart[0]["serial_number"],
            "target_used_for_read_only_identification": args.observed_nordic_target,
            "ficr_info_part": f"0x{ficr_part:08X}",
            "ficr_info_variant": f"0x{ficr_variant:08X}",
        },
    }
    _write(result_root / "inventory.json", inventory)

    nrf_result = {
        "schema_version": 1,
        "phase": "nrf52833dk_setup_safety_validation_actions",
        "status": "blocked",
        "recorded_at": _timestamp(),
        "required_identity": {
            "board_id": args.required_nrf_board_id,
            "ficr_info_part": f"0x{args.required_ficr_part:08X}",
            "probe_id": "designated nRF52833 DK probe not present",
        },
        "observed_incompatible_hardware": inventory["observed_nordic"],
        "safety_stop": {
            "substituted": False,
            "setup_calls": 0,
            "validation_calls": 0,
            "flash_calls": 0,
            "erase_calls": 0,
            "write_calls": 0,
        },
        "remedy": "Attach the designated nRF52833 DK and verify FICR.INFO.PART=0x00052833.",
        "exact_command": exact_command,
    }
    _write(result_root / "boards/nrf52833dk/acceptance.json", nrf_result)

    prior_m8 = Path("docs/evidence/m8-hardware-recovery-2026-07-17.json").resolve()
    recovery_result = {
        "schema_version": 1,
        "phase": "single_designated_recovery_proof",
        "status": "blocked",
        "recorded_at": _timestamp(),
        "required_identity": nrf_result["required_identity"],
        "blockers": [
            "The designated recoverable nRF52833 DK is absent.",
            "No verified backup exists for the absent board.",
            "No live nRF52833 safety map exists for disclosure comparison.",
            "Fresh one-time permission was not requested because identity prerequisites failed.",
        ],
        "safety_stop": {
            "target_unlock_plan_calls": 0,
            "permission_requests": 0,
            "target_unlock_calls": 0,
            "mass_erase_calls": 0,
            "flash_calls": 0,
        },
        "prior_blocked_evidence": {
            "path": str(prior_m8),
            "sha256": _sha256(prior_m8),
        },
        "exact_command": exact_command,
    }
    _write(result_root / "recovery/nrf52833dk/acceptance.json", recovery_result)

    prior_m9 = Path("docs/evidence/m9-hardware-lifecycle-2026-07-17.json").resolve()
    nucleo_result = args.nucleo_result.resolve()
    if not nucleo_result.is_file():
        raise FileNotFoundError(nucleo_result)
    current_nucleo = json.loads(nucleo_result.read_text(encoding="utf-8"))
    if current_nucleo.get("status") != "pass" or current_nucleo.get("final_gate") is not None:
        raise RuntimeError("Current Nucleo cleanup/reconnect evidence is not an accepted pass")
    lifecycle_result = {
        "schema_version": 1,
        "phase": "lifecycle_cancellation_without_repeat_flash",
        "status": "pass_with_documented_client_gaps",
        "recorded_at": _timestamp(),
        "preserved_cancellation_evidence": {
            "path": str(prior_m9),
            "sha256": _sha256(prior_m9),
            "reason_not_repeated": "Avoid a second disposable application flash.",
        },
        "current_release_and_reconnect_evidence": {
            "path": str(nucleo_result),
            "sha256": _sha256(nucleo_result),
            "post_reconnect_validation": current_nucleo["post_reconnect_validation"]["status"],
            "final_gate": current_nucleo["final_gate"],
        },
        "exact_command": exact_command,
    }
    _write(result_root / "lifecycle/acceptance.json", lifecycle_result)

    isolation_result = {
        "schema_version": 1,
        "phase": "simultaneous_two_board_isolation",
        "status": "blocked",
        "recorded_at": _timestamp(),
        "required_boards": ["nucleo_l476rg", "nrf52833dk"],
        "available_official_boards": ["nucleo_l476rg"],
        "incompatible_hardware_not_substituted": inventory["observed_nordic"],
        "hardware_assertions_executed": 0,
        "automated_fake_backend_evidence_remains_non_hardware": [
            "one-to-one assignment",
            "board-specific plan and permission isolation",
            "validated A while B denied",
            "cross-board concurrency",
            "disconnect isolation",
        ],
        "remedy": "Attach the official nRF52833 DK alongside the Nucleo and rerun this phase.",
        "exact_command": exact_command,
    }
    _write(result_root / "two-board/acceptance.json", isolation_result)

    summary = {
        "status": "recorded",
        "elapsed_seconds": time.perf_counter() - started,
        "nrf52833dk": "blocked",
        "recovery": "blocked",
        "lifecycle": lifecycle_result["status"],
        "two_board": "blocked",
        "versions": {
            "python": sys.version.split()[0],
            "pyocd": importlib.metadata.version("pyocd"),
            "mcp": importlib.metadata.version("mcp"),
            "package": importlib.metadata.version("pyocd-debug-mcp"),
        },
    }
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
