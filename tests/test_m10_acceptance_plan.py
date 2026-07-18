from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "docs/evidence/m10-task20-acceptance-plan-2026-07-17.json"
BOUNDARY_PATH = ROOT / "docs/evidence/m10-software-boundary-2026-07-17.json"
SPEC_PATH = ROOT / "archive_docs/Design_Proto_Spec.md"
PROMPTS_PATH = ROOT / "archive_docs/codex_prompts.md"
PROMPT_AUDIT_PATH = ROOT / "docs/evidence/prompt-audit-through-20.1-2026-07-17.md"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _criterion_ids(prefix: str) -> set[str]:
    pattern = rf"- \*\*({prefix}-\d+(?:\.\d+)?)\*\*"
    return set(re.findall(pattern, SPEC_PATH.read_text(encoding="utf-8")))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_task20_traceability_is_exact_and_assertions_were_inspected() -> None:
    plan = _load(PLAN_PATH)
    traceability = plan["traceability"]
    acceptance = traceability["acceptance"]
    cross_cutting = traceability["cross_cutting"]

    assert {row["id"] for row in acceptance} == _criterion_ids("AC")
    assert {row["id"] for row in cross_cutting} == _criterion_ids("CC")
    assert len(acceptance) == 122
    assert len(cross_cutting) == 22

    for row in [*acceptance, *cross_cutting]:
        assert row["status"] == "prepared_not_executed"
        assert row["assertion_inspection"]
        assert row["automated_tests"]
        for proof in row["automated_tests"]:
            relative, function_name = proof["node_id"].split("::", 1)
            source_path = ROOT / relative
            if not source_path.is_file():
                assert relative == "tests/test_safety_fingerprints.py"
                assert proof["line"] > 0
                continue
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            functions = [
                node
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == function_name
            ]
            # This is immutable historical M10 evidence. Safety Layer v2 may
            # replace or move the cited test while preserving the recorded
            # inspected assertion; do not rewrite the old evidence bundle.
            assert proof["line"] > 0
            if functions:
                assert functions[0].lineno > 0
            assert proof["direct_asserts"] + proof["raises_contexts"] > 0
            assert len(proof["inspected_assertions"]) == (
                proof["direct_asserts"] + proof["raises_contexts"]
            )
            assert all(item["expression"] for item in proof["inspected_assertions"])


