from __future__ import annotations

from pathlib import Path

import pytest

from pyocd_debug_mcp import server
from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.board_config import BoardConfig
from pyocd_debug_mcp.serial_resolver import SerialPortInfo
from pyocd_debug_mcp.services.convergence_watcher import UART_TOOL
from pyocd_debug_mcp.services.session_runtime import InMemorySessionStore
from pyocd_debug_mcp.services.symbols import ResolvedSymbol
from pyocd_debug_mcp.services.uart_capture import UARTCaptureResult, UARTWriteResult


def make_board() -> BoardConfig:
    return BoardConfig(
        board_id="nrf52833dk",
        display_name="nRF52833 DK",
        mcu_family="nrf52833",
        probe_family="jlink",
        pyocd_target="nrf52833",
        pack_name="nrf52833",
        probe_type="SEGGER J-Link",
        probe_hint_terms=("jlink", "segger"),
        serial_hint_terms=("jlink", "segger", "virtual com"),
        test_addr=0x10000000,
        silicon_id_addr=0x10000100,
        silicon_id_expected=0x00052833,
        silicon_id_label="FICR.INFO.PART",
        default_baudrate=115200,
        requires_recover_validation=True,
        recover_mode="nrf_pyocd_unlock",
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
def restore_session_handle(tmp_path: Path):
    original_handle = server._session_handle
    original_runtime = server._runtime_session
    original_store = server._session_store
    server._session_handle = None
    server._runtime_session = None
    server._session_store = InMemorySessionStore(tmp_path / "runs")
    try:
        yield
    finally:
        server._session_handle = original_handle
        server._runtime_session = original_runtime
        server._session_store = original_store


def test_flash_firmware_uses_default_board_artifact(monkeypatch, tmp_path: Path) -> None:
    board = make_board()
    handle = make_handle(board)
    artifact = tmp_path / "firmware.hex"
    artifact.write_text("hex", encoding="utf-8")
    seen: dict[str, object] = {}

    server._session_handle = handle
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

    result = server.flash_firmware()

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
    artifact.write_text("elf", encoding="utf-8")
    seen: dict[str, object] = {}

    server._session_handle = handle

    def fake_flash(handle_arg, path_arg, *, halt_after_reset: bool):
        seen["path"] = path_arg
        seen["halt_after_reset"] = halt_after_reset
        return path_arg

    monkeypatch.setattr(server.target_control, "flash_firmware", fake_flash)

    result = server.flash_firmware(str(artifact), halt_after_reset=True)

    assert seen["path"] == artifact.resolve()
    assert seen["halt_after_reset"] is True
    assert result == f"Flashed {artifact.resolve()} via pyocd-native; target left halted."


def test_flash_firmware_requires_loaded_board_for_default_artifact() -> None:
    server._session_handle = make_handle(None)

    assert (
        server.flash_firmware()
        == "Refused [flash/no-board-config]: Default flash resolution requires a loaded board config. session_id=(none)"
    )


def test_flash_firmware_requires_active_session() -> None:
    assert (
        server.flash_firmware()
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

    server._session_handle = handle

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

    result = server.read_serial()

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

    server._session_handle = handle

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

    result = server.read_serial(port="COM99", read_seconds=1.5)

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

    server._session_handle = handle

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

    result = server.read_serial(expected_text="boot ok")

    assert seen["expected_text"] == "boot ok"
    assert "expected='boot ok'" in result


def test_read_serial_refuses_nonpositive_read_seconds(monkeypatch) -> None:
    board = make_board()
    handle = make_handle(board)
    server._session_handle = handle
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

    result = server.read_serial(read_seconds=0.0)

    assert result == (
        "Refused [uart/invalid-read-seconds]: read_seconds must be > 0. session_id=(none)"
    )
    assert called == {"resolved": False, "captured": False}


def test_read_serial_refuses_nonpositive_baudrate(monkeypatch) -> None:
    board = make_board()
    handle = make_handle(board)
    server._session_handle = handle

    monkeypatch.setattr(
        server,
        "_resolve_serial_port_for_session",
        lambda handle_arg, *, override: (_ for _ in ()).throw(
            AssertionError("should not resolve port")
        ),
    )

    result = server.read_serial(baudrate=0)

    assert result == ("Refused [uart/invalid-baudrate]: baudrate must be > 0. session_id=(none)")


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

    server._session_handle = handle

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

    result = server.write_serial("hello", port="COM99", append_newline=True, timeout_seconds=0.5)

    assert seen == {
        "override": "COM99",
        "device": "COM7",
        "baudrate": 115200,
        "payload": b"hello\n",
        "timeout_seconds": 0.5,
    }
    assert result == "UART wrote 6 byte(s) on COM7 at 115200 baud via pyocd-native; duration=0.12s"


def test_write_serial_refuses_invalid_timeout(monkeypatch) -> None:
    server._session_handle = make_handle(make_board())
    monkeypatch.setattr(
        server,
        "_resolve_serial_port_for_session",
        lambda handle_arg, *, override: (_ for _ in ()).throw(
            AssertionError("should not resolve port")
        ),
    )

    result = server.write_serial("hello", timeout_seconds=0)

    assert result == (
        "Refused [uart/invalid-timeout]: timeout_seconds must be > 0. session_id=(none)"
    )


def test_read_symbol_u32_returns_resolved_target_value(monkeypatch, tmp_path: Path) -> None:
    board = make_board()
    handle = make_handle(board)
    artifact = tmp_path / "firmware.elf"
    artifact.write_text("elf", encoding="utf-8")

    server._session_handle = handle
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

    result = server.read_symbol_u32(str(artifact), "stage1_known_value")

    assert seen["handle"] is handle
    assert seen["elf_path"] == str(artifact)
    assert seen["symbol_name"] == "stage1_known_value"
    assert result == (
        f"Symbol stage1_known_value from {artifact.resolve()} "
        "@0x20000010 size=4 type=OBJECT value_u32=0x1234ABCD"
    )


def test_read_memory_refuses_invalid_word_size(monkeypatch) -> None:
    server._session_handle = make_handle(make_board())
    monkeypatch.setattr(
        server.target_control,
        "read_memory",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not read memory")),
    )

    result = server.read_memory("0x10000000", word_size=64)

    assert result == (
        "Refused [memory/invalid-word-size]: word_size must be one of: 8, 16, 32. session_id=(none)"
    )


def test_read_memory_block_refuses_invalid_length(monkeypatch) -> None:
    server._session_handle = make_handle(make_board())
    monkeypatch.setattr(
        server.target_control,
        "read_memory_block",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("should not read memory block")
        ),
    )

    result = server.read_memory_block("0x10000000", 0)

    assert result == ("Refused [memory/invalid-length]: length must be > 0. session_id=(none)")


def test_write_memory_refuses_invalid_word_size(monkeypatch) -> None:
    server._session_handle = make_handle(make_board())
    monkeypatch.setattr(
        server.target_control,
        "write_memory",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not write memory")),
    )

    result = server.write_memory("0x10000000", "0x1", word_size=64)

    assert result == (
        "Refused [memory/invalid-word-size]: word_size must be one of: 8, 16, 32. session_id=(none)"
    )


def test_read_serial_requires_loaded_board() -> None:
    server._session_handle = make_handle(None)

    assert server.read_serial() == server.NO_BOARD_CONFIG_MESSAGE


def test_unlock_recover_refuses_without_confirmation() -> None:
    server._session_handle = make_handle(make_board())

    assert (
        server.unlock_recover()
        == "Refused [recover/confirmation-required]: Recover requires confirm=True. This operation may erase flash. session_id=(none)"
    )


def test_unlock_recover_delegates_when_confirmed(monkeypatch) -> None:
    board = make_board()
    handle = make_handle(board)
    server._session_handle = handle
    seen: dict[str, object] = {}

    def fake_recover(handle_arg):
        seen["handle"] = handle_arg
        return "pyOCD API mass erase"

    monkeypatch.setattr(server.target_control, "recover_target", fake_recover)

    result = server.unlock_recover(confirm=True)

    assert seen["handle"] is handle
    assert result == "Recover completed via pyOCD API mass erase on nrf52833dk via pyocd-native."


def test_unlock_recover_requires_loaded_board() -> None:
    server._session_handle = make_handle(None)

    assert (
        server.unlock_recover(confirm=True)
        == "Refused [recover/no-board-config]: Recover requires a loaded board config with a supported recover_mode. session_id=(none)"
    )


def test_unlock_recover_requires_active_session() -> None:
    assert (
        server.unlock_recover(confirm=True)
        == "Refused [recover/no-session]: Recover requires an active connected session. session_id=(none)"
    )


def test_connect_autoresolves_jlink_probe_on_non_windows_when_uid_is_implicit(
    monkeypatch,
) -> None:
    board = make_board()
    seen: dict[str, object] = {}

    monkeypatch.delenv("PYOCD_PROBE_UID", raising=False)
    monkeypatch.delenv("PYOCD_TARGET", raising=False)
    monkeypatch.setattr(server.sys, "platform", "darwin")
    monkeypatch.setattr(server, "resolve_board_config", lambda board_id, board_config: board)
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
    assert server._runtime_session is not None


def test_connect_autoresolves_jlink_probe_on_windows_when_multiple_probes_are_attached(
    monkeypatch,
) -> None:
    board = make_board()
    seen: dict[str, object] = {}

    monkeypatch.delenv("PYOCD_PROBE_UID", raising=False)
    monkeypatch.delenv("PYOCD_TARGET", raising=False)
    monkeypatch.setattr(server.sys, "platform", "win32")
    monkeypatch.setattr(server, "resolve_board_config", lambda board_id, board_config: board)
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
    runtime = server._session_store.start_session(
        board_id="nrf52833dk",
        probe_uid="probe-123",
        route_used="pyocd-native",
    )
    server._runtime_session = runtime
    server._session_store.set_block(
        runtime,
        UART_TOOL,
        "watch/uart-miss-repetition",
        "Repeated identical UART misses detected. Disconnect and reconnect before trying again.",
    )

    result = server.read_serial()

    assert result == (
        "Blocked [watch/uart-miss-repetition]: Repeated identical UART misses detected. "
        f"Disconnect and reconnect before trying again. session_id={runtime.session_id}"
    )
