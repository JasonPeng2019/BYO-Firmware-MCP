from __future__ import annotations

import sys
from types import SimpleNamespace

from pyocd_debug_mcp.adapters.uart_pyserial import PySerialUARTInterface
from pyocd_debug_mcp.adapters.uart_interface import UARTInterface, UARTPortHandle
from pyocd_debug_mcp.services import uart_capture


class FakeUARTAdapter(UARTInterface):
    def __init__(
        self,
        sessions: list[list[bytes | Exception]],
        *,
        open_error: Exception | None = None,
    ) -> None:
        self._sessions = [list(session) for session in sessions]
        self._open_error = open_error
        self.open_count = 0
        self.reset_count = 0
        self.close_count = 0
        self.writes: list[bytes] = []

    def open(self, device: str, *, baudrate: int, timeout_seconds: float) -> UARTPortHandle:
        assert device
        assert baudrate > 0
        assert timeout_seconds > 0
        if self._open_error is not None:
            raise self._open_error
        self.open_count += 1
        return UARTPortHandle(
            handle=self._sessions.pop(0),
            device=device,
            baudrate=baudrate,
            timeout_seconds=timeout_seconds,
        )

    def close(self, handle: UARTPortHandle) -> None:
        self.close_count += 1

    def reset_input_buffer(self, handle: UARTPortHandle) -> None:
        self.reset_count += 1

    def read(self, handle: UARTPortHandle, size: int) -> bytes:
        session = handle.handle
        if session:
            item = session.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return b""

    def write(self, handle: UARTPortHandle, data: bytes) -> int:
        self.writes.append(data)
        return len(data)


def test_capture_uart_output_matches_expected_text_across_partial_chunks() -> None:
    adapter = FakeUARTAdapter([[b"bo", b"ot ", b"ok\r\n"]])

    result = uart_capture.capture_uart_output(
        "COM1",
        115200,
        0.05,
        "boot ok",
        reopen_attempts=0,
        per_open_window_seconds=0.05,
        adapter=adapter,
    )

    assert result.matched is True
    assert "boot ok" in result.text


def test_capture_uart_output_times_out_without_expected_text() -> None:
    adapter = FakeUARTAdapter([[b"noise only\r\n"]])

    result = uart_capture.capture_uart_output(
        "COM1",
        115200,
        0.05,
        "boot ok",
        reopen_attempts=0,
        per_open_window_seconds=0.05,
        adapter=adapter,
    )

    assert result.matched is False
    assert "noise only" in result.text


def test_capture_uart_output_reopens_once_when_first_open_is_quiet() -> None:
    adapter = FakeUARTAdapter([[b""], [b"boot ok\r\n"]])

    result = uart_capture.capture_uart_output(
        "COM1",
        115200,
        0.03,
        "boot ok",
        reopen_attempts=1,
        reopen_delay_seconds=0,
        per_open_window_seconds=0.01,
        adapter=adapter,
    )

    assert result.matched is True
    assert result.reopen_count == 1


def test_capture_default_preserves_state_with_exactly_one_open() -> None:
    adapter = FakeUARTAdapter([[b"quiet"], [b"unexpected second open"]])

    result = uart_capture.capture_uart_output(
        "COM1",
        115200,
        0.02,
        "not present",
        per_open_window_seconds=0.02,
        adapter=adapter,
    )

    assert result.matched is False
    assert result.reopen_count == 0
    assert adapter.open_count == 1


def test_capture_uart_output_without_expected_text_reports_any_output() -> None:
    adapter = FakeUARTAdapter([[b"hello world\r\n"]])

    result = uart_capture.capture_uart_output(
        "COM1",
        115200,
        0.05,
        None,
        reopen_attempts=0,
        per_open_window_seconds=0.05,
        adapter=adapter,
    )

    assert result.has_output is True
    assert result.matched is True
    assert result.excerpt == "hello world"


