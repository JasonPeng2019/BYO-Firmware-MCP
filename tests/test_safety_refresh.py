from __future__ import annotations

import json
from pathlib import Path

import pytest

from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.safety.fingerprints import FingerprintInputs, FingerprintSource
from pyocd_debug_mcp.safety.map_build import (
    RegionContribution,
    SafetyArtifactRepository,
    SafetyMapBuilder,
    SafetySetupRequest,
)
from pyocd_debug_mcp.safety.refresh import SafetyRefresher, SafetyRefreshRequest
from pyocd_debug_mcp.safety.regions import (
    AddressRange,
    Provenance,
    RegionKind,
    SafetyRegion,
    SourceAuthority,
)


def inputs(**overrides: object) -> FingerprintInputs:
    values: dict[str, object] = {
        "profile": {"board_id": "board", "display_name": "Board"},
        "part_target": {"mcu_part_number": "MCU-1", "target": "target_1"},
        "pack": {"id": "Vendor.Pack", "version": "1.0"},
        "evidence": {"manual": "R2", "svd": "1.0"},
        "application_artifacts": {"elf": "app-v1"},
        "bootloader_artifacts": {"elf": "boot-v1"},
        "geometry": {"erase_size": 4096},
        "schema": {"memory_map": 1},
    }
    values.update(overrides)
    return FingerprintInputs(
        values["profile"],
        values["part_target"],
        values["pack"],
        values["evidence"],
        values["application_artifacts"],
        values["bootloader_artifacts"],
        values["geometry"],
        values["schema"],
    )


def contribution(
    name: str,
    kind: RegionKind,
    start: int,
    end: int,
    *groups: FingerprintSource,
) -> RegionContribution:
    authority = (
        SourceAuthority.BUILD
        if set(groups).intersection(
            {
                FingerprintSource.APPLICATION_ARTIFACTS,
                FingerprintSource.BOOTLOADER_ARTIFACTS,
            }
        )
        else SourceAuthority.RECONCILED
    )
    return RegionContribution(
        SafetyRegion(
            name,
            kind,
            AddressRange(start, end),
            (Provenance(authority, f"source:{name}", "verified refresh evidence"),),
            kind in {RegionKind.APPLICATION_FLASH, RegionKind.BOOTLOADER_FLASH},
        ),
        groups,
    )


def regions() -> tuple[RegionContribution, ...]:
    return (
        contribution(
            "physical flash",
            RegionKind.PHYSICAL_FLASH,
            0,
            0x10000,
            FingerprintSource.PACK,
            FingerprintSource.EVIDENCE,
            FingerprintSource.GEOMETRY,
        ),
        contribution(
            "application",
            RegionKind.APPLICATION_FLASH,
            0x2000,
            0x8000,
            FingerprintSource.APPLICATION_ARTIFACTS,
        ),
        contribution(
            "bootloader",
            RegionKind.BOOTLOADER_FLASH,
            0,
            0x2000,
            FingerprintSource.BOOTLOADER_ARTIFACTS,
        ),
        contribution(
            "RAM",
            RegionKind.RAM,
            0x20000000,
            0x20001000,
            FingerprintSource.APPLICATION_ARTIFACTS,
        ),
        contribution(
            "peripheral",
            RegionKind.PERIPHERAL,
            0x40000000,
            0x50000000,
            FingerprintSource.PACK,
            FingerprintSource.EVIDENCE,
        ),
        contribution(
            "security",
            RegionKind.PROHIBITED,
            0x40001000,
            0x40001100,
            FingerprintSource.PACK,
            FingerprintSource.EVIDENCE,
        ),
    )


def initialized(tmp_path: Path) -> tuple[FirmStore, str]:
    store = FirmStore(tmp_path)
    result = SafetyMapBuilder(store).build(
        SafetySetupRequest("board", "setup-1", inputs(), regions())
    )
    assert result.aggregate_fingerprint is not None
    return store, result.aggregate_fingerprint


