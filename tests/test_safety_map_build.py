from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from pyocd_debug_mcp.firmstore.store import FirmStore, PERSISTED_AUTHORITY_KEYS
from pyocd_debug_mcp.safety.map_build import (
    GenericMapIdentity,
    GenericSafetyMapDocument,
    GenericSourceDigests,
    MapGeometry,
    MapIdentity,
    MapPartitions,
    RegionContribution,
    RegionSource,
    SafetyMapBuildRequest,
    SafetyMapBuilder,
    SafetyMapDocument,
    SafetyMapError,
    SafetyMapRepository,
    SourceDigests,
    canonical_map_digest,
    semantic_profile_digest,
)
from pyocd_debug_mcp.safety.regions import (
    AddressRange,
    Provenance,
    RegionKind,
    SafetyRegion,
    SourceAuthority,
)


PROFILE = {
    "schema_version": 2,
    "board_id": "board",
    "display_name": "My Board",
    "mcu_part_number": "nRF52840-QIAA",
    "mcu_family": "nRF52",
    "probe_family": "jlink",
    "probe_type": "jlink",
    "pyocd_target": "nrf52840",
    "serial_baudrate": 115200,
    "uart_note": "ignored",
    "created_at": "2025-01-01T00:00:00Z",
    "updated_at": "2025-01-02T00:00:00Z",
    "safety_ref": ".firm/safety/board/memory_map.yaml",
}

PROVENANCE = (Provenance(SourceAuthority.RECONCILED, "pack+datasheet", "two-source agreement"),)


def contribution(
    name: str,
    kind: RegionKind,
    start: int,
    end: int,
) -> RegionContribution:
    return RegionContribution(
        SafetyRegion(name, kind, AddressRange(start, end), PROVENANCE),
        (
            RegionSource.REVIEWED_DEVICE_SUPPORT,
            RegionSource.REVIEWED_OFFICIAL_EVIDENCE,
        ),
    )


def request(**changes: object) -> SafetyMapBuildRequest:
    values: dict[str, object] = {
        "board_id": "board",
        "identity": MapIdentity("nRF52840-QIAA", "nrf52840", "nrf52840dk"),
        "profile": PROFILE,
        "reviewed_device_support": {"asset_sha256": "a" * 64, "version": "1"},
        "reviewed_official_evidence": {"asset_sha256": "b" * 64, "revision": "1.1"},
        "geometry": MapGeometry(
            AddressRange(0, 0x10000),
            AddressRange(0x20000000, 0x20001000),
            erase_origin=0,
            erase_size=0x1000,
        ),
        "partitions": MapPartitions(AddressRange(0x2000, 0x10000)),
        "regions": (
            contribution("physical flash", RegionKind.PHYSICAL_FLASH, 0, 0x10000),
            contribution("physical RAM", RegionKind.PHYSICAL_RAM, 0x20000000, 0x20001000),
            contribution("writable RAM", RegionKind.RAM, 0x20000000, 0x20001000),
            contribution("peripherals", RegionKind.PERIPHERAL, 0x40000000, 0x50000000),
            contribution("configuration", RegionKind.PROHIBITED, 0x10000000, 0x10001000),
        ),
    }
    values.update(changes)
    return SafetyMapBuildRequest(**values)  # type: ignore[arg-type]


def artifact_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return {str(key) for key in value} | {
            nested for item in value.values() for nested in artifact_keys(item)
        }
    if isinstance(value, list):
        return {nested for item in value for nested in artifact_keys(item)}
    return set()


def test_builder_persists_only_deterministic_schema_v2_memory_map(tmp_path: Path) -> None:
    repository = SafetyMapRepository(FirmStore(tmp_path))
    built = SafetyMapBuilder(repository).build(request())
    path = repository.path("board")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert built == repository.load_current("board")
    assert set(path.parent.iterdir()) == {path}
    assert set(document) == {
        "schema_version",
        "board_id",
        "identity",
        "source_digests",
        "geometry",
        "partitions",
        "regions",
    }
    assert document["schema_version"] == 2
    assert set(document["source_digests"]) == {
        "semantic_profile",
        "reviewed_device_support",
        "reviewed_official_evidence",
        "map_generator_schema",
    }
    keys = {key.casefold().replace("-", "_") for key in artifact_keys(document)}
    assert not keys.intersection(PERSISTED_AUTHORITY_KEYS)
    assert not {"fingerprints", "source_manifest", "safety_report", "created_at"} & keys
    assert all("source_groups" not in row for row in document["regions"])


