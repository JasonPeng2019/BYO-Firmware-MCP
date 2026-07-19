from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from pyocd_debug_mcp import server
from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.firmstore.profiles import ProfileRepository
from pyocd_debug_mcp.guardrails.gate import GateManager
from pyocd_debug_mcp.safety.map_build import (
    MapGeometry,
    MapIdentity,
    MapPartitions,
    RegionContribution,
    RegionSource,
    SafetyMapBuildRequest,
    SafetyMapBuilder,
    SafetyMapError,
    SafetyMapRepository,
)
from pyocd_debug_mcp.safety.refresh import SafetyRefresher
from pyocd_debug_mcp.safety.regions import (
    AddressRange,
    Provenance,
    RegionKind,
    SafetyRegion,
    SourceAuthority,
)


def _contribution(name: str, kind: RegionKind, start: int, end: int) -> RegionContribution:
    return RegionContribution(
        SafetyRegion(
            name,
            kind,
            AddressRange(start, end),
            (Provenance(SourceAuthority.RECONCILED, "reviewed", "test authority"),),
        ),
        (RegionSource.REVIEWED_DEVICE_SUPPORT, RegionSource.REVIEWED_OFFICIAL_EVIDENCE),
    )


def _request(board_id: str) -> SafetyMapBuildRequest:
    return SafetyMapBuildRequest(
        board_id,
        MapIdentity("STM32L476RGT6", "stm32l476rgtx", "nucleo_l476rg"),
        {
            "schema_version": 2,
            "board_id": board_id,
            "mcu_part_number": "STM32L476RGT6",
            "pyocd_target": "stm32l476rgtx",
        },
        {"pack": "reviewed"},
        {"datasheet": "reviewed", "partition_policy": "reviewed"},
        MapGeometry(
            AddressRange(0x08000000, 0x08100000),
            AddressRange(0x20000000, 0x20018000),
            erase_origin=0x08000000,
            erase_size=0x800,
        ),
        MapPartitions(AddressRange(0x08000000, 0x08008000)),
        (
            _contribution("physical flash", RegionKind.PHYSICAL_FLASH, 0x08000000, 0x08100000),
            _contribution("physical RAM", RegionKind.PHYSICAL_RAM, 0x20000000, 0x20018000),
            _contribution("RAM", RegionKind.RAM, 0x20000000, 0x20018000),
            _contribution("peripherals", RegionKind.PERIPHERAL, 0x40000000, 0x60000000),
            _contribution("system", RegionKind.CPU_SYSTEM, 0xE0000000, 0xE0100000),
            _contribution("option bytes", RegionKind.PROHIBITED, 0x1FFF7800, 0x1FFF7820),
        ),
    )


def _install_fresh_refresher(monkeypatch: pytest.MonkeyPatch, root: Path) -> SafetyMapRepository:
    store = FirmStore(root)
    repository = SafetyMapRepository(store)
    builder = SafetyMapBuilder(repository)
    monkeypatch.setattr(
        server,
        "_safety_refresher",
        SafetyRefresher(store, derive=lambda board_id: builder.derive(_request(board_id))),
    )
    monkeypatch.setattr(server, "_safety_continuation", lambda _prefix: "refresh-test")
    return repository


def test_public_refresh_schema_accepts_only_board_id() -> None:
    signature = inspect.signature(server._run_board_safety_refresh)

    assert tuple(signature.parameters) == ("board_id",)
    assert all(
        name not in signature.parameters
        for name in (
            "application_elf",
            "application_hex",
            "application_map",
            "bootloader_elf",
            "bootloader_hex",
            "bootloader_map",
            "allowed_ranges",
        )
    )


