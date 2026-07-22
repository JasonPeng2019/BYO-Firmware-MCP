"""Revised symbol-first Layer-2 memory actions."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from elftools.common.exceptions import ELFError

from pyocd_debug_mcp.kernel.operations import wrap_layer2_response
from pyocd_debug_mcp.services.session_runtime import PolicyRefusal, SessionRecord, ToolOutcome
from pyocd_debug_mcp.services.symbols import ResolvedSymbol, is_elf_artifact
from pyocd_debug_mcp.target_errors import ReferenceArtifactError, SymbolLookupError

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
    prepared_symbol_for: Callable[[str, str, str | None], ResolvedSymbol | None] | None = None


@dataclass(frozen=True, slots=True)
class SymbolArtifactSelection:
    """One immutable view of the ELF used for a symbol operation."""

    path: Path
    digest: str
    explicit: bool


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


def _hash_artifact(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _select_symbol_artifact(
    services: MemoryToolServices,
    handle: Any,
    elf_artifact: str | None,
) -> tuple[SymbolArtifactSelection | None, PolicyRefusal | None]:
    try:
        if elf_artifact is None:
            path = services.symbol_artifact_for(handle)
            explicit = False
        else:
            if not elf_artifact.strip():
                raise ValueError("elf_artifact must be a non-empty path when supplied")
            path = Path(elf_artifact).expanduser().resolve(strict=True)
            explicit = True
        path = path.expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError(f"The selected symbol artifact is not a regular file: {path}")
        if not is_elf_artifact(path):
            raise ValueError(f"The selected symbol artifact is not an ELF image: {path}")
        return SymbolArtifactSelection(path, _hash_artifact(path), explicit), None
    except (OSError, RuntimeError, ReferenceArtifactError) as exc:
        return None, PolicyRefusal(
            "memory/symbol-artifact-unavailable",
            "The selected firmware ELF is unavailable or changed: "
            f"{exc}. Pass the current project's local ELF/AXF path as elf_artifact, or flash that "
            "ELF in this Server Run to create a temporary convenience binding.",
        )
    except ValueError as exc:
        return None, PolicyRefusal("memory/symbol-artifact-unavailable", str(exc))


def _reverify_symbol_artifact(
    services: MemoryToolServices,
    handle: Any,
    selection: SymbolArtifactSelection,
) -> PolicyRefusal | None:
    try:
        if _hash_artifact(selection.path) != selection.digest:
            raise ValueError("The selected firmware ELF changed during symbol resolution")
        if not selection.explicit:
            current = services.symbol_artifact_for(handle).expanduser().resolve(strict=True)
            if current != selection.path:
                raise ValueError(
                    "The current firmware ELF association changed during symbol resolution"
                )
    except (OSError, RuntimeError, ReferenceArtifactError, ValueError) as exc:
        return PolicyRefusal(
            "memory/symbol-artifact-unavailable",
            f"{exc}. Retry with the current project ELF supplied as elf_artifact.",
        )
    return None


def _symbol_parse_refusal(exc: Exception) -> PolicyRefusal:
    return PolicyRefusal(
        "memory/symbol-artifact-unavailable",
        "The selected firmware ELF could not be parsed safely: "
        f"{exc}. Rebuild it or pass the correct current project .elf as elf_artifact.",
    )


def _symbol_lookup_refusal(exc: SymbolLookupError) -> PolicyRefusal:
    return PolicyRefusal("memory/symbol-not-found", str(exc))


def symbol_scalar_refusal(resolved: ResolvedSymbol, width: int) -> PolicyRefusal | None:
    """Return a pre-I/O refusal for a non-data or misaligned scalar symbol."""

    if resolved.type.upper() == "STT_FUNC":
        return PolicyRefusal(
            "memory/symbol-is-function",
            f"Symbol '{resolved.name}' is an executable function, not a data object. "
            "Use find_symbol and the breakpoint tools; use the planned mapped-address read only "
            "when inspecting code bytes is intentional.",
        )
    alignment = width // 8
    if resolved.address % alignment:
        return PolicyRefusal(
            "memory/symbol-address-unaligned",
            f"Symbol '{resolved.name}' at 0x{resolved.address:08X} is not aligned for a "
            f"{width}-bit scalar access. Choose a compatible width.",
        )
    return None


def _lookup_or_artifact_refusal(
    services: MemoryToolServices,
    handle: Any,
    selection: SymbolArtifactSelection,
    exc: SymbolLookupError,
) -> PolicyRefusal:
    return _reverify_symbol_artifact(services, handle, selection) or _symbol_lookup_refusal(exc)


def build_memory_handlers(
    services: MemoryToolServices,
) -> dict[str, Callable[..., str]]:
    """Build the memory tool surface."""

    def find_symbol(board_id: str, query: str, elf_artifact: str | None = None) -> str:
        """Search an ELF for symbols; pass elf_artifact after restart or before same-run flash."""

        started = time.monotonic()
        runtime = services.runtime_for(board_id)
        args = {"board_id": board_id, "query": query, "elf_artifact": elf_artifact}
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
        selection, refusal = _select_symbol_artifact(services, handle, elf_artifact)
        if selection is None:
            assert refusal is not None
            return _record_refusal(
                services, "find_symbol", board_id, args, refusal, started, runtime
            )
        try:
            matches = services.find_symbols(selection.path, query)
        except SymbolLookupError as exc:
            return _record_refusal(
                services,
                "find_symbol",
                board_id,
                args,
                _lookup_or_artifact_refusal(services, handle, selection, exc),
                started,
                runtime,
            )
        except (ELFError, OSError, RuntimeError, ValueError) as exc:
            return _record_refusal(
                services,
                "find_symbol",
                board_id,
                args,
                _symbol_parse_refusal(exc),
                started,
                runtime,
            )
        refusal = _reverify_symbol_artifact(services, handle, selection)
        if refusal is not None:
            return _record_refusal(
                services, "find_symbol", board_id, args, refusal, started, runtime
            )
        if not matches:
            result = f"No symbols matching '{query}' were found in {selection.path}."
        else:
            rendered = ", ".join(
                f"{item.name}@0x{item.address:08X} size={item.size} type={item.type}"
                for item in matches
            )
            result = f"Symbols matching '{query}' in {selection.path}: {rendered}"
        return _record_success(services, "find_symbol", board_id, args, result, started, runtime)

    def read_memory_symbol(
        board_id: str,
        symbol: str,
        width: int = 32,
        elf_artifact: str | None = None,
    ) -> str:
        """Read a mapped symbol; pass its project ELF after restart or before same-run flash."""

        started = time.monotonic()
        runtime = services.runtime_for(board_id)
        args = {
            "board_id": board_id,
            "symbol": symbol,
            "width": width,
            "elf_artifact": elf_artifact,
        }
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
        selection, refusal = _select_symbol_artifact(services, handle, elf_artifact)
        if selection is None:
            assert refusal is not None
            return _record_refusal(
                services, "read_memory_symbol", board_id, args, refusal, started, runtime
            )
        try:
            resolved = services.resolve_symbol(selection.path, symbol)
        except SymbolLookupError as exc:
            return _record_refusal(
                services,
                "read_memory_symbol",
                board_id,
                args,
                _lookup_or_artifact_refusal(services, handle, selection, exc),
                started,
                runtime,
            )
        except (ELFError, OSError, RuntimeError, ValueError) as exc:
            return _record_refusal(
                services,
                "read_memory_symbol",
                board_id,
                args,
                _symbol_parse_refusal(exc),
                started,
                runtime,
            )
        refusal = _reverify_symbol_artifact(services, handle, selection)
        if refusal is not None:
            return _record_refusal(
                services, "read_memory_symbol", board_id, args, refusal, started, runtime
            )
        refusal = symbol_scalar_refusal(resolved, width)
        if refusal is not None:
            return _record_refusal(
                services, "read_memory_symbol", board_id, args, refusal, started, runtime
            )
        requested_bytes = width // 8
        if resolved.size <= 0:
            return _record_refusal(
                services,
                "read_memory_symbol",
                board_id,
                args,
                PolicyRefusal(
                    "memory/symbol-size-unknown",
                    "The ELF does not describe this symbol as a sized variable; use a real "
                    "variable symbol or the planned raw-address path with an explicit width.",
                ),
                started,
                runtime,
            )
        if requested_bytes > resolved.size:
            return _record_refusal(
                services,
                "read_memory_symbol",
                board_id,
                args,
                PolicyRefusal(
                    "memory/symbol-width-exceeds-object",
                    f"The {width}-bit read exceeds the {resolved.size}-byte symbol.",
                ),
                started,
                runtime,
            )
        services.check_memory_read(board_id, resolved.address, requested_bytes)
        value = services.read_target_memory(handle, resolved.address, width)
        result = (
            f"Symbol {resolved.name} from {selection.path} @0x{resolved.address:08X} "
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
        if length is not None and (isinstance(length, bool) or length < 1):
            return _record_refusal(
                services,
                "read_memory_address",
                board_id,
                args,
                PolicyRefusal(
                    "memory/invalid-length",
                    "length must be a positive integer.",
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
        elf_artifact: str | None = None,
    ) -> str:
        """Write a symbol from elf_artifact, or a justified mapped raw address without an ELF."""

        started = time.monotonic()
        runtime = services.runtime_for(board_id)
        args = {
            "board_id": board_id,
            "symbol_or_address": symbol_or_address,
            "value": value,
            "width": width,
            "allow_address_fallback": allow_address_fallback,
            "reason": reason,
            "elf_artifact": elf_artifact,
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
        resolved = None
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
            prepared = (
                services.prepared_symbol_for(board_id, symbol_or_address, elf_artifact)
                if services.prepared_symbol_for is not None
                else None
            )
            if prepared is not None:
                resolved = prepared
            else:
                selection, refusal = _select_symbol_artifact(services, handle, elf_artifact)
                if selection is None:
                    assert refusal is not None
                    return _record_refusal(
                        services, "write_memory", board_id, args, refusal, started, runtime
                    )
                try:
                    resolved = services.resolve_symbol(selection.path, symbol_or_address)
                except SymbolLookupError as exc:
                    return _record_refusal(
                        services,
                        "write_memory",
                        board_id,
                        args,
                        _lookup_or_artifact_refusal(services, handle, selection, exc),
                        started,
                        runtime,
                    )
                except (ELFError, OSError, RuntimeError, ValueError) as exc:
                    return _record_refusal(
                        services,
                        "write_memory",
                        board_id,
                        args,
                        _symbol_parse_refusal(exc),
                        started,
                        runtime,
                    )
                refusal = _reverify_symbol_artifact(services, handle, selection)
                if refusal is not None:
                    return _record_refusal(
                        services, "write_memory", board_id, args, refusal, started, runtime
                    )
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
        if resolved is not None:
            refusal = symbol_scalar_refusal(resolved, width)
            if refusal is not None:
                return _record_refusal(
                    services, "write_memory", board_id, args, refusal, started, runtime
                )
            requested_bytes = width // 8
            if resolved.size <= 0:
                return _record_refusal(
                    services,
                    "write_memory",
                    board_id,
                    args,
                    PolicyRefusal(
                        "memory/symbol-size-unknown",
                        "The ELF does not describe this symbol as a sized variable; use a real "
                        "variable symbol or the planned raw-address fallback with an explicit reason.",
                    ),
                    started,
                    runtime,
                )
            if requested_bytes > resolved.size:
                return _record_refusal(
                    services,
                    "write_memory",
                    board_id,
                    args,
                    PolicyRefusal(
                        "memory/symbol-width-exceeds-object",
                        f"The {width}-bit write exceeds the {resolved.size}-byte symbol.",
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
