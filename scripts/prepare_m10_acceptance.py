#!/usr/bin/env python3
"""Prepare, but never execute, the final M10 acceptance matrix."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
import platform
import re
import shutil
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pyocd.target.builtin import BUILTIN_TARGETS

from pyocd_debug_mcp.kernel.processes import run_owned
from pyocd_debug_mcp.probe_inventory import _list_connected_probes_via_pyocd_api
from pyocd_debug_mcp.safety.linker import (
    BuildArtifactSelection,
    BuildRole,
    extract_build_evidence,
)
from pyocd_debug_mcp.serial_resolver import list_serial_ports

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "Design_Proto_Spec.md"


def _node(path: str, function: str) -> str:
    return f"tests/{path}::{function}"


FEATURE_PROOFS: dict[int, tuple[str, tuple[str, ...]]] = {
    1: (
        "Handshake assertions inspect visibility, complete guidance, side-effect freedom, and restart-default authority.",
        (
            _node("test_initialization_handshake.py", "test_handshake_is_visible_at_server_run_start"),
            _node("test_initialization_handshake.py", "test_in_process_client_lists_and_calls_handshake_before_hardware"),
            _node("test_initialization_handshake.py", "test_repeated_handshake_is_side_effect_free"),
            _node("test_initialization_handshake.py", "test_fresh_interpreter_restart_has_new_empty_server_run"),
            _node("test_kernel_registry.py", "test_in_process_dynamic_list_and_notification_preserve_physical_lock"),
        ),
    ),
    2: (
        "Routing assertions compare exact fake handles, reject cross-board fallback/duplicates, and inspect deterministic setup inventory choices.",
        (
            _node("test_connections.py", "test_assign_enforces_one_board_per_connection_and_ignores_display_labels"),
            _node("test_connections.py", "test_two_fake_boards_route_to_the_named_handle"),
            _node("test_connections.py", "test_unconnected_board_b_cannot_fall_back_to_board_a"),
            _node("test_connections.py", "test_disconnect_clears_only_the_named_board"),
            _node("test_connections.py", "test_same_board_serializes_while_cross_board_operations_overlap"),
            _node("test_setup_preflight.py", "test_every_preflight_terminal_row_is_deterministic"),
        ),
    ),
    3: (
        "Registry assertions compare exact advertised/registered sets and prove a hidden direct call still hits the physical lock.",
        (
            _node("test_kernel_registry.py", "test_registry_visibility_never_authorizes_a_handler"),
            _node("test_m5_surface_contract.py", "test_m5_in_process_surface_is_exact_and_every_hidden_handler_is_locked"),
        ),
    ),
    4: (
        "Plan assertions exercise NULL-first initialization, exact binding, execution-start burn, replacement, exhaustion, and restart.",
        (
            _node("test_plan_engine.py", "test_ac_4_2_null_response_contains_complete_guidance"),
            _node("test_plan_engine.py", "test_ac_4_6_parameter_drift_is_exact_and_consumes_no_budget"),
            _node("test_plan_engine.py", "test_ac_4_7_each_terminal_path_after_start_has_no_budget_refund"),
            _node("test_plan_engine.py", "test_ac_4_10_restart_has_no_initialized_or_active_plan_state"),
        ),
    ),
    5: (
        "Permission assertions inspect structured modes, one-time 1,0 consumption, full-session scope, revocation, and reset.",
        (
            _node("test_permissions.py", "test_ac_5_2_one_time_permission_requires_exactly_one_zero_budget"),
            _node("test_permissions.py", "test_ac_5_3_one_time_is_consumed_at_execution_start_and_plan_relocks"),
            _node("test_permissions.py", "test_ac_5_5_full_session_grant_is_scoped_to_exact_tool_and_board"),
        ),
    ),
    6: (
        "Store/profile assertions inspect stem identity, Unicode uniqueness, exact part preservation, staged ordering, pack ownership, and writer confinement.",
        (
            _node("test_profiles_v2.py", "test_profile_filename_stem_must_match_internal_board_id"),
            _node("test_profiles_v2.py", "test_core_stage_and_commit_preserve_exact_part_and_absolute_timestamps"),
            _node("test_profiles_v2.py", "test_pack_manifest_is_authoritative_and_v2_rejects_package_identifiers"),
            _node("test_firmstore.py", "test_firmstore_is_the_only_low_level_writer_for_new_artifacts"),
        ),
    ),
    7: (
        "Workflow assertions inspect every preflight terminal row, setup/fix allowance, resumable first-unverified phase, immutable reports, and no silent profile mutation.",
        (
            _node("test_setup_preflight.py", "test_every_preflight_terminal_row_is_deterministic"),
            _node("test_setup_workflow.py", "test_failed_setup_gets_one_fix_with_fresh_preflight_and_first_unverified_resume"),
            _node("test_setup_workflow.py", "test_known_unknown_incomplete_and_mismatch_name_routes_do_not_mutate_profiles"),
        ),
    ),
    8: (
        "Research assertions inspect exact requested fields, immutable part number, candidate dedupe, retry exhaustion, and blocked classification.",
        (
            _node("test_setup_research.py", "test_research_prompt_is_self_contained_and_has_no_authority"),
            _node("test_setup_research.py", "test_reply_rejects_unrequested_fields_and_attempted_part_change"),
            _node("test_setup_research.py", "test_third_distinct_failure_exhausts_fact_budget"),
        ),
    ),
    9: (
        "Target/pack assertions inspect exact detection, live-connect-before-commit, checksum and provided-target checks, materially distinct retries, and manifest-only promotion.",
        (
            _node("test_setup_targets.py", "test_live_connect_failure_occurs_before_core_profile_commit"),
            _node("test_setup_packs.py", "test_checksum_mismatch_never_stages_or_promotes"),
            _node("test_setup_packs.py", "test_success_promotes_manifest_and_profile_has_no_pack_metadata"),
        ),
    ),
    10: (
        "Double-verification assertions compare exact/aliased agreement and prove incomplete, conflicting, or caller-defined range evidence fails closed.",
        (
            _node("test_safety_verify2.py", "test_exact_agreement_produces_only_reconciled_regions"),
            _node("test_safety_verify2.py", "test_region_disagreement_matrix_fails_closed"),
            _node("test_safety_verify2.py", "test_strict_schema_rejects_allowed_ranges_unknown_fields_and_wrong_authority"),
        ),
    ),
    11: (
        "Map assertions inspect half-open/UNKNOWN/prohibited semantics, source-specific fingerprints, atomic promotion, drift routing, and conflict preservation.",
        (
            _node("test_safety_regions.py", "test_unknown_is_default_and_any_uncovered_byte_denies"),
            _node("test_safety_fingerprints.py", "test_each_fingerprint_source_changes_independently"),
            _node("test_safety_refresh.py", "test_drift_routing_matrix_never_uses_refresh_for_anchor_or_structural_changes"),
        ),
    ),
    12: (
        "Validation assertions inspect the seven statuses, bounded non-destructive call order, silicon non-mutation, map requirement, cache confirmation, and exact stamp fields.",
        (
            _node("test_setup_validation.py", "test_exact_seven_status_vocabulary"),
            _node("test_setup_validation.py", "test_validation_backend_call_order_is_bounded_and_non_destructive"),
            _node("test_gate.py", "test_ac_12_1_stamp_binds_board_connection_probe_result_and_fingerprint"),
        ),
    ),
    13: (
        "Gate assertions inspect default closure, absence of open-gate tools, disk non-restoration, board-local disconnect, and fingerprint-drift remedies.",
        (
            _node("test_gate.py", "test_ac_13_1_restart_and_new_manager_are_default_closed"),
            _node("test_gate.py", "test_ac_13_4_disk_artifacts_never_restore_gate_authority"),
            _node("test_gate.py", "test_ac_11_6_fingerprint_drift_closes_gate_and_names_refresh"),
        ),
    ),
    14: (
        "Action assertions inspect RAM/peripheral/executable/flash containment, exact target and sector bounds, and zero backend mutation after refusal.",
        (
            _node("test_safety_enforcement.py", "test_ac_14_4_and_14_10_crafted_flash_rejection_has_zero_backend_calls"),
            _node("test_safety_enforcement.py", "test_backend_mutations_are_never_called_after_containment_refusal"),
            _node("test_revised_memory_flash_misc.py", "test_task8_surface_visibility_and_legacy_retirement"),
        ),
    ),
    15: (
        "Recovery assertions inspect complete disclosure, unchanged-plan approval, live binding invalidation, one-time consumption, typed backend, immutable reports, and closed gate.",
        (
            _node("test_target_unlock.py", "test_ac_15_2_and_15_3_permission_payload_is_complete_and_relayable"),
            _node("test_target_unlock.py", "test_ac_15_6_live_binding_change_invalidates_before_execution"),
            _node("test_target_unlock.py", "test_ac_15_7_execution_closes_gate_writes_report_and_consumes_once"),
            _node("test_target_unlock.py", "test_ac_15_8_only_typed_vendor_recovery_and_manual_only_refuses"),
        ),
    ),
    16: (
        "Batch assertions inspect whole-list structural precheck, child-time authorization/budget, policy drift, exact order/parity, and stop-on-failure.",
        (
            _node("test_batch.py", "test_ac_16_1_16_2_complete_precheck_rejects_before_any_child"),
            _node("test_batch.py", "test_ac_16_3_each_child_consumes_budget_only_at_its_execution_start"),
            _node("test_batch.py", "test_ac_16_5_children_execute_in_order_and_match_direct_results"),
        ),
    ),
    17: (
        "Lifecycle assertions use real subprocess MCP traffic to inspect EOF/cancel/timeout cleanup, flash completion-before-release, busy isolation, finalizers, and stale markers.",
        (
            _node("test_lifecycle_stdio_integration.py", "test_client_eof_mid_operation_cleans_every_resource_and_releases_lock"),
            _node("test_lifecycle_stdio_integration.py", "test_cancellation_during_flash_finishes_transaction_before_cleanup"),
            _node("test_finalizers.py", "test_failing_finalizer_precedes_and_never_blocks_mandatory_cleanup"),
            _node("test_process_hygiene.py", "test_seeded_live_marker_is_identity_checked_and_terminated"),
        ),
    ),
    18: (
        "Cache assertions inspect exact stable identity reuse with current port resolution, every ignore condition, revocation, and authority-free schema.",
        (
            _node("test_attachment_cache.py", "test_exact_match_reuses_current_port_path_in_a_later_run"),
            _node("test_attachment_cache.py", "test_every_cache_ignore_condition_requires_reconfirmation"),
            _node("test_attachment_cache.py", "test_revocation_survives_cache_reconstruction"),
        ),
    ),
    19: (
        "Isolation assertions inspect exact handle routing, board-local gate closure, board-scoped plans/permissions, and cross-board concurrency.",
        (
            _node("test_connections.py", "test_same_board_serializes_while_cross_board_operations_overlap"),
            _node("test_connections.py", "test_ac_13_3_disconnect_clears_only_named_assignment_stamp_and_gate"),
            _node("test_permissions.py", "test_ac_5_5_full_session_grant_is_scoped_to_exact_tool_and_board"),
        ),
    ),
}

CC_PROOFS: dict[int, tuple[str, str]] = {
    1: (_node("test_m10_security.py", "test_cc_1_server_entrypoint_is_stdio_only_and_opens_no_socket_listener"), "AST inspects the entrypoint and FastMCP stdio default."),
    2: (_node("test_kernel_registry.py", "test_registry_visibility_never_authorizes_a_handler"), "A hidden direct call is refused by the handler lock."),
    3: (_node("test_safety_regions.py", "test_prohibited_overrides_all_for_every_overlap_shape"), "All prohibited overlap shapes override broader classifications."),
    4: (_node("test_m10_security.py", "test_cc_4_and_cc_5_public_schemas_expose_no_shell_or_authority_write_route"), "Public schemas are recursively checked for authority/range/shell routes."),
    5: (_node("test_finalizers.py", "test_hostile_or_arbitrary_finalizers_are_rejected"), "Strings, commands, and unknown structured actions are rejected."),
    6: (_node("test_target_unlock.py", "test_ac_5_7_and_15_5_full_session_never_authorizes_or_carries_forward"), "Mass erase refuses full-session and prior grants."),
    7: (_node("test_m10_relay_text.py", "test_cc_7_setup_and_research_prompts_are_plain_user_relay_text"), "Representative prompts are prose and exclude opaque identifiers/payload syntax."),
    8: (_node("test_packaging_contract.py", "test_public_scripts_and_dependencies_are_byo_only"), "The public command boundary excludes a user terminal/research-provider layer."),
    9: (_node("test_lifecycle_stdio_integration.py", "test_real_mcp_cancellation_cleans_then_same_board_resources_are_reusable"), "A real MCP cancellation notification reaches managed cleanup."),
    10: (_node("test_m10_performance.py", "test_m10_performance_targets_are_measured_with_host_context"), "The recorded gate/freshness maximum is retained and compared with 250 ms."),
    11: (_node("test_m10_performance.py", "test_m10_performance_targets_are_measured_with_host_context"), "Eight-probe/eight-port enumeration is measured against 10 seconds."),
    12: (_node("test_m10_performance.py", "test_m10_performance_targets_are_measured_with_host_context"), "NULL-plan and handshake are measured against 2 seconds."),
    13: (_node("test_m10_security.py", "test_cc_13_every_registered_dispatch_has_a_finite_timeout"), "Every registered tool and dispatch call site has a positive finite timeout."),
    14: (_node("test_safety_enforcement.py", "test_backend_mutations_are_never_called_after_containment_refusal"), "Ordinary unsafe mutations are rejected before backend calls."),
    15: (_node("test_lifecycle_stdio_integration.py", "test_timeout_failure_and_repeated_cleanup_have_one_start_budget_and_parity"), "All termination paths share cleanup and permit reuse."),
    16: (_node("test_profiles_v2.py", "test_core_stage_and_commit_preserve_exact_part_and_absolute_timestamps"), "Profile creation and staged updates are server owned."),
    17: (_node("test_firmstore_reports.py", "test_setup_report_is_immutable_while_its_log_is_append_only"), "Attempts are immutable and logs append only."),
    18: (_node("test_attachment_cache.py", "test_revocation_survives_cache_reconstruction"), "Cache revocation is independent and persistent."),
    19: (_node("test_m10_security.py", "test_cc_4_firmstore_rejects_every_authority_field_at_any_depth"), "All authority-bearing keys are rejected recursively."),
    20: (_node("test_m10_relay_text.py", "test_cc_7_setup_and_research_prompts_are_plain_user_relay_text"), "The server interface is conversational prose plus structured agent control."),
    21: (_node("test_setup_preflight.py", "test_every_preflight_prompt_and_choice_is_plain_prose_with_relay_guard"), "All preflight prompts and friendly choices are inspected."),
    22: (_node("test_profiles_v2.py", "test_unicode_display_name_round_trips_losslessly_through_disk"), "A non-ASCII display name survives commit and reload exactly."),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _official_profile(board_id: str) -> dict[str, object]:
    path = ROOT / f"boards/{board_id}.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    expected: dict[str, dict[str, object]] = {
        "nucleo_l476rg": {
            "board_id": "nucleo_l476rg",
            "mcu_family": "stm32l476",
            "probe_family": "stlink",
            "pyocd_target": "stm32l476rgtx",
            "serial_baudrate": 115200,
            "test_read_address": 0x08000000,
        },
        "nrf52833dk": {
            "board_id": "nrf52833dk",
            "mcu_family": "nrf52833",
            "probe_family": "jlink",
            "pyocd_target": "nrf52833",
            "serial_baudrate": 115200,
            "test_read_address": 0x10000000,
            "silicon_id_address": 0x10000100,
            "silicon_id_expected": 0x00052833,
        },
    }
    verified = expected[board_id]
    for field, value in verified.items():
        if document.get(field) != value:
            raise RuntimeError(f"Official {board_id} fixture has unexpected {field}")
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "verified_fields": verified,
    }


def _descriptions(prefix: str) -> dict[str, str]:
    text = SPEC.read_text(encoding="utf-8")
    pattern = rf"- \*\*({prefix}-\d+(?:\.\d+)?)\*\*\s+(.*?)(?=\n- \*\*{prefix}-|\n\n(?:###|---|##))"
    return {
        name: " ".join(body.split())
        for name, body in re.findall(pattern, text, flags=re.DOTALL)
    }


def _inspect_test(node_id: str) -> dict[str, object]:
    relative, function = node_id.split("::", 1)
    path = ROOT / relative
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    node = next(
        (
            item
            for item in tree.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and item.name == function
        ),
        None,
    )
    if node is None:
        raise RuntimeError(f"Traceability node does not exist: {node_id}")
    direct_asserts = sum(isinstance(item, ast.Assert) for item in ast.walk(node))
    raises = sum(
        isinstance(item, ast.Call)
        and isinstance(item.func, ast.Attribute)
        and item.func.attr == "raises"
        for item in ast.walk(node)
    )
    if direct_asserts + raises == 0:
        raise RuntimeError(f"Traceability node has no inspected assertion/refusal: {node_id}")
    assertion_expressions = [
        {
            "line": item.lineno,
            "kind": "assert",
            "expression": ast.get_source_segment(source, item) or ast.unparse(item),
        }
        for item in ast.walk(node)
        if isinstance(item, ast.Assert)
    ]
    assertion_expressions.extend(
        {
            "line": item.lineno,
            "kind": "pytest.raises",
            "expression": ast.get_source_segment(source, item) or ast.unparse(item),
        }
        for item in ast.walk(node)
        if isinstance(item, ast.Call)
        and isinstance(item.func, ast.Attribute)
        and item.func.attr == "raises"
    )
    assertion_expressions.sort(key=lambda item: int(item["line"]))
    return {
        "node_id": node_id,
        "line": node.lineno,
        "direct_asserts": direct_asserts,
        "raises_contexts": raises,
        "inspected_assertions": assertion_expressions,
    }


def _range_document(value: object) -> object:
    if value is None:
        return None
    return asdict(value)  # type: ignore[arg-type]


def _artifact(board_id: str, elf: Path, hex_file: Path) -> dict[str, object]:
    evidence = extract_build_evidence(
        BuildArtifactSelection(
            f"{board_id}_task20",
            BuildRole.APPLICATION,
            elf,
            hex_path=hex_file,
        )
    )
    if not evidence.flash_available or evidence.flash_partition is None:
        raise RuntimeError(f"{board_id} reference build is not safe application evidence")
    return {
        "board_id": board_id,
        "role": "application",
        "elf": {"path": str(elf), "sha256": _sha256(elf)},
        "hex": {"path": str(hex_file), "sha256": _sha256(hex_file)},
        "flash_partition": _range_document(evidence.flash_partition),
        "entry_point": evidence.entry_point,
        "vector_table": evidence.vector_table,
        "load_ranges": [
            _range_document(segment.load_range)
            for segment in evidence.loadable_segments
            if segment.load_range is not None
        ],
        "hex_ranges": [_range_document(item) for item in evidence.hex_ranges],
        "application_partition_contained": True,
    }


def _version(argv: list[str]) -> dict[str, object]:
    executable = shutil.which(argv[0])
    if executable is None:
        return {"available": False, "argv": argv}
    completed = run_owned(
        [executable, *argv[1:]],
        capture_output=True,
        text=True,
        timeout=5.0,
    )
    output = (completed.stdout or completed.stderr or "").strip().splitlines()
    return {
        "available": completed.returncode == 0,
        "argv": [executable, *argv[1:]],
        "version": output[0] if output else None,
    }


def _vscode_version() -> dict[str, object]:
    install_root = Path.home() / "AppData/Local/Programs/Microsoft VS Code"
    manifests = sorted(install_root.glob("*/resources/app/package.json"))
    if not manifests:
        return {"available": False}
    manifest = manifests[-1]
    document = json.loads(manifest.read_text(encoding="utf-8"))
    return {
        "available": True,
        "version": str(document["version"]),
        "manifest": str(manifest),
    }


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--nucleo-probe-id", required=True)
    parser.add_argument("--nucleo-serial-id", required=True)
    parser.add_argument("--nucleo-artifact-source", type=Path, required=True)
    parser.add_argument("--result-root", type=Path, required=True)
    parser.add_argument("--nrf-probe-id")
    parser.add_argument("--nrf-serial-id")
    parser.add_argument("--nrf-backup-hex", type=Path)
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    ac_descriptions = _descriptions("AC")
    cc_descriptions = _descriptions("CC")
    if len(ac_descriptions) != 122 or len(cc_descriptions) != 22:
        raise RuntimeError("The specification criterion vocabulary changed")

    inspected: dict[str, dict[str, object]] = {}
    for _, nodes in FEATURE_PROOFS.values():
        for node in nodes:
            inspected.setdefault(node, _inspect_test(node))
    for node, _ in CC_PROOFS.values():
        inspected.setdefault(node, _inspect_test(node))

    boundary = json.loads(
        (ROOT / "docs/evidence/m10-software-boundary-2026-07-17.json").read_text(
            encoding="utf-8"
        )
    )
    task20_by_ac: dict[str, list[str]] = {}
    task20_by_cc: dict[str, list[str]] = {}
    procedure_types: dict[str, str] = {}
    for procedure in boundary["task20_remaining"]:
        procedure_id = str(procedure["id"])
        procedure_types[procedure_id] = str(procedure["type"])
        for criterion in procedure.get("criterion_ids", []):
            task20_by_ac.setdefault(str(criterion), []).append(procedure_id)
        for criterion in procedure.get("cross_cutting_ids", []):
            task20_by_cc.setdefault(str(criterion), []).append(procedure_id)

    probes = []
    probe_error: str | None = None
    try:
        probes = [asdict(item) for item in _list_connected_probes_via_pyocd_api()]
    except Exception as exc:  # noqa: BLE001 - inventory report must preserve the exact blocker
        probe_error = f"{type(exc).__name__}: {exc}"
    serial = list_serial_ports() or []
    ports = [asdict(item) for item in serial]
    probe_ids = {str(item["uid"]) for item in probes}
    port_by_device = {str(item["device"]): item for item in ports}

    source = args.nucleo_artifact_source.resolve()
    v1_elf = source / "builds/v1/reference/build/firmware.elf"
    v1_hex = source / "builds/v1/reference/build/firmware.hex"
    v2_elf = source / "builds/v2/reference/build/firmware.elf"
    v2_hex = source / "builds/v2/reference/build/firmware.hex"
    backup_hex = source / "backups/nucleo_l476rg_application_before.hex"
    for path in (v1_elf, v1_hex, v2_elf, v2_hex, backup_hex):
        if not path.is_file():
            raise FileNotFoundError(path)

    tracked_artifacts = {
        board: _artifact(
            board,
            ROOT / f"firmware/{board}/reference/build/firmware.elf",
            ROOT / f"firmware/{board}/reference/build/firmware.hex",
        )
        for board in ("nucleo_l476rg", "nrf52833dk")
    }
    expected_partitions = {
        "nucleo_l476rg": {"start": 0x08000000, "end": 0x08008000},
        "nrf52833dk": {"start": 0x00000000, "end": 0x00008000},
    }
    for board_id, expected_partition in expected_partitions.items():
        if tracked_artifacts[board_id]["flash_partition"] != expected_partition:
            raise RuntimeError(f"Official {board_id} application partition changed")
    nucleo_disposable = {
        "v1": _artifact("nucleo_l476rg", v1_elf, v1_hex),
        "v2": _artifact("nucleo_l476rg", v2_elf, v2_hex),
        "backup": {"path": str(backup_hex), "sha256": _sha256(backup_hex)},
        "ready": True,
        "purpose": "preserved M7 application flash/fingerprint-drift evidence",
    }
    if nucleo_disposable["v1"]["flash_partition"] != nucleo_disposable["v2"][
        "flash_partition"
    ]:
        raise RuntimeError("Nucleo disposable builds do not share one application partition")

    pack_path = ROOT / "packs/Keil.STM32L4xx_DFP.3.1.0.pack"
    expected_pack_hash = "5672383c07fbdcee0e471a33f4f8beb2e1f3200bc999244dcd6858e0e8e8203f"
    if not pack_path.is_file() or _sha256(pack_path) != expected_pack_hash:
        raise RuntimeError("Pinned Nucleo pack is missing or has the wrong checksum")

    nucleo_port = port_by_device.get(args.nucleo_serial_id)
    nucleo_ready = (
        args.nucleo_probe_id in probe_ids
        and nucleo_port is not None
        and nucleo_port.get("serial_number") == args.nucleo_probe_id
    )
    nrf_port = port_by_device.get(args.nrf_serial_id or "")
    nrf_ready = bool(
        args.nrf_probe_id
        and args.nrf_probe_id in probe_ids
        and nrf_port is not None
        and nrf_port.get("serial_number") in {args.nrf_probe_id, f"000{args.nrf_probe_id}"}
    )

    ac_rows = []
    for criterion in sorted(
        ac_descriptions,
        key=lambda item: tuple(int(value) for value in item.removeprefix("AC-").split(".")),
    ):
        feature = int(criterion.split("-")[1].split(".")[0])
        summary, nodes = FEATURE_PROOFS[feature]
        procedures = task20_by_ac.get(criterion, [])
        proof_type = "automated"
        if procedures:
            proof_type = (
                "automated_plus_manual"
                if all(procedure_types[item] == "manual_procedural" for item in procedures)
                else "automated_plus_hardware"
            )
        ac_rows.append(
            {
                "id": criterion,
                "description": ac_descriptions[criterion],
                "proof_type": proof_type,
                "automated_tests": [inspected[node] for node in nodes],
                "assertion_inspection": summary,
                "task20_procedures": procedures,
                "status": "prepared_not_executed",
            }
        )

    cc_rows = []
    for index in range(1, 23):
        criterion = f"CC-{index}"
        node, summary = CC_PROOFS[index]
        procedures = task20_by_cc.get(criterion, [])
        proof_type = "automated"
        if procedures:
            proof_type = (
                "automated_plus_manual"
                if all(procedure_types[item] == "manual_procedural" for item in procedures)
                else "automated_plus_hardware"
            )
        cc_rows.append(
            {
                "id": criterion,
                "description": cc_descriptions[criterion],
                "proof_type": proof_type,
                "automated_tests": [inspected[node]],
                "assertion_inspection": summary,
                "task20_procedures": procedures,
                "status": "prepared_not_executed",
            }
        )

    result_root = args.result_root.resolve()
    try:
        result_root.relative_to(ROOT)
    except ValueError:
        pass
    else:
        raise RuntimeError("Task 20 result root must be outside the repository")
    if result_root.exists() and any(result_root.iterdir()):
        raise RuntimeError("Task 20 result root must be new or empty")
    run_order = [
        {
            "order": 1,
            "id": "software_once",
            "ready": True,
            "destructive": False,
            "commands": [
                "uv run --locked pytest -q",
                "uv run --locked ruff check .",
                "uv run --locked pyright",
                f'uv build --out-dir "{result_root / "software/dist"}"',
            ],
            "result": str(result_root / "software/result.json"),
        },
        {
            "order": 2,
            "id": "nucleo_l476rg_setup_safety_validation_actions",
            "ready": nucleo_ready,
            "destructive": False,
            "preserved_evidence": "m7_nucleo_application_flash_and_refresh",
            "required_identity": {
                "board_id": "nucleo_l476rg",
                "probe_id": args.nucleo_probe_id,
                "serial_id": args.nucleo_serial_id,
                "target": "stm32l476rgtx",
            },
            "mcp_sequence": [
                "connect",
                "board_setup/board_fix_setup as routed",
                "board_safety_setup",
                "board_validate",
                "always-available reads/state/UART",
                "guarded read/write refusal and success matrix",
                "reuse preserved M7 application flash, boundary, and refresh proof",
                "do not repeat an application flash unless that evidence is invalidated",
                "disconnect and reconnect",
            ],
            "result": str(result_root / "boards/nucleo_l476rg/acceptance.json"),
        },
        {
            "order": 3,
            "id": "nrf52833dk_setup_safety_validation_actions",
            "ready": nrf_ready,
            "blocker": None if nrf_ready else "designated nRF52833 DK is not present",
            "destructive": "one application-partition flash only",
            "required_identity": {
                "board_id": "nrf52833dk",
                "probe_id": args.nrf_probe_id or "<DESIGNATED_NRF52833_PROBE_UID>",
                "serial_id": args.nrf_serial_id or "<MATCHED_NRF52833_UART>",
                "target": "nrf52833",
                "required_ficr_info_part": "0x00052833",
            },
            "mcp_sequence": [
                "connect with exact probe identity",
                "read FICR.INFO.PART and stop unless exactly 0x00052833",
                "board_setup/board_fix_setup as routed",
                "board_safety_setup",
                "board_validate",
                "always-available reads/state/UART",
                "guarded read/write refusal and success matrix",
                "flash_application exactly once with tracked application artifact",
                "board_safety_refresh without a second flash",
                "disconnect and reconnect",
            ],
            "result": str(result_root / "boards/nrf52833dk/acceptance.json"),
        },
        {
            "order": 4,
            "id": "single_designated_recovery_proof",
            "ready": bool(
                nrf_ready and args.nrf_backup_hex and args.nrf_backup_hex.is_file()
            ),
            "blocker": (
                None
                if nrf_ready and args.nrf_backup_hex and args.nrf_backup_hex.is_file()
                else "designated nRF52833 DK and verified firmware backup are required"
            ),
            "destructive": "one mass erase maximum",
            "required_identity": {
                "board_id": "nrf52833dk",
                "probe_id": args.nrf_probe_id or "<DESIGNATED_NRF52833_PROBE_UID>",
                "target": "nrf52833",
                "required_ficr_info_part": "0x00052833",
            },
            "prerequisites": [
                "recoverable designated board positively matched",
                "firmware backup path and SHA-256 recorded",
                "current safety map compared to disclosure",
                "fresh target_unlock-plan one-time human approval",
            ],
            "mcp_sequence": [
                "target_unlock-plan all-NULL",
                "target_unlock-plan disclosure with unchanged-plan approval handshake",
                "target_unlock exactly once",
                "prove gate closed",
                "board_validate",
                "prove guarded operation restored",
                "request a second plan and prove fresh permission is required without executing it",
            ],
            "result": str(result_root / "recovery/nrf52833dk/acceptance.json"),
        },
        {
            "order": 5,
            "id": "lifecycle_cancellation_without_repeat_flash",
            "ready": nucleo_ready,
            "destructive": False,
            "required_identity": {
                "board_id": "nucleo_l476rg",
                "probe_id": args.nucleo_probe_id,
                "serial_id": args.nucleo_serial_id,
            },
            "sequence": [
                "interrupt bounded serial/read work with a cancellation-sending client",
                "record non-sending clients as timeout-only if available",
                "verify reset/probe/UART release and immediate reconnect",
                "reuse preserved M9 non-interruptible flash evidence; do not flash again",
            ],
            "result": str(result_root / "lifecycle/acceptance.json"),
        },
        {
            "order": 6,
            "id": "simultaneous_two_board_isolation",
            "ready": bool(nucleo_ready and nrf_ready),
            "blocker": (
                None
                if nucleo_ready and nrf_ready
                else "both positively identified official boards must be present together"
            ),
            "destructive": False,
            "required_identities": [
                {"board_id": "nucleo_l476rg", "probe_id": args.nucleo_probe_id},
                {
                    "board_id": "nrf52833dk",
                    "probe_id": args.nrf_probe_id or "<DESIGNATED_NRF52833_PROBE_UID>",
                },
            ],
            "sequence": [
                "prove one-to-one assignment",
                "board A validated while board B guarded action is denied",
                "prove plans and full-session permissions do not cross boards",
                "run bounded cross-board operations concurrently",
                "disconnect board A and prove board B gate/plan/permission remain active",
            ],
            "result": str(result_root / "two-board/acceptance.json"),
        },
    ]

    output = {
        "schema_version": 1,
        "milestone": "M10",
        "task": "Prompt 20.1 acceptance preparation",
        "generated_at": _timestamp(),
        "semantics_changed": False,
        "execution_status": "prepared_not_executed",
        "external_result_root": {
            "path": str(result_root),
            "exists_at_preparation": result_root.exists(),
            "empty_at_preparation": not result_root.exists() or not any(result_root.iterdir()),
        },
        "criterion_counts": {"acceptance": len(ac_rows), "cross_cutting": len(cc_rows)},
        "traceability": {"acceptance": ac_rows, "cross_cutting": cc_rows},
        "fixture_verification": {
            "profiles": {
                board: _official_profile(board)
                for board in ("nucleo_l476rg", "nrf52833dk")
            },
            "tracked_reference_artifacts": tracked_artifacts,
            "nucleo_disposable_artifacts": nucleo_disposable,
            "nrf52833dk_disposable_artifacts": {
                "ready": False,
                "reference_application": tracked_artifacts["nrf52833dk"],
                "blocker": "no positively matched nRF52833 DK firmware backup exists",
            },
            "pinned_pack": {"path": str(pack_path), "sha256": _sha256(pack_path)},
            "target_resolution": {
                "nucleo_l476rg": {
                    "target": "stm32l476rgtx",
                    "source": "pinned CMSIS-Pack",
                    "available_from_verified_pack": True,
                },
                "nrf52833dk": {
                    "target": "nrf52833",
                    "source": "pyOCD built-in target registry",
                    "available": "nrf52833" in BUILTIN_TARGETS,
                },
            },
        },
        "preserved_destructive_evidence": {
            "m7_nucleo_application_flash_and_refresh": {
                "path": str(ROOT / "docs/evidence/m7-hardware-acceptance-2026-07-17.md"),
                "sha256": _sha256(
                    ROOT / "docs/evidence/m7-hardware-acceptance-2026-07-17.md"
                ),
                "reuse_scope": [
                    "Nucleo application-partition flash",
                    "erase-sector containment",
                    "application fingerprint drift and safety refresh",
                ],
            },
            "m9_nucleo_non_interruptible_flash": {
                "path": str(ROOT / "docs/evidence/m9-hardware-lifecycle-2026-07-17.json"),
                "sha256": _sha256(
                    ROOT / "docs/evidence/m9-hardware-lifecycle-2026-07-17.json"
                ),
                "reuse_scope": [
                    "non-interruptible application flash completion before release",
                    "probe/UART/reset release and reconnect",
                ],
            },
        },
        "inventory": {
            "probe_error": probe_error,
            "probes": probes,
            "serial_ports": ports,
            "nucleo_l476rg": {
                "ready": nucleo_ready,
                "required_probe_id": args.nucleo_probe_id,
                "required_serial_id": args.nucleo_serial_id,
            },
            "nrf52833dk": {
                "ready": nrf_ready,
                "required_probe_id": args.nrf_probe_id,
                "required_serial_id": args.nrf_serial_id,
                "required_ficr_info_part": "0x00052833",
                "wrong_board_must_not_be_substituted": "probe 683377322 / FICR 0x00052840",
            },
        },
        "recovery_readiness": {
            "ready": bool(nrf_ready and args.nrf_backup_hex and args.nrf_backup_hex.is_file()),
            "prerequisite_checks": {
                "designated_nrf52833_present": nrf_ready,
                "live_ficr_info_part_verified": False,
                "verified_firmware_backup_present": bool(
                    args.nrf_backup_hex and args.nrf_backup_hex.is_file()
                ),
                "profile_recovery_mode": "nrf_pyocd_unlock",
                "current_safety_map_present": False,
                "fresh_one_time_human_permission": "obtain only immediately before execution",
            },
            "backup": (
                {"path": str(args.nrf_backup_hex.resolve()), "sha256": _sha256(args.nrf_backup_hex)}
                if args.nrf_backup_hex and args.nrf_backup_hex.is_file()
                else None
            ),
            "fresh_human_permission": "required_at_execution_never_pregranted",
        },
        "versions": {
            "os": platform.platform(),
            "python": sys.version.split()[0],
            "pyocd": importlib.metadata.version("pyocd"),
            "mcp": importlib.metadata.version("mcp"),
            "pyserial": importlib.metadata.version("pyserial"),
            "pyelftools": importlib.metadata.version("pyelftools"),
            "package": importlib.metadata.version("pyocd-debug-mcp"),
            "uv": _version(["uv", "--version"]),
            "git": _version(["git", "--version"]),
            "codex": _version(["codex", "--version"]),
            "claude": _version(["claude", "--version"]),
            "vscode": _vscode_version(),
        },
        "hardware_command_contract": {
            "required_for_every_invocation": [
                "board_id",
                "stable_probe_id",
                "absolute_external_artifact_root",
                "machine_readable_result_path",
            ],
            "connect_requires": ["board_id", "probe_id", "target"],
            "nrf52833_requires_live_check": "FICR.INFO.PART == 0x00052833",
            "mismatch_action": "stop_before_setup, validation, flash, or recovery",
        },
        "remaining_procedures": boundary["task20_remaining"],
        "run_order": run_order,
        "safety_invariants": [
            "Every hardware phase has an explicit logical board and stable probe identity.",
            "Every phase writes a machine-readable result below one clean external result root.",
            "The wrong nRF52840 bench board is never substituted for nrf52833dk.",
            "The preserved Nucleo M7 application flash is not repeated unless invalidated.",
            "The nRF52833 application partition is flashed at most once when its board is available.",
            "Mass erase occurs at most once and only after fresh unchanged-plan approval.",
            "Lifecycle reuses preserved non-interruptible-flash evidence and does not flash again.",
            "No bootloader flash or needless second recovery is planned.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "nucleo_ready": nucleo_ready,
                "nrf_ready": nrf_ready,
                "recovery_ready": output["recovery_readiness"]["ready"],
            }
        )
    )


if __name__ == "__main__":
    main()
