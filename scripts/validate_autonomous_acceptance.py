"""Strictly validate autonomous hardware-acceptance evidence and its file links."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from pyocd_debug_mcp.setup_flow.board_catalog import BoardCatalogError, catalog_board
from pyocd_debug_mcp.safety.fingerprints import canonical_bytes
from pyocd_debug_mcp.setup_flow.reviewed_evidence import load_pinned_reviewed_evidence


class EvidenceValidationError(ValueError):
    """Raised when acceptance evidence is incomplete or internally inconsistent."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise EvidenceValidationError(message)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_records(value: object) -> Iterator[Mapping[str, object]]:
    if isinstance(value, Mapping):
        if {"path", "relative_path", "sha256", "size_bytes"} <= set(value):
            yield value
        for child in value.values():
            yield from _artifact_records(child)
    elif isinstance(value, list):
        for child in value:
            yield from _artifact_records(child)


def _validate_artifact(workspace: Path, record: Mapping[str, object]) -> Path:
    relative = record["relative_path"]
    if not isinstance(relative, str) or not relative:
        raise EvidenceValidationError("artifact relative_path must be text")
    relative_path = Path(relative)
    _require(not relative_path.is_absolute(), f"artifact relative_path is absolute: {relative}")
    _require(".." not in relative_path.parts, f"artifact relative_path traverses upward: {relative}")
    unresolved_expected = workspace / relative_path
    cursor = workspace
    for part in relative_path.parts:
        cursor /= part
        _require(not cursor.is_symlink(), f"artifact path contains a symlink: {relative}")
    expected = unresolved_expected.resolve(strict=True)
    _require(expected.is_relative_to(workspace), f"artifact escapes workspace: {relative}")
    declared_value = record["path"]
    if not isinstance(declared_value, str):
        raise EvidenceValidationError(f"artifact path must be text: {relative}")
    unresolved_declared = Path(declared_value)
    _require(not unresolved_declared.is_symlink(), f"artifact must not be a symlink: {relative}")
    declared = unresolved_declared.resolve(strict=True)
    _require(declared == expected, f"artifact path/relative_path mismatch: {relative}")
    _require(declared.is_file(), f"artifact is not a file: {relative}")
    _require(declared.stat().st_size == record["size_bytes"], f"artifact size mismatch: {relative}")
    digest = record["sha256"]
    if not isinstance(digest, str) or len(digest) != 64:
        raise EvidenceValidationError(f"invalid SHA-256: {relative}")
    _require(_sha256(declared) == digest.casefold(), f"artifact digest mismatch: {relative}")
    return declared


def _timeline_text(evidence: Mapping[str, Any]) -> str:
    record = evidence["timeline"]
    _require(isinstance(record, Mapping), "timeline artifact record is missing")
    path_value = record["path"]
    if not isinstance(path_value, str):
        raise EvidenceValidationError("timeline path must be text")
    return Path(path_value).read_text(encoding="utf-8")


