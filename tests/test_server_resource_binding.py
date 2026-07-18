from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from pathlib import Path
from typing import Any, cast

import mcp.types as types
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from pyocd_debug_mcp import server
from pyocd_debug_mcp.firmstore.cache import CacheResolution
from pyocd_debug_mcp.firmstore.profiles import ProfileRepository
from pyocd_debug_mcp.firmstore.reports import ReportWriter
from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.guardrails.plan_defs import PLAN_DEFINITIONS
from pyocd_debug_mcp.guardrails.plan_engine import PlanRefusal
from pyocd_debug_mcp.guardrails.gate import GateManager
from pyocd_debug_mcp.kernel.operations import ManagedOperation, OperationState
from pyocd_debug_mcp.setup_flow.preflight import (
    PreflightDecision,
    PreflightInventory,
    ProbeCandidate,
    SerialCandidate,
    SetupUserInput,
)
from pyocd_debug_mcp.setup_flow.setup import SetupPhase, SetupPhaseContext, SetupPhaseOutcome
from pyocd_debug_mcp.setup_flow.validate import ValidationInventory, ValidationSerial
from pyocd_debug_mcp.safety.map_build import SafetyMapError
from pyocd_debug_mcp.services.connections import ConnectionManager


def _call_payload(result: types.CallToolResult) -> dict[str, object]:
    content = result.content[0]
    assert isinstance(content, types.TextContent)
    return json.loads(content.text)


def test_successful_ordinary_operation_releases_reset_without_rebooting(monkeypatch) -> None:
    reset_line: list[bool] = []
    handle = SimpleNamespace(
        session=SimpleNamespace(probe=SimpleNamespace(assert_reset=reset_line.append))
    )
    connection = SimpleNamespace(handle=handle, runtime_session=object())
    monkeypatch.setattr(
        server.connection_manager,
        "maybe_connection",
        lambda board_id: connection if board_id == "board_a" else None,
    )
    reset_calls: list[object] = []
    monkeypatch.setattr(
        server.target_control, "reset", lambda *args, **kwargs: reset_calls.append(args)
    )
    operation = ManagedOperation(
        "op-1",
        "request-1",
        "serial_exchange",
        "board_a",
        10.0,
        False,
        False,
        state=OperationState.COMPLETED,
    )

    server._bind_managed_board_resources(operation)
    operation.cleanup()

    assert reset_line == [False]
    assert reset_calls == []
    assert operation.resources.restore_final_state == []


def test_setup_status_exposes_uart_readiness_as_a_separate_barrier(monkeypatch) -> None:
    expected_ref = (
        server._firm_store.layout.safety_reference_prefix("board_a") / "memory_map.yaml"
    ).as_posix()
    profile = SimpleNamespace(
        safety_ref=expected_ref,
        mcu_part_number="nRF52840-QIAA",
        board=SimpleNamespace(probe_family="jlink"),
    )
    artifacts = SimpleNamespace(regions=())
    connection = SimpleNamespace(
        connection_id="connection-a",
        handle=SimpleNamespace(probe_uid="683377322"),
    )
    stamp = SimpleNamespace(connection_id="connection-a", map_digest="fingerprint-a")
    monkeypatch.setattr(server._profile_repository, "load", lambda *_args, **_kwargs: profile)
    monkeypatch.setattr(server._safety_repository, "load_current", lambda _board_id: artifacts)
    monkeypatch.setattr(server, "region_conflicts", lambda _regions: ())
    monkeypatch.setattr(server, "_missing_base_safety_kinds", lambda _regions: ())
    monkeypatch.setattr(
        server._safety_policy, "current_aggregate", lambda _board_id: "fingerprint-a"
    )
    monkeypatch.setattr(server.connection_manager, "maybe_connection", lambda _board_id: connection)
    monkeypatch.setattr(server.gate_manager, "snapshot", lambda _board_id: stamp)
    monkeypatch.setattr(
        server,
        "_validation_inventory",
        lambda: ValidationInventory(
            serial_ports=(ValidationSerial("COM11", "COM11", "J-Link UART", "000683377322", 1, 2),)
        ),
    )
    monkeypatch.setattr(
        server._attachment_cache,
        "resolve",
        lambda *_args, **_kwargs: CacheResolution(True, "exact_match", "COM11"),
    )

    status = server._get_setup_status("board_a")

    assert status["ready_for_code"] is True
    assert status["uart_attachment_ready"] is True
    assert status["ready_for_uart_work"] is True
    assert status["resolved_uart"] == {
        "serial_id": "COM11",
        "usb_serial": "000683377322",
        "port_path": "COM11",
        "vid": 1,
        "pid": 2,
    }
    assert status["resolved_probe"] == {
        "probe_uid": "683377322",
        "connection_id": "connection-a",
        "probe_family": "jlink",
    }
    guidance = cast(dict[str, object], status["build_guidance"])
    assert guidance["authority"] == "advisory_only"
    assert guidance["primary_workflow"] == "native_project_build"
    collector = cast(dict[str, object], guidance["artifact_collection"])
    assert collector["tool"] == "collect_build_artifacts"
    assert guidance["toolchain_fallback"] is None
    boundary = str(guidance["safety_boundary"])
    assert "advisory and not safety authority" in boundary
    assert "flash plan, which binds that artifact" in boundary
    assert "revalidates its bytes and complete containment before target mutation" in boundary
    assert "board_safety_refresh only for a stable-map problem" in boundary


