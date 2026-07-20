from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

import pytest

from pyocd_debug_mcp.board_config import BoardConfig, LegacyPackNameWarning
from pyocd_debug_mcp.firmstore.profiles import (
    PROFILE_SCHEMA_VERSION,
    ProfileError,
    ProfileRepository,
    StaleProfileStageError,
)
from pyocd_debug_mcp.firmstore.store import FirmStore, PersistedAuthorityError
from pyocd_debug_mcp.pack_provision import LiveIdentityProof, load_manifest
from pyocd_debug_mcp.setup_flow import device_support
from pyocd_debug_mcp.setup_flow.datasheet_evidence import capture_datasheet_evidence
from pyocd_debug_mcp.setup_flow.device_support import DeviceSupportCandidate


PACKAGE_METADATA_FIELDS = {
    "needed_by_boards",
    "pack_filename",
    "pack_id",
    "pack_name",
    "pack_sha256",
    "pack_url",
    "pack_version",
    "provides_targets",
}


def core_fields(
    board_id: str = "bench_board",
    display_name: str = "Bench Board",
    mcu_part_number: str = "STM32L476RGTx-Exact",
) -> dict[str, object]:
    return {
        "board_id": board_id,
        "display_name": display_name,
        "mcu_part_number": mcu_part_number,
        "mcu_family": "stm32l4",
        "probe_family": "stlink",
        "pyocd_target": "stm32l476rgtx",
    }


DeviceSupportVerifier = Callable[[str, BoardConfig, Mapping[str, str]], None]


def repository(
    tmp_path: Path, *, device_support_verifier: DeviceSupportVerifier | None = None
) -> ProfileRepository:
    legacy = tmp_path / "boards"
    legacy.mkdir(exist_ok=True)
    return ProfileRepository(
        FirmStore(tmp_path),
        legacy_board_dir=legacy,
        device_support_verifier=device_support_verifier,
    )


def captured_datasheet_fields(tmp_path: Path, profiles: ProfileRepository) -> dict[str, str]:
    source = tmp_path / "device-datasheet.pdf"
    source.write_bytes(b"%PDF-1.7\nminimal test datasheet\n%%EOF\n")
    evidence = capture_datasheet_evidence(profiles.store, source)
    return {
        "datasheet_sha256": evidence.sha256,
        "datasheet_ref": evidence.reference,
    }


def test_core_stage_and_commit_preserve_exact_part_and_absolute_timestamps(
    tmp_path: Path,
) -> None:
    profiles = repository(tmp_path)
    exact_part = "  nRF52833-QIAA / User Case  "

    staged = profiles.stage_core(
        core_fields(mcu_part_number=exact_part)
        | {
            "mcu_family": "nrf52833",
            "probe_family": "jlink",
            "pyocd_target": "nrf52833",
        }
    )
    assert not staged.profile.source_path.exists()
    committed = profiles.commit_core(staged)

    assert committed.schema_version == PROFILE_SCHEMA_VERSION
    assert committed.mcu_part_number == exact_part
    assert committed.to_document()["mcu_part_number"] == exact_part
    assert PACKAGE_METADATA_FIELDS.isdisjoint(committed.to_document())
    assert committed.created_at is not None
    assert committed.updated_at is not None
    for timestamp in (committed.created_at, committed.updated_at):
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        assert parsed.utcoffset() is not None


def test_unicode_display_name_round_trips_losslessly_through_disk(tmp_path: Path) -> None:
    display_name = "開発ボード – Café 🚀"
    profiles = repository(tmp_path)
    committed = profiles.commit_core(
        profiles.stage_core(core_fields(display_name=display_name))
    )

    reloaded = repository(tmp_path).load(committed.board_id, include_legacy=False)

    assert committed.display_name == display_name
    assert reloaded.display_name == display_name
    assert display_name in committed.source_path.read_text(encoding="utf-8")


def test_core_commit_excludes_optional_fields_until_separately_staged(tmp_path: Path) -> None:
    profiles = repository(tmp_path)
    core = profiles.commit_core(profiles.stage_core(core_fields()))

    assert "test_read_address" not in core.to_document()
    staged_optional = profiles.stage_optional(
        core.board_id,
        {
            "test_read_address": 0x08000000,
            "expected_uart_substring": "boot ok",
        },
    )
    assert profiles.load(core.board_id).to_document().get("test_read_address") is None
    enriched = profiles.commit_optional(staged_optional)

    assert enriched.to_document()["test_read_address"] == 0x08000000
    assert enriched.to_document()["expected_uart_substring"] == "boot ok"


