"""Flash explicit firmware artifacts at their physical image addresses."""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, cast

from firmware_mcp.adapters.debug_interface import FlashVerification, TargetSessionHandle
from firmware_mcp.kernel.operations import current_operation, wrap_layer2_response
from firmware_mcp.services.session_runtime import (
    ActionContext,
    InvalidRequestError,
    SessionRecord,
    ToolOutcome,
)
from firmware_mcp.target_errors import FlashFinalResetFailed, FlashFinalResetUncertain


SUPPORTED_FLASH_SUFFIXES = frozenset({".axf", ".elf", ".hex"})
_URL_LIKE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


@dataclass(frozen=True, slots=True)
class FlashArtifactIdentity:
    path: Path
    suffix: str
    size_bytes: int
    sha256: str
    source: str

    def as_log_fields(self) -> dict[str, object]:
        return {
            "artifact_path": str(self.path),
            "artifact_suffix": self.suffix,
            "artifact_size_bytes": self.size_bytes,
            "artifact_sha256": self.sha256,
            "artifact_source": self.source,
        }


@dataclass(frozen=True, slots=True)
class ResolvedFlashRequest:
    artifact_path: Path
    identity: FlashArtifactIdentity


def resolve_flash_request(
    handle: TargetSessionHandle | None,
    *,
    explicit_path: Path | str | None,
    action_context: ActionContext,
) -> ResolvedFlashRequest:
    """Resolve one explicit local ELF, AXF, or HEX input and record its identity."""

    if handle is None:
        raise InvalidRequestError(
            "flash/no-session",
            "Flash requires an active connected session.",
            session_id=action_context.session_id,
        )
    if explicit_path is None or (isinstance(explicit_path, str) and not explicit_path.strip()):
        raise InvalidRequestError(
            "flash/firmware-required",
            "Flash requires a non-empty firmware_path.",
            session_id=action_context.session_id,
        )
    raw_path = str(explicit_path)
    if _URL_LIKE.match(raw_path):
        raise InvalidRequestError(
            "flash/non-local-path",
            "Flash path must be a local filesystem path.",
            session_id=action_context.session_id,
        )
    path = Path(explicit_path).expanduser().resolve()
    if not path.exists() or path.is_dir():
        raise InvalidRequestError(
            "flash/invalid-file",
            f"Flash artifact must be an existing file: {path}",
            session_id=action_context.session_id,
        )
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_FLASH_SUFFIXES:
        raise InvalidRequestError(
            "flash/unsupported-suffix",
            f"Unsupported flash artifact type '{suffix or '(none)'}'.",
            session_id=action_context.session_id,
        )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return ResolvedFlashRequest(
        artifact_path=path,
        identity=FlashArtifactIdentity(path, suffix, path.stat().st_size, digest, "explicit"),
    )


@dataclass(frozen=True, slots=True)
class FlashToolServices:
    runtime_for: Callable[[str], SessionRecord | None]
    active_session_id: Callable[[str], str | None]
    duration_ms: Callable[[float], int]
    record_event: Callable[..., object]
    format_invalid: Callable[..., str]
    action_context: Callable[[str, str], ActionContext]
    maybe_handle_for: Callable[[str], Any | None]
    handle_for: Callable[[str], Any]
    resolve_request: Callable[[Any | None, str, ActionContext], Any]
    flash_target: Callable[[Any, Path, bool], FlashVerification]
    error_code: Callable[[Exception], str]
    validate_flash: Callable[[str, str, Path], None] | None = None
    prepare_symbol_artifact: Callable[[str, str, Path], object] | None = None
    bind_symbol_artifact: Callable[[str, object], None] | None = None
    execution_file: Callable[[str], bytes | None] | None = None
    stage_snapshot: Callable[[Path, bytes], Path] | None = None
    cleanup_snapshot: Callable[[Path], str | None] | None = None


