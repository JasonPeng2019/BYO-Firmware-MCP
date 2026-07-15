from __future__ import annotations

import json
from pathlib import Path

import pytest

from pyocd_debug_mcp import server
from pyocd_debug_mcp.board_config import ConfigError, make_board_config


def test_resolve_board_config_returns_none_without_selection(monkeypatch) -> None:
    monkeypatch.delenv("PYOCD_BOARD_ID", raising=False)
    monkeypatch.delenv("PYOCD_BOARD_CONFIG", raising=False)
    assert server.resolve_board_config(None, None) is None


def test_resolve_board_config_loads_tracked_board(monkeypatch) -> None:
    monkeypatch.delenv("PYOCD_BOARD_ID", raising=False)
    board = server.resolve_board_config("nrf52833dk", None)
    assert board is not None
    assert board.pyocd_target == "nrf52833"
    assert board.recover_mode == "nrf_pyocd_unlock"


def test_resolve_board_config_reads_env_board_id(monkeypatch) -> None:
    monkeypatch.setenv("PYOCD_BOARD_ID", "nucleo_l476rg")
    monkeypatch.delenv("PYOCD_BOARD_CONFIG", raising=False)
    board = server.resolve_board_config(None, None)
    assert board is not None
    assert board.board_id == "nucleo_l476rg"


def test_resolve_board_config_loads_custom_external_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("PYOCD_BOARD_ID", raising=False)
    monkeypatch.delenv("PYOCD_BOARD_CONFIG", raising=False)
    custom = tmp_path / "tmp_custom_pcb.json"
    custom.write_text(
        json.dumps(
            {
                "board_id": "tmp_custom_pcb",
                "display_name": "Tmp Custom PCB",
                "mcu_family": "nrf52840",
                "probe_family": "cmsisdap",
                "pyocd_target": "nrf52840",
            }
        ),
        encoding="utf-8",
    )
    board = server.resolve_board_config("tmp_custom_pcb", str(custom))
    assert board is not None
    assert board.pyocd_target == "nrf52840"
    assert board.probe_family == "cmsisdap"


def test_resolve_board_config_unknown_id_raises(monkeypatch) -> None:
    monkeypatch.delenv("PYOCD_BOARD_ID", raising=False)
    with pytest.raises(ConfigError, match="not found"):
        server.resolve_board_config("no_such_board", None)


def test_resolve_probe_uid_resolves_jlink_uid_on_non_windows(monkeypatch) -> None:
    board = server.resolve_board_config("nrf52840dk", None)
    assert board is not None

    monkeypatch.delenv("PYOCD_PROBE_UID", raising=False)
    monkeypatch.setattr(server.sys, "platform", "darwin")
    monkeypatch.setattr(
        server,
        "resolve_probe_for_board",
        lambda *args, **kwargs: type(
            "Resolution",
            (),
            {"probe": type("Probe", (), {"uid": "jlink-123"})()},
        )(),
    )

    assert server._resolve_probe_uid_for_connect(board, None) == "jlink-123"


def test_resolve_probe_uid_prefers_explicit_unique_id(monkeypatch) -> None:
    board = server.resolve_board_config("nrf52840dk", None)
    assert board is not None

    monkeypatch.setenv("PYOCD_PROBE_UID", "env-uid")
    monkeypatch.setattr(
        server,
        "resolve_probe_for_board",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("resolve_probe_for_board should not run when unique_id is explicit")
        ),
    )

    assert server._resolve_probe_uid_for_connect(board, "explicit-uid") == "explicit-uid"


def test_resolve_probe_uid_prefers_env_uid(monkeypatch) -> None:
    board = server.resolve_board_config("nrf52840dk", None)
    assert board is not None

    monkeypatch.setenv("PYOCD_PROBE_UID", "env-uid")
    monkeypatch.setattr(
        server,
        "resolve_probe_for_board",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("resolve_probe_for_board should not run when env UID is set")
        ),
    )

    assert server._resolve_probe_uid_for_connect(board, None) == "env-uid"


