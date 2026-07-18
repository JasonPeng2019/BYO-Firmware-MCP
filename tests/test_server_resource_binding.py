from __future__ import annotations

from collections.abc import Mapping
import json
import subprocess
from types import SimpleNamespace
from pathlib import Path
from typing import cast

import mcp.types as types
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from pyocd_debug_mcp import server
from pyocd_debug_mcp.firmstore.cache import CacheResolution
from pyocd_debug_mcp.firmstore.profiles import ProfileRepository
from pyocd_debug_mcp.firmstore.reports import ReportWriter
from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.guardrails.plan_defs import PLAN_DEFINITIONS
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
from pyocd_debug_mcp.safety.fingerprints import FingerprintSource
from pyocd_debug_mcp.safety.regions import SourceAuthority
from pyocd_debug_mcp.safety.map_build import SafetySetupRequest


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
    artifacts = SimpleNamespace(regions=(), fingerprints=SimpleNamespace(aggregate="fingerprint-a"))
    connection = SimpleNamespace(
        connection_id="connection-a",
        handle=SimpleNamespace(probe_uid="683377322"),
    )
    stamp = SimpleNamespace(connection_id="connection-a", aggregate_fingerprint="fingerprint-a")
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
    fallback = cast(dict[str, object], guidance["toolchain_fallback"])
    assert fallback["zephyr_board_target"] == "nrf52840dk/nrf52840"
    expected_argv = [
        server.sys.executable,
        "-m",
        "pyocd_debug_mcp.zephyr_build",
        "--app-dir",
        "<app-dir>",
        "--build-dir",
        "<build-dir>",
        "--board",
        "nrf52840dk/nrf52840",
    ]
    assert fallback["recommended_argv"] == expected_argv
    assert str(fallback["recommended_powershell"]).startswith("& '")
    assert "-m" in str(fallback["recommended_command"])
    assert "optional parameterized fallback" in str(fallback["reason"])
    assert "Build guidance is not safety authority" in str(guidance["safety_boundary"])


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
            remedy=("board_setup", "board_safety_setup", "board_validate"),
        )

    monkeypatch.setattr(
        server,
        "_safety_policy",
        SimpleNamespace(current_aggregate=reject_legacy),
    )

    status = server._get_setup_status("board_a")

    assert status["configuration_ready"] is False
    assert "legacy authority schema" in str(status["configuration_reason"])


@pytest.mark.skipif(server.sys.platform != "win32", reason="PowerShell rendering is Windows-only")
def test_returned_build_guidance_is_executable_in_powershell(monkeypatch) -> None:
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
    fallback = cast(dict[str, object], guidance["toolchain_fallback"])
    command = str(fallback["recommended_powershell"])
    help_command = command.split(" '--app-dir'", 1)[0] + " '--help'"
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", help_command],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stderr
    assert "usage:" in completed.stdout.casefold()


def test_automatic_setup_builds_only_strictly_reconciled_regions(monkeypatch) -> None:
    captured: dict[str, object] = {}
    profile = SimpleNamespace(
        mcu_part_number="nRF52840-QIAA",
        to_document=lambda: {
            "schema_version": 2,
            "board_id": "nrf_board",
            "mcu_part_number": "nRF52840-QIAA",
        },
    )
    monkeypatch.setattr(server._profile_repository, "load", lambda *_args, **_kwargs: profile)

    def build(request: object) -> object:
        captured["request"] = request
        return SimpleNamespace(status="safety_setup_completed")

    monkeypatch.setattr(server._safety_builder, "build", build)
    sentinel = object()
    monkeypatch.setattr(server._safety_repository, "load_current", lambda _board_id: sentinel)
    datasheet = Path("Nano_BLE_MCU-nRF52840_PS_v1.1.pdf").resolve()
    context = SimpleNamespace(
        user_input=SimpleNamespace(
            board_id="nrf_board",
            board_type="nrf52840dk",
            datasheet_path=str(datasheet),
        )
    )

    assert server._build_automatic_catalog_safety(cast(SetupPhaseContext, context)) is sentinel
    request = cast(SafetySetupRequest, captured["request"])
    assert {
        (item.region.name, item.region.kind.value) for item in request.regions
    } >= {
        ("volatile GPIO registers", "peripheral"),
        ("nonvolatile memory and access control registers", "prohibited"),
    }
    assert len(request.regions) == 8
    for contribution in request.regions:
        assert contribution.source_groups == (
            FingerprintSource.EVIDENCE,
            FingerprintSource.GEOMETRY,
        )
        assert {item.authority for item in contribution.region.provenance} == {
            SourceAuthority.RECONCILED
        }
    sources = request.inputs.canonical_documents()
    support = cast(Mapping[str, object], sources["pack"])
    evidence = cast(Mapping[str, object], sources["evidence"])
    official = cast(Mapping[str, object], evidence["official_document"])
    assert support["asset_sha256"] != official["asset_sha256"]
    reconciliation = cast(Mapping[str, object], evidence["reconciliation"])
    assert reconciliation["status"] == "agreement"


