"""Distinct M5 application and bootloader flash actions."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyocd_debug_mcp.kernel.operations import wrap_layer2_response
from pyocd_debug_mcp.services.session_runtime import (
    ActionContext,
    PolicyRefusal,
    SessionRecord,
    ToolOutcome,
    WatcherBlocked,
)

@dataclass(frozen=True, slots=True)
class FlashToolServices:
    runtime_for: Callable[[str], SessionRecord | None]
    active_session_id: Callable[[str], str | None]
    duration_ms: Callable[[float], int]
    record_event: Callable[..., object]
    record_blocked_event: Callable[..., object]
    format_refusal: Callable[..., str]
    format_block: Callable[..., str]
    ensure_flash_allowed: Callable[[SessionRecord], None]
    action_context: Callable[[str, str], ActionContext]
    maybe_handle_for: Callable[[str], Any | None]
    handle_for: Callable[[str], Any]
    resolve_request: Callable[[Any | None, str, ActionContext], Any]
    flash_target: Callable[[Any, Path], Path]
    handle_mutation_event: Callable[[str, object], None]
    error_code: Callable[[Exception], str]
    validate_flash: Callable[[str, str, Path], None] | None = None


def _parse_optional_address(value: str | int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("target_address must not be boolean")
    parsed = value if isinstance(value, int) else int(value, 0)
    if parsed < 0:
        raise ValueError("target_address must be non-negative")
    return parsed


def build_flash_handlers(
    services: FlashToolServices,
) -> dict[str, Callable[..., str]]:
    """Build separate application and bootloader flash handlers."""

    def execute(
        tool_name: str,
        board_id: str,
        artifact: str,
        target_address: str | int | None,
    ) -> str:
        started = time.monotonic()
        runtime = services.runtime_for(board_id)
        args: dict[str, object] = {
            "board_id": board_id,
            "artifact": artifact,
            "target_address": target_address,
        }
        try:
            parsed_target_address = _parse_optional_address(target_address)
        except ValueError as exc:
            refusal = PolicyRefusal("flash/invalid-target-address", str(exc))
            services.record_event(
                tool_name,
                args,
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
        if parsed_target_address is not None:
            refusal = PolicyRefusal(
                "flash/explicit-address-unavailable",
                "Explicit target_address is unavailable for the current ELF/HEX backend. "
                "Use the artifact's own load addresses; M7 validates them against the named "
                "partition.",
            )
            services.record_event(
                tool_name,
                args,
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
        if runtime is not None:
            try:
                services.ensure_flash_allowed(runtime)
            except WatcherBlocked as blocked:
                services.record_blocked_event(
                    tool_name,
                    args,
                    blocked,
                    started=started,
                    board_id=board_id,
                    session=runtime,
                )
                return wrap_layer2_response(
                    services.format_block(blocked, session_id=runtime.session_id)
                )
        pending = services.maybe_handle_for(board_id)
        try:
            context = services.action_context(tool_name, board_id)
            request = services.resolve_request(pending, artifact, context)
            args.update(request.identity.as_log_fields())
            if services.validate_flash is not None:
                services.validate_flash(tool_name, board_id, request.artifact_path)
            handle = services.handle_for(board_id)
            flashed = services.flash_target(handle, request.artifact_path)
        except PolicyRefusal as refusal:
            event = services.record_event(
                tool_name,
                args,
                outcome_kind=ToolOutcome.REFUSED,
                error_code=refusal.code,
                duration_ms=services.duration_ms(started),
                details={"message": refusal.message},
                board_id=board_id,
                session=runtime,
            )
            if runtime is not None:
                services.handle_mutation_event(board_id, event)
            return wrap_layer2_response(
                services.format_refusal(
                    refusal,
                    session_id=services.active_session_id(board_id),
                )
            )
        except Exception as exc:
            event = services.record_event(
                tool_name,
                args,
                outcome_kind=ToolOutcome.FAILED,
                error_code=services.error_code(exc),
                duration_ms=services.duration_ms(started),
                details={"message": str(exc)[:300]},
                board_id=board_id,
                session=runtime,
            )
            if runtime is not None:
                services.handle_mutation_event(board_id, event)
            raise
        event = services.record_event(
            tool_name,
            args,
            outcome_kind=ToolOutcome.SUCCESS,
            error_code=None,
            duration_ms=services.duration_ms(started),
            details={"target_state": "running", "safety_map_checked": True},
            board_id=board_id,
            session=runtime,
        )
        if runtime is not None:
            services.handle_mutation_event(board_id, event)
        return wrap_layer2_response(
            f"Flashed {flashed} as {tool_name} within its mapped partition; target left running."
        )

    def flash_application(
        board_id: str,
        artifact: str,
        target_address: str | int | None = None,
    ) -> str:
        """Flash one application artifact contained by the mapped application partition."""

        return execute("flash_application", board_id, artifact, target_address)

    def flash_bootloader(
        board_id: str,
        artifact: str,
        target_address: str | int | None = None,
    ) -> str:
        """Flash one bootloader artifact after a permission-carrying fixed plan."""

        return execute("flash_bootloader", board_id, artifact, target_address)

    return {
        "flash_application": flash_application,
        "flash_bootloader": flash_bootloader,
    }
