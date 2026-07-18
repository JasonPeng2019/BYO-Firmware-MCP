from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.board_config import (
    RECOVER_MODE_MANUAL_ONLY,
    RECOVER_MODE_BACKEND_MASS_ERASE,
)
from pyocd_debug_mcp.firmstore.profiles import ProfileRepository
from pyocd_debug_mcp.firmstore.reports import ReportWriter
from pyocd_debug_mcp.firmstore.store import (
    PERSISTED_AUTHORITY_KEYS,
    FirmStore,
    ImmutableArtifactError,
)
from pyocd_debug_mcp.guardrails.gate import GateManager, GateRefusal
from pyocd_debug_mcp.guardrails.permissions import (
    GrantMode,
    PermissionGrant,
    PermissionStore,
)
from pyocd_debug_mcp.guardrails.plan_defs import PLAN_DEFINITIONS
from pyocd_debug_mcp.guardrails.plan_engine import PlanEngine, PlanRefusal
from pyocd_debug_mcp.kernel.registry import ToolRegistry
from pyocd_debug_mcp.kernel.run_state import ServerRun
from pyocd_debug_mcp.safety.map_build import (
    MapGeometry,
    MapIdentity,
    MapPartitions,
    RegionContribution,
    RegionSource,
    SafetyMapBuildRequest,
    SafetyMapBuilder,
    SafetyMapRepository,
)
from pyocd_debug_mcp.safety.regions import (
    AddressRange,
    Provenance,
    RegionKind,
    SafetyRegion,
    SourceAuthority,
)
from pyocd_debug_mcp.tools.unlock import (
    NO_INTERNALS,
    UnlockCoordinator,
    UnlockToolServices,
)

BOARD_ID = "nrf_board"
SESSION_ID = "session-1"


def _contribution(
    name: str,
    kind: RegionKind,
    start: int,
    end: int,
    *groups: RegionSource,
) -> RegionContribution:
    return RegionContribution(
        SafetyRegion(
            name,
            kind,
            AddressRange(start, end),
            (Provenance(SourceAuthority.RECONCILED, f"test:{name}", "Task 15 fixture"),),
            kind in {RegionKind.APPLICATION_FLASH, RegionKind.BOOTLOADER_FLASH},
        ),
        groups,
    )


def _complete_fields(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "board_id": BOARD_ID,
        "hypothesis": "Debug protection is the sole cause of the failed attachment.",
        "hypothesis_made": True,
        "strategy": "Use the documented Nordic recovery primitive exactly once.",
        "strategy_evaluated": True,
        "expected_fail_return": "Recovery reports a typed backend failure and stays closed.",
        "expected_success_return": "Recovery erases the target and requires validation.",
        "max_calls": 1,
        "max_calls_buffer": 0,
        "user_permission": None,
        "action_parameters": {
            "recovery_mechanism": RECOVER_MODE_BACKEND_MASS_ERASE,
        },
    }
    raw_parameters = values["action_parameters"]
    assert isinstance(raw_parameters, dict)
    parameters: dict[str, object] = dict(raw_parameters)
    for name, value in changes.items():
        if name == "recovery_mechanism":
            parameters[name] = value
        else:
            values[name] = value
    values["action_parameters"] = parameters
    return values


@dataclass
class UnlockFixture:
    coordinator: UnlockCoordinator
    engine: PlanEngine
    registry: ToolRegistry
    gate: GateManager
    handle: TargetSessionHandle
    state: dict[str, str]
    backend_calls: list[str]
    store: FirmStore
    permissions: PermissionStore


