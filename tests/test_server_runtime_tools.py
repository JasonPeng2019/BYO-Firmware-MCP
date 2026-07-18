from __future__ import annotations

from pathlib import Path

import pytest

from pyocd_debug_mcp import server
from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.board_config import BoardConfig
from pyocd_debug_mcp.serial_resolver import SerialPortInfo
from pyocd_debug_mcp.services.convergence_watcher import UART_TOOL
from pyocd_debug_mcp.services.connections import (
    ConnectionManager,
    stable_connection_identity,
)
from pyocd_debug_mcp.services.session_runtime import InMemorySessionStore
from pyocd_debug_mcp.services.symbols import ResolvedSymbol
from pyocd_debug_mcp.services.uart_capture import UARTCaptureResult, UARTWriteResult

BOARD_ID = "nrf52833dk"


def make_board() -> BoardConfig:
    return BoardConfig(
        board_id="nrf52833dk",
        display_name="nRF52833 DK",
        mcu_family="nrf52833",
        probe_family="jlink",
        target_identity="nrf52833",
        probe_type="SEGGER J-Link",
        probe_hint_terms=("jlink", "segger"),
        serial_hint_terms=("jlink", "segger", "virtual com"),
        test_addr=0x10000000,
        silicon_id_addr=0x10000100,
        silicon_id_expected=0x00052833,
        silicon_id_label="FICR.INFO.PART",
        default_baudrate=115200,
        requires_recover_validation=True,
        recover_mode="backend_mass_erase",
        expected_uart_substring="boot ok",
    )


def make_handle(board: BoardConfig | None) -> TargetSessionHandle:
    session_board = type(
        "SessionBoard", (), {"name": board.display_name if board else "Raw Target"}
    )()
    session = type("Session", (), {"board": session_board if board else None})()
    return TargetSessionHandle(
        session=session,
        board=board,
        probe_uid="probe-123",
        route_used="pyocd-native",
        target_override=board.pyocd_target if board else "raw-target",
    )


@pytest.fixture(autouse=True)
def restore_connections(tmp_path: Path):
    original_manager = server.connection_manager
    original_store = server._session_store
    server.connection_manager = ConnectionManager()
    server._session_store = InMemorySessionStore(tmp_path / "runs")
    try:
        yield
    finally:
        server.connection_manager = original_manager
        server._session_store = original_store


def attach_handle(
    handle: TargetSessionHandle,
    *,
    board_id: str = BOARD_ID,
):
    connection_id = stable_connection_identity(handle)
    runtime = server._session_store.start_session(
        board_id=board_id,
        connection_id=connection_id,
        probe_uid=handle.probe_uid,
        route_used=handle.route_used,
    )
    server.connection_manager.assign(
        board_id,
        handle,
        runtime,
        connection_id=connection_id,
    )
    return runtime


def test_flash_firmware_uses_default_board_artifact(monkeypatch, tmp_path: Path) -> None:
    board = make_board()
    handle = make_handle(board)
    artifact = tmp_path / "firmware.hex"
    artifact.write_text("hex", encoding="utf-8")
    seen: dict[str, object] = {}

    attach_handle(handle)
    monkeypatch.setattr(
        server,
        "resolve_flash_request",
        lambda handle_arg, *, explicit_path, action_context: type(
            "ResolvedFlashRequest",
            (),
            {
                "artifact_path": artifact,
                "identity": type(
                    "FlashIdentity",
                    (),
                    {
                        "as_log_fields": staticmethod(
                            lambda: {
                                "artifact_path": str(artifact),
                                "artifact_suffix": ".hex",
                                "artifact_size_bytes": artifact.stat().st_size,
                                "artifact_sha256": "sha",
                                "artifact_source": "default",
                            }
                        )
                    },
                )(),
            },
        )(),
    )

    def fake_flash(handle_arg, path_arg, *, halt_after_reset: bool):
        seen["handle"] = handle_arg
        seen["path"] = path_arg
        seen["halt_after_reset"] = halt_after_reset
        return path_arg

    monkeypatch.setattr(server.target_control, "flash_firmware", fake_flash)

    result = server.flash_firmware(BOARD_ID)

    assert seen["handle"] is handle
    assert seen["path"] == artifact
    assert seen["halt_after_reset"] is False
    assert result == f"Flashed {artifact} via pyocd-native; target left running."


