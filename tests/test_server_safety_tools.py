from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from pyocd_debug_mcp import server
from pyocd_debug_mcp.safety.linker import BuildRole
from pyocd_debug_mcp.safety.regions import RegionKind, SourceAuthority


ROOT = Path(__file__).resolve().parents[1]
NUCLEO_ELF = ROOT / "firmware/nucleo_l476rg/reference/build/firmware.elf"
NUCLEO_HEX = ROOT / "firmware/nucleo_l476rg/reference/build/firmware.hex"


def test_live_safety_inputs_rehash_only_server_tracked_artifact_paths(tmp_path: Path) -> None:
    artifact = tmp_path / "firmware.elf"
    artifact.write_bytes(b"first")
    stored = {
        "configuration": "release",
        "artifacts": [
            {"kind": "elf", "path": str(artifact), "sha256": "0" * 64},
        ],
    }

    refreshed = server._refresh_tracked_artifact_hashes(stored)

    assert isinstance(refreshed, dict)
    refreshed_rows = cast(list[dict[str, Any]], refreshed["artifacts"])
    assert refreshed_rows[0]["sha256"] == sha256(b"first").hexdigest()
    assert stored["artifacts"][0]["sha256"] == "0" * 64

    artifact.write_bytes(b"second")
    changed = server._refresh_tracked_artifact_hashes(stored)
    assert isinstance(changed, dict)
    changed_rows = cast(list[dict[str, Any]], changed["artifacts"])
    assert changed_rows[0]["sha256"] == sha256(b"second").hexdigest()


def test_public_refresh_rebuilds_regions_from_tracked_build_artifacts() -> None:
    replacements = server._build_region_replacements(
        {
            "configuration": "nucleo-reference",
            "artifacts": [
                {"kind": "elf", "path": str(NUCLEO_ELF), "sha256": "stale"},
                {"kind": "hex", "path": str(NUCLEO_HEX), "sha256": "stale"},
            ],
        },
        BuildRole.APPLICATION,
    )

    application = [
        item for item in replacements if item.region.kind is RegionKind.APPLICATION_FLASH
    ]
    assert application
    assert any(not item.region.executable for item in application)
    assert any(item.region.executable for item in application)
    assert not any(item.region.kind is RegionKind.RAM for item in replacements)
    assert all(
        provenance.authority is SourceAuthority.BUILD
        for item in replacements
        for provenance in item.region.provenance
    )
    assert all(
        item.source_groups == (server.FingerprintSource.APPLICATION_ARTIFACTS,)
        for item in replacements
    )
