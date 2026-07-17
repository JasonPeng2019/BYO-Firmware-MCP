#!/usr/bin/env python3
"""Run the bounded M6 Nucleo setup/validation hardware acceptance.

The artifact-root environment variable is set before importing the server so the
run cannot read or write the checkout's ordinary ``.firm`` state.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _text_result(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and len(value) == 1:
        text = getattr(value[0], "text", None)
        if isinstance(text, str):
            return text
    raise TypeError(f"Expected one MCP text result, got {type(value).__name__}")


def _json_result(value: object) -> dict[str, Any]:
    parsed = json.loads(_text_result(value))
    if not isinstance(parsed, dict):
        raise TypeError("Expected an MCP JSON object result")
    return parsed


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--probe-id", required=True)
    parser.add_argument("--serial-id", required=True)
    parser.add_argument("--board-id", default="nucleo_l476rg")
    parser.add_argument("--display-name", default="Nucleo-L476RG M6 Acceptance")
    parser.add_argument("--mcu-part-number", default="STM32L476RGT6")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--target", default="stm32l476rgtx")
    parser.add_argument("--test-read-address", type=lambda text: int(text, 0), default=0x08000000)
    parser.add_argument("--silicon-id-address", type=lambda text: int(text, 0), default=0xE0042000)
    parser.add_argument("--silicon-id-expected", type=lambda text: int(text, 0), default=0x415)
    parser.add_argument("--silicon-id-mask", type=lambda text: int(text, 0), default=0xFFF)
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    artifact_root = args.artifact_root.expanduser().resolve()
    checkout_root = Path.cwd().resolve()
    if artifact_root == checkout_root or checkout_root in artifact_root.parents:
        raise ValueError("--artifact-root must be outside the checkout")
    if (artifact_root / ".firm").exists():
        raise FileExistsError("A clean artifact root is required; .firm already exists")
    artifact_root.mkdir(parents=True, exist_ok=True)
    os.environ["BYO_MCP_ARTIFACT_ROOT"] = str(artifact_root)

    # Import only after isolation is established.
    from pyocd_debug_mcp import server
    from pyocd_debug_mcp.firmstore.cache import ProbeIdentity, SerialEndpoint
    from pyocd_debug_mcp.firmstore.store import ImmutableArtifactError
    from pyocd_debug_mcp.pack_provision import PACKS_DIR, load_manifest, sha256_file
    from pyocd_debug_mcp.services import target_control
    from pyocd_debug_mcp.services.uart_capture import capture_uart_output
    from pyocd_debug_mcp.setup_flow.preflight import SetupUserInput
    from pyocd_debug_mcp.setup_flow.targets import (
        EnrichmentResult,
        EnrichmentValidator,
        ProfileCommitCoordinator,
        SiliconIdentityCandidate,
    )

    if server._firm_store.layout.project_root != artifact_root:
        raise RuntimeError("Server artifact-root isolation did not take effect")

    invocation = [sys.executable, *sys.argv]
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "started_at": _timestamp(),
        "checkout_root": str(checkout_root),
        "artifact_root": str(artifact_root),
        "exact_command": invocation,
        "board_id": args.board_id,
        "requested_hardware": {
            "probe_id": args.probe_id,
            "serial_id": args.serial_id,
            "target": args.target,
            "mcu_part_number": args.mcu_part_number,
        },
        "safety_authority": {
            "m7_gate_claimed": False,
            "note": "M7 owns the real safety map and validated-session stamp.",
        },
    }

    user_input = SetupUserInput(
        args.board_id,
        args.probe_id,
        args.display_name,
        args.mcu_part_number,
        args.baudrate,
    )
    first_inventory = server._setup_inventory(user_input)
    first_report = first_inventory.to_report(user_input)
    evidence["first_resolution"] = first_report
    if len(first_inventory.probes) != 1 or first_inventory.probes[0].probe_id != args.probe_id:
        raise RuntimeError("The requested probe did not resolve uniquely")
    if len(first_inventory.serial_ports) != 1:
        raise RuntimeError("The requested board UART did not resolve uniquely")
    if first_inventory.serial_ports[0].serial_id != args.serial_id:
        raise RuntimeError("The resolved UART does not match --serial-id")
    if first_inventory.exact_detected_targets != (args.target,):
        raise RuntimeError("The exact target did not resolve from the official board mapping")

    pack_specs = [spec for spec in load_manifest() if args.board_id in spec.needed_by_boards]
    evidence["target_and_pack"] = {
        "target": args.target,
        "resolution": "exact official board mapping, part-consistency checked",
        "support": "manifest" if pack_specs else "built_in",
        "packs": [
            {
                **asdict(spec),
                "local_path": str(PACKS_DIR / spec.filename),
                "local_sha256": sha256_file(PACKS_DIR / spec.filename),
                "checksum_matches": sha256_file(PACKS_DIR / spec.filename) == spec.sha256,
            }
            for spec in pack_specs
        ],
    }

    await server.mcp.call_tool(
        "load_setup_tool", {"board_id": args.board_id, "tool_name": "board_setup-plan"}
    )
    null_guidance = _text_result(await server.mcp.call_tool("board_setup-plan", {}))
    plan_parameters = {
        "mode": "setup",
        "connection_id": args.probe_id,
        "display_name": args.display_name,
        "mcu_part_number": args.mcu_part_number,
        "serial_baudrate": args.baudrate,
    }
    plan_submission = _text_result(
        await server.mcp.call_tool(
            "board_setup-plan",
            {
                "board_id": args.board_id,
                "hypothesis": "The selected Nucleo can complete bounded first-time M6 setup.",
                "hypothesis_made": True,
                "strategy": "Resolve exact hardware, live-connect, commit core, and stop at M7 safety.",
                "strategy_evaluated": True,
                "expected_fail_return": "A terminal setup status with immutable evidence.",
                "expected_success_return": "Hardware checks pass while the M7 safety gate stays closed.",
                "max_calls": 1,
                "max_calls_buffer": 0,
                "user_permission": "one-time",
                **plan_parameters,
            },
        )
    )
    setup_result = _json_result(
        await server.mcp.call_tool("board_setup", {"board_id": args.board_id, **plan_parameters})
    )
    evidence["setup"] = {
        "null_guidance": null_guidance,
        "plan_submission": plan_submission,
        "result": setup_result,
    }
    if setup_result["status"] != "setup_safety_incomplete":
        raise RuntimeError(f"Unexpected setup terminal status: {setup_result['status']}")

    profile = server._profile_repository.load(args.board_id, include_legacy=False)
    core_path = profile.source_path
    core_hash = _sha256(core_path)
    evidence["profile_commit_order"] = {
        "core_commit": {
            "path": str(core_path),
            "sha256": core_hash,
            "created_at": profile.created_at,
            "updated_at": profile.updated_at,
        },
        "setup_phase_records": setup_result["observed"]["phase_records"],
    }

    # Validate optional silicon facts with read-only live reads before committing them.
    handle = target_control.open_session(
        board=None,
        unique_id=args.probe_id,
        target=args.target,
        server_timeouts=server._staged_server_timeouts,
    )
    try:
        enricher = EnrichmentValidator(
            safe_readable=lambda address, width: (
                width == 32 and address in {args.test_read_address, args.silicon_id_address}
            ),
            read_value=lambda address, width: target_control.read_memory(handle, address, width),
        )
        test_read = enricher.test_read_address(args.test_read_address)
        silicon = enricher.silicon_identity(
            SiliconIdentityCandidate(
                args.silicon_id_address,
                args.silicon_id_expected,
                args.silicon_id_mask,
                32,
                "DBGMCU_IDCODE.DEV_ID",
            )
        )
    finally:
        target_control.close_session(handle)
    optional_fields = {**dict(test_read.fields), **dict(silicon.fields)}
    coordinator = ProfileCommitCoordinator(
        server._profile_repository,
        live_connect=lambda _target, _pack: None,
    )
    enriched = coordinator.commit_optional(
        args.board_id,
        EnrichmentResult(
            optional_fields,
            {"test_read": dict(test_read.observations), "silicon": dict(silicon.observations)},
        ),
    )
    enriched_bytes = enriched.source_path.read_bytes()
    enriched_hash = _sha256(enriched.source_path)
    evidence["profile_commit_order"]["optional_commit"] = {
        "path": str(enriched.source_path),
        "sha256": enriched_hash,
        "updated_at": enriched.updated_at,
        "occurred_after_core": enriched.updated_at != profile.updated_at,
        "observations": {
            "test_read": dict(test_read.observations),
            "silicon": dict(silicon.observations),
        },
    }

    await server.mcp.call_tool(
        "load_setup_tool", {"board_id": args.board_id, "tool_name": "board_validate"}
    )
    validation = _json_result(
        await server.mcp.call_tool(
            "board_validate",
            {"board_id": args.board_id, "probe_id": args.probe_id, "serial_id": args.serial_id},
        )
    )
    evidence["validation"] = validation
    if (
        validation["status"] != "validation_incomplete"
        or validation.get("observed", {}).get("hardware_result")
        != "validation_passed_uart_not_configured"
    ):
        raise RuntimeError(
            "The bounded hardware validation did not reach the expected M7 placeholder"
        )

    uart = capture_uart_output(
        args.serial_id,
        args.baudrate,
        3.0,
        None,
        reopen_attempts=0,
        max_bytes=65536,
    )
    evidence["uart_observation"] = {
        "port": args.serial_id,
        "baudrate": args.baudrate,
        "duration_bound_seconds": 3.0,
        "byte_bound": 65536,
        "captured_text": uart.text,
        "captured_bytes": len(uart.text.encode("utf-8")),
        "profile_expectation_configured": False,
    }

    # The exact stable pairing is explicitly confirmed only after live validation.
    selected_probe = first_inventory.probes[0]
    selected_serial = first_inventory.serial_ports[0]
    server._attachment_cache.confirm(
        args.board_id,
        ProbeIdentity(selected_probe.probe_family, selected_probe.usb_serial),
        SerialEndpoint(
            selected_serial.port_path,
            selected_serial.usb_serial,
            selected_serial.vid,
            selected_serial.pid,
        ),
    )
    second_inventory = server._setup_inventory(user_input)
    evidence["cache"] = {
        "confirmation_timing": "after successful bounded hardware validation",
        "cache_path": str(server._attachment_cache.path),
        "cache_sha256": _sha256(server._attachment_cache.path),
        "second_resolution": asdict(second_inventory.cache_resolution),
    }
    if (
        not second_inventory.cache_resolution.reused
        or second_inventory.cache_resolution.reason != "exact_match"
    ):
        raise RuntimeError(
            "The second attachment resolution did not reuse the exact stable cache match"
        )

    # Deliberately alter only the isolated profile fixture, prove failure is non-mutating,
    # and restore exactly the pre-test bytes afterward.
    wrong_expected = (args.silicon_id_expected ^ 1) & args.silicon_id_mask
    mismatch_stage = server._profile_repository.stage_optional(
        args.board_id, {"silicon_id_expected": wrong_expected}
    )
    mismatch_profile = server._profile_repository.commit_optional(mismatch_stage)
    mismatch_bytes = mismatch_profile.source_path.read_bytes()
    mismatch_hash = _sha256(mismatch_profile.source_path)
    mismatch_result = _json_result(
        await server.mcp.call_tool(
            "board_validate",
            {"board_id": args.board_id, "probe_id": args.probe_id, "serial_id": args.serial_id},
        )
    )
    unchanged_after_failure = mismatch_profile.source_path.read_bytes() == mismatch_bytes
    server._firm_store.atomic_write_bytes(mismatch_profile.source_path, enriched_bytes)
    restored_hash = _sha256(mismatch_profile.source_path)
    evidence["silicon_mismatch"] = {
        "fixture_expected": wrong_expected,
        "fixture_sha256": mismatch_hash,
        "result": mismatch_result,
        "profile_unchanged_by_validation": unchanged_after_failure,
        "restored_only_fixture": str(mismatch_profile.source_path),
        "restored_sha256": restored_hash,
        "restored_matches_pretest": restored_hash == enriched_hash,
    }
    if (
        mismatch_result["status"] != "validation_failed"
        or mismatch_result["code"] != "validation/silicon-mismatch"
    ):
        raise RuntimeError("The deliberate silicon mismatch was not rejected precisely")
    if not unchanged_after_failure or restored_hash != enriched_hash:
        raise RuntimeError("Mismatch validation rewrote the profile or fixture restoration failed")

    report_files = sorted((artifact_root / ".firm").glob("**/report.json"))
    report_hashes_before = {str(path): _sha256(path) for path in report_files}
    immutable_error = None
    if report_files:
        try:
            server._firm_store.atomic_create_bytes(report_files[0], b"tamper")
        except ImmutableArtifactError as exc:
            immutable_error = str(exc)
    report_hashes_after = {str(path): _sha256(path) for path in report_files}
    evidence["immutable_reports"] = {
        "count": len(report_files),
        "hashes": report_hashes_after,
        "overwrite_refusal": immutable_error,
        "unchanged_after_refusal": report_hashes_before == report_hashes_after,
    }
    if not report_files or immutable_error is None or report_hashes_before != report_hashes_after:
        raise RuntimeError("Immutable report preservation check failed")

    evidence["terminal_status"] = "hardware_checks_passed_safety_closed"
    evidence["completed_at"] = _timestamp()
    return evidence


def main() -> int:
    args = _arguments()
    artifact_root = args.artifact_root.expanduser().resolve()
    result_path = artifact_root / "acceptance.json"
    try:
        result = asyncio.run(_run(args))
    except Exception as exc:
        artifact_root.mkdir(parents=True, exist_ok=True)
        failure = {
            "schema_version": 1,
            "terminal_status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "recorded_at": _timestamp(),
            "exact_command": [sys.executable, *sys.argv],
        }
        result_path.write_text(
            json.dumps(failure, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        raise
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(result_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