def test_flash_firmware_uses_explicit_path_without_board_config(
    monkeypatch,
    tmp_path: Path,
) -> None:
    handle = make_handle(None)
    artifact = tmp_path / "custom.elf"
    artifact.write_bytes(b"\x7fELF" + bytes(60))
    seen: dict[str, object] = {}

    attach_handle(handle)

    def fake_flash(handle_arg, path_arg, *, halt_after_reset: bool):
        seen["path"] = path_arg
        seen["halt_after_reset"] = halt_after_reset
        return path_arg

    monkeypatch.setattr(server.target_control, "flash_firmware", fake_flash)

    result = server.flash_firmware(BOARD_ID, str(artifact), halt_after_reset=True)

    assert seen["path"] == artifact.resolve()
    assert seen["halt_after_reset"] is True
    assert result == f"Flashed {artifact.resolve()} via pyocd-native; target left halted."


def test_flash_firmware_requires_loaded_board_for_default_artifact() -> None:
    attach_handle(make_handle(None))

    result = server.flash_firmware(BOARD_ID)
    assert result.startswith(
        "Refused [flash/no-board-config]: Default flash resolution requires a loaded "
        "board config. session_id="
    )


def test_flash_firmware_requires_active_session() -> None:
    assert (
        server.flash_firmware(BOARD_ID)
        == "Refused [flash/no-session]: Flash requires an active connected session. session_id=(none)"
    )


def test_read_serial_without_expected_text_accepts_any_output(monkeypatch) -> None:
    board = make_board()
    handle = make_handle(board)
    port = SerialPortInfo(
        device="/dev/cu.usbmodem0001",
        description="J-Link",
        manufacturer="SEGGER",
        product="J-Link",
        interface="VCOM",
        hwid="USB VID:PID=1366:0105",
    )
    seen: dict[str, object] = {}

    attach_handle(handle)

    def fake_resolve(handle_arg, *, override: str | None):
        seen["override"] = override
        return port

    def fake_capture(device, baudrate, read_seconds, expected_text, *, on_port_open=None):
        seen["device"] = device
        seen["baudrate"] = baudrate
        seen["read_seconds"] = read_seconds
        seen["expected_text"] = expected_text
        seen["has_hook"] = on_port_open is not None
        return UARTCaptureResult(
            text="boot ok\r\n",
            expected_text=expected_text,
            reopen_count=1,
            duration_seconds=0.5,
        )

    monkeypatch.setattr(server, "_resolve_serial_port_for_session", fake_resolve)
    monkeypatch.setattr(server, "capture_uart_output", fake_capture)

    result = server.read_serial(BOARD_ID)

    assert seen["override"] is None
    assert seen["device"] == "/dev/cu.usbmodem0001"
    assert seen["baudrate"] == 115200
    assert seen["read_seconds"] == 3.0
    assert seen["expected_text"] is None
    assert seen["has_hook"] is False
    assert result == (
        "UART matched on /dev/cu.usbmodem0001 at 115200 baud via pyocd-native; "
        "expected=(none); reopen_count=1; duration=0.50s; excerpt=boot ok"
    )


def test_read_serial_uses_port_override(monkeypatch) -> None:
    board = make_board()
    handle = make_handle(board)
    port = SerialPortInfo(
        device="COM7",
        description="J-Link",
        manufacturer="SEGGER",
        product="J-Link",
        interface="VCOM",
        hwid="USB VID:PID=1366:0105",
    )
    seen: dict[str, object] = {}

    attach_handle(handle)

    def fake_resolve(handle_arg, *, override: str | None):
        seen["override"] = override
        return port

    monkeypatch.setattr(server, "_resolve_serial_port_for_session", fake_resolve)
    monkeypatch.setattr(
        server,
        "capture_uart_output",
        lambda device, baudrate, read_seconds, expected_text, *, on_port_open=None: (
            UARTCaptureResult(
                text="boot ok\r\n",
                expected_text=expected_text,
                reopen_count=0,
                duration_seconds=0.25,
            )
        ),
    )

    result = server.read_serial(BOARD_ID, port="COM99", read_seconds=1.5)

    assert seen["override"] == "COM99"
    assert "COM7" in result
    assert "duration=0.25s" in result