def hardware_replacements() -> tuple[RegionContribution, ...]:
    hardware = {FingerprintSource.PACK, FingerprintSource.EVIDENCE}
    return tuple(item for item in regions() if hardware.intersection(item.source_groups))


@pytest.mark.parametrize(
    ("override", "expected_status", "remedy"),
    [
        (
            {"part_target": {"mcu_part_number": "MCU-2", "target": "target_1"}},
            "safety_refresh_blocked",
            ("board_safety_setup", "board_validate"),
        ),
        (
            {"geometry": {"erase_size": 8192}},
            "safety_refresh_blocked",
            ("board_safety_setup",),
        ),
        (
            {"schema": {"memory_map": 2}},
            "safety_refresh_blocked",
            ("board_safety_setup",),
        ),
        (
            {"profile": {"board_id": "board", "display_name": "Renamed"}},
            "refresh_scope_unclear",
            ("board_safety_setup",),
        ),
    ],
)
def test_drift_routing_matrix_never_uses_refresh_for_anchor_or_structural_changes(
    tmp_path: Path,
    override: dict[str, object],
    expected_status: str,
    remedy: tuple[str, ...],
) -> None:
    store, baseline = initialized(tmp_path)
    result = SafetyRefresher(store).refresh(
        SafetyRefreshRequest("board", "refresh-1", inputs(**override))
    )

    assert result.status == expected_status
    assert result.remedy == remedy
    assert result.aggregate_fingerprint is None
    assert SafetyArtifactRepository(store).load_current("board").fingerprints.aggregate == baseline


def test_application_only_refresh_replaces_only_build_owned_regions(tmp_path: Path) -> None:
    store, baseline = initialized(tmp_path)
    replacement = (
        contribution(
            "application-v2",
            RegionKind.APPLICATION_FLASH,
            0x3000,
            0x8000,
            FingerprintSource.APPLICATION_ARTIFACTS,
        ),
        contribution(
            "RAM-v2",
            RegionKind.RAM,
            0x20000000,
            0x20001800,
            FingerprintSource.APPLICATION_ARTIFACTS,
        ),
    )
    result = SafetyRefresher(store).refresh(
        SafetyRefreshRequest(
            "board",
            "refresh-app",
            inputs(application_artifacts={"elf": "app-v2"}),
            (FingerprintSource.APPLICATION_ARTIFACTS,),
            replacement,
        )
    )

    assert result.status == "safety_refresh_completed"
    assert result.aggregate_fingerprint != baseline
    current = SafetyArtifactRepository(store).load_current("board")
    names = {item.region.name for item in current.regions}
    assert {"application-v2", "RAM-v2", "physical flash", "bootloader", "security"} <= names
    assert "application" not in names and "RAM" not in names


def test_bootloader_only_refresh_preserves_application_and_hardware_regions(tmp_path: Path) -> None:
    store, _ = initialized(tmp_path)
    replacement = (
        contribution(
            "bootloader-v2",
            RegionKind.BOOTLOADER_FLASH,
            0,
            0x1800,
            FingerprintSource.BOOTLOADER_ARTIFACTS,
        ),
    )
    result = SafetyRefresher(store).refresh(
        SafetyRefreshRequest(
            "board",
            "refresh-boot",
            inputs(bootloader_artifacts={"elf": "boot-v2"}),
            (FingerprintSource.BOOTLOADER_ARTIFACTS,),
            replacement,
        )
    )

    assert result.status == "safety_refresh_completed"
    names = {
        item.region.name for item in SafetyArtifactRepository(store).load_current("board").regions
    }
    assert {"bootloader-v2", "application", "RAM", "physical flash", "security"} <= names
    assert "bootloader" not in names


