from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from pyocd_debug_mcp.firmstore.profiles import ProfileRepository
from pyocd_debug_mcp.firmstore.reports import ReportWriter
from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.pack_provision import (
    MAX_CMSIS_PACK_ARCHIVE_BYTES,
    DeviceBinding,
    sha256_file,
)
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


def test_candidate_accepts_case_insensitive_pack_extension(tmp_path: Path) -> None:
    source = tmp_path / "Vendor.Device.PACK"
    source.write_bytes(b"pack")

    accepted = candidate(source, filename="Vendor.Device.PACK")

    assert accepted.filename == "Vendor.Device.PACK"


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def test_checksum_mismatch_never_stages_or_promotes(tmp_path: Path) -> None:
    source = tmp_path / "candidate.pack"
    source.write_bytes(b"pack-one")
    store = FirmStore(tmp_path)
    pipeline = PackCandidatePipeline(
        store,
        enumerate_targets=lambda _path, _sha256: ("target_a",),
        live_connect=lambda _target, _path, _sha256, _pdsc: None,
    )

    with pytest.raises(PackCandidateError) as mismatch:
        pipeline.validate(candidate(source, checksum="0" * 64), required_target="target_a")

    assert mismatch.value.code == "package/checksum-mismatch"
    assert not store.layout.pack_files.exists()
    assert not store.layout.pack_manifest.exists()


def test_oversized_candidate_refuses_before_read_or_live_attach(tmp_path: Path) -> None:
    source = tmp_path / "oversized.pack"
    with source.open("wb") as stream:
        stream.truncate(MAX_CMSIS_PACK_ARCHIVE_BYTES + 1)
    store = FirmStore(tmp_path)
    pipeline = PackCandidatePipeline(
        store,
        enumerate_targets=lambda _path, _sha256: pytest.fail("oversized pack must not be parsed"),
        live_connect=lambda _target, _path, _sha256, _pdsc: pytest.fail(
            "oversized pack must not be attached"
        ),
    )

    with pytest.raises(PackCandidateError, match="size is outside"):
        pipeline.validate(candidate(source), required_target="candidate")

    assert not store.layout.pack_files.exists()