def build_flash_handlers(
    services: FlashToolServices,
) -> dict[str, Callable[..., str]]:
    """Build the single physical flash handler."""

    def execute(
        tool_name: str,
        board_id: str,
        firmware_path: str,
        flash_role: Literal["application", "bootloader", "full-device", "sensitive"],
        halt_after_reset: bool,
        artifact_target_evidence_path: str | None,
    ) -> str:
        started = time.monotonic()
        runtime = services.runtime_for(board_id)
        args: dict[str, object] = {
            "board_id": board_id,
            "firmware_path": firmware_path,
            "halt_after_reset": halt_after_reset,
            "flash_role": flash_role,
            "artifact_target_evidence_path": artifact_target_evidence_path,
        }
        pending = services.maybe_handle_for(board_id)
        symbol_binding: object | None = None
        staged: Path | None = None
        request: ResolvedFlashRequest | None = None
        cleanup_diagnostic: str | None = None
        cleanup_attempted = False

        def cleanup_staged_snapshot() -> str | None:
            nonlocal cleanup_attempted, cleanup_diagnostic
            current_request = request
            if (
                not cleanup_attempted
                and staged is not None
                and current_request is not None
                and staged != current_request.artifact_path
                and services.cleanup_snapshot is not None
            ):
                cleanup_attempted = True
                cleanup_diagnostic = services.cleanup_snapshot(staged)
            return cleanup_diagnostic

        try:
            context = services.action_context(tool_name, board_id)
            captured = services.execution_file("firmware_path") if services.execution_file else None
            if captured is None:
                # Unguarded unit-level use retains the owned resolver. Public
                # server execution always has GuardCore's immutable bytes.
                request = cast(
                    ResolvedFlashRequest,
                    services.resolve_request(pending, firmware_path, context),
                )
                captured = request.artifact_path.read_bytes()
            else:
                source = Path(firmware_path).expanduser().resolve()
                suffix = source.suffix.lower()
                if suffix not in SUPPORTED_FLASH_SUFFIXES:
                    raise InvalidRequestError(
                        "flash/unsupported-suffix",
                        f"Unsupported flash artifact type '{suffix or '(none)'}'.",
                        session_id=context.session_id,
                    )
                request = ResolvedFlashRequest(
                    artifact_path=source,
                    identity=FlashArtifactIdentity(
                        source,
                        suffix,
                        len(captured),
                        hashlib.sha256(captured).hexdigest(),
                        "guard-execution-snapshot",
                    ),
                )
            # Each branch above constructs the request from either the
            # explicitly resolved input or GuardCore's immutable bytes.
            assert request is not None
            args.update(request.identity.as_log_fields())
            # From this point the caller path is never opened by the public
            # flash route: all parsing, symbols and provider work use staged.
            staged = (
                services.stage_snapshot(request.artifact_path, captured)
                if services.stage_snapshot
                else request.artifact_path
            )
            args["staged_artifact_sha256"] = hashlib.sha256(captured).hexdigest()
            if services.validate_flash is not None:
                services.validate_flash(tool_name, board_id, staged)
            if services.prepare_symbol_artifact is not None:
                symbol_binding = services.prepare_symbol_artifact(tool_name, board_id, staged)
            handle = services.handle_for(board_id)
            operation = current_operation()
            if operation is not None:
                operation.begin_non_interruptible()
            flashed = services.flash_target(handle, staged, halt_after_reset)
            # Worker evidence identifies the internal immutable file. Public
            # records retain the selected artifact identity while preserving
            # the staged digest as an auditable execution fact.
            flashed = replace(flashed, firmware_path=str(request.artifact_path))
            if symbol_binding is not None and services.bind_symbol_artifact is not None:
                services.bind_symbol_artifact(board_id, symbol_binding)
        except InvalidRequestError as issue:
            cleanup_staged_snapshot()
            services.record_event(
                tool_name,
                args,
                outcome_kind=ToolOutcome.INVALID,
                error_code=issue.code,
                duration_ms=services.duration_ms(started),
                details={
                    "message": issue.message,
                    **(
                        {"snapshot_cleanup_diagnostic": cleanup_diagnostic}
                        if cleanup_diagnostic
                        else {}
                    ),
                },
                board_id=board_id,
                session=runtime,
            )
            return wrap_layer2_response(
                services.format_invalid(
                    issue,
                    session_id=services.active_session_id(board_id),
                )
            )
        except (FlashFinalResetFailed, FlashFinalResetUncertain) as exc:
            evidence = exc.evidence
            cleanup_staged_snapshot()
            services.record_event(
                tool_name,
                args,
                outcome_kind=ToolOutcome.FAILED,
                error_code=services.error_code(exc),
                duration_ms=services.duration_ms(started),
                details={
                    "message": str(exc),
                    "program_readback": "verified",
                    "firmware_path": evidence.firmware_path,
                    "byte_count": evidence.byte_count,
                    "verified_ranges": evidence.verified_ranges,
                    "expected_sha256": evidence.expected_sha256,
                    "observed_sha256": evidence.observed_sha256,
                    "final_reset_postcondition": evidence.final_reset_postcondition,
                    "final_reset_error_type": evidence.final_reset_error_type,
                    "final_reset_error_message": evidence.final_reset_error_message,
                    "flash_and_run_completion": (
                        "uncertain"
                        if isinstance(exc, FlashFinalResetUncertain)
                        else "failed_postcondition"
                    ),
                    **(
                        {"snapshot_cleanup_diagnostic": cleanup_diagnostic}
                        if cleanup_diagnostic
                        else {}
                    ),
                },
                board_id=board_id,
                session=runtime,
            )
            raise
        except Exception as exc:
            cleanup_staged_snapshot()
            services.record_event(
                tool_name,
                args,
                outcome_kind=ToolOutcome.FAILED,
                error_code=services.error_code(exc),
                duration_ms=services.duration_ms(started),
                details={
                    "message": str(exc),
                    **(
                        {"snapshot_cleanup_diagnostic": cleanup_diagnostic}
                        if cleanup_diagnostic
                        else {}
                    ),
                },
                board_id=board_id,
                session=runtime,
            )
            raise
        finally:
            cleanup_staged_snapshot()
        services.record_event(
            tool_name,
            args,
            outcome_kind=ToolOutcome.SUCCESS,
            error_code=None,
            duration_ms=services.duration_ms(started),
            details={
                "target_state": flashed.final_reset_postcondition,
                "byte_count": flashed.byte_count,
                "verified_ranges": flashed.verified_ranges,
                "expected_sha256": flashed.expected_sha256,
                "observed_sha256": flashed.observed_sha256,
                "final_reset_postcondition": flashed.final_reset_postcondition,
                **(
                    {"snapshot_cleanup_diagnostic": cleanup_diagnostic}
                    if cleanup_diagnostic
                    else {}
                ),
            },
            board_id=board_id,
            session=runtime,
        )
        return wrap_layer2_response(
            f"Flashed {flashed.firmware_path} as {tool_name}; byte readback verified "
            f"{flashed.byte_count} byte(s) across {len(flashed.verified_ranges)} range(s), "
            f"expected_sha256={flashed.expected_sha256}, observed_sha256={flashed.observed_sha256}, "
            f"final_reset_postcondition={flashed.final_reset_postcondition}."
        )

    def flash_firmware(
        board_id: str,
        firmware_path: str,
        flash_role: Literal["application", "bootloader", "full-device", "sensitive"],
        halt_after_reset: bool = False,
        artifact_target_evidence_path: str | None = None,
    ) -> str:
        """**What** Program one ELF, AXF, or Intel HEX image and read back every byte.

        **When** Use after current capability-aware identity observation and a board connection.

        **Parameters** `board_id` is the board; `firmware_path` is a local `.elf`, `.axf`, or
        `.hex` path (for example `"build/fw.elf"`); `flash_role` is exactly `application`,
        `bootloader`, `full-device`, or `sensitive`; `halt_after_reset` requests a halted final
        reset; optional `artifact_target_evidence_path` is exact artifact-bound target metadata.

        **Returns** Artifact identity, verified ranges, byte count, expected/observed SHA-256,
        and final-reset evidence.

        **Failures and recovery** Identity, writable-flash, readback, or final-reset failures are
        explicit; inspect the evidence, reconnect with `connect_board`, and retry after correction.
        """
        return execute(
            "flash_firmware",
            board_id,
            firmware_path,
            flash_role,
            halt_after_reset,
            artifact_target_evidence_path,
        )

    return {"flash_firmware": flash_firmware}
