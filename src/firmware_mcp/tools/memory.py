"""Direct symbol and address memory actions with honest verification."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from elftools.common.exceptions import ELFError

from firmware_mcp.kernel.operations import wrap_layer2_response
from firmware_mcp.services.physical_memory import PhysicalMemoryAccessError
from firmware_mcp.services.session_runtime import InvalidRequestError, SessionRecord, ToolOutcome
from firmware_mcp.services.symbols import ResolvedSymbol, is_elf_artifact
from firmware_mcp.target_errors import (
    ReferenceArtifactError,
    SymbolLookupError,
    TargetControlError,
)


@dataclass(frozen=True, slots=True)
class MemoryToolServices:
    runtime_for: Callable[[str], SessionRecord | None]
    active_session_id: Callable[[str], str | None]
    duration_ms: Callable[[float], int]
    record_event: Callable[..., object]
    format_invalid: Callable[..., str]
    handle_for: Callable[[str], Any]
    symbol_artifact_for: Callable[[Any], Path]
    find_symbols: Callable[[Path, str], tuple[ResolvedSymbol, ...]]
    resolve_symbol: Callable[[Path, str], ResolvedSymbol]
    read_target_memory: Callable[[Any, int, int], int]
    read_target_block: Callable[[Any, int, int], list[int]]
    write_target_memory: Callable[[Any, int, int, int], None]
    check_memory_read: Callable[[Any, int, int], object]
    check_memory_write: Callable[[Any, int, int], object]


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


def _record_invalid(
    services: MemoryToolServices,
    tool_name: str,
    board_id: str,
    normalized_args: Mapping[str, object],
    issue: InvalidRequestError,
    started: float,
    runtime: SessionRecord | None,
) -> str:
    services.record_event(
        tool_name,
        normalized_args,
        outcome_kind=ToolOutcome.INVALID,
        error_code=issue.code,
        duration_ms=services.duration_ms(started),
        details={"message": issue.message},
        board_id=board_id,
        session=runtime,
    )
    return wrap_layer2_response(
        services.format_invalid(
            issue,
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
    details: Mapping[str, object] | None = None,
) -> str:
    services.record_event(
        tool_name,
        normalized_args,
        outcome_kind=ToolOutcome.SUCCESS,
        error_code=None,
        duration_ms=services.duration_ms(started),
        details=dict(details or {}),
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
) -> tuple[SymbolArtifactSelection | None, InvalidRequestError | None]:
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
        return None, InvalidRequestError(
            "memory/symbol-artifact-unavailable",
            "The selected firmware ELF is unavailable or changed: "
            f"{exc}. Pass the current project's local ELF/AXF path as elf_artifact, or flash that "
            "ELF in this Server Run to create a temporary convenience binding.",
        )
    except ValueError as exc:
        return None, InvalidRequestError("memory/symbol-artifact-unavailable", str(exc))


def _reverify_symbol_artifact(
    services: MemoryToolServices,
    handle: Any,
    selection: SymbolArtifactSelection,
) -> InvalidRequestError | None:
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
        return InvalidRequestError(
            "memory/symbol-artifact-unavailable",
            f"{exc}. Retry with the current project ELF supplied as elf_artifact.",
        )
    return None


def _symbol_parse_issue(exc: Exception) -> InvalidRequestError:
    return InvalidRequestError(
        "memory/symbol-artifact-parse-failed",
        "The selected firmware ELF could not be parsed safely: "
        f"{exc}. Rebuild it or pass the correct current project .elf as elf_artifact.",
    )


def _symbol_lookup_issue(exc: SymbolLookupError) -> InvalidRequestError:
    return InvalidRequestError("memory/symbol-not-found", str(exc))


def symbol_scalar_issue(resolved: ResolvedSymbol, width: int) -> InvalidRequestError | None:
    """Return a pre-I/O issue for a non-data or misaligned scalar symbol."""

    if resolved.type.upper() == "STT_FUNC":
        return InvalidRequestError(
            "memory/symbol-is-function",
            f"Symbol '{resolved.name}' is an executable function, not a data object. "
            "Use find_symbol and the breakpoint tools; use the direct mapped-address read only "
            "when inspecting code bytes is intentional.",
        )
    alignment = width // 8
    if resolved.address % alignment:
        return InvalidRequestError(
            "memory/symbol-address-unaligned",
            f"Symbol '{resolved.name}' at 0x{resolved.address:08X} is not aligned for a "
            f"{width}-bit scalar access. Choose a compatible width.",
        )
    return None


def scalar_address_issue(address: int, width: int) -> InvalidRequestError | None:
    """Return a pre-I/O issue for an unaligned scalar physical address."""

    alignment = width // 8
    if address % alignment:
        return InvalidRequestError(
            "memory/address-unaligned",
            f"Address 0x{address:08X} is not aligned for a {width}-bit scalar access. "
            "Choose a compatible width or an aligned address.",
        )
    return None


def _lookup_or_artifact_issue(
    services: MemoryToolServices,
    handle: Any,
    selection: SymbolArtifactSelection,
    exc: SymbolLookupError,
) -> InvalidRequestError:
    return _reverify_symbol_artifact(services, handle, selection) or _symbol_lookup_issue(exc)


def build_memory_handlers(
    services: MemoryToolServices,
) -> dict[str, Callable[..., str]]:
    """Build the memory tool surface."""

    def find_symbol(board_id: str, query: str, elf_path: str) -> str:
        """**What** Find every sorted symbol matching a query in an explicit ELF.

        **When** Use before composing an address-based debug operation.

        **Parameters** `board_id` is the connected board; `query` is a symbol fragment (for
        example `"main"`); `elf_path` is the current `.elf`/`.axf` path.

        **Returns** Sorted name, address, size, and type evidence from that exact ELF.

        **Failures and recovery** Parse or missing-symbol errors are explicit; rebuild and retry
        with the current `elf_path`.
        """

        started = time.monotonic()
        runtime = services.runtime_for(board_id)
        args = {"board_id": board_id, "query": query, "elf_path": elf_path}
        if not query.strip():
            return _record_invalid(
                services,
                "find_symbol",
                board_id,
                args,
                InvalidRequestError("memory/empty-symbol-query", "query must not be empty."),
                started,
                runtime,
            )
        handle = services.handle_for(board_id)
        selection, issue = _select_symbol_artifact(services, handle, elf_path)
        if selection is None:
            assert issue is not None
            return _record_invalid(services, "find_symbol", board_id, args, issue, started, runtime)
        try:
            matches = services.find_symbols(selection.path, query)
        except SymbolLookupError as exc:
            return _record_invalid(
                services,
                "find_symbol",
                board_id,
                args,
                _lookup_or_artifact_issue(services, handle, selection, exc),
                started,
                runtime,
            )
        except (ELFError, OSError, RuntimeError, ValueError) as exc:
            return _record_invalid(
                services,
                "find_symbol",
                board_id,
                args,
                _symbol_parse_issue(exc),
                started,
                runtime,
            )
        issue = _reverify_symbol_artifact(services, handle, selection)
        if issue is not None:
            return _record_invalid(services, "find_symbol", board_id, args, issue, started, runtime)
        if not matches:
            result = f"No symbols matching '{query}' were found in {selection.path}."
        else:
            rendered = ", ".join(
                f"{item.name}@0x{item.address:08X} size={item.size} type={item.type}"
                for item in matches
            )
            result = f"Symbols matching '{query}' in {selection.path}: {rendered}"
        return _record_success(services, "find_symbol", board_id, args, result, started, runtime)

    def read_memory(
        board_id: str,
        address: str | int,
        width_bits: int = 32,
        length_bytes: int | None = None,
    ) -> str:
        """**What** Read a scalar or byte block at a live mapped physical address.

        **When** Use to inspect RAM, flash, or a readable peripheral span.

        **Parameters** `board_id` is the board; `address` is decimal/hex (for example
        `"0x20000000"`); `width_bits` is 8, 16, or 32 bits; optional `length_bytes` requests a
        byte block (for example `16`).

        **Returns** The observed scalar or bytes after live readable-span validation.

        **Failures and recovery** Unmapped, unreadable, or unaligned spans are explicit; inspect
        `get_board_info` or choose a valid address.
        """

        started = time.monotonic()
        runtime = services.runtime_for(board_id)
        args = {
            "board_id": board_id,
            "address": address,
            "width_bits": width_bits,
            "length_bytes": length_bytes,
        }
        if not _valid_width(width_bits):
            return _record_invalid(
                services,
                "read_memory",
                board_id,
                args,
                InvalidRequestError("memory/invalid-width", "width must be one of: 8, 16, 32."),
                started,
                runtime,
            )
        if length_bytes is not None and (
            not isinstance(length_bytes, int) or isinstance(length_bytes, bool) or length_bytes < 1
        ):
            return _record_invalid(
                services,
                "read_memory",
                board_id,
                args,
                InvalidRequestError(
                    "memory/invalid-length",
                    "length must be a positive integer.",
                ),
                started,
                runtime,
            )
        try:
            parsed_address = _parse_integer(address)
        except (TypeError, ValueError) as exc:
            return _record_invalid(
                services,
                "read_memory",
                board_id,
                args,
                InvalidRequestError("memory/invalid-address", str(exc)),
                started,
                runtime,
            )
        if parsed_address < 0:
            return _record_invalid(
                services,
                "read_memory",
                board_id,
                args,
                InvalidRequestError("memory/invalid-address", "address must be non-negative."),
                started,
                runtime,
            )
        if length_bytes is None:
            issue = scalar_address_issue(parsed_address, width_bits)
            if issue is not None:
                return _record_invalid(
                    services, "read_memory", board_id, args, issue, started, runtime
                )
        size_bytes = width_bits // 8 if length_bytes is None else length_bytes
        handle = services.handle_for(board_id)
        safety_evidence = services.check_memory_read(handle, parsed_address, size_bytes)
        if length_bytes is None:
            value = services.read_target_memory(handle, parsed_address, width_bits)
            result = f"0x{value:0{width_bits // 4}X}"
        else:
            values = services.read_target_block(handle, parsed_address, length_bytes)
            result = " ".join(f"{byte:02X}" for byte in values)
        unknown_semantics = isinstance(safety_evidence, Mapping) and bool(
            safety_evidence.get("unknown")
        )
        if unknown_semantics:
            result += " (semantic_role=unknown; readable observation is allowed, but no classified role was evidenced.)"
        return _record_success(
            services,
            "read_memory",
            board_id,
            args,
            result,
            started,
            runtime,
            details={"semantic_role_unknown": unknown_semantics},
        )

    def write_memory(
        board_id: str,
        address: str | int,
        value: object,
        width_bits: int = 32,
        verify: bool = True,
    ) -> str:
        """**What** Write one direct live physical memory scalar.

        **When** Use for intentional RAM/peripheral writes after selecting a numeric address.

        **Parameters** `board_id` is the board; `address` is decimal/hex (for example
        `"0x20000000"`); `value` is an integer; `width_bits` is 8, 16, or 32 bits; `verify`
        requests readback (for example `true`).

        **Returns** Matched readback or explicit provider-accepted/no-readback evidence.

        **Failures and recovery** Invalid, unmapped, unreadable verified, or mismatch outcomes are
        explicit; use `read_memory` or choose `verify=false` for an intended write-only span.
        """

        started = time.monotonic()
        runtime = services.runtime_for(board_id)
        args = {
            "board_id": board_id,
            "address": address,
            "value": value,
            "width_bits": width_bits,
            "verify": verify,
        }
        if not _valid_width(width_bits):
            return _record_invalid(
                services,
                "write_memory",
                board_id,
                args,
                InvalidRequestError("memory/invalid-width", "width must be one of: 8, 16, 32."),
                started,
                runtime,
            )
        handle = services.handle_for(board_id)
        try:
            parsed_address = _parse_integer(address)
        except (TypeError, ValueError) as exc:
            return _record_invalid(
                services,
                "write_memory",
                board_id,
                args,
                InvalidRequestError("memory/invalid-address", str(exc)),
                started,
                runtime,
            )
        try:
            parsed_value = _parse_integer(value)
        except (TypeError, ValueError) as exc:
            return _record_invalid(
                services,
                "write_memory",
                board_id,
                args,
                InvalidRequestError("memory/invalid-value", str(exc)),
                started,
                runtime,
            )
        if parsed_address < 0 or parsed_value < 0 or parsed_value >= 1 << width_bits:
            return _record_invalid(
                services,
                "write_memory",
                board_id,
                args,
                InvalidRequestError(
                    "memory/value-out-of-range",
                    f"address must be non-negative and value must fit in {width_bits} bits.",
                ),
                started,
                runtime,
            )
        issue = scalar_address_issue(parsed_address, width_bits)
        if issue is not None:
            return _record_invalid(
                services, "write_memory", board_id, args, issue, started, runtime
            )
        services.check_memory_write(handle, parsed_address, width_bits // 8)
        if verify:
            # Establish readback capability before mutation. A write-only live span
            # remains usable only through the explicit unverified path below.
            try:
                services.check_memory_read(handle, parsed_address, width_bits // 8)
            except PhysicalMemoryAccessError as exc:
                raise TargetControlError(
                    "Verified memory writes require live readable as well as writable coverage. "
                    "Use verify=false only when an intentionally unverified write to this live "
                    "write-only span is desired."
                ) from exc
        services.write_target_memory(handle, parsed_address, parsed_value, width_bits)
        target = f"0x{parsed_address:08X}"
        if verify:
            observed = services.read_target_memory(handle, parsed_address, width_bits)
            if observed != parsed_value:
                raise RuntimeError(
                    f"Memory write readback mismatch at 0x{parsed_address:08X}: expected 0x{parsed_value:X}, "
                    f"observed 0x{observed:X}. Reconnect and retry after confirming this location is "
                    "not volatile or clear-on-write."
                )
            result = (
                f"Wrote and verified 0x{parsed_value:X} at {target} "
                f"(address=0x{parsed_address:08X}; verification=matched)."
            )
        else:
            result = (
                f"Provider accepted write of 0x{parsed_value:X} at {target} "
                f"(address=0x{parsed_address:08X}); "
                "verification=not_requested."
            )
        return _record_success(services, "write_memory", board_id, args, result, started, runtime)

    return {
        "find_symbol": find_symbol,
        "read_memory": read_memory,
        "write_memory": write_memory,
    }