def test_target_absent_records_observed_listing_and_removes_stage(tmp_path: Path) -> None:
    source = tmp_path / "candidate.pack"
    payload = b"pack-two"
    source.write_bytes(payload)
    pipeline = PackCandidatePipeline(
        FirmStore(tmp_path),
        enumerate_targets=lambda _path, _sha256: ("other_target",),
        live_connect=lambda _target, _path, _sha256, _pdsc: None,
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

    def enumerate_targets(_path: Path, _sha256: str):
        nonlocal calls
        calls += 1
        return ("wrong_target",)

    pipeline = PackCandidatePipeline(
        FirmStore(tmp_path), enumerate_targets=enumerate_targets, live_connect=lambda _t, _p, _sha256, _pdsc: None
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
        enumerate_targets=lambda _path, _sha256: ("target_a",),
        live_connect=lambda _target, _path, _sha256, _pdsc: (_ for _ in ()).throw(OSError("probe missing")),
    )
    with pytest.raises(PackCandidateError) as failed:
        pipeline.validate(candidate(source, checksum=digest(payload)), required_target="target_a")

    assert failed.value.code == "package/live-connect-failed"
    assert not store.layout.pack_manifest.exists()
    assert not any(store.layout.pack_files.glob("*.pack"))


def test_staged_bytes_must_replay_the_previously_derived_device_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "candidate.pack"
    payload = b"changed-between-research-and-quarantine"
    source.write_bytes(payload)
    store = FirmStore(tmp_path)
    pipeline = PackCandidatePipeline(
        store,
        enumerate_targets=lambda _path, _sha256: pytest.fail("drift must fail before enumeration"),
        live_connect=lambda _target, _path, _sha256, _pdsc: pytest.fail("drift must fail before attach"),
    )
    expected = DeviceBinding("PART-A", "PART-A", "parta")
    monkeypatch.setattr(
        "pyocd_debug_mcp.setup_flow.device_support.derive_candidate_binding",
        lambda _path, _part: DeviceBinding("PART-A", "PART-A-CHANGED", "partachanged"),
    )

    with pytest.raises(PackCandidateError) as drift:
        pipeline.validate_device(
            candidate(source, checksum=digest(payload)),
            required_target="parta",
            device_binding=expected,
        )

    assert drift.value.code == "package/device-binding-drift"
    assert not any(store.layout.pack_files.glob("*.pack"))
    assert not store.layout.pack_manifest.exists()


def test_device_binding_target_mismatch_is_refused_before_staging_or_attach(
    tmp_path: Path,
) -> None:
    source = tmp_path / "candidate.pack"
    payload = b"target-mismatch"
    source.write_bytes(payload)
    store = FirmStore(tmp_path)
    pipeline = PackCandidatePipeline(
        store,
        enumerate_targets=lambda _path, _sha256: pytest.fail("mismatch must fail before enumeration"),
        live_connect=lambda _target, _path, _sha256, _pdsc: pytest.fail("mismatch must fail before attach"),
    )

    with pytest.raises(PackCandidateError) as mismatch:
        pipeline.validate_device(
            candidate(source, checksum=digest(payload)),
            required_target="agent-proposed-target",
            device_binding=DeviceBinding("PART-A", "PART-A", "server-derived-target"),
        )

    assert mismatch.value.code == "package/device-binding-target-mismatch"
    assert not store.layout.pack_files.exists()
    assert not store.layout.pack_manifest.exists()


def test_promotion_rechecks_quarantined_bytes_after_live_attach(tmp_path: Path) -> None:
    source = tmp_path / "candidate.pack"
    payload = b"validated-before-promotion"
    source.write_bytes(payload)
    store = FirmStore(tmp_path / "project")
    pipeline = PackCandidatePipeline(
        store,
        enumerate_targets=lambda _path, _sha256: ("target_a",),
        live_connect=lambda _target, _path, _sha256, _pdsc: None,
    )
    validated = pipeline.validate(
        candidate(source, checksum=digest(payload)), required_target="target_a"
    )
    validated.staged_path.write_bytes(b"changed-after-attach")

    with pytest.raises(PackCandidateError) as changed:
        pipeline.promote(validated, board_id="board_a")

    assert changed.value.code == "package/staged-bytes-changed"
    assert not validated.staged_path.exists()
    assert not store.layout.pack_manifest.exists()


def test_promotion_rebinds_validated_payload_before_manifest_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "candidate.pack"
    payload = b"validated-promotion-payload"
    source.write_bytes(payload)
    store = FirmStore(tmp_path / "project")
    pipeline = PackCandidatePipeline(
        store,
        enumerate_targets=lambda _path, _sha256: ("target_a",),
        live_connect=lambda _target, _path, _sha256, _pdsc: None,
    )
    validated = pipeline.validate(
        candidate(source, checksum=digest(payload)), required_target="target_a"
    )
    original_hash = sha256_file
    calls = 0

    def race(path: Path) -> str:
        nonlocal calls
        calls += 1
        result = original_hash(path)
        if calls == 2:
            path.write_bytes(b"raced-after-final-check")
        return result

    monkeypatch.setattr("pyocd_debug_mcp.setup_flow.packs.sha256_file", race)

    pipeline.promote(validated, board_id="board_a")

    assert validated.staged_path.read_bytes() == payload
    manifest = yaml.safe_load(store.layout.pack_manifest.read_text(encoding="utf-8"))
    assert manifest["packs"][0]["sha256"] == digest(payload)


def test_concurrent_promotions_merge_without_lost_bindings(tmp_path: Path) -> None:
    store = FirmStore(tmp_path / "project")
    validated = []
    for index in range(2):
        payload = f"pack-{index}".encode()
        source = tmp_path / f"source-{index}.pack"
        source.write_bytes(payload)
        pipeline = PackCandidatePipeline(
            store,
            enumerate_targets=lambda _path, _sha256, i=index: (f"target_{i}",),
            live_connect=lambda _target, _path, _sha256, _pdsc: None,
        )
        item = pipeline.validate(
            PackCandidate(
                f"Vendor.Device{index}",
                "1.0.0",
                f"Vendor.Device{index}.pack",
                f"https://vendor.example/Device{index}.pack",
                source,
                digest(payload),
            ),
            required_target=f"target_{index}",
        )
        validated.append((pipeline, item, f"board_{index}"))

    with ThreadPoolExecutor(max_workers=2) as executor:
        paths = tuple(
            executor.map(lambda row: row[0].promote(row[1], board_id=row[2]), validated)
        )

    assert paths == (store.layout.pack_manifest, store.layout.pack_manifest)
    manifest = yaml.safe_load(store.layout.pack_manifest.read_text(encoding="utf-8"))
    assert {entry["id"] for entry in manifest["packs"]} == {
        "Vendor.Device0",
        "Vendor.Device1",
    }


def test_third_material_failure_exhausts_retry_budget(tmp_path: Path) -> None:
    store = FirmStore(tmp_path)
    pipeline = PackCandidatePipeline(
        store,
        enumerate_targets=lambda _path, _sha256: ("wrong",),
        live_connect=lambda _target, _path, _sha256, _pdsc: None,
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
        enumerate_targets=lambda _path, _sha256: (),
        live_connect=lambda _target, _path, _sha256, _pdsc: None,
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
        enumerate_targets=lambda _path, _sha256: ("target_a", "target_b"),
        live_connect=lambda target, path, _sha256, _pdsc: (
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


def test_device_validation_promotes_server_derived_exact_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "candidate.pack"
    payload = b"validated-device-pack"
    source.write_bytes(payload)
    store = FirmStore(tmp_path)
    pipeline = PackCandidatePipeline(
        store,
        enumerate_targets=lambda _path, _sha256: ("vendorpartx",),
        live_connect=lambda target, _path, _sha256, _pdsc: None if target == "vendorpartx" else pytest.fail(),
    )
    binding = DeviceBinding("VENDORPART7", "VendorPartX", "vendorpartx")
    replayed = {
        "VENDORPART7": binding,
        "VENDORPART8": DeviceBinding("VENDORPART8", "VendorPartY", "vendorpartx"),
    }
    monkeypatch.setattr(
        "pyocd_debug_mcp.setup_flow.device_support.derive_candidate_binding",
        lambda _path, part: replayed[part],
    )

    validated = pipeline.validate_device(
        candidate(source, checksum=digest(payload)),
        required_target="vendorpartx",
        device_binding=binding,
    )
    manifest_path = pipeline.promote(validated, board_id="custom_board")

    entry = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))["packs"][0]
    assert entry["device_bindings"] == [
        {
            "part_number": "VENDORPART7",
            "pdsc_device": "VendorPartX",
            "pyocd_target": "vendorpartx",
        }
    ]

    second_pipeline = PackCandidatePipeline(
        store,
        enumerate_targets=lambda _path, _sha256: ("vendorpartx",),
        live_connect=lambda _target, _path, _sha256, _pdsc: None,
    )
    second_binding = replayed["VENDORPART8"]
    second = second_pipeline.validate_device(
        candidate(source, checksum=digest(payload)),
        required_target="vendorpartx",
        device_binding=second_binding,
    )
    second_pipeline.promote(second, board_id="second_board")
    merged = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))["packs"][0]
    assert merged["needed_by_boards"] == ["custom_board", "second_board"]
    assert [item["part_number"] for item in merged["device_bindings"]] == [
        "VENDORPART7",
        "VENDORPART8",
    ]
