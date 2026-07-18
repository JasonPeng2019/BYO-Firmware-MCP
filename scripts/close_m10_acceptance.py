#!/usr/bin/env python3
"""Produce the final Task 20 validation report from immutable execution evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _evidence(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": _sha256(path)}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result-root", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--execution", type=Path, required=True)
    parser.add_argument("--nucleo-result", type=Path, required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    result_root = args.result_root.resolve()
    plan_path = args.plan.resolve()
    execution_path = args.execution.resolve()
    nucleo_path = args.nucleo_result.resolve()
    plan = _load(plan_path)
    execution = _load(execution_path)
    nucleo = _load(nucleo_path)
    performance_path = ROOT / "docs/evidence/m10-performance-2026-07-17.json"
    lifecycle_path = ROOT / "docs/evidence/m9-hardware-lifecycle-2026-07-17.json"
    performance = _load(performance_path)
    lifecycle = _load(lifecycle_path)
    nrf_path = result_root / "boards/nrf52833dk/acceptance.json"
    recovery_path = result_root / "recovery/nrf52833dk/acceptance.json"
    isolation_path = result_root / "two-board/acceptance.json"
    inventory_path = result_root / "inventory.json"
    for path in (
        plan_path,
        execution_path,
        nucleo_path,
        performance_path,
        lifecycle_path,
        nrf_path,
        recovery_path,
        isolation_path,
        inventory_path,
    ):
        if not path.is_file():
            raise FileNotFoundError(path)
    if nucleo["status"] != "pass":
        raise RuntimeError("The focused halted Nucleo recheck did not pass")
    memory_result = nucleo["guarded_actions"]["read_memory_address"]
    if not memory_result.startswith("0x20001180"):
        raise RuntimeError("The final Nucleo vector read is not the accepted v2 value")

    procedure_results = {
        "official_pair_end_to_end": {
            "status": "blocked_missing_nrf52833",
            "evidence": [_evidence(nucleo_path), _evidence(nrf_path)],
        },
        "real_map_flash_and_backend_containment": {
            "status": "blocked_missing_nrf52833",
            "evidence": [
                _evidence(nucleo_path),
                _evidence(ROOT / "docs/evidence/m7-hardware-acceptance-2026-07-17.md"),
                _evidence(nrf_path),
            ],
        },
        "single_designated_recovery_proof": {
            "status": "blocked_identity_backup_map_and_permission",
            "evidence": [_evidence(recovery_path)],
        },
        "lifecycle_and_cancellation": {
            "status": "pass_with_q1_client_gaps",
            "evidence": [_evidence(lifecycle_path)],
        },
        "two_board_isolation": {
            "status": "blocked_missing_nrf52833",
            "evidence": [_evidence(isolation_path)],
        },
        "human_approval_legitimacy": {
            "status": "blocked_not_requested_after_identity_failure",
            "evidence": [_evidence(recovery_path)],
        },
        "client_relay_conduct": {
            "status": "manual_procedural_not_observed",
            "evidence": [
                _evidence(ROOT / "docs/evidence/m10-software-boundary-2026-07-17.json")
            ],
        },
    }

    def final_row(row: dict[str, Any]) -> dict[str, Any]:
        procedure_ids = row["task20_procedures"]
        statuses = [procedure_results[item]["status"] for item in procedure_ids]
        if not statuses:
            status = "pass"
        elif any(value.startswith("blocked") for value in statuses):
            status = "blocked"
        elif any(value.startswith("manual") for value in statuses):
            status = "blocked_manual_procedure"
        elif any("q1_client_gaps" in value for value in statuses):
            status = "partial"
        else:
            status = "pass"
        return {
            "id": row["id"],
            "description": row["description"],
            "status": status,
            "automated_test_nodes": [
                test["node_id"] for test in row["automated_tests"]
            ],
            "inspected_assertion_count": sum(
                len(test["inspected_assertions"]) for test in row["automated_tests"]
            ),
            "procedures": [
                {"id": procedure_id, **procedure_results[procedure_id]}
                for procedure_id in procedure_ids
            ],
        }

    acceptance_rows = [
        final_row(row) for row in plan["traceability"]["acceptance"]
    ]
    cross_cutting_rows = [
        final_row(row) for row in plan["traceability"]["cross_cutting"]
    ]
    status_counts: dict[str, int] = {}
    for row in [*acceptance_rows, *cross_cutting_rows]:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1

    open_questions = [
        {
            "id": "Q-1",
            "status": "open_external_client_matrix",
            "treatment": (
                "Official Python MCP SDK 1.28.1 sent notifications/cancelled and cleanup passed. "
                "Codex CLI, Claude Code, VS Code, a known non-sending client, and deliberately "
                "slowed real-memory cancellation remain unverified; finite timeout cleanup passes."
            ),
            "evidence": [_evidence(lifecycle_path)],
        },
        {
            "id": "Q-2",
            "status": "implemented_conservatively_pending_spec_confirmation",
            "treatment": (
                "Setup may complete without build artifacts, while flash remains unavailable "
                "until application/bootloader partitions exist."
            ),
            "evidence": ["tests/test_setup_validation.py", "tests/test_safety_enforcement.py"],
        },
        {
            "id": "Q-3",
            "status": "open_product_decision",
            "treatment": (
                "Manual deletion remains the supported forget path; no atomic forget-board "
                "workflow was added without product authorization."
            ),
            "evidence": ["archive_docs/Design_Proto_Spec.md", "docs/agent-contract.md"],
        },
        {
            "id": "Q-4",
            "status": "open_product_decision_and_hardware_limit",
            "treatment": (
                "Enumeration is measured at eight simulated devices, but only one official board "
                "is currently present and the required two-board live proof is blocked."
            ),
            "evidence": [_evidence(performance_path), _evidence(isolation_path)],
        },
        {
            "id": "Q-5",
            "status": "open_client_security_decision",
            "treatment": (
                "The specified soft permission gate, disclosure binding, and consumption are "
                "implemented. Genuine human approval and stronger client elicitation remain external."
            ),
            "evidence": ["tests/test_permissions.py", "tests/test_target_unlock.py"],
        },
        {
            "id": "Q-6",
            "status": "closed_for_initial_release",
            "treatment": (
                "The release remains English-only under A-17; Unicode display names round-trip "
                "losslessly."
            ),
            "evidence": ["tests/test_profiles_v2.py", "tests/test_m10_relay_text.py"],
        },
        {
            "id": "Q-7",
            "status": "closed_by_current_spec",
            "treatment": (
                "connect_under_reset fails clearly when reset-line support is absent and does not "
                "silently degrade."
            ),
            "evidence": ["tests/test_revised_session_execution_registers.py"],
        },
        {
            "id": "Q-8",
            "status": "implemented_conservatively_pending_spec_confirmation",
            "treatment": (
                "Validation uses the profile/default bounded three-second serial capture and remains "
                "non-destructive."
            ),
            "evidence": ["tests/test_setup_validation.py", _evidence(nucleo_path)],
        },
        {
            "id": "Q-9",
            "status": "open_product_decision",
            "treatment": (
                "Active session identity remains bound at validation. No mid-session rename behavior "
                "beyond the specification's implication was invented."
            ),
            "evidence": ["archive_docs/Design_Proto_Spec.md", "tests/test_profiles_v2.py"],
        },
        {
            "id": "Q-10",
            "status": "open_product_decision",
            "treatment": (
                "Per-action responses and action-specific immutable reports are implemented; no "
                "additional universal Layer 2 audit log was added without a product requirement."
            ),
            "evidence": ["tests/test_firmstore_reports.py", "docs/agent-contract.md"],
        },
    ]

    risks = [
        {
            "id": "R-1",
            "status": "mitigated",
            "treatment": "Pinned MCP dynamic listing/list_changed and physical locks pass in-process tests.",
            "evidence": ["tests/test_kernel_registry.py", "tests/test_initialization_handshake.py"],
        },
        {
            "id": "R-2",
            "status": "mitigated",
            "treatment": "Blocking pyOCD work uses per-board managed operations and bounded cleanup.",
            "evidence": ["tests/test_managed_operations.py", _evidence(nucleo_path)],
        },
        {
            "id": "R-3",
            "status": "mitigated",
            "treatment": "The active product contract formally supersedes extraction snapshots.",
            "evidence": ["tests/contracts/product-server-tools.json", "docs/contract-history.md"],
        },
        {
            "id": "R-4",
            "status": "partially_mitigated_external",
            "treatment": "One real client cancellation path passes; remaining client UI behavior is Q-1.",
            "evidence": [_evidence(lifecycle_path)],
        },
        {
            "id": "R-5",
            "status": "accepted_spec_limit_manual",
            "treatment": "Soft permission enforcement passes; human legitimacy is intentionally procedural.",
            "evidence": ["tests/test_permissions.py", "tests/test_target_unlock.py"],
        },
        {
            "id": "R-6",
            "status": "partial_nucleo_pass_nrf_blocked",
            "treatment": "Sector containment passed on Nucleo; the official nRF52833 proof is blocked.",
            "evidence": [
                _evidence(ROOT / "docs/evidence/m7-hardware-acceptance-2026-07-17.md"),
                _evidence(nrf_path),
            ],
        },
        {
            "id": "R-7",
            "status": "partial_windows_pass_posix_automated_only",
            "treatment": "Windows process groups pass real lifecycle tests; POSIX has abstraction tests only.",
            "evidence": ["tests/test_process_hygiene.py", "tests/test_lifecycle_stdio_integration.py"],
        },
        {
            "id": "R-8",
            "status": "mitigated",
            "treatment": "Strict evidence schemas and deterministic conflict/alias matrices fail closed.",
            "evidence": ["tests/test_safety_verify2.py"],
        },
        {
            "id": "R-9",
            "status": "partially_mitigated_pending_signoff",
            "treatment": "Conservative Q-2 and Q-8 behavior is implemented and documented pending confirmation.",
            "evidence": ["archive_docs/Implementation_Plan.md", "tests/test_setup_validation.py"],
        },
        {
            "id": "R-10",
            "status": "open_blocker",
            "treatment": "The simultaneous official-board proof cannot run until an nRF52833 DK is attached.",
            "evidence": [_evidence(isolation_path)],
        },
        {
            "id": "R-11",
            "status": "open_external_blocker",
            "treatment": "No LICENSE file or authoritative licensing decision exists; publication remains blocked.",
            "evidence": ["README.md", "pyproject.toml"],
        },
    ]

    implementation_findings = [
        {
            "finding": "Acceptance subprocess marker collided with startup hygiene.",
            "classification": "acceptance_harness_defect",
            "resolution": (
                "Acceptance subprocesses now use an external ProcessMarkerStore. Direct Pyright and "
                "stdio reruns passed; product server semantics were unchanged."
            ),
            "evidence": [
                _evidence(result_root / "software/affected-rerun-2/result.json"),
                "scripts/run_m10_software_acceptance.py",
            ],
        },
        {
            "finding": "Early Nucleo harness assumptions did not match public types/plans/A-15 cleanup.",
            "classification": "acceptance_harness_defect",
            "resolution": (
                "The harness now uses ValidationStamp, complete SetupUserInput, stable probe identity, "
                "and a reset-and-halt plan before each state-sensitive read."
            ),
            "evidence": [_evidence(nucleo_path)],
        },
        {
            "finding": "A running-core flash-base read returned transient zero.",
            "classification": "investigated_live_backend_state",
            "resolution": (
                "The affected read-only step was rerun halted and returned the accepted v2 vector "
                "0x20001180. No server implementation change or destructive rerun was required."
            ),
            "evidence": [_evidence(nucleo_path)],
        },
    ]

    inventory = _load(inventory_path)
    report = {
        "schema_version": 1,
        "milestone": "M10",
        "task": "Prompt 20.3 final validation closure",
        "recorded_at": _timestamp(),
        "overall_status": "blocked_external_hardware_and_manual_acceptance",
        "repository_completion": {
            "software_implementation_complete": True,
            "software_quality_green": True,
            "stdio_server_boots": True,
            "all_in_scope_criteria_pass": False,
            "every_hardware_criterion_has_real_evidence": False,
            "release_or_publication_complete": False,
            "repository_complete_under_task_20_definition": False,
        },
        "software": execution["software"],
        "performance": {
            "evidence": _evidence(performance_path),
            "host": performance["host"],
            "metrics": performance["metrics"],
        },
        "client_cancellation": {
            "evidence": _evidence(lifecycle_path),
            "status": lifecycle["status"],
            "verified_client": lifecycle["client"],
            "serial_cancellation": lifecycle["serial_cancellation"],
            "flash_cancellation": lifecycle["flash_cancellation"],
            "q1_matrix": lifecycle["q1_matrix"],
            "unverified": lifecycle["unverified_q1_rows"],
        },
        "hardware_identities": {
            "nucleo_l476rg": nucleo["identity"],
            "observed_incompatible_nordic": inventory["observed_nordic"],
            "required_nrf52833": {
                "board_id": "nrf52833dk",
                "ficr_info_part": "0x00052833",
                "status": "not_present",
            },
        },
        "hardware_results": {
            "nucleo_l476rg": {"status": "pass", "evidence": _evidence(nucleo_path)},
            "nrf52833dk": {"status": "blocked", "evidence": _evidence(nrf_path)},
            "recovery": {"status": "blocked", "evidence": _evidence(recovery_path)},
            "two_board": {"status": "blocked", "evidence": _evidence(isolation_path)},
        },
        "destructive_operation_accounting": execution["destructive_operation_accounting"],
        "implementation_findings": implementation_findings,
        "open_questions": open_questions,
        "risks": risks,
        "traceability": {
            "acceptance": acceptance_rows,
            "cross_cutting": cross_cutting_rows,
            "status_counts": status_counts,
            "failed_count": status_counts.get("fail", 0),
        },
        "repo_complete_work": [
            "All software behavior, contracts, packaging, docs, and performance checks are green.",
            "Nucleo setup/safety/validation/actions/containment/lifecycle evidence is real and passing.",
            "All unavailable hardware paths failed closed without substitution or destructive calls.",
        ],
        "external_blockers": [
            "Attach the designated nRF52833 DK and create a verified recoverable backup.",
            "Run nRF setup/safety/validation/application containment and the single approved recovery.",
            "Run the simultaneous official two-board isolation phase.",
            "Complete the remaining Q-1 client cancellation census and manual relay/approval observations.",
            "Obtain the authoritative licensing decision and add the appropriate LICENSE before publication.",
        ],
        "source_evidence": {
            "prepared_plan": _evidence(plan_path),
            "task20_execution": _evidence(execution_path),
            "external_result_root": str(result_root),
        },
    }

    if len(acceptance_rows) != 122 or len(cross_cutting_rows) != 22:
        raise RuntimeError("Final traceability does not cover the full criterion vocabulary")
    if len(open_questions) != 10 or len(risks) != 11:
        raise RuntimeError("Final Q/R treatment is incomplete")
    if status_counts.get("fail", 0):
        raise RuntimeError("An implementation failure remains in final traceability")

    json_text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    markdown = f"""# M10 final validation — 2026-07-17