def test_generic_device_support_source_is_closed_and_round_trips(tmp_path: Path) -> None:
    """A profile may record only the server-generated generic source identity."""

    calls: list[tuple[str, str, dict[str, str]]] = []

    def verify(part_number: str, board: BoardConfig, source: Mapping[str, str]) -> None:
        calls.append((part_number, board.pyocd_target, dict(source)))

    profiles = repository(tmp_path, device_support_verifier=verify)
    profiles.commit_core(profiles.stage_core(core_fields()))
    source = {
        "kind": "resolved_pack",
        "support_id": "a" * 64,
        "pack_id": "Keil.STM32L4xx_DFP",
        "pack_filename": "Keil.STM32L4xx_DFP.3.1.0.pack",
        "pack_sha256": "b" * 64,
        "pdsc_device": "STM32L476RGTx",
        "pyocd_target": "stm32l476rgtx",
    }

    committed = profiles.commit_optional(
        profiles.stage_optional(
            "bench_board", {"device_support": source} | captured_datasheet_fields(tmp_path, profiles)
        )
    )

    assert committed.device_support == source
    assert profiles.load("bench_board", include_legacy=False).device_support == source
    assert calls and all(call[1] == "stm32l476rgtx" for call in calls)
    with pytest.raises(ProfileError, match="exactly"):
        profiles.stage_optional("bench_board", {"device_support": source | {"extra": "no"}})


def test_default_device_support_verifier_replays_the_registry_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = DeviceSupportCandidate(
        candidate_id="a" * 64,
        part_number="STM32L476RGTx-Exact",
        pdsc_device="STM32L476RGTx",
        pyocd_target="stm32l476rgtx",
        pack_id="Vendor.Device_DFP",
        pack_filename="device.pack",
        pack_sha256="b" * 64,
    )
    monkeypatch.setattr(
        device_support,
        "resolve_persisted_pack_support",
        lambda _store, _part, _source: candidate,
    )
    profiles = repository(tmp_path)
    profiles.commit_core(profiles.stage_core(core_fields()))

    committed = profiles.commit_optional(
        profiles.stage_optional(
            "bench_board",
            {"device_support": candidate.to_authority_document()}
            | captured_datasheet_fields(tmp_path, profiles),
        )
    )

    assert committed.device_support == candidate.to_authority_document()
    wrong_target = candidate.to_authority_document() | {"pyocd_target": "other-target"}
    with pytest.raises(ProfileError, match="verified binding"):
        profiles.stage_optional("bench_board", {"device_support": wrong_target})


def test_generic_profile_identity_fields_must_replay_verified_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proof = LiveIdentityProof(
        "compatible", 0xE000ED00, 0xC240, 0xFFF0, 32, "Cortex-M4 identity"
    )
    candidate = DeviceSupportCandidate(
        candidate_id="a" * 64,
        part_number="STM32L476RGTx-Exact",
        pdsc_device="STM32L476RGTx",
        pyocd_target="stm32l476rgtx",
        pack_id="Vendor.Device_DFP",
        pack_filename="device.pack",
        pack_sha256="b" * 64,
        identity_proof=proof,
    )
    monkeypatch.setattr(
        device_support,
        "resolve_persisted_pack_support",
        lambda _store, _part, _source: candidate,
    )
    profiles = repository(tmp_path)
    profiles.commit_core(profiles.stage_core(core_fields()))
    source = candidate.to_authority_document()
    valid = {
        "device_support": source,
        "silicon_id_address": proof.address,
        "silicon_id_expected": proof.expected,
        "silicon_id_mask": proof.mask,
        "silicon_id_width_bits": proof.width_bits,
        "silicon_id_label": proof.label,
    } | captured_datasheet_fields(tmp_path, profiles)
    profiles.commit_optional(profiles.stage_optional("bench_board", valid))

    tampered = profiles.load("bench_board", include_legacy=False).to_document()
    tampered["silicon_id_expected"] = 0
    profiles.store.atomic_write_yaml(
        profiles.store.layout.board_profile("bench_board"), tampered
    )

    with pytest.raises(ProfileError, match="identity fields do not match"):
        profiles.load("bench_board", include_legacy=False)


def test_generic_profile_accepts_server_captured_canonical_cpuid_when_pack_has_no_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = DeviceSupportCandidate(
        candidate_id="a" * 64,
        part_number="STM32L476RGTx-Exact",
        pdsc_device="STM32L476RGTx",
        pyocd_target="stm32l476rgtx",
        pack_id="Vendor.Device_DFP",
        pack_filename="device.pack",
        pack_sha256="b" * 64,
    )
    monkeypatch.setattr(
        device_support,
        "resolve_persisted_pack_support",
        lambda _store, _part, _source: candidate,
    )
    profiles = repository(tmp_path)
    profiles.commit_core(profiles.stage_core(core_fields()))
    proof = device_support.live_cpuid_compatibility_proof(0x410FC241)
    fields = {
        "device_support": candidate.to_authority_document(),
        "silicon_id_address": proof.address,
        "silicon_id_expected": proof.expected,
        "silicon_id_mask": proof.mask,
        "silicon_id_width_bits": proof.width_bits,
        "silicon_id_label": proof.label,
    } | captured_datasheet_fields(tmp_path, profiles)

    committed = profiles.commit_optional(profiles.stage_optional("bench_board", fields))

    assert committed.board.silicon_id_expected == proof.expected


