"""Unit tests for pinned CMSIS-Pack provisioning (no network)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from pyocd_debug_mcp import pack_provision
from pyocd_debug_mcp.pack_provision import (
    PackProvisionError,
    PackSpec,
    discover_local_packs,
    ensure_pack,
    load_manifest,
)


def _write_pack(packs_dir: Path, name: str, content: bytes) -> tuple[Path, str]:
    packs_dir.mkdir(parents=True, exist_ok=True)
    path = packs_dir / name
    path.write_bytes(content)
    return path, hashlib.sha256(content).hexdigest()


def test_discover_local_packs_finds_only_pack_files(tmp_path: Path) -> None:
    (tmp_path / "a.pack").write_bytes(b"a")
    (tmp_path / "b.pack").write_bytes(b"b")
    (tmp_path / "notes.txt").write_bytes(b"x")
    found = discover_local_packs(tmp_path)
    assert [p.name for p in found] == ["a.pack", "b.pack"]
    assert all(p.is_absolute() for p in found)


def test_discover_local_packs_missing_dir(tmp_path: Path) -> None:
    assert discover_local_packs(tmp_path / "nope") == []


def test_ensure_pack_returns_existing_when_checksum_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, sha = _write_pack(tmp_path, "x.pack", b"firmware-pack-bytes")
    spec = PackSpec(
        id="X",
        version="1.0",
        filename="x.pack",
        url="https://example.invalid/x.pack",
        sha256=sha,
    )

    def _boom(url: str, dest: Path) -> None:
        raise AssertionError("download must not be called when checksum matches")

    monkeypatch.setattr(pack_provision, "_download", _boom)
    result = ensure_pack(spec, tmp_path)
    assert result == (tmp_path / "x.pack")


def test_ensure_pack_unpinned_and_absent_raises(tmp_path: Path) -> None:
    spec = PackSpec(id="X", version="", filename="missing.pack", url="", sha256="")
    with pytest.raises(PackProvisionError):
        ensure_pack(spec, tmp_path)


def test_ensure_pack_checksum_mismatch_after_download_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate a download that yields the wrong bytes; ensure_pack must reject + clean up.
    def fake_download(url: str, dest: Path) -> None:
        dest.write_bytes(b"wrong-bytes")

    monkeypatch.setattr(pack_provision, "_download", fake_download)
    spec = PackSpec(
        id="X",
        version="1.0",
        filename="y.pack",
        url="https://example.invalid/y.pack",
        sha256=hashlib.sha256(b"expected-bytes").hexdigest(),
    )
    with pytest.raises(PackProvisionError, match="Checksum mismatch"):
        ensure_pack(spec, tmp_path)
    assert not (tmp_path / "y.pack").exists()


def test_load_manifest_parses_entries(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "packs:\n"
        "  - id: Keil.Test_DFP\n"
        "    version: '2.0.0'\n"
        "    filename: Keil.Test_DFP.2.0.0.pack\n"
        "    url: https://example.invalid/Keil.Test_DFP.2.0.0.pack\n"
        "    sha256: ABC123\n",
        encoding="utf-8",
    )
    specs = load_manifest(manifest)
    assert len(specs) == 1
    assert specs[0].id == "Keil.Test_DFP"
    assert specs[0].sha256 == "abc123"  # normalized to lowercase
    assert specs[0].is_pinned


def test_load_manifest_missing_required_field_raises(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "packs:\n  - id: Broken\n    version: '1.0'\n",  # no filename/url/sha256
        encoding="utf-8",
    )
    with pytest.raises(PackProvisionError):
        load_manifest(manifest)


def test_load_manifest_absent_returns_empty(tmp_path: Path) -> None:
    assert load_manifest(tmp_path / "none.yaml") == []


def test_repo_manifest_is_valid_and_pinned() -> None:
    # The tracked repo manifest must parse and have fully-pinned entries.
    specs = load_manifest()
    assert specs, "repo packs/manifest.yaml should list at least one pack"
    for spec in specs:
        assert spec.is_pinned, f"{spec.id} must have url + sha256"
        assert len(spec.sha256) == 64, f"{spec.id} sha256 should be 64 hex chars"