def test_stale_validation_cannot_clear_new_assignment_or_record_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assignments = type(server.assignment_store)({})
    assignments.assign("probe:NEW-PROBE", "board_a")
    gates = GateManager()
    monkeypatch.setattr(server, "assignment_store", assignments)
    monkeypatch.setattr(server, "gate_manager", gates)

    recorded = server._record_validation_mismatch(
        "board_a",
        "validation-old",
        "old-probe",
        "OLD-PROBE",
        "Expected MCU",
        "Observed MCU",
    )

    assert recorded is False
    assert assignments.bindings() == {"probe:NEW-PROBE": "board_a"}
    assert gates.current_mismatch("board_a", "probe:NEW-PROBE", "NEW-PROBE") is None


def test_assignment_replacement_retires_conflicting_live_physical_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assignments = type(server.assignment_store)({})
    assignments.assign("probe:PROBE-001", "board_a")
    connections = ConnectionManager()
    handle = SimpleNamespace(probe_uid="PROBE-001")
    runtime = SimpleNamespace(session_id="runtime-a")
    connections.assign(
        "board_a",
        cast(Any, handle),
        cast(Any, runtime),
        connection_id="probe:probe-001",
    )
    closed_handles: list[object] = []
    closed_runtimes: list[object] = []
    monkeypatch.setattr(server, "assignment_store", assignments)
    monkeypatch.setattr(server, "connection_manager", connections)
    monkeypatch.setattr(server.target_control, "close_session", closed_handles.append)
    monkeypatch.setattr(server._session_store, "close_session", closed_runtimes.append)

    server._replace_setup_assignments(
        {"probe:PROBE-001": "board_b"},
        "test reassignment",
    )

    assert connections.maybe_connection("board_a") is None
    assert assignments.bindings() == {"probe:PROBE-001": "board_b"}
    assert closed_handles == [handle]
    assert closed_runtimes == [runtime]


def test_setup_status_cannot_report_legacy_safety_authority_ready(monkeypatch) -> None:
    expected_ref = (
        server._firm_store.layout.safety_reference_prefix("board_a") / "memory_map.yaml"
    ).as_posix()
    monkeypatch.setattr(
        server,
        "_profile_repository",
        SimpleNamespace(
            load=lambda *_args, **_kwargs: SimpleNamespace(
                board_id="board_a",
                safety_ref=expected_ref,
                mcu_part_number="nRF52840-QIAA",
            )
        ),
    )
    monkeypatch.setattr(
        server,
        "_safety_repository",
        SimpleNamespace(load_current=lambda _board: SimpleNamespace(regions=())),
    )
    monkeypatch.setattr(server, "region_conflicts", lambda _regions: ())
    monkeypatch.setattr(server, "_missing_base_safety_kinds", lambda _regions: ())

    def reject_legacy(_board: str) -> str:
        raise server.SafetyPolicyError(
            "safety/authority-migration-required",
            "legacy authority schema",
            remedy=("board_safety_refresh", "board_validate"),
        )

    monkeypatch.setattr(
        server,
        "_safety_policy",
        SimpleNamespace(current_aggregate=reject_legacy),
    )

    status = server._get_setup_status("board_a")

    assert status["configuration_ready"] is False
    assert "legacy authority schema" in str(status["configuration_reason"])


