from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from pyocd_debug_mcp.firmstore.profiles import ProfileError, ProfileRepository
from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.setup_flow.datasheet_evidence import (
    DatasheetEvidenceError,
    capture_datasheet_evidence,
    replay_datasheet_evidence,
)


def test_datasheet_capture_is_content_addressed_and_replay_rejects_drift(tmp_path: Path) -> None:
    source = tmp_path / "vendor.pdf"
    source.write_bytes(b"%PDF-1.7\nimmutable device evidence")
    store = FirmStore(tmp_path / "project")

    evidence = capture_datasheet_evidence(store, source)

    assert evidence.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert evidence.reference == f".firm/evidence/datasheets/{evidence.sha256}.pdf"
    assert replay_datasheet_evidence(store, evidence.reference, evidence.sha256) == evidence
    store.layout.datasheet_evidence(evidence.sha256).write_bytes(b"%PDF-mutated")
    with pytest.raises(DatasheetEvidenceError, match="changed"):
        replay_datasheet_evidence(store, evidence.reference, evidence.sha256)


def test_profile_load_replays_captured_datasheet_bytes(tmp_path: Path) -> None:
    store = FirmStore(tmp_path)
    profiles = ProfileRepository(store, legacy_board_dir=tmp_path / "legacy")
    source = tmp_path / "device.pdf"
    source.write_bytes(b"%PDF-1.7\npart")
    evidence = capture_datasheet_evidence(store, source)
    profiles.commit_core(
        profiles.stage_core(
            {
                "board_id": "generic_board",
                "display_name": "Generic Board",
                "mcu_part_number": "GENERIC123",
                "mcu_family": "generic",
                "probe_family": "cmsis-dap",
                "pyocd_target": "generic123",
            }
        )
    )
    profiles.commit_optional(
        profiles.stage_optional(
            "generic_board",
            {
                "datasheet_sha256": evidence.sha256,
                "datasheet_ref": evidence.reference,
            },
        )
    )

    store.layout.datasheet_evidence(evidence.sha256).write_bytes(b"%PDF-tampered")
    with pytest.raises(ProfileError, match="evidence replay failed"):
        profiles.load("generic_board", include_legacy=False)
