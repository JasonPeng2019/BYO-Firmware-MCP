from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
BOARD_DIR = REPO_ROOT / "boards"
for entry in (REPO_ROOT, SRC_ROOT):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from pyocd_debug_mcp.board_config import (  # noqa: E402
    ConfigError,
    load_board_configs_from_paths,
)


def test_all_tracked_board_configs_load() -> None:
    paths = sorted(
        path
        for path in BOARD_DIR.iterdir()
        if path.is_file() and path.suffix.lower() in {".json", ".yaml", ".yml"}
    )
    boards = load_board_configs_from_paths(paths)

    assert boards, "expected at least one board config"
    assert len(boards) == len(paths)
    assert len({board.board_id for board in boards}) == len(boards)

    boards_by_source = {board.source_path.name: board for board in boards if board.source_path}
    assert set(boards_by_source) == {path.name for path in paths}

    for filename, expected_board_id in {
        "nrf52833dk.yaml": "nrf52833dk",
        "nrf52840dk.yaml": "nrf52840dk",
        "nucleo_l476rg.yaml": "nucleo_l476rg",
    }.items():
        assert boards_by_source[filename].board_id == expected_board_id


def test_nrf52833_board_profile_matches_frozen_official_contract() -> None:
    [board] = load_board_configs_from_paths([BOARD_DIR / "nrf52833dk.yaml"])

    assert board.board_id == "nrf52833dk"
    assert board.display_name == "nRF52833 DK"
    assert board.mcu_family == "nrf52833"
    assert board.pyocd_target == "nrf52833"
    assert board.probe_family == "jlink"
    assert board.pack_name == "nrf52833"
    assert board.default_baudrate == 115200
    assert board.test_addr == 0x10000000
    assert board.recover_mode == "nrf_pyocd_unlock"
    assert board.silicon_id_addr == 0x10000100
    assert board.silicon_id_expected == 0x00052833
    assert board.silicon_id_label == "FICR.INFO.PART"
    assert board.expected_uart_substring == "boot ok"
    assert board.requires_recover_validation is True


def test_nrf52840_board_profile_loads_as_retained_alternate_profile() -> None:
    [board] = load_board_configs_from_paths([BOARD_DIR / "nrf52840dk.yaml"])

    assert board.board_id == "nrf52840dk"
    assert board.display_name == "nRF52840-DK"
    assert board.mcu_family == "nrf52840"
    assert board.probe_family == "jlink"
    assert board.pyocd_target == "nrf52840"
    assert board.pack_name == "nrf52840"
    assert board.default_baudrate == 115200
    assert board.test_addr == 0x10000000
    assert board.silicon_id_addr == 0x10000100
    assert board.silicon_id_expected == 0x00052840
    assert board.silicon_id_label == "FICR.INFO.PART"
    assert board.expected_uart_substring == "boot ok"
    assert board.requires_recover_validation is True
    assert board.recover_mode == "nrf_pyocd_unlock"


def test_non_nrf_recover_validation_defaults_to_manual_only(tmp_path: Path) -> None:
    board_path = tmp_path / "manual_only_board.json"
    board_path.write_text(
        json.dumps(
            {
                "board_id": "manual_only_board",
                "display_name": "Manual Only Board",
                "mcu_family": "stm32f4",
                "probe_family": "stlink",
                "pyocd_target": "stm32f4x",
                "requires_recover_validation": True,
                "test_read_address": 134217728,
            }
        ),
        encoding="utf-8",
    )

    [board] = load_board_configs_from_paths([board_path])

    assert board.requires_recover_validation is True
    assert board.recover_mode == "manual_only"


def test_invalid_recover_mode_is_rejected(tmp_path: Path) -> None:
    board_path = tmp_path / "invalid_recover_mode.json"
    board_path.write_text(
        json.dumps(
            {
                "board_id": "invalid_recover_mode",
                "display_name": "Invalid Recover Mode",
                "mcu_family": "nrf52840",
                "probe_family": "jlink",
                "pyocd_target": "nrf52840",
                "recover_mode": "run_anything",
                "test_read_address": 268435456,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="recover_mode"):
        load_board_configs_from_paths([board_path])