def test_generic_profile_refuses_noncanonical_live_cpuid_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = DeviceSupportCandidate(
        candidate_id="a" * 64,
        part_number="STM32L476RGTx-Exact",
        pdsc_device="STM32L476RGTx",
        pyocd_target="stm32l476rgtx",
        pack_id="Vendor.Device_DFP",
        pack_filename="device.pack",
        pack_sha256="b" * 64,
    )
    monkeypatch.setattr(
        device_support,
        "resolve_persisted_pack_support",
        lambda _store, _part, _source: candidate,
    )
    profiles = repository(tmp_path)
    profiles.commit_core(profiles.stage_core(core_fields()))
    proof = device_support.live_cpuid_compatibility_proof(0x410FC241)
    fields = {
        "device_support": candidate.to_authority_document(),
        "silicon_id_address": proof.address,
        "silicon_id_expected": proof.expected,
        "silicon_id_mask": 0xFFF0,
        "silicon_id_width_bits": proof.width_bits,
        "silicon_id_label": proof.label,
    } | captured_datasheet_fields(tmp_path, profiles)

    with pytest.raises(ProfileError, match="canonical live CPUID"):
        profiles.stage_optional("bench_board", fields)


def test_generic_device_support_requires_captured_datasheet_evidence(tmp_path: Path) -> None:
    profiles = repository(tmp_path, device_support_verifier=lambda *_args: None)
    profiles.commit_core(profiles.stage_core(core_fields()))
    source = {
        "kind": "resolved_pack",
        "support_id": "a" * 64,
        "pack_id": "Vendor.Device_DFP",
        "pack_filename": "device.pack",
        "pack_sha256": "b" * 64,
        "pdsc_device": "STM32L476RGTx",
        "pyocd_target": "stm32l476rgtx",
    }

    with pytest.raises(ProfileError, match="requires captured datasheet"):
        profiles.stage_optional("bench_board", {"device_support": source})


def test_stale_optional_stage_cannot_overwrite_newer_commit(tmp_path: Path) -> None:
    profiles = repository(tmp_path)
    profiles.commit_core(profiles.stage_core(core_fields()))
    first = profiles.stage_optional("bench_board", {"test_read_address": 0x08000000})
    stale = profiles.stage_optional("bench_board", {"test_read_address": 0x08000004})
    profiles.commit_optional(first)

    with pytest.raises(StaleProfileStageError, match="changed after"):
        profiles.commit_optional(stale)


def test_safety_reference_has_a_separate_project_local_stage(tmp_path: Path) -> None:
    profiles = repository(tmp_path)
    profiles.commit_core(profiles.stage_core(core_fields()))
    reference = ".firm/safety/bench_board/memory_map.yaml"

    staged = profiles.stage_safety_ref("bench_board", reference)
    assert profiles.load("bench_board").safety_ref is None
    committed = profiles.commit_safety_ref(staged)

    assert committed.safety_ref == reference
    with pytest.raises(ProfileError, match="below .firm/safety/bench_board"):
        profiles.stage_safety_ref("bench_board", "../outside.yaml")


def test_profile_filename_stem_must_match_internal_board_id(tmp_path: Path) -> None:
    profiles = repository(tmp_path)
    staged = profiles.stage_core(core_fields())
    wrong_path = profiles.store.layout.boards / "wrong_name.yaml"
    profiles.store.atomic_write_yaml(wrong_path, staged.profile.to_document())

    with pytest.raises(ProfileError, match="filename stem 'wrong_name'.*board_id 'bench_board'"):
        profiles.load_all(include_legacy=False)


def test_duplicate_unicode_display_names_are_rejected(tmp_path: Path) -> None:
    profiles = repository(tmp_path)
    profiles.commit_core(
        profiles.stage_core(core_fields(board_id="first", display_name="Café Controller"))
    )

    with pytest.raises(ProfileError, match="Duplicate display_name"):
        profiles.stage_core(core_fields(board_id="second", display_name="Cafe\u0301 Controller"))


def test_duplicate_board_documents_are_rejected(tmp_path: Path) -> None:
    profiles = repository(tmp_path)
    committed = profiles.commit_core(profiles.stage_core(core_fields()))
    duplicate = profiles.store.layout.board_profile("bench_board", suffix=".json")
    profiles.store.atomic_write_json(duplicate, committed.to_document())

    with pytest.raises(ProfileError, match="Duplicate board_id 'bench_board'"):
        profiles.load_all(include_legacy=False)