def test_capture_uart_output_surfaces_open_failures() -> None:
    adapter = FakeUARTAdapter([], open_error=OSError("port busy"))

    try:
        uart_capture.capture_uart_output(
            "COM1",
            115200,
            0.05,
            "boot ok",
            reopen_attempts=0,
            per_open_window_seconds=0.05,
            adapter=adapter,
        )
    except RuntimeError as exc:
        assert "port busy" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_capture_uart_output_surfaces_mid_read_disappearance() -> None:
    adapter = FakeUARTAdapter([[b"bo", OSError("device disappeared")]])

    try:
        uart_capture.capture_uart_output(
            "COM1",
            115200,
            0.05,
            "boot ok",
            reopen_attempts=0,
            per_open_window_seconds=0.05,
            adapter=adapter,
        )
    except RuntimeError as exc:
        assert "device disappeared" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_capture_uart_output_decodes_non_utf8_with_replacement() -> None:
    adapter = FakeUARTAdapter([[b"\xffboot ok\r\n"]])

    result = uart_capture.capture_uart_output(
        "COM1",
        115200,
        0.05,
        "boot ok",
        reopen_attempts=0,
        per_open_window_seconds=0.05,
        adapter=adapter,
    )

    assert result.matched is True
    assert "\ufffd" in result.text


def test_capture_uart_output_rejects_nonpositive_read_seconds() -> None:
    adapter = FakeUARTAdapter([[b"boot ok\r\n"]])

    try:
        uart_capture.capture_uart_output(
            "COM1",
            115200,
            0.0,
            "boot ok",
            adapter=adapter,
        )
    except ValueError as exc:
        assert "read_seconds must be > 0" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_capture_uart_output_rejects_nonpositive_baudrate() -> None:
    adapter = FakeUARTAdapter([[b"boot ok\r\n"]])

    try:
        uart_capture.capture_uart_output(
            "COM1",
            0,
            0.05,
            "boot ok",
            adapter=adapter,
        )
    except ValueError as exc:
        assert "baudrate must be > 0" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_write_uart_output_writes_payload_and_closes_port() -> None:
    adapter = FakeUARTAdapter([[]])

    result = uart_capture.write_uart_output(
        "COM1",
        115200,
        b"hello\n",
        timeout_seconds=0.2,
        adapter=adapter,
    )

    assert result.bytes_written == 6
    assert result.duration_seconds >= 0


def test_write_uart_output_rejects_nonpositive_timeout() -> None:
    adapter = FakeUARTAdapter([[]])

    try:
        uart_capture.write_uart_output(
            "COM1",
            115200,
            b"hello",
            timeout_seconds=0,
            adapter=adapter,
        )
    except ValueError as exc:
        assert "timeout_seconds must be > 0" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_exchange_uart_output_writes_and_matches_split_response_in_one_open() -> None:
    adapter = FakeUARTAdapter([[b"BLINK ", b"ON\r\n"]])

    result = uart_capture.exchange_uart_output(
        "COM1",
        115200,
        b"blink on\n",
        "BLINK ON",
        0.1,
        adapter=adapter,
    )

    assert result.bytes_written == len(b"blink on\n")
    assert result.matched is True
    assert "BLINK ON" in result.text


def test_exchange_waits_for_same_open_readiness_before_writing() -> None:
    adapter = FakeUARTAdapter([[b"booting\r\n", b"nf-board> ", b"BLINK ON\r\n"]])

    result = uart_capture.exchange_uart_output(
        "COM1",
        115200,
        b"blink on\n",
        "BLINK ON",
        0.1,
        ready_text="nf-board>",
        ready_seconds=0.1,
        ready_probe=b"\n",
        adapter=adapter,
    )

    assert result.ready_matched is True
    assert result.ready_probe_bytes_written == 1
    assert result.bytes_written == len(b"blink on\n")
    assert result.matched is True


def test_exchange_observes_boot_marker_before_delayed_probe_without_sending_probe() -> None:
    adapter = FakeUARTAdapter([[b"booting\r\n", b"READY\r\n", b"COMMAND OK\r\n"]])

    result = uart_capture.exchange_uart_output(
        "COM1",
        115200,
        b"command\n",
        "COMMAND OK",
        0.1,
        ready_text="READY",
        ready_seconds=0.1,
        ready_probe=b"status\n",
        ready_probe_delay_seconds=0.05,
        adapter=adapter,
    )

    assert result.ready_matched is True
    assert result.ready_probe_bytes_written == 0
    assert adapter.writes == [b"command\n"]
    assert result.matched is True


