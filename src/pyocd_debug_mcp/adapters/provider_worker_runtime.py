"""Child-only JSON RPC loop that owns one native pyOCD session."""

from __future__ import annotations

import json
import math
import sys
from dataclasses import fields
from pathlib import Path
from typing import Any, BinaryIO, cast

from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.adapters.swd_pyocd import (
    PyOCDSWDInterface,
)
from pyocd_debug_mcp.board_config import BoardConfig
from pyocd_debug_mcp.probe_inventory import probe_family_from_pyocd_probe
from pyocd_debug_mcp.target_errors import (
    LockedTargetError,
    ProbeNotFoundError,
    ResetLineUnavailableError,
    TargetConnectionError,
    TargetControlError,
    TargetStateError,
    UnsupportedArtifactError,
)
from pyocd_debug_mcp.timeouts import ServerTimeoutConfig

_VERSION = 1
_BOARD_FIELDS = {field.name for field in fields(BoardConfig)}
_TIMEOUT_FIELDS = set(ServerTimeoutConfig().to_record())
_ERROR_KINDS: tuple[tuple[type[TargetControlError], str], ...] = (
    (LockedTargetError, "locked_target"),
    (ProbeNotFoundError, "probe_not_found"),
    (ResetLineUnavailableError, "reset_line_unavailable"),
    (TargetStateError, "target_state"),
    (UnsupportedArtifactError, "unsupported_artifact"),
    (TargetConnectionError, "target_connection"),
    (TargetControlError, "target_control"),
)


def _integer(value: object, name: str, *, minimum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} is below its allowed range")
    return value


