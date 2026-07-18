"""Collect explicit native firmware outputs into a portable artifact bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping, Sequence

from pyocd_debug_mcp.firmstore.store import FIRMSTORE_DIRNAME


MANIFEST_NAME = "build-manifest.json"
MANIFEST_OWNER = "pyocd-debug-mcp-artifact-collector"


class ArtifactRole(str, Enum):
    ELF = "elf"
    HEX = "hex"
    BIN = "bin"
    MAP = "map"


CANONICAL_NAMES: Mapping[ArtifactRole, str] = {
    ArtifactRole.ELF: "firmware.elf",
    ArtifactRole.HEX: "firmware.hex",
    ArtifactRole.BIN: "firmware.bin",
    ArtifactRole.MAP: "firmware.map",
}
DEPLOYABLE_ROLES = frozenset({ArtifactRole.ELF, ArtifactRole.HEX, ArtifactRole.BIN})


@dataclass(frozen=True)
class ArtifactRecord:
    role: ArtifactRole
    path: str
    source_name: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class CollectionResult:
    output_dir: Path
    manifest_path: Path
    artifacts: tuple[ArtifactRecord, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "status": "artifacts_collected",
            "output_dir": str(self.output_dir),
            "manifest_path": str(self.manifest_path),
            "artifacts": {
                record.role.value: {
                    "path": record.path,
                    "size_bytes": record.size_bytes,
                    "sha256": record.sha256,
                }
                for record in self.artifacts
            },
        }


def _path_is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction is not None and is_junction())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_sources(
    sources: Mapping[ArtifactRole, Path | str],
) -> dict[ArtifactRole, Path]:
    normalized: dict[ArtifactRole, Path] = {}
    resolved_sources: set[Path] = set()
    for role, raw_path in sources.items():
        path = Path(raw_path).expanduser().resolve()
        if role in normalized:
            raise ValueError(f"Artifact role was supplied more than once: {role.value}")
        if not path.is_file():
            raise ValueError(f"Artifact is not a regular file: {path}")
        if path.stat().st_size <= 0:
            raise ValueError(f"Artifact is empty: {path}")
        if path in resolved_sources:
            raise ValueError(f"One source cannot fill multiple artifact roles: {path}")
        normalized[role] = path
        resolved_sources.add(path)
    if not DEPLOYABLE_ROLES.intersection(normalized):
        raise ValueError("Supply at least one explicit ELF, HEX, or BIN artifact.")
    return normalized


def _validate_destination(destination: Path, sources: Mapping[ArtifactRole, Path]) -> Path:
    requested = destination.expanduser()
    if _path_is_link_or_junction(requested):
        raise ValueError(f"Output directory must not be a link or junction: {requested}")
    resolved = requested.resolve()
    if resolved == Path(resolved.anchor).resolve() or resolved == Path.home().resolve():
        raise ValueError("Output directory must not be a filesystem root or the user's home.")
    if any(part.casefold() == FIRMSTORE_DIRNAME for part in resolved.parts):
        raise ValueError("Artifact bundles must not be written inside the FirmStore tree.")
    if resolved.exists():
        if not resolved.is_dir():
            raise ValueError(f"Output path is not a directory: {resolved}")
        if any(resolved.iterdir()):
            raise ValueError(f"Output directory must be absent or empty: {resolved}")
    for source in sources.values():
        if source == resolved or source.is_relative_to(resolved):
            raise ValueError("Artifact sources cannot be inside the output directory.")
    return resolved


def collect_artifacts(
    sources: Mapping[ArtifactRole, Path | str],
    output_dir: Path | str,
    *,
    producer: str = "native-project-build",
    expected_roles: Sequence[ArtifactRole | str] = (),
) -> CollectionResult:
    """Copy explicit typed artifacts into a deterministic, non-authoritative bundle."""

    producer = producer.strip()
    if not producer or len(producer) > 128:
        raise ValueError("Producer must contain 1 to 128 non-whitespace characters.")
    normalized = _normalize_sources(sources)
    expected = {
        role if isinstance(role, ArtifactRole) else ArtifactRole(role) for role in expected_roles
    }
    missing = expected.difference(normalized)
    if missing:
        names = ", ".join(sorted(role.value for role in missing))
        raise ValueError(f"Missing expected artifact roles: {names}")
    destination = _validate_destination(Path(output_dir), normalized)
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = destination.parent / f".{destination.name}.artifact-stage-{uuid.uuid4().hex}"
    if stage.exists():  # UUID collision is not a user directory to adopt.
        raise RuntimeError(f"Collector staging path already exists: {stage}")

    try:
        stage.mkdir()
        records: list[ArtifactRecord] = []
        for role in sorted(normalized, key=lambda item: item.value):
            source = normalized[role]
            relative_path = CANONICAL_NAMES[role]
            target = stage / relative_path
            shutil.copyfile(source, target)
            records.append(
                ArtifactRecord(
                    role=role,
                    path=relative_path,
                    source_name=source.name,
                    size_bytes=target.stat().st_size,
                    sha256=_sha256(target),
                )
            )
        manifest = {
            "schema_version": 1,
            "owner": MANIFEST_OWNER,
            "producer": producer,
            "present_roles": [record.role.value for record in records],
            "expected_roles": sorted(role.value for role in expected),
            "artifacts": {
                record.role.value: {
                    "path": record.path,
                    "source_name": record.source_name,
                    "size_bytes": record.size_bytes,
                    "sha256": record.sha256,
                }
                for record in records
            },
        }
        (stage / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if destination.exists():
            destination.rmdir()  # It was validated empty; refuse if another writer raced us.
        os.replace(stage, destination)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    final_records = tuple(records)
    return CollectionResult(
        output_dir=destination,
        manifest_path=destination / MANIFEST_NAME,
        artifacts=final_records,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--producer", default="native-project-build")
    for role in ArtifactRole:
        parser.add_argument(f"--{role.value}", dest=role.value)
    parser.add_argument(
        "--expect",
        action="append",
        default=[],
        choices=[role.value for role in ArtifactRole],
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    sources = {
        role: Path(value)
        for role in ArtifactRole
        if (value := getattr(args, role.value)) is not None
    }
    try:
        result = collect_artifacts(
            sources,
            args.output_dir,
            producer=args.producer,
            expected_roles=args.expect,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result.to_payload(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