def _fixture(
    tmp_path: Path,
    *,
    recover_mode: str | None = RECOVER_MODE_BACKEND_MASS_ERASE,
    backend_failure: bool = False,
    backend_supports_recovery: bool = True,
    geometry: object | None = None,
) -> UnlockFixture:
    store = FirmStore(tmp_path)
    profiles = ProfileRepository(store, legacy_board_dir=tmp_path / "legacy")
    profile = profiles.commit_core(
        profiles.stage_core(
            {
                "board_id": BOARD_ID,
                "display_name": "Nordic Test Board",
                "mcu_part_number": "nRF52833-QIAA",
                "mcu_family": "nrf52833",
                "probe_family": "jlink",
                "pyocd_target": "nrf52833",
                "requires_recover_validation": recover_mode is not None,
                **({"recover_mode": recover_mode} if recover_mode is not None else {}),
            }
        )
    )
    regions = (
        _contribution(
            "physical flash",
            RegionKind.PHYSICAL_FLASH,
            0,
            0x10000,
            RegionSource.REVIEWED_DEVICE_SUPPORT,
            RegionSource.REVIEWED_OFFICIAL_EVIDENCE,
            RegionSource.GEOMETRY,
        ),
        _contribution(
            "physical RAM",
            RegionKind.PHYSICAL_RAM,
            0x20000000,
            0x20010000,
            RegionSource.REVIEWED_DEVICE_SUPPORT,
            RegionSource.GEOMETRY,
        ),
        _contribution(
            "RAM",
            RegionKind.RAM,
            0x20000000,
            0x20010000,
            RegionSource.REVIEWED_DEVICE_SUPPORT,
        ),
        _contribution(
            "UICR and protection configuration",
            RegionKind.PROHIBITED,
            0x10001000,
            0x10001400,
            RegionSource.REVIEWED_DEVICE_SUPPORT,
            RegionSource.REVIEWED_OFFICIAL_EVIDENCE,
        ),
    )
    erase_size = (
        int(geometry["erase_size"])
        if isinstance(geometry, dict) and "erase_size" in geometry
        else 0x1000
    )
    result = SafetyMapBuilder(store).build(
        SafetyMapBuildRequest(
            BOARD_ID,
            MapIdentity("nRF52833-QIAA", "nrf52833", "nrf_test"),
            profile.to_document(),
            {"target": "nrf52833", "pack": "Nordic.nRF_DeviceFamilyPack@8.58.0"},
            {"datasheet": "nRF52833 PS", "partition_policy": "reviewed-test"},
            MapGeometry(
                AddressRange(0, 0x10000),
                AddressRange(0x20000000, 0x20010000),
                erase_origin=0,
                erase_size=erase_size,
            ),
            MapPartitions(
                application=AddressRange(0x2000, 0x10000),
                bootloader=AddressRange(0, 0x2000),
            ),
            regions,
        )
    )
    map_digest = result.canonical_digest

    run = ServerRun(run_id="run-task-15")
    registry = ToolRegistry()
    registry.register(
        "target_unlock", hidden=True, locked=True, prerequisite="target_unlock-plan"
    )
    permissions = PermissionStore(run)
    engine = PlanEngine(run, registry, permission_provider=permissions)
    permissions.set_revocation_handler(engine.invalidate)
    gate = GateManager(run.gates)
    gate.stamp_validation(
        board_id=BOARD_ID,
        connection_id="connection-1",
        probe_identity="probe-1",
        observed_mcu="nRF52833-QIAA",
        validation_run="validation-task-15",
        map_digest=map_digest,
    )
    target = SimpleNamespace(part_number="nRF52833-QIAA")
    handle = TargetSessionHandle(
        SimpleNamespace(target=target),
        profile.board,
        "probe-1",
        "profile",
        None,
    )
    state = {
        "connection_id": "connection-1",
        "session_id": SESSION_ID,
        "fingerprint": map_digest,
    }
    backend_calls: list[str] = []

    def recover(selected: TargetSessionHandle, mechanism: str) -> str:
        del selected
        backend_calls.append(mechanism)
        if backend_failure:
            raise RuntimeError("typed recovery backend failed")
        return "typed recovery"

    def revoke_unlock_permission(board_id: str, reason: str) -> None:
        permissions.revoke("target_unlock", board_id, reason=reason)

    coordinator = UnlockCoordinator(
        UnlockToolServices(
            run,
            engine,
            profiles,
            SafetyMapRepository(store),
            ReportWriter(store),
            gate,
            lambda board_id: handle,
            lambda board_id: state["connection_id"],
            lambda board_id: state["session_id"],
            lambda board_id: state["fingerprint"],
            lambda handle, mechanism: (
                backend_supports_recovery
                and mechanism == RECOVER_MODE_BACKEND_MASS_ERASE
            ),
            recover,
            lambda board_id: None,
            revoke_unlock_permission,
        )
    )
    return UnlockFixture(
        coordinator, engine, registry, gate, handle, state, backend_calls, store, permissions
    )