def test_resolve_probe_uid_uses_api_only_resolution_on_windows_jlink(monkeypatch) -> None:
    board = server.resolve_board_config("nrf52840dk", None)
    assert board is not None

    monkeypatch.delenv("PYOCD_PROBE_UID", raising=False)
    monkeypatch.setattr(server.sys, "platform", "win32")
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        server,
        "resolve_probe_for_board",
        lambda *args, **kwargs: (
            seen.update(kwargs)
            or type(
                "Resolution",
                (),
                {"probe": type("Probe", (), {"uid": "jlink-win-123"})(), "note": ""},
            )()
        ),
    )

    assert server._resolve_probe_uid_for_connect(board, None) == "jlink-win-123"
    assert seen["allow_subprocess_fallback"] is False


def test_resolve_probe_uid_returns_none_when_windows_jlink_api_resolution_is_empty(
    monkeypatch,
) -> None:
    board = server.resolve_board_config("nrf52840dk", None)
    assert board is not None

    monkeypatch.delenv("PYOCD_PROBE_UID", raising=False)
    monkeypatch.setattr(server.sys, "platform", "win32")
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        server,
        "resolve_probe_for_board",
        lambda *args, **kwargs: (
            seen.update(kwargs)
            or type(
                "Resolution",
                (),
                {"probe": None, "note": "no probes visible via API"},
            )()
        ),
    )

    assert server._resolve_probe_uid_for_connect(board, None) is None
    assert seen["allow_subprocess_fallback"] is False


def test_resolve_probe_uid_raises_for_ambiguous_non_windows_jlink(monkeypatch) -> None:
    board = server.resolve_board_config("nrf52840dk", None)
    assert board is not None

    monkeypatch.delenv("PYOCD_PROBE_UID", raising=False)
    monkeypatch.setattr(server.sys, "platform", "darwin")
    monkeypatch.setattr(
        server,
        "resolve_probe_for_board",
        lambda *args, **kwargs: type(
            "Resolution",
            (),
            {"probe": None, "note": "multiple matching probes found"},
        )(),
    )

    with pytest.raises(
        RuntimeError,
        match="Probe resolution failed for nRF52840-DK: multiple matching probes found",
    ):
        server._resolve_probe_uid_for_connect(board, None)


def test_format_board_info_includes_silicon_and_recover() -> None:
    board = server.resolve_board_config("nrf52833dk", None)
    assert board is not None
    text = server.format_board_info(board)
    assert "pyocd_target: nrf52833" in text
    assert "recover_mode: nrf_pyocd_unlock" in text
    assert "silicon_id: addr=0x10000100" in text


def test_format_board_info_minimal_board_has_no_silicon_line() -> None:
    board = make_board_config(
        {
            "board_id": "tmp_plain",
            "display_name": "Tmp Plain",
            "mcu_family": "stm32f4",
            "probe_family": "stlink",
            "pyocd_target": "stm32f4x",
            "test_read_address": "0x08000000",
        },
        None,
    )
    text = server.format_board_info(board)
    assert "recover_mode: (none)" in text
    assert "silicon_id:" not in text


def test_build_session_options_adds_jlink_workaround() -> None:
    board = server.resolve_board_config("nrf52833dk", None)
    assert board is not None

    options = server.build_session_options(board, board.pyocd_target)

    assert options is not None
    assert options["target_override"] == "nrf52833"
    assert options["jlink.non_interactive"] is False
    assert options["flash.timeout.erase_all"] == 240.0
    assert options["cpu.step.instruction.timeout"] == 2.0


def test_build_session_options_leaves_non_jlink_boards_clean() -> None:
    board = server.resolve_board_config("nucleo_l476rg", None)
    assert board is not None

    options = server.build_session_options(board, board.pyocd_target)

    assert options is not None
    assert options["target_override"] == "stm32l476rgtx"
    assert options["connect_mode"] == "under-reset"
    assert options["frequency"] == 1_000_000
    assert options["flash.timeout.erase_all"] == 240.0
    assert options["cpu.step.instruction.timeout"] == 2.0
