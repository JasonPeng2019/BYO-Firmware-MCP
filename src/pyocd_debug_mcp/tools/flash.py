"""Distinct application and bootloader flash actions."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyocd_debug_mcp.kernel.operations import current_operation, wrap_layer2_response
from pyocd_debug_mcp.services.session_runtime import (
    ActionContext,
    PolicyRefusal,
    SessionRecord,
    ToolOutcome,
)


@dataclass(frozen=True, slots=True)
class FlashToolServices:
    runtime_for: Callable[[str], SessionRecord | None]
    active_session_id: Callable[[str], str | None]
    duration_ms: Callable[[float], int]
    record_event: Callable[..., object]
    format_refusal: Callable[..., str]
    action_context: Callable[[str, str], ActionContext]
    maybe_handle_for: Callable[[str], Any | None]
    handle_for: Callable[[str], Any]
    resolve_request: Callable[[Any | None, str, ActionContext], Any]
    flash_target: Callable[[Any, Path], Path]
    error_code: Callable[[Exception], str]
    validate_flash: Callable[[str, str, Path], None] | None = None
    prepare_symbol_artifact: Callable[[str, str, Path], object] | None = None
    bind_symbol_artifact: Callable[[str, object], None] | None = None
    prepare_generic_allocation: Callable[[str, str, Path, Any], object | None] | None = None
    commit_generic_allocation: Callable[[str, object], None] | None = None
    clear_generic_allocation: Callable[[str], None] | None = None


def build_flash_handlers(
    services: FlashToolServices,
) -> dict[str, Callable[..., str]]:
    """Build separate application and bootloader flash handlers."""

    def execute(
        tool_name: str,
        board_id: str,
        artifact: str,
    ) -> str:
        started = time.monotonic()
        runtime = services.runtime_for(board_id)
        args: dict[str, object] = {
            "board_id": board_id,
            "artifact": artifact,
        }
        pending = services.maybe_handle_for(board_id)
        symbol_binding: object | None = None
        generic_allocation: object | None = None
        try:
            context = services.action_context(tool_name, board_id)
            request = services.resolve_request(pending, artifact, context)
            args.update(request.identity.as_log_fields())
            if services.validate_flash is not None:
                services.validate_flash(tool_name, board_id, request.artifact_path)
            if services.prepare_generic_allocation is not None:
                generic_allocation = services.prepare_generic_allocation(
                    tool_name, board_id, request.artifact_path, pending
                )
            if tool_name == "flash_application" and services.prepare_symbol_artifact is not None:
                symbol_binding = services.prepare_symbol_artifact(
                    tool_name, board_id, request.artifact_path
                )
            handle = services.handle_for(board_id)
            if generic_allocation is not None and services.commit_generic_allocation is not None:
                # Persist the one-way ownership boundary before hardware
                # mutation. If programming then fails, keeping that narrow
                # allocation is safer than leaving partially written flash
                # with no durable owner and still permits an in-range retry.
                services.commit_generic_allocation(board_id, generic_allocation)
            operation = current_operation()
            if operation is not None:
                operation.begin_non_interruptible()
            flashed = services.flash_target(handle, request.artifact_path)
            if symbol_binding is not None and services.bind_symbol_artifact is not None:
                services.bind_symbol_artifact(board_id, symbol_binding)
        except PolicyRefusal as refusal:
            if services.clear_generic_allocation is not None:
                services.clear_generic_allocation(board_id)
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
        except Exception as exc:
            if services.clear_generic_allocation is not None:
                services.clear_generic_allocation(board_id)
            services.record_event(
                tool_name,
                args,
                outcome_kind=ToolOutcome.FAILED,
                error_code=services.error_code(exc),
                duration_ms=services.duration_ms(started),
                details={"message": str(exc)},
                board_id=board_id,
                session=runtime,
            )
            raise
        services.record_event(
            tool_name,
            args,
            outcome_kind=ToolOutcome.SUCCESS,
            error_code=None,
            duration_ms=services.duration_ms(started),
            details={"target_state": "running", "safety_map_checked": True},
            board_id=board_id,
            session=runtime,
        )
        return wrap_layer2_response(
            f"Flashed {flashed} as {tool_name} within its mapped partition; target left running."
        )

    def flash_application(
        board_id: str,
        artifact: str,
    ) -> str:
        """Flash artifact-defined addresses contained by the mapped application partition."""

        return execute("flash_application", board_id, artifact)

    def flash_bootloader(
        board_id: str,
        artifact: str,
    ) -> str:
        """Flash artifact-defined bootloader addresses after a permission-carrying fixed plan."""

        return execute("flash_bootloader", board_id, artifact)

    return {
        "flash_application": flash_application,
        "flash_bootloader": flash_bootloader,
    }
