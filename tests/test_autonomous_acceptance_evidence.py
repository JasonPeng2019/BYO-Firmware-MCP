from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

sys.path.insert(0, str(Path(__file__).parents[1]))

from scripts.validate_autonomous_acceptance import (
    EvidenceValidationError,
    validate_evidence,
)  # noqa: E402
from pyocd_debug_mcp.setup_flow.board_catalog import catalog_board  # noqa: E402
from pyocd_debug_mcp.setup_flow import board_catalog as board_catalog_module  # noqa: E402
from pyocd_debug_mcp.setup_flow.reviewed_evidence import (  # noqa: E402
    verify_persisted_reviewed_evidence,
)


def _record(workspace: Path, relative: str) -> dict[str, object]:
    path = workspace / relative
    return {
        "path": str(path),
        "relative_path": relative,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size_bytes": path.stat().st_size,
        "modified_at": datetime.fromtimestamp(
            path.stat().st_mtime, timezone.utc
        ).isoformat(),
    }


def _fixture(tmp_path: Path) -> Path:
    workspace = tmp_path / "isolated"
    acceptance = workspace / "acceptance"
    report_dir = workspace / ".firm" / "validation" / "validation-1"
    safety_dir = workspace / ".firm" / "safety" / "nf_board"
    board_dir = workspace / ".firm" / "boards"
    source_dir = workspace / "src"
    build_dir = workspace / "build"
    acceptance.mkdir(parents=True)
    report_dir.mkdir(parents=True)
    safety_dir.mkdir(parents=True)
    board_dir.mkdir(parents=True)
    source_dir.mkdir(parents=True)
    build_dir.mkdir(parents=True)
    report = report_dir / "report.json"
    report.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "report_type": "validation",
                "validation_id": "validation-1",
                "board_id": "nf_board",
                "terminal_status": "validation_passed_uart_not_configured",
                "code": "validation/passed",
            }
        ),
        encoding="utf-8",
    )
    profile = board_dir / "nf_board.yaml"
    profile.write_text(
        "board_id: nf_board\ndisplay_name: NF Board\nmcu_part_number: nRF52840-QIAA\n"
        "pyocd_target: nrf52840\nschema_version: 2\n",
        encoding="utf-8",
    )
    catalog = catalog_board("nrf52840dk")
    evidence_root = Path(board_catalog_module.__file__).resolve().parent
    support_document = json.loads(
        (evidence_root / str(catalog.device_support_evidence_resource)).read_text(
            encoding="utf-8"
        )
    )
    official_document = json.loads(
        (evidence_root / str(catalog.official_evidence_resource)).read_text(
            encoding="utf-8"
        )
    )
    pack_record = {
        "asset_sha256": catalog.device_support_evidence_sha256,
        "document": support_document,
        "runtime": {
            "pyocd_version": catalog.pyocd_version,
            "target_module_sha256": catalog.pyocd_target_module_sha256,
            "svd_bundle_sha256": catalog.pyocd_svd_bundle_sha256,
        },
    }
    authority_record: dict[str, object] = {
        "official_document": {
            "asset_sha256": catalog.official_evidence_sha256,
            "datasheet_sha256": catalog.datasheet_sha256[0],
            "document": official_document,
        }
    }
    reviewed = verify_persisted_reviewed_evidence(
        catalog, pack_record, authority_record
    )
    authority_record["reconciliation"] = reviewed.source_record()["reconciliation"]
    geometry = {
        "erase_origin": 0,
        "erase_size": 4096,
        "flash_start": 0,
        "flash_end": 1048576,
        "ram_start": 536870912,
        "ram_end": 537133056,
    }
    fingerprints = {"aggregate": "aggregate-1"}
    memory_map = safety_dir / "memory_map.yaml"
    memory_map.write_text(
        yaml.safe_dump(
            {
                "board_id": "nf_board",
                "schema_version": 1,
                "fingerprints": fingerprints,
                "regions": [
                    {
                        "name": item.name,
                        "kind": item.kind.value,
                        "start": item.address_range.start,
                        "end": item.address_range.end,
                        "executable": False,
                        "provenance": [
                            {
                                "authority": "reconciled",
                                "source_id": "+".join(item.source_ids),
                                "detail": ", ".join(item.reconciliations)
                                or "exact two-source agreement",
                            }
                        ],
                        "source_groups": ["evidence", "geometry"],
                    }
                    for item in reviewed.reconciliation.regions
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    source_manifest = safety_dir / "source_manifest.json"
    source_manifest.write_text(
        json.dumps(
            {
                "board_id": "nf_board",
                "schema_version": 1,
                "fingerprints": fingerprints,
                "sources": {
                    "schema": {
                        "evidence": {"memory_map": 1, "evidence": 2, "catalog": 2}
                    },
                    "part_target": {
                        "evidence": {
                            "board_type": "nrf52840dk",
                            "mcu_part_number": "nRF52840-QIAA",
                            "target": "nrf52840",
                        }
                    },
                    "pack": {"evidence": pack_record},
                    "evidence": {"evidence": authority_record},
                    "geometry": {"evidence": geometry},
                },
            }
        ),
        encoding="utf-8",
    )
    main_source = source_dir / "main.c"
    main_source.write_text("/* worker-thread LED console fixture */", encoding="utf-8")
    for name in ("firmware.elf", "firmware.hex", "zephyr.map"):
        (build_dir / name).write_text(f"fixture {name}", encoding="utf-8")
    source_epoch = datetime.fromisoformat("2026-07-17T00:01:00+00:00").timestamp()
    build_epoch = datetime.fromisoformat("2026-07-17T00:01:30+00:00").timestamp()
    os.utime(main_source, (source_epoch, source_epoch))
    for name in ("firmware.elf", "firmware.hex", "zephyr.map"):
        os.utime(build_dir / name, (build_epoch, build_epoch))
    prompt = acceptance / "supervisor_prompt.txt"
    prompt.write_text("Autonomous acceptance prompt", encoding="utf-8")
    run_id = "run-20260717T000000Z-12345678"
    started_at = "2026-07-17T00:00:00Z"
    steps = [
        {"text": "led on", "expected_text": "[BLINK_WORKER] ON [BLINK_CMD] ACK ON", "line_ending": "lf"},
        {"text": "led status", "expected_text": "[BLINK_STATUS] ON", "line_ending": "lf"},
        {"text": "led off", "expected_text": "[BLINK_WORKER] OFF [BLINK_CMD] ACK OFF", "line_ending": "lf"},
        {"text": "led status", "expected_text": "[BLINK_STATUS] OFF", "line_ending": "lf"},
        {"text": "led quiet", "expected_text": "[BLINK_QUIET] PASS 1200ms", "line_ending": "lf"},
    ]
    final_response = (
        "UART exchange matched; ready=matched; steps=5 "
        "[1:[BLINK_WORKER] ON [BLINK_CMD] ACK ON=matched; "
        "2:[BLINK_STATUS] ON=matched; 3:[BLINK_WORKER] OFF [BLINK_CMD] ACK OFF=matched; "
        "4:[BLINK_STATUS] OFF=matched; 5:[BLINK_QUIET] PASS 1200ms=matched]"
    )
    readiness = {
        "board_id": "nf_board",
        "status": "setup_ready",
        "configuration_ready": True,
        "live_session_ready": True,
        "ready_for_code": True,
        "ready_for_uart_work": True,
    }

    def response(text: str) -> dict[str, object]:
        return {
            "content": [{"type": "text", "text": text}],
            "isError": False,
            "meta": None,
            "structuredContent": None,
        }

    def request(tool: str, arguments: dict[str, object], start: str, end: str) -> dict[str, object]:
        text = {
            "board_setup-plan": "Accepted plan-1; immutable report validation-1",
            "board_setup": json.dumps(
                {
                    "status": "setup_completed",
                    "observed": {"validation_report": "validation-1"},
                }
            ),
            "board_validate": json.dumps(
                {
                    "status": "validation_passed_uart_not_configured",
                    "continuation_id": "validation-1",
                }
            ),
            "get_setup_status": json.dumps(readiness),
            "board_safety_refresh": json.dumps({"status": "safety_refresh_completed"}),
            "flash_application": "Flashed firmware.elf as flash_application within its mapped partition.",
            "serial_exchange": final_response,
            "disconnect": "Disconnected board 'nf_board'.",
        }[tool]
        return {
            "kind": "mcp_request_response",
            "phase": "acceptance",
            "tool": tool,
            "arguments": arguments,
            "request_started_at": start,
            "timestamp": end,
            "run_id": run_id,
            "server_started_at": started_at,
            "response": response(text),
        }

    serial_arguments = {
        "board_id": "nf_board",
        "steps": steps,
        "read_seconds": 3.0,
        "baudrate": 115200,
        "port": "COM11",
        "ready_text": "READY",
        "ready_seconds": 5.0,
        "ready_probe_text": "status",
        "ready_probe_line_ending": "lf",
        "ready_probe_delay_seconds": 1.5,
        "clear_input": False,
    }
    request_rows = [
        request("board_setup-plan", {"plan": "exact"}, "2026-07-17T00:00:02Z", "2026-07-17T00:00:03Z"),
        request("board_setup", {"board_id": "nf_board"}, "2026-07-17T00:00:04Z", "2026-07-17T00:00:05Z"),
        request("board_validate", {"board_id": "nf_board"}, "2026-07-17T00:00:06Z", "2026-07-17T00:00:07Z"),
        request("get_setup_status", {"board_id": "nf_board"}, "2026-07-17T00:00:08Z", "2026-07-17T00:00:10Z"),
        request(
            "board_safety_refresh",
            {
                "board_id": "nf_board",
                "application_elf": str(build_dir / "firmware.elf"),
                "application_hex": str(build_dir / "firmware.hex"),
                "application_map": str(build_dir / "zephyr.map"),
            },
            "2026-07-17T00:01:40Z",
            "2026-07-17T00:01:41Z",
        ),
        request("board_validate", {"board_id": "nf_board"}, "2026-07-17T00:01:42Z", "2026-07-17T00:01:43Z"),
        request("get_setup_status", {"board_id": "nf_board"}, "2026-07-17T00:01:44Z", "2026-07-17T00:01:45Z"),
        request(
            "flash_application",
            {
                "board_id": "nf_board",
                "artifact": str(build_dir / "firmware.elf"),
            },
            "2026-07-17T00:01:46Z",
            "2026-07-17T00:01:50Z",
        ),
        request("serial_exchange", serial_arguments, "2026-07-17T00:02:00Z", "2026-07-17T00:02:04Z"),
        request("disconnect", {"board_id": "nf_board"}, "2026-07-17T00:02:05Z", "2026-07-17T00:02:06Z"),
    ]
    timeline_rows = [
        {
            "kind": "mcp_initialize_and_handshake",
            "tool": "initialization_handshake",
            "phase": "acceptance",
            "timestamp": "2026-07-17T00:00:01Z",
            "run_id": run_id,
            "server_started_at": started_at,
            "handshake_response": response(f"run_id: {run_id}\nstarted_at: {started_at}"),
        },
        *request_rows,
    ]
    timeline = acceptance / "mcp_timeline.jsonl"
    timeline.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in timeline_rows),
        encoding="utf-8",
    )
    operation_links = [
        {
            "tool": row["tool"],
            "phase": row["phase"],
            "request_timestamp": row["request_started_at"],
            "response_timestamp": row["timestamp"],
            "run_id": run_id,
            "server_started_at": started_at,
            "canonical_arguments_sha256": hashlib.sha256(
                json.dumps(
                    row["arguments"],
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest(),
        }
        for row in request_rows
    ]
    evidence = {
        "schema_version": 3,
        "hardware_acceptance_status": "pass",
        "strict_evidence_status": "pass",
        "workspace": str(workspace),
        "server": {
            "server_run_id": run_id,
            "server_started_at": started_at,
            "commit": "abc123",
            "transports": [
                {
                    "phase": "acceptance",
                    "run_id": run_id,
                    "started_at": started_at,
                    "handshake_timestamp": "2026-07-17T00:00:01Z",
                }
            ],
        },
        "versions": {
            "mcp_protocol": "2025-11-25",
            "mcp_sdk": "1.28.1",
            "package": "0.1.0",
            "pyocd": "0.45.0",
            "zephyr": "4.3.99",
            "toolchain": "Zephyr SDK 0.17.0",
        },
        "hardware": {
            "board_id": "nf_board",
            "display_name": "NF Board",
            "board_type": "nrf52840dk",
            "mcu_part_number": "nRF52840-QIAA",
            "probe": {"id": "683377322"},
            "uart": {"port": "COM11", "baudrate": 115200},
        },
        "supervisor_facts": {
            "familiar_name": "NF Board",
            "board_type": "nrf52840dk",
            "mcu_part_number": "nRF52840-QIAA",
            "probe_id": "683377322",
            "serial_port": "COM11",
        },
        "supervisor_prompt": _record(workspace, "acceptance/supervisor_prompt.txt"),
        "timeline": _record(workspace, "acceptance/mcp_timeline.jsonl"),
        "pre_code_barrier": {
            "ordering_proof": True,
            "profile_committed_before_source": True,
            "source_absent": True,
            "timestamp": "2026-07-17T00:00:10Z",
            "source_first_modified_at": "2026-07-17T00:01:00Z",
            "run_id": run_id,
            "readiness": readiness,
        },
        "safety": {
            "aggregates_match": True,
            "reconciliation_status": "agreement",
            "hardware_provenance_only_reconciled": True,
            "hardware_provenance_authorities": ["reconciled"],
            "official_document_asset_sha256": reviewed.official_asset_sha256,
            "device_support_asset_sha256": reviewed.device_support_asset_sha256,
            "runtime_pins": {
                "pyocd_version": reviewed.pyocd_version,
                "target_module_sha256": reviewed.pyocd_target_module_sha256,
                "svd_bundle_sha256": reviewed.pyocd_svd_bundle_sha256,
            },
            "aggregate_fingerprint": "aggregate-1",
        },
        "uart": {
            "single_handle_five_steps": True,
            "worker_thread_on": True,
            "worker_thread_off": True,
            "command_ack_on": True,
            "command_ack_off": True,
            "status_on": True,
            "status_off": True,
            "quiet_after_off_1200ms": True,
            "final_exact_response": final_response,
            "final_run_id": run_id,
            "ready_probe_delay_seconds": 1.5,
        },
        "commands": {"hardware_shell_commands": []},
        "no_bypass_assertion": "MCP only",
        "safe_exit": "off and disconnected",
        "plans": {"board_setup-plan": ["plan-1"]},
        "operation_run_linkage": operation_links,
        "source_artifacts": [_record(workspace, "src/main.c")],
        "build_artifacts": [
            _record(workspace, "build/firmware.elf"),
            _record(workspace, "build/firmware.hex"),
            _record(workspace, "build/zephyr.map"),
        ],
        "firm_artifacts": {
            "profile": _record(workspace, ".firm/boards/nf_board.yaml"),
            "memory_map": _record(workspace, ".firm/safety/nf_board/memory_map.yaml"),
            "source_manifest": _record(
                workspace, ".firm/safety/nf_board/source_manifest.json"
            ),
            "reports": [
                {
                    **_record(workspace, ".firm/validation/validation-1/report.json"),
                    "report_id": "validation-1",
                    "report_type": "validation",
                    "terminal_status": "validation_passed_uart_not_configured",
                }
            ]
        },
    }
    path = acceptance / "evidence.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    return path


def _rewrite_timeline(
    evidence_path: Path,
    mutate: Callable[[list[dict[str, Any]]], None],
) -> dict[str, Any]:
    """Mutate structured timeline rows and keep the evidence artifact link exact."""

    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    timeline_path = evidence_path.parent / "mcp_timeline.jsonl"
    rows = [json.loads(line) for line in timeline_path.read_text(encoding="utf-8").splitlines()]
    mutate(rows)
    timeline_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    evidence["timeline"] = _record(evidence_path.parents[1], "acceptance/mcp_timeline.jsonl")
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    return evidence


def test_strict_acceptance_evidence_links_all_artifacts(tmp_path: Path) -> None:
    result = validate_evidence(_fixture(tmp_path))

    assert result["status"] == "pass"
    assert result["artifact_count"] == 10
    assert result["report_count"] == 1


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value["server"].update(server_run_id=None), "run_id"),
        (lambda value: value["safety"].update(hardware_provenance_authorities=["official_document"]), "provenance"),
        (lambda value: value["uart"].update(worker_thread_off=False), "worker_thread_off"),
    ],
)
def test_strict_acceptance_evidence_rejects_missing_proof(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    path = _fixture(tmp_path)
    value = json.loads(path.read_text(encoding="utf-8"))
    mutation(value)
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(EvidenceValidationError, match=message):
        validate_evidence(path)


def test_strict_acceptance_evidence_rejects_changed_artifact(tmp_path: Path) -> None:
    path = _fixture(tmp_path)
    (path.parent / "supervisor_prompt.txt").write_text("changed", encoding="utf-8")

    with pytest.raises(EvidenceValidationError, match="(size|digest) mismatch"):
        validate_evidence(path)


def test_self_asserted_plain_text_timeline_is_not_semantic_evidence(tmp_path: Path) -> None:
    path = _fixture(tmp_path)
    value = json.loads(path.read_text(encoding="utf-8"))
    timeline = path.parent / "mcp_timeline.jsonl"
    timeline.write_text(
        "initialization_handshake run-20260717T000000Z-12345678 plan-1 validation-1",
        encoding="utf-8",
    )
    value["timeline"] = _record(path.parents[1], "acceptance/mcp_timeline.jsonl")
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(EvidenceValidationError, match="not JSON"):
        validate_evidence(path)


def test_operation_link_cannot_be_self_asserted_with_wrong_arguments(tmp_path: Path) -> None:
    path = _fixture(tmp_path)
    value = json.loads(path.read_text(encoding="utf-8"))
    value["operation_run_linkage"][1]["canonical_arguments_sha256"] = "0" * 64
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(EvidenceValidationError, match="no exact operation/run link"):
        validate_evidence(path)


def test_report_status_must_match_immutable_report_bytes(tmp_path: Path) -> None:
    path = _fixture(tmp_path)
    value = json.loads(path.read_text(encoding="utf-8"))
    value["firm_artifacts"]["reports"][0]["terminal_status"] = "validation_failed"
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(EvidenceValidationError, match="report status mismatch"):
        validate_evidence(path)


def test_reconciled_erase_geometry_must_match_persisted_authority_subset(
    tmp_path: Path,
) -> None:
    path = _fixture(tmp_path)
    value = json.loads(path.read_text(encoding="utf-8"))
    manifest_path = path.parents[1] / ".firm/safety/nf_board/source_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sources"]["geometry"]["evidence"]["erase_size"] = 8192
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    value["firm_artifacts"]["source_manifest"] = _record(
        path.parents[1], ".firm/safety/nf_board/source_manifest.json"
    )
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(EvidenceValidationError, match="persisted erase geometry"):
        validate_evidence(path)


def test_acceptance_rejects_missing_required_safety_operation(tmp_path: Path) -> None:
    path = _fixture(tmp_path)

    def remove_refresh(rows: list[dict[str, object]]) -> None:
        rows[:] = [row for row in rows if row.get("tool") != "board_safety_refresh"]

    evidence = _rewrite_timeline(path, remove_refresh)
    evidence["operation_run_linkage"] = [
        link
        for link in evidence["operation_run_linkage"]
        if link["tool"] != "board_safety_refresh"
    ]
    path.write_text(json.dumps(evidence), encoding="utf-8")

    with pytest.raises(EvidenceValidationError, match="missing required safety setup/refresh"):
        validate_evidence(path)


def test_acceptance_rejects_product_refusal_even_when_transport_succeeds(
    tmp_path: Path,
) -> None:
    path = _fixture(tmp_path)

    def refuse_setup(rows: list[dict[str, object]]) -> None:
        setup = next(row for row in rows if row.get("tool") == "board_setup")
        setup["response"] = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({"status": "setup_blocked", "code": "setup/no-probe"}),
                }
            ],
            "isError": False,
            "meta": None,
            "structuredContent": None,
        }

    _rewrite_timeline(path, refuse_setup)

    with pytest.raises(EvidenceValidationError, match="board_setup did not complete"):
        validate_evidence(path)


