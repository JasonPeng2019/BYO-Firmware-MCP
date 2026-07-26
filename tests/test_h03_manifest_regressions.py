"""Regression coverage for H03 manifest serialization callers and cleanup."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pyocd_debug_mcp.artifact_collector import MANIFEST_NAME
from pyocd_debug_mcp.tools.artifacts import build_artifact_handlers


def test_h03_mcp_collection_preserves_payload_and_writes_canonical_manifest(tmp_path: Path) -> None:
    elf = tmp_path / "firmware-\u03bb.elf"
    linker_map = tmp_path / "firmware-\u6f22.map"
    elf.write_bytes(b"\x7fELF\x00payload")
    linker_map.write_bytes(b"MEMORY { FLASH }\n")
    output = tmp_path / "bundle"

    response = build_artifact_handlers()["collect_build_artifacts"](
        str(output),
        elf_path=str(elf),
        map_path=str(linker_map),
        expected_roles=["elf", "map"],
    )

    payload = json.loads(response)
    assert payload["status"] == "artifacts_collected"
    assert payload["authority"] == "provenance_only"
    assert payload["canonical_paths"] == {
        "elf": str(output / "firmware.elf"),
        "map": str(output / "firmware.map"),
    }
    assert payload["artifacts"] == {
        "elf": {
            "path": "firmware.elf",
            "size_bytes": len(elf.read_bytes()),
            "sha256": hashlib.sha256(elf.read_bytes()).hexdigest(),
        },
        "map": {
            "path": "firmware.map",
            "size_bytes": len(linker_map.read_bytes()),
            "sha256": hashlib.sha256(linker_map.read_bytes()).hexdigest(),
        },
    }
    manifest_bytes = (output / MANIFEST_NAME).read_bytes()
    manifest = json.loads(manifest_bytes)
    assert manifest_bytes == (
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        + b"\n"
    )
    assert b"\r" not in manifest_bytes
    assert not manifest_bytes.startswith(b"\xef\xbb\xbf")
    assert (output / "firmware.elf").read_bytes() == elf.read_bytes()
    assert (output / "firmware.map").read_bytes() == linker_map.read_bytes()


def test_h03_mcp_refusal_keeps_existing_destination_and_creates_no_stage(tmp_path: Path) -> None:
    elf = tmp_path / "firmware.elf"
    elf.write_bytes(b"ELF")
    output = tmp_path / "occupied"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_bytes(b"unchanged")

    response = build_artifact_handlers()["collect_build_artifacts"](str(output), elf_path=str(elf))

    payload = json.loads(response)
    assert payload["status"] == "artifact_collection_refused"
    assert "absent or empty" in payload["message"]
    assert sentinel.read_bytes() == b"unchanged"
    assert sorted(path.name for path in output.iterdir()) == ["keep.txt"]
    assert not list(tmp_path.glob(".occupied.artifact-stage-*"))