def test_automatic_setup_rejects_family_name_without_rewriting_profile(monkeypatch) -> None:
    builder_called = False
    profile = SimpleNamespace(
        mcu_part_number="nRF52840",
        to_document=lambda: {
            "schema_version": 2,
            "board_id": "nrf_board",
            "mcu_part_number": "nRF52840",
        },
    )
    monkeypatch.setattr(server._profile_repository, "load", lambda *_args, **_kwargs: profile)
    def build(_request: object) -> object:
        nonlocal builder_called
        builder_called = True
        return SimpleNamespace(status="safety_setup_completed")

    monkeypatch.setattr(server._safety_builder, "build", build)
    datasheet = Path("Nano_BLE_MCU-nRF52840_PS_v1.1.pdf").resolve()
    context = cast(
        SetupPhaseContext,
        SimpleNamespace(
            user_input=SimpleNamespace(
                board_id="nrf_board",
                board_type="nrf52840dk",
                datasheet_path=str(datasheet),
            )
        ),
    )

    with pytest.raises(server.BoardCatalogError, match="exact reviewed package variant"):
        server._build_automatic_catalog_safety(context)
    assert profile.mcu_part_number == "nRF52840"
    assert builder_called is False


def test_setup_safety_research_automatically_rebuilds_obsolete_reviewed_map(
    monkeypatch,
) -> None:
    legacy = SimpleNamespace(source_manifest={})
    rebuilt = SimpleNamespace(
        source_manifest={"sources": {"evidence": {}}},
        fingerprints=SimpleNamespace(aggregate="current-reviewed-aggregate"),
    )
    monkeypatch.setattr(server._safety_repository, "load_current", lambda _board: legacy)
    monkeypatch.setattr(server, "_build_automatic_catalog_safety", lambda _context: rebuilt)
    context = cast(
        SetupPhaseContext,
        SimpleNamespace(user_input=SimpleNamespace(board_id="nf_board")),
    )

    result = server._setup_safety_research_phase(context)

    assert result.verified is True
    assert result.code == "setup/safety-sources-verified"
    assert result.details["aggregate_fingerprint"] == "current-reviewed-aggregate"


def test_fresh_setup_rejects_family_only_mcu_before_profile_commit() -> None:
    datasheet = Path("Nano_BLE_MCU-nRF52840_PS_v1.1.pdf").resolve()
    context = cast(
        SetupPhaseContext,
        SimpleNamespace(
            user_input=SimpleNamespace(
                board_id="nrf_board",
                board_type="nrf52840dk",
                mcu_part_number="nRF52840",
                datasheet_path=str(datasheet),
                datasheet_sha256="unused-because-package-check-runs-first",
            )
        ),
    )

    result = server._setup_connection_phase(context)

    assert result.verified is False
    assert result.code == "setup/catalog-evidence-mismatch"
    assert "exact reviewed package" in result.agent_prompt
    assert "do not guess" in result.agent_prompt.casefold()


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
        requires_uart=False,
        board_type="nrf52840dk",
        datasheet_path=str(datasheet),
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
        requires_uart=False,
        board_type="nrf52840dk",
        datasheet_path=str(datasheet),
    )
    preflight = PreflightDecision(
        "preflight_ready",
        "setup/preflight-ready",
        "ready",
        selected_probe=ProbeCandidate("opaque-probe", "Probe", "jlink", "opaque-probe"),
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
    assert committed.board.silicon_id_label == "silicon_id"
    assert committed.board.silicon_id_addr == 0x10000100
    assert committed.board.silicon_id_expected == 0x00052840
    assert committed.board.silicon_id_mask == 0xFFFFFFFF


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
        "board_type": "nrf52840dk",
        "mcu_part_number": "nRF52840-QIAA",
        "serial_baudrate": 115200,
        "serial_id": "UART-001",
        "datasheet_path": datasheet,
        "datasheet_sha256": None,
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
    assert committed.board.silicon_id_label == "silicon_id"
    assert committed.board.silicon_id_addr == 0x10000100
    assert committed.board.silicon_id_expected == 0x00052840
    assert committed.board.silicon_id_mask == 0xFFFFFFFF
