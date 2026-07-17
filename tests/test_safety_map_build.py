from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
import yaml  # type: ignore[import-untyped]

from pyocd_debug_mcp.firmstore.store import PERSISTED_AUTHORITY_KEYS, FirmStore
from pyocd_debug_mcp.safety.fingerprints import FingerprintInputs, FingerprintSource
from pyocd_debug_mcp.safety.map_build import (
    RegionContribution,
    SafetyArtifactRepository,
    SafetyIssue,
    SafetyMapBuilder,
    SafetySetupRequest,
)
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
        if FingerprintSource.APPLICATION_ARTIFACTS in groups
        or FingerprintSource.BOOTLOADER_ARTIFACTS in groups
        else SourceAuthority.RECONCILED
    )
    return RegionContribution(
        SafetyRegion(
            name,
            kind,
            AddressRange(start, end),
            (Provenance(authority, f"source:{name}", "verified Task 13 test evidence"),),
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
            "application RAM",
            RegionKind.RAM,
            0x20000000,
            0x20001000,
            FingerprintSource.APPLICATION_ARTIFACTS,
        ),
        contribution(
            "peripherals",
            RegionKind.PERIPHERAL,
            0x40000000,
            0x50000000,
            FingerprintSource.PACK,
            FingerprintSource.EVIDENCE,
        ),
        contribution(
            "security registers",
            RegionKind.PROHIBITED,
            0x40001000,
            0x40001100,
            FingerprintSource.PACK,
            FingerprintSource.EVIDENCE,
        ),
    )


def request(**changes: object) -> SafetySetupRequest:
    values: dict[str, object] = {
        "board_id": "board",
        "continuation_id": "safety-1",
        "inputs": inputs(),
        "regions": regions(),
        "issues": (),
    }
    values.update(changes)
    return SafetySetupRequest(
        cast(str, values["board_id"]),
        cast(str, values["continuation_id"]),
        cast(FingerprintInputs, values["inputs"]),
        cast(tuple[RegionContribution, ...], values["regions"]),
        cast(tuple[SafetyIssue, ...], values["issues"]),
    )


def artifact_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return {str(key) for key in value} | {
            nested
            for item in value.values()
            for nested in artifact_keys(item)
        }
    if isinstance(value, list):
        return {nested for item in value for nested in artifact_keys(item)}
    return set()


def test_completed_setup_writes_current_artifacts_with_provenance_and_no_authority(
    tmp_path: Path,
) -> None:
    store = FirmStore(tmp_path)
    result = SafetyMapBuilder(store).build(request())

    assert result.status == "safety_setup_completed"
    assert result.aggregate_fingerprint
    artifacts = SafetyArtifactRepository(store).load_current("board")
    assert artifacts.fingerprints.aggregate == result.aggregate_fingerprint
    assert len(artifacts.regions) == len(regions())
    paths = SafetyArtifactRepository(store).paths("board")
    memory = yaml.safe_load(paths["memory_map"].read_text(encoding="utf-8"))
    manifest = json.loads(paths["source_manifest"].read_text(encoding="utf-8"))
    report = json.loads(paths["safety_report"].read_text(encoding="utf-8"))
    assert set(memory) == {
        "schema_version",
        "board_id",
        "created_at",
        "fingerprints",
        "regions",
    }
    assert set(memory["fingerprints"]["sub_fingerprints"]) == {
        source.value for source in FingerprintSource
    }
    assert set(manifest["sources"]) == {source.value for source in FingerprintSource}
    assert all(row["provenance"] and row["source_groups"] for row in memory["regions"])
    for document in (memory, manifest, report):
        keys = {key.casefold().replace("-", "_") for key in artifact_keys(document)}
        assert not keys.intersection(PERSISTED_AUTHORITY_KEYS)
        assert all(not key.startswith("gate") for key in keys)
    assert "do not expose structured internals" in result.agent_prompt


def test_unchanged_rebuild_preserves_map_manifest_and_aggregate_bytes(tmp_path: Path) -> None:
    store = FirmStore(tmp_path)
    builder = SafetyMapBuilder(store)
    first = builder.build(request())
    paths = SafetyArtifactRepository(store).paths("board")
    before = {
        name: paths[name].read_bytes() for name in ("memory_map", "source_manifest")
    }

    second = builder.build(request(continuation_id="safety-2"))

    assert second.status == "safety_setup_completed"
    assert second.aggregate_fingerprint == first.aggregate_fingerprint
    assert second.observed["unchanged_rebuild"] is True
    assert {
        name: paths[name].read_bytes() for name in ("memory_map", "source_manifest")
    } == before


@pytest.mark.parametrize(
    "status",
    [
        "safety_setup_needs_user_input",
        "safety_setup_research_required",
        "safety_setup_incomplete",
        "safety_setup_blocked",
    ],
)
def test_noncomplete_statuses_write_reports_without_committing_a_map(
    tmp_path: Path, status: str
) -> None:
    issue = SafetyIssue(status, "safety/test", "More authoritative input is required.")  # type: ignore[arg-type]
    store = FirmStore(tmp_path)
    result = SafetyMapBuilder(store).build(request(issues=(issue,)))

    assert result.status == status
    paths = SafetyArtifactRepository(store).paths("board")
    assert paths["safety_report"].is_file()
    assert not paths["memory_map"].exists()
    assert not paths["source_manifest"].exists()


def test_partition_prohibited_conflict_fails_closed_and_preserves_no_map(tmp_path: Path) -> None:
    conflicting = (
        contribution(
            "application",
            RegionKind.APPLICATION_FLASH,
            0,
            0x1000,
            FingerprintSource.APPLICATION_ARTIFACTS,
        ),
        contribution(
            "option bytes",
            RegionKind.PROHIBITED,
            0xF00,
            0x1100,
            FingerprintSource.PACK,
            FingerprintSource.EVIDENCE,
        ),
    )
    store = FirmStore(tmp_path)
    result = SafetyMapBuilder(store).build(request(regions=conflicting))

    assert result.status == "safety_setup_conflict"
    assert result.aggregate_fingerprint is None
    assert not SafetyArtifactRepository(store).paths("board")["memory_map"].exists()


def test_interrupted_bundle_restores_every_previous_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FirmStore(tmp_path)
    builder = SafetyMapBuilder(store)
    baseline = builder.build(request())
    assert baseline.aggregate_fingerprint
    paths = SafetyArtifactRepository(store).paths("board")
    before = {name: path.read_bytes() for name, path in paths.items()}
    original = store._atomic_write_bytes
    calls = 0

    def interrupted(destination: Path, payload: bytes) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated interrupted bundle")
        return original(destination, payload)

    monkeypatch.setattr(store, "_atomic_write_bytes", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        builder.build(request(inputs=inputs(application_artifacts={"elf": "app-v2"})))

    assert {name: path.read_bytes() for name, path in paths.items()} == before
    assert SafetyArtifactRepository(store).load_current("board").fingerprints.aggregate == (
        baseline.aggregate_fingerprint
    )
