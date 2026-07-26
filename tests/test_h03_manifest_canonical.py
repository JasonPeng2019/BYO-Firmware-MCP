"""H03 specification coverage for byte-stable artifact manifests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from pyocd_debug_mcp.artifact_collector import (
    MANIFEST_NAME,
    MANIFEST_OWNER,
    ArtifactRole,
    collect_artifacts,
)


def _artifact_metadata(source: Path, canonical_path: str) -> dict[str, str | int]:
    contents = source.read_bytes()
    return {
        "path": canonical_path,
        "source_name": source.name,
        "size_bytes": len(contents),
        "sha256": hashlib.sha256(contents).hexdigest(),
    }


def test_h03_manifest_is_exact_canonical_utf8_and_preserves_provenance(tmp_path: Path) -> None:
    elf = tmp_path / "application-é.elf"
    binary = tmp_path / "payload-漢.bin"
    elf.write_bytes(b"ELF\x00payload")
    binary.write_bytes(b"BIN\xffpayload")

    result = collect_artifacts(
        {ArtifactRole.BIN: binary, ArtifactRole.ELF: elf},
        tmp_path / "bundle",
        producer="builder-ø",
        expected_roles=(ArtifactRole.ELF, ArtifactRole.BIN),
    )

    manifest_bytes = result.manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    expected_bytes = (
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        + b"\n"
    )
    assert manifest_bytes == expected_bytes
    assert manifest_bytes.endswith(b"\n")
    assert not manifest_bytes.endswith(b"\r\n")
    assert b"\r" not in manifest_bytes
    assert not manifest_bytes.startswith(b"\xef\xbb\xbf")
    assert manifest["schema_version"] == 1
    assert manifest["owner"] == MANIFEST_OWNER
    assert manifest["producer"] == "builder-ø"
    assert manifest["present_roles"] == ["bin", "elf"]
    assert manifest["expected_roles"] == ["bin", "elf"]
    assert manifest["artifacts"] == {
        "bin": _artifact_metadata(binary, "firmware.bin"),
        "elf": _artifact_metadata(elf, "firmware.elf"),
    }
    assert result.to_payload()["artifacts"] == {
        "bin": {
            "path": "firmware.bin",
            "size_bytes": len(binary.read_bytes()),
            "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        },
        "elf": {
            "path": "firmware.elf",
            "size_bytes": len(elf.read_bytes()),
            "sha256": hashlib.sha256(elf.read_bytes()).hexdigest(),
        },
    }


def test_h03_collector_refusals_leave_destinations_and_stages_untouched(tmp_path: Path) -> None:
    source = tmp_path / "source.elf"
    source.write_bytes(b"ELF")
    missing_destination = tmp_path / "missing-output"

    with pytest.raises(ValueError, match="Missing expected artifact roles: bin"):
        collect_artifacts(
            {ArtifactRole.ELF: source},
            missing_destination,
            expected_roles=(ArtifactRole.ELF, ArtifactRole.BIN),
        )
    assert not missing_destination.exists()
    assert not list(tmp_path.glob(".missing-output.artifact-stage-*"))

    occupied_destination = tmp_path / "occupied-output"
    occupied_destination.mkdir()
    sentinel = occupied_destination / "preserve.txt"
    sentinel.write_bytes(b"do-not-change")
    with pytest.raises(ValueError, match="Output directory must be absent or empty"):
        collect_artifacts({ArtifactRole.ELF: source}, occupied_destination)
    assert sentinel.read_bytes() == b"do-not-change"
    assert sorted(path.name for path in occupied_destination.iterdir()) == ["preserve.txt"]
    assert not list(tmp_path.glob(".occupied-output.artifact-stage-*"))
    assert not (occupied_destination / MANIFEST_NAME).exists()
