from __future__ import annotations

from pathlib import Path

import pytest

from pyocd_debug_mcp.firmstore.profiles import ProfileRepository
from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.setup_flow.research import ResearchTracker
from pyocd_debug_mcp.setup_flow.targets import (
    EnrichmentValidator,
    ProfileCommitCoordinator,
    SiliconIdentityCandidate,
    TargetResolutionError,
    TargetResolver,
)


def core_fields() -> dict[str, object]:
    return {
        "board_id": "bench_board",
        "display_name": "Bench Board",
        "mcu_part_number": "STM32L476RGT6-Exact",
        "mcu_family": "stm32l4",
        "probe_family": "stlink",
        "pyocd_target": "stm32l476rgtx",
    }


def repository(tmp_path: Path) -> ProfileRepository:
    legacy = tmp_path / "boards"
    legacy.mkdir()
    return ProfileRepository(FirmStore(tmp_path), legacy_board_dir=legacy)


def test_exact_auto_detection_skips_research_and_unknown_requests_it() -> None:
    tracker = ResearchTracker()
    resolver = TargetResolver(tracker)

    exact = resolver.resolve_detection(
        board_id="bench_board",
        mcu_part_number="STM32L476RGT6-Exact",
        detected_targets=("stm32l476rgtx",),
        continuation_token="token",
    )
    unknown = resolver.resolve_detection(
        board_id="bench_board",
        mcu_part_number="STM32L476RGT6-Exact",
        detected_targets=(),
        continuation_token="token",
    )

    assert exact.status == "exact" and exact.target == "stm32l476rgtx"
    assert exact.research_request is None
    assert unknown.status == "research"
    assert unknown.research_request is not None
    assert unknown.research_request.requested_fields == (
        "target_identity",
        "evidence",
        "reasoning_summary",
    )
    assert unknown.agent_prompt is not None


def test_target_candidate_requires_exact_reviewed_mapping_and_support() -> None:
    assert (
        TargetResolver.validate_candidate(
            "stm32l476rgtx",
            expected_target="stm32l476rgtx",
            built_in_targets=("stm32l476rgtx",),
        )
        == "built_in"
    )
    with pytest.raises(TargetResolutionError) as mismatch:
        TargetResolver.validate_candidate(
            "nrf52840",
            expected_target="opaque-reviewed-alias",
            built_in_targets=("nrf52840",),
        )
    assert mismatch.value.code == "target/reviewed-mapping-mismatch"


def test_live_connect_failure_occurs_before_core_profile_commit(tmp_path: Path) -> None:
    profiles = repository(tmp_path)
    coordinator = ProfileCommitCoordinator(
        profiles, live_connect=lambda _target, _pack: (_ for _ in ()).throw(OSError("probe"))
    )

    with pytest.raises(TargetResolutionError) as failed:
        coordinator.commit_core(core_fields())

    assert failed.value.code == "target/live-connect-failed"
    assert not profiles.store.layout.board_profile("bench_board").exists()


def test_successful_core_commit_orders_live_connect_before_write(tmp_path: Path) -> None:
    profiles = repository(tmp_path)
    events: list[str] = []

    def connect(target: str, pack_path: str | None) -> None:
        assert target == "stm32l476rgtx"
        assert pack_path is None
        assert not profiles.store.layout.board_profile("bench_board").exists()
        events.append("live_connect")

    coordinator = ProfileCommitCoordinator(profiles, live_connect=connect)
    committed = coordinator.commit_core(core_fields())
    events.append("profile_commit")

    assert committed.mcu_part_number == "STM32L476RGT6-Exact"
    assert events == ["live_connect", "profile_commit"]


def test_optional_enrichment_requires_safe_successful_live_reads(tmp_path: Path) -> None:
    reads = {0x08000000: 0x12345678, 0xE0042000: 0x10016415}
    validator = EnrichmentValidator(
        safe_readable=lambda address, width: address in reads and width == 32,
        read_value=lambda address, _width: reads[address],
    )
    test_read = validator.test_read_address(0x08000000)
    silicon = validator.silicon_identity(
        SiliconIdentityCandidate(
            address=0xE0042000,
            expected=0x10016400,
            mask=0xFFFFFF00,
            label="device id",
        )
    )
    absent = validator.silicon_identity(None)

    assert test_read.fields == {"test_read_address": 0x08000000}
    assert silicon.fields["silicon_id_address"] == 0xE0042000
    assert absent.fields == {}
    with pytest.raises(TargetResolutionError) as unsafe:
        validator.test_read_address(0x40000000)
    assert unsafe.value.code == "enrichment/unsafe-read"

    profiles = repository(tmp_path)
    coordinator = ProfileCommitCoordinator(profiles, live_connect=lambda _target, _pack: None)
    coordinator.commit_core(core_fields())
    enriched = coordinator.commit_optional("bench_board", silicon)
    assert enriched.to_document()["silicon_id_mask"] == 0xFFFFFF00


def test_silicon_identity_mismatch_is_rejected() -> None:
    validator = EnrichmentValidator(
        safe_readable=lambda _address, _width: True,
        read_value=lambda _address, _width: 0xDEADBEEF,
    )
    with pytest.raises(TargetResolutionError) as mismatch:
        validator.silicon_identity(SiliconIdentityCandidate(0x1000, 0x1234, 0xFFFF))
    assert mismatch.value.code == "enrichment/silicon-mismatch"
