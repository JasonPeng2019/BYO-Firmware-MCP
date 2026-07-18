from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pyocd_debug_mcp import server
from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.safety.fingerprints import (
    FingerprintInputs,
    FingerprintSet,
    FingerprintSource,
)
from pyocd_debug_mcp.safety.map_build import (
    RegionContribution,
    SafetyArtifactError,
    SafetyArtifactRepository,
    SafetyMapBuilder,
    SafetySetupRequest,
)
from pyocd_debug_mcp.safety.refresh import SafetyRefresher, SafetyRefreshRequest
from pyocd_debug_mcp.safety.regions import (
    AddressRange,
    Provenance,
    RegionKind,
    SafetyRegion,
    SourceAuthority,
)


def refresher(store: FirmStore) -> SafetyRefresher:
    return SafetyRefresher(store, authority_verifier=lambda _artifacts: None)


def inputs(**overrides: object) -> FingerprintInputs:
    geometry = {"erase_origin": 0, "erase_size": 4096}
    values: dict[str, object] = {
        "profile": {"board_id": "board", "display_name": "Board"},
        "part_target": {"mcu_part_number": "MCU-1", "target": "target_1"},
        "pack": {
            "id": "Vendor.Pack",
            "version": "1.0",
            "document": {"schema_version": 2},
        },
        "evidence": {
            "manual": "R2",
            "svd": "1.0",
            "official_document": {"document": {"schema_version": 2}},
            "reconciliation": {"status": "agreement", "erase_geometry": dict(geometry)},
        },
        "application_artifacts": {"elf": "app-v1"},
        "bootloader_artifacts": {"elf": "boot-v1"},
        "geometry": geometry,
        "schema": {"memory_map": 1, "evidence": 2, "catalog": 2},
    }
    for key, value in overrides.items():
        if key in {"pack", "evidence", "geometry", "schema"} and isinstance(value, dict):
            current = values[key]
            assert isinstance(current, dict)
            values[key] = {**current, **value}
        else:
            values[key] = value
    return FingerprintInputs(
        values["profile"],
        values["part_target"],
        values["pack"],
        values["evidence"],
        values["application_artifacts"],
        values["bootloader_artifacts"],
        values["geometry"],
        values["schema"],
    )


def contribution(
    name: str,
    kind: RegionKind,
    start: int,
    end: int,
    *groups: FingerprintSource,
) -> RegionContribution:
    authority = (
        SourceAuthority.BUILD
        if set(groups).intersection(
            {
                FingerprintSource.APPLICATION_ARTIFACTS,
                FingerprintSource.BOOTLOADER_ARTIFACTS,
            }
        )
        else SourceAuthority.RECONCILED
    )
    return RegionContribution(
        SafetyRegion(
            name,
            kind,
            AddressRange(start, end),
            (Provenance(authority, f"source:{name}", "verified refresh evidence"),),
            kind in {RegionKind.APPLICATION_FLASH, RegionKind.BOOTLOADER_FLASH},
        ),
        groups,
    )


def regions() -> tuple[RegionContribution, ...]:
    return (
        contribution(
            "physical flash",
            RegionKind.PHYSICAL_FLASH,
            0,
            0x10000,
            FingerprintSource.PACK,
            FingerprintSource.EVIDENCE,
            FingerprintSource.GEOMETRY,
        ),
        contribution(
            "application",
            RegionKind.APPLICATION_FLASH,
            0x2000,
            0x8000,
            FingerprintSource.APPLICATION_ARTIFACTS,
        ),
        contribution(
            "bootloader",
            RegionKind.BOOTLOADER_FLASH,
            0,
            0x2000,
            FingerprintSource.BOOTLOADER_ARTIFACTS,
        ),
        contribution(
            "RAM",
            RegionKind.RAM,
            0x20000000,
            0x20001000,
            FingerprintSource.APPLICATION_ARTIFACTS,
        ),
        contribution(
            "peripheral",
            RegionKind.PERIPHERAL,
            0x40000000,
            0x50000000,
            FingerprintSource.PACK,
            FingerprintSource.EVIDENCE,
        ),
        contribution(
            "security",
            RegionKind.PROHIBITED,
            0x40001000,
            0x40001100,
            FingerprintSource.PACK,
            FingerprintSource.EVIDENCE,
        ),
    )


