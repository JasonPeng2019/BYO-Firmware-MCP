from __future__ import annotations

from pathlib import Path

import pytest

from pyocd_debug_mcp import reference_artifacts
from pyocd_debug_mcp.target_errors import ReferenceArtifactError


def make_build_dir(tmp_path: Path, board_id: str) -> Path:
    build_dir = tmp_path / "firmware" / board_id / "reference" / "build"
    build_dir.mkdir(parents=True)
    return build_dir


def test_resolve_reference_artifacts_prefers_hex_when_present(monkeypatch, tmp_path: Path) -> None:
    build_dir = make_build_dir(tmp_path, "nucleo_l476rg")
    elf_path = build_dir / "firmware.elf"
    hex_path = build_dir / "firmware.hex"
    elf_path.write_text("elf", encoding="utf-8")
    hex_path.write_text("hex", encoding="utf-8")
    monkeypatch.setattr(reference_artifacts, "FIRMWARE_ROOT", tmp_path / "firmware")

    pair = reference_artifacts.resolve_reference_artifacts("nucleo_l476rg")

    assert pair.symbol_artifact == elf_path.resolve()
    assert pair.flash_artifact == hex_path.resolve()


def test_resolve_reference_artifacts_falls_back_to_elf_when_hex_missing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    build_dir = make_build_dir(tmp_path, "nrf52833dk")
    elf_path = build_dir / "firmware.elf"
    elf_path.write_text("elf", encoding="utf-8")
    monkeypatch.setattr(reference_artifacts, "FIRMWARE_ROOT", tmp_path / "firmware")

    pair = reference_artifacts.resolve_reference_artifacts("nrf52833dk")

    assert pair.symbol_artifact == elf_path.resolve()
    assert pair.flash_artifact == elf_path.resolve()


def test_resolve_reference_artifacts_raises_for_missing_canonical_elf(
    monkeypatch,
    tmp_path: Path,
) -> None:
    make_build_dir(tmp_path, "nrf52833dk")
    monkeypatch.setattr(reference_artifacts, "FIRMWARE_ROOT", tmp_path / "firmware")

    with pytest.raises(ReferenceArtifactError, match="Missing canonical symbol artifact"):
        reference_artifacts.resolve_reference_artifacts("nrf52833dk")


def test_tracked_nrf52833dk_artifacts_follow_canonical_contract() -> None:
    pair = reference_artifacts.resolve_reference_artifacts("nrf52833dk")

    assert pair.board_id == "nrf52833dk"
    assert pair.symbol_artifact.name == "firmware.elf"
    assert pair.flash_artifact.name == "firmware.hex"