@pytest.mark.parametrize(
    ("override", "changed_sources"),
    [
        (
            {"pack": {"id": "Vendor.Pack", "version": "2.0"}},
            (FingerprintSource.PACK,),
        ),
        (
            {"evidence": {"manual": "R3", "svd": "1.0"}},
            (FingerprintSource.EVIDENCE,),
        ),
        (
            {
                "pack": {"id": "Vendor.Pack", "version": "2.0"},
                "evidence": {"manual": "R3", "svd": "1.0"},
            },
            (FingerprintSource.PACK, FingerprintSource.EVIDENCE),
        ),
    ],
)
def test_pack_or_evidence_drift_rebuilds_both_verification_sources(
    tmp_path: Path,
    override: dict[str, object],
    changed_sources: tuple[FingerprintSource, ...],
) -> None:
    store, _ = initialized(tmp_path)
    result = SafetyRefresher(store).refresh(
        SafetyRefreshRequest(
            "board",
            "refresh-hardware",
            inputs(**override),
            (FingerprintSource.PACK, FingerprintSource.EVIDENCE),
            hardware_replacements(),
        )
    )

    assert result.status == "safety_refresh_completed"
    assert result.changed_sources == changed_sources
    assert set(result.rebuilt_groups) == {
        FingerprintSource.PACK,
        FingerprintSource.EVIDENCE,
    }


def test_combined_application_and_bootloader_drift_uses_one_exact_scoped_rebuild(
    tmp_path: Path,
) -> None:
    store, _ = initialized(tmp_path)
    replacements = (
        contribution(
            "application-v2",
            RegionKind.APPLICATION_FLASH,
            0x2000,
            0x8000,
            FingerprintSource.APPLICATION_ARTIFACTS,
        ),
        contribution(
            "RAM-v2",
            RegionKind.RAM,
            0x20000000,
            0x20001800,
            FingerprintSource.APPLICATION_ARTIFACTS,
        ),
        contribution(
            "bootloader-v2",
            RegionKind.BOOTLOADER_FLASH,
            0,
            0x2000,
            FingerprintSource.BOOTLOADER_ARTIFACTS,
        ),
    )
    result = SafetyRefresher(store).refresh(
        SafetyRefreshRequest(
            "board",
            "refresh-both-builds",
            inputs(
                application_artifacts={"elf": "app-v2"},
                bootloader_artifacts={"elf": "boot-v2"},
            ),
            (
                FingerprintSource.BOOTLOADER_ARTIFACTS,
                FingerprintSource.APPLICATION_ARTIFACTS,
            ),
            replacements,
        )
    )

    assert result.status == "safety_refresh_completed"
    assert result.changed_sources == (
        FingerprintSource.APPLICATION_ARTIFACTS,
        FingerprintSource.BOOTLOADER_ARTIFACTS,
    )
    assert {item.region.name for item in SafetyArtifactRepository(store).load_current("board").regions} >= {
        "application-v2",
        "bootloader-v2",
        "RAM-v2",
        "physical flash",
    }


@pytest.mark.parametrize(
    ("overrides", "expected_status", "expected_remedy"),
    [
        (
            {
                "part_target": {"mcu_part_number": "MCU-2", "target": "target_2"},
                "application_artifacts": {"elf": "app-v2"},
            },
            "safety_refresh_blocked",
            ("board_safety_setup", "board_validate"),
        ),
        (
            {
                "profile": {"board_id": "board", "unscoped": "change"},
                "application_artifacts": {"elf": "app-v2"},
            },
            "refresh_scope_unclear",
            ("board_safety_setup",),
        ),
    ],
)
def test_anchor_or_unclear_drift_dominates_other_refreshable_changes(
    tmp_path: Path,
    overrides: dict[str, object],
    expected_status: str,
    expected_remedy: tuple[str, ...],
) -> None:
    store, baseline = initialized(tmp_path)
    result = SafetyRefresher(store).refresh(
        SafetyRefreshRequest("board", "refresh-dominance", inputs(**overrides))
    )

    assert result.status == expected_status
    assert result.remedy == expected_remedy
    assert SafetyArtifactRepository(store).load_current("board").fingerprints.aggregate == baseline


