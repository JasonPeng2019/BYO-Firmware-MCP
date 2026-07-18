from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from pyocd_debug_mcp import artifact_collector
from pyocd_debug_mcp.artifact_collector import ArtifactRole, collect_artifacts


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_collects_all_roles_byte_exactly_with_portable_deterministic_manifest(
    tmp_path: Path,
) -> None:
    first_sources = tmp_path / "first sources"
    second_sources = tmp_path / "second sources"
    payloads = {
        ArtifactRole.ELF: b"elf-bytes",
        ArtifactRole.HEX: b":020000040000FA\n",
        ArtifactRole.BIN: b"\x00\x01\xff",
        ArtifactRole.MAP: b"vendor map bytes\r\n",
    }
    names = {
        ArtifactRole.ELF: "application.out",
        ArtifactRole.HEX: "application.ihex",
        ArtifactRole.BIN: "application.raw",
        ArtifactRole.MAP: "application.linkermap",
    }
    sources1 = {role: _write(first_sources / names[role], data) for role, data in payloads.items()}
    sources2 = {
        role: _write(second_sources / names[role], data) for role, data in payloads.items()
    }

    first = collect_artifacts(
        sources1,
        tmp_path / "bundle-one",
        producer="vendor-native-build",
        expected_roles=tuple(ArtifactRole),
    )
    second = collect_artifacts(
        sources2,
        tmp_path / "bundle-two",
        producer="vendor-native-build",
        expected_roles=tuple(ArtifactRole),
    )

    assert first.manifest_path.read_bytes() == second.manifest_path.read_bytes()
    manifest_text = first.manifest_path.read_text(encoding="utf-8")
    assert str(first_sources.resolve()) not in manifest_text
    assert all(word not in manifest_text.lower() for word in ("permission", "allowed_range", "gate"))
    manifest = json.loads(manifest_text)
    assert manifest["present_roles"] == ["bin", "elf", "hex", "map"]
    for role, data in payloads.items():
        canonical = first.output_dir / artifact_collector.CANONICAL_NAMES[role]
        assert canonical.read_bytes() == data
        assert manifest["artifacts"][role.value]["sha256"] == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("role", [ArtifactRole.ELF, ArtifactRole.HEX, ArtifactRole.BIN])
def test_accepts_each_deployable_role_by_itself(tmp_path: Path, role: ArtifactRole) -> None:
    result = collect_artifacts({role: _write(tmp_path / f"input.{role.value}", b"x")}, tmp_path / "out")
    assert [record.role for record in result.artifacts] == [role]


def test_rejects_map_only_and_missing_expected_role_before_output(tmp_path: Path) -> None:
    map_path = _write(tmp_path / "input.map", b"map")
    with pytest.raises(ValueError, match="ELF, HEX, or BIN"):
        collect_artifacts({ArtifactRole.MAP: map_path}, tmp_path / "map-only")
    elf_path = _write(tmp_path / "input.elf", b"elf")
    with pytest.raises(ValueError, match="Missing expected artifact roles: map"):
        collect_artifacts(
            {ArtifactRole.ELF: elf_path},
            tmp_path / "missing-map",
            expected_roles=(ArtifactRole.MAP,),
        )
    assert not (tmp_path / "missing-map").exists()


def test_rejects_missing_empty_duplicate_and_directory_sources(tmp_path: Path) -> None:
    valid = _write(tmp_path / "valid.elf", b"elf")
    empty = _write(tmp_path / "empty.hex", b"")
    directory = tmp_path / "directory.bin"
    directory.mkdir()
    with pytest.raises(ValueError, match="empty"):
        collect_artifacts({ArtifactRole.HEX: empty}, tmp_path / "empty-out")
    with pytest.raises(ValueError, match="regular file"):
        collect_artifacts({ArtifactRole.BIN: directory}, tmp_path / "dir-out")
    with pytest.raises(ValueError, match="multiple artifact roles"):
        collect_artifacts(
            {ArtifactRole.ELF: valid, ArtifactRole.HEX: valid},
            tmp_path / "duplicate-out",
        )


def test_refuses_nonempty_and_linked_output_without_modification(tmp_path: Path) -> None:
    source = _write(tmp_path / "source.elf", b"elf")
    nonempty = tmp_path / "nonempty"
    sentinel = _write(nonempty / "keep.txt", b"keep")
    with pytest.raises(ValueError, match="absent or empty"):
        collect_artifacts({ArtifactRole.ELF: source}, nonempty)
    assert sentinel.read_bytes() == b"keep"

    real = tmp_path / "real-output"
    real.mkdir()
    linked = tmp_path / "linked-output"
    try:
        linked.symlink_to(real, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"host cannot create directory link: {exc}")
    with pytest.raises(ValueError, match="link or junction"):
        collect_artifacts({ArtifactRole.ELF: source}, linked)


def test_refuses_source_inside_destination(tmp_path: Path) -> None:
    output = tmp_path / "output"
    source = _write(output / "source.elf", b"elf")
    with pytest.raises(ValueError, match="absent or empty|inside the output"):
        collect_artifacts({ArtifactRole.ELF: source}, output)
    assert source.read_bytes() == b"elf"


def test_refuses_firmstore_output_route(tmp_path: Path) -> None:
    source = _write(tmp_path / "source.elf", b"elf")
    output = tmp_path / ".firm" / "agent-artifacts"
    with pytest.raises(ValueError, match="FirmStore"):
        collect_artifacts({ArtifactRole.ELF: source}, output)
    assert not output.exists()


def test_copy_failure_removes_owned_stage_and_does_not_create_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = _write(tmp_path / "source.elf", b"elf")

    def fail_copy(_source: Path, _target: Path) -> None:
        raise OSError("injected copy failure")

    monkeypatch.setattr(artifact_collector.shutil, "copyfile", fail_copy)
    output = tmp_path / "output"
    with pytest.raises(OSError, match="injected"):
        collect_artifacts({ArtifactRole.ELF: source}, output)
    assert not output.exists()
    assert list(tmp_path.glob(".output.artifact-stage-*")) == []


def test_module_cli_prints_success_json_and_uses_stderr_for_errors(tmp_path: Path) -> None:
    elf = _write(tmp_path / "app.elf", b"elf")
    success = subprocess.run(
        [
            sys.executable,
            "-m",
            "pyocd_debug_mcp.artifact_collector",
            "--output-dir",
            str(tmp_path / "success"),
            "--elf",
            str(elf),
            "--expect",
            "elf",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert success.returncode == 0, success.stderr
    assert json.loads(success.stdout)["status"] == "artifacts_collected"
    assert success.stderr == ""

    failure = subprocess.run(
        [
            sys.executable,
            "-m",
            "pyocd_debug_mcp.artifact_collector",
            "--output-dir",
            str(tmp_path / "failure"),
            "--elf",
            str(elf),
            "--expect",
            "map",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert failure.returncode != 0
    assert failure.stdout == ""
    assert "Missing expected artifact roles: map" in failure.stderr