def _time(value: object, location: str) -> datetime:
    if not isinstance(value, str):
        raise EvidenceValidationError(f"{location} must be an absolute timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceValidationError(f"{location} is not an ISO timestamp") from exc
    _require(parsed.tzinfo is not None, f"{location} has no timezone")
    return parsed


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _response_text(response: object) -> str:
    if not isinstance(response, Mapping):
        raise EvidenceValidationError("MCP response must be an object")
    content = response.get("content")
    if not isinstance(content, list):
        raise EvidenceValidationError("MCP response content must be a list")
    texts = [
        item.get("text")
        for item in content
        if isinstance(item, Mapping) and item.get("type") == "text"
    ]
    if not texts or any(not isinstance(item, str) for item in texts):
        raise EvidenceValidationError("MCP response has no exact text content")
    return "\n".join(texts)  # type: ignore[arg-type]


def _parse_timeline(evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    previous_time: datetime | None = None
    for number, line in enumerate(_timeline_text(evidence).splitlines(), start=1):
        _require(bool(line.strip()), f"timeline line {number} is blank")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvidenceValidationError(f"timeline line {number} is not JSON") from exc
        _require(isinstance(row, dict), f"timeline line {number} is not an object")
        timestamp = _time(row.get("timestamp"), f"timeline line {number}.timestamp")
        _require(
            previous_time is None or timestamp >= previous_time,
            f"timeline line {number} is not monotonic",
        )
        previous_time = timestamp
        if row.get("kind") == "mcp_request_response":
            request_started = _time(
                row.get("request_started_at"),
                f"timeline line {number}.request_started_at",
            )
            _require(request_started <= timestamp, f"timeline line {number} ends before it starts")
        rows.append(row)
    _require(bool(rows), "timeline is empty")
    return rows


def _validate_semantic_links(evidence: dict[str, Any], workspace: Path) -> tuple[int, int]:
    hardware = evidence.get("hardware")
    facts = evidence.get("supervisor_facts")
    _require(isinstance(hardware, Mapping), "hardware identity evidence is missing")
    _require(isinstance(facts, Mapping), "supervisor facts are missing")
    assert isinstance(hardware, Mapping) and isinstance(facts, Mapping)
    for field in ("board_type", "display_name", "mcu_part_number"):
        expected_field = "familiar_name" if field == "display_name" else field
        _require(
            hardware.get(field) == facts.get(expected_field),
            f"hardware identity disagrees with supervisor fact: {field}",
        )
    _require(bool(hardware.get("board_id")), "logical board identity is missing")
    probe = hardware.get("probe")
    uart_identity = hardware.get("uart")
    _require(isinstance(probe, Mapping) and bool(probe.get("id")), "probe identity is missing")
    assert isinstance(probe, Mapping)
    _require(
        str(probe.get("id")) == str(facts.get("probe_id")),
        "probe identity disagrees with supervisor facts",
    )
    _require(
        isinstance(uart_identity, Mapping)
        and uart_identity.get("port") == facts.get("serial_port")
        and isinstance(uart_identity.get("baudrate"), int),
        "UART identity disagrees with supervisor facts",
    )
    assert isinstance(uart_identity, Mapping)

    rows = _parse_timeline(evidence)
    handshakes = [row for row in rows if row.get("kind") == "mcp_initialize_and_handshake"]
    requests = [row for row in rows if row.get("kind") == "mcp_request_response"]
    _require(bool(handshakes), "timeline has no public initialization handshake")
    _require(bool(requests), "timeline has no MCP request/response records")
    public_runs: dict[str, str] = {}
    for row in handshakes:
        run_id = row.get("run_id")
        started = row.get("server_started_at")
        _require(isinstance(run_id, str) and run_id.startswith("run-"), "handshake run_id is invalid")
        assert isinstance(run_id, str)
        _time(started, "handshake server_started_at")
        response = row.get("handshake_response")
        text = _response_text(response)
        _require(run_id in text and str(started) in text, "handshake text does not disclose its run")
        public_runs[run_id] = str(started)

    server = evidence["server"]
    assert isinstance(server, Mapping)
    transports = server.get("transports")
    _require(isinstance(transports, list) and bool(transports), "server transports are missing")
    assert isinstance(transports, list)
    for transport in transports:
        _require(isinstance(transport, Mapping), "server transport record is malformed")
        assert isinstance(transport, Mapping)
        run_id = transport.get("run_id")
        _require(public_runs.get(str(run_id)) == transport.get("started_at"), "transport is not linked to a public handshake")
        _time(transport.get("handshake_timestamp"), "transport handshake_timestamp")

    links = evidence.get("operation_run_linkage")
    _require(isinstance(links, list), "operation/run linkage is missing")
    assert isinstance(links, list)
    _require(len(links) == len(requests), "operation/run linkage count disagrees with timeline")
    link_keys: set[tuple[object, ...]] = set()
    for link in links:
        _require(isinstance(link, Mapping), "operation/run link is malformed")
        assert isinstance(link, Mapping)
        key = (
            link.get("tool"),
            link.get("phase"),
            link.get("request_timestamp"),
            link.get("response_timestamp"),
            link.get("run_id"),
            link.get("canonical_arguments_sha256"),
        )
        _require(key not in link_keys, "duplicate operation/run link")
        link_keys.add(key)
    for row in requests:
        run_id = row.get("run_id")
        _require(public_runs.get(str(run_id)) == row.get("server_started_at"), "request uses an undisclosed server run")
        handshake_row = next(item for item in handshakes if item.get("run_id") == run_id)
        _require(
            _time(handshake_row.get("timestamp"), "handshake timestamp")
            <= _time(row.get("request_started_at"), "request start timestamp"),
            "request starts before its run handshake",
        )
        arguments = row.get("arguments")
        _require(isinstance(arguments, dict), "MCP request arguments are not an object")
        assert isinstance(arguments, dict)
        response = row.get("response")
        _response_text(response)
        assert isinstance(response, Mapping)
        _require(response.get("isError") is False, f"recorded MCP request failed: {row.get('tool')}")
        key = (
            row.get("tool"),
            row.get("phase"),
            row.get("request_started_at"),
            row.get("timestamp"),
            run_id,
            _canonical_sha256(arguments),
        )
        _require(key in link_keys, f"MCP request has no exact operation/run link: {row.get('tool')}")

    terminations = {
        str(row.get("run_id")): _time(row.get("timestamp"), "transport termination timestamp")
        for row in rows
        if row.get("kind") == "transport_terminated"
    }
    for row in requests:
        terminated_at = terminations.get(str(row.get("run_id")))
        _require(
            terminated_at is None
            or _time(row.get("timestamp"), "request response timestamp") <= terminated_at,
            "request occurs after transport termination",
        )

    ordered_tools = [str(row.get("tool")) for row in requests]

    def ordered_index(tool: str, after: int = -1) -> int:
        try:
            return ordered_tools.index(tool, after + 1)
        except ValueError as exc:
            raise EvidenceValidationError(
                f"acceptance timeline is missing required operation: {tool}"
            ) from exc

    setup_index = ordered_index("board_setup")
    setup_validate_index = ordered_index("board_validate", setup_index)
    barrier_index = ordered_index("get_setup_status", setup_validate_index)

    def response_json(index: int, label: str) -> Mapping[str, Any]:
        try:
            payload = json.loads(_response_text(requests[index].get("response")))
        except json.JSONDecodeError as exc:
            raise EvidenceValidationError(f"{label} response is not JSON") from exc
        _require(isinstance(payload, Mapping), f"{label} response is not an object")
        assert isinstance(payload, Mapping)
        return payload

    setup_payload = response_json(setup_index, "board_setup")
    setup_validation_payload = response_json(setup_validate_index, "setup validation")
    _require(setup_payload.get("status") == "setup_completed", "board_setup did not complete")
    _require(
        str(setup_validation_payload.get("status", "")).startswith("validation_passed"),
        "setup validation did not pass",
    )

    pre_code = evidence["pre_code_barrier"]
    assert isinstance(pre_code, Mapping)
    source_time = _time(pre_code.get("source_first_modified_at"), "source_first_modified_at")
    barrier_time = _time(pre_code.get("timestamp"), "pre-code barrier timestamp")
    _require(pre_code.get("source_absent") is True, "source was present at the pre-code barrier")
    _require(barrier_time < source_time, "source was created before setup readiness")
    _require(pre_code.get("run_id") in public_runs, "pre-code barrier uses an undisclosed run")
    barrier_row = requests[barrier_index]
    _require(
        barrier_row.get("run_id") == pre_code.get("run_id")
        and barrier_row.get("arguments") == {"board_id": hardware.get("board_id")},
        "pre-code barrier is not the same-run board status request",
    )
    observed_readiness = response_json(barrier_index, "pre-code status")
    _require(
        observed_readiness == pre_code.get("readiness"),
        "pre-code readiness is not the exact get_setup_status response",
    )
    _require(
        _time(barrier_row.get("timestamp"), "pre-code status response timestamp") == barrier_time,
        "pre-code barrier timestamp is not the status response timestamp",
    )

    firm = evidence.get("firm_artifacts")
    _require(isinstance(firm, Mapping), "FirmStore artifact links are missing")
    assert isinstance(firm, Mapping)
    profile_record = firm.get("profile")
    map_record = firm.get("memory_map")
    _require(isinstance(profile_record, Mapping), "profile artifact is missing")
    _require(isinstance(map_record, Mapping), "memory map artifact is missing")
    _require("source_manifest" not in firm, "legacy source manifest must not be present")
    assert isinstance(profile_record, Mapping)
    assert isinstance(map_record, Mapping)
    profile = yaml.safe_load(Path(str(profile_record["path"])).read_text(encoding="utf-8"))
    memory_map = yaml.safe_load(Path(str(map_record["path"])).read_text(encoding="utf-8"))
    _require(isinstance(profile, Mapping) and profile.get("schema_version") == 2, "profile is not schema v2")
    assert isinstance(profile, Mapping)
    for field in ("board_id", "display_name", "mcu_part_number"):
        _require(profile.get(field) == hardware.get(field), f"profile/hardware mismatch: {field}")
    expected_map_fields = {
        "schema_version", "board_id", "identity", "source_digests",
        "geometry", "partitions", "regions",
    }
    _require(
        isinstance(memory_map, Mapping)
        and set(memory_map) == expected_map_fields
        and memory_map.get("schema_version") == 2
        and memory_map.get("board_id") == hardware.get("board_id"),
        "memory map is not the exact single-file schema v2 authority",
    )
    assert isinstance(memory_map, Mapping)
    identity = memory_map.get("identity")
    source_digests = memory_map.get("source_digests")
    geometry = memory_map.get("geometry")
    partitions = memory_map.get("partitions")
    _require(
        isinstance(identity, Mapping)
        and identity.get("mcu_part_number") == hardware.get("mcu_part_number")
        and isinstance(identity.get("board_type"), str)
        and isinstance(identity.get("target"), str),
        "memory map identity does not match the hardware evidence",
    )
    _require(
        isinstance(source_digests, Mapping)
        and set(source_digests) == {
            "semantic_profile", "device_support", "official_evidence", "generator_schema"
        }
        and all(isinstance(value, str) and len(value) == 64 for value in source_digests.values()),
        "memory map semantic source digests are incomplete",
    )
    assert isinstance(identity, Mapping)
    try:
        catalog = catalog_board(str(identity["board_type"]))
        reviewed = load_pinned_reviewed_evidence(catalog, catalog.datasheet_sha256[0])
    except (BoardCatalogError, ValueError, OSError) as exc:
        raise EvidenceValidationError(
            f"persisted safety authority cannot be independently reproduced: {exc}"
        ) from exc
    _require(
        identity == {
            "board_type": catalog.board_type,
            "mcu_part_number": catalog.package_part_number,
            "target": catalog.pyocd_target,
        },
        "memory map identity does not equal the reviewed catalog identity",
    )
    _require(
        isinstance(geometry, Mapping)
        and geometry.get("flash_start") == catalog.flash_start
        and geometry.get("flash_end") == catalog.flash_end
        and geometry.get("ram_start") == catalog.ram_start
        and geometry.get("ram_end") == catalog.ram_end
        and geometry.get("erase_origin") == catalog.flash_start
        and geometry.get("erase_size") == catalog.erase_size,
        "persisted erase geometry does not equal the reviewed catalog geometry",
    )
    expected_application = (
        {"start": catalog.application_start, "end": catalog.application_end}
        if catalog.application_partition_authoritative
        else None
    )
    expected_bootloader = (
        {"start": catalog.bootloader_start, "end": catalog.bootloader_end}
        if catalog.bootloader_partition_authoritative
        else None
    )
    _require(
        isinstance(partitions, Mapping)
        and partitions.get("application") == expected_application
        and partitions.get("bootloader") == expected_bootloader,
        "memory map partitions do not equal reviewed deployment policy",
    )
    regions = memory_map.get("regions")
    _require(isinstance(regions, list) and bool(regions), "memory map regions are missing")
    assert isinstance(regions, list)
    for region in regions:
        _require(isinstance(region, Mapping), "memory map region is malformed")
        assert isinstance(region, Mapping)
        provenance = region.get("provenance")
        _require(
            isinstance(provenance, list)
            and bool(provenance)
            and all(
                isinstance(item, Mapping) and item.get("authority") == "reconciled"
                for item in provenance
            ),
            f"memory map region is not reconciled: {region.get('name')}",
        )
    map_digest = hashlib.sha256(canonical_bytes(memory_map)).hexdigest()

    safety_summary = evidence["safety"]
    assert isinstance(safety_summary, Mapping)
    _require(
        safety_summary.get("memory_map_digest") == map_digest,
        "evidence memory-map digest disagrees with memory_map.yaml",
    )
    _require(
        safety_summary.get("official_document_asset_sha256")
        == reviewed.official_asset_sha256
        and safety_summary.get("device_support_asset_sha256")
        == reviewed.device_support_asset_sha256,
        "top-level safety asset hashes do not equal reviewed authority",
    )
    _require(
        safety_summary.get("runtime_pins")
        == {
            "pyocd_version": reviewed.pyocd_version,
            "target_module_sha256": reviewed.pyocd_target_module_sha256,
            "svd_bundle_sha256": reviewed.pyocd_svd_bundle_sha256,
        },
        "top-level runtime pins do not equal installed reviewed authority",
    )

    report_count = 0
    reports = firm.get("reports")
    _require(isinstance(reports, list) and bool(reports), "immutable report links are missing")
    assert isinstance(reports, list)
    required_response_text = "\n".join(
        _response_text(row.get("response"))
        for row in requests
        if row.get("tool") in {"board_setup", "board_validate"}
    )
    for report_record in reports:
        _require(isinstance(report_record, Mapping), "report record is malformed")
        assert isinstance(report_record, Mapping)
        report = json.loads(Path(str(report_record["path"])).read_text(encoding="utf-8"))
        _require(isinstance(report, Mapping), "report document is not an object")
        assert isinstance(report, Mapping)
        _require(report.get("report_type") == report_record.get("report_type"), "report type mismatch")
        _require(report.get("terminal_status") == report_record.get("terminal_status"), "report status mismatch")
        report_id = report_record.get("report_id")
        actual_id = report.get("attempt_id", report.get("validation_id"))
        _require(actual_id == report_id, "report ID does not match immutable report")
        _require(
            str(report_id) in required_response_text,
            f"setup report is not linked from a required setup/validation response: {report_id}",
        )
        report_count += 1

    source_records = evidence.get("source_artifacts")
    build_records = evidence.get("build_artifacts")
    _require(isinstance(source_records, list) and bool(source_records), "source artifacts are missing")
    _require(isinstance(build_records, list) and bool(build_records), "build artifacts are missing")
    assert isinstance(source_records, list) and isinstance(build_records, list)
    source_paths = {str(item.get("relative_path")) for item in source_records if isinstance(item, Mapping)}
    build_paths = {str(item.get("relative_path")) for item in build_records if isinstance(item, Mapping)}
    _require("src/main.c" in source_paths, "threaded application source is missing")
    _require(any(path.endswith(".elf") for path in build_paths), "ELF evidence is missing")
    _require(any(path.endswith(".hex") for path in build_paths), "HEX evidence is missing")
    _require(any(path.endswith(".map") for path in build_paths), "linker-map evidence is missing")
    source_mtimes: list[datetime] = []
    for source_record in source_records:
        _require(isinstance(source_record, Mapping), "source artifact record is malformed")
        assert isinstance(source_record, Mapping)
        modified = _time(source_record.get("modified_at"), "source artifact modified_at")
        source_path = Path(str(source_record.get("path"))).resolve(strict=True)
        filesystem_modified = datetime.fromtimestamp(
            source_path.stat().st_mtime, timezone.utc
        )
        _require(
            abs((modified - filesystem_modified).total_seconds()) < 0.001,
            f"source artifact modified_at does not match filesystem: {source_path.name}",
        )
        source_mtimes.append(modified)
    _require(
        min(source_mtimes) == source_time,
        "source_first_modified_at is not the earliest recorded source artifact",
    )

    routine_safety_calls = [
        tool
        for index, tool in enumerate(ordered_tools)
        if index > barrier_index and tool == "board_safety_refresh"
    ]
    _require(
        not routine_safety_calls,
        "routine build unexpectedly invoked safety setup/refresh instead of flash-time containment",
    )
    flash_index = ordered_index("flash_application", barrier_index)
    serial_index = ordered_index("serial_exchange", flash_index)
    disconnect_index = ordered_index("disconnect", serial_index)
    _require(
        source_time < _time(requests[flash_index].get("timestamp"), "flash operation timestamp")
        and disconnect_index == len(requests) - 1,
        "acceptance operation ordering is incomplete or has work after disconnect",
    )

    build_mtimes: list[datetime] = []
    build_by_path: dict[Path, Mapping[str, object]] = {}
    for build_record in build_records:
        _require(isinstance(build_record, Mapping), "build artifact record is malformed")
        assert isinstance(build_record, Mapping)
        modified = _time(build_record.get("modified_at"), "build artifact modified_at")
        build_path = Path(str(build_record.get("path"))).resolve(strict=True)
        filesystem_modified = datetime.fromtimestamp(build_path.stat().st_mtime, timezone.utc)
        _require(
            abs((modified - filesystem_modified).total_seconds()) < 0.001,
            f"build artifact modified_at does not match filesystem: {build_path.name}",
        )
        build_mtimes.append(modified)
        build_by_path[build_path] = build_record
    flash_started = _time(
        requests[flash_index].get("request_started_at"), "flash start"
    )
    _require(
        max(source_mtimes) < min(build_mtimes)
        and max(build_mtimes) <= flash_started,
        "build artifacts are not ordered after source and before flash-time containment",
    )

    flash_text = _response_text(requests[flash_index].get("response"))
    _require(
        "Flashed " in flash_text
        and "Refused" not in flash_text
        and "failed" not in flash_text.casefold(),
        "application flash did not report product-level success",
    )
    flash_arguments = requests[flash_index].get("arguments")
    _require(isinstance(flash_arguments, Mapping), "flash arguments are missing")
    assert isinstance(flash_arguments, Mapping)
    _require(
        Path(str(flash_arguments.get("artifact"))).resolve(strict=True) in build_by_path,
        "flashed artifact is not a recorded build artifact",
    )

    uart = evidence["uart"]
    assert isinstance(uart, Mapping)
    final_run = uart.get("final_run_id")
    serial_rows = [
        row
        for row in requests
        if row.get("tool") == "serial_exchange" and row.get("run_id") == final_run
    ]
    _require(bool(serial_rows), "final UART proof is absent from the MCP timeline")
    final_serial = serial_rows[-1]
    serial_args = final_serial["arguments"]
    assert isinstance(serial_args, Mapping)
    _require(serial_args.get("board_id") == hardware.get("board_id"), "UART proof used the wrong board")
    _require(serial_args.get("port") == uart_identity.get("port"), "UART proof used the wrong port")
    _require(serial_args.get("ready_probe_delay_seconds") == uart.get("ready_probe_delay_seconds"), "UART delay evidence disagrees")
    steps = serial_args.get("steps")
    _require(isinstance(steps, list) and len(steps) == 5, "UART proof does not have five exact steps")
    assert isinstance(steps, list)
    expectations = " ".join(str(item.get("expected_text")) for item in steps if isinstance(item, Mapping))
    for marker in ("[BLINK_WORKER] ON", "[BLINK_WORKER] OFF", "[BLINK_STATUS] ON", "[BLINK_STATUS] OFF", "1200ms"):
        _require(marker in expectations, f"UART planned expectations omit {marker}")
    exact_response = _response_text(final_serial.get("response"))
    _require(exact_response == uart.get("final_exact_response"), "UART result is not the exact MCP response")
    _require(
        "UART exchange matched" in exact_response
        and "ready=matched" in exact_response
        and "steps=5" in exact_response
        and exact_response.count("=matched") >= 6
        and "did not match" not in exact_response
        and "Refused" not in exact_response,
        "UART response does not prove readiness and all five executed matches",
    )

    disconnects = [row for row in requests if row.get("tool") == "disconnect"]
    _require(
        bool(disconnects)
        and _response_text(disconnects[-1]["response"]).startswith("Disconnected board")
        and str(hardware.get("board_id")) in _response_text(disconnects[-1]["response"]),
        "safe disconnect proof is missing",
    )
    return len(requests), report_count


def validate_evidence(evidence_path: Path) -> dict[str, object]:
    """Validate one evidence document and return a machine-readable summary."""

    evidence_path = evidence_path.resolve(strict=True)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    _require(isinstance(evidence, dict), "evidence root must be an object")
    _require(evidence.get("schema_version") == 3, "strict evidence requires schema version 3")
    _require(evidence.get("hardware_acceptance_status") == "pass", "hardware status is not pass")
    _require(evidence.get("strict_evidence_status") == "pass", "strict evidence status is not pass")

    workspace_value = evidence.get("workspace")
    if isinstance(workspace_value, Mapping):
        workspace_value = workspace_value.get("path")
    if not isinstance(workspace_value, str):
        raise EvidenceValidationError("workspace path is missing")
    workspace = Path(workspace_value).resolve(strict=True)
    _require(workspace.is_dir(), "workspace is not a directory")
    _require(evidence_path.is_relative_to(workspace), "evidence document is outside its workspace")

    server = evidence.get("server")
    _require(isinstance(server, Mapping), "server evidence is missing")
    run_id = server.get("server_run_id")
    if not isinstance(run_id, str) or not run_id.startswith("run-"):
        raise EvidenceValidationError("public server run_id is missing")
    _require(isinstance(server.get("server_started_at"), str), "server started_at is missing")
    _require(bool(server.get("commit")), "server commit is missing")

    versions = evidence.get("versions")
    _require(isinstance(versions, Mapping), "version evidence is missing")
    for name in ("mcp_protocol", "mcp_sdk", "package", "pyocd", "zephyr", "toolchain"):
        value = versions.get(name)
        _require(isinstance(value, str) and len(value) >= 3 and value != "v", f"version is missing: {name}")

    artifacts = list(_artifact_records(evidence))
    _require(bool(artifacts), "no linked artifacts were recorded")
    resolved_artifacts: set[Path] = set()
    for artifact in artifacts:
        resolved = _validate_artifact(workspace, artifact)
        _require(resolved not in resolved_artifacts, f"duplicate artifact record: {resolved}")
        resolved_artifacts.add(resolved)

    pre_code = evidence.get("pre_code_barrier")
    _require(isinstance(pre_code, Mapping), "pre-code barrier evidence is missing")
    _require(pre_code.get("ordering_proof") is True, "pre-code ordering proof failed")
    _require(pre_code.get("profile_committed_before_source") is True, "profile was not committed first")
    readiness = pre_code.get("readiness")
    _require(isinstance(readiness, Mapping), "pre-code readiness payload is missing")
    for field in ("configuration_ready", "live_session_ready", "ready_for_code"):
        _require(readiness.get(field) is True, f"pre-code readiness failed: {field}")

    safety = evidence.get("safety")
    _require(isinstance(safety, Mapping), "safety evidence is missing")
    _require(safety.get("aggregates_match") is True, "safety aggregate links disagree")
    _require(safety.get("reconciliation_status") == "agreement", "source reconciliation did not agree")
    _require(safety.get("hardware_provenance_only_reconciled") is True, "hardware provenance is not reconciled")
    _require(safety.get("hardware_provenance_authorities") == ["reconciled"], "unexpected hardware provenance")
    official = safety.get("official_document_asset_sha256")
    support = safety.get("device_support_asset_sha256")
    _require(isinstance(official, str) and isinstance(support, str), "source hashes are missing")
    _require(official != support, "official and device-support evidence are not distinct")
    pins = safety.get("runtime_pins")
    _require(isinstance(pins, Mapping), "runtime source pins are missing")
    for name in ("pyocd_version", "target_module_sha256", "svd_bundle_sha256"):
        _require(bool(pins.get(name)), f"runtime source pin is missing: {name}")

    uart = evidence.get("uart")
    _require(isinstance(uart, Mapping), "UART evidence is missing")
    for field in (
        "single_handle_five_steps",
        "worker_thread_on",
        "worker_thread_off",
        "command_ack_on",
        "command_ack_off",
        "status_on",
        "status_off",
        "quiet_after_off_1200ms",
    ):
        _require(uart.get(field) is True, f"UART proof failed: {field}")
    response = str(uart.get("final_exact_response", ""))
    for marker in ("[BLINK_WORKER] ON", "[BLINK_WORKER] OFF", "steps=5"):
        _require(marker in response, f"UART response is missing {marker}")

    commands = evidence.get("commands")
    _require(isinstance(commands, Mapping), "command evidence is missing")
    _require(commands.get("hardware_shell_commands") == [], "hardware shell bypass was recorded")
    _require(bool(evidence.get("no_bypass_assertion")), "hardware-boundary assertion is missing")
    _require(bool(evidence.get("safe_exit")), "safe-exit evidence is missing")

    timeline = _timeline_text(evidence)
    _require("initialization_handshake" in timeline, "timeline omits initialization_handshake")
    _require(run_id in timeline, "public run_id is not linked from the MCP timeline")
    plans = evidence.get("plans")
    _require(isinstance(plans, Mapping) and bool(plans), "plan evidence is missing")
    for plan_ids in plans.values():
        _require(isinstance(plan_ids, list) and bool(plan_ids), "plan ID list is empty")
        for plan_id in plan_ids:
            _require(str(plan_id) in timeline, f"plan ID is absent from timeline: {plan_id}")

    report_count = 0
    firm_artifacts = evidence.get("firm_artifacts")
    if isinstance(firm_artifacts, Mapping):
        reports = firm_artifacts.get("reports")
        if isinstance(reports, list):
            report_count = len(reports)
            for report in reports:
                _require(isinstance(report, Mapping), "report record is malformed")
                report_id = report.get("report_id")
                _require(bool(report_id), "report ID is missing")
                _require(str(report_id) in timeline, f"report ID is absent from timeline: {report_id}")
    _require(report_count > 0, "immutable report links are missing")

    operation_count, semantic_report_count = _validate_semantic_links(evidence, workspace)
    _require(semantic_report_count == report_count, "semantic report count disagrees")

    return {
        "status": "pass",
        "evidence": str(evidence_path),
        "evidence_sha256": _sha256(evidence_path),
        "server_run_id": run_id,
        "artifact_count": len(artifacts),
        "report_count": report_count,
        "operation_count": operation_count,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    try:
        result = validate_evidence(args.evidence)
    except (EvidenceValidationError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
