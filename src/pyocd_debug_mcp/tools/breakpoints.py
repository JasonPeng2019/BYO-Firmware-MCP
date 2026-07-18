"""Revised Layer-2 breakpoint actions."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyocd_debug_mcp.kernel.operations import wrap_layer2_response
from pyocd_debug_mcp.services.session_runtime import PolicyRefusal, SessionRecord, ToolOutcome
from pyocd_debug_mcp.services.symbols import ResolvedSymbol

@dataclass(frozen=True, slots=True)
class BreakpointToolServices:
    runtime_for: Callable[[str], SessionRecord | None]
    active_session_id: Callable[[str], str | None]
    duration_ms: Callable[[float], int]
    record_event: Callable[..., object]
    format_refusal: Callable[..., str]
    handle_for: Callable[[str], Any]
    resolve_symbol: Callable[[Path, str], ResolvedSymbol]
    set_target_breakpoint: Callable[[Any, int], None]
    remove_target_breakpoint: Callable[[Any, int], None]
    check_breakpoint: Callable[[str, int, Path], None] | None = None
    # Transitional construction compatibility only. The selected ELF now comes
    # from the immutable action parameters, never from the connected handle.
    symbol_artifact_for: Callable[[Any], Path] | None = None


def _parse_address(value: str | int) -> int:
    if isinstance(value, bool):
        raise ValueError("address must not be boolean")
    parsed = value if isinstance(value, int) else int(value, 0)
    if parsed < 0:
        raise ValueError("address must be non-negative")
    return parsed


def build_breakpoint_handlers(
    services: BreakpointToolServices,
) -> dict[str, Callable[..., str]]:
    """Build guarded set and always-available remove breakpoint handlers."""

    def refuse(
        tool_name: str,
        board_id: str,
        args: dict[str, object],
        message: str,
        started: float,
        runtime: SessionRecord | None,
    ) -> str:
        refusal = PolicyRefusal("breakpoint/invalid-location", message)
        services.record_event(
            tool_name,
            args,
            outcome_kind=ToolOutcome.REFUSED,
            error_code=refusal.code,
            duration_ms=services.duration_ms(started),
            details={"message": message},
            board_id=board_id,
            session=runtime,
        )
        return wrap_layer2_response(
            services.format_refusal(
                refusal,
                session_id=services.active_session_id(board_id),
            )
        )

    def set_breakpoint(
        board_id: str,
        symbol_or_address: str | int,
        elf_artifact: str,
    ) -> str:
        """Set one symbol-backed or explicit breakpoint under a fixed plan."""

        started = time.monotonic()
        runtime = services.runtime_for(board_id)
        args = {
            "board_id": board_id,
            "symbol_or_address": symbol_or_address,
            "elf_artifact": elf_artifact,
        }
        try:
            artifact = Path(elf_artifact).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            return refuse(
                "set_breakpoint",
                board_id,
                args,
                "elf_artifact must name the current local ELF file.",
                started,
                runtime,
            )
        if artifact.suffix.casefold() != ".elf" or not artifact.is_file():
            return refuse(
                "set_breakpoint",
                board_id,
                args,
                "elf_artifact must name the current local ELF file.",
                started,
                runtime,
            )
        handle = services.handle_for(board_id)
        try:
            address = _parse_address(symbol_or_address)
        except (TypeError, ValueError):
            if not isinstance(symbol_or_address, str) or not symbol_or_address.strip():
                return refuse(
                    "set_breakpoint",
                    board_id,
                    args,
                    "symbol_or_address must be a non-empty symbol or address.",
                    started,
                    runtime,
                )
            address = services.resolve_symbol(artifact, symbol_or_address).address
        if services.check_breakpoint is not None:
            services.check_breakpoint(board_id, address, artifact)
        services.set_target_breakpoint(handle, address)
        services.record_event(
            "set_breakpoint",
            args,
            outcome_kind=ToolOutcome.SUCCESS,
            error_code=None,
            duration_ms=services.duration_ms(started),
            details={"resolved_address": address, "safety_map_checked": True},
            board_id=board_id,
            session=runtime,
        )
        return wrap_layer2_response(f"Breakpoint set in executable space at 0x{address:08X}.")

    def remove_breakpoint(board_id: str, address: str | int) -> str:
        """Remove one breakpoint at an exact hexadecimal or decimal address."""

        started = time.monotonic()
        runtime = services.runtime_for(board_id)
        args = {"board_id": board_id, "address": address}
        try:
            parsed = _parse_address(address)
        except (TypeError, ValueError) as exc:
            return refuse(
                "remove_breakpoint",
                board_id,
                args,
                str(exc),
                started,
                runtime,
            )
        handle = services.handle_for(board_id)
        services.remove_target_breakpoint(handle, parsed)
        services.record_event(
            "remove_breakpoint",
            args,
            outcome_kind=ToolOutcome.SUCCESS,
            error_code=None,
            duration_ms=services.duration_ms(started),
            board_id=board_id,
            session=runtime,
        )
        return wrap_layer2_response(f"Breakpoint removed at 0x{parsed:08X}.")

    return {
        "set_breakpoint": set_breakpoint,
        "remove_breakpoint": remove_breakpoint,
    }