def test_build_guidance_does_not_infer_board_target_from_mcu(monkeypatch) -> None:
    monkeypatch.setattr(
        server,
        "_profile_repository",
        SimpleNamespace(
            load=lambda *_args, **_kwargs: SimpleNamespace(
                mcu_part_number="nRF52840-QIAA",
                safety_ref=None,
            )
        ),
    )

    status = server._get_setup_status("board_a")
    guidance = cast(dict[str, object], status["build_guidance"])
    assert guidance["primary_workflow"] == "native_project_build"
    assert guidance["toolchain_fallback"] is None


def test_automatic_setup_commits_the_complete_reviewed_candidate(monkeypatch) -> None:
    datasheet = Path("Nano_BLE_MCU-nRF52840_PS_v1.1.pdf").resolve()
    datasheet_digest = hashlib.sha256(datasheet.read_bytes()).hexdigest()
    profile = SimpleNamespace(
        mcu_part_number="nRF52840-QIAA",
        to_document=lambda: {"datasheet_sha256": datasheet_digest},
    )
    monkeypatch.setattr(server._profile_repository, "load", lambda *_args, **_kwargs: profile)
    sentinel = object()
    monkeypatch.setattr(server, "_derive_reviewed_safety_map", lambda _board_id: sentinel)
    commits: list[tuple[str, object]] = []
    monkeypatch.setattr(
        server._safety_repository,
        "commit",
        lambda board_id, candidate: commits.append((board_id, candidate)),
    )
    context = SimpleNamespace(
        user_input=SimpleNamespace(
            board_id="nrf_board",
            mcu_part_number="nRF52840-QIAA",
            datasheet_path=str(datasheet),
        )
    )

    assert server._build_automatic_catalog_safety(cast(SetupPhaseContext, context)) is sentinel
    assert commits == [("nrf_board", sentinel)]


def test_automatic_setup_rejects_family_name_without_rewriting_profile(monkeypatch) -> None:
    builder_called = False
    datasheet = Path("Nano_BLE_MCU-nRF52840_PS_v1.1.pdf").resolve()
    profile = SimpleNamespace(
        mcu_part_number="nRF52840",
        to_document=lambda: {
            "schema_version": 2,
            "board_id": "nrf_board",
            "mcu_part_number": "nRF52840",
            "datasheet_sha256": hashlib.sha256(datasheet.read_bytes()).hexdigest(),
        },
    )
    monkeypatch.setattr(server._profile_repository, "load", lambda *_args, **_kwargs: profile)
    def build(_request: object) -> object:
        nonlocal builder_called
        builder_called = True
        return SimpleNamespace(status="safety_setup_completed")

    monkeypatch.setattr(server, "_derive_reviewed_safety_map", build)
    context = cast(
        SetupPhaseContext,
        SimpleNamespace(
            user_input=SimpleNamespace(
                board_id="nrf_board",
                mcu_part_number="nRF52840",
                datasheet_path=str(datasheet),
            )
        ),
    )

    with pytest.raises(server.ReviewedSupportNotFoundError, match="No reviewed support"):
        server._build_automatic_catalog_safety(context)
    assert profile.mcu_part_number == "nRF52840"
    assert builder_called is False


