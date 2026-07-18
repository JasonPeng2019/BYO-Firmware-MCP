from __future__ import annotations

import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = PROJECT_ROOT / "docs" / "extraction-manifest.json"


def _entry_sections(manifest: dict[str, Any]) -> list[list[dict[str, Any]]]:
    return [
        value
        for key, value in manifest.items()
        if key == "entries" or (key.endswith("_entries") and key != "planned_extraction_entries")
    ]


def test_historical_destination_records_are_well_formed_and_paths_still_exist() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = [entry for section in _entry_sections(manifest) for entry in section]

    assert rows
    for entry in rows:
        destination_path = entry["destination_path"]
        assert destination_path
        destination = PROJECT_ROOT / destination_path.rstrip("/")
        if destination_path.endswith("/"):
            assert destination.is_dir(), destination_path
            assert isinstance(entry["file_count"], int) and entry["file_count"] >= 0
            assert all(
                isinstance(value, str) for value in entry.get("destination_exclusions", [])
            )
        else:
            assert destination.is_file(), destination_path
        digest = entry["destination_sha256"]
        assert isinstance(digest, str) and len(digest) == 64
        assert all(character in "0123456789abcdef" for character in digest)


def test_every_future_planned_row_has_a_destination_outcome() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for entry in manifest["planned_extraction_entries"]:
        assert entry.get("destination_path")
        assert entry.get("destination_outcome") in {"copied", "excluded", "split", "blocked"}