def _initialize(fixture: UnlockFixture) -> None:
    fields = {
        field.name: None for field in PLAN_DEFINITIONS["target_unlock"].null_fields
    }
    response = fixture.coordinator.plan(fields)
    assert "Plan initialization for target_unlock-plan" in response


def _approve(fixture: UnlockFixture, fields: dict[str, object] | None = None) -> str:
    _initialize(fixture)
    planned = fields or _complete_fields()
    request = json.loads(fixture.coordinator.plan(planned))
    approved = fixture.coordinator.plan(planned | {"user_permission": "one-time"})
    assert json.loads(approved)["plan_id"] == request["plan_id"]
    return request["plan_id"]


def test_ac_15_1_fixed_one_zero_budget(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _initialize(fixture)

    with pytest.raises(PlanRefusal, match="max_calls=1"):
        fixture.coordinator.plan(_complete_fields(max_calls=2))
    with pytest.raises(PlanRefusal, match="max_calls_buffer=0"):
        fixture.coordinator.plan(_complete_fields(max_calls_buffer=1))


def test_ac_15_2_and_15_3_permission_payload_is_complete_and_relayable(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    _initialize(fixture)

    payload = json.loads(fixture.coordinator.plan(_complete_fields()))

    assert payload["status"] == "unlock_permission_requested"
    assert payload["plan_id"].startswith("plan-")
    assert payload["live_identity"] == {
        "run_id": "run-task-15",
        "board_id": BOARD_ID,
        "display_name": "Nordic Test Board",
        "mcu_part_number": "nRF52833-QIAA",
        "live_target_part": "nRF52833-QIAA",
        "pyocd_target": "nrf52833",
        "probe_identity": "probe-1",
        "connection_id": "connection-1",
        "map_digest": fixture.state["fingerprint"],
    }
    assert payload["mechanism"]["vendor"] == "connected target backend"
    assert payload["mechanism"]["mass_erase"] is True
    disclosure = payload["disclosure"]
    assert disclosure["all_nonvolatile_erased"] is True
    assert disclosure["erased_ranges"] == [
        {
            "start": 0,
            "end": 0x10000,
            "bank": "physical flash",
            "first_sector": 0,
            "last_sector": 15,
            "sectors": list(range(16)),
        }
    ]
    assert {item["name"] for item in disclosure["affected_regions"]} == {
        "physical flash",
        "application",
        "bootloader",
        "UICR and protection configuration",
    }
    assert {
        "application firmware",
        "user bootloader firmware",
        "nonvolatile configuration, protection, provisioning, and user settings",
        "all user data in addressable nonvolatile memory",
    } <= set(payload["expected_losses"])
    prompt = payload["agent_prompt"]
    for required in (
        "entire addressable nonvolatile memory",
        "sectors 0-15",
        "Mass erase: yes",
        payload["plan_id"],
        NO_INTERNALS,
    ):
        assert required in prompt
    assert Path(payload["report"]).is_file()


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("hypothesis", "A different cause now explains the failed attachment."),
        ("hypothesis_made", False),
        ("strategy", "Use a different documented recovery strategy."),
        ("strategy_evaluated", False),
        ("expected_fail_return", "A different failure is now expected."),
        ("expected_success_return", "A different success is now expected."),
        ("max_calls", 2),
        ("max_calls_buffer", 1),
        ("recovery_mechanism", "arbitrary_register_write"),
    ],
)
def test_ac_15_4_any_changed_plan_field_voids_approval_handshake(
    tmp_path: Path, field_name: str, replacement: object
) -> None:
    fixture = _fixture(tmp_path)
    _initialize(fixture)
    fixture.coordinator.plan(_complete_fields())

    with pytest.raises(PlanRefusal) as caught:
        fixture.coordinator.plan(
            _complete_fields(**{field_name: replacement}, user_permission="one-time")
        )

    assert caught.value.code == "unlock/plan-changed"
    with pytest.raises(PlanRefusal, match="disclosure"):
        fixture.coordinator.plan(_complete_fields(user_permission="one-time"))