def initialized(tmp_path: Path) -> tuple[FirmStore, str]:
    store = FirmStore(tmp_path)
    result = SafetyMapBuilder(store).build(
        SafetySetupRequest("board", "setup-1", inputs(), regions())
    )
    assert result.aggregate_fingerprint is not None
    return store, result.aggregate_fingerprint


def hardware_replacements() -> tuple[RegionContribution, ...]:
    hardware = {FingerprintSource.PACK, FingerprintSource.EVIDENCE}
    return tuple(item for item in regions() if hardware.intersection(item.source_groups))


@pytest.mark.parametrize(
    ("override", "expected_status", "remedy"),
    [
        (
            {"part_target": {"mcu_part_number": "MCU-2", "target": "target_1"}},
            "safety_refresh_blocked",
            ("board_safety_setup", "board_validate"),
        ),
        (
            {"geometry": {"erase_size": 8192}},
            "safety_refresh_blocked",
            ("board_safety_setup", "board_validate"),
        ),
        (
            {"schema": {"memory_map": 2}},
            "safety_refresh_blocked",
            ("board_safety_setup", "board_validate"),
        ),
        (
            {"profile": {"board_id": "board", "display_name": "Renamed"}},
            "refresh_scope_unclear",
            ("board_safety_setup",),
        ),
    ],
)
def test_drift_routing_matrix_never_uses_refresh_for_anchor_or_structural_changes(
    tmp_path: Path,
    override: dict[str, object],
    expected_status: str,
    remedy: tuple[str, ...],
) -> None:
    store, baseline = initialized(tmp_path)
    result = refresher(store).refresh(
        SafetyRefreshRequest("board", "refresh-1", inputs(**override))
    )

    assert result.status == expected_status
    assert result.remedy == remedy
    assert result.aggregate_fingerprint is None
    assert SafetyArtifactRepository(store).load_current("board").fingerprints.aggregate == baseline


def test_application_only_refresh_replaces_only_build_owned_regions(tmp_path: Path) -> None:
    store, baseline = initialized(tmp_path)
    replacement = (
        contribution(
            "application-v2",
            RegionKind.APPLICATION_FLASH,
            0x3000,
            0x8000,
            FingerprintSource.APPLICATION_ARTIFACTS,
        ),
        contribution(
            "RAM-v2",
            RegionKind.RAM,
            0x20000000,
            0x20001800,
            FingerprintSource.APPLICATION_ARTIFACTS,
        ),
    )
    result = refresher(store).refresh(
        SafetyRefreshRequest(
            "board",
            "refresh-app",
            inputs(application_artifacts={"elf": "app-v2"}),
            (FingerprintSource.APPLICATION_ARTIFACTS,),
            replacement,
        )
    )

    assert result.status == "safety_refresh_completed"
    assert result.aggregate_fingerprint != baseline
    current = SafetyArtifactRepository(store).load_current("board")
    names = {item.region.name for item in current.regions}
    assert {"application-v2", "RAM-v2", "physical flash", "bootloader", "security"} <= names
    assert "application" not in names and "RAM" not in names


def test_legacy_authority_map_cannot_be_promoted_by_refresh(tmp_path: Path) -> None:
    store = FirmStore(tmp_path)
    current = inputs()
    legacy = FingerprintInputs(
        current.profile,
        current.part_target,
        current.pack,
        current.evidence,
        current.application_artifacts,
        current.bootloader_artifacts,
        current.geometry,
        {"memory_map": 1, "evidence": 1, "catalog": 1},
    )
    setup = SafetyMapBuilder(store).build(
        SafetySetupRequest("board", "legacy-setup", legacy, regions())
    )
    assert setup.aggregate_fingerprint is not None
    repository = SafetyArtifactRepository(store)
    before = repository.paths("board")["memory_map"].read_bytes()

    result = SafetyRefresher(store).refresh(SafetyRefreshRequest("board", "legacy-refresh", legacy))

    assert result.status == "refresh_scope_unclear"
    assert result.remedy == ("board_safety_setup",)
    assert repository.paths("board")["memory_map"].read_bytes() == before


