"""Revised symbol-first Layer-2 memory actions."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyocd_debug_mcp.kernel.operations import wrap_layer2_response
from pyocd_debug_mcp.services.session_runtime import PolicyRefusal, SessionRecord, ToolOutcome
from pyocd_debug_mcp.services.symbols import ResolvedSymbol

MAX_ADDRESS_READ_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class MemoryToolServices:
    runtime_for: Callable[[str], SessionRecord | None]
    active_session_id: Callable[[str], str | None]
    duration_ms: Callable[[float], int]
    record_event: Callable[..., object]
    format_refusal: Callable[..., str]
    handle_for: Callable[[str], Any]
    symbol_artifact_for: Callable[[Any], Path]
    find_symbols: Callable[[Path, str], tuple[ResolvedSymbol, ...]]
    resolve_symbol: Callable[[Path, str], ResolvedSymbol]
    read_target_memory: Callable[[Any, int, int], int]
    read_target_block: Callable[[Any, int, int], list[int]]
    write_target_memory: Callable[[Any, int, int, int], None]
    check_memory_read: Callable[[str, int, int], None]
    check_memory_write: Callable[[str, int, int], None] | None = None


def _parse_integer(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean is not a memory integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 0)
    raise ValueError("value must be a hexadecimal/decimal string or integer")


def _record_refusal(
    services: MemoryToolServices,
    tool_name: str,
    board_id: str,
    normalized_args: Mapping[str, object],
    refusal: PolicyRefusal,
    started: float,
    runtime: SessionRecord | None,
) -> str:
    services.record_event(
        tool_name,
        normalized_args,
        outcome_kind=ToolOutcome.REFUSED,
        error_code=refusal.code,
        duration_ms=services.duration_ms(started),
        details={"message": refusal.message},
        board_id=board_id,
        session=runtime,
    )
    return wrap_layer2_response(
        services.format_refusal(
            refusal,
            session_id=services.active_session_id(board_id),
        )
    )


def _record_success(
    services: MemoryToolServices,
    tool_name: str,
    board_id: str,
    normalized_args: Mapping[str, object],
    result: str,
    started: float,
    runtime: SessionRecord | None,
) -> str:
    services.record_event(
        tool_name,
        normalized_args,
        outcome_kind=ToolOutcome.SUCCESS,
        error_code=None,
        duration_ms=services.duration_ms(started),
        board_id=board_id,
        session=runtime,
    )
    return wrap_layer2_response(result)


def _valid_width(width: int) -> bool:
    return not isinstance(width, bool) and width in {8, 16, 32}


def build_memory_handlers(
    services: MemoryToolServices,
) -> dict[str, Callable[..., str]]:
    """Build the final M5 memory surface."""

    def find_symbol(board_id: str, query: str) -> str:
        """Search the current firmware ELF for a bounded list of matching symbols."""

        started = time.monotonic()
        runtime = services.runtime_for(board_id)
        args = {"board_id": board_id, "query": query}
        if not query.strip():
            return _record_refusal(
                services,
                "find_symbol",
                board_id,
                args,
                PolicyRefusal("memory/empty-symbol-query", "query must not be empty."),
                started,
                runtime,
            )
        handle = services.handle_for(board_id)
        artifact = services.symbol_artifact_for(handle)
        matches = services.find_symbols(artifact, query)
        if not matches:
            result = f"No symbols matching '{query}' were found in {artifact}."
        else:
            rendered = ", ".join(
                f"{item.name}@0x{item.address:08X} size={item.size} type={item.type}"
                for item in matches
            )
            result = f"Symbols matching '{query}' in {artifact}: {rendered}"
        return _record_success(services, "find_symbol", board_id, args, result, started, runtime)

    def read_memory_symbol(board_id: str, symbol: str, width: int = 32) -> str:
        """Resolve and read a mapped, non-prohibited symbol; prefer it to raw access."""

        started = time.monotonic()
        runtime = services.runtime_for(board_id)
        args = {"board_id": board_id, "symbol": symbol, "width": width}
        if not _valid_width(width):
            return _record_refusal(
                services,
                "read_memory_symbol",
                board_id,
                args,
                PolicyRefusal("memory/invalid-width", "width must be one of: 8, 16, 32."),
                started,
                runtime,
            )
        if not symbol.strip():
            return _record_refusal(
                services,
                "read_memory_symbol",
                board_id,
                args,
                PolicyRefusal("memory/empty-symbol", "symbol must not be empty."),
                started,
                runtime,
            )
        handle = services.handle_for(board_id)
        artifact = services.symbol_artifact_for(handle)
        resolved = services.resolve_symbol(artifact, symbol)
        services.check_memory_read(board_id, resolved.address, width // 8)
        value = services.read_target_memory(handle, resolved.address, width)
        result = (
            f"Symbol {resolved.name} from {artifact} @0x{resolved.address:08X} "
            f"size={resolved.size} type={resolved.type} value=0x{value:0{width // 4}X}"
        )
        return _record_success(
            services, "read_memory_symbol", board_id, args, result, started, runtime
        )

    def read_memory_address(
        board_id: str,
        address: str | int,
        width: int = 32,
        length: int | None = None,
    ) -> str:
        """Read a mapped, non-prohibited value or bounded block under an address plan."""

        started = time.monotonic()
        runtime = services.runtime_for(board_id)
        args = {
            "board_id": board_id,
            "address": address,
            "width": width,
            "length": length,
        }
        if not _valid_width(width):
            return _record_refusal(
                services,
                "read_memory_address",
                board_id,
                args,
                PolicyRefusal("memory/invalid-width", "width must be one of: 8, 16, 32."),
                started,
                runtime,
            )
        if length is not None and (
            isinstance(length, bool) or length < 1 or length > MAX_ADDRESS_READ_BYTES
        ):
            return _record_refusal(
                services,
                "read_memory_address",
                board_id,
                args,
                PolicyRefusal(
                    "memory/invalid-length",
                    f"length must be between 1 and {MAX_ADDRESS_READ_BYTES} bytes.",
                ),
                started,
                runtime,
            )
        try:
            parsed_address = _parse_integer(address)
        except (TypeError, ValueError) as exc:
            return _record_refusal(
                services,
                "read_memory_address",
                board_id,
                args,
                PolicyRefusal("memory/invalid-address", str(exc)),
                started,
                runtime,
            )
        if parsed_address < 0:
            return _record_refusal(
                services,
                "read_memory_address",
                board_id,
                args,
                PolicyRefusal("memory/invalid-address", "address must be non-negative."),
                started,
                runtime,
            )
        size_bytes = width // 8 if length is None else length
        services.check_memory_read(board_id, parsed_address, size_bytes)
        handle = services.handle_for(board_id)
        if length is None:
            value = services.read_target_memory(handle, parsed_address, width)
            result = f"0x{value:0{width // 4}X}"
        else:
            values = services.read_target_block(handle, parsed_address, length)
            result = " ".join(f"{byte:02X}" for byte in values)
        return _record_success(
            services, "read_memory_address", board_id, args, result, started, runtime
        )

    def write_memory(
        board_id: str,
        symbol_or_address: str | int,
        value: object,
        width: int = 32,
        allow_address_fallback: bool = False,
        reason: str | None = None,
    ) -> str:
        """Prefer symbol writes; raw addresses require explicit fallback and a concrete reason."""

        started = time.monotonic()
        runtime = services.runtime_for(board_id)
        args = {
            "board_id": board_id,
            "symbol_or_address": symbol_or_address,
            "value": value,
            "width": width,
            "allow_address_fallback": allow_address_fallback,
            "reason": reason,
        }
        if not _valid_width(width):
            return _record_refusal(
                services,
                "write_memory",
                board_id,
                args,
                PolicyRefusal("memory/invalid-width", "width must be one of: 8, 16, 32."),
                started,
                runtime,
            )
        handle = services.handle_for(board_id)
        try:
            address = _parse_integer(symbol_or_address)
            is_address = True
        except (TypeError, ValueError):
            is_address = False
            if not isinstance(symbol_or_address, str) or not symbol_or_address.strip():
                return _record_refusal(
                    services,
                    "write_memory",
                    board_id,
                    args,
                    PolicyRefusal("memory/empty-symbol", "symbol must not be empty."),
                    started,
                    runtime,
                )
            artifact = services.symbol_artifact_for(handle)
            resolved = services.resolve_symbol(artifact, symbol_or_address)
            address = resolved.address
        if is_address and not allow_address_fallback:
            return _record_refusal(
                services,
                "write_memory",
                board_id,
                args,
                PolicyRefusal(
                    "memory/symbol-first-required",
                    "Try a symbol first. Provide a symbol name or explicitly request address "
                    "fallback.",
                ),
                started,
                runtime,
            )
        if is_address and (reason is None or not reason.strip()):
            return _record_refusal(
                services,
                "write_memory",
                board_id,
                args,
                PolicyRefusal(
                    "memory/address-fallback-reason-required",
                    "Raw-address fallback requires a brief concrete reason symbols are unsuitable.",
                ),
                started,
                runtime,
            )
        try:
            parsed_value = _parse_integer(value)
        except (TypeError, ValueError) as exc:
            return _record_refusal(
                services,
                "write_memory",
                board_id,
                args,
                PolicyRefusal("memory/invalid-value", str(exc)),
                started,
                runtime,
            )
        if address < 0 or parsed_value < 0 or parsed_value >= 1 << width:
            return _record_refusal(
                services,
                "write_memory",
                board_id,
                args,
                PolicyRefusal(
                    "memory/value-out-of-range",
                    f"address must be non-negative and value must fit in {width} bits.",
                ),
                started,
                runtime,
            )
        if services.check_memory_write is not None:
            services.check_memory_write(board_id, address, width)
        services.write_target_memory(handle, address, parsed_value, width)
        target = f"0x{address:08X}" if is_address else str(symbol_or_address)
        result = f"Wrote 0x{parsed_value:X} to mapped RAM at {target}."
        return _record_success(services, "write_memory", board_id, args, result, started, runtime)

    return {
        "find_symbol": find_symbol,
        "read_memory_symbol": read_memory_symbol,
        "read_memory_address": read_memory_address,
        "write_memory": write_memory,
    }
