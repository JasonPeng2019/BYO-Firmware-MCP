"""Shared UART capture helpers for Stage 0 and later harnesses."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from pyocd_debug_mcp.adapters.uart_interface import UARTInterface
from pyocd_debug_mcp.adapters.uart_pyserial import PySerialUARTInterface
from pyocd_debug_mcp.kernel.operations import cancellation_checkpoint

_BACKEND: UARTInterface = PySerialUARTInterface()


@dataclass(frozen=True)
class UARTCaptureResult:
    text: str
    expected_text: str | None
    reopen_count: int
    duration_seconds: float

    @property
    def excerpt(self) -> str:
        excerpt = self.text.strip().replace("\r", "\\r").replace("\n", "\\n")
        return excerpt[:300] if excerpt else ""

    @property
    def has_output(self) -> bool:
        return bool(self.text.strip())

    @property
    def matched(self) -> bool:
        if self.expected_text is None:
            return self.has_output
        return self.expected_text in self.text


@dataclass(frozen=True)
class UARTWriteResult:
    bytes_written: int
    duration_seconds: float


@dataclass(frozen=True)
class UARTExchangeStepResult:
    expected_text: str
    text: str
    bytes_written: int

    @property
    def matched(self) -> bool:
        return self.expected_text in self.text


@dataclass(frozen=True)
class UARTExchangeResult:
    text: str
    expected_text: str
    bytes_written: int
    duration_seconds: float
    expected_step_count: int
    ready_text: str | None = None
    ready_matched: bool = True
    ready_probe_bytes_written: int = 0
    steps: tuple[UARTExchangeStepResult, ...] = ()

    @property
    def matched(self) -> bool:
        return (
            self.ready_matched
            and len(self.steps) == self.expected_step_count
            and self.expected_step_count > 0
            and all(step.matched for step in self.steps)
        )

    @property
    def excerpt(self) -> str:
        value = self.text.strip().replace("\r", "\\r").replace("\n", "\\n")
        return value[:300] if value else ""


def capture_uart_output(
    device: str,
    baudrate: int,
    read_seconds: float,
    expected_text: str | None,
    *,
    on_port_open: Callable[[], None] | None = None,
    reopen_attempts: int = 0,
    reopen_delay_seconds: float = 0.15,
    per_open_window_seconds: float = 0.75,
    max_bytes: int | None = None,
    adapter: UARTInterface | None = None,
) -> UARTCaptureResult:
    """Capture UART output, using an explicit reopen only when the caller requests it.

    Reopening a virtual COM port can toggle modem-control lines or otherwise reset
    a board.  The state-preserving default is therefore one open.  Boot-capture
    callers that deliberately accept a reopen race may pass ``reopen_attempts``;
    captured bytes are then accumulated across opens.
    """

    if baudrate <= 0:
        raise ValueError("baudrate must be > 0")
    if read_seconds <= 0:
        raise ValueError("read_seconds must be > 0")
    if reopen_attempts < 0:
        raise ValueError("reopen_attempts must be >= 0")
    if reopen_delay_seconds < 0:
        raise ValueError("reopen_delay_seconds must be >= 0")
    if per_open_window_seconds <= 0:
        raise ValueError("per_open_window_seconds must be > 0")
    if max_bytes is not None and max_bytes <= 0:
        raise ValueError("max_bytes must be > 0 when provided")

    backend = adapter or _BACKEND
    started = time.monotonic()
    deadline = started + read_seconds
    captured = bytearray()
    total_attempts = max(1, reopen_attempts + 1)
    reopen_count = 0

    for attempt in range(total_attempts):
        cancellation_checkpoint()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break

        open_deadline = min(deadline, time.monotonic() + min(per_open_window_seconds, remaining))
        timeout = min(0.2, max(0.05, min(per_open_window_seconds, remaining)))
        port_handle = None
        try:
            port_handle = backend.open(device, baudrate=baudrate, timeout_seconds=timeout)
            backend.reset_input_buffer(port_handle)
            if on_port_open is not None:
                on_port_open()
            while time.monotonic() < open_deadline:
                cancellation_checkpoint()
                chunk = backend.read(port_handle, 256)
                if chunk:
                    if max_bytes is not None:
                        chunk = chunk[: max(0, max_bytes - len(captured))]
                    captured.extend(chunk)
                    text = captured.decode("utf-8", errors="replace")
                    if expected_text is None and text.strip():
                        return UARTCaptureResult(
                            text=text,
                            expected_text=expected_text,
                            reopen_count=reopen_count,
                            duration_seconds=time.monotonic() - started,
                        )
                    if expected_text and expected_text in text:
                        return UARTCaptureResult(
                            text=text,
                            expected_text=expected_text,
                            reopen_count=reopen_count,
                            duration_seconds=time.monotonic() - started,
                        )
                    if max_bytes is not None and len(captured) >= max_bytes:
                        return UARTCaptureResult(
                            text=text,
                            expected_text=expected_text,
                            reopen_count=reopen_count,
                            duration_seconds=time.monotonic() - started,
                        )
        except Exception as exc:  # noqa: BLE001 - want the raw serial error
            raise RuntimeError(f"Unable to read {device} at {baudrate} baud: {exc}") from exc
        finally:
            if port_handle is not None:
                backend.close(port_handle)

        if attempt < total_attempts - 1:
            reopen_count += 1
            if reopen_delay_seconds > 0:
                sleep_until = time.monotonic() + min(
                    reopen_delay_seconds, max(0.0, deadline - time.monotonic())
                )
                while time.monotonic() < sleep_until:
                    cancellation_checkpoint()
                    time.sleep(min(0.05, max(0.0, sleep_until - time.monotonic())))

    return UARTCaptureResult(
        text=captured.decode("utf-8", errors="replace"),
        expected_text=expected_text,
        reopen_count=reopen_count,
        duration_seconds=time.monotonic() - started,
    )


def write_uart_output(
    device: str,
    baudrate: int,
    payload: bytes,
    *,
    timeout_seconds: float = 1.0,
    adapter: UARTInterface | None = None,
) -> UARTWriteResult:
    """Write bounded UART bytes through the same backend-neutral transport as capture."""

    if baudrate <= 0:
        raise ValueError("baudrate must be > 0")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be > 0")

    backend = adapter or _BACKEND
    started = time.monotonic()
    port_handle = None
    try:
        cancellation_checkpoint()
        port_handle = backend.open(device, baudrate=baudrate, timeout_seconds=timeout_seconds)
        cancellation_checkpoint()
        bytes_written = backend.write(port_handle, payload)
        cancellation_checkpoint()
    except Exception as exc:  # noqa: BLE001 - want the raw serial error
        raise RuntimeError(f"Unable to write {device} at {baudrate} baud: {exc}") from exc
    finally:
        if port_handle is not None:
            backend.close(port_handle)
    return UARTWriteResult(
        bytes_written=bytes_written,
        duration_seconds=time.monotonic() - started,
    )


def exchange_uart_output(
    device: str,
    baudrate: int,
    payload: bytes,
    expected_text: str,
    read_seconds: float,
    *,
    ready_text: str | None = None,
    ready_seconds: float = 0.0,
    ready_probe: bytes | None = None,
    ready_probe_delay_seconds: float = 0.0,
    followup_steps: tuple[tuple[bytes, str], ...] = (),
    clear_input: bool = False,
    max_bytes: int = 65536,
    adapter: UARTInterface | None = None,
) -> UARTExchangeResult:
    """Write and capture the immediate response through one bounded port open."""

    if baudrate <= 0 or read_seconds <= 0 or max_bytes <= 0:
        raise ValueError("baudrate, read_seconds, and max_bytes must be positive")
    if ready_text is not None and (not ready_text or ready_seconds <= 0):
        raise ValueError("ready_text requires a positive ready_seconds window")
    if ready_probe_delay_seconds < 0 or ready_probe_delay_seconds > ready_seconds:
        raise ValueError("ready_probe_delay_seconds must be between 0 and ready_seconds")
    if ready_probe is None and ready_probe_delay_seconds != 0:
        raise ValueError("ready_probe_delay_seconds requires a ready_probe")
    if not payload or not expected_text:
        raise ValueError("payload and expected_text must be non-empty")
    backend = adapter or _BACKEND
    started = time.monotonic()
    captured = bytearray()
    port_handle = None
    bytes_written = 0
    try:
        cancellation_checkpoint()
        port_handle = backend.open(
            device,
            baudrate=baudrate,
            timeout_seconds=min(0.2, read_seconds),
        )
        if clear_input:
            backend.reset_input_buffer(port_handle)
        cancellation_checkpoint()
        ready_matched = ready_text is None
        ready_probe_bytes_written = 0
        if ready_text is not None:
            ready_deadline = time.monotonic() + ready_seconds
            probe_at = min(
                ready_deadline,
                time.monotonic() + ready_probe_delay_seconds,
            )
            probe_sent = not bool(ready_probe)
            while time.monotonic() < ready_deadline and len(captured) < max_bytes:
                cancellation_checkpoint()
                chunk = backend.read(port_handle, min(256, max_bytes - len(captured)))
                if chunk:
                    captured.extend(chunk)
                    if ready_text in captured.decode("utf-8", errors="replace"):
                        ready_matched = True
                        break
                if (
                    not probe_sent
                    and time.monotonic() >= probe_at
                    and time.monotonic() < ready_deadline
                ):
                    assert ready_probe is not None
                    ready_probe_bytes_written = backend.write(port_handle, ready_probe)
                    probe_sent = True
        step_results: list[UARTExchangeStepResult] = []
        if ready_matched:
            for step_payload, step_expected in ((payload, expected_text), *followup_steps):
                step_written = backend.write(port_handle, step_payload)
                bytes_written += step_written
                step_capture = bytearray()
                deadline = time.monotonic() + read_seconds
                while time.monotonic() < deadline and len(captured) < max_bytes:
                    cancellation_checkpoint()
                    chunk = backend.read(port_handle, min(256, max_bytes - len(captured)))
                    if chunk:
                        captured.extend(chunk)
                        step_capture.extend(chunk)
                        if step_expected in step_capture.decode("utf-8", errors="replace"):
                            break
                step_result = UARTExchangeStepResult(
                    step_expected,
                    step_capture.decode("utf-8", errors="replace"),
                    step_written,
                )
                step_results.append(step_result)
                if not step_result.matched:
                    break
    except Exception as exc:  # noqa: BLE001 - normalize backend-specific serial failures
        raise RuntimeError(
            f"Unable to exchange data on {device} at {baudrate} baud: {exc}"
        ) from exc
    finally:
        if port_handle is not None:
            backend.close(port_handle)
    return UARTExchangeResult(
        captured.decode("utf-8", errors="replace"),
        expected_text,
        bytes_written,
        time.monotonic() - started,
        1 + len(followup_steps),
        ready_text,
        ready_matched,
        ready_probe_bytes_written,
        tuple(step_results),
    )
