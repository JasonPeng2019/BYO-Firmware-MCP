"""Unit tests for pinned CMSIS-Pack provisioning (no network)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from pyocd_debug_mcp import pack_provision
from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.pack_provision import (
    PackProvisionError,
    PackSpec,
    discover_local_packs,
    ensure_pack,
    load_manifest,
    load_manifest_document,
    pack_spec_document,
    sha256_bytes,
    sha256_file,
    verified_pack_for_target,
    verified_registry_pack_for_target,
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


def test_discover_local_packs_includes_selected_project_firmstore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "application"
    store = FirmStore(project)
    pack = store.layout.pack_files / "custom.pack"
    store.atomic_write_bytes(pack, b"pack")
    monkeypatch.setenv("BYO_MCP_ARTIFACT_ROOT", str(project))

    found = discover_local_packs(tmp_path / "no-shipped-packs")

    assert pack.resolve() in found


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


def test_shared_manifest_document_parser_rejects_non_list_packs(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("packs: invalid\n", encoding="utf-8")
    with pytest.raises(PackProvisionError, match="must be a list"):
        load_manifest_document(manifest)


def test_shared_pack_helpers_produce_canonical_metadata(tmp_path: Path) -> None:
    path, expected = _write_pack(tmp_path, "shared.pack", b"shared-pack-content")
    assert sha256_bytes(b"shared-pack-content") == expected
    assert sha256_file(path) == expected
    assert pack_spec_document(
        PackSpec(
            id="Vendor.Device_DFP",
            version="1.0",
            filename="shared.pack",
            url="https://example.invalid/shared.pack",
            sha256=expected.upper(),
            provides_targets=("target_a",),
            needed_by_boards=("board_a",),
        )
    ) == {
        "id": "Vendor.Device_DFP",
        "version": "1.0",
        "filename": "shared.pack",
        "url": "https://example.invalid/shared.pack",
        "sha256": expected,
        "provides_targets": ["target_a"],
        "needed_by_boards": ["board_a"],
    }


def test_repo_manifest_is_valid_and_pinned() -> None:
    # The tracked repo manifest must parse and have fully-pinned entries.
    specs = load_manifest()
    assert specs, "repo packs/manifest.yaml should list at least one pack"
    for spec in specs:
        assert spec.is_pinned, f"{spec.id} must have url + sha256"
        assert len(spec.sha256) == 64, f"{spec.id} sha256 should be 64 hex chars"


def test_verified_pack_for_target_selects_only_manifest_provider(tmp_path: Path) -> None:
    pack, digest = _write_pack(tmp_path / "packs", "selected.pack", b"selected")
    _write_pack(tmp_path / "packs", "unrelated.pack", b"unrelated")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "packs:\n"
        "  - id: Vendor.Selected\n"
        "    filename: selected.pack\n"
        "    url: https://example.invalid/selected.pack\n"
        f"    sha256: {digest}\n"
        "    provides_targets: [target_a]\n"
        "    needed_by_boards: [board_a]\n",
        encoding="utf-8",
    )

    selected = verified_pack_for_target(
        "TARGET_A", manifest_path=manifest, packs_dir=tmp_path / "packs"
    )

    assert selected is not None
    assert selected.path == pack.resolve()
    assert selected.spec.needed_by_boards == ("board_a",)
    assert verified_pack_for_target(
        "builtin_target", manifest_path=manifest, packs_dir=tmp_path / "packs"
    ) is None


def test_verified_pack_for_target_rejects_ambiguous_or_changed_bytes(tmp_path: Path) -> None:
    packs = tmp_path / "packs"
    _, digest = _write_pack(packs, "selected.pack", b"selected")
    manifest = tmp_path / "manifest.yaml"
    entry = (
        "  - id: Vendor.Selected\n"
        "    filename: selected.pack\n"
        "    url: https://example.invalid/selected.pack\n"
        f"    sha256: {digest}\n"
        "    provides_targets: [target_a]\n"
    )
    manifest.write_text("packs:\n" + entry + entry.replace("Selected", "Other"), encoding="utf-8")
    with pytest.raises(PackProvisionError, match="2 providers"):
        verified_pack_for_target("target_a", manifest_path=manifest, packs_dir=packs)

    manifest.write_text("packs:\n" + entry, encoding="utf-8")
    selected = verified_pack_for_target("target_a", manifest_path=manifest, packs_dir=packs)
    assert selected is not None
    selected.path.write_bytes(b"changed")
    with pytest.raises(PackProvisionError, match="changed or disappeared"):
        selected.verify_unchanged()


def test_registry_selector_never_uses_an_active_project_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A project may provide build inventory but cannot redefine device authority."""

    registry_packs = tmp_path / "registry-packs"
    _, registry_digest = _write_pack(registry_packs, "registry.pack", b"registry")
    registry_manifest = tmp_path / "registry.yaml"
    registry_manifest.write_text(
        "packs:\n"
        "  - id: Vendor.Registry\n"
        "    filename: registry.pack\n"
        "    url: https://example.invalid/registry.pack\n"
        f"    sha256: {registry_digest}\n"
        "    provides_targets: [target_a]\n",
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project_store = FirmStore(project)
    _, project_digest = _write_pack(project_store.layout.pack_files, "project.pack", b"project")
    project_store.layout.pack_manifest.parent.mkdir(parents=True, exist_ok=True)
    project_store.layout.pack_manifest.write_text(
        "packs:\n"
        "  - id: Vendor.Project\n"
        "    filename: project.pack\n"
        "    url: https://example.invalid/project.pack\n"
        f"    sha256: {project_digest}\n"
        "    provides_targets: [target_a]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(pack_provision, "MANIFEST_PATH", registry_manifest)
    monkeypatch.setattr(pack_provision, "PACKS_DIR", registry_packs)
    monkeypatch.setenv("BYO_MCP_ARTIFACT_ROOT", str(project))

    selected = verified_registry_pack_for_target("target_a")

    assert selected is not None
    assert selected.spec.id == "Vendor.Registry"
