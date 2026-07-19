from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from pyocd_debug_mcp.setup_flow.board_catalog import (
    BoardCatalogError,
    ReviewedSupportAmbiguityError,
    ReviewedSupportNotFoundError,
    _load_catalog,
    catalog_board,
    catalog_board_types,
    resolve_reviewed_support,
    resolve_reviewed_support_from_datasheet,
    reviewed_setup_board_types,
)
from pyocd_debug_mcp.safety.regions import AddressRange, RegionKind


def test_nrf52840_catalog_separates_live_family_from_package_evidence() -> None:
    board = catalog_board("nrf52840dk")

    assert board.accepts_mcu("nRF52840")
    assert board.accepts_mcu("nRF52840-QIAA")
    assert board.package_part_number == "nRF52840-QIAA"
    assert board.silicon_id_address == 0x10000100
    assert board.silicon_id_expected == 0x00052840
    assert board.application_start >= board.flash_start
    assert board.application_end <= board.flash_end
    assert board.application_partition_authoritative is True
    assert board.application_partition is not None
    assert board.bootloader_partition is None
    assert board.no_protected_resident_bootloader is True
    assert board.live_identity_reviewed is True
    assert board.deployment_partition_policy_document() == {
        "application": {"authoritative": True, "start": 0, "end": 0x100000},
        "bootloader": {"authoritative": False, "start": None, "end": None},
        "no_protected_resident_bootloader": True,
    }
    assert {region.kind for region in board.hardware_regions} >= {
        RegionKind.PROHIBITED,
        RegionKind.PERIPHERAL,
        RegionKind.CPU_SYSTEM,
    }
    assert all(region.start < region.end for region in board.hardware_regions)


def test_every_catalog_entry_has_explicit_partition_and_live_identity_policy() -> None:
    for board_type in catalog_board_types():
        board = catalog_board(board_type)
        assert board.application_partition is not None
        assert board.live_identity_reviewed
        assert board.silicon_id_address is not None
        assert board.silicon_id_expected is not None
        assert board.silicon_id_label
        assert board.silicon_id_limitation

    stm32 = catalog_board("nucleo_l476rg")
    assert stm32.silicon_id_address == 0xE0042000
    assert stm32.silicon_id_expected == 0x415
    assert stm32.silicon_id_mask == 0xFFF
    assert "package and density suffix" in stm32.silicon_id_limitation


def test_catalog_is_closed_and_requires_reviewed_datasheet_hash(tmp_path: Path) -> None:
    assert catalog_board_types() == ("nrf52833dk", "nrf52840dk", "nucleo_l476rg")
    assert reviewed_setup_board_types() == ("nrf52840dk", "nucleo_l476rg")
    with pytest.raises(BoardCatalogError, match="Unsupported board type"):
        catalog_board("guessed_board")

    fake_pdf = tmp_path / "device.pdf"
    fake_pdf.write_bytes(b"%PDF-1.7\nnot the reviewed document")
    with pytest.raises(BoardCatalogError, match="not reviewed"):
        catalog_board("nrf52840dk").validate_datasheet(fake_pdf)


@pytest.mark.parametrize("board_type", ["nrf52833dk"])
def test_empty_datasheet_allowlist_is_unavailable_not_accept_anything(
    board_type: str, tmp_path: Path
) -> None:
    fake_pdf = tmp_path / f"{board_type}.pdf"
    fake_pdf.write_bytes(b"%PDF-1.7\nwell formed but unreviewed")

    with pytest.raises(BoardCatalogError, match="No reviewed datasheet"):
        catalog_board(board_type).validate_datasheet(fake_pdf)


