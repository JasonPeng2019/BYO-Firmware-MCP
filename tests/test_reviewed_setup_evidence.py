from __future__ import annotations

from dataclasses import replace
from copy import deepcopy
from pathlib import Path

import pytest

from pyocd_debug_mcp.pack_provision import PackProvisionError
from pyocd_debug_mcp.safety.verify2 import ReconciliationResult, VerificationConflict
from pyocd_debug_mcp.safety.regions import ActionCategory, AddressRange, Allowed, Refusal, SafetyMap
from pyocd_debug_mcp.setup_flow import reviewed_evidence
from pyocd_debug_mcp.setup_flow.board_catalog import BoardCatalogError, catalog_board


def _datasheet() -> Path:
    return Path("Nano_BLE_MCU-nRF52840_PS_v1.1.pdf").resolve()


def _stm32_datasheet() -> Path:
    return Path("stm32l476je (2).pdf").resolve()


def test_reviewed_evidence_checks_runtime_and_reconciles_distinct_authorities() -> None:
    path = _datasheet()

    bundle = reviewed_evidence.load_reviewed_evidence(catalog_board("nrf52840dk"), path)

    assert bundle.reconciliation.accepted
    assert {
        (item.fact_id, item.kind.value) for item in bundle.reconciliation.regions
    } == {
        ("physical_flash", "physical_flash"),
        ("physical_ram", "physical_ram"),
        ("writable_ram", "ram"),
        ("uicr", "prohibited"),
        ("ficr", "rom"),
        ("apb_before_nvmc", "peripheral_read_only"),
        ("safe_gpio", "peripheral"),
        ("nvmc_acl", "prohibited"),
        ("apb_after_nvmc", "peripheral_read_only"),
        ("ahb_before_gpio", "peripheral_read_only"),
        ("ahb_after_gpio", "peripheral_read_only"),
        ("cpu_system", "cpu_system"),
    }
    record = bundle.source_record()
    support = record["device_support"]
    official = record["official_document"]
    assert isinstance(support, dict) and isinstance(official, dict)
    assert support["asset_sha256"] != official["asset_sha256"]
    assert support["document"] != official["document"]
    assert support["runtime"]["pyocd_version"] == "0.45.0"  # type: ignore[index]


def test_stm32_reviewed_evidence_is_bound_to_local_pack_and_official_pdf() -> None:
    catalog = catalog_board("nucleo_l476rg")

    bundle = reviewed_evidence.load_reviewed_evidence(catalog, _stm32_datasheet())

    assert bundle.reconciliation.accepted
    assert bundle.pyocd_target_module_sha256 == catalog.pyocd_pack_sha256
    assert bundle.pyocd_svd_bundle_sha256 == catalog.pyocd_pack_sha256
    assert {item.fact_id for item in bundle.reconciliation.regions} == {
        "physical_flash",
        "physical_ram",
        "writable_ram",
        "system_memory",
        "otp_config",
        "option_bytes",
        "peripherals",
        "cpu_system",
    }


def test_stm32_pack_runtime_fails_closed_on_missing_or_tampered_pack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = catalog_board("nucleo_l476rg")
    monkeypatch.setattr(
        reviewed_evidence,
        "verified_pack_for_target",
        lambda _target: (_ for _ in ()).throw(PackProvisionError("absent")),
    )
    with pytest.raises(BoardCatalogError, match="CMSIS-Pack is unavailable"):
        reviewed_evidence.load_reviewed_evidence(catalog, _stm32_datasheet())

    monkeypatch.undo()
    with pytest.raises(BoardCatalogError, match="catalog and pinned pack manifest"):
        reviewed_evidence.load_reviewed_evidence(
            replace(catalog, pyocd_pack_sha256="0" * 64), _stm32_datasheet()
        )


def test_runtime_identity_rejects_mixed_module_and_pack_configuration() -> None:
    catalog = catalog_board("nucleo_l476rg")
    mixed = replace(
        catalog,
        pyocd_target_module="pyocd.target.builtin.target_nRF52840_xxAA",
        pyocd_target_module_sha256="0" * 64,
        pyocd_svd_bundle_sha256="0" * 64,
    )

    with pytest.raises(BoardCatalogError, match="exactly one reviewed module or CMSIS-Pack"):
        reviewed_evidence._runtime_pyocd_identity(mixed)


@pytest.mark.parametrize(
    "address",
    [0x4001E504, 0x4001E50C, 0x4001E514, 0x4001E518],
)
def test_reviewed_nrf_nonvolatile_control_registers_are_prohibited(address: int) -> None:
    path = _datasheet()
    bundle = reviewed_evidence.load_reviewed_evidence(catalog_board("nrf52840dk"), path)
    safety_map = SafetyMap(
        [item.to_safety_region() for item in bundle.reconciliation.regions]
    )

    result = safety_map.check(
        ActionCategory.REGISTER_WRITE, [AddressRange.from_start_size(address, 4)]
    )

    assert isinstance(result, Refusal)
    assert result.code == "safety/prohibited"


def test_reviewed_nrf_gpio_register_write_window_remains_available() -> None:
    path = _datasheet()
    bundle = reviewed_evidence.load_reviewed_evidence(catalog_board("nrf52840dk"), path)
    safety_map = SafetyMap(
        [item.to_safety_region() for item in bundle.reconciliation.regions]
    )

    result = safety_map.check(
        ActionCategory.REGISTER_WRITE,
        [AddressRange.from_start_size(0x50000504, 4)],
    )

    assert isinstance(result, Allowed)