def test_legacy_boards_are_read_only_and_pack_name_is_warned_then_removed(
    tmp_path: Path,
) -> None:
    profiles = repository(tmp_path)
    legacy_path = profiles.legacy_board_dir / "legacy_board.yaml"
    legacy_path.write_text(
        "\n".join(
            (
                "board_id: legacy_board",
                'display_name: "Legacy Board"',
                "mcu_family: nrf52833",
                "probe_family: jlink",
                "pyocd_target: nrf52833",
                "pack_name: nrf52833",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.warns(LegacyPackNameWarning, match="deprecated and ignored"):
        legacy = profiles.load("legacy_board")

    assert legacy.schema_version == 1
    assert legacy.read_only is True
    assert legacy.mcu_part_number is None
    assert PACKAGE_METADATA_FIELDS.isdisjoint(legacy.to_document())
    assert not hasattr(legacy.board, "pack_name")
    with pytest.raises(ProfileError, match="Board profile not found"):
        profiles.stage_optional("legacy_board", {"test_read_address": 0x10000000})
    assert not profiles.store.layout.board_profile("legacy_board").exists()


def test_legacy_filename_stem_mismatch_is_rejected(tmp_path: Path) -> None:
    profiles = repository(tmp_path)
    legacy_path = profiles.legacy_board_dir / "wrong_name.yaml"
    legacy_path.write_text(
        "\n".join(
            (
                "board_id: legacy_board",
                'display_name: "Legacy Board"',
                "mcu_family: nrf52833",
                "probe_family: jlink",
                "pyocd_target: nrf52833",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ProfileError, match="Legacy profile filename stem 'wrong_name'"):
        profiles.load_all()


@pytest.mark.parametrize("field_name", sorted(PACKAGE_METADATA_FIELDS))
def test_pack_manifest_is_authoritative_and_v2_rejects_package_identifiers(
    tmp_path: Path,
    field_name: str,
) -> None:
    specs = load_manifest()
    stm = next(spec for spec in specs if spec.id == "Keil.STM32L4xx_DFP")
    assert "stm32l476rgtx" in stm.provides_targets
    assert "nucleo_l476rg" in stm.needed_by_boards

    profiles = repository(tmp_path)
    fields = core_fields() | {field_name: "forbidden-profile-owner"}
    with pytest.raises(ProfileError, match=r"package metadata.*packs/manifest\.yaml"):
        profiles.stage_core(fields)


@pytest.mark.parametrize(
    ("document_update", "message"),
    [
        ({"schema_version": 1}, "schema_version"),
        ({"mcu_part_number": None}, "mcu_part_number"),
        ({"created_at": "not-a-timestamp"}, "created_at"),
        ({"unexpected_profile_field": True}, "Unknown schema-v2 profile fields"),
    ],
)
def test_malformed_v2_profiles_are_rejected(
    tmp_path: Path,
    document_update: dict[str, object],
    message: str,
) -> None:
    profiles = repository(tmp_path)
    staged = profiles.stage_core(core_fields())
    malformed = staged.profile.to_document() | document_update
    profiles.store.atomic_write_yaml(
        profiles.store.layout.board_profile("bench_board"),
        malformed,
    )

    with pytest.raises(ProfileError, match=message):
        profiles.load("bench_board", include_legacy=False)


def test_syntactically_malformed_profile_is_rejected(tmp_path: Path) -> None:
    profiles = repository(tmp_path)
    target = profiles.store.layout.board_profile("bench_board")
    profiles.store.atomic_write_text(target, "board_id: [unterminated\n")

    with pytest.raises(ProfileError, match="Could not parse board config"):
        profiles.load("bench_board", include_legacy=False)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("board_id", "Uppercase", "board_id must be 1-64"),
        ("board_id", "b" * 65, "board_id must be 1-64"),
        ("display_name", "x" * 101, "display_name.*at most 100"),
    ],
)
def test_a6_identifier_and_display_limits(
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    profiles = repository(tmp_path)
    fields = core_fields()
    fields[field] = value

    with pytest.raises(ProfileError, match=message):
        profiles.stage_core(fields)


def test_profile_rejects_non_absolute_timestamp_and_persisted_authority(
    tmp_path: Path,
) -> None:
    profiles = repository(tmp_path)
    staged = profiles.stage_core(core_fields())
    malformed = copy.deepcopy(staged.profile.to_document())
    malformed["created_at"] = "2026-07-17T12:00:00"
    profiles.store.atomic_write_yaml(profiles.store.layout.board_profile("bench_board"), malformed)

    with pytest.raises(ProfileError, match="created_at.*timezone"):
        profiles.load("bench_board", include_legacy=False)

    with pytest.raises(PersistedAuthorityError, match="permission_grant"):
        profiles.stage_core(core_fields(board_id="other") | {"permission_grant": True})