def _text(value: object, name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _exact(arguments: dict[str, Any], expected: set[str]) -> None:
    if set(arguments) != expected:
        raise ValueError("worker operation has missing or unknown arguments")


def _validate_arguments(operation: str, arguments: dict[str, Any]) -> None:
    no_arguments = {"close", "get_state", "supported_core_registers", "halt", "resume", "step", "reset", "reset_and_halt", "recover", "release_reset"}
    if operation in no_arguments:
        _exact(arguments, set())
        return
    if operation in {"open", "connect_under_reset"}:
        expected = {"board", "unique_id", "target", "server_timeouts", "protocol", "connect_mode", "pack_path", "pack_sha256", "pdsc_device", "frequency_hz"}
        _exact(arguments, expected)
        if arguments["board"] is not None and not isinstance(arguments["board"], dict):
            raise ValueError("board must be an object or null")
        for name in ("unique_id", "target", "protocol", "connect_mode", "pack_path", "pack_sha256", "pdsc_device"):
            _text(arguments[name], name, nullable=True)
        if arguments["frequency_hz"] is not None:
            _integer(arguments["frequency_hz"], "frequency_hz", minimum=1)
        if not isinstance(arguments["server_timeouts"], dict):
            raise ValueError("server_timeouts must be an object")
        return
    if operation == "read_memory":
        _exact(arguments, {"address", "width_bits"})
        _integer(arguments["address"], "address", minimum=0)
        if _integer(arguments["width_bits"], "width_bits") not in {8, 16, 32}:
            raise ValueError("width_bits is unsupported")
        return
    if operation == "read_memory_block":
        _exact(arguments, {"address", "length"})
        _integer(arguments["address"], "address", minimum=0)
        _integer(arguments["length"], "length", minimum=1)
        return
    if operation == "write_memory":
        _exact(arguments, {"address", "value", "width_bits"})
        _integer(arguments["address"], "address", minimum=0)
        value = _integer(arguments["value"], "value", minimum=0)
        width_bits = _integer(arguments["width_bits"], "width_bits")
        if width_bits not in {8, 16, 32}:
            raise ValueError("width_bits is unsupported")
        if value > (1 << width_bits) - 1:
            raise ValueError("value exceeded requested memory width")
        return
    if operation in {"read_core_register", "supports_recovery"}:
        field = "name" if operation == "read_core_register" else "mechanism"
        _exact(arguments, {field})
        _text(arguments[field], field)
        return
    if operation == "write_core_register":
        _exact(arguments, {"name", "value"})
        _text(arguments["name"], "name")
        _integer(arguments["value"], "value", minimum=0)
        return
    if operation in {"set_breakpoint", "remove_breakpoint"}:
        _exact(arguments, {"address"})
        _integer(arguments["address"], "address", minimum=0)
        return
    if operation == "flash":
        _exact(arguments, {"path", "halt_after_reset"})
        _text(arguments["path"], "path")
        if not isinstance(arguments["halt_after_reset"], bool):
            raise ValueError("halt_after_reset must be boolean")
        return
    raise ValueError("unknown worker operation")


def _send(protocol: BinaryIO, payload: dict[str, Any]) -> None:
    encoded = (json.dumps(payload, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    protocol.write(encoded)
    protocol.flush()


def _error_kind(error: Exception) -> str:
    for error_type, kind in _ERROR_KINDS:
        if isinstance(error, error_type):
            return kind
    return "provider_failure"


def _board(raw: object) -> BoardConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("board must be an object or null")
    record = dict(raw)
    if set(record) != _BOARD_FIELDS:
        raise ValueError("board record has missing or unknown fields")
    required_text = {
        "board_id", "display_name", "mcu_family", "probe_family", "pyocd_target", "probe_type",
        "silicon_id_label", "uart_note",
    }
    nullable_text = {"recover_mode", "debug_protocol", "debug_connect_mode", "expected_uart_substring"}
    for key in required_text:
        _text(record[key], key)
    for key in nullable_text:
        _text(record[key], key, nullable=True)
    for key in {"test_addr", "silicon_id_addr", "silicon_id_expected", "silicon_id_mask", "debug_clock_hz"}:
        if record[key] is not None:
            _integer(record[key], key)
    for key in {"silicon_id_width_bits", "default_baudrate"}:
        _integer(record[key], key, minimum=0)
    for key in {"requires_recover_validation"}:
        if not isinstance(record[key], bool):
            raise ValueError(f"{key} must be boolean")
    for key in {"probe_hint_terms", "serial_hint_terms"}:
        value = record[key]
        if not isinstance(value, list):
            raise ValueError(f"{key} must be a string list")
        for item in value:
            _text(item, key)
    if record.get("source_path") is not None:
        record["source_path"] = Path(str(_text(record["source_path"], "source_path")))
    for key in ("probe_hint_terms", "serial_hint_terms"):
        if isinstance(record.get(key), list):
            record[key] = tuple(str(item) for item in record[key])
    return BoardConfig(**record)


def _timeouts(raw: object) -> ServerTimeoutConfig:
    if not isinstance(raw, dict):
        raise ValueError("server_timeouts must be an object")
    if set(raw) != _TIMEOUT_FIELDS:
        raise ValueError("server_timeouts has missing or unknown fields")
    values: dict[str, float] = {}
    for key, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"{key} must be a finite positive number")
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            raise ValueError(f"{key} must be a finite positive number")
        values[key] = number
    return ServerTimeoutConfig(**values)


def _metadata(handle: TargetSessionHandle) -> dict[str, Any]:
    session = handle.session
    probe = getattr(session, "probe", None)
    board = getattr(session, "board", None)
    target = getattr(session, "target", None)
    probe_uid = handle.probe_uid or getattr(probe, "unique_id", None)
    part_number = getattr(target, "part_number", None)
    metadata = handle.metadata
    if metadata is None:  # TargetSessionHandle guarantees this, retain an honest failure.
        raise RuntimeError("native session did not provide runtime metadata")
    return {
        "probe_uid": str(probe_uid) if probe_uid else None,
        "route_used": str(handle.route_used),
        "target_override": handle.target_override,
        "board_name": str(getattr(board, "name", "") or metadata.board_name),
        "probe_description": str(getattr(probe, "description", "") or ""),
        "probe_family": probe_family_from_pyocd_probe(probe),
        "live_part_number": str(part_number) if part_number else None,
        "runtime_token": metadata.runtime_token,
    }


def _dispatch(adapter: PyOCDSWDInterface, handle: TargetSessionHandle | None, operation: str,
              arguments: dict[str, Any]) -> tuple[TargetSessionHandle | None, Any, bool]:
    if operation in {"open", "connect_under_reset"}:
        if handle is not None:
            raise RuntimeError("worker already has a live session")
        kwargs = {
            "board": _board(arguments.get("board")),
            "unique_id": arguments.get("unique_id"),
            "target": arguments.get("target"),
            "server_timeouts": _timeouts(arguments.get("server_timeouts")),
            "pack_path": Path(arguments["pack_path"]) if arguments.get("pack_path") else None,
            "pack_sha256": arguments.get("pack_sha256"),
            "pdsc_device": arguments.get("pdsc_device"),
        }
        if operation == "open":
            handle = adapter.open(
                **kwargs,
                protocol=arguments.get("protocol"),
                connect_mode=arguments.get("connect_mode"),
                frequency_hz=arguments.get("frequency_hz"),
            )
        else:
            handle = adapter.connect_under_reset(**kwargs)
        return handle, _metadata(handle), False
    if operation == "close":
        if handle is not None:
            adapter.close(handle)
        return None, None, True
    if handle is None:
        raise RuntimeError("worker has no live target session")
    if operation == "get_state":
        result = adapter.get_state(handle)
    elif operation == "read_memory":
        result = adapter.read_memory(handle, int(arguments["address"]), int(arguments["width_bits"]))
        width_bits = int(arguments["width_bits"])
        if not isinstance(result, int) or isinstance(result, bool) or not 0 <= result <= (1 << width_bits) - 1:
            raise RuntimeError("memory read result exceeded requested width")
    elif operation == "read_memory_block":
        requested_length = int(arguments["length"])
        result = adapter.read_memory_block(handle, int(arguments["address"]), requested_length)
        if len(result) != requested_length or any(
            not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 255
            for value in result
        ):
            raise RuntimeError("memory block result did not exactly match the requested byte block")
    elif operation == "write_memory":
        width_bits = int(arguments["width_bits"])
        if int(arguments["value"]) > (1 << width_bits) - 1:
            raise ValueError("value exceeded requested memory width")
        result = adapter.write_memory(
            handle, int(arguments["address"]), int(arguments["value"]), int(arguments["width_bits"])
        )
    elif operation == "read_core_register":
        result = adapter.read_core_register(handle, str(arguments["name"]))
    elif operation == "write_core_register":
        result = adapter.write_core_register(handle, str(arguments["name"]), int(arguments["value"]))
    elif operation == "supported_core_registers":
        result = list(adapter.supported_core_registers(handle))
    elif operation == "halt":
        result = adapter.halt(handle)
    elif operation == "resume":
        result = adapter.resume(handle)
    elif operation == "step":
        result = adapter.step(handle)
    elif operation == "reset":
        result = adapter.reset(handle)
    elif operation == "reset_and_halt":
        result = adapter.reset_and_halt(handle)
    elif operation == "flash":
        result = adapter.flash(
            handle, Path(str(arguments["path"])), halt_after_reset=bool(arguments["halt_after_reset"])
        )
    elif operation == "recover":
        result = adapter.recover(handle)
    elif operation == "supports_recovery":
        result = adapter.supports_recovery(handle, str(arguments["mechanism"]))
    elif operation == "set_breakpoint":
        result = adapter.set_breakpoint(handle, int(arguments["address"]))
    elif operation == "remove_breakpoint":
        result = adapter.remove_breakpoint(handle, int(arguments["address"]))
    elif operation == "release_reset":
        result = adapter.release_reset(handle)
    else:
        raise ValueError(f"unknown worker operation: {operation}")
    return handle, result, False


def main(protocol: BinaryIO) -> None:
    adapter = PyOCDSWDInterface()
    handle: TargetSessionHandle | None = None
    last_request_id = 0
    _send(protocol, {"version": _VERSION, "ready": True})
    pending = bytearray()
    while True:
        chunk = cast(Any, sys.stdin.buffer).read1(4096)
        if not chunk:
            return
        pending.extend(chunk)
        while b"\n" in pending:
            raw, _, remainder = pending.partition(b"\n")
            pending = bytearray(remainder)
            request_id: object = None
            try:
                request = json.loads(raw.decode("utf-8"))
                if (
                    not isinstance(request, dict)
                    or set(request) != {"version", "request_id", "operation", "arguments"}
                    or request.get("version") != _VERSION
                ):
                    raise ValueError("unsupported worker protocol frame")
                request_id = request.get("request_id")
                operation = request.get("operation")
                arguments = request.get("arguments")
                if not isinstance(request_id, int) or isinstance(request_id, bool) or request_id < 1:
                    raise ValueError("invalid worker request id")
                if request_id != last_request_id + 1:
                    raise ValueError("worker request id was not the next monotonic id")
                if not isinstance(operation, str) or not isinstance(arguments, dict):
                    raise ValueError("invalid worker request schema")
                _validate_arguments(operation, arguments)
                last_request_id = request_id
                handle, result, exit_after = _dispatch(
                    adapter,
                    handle,
                    operation,
                    arguments,
                )
                _send(
                    protocol,
                    {"version": _VERSION, "request_id": request_id, "ok": True, "result": result},
                )
                if exit_after:
                    return
            except Exception as exc:  # parent maps all child failures to typed connection failure
                kind = _error_kind(exc)
                _send(
                    protocol,
                    {
                        "version": _VERSION,
                        "request_id": request_id,
                        "ok": False,
                        "error": {"kind": kind, "message": str(exc)},
                    }
                )