def test_reviewed_nrf_uarte_registers_are_readable_but_not_writable() -> None:
    bundle = reviewed_evidence.load_reviewed_evidence(
        catalog_board("nrf52840dk"), _datasheet()
    )
    safety_map = SafetyMap(
        [item.to_safety_region() for item in bundle.reconciliation.regions]
    )
    register = AddressRange.from_start_size(0x40002200, 4)

    assert isinstance(safety_map.check(ActionCategory.MEMORY_READ, [register]), Allowed)
    write = safety_map.check(ActionCategory.REGISTER_WRITE, [register])
    assert isinstance(write, Refusal)
    assert write.code == "safety/wrong-region-kind"


def test_persisted_reviewed_authority_rejects_self_asserted_documents() -> None:
    path = _datasheet()
    catalog = catalog_board("nrf52840dk")
    bundle = reviewed_evidence.load_reviewed_evidence(catalog, path)
    record = bundle.source_record()
    pack = deepcopy(record["device_support"])
    authority = {
        "official_document": deepcopy(record["official_document"]),
        "reconciliation": deepcopy(record["reconciliation"]),
    }
    assert isinstance(pack, dict)
    document = pack["document"]
    assert isinstance(document, dict)
    document["regions"] = []

    with pytest.raises(BoardCatalogError, match="pinned repository asset"):
        reviewed_evidence.verify_persisted_reviewed_evidence(
            catalog, pack, authority
        )


def test_reviewed_evidence_fails_closed_on_every_missing_pin() -> None:
    path = _datasheet()
    original = catalog_board("nrf52840dk")

    for field in (
        "device_support_evidence_resource",
        "device_support_evidence_sha256",
        "official_evidence_resource",
        "official_evidence_sha256",
        "pyocd_version",
        "pyocd_target_module",
        "pyocd_target_module_sha256",
        "pyocd_svd_bundle_sha256",
    ):
        with pytest.raises(BoardCatalogError, match="lacks complete reviewed"):
            reviewed_evidence.load_reviewed_evidence(replace(original, **{field: None}), path)


def test_reviewed_evidence_rejects_asset_and_runtime_identity_drift() -> None:
    path = _datasheet()
    original = catalog_board("nrf52840dk")

    with pytest.raises(BoardCatalogError, match="evidence resource failed"):
        reviewed_evidence.load_reviewed_evidence(
            replace(original, official_evidence_sha256="0" * 64), path
        )
    with pytest.raises(BoardCatalogError, match="does not match reviewed version"):
        reviewed_evidence.load_reviewed_evidence(replace(original, pyocd_version="0.0.0"), path)
    with pytest.raises(BoardCatalogError, match="target implementation failed"):
        reviewed_evidence.load_reviewed_evidence(
            replace(original, pyocd_target_module_sha256="0" * 64), path
        )
    with pytest.raises(BoardCatalogError, match="SVD bundle failed"):
        reviewed_evidence.load_reviewed_evidence(
            replace(original, pyocd_svd_bundle_sha256="0" * 64), path
        )


def test_reviewed_evidence_calls_strict_reconciliation_and_rejects_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _datasheet()
    called: dict[str, object] = {}

    def conflict(**kwargs: object) -> ReconciliationResult:
        called.update(kwargs)
        return ReconciliationResult(
            "conflict",
            (),
            (
                VerificationConflict(
                    "verify/address",
                    "physical_flash",
                    "ranges conflict",
                    [0, 1],
                    [0, 2],
                ),
            ),
            (),
            (),
            None,
        )

    monkeypatch.setattr(reviewed_evidence, "reconcile_hardware_evidence", conflict)

    with pytest.raises(BoardCatalogError, match="verify/address: ranges conflict"):
        reviewed_evidence.load_reviewed_evidence(catalog_board("nrf52840dk"), path)
    assert called["expected_mcu_part_number"] == "nRF52840-QIAA"
    assert called["expected_target"] == "nrf52840"


@pytest.mark.parametrize(
    ("changed_label", "replacement", "message"),
    [
        (
            "official-document",
            "sha256:" + "0" * 64,
            "not bound to the server-computed datasheet",
        ),
        ("device-support", "sha256:" + "0" * 64, "not bound to the installed pyOCD"),
    ],
)
def test_reviewed_evidence_cross_binds_documents_to_live_source_hashes(
    monkeypatch: pytest.MonkeyPatch,
    changed_label: str,
    replacement: str,
    message: str,
) -> None:
    path = _datasheet()
    original_load = reviewed_evidence._load_asset

    def changed_asset(
        resource: str | None, expected_digest: str | None, label: str
    ) -> tuple[dict[str, object], str]:
        document, asset_hash = original_load(resource, expected_digest, label)
        copied = deepcopy(document)
        if label == changed_label:
            sources = copied["sources"]
            assert isinstance(sources, list) and isinstance(sources[0], dict)
            sources[0]["revision"] = replacement
        return copied, asset_hash

    monkeypatch.setattr(reviewed_evidence, "_load_asset", changed_asset)

    with pytest.raises(BoardCatalogError, match=message):
        reviewed_evidence.load_reviewed_evidence(catalog_board("nrf52840dk"), path)