def test_prompt_audit_names_every_prompt_through_20_1_exactly_once() -> None:
    prompt_ids = re.findall(
        r"^### Prompt (\d+\.\d+)",
        PROMPTS_PATH.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    through_20_1 = prompt_ids[: prompt_ids.index("20.1") + 1]
    audit = PROMPT_AUDIT_PATH.read_text(encoding="utf-8")
    audited = re.findall(r"^\| (\d+\.\d+) \|", audit, flags=re.MULTILINE)

    assert audited == through_20_1
    assert "20.2" not in audited
    assert "20.3" not in audited


def test_every_remaining_task20_procedure_is_routed_to_a_criterion() -> None:
    plan = _load(PLAN_PATH)
    boundary = _load(BOUNDARY_PATH)
    rows = [
        *plan["traceability"]["acceptance"],
        *plan["traceability"]["cross_cutting"],
    ]
    routed = {
        procedure
        for row in rows
        for procedure in row["task20_procedures"]
    }
    expected = {row["id"] for row in boundary["task20_remaining"]}

    assert routed == expected
    assert plan["remaining_procedures"] == boundary["task20_remaining"]


def test_verified_fixtures_and_disposable_artifacts_match_recorded_hashes() -> None:
    plan = _load(PLAN_PATH)
    fixtures = plan["fixture_verification"]

    for board_id, profile in fixtures["profiles"].items():
        path = Path(profile["path"])
        assert path.is_file()
        assert re.fullmatch(r"[0-9a-f]{64}", profile["sha256"])
        assert profile["verified_fields"]["board_id"] == board_id
        assert profile["verified_fields"]["serial_baudrate"] == 115200
    assert fixtures["profiles"]["nucleo_l476rg"]["verified_fields"]["pyocd_target"] == (
        "stm32l476rgtx"
    )
    assert fixtures["profiles"]["nrf52833dk"]["verified_fields"][
        "silicon_id_expected"
    ] == 0x00052833

    for artifact in fixtures["tracked_reference_artifacts"].values():
        assert artifact["application_partition_contained"] is True
        assert artifact["flash_partition"]
        for key in ("elf", "hex"):
            path = Path(artifact[key]["path"])
            assert path.is_file()
            assert _sha256(path) == artifact[key]["sha256"]

    disposable = fixtures["nucleo_disposable_artifacts"]
    assert disposable["ready"] is True
    assert disposable["v1"]["flash_partition"] == disposable["v2"]["flash_partition"]
    for version in ("v1", "v2"):
        for key in ("elf", "hex"):
            item = disposable[version][key]
            assert re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
            if Path(item["path"]).is_file():
                assert _sha256(Path(item["path"])) == item["sha256"]
    backup = disposable["backup"]
    assert re.fullmatch(r"[0-9a-f]{64}", backup["sha256"])
    if Path(backup["path"]).is_file():
        assert _sha256(Path(backup["path"])) == backup["sha256"]

    pack = fixtures["pinned_pack"]
    assert pack["sha256"] == "5672383c07fbdcee0e471a33f4f8beb2e1f3200bc999244dcd6858e0e8e8203f"
    assert _sha256(Path(pack["path"])) == pack["sha256"]

    nrf_disposable = fixtures["nrf52833dk_disposable_artifacts"]
    assert nrf_disposable["ready"] is False
    assert "backup" in nrf_disposable["blocker"]
    assert nrf_disposable["reference_application"]["application_partition_contained"] is True
    assert fixtures["target_resolution"]["nucleo_l476rg"][
        "available_from_verified_pack"
    ] is True
    assert fixtures["target_resolution"]["nrf52833dk"]["available"] is True

    for evidence in plan["preserved_destructive_evidence"].values():
        path = Path(evidence["path"])
        assert path.is_file()
        assert _sha256(path) == evidence["sha256"]


def test_current_identity_readiness_fails_closed_without_nrf52833() -> None:
    plan = _load(PLAN_PATH)
    inventory = plan["inventory"]

    assert inventory["nucleo_l476rg"] == {
        "ready": True,
        "required_probe_id": "066FFF514988525067233337",
        "required_serial_id": "COM12",
    }
    assert inventory["nrf52833dk"]["ready"] is False
    assert inventory["nrf52833dk"]["required_ficr_info_part"] == "0x00052833"
    assert "0x00052840" in inventory["nrf52833dk"]["wrong_board_must_not_be_substituted"]
    assert plan["recovery_readiness"]["ready"] is False
    assert plan["recovery_readiness"]["fresh_human_permission"] == (
        "required_at_execution_never_pregranted"
    )
    recovery_checks = plan["recovery_readiness"]["prerequisite_checks"]
    assert recovery_checks["designated_nrf52833_present"] is False
    assert recovery_checks["live_ficr_info_part_verified"] is False
    assert recovery_checks["verified_firmware_backup_present"] is False
    assert recovery_checks["profile_recovery_mode"] == "backend_mass_erase"
    assert recovery_checks["current_safety_map_present"] is False


def test_bounded_run_order_requires_identity_and_avoids_repeat_destruction() -> None:
    plan = _load(PLAN_PATH)
    phases = plan["run_order"]
    result_root = Path(plan["external_result_root"]["path"])

    assert result_root.is_absolute()
    assert ROOT not in result_root.parents
    assert plan["external_result_root"]["empty_at_preparation"] is True
    assert [phase["order"] for phase in phases] == list(range(1, 7))
    assert [phase["id"] for phase in phases] == [
        "software_once",
        "nucleo_l476rg_setup_safety_validation_actions",
        "nrf52833dk_setup_safety_validation_actions",
        "single_designated_recovery_proof",
        "lifecycle_cancellation_without_repeat_flash",
        "simultaneous_two_board_isolation",
    ]
    assert sum("mass erase" in str(phase["destructive"]) for phase in phases) == 1
    assert sum("application-partition flash" in str(phase["destructive"]) for phase in phases) == 1

    for phase in phases[1:5]:
        identity = phase["required_identity"]
        assert identity["board_id"]
        assert identity["probe_id"]
        assert phase["result"].endswith(".json")
        assert result_root in Path(phase["result"]).parents
    assert all(identity["probe_id"] for identity in phases[5]["required_identities"])
    assert phases[5]["result"].endswith(".json")
    assert result_root in Path(phases[5]["result"]).parents

    lifecycle_steps = " ".join(phases[4]["sequence"])
    assert "do not flash again" in lifecycle_steps
    assert "reuse preserved M9" in lifecycle_steps
    invariants = " ".join(plan["safety_invariants"])
    assert "No bootloader flash" in invariants
    assert "Mass erase occurs at most once" in invariants
    assert "Nucleo M7 application flash is not repeated" in invariants
    assert phases[1]["destructive"] is False
    assert "do not repeat an application flash" in " ".join(phases[1]["mcp_sequence"])

    command_contract = plan["hardware_command_contract"]
    assert command_contract["required_for_every_invocation"] == [
        "board_id",
        "stable_probe_id",
        "absolute_external_artifact_root",
        "machine_readable_result_path",
    ]
    assert command_contract["connect_requires"] == ["board_id"]
    assert command_contract["nrf52833_requires_live_check"] == (
        "FICR.INFO.PART == 0x00052833"
    )


def test_tool_and_client_versions_are_frozen_for_the_acceptance_run() -> None:
    versions = _load(PLAN_PATH)["versions"]

    for package in ("python", "pyocd", "mcp", "pyserial", "pyelftools", "package"):
        assert versions[package]
    for command in ("uv", "git", "codex", "claude"):
        assert versions[command]["available"] is True
        assert versions[command]["version"]
    assert versions["vscode"]["available"] is True
    assert versions["vscode"]["version"] == "1.129.0"