def test_automatic_setup_rejects_datasheet_changed_after_profile_acceptance(
    monkeypatch, tmp_path: Path
) -> None:
    accepted = Path("Nano_BLE_MCU-nRF52840_PS_v1.1.pdf").resolve()
    profile = SimpleNamespace(
        mcu_part_number="nRF52840-QIAA",
        to_document=lambda: {
            "datasheet_sha256": hashlib.sha256(accepted.read_bytes()).hexdigest()
        },
    )
    monkeypatch.setattr(server._profile_repository, "load", lambda *_args, **_kwargs: profile)
    builder_called = False

    def build(_board_id: str) -> object:
        nonlocal builder_called
        builder_called = True
        return object()

    monkeypatch.setattr(server, "_derive_reviewed_safety_map", build)
    changed = tmp_path / "changed.pdf"
    changed.write_bytes(b"%PDF-changed-after-profile")
    context = cast(
        SetupPhaseContext,
        SimpleNamespace(
            user_input=SimpleNamespace(
                board_id="nrf_board",
                mcu_part_number="nRF52840-QIAA",
                datasheet_path=str(changed),
            )
        ),
    )

    with pytest.raises(server.BoardCatalogError, match="changed after profile acceptance"):
        server._build_automatic_catalog_safety(context)
    assert builder_called is False


def test_setup_safety_research_automatically_rebuilds_obsolete_reviewed_map(
    monkeypatch,
) -> None:
    rebuilt = SimpleNamespace(
        canonical_digest="current-reviewed-map",
        source_digests=SimpleNamespace(
            to_document=lambda: {
                "semantic_profile": "a" * 64,
                "reviewed_device_support": "b" * 64,
            }
        ),
    )
    monkeypatch.setattr(
        server._safety_repository,
        "load_current",
        lambda _board: (_ for _ in ()).throw(SafetyMapError("old schema")),
    )
    monkeypatch.setattr(server, "_build_automatic_catalog_safety", lambda _context: rebuilt)
    context = cast(
        SetupPhaseContext,
        SimpleNamespace(user_input=SimpleNamespace(board_id="nf_board")),
    )

    result = server._setup_safety_research_phase(context)

    assert result.verified is True
    assert result.code == "setup/safety-sources-verified"
    assert result.details["map_digest"] == "current-reviewed-map"


def test_fresh_setup_rejects_family_only_mcu_before_profile_commit() -> None:
    datasheet = Path("Nano_BLE_MCU-nRF52840_PS_v1.1.pdf").resolve()
    context = cast(
        SetupPhaseContext,
        SimpleNamespace(
            user_input=SimpleNamespace(
                board_id="nrf_board",
                mcu_part_number="nRF52840",
                datasheet_path=str(datasheet),
            )
        ),
    )

    result = server._setup_connection_phase(context)

    assert result.verified is False
    assert result.code == "setup/reviewed-support-not-found"
    assert "exact MCU" in result.agent_prompt
    assert "server-hashed datasheet" in result.agent_prompt


def test_fresh_setup_rejects_unknown_probe_provider_before_connect_or_profile_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profiles = ProfileRepository(FirmStore(tmp_path), legacy_board_dir=tmp_path / "legacy")
    monkeypatch.setattr(server, "_profile_repository", profiles)
    monkeypatch.setattr(
        server.target_control,
        "open_session",
        lambda **_kwargs: pytest.fail("unknown provider must be rejected before connect"),
    )
    datasheet = Path("Nano_BLE_MCU-nRF52840_PS_v1.1.pdf").resolve()
    user_input = SetupUserInput(
        "unknown_probe_board",
        "probe:future-probe",
        "Unknown Probe Board",
        "nRF52840-QIAA",
        115200,
        datasheet_path=str(datasheet),
        requires_uart=False,
    )
    preflight = PreflightDecision(
        "preflight_ready",
        "setup/preflight-ready",
        "ready",
        selected_probe=ProbeCandidate(
            "future-probe", "Future Universal Probe", "unknown", "future-probe"
        ),
        selected_target="nrf52840",
    )
    context = cast(
        SetupPhaseContext,
        SimpleNamespace(user_input=user_input, preflight=preflight),
    )

    result = server._setup_connection_phase(context)

    assert result.verified is False
    assert result.code == "setup/catalog-route-mismatch"
    assert "could not be identified" in result.agent_prompt
    assert not profiles.store.layout.board_profile("unknown_probe_board").exists()