## Outcome

**Blocked: the repository is not complete under Task 20's definition.** The software
implementation and quality boundary are green, and the Nucleo hardware boundary passes, but
the designated nRF52833 DK is absent. Recovery and simultaneous-board acceptance therefore
remain blocked, and publication also remains blocked by the missing licensing decision.

## Software and performance

- Complete suite: **717 passed** with 63 expected legacy-profile deprecation warnings.
- Ruff, Pyright, package build, dependency check, imports, and 35-tool stdio boot/shutdown pass.
- Gate/freshness maximum: `{performance['metrics']['gate_and_freshness']['max_seconds']:.6f}` s
  against 0.250 s.
- Eight-device enumeration maximum:
  `{performance['metrics']['enumerate_eight_devices']['max_seconds']:.6f}` s against 10 s.
- NULL-plan/handshake maximum:
  `{performance['metrics']['null_plan_and_handshake']['max_seconds']:.6f}` s against 2 s.

## Hardware

- Nucleo L476RG: **pass**, probe `{nucleo['identity']['probe_id']}`, UART
  `{nucleo['identity']['serial_id']}`. The focused halted recheck returned the accepted v2
  vector `0x20001180`; validation, guarded actions, cleanup/reconnect, and final gate closure pass.
- nRF52833 DK: **blocked**. The observed Nordic probe is
  `{inventory['observed_nordic']['probe_id']}` and reads
  `{inventory['observed_nordic']['ficr_info_part']}` (nRF52840), not `0x00052833`.
