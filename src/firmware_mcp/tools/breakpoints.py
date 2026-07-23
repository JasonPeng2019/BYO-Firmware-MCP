"""Revised Layer-2 breakpoint actions."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from firmware_mcp.kernel.operations import wrap_layer2_response
from firmware_mcp.services.session_runtime import InvalidRequestError, SessionRecord, ToolOutcome


@dataclass(frozen=True, slots=True)
class BreakpointToolServices:
    runtime_for: Callable[[str], SessionRecord | None]
    active_session_id: Callable[[str], str | None]
    duration_ms: Callable[[float], int]
    record_event: Callable[..., object]
    format_invalid: Callable[..., str]
    handle_for: Callable[[str], Any]
    set_target_breakpoint: Callable[[Any, int], None]
    remove_target_breakpoint: Callable[[Any, int], None]
    check_breakpoint: Callable[[Any, int, str | None], object] | None = None


def _parse_address(value: str | int) -> int:
    if isinstance(value, bool):
        raise ValueError("address must not be boolean")
    parsed = value if isinstance(value, int) else int(value, 0)
    if parsed < 0:
        raise ValueError("address must be non-negative")
    return parsed


def canonicalize_breakpoint_address(address: int) -> int:
    """Return an ARM code address without the Thumb-state indicator bit."""

    return address & ~1


def build_breakpoint_handlers(
    services: BreakpointToolServices,
) -> dict[str, Callable[..., str]]:
    """Build direct set and remove breakpoint handlers."""

    def refuse(
        tool_name: str,
        board_id: str,
        args: dict[str, object],
        message: str,
        started: float,
        runtime: SessionRecord | None,
    ) -> str:
        issue = InvalidRequestError("breakpoint/invalid-location", message)
        services.record_event(
            tool_name,
            args,
            outcome_kind=ToolOutcome.INVALID,
            error_code=issue.code,
            duration_ms=services.duration_ms(started),
            details={"message": message},
            board_id=board_id,
            session=runtime,
        )
        return wrap_layer2_response(
            services.format_invalid(
                issue,
                session_id=services.active_session_id(board_id),
            )
        )

    def set_breakpoint(board_id: str, address: str | int, elf_path: str) -> str:
        """**What** Set one breakpoint at a numeric executable address.

        **When** Use after `find_symbol` if a symbol address is desired.

        **Parameters** `board_id` is the board; `address` is decimal/hex, for example
        `"0x08000100"`; `elf_path` is the exact ELF whose executable PT_LOAD
        bytes cover the address.

        **Returns** The canonical target breakpoint address after live execute-span validation.

        **Failures and recovery** Invalid or non-executable addresses are reported; use
        `find_symbol` or inspect the live target map.
        """

        started = time.monotonic()
        runtime = services.runtime_for(board_id)
        args = {
            "board_id": board_id,
            "address": address,
            "elf_path": elf_path,
        }
        try:
            parsed_address = _parse_address(address)
        except (TypeError, ValueError):
            return refuse(
                "set_breakpoint",
                board_id,
                args,
                "address must be a non-negative decimal or hexadecimal integer.",
                started,
                runtime,
            )
        handle = services.handle_for(board_id)
        parsed_address = canonicalize_breakpoint_address(parsed_address)
        if not isinstance(elf_path, str) or not elf_path:
            return refuse(
                "set_breakpoint",
                board_id,
                args,
                "elf_path is required for executable breakpoint evidence.",
                started,
                runtime,
            )
        if services.check_breakpoint is not None:
            services.check_breakpoint(handle, parsed_address, elf_path)
        services.set_target_breakpoint(handle, parsed_address)
        services.record_event(
            "set_breakpoint",
            args,
            outcome_kind=ToolOutcome.SUCCESS,
            error_code=None,
            duration_ms=services.duration_ms(started),
            details={"resolved_address": parsed_address},
            board_id=board_id,
            session=runtime,
        )
        return wrap_layer2_response(
            f"Breakpoint set at 0x{parsed_address:08X} after live execute-span validation."
        )

    def remove_breakpoint(board_id: str, address: str | int) -> str:
        """**What** Remove one numeric breakpoint.

        **When** Use to remove a breakpoint previously set for this board.

        **Parameters** `board_id` is the board; `address` is decimal/hex, for example
        `"0x08000100"`.

        **Returns** The canonical removed address after live execute-span validation.

        **Failures and recovery** Invalid, unmapped, or disconnected targets are reported; use
        `get_target_state` or `connect_board` before retrying.
        """

        started = time.monotonic()
        runtime = services.runtime_for(board_id)
        args = {"board_id": board_id, "address": address}
        try:
            parsed = canonicalize_breakpoint_address(_parse_address(address))
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
        if services.check_breakpoint is not None:
            services.check_breakpoint(handle, parsed, None)
        services.remove_target_breakpoint(handle, parsed)
        services.record_event(
            "remove_breakpoint",
            args,
            outcome_kind=ToolOutcome.SUCCESS,
            error_code=None,
            duration_ms=services.duration_ms(started),
            details={"resolved_address": parsed},
            board_id=board_id,
            session=runtime,
        )
        return wrap_layer2_response(f"Breakpoint removed at 0x{parsed:08X}.")

    return {
        "set_breakpoint": set_breakpoint,
        "remove_breakpoint": remove_breakpoint,
    }