def test_read_serial_uses_explicit_expected_text(monkeypatch) -> None:
    board = make_board()
    handle = make_handle(board)
    port = SerialPortInfo(
        device="COM8",
        description="J-Link",
        manufacturer="SEGGER",
        product="J-Link",
        interface="VCOM",
        hwid="USB VID:PID=1366:0105",
    )
    seen: dict[str, object] = {}

    attach_handle(handle)

    monkeypatch.setattr(
        server, "_resolve_serial_port_for_session", lambda handle_arg, *, override: port
    )

    def fake_capture(device, baudrate, read_seconds, expected_text, *, on_port_open=None):
        seen["expected_text"] = expected_text
        return UARTCaptureResult(
            text="boot ok\r\n",
            expected_text=expected_text,
            reopen_count=0,
            duration_seconds=0.2,
        )

    monkeypatch.setattr(server, "capture_uart_output", fake_capture)

    result = server.read_serial(BOARD_ID, expected_text="boot ok")

    assert seen["expected_text"] == "boot ok"
    assert "expected='boot ok'" in result


def test_read_serial_refuses_nonpositive_read_seconds(monkeypatch) -> None:
    board = make_board()
    handle = make_handle(board)
    attach_handle(handle)
    called: dict[str, bool] = {"resolved": False, "captured": False}

    def fake_resolve(handle_arg, *, override: str | None):
        del handle_arg, override
        called["resolved"] = True
        raise AssertionError("should not resolve port for invalid read_seconds")

    def fake_capture(*args, **kwargs):
        called["captured"] = True
        raise AssertionError("should not capture UART for invalid read_seconds")

    monkeypatch.setattr(server, "_resolve_serial_port_for_session", fake_resolve)
    monkeypatch.setattr(server, "capture_uart_output", fake_capture)

    result = server.read_serial(BOARD_ID, read_seconds=0.0)

    assert result.startswith(
        "Refused [uart/invalid-read-seconds]: read_seconds must be > 0. session_id="
    )
    assert called == {"resolved": False, "captured": False}


def test_read_serial_refuses_nonpositive_baudrate(monkeypatch) -> None:
    board = make_board()
    handle = make_handle(board)
    attach_handle(handle)

    monkeypatch.setattr(
        server,
        "_resolve_serial_port_for_session",
        lambda handle_arg, *, override: (_ for _ in ()).throw(
            AssertionError("should not resolve port")
        ),
    )

    result = server.read_serial(BOARD_ID, baudrate=0)

    assert result.startswith("Refused [uart/invalid-baudrate]: baudrate must be > 0. session_id=")


def test_write_serial_uses_port_override_and_utf8_payload(monkeypatch) -> None:
    board = make_board()
    handle = make_handle(board)
    port = SerialPortInfo(
        device="COM7",
        description="J-Link",
        manufacturer="SEGGER",
        product="J-Link",
        interface="VCOM",
        hwid="USB VID:PID=1366:0105",
    )
    seen: dict[str, object] = {}

    attach_handle(handle)

    def fake_resolve(handle_arg, *, override: str | None):
        seen["override"] = override
        return port

    def fake_write(device, baudrate, payload, *, timeout_seconds):
        seen["device"] = device
        seen["baudrate"] = baudrate
        seen["payload"] = payload
        seen["timeout_seconds"] = timeout_seconds
        return UARTWriteResult(bytes_written=len(payload), duration_seconds=0.125)

    monkeypatch.setattr(server, "_resolve_serial_port_for_session", fake_resolve)
    monkeypatch.setattr(server, "write_uart_output", fake_write)

    result = server.write_serial(
        BOARD_ID,
        "hello",
        port="COM99",
        append_newline=True,
        timeout_seconds=0.5,
    )

    assert seen == {
        "override": "COM99",
        "device": "COM7",
        "baudrate": 115200,
        "payload": b"hello\n",
        "timeout_seconds": 0.5,
    }
    assert result == "UART wrote 6 byte(s) on COM7 at 115200 baud via pyocd-native; duration=0.12s"