- Recovery: **blocked before permission or execution**. No correct board, backup, or live map.
- Simultaneous two-board isolation: **blocked** because only one official board is present.
- New destructive operations during Task 20: zero flashes, erases, unlocks, or bootloader writes.

## Traceability

- Pass: {status_counts.get('pass', 0)}
- Partial: {status_counts.get('partial', 0)}
- Blocked: {status_counts.get('blocked', 0)}
- Blocked manual/procedural: {status_counts.get('blocked_manual_procedure', 0)}
- Fail: {status_counts.get('fail', 0)}

Every AC-1.x through AC-19.x and CC-1 through CC-22 row, including exact automated test nodes,
inspected assertion counts, procedures, status, and evidence, is present in the companion JSON.

## Open questions and risks

All Q-1 through Q-10 and R-1 through R-11 have explicit treatments in the companion JSON.
Material open items are the client cancellation census, soft human-approval legitimacy,
product decisions Q-3/Q-4/Q-5/Q-9/Q-10, pending Q-2/Q-8 confirmation, real POSIX lifecycle
coverage, the nRF/two-board bench, and licensing.

## Evidence

- Machine-readable final report: `{args.json_output}`
- Task 20 execution report: `{execution_path}`
- External result root: `{result_root}`
- Final Nucleo result: `{nucleo_path}`
- Performance: `{performance_path}`
- Cancellation evidence: `{lifecycle_path}`
"""

    external_json = result_root / "final-validation.json"
    external_markdown = result_root / "final-validation.md"
    for path in (args.json_output, args.markdown_output, external_json, external_markdown):
        if path.exists():
            raise FileExistsError(path)
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json_text, encoding="utf-8")
    args.markdown_output.write_text(markdown, encoding="utf-8")
    external_json.write_text(json_text, encoding="utf-8")
    external_markdown.write_text(markdown, encoding="utf-8")
    print(
        json.dumps(
            {
                "overall_status": report["overall_status"],
                "json": str(args.json_output),
                "markdown": str(args.markdown_output),
                "status_counts": status_counts,
            }
        )
    )


if __name__ == "__main__":
    main()
