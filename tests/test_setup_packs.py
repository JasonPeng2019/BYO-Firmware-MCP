from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from pyocd_debug_mcp.firmstore.profiles import ProfileRepository
from pyocd_debug_mcp.firmstore.reports import ReportWriter
from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.setup_flow.packs import (
    PackCandidate,
    PackCandidateError,
    PackCandidatePipeline,
)


def candidate(
    source: Path,
    *,
    filename: str = "Vendor.Device.1.0.0.pack",
    url: str = "https://vendor.example/Device.1.0.0.pack",
    checksum: str | None = None,
) -> PackCandidate:
    return PackCandidate(
        pack_id="Vendor.Device_DFP",
        version="1.0.0",
        filename=filename,
        url=url,
        source_path=source,
        official_sha256=checksum,
    )


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def test_checksum_mismatch_never_stages_or_promotes(tmp_path: Path) -> None:
    source = tmp_path / "candidate.pack"
    source.write_bytes(b"pack-one")
    store = FirmStore(tmp_path)
    pipeline = PackCandidatePipeline(
        store,
        enumerate_targets=lambda _path: ("target_a",),
        live_connect=lambda _target, _path: None,
    )

    with pytest.raises(PackCandidateError) as mismatch:
        pipeline.validate(candidate(source, checksum="0" * 64), required_target="target_a")

    assert mismatch.value.code == "package/checksum-mismatch"
    assert not store.layout.pack_files.exists()
    assert not store.layout.pack_manifest.exists()


def test_target_absent_records_observed_listing_and_removes_stage(tmp_path: Path) -> None:
    source = tmp_path / "candidate.pack"
    payload = b"pack-two"
    source.write_bytes(payload)
    pipeline = PackCandidatePipeline(
        FirmStore(tmp_path),
        enumerate_targets=lambda _path: ("other_target",),
        live_connect=lambda _target, _path: None,
    )

    with pytest.raises(PackCandidateError) as absent:
        pipeline.validate(candidate(source, checksum=digest(payload)), required_target="target_a")

    assert absent.value.code == "package/target-absent"
    assert absent.value.failure is not None
    assert absent.value.failure.observed["enumerated_targets"] == ["other_target"]
    assert not any(pipeline._store.layout.pack_files.glob("*.pack"))


def test_duplicate_and_renamed_candidate_are_not_revalidated(tmp_path: Path) -> None:
    source = tmp_path / "candidate.pack"
    payload = b"same-content"
    source.write_bytes(payload)
    calls = 0

    def enumerate_targets(_path: Path):
        nonlocal calls
        calls += 1
        return ("wrong_target",)

    pipeline = PackCandidatePipeline(
        FirmStore(tmp_path), enumerate_targets=enumerate_targets, live_connect=lambda _t, _p: None
    )
    with pytest.raises(PackCandidateError):
        pipeline.validate(candidate(source, checksum=digest(payload)), required_target="target_a")
    with pytest.raises(PackCandidateError) as renamed:
        pipeline.validate(
            candidate(source, filename="Renamed.pack", checksum=digest(payload)),
            required_target="target_a",
        )

    assert renamed.value.code == "package/duplicate-candidate"
    assert calls == 1


def test_live_connect_failure_prevents_manifest_promotion(tmp_path: Path) -> None:
    source = tmp_path / "candidate.pack"
    payload = b"connect-fails"
    source.write_bytes(payload)
    store = FirmStore(tmp_path)
    pipeline = PackCandidatePipeline(
        store,
        enumerate_targets=lambda _path: ("target_a",),
        live_connect=lambda _target, _path: (_ for _ in ()).throw(OSError("probe missing")),
    )
    with pytest.raises(PackCandidateError) as failed:
        pipeline.validate(candidate(source, checksum=digest(payload)), required_target="target_a")

    assert failed.value.code == "package/live-connect-failed"
    assert not store.layout.pack_manifest.exists()
    assert not any(store.layout.pack_files.glob("*.pack"))


def test_third_material_failure_exhausts_retry_budget(tmp_path: Path) -> None:
    store = FirmStore(tmp_path)
    pipeline = PackCandidatePipeline(
        store,
        enumerate_targets=lambda _path: ("wrong",),
        live_connect=lambda _target, _path: None,
    )
    codes = []
    for index in range(3):
        source = tmp_path / f"candidate-{index}.pack"
        payload = f"content-{index}".encode()
        source.write_bytes(payload)
        with pytest.raises(PackCandidateError) as rejected:
            pipeline.validate(
                candidate(
                    source,
                    url=f"https://vendor.example/{index}.pack",
                    checksum=digest(payload),
                ),
                required_target="target_a",
            )
        codes.append(rejected.value.code)
    assert codes[-1] == "package/retry-exhausted"


def test_failed_candidate_metadata_is_only_in_report_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "candidate.pack"
    source.write_bytes(b"bad-checksum")
    store = FirmStore(tmp_path)
    pipeline = PackCandidatePipeline(
        store,
        enumerate_targets=lambda _path: (),
        live_connect=lambda _target, _path: None,
    )
    with pytest.raises(PackCandidateError):
        pipeline.validate(candidate(source, checksum="f" * 64), required_target="target_a")

    reports = ReportWriter(store)
    paths = reports.create_setup(
        "attempt-1",
        {"candidate_validation_results": [item.to_document() for item in pipeline.failures]},
    )
    report = json.loads(paths.report.read_text(encoding="utf-8"))

    assert report["candidate_validation_results"][0]["candidate"]["id"] == "Vendor.Device_DFP"
    assert not store.layout.pack_manifest.exists()
    assert not store.layout.boards.exists()
    assert not any(store.layout.pack_files.glob("*.pack"))


def test_success_promotes_manifest_and_profile_has_no_pack_metadata(tmp_path: Path) -> None:
    source = tmp_path / "candidate.pack"
    payload = b"valid-pack"
    source.write_bytes(payload)
    store = FirmStore(tmp_path)
    pipeline = PackCandidatePipeline(
        store,
        enumerate_targets=lambda _path: ("target_a", "target_b"),
        live_connect=lambda target, path: (
            None if target == "target_a" and path.exists() else (_ for _ in ()).throw(OSError())
        ),
    )

    validated = pipeline.validate(
        candidate(source, checksum=digest(payload)), required_target="target_a"
    )
    manifest_path = pipeline.promote(validated, board_id="bench_board")

    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert manifest["packs"][0]["sha256"] == digest(payload)
    assert manifest["packs"][0]["provides_targets"] == ["target_a", "target_b"]

    legacy = tmp_path / "boards"
    legacy.mkdir()
    profiles = ProfileRepository(store, legacy_board_dir=legacy)
    profile = profiles.commit_core(
        profiles.stage_core(
            {
                "board_id": "bench_board",
                "display_name": "Bench Board",
                "mcu_part_number": "Part-Exact",
                "mcu_family": "stm32l4",
                "probe_family": "cmsis-dap",
                "pyocd_target": "target_a",
            }
        )
    )
    document = profile.to_document()
    assert not {
        "pack_id",
        "pack_name",
        "pack_sha256",
        "pack_url",
        "pack_version",
        "provides_targets",
    }.intersection(document)