def test_write_serial_refuses_invalid_timeout(monkeypatch) -> None:
    attach_handle(make_handle(make_board()))
    monkeypatch.setattr(
        server,
        "_resolve_serial_port_for_session",
        lambda handle_arg, *, override: (_ for _ in ()).throw(
            AssertionError("should not resolve port")
        ),
    )

    result = server.write_serial(BOARD_ID, "hello", timeout_seconds=0)

    assert result.startswith(
        "Refused [uart/invalid-timeout]: timeout_seconds must be > 0. session_id="
    )


def test_read_symbol_u32_returns_resolved_target_value(monkeypatch, tmp_path: Path) -> None:
    board = make_board()
    handle = make_handle(board)
    artifact = tmp_path / "firmware.elf"
    artifact.write_text("elf", encoding="utf-8")

    attach_handle(handle)
    seen: dict[str, object] = {}

    def fake_read_symbol(handle_arg, elf_path_arg, symbol_name_arg):
        seen["handle"] = handle_arg
        seen["elf_path"] = elf_path_arg
        seen["symbol_name"] = symbol_name_arg
        return ResolvedSymbol(
            name="stage1_known_value",
            address=0x20000010,
            size=4,
            type="OBJECT",
            value_u32=0x1234ABCD,
        )

    monkeypatch.setattr(server, "read_symbol_u32_from_elf", fake_read_symbol)

    result = server.read_symbol_u32(BOARD_ID, str(artifact), "stage1_known_value")

    assert seen["handle"] is handle
    assert seen["elf_path"] == str(artifact)
    assert seen["symbol_name"] == "stage1_known_value"
    assert result == (
        f"Symbol stage1_known_value from {artifact.resolve()} "
        "@0x20000010 size=4 type=OBJECT value_u32=0x1234ABCD"
    )


def test_read_memory_refuses_invalid_word_size(monkeypatch) -> None:
    attach_handle(make_handle(make_board()))
    monkeypatch.setattr(
        server.target_control,
        "read_memory",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not read memory")),
    )

    result = server.read_memory(BOARD_ID, "0x10000000", word_size=64)

    assert result.startswith(
        "Refused [memory/invalid-word-size]: word_size is not supported by the connected "
        "backend. session_id="
    )


def test_read_memory_block_refuses_invalid_length(monkeypatch) -> None:
    attach_handle(make_handle(make_board()))
    monkeypatch.setattr(
        server.target_control,
        "read_memory_block",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("should not read memory block")
        ),
    )

    result = server.read_memory_block(BOARD_ID, "0x10000000", 0)

    assert result.startswith("Refused [memory/invalid-length]: length must be > 0. session_id=")


def test_write_memory_refuses_invalid_width(monkeypatch) -> None:
    attach_handle(make_handle(make_board()))
    monkeypatch.setattr(
        server.target_control,
        "write_memory",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not write memory")),
    )

    result = server.write_memory(
        BOARD_ID,
        "0x10000000",
        "0x1",
        width=64,
        allow_address_fallback=True,
        reason="The location is pointer-derived.",
    )

    assert result.startswith(
        "Refused [memory/invalid-width]: width must be supported by the connected backend "
        "(8, 16, 32 bits). session_id="
    )


def test_read_serial_requires_loaded_board() -> None:
    attach_handle(make_handle(None))

    assert server.read_serial(BOARD_ID) == server.NO_BOARD_CONFIG_MESSAGE


def test_legacy_unlock_recover_surface_is_removed() -> None:
    registered = {tool.name for tool in server.mcp._tool_manager.list_tools()}

    assert "unlock_recover" not in registered
    assert not hasattr(server, "unlock_recover")
    assert {"target_unlock-plan", "target_unlock"} <= registered


