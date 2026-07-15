from __future__ import annotations

import pytest

from pyocd_debug_mcp.board_config import load_selected_board_configs, DEFAULT_BOARD_CONFIG_DIR
from pyocd_debug_mcp import probe_inventory
from pyocd_debug_mcp.probe_inventory import (
    ProbeInfo,
    list_connected_probes,
    parse_pyocd_probe_listing,
    pick_probe_for_board,
)


SAMPLE_LISTING = """\
  #   Probe/Board                              Unique ID                  Target
-------------------------------------------------------------------------------------------
  0   STM32 STLink                             0668FF514988525067213913   ✔︎ stm32l476rgtx
      NUCLEO-L476RG

  1   Segger J-Link OB-SAM3U128-V2-NordicSem   685400693                  n/a
"""


def test_parse_pyocd_probe_listing_preserves_uid_description_and_state() -> None:
    probes = parse_pyocd_probe_listing(SAMPLE_LISTING)

    assert probes == [
        ProbeInfo(
            uid="0668FF514988525067213913",
            description="STM32 STLink NUCLEO-L476RG",
            raw="  0   STM32 STLink                             0668FF514988525067213913   ✔︎ stm32l476rgtx\n      NUCLEO-L476RG",
            state="✔︎ stm32l476rgtx",
        ),
        ProbeInfo(
            uid="685400693",
            description="Segger J-Link OB-SAM3U128-V2-NordicSem",
            raw="  1   Segger J-Link OB-SAM3U128-V2-NordicSem   685400693                  n/a",
            state="n/a",
        ),
    ]


def test_pick_probe_for_board_selects_official_nordic_probe_when_both_boards_attached() -> None:
    [board] = load_selected_board_configs(DEFAULT_BOARD_CONFIG_DIR, requested_ids=["nrf52833dk"])
    probes = parse_pyocd_probe_listing(SAMPLE_LISTING)

    resolution = pick_probe_for_board(board, probes, allow_single_fallback=False)

    assert resolution.probe is not None
    assert resolution.probe.uid == "685400693"
    assert resolution.note == ""


def test_pick_probe_for_board_selects_stlink_probe_when_both_boards_attached() -> None:
    [board] = load_selected_board_configs(DEFAULT_BOARD_CONFIG_DIR, requested_ids=["nucleo_l476rg"])
    probes = parse_pyocd_probe_listing(SAMPLE_LISTING)

    resolution = pick_probe_for_board(board, probes, allow_single_fallback=False)

    assert resolution.probe is not None
    assert resolution.probe.uid == "0668FF514988525067213913"
    assert resolution.note == ""


def test_pick_probe_for_board_reports_ambiguity_for_multiple_matching_jlinks() -> None:
    [board] = load_selected_board_configs(DEFAULT_BOARD_CONFIG_DIR, requested_ids=["nrf52833dk"])
    probes = [
        ProbeInfo(uid="685400693", description="Segger J-Link nRF52833 DK", raw="probe 1"),
        ProbeInfo(uid="123456789", description="Segger J-Link nRF52833 DK", raw="probe 2"),
    ]

    resolution = pick_probe_for_board(board, probes, allow_single_fallback=False)

    assert resolution.probe is None
    assert "multiple matching probes" in resolution.note


