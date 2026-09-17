"""Explicit raw hardware tools for no-setup and deliberate lite bypasses."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyocd_debug_mcp.capabilities.routing import TierRouter
from pyocd_debug_mcp.services.uart_exchange_schema import (
    MAX_UART_SECONDS,
    MAX_UART_TEXT_BYTES,
    validate_serial_exchange_parameters,
)


_RAW_FAILURE_CODES = {
    "read_memory_raw": "backend/read-failed",
    "write_memory_raw": "backend/write-failed",
    "register_write_raw": "backend/register-write-failed",
    "write_cpu_register_raw": "backend/register-write-failed",
    "set_execution_state_raw": "backend/execution-state-failed",
    "set_breakpoint_raw": "backend/breakpoint-failed",
    "reset_and_halt_raw": "backend/reset-failed",
    "flash_raw": "backend/flash-failed",
    "read_serial_raw": "backend/serial-read-failed",
    "write_serial_raw": "backend/serial-write-failed",
    "serial_exchange_raw": "backend/serial-exchange-failed",
    "target_unlock_raw": "backend/recovery-failed",
}


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a numeric integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 0)
    raise ValueError(f"{name} must be a numeric integer")


def _width(width: int) -> int:
    if isinstance(width, bool) or width not in {8, 16, 32}:
        raise ValueError("width must be one of: 8, 16, 32")
    return width


def _bounded_seconds(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a positive finite number")
    if not 0 < value <= MAX_UART_SECONDS:
        raise ValueError(f"{name} must be greater than zero and at most {MAX_UART_SECONDS:g}")
    return float(value)


@dataclass(frozen=True, slots=True)
class RawToolServices:
    # A callable keeps public raw handlers aligned with an injected policy
    # repository.  Route guards already dereference the server-global router;
    # capturing the import-time instance here would make their result disagree.
    router: TierRouter | Callable[[], TierRouter]
    global_stop: Callable[[], None]
    handle_for: Callable[[str], Any]
    read_memory: Callable[[Any, int, int], int]
    read_block: Callable[[Any, int, int], list[int]]
    write_memory: Callable[[Any, int, int, int], None]
    write_register: Callable[[Any, str, int], None]
    set_breakpoint: Callable[[Any, int], None]
    reset: Callable[[Any, bool], None]
    flash: Callable[[Any, Path], tuple[Path, str]]
    recover: Callable[[Any, str], str]
    capture_uart: Callable[..., Any]
    write_uart: Callable[..., Any]
    exchange_uart: Callable[..., Any]
    finalize_recovery: Callable[[str], None] | None = None


def build_raw_handlers(services: RawToolServices) -> dict[str, Callable[..., str]]:
    """Build raw calls with a common, versioned response and lite warning envelope."""

    def router() -> TierRouter:
        configured = services.router
        return configured() if callable(configured) else configured

    def execute(operation: str, board_id: str, action: Callable[[], object]) -> str:
        active_router = router()
        state = active_router.state_for(board_id)
        warning = active_router.require_raw(operation, board_id, state=state)
        try:
            services.global_stop()
            result = action()
        except Exception as exc:  # raw failures still carry lite bypass instruction
            failed_warning = active_router.raw_error_warning(operation, board_id, state=state)
            if state.tier is None:
                raise RuntimeError("raw route lost its resolved capability tier")
            payload: dict[str, object] = {
                "schema_version": 1,
                "status": "error",
                "code": _RAW_FAILURE_CODES[operation],
                "board_id": board_id,
                "tier": state.tier.value,
                "operation": operation,
                "raw": True,
                "message": str(exc),
            }
            if failed_warning is not None:
                payload["warning"] = failed_warning
            return json.dumps(payload, sort_keys=True)
        if state.tier is None:
            raise RuntimeError("raw route lost its resolved capability tier")
        payload = {
            "schema_version": 1,
            "status": "ok",
            "board_id": board_id,
            "tier": state.tier.value,
            "operation": operation,
            "raw": True,
            "result": result,
        }
        if warning is not None:
            payload["warning"] = warning
        return json.dumps(payload, sort_keys=True)

    def read_memory_raw(
        board_id: str,
        address: int | str,
        width: int = 32,
        length: int | None = None,
    ) -> str:
        """Read an explicit raw address without safety-map containment."""

        def action() -> object:
            parsed = _integer(address, "address")
            selected_width = _width(width)
            if parsed < 0:
                raise ValueError("address must be non-negative")
            if length is None:
                value = services.read_memory(services.handle_for(board_id), parsed, selected_width)
                return f"0x{value:0{selected_width // 4}X}"
            if isinstance(length, bool) or not isinstance(length, int) or not 1 <= length <= 4096:
                raise ValueError("length must be an integer from 1 through 4096")
            values = services.read_block(services.handle_for(board_id), parsed, length)
            return " ".join(f"{item:02X}" for item in values)

        return execute("read_memory_raw", board_id, action)

    def write_memory_raw(
        board_id: str, address: int | str, value: int | str, width: int = 32
    ) -> str:
        """Write one explicit raw 8/16/32-bit value without containment."""

        def action() -> object:
            parsed_address = _integer(address, "address")
            parsed_value = _integer(value, "value")
            selected_width = _width(width)
            if parsed_address < 0 or not 0 <= parsed_value < 1 << selected_width:
                raise ValueError("address/value is outside the selected transfer width")
            services.write_memory(
                services.handle_for(board_id), parsed_address, parsed_value, selected_width
            )
            return f"wrote 0x{parsed_value:X} at 0x{parsed_address:X}"

        return execute("write_memory_raw", board_id, action)

    def register_write_raw(
        board_id: str, address: int | str, value: int | str, width: int = 32
    ) -> str:
        """Write one raw memory-mapped register without read-modify-write semantics."""

        def action() -> object:
            parsed_address = _integer(address, "address")
            parsed_value = _integer(value, "value")
            selected_width = _width(width)
            if parsed_address < 0 or not 0 <= parsed_value < 1 << selected_width:
                raise ValueError("address/value is outside the selected transfer width")
            services.write_memory(
                services.handle_for(board_id), parsed_address, parsed_value, selected_width
            )
            return f"wrote 0x{parsed_value:X} register value at 0x{parsed_address:X}"

        return execute("register_write_raw", board_id, action)

    def write_cpu_register_raw(board_id: str, name: str, value: int | str) -> str:
        """Write one backend-supported CPU register without a plan or permission prompt."""

        return execute(
            "write_cpu_register_raw",
            board_id,
            lambda: _write_register(board_id, name, value),
        )

    def _write_register(board_id: str, name: str, value: int | str) -> str:
        parsed = _integer(value, "value")
        services.write_register(services.handle_for(board_id), name, parsed)
        return f"wrote 0x{parsed:X} to {name}"

    def set_execution_state_raw(board_id: str, name: str, value: int | str) -> str:
        """Set a raw backend-supported execution-state register."""

        return execute(
            "set_execution_state_raw",
            board_id,
            lambda: _write_register(board_id, name, value),
        )

    def set_breakpoint_raw(board_id: str, address: int | str) -> str:
        """Place a raw execution breakpoint at an explicit address."""

        def action() -> object:
            parsed = _integer(address, "address")
            if parsed < 0:
                raise ValueError("address must be non-negative")
            services.set_breakpoint(services.handle_for(board_id), parsed)
            return f"breakpoint set at 0x{parsed:X}"

        return execute("set_breakpoint_raw", board_id, action)

    def reset_and_halt_raw(board_id: str) -> str:
        """Reset and halt without a profile, map, plan, or identity gate."""

        return execute(
            "reset_and_halt_raw",
            board_id,
            lambda: _reset_and_text(board_id),
        )

    def _reset_and_text(board_id: str) -> str:
        services.reset(services.handle_for(board_id), True)
        return "reset and halted"

    def flash_raw(board_id: str, artifact: str) -> str:
        """Program an explicit artifact without safety-map containment."""

        def action() -> object:
            path = Path(artifact).expanduser().resolve(strict=True)
            if not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
                raise ValueError("artifact must be a regular local file no larger than 64 MiB")
            flashed, state = services.flash(services.handle_for(board_id), path)
            return {"artifact": str(flashed), "target_state": state}

        return execute("flash_raw", board_id, action)

    def read_serial_raw(
        board_id: str,
        port: str,
        baudrate: int,
        expected_text: str | None = None,
        read_seconds: float = 3.0,
        reset_on_open: bool = False,
    ) -> str:
        """Read a bounded explicit raw serial port without board-profile lookup."""

        def action() -> object:
            if isinstance(baudrate, bool) or not isinstance(baudrate, int) or baudrate <= 0:
                raise ValueError("baudrate must be a positive integer")
            if expected_text is not None:
                if not isinstance(expected_text, str):
                    raise ValueError("expected_text must be text or null")
                if len(expected_text.encode("utf-8")) > MAX_UART_TEXT_BYTES:
                    raise ValueError(
                        f"expected_text must not exceed {MAX_UART_TEXT_BYTES} UTF-8 bytes"
                    )
            bounded_read = _bounded_seconds(read_seconds, "read_seconds")
            capture = services.capture_uart(
                port,
                baudrate,
                bounded_read,
                expected_text,
                on_port_open=(lambda: services.reset(services.handle_for(board_id), False))
                if reset_on_open
                else None,
                max_bytes=MAX_UART_TEXT_BYTES,
            )
            return {
                "matched": capture.matched,
                "text": capture.text,
                "duration_seconds": capture.duration_seconds,
            }

        return execute("read_serial_raw", board_id, action)

    def write_serial_raw(
        board_id: str,
        port: str,
        baudrate: int,
        text: str,
        append_newline: bool = False,
        timeout_seconds: float = 1.0,
    ) -> str:
        """Write bounded UTF-8 text to an explicit raw serial port."""

        def action() -> object:
            if isinstance(baudrate, bool) or not isinstance(baudrate, int) or baudrate <= 0:
                raise ValueError("baudrate must be a positive integer")
            bounded_timeout = _bounded_seconds(timeout_seconds, "timeout_seconds")
            payload = (text + ("\n" if append_newline else "")).encode("utf-8")
            if len(payload) > MAX_UART_TEXT_BYTES:
                raise ValueError(f"text must not exceed {MAX_UART_TEXT_BYTES} UTF-8 bytes")
            result = services.write_uart(
                port,
                baudrate,
                payload,
                timeout_seconds=bounded_timeout,
                max_bytes=MAX_UART_TEXT_BYTES,
            )
            return {
                "bytes_written": result.bytes_written,
                "duration_seconds": result.duration_seconds,
            }

        return execute("write_serial_raw", board_id, action)

    def serial_exchange_raw(
        board_id: str,
        port: str,
        baudrate: int,
        steps: list[dict[str, object]],
        read_seconds: float = 3.0,
        ready_text: str | None = None,
        ready_seconds: float = 0.0,
        ready_probe_text: str | None = None,
        ready_probe_line_ending: str = "none",
        ready_probe_delay_seconds: float = 0.0,
        clear_input: bool = False,
    ) -> str:
        """Run a bounded explicit raw serial exchange without a profile or map."""

        def action() -> object:
            parameters = {
                "steps": steps,
                "read_seconds": read_seconds,
                "baudrate": baudrate,
                "port": port,
                "ready_text": ready_text,
                "ready_seconds": ready_seconds,
                "ready_probe_text": ready_probe_text,
                "ready_probe_line_ending": ready_probe_line_ending,
                "ready_probe_delay_seconds": ready_probe_delay_seconds,
                "clear_input": clear_input,
            }
            schema_error = validate_serial_exchange_parameters(
                parameters,
                max_seconds=MAX_UART_SECONDS,
                max_steps=16,
                max_text_bytes=MAX_UART_TEXT_BYTES,
            )
            if schema_error is not None:
                raise ValueError(schema_error)
            endings = {"none": "", "lf": "\n", "cr": "\r", "crlf": "\r\n"}
            validated_steps: list[tuple[bytes, str]] = []
            for row in steps:
                text = row["text"]
                expected = row["expected_text"]
                ending = row["line_ending"]
                assert (
                    isinstance(text, str) and isinstance(expected, str) and isinstance(ending, str)
                )
                validated_steps.append(((text + endings[ending]).encode("utf-8"), expected))
            first_payload, first_expected = validated_steps[0]
            exchange = services.exchange_uart(
                port,
                baudrate,
                first_payload,
                first_expected,
                read_seconds,
                ready_text=ready_text,
                ready_seconds=ready_seconds,
                ready_probe=(
                    (ready_probe_text + endings[ready_probe_line_ending]).encode("utf-8")
                    if ready_probe_text is not None
                    else None
                ),
                ready_probe_delay_seconds=ready_probe_delay_seconds,
                followup_steps=tuple(validated_steps[1:]),
                clear_input=clear_input,
                max_bytes=MAX_UART_TEXT_BYTES,
                max_steps=16,
                max_input_bytes=MAX_UART_TEXT_BYTES,
                max_read_seconds=MAX_UART_SECONDS,
            )
            return {
                "matched": exchange.matched,
                "text": exchange.text,
                "bytes_written": exchange.bytes_written,
                "steps": [
                    {
                        "expected_text": step.expected_text,
                        "text": step.text,
                        "matched": step.matched,
                        "bytes_written": step.bytes_written,
                    }
                    for step in exchange.steps
                ],
            }

        return execute("serial_exchange_raw", board_id, action)

    def target_unlock_raw(board_id: str, recovery_mechanism: str) -> str:
        """Invoke one backend-provided raw recovery mechanism without safe-map authority."""

        def action() -> str:
            # Recovery can reset or erase a target even when the backend raises.
            # The old connection/gate/assignment is therefore unusable in both
            # outcomes; unlike the safe route this deliberately has no manual
            # permission requirement.
            backend_error: BaseException | None = None
            try:
                try:
                    return services.recover(services.handle_for(board_id), recovery_mechanism)
                except BaseException as exc:
                    backend_error = exc
                    raise
            finally:
                if services.finalize_recovery is not None:
                    try:
                        services.finalize_recovery(board_id)
                    except Exception as cleanup_error:
                        if backend_error is not None:
                            backend_error.add_note(
                                f"raw recovery cleanup also failed: {cleanup_error}"
                            )
                        else:
                            raise

        return execute(
            "target_unlock_raw",
            board_id,
            action,
        )

    return {
        "read_memory_raw": read_memory_raw,
        "write_memory_raw": write_memory_raw,
        "register_write_raw": register_write_raw,
        "write_cpu_register_raw": write_cpu_register_raw,
        "set_execution_state_raw": set_execution_state_raw,
        "set_breakpoint_raw": set_breakpoint_raw,
        "reset_and_halt_raw": reset_and_halt_raw,
        "flash_raw": flash_raw,
        "read_serial_raw": read_serial_raw,
        "write_serial_raw": write_serial_raw,
        "serial_exchange_raw": serial_exchange_raw,
        "target_unlock_raw": target_unlock_raw,
    }
