from __future__ import annotations

from pathlib import Path

import pytest

from pyocd_debug_mcp.firmstore.store import FirmStore
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
from pyocd_debug_mcp.safety.refresh import SafetyRefreshRequest, SafetyRefresher
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


def _request(board_id: str = "board", *, application_end: int = 0x08008000) -> SafetyMapBuildRequest:
    return SafetyMapBuildRequest(
        board_id,
        MapIdentity("STM32L476RGT6", "stm32l4", "nucleo_l476rg"),
        {
            "schema_version": 2,
            "board_id": board_id,
            "mcu_part_number": "STM32L476RGT6",
            "pyocd_target": "stm32l4",
            "display_name": "ignored",
        },
        {"pack": "reviewed"},
        {"datasheet": "reviewed", "policy": application_end},
        MapGeometry(
            AddressRange(0x08000000, 0x08100000),
            AddressRange(0x20000000, 0x20018000),
            erase_origin=0x08000000,
            erase_size=0x800,
        ),
        MapPartitions(AddressRange(0x08000000, application_end)),
        (
            _contribution("physical flash", RegionKind.PHYSICAL_FLASH, 0x08000000, 0x08100000),
            _contribution("physical RAM", RegionKind.PHYSICAL_RAM, 0x20000000, 0x20018000),
            _contribution("RAM", RegionKind.RAM, 0x20000000, 0x20018000),
            _contribution("peripherals", RegionKind.PERIPHERAL, 0x40000000, 0x60000000),
            _contribution("system", RegionKind.CPU_SYSTEM, 0xE0000000, 0xE0100000),
            _contribution("option bytes", RegionKind.PROHIBITED, 0x1FFF7800, 0x1FFF7820),
        ),
    )


def _refresher(
    root: Path,
    *,
    live: bool = False,
    application_end: int = 0x08008000,
    commits: list[tuple[str, str, bool]] | None = None,
) -> SafetyRefresher:
    builder = SafetyMapBuilder(FirmStore(root))
    return SafetyRefresher(
        FirmStore(root),
        derive=lambda board: builder.derive(_request(board, application_end=application_end)),
        has_live_identity=lambda _board: live,
        on_commit=(
            (lambda board, digest, identity_changed: commits.append(
                (board, digest, identity_changed)
            ))
            if commits is not None
            else None
        ),
    )


def test_refresh_creates_first_map_and_reports_validation_required(tmp_path: Path) -> None:
    result = _refresher(tmp_path).refresh(SafetyRefreshRequest("board", "refresh-1"))

    assert result.status == "safety_refresh_completed"
    assert result.changed_groups == ("missing_or_invalid_map",)
    assert result.validation_required is True
    assert result.remedy == ("board_validate",)
    assert SafetyMapRepository(FirmStore(tmp_path)).load_current("board").canonical_digest == (
        result.map_digest
    )


@pytest.mark.parametrize(
    "content",
    ["not: [valid", "schema_version: 1\nboard_id: board\n"],
)
def test_malformed_and_old_maps_follow_the_same_refresh_path(
    tmp_path: Path, content: str
) -> None:
    repository = SafetyMapRepository(FirmStore(tmp_path))
    repository.path("board").parent.mkdir(parents=True)
    repository.path("board").write_text(content, encoding="utf-8")

    result = _refresher(tmp_path).refresh(SafetyRefreshRequest("board", "refresh-2"))

    assert result.status == "safety_refresh_completed"
    assert result.changed_groups == ("missing_or_invalid_map",)
    assert repository.load_current("board").board_id == "board"


def test_refresh_rederives_complete_candidate_and_classifies_change(tmp_path: Path) -> None:
    first = _refresher(tmp_path, application_end=0x08008000)
    first.refresh(SafetyRefreshRequest("board", "refresh-1"))

    second = _refresher(tmp_path, application_end=0x08010000)
    result = second.refresh(SafetyRefreshRequest("board", "refresh-2"))

    assert "partitions" in result.changed_groups
    assert "reviewed_official_evidence" in result.changed_groups
    assert result.drift_classification == "stable_authority_change"
    assert SafetyMapRepository(FirmStore(tmp_path)).load_current(
        "board"
    ).partitions.application == AddressRange(0x08000000, 0x08010000)


def test_refresh_preserves_live_identity_and_updates_only_map_stamp_hook(tmp_path: Path) -> None:
    commits: list[tuple[str, str, bool]] = []
    result = _refresher(tmp_path, live=True, commits=commits).refresh(
        SafetyRefreshRequest("board", "refresh-live")
    )

    assert result.validation_required is False
    assert result.remedy == ()
    assert commits == [("board", result.map_digest, False)]


def test_identity_change_is_explicit_to_gate_commit_hook(tmp_path: Path) -> None:
    repository = SafetyMapRepository(FirmStore(tmp_path))
    builder = SafetyMapBuilder(repository)
    old = builder.derive(_request())
    repository.commit("board", old)
    changed_request = _request()
    changed = builder.derive(changed_request)
    object.__setattr__(
        changed,
        "identity",
        MapIdentity("STM32L476RET6", "stm32l4", "nucleo_l476rg"),
    )
    commits: list[tuple[str, str, bool]] = []
    refresher = SafetyRefresher(
        FirmStore(tmp_path),
        derive=lambda _board: changed,
        has_live_identity=lambda _board: True,
        on_commit=lambda board, digest, identity_changed: commits.append(
            (board, digest, identity_changed)
        ),
    )

    refresher.refresh(SafetyRefreshRequest("board", "identity-change"))

    assert commits[0][2] is True


def test_failed_derivation_never_replaces_current_map(tmp_path: Path) -> None:
    repository = SafetyMapRepository(FirmStore(tmp_path))
    original = SafetyMapBuilder(repository).build(_request())

    def fail(_board: str):
        raise SafetyMapError("reviewed source disappeared")

    result = SafetyRefresher(FirmStore(tmp_path), derive=fail).refresh(
        SafetyRefreshRequest("board", "blocked")
    )

    assert result.status == "safety_refresh_blocked"
    assert result.remedy[-1] == "board_safety_refresh"
    assert repository.load_current("board") == original