def test_reviewed_datasheet_requires_exact_bytes_and_internally_computed_hash() -> None:
    datasheet = Path("Nano_BLE_MCU-nRF52840_PS_v1.1.pdf").resolve()

    digest = catalog_board("nrf52840dk").validate_datasheet(datasheet)

    assert digest in catalog_board("nrf52840dk").datasheet_sha256

    stm32_datasheet = Path("stm32l476je (2).pdf").resolve()
    stm32_digest = catalog_board("nucleo_l476rg").validate_datasheet(stm32_datasheet)
    assert stm32_digest == "a45a857e3aa75ac166dd532c76d76d5dd8377b9c5bf6f15c03c9cf85aeec0f65"


def test_stm32_uses_pack_backed_runtime_without_changing_standard_partition() -> None:
    board = catalog_board("nucleo_l476rg")

    assert board.automatic_setup_reviewed
    assert board.pyocd_target_module is None
    assert board.pyocd_pack_filename == "Keil.STM32L4xx_DFP.3.1.0.pack"
    assert board.pyocd_pack_sha256 == (
        "5672383c07fbdcee0e471a33f4f8beb2e1f3200bc999244dcd6858e0e8e8203f"
    )
    assert board.application_partition == AddressRange(0x08000000, 0x08100000)
    assert board.bootloader_partition is None


def test_catalog_rejects_non_pdf_evidence(tmp_path: Path) -> None:
    text = tmp_path / "device.txt"
    text.write_text("not a PDF", encoding="utf-8")
    with pytest.raises(BoardCatalogError, match="local PDF"):
        catalog_board("nrf52840dk").validate_datasheet(Path(text))


def test_reviewed_support_is_resolved_only_from_exact_package_and_server_hash(
    tmp_path: Path,
) -> None:
    pdf = tmp_path / "custom-controller.pdf"
    pdf.write_bytes(b"%PDF-1.7\ncustom PCB official datasheet")
    original = catalog_board("nrf52840dk")
    import hashlib

    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    reviewed = replace(original, datasheet_sha256=(digest,))

    resolved = resolve_reviewed_support_from_datasheet(
        "nRF52840-QIAA",
        pdf,
        candidates=(reviewed,),
    )

    assert resolved.catalog is reviewed
    assert resolved.datasheet_sha256 == digest
    assert resolved.datasheet_path == pdf.resolve()
    with pytest.raises(ReviewedSupportNotFoundError, match="exact MCU"):
        resolve_reviewed_support("nRF52840", digest, candidates=(reviewed,))
    with pytest.raises(ReviewedSupportNotFoundError, match="exact MCU"):
        resolve_reviewed_support("nRF52840-QIAA", "0" * 64, candidates=(reviewed,))


def test_reviewed_support_ambiguity_fails_closed() -> None:
    original = catalog_board("nrf52840dk")
    duplicate = replace(original, board_type="alternate_internal_record")
    digest = original.datasheet_sha256[0]

    with pytest.raises(ReviewedSupportAmbiguityError, match="multiple"):
        resolve_reviewed_support(
            original.package_part_number,
            digest,
            candidates=(original, duplicate),
        )


def test_reviewed_board_facts_are_packaged_data_not_python_branches(tmp_path: Path) -> None:
    module_text = Path("src/pyocd_debug_mcp/setup_flow/board_catalog.py").read_text(
        encoding="utf-8"
    )
    assert "nrf52840dk" not in module_text
    assert "nucleo_l476rg" not in module_text
    assert "0x10000000" not in module_text

    bad = tmp_path / "bad.json"
    bad.write_text('{"schema_version": 1, "boards": [{"board_type": "x"}]}', encoding="utf-8")
    with pytest.raises(BoardCatalogError):
        _load_catalog(bad)


def test_full_flash_ceiling_without_explicit_no_resident_bootloader_is_rejected(
    tmp_path: Path,
) -> None:
    import json

    source = Path("src/pyocd_debug_mcp/setup_flow/reviewed_boards.json")
    document = json.loads(source.read_text(encoding="utf-8"))
    document["boards"][0]["no_protected_resident_bootloader"] = False
    path = tmp_path / "bad-policy.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(BoardCatalogError, match="full-flash application authority"):
        _load_catalog(path)