def test_bootloader_only_refresh_preserves_application_and_hardware_regions(tmp_path: Path) -> None:
    store, _ = initialized(tmp_path)
    replacement = (
        contribution(
            "bootloader-v2",
            RegionKind.BOOTLOADER_FLASH,
            0,
            0x1800,
            FingerprintSource.BOOTLOADER_ARTIFACTS,
        ),
    )
    result = refresher(store).refresh(
        SafetyRefreshRequest(
            "board",
            "refresh-boot",
            inputs(bootloader_artifacts={"elf": "boot-v2"}),
            (FingerprintSource.BOOTLOADER_ARTIFACTS,),
            replacement,
        )
    )

    assert result.status == "safety_refresh_completed"
    names = {
        item.region.name for item in SafetyArtifactRepository(store).load_current("board").regions
    }
    assert {"bootloader-v2", "application", "RAM", "physical flash", "security"} <= names
    assert "bootloader" not in names


@pytest.mark.parametrize(
    ("override", "changed_sources", "classification"),
    [
        (
            {"pack": {"id": "Vendor.Pack", "version": "2.0"}},
            (FingerprintSource.PACK,),
            "pack_change",
        ),
        (
            {"evidence": {"manual": "R3", "svd": "1.0"}},
            (FingerprintSource.EVIDENCE,),
            "official_evidence_change",
        ),
        (
            {
                "pack": {"id": "Vendor.Pack", "version": "2.0"},
                "evidence": {"manual": "R3", "svd": "1.0"},
            },
            (FingerprintSource.PACK, FingerprintSource.EVIDENCE),
            "pack_and_official_evidence_change",
        ),
    ],
)
def test_pack_or_evidence_drift_rebuilds_both_verification_sources(
    tmp_path: Path,
    override: dict[str, object],
    changed_sources: tuple[FingerprintSource, ...],
    classification: str,
) -> None:
    store, _ = initialized(tmp_path)
    result = refresher(store).refresh(
        SafetyRefreshRequest(
            "board",
            "refresh-hardware",
            inputs(**override),
            (FingerprintSource.PACK, FingerprintSource.EVIDENCE),
            hardware_replacements(),
        )
    )

    assert result.status == "safety_refresh_completed"
    assert result.changed_sources == changed_sources
    assert set(result.rebuilt_groups) == {
        FingerprintSource.PACK,
        FingerprintSource.EVIDENCE,
    }
    assert result.to_payload()["observed"]["drift_classification"] == classification  # type: ignore[index]


def test_combined_application_and_bootloader_drift_uses_one_exact_scoped_rebuild(
    tmp_path: Path,
) -> None:
    store, _ = initialized(tmp_path)
    replacements = (
        contribution(
            "application-v2",
            RegionKind.APPLICATION_FLASH,
            0x2000,
            0x8000,
            FingerprintSource.APPLICATION_ARTIFACTS,
        ),
        contribution(
            "RAM-v2",
            RegionKind.RAM,
            0x20000000,
            0x20001800,
            FingerprintSource.APPLICATION_ARTIFACTS,
        ),
        contribution(
            "bootloader-v2",
            RegionKind.BOOTLOADER_FLASH,
            0,
            0x2000,
            FingerprintSource.BOOTLOADER_ARTIFACTS,
        ),
    )
    result = refresher(store).refresh(
        SafetyRefreshRequest(
            "board",
            "refresh-both-builds",
            inputs(
                application_artifacts={"elf": "app-v2"},
                bootloader_artifacts={"elf": "boot-v2"},
            ),
            (
                FingerprintSource.BOOTLOADER_ARTIFACTS,
                FingerprintSource.APPLICATION_ARTIFACTS,
            ),
            replacements,
        )
    )

    assert result.status == "safety_refresh_completed"
    assert result.changed_sources == (
        FingerprintSource.APPLICATION_ARTIFACTS,
        FingerprintSource.BOOTLOADER_ARTIFACTS,
    )
    assert {
        item.region.name for item in SafetyArtifactRepository(store).load_current("board").regions
    } >= {
        "application-v2",
        "bootloader-v2",
        "RAM-v2",
        "physical flash",
    }


