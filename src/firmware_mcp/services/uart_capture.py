"""Shared UART capture helpers for host checks and server tools."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from firmware_mcp.adapters.uart_interface import UARTInterface
from firmware_mcp.adapters.uart_pyserial import PySerialUARTInterface
from firmware_mcp.kernel.diagnostics import attach_cleanup_error
from firmware_mcp.kernel.operations import cancellation_checkpoint

_BACKEND: UARTInterface = PySerialUARTInterface()


@dataclass(frozen=True)
class UARTCaptureResult:
    text: str
    expected_text: str | None
    duration_seconds: float
    raw_bytes: bytes = b""

    @property
    def has_output(self) -> bool:
        return bool(self.text.strip())

    @property
    def matched(self) -> bool:
        if self.expected_text is None:
            # No expectation means capture is observational: a completed empty
            # read is transport success, not a content failure.
            return True
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
    raw_bytes: bytes = b""

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
    raw_bytes: bytes = b""

    @property
    def matched(self) -> bool:
        return (
            self.ready_matched
            and len(self.steps) == self.expected_step_count
            and self.expected_step_count > 0
            and all(step.matched for step in self.steps)
        )


def capture_uart_output(
    device: str,
    baudrate: int,
    read_seconds: float,
    expected_text: str | None,
    *,
    on_port_open: Callable[[], None] | None = None,
    max_bytes: int | None = None,
    adapter: UARTInterface | None = None,
) -> UARTCaptureResult:
    """Capture UART output once for the caller's exact semantic duration."""

    if baudrate <= 0:
        raise ValueError("baudrate must be > 0")
    if read_seconds <= 0:
        raise ValueError("read_seconds must be > 0")
    if max_bytes is not None and max_bytes <= 0:
        raise ValueError("max_bytes must be > 0 when provided")

    backend = adapter or _BACKEND
    started = time.monotonic()
    deadline = started + read_seconds
    captured = bytearray()
    port_handle = None
    primary: BaseException | None = None
    try:
        cancellation_checkpoint()
        port_handle = backend.open(device, baudrate=baudrate, timeout_seconds=read_seconds)
        backend.reset_input_buffer(port_handle)
        if on_port_open is not None:
            on_port_open()
        while time.monotonic() < deadline:
            cancellation_checkpoint()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # 256 bytes is transport buffering only, not a capture or duration cap.
            chunk = backend.read_with_timeout(port_handle, 256, timeout_seconds=remaining)
            if not chunk:
                continue
            if max_bytes is not None:
                chunk = chunk[: max(0, max_bytes - len(captured))]
            captured.extend(chunk)
            text = captured.decode("utf-8", errors="replace")
            if (expected_text and expected_text in text) or (
                max_bytes is not None and len(captured) >= max_bytes
            ):
                break
    except Exception as exc:  # noqa: BLE001 - retain exact transport failure
        primary = RuntimeError(f"Unable to read {device} at {baudrate} baud: {exc}")
        raise primary from exc
    finally:
        if port_handle is not None:
            try:
                backend.close(port_handle)
            except Exception as cleanup:  # noqa: BLE001 - preserve the transport error
                if primary is not None:
                    attach_cleanup_error(primary, "UART close", cleanup)
                else:
                    raise RuntimeError(
                        f"Unable to close UART {device} at {baudrate} baud: {cleanup}"
                    ) from cleanup

    return UARTCaptureResult(
        text=captured.decode("utf-8", errors="replace"),
        expected_text=expected_text,
        duration_seconds=time.monotonic() - started,
        raw_bytes=bytes(captured),
    )


def write_uart_output(
    device: str,
    baudrate: int,
    payload: bytes,
    *,
    timeout_seconds: float,
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
    primary: BaseException | None = None
    try:
        cancellation_checkpoint()
        port_handle = backend.open(device, baudrate=baudrate, timeout_seconds=timeout_seconds)
        cancellation_checkpoint()
        bytes_written = backend.write(port_handle, payload)
        if bytes_written != len(payload):
            raise RuntimeError(
                f"UART transport wrote {bytes_written} of {len(payload)} byte(s); "
                "reopen the port and retry the complete payload."
            )
        cancellation_checkpoint()
    except Exception as exc:  # noqa: BLE001 - want the raw serial error
        primary = RuntimeError(f"Unable to write {device} at {baudrate} baud: {exc}")
        raise primary from exc
    finally:
        if port_handle is not None:
            try:
                backend.close(port_handle)
            except Exception as cleanup:  # noqa: BLE001 - preserve the write failure
                if primary is not None:
                    attach_cleanup_error(primary, "UART close", cleanup)
                else:
                    raise RuntimeError(
                        f"Unable to close UART {device} at {baudrate} baud: {cleanup}"
                    ) from cleanup
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
    adapter: UARTInterface | None = None,
) -> UARTExchangeResult:
    """Write and capture the immediate response through one bounded port open."""

    if baudrate <= 0 or read_seconds <= 0:
        raise ValueError("baudrate and read_seconds must be positive")
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
    primary: BaseException | None = None
    try:
        cancellation_checkpoint()
        port_handle = backend.open(
            device,
            baudrate=baudrate,
            timeout_seconds=read_seconds,
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
            while time.monotonic() < ready_deadline:
                cancellation_checkpoint()
                remaining = ready_deadline - time.monotonic()
                if remaining <= 0:
                    break
                # 256 bytes is transport buffering only, not a response cap.
                chunk = backend.read_with_timeout(port_handle, 256, timeout_seconds=remaining)
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
                    if ready_probe_bytes_written != len(ready_probe):
                        raise RuntimeError(
                            f"UART readiness probe wrote {ready_probe_bytes_written} of "
                            f"{len(ready_probe)} byte(s); reopen the port and retry the exchange."
                        )
                    probe_sent = True
        step_results: list[UARTExchangeStepResult] = []
        if ready_matched:
            for step_payload, step_expected in ((payload, expected_text), *followup_steps):
                step_written = backend.write(port_handle, step_payload)
                if step_written != len(step_payload):
                    raise RuntimeError(
                        f"UART exchange step wrote {step_written} of {len(step_payload)} byte(s); "
                        "reopen the port and retry the complete exchange."
                    )
                bytes_written += step_written
                step_capture = bytearray()
                deadline = time.monotonic() + read_seconds
                while time.monotonic() < deadline:
                    cancellation_checkpoint()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    chunk = backend.read_with_timeout(port_handle, 256, timeout_seconds=remaining)
                    if chunk:
                        captured.extend(chunk)
                        step_capture.extend(chunk)
                        if step_expected in step_capture.decode("utf-8", errors="replace"):
                            break
                step_result = UARTExchangeStepResult(
                    step_expected,
                    step_capture.decode("utf-8", errors="replace"),
                    step_written,
                    bytes(step_capture),
                )
                step_results.append(step_result)
                if not step_result.matched:
                    break
    except Exception as exc:  # noqa: BLE001 - normalize backend-specific serial failures
        primary = RuntimeError(f"Unable to exchange data on {device} at {baudrate} baud: {exc}")
        raise primary from exc
    finally:
        if port_handle is not None:
            try:
                backend.close(port_handle)
            except Exception as cleanup:  # noqa: BLE001 - preserve the exchange failure
                if primary is not None:
                    attach_cleanup_error(primary, "UART close", cleanup)
                else:
                    raise RuntimeError(
                        f"Unable to close UART {device} at {baudrate} baud: {cleanup}"
                    ) from cleanup
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
        bytes(captured),
    )
