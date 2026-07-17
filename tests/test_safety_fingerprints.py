from __future__ import annotations

import copy

import pytest

from pyocd_debug_mcp.safety.fingerprints import (
    FingerprintError,
    FingerprintInputs,
    FingerprintSet,
    FingerprintSource,
    canonical_bytes,
)


def inputs(**overrides: object) -> FingerprintInputs:
    values: dict[str, object] = {
        "profile": {"board_id": "board", "display_name": "Board"},
        "part_target": {"mcu_part_number": "MCU-1", "target": "target_1"},
        "pack": {"id": "Vendor.Pack", "version": "1.0", "sha256": "a" * 64},
        "evidence": {"sources": [{"revision": "R2", "identifier": "manual"}]},
        "application_artifacts": {"elf": "app-sha"},
        "bootloader_artifacts": None,
        "geometry": {"erase_size": 4096, "banks": [0, 1]},
        "schema": {"memory_map": 1, "evidence": 1},
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


def test_canonical_mapping_order_and_aggregate_are_stable() -> None:
    left = inputs(profile={"z": [1, {"b": 2, "a": 1}], "a": "exact"})
    right = inputs(profile={"a": "exact", "z": [1, {"a": 1, "b": 2}]})

    assert canonical_bytes(left.profile) == canonical_bytes(right.profile)
    assert FingerprintSet.build(left) == FingerprintSet.build(right)


def test_every_source_is_domain_separated_and_drift_is_exact() -> None:
    baseline = FingerprintSet.build(inputs())
    changed = FingerprintSet.build(inputs(pack={"id": "Vendor.Pack", "version": "2.0"}))

    assert baseline.changed_sources(changed) == (FingerprintSource.PACK,)
    assert len({digest for _, digest in baseline.sub_fingerprints}) == len(FingerprintSource)
    assert baseline.aggregate != changed.aggregate


@pytest.mark.parametrize(
    ("source", "override"),
    [
        (FingerprintSource.PROFILE, {"profile": {"board_id": "board", "revision": 2}}),
        (
            FingerprintSource.PART_TARGET,
            {"part_target": {"mcu_part_number": "MCU-2", "target": "target_1"}},
        ),
        (FingerprintSource.PACK, {"pack": {"id": "Vendor.Pack", "version": "2.0"}}),
        (FingerprintSource.EVIDENCE, {"evidence": {"sources": [{"revision": "R3"}]}}),
        (
            FingerprintSource.APPLICATION_ARTIFACTS,
            {"application_artifacts": {"elf": "app-v2"}},
        ),
        (
            FingerprintSource.BOOTLOADER_ARTIFACTS,
            {"bootloader_artifacts": {"elf": "boot-v2"}},
        ),
        (FingerprintSource.GEOMETRY, {"geometry": {"erase_size": 8192}}),
        (FingerprintSource.SCHEMA, {"schema": {"memory_map": 2, "evidence": 1}}),
    ],
)
def test_each_fingerprint_source_changes_independently(
    source: FingerprintSource, override: dict[str, object]
) -> None:
    baseline = FingerprintSet.build(inputs())
    candidate = FingerprintSet.build(inputs(**override))

    assert baseline.changed_sources(candidate) == (source,)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {
                "application_artifacts": {"elf": "app-v2"},
                "bootloader_artifacts": {"elf": "boot-v2"},
            },
            (
                FingerprintSource.APPLICATION_ARTIFACTS,
                FingerprintSource.BOOTLOADER_ARTIFACTS,
            ),
        ),
        (
            {
                "pack": {"id": "Vendor.Pack", "version": "2.0"},
                "evidence": {"sources": [{"revision": "R3"}]},
            },
            (FingerprintSource.PACK, FingerprintSource.EVIDENCE),
        ),
        (
            {
                "profile": {"board_id": "board", "revision": 2},
                "part_target": {"mcu_part_number": "MCU-2", "target": "target_2"},
                "schema": {"memory_map": 2},
            },
            (
                FingerprintSource.PROFILE,
                FingerprintSource.PART_TARGET,
                FingerprintSource.SCHEMA,
            ),
        ),
    ],
)
def test_meaningful_combined_drift_is_reported_in_canonical_source_order(
    overrides: dict[str, object], expected: tuple[FingerprintSource, ...]
) -> None:
    baseline = FingerprintSet.build(inputs())
    assert baseline.changed_sources(FingerprintSet.build(inputs(**overrides))) == expected


def test_artifact_file_order_and_path_spelling_are_normalized() -> None:
    left = inputs(
        application_artifacts={
            "files": [
                {"path": r"build\objects\..\firmware.map", "sha256": "b" * 64},
                {"path": r".\build\firmware.elf", "sha256": "a" * 64},
            ]
        }
    )
    right = inputs(
        application_artifacts={
            "files": [
                {"sha256": "a" * 64, "path": "build/firmware.elf"},
                {"sha256": "b" * 64, "path": "build/firmware.map"},
            ]
        }
    )

    assert FingerprintSet.build(left) == FingerprintSet.build(right)
    assert left.canonical_documents()["application_artifacts"] == (
        right.canonical_documents()["application_artifacts"]
    )

    path_list_left = inputs(
        bootloader_artifacts={"files": [r"build\boot.hex", r".\build\boot.elf"]}
    )
    path_list_right = inputs(
        bootloader_artifacts={"files": ["build/boot.elf", "build/boot.hex"]}
    )
    assert FingerprintSet.build(path_list_left) == FingerprintSet.build(path_list_right)


def test_round_trip_recomputes_aggregate_and_rejects_tampering() -> None:
    fingerprints = FingerprintSet.build(inputs())
    document = fingerprints.to_document()

    assert FingerprintSet.from_document(document) == fingerprints
    tampered = copy.deepcopy(document)
    tampered["sub_fingerprints"]["geometry"] = "0" * 64  # type: ignore[index]
    with pytest.raises(FingerprintError, match="aggregate"):
        FingerprintSet.from_document(tampered)


@pytest.mark.parametrize("invalid", [1.5, b"bytes", {1: "non-string key"}, {"x": {1, 2}}])
def test_noncanonical_types_are_rejected(invalid: object) -> None:
    with pytest.raises(FingerprintError):
        canonical_bytes(invalid)