def test_post_refresh_hook_associates_profile_with_canonical_map(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FirmStore(tmp_path)
    profiles = ProfileRepository(store, legacy_board_dir=tmp_path / "legacy")
    profiles.commit_core(
        profiles.stage_core(
            {
                "board_id": "generic_board",
                "display_name": "Generic board",
                "mcu_part_number": "GENERIC-123",
                "mcu_family": "generic",
                "probe_family": "cmsis-dap",
                "pyocd_target": "generic_123",
            }
        )
    )
    monkeypatch.setattr(server, "_firm_store", store)
    monkeypatch.setattr(server, "_profile_repository", profiles)

    server._restamp_after_refresh("generic_board", "map-digest", False)

    refreshed = profiles.load("generic_board", include_legacy=False)
    assert refreshed.safety_ref == ".firm/safety/generic_board/memory_map.yaml"
    assert refreshed.mcu_part_number == "GENERIC-123"
    assert refreshed.board.pyocd_target == "generic_123"


def test_public_refresh_creates_first_map_as_the_only_safety_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _install_fresh_refresher(monkeypatch, tmp_path)

    payload = server._run_board_safety_refresh("board")

    assert payload["status"] == "safety_refresh_completed"
    observed = payload["observed"]
    assert isinstance(observed, dict)
    assert observed["changed_groups"] == ["missing_or_invalid_map"]
    assert observed["validation_required"] is True
    assert payload["validation_plan"] == ["board_validate"]
    document = repository.load_current("board")
    assert observed["map_digest"] == document.canonical_digest
    assert [path.name for path in repository.path("board").parent.iterdir()] == [
        "memory_map.yaml"
    ]


@pytest.mark.parametrize(
    "content",
    ("not: [valid", "schema_version: 1\nboard_id: board\n"),
)
def test_public_refresh_rebuilds_malformed_and_old_maps_without_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
) -> None:
    repository = _install_fresh_refresher(monkeypatch, tmp_path)
    repository.path("board").parent.mkdir(parents=True)
    repository.path("board").write_text(content, encoding="utf-8")

    payload = server._run_board_safety_refresh("board")

    assert payload["status"] == "safety_refresh_completed"
    observed = payload["observed"]
    assert isinstance(observed, dict)
    assert observed["changed_groups"] == ["missing_or_invalid_map"]
    assert "board_safety_setup" not in str(payload)
    assert repository.load_current("board").to_document()["schema_version"] == 2


def test_ordinary_artifact_bytes_are_not_refresh_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _install_fresh_refresher(monkeypatch, tmp_path)
    first = server._run_board_safety_refresh("board")
    artifact = tmp_path / "build" / "firmware.elf"
    artifact.parent.mkdir()
    artifact.write_bytes(b"first build")

    second = server._run_board_safety_refresh("board")
    artifact.write_bytes(b"different ordinary rebuild")
    third = server._run_board_safety_refresh("board")

    first_observed = first["observed"]
    second_observed = second["observed"]
    third_observed = third["observed"]
    assert isinstance(first_observed, dict)
    assert isinstance(second_observed, dict)
    assert isinstance(third_observed, dict)
    assert second_observed["changed_groups"] == []
    assert third_observed["changed_groups"] == []
    assert (
        first_observed["map_digest"]
        == second_observed["map_digest"]
        == third_observed["map_digest"]
    )
    assert (
        repository.load_current("board").canonical_digest == first_observed["map_digest"]
    )


def test_policy_currentness_rejects_semantic_profile_drift_until_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = SafetyMapBuilder(SafetyMapRepository(FirmStore(tmp_path)))
    current = builder.derive(_request("board"))
    request = _request("board")
    changed = builder.derive(
        SafetyMapBuildRequest(
            request.board_id,
            request.identity,
            {**request.profile, "mcu_family": "changed-semantic-value"},
            request.reviewed_device_support,
            request.reviewed_official_evidence,
            request.geometry,
            request.partitions,
            request.regions,
        )
    )
    monkeypatch.setattr(server, "require_reconciled_authority", lambda _document: None)
    monkeypatch.setattr(server, "_derive_reviewed_safety_map", lambda _board: current)
    server._require_current_reviewed_map(current)

    monkeypatch.setattr(server, "_derive_reviewed_safety_map", lambda _board: changed)
    with pytest.raises(SafetyMapError, match="semantic profile"):
        server._require_current_reviewed_map(current)


def test_refresh_live_identity_provider_requires_the_current_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gates = GateManager()
    gates.stamp_validation(
        board_id="board",
        connection_id="old-connection",
        probe_identity="probe",
        observed_mcu="STM32L476RGT6",
        validation_run="validation-old",
        map_digest="digest",
    )
    monkeypatch.setattr(server, "gate_manager", gates)
    monkeypatch.setattr(
        server.connection_manager,
        "maybe_connection",
        lambda _board: SimpleNamespace(connection_id="new-connection"),
    )

    assert server._has_current_live_identity("board") is False

    monkeypatch.setattr(
        server.connection_manager,
        "maybe_connection",
        lambda _board: SimpleNamespace(connection_id="old-connection"),
    )
    assert server._has_current_live_identity("board") is True