def test_exchange_rejects_delay_without_probe() -> None:
    adapter = FakeUARTAdapter([[]])

    try:
        uart_capture.exchange_uart_output(
            "COM1",
            115200,
            b"command\n",
            "COMMAND OK",
            0.1,
            ready_text="READY",
            ready_seconds=0.1,
            ready_probe_delay_seconds=0.05,
            adapter=adapter,
        )
    except ValueError as exc:
        assert "requires a ready_probe" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_exchange_runs_ordered_followups_without_reopening() -> None:
    adapter = FakeUARTAdapter(
        [
            [
                b"nf-board> ",
                b"BLINK ON\r\n",
                b"BLINK STATUS: ON\r\n",
                b"BLINK OFF\r\n",
                b"BLINK STATUS: OFF\r\n",
            ]
        ]
    )

    result = uart_capture.exchange_uart_output(
        "COM1",
        115200,
        b"blink on\n",
        "BLINK ON",
        0.1,
        ready_text="nf-board>",
        ready_seconds=0.1,
        ready_probe=b"\n",
        followup_steps=(
            (b"blink status\n", "BLINK STATUS: ON"),
            (b"blink off\n", "BLINK OFF"),
            (b"blink status\n", "BLINK STATUS: OFF"),
        ),
        adapter=adapter,
    )

    assert result.matched is True
    assert [step.matched for step in result.steps] == [True, True, True, True]
    assert adapter.open_count == 1
    assert adapter.reset_count == 0


def test_exchange_discards_buffer_only_when_explicitly_requested() -> None:
    adapter = FakeUARTAdapter([[b"READY", b"OK"]])

    result = uart_capture.exchange_uart_output(
        "COM1",
        115200,
        b"go\r\n",
        "OK",
        0.1,
        ready_text="READY",
        ready_seconds=0.1,
        clear_input=True,
        adapter=adapter,
    )

    assert result.matched
    assert adapter.reset_count == 1


def test_exchange_readiness_failure_sends_no_commands_and_closes() -> None:
    adapter = FakeUARTAdapter([[b"FIRST OK but never ready"]])

    result = uart_capture.exchange_uart_output(
        "COM1",
        115200,
        b"first\n",
        "FIRST OK",
        0.01,
        ready_text="READY",
        ready_seconds=0.01,
        ready_probe=b"\n",
        followup_steps=((b"second\n", "SECOND OK"),),
        adapter=adapter,
    )

    assert result.ready_matched is False
    assert result.matched is False
    assert result.steps == ()
    assert adapter.writes == [b"\n"]
    assert adapter.close_count == 1


def test_exchange_first_step_mismatch_sends_no_followups_and_closes() -> None:
    adapter = FakeUARTAdapter([[b"WRONG"]])

    result = uart_capture.exchange_uart_output(
        "COM1",
        115200,
        b"first\n",
        "FIRST OK",
        0.01,
        followup_steps=((b"second\n", "SECOND OK"), (b"third\n", "THIRD OK")),
        adapter=adapter,
    )

    assert [step.matched for step in result.steps] == [False]
    assert adapter.writes == [b"first\n"]
    assert adapter.close_count == 1


def test_exchange_middle_step_mismatch_sends_no_later_steps_and_closes() -> None:
    adapter = FakeUARTAdapter([[b"FIRST OK", b"WRONG SECOND"]])

    result = uart_capture.exchange_uart_output(
        "COM1",
        115200,
        b"first\n",
        "FIRST OK",
        0.01,
        followup_steps=((b"second\n", "SECOND OK"), (b"third\n", "THIRD OK")),
        adapter=adapter,
    )

    assert [step.matched for step in result.steps] == [True, False]
    assert adapter.writes == [b"first\n", b"second\n"]
    assert adapter.close_count == 1


def test_pyserial_open_sets_read_and_write_timeouts(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeSerial:
        def __init__(
            self, device: str, *, baudrate: int, timeout: float, write_timeout: float
        ) -> None:
            captured["device"] = device
            captured["baudrate"] = baudrate
            captured["timeout"] = timeout
            captured["write_timeout"] = write_timeout

    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=FakeSerial))

    handle = PySerialUARTInterface().open("COM9", baudrate=115200, timeout_seconds=0.2)

    assert captured == {
        "device": "COM9",
        "baudrate": 115200,
        "timeout": 0.2,
        "write_timeout": 0.2,
    }
    assert handle.timeout_seconds == 0.2


def test_pyserial_write_flushes_and_returns_count(monkeypatch) -> None:
    class FakeSerial:
        def __init__(
            self, device: str, *, baudrate: int, timeout: float, write_timeout: float
        ) -> None:
            self.flushed = False

        def write(self, data: bytes) -> int:
            return len(data)

        def flush(self) -> None:
            self.flushed = True

    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=FakeSerial))

    adapter = PySerialUARTInterface()
    handle = adapter.open("COM9", baudrate=115200, timeout_seconds=0.2)

    assert adapter.write(handle, b"abc") == 3
    assert handle.handle.flushed is True
