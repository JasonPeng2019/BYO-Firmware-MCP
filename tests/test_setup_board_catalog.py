from __future__ import annotations

from pathlib import Path

import pytest

from pyocd_debug_mcp.setup_flow.board_catalog import (
    BoardCatalogError,
    _load_catalog,
    catalog_board,
    catalog_board_types,
    reviewed_setup_board_types,
)
from pyocd_debug_mcp.pack_provision import sha256_file
from pyocd_debug_mcp.safety.regions import RegionKind


def test_nrf52840_catalog_separates_live_family_from_package_evidence() -> None:
    board = catalog_board("nrf52840dk")

    assert board.accepts_mcu("nRF52840")
    assert board.accepts_mcu("nRF52840-QIAA")
    assert board.package_part_number == "nRF52840-QIAA"
    assert board.silicon_id_address == 0x10000100
    assert board.silicon_id_width_bits == 32
    assert board.test_read_width_bits == 32
    assert board.silicon_id_expected == 0x00052840
    assert board.application_start >= board.flash_start
    assert board.application_end <= board.flash_end
    assert {region.kind for region in board.hardware_regions} >= {
        RegionKind.PROHIBITED,
        RegionKind.PERIPHERAL,
        RegionKind.CPU_SYSTEM,
    }
    assert all(region.start < region.end for region in board.hardware_regions)


def test_catalog_is_closed_and_requires_reviewed_datasheet_hash(tmp_path: Path) -> None:
    assert catalog_board_types() == ("nrf52833dk", "nrf52840dk", "nucleo_l476rg")
    assert reviewed_setup_board_types() == ("nrf52840dk",)
    with pytest.raises(BoardCatalogError, match="Unsupported board type"):
        catalog_board("guessed_board")

    fake_pdf = tmp_path / "device.pdf"
    fake_pdf.write_bytes(b"%PDF-1.7\nnot the reviewed document")
    with pytest.raises(BoardCatalogError, match="does not match"):
        catalog_board("nrf52840dk").validate_datasheet(fake_pdf, "0" * 64)


@pytest.mark.parametrize("board_type", ["nrf52833dk", "nucleo_l476rg"])
def test_empty_datasheet_allowlist_is_unavailable_not_accept_anything(
    board_type: str, tmp_path: Path
) -> None:
    fake_pdf = tmp_path / f"{board_type}.pdf"
    fake_pdf.write_bytes(b"%PDF-1.7\nwell formed but unreviewed")

    with pytest.raises(BoardCatalogError, match="No reviewed datasheet"):
        catalog_board(board_type).validate_datasheet(fake_pdf, sha256_file(fake_pdf))


def test_reviewed_datasheet_requires_exact_bytes_and_internally_computed_hash() -> None:
    datasheet = Path("Nano_BLE_MCU-nRF52840_PS_v1.1.pdf").resolve()
    digest = sha256_file(datasheet)

    catalog_board("nrf52840dk").validate_datasheet(datasheet, digest)
    with pytest.raises(BoardCatalogError, match="does not match"):
        catalog_board("nrf52840dk").validate_datasheet(datasheet, "0" * 64)


def test_catalog_rejects_non_pdf_evidence(tmp_path: Path) -> None:
    text = tmp_path / "device.txt"
    text.write_text("not a PDF", encoding="utf-8")
    with pytest.raises(BoardCatalogError, match="local PDF"):
        catalog_board("nrf52840dk").validate_datasheet(Path(text), "0" * 64)


def test_reviewed_board_facts_are_packaged_data_not_python_branches(tmp_path: Path) -> None:
    module_text = Path(
        "src/pyocd_debug_mcp/setup_flow/board_catalog.py"
    ).read_text(encoding="utf-8")
    assert "nrf52840dk" not in module_text
    assert "nucleo_l476rg" not in module_text
    assert "0x10000000" not in module_text

    bad = tmp_path / "bad.json"
    bad.write_text('{"schema_version": 1, "boards": [{"board_type": "x"}]}', encoding="utf-8")
    with pytest.raises(BoardCatalogError):
        _load_catalog(bad)