def test_serialization_and_digest_are_stable_across_input_order(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    baseline = request()
    first = SafetyMapBuilder(FirmStore(first_root)).build(baseline)
    second = SafetyMapBuilder(FirmStore(second_root)).build(
        replace(
            baseline,
            profile=dict(reversed(list(PROFILE.items()))),
            regions=tuple(reversed(baseline.regions)),
            reviewed_device_support={"version": "1", "asset_sha256": "a" * 64},
        )
    )

    assert first == second
    assert first.canonical_digest == second.canonical_digest
    assert canonical_map_digest(first) == first.canonical_digest
    assert SafetyMapRepository(FirmStore(first_root)).path("board").read_bytes() == (
        SafetyMapRepository(FirmStore(second_root)).path("board").read_bytes()
    )


def test_semantic_profile_ignores_bookkeeping_uart_display_paths_and_timestamps() -> None:
    changed = {
        **PROFILE,
        "display_name": "Renamed",
        "serial_baudrate": 9600,
        "uart_note": "changed",
        "created_at": "2030-01-01T00:00:00Z",
        "updated_at": "2030-01-01T00:00:01Z",
        "safety_ref": "another/path.yaml",
        "source_path": "C:/different/root/profile.yaml",
    }

    assert semantic_profile_digest(PROFILE) == semantic_profile_digest(changed)
    assert semantic_profile_digest(PROFILE) != semantic_profile_digest(
        {**PROFILE, "pyocd_target": "different_target"}
    )
    assert semantic_profile_digest(PROFILE) != semantic_profile_digest(
        {**PROFILE, "mcu_part_number": "different-part"}
    )


def test_load_rejects_malformed_old_and_wrong_board_maps(tmp_path: Path) -> None:
    repository = SafetyMapRepository(FirmStore(tmp_path))
    path = repository.path("board")
    path.parent.mkdir(parents=True)
    path.write_text("not: [valid", encoding="utf-8")
    with pytest.raises(SafetyMapError, match="malformed"):
        repository.load_current("board")

    path.write_text("schema_version: 1\nboard_id: board\n", encoding="utf-8")
    with pytest.raises(SafetyMapError, match="schema v2|schema version"):
        repository.load_current("board")

    document = SafetyMapBuilder(repository).derive(request())
    wrong = document.to_document()
    wrong["board_id"] = "other"
    path.write_text(yaml.safe_dump(wrong, sort_keys=False), encoding="utf-8")
    with pytest.raises(SafetyMapError, match="requested board"):
        repository.load_current("board")


def test_generic_schema_v3_map_preserves_physical_authority_without_deployment(tmp_path: Path) -> None:
    """Generic device support must not turn physical flash into application ownership."""

    repository = SafetyMapRepository(FirmStore(tmp_path))
    geometry = MapGeometry(
        AddressRange(0x08000000, 0x08100000),
        AddressRange(0x20000000, 0x20018000),
        erase_available=False,
    )
    document = GenericSafetyMapDocument(
        "board",
        GenericMapIdentity("STM32L476RGT6", "stm32l476rgtx", "a" * 64),
        {
            "kind": "resolved_pack",
            "support_id": "a" * 64,
            "pack_sha256": "b" * 64,
            "pdsc_device": "STM32L476RGTx",
            "pyocd_target": "stm32l476rgtx",
        },
        GenericSourceDigests.build(
            profile=PROFILE | {"mcu_part_number": "STM32L476RGT6", "pyocd_target": "stm32l476rgtx"},
            device_support={"support": "pack"},
            datasheet_evidence={"sha256": "c" * 64},
            deployment_policy={"kind": "none"},
        ),
        geometry,
        MapPartitions(None),
        {"kind": "none"},
        (
            SafetyRegion(
                "physical flash",
                RegionKind.PHYSICAL_FLASH,
                geometry.physical_flash,
                (Provenance(SourceAuthority.DEVICE_SUPPORT, "pack", "PDSC memory map"),),
            ),
            SafetyRegion(
                "physical RAM",
                RegionKind.PHYSICAL_RAM,
                geometry.physical_ram,
                (Provenance(SourceAuthority.DEVICE_SUPPORT, "pack", "PDSC memory map"),),
            ),
            SafetyRegion(
                "writable RAM",
                RegionKind.RAM,
                geometry.physical_ram,
                (Provenance(SourceAuthority.DEVICE_SUPPORT, "pack", "PDSC memory map"),),
            ),
        ),
    )

    repository.commit("board", document)
    raw = yaml.safe_load(repository.path("board").read_text(encoding="utf-8"))

    assert raw["schema_version"] == 3
    assert raw["partitions"] == {"application": None, "bootloader": None}
    assert raw["deployment_policy"] == {"kind": "none"}
    assert repository.load_current("board") == document
    malformed = document.to_document()
    malformed["authority_source"]["pack_sha256"] = 1  # type: ignore[index]
    with pytest.raises(SafetyMapError, match="values must be strings"):
        GenericSafetyMapDocument.from_document(malformed)
    with pytest.raises(SafetyMapError, match="deployment partition"):
        GenericSafetyMapDocument(
            document.board_id,
            document.identity,
            document.authority_source,
            document.source_digests,
            document.geometry,
            MapPartitions(AddressRange(0x08000000, 0x08001000)),
            {"kind": "none"},
            document.regions,
        )


def test_commit_and_load_cleanup_only_exact_legacy_siblings(tmp_path: Path) -> None:
    repository = SafetyMapRepository(FirmStore(tmp_path))
    root = repository.path("board").parent
    root.mkdir(parents=True)
    manifest = root / "source_manifest.json"
    report = root / "safety_report.json"
    unrelated = root / "p4-10-evidence.json"
    for path in (manifest, report, unrelated):
        path.write_text("preserve test", encoding="utf-8")

    repository.commit("board", SafetyMapBuilder(repository).derive(request()))

    assert not manifest.exists()
    assert not report.exists()
    assert unrelated.read_text(encoding="utf-8") == "preserve test"
    manifest.write_text("legacy", encoding="utf-8")
    report.write_text("legacy", encoding="utf-8")
    repository.load_current("board")
    assert not manifest.exists() and not report.exists()
    assert unrelated.is_file()


def test_failed_atomic_replacement_preserves_previous_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FirmStore(tmp_path)
    repository = SafetyMapRepository(store)
    builder = SafetyMapBuilder(repository)
    baseline = builder.build(request())
    before = repository.path("board").read_bytes()

    def interrupted(_destination: Path, _payload: bytes) -> Path:
        raise OSError("simulated interrupted replacement")

    monkeypatch.setattr(store, "_atomic_write_bytes", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        builder.build(
            request(reviewed_official_evidence={"asset_sha256": "c" * 64, "revision": "2"})
        )

    assert repository.path("board").read_bytes() == before
    assert repository.load_current("board") == baseline


def test_partition_absence_is_representable_but_never_inferred_from_flash_geometry() -> None:
    document = SafetyMapBuilder(FirmStore(Path.cwd())).derive(
        request(partitions=MapPartitions(None))
    )

    assert document.partitions.application is None
    assert all(region.kind is not RegionKind.APPLICATION_FLASH for region in document.regions)
    assert all(
        region.kind is not RegionKind.APPLICATION_FLASH
        for region in document.to_safety_map().regions
    )


def test_partition_overlap_with_prohibited_region_fails_closed() -> None:
    blocked = request().regions + (
        contribution("protected resident data", RegionKind.PROHIBITED, 0x3000, 0x4000),
    )

    with pytest.raises(SafetyMapError, match="partition_prohibited_overlap"):
        SafetyMapBuilder(FirmStore(Path.cwd())).derive(request(regions=blocked))


def test_explicit_geometry_round_trips_and_requires_complete_coverage() -> None:
    from pyocd_debug_mcp.safety.map_build import EraseSector

    geometry = MapGeometry(
        AddressRange(0, 0x2000),
        AddressRange(0x20000000, 0x20001000),
        erase_sectors=(
            EraseSector(AddressRange(0x1000, 0x2000), "bank-0"),
            EraseSector(AddressRange(0, 0x1000), "bank-0"),
        ),
    )
    assert MapGeometry.from_document(geometry.to_document()) == geometry
    with pytest.raises(SafetyMapError, match="cover flash contiguously"):
        MapGeometry(
            AddressRange(0, 0x3000),
            geometry.physical_ram,
            erase_sectors=(EraseSector(AddressRange(0x1000, 0x2000), "bank-0"),),
        )


def test_parser_rejects_persisted_fingerprint_or_unknown_fields() -> None:
    digest = "0" * 64
    raw = {
        "schema_version": 2,
        "board_id": "board",
        "identity": MapIdentity("part", "target", "type").to_document(),
        "source_digests": SourceDigests(digest, digest, digest, digest).to_document(),
        "geometry": MapGeometry(
            AddressRange(0, 0x1000),
            AddressRange(0x2000, 0x3000),
            erase_origin=0,
            erase_size=0x1000,
        ).to_document(),
        "partitions": MapPartitions(None).to_document(),
        "regions": [
            SafetyRegion(
                "flash", RegionKind.PHYSICAL_FLASH, AddressRange(0, 0x1000), PROVENANCE
            ).to_document()
        ],
        "fingerprints": {},
    }
    with pytest.raises(SafetyMapError, match="schema v2"):
        SafetyMapDocument.from_document(raw)


def test_build_derived_region_authority_cannot_be_persisted() -> None:
    build_region = RegionContribution(
        SafetyRegion(
            "build text",
            RegionKind.ROM,
            AddressRange(0x30000000, 0x30001000),
            (Provenance(SourceAuthority.BUILD, "firmware.elf", "sha256:abc"),),
            executable=True,
        ),
        (RegionSource.DERIVED,),
    )

    with pytest.raises(SafetyMapError, match="Build-derived|build-derived"):
        SafetyMapBuilder(FirmStore(Path.cwd())).derive(
            request(regions=request().regions + (build_region,))
        )
