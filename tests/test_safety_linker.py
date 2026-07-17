from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from pyocd_debug_mcp.safety.linker import (
    BuildArtifactSelection,
    BuildRole,
    LinkerEvidenceError,
    extract_build_evidence,
    select_build_configuration,
)
from pyocd_debug_mcp.safety.regions import AddressRange

ROOT = Path(__file__).resolve().parents[1]
NUCLEO_ELF = ROOT / "firmware/nucleo_l476rg/reference/build/firmware.elf"
NRF_ELF = ROOT / "firmware/nrf52833dk/reference/build/firmware.elf"
NUCLEO_MAP = ROOT / "tests/fixtures/safety/nucleo_reference.map"
REFERENCE_BUILDS = {
    "nucleo_l476rg": {
        "flash": AddressRange(0x08000000, 0x08008000),
        "ram": AddressRange(0x20000000, 0x200011C0),
        "entry": 0x08000B29,
        "vector": 0x08000000,
    },
    "nrf52833dk": {
        "flash": AddressRange(0x0, 0x8000),
        "ram": AddressRange(0x20000000, 0x200011C0),
        "entry": 0x909,
        "vector": 0,
    },
    "nrf52840dk": {
        "flash": AddressRange(0x0, 0x8000),
        "ram": AddressRange(0x20000000, 0x20001980),
        "entry": 0xAE1,
        "vector": 0,
    },
}


def ihex_record(address: int, record_type: int, data: bytes = b"") -> str:
    payload = bytes(
        [len(data), (address >> 8) & 0xFF, address & 0xFF, record_type, *data]
    )
    checksum = (-sum(payload)) & 0xFF
    return f":{payload.hex().upper()}{checksum:02X}"


def test_tracked_nucleo_elf_and_linker_map_extract_all_build_owned_facts() -> None:
    evidence = extract_build_evidence(
        BuildArtifactSelection("nucleo_reference", BuildRole.APPLICATION, NUCLEO_ELF, NUCLEO_MAP)
    )

    assert evidence.artifact_present
    assert evidence.flash_available
    assert evidence.flash_partition == AddressRange(0x08000000, 0x08008000)
    assert evidence.ram_partitions == (AddressRange(0x20000000, 0x200011C0),)
    assert evidence.entry_point == 0x08000B29
    assert evidence.vector_table == 0x08000000
    assert len(evidence.loadable_segments) == 4
    assert any(segment.executable for segment in evidence.loadable_segments)
    partition = evidence.flash_partition
    assert partition is not None
    assert all(
        segment.load_range is None or partition.contains(segment.load_range)
        for segment in evidence.loadable_segments
    )


def test_tracked_nrf_elf_preserves_zero_vector_table_address() -> None:
    evidence = extract_build_evidence(
        BuildArtifactSelection("nrf_reference", BuildRole.APPLICATION, NRF_ELF)
    )

    assert evidence.flash_partition == AddressRange(0x0, 0x8000)
    assert evidence.vector_table == 0
    assert evidence.entry_point == 0x909


@pytest.mark.parametrize("board", sorted(REFERENCE_BUILDS))
def test_every_reference_elf_and_hex_agree_with_complete_build_evidence(board: str) -> None:
    build = ROOT / "firmware" / board / "reference" / "build"
    expected = REFERENCE_BUILDS[board]
    map_path = NUCLEO_MAP if board == "nucleo_l476rg" else None
    evidence = extract_build_evidence(
        BuildArtifactSelection(
            f"{board}_reference",
            BuildRole.APPLICATION,
            build / "firmware.elf",
            map_path,
            build / "firmware.hex",
        )
    )

    assert evidence.flash_available
    assert evidence.flash_partition == expected["flash"]
    assert evidence.ram_partitions == (expected["ram"],)
    assert evidence.entry_point == expected["entry"]
    assert evidence.vector_table == expected["vector"]
    assert evidence.hex_ranges
    partition = evidence.flash_partition
    assert partition is not None
    assert all(partition.contains(item) for item in evidence.hex_ranges)
    assert evidence.provenance == tuple(
        sorted(evidence.provenance, key=lambda item: item.artifact_kind)
    )
    assert {item.artifact_kind for item in evidence.provenance} == (
        {"elf", "hex", "linker_map"} if map_path is not None else {"elf", "hex"}
    )
    assert all(len(item.sha256) == 64 and item.path.is_absolute() for item in evidence.provenance)


def test_bootloader_role_uses_build_partition_without_caller_ranges() -> None:
    evidence = extract_build_evidence(
        BuildArtifactSelection("boot_reference", BuildRole.BOOTLOADER, NUCLEO_ELF)
    )

    assert evidence.role is BuildRole.BOOTLOADER
    assert evidence.flash_partition == AddressRange(0x08000000, 0x08008000)


