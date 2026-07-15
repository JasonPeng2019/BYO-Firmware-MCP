from __future__ import annotations

from pathlib import Path

from pyocd_debug_mcp.pack_index_repair import (
    PdscRef,
    descriptor_path,
    parse_master_index,
    plan_downloads,
    select_refs,
)


def test_parse_master_index_reads_pdsc_entries() -> None:
    xml = """
    <index>
      <pindex>
        <pdsc vendor="Keil" name="STM32L4xx_DFP" version="3.1.0" url="https://www.keil.com/pack/" />
        <pdsc vendor="Nordic" name="nRF_DeviceFamilyPack" version="8.62.0" url="https://example.invalid/packs/" />
      </pindex>
    </index>
    """
    refs = parse_master_index(xml)
    assert refs == [
        PdscRef("Keil", "STM32L4xx_DFP", "3.1.0", "https://www.keil.com/pack/"),
        PdscRef("Nordic", "nRF_DeviceFamilyPack", "8.62.0", "https://example.invalid/packs/"),
    ]
    assert refs[0].remote_pdsc_url == "https://www.keil.com/pack/Keil.STM32L4xx_DFP.pdsc"
    assert refs[0].cache_filename == "Keil.STM32L4xx_DFP.3.1.0.pdsc"


def test_select_refs_applies_vendor_exact_name_and_contains_filters() -> None:
    refs = [
        PdscRef("Keil", "STM32L4xx_DFP", "3.1.0", "https://www.keil.com/pack/"),
        PdscRef("Keil", "STM32F4xx_DFP", "2.17.0", "https://www.keil.com/pack/"),
        PdscRef("Nordic", "nRF_DeviceFamilyPack", "8.62.0", "https://example.invalid/"),
    ]
    selected = select_refs(
        refs,
        vendors=["keil"],
        pack_names=["stm32l4xx_dfp"],
        name_contains=["l4"],
    )
    assert selected == [refs[0]]


def test_plan_downloads_skips_existing_when_missing_only(tmp_path: Path) -> None:
    refs = [
        PdscRef("Keil", "STM32L4xx_DFP", "3.1.0", "https://www.keil.com/pack/"),
        PdscRef("Nordic", "nRF_DeviceFamilyPack", "8.62.0", "https://example.invalid/"),
    ]
    descriptor_path(tmp_path, refs[0]).write_text("<package />", encoding="utf-8")

    planned = plan_downloads(refs, tmp_path, missing_only=True)
    assert planned == [refs[1]]

    planned_all = plan_downloads(refs, tmp_path, missing_only=False)
    assert planned_all == refs
