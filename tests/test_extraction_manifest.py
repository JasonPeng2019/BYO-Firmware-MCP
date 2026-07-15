from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = PROJECT_ROOT / "docs" / "extraction-manifest.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_digest(root: Path, *, exclusions: set[str]) -> tuple[str, int]:
    files = [
        path
        for path in root.rglob("*")
        if path.is_file() and path.relative_to(PROJECT_ROOT).as_posix() not in exclusions
    ]
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda item: item.relative_to(PROJECT_ROOT).as_posix()):
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest(), len(files)


def _entry_sections(manifest: dict[str, Any]) -> list[list[dict[str, Any]]]:
    return [
        value
        for key, value in manifest.items()
        if key == "entries" or (key.endswith("_entries") and key != "planned_extraction_entries")
    ]


def test_every_recorded_destination_matches_the_manifest() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = [entry for section in _entry_sections(manifest) for entry in section]

    assert rows
    for entry in rows:
        destination_path = entry["destination_path"]
        assert destination_path
        destination = PROJECT_ROOT / destination_path.rstrip("/")
        if destination_path.endswith("/"):
            exclusions = set(entry.get("destination_exclusions", []))
            actual_hash, actual_count = _tree_digest(destination, exclusions=exclusions)
            assert actual_count == entry["file_count"], destination_path
        else:
            assert destination.is_file(), destination_path
            actual_hash = _sha256(destination)
        assert actual_hash == entry["destination_sha256"], destination_path


def test_every_future_planned_row_has_a_destination_outcome() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for entry in manifest["planned_extraction_entries"]:
        assert entry.get("destination_path")
        assert entry.get("destination_outcome") in {"copied", "excluded", "split", "blocked"}