def test_reviewed_opaque_target_reaches_live_connect_before_profile_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profiles = ProfileRepository(FirmStore(tmp_path), legacy_board_dir=tmp_path / "legacy")
    monkeypatch.setattr(server, "_profile_repository", profiles)
    events: list[str] = []
    handle = SimpleNamespace()

    def open_session(**kwargs: object) -> object:
        assert kwargs["target"] == "nrf52840"
        events.append("connect")
        assert not profiles.store.layout.board_profile("reviewed_board").exists()
        return handle

    def read_memory(_handle: object, address: int, _width: int) -> int:
        return 0x00052840 if address == 0x10000100 else 0x12345678

    monkeypatch.setattr(server.target_control, "open_session", open_session)
    monkeypatch.setattr(server.target_control, "read_memory", read_memory)
    monkeypatch.setattr(server.target_control, "close_session", lambda _handle: None)
    datasheet = Path("Nano_BLE_MCU-nRF52840_PS_v1.1.pdf").resolve()
    user_input = SetupUserInput(
        "reviewed_board",
        "probe:opaque-probe",
        "Reviewed Board",
        "nRF52840-QIAA",
        115200,
        datasheet_path=str(datasheet),
        requires_uart=False,
    )
    preflight = PreflightDecision(
        "preflight_ready",
        "setup/preflight-ready",
        "ready",
        selected_probe=ProbeCandidate(
            "opaque-probe", "CMSIS-DAP Probe", "cmsisdap", "opaque-probe"
        ),
        selected_target="nrf52840",
    )
    context = cast(
        SetupPhaseContext,
        SimpleNamespace(user_input=user_input, preflight=preflight),
    )

    result = server._setup_connection_phase(context)

    assert result.verified is True
    assert events == ["connect"]
    committed = profiles.load("reviewed_board", include_legacy=False)
    assert committed.mcu_part_number == "nRF52840-QIAA"
    assert committed.board.pyocd_target == "nrf52840"
    assert committed.board.probe_family == "cmsisdap"
    assert committed.board.silicon_id_label == "FICR INFO.PART exact part identifier"
    assert committed.board.silicon_id_addr == 0x10000100
    assert committed.board.silicon_id_expected == 0x00052840
    assert committed.board.silicon_id_mask == 0xFFFFFFFF


def test_incomplete_profile_repair_rechecks_identity_and_commits_optional_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profiles = ProfileRepository(FirmStore(tmp_path), legacy_board_dir=tmp_path / "legacy")
    profiles.commit_core(
        profiles.stage_core(
            {
                "board_id": "repair_board",
                "display_name": "Repair Board",
                "mcu_part_number": "nRF52840-QIAA",
                "mcu_family": "nrf52840",
                "probe_family": "jlink",
                "pyocd_target": "nrf52840",
            }
        )
    )
    monkeypatch.setattr(server, "_profile_repository", profiles)
    connected: list[str] = []
    handle = SimpleNamespace()

    def open_session(**kwargs: object) -> object:
        connected.append(str(kwargs["target"]))
        return handle

    monkeypatch.setattr(server.target_control, "open_session", open_session)
    monkeypatch.setattr(
        server.target_control,
        "read_memory",
        lambda _handle, address, _width: 0x00052840 if address == 0x10000100 else 0,
    )
    monkeypatch.setattr(server.target_control, "close_session", lambda _handle: None)
    datasheet = Path("Nano_BLE_MCU-nRF52840_PS_v1.1.pdf").resolve()
    context = cast(
        SetupPhaseContext,
        SimpleNamespace(
            user_input=SetupUserInput(
                "repair_board",
                "probe:repair-probe",
                "Repair Board",
                "nRF52840-QIAA",
                None,
                datasheet_path=str(datasheet),
                requires_uart=False,
            ),
            preflight=PreflightDecision(
                "preflight_ready",
                "setup/preflight-ready",
                "ready",
                selected_probe=ProbeCandidate(
                    "repair-probe", "CMSIS-DAP Probe", "cmsisdap", "repair-probe"
                ),
                selected_target="nrf52840",
            ),
        ),
    )

    result = server._setup_connection_phase(context)

    assert result.verified is True
    assert connected == ["nrf52840"]
    repaired = profiles.load("repair_board", include_legacy=False)
    assert repaired.to_document()["datasheet_sha256"] == hashlib.sha256(
        datasheet.read_bytes()
    ).hexdigest()
    assert repaired.board.silicon_id_expected == 0x00052840