def test_acceptance_rejects_nonmonotonic_timeline(tmp_path: Path) -> None:
    path = _fixture(tmp_path)

    def backdate_flash(rows: list[dict[str, object]]) -> None:
        flash = next(row for row in rows if row.get("tool") == "flash_application")
        flash["request_started_at"] = "2026-07-17T00:00:11Z"
        flash["timestamp"] = "2026-07-17T00:00:12Z"

    _rewrite_timeline(path, backdate_flash)

    with pytest.raises(EvidenceValidationError, match="not monotonic"):
        validate_evidence(path)


def test_acceptance_rejects_duplicate_artifact_alias(tmp_path: Path) -> None:
    path = _fixture(tmp_path)
    evidence = json.loads(path.read_text(encoding="utf-8"))
    evidence["build_artifacts"].append(dict(evidence["source_artifacts"][0]))
    path.write_text(json.dumps(evidence), encoding="utf-8")

    with pytest.raises(EvidenceValidationError, match="duplicate artifact record"):
        validate_evidence(path)


def test_acceptance_rejects_build_timestamp_not_bound_to_file(tmp_path: Path) -> None:
    path = _fixture(tmp_path)
    evidence = json.loads(path.read_text(encoding="utf-8"))
    evidence["build_artifacts"][0]["modified_at"] = "2026-07-17T00:01:31Z"
    path.write_text(json.dumps(evidence), encoding="utf-8")

    with pytest.raises(EvidenceValidationError, match="does not match filesystem"):
        validate_evidence(path)