@pytest.mark.parametrize(
    ("overrides", "expected_status", "expected_remedy"),
    [
        (
            {
                "part_target": {"mcu_part_number": "MCU-2", "target": "target_2"},
                "application_artifacts": {"elf": "app-v2"},
            },
            "safety_refresh_blocked",
            ("board_safety_setup", "board_validate"),
        ),
        (
            {
                "profile": {"board_id": "board", "unscoped": "change"},
                "application_artifacts": {"elf": "app-v2"},
            },
            "refresh_scope_unclear",
            ("board_safety_setup",),
        ),
    ],
)
def test_anchor_or_unclear_drift_dominates_other_refreshable_changes(
    tmp_path: Path,
    overrides: dict[str, object],
    expected_status: str,
    expected_remedy: tuple[str, ...],
) -> None:
    store, baseline = initialized(tmp_path)
    result = refresher(store).refresh(
        SafetyRefreshRequest("board", "refresh-dominance", inputs(**overrides))
    )

    assert result.status == expected_status
    assert result.remedy == expected_remedy
    assert SafetyArtifactRepository(store).load_current("board").fingerprints.aggregate == baseline


def test_missing_or_overbroad_rebuild_scope_is_unclear(tmp_path: Path) -> None:
    store, _ = initialized(tmp_path)
    missing = refresher(store).refresh(
        SafetyRefreshRequest(
            "board",
            "refresh-missing",
            inputs(pack={"id": "Vendor.Pack", "version": "2.0"}),
            (FingerprintSource.PACK,),
            hardware_replacements(),
        )
    )

    assert missing.status == "refresh_scope_unclear"
    assert missing.remedy == ("board_safety_setup",)


def test_refresh_conflict_preserves_prior_aggregate(tmp_path: Path) -> None:
    store, baseline = initialized(tmp_path)
    paths = SafetyArtifactRepository(store).paths("board")
    before = {name: paths[name].read_bytes() for name in ("memory_map", "source_manifest")}
    overlapping_boot = (
        contribution(
            "application-v2",
            RegionKind.APPLICATION_FLASH,
            0x1000,
            0x8000,
            FingerprintSource.APPLICATION_ARTIFACTS,
        ),
    )
    result = refresher(store).refresh(
        SafetyRefreshRequest(
            "board",
            "refresh-conflict",
            inputs(application_artifacts={"elf": "app-v2"}),
            (FingerprintSource.APPLICATION_ARTIFACTS,),
            overlapping_boot,
        )
    )

    assert result.status == "safety_conflict"
    assert SafetyArtifactRepository(store).load_current("board").fingerprints.aggregate == baseline
    assert {name: paths[name].read_bytes() for name in ("memory_map", "source_manifest")} == before


def test_successful_refresh_rechecks_adjacent_prohibited_boundary_before_promotion(
    tmp_path: Path,
) -> None:
    store, baseline = initialized(tmp_path)
    replacements = (
        contribution(
            "physical flash-v2",
            RegionKind.PHYSICAL_FLASH,
            0,
            0x10000,
            FingerprintSource.PACK,
            FingerprintSource.EVIDENCE,
            FingerprintSource.GEOMETRY,
        ),
        contribution(
            "peripheral-v2",
            RegionKind.PERIPHERAL,
            0x40000000,
            0x50000000,
            FingerprintSource.PACK,
            FingerprintSource.EVIDENCE,
        ),
        contribution(
            "prohibited-at-app-end",
            RegionKind.PROHIBITED,
            0x8000,
            0x8100,
            FingerprintSource.PACK,
            FingerprintSource.EVIDENCE,
        ),
    )
    result = refresher(store).refresh(
        SafetyRefreshRequest(
            "board",
            "refresh-boundary",
            inputs(evidence={"manual": "R3", "svd": "1.0"}),
            (FingerprintSource.PACK, FingerprintSource.EVIDENCE),
            replacements,
        )
    )

    assert result.status == "safety_refresh_completed"
    assert result.aggregate_fingerprint != baseline
    current = SafetyArtifactRepository(store).load_current("board")
    assert "prohibited-at-app-end" in {item.region.name for item in current.regions}


