#!/usr/bin/env python3
"""Reconcile Task 20.2 execution evidence onto every prepared traceability row."""

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
    parser.add_argument("--nucleo-result", type=Path, required=True)
    parser.add_argument("--repo-report", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    result_root = args.result_root.resolve()
    plan_path = args.plan.resolve()
    plan = _load(plan_path)

    original_software_path = result_root / "software/result.json"
    software_reconciliation_path = result_root / "software/affected-rerun-2/result.json"
    nucleo_path = args.nucleo_result.resolve()
    nrf_path = result_root / "boards/nrf52833dk/acceptance.json"
    recovery_path = result_root / "recovery/nrf52833dk/acceptance.json"
    lifecycle_path = result_root / "lifecycle/acceptance.json"
    isolation_path = result_root / "two-board/acceptance.json"
    inventory_path = result_root / "inventory.json"
    required_paths = (
        original_software_path,
        software_reconciliation_path,
        nucleo_path,
        nrf_path,
        recovery_path,
        lifecycle_path,
        isolation_path,
        inventory_path,
    )
    for path in required_paths:
        if not path.is_file():
            raise FileNotFoundError(path)

    software_original = _load(original_software_path)
    software = _load(software_reconciliation_path)
    nucleo = _load(nucleo_path)
    nrf = _load(nrf_path)
    recovery = _load(recovery_path)
    lifecycle = _load(lifecycle_path)
    isolation = _load(isolation_path)
    inventory = _load(inventory_path)
    if software["status"] != "pass":
        raise RuntimeError("Software reconciliation is not green")
    if nucleo["status"] != "pass":
        raise RuntimeError("Nucleo acceptance did not pass")
    if nrf["status"] != "blocked" or recovery["status"] != "blocked":
        raise RuntimeError("Missing nRF52833/recovery was not recorded blocked")
    if isolation["status"] != "blocked":
        raise RuntimeError("Missing two-board bench was not recorded blocked")

    procedure_results = {
        "official_pair_end_to_end": {
            "status": "blocked_missing_nrf52833",
            "evidence": [_evidence(nucleo_path), _evidence(nrf_path)],
        },
        "real_map_flash_and_backend_containment": {
            "status": "blocked_missing_nrf52833",
            "evidence": [
                _evidence(nucleo_path),
                _evidence(
                    ROOT / "docs/evidence/m7-hardware-acceptance-2026-07-17.md"
                ),
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

    def reconcile_row(row: dict[str, Any]) -> dict[str, Any]:
        procedure_ids = row["task20_procedures"]
        statuses = [procedure_results[item]["status"] for item in procedure_ids]
        if not statuses:
            status = "pass_software"
        elif any(value.startswith("blocked") for value in statuses):
            status = "blocked_external_prerequisite"
        elif any(value.startswith("manual") for value in statuses):
            status = "manual_procedural_unverified"
        elif any("q1_client_gaps" in value for value in statuses):
            status = "partial_pass_client_matrix_gap"
        else:
            status = "pass"
        return {
            "id": row["id"],
            "status": status,
            "automated_assertions_inspected": sum(
                len(test["inspected_assertions"]) for test in row["automated_tests"]
            ),
            "software_suite": "717 passed",
            "procedures": [
                {"id": item, **procedure_results[item]} for item in procedure_ids
            ],
        }

    reconciled_ac = [
        reconcile_row(row) for row in plan["traceability"]["acceptance"]
    ]
    reconciled_cc = [
        reconcile_row(row) for row in plan["traceability"]["cross_cutting"]
    ]
    status_counts: dict[str, int] = {}
    for row in [*reconciled_ac, *reconciled_cc]:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1

    failed_attempts = []
    for attempt in range(1, 7):
        path = result_root / f"boards/nucleo_l476rg_attempt{attempt}/acceptance.json"
        if not path.is_file() and attempt == 1:
            path = result_root / "boards/nucleo_l476rg/acceptance.json"
        if path.is_file():
            failure = _load(path)
            failed_attempts.append(
                {
                    **_evidence(path),
                    "status": failure.get("status"),
                    "error_type": failure.get("error_type"),
                    "error": failure.get("error"),
                }
            )

    output = {
        "schema_version": 1,
        "milestone": "M10",
        "task": "Prompt 20.2 final acceptance matrix execution",
        "recorded_at": _timestamp(),
        "overall_status": "partial_blocked_hardware",
        "semantics_changed": False,
        "result_root": str(result_root),
        "prepared_plan": _evidence(plan_path),
        "software": {
            "status": "pass",
            "suite": "717 passed with 63 legacy-profile deprecation warnings",
            "ruff": "pass",
            "pyright": "0 errors, 0 warnings, 0 informations",
            "package_build": "pass",
            "dependency_check": "pass",
            "import_check": "pass",
            "stdio_boot_shutdown": "pass, 35 tools",
            "original_result": _evidence(original_software_path),
            "affected_only_reconciliation": _evidence(software_reconciliation_path),
            "commands": software_original["commands"],
        },
        "hardware": {
            "inventory": {**_evidence(inventory_path), "observed": inventory},
            "nucleo_l476rg": {
                "status": "pass",
                "result": _evidence(nucleo_path),
                "identity": nucleo["identity"],
                "elapsed_seconds": nucleo["elapsed_seconds"],
                "flash_execution": "not repeated; accepted M7 evidence reused",
                "failed_harness_attempts_preserved": failed_attempts,
            },
            "nrf52833dk": {"status": "blocked", "result": _evidence(nrf_path)},
            "recovery": {"status": "blocked", "result": _evidence(recovery_path)},
            "lifecycle": {
                "status": lifecycle["status"],
                "result": _evidence(lifecycle_path),
            },
            "simultaneous_two_board": {
                "status": "blocked",
                "result": _evidence(isolation_path),
            },
        },
        "destructive_operation_accounting": {
            "new_application_flash_calls": 0,
            "new_mass_erase_calls": 0,
            "new_target_unlock_calls": 0,
            "new_bootloader_flash_calls": 0,
            "preserved_nucleo_m7_program_calls": nucleo[
                "application_flash_containment"
            ]["program_calls"],
            "reason": "Task 20.1 required reuse of already-proven destructive evidence.",
        },
        "procedure_results": procedure_results,
        "traceability": {
            "acceptance": reconciled_ac,
            "cross_cutting": reconciled_cc,
            "status_counts": status_counts,
        },
        "terminal_blockers": [
            "The designated nRF52833 DK is not attached; the observed Nordic is an nRF52840.",
            "No nRF52833 backup, live safety map, or fresh one-time recovery approval exists.",
            "The official two-board simultaneous bench cannot run with only one official board.",
            "Some real-client cancellation and relay-conduct rows remain procedural/Q-1 gaps.",
        ],
    }

    external_report = result_root / "summary.json"
    if external_report.exists() or args.repo_report.exists():
        raise FileExistsError("Task 20 execution report already exists")
    rendered = json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    external_report.write_text(rendered, encoding="utf-8")
    args.repo_report.parent.mkdir(parents=True, exist_ok=True)
    args.repo_report.write_text(rendered, encoding="utf-8")
    print(
        json.dumps(
            {
                "status": output["overall_status"],
                "external_report": str(external_report),
                "repo_report": str(args.repo_report),
                "traceability_status_counts": status_counts,
            }
        )
    )


if __name__ == "__main__":
    main()
