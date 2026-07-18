from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "docs/evidence/m10-final-validation-2026-07-17.json"
MARKDOWN_PATH = ROOT / "docs/evidence/m10-final-validation-2026-07-17.md"
EXTERNAL_ROOT = Path(
    r"C:\Users\Jason\Documents\Jason\FirmCLI\M10-Final-Acceptance\2026-07-17_run1"
)


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _criterion_ids(prefix: str) -> set[str]:
    pattern = rf"- \*\*({prefix}-\d+(?:\.\d+)?)\*\*"
    return set(
        re.findall(
            pattern,
            (ROOT / "archive_docs/Design_Proto_Spec.md").read_text(encoding="utf-8"),
        )
    )


def test_final_report_reconciles_every_criterion_without_hiding_failures() -> None:
    report = _load(REPORT_PATH)
    acceptance = report["traceability"]["acceptance"]
    cross_cutting = report["traceability"]["cross_cutting"]

    assert {row["id"] for row in acceptance} == _criterion_ids("AC")
    assert {row["id"] for row in cross_cutting} == _criterion_ids("CC")
    assert len(acceptance) == 122
    assert len(cross_cutting) == 22
    assert report["traceability"]["status_counts"] == {
        "pass": 63,
        "blocked": 72,
        "partial": 6,
        "blocked_manual_procedure": 3,
    }
    assert report["traceability"]["failed_count"] == 0
    for row in [*acceptance, *cross_cutting]:
        assert row["status"] in {
            "pass",
            "partial",
            "blocked",
            "blocked_manual_procedure",
        }
        assert row["automated_test_nodes"]
        assert row["inspected_assertion_count"] > 0


def test_completion_state_distinguishes_software_from_external_blockers() -> None:
    report = _load(REPORT_PATH)
    completion = report["repository_completion"]

    assert report["overall_status"] == "blocked_external_hardware_and_manual_acceptance"
    assert completion["software_implementation_complete"] is True
    assert completion["software_quality_green"] is True
    assert completion["stdio_server_boots"] is True
    assert completion["all_in_scope_criteria_pass"] is False
    assert completion["every_hardware_criterion_has_real_evidence"] is False
    assert completion["repository_complete_under_task_20_definition"] is False
    assert completion["release_or_publication_complete"] is False
    assert not (ROOT / "LICENSE").exists()
    assert any("licensing" in item for item in report["external_blockers"])


def test_performance_client_and_hardware_evidence_are_exact() -> None:
    report = _load(REPORT_PATH)
    metrics = report["performance"]["metrics"]

    assert metrics["gate_and_freshness"]["max_seconds"] <= 0.250
    assert metrics["enumerate_eight_devices"]["max_seconds"] <= 10.0
    assert metrics["null_plan_and_handshake"]["max_seconds"] <= 2.0
    assert report["client_cancellation"]["verified_client"]["version"] == "1.28.1"
    assert report["client_cancellation"]["serial_cancellation"]["result"] == "passed"
    assert report["client_cancellation"]["flash_cancellation"][
        "completion_preceded_release"
    ] is True
    assert report["client_cancellation"]["unverified"]

    nucleo = report["hardware_identities"]["nucleo_l476rg"]
    nordic = report["hardware_identities"]["observed_incompatible_nordic"]
    assert nucleo["probe_id"] == "066FFF514988525067233337"
    assert nucleo["serial_id"] == "COM12"
    assert nordic["probe_id"] == "683377322"
    assert nordic["ficr_info_part"] == "0x00052840"
    assert report["hardware_identities"]["required_nrf52833"]["status"] == "not_present"


def test_no_unrelated_destructive_operation_was_repeated() -> None:
    accounting = _load(REPORT_PATH)["destructive_operation_accounting"]

    assert accounting["new_application_flash_calls"] == 0
    assert accounting["new_bootloader_flash_calls"] == 0
    assert accounting["new_mass_erase_calls"] == 0
    assert accounting["new_target_unlock_calls"] == 0
    assert accounting["preserved_nucleo_m7_program_calls"] == 2


def test_every_open_question_and_risk_has_an_explicit_disposition() -> None:
    report = _load(REPORT_PATH)
    questions = report["open_questions"]
    risks = report["risks"]

    assert [item["id"] for item in questions] == [f"Q-{index}" for index in range(1, 11)]
    assert [item["id"] for item in risks] == [f"R-{index}" for index in range(1, 12)]
    assert all(item["status"] and item["treatment"] and item["evidence"] for item in questions)
    assert all(item["status"] and item["treatment"] and item["evidence"] for item in risks)
    assert questions[0]["status"] == "open_external_client_matrix"
    assert risks[9]["status"] == "open_blocker"
    assert risks[10]["status"] == "open_external_blocker"


def test_repo_and_external_final_reports_are_identical_and_linked() -> None:
    external_json = EXTERNAL_ROOT / "final-validation.json"
    external_markdown = EXTERNAL_ROOT / "final-validation.md"

    assert _sha256(REPORT_PATH) == _sha256(external_json)
    assert MARKDOWN_PATH.read_text(encoding="utf-8") == external_markdown.read_text(
        encoding="utf-8"
    )
    markdown = MARKDOWN_PATH.read_text(encoding="utf-8")
    assert "repository is not complete" in markdown
    assert "0x00052840" in markdown
    assert "Fail: 0" in markdown
