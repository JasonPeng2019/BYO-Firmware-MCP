from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest
import yaml  # type: ignore[import-untyped]

from pyocd_debug_mcp.firmstore.store import PERSISTED_AUTHORITY_KEYS, FirmStore
from pyocd_debug_mcp.safety.fingerprints import FingerprintInputs, FingerprintSource
from pyocd_debug_mcp.safety.map_build import (
    RegionContribution,
    SafetyArtifactError,
    SafetyArtifactRepository,
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


def inputs(*, profile: object | None = None, evidence: object | None = None) -> FingerprintInputs:
    deployment = {
        "official_document": {"revision": "R1"},
        "reconciliation": {"status": "agreement"},
        "deployment_policy": {
            "application_start": 0x08000000,
            "application_end": 0x08010000,
            "application_authoritative": True,
            "bootloader_authoritative": False,
        },
    }
    return FingerprintInputs(
        profile or {
            "board_id": "board",
            "mcu_part_number": "MCU-1",
            "display_name": "Friendly",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "serial_baudrate": 115200,
            "safety_ref": ".firm/safety/board/memory_map.yaml",
        },
        {"board_type": "fixture", "mcu_part_number": "MCU-1", "target": "target_1"},
        {"id": "Vendor.Pack", "version": "1.0"},
        evidence or deployment,
        {"artifact": "app-v1"},
        {"artifact": "boot-v1"},
        {
            "flash_start": 0x08000000,
            "flash_end": 0x08020000,
            "ram_start": 0x20000000,
            "ram_end": 0x20010000,
            "erase_origin": 0x08000000,
            "erase_size": 0x1000,
        },
        {"memory_map": 2, "evidence": 2},
    )


def region(name: str, kind: RegionKind, start: int, end: int) -> RegionContribution:
    return RegionContribution(
        SafetyRegion(
            name,
            kind,
            AddressRange(start, end),
            (Provenance(SourceAuthority.RECONCILED, name, "reviewed fixture"),),
        ),
        (FingerprintSource.PACK, FingerprintSource.EVIDENCE),
    )


def regions(*extra: RegionContribution) -> tuple[RegionContribution, ...]:
    return (
        region("physical flash", RegionKind.PHYSICAL_FLASH, 0x08000000, 0x08020000),
        region("physical RAM", RegionKind.PHYSICAL_RAM, 0x20000000, 0x20010000),
        region("usable RAM", RegionKind.RAM, 0x20000000, 0x20010000),
        region("peripherals", RegionKind.PERIPHERAL, 0x40000000, 0x50000000),
        region("option bytes", RegionKind.PROHIBITED, 0x1FFF7800, 0x1FFF7900),
        *extra,
    )


def request(*, selected_inputs: FingerprintInputs | None = None, selected_regions=None):
    return SafetySetupRequest(
        "board",
        "safety-v2-test",
        selected_inputs or inputs(),
        tuple(selected_regions if selected_regions is not None else regions()),
    )


def nested_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(map(str, value)) | {key for item in value.values() for key in nested_keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in nested_keys(item)}
    return set()


def test_v2_persists_only_one_self_contained_memory_map(tmp_path: Path) -> None:
    store = FirmStore(tmp_path)
    result = SafetyMapBuilder(store).build(request())
    assert result.status == "safety_setup_completed"
    paths = SafetyArtifactRepository(store).paths("board")
    assert set(paths) == {"memory_map"}
    assert [item.name for item in paths["memory_map"].parent.iterdir()] == ["memory_map.yaml"]
    document = yaml.safe_load(paths["memory_map"].read_text(encoding="utf-8"))
    assert set(document) == {
        "schema_version", "board_id", "identity", "source_digests",
        "geometry", "partitions", "regions",
    }
    assert document["schema_version"] == 2
    assert document["partitions"]["application"] == {"start": 0x08000000, "end": 0x08010000}
    assert not {key.casefold().replace("-", "_") for key in nested_keys(document)}.intersection(
        PERSISTED_AUTHORITY_KEYS
    )


def test_bookkeeping_and_build_bytes_do_not_change_stable_map(tmp_path: Path) -> None:
    store = FirmStore(tmp_path)
    builder = SafetyMapBuilder(store)
    first = builder.build(request())
    changed = inputs(
        profile={
            "board_id": "board", "mcu_part_number": "MCU-1",
            "display_name": "Renamed", "updated_at": "2030-01-01T00:00:00Z",
            "serial_baudrate": 9600, "safety_ref": "changed-bookkeeping",
        }
    )
    changed = FingerprintInputs(
        changed.profile, changed.part_target, changed.pack, changed.evidence,
        {"artifact": "completely-new-build"}, {"artifact": "another-boot-build"},
        changed.geometry, changed.schema,
    )
    second = builder.build(request(selected_inputs=changed))
    assert first.map_digest == second.map_digest
    assert second.observed["unchanged_rebuild"] is True


def test_commit_removes_exact_legacy_siblings(tmp_path: Path) -> None:
    store = FirmStore(tmp_path)
    root = store.layout.safety_board("board")
    root.mkdir(parents=True)
    (root / "source_manifest.json").write_text("{}", encoding="utf-8")
    (root / "safety_report.json").write_text("{}", encoding="utf-8")
    SafetyMapBuilder(store).build(request())
    assert {item.name for item in root.iterdir()} == {"memory_map.yaml"}


def test_load_removes_exact_legacy_siblings(tmp_path: Path) -> None:
    store = FirmStore(tmp_path)
    repository = SafetyArtifactRepository(store)
    assert SafetyMapBuilder(store).build(request()).status == "safety_setup_completed"
    root = repository.paths("board")["memory_map"].parent
    (root / "source_manifest.json").write_text("{}", encoding="utf-8")
    (root / "safety_report.json").write_text("{}", encoding="utf-8")

    repository.load_current("board")

    assert {item.name for item in root.iterdir()} == {"memory_map.yaml"}


@pytest.mark.parametrize("document", [{}, {"schema_version": 1}])
def test_malformed_or_old_map_is_never_authority(tmp_path: Path, document: object) -> None:
    store = FirmStore(tmp_path)
    path = SafetyArtifactRepository(store).paths("board")["memory_map"]
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(SafetyArtifactError, match="schema|fields"):
        SafetyArtifactRepository(store).load_current("board")


def test_missing_reviewed_partition_authority_fails_closed(tmp_path: Path) -> None:
    evidence = dict(cast(Mapping[str, object], inputs().evidence))
    evidence["deployment_policy"] = {"application_authoritative": False}
    result = SafetyMapBuilder(FirmStore(tmp_path)).build(
        request(selected_inputs=inputs(evidence=evidence))
    )
    assert result.status == "safety_setup_blocked"
    assert "partition authority" in result.agent_prompt


def test_profile_identity_must_match_reviewed_map_identity(tmp_path: Path) -> None:
    result = SafetyMapBuilder(FirmStore(tmp_path)).build(
        request(
            selected_inputs=inputs(
                profile={"board_id": "board", "mcu_part_number": "DIFFERENT-MCU"}
            )
        )
    )

    assert result.status == "safety_setup_blocked"
    assert "MCU part" in result.agent_prompt


def test_prohibited_partition_overlap_is_rejected_without_promotion(tmp_path: Path) -> None:
    overlap = region("prohibited app bytes", RegionKind.PROHIBITED, 0x08001000, 0x08002000)
    store = FirmStore(tmp_path)
    result = SafetyMapBuilder(store).build(request(selected_regions=regions(overlap)))
    assert result.status == "safety_setup_blocked"
    assert not SafetyArtifactRepository(store).paths("board")["memory_map"].exists()


def test_atomic_write_failure_preserves_previous_map(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = FirmStore(tmp_path)
    builder = SafetyMapBuilder(store)
    assert builder.build(request()).status == "safety_setup_completed"
    path = builder.repository.paths("board")["memory_map"]
    before = path.read_bytes()
    monkeypatch.setattr(store, "atomic_write_bytes", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("interrupted")))
    with pytest.raises(OSError, match="interrupted"):
        builder.repository.commit("board", memory_map=builder.candidate(request()).memory_map)
    assert path.read_bytes() == before