def test_absent_build_artifacts_allow_non_flash_evidence_but_close_flash() -> None:
    evidence = extract_build_evidence(None)

    assert not evidence.artifact_present
    assert not evidence.flash_available
    assert evidence.flash_partition is None
    assert evidence.ram_partitions == ()
    assert "Non-flash safety evidence may continue" in (evidence.reason or "")
    assert "flashing remain unavailable" in (evidence.reason or "")


def test_build_configuration_selection_is_exact_and_never_accepts_ranges() -> None:
    app = BuildArtifactSelection("debug", BuildRole.APPLICATION, NUCLEO_ELF)
    release = BuildArtifactSelection("release", BuildRole.APPLICATION, NUCLEO_ELF)

    assert select_build_configuration([app], None) is app
    assert select_build_configuration([app, release], "release") is release
    with pytest.raises(LinkerEvidenceError, match="select one") as ambiguous:
        select_build_configuration([app, release], None)
    assert ambiguous.value.code == "build/selection-required"
    constructor = cast(Any, BuildArtifactSelection)
    with pytest.raises(TypeError):
        constructor("debug", BuildRole.APPLICATION, NUCLEO_ELF, allowed_ranges=[])


def test_malformed_and_missing_elf_fail_closed(tmp_path: Path) -> None:
    malformed = tmp_path / "bad.elf"
    malformed.write_bytes(b"not an ELF")

    with pytest.raises(LinkerEvidenceError) as bad:
        extract_build_evidence(BuildArtifactSelection("bad", BuildRole.APPLICATION, malformed))
    assert bad.value.code == "build/elf-malformed"
    with pytest.raises(LinkerEvidenceError) as missing:
        extract_build_evidence(
            BuildArtifactSelection("missing", BuildRole.APPLICATION, tmp_path / "missing.elf")
        )
    assert missing.value.code == "build/elf-missing"


def test_malformed_or_conflicting_linker_map_fails_closed(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.map"
    malformed.write_text("__rom_region_start ??? 0x08000000\n", encoding="utf-8")
    with pytest.raises(LinkerEvidenceError) as bad:
        extract_build_evidence(
            BuildArtifactSelection("bad_map", BuildRole.APPLICATION, NUCLEO_ELF, malformed)
        )
    assert bad.value.code == "build/map-malformed"

    conflict = tmp_path / "conflict.map"
    conflict.write_text("__rom_region_end = 0x08009000\n", encoding="utf-8")
    with pytest.raises(LinkerEvidenceError) as disagreement:
        extract_build_evidence(
            BuildArtifactSelection("conflict", BuildRole.APPLICATION, NUCLEO_ELF, conflict)
        )
    assert disagreement.value.code == "build/artifact-conflict"


@pytest.mark.parametrize(
    ("first_byte", "expected_code"),
    [(b"\x80", "build/hex-incomplete"), (b"\x81", "build/hex-content-conflict")],
)
def test_incomplete_or_conflicting_hex_cannot_produce_flash_evidence(
    tmp_path: Path, first_byte: bytes, expected_code: str
) -> None:
    candidate = tmp_path / "candidate.hex"
    candidate.write_text(
        "\n".join([ihex_record(0, 0, first_byte), ihex_record(0, 1)]) + "\n",
        encoding="ascii",
    )

    with pytest.raises(LinkerEvidenceError) as failure:
        extract_build_evidence(
            BuildArtifactSelection(
                "candidate",
                BuildRole.APPLICATION,
                NRF_ELF,
                None,
                candidate,
            )
        )
    assert failure.value.code == expected_code


def test_hex_checksum_overflow_and_data_outside_elf_fail_closed(tmp_path: Path) -> None:
    malformed = tmp_path / "bad-checksum.hex"
    malformed.write_text(":010000008000\n:00000001FF\n", encoding="ascii")
    with pytest.raises(LinkerEvidenceError) as checksum:
        extract_build_evidence(
            BuildArtifactSelection("checksum", BuildRole.APPLICATION, NRF_ELF, None, malformed)
        )
    assert checksum.value.code == "build/hex-checksum"

    outside = tmp_path / "outside.hex"
    outside.write_text(
        "\n".join(
            [
                ihex_record(0, 4, b"\x00\x01"),
                ihex_record(0, 0, b"\xAA"),
                ihex_record(0, 1),
            ]
        )
        + "\n",
        encoding="ascii",
    )
    with pytest.raises(LinkerEvidenceError) as outside_failure:
        extract_build_evidence(
            BuildArtifactSelection("outside", BuildRole.APPLICATION, NRF_ELF, None, outside)
        )
    assert outside_failure.value.code == "build/hex-outside-elf"