def test_list_connected_probes_prefers_pyocd_api(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeProbe:
        description = "STM32 STLink"
        unique_id = "0670FF3031454D3043223536"
        associated_board_info = None

    class FakeConnectHelper:
        @staticmethod
        def get_all_connected_probes(
            *,
            blocking: bool,
            print_wait_message: bool,
        ) -> list[FakeProbe]:
            assert blocking is False
            assert print_wait_message is False
            return [FakeProbe()]

    monkeypatch.setattr(probe_inventory, "ConnectHelper", FakeConnectHelper)
    seen: list[list[str]] = []

    def fake_run(cmd: list[str]) -> tuple[int, str, str]:
        seen.append(cmd)
        return 0, SAMPLE_LISTING, ""

    probes = list_connected_probes(fake_run)

    assert seen == []
    assert probes == [
        ProbeInfo(
            uid="0670FF3031454D3043223536",
            description="STM32 STLink",
            raw="STM32 STLink | 0670FF3031454D3043223536",
            state="",
        )
    ]


def test_list_connected_probes_falls_back_to_plain_list_when_api_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeConnectHelper:
        @staticmethod
        def get_all_connected_probes(
            *,
            blocking: bool,
            print_wait_message: bool,
        ) -> list[object]:
            assert blocking is False
            assert print_wait_message is False
            return []

    monkeypatch.setattr(probe_inventory, "ConnectHelper", FakeConnectHelper)
    seen: list[list[str]] = []
    fallback_listing = """\
  #   Probe/Board     Unique ID                  Target
------------------------------------------------------------------
  0   STM32 STLink    0670FF3031454D3043223536
"""

    def fake_run(cmd: list[str]) -> tuple[int, str, str]:
        seen.append(cmd)
        if cmd == ["pyocd", "list", "--probes"]:
            return 0, "", ""
        if cmd == ["pyocd", "list"]:
            return 0, fallback_listing, ""
        raise AssertionError(f"unexpected command: {cmd}")

    probes = list_connected_probes(fake_run)

    assert seen == [["pyocd", "list", "--probes"], ["pyocd", "list"]]
    assert probes == [
        ProbeInfo(
            uid="0670FF3031454D3043223536",
            description="STM32 STLink",
            raw="  0   STM32 STLink    0670FF3031454D3043223536",
            state="",
        )
    ]


def test_list_connected_probes_falls_back_to_subprocess_when_api_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeConnectHelper:
        @staticmethod
        def get_all_connected_probes(
            *,
            blocking: bool,
            print_wait_message: bool,
        ) -> list[object]:
            raise RuntimeError("boom")

    monkeypatch.setattr(probe_inventory, "ConnectHelper", FakeConnectHelper)
    seen: list[list[str]] = []

    def fake_run(cmd: list[str]) -> tuple[int, str, str]:
        seen.append(cmd)
        return 0, SAMPLE_LISTING, ""

    probes = list_connected_probes(fake_run)

    assert seen == [["pyocd", "list", "--probes"]]
    assert [probe.uid for probe in probes] == [
        "0668FF514988525067213913",
        "685400693",
    ]


def test_list_connected_probes_can_skip_subprocess_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeConnectHelper:
        @staticmethod
        def get_all_connected_probes(
            *,
            blocking: bool,
            print_wait_message: bool,
        ) -> list[object]:
            assert blocking is False
            assert print_wait_message is False
            return []

    monkeypatch.setattr(probe_inventory, "ConnectHelper", FakeConnectHelper)
    seen: list[list[str]] = []

    def fake_run(cmd: list[str]) -> tuple[int, str, str]:
        seen.append(cmd)
        return 0, SAMPLE_LISTING, ""

    probes = list_connected_probes(fake_run, allow_subprocess_fallback=False)

    assert probes == []
    assert seen == []


def test_parse_pyocd_probe_listing_accepts_windows_stlink_row_without_state() -> None:
    sample = """\
  #   Probe/Board     Unique ID                  Target
------------------------------------------------------------------
  0   STM32 STLink    0670FF3031454D3043223536
"""

    probes = parse_pyocd_probe_listing(sample)

    assert probes == [
        ProbeInfo(
            uid="0670FF3031454D3043223536",
            description="STM32 STLink",
            raw="  0   STM32 STLink    0670FF3031454D3043223536",
            state="",
        )
    ]


def test_parse_pyocd_probe_listing_strips_ansi_and_uses_column_fallback() -> None:
    sample = "\x1b[32m  0   STM32 STLink    0670FF3031454D3043223536   \x1b[0m"

    probes = parse_pyocd_probe_listing(sample)

    assert probes == [
        ProbeInfo(
            uid="0670FF3031454D3043223536",
            description="STM32 STLink",
            raw="  0   STM32 STLink    0670FF3031454D3043223536",
            state="",
        )
    ]