def test_stale_source_manifest_routes_to_full_setup_without_committing(tmp_path: Path) -> None:
    store, baseline = initialized(tmp_path)
    manifest_path = SafetyArtifactRepository(store).paths("board")["source_manifest"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sources"]["evidence"]["evidence"] = {"manual": "silently changed"}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = refresher(store).refresh(SafetyRefreshRequest("board", "refresh-stale", inputs()))

    assert result.status == "refresh_scope_unclear"
    assert result.aggregate_fingerprint is None
    assert result.remedy == ("board_safety_setup",)
    assert baseline != "0" * 64


def test_fresh_inputs_are_idempotent_and_do_not_claim_gate_opening(tmp_path: Path) -> None:
    store, baseline = initialized(tmp_path)
    result = refresher(store).refresh(SafetyRefreshRequest("board", "refresh-fresh", inputs()))

    assert result.status == "safety_refresh_completed"
    assert result.changed_sources == ()
    assert result.aggregate_fingerprint == baseline
    constraints = " ".join(result.to_payload()["constraints"])  # type: ignore[arg-type]
    assert "already hardware-validated active connection" in constraints
    assert "cannot restore validation after disconnect or restart" in constraints


def test_refresh_routes_expose_stable_machine_readable_scope_and_remedies(
    tmp_path: Path,
) -> None:
    store, _ = initialized(tmp_path)
    cases = (
        (
            inputs(part_target={"mcu_part_number": "MCU-2", "target": "target_2"}),
            "anchor_change",
            ("board_safety_setup", "board_validate"),
        ),
        (
            inputs(geometry={"erase_origin": 0, "erase_size": 8192}),
            "geometry_change",
            ("board_safety_setup", "board_validate"),
        ),
        (
            inputs(schema={"memory_map": 2}),
            "schema_change",
            ("board_safety_setup", "board_validate"),
        ),
        (
            inputs(profile={"board_id": "board", "display_name": "Renamed"}),
            "unclear_scope",
            ("board_safety_setup",),
        ),
    )

    for candidate, classification, remedy in cases:
        result = refresher(store).refresh(
            SafetyRefreshRequest("board", f"refresh-{classification}", candidate)
        )
        payload = result.to_payload()
        assert payload["observed"]["drift_classification"] == classification  # type: ignore[index]
        assert payload["validation_plan"] == list(remedy)


def test_public_bootloader_refresh_rebuilds_only_seeded_bootloader_regions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = inputs()
    reviewed_bootloader_envelope = contribution(
        "reviewed bootloader ceiling",
        RegionKind.BOOTLOADER_FLASH,
        0,
        0x2000,
        FingerprintSource.PACK,
        FingerprintSource.EVIDENCE,
    )
    current = SimpleNamespace(
        fingerprints=FingerprintSet.build(baseline),
        regions=regions() + (reviewed_bootloader_envelope,),
        source_manifest={"sources": {}},
    )
    captured: list[SafetyRefreshRequest] = []
    boot_elf = tmp_path / "bootloader.elf"
    boot_elf.write_bytes(b"boot")
    evidence = SimpleNamespace(
        artifact_present=True,
        flash_partition=AddressRange(0, 0x1800),
        loadable_segments=(
            SimpleNamespace(
                index=0,
                executable=True,
                runtime_range=AddressRange(0x100, 0x500),
                load_range=AddressRange(0x100, 0x500),
            ),
        ),
        hex_ranges=(),
        entry_point=0x100,
        vector_table=0,
        provenance=(SimpleNamespace(artifact_kind="elf", path=boot_elf, sha256="a" * 64),),
    )

    class CapturingRefresher:
        def refresh(self, request: SafetyRefreshRequest):
            captured.append(request)
            return SimpleNamespace(
                to_payload=lambda: {
                    "status": "safety_refresh_completed",
                    "observed": {"board_id": request.board_id},
                }
            )

    monkeypatch.setattr(
        server,
        "_safety_repository",
        SimpleNamespace(load_current=lambda _board: current),
    )
    monkeypatch.setattr(server, "require_reconciled_authority", lambda _current: None)
    monkeypatch.setattr(server, "_live_safety_inputs", lambda _board, _current: baseline)
    monkeypatch.setattr(server, "extract_build_evidence", lambda selection: evidence)
    monkeypatch.setattr(server, "_safety_refresher", CapturingRefresher())
    monkeypatch.setattr(server, "_safety_continuation", lambda _prefix: "refresh-test")

    payload = server._run_board_safety_refresh(
        "board",
        bootloader_elf=str(boot_elf),
    )

    assert payload["status"] == "safety_refresh_completed", payload
    assert len(captured) == 1
    request = captured[0]
    assert request.rebuilt_groups == (FingerprintSource.BOOTLOADER_ARTIFACTS,)
    assert {item.region.kind for item in request.replacement_regions} == {
        RegionKind.BOOTLOADER_FLASH
    }
    assert request.inputs.application_artifacts == baseline.application_artifacts
    assert request.inputs.bootloader_artifacts != baseline.bootloader_artifacts


def test_public_refresh_schema_advertises_symmetric_application_and_bootloader_inputs() -> None:
    tool = {item.name: item for item in server.mcp._tool_manager.list_tools()}[
        "board_safety_refresh"
    ]
    assert set(tool.parameters["properties"]) == {
        "board_id",
        "application_elf",
        "application_hex",
        "application_map",
        "bootloader_elf",
        "bootloader_hex",
        "bootloader_map",
    }


def test_public_bootloader_refresh_without_existing_authority_is_honestly_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = inputs()
    current = SimpleNamespace(
        fingerprints=FingerprintSet.build(baseline),
        # The only bootloader region is owned by the old build. It must not
        # authorize its own replacement as if it were an independent ceiling.
        regions=regions(),
        source_manifest={"sources": {}},
    )
    boot_elf = tmp_path / "bootloader.elf"
    boot_elf.write_bytes(b"boot")
    evidence = SimpleNamespace(
        flash_partition=AddressRange(0, 0x1800),
        loadable_segments=(),
        hex_ranges=(AddressRange(0, 0x100),),
        entry_point=0,
        vector_table=0,
        provenance=(SimpleNamespace(artifact_kind="elf", path=boot_elf, sha256="a" * 64),),
    )
    monkeypatch.setattr(
        server, "_safety_repository", SimpleNamespace(load_current=lambda _board: current)
    )
    monkeypatch.setattr(server, "require_reconciled_authority", lambda _current: None)
    monkeypatch.setattr(server, "_live_safety_inputs", lambda _board, _current: baseline)
    monkeypatch.setattr(server, "extract_build_evidence", lambda _selection: evidence)
    monkeypatch.setattr(
        server,
        "_safety_refresher",
        SafetyRefresher(FirmStore(tmp_path), authority_verifier=lambda _artifacts: None),
    )

    payload = server._run_board_safety_refresh(
        "board",
        bootloader_elf=str(boot_elf),
    )

    assert payload["status"] == "safety_refresh_blocked"
    assert "continuation_id" not in payload
    assert payload["accepted_response"] is None
    assert payload["validation_plan"] == []
    assert payload["observed"]["drift_classification"] == "bootloader_authority_missing"  # type: ignore[index]
    agent_prompt = payload["agent_prompt"]
    assert isinstance(agent_prompt, str)
    assert "maintainers" in agent_prompt
    assert Path(payload["observed"]["report"]).is_file()  # type: ignore[index]


def test_bootloader_build_region_cannot_widen_independent_reviewed_ceiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = inputs()
    reviewed = contribution(
        "reviewed bootloader ceiling",
        RegionKind.BOOTLOADER_FLASH,
        0,
        0x1000,
        FingerprintSource.PACK,
        FingerprintSource.EVIDENCE,
    )
    current = SimpleNamespace(
        fingerprints=FingerprintSet.build(baseline),
        regions=regions() + (reviewed,),
        source_manifest={"sources": {}},
    )
    boot_elf = tmp_path / "outside.elf"
    boot_elf.write_bytes(b"boot")
    evidence = SimpleNamespace(
        flash_partition=AddressRange(0x1800, 0x1900),
        loadable_segments=(),
        hex_ranges=(AddressRange(0x1800, 0x1900),),
        entry_point=0x1800,
        vector_table=0x1800,
        provenance=(SimpleNamespace(artifact_kind="elf", path=boot_elf, sha256="a" * 64),),
    )
    monkeypatch.setattr(
        server, "_safety_repository", SimpleNamespace(load_current=lambda _board: current)
    )
    monkeypatch.setattr(server, "require_reconciled_authority", lambda _current: None)
    monkeypatch.setattr(server, "_live_safety_inputs", lambda _board, _current: baseline)
    monkeypatch.setattr(server, "extract_build_evidence", lambda _selection: evidence)
    monkeypatch.setattr(
        server,
        "_safety_refresher",
        SafetyRefresher(FirmStore(tmp_path), authority_verifier=lambda _artifacts: None),
    )

    payload = server._run_board_safety_refresh("board", bootloader_elf=str(boot_elf))

    assert payload["status"] == "safety_refresh_blocked"
    agent_prompt = payload["agent_prompt"]
    assert isinstance(agent_prompt, str)
    assert "existing reviewed bootloader partition" in agent_prompt


def test_public_safety_setup_uses_terminal_status_for_unreviewed_board(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_current(_board_id: str):
        raise SafetyArtifactError("missing")

    monkeypatch.setattr(server, "_safety_repository", SimpleNamespace(load_current=missing_current))
    monkeypatch.setattr(server, "_bootstrap_safety_inputs", lambda _board: inputs())
    monkeypatch.setattr(
        server,
        "_profile_repository",
        SimpleNamespace(
            load=lambda _board, include_legacy=False: SimpleNamespace(
                mcu_part_number="MCU-UNREVIEWED"
            )
        ),
    )
    monkeypatch.setattr(
        server,
        "catalog_board_for_mcu",
        lambda _part: SimpleNamespace(
            board_type="unreviewed_board", automatic_setup_reviewed=False
        ),
    )
    monkeypatch.setattr(server, "reviewed_setup_board_types", lambda: ("nrf52840dk",))
    monkeypatch.setattr(server, "_safety_builder", SafetyMapBuilder(FirmStore(tmp_path)))

    payload = server._run_board_safety_setup("board")

    assert payload["status"] == "safety_setup_unsupported_board"
    assert "continuation_id" not in payload
    assert payload["accepted_response"] is None
    agent_prompt = payload["agent_prompt"]
    assert isinstance(agent_prompt, str)
    assert "nrf52840dk" in agent_prompt
    assert "caller-supplied allowed ranges" in agent_prompt.lower()


def test_public_pack_evidence_drift_reloads_current_pinned_sources_and_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FirmStore(tmp_path)
    old = inputs(
        profile={
            "board_id": "board",
            "display_name": "Board",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "safety_ref": None,
        },
        part_target={
            "board_type": "reviewed_board",
            "mcu_part_number": "MCU-1",
            "target": "target_1",
        },
        pack={"asset_sha256": "old", "document": {"schema_version": 2}},
        evidence={
            "official_document": {
                "asset_sha256": "old",
                "datasheet_sha256": "reviewed-digest",
                "document": {"schema_version": 2},
            },
            "reconciliation": {
                "status": "agreement",
                "erase_geometry": {"erase_origin": 0, "erase_size": 4096},
            },
            "deployment_policy": {"application_start": 0x2000, "application_end": 0x8000},
        },
        application_artifacts={"configuration": None, "artifacts": []},
        bootloader_artifacts={"configuration": None, "artifacts": []},
        geometry={
            "flash_start": 0,
            "flash_end": 0x10000,
            "ram_start": 0x20000000,
            "ram_end": 0x20001000,
            "erase_origin": 0,
            "erase_size": 4096,
        },
    )
    hardware = hardware_replacements()
    assert (
        SafetyMapBuilder(store)
        .build(SafetySetupRequest("board", "setup-old", old, hardware))
        .status
        == "safety_setup_completed"
    )
    repository = SafetyArtifactRepository(store)
    memory_path = repository.paths("board")["memory_map"]
    old_map = memory_path.read_bytes()
    new_source_record = {
        "device_support": {
            "asset_sha256": "new-pack",
            "document": {"schema_version": 2},
            "runtime": {"pyocd_version": "new"},
        },
        "official_document": {
            "asset_sha256": "new-official",
            "datasheet_sha256": "reviewed-digest",
            "document": {"schema_version": 2},
        },
        "reconciliation": {
            "status": "agreement",
            "erase_geometry": {"erase_origin": 0, "erase_size": 4096},
        },
    }
    bundle = SimpleNamespace(
        source_record=lambda: new_source_record,
        reconciliation=SimpleNamespace(
            erase_geometry=SimpleNamespace(erase_origin=0, erase_size=4096),
            regions=tuple(
                SimpleNamespace(to_safety_region=lambda region=item.region: region)
                for item in hardware
            ),
        ),
    )
    catalog = SimpleNamespace(
        package_part_number="MCU-1",
        pyocd_target="target_1",
        application_start=0x2000,
        application_end=0x8000,
        flash_start=0,
        flash_end=0x10000,
        ram_start=0x20000000,
        ram_end=0x20001000,
    )
    verifier_calls = 0
    restamps: list[tuple[str, str]] = []

    def verifier(_artifacts) -> None:
        nonlocal verifier_calls
        verifier_calls += 1
        if verifier_calls == 1:
            raise SafetyArtifactError("pinned authority changed")
        assert memory_path.read_bytes() == old_map
        assert restamps == []

    monkeypatch.setattr(server, "_firm_store", store)
    monkeypatch.setattr(server, "_safety_repository", repository)
    monkeypatch.setattr(
        server,
        "_restamp_after_refresh",
        lambda board, aggregate: restamps.append((board, aggregate)),
    )
    monkeypatch.setattr(server, "require_reconciled_authority", verifier)
    monkeypatch.setattr(server, "catalog_board", lambda _board_type: catalog)
    monkeypatch.setattr(server, "load_pinned_reviewed_evidence", lambda _catalog, _digest: bundle)
    monkeypatch.setattr(
        server,
        "_profile_repository",
        SimpleNamespace(
            load=lambda _board, include_legacy=False: SimpleNamespace(
                mcu_part_number="MCU-1",
                board=SimpleNamespace(pyocd_target="target_1"),
                to_document=lambda: {
                    "board_id": "board",
                    "display_name": "Board",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-07-17T00:00:00+00:00",
                    "safety_ref": ".firm/safety/board/memory_map.yaml",
                },
            )
        ),
    )
    monkeypatch.setattr(server, "_safety_continuation", lambda _prefix: "refresh-pins")

    payload = server._run_board_safety_refresh("board")

    assert payload["status"] == "safety_refresh_completed", payload
    assert payload["observed"]["drift_classification"] == (  # type: ignore[index]
        "pack_and_official_evidence_change"
    )
    assert set(payload["observed"]["rebuilt_groups"]) == {"pack", "evidence"}  # type: ignore[index]
    assert Path(payload["observed"]["report"]).is_file()  # type: ignore[index]
    assert verifier_calls == 2
    assert len(restamps) == 1