def test_connect_autoresolves_jlink_probe_on_non_windows_when_uid_is_implicit(
    monkeypatch,
) -> None:
    board = make_board()
    seen: dict[str, object] = {}

    monkeypatch.delenv("PYOCD_PROBE_UID", raising=False)
    monkeypatch.delenv("PYOCD_TARGET", raising=False)
    monkeypatch.setattr(server.sys, "platform", "darwin")
    monkeypatch.setattr(
        server, "resolve_board_config", lambda board_id, board_config, **kwargs: board
    )
    monkeypatch.setattr(
        server,
        "resolve_probe_for_board",
        lambda *args, **kwargs: type(
            "Resolution",
            (),
            {"probe": type("Probe", (), {"uid": "jlink-123"})()},
        )(),
    )

    def fake_open_session(*, board, unique_id, target, server_timeouts=None):
        seen["board"] = board
        seen["unique_id"] = unique_id
        seen["target"] = target
        seen["server_timeouts"] = server_timeouts
        return TargetSessionHandle(
            session=type("Session", (), {"board": type("Board", (), {"name": "nRF52833 DK"})()})(),
            board=board,
            probe_uid=unique_id,
            route_used="pyocd-native",
            target_override=target,
        )

    monkeypatch.setattr(server.target_control, "open_session", fake_open_session)

    result = server.connect(board_id="nrf52833dk")

    assert seen["board"] is board
    assert seen["unique_id"] == "jlink-123"
    assert seen["target"] == "nrf52833"
    assert "Connected to board" in result
    assert "probe jlink-123" in result
    assert "[board config: nrf52833dk]" in result
    assert "session_id=" in result
    assert server.connection_manager.runtime_for(BOARD_ID) is not None


def test_connect_autoresolves_jlink_probe_on_windows_when_multiple_probes_are_attached(
    monkeypatch,
) -> None:
    board = make_board()
    seen: dict[str, object] = {}

    monkeypatch.delenv("PYOCD_PROBE_UID", raising=False)
    monkeypatch.delenv("PYOCD_TARGET", raising=False)
    monkeypatch.setattr(server.sys, "platform", "win32")
    monkeypatch.setattr(
        server, "resolve_board_config", lambda board_id, board_config, **kwargs: board
    )
    monkeypatch.setattr(
        server,
        "resolve_probe_for_board",
        lambda *args, **kwargs: type(
            "Resolution",
            (),
            {"probe": type("Probe", (), {"uid": "jlink-683377322"})(), "note": ""},
        )(),
    )

    def fake_open_session(*, board, unique_id, target, server_timeouts=None):
        seen["board"] = board
        seen["unique_id"] = unique_id
        seen["target"] = target
        seen["server_timeouts"] = server_timeouts
        return TargetSessionHandle(
            session=type("Session", (), {"board": type("Board", (), {"name": "nRF52833 DK"})()})(),
            board=board,
            probe_uid="jlink-683377322",
            route_used="pyocd-native",
            target_override=target,
        )

    monkeypatch.setattr(server.target_control, "open_session", fake_open_session)

    result = server.connect(board_id="nrf52833dk")

    assert seen["board"] is board
    assert seen["unique_id"] == "jlink-683377322"
    assert seen["target"] == "nrf52833"
    assert "Connected to board" in result
    assert "probe jlink-683377322" in result


def test_read_serial_returns_blocked_message_for_watcher_state() -> None:
    runtime = attach_handle(make_handle(make_board()))
    server._session_store.set_block(
        runtime,
        UART_TOOL,
        "watch/uart-miss-repetition",
        "Repeated identical UART misses detected. Disconnect and reconnect before trying again.",
    )

    result = server.read_serial(BOARD_ID)

    assert result == (
        "Blocked [watch/uart-miss-repetition]: Repeated identical UART misses detected. "
        f"Disconnect and reconnect before trying again. session_id={runtime.session_id}"
    )