def test_missing_or_overbroad_rebuild_scope_is_unclear(tmp_path: Path) -> None:
    store, _ = initialized(tmp_path)
    missing = SafetyRefresher(store).refresh(
        SafetyRefreshRequest(
            "board",
            "refresh-missing",
            inputs(pack={"id": "Vendor.Pack", "version": "2.0"}),
            (FingerprintSource.PACK,),
            hardware_replacements(),
        )
    )

    assert missing.status == "refresh_scope_unclear"
    assert missing.remedy == ("board_safety_setup",)


def test_refresh_conflict_preserves_prior_aggregate(tmp_path: Path) -> None:
    store, baseline = initialized(tmp_path)
    paths = SafetyArtifactRepository(store).paths("board")
    before = {
        name: paths[name].read_bytes() for name in ("memory_map", "source_manifest")
    }
    overlapping_boot = (
        contribution(
            "application-v2",
            RegionKind.APPLICATION_FLASH,
            0x1000,
            0x8000,
            FingerprintSource.APPLICATION_ARTIFACTS,
        ),
    )
    result = SafetyRefresher(store).refresh(
        SafetyRefreshRequest(
            "board",
            "refresh-conflict",
            inputs(application_artifacts={"elf": "app-v2"}),
            (FingerprintSource.APPLICATION_ARTIFACTS,),
            overlapping_boot,
        )
    )

    assert result.status == "safety_conflict"
    assert SafetyArtifactRepository(store).load_current("board").fingerprints.aggregate == baseline
    assert {
        name: paths[name].read_bytes() for name in ("memory_map", "source_manifest")
    } == before


def test_successful_refresh_rechecks_adjacent_prohibited_boundary_before_promotion(
    tmp_path: Path,
) -> None:
    store, baseline = initialized(tmp_path)
    replacements = (
        contribution(
            "physical flash-v2",
            RegionKind.PHYSICAL_FLASH,
            0,
            0x10000,
            FingerprintSource.PACK,
            FingerprintSource.EVIDENCE,
            FingerprintSource.GEOMETRY,
        ),
        contribution(
            "peripheral-v2",
            RegionKind.PERIPHERAL,
            0x40000000,
            0x50000000,
            FingerprintSource.PACK,
            FingerprintSource.EVIDENCE,
        ),
        contribution(
            "prohibited-at-app-end",
            RegionKind.PROHIBITED,
            0x8000,
            0x8100,
            FingerprintSource.PACK,
            FingerprintSource.EVIDENCE,
        ),
    )
    result = SafetyRefresher(store).refresh(
        SafetyRefreshRequest(
            "board",
            "refresh-boundary",
            inputs(evidence={"manual": "R3", "svd": "1.0"}),
            (FingerprintSource.PACK, FingerprintSource.EVIDENCE),
            replacements,
        )
    )

    assert result.status == "safety_refresh_completed"
    assert result.aggregate_fingerprint != baseline
    current = SafetyArtifactRepository(store).load_current("board")
    assert "prohibited-at-app-end" in {item.region.name for item in current.regions}


def test_stale_source_manifest_routes_to_full_setup_without_committing(tmp_path: Path) -> None:
    store, baseline = initialized(tmp_path)
    manifest_path = SafetyArtifactRepository(store).paths("board")["source_manifest"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sources"]["evidence"]["evidence"] = {"manual": "silently changed"}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = SafetyRefresher(store).refresh(
        SafetyRefreshRequest("board", "refresh-stale", inputs())
    )

    assert result.status == "refresh_scope_unclear"
    assert result.aggregate_fingerprint is None
    assert result.remedy == ("board_safety_setup",)
    assert baseline != "0" * 64


def test_fresh_inputs_are_idempotent_and_do_not_claim_gate_opening(tmp_path: Path) -> None:
    store, baseline = initialized(tmp_path)
    result = SafetyRefresher(store).refresh(
        SafetyRefreshRequest("board", "refresh-fresh", inputs())
    )

    assert result.status == "safety_refresh_completed"
    assert result.changed_sources == ()
    assert result.aggregate_fingerprint == baseline
    constraints = " ".join(result.to_payload()["constraints"])  # type: ignore[arg-type]
    assert "already hardware-validated active connection" in constraints
    assert "cannot restore validation after disconnect or restart" in constraints
