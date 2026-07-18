from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast


from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.safety.fingerprints import FingerprintInputs
from pyocd_debug_mcp.safety.map_build import SafetyArtifactRepository
from pyocd_debug_mcp.safety.refresh import SafetyRefreshRequest, SafetyRefresher

from test_safety_map_build import inputs, regions


def refresh_request(selected_inputs: FingerprintInputs | None = None):
    return SafetyRefreshRequest(
        "board", "refresh-v2-test", selected_inputs or inputs(), (), regions()
    )


def test_missing_map_is_fully_rebuilt_and_requires_validation(tmp_path: Path) -> None:
    result = SafetyRefresher(FirmStore(tmp_path)).refresh(refresh_request())
    assert result.status == "safety_refresh_completed"
    assert result.validation_required is True
    assert result.to_payload()["accepted_response"] == {
        "tool": "board_validate", "arguments": {"board_id": "board"}
    }
    assert SafetyArtifactRepository(FirmStore(tmp_path)).load_current("board").map_digest == result.map_digest


def test_corrupt_map_is_maximum_safety_recovery_not_setup(tmp_path: Path) -> None:
    store = FirmStore(tmp_path)
    path = SafetyArtifactRepository(store).paths("board")["memory_map"]
    path.parent.mkdir(parents=True)
    path.write_text("not: [valid", encoding="utf-8")
    result = SafetyRefresher(store).refresh(refresh_request())
    assert result.status == "safety_refresh_completed"
    assert "setup" not in " ".join(result.remedy)
    assert SafetyArtifactRepository(store).load_current("board").map_digest == result.map_digest


def test_refresh_preserves_same_connection_identity_but_never_creates_it(tmp_path: Path) -> None:
    store = FirmStore(tmp_path)
    first = SafetyRefresher(store).refresh(refresh_request())
    assert first.validation_required is True
    calls: list[tuple[str, str, bool]] = []
    refresher = SafetyRefresher(
        store,
        on_commit=(
            lambda board, digest, identity_unchanged: (
                calls.append((board, digest, identity_unchanged)) or True
            )
        ),
    )
    result = refresher.refresh(refresh_request())
    assert result.validation_required is False
    assert result.remedy == ()
    assert calls == [("board", result.map_digest, True)]
    assert "continue without calling board_validate" in result.agent_prompt


def test_refresh_identity_change_requires_validation_and_cannot_preserve_old_proof(
    tmp_path: Path,
) -> None:
    store = FirmStore(tmp_path)
    SafetyRefresher(store).refresh(refresh_request())
    base = inputs()
    changed = FingerprintInputs(
        {"board_id": "board", "mcu_part_number": "MCU-2", "pyocd_target": "target_2"},
        {"board_type": "fixture-v2", "mcu_part_number": "MCU-2", "target": "target_2"},
        base.pack,
        base.evidence,
        base.application_artifacts,
        base.bootloader_artifacts,
        base.geometry,
        base.schema,
    )
    callbacks: list[bool] = []
    result = SafetyRefresher(
        store,
        on_commit=lambda _board, _digest, unchanged: callbacks.append(unchanged) or True,
    ).refresh(refresh_request(changed))

    assert result.status == "safety_refresh_completed"
    assert result.validation_required is True
    assert result.remedy == ("board_validate",)
    assert result.observed["identity_unchanged"] is False
    assert callbacks == [False]


def test_routine_application_or_bootloader_build_change_does_not_change_map(tmp_path: Path) -> None:
    store = FirmStore(tmp_path)
    first = SafetyRefresher(store).refresh(refresh_request())
    base = inputs()
    changed = FingerprintInputs(
        base.profile, base.part_target, base.pack, base.evidence,
        {"elf": "new bytes and path"}, {"elf": "new boot bytes and path"},
        base.geometry, base.schema,
    )
    second = SafetyRefresher(store).refresh(refresh_request(changed))
    assert second.map_digest == first.map_digest
    assert second.observed["changed"] is False


def test_reviewed_evidence_change_rebuilds_complete_map(tmp_path: Path) -> None:
    store = FirmStore(tmp_path)
    first = SafetyRefresher(store).refresh(refresh_request())
    evidence = dict(cast(Mapping[str, object], inputs().evidence))
    evidence["official_document"] = {"revision": "R2"}
    second = SafetyRefresher(store).refresh(refresh_request(inputs(evidence=evidence)))
    assert second.status == "safety_refresh_completed"
    assert second.map_digest != first.map_digest
    assert second.observed["drift_classification"] == "complete_rebuild"


def test_failed_full_rebuild_preserves_old_map(tmp_path: Path) -> None:
    store = FirmStore(tmp_path)
    first = SafetyRefresher(store).refresh(refresh_request())
    evidence = dict(cast(Mapping[str, object], inputs().evidence))
    evidence["deployment_policy"] = {"application_authoritative": False}
    failed = SafetyRefresher(store).refresh(refresh_request(inputs(evidence=evidence)))
    assert failed.status == "safety_refresh_blocked"
    assert failed.remedy[-1] == "board_safety_refresh"
    assert SafetyArtifactRepository(store).load_current("board").map_digest == first.map_digest


def test_refresh_writes_no_manifest_or_report(tmp_path: Path) -> None:
    store = FirmStore(tmp_path)
    SafetyRefresher(store).refresh(refresh_request())
    root = store.layout.safety_board("board")
    assert {item.name for item in root.iterdir()} == {"memory_map.yaml"}


def test_refresh_is_deterministic(tmp_path: Path) -> None:
    store = FirmStore(tmp_path)
    first = SafetyRefresher(store).refresh(refresh_request())
    before = SafetyArtifactRepository(store).paths("board")["memory_map"].read_bytes()
    second = SafetyRefresher(store).refresh(refresh_request())
    assert second.map_digest == first.map_digest
    assert SafetyArtifactRepository(store).paths("board")["memory_map"].read_bytes() == before