def test_plan_scope_allows_only_repair_for_parseable_incomplete_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profiles = ProfileRepository(FirmStore(tmp_path), legacy_board_dir=tmp_path / "legacy")
    profiles.commit_core(
        profiles.stage_core(
            {
                "board_id": "repair_board",
                "display_name": "Repair Board",
                "mcu_part_number": "nRF52840-QIAA",
                "mcu_family": "nrf52840",
                "probe_family": "jlink",
                "pyocd_target": "nrf52840",
            }
        )
    )
    monkeypatch.setattr(server, "_profile_repository", profiles)
    monkeypatch.setattr(server.connection_manager, "maybe_connection", lambda _board: None)
    definition = PLAN_DEFINITIONS["board_setup"]
    monkeypatch.setattr(
        server.plan_engine,
        "active_plan",
        lambda *_args: SimpleNamespace(action_parameters={"mode": "repair"}),
    )

    server._validate_plan_scope(definition, "repair_board", None)

    monkeypatch.setattr(
        server.plan_engine,
        "active_plan",
        lambda *_args: SimpleNamespace(action_parameters={"mode": "setup"}),
    )
    with pytest.raises(PlanRefusal, match="Only an incomplete, parseable"):
        server._validate_plan_scope(definition, "repair_board", None)


def test_legacy_repair_preserves_identity_and_uses_constrained_migration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    (legacy_dir / "legacy_board.yaml").write_text(
        "\n".join(
            (
                "board_id: legacy_board",
                'display_name: "Legacy Board"',
                "mcu_family: nrf52840",
                "probe_family: jlink",
                "pyocd_target: nrf52840",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    profiles = ProfileRepository(FirmStore(tmp_path), legacy_board_dir=legacy_dir)
    monkeypatch.setattr(server, "_profile_repository", profiles)
    handle = SimpleNamespace()
    monkeypatch.setattr(server.target_control, "open_session", lambda **_kwargs: handle)
    monkeypatch.setattr(
        server.target_control,
        "read_memory",
        lambda _handle, address, _width: 0x00052840 if address == 0x10000100 else 0,
    )
    monkeypatch.setattr(server.target_control, "close_session", lambda _handle: None)
    datasheet = Path("Nano_BLE_MCU-nRF52840_PS_v1.1.pdf").resolve()
    preflight = PreflightDecision(
        "preflight_ready",
        "setup/preflight-ready",
        "ready",
        selected_probe=ProbeCandidate("probe-a", "J-Link", "jlink", "probe-a"),
        selected_target="nrf52840",
    )

    wrong = server._setup_connection_phase(
        cast(
            SetupPhaseContext,
            SimpleNamespace(
                user_input=SetupUserInput(
                    "legacy_board",
                    "probe:probe-a",
                    "Renamed Board",
                    "nRF52840-QIAA",
                    None,
                    datasheet_path=str(datasheet),
                    requires_uart=False,
                ),
                preflight=preflight,
            ),
        )
    )
    assert wrong.code == "setup/existing-profile-identity-mismatch"
    assert not profiles.store.layout.board_profile("legacy_board").exists()

    repaired = server._setup_connection_phase(
        cast(
            SetupPhaseContext,
            SimpleNamespace(
                user_input=SetupUserInput(
                    "legacy_board",
                    "probe:probe-a",
                    "Legacy Board",
                    "nRF52840-QIAA",
                    None,
                    datasheet_path=str(datasheet),
                    requires_uart=False,
                ),
                preflight=preflight,
            ),
        )
    )

    assert repaired.verified is True
    migrated = profiles.load("legacy_board", include_legacy=False)
    assert migrated.display_name == "Legacy Board"
    assert migrated.mcu_part_number == "nRF52840-QIAA"


async def test_live_mcp_board_setup_commits_target_neutral_silicon_identity_label(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    board_id = "debias_mcp_board"
    store = FirmStore(tmp_path)
    profiles = ProfileRepository(store, legacy_board_dir=tmp_path / "legacy")
    reports = ReportWriter(store)
    inventory = PreflightInventory(
        probes=(ProbeCandidate("probe-a", "Reviewed J-Link", "jlink", "PROBE-001"),),
        serial_ports=(
            SerialCandidate("UART-001", "COM-test", "Reviewed UART", "UART-001", 1, 2),
        ),
        built_in_targets=("nrf52840",),
        exact_detected_targets=("nrf52840",),
    )
    phase_handlers = {
        SetupPhase.TARGET_SUPPORT: lambda _context: SetupPhaseOutcome.success(
            "test/target-supported"
        ),
        SetupPhase.CONNECTION: server._setup_connection_phase,
        SetupPhase.VALIDATION: lambda _context: SetupPhaseOutcome.success(
            "test/validation-complete"
        ),
        SetupPhase.SAFETY_RESEARCH: lambda _context: SetupPhaseOutcome.success(
            "test/safety-reviewed"
        ),
        SetupPhase.SAFETY_MAP: lambda _context: SetupPhaseOutcome.success(
            "test/safety-map-complete"
        ),
        SetupPhase.COMMIT: lambda _context: SetupPhaseOutcome.success("test/commit-complete"),
    }
    monkeypatch.setattr(server, "_profile_repository", profiles)
    monkeypatch.setattr(server._setup_workflow, "reports", reports)
    monkeypatch.setattr(server._setup_workflow, "inventory_provider", lambda _input: inventory)
    monkeypatch.setattr(server._setup_workflow, "phase_handlers", phase_handlers)
    monkeypatch.setattr(server._setup_workflow, "on_cache_confirmation", lambda *_args: None)
    assignment_store = type(server.assignment_store)({})
    assignment_store.assign("probe:PROBE-001", board_id)
    monkeypatch.setattr(server, "assignment_store", assignment_store)
    handle = SimpleNamespace()
    monkeypatch.setattr(server.target_control, "open_session", lambda **_kwargs: handle)
    monkeypatch.setattr(
        server.target_control,
        "read_memory",
        lambda _handle, address, _width: 0x00052840 if address == 0x10000100 else 0,
    )
    monkeypatch.setattr(server.target_control, "close_session", lambda _handle: None)
    datasheet = str(Path("Nano_BLE_MCU-nRF52840_PS_v1.1.pdf").resolve())
    action_parameters = {
        "mode": "setup",
        "connection_id": "probe:PROBE-001",
        "display_name": "De-bias MCP Board",
        "mcu_part_number": "nRF52840-QIAA",
        "requires_uart": True,
        "serial_baudrate": 115200,
        "serial_id": "UART-001",
        "datasheet_path": datasheet,
    }
    plan = {
        "board_id": board_id,
        "hypothesis": "The reviewed live identity matches the selected board and package.",
        "hypothesis_made": True,
        "strategy": "Complete one bounded setup and inspect the committed identity evidence.",
        "strategy_evaluated": True,
        "expected_fail_return": "A setup status naming the exact failed evidence check.",
        "expected_success_return": "A completed setup report and one schema-v2 profile.",
        "max_calls": 1,
        "max_calls_buffer": 0,
        "action_parameters": action_parameters,
        "user_permission": "one-time",
    }
    null_plan = {name: None for name in PLAN_DEFINITIONS["board_setup"].null_field_names}

    async with create_connected_server_and_client_session(server.mcp) as session:
        initialized = await session.call_tool("board_setup-plan", null_plan)
        assert initialized.isError is not True
        await session.call_tool(
            "load_setup_tool", {"board_id": board_id, "tool_name": "board_setup-plan"}
        )
        accepted = await session.call_tool("board_setup-plan", plan)
        assert accepted.isError is not True
        visible = {tool.name for tool in (await session.list_tools()).tools}
        assert "board_setup" in visible
        completed = await session.call_tool(
            "board_setup", {"board_id": board_id, **action_parameters}
        )

    payload = _call_payload(completed)
    assert payload["status"] == "setup_completed"
    committed = profiles.load(board_id, include_legacy=False)
    assert committed.display_name == "De-bias MCP Board"
    assert committed.to_document()["datasheet_sha256"] == hashlib.sha256(
        Path(datasheet).read_bytes()
    ).hexdigest()
    assert committed.board.silicon_id_label == "FICR INFO.PART exact part identifier"
    assert committed.board.silicon_id_addr == 0x10000100
    assert committed.board.silicon_id_expected == 0x00052840
    assert committed.board.silicon_id_mask == 0xFFFFFFFF