def test_ac_5_7_and_15_5_full_session_never_authorizes_or_carries_forward(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    fixture.permissions.server_run.permissions[("target_unlock", BOARD_ID)] = PermissionGrant(
        "permission-prior-full-session",
        fixture.permissions.server_run.run_id,
        "target_unlock",
        BOARD_ID,
        GrantMode.FULL_SESSION,
        "2026-01-01T00:00:00Z",
    )
    _initialize(fixture)

    with pytest.raises(PlanRefusal) as caught:
        fixture.coordinator.plan(_complete_fields(user_permission="full-session"))
    assert caught.value.code == "permission/fresh-one-time-required"

    first = json.loads(fixture.coordinator.plan(_complete_fields()))
    assert first["status"] == "unlock_permission_requested"
    assert fixture.permissions.active_grant("target_unlock", BOARD_ID) is None
    fixture.coordinator.plan(_complete_fields(user_permission="one-time"))
    fixture.coordinator.validate_execution(
        BOARD_ID, {"recovery_mechanism": RECOVER_MODE_BACKEND_MASS_ERASE}
    )
    fixture.engine.enforce(
        "target_unlock",
        BOARD_ID,
        {"recovery_mechanism": RECOVER_MODE_BACKEND_MASS_ERASE},
        session_id=SESSION_ID,
    )
    fixture.coordinator.execute(BOARD_ID, RECOVER_MODE_BACKEND_MASS_ERASE)

    second = json.loads(fixture.coordinator.plan(_complete_fields()))
    assert second["status"] == "unlock_permission_requested"
    assert second["plan_id"]
    assert fixture.registry.definition("target_unlock").locked_by_default
    with pytest.raises(PlanRefusal, match="field changed"):
        fixture.coordinator.plan(_complete_fields(user_permission="one-time", strategy="changed"))


def test_invalid_map_fails_closed_before_permission(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    SafetyMapRepository(fixture.store).path(BOARD_ID).write_text(
        "schema_version: 1\n", encoding="utf-8"
    )
    _initialize(fixture)

    with pytest.raises(PlanRefusal) as caught:
        fixture.coordinator.plan(_complete_fields())

    assert caught.value.code == "unlock/safety-map-invalid"
    assert "board_safety_refresh" in str(caught.value)
    assert fixture.backend_calls == []
    assert fixture.permissions.active_grant("target_unlock", BOARD_ID) is None
    assert list(fixture.store.layout.validation.glob("target-unlock-*/report.json")) == []


def test_replacement_plan_invalidates_prior_approved_plan(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    first_plan = _approve(fixture)
    assert fixture.registry.is_unlocked("target_unlock", BOARD_ID)

    replacement = json.loads(
        fixture.coordinator.plan(
            _complete_fields(strategy="Use a newly reviewed documented recovery strategy.")
        )
    )

    assert replacement["plan_id"] != first_plan
    assert fixture.engine.active_plan("target_unlock", BOARD_ID) is None
    assert fixture.permissions.active_grant("target_unlock", BOARD_ID) is None
    assert not fixture.registry.is_unlocked("target_unlock", BOARD_ID)
    with pytest.raises(PlanRefusal, match="No active"):
        fixture.coordinator.validate_execution(
            BOARD_ID, {"recovery_mechanism": RECOVER_MODE_BACKEND_MASS_ERASE}
        )


@pytest.mark.parametrize("change", ["target", "probe", "connection", "map"])
def test_ac_15_6_live_binding_change_invalidates_before_execution(
    tmp_path: Path, change: str
) -> None:
    fixture = _fixture(tmp_path)
    _approve(fixture)
    if change == "target":
        fixture.handle.session.target.part_number = "nRF52840-QIAA"
    elif change == "probe":
        fixture.handle.probe_uid = "probe-2"
    elif change == "connection":
        fixture.state["connection_id"] = "connection-2"
    else:
        fixture.state["fingerprint"] = "f" * 64

    with pytest.raises(PlanRefusal) as caught:
        fixture.coordinator.validate_execution(
            BOARD_ID, {"recovery_mechanism": RECOVER_MODE_BACKEND_MASS_ERASE}
        )

    assert caught.value.code == "unlock/binding-changed"
    assert fixture.backend_calls == []
    assert "target_unlock" not in fixture.registry.advertised()
    assert fixture.permissions.active_grant("target_unlock", BOARD_ID) is None


def test_cross_board_approval_never_transfers(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _approve(fixture)
    other_board = "other_board"
    parameters = {"recovery_mechanism": RECOVER_MODE_BACKEND_MASS_ERASE}

    assert fixture.registry.is_unlocked("target_unlock", BOARD_ID)
    assert not fixture.registry.is_unlocked("target_unlock", other_board)
    with pytest.raises(PlanRefusal) as caught:
        fixture.coordinator.validate_execution(other_board, parameters)

    assert caught.value.code == "unlock/approval-inactive"
    assert fixture.permissions.active_grant("target_unlock", other_board) is None
    assert fixture.backend_calls == []
    assert fixture.engine.active_plan("target_unlock", BOARD_ID) is not None


def test_expired_and_restarted_runs_restore_no_unlock_authority(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _approve(fixture)
    assert fixture.registry.is_unlocked("target_unlock", BOARD_ID)

    fixture.engine.close_run()

    assert fixture.engine.active_plan("target_unlock", BOARD_ID) is None
    assert fixture.permissions.active_grant("target_unlock", BOARD_ID) is None
    assert not fixture.registry.is_unlocked("target_unlock", BOARD_ID)
    with pytest.raises(PlanRefusal, match="No active"):
        fixture.coordinator.validate_execution(
            BOARD_ID, {"recovery_mechanism": RECOVER_MODE_BACKEND_MASS_ERASE}
        )

    restarted_run = ServerRun(run_id="run-task-15-restarted")
    restarted_registry = ToolRegistry()
    restarted_registry.register(
        "target_unlock", hidden=True, locked=True, prerequisite="target_unlock-plan"
    )
    restarted_permissions = PermissionStore(restarted_run)
    restarted_engine = PlanEngine(
        restarted_run, restarted_registry, permission_provider=restarted_permissions
    )
    restarted_permissions.set_revocation_handler(restarted_engine.invalidate)

    def revoke_restarted(board_id: str, reason: str) -> None:
        restarted_permissions.revoke("target_unlock", board_id, reason=reason)

    old = fixture.coordinator.services
    restarted = UnlockCoordinator(
        UnlockToolServices(
            restarted_run,
            restarted_engine,
            old.profiles,
            old.safety_repository,
            old.reports,
            GateManager(restarted_run.gates),
            old.handle_for,
            old.connection_id_for,
            old.session_id_for,
            old.current_map_digest,
            old.supports_recovery,
            old.recover_target,
            old.mark_recover_completed,
            revoke_restarted,
        )
    )
    with pytest.raises(PlanRefusal, match="No active"):
        restarted.validate_execution(
            BOARD_ID, {"recovery_mechanism": RECOVER_MODE_BACKEND_MASS_ERASE}
        )
    assert restarted_run.plans == {}
    assert restarted_run.permissions == {}
    assert restarted_run.gates == {}


def test_ac_15_7_execution_closes_gate_writes_report_and_consumes_once(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    plan_id = _approve(fixture)
    params = {"recovery_mechanism": RECOVER_MODE_BACKEND_MASS_ERASE}
    fixture.coordinator.validate_execution(BOARD_ID, params)
    fixture.engine.enforce(
        "target_unlock", BOARD_ID, params, session_id=SESSION_ID
    )

    response = fixture.coordinator.execute(BOARD_ID, RECOVER_MODE_BACKEND_MASS_ERASE)

    assert plan_id in response
    assert "board_validate" in response
    assert fixture.backend_calls == [RECOVER_MODE_BACKEND_MASS_ERASE]
    assert fixture.gate.snapshot(BOARD_ID) is None
    assert "target_unlock" not in fixture.registry.advertised()
    reports = list(fixture.store.layout.validation.glob("target-unlock-*/report.json"))
    completed = [json.loads(path.read_text(encoding="utf-8")) for path in reports]
    assert any(
        report["terminal_status"] == "unlock_completed_revalidation_required"
        for report in completed
    )
    with pytest.raises(PlanRefusal, match="no longer active"):
        fixture.coordinator.execute(BOARD_ID, RECOVER_MODE_BACKEND_MASS_ERASE)

    with pytest.raises(GateRefusal) as closed:
        fixture.gate.require_write(BOARD_ID, "connection-1", fixture.state["fingerprint"])
    assert closed.value.code == "gate/validation-required"
    fixture.gate.stamp_validation(
        board_id=BOARD_ID,
        connection_id="connection-1",
        probe_identity="probe-1",
        observed_mcu="nRF52833-QIAA",
        validation_run="validation-task-15-repeat",
        map_digest=fixture.state["fingerprint"],
    )
    assert (
        fixture.gate.require_write(BOARD_ID, "connection-1", fixture.state["fingerprint"])
        is not None
    )


def test_ac_15_8_only_typed_vendor_recovery_and_manual_only_refuses(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path / "typed")
    _initialize(fixture)
    with pytest.raises(PlanRefusal) as unsupported:
        fixture.coordinator.plan(_complete_fields(recovery_mechanism="arbitrary_register_write"))
    assert unsupported.value.code == "unlock/mechanism-unsupported"
    assert fixture.backend_calls == []

    manual = _fixture(tmp_path / "manual", recover_mode=RECOVER_MODE_MANUAL_ONLY)
    _initialize(manual)
    with pytest.raises(PlanRefusal) as refused:
        manual.coordinator.plan(_complete_fields())
    assert refused.value.code == "unlock/manual-only"
    assert manual.backend_calls == []


def test_recovery_capability_is_checked_before_disclosure_or_permission(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, backend_supports_recovery=False)
    _initialize(fixture)

    with pytest.raises(PlanRefusal) as refused:
        fixture.coordinator.plan(_complete_fields())

    assert refused.value.code == "unlock/mechanism-backend-unsupported"
    assert fixture.permissions.active_grant("target_unlock", BOARD_ID) is None
    assert fixture.backend_calls == []


def test_started_backend_failure_still_closes_gate_and_writes_attempt_report(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path, backend_failure=True)
    _approve(fixture)
    params = {"recovery_mechanism": RECOVER_MODE_BACKEND_MASS_ERASE}
    fixture.coordinator.validate_execution(BOARD_ID, params)
    fixture.engine.enforce("target_unlock", BOARD_ID, params, session_id=SESSION_ID)

    with pytest.raises(RuntimeError, match="backend failed"):
        fixture.coordinator.execute(BOARD_ID, RECOVER_MODE_BACKEND_MASS_ERASE)

    assert fixture.gate.snapshot(BOARD_ID) is None
    assert fixture.backend_calls == [RECOVER_MODE_BACKEND_MASS_ERASE]
    reports = list(fixture.store.layout.validation.glob("target-unlock-*/report.json"))
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in reports]
    assert any(
        report["terminal_status"] == "unlock_failed_revalidation_required"
        for report in payloads
    )


def test_unknown_mechanism_requests_research_without_authority(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, recover_mode=None)
    _initialize(fixture)

    payload = json.loads(
        fixture.coordinator.plan(_complete_fields(recovery_mechanism=None))
    )

    assert payload["status"] == "unlock_research_required"
    assert payload["requested_fields"] == ["recovery_mechanism", "vendor", "mass_erase"]
    assert NO_INTERNALS in payload["agent_prompt"]
    assert "target_unlock" not in fixture.registry.advertised()
    assert fixture.backend_calls == []


def test_target_unlock_attempt_report_is_immutable(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    paths = ReportWriter(fixture.store).create_target_unlock(
        "target-unlock-fixed", {"terminal_status": "failed"}
    )
    assert paths.report.is_file()
    with pytest.raises(ImmutableArtifactError):
        ReportWriter(fixture.store).create_target_unlock(
            "target-unlock-fixed", {"terminal_status": "completed"}
        )


def _artifact_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return {str(key).strip().lower().replace("-", "_") for key in value} | {
            nested
            for item in value.values()
            for nested in _artifact_keys(item)
        }
    if isinstance(value, list):
        return {nested for item in value for nested in _artifact_keys(item)}
    return set()


def test_unlock_reports_preserve_exact_disclosure_without_persisted_authority(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    _approve(fixture)
    parameters = {"recovery_mechanism": RECOVER_MODE_BACKEND_MASS_ERASE}
    fixture.coordinator.validate_execution(BOARD_ID, parameters)
    fixture.engine.enforce(
        "target_unlock", BOARD_ID, parameters, session_id=SESSION_ID
    )
    fixture.coordinator.execute(BOARD_ID, RECOVER_MODE_BACKEND_MASS_ERASE)

    report_paths = sorted(
        fixture.store.layout.validation.glob("target-unlock-*/report.json")
    )
    event_paths = sorted(
        fixture.store.layout.validation.glob("target-unlock-*/events.jsonl")
    )
    assert report_paths
    assert len(event_paths) == len(report_paths)
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in report_paths]
    permission_report = next(
        report
        for report in reports
        if report["terminal_status"] == "unlock_permission_requested"
    )
    assert permission_report["details"]["disclosure"]["erased_ranges"] == [
        {
            "start": 0,
            "end": 0x10000,
            "bank": "physical flash",
            "first_sector": 0,
            "last_sector": 15,
            "sectors": list(range(16)),
        }
    ]
    assert permission_report["details"]["live_identity"]["map_digest"]
    for path, report in zip(report_paths, reports, strict=True):
        assert PERSISTED_AUTHORITY_KEYS.isdisjoint(_artifact_keys(report)), path
        assert "user_permission" not in path.read_text(encoding="utf-8")
    for path in event_paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            assert PERSISTED_AUTHORITY_KEYS.isdisjoint(_artifact_keys(json.loads(line))), path


def test_approved_unlock_returns_exact_non_authority_static_client_fallback(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    _initialize(fixture)
    fields = _complete_fields()
    requested = json.loads(fixture.coordinator.plan(fields))
    approved = json.loads(
        fixture.coordinator.plan(fields | {"user_permission": "one-time"})
    )

    assert approved["plan_id"] == requested["plan_id"]
    assert approved["status"] == "unlock_plan_approved"
    assert approved["stable_client_fallback"] == {
        "tool_name": "action_batch",
        "arguments": {
            "board_id": BOARD_ID,
            "actions": [
                {
                    "tool_name": "target_unlock",
                    "arguments": {
                        "board_id": BOARD_ID,
                        "recovery_mechanism": RECOVER_MODE_BACKEND_MASS_ERASE,
                    },
                }
            ],
        },
    }
    assert "user_permission" not in json.dumps(approved["stable_client_fallback"])
