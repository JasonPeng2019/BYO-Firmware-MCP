"""Minimal MCP server exposing pyOCD debug control to an LLM client.

Design notes
------------
* Debug sessions are *stateful* (halt state, breakpoints, and live target
  connections persist across calls), so each logical board owns one explicit
  connection until it is disconnected.
* pyOCD's target access is blocking and **not thread-safe**. Each live session
  therefore has one serialized worker process.
* Request cancellation terminates only its owned provider worker without
  poisoning another board or the MCP transport.
"""

from __future__ import annotations

import argparse
import io
import inspect
import json
import os
import hashlib
import re
import secrets
import shlex
import subprocess
import sys
import time
import unicodedata
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import replace
from functools import wraps
from pathlib import Path
from typing import Any, Literal, cast

from mcp.server.fastmcp import Context
from pydantic import BaseModel, ConfigDict
from pyocd.target.pack.cmsis_pack import CmsisPack  # type: ignore[import-untyped]

from firmware_mcp.adapters.debug_interface import (
    RecoveryCapability,
    RecoveryResult,
    TargetSessionHandle,
    session_metadata,
)
from firmware_mcp.board_config import (
    BoardConfig,
    ConfigError,
    load_board_configs_from_paths,
    select_boards_by_id,
)
from firmware_mcp.firmstore.cache import (
    AttachmentCache,
    CacheResolution,
    ProbeIdentity,
    SerialEndpoint,
)
from firmware_mcp.firmstore.profiles import BoardProfile, ProfileError, ProfileRepository
from firmware_mcp.firmstore.reports import ReportWriter
from firmware_mcp.firmstore.store import FirmStore
from firmware_mcp.firmstore.safety_lock import safety_publication_lock
from firmware_mcp.guardrails.core import ActionSpec, GuardCore, GuardError
from firmware_mcp.kernel.registry import RegistryFastMCP
from firmware_mcp.kernel.operations import (
    ManagedOperation,
    cancellation_checkpoint,
    current_operation,
)
from firmware_mcp.kernel.hygiene import require_clean_startup
from firmware_mcp.kernel.processes import run_owned
from firmware_mcp.kernel.run_state import create_server_run
from firmware_mcp.pack_provision import (
    PackProvisionError,
    load_manifest,
    read_pack_bytes,
    sha256_bytes,
    verified_pack_for_target,
)
from firmware_mcp.probe_inventory import (
    list_connected_probes,
    resolve_probe_for_board_cli,
)
from firmware_mcp.serial_resolver import (
    BoardLike,
    ProbeLike,
    SerialPortInfo,
    list_serial_ports,
    resolve_serial_port,
)
from firmware_mcp.services.session_runtime import (
    ActionContext,
    InMemorySessionStore,
    InvalidRequestError,
    SessionRecord,
    ToolEvent,
    ToolOutcome,
    utc_now_text,
)
from firmware_mcp.services import target_control
from firmware_mcp.services.physical_memory import (
    PhysicalMemoryAccessError,
    require_live_physical_access,
)
from firmware_mcp.services.live_identity import (
    LiveIdentityContradiction,
    LiveIdentityObservationError,
    observe_live_identity,
)
from firmware_mcp.services.safety_authority import (
    SafetyAuthority,
    SafetyAuthorityError,
    build_document,
    map_digest,
    validate_document,
)
from firmware_mcp.safety.linker import (
    LinkerEvidenceError,
    executable_elf_ranges,
    parse_flash_image_bytes,
)
from firmware_mcp.services.symbols import (
    find_symbols,
    read_symbol_u32 as read_symbol_u32_from_elf,
    resolve_symbol,
)
from firmware_mcp.services.uart_capture import (
    capture_uart_output,
    exchange_uart_output,
    write_uart_output,
)
from firmware_mcp.setup_flow.preflight import (
    PreflightBlock,
    PreflightSelections,
    PreflightInventory,
    ProbeCandidate,
    SerialCandidate,
    SetupUserInput,
)
from firmware_mcp.setup_flow.packs import (
    PackCandidate,
    PackCandidateError,
    PackCandidatePipeline,
)
from firmware_mcp.setup_flow.research import (
    ResearchError,
    ResearchTracker,
    ValidationOutcome,
    make_research_request,
)
from firmware_mcp.firmstore.providers import (
    ProviderRecipe,
    ProviderRecipeError,
    ProviderRecipeStore,
    run_inventory as run_provider_inventory,
)
from firmware_mcp.firmstore.provider_lock import provider_recipe_publication_lock
from firmware_mcp.identity_observation import (
    IdentityObservationError,
    validate_identity_observation,
)
from firmware_mcp.setup_flow.device_support import (
    BuiltInTargetGeometryError,
    BuiltInTargetSupportCandidate,
    DeviceSupportCandidate,
    DeviceSupportAuthority,
    derive_candidate_binding,
    live_cpuid_compatibility_proof,
    normalize_part_number,
    resolve_available_pack_support,
    resolve_builtin_target_support,
    resolve_device_support_geometry,
    resolve_persisted_builtin_target_support,
    resolve_persisted_pack_support,
    verified_pack_for_candidate,
)
from firmware_mcp.setup_flow.datasheet_evidence import (
    capture_datasheet_evidence,
)
from firmware_mcp.setup_flow.setup import (
    RunAssignmentStore,
    SetupPhase,
    SetupPhaseContext,
    SetupPhaseOutcome,
    SetupWorkflow,
)
from firmware_mcp.setup_flow.targets import (
    ProfileCommitCoordinator,
    TargetResolutionError,
    TargetResolver,
)
from firmware_mcp.setup_flow.validate import (
    BoardValidator,
    ValidationBackend,
    ValidationInventory,
    ValidationProbe,
    ValidationRequest,
    ValidationSerial,
)
from firmware_mcp.target_errors import (
    FlashFinalResetFailed,
    LockedTargetError,
    ProbeNotFoundError,
    ReferenceArtifactError,
    RecoveryPostDispatchError,
    RecoverySessionFinalizationError,
    SymbolLookupError,
    TargetConnectionError,
    TargetControlError,
    TargetStateError,
    FlashFinalResetUncertain,
    UnsupportedArtifactError,
)
from firmware_mcp.timeouts import subprocess_timeout_stream_text
from firmware_mcp.services.connections import (
    BoardNotConnectedError,
    ConnectionAssignmentError,
    ConnectionManager,
    ManagedConnection,
    stable_connection_identity,
)
from firmware_mcp.tools.artifacts import build_artifact_handlers
from firmware_mcp.tools.build import build_build_handlers
from firmware_mcp.tools.breakpoints import (
    BreakpointToolServices,
    build_breakpoint_handlers,
)
from firmware_mcp.tools.execution import ExecutionToolServices, build_execution_handlers
from firmware_mcp.tools.flash import (
    FlashToolServices,
    build_flash_handlers,
    resolve_flash_request,
)
from firmware_mcp.tools.memory import (
    MemoryToolServices,
    build_memory_handlers,
)
from firmware_mcp.tools.misc import MiscToolServices, build_misc_handlers
from firmware_mcp.tools.registers import (
    RegisterToolServices,
    build_register_handlers,
)
from firmware_mcp.tools.session import SessionToolServices, build_session_handlers
from firmware_mcp.tools.serial import (
    SerialToolServices,
    build_serial_handlers,
)
from firmware_mcp.tools.setup import (
    SetupToolServices,
    build_setup_handlers,
)

# Project state is always rooted at the caller's explicit working project.
# Routing it through ambient environment would make one MCP request depend on
# unrelated process state instead of its selected project.
_project_root = Path.cwd().resolve()

mcp = RegistryFastMCP("byo-firmware-mcp")
tool_registry = mcp.registry
server_run = create_server_run()
assignment_store = RunAssignmentStore(server_run.assignments)

connection_manager = ConnectionManager()
_current_symbol_artifacts: dict[str, tuple[Path, str]] = {}
_session_store = InMemorySessionStore(_project_root / ".firm" / "runs")
NO_BOARD_CONFIG_MESSAGE = (
    "No project board profile is loaded for this session. Run setup for a new board, or pass an "
    "explicit board-config path through the explicit launch override."
)


class _ProbeHint:
    def __init__(self, uid: str) -> None:
        self.uid = uid


def _next_event_id() -> str:
    return f"evt-{secrets.token_hex(6)}"


def _duration_ms(started: float) -> int:
    return max(0, int(round((time.monotonic() - started) * 1000)))


def _jsonable_args(values: Mapping[str, object]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in values.items():
        if value is None or isinstance(value, (str, int, float, bool)):
            output[key] = value
        elif isinstance(value, Path):
            output[key] = str(value)
        else:
            output[key] = str(value)
    return output


def _error_code(exc: Exception) -> str:
    if isinstance(exc, ProbeNotFoundError):
        return "probe/not-found"
    if isinstance(exc, LockedTargetError):
        return "target/locked"
    if isinstance(exc, TargetConnectionError):
        return "target/connection-failure"
    if isinstance(exc, UnsupportedArtifactError):
        return "flash/unsupported-artifact"
    if isinstance(exc, ReferenceArtifactError):
        return "flash/reference-artifact"
    if isinstance(exc, SymbolLookupError):
        return "symbols/lookup-failure"
    if isinstance(exc, BoardNotConnectedError):
        return "server/not-connected"
    return f"runtime/{type(exc).__name__}"


def _connection(board_id: str) -> ManagedConnection:
    return connection_manager.connection_for(board_id)


def _runtime_for(board_id: str) -> SessionRecord | None:
    connection = connection_manager.maybe_connection(board_id)
    return connection.runtime_session if connection is not None else None


def _active_session_id(board_id: str) -> str | None:
    runtime = _runtime_for(board_id)
    return runtime.session_id if runtime is not None else None


def _supported_registers_for(board_id: str) -> tuple[str, ...]:
    with connection_manager.lock_for(board_id):
        return target_control.supported_core_registers(_handle(board_id))


def _require_physical_access(
    handle: TargetSessionHandle, address: int, length: int, access: str
) -> object:
    """Establish current-session physical evidence for one raw target span."""

    return require_live_physical_access(
        handle,
        address,
        length,
        access,
        regions_for=target_control.physical_memory_regions,
        read_memory=target_control.read_memory,
    )


def _require_safety_access(
    handle: TargetSessionHandle,
    address: int,
    length: int,
    access: str,
    *,
    roles: set[str] | None = None,
    allow_unknown_read: bool = False,
) -> object:
    authority = globals().get("_safety_authority")
    if authority is None:
        raise SafetyAuthorityError(
            "Safety-map authority is unavailable; refresh_safety_map after connecting."
        )
    board = handle.board
    if board is None:
        raise SafetyAuthorityError(
            "Current session has no board identity; reconnect and validate before map-bound work."
        )
    return authority.require(
        board.board_id,
        handle,
        address,
        length,
        access,
        roles=roles,
        allow_unknown_read=allow_unknown_read,
    )


def _masked_register_write(
    board_id: str,
    address: int,
    mask: int,
    value: int,
    verify: bool = True,
) -> str:
    with connection_manager.lock_for(board_id):
        normalized_args = {
            "board_id": board_id,
            "address": address,
            "mask": mask,
            "value": value,
        }

        def operation() -> str:
            handle = _handle(board_id)
            _require_physical_access(handle, address, 4, "write")
            _require_safety_access(
                handle,
                address,
                4,
                "write",
                roles={"peripheral"},
            )
            full_mask = 0xFFFFFFFF
            if mask == full_mask and not verify:
                updated = value & full_mask
                target_control.write_memory(handle, address, updated, 32)
                return (
                    f"Peripheral register 0x{address:08X}: provider accepted full write "
                    f"0x{updated:08X}; verification=not_requested."
                )
            try:
                _require_safety_access(handle, address, 4, "read", roles={"peripheral"})
            except PhysicalMemoryAccessError as exc:
                if mask != full_mask:
                    raise TargetControlError(
                        "Partial peripheral-register writes require live readable access for "
                        "read-modify-write. Use full mask 0xFFFFFFFF with verify=false when the "
                        "provider reports a write-only register."
                    ) from exc
                raise TargetControlError(
                    "Verified peripheral-register writes require live readable access for readback; "
                    "use full mask 0xFFFFFFFF with verify=false when the provider reports a "
                    "write-only register."
                ) from exc
            prior = target_control.read_memory(handle, address, 32)
            updated = (prior & ~mask) | (value & mask)
            target_control.write_memory(handle, address, updated, 32)
            if not verify:
                return (
                    f"Peripheral register 0x{address:08X}: provider accepted write "
                    f"0x{updated:08X} with mask 0x{mask:08X}; verification=not_requested."
                )
            observed = target_control.read_memory(handle, address, 32)
            if (observed & mask) != (updated & mask):
                raise TargetStateError(
                    f"Peripheral register 0x{address:08X} readback mismatch in mask 0x{mask:08X}: "
                    f"expected 0x{updated & mask:08X}, observed 0x{observed & mask:08X}. "
                    "Retry only after confirming whether the register is volatile or clear-on-write."
                )
            return (
                f"Peripheral register 0x{address:08X}: 0x{prior:08X} -> "
                f"0x{updated:08X} with mask 0x{mask:08X}; verification=matched "
                f"(observed=0x{observed:08X})."
            )

        return _run_logged_tool(board_id, "write_peripheral_register", normalized_args, operation)


def _action_context(tool_name: str, board_id: str) -> ActionContext:
    return ActionContext(
        source="server",
        action_name=tool_name,
        session_id=_active_session_id(board_id),
    )


def _record_event(
    tool_name: str,
    normalized_args: Mapping[str, object],
    *,
    outcome_kind: ToolOutcome,
    error_code: str | None,
    duration_ms: int,
    details: dict[str, object] | None = None,
    board_id: str,
    session: SessionRecord | None = None,
    probe_uid: str | None = None,
    route_used: str | None = None,
) -> ToolEvent:
    runtime = session
    event = ToolEvent(
        event_id=_next_event_id(),
        session_id=runtime.session_id if runtime is not None else None,
        timestamp=utc_now_text(),
        tool_name=tool_name,
        board_id=board_id,
        probe_uid=probe_uid
        if probe_uid is not None
        else (runtime.probe_uid if runtime is not None else None),
        route_used=route_used
        if route_used is not None
        else (runtime.route_used if runtime is not None else None),
        normalized_args=_jsonable_args(normalized_args),
        outcome_kind=outcome_kind,
        error_code=error_code,
        duration_ms=duration_ms,
        details=details or {},
    )
    if runtime is None:
        _session_store.append_global_event(event)
    else:
        _session_store.append_event(runtime, event)
    return event


def _format_invalid_request(error: InvalidRequestError, *, session_id: str | None) -> str:
    return f"Invalid request [{error.code}]: {error.message} session_id={session_id or '(none)'}"


def _report_invalid_argument(
    tool_name: str,
    normalized_args: Mapping[str, object],
    *,
    code: str,
    message: str,
    started: float,
    board_id: str,
    session: SessionRecord | None,
) -> str:
    error = InvalidRequestError(code, message)
    _record_event(
        tool_name,
        normalized_args,
        outcome_kind=ToolOutcome.INVALID,
        error_code=error.code,
        duration_ms=_duration_ms(started),
        details={"message": error.message},
        board_id=board_id,
        session=session,
    )
    return _format_invalid_request(error, session_id=_active_session_id(board_id))


def _parse_int(text: str) -> int:
    """Parse an int from a string, accepting hex (0x...), binary, or decimal."""
    return int(text, 0)


def _word_size_is_valid(word_size: int) -> bool:
    return word_size in {8, 16, 32}


def _run_cmd(
    cmd: list[str],
    timeout_seconds: float | None = None,
) -> tuple[int, str, str]:
    try:
        result = run_owned(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout_seconds=timeout_seconds,
        )
    except FileNotFoundError:
        executable = cmd[0] if cmd else "<unknown>"
        return 127, "", f"command not found: {executable}"
    except subprocess.TimeoutExpired as exc:
        return (
            124,
            subprocess_timeout_stream_text(exc.stdout),
            f"command timed out after {timeout_seconds:.0f}s: {' '.join(cmd)}",
        )
    return result.returncode, result.stdout or "", result.stderr or ""


def resolve_board_config(
    board_id: str | None,
    board_config: str | None,
) -> BoardConfig | None:
    """Resolve a project-local profile or an explicit config file."""
    bid = (board_id or "").strip()
    if not bid:
        return None
    extra = board_config or None
    if extra is None:
        repository = globals().get("_profile_repository")
        if isinstance(repository, ProfileRepository):
            try:
                return repository.load(bid).board
            except ProfileError:
                pass
        raise ConfigError(
            f"Board profile '{bid}' was not found in the configured artifact root. "
            "Run board setup first."
        )
    boards = select_boards_by_id(load_board_configs_from_paths([Path(extra)]), [bid])
    return boards[0]


def format_board_info(b: BoardConfig) -> str:
    """Render a loaded board definition's facts as a stable text block."""
    lines = [
        f"board_id: {b.board_id}",
        f"display_name: {b.display_name}",
        f"mcu_family: {b.mcu_family}",
        f"probe_family: {b.probe_family}",
        f"target: {b.target}",
        f"default_baudrate: {b.default_baudrate}",
        (
            f"test_read_address: 0x{b.test_addr:08X}"
            if b.test_addr is not None
            else "test_read_address: (not configured)"
        ),
    ]
    if b.silicon_id_addr is not None and b.silicon_id_expected is not None:
        width_nibbles = b.silicon_id_width_bits // 4
        lines.append(
            f"silicon_id: addr=0x{b.silicon_id_addr:08X} "
            f"expected=0x{b.silicon_id_expected:0{width_nibbles}X} "
            f"({b.silicon_id_label or 'silicon identity'})"
        )
    if b.uart_note:
        lines.append(f"uart_note: {b.uart_note}")
    return "\n".join(lines)


def _resolve_probe_uid_for_connect(
    board: BoardConfig | None,
    unique_id: str | None,
) -> str | None:
    if unique_id is not None:
        return unique_id
    if board is None:
        return None

    resolution = resolve_probe_for_board_cli(
        board,
        run_cmd=_run_cmd,
        allow_single_fallback=True,
    )
    if resolution.probe is None:
        reroute = (
            " Rerun setup routing to choose the current physical connection."
            if resolution.probes
            else ""
        )
        raise RuntimeError(
            f"Probe resolution failed for {board.display_name}: {resolution.note}.{reroute}"
        )
    if not resolution.probe.uid:
        remedy = "Supply the explicit probe ID to connect_board."
        raise RuntimeError(
            f"Probe resolution for {board.display_name} did not yield a unique id. {remedy}"
        )
    return resolution.probe.uid


def _assigned_probe_uid_for_connect(board_id: str) -> str | None:
    """Return this run's explicit setup probe UID, never a broad-profile fallback.

    Assumption: get_setup_overview's assignment is the user's current physical binding
    for this server run.  Normal connect must honor it; reconnecting a missing
    binding must fail rather than silently selecting a similarly described probe.
    """

    assigned = assignment_store.connection_for(board_id)
    if assigned is None:
        return None
    candidate = assigned.split(":", 1)[1] if assigned.casefold().startswith("probe:") else assigned
    inventory = _validation_inventory()
    if not any(_connection_matches_probe(candidate, probe) for probe in inventory.probes):
        raise RuntimeError(
            f"The assigned probe for {board_id} is no longer present; rerun setup routing "
            "to choose the current physical connection."
        )
    return candidate


def _handle(board_id: str) -> TargetSessionHandle:
    """Return the named board's live session handle or raise if disconnected."""

    return connection_manager.handle_for(board_id)


def _maybe_handle(board_id: str) -> TargetSessionHandle | None:
    connection = connection_manager.maybe_connection(board_id)
    return connection.handle if connection is not None else None


def _promote_open_session(
    board_id: str,
    handle: TargetSessionHandle,
    *,
    commit_operation: bool = False,
) -> ManagedConnection:
    """Atomically promote one newly opened worker into the board connection table.

    The caller transfers ownership of ``handle`` on entry.  Promotion commits only
    after the runtime record and exact assignment succeed.  Public connect
    routes additionally linearize that publication with their request's
    completion decision; validation uses the same rollback transaction but
    remains interruptible through its subsequent observations.
    Any failure rolls back only this transaction's assignment and always attempts to
    release both the runtime record and worker.  Cleanup diagnostics are chained as
    the cause of the unchanged primary exception, which keeps the package's Python
    3.10 contract without hiding either failure.
    """

    runtime: SessionRecord | None = None
    assignment: ManagedConnection | None = None
    try:
        metadata = session_metadata(handle)
        connection_id = stable_connection_identity(handle)
        runtime = _session_store.start_session(
            board_id=board_id,
            connection_id=connection_id,
            probe_uid=metadata.probe_uid,
            route_used=metadata.route_used,
        )
        assignment = connection_manager.assign(
            board_id,
            handle,
            runtime,
            connection_id=connection_id,
        )

        # The provider worker belonged to the connect request through its ready
        # handshake and open call.  The operation state lock makes the final
        # detach and completion decision one outcome: a re-entrant cancellation
        # from promotion is observed by commit_completion's post-action
        # checkpoint and rolls this transaction back; a later cancellation sees
        # the committed result and cannot kill the long-lived worker.
        def promote_worker() -> None:
            promotion = getattr(handle.worker, "promote_to_session", None)
            if callable(promotion):
                promotion()

        operation = current_operation() if commit_operation else None
        if operation is not None:
            operation.commit_completion(promote_worker)
        else:
            promote_worker()
        return assignment
    except BaseException as primary:
        cleanup_errors: list[str] = []
        if assignment is not None:
            try:
                connection_manager.clear_if_current(board_id, assignment)
            except BaseException as exc:  # continue releasing every owned resource
                cleanup_errors.append(f"assignment rollback: {type(exc).__name__}: {exc}")
        if runtime is not None:
            try:
                _session_store.close_session(runtime)
            except BaseException as exc:  # the worker still must be released
                cleanup_errors.append(f"runtime close: {type(exc).__name__}: {exc}")
        try:
            target_control.close_session(handle)
        except BaseException as exc:  # preserve primary and report cleanup uncertainty
            cleanup_errors.append(f"worker close: {type(exc).__name__}: {exc}")
        if cleanup_errors:
            cleanup = RuntimeError(
                "Post-open promotion cleanup reported: " + "; ".join(cleanup_errors)
            )
            raise primary from cleanup
        raise


def _connect_impl(
    board_id: str,
    unique_id: str | None = None,
    target: str | None = None,
    board_config: str | None = None,
) -> str:
    """Assign one connected probe session to the required logical board.

    Args:
        board_id: Required logical board identity. It selects the project-local
            profile previously created by setup.
        unique_id: Whole or partial probe serial/unique ID for the explicit override path.
        target: Target type override. It must agree with the stored verified
            provider support selected during setup.
        board_config: Internal stored-profile route only; public connect_board
            rejects external board-config paths.
    """
    with connection_manager.lock_for(board_id):
        started = time.monotonic()
        normalized_args: dict[str, object] = {
            "unique_id": unique_id,
            "target": target,
            "board_id": board_id,
            "board_config": board_config,
        }
        existing = connection_manager.maybe_connection(board_id)
        if existing is not None:
            result = f"Board '{board_id}' is already connected."
            _record_event(
                "connect_board",
                normalized_args,
                outcome_kind=ToolOutcome.SUCCESS,
                error_code=None,
                duration_ms=_duration_ms(started),
                details={"status": "already-connected"},
                board_id=board_id,
                session=existing.runtime_session,
            )
            return result

        board = None
        stored_profile: BoardProfile | None = None
        uid = None
        tgt = None
        try:
            board = resolve_board_config(board_id, board_config)
            if board is None:
                raise ConfigError("connect_board requires a non-empty stored board_id.")
            tgt = target or board.target or None
            try:
                stored_profile = _profile_repository.load(board_id)
            except ProfileError as exc:
                if _profile_repository.store.layout.board_profile(board_id).is_file():
                    if board is not None and board.provider_id != "pyocd":
                        raise TargetConnectionError(
                            "Stored provider profile cannot be replayed with the current recipe; "
                            "rerun setup_board."
                        ) from exc
                    raise
                selected_pack = None
                selected_pdsc_device = None
            else:
                selected_pack = _verified_pack_for_profile(stored_profile)
                selected_pdsc_device = (
                    stored_profile.device_support["pdsc_device"]
                    if selected_pack is not None and stored_profile.device_support is not None
                    else None
                )
                if selected_pack is not None:
                    if (
                        target is not None
                        and target.casefold() != stored_profile.board.target.casefold()
                    ):
                        raise ValueError(
                            "generic profile target override must match its exact PDSC target"
                        )
                    tgt = stored_profile.board.target
            uid, worker_argv = _resolve_current_connection_for_connect(
                board_id,
                board,
                stored_profile,
                unique_id,
            )
            handle = target_control.open_session(
                board=board,
                unique_id=uid,
                target=tgt,
                pack_path=(selected_pack.path if selected_pack is not None else None),
                pack_sha256=(selected_pack.spec.sha256 if selected_pack is not None else None),
                pdsc_device=selected_pdsc_device,
                worker_argv=worker_argv,
            )
        except Exception as exc:  # noqa: BLE001 - preserve the original connect error
            _record_event(
                "connect_board",
                normalized_args,
                outcome_kind=ToolOutcome.FAILED,
                error_code=_error_code(exc),
                duration_ms=_duration_ms(started),
                details={"message": str(exc)},
                board_id=board_id,
                probe_uid=uid,
                route_used=None,
            )
            raise

        unpromoted_handle: TargetSessionHandle | None = handle
        try:
            _require_current_provider_identity(stored_profile, handle)
            # `_promote_open_session` consumes the handle on every path and
            # performs assignment/runtime/worker rollback itself.
            unpromoted_handle = None
            assignment = _promote_open_session(board_id, handle, commit_operation=True)
        except Exception as exc:
            cleanup_errors: list[str] = []
            if unpromoted_handle is not None:
                try:
                    target_control.close_session(unpromoted_handle)
                except BaseException as cleanup_exc:
                    cleanup_errors.append(
                        f"worker close: {type(cleanup_exc).__name__}: {cleanup_exc}"
                    )
            if exc.__cause__ is not None:
                cleanup_errors.append(f"promotion cleanup: {exc.__cause__}")
            details: dict[str, object] = {"message": str(exc)}
            if cleanup_errors:
                details["cleanup_diagnostics"] = cleanup_errors
            _record_event(
                "connect_board",
                normalized_args,
                outcome_kind=ToolOutcome.FAILED,
                error_code=(
                    "connection/already-assigned"
                    if isinstance(exc, ConnectionAssignmentError)
                    else _error_code(exc)
                ),
                duration_ms=_duration_ms(started),
                details=details,
                board_id=board_id,
                probe_uid=session_metadata(handle).probe_uid,
            )
            if cleanup_errors:
                raise exc from RuntimeError("; ".join(cleanup_errors))
            raise
        runtime_session = assignment.runtime_session
        suffix = f" [board config: {board.board_id}]" if board else ""
        metadata = session_metadata(handle)
        board_name = metadata.board_name or "<unknown>"
        result = (
            f"Connected to board '{board_name}' via probe "
            f"{metadata.probe_uid or '(unknown)'} via {metadata.route_used}.{suffix} "
            f"session_id={runtime_session.session_id}"
        )
        _record_event(
            "connect_board",
            normalized_args,
            outcome_kind=ToolOutcome.SUCCESS,
            error_code=None,
            duration_ms=_duration_ms(started),
            details={"board_name": board_name},
            board_id=board_id,
            session=runtime_session,
        )
        return result


def connect(board_id: str) -> str:
    """Connect using only the named schema-v2 project profile.

    Normal connection accepts no manual probe, target, or external board-config override.
    This internal implementation is composed by connect_board.
    """

    return _connect_impl(board_id)


def _connect_with_wired_reset_impl(
    board_id: str,
    probe_id: str | None,
    target: str | None,
    board_config_path: str | None,
) -> str:
    """Open and assign one session using physical reset-line attach."""

    with connection_manager.lock_for(board_id):
        started = time.monotonic()
        normalized_args: dict[str, object] = {
            "board_id": board_id,
            "probe_id": probe_id,
            "target": target,
            "board_config_path": board_config_path,
        }
        if connection_manager.maybe_connection(board_id) is not None:
            return _report_invalid_argument(
                "connect_board",
                normalized_args,
                code="connection/already-active",
                message=f"Board '{board_id}' is already connected; disconnect it first.",
                started=started,
                board_id=board_id,
                session=_runtime_for(board_id),
            )
        # Public guarded calls have already bound this route to the persisted
        # profile and assignment.  Keep the raw implementation on that same
        # route as well: an external board config is a diagnostic input, not
        # authority for a planned connection.
        if board_config_path is not None:
            raise ConfigError(
                "connect_board uses the stored board profile; board_config_path is not accepted."
            )
        stored_profile: BoardProfile | None = None
        board = resolve_board_config(board_id, None)
        if board is None:
            raise ConfigError("connect_board requires a non-empty stored board_id.")
        resolved_target = target or board.target
        try:
            stored_profile = _profile_repository.load(board_id)
        except ProfileError as exc:
            if _profile_repository.store.layout.board_profile(board_id).is_file():
                if board is not None and board.provider_id != "pyocd":
                    raise TargetConnectionError(
                        "Stored provider profile cannot be replayed with the current recipe; "
                        "rerun setup_board."
                    ) from exc
                raise
            selected_pack = None
            selected_pdsc_device = None
        else:
            selected_pack = _verified_pack_for_profile(stored_profile)
            selected_pdsc_device = (
                stored_profile.device_support["pdsc_device"]
                if selected_pack is not None and stored_profile.device_support is not None
                else None
            )
            if selected_pack is not None:
                if (
                    target is not None
                    and target.casefold() != stored_profile.board.target.casefold()
                ):
                    raise ValueError(
                        "generic profile target override must match its exact PDSC target"
                    )
                resolved_target = stored_profile.board.target
        resolved_uid, worker_argv = _resolve_current_connection_for_connect(
            board_id,
            board,
            stored_profile,
            probe_id,
        )
        handle: TargetSessionHandle | None = None
        try:
            handle = target_control.connect_under_reset(
                board=board,
                unique_id=resolved_uid,
                target=resolved_target,
                pack_path=(selected_pack.path if selected_pack is not None else None),
                pack_sha256=(selected_pack.spec.sha256 if selected_pack is not None else None),
                pdsc_device=selected_pdsc_device,
                worker_argv=worker_argv,
            )
            _require_current_provider_identity(stored_profile, handle)
            promote_handle = handle
            handle = None  # promotion owns worker/runtime/assignment cleanup
            assignment = _promote_open_session(
                board_id,
                promote_handle,
                commit_operation=True,
            )
            runtime = assignment.runtime_session
            connected_route = session_metadata(promote_handle).route_used
        except Exception as exc:  # noqa: BLE001 - preserve typed backend failure
            cleanup_errors: list[str] = []
            if handle is not None:
                try:
                    target_control.close_session(handle)
                except BaseException as cleanup_exc:
                    cleanup_errors.append(
                        f"worker close: {type(cleanup_exc).__name__}: {cleanup_exc}"
                    )
            if exc.__cause__ is not None:
                cleanup_errors.append(f"promotion cleanup: {exc.__cause__}")
            details: dict[str, object] = {"message": str(exc)}
            if cleanup_errors:
                details["cleanup_diagnostics"] = cleanup_errors
            _record_event(
                "connect_board",
                normalized_args,
                outcome_kind=ToolOutcome.FAILED,
                error_code=_error_code(exc),
                duration_ms=_duration_ms(started),
                details=details,
                board_id=board_id,
                probe_uid=resolved_uid,
            )
            if cleanup_errors:
                raise exc from RuntimeError("; ".join(cleanup_errors))
            raise
        _record_event(
            "connect_board",
            normalized_args,
            outcome_kind=ToolOutcome.SUCCESS,
            error_code=None,
            duration_ms=_duration_ms(started),
            details={"target_state": "halted", "reset_line_released": True},
            board_id=board_id,
            session=runtime,
        )
        return (
            f"Connected under physical reset to board '{board_id}' via "
            f"{connected_route}; target halted and reset line released."
        )


def disconnect(board_id: str) -> str:
    """Close only the named board's debug session and release its probe."""

    with connection_manager.lock_for(board_id):
        # Disconnect is a real session boundary.  It invalidates only this
        # board's cooperative-user records; the other board remains isolated.
        invalidation_error: GuardError | None = None
        try:
            _guard_core.invalidate_board(board_id, "disconnect")
        except GuardError as error:
            # Runtime grants are already conservatively closed by GuardCore,
            # but the durable invalidation may or may not have published.
            invalidation_error = error
        connection = connection_manager.maybe_connection(board_id)
        if connection is None:
            assignment_store.clear_board(board_id)
            workflow = globals().get("_setup_workflow")
            if isinstance(workflow, SetupWorkflow):
                workflow.cancel_board(board_id, "board disconnected")
            started = time.monotonic()
            result = "Not connected."
            outcome = ToolOutcome.SUCCESS
            error_code: str | None = None
            details: dict[str, object] = {"status": "not-connected"}
            if invalidation_error is not None:
                outcome, error_code = ToolOutcome.FAILED, invalidation_error.code
                details["permission_invalidation"] = "uncertain"
                details["message"] = invalidation_error.message
                result += f" {_guard_error_result(invalidation_error)}"
            _record_event(
                "disconnect_board",
                {"board_id": board_id},
                outcome_kind=outcome,
                error_code=error_code,
                duration_ms=_duration_ms(started),
                details=details,
                board_id=board_id,
            )
            return result

        started = time.monotonic()
        assignment_store.clear_board(board_id)
        workflow = globals().get("_setup_workflow")
        if isinstance(workflow, SetupWorkflow):
            workflow.disconnect(connection.connection_id)
        cleared = connection_manager.clear_if_current(board_id, connection)
        if cleared is None:
            raise RuntimeError(
                f"Board '{board_id}' connection changed while disconnecting; retry disconnect."
            )
        _finish_disconnect_cleanup(
            board_id,
            cleared,
            started=started,
            permission_invalidation_error=invalidation_error,
        )
        result = f"Disconnected board '{board_id}'."
        if invalidation_error is not None:
            result += f" {_guard_error_result(invalidation_error)}"
        return result


def _finish_disconnect_cleanup(
    board_id: str,
    connection: ManagedConnection,
    *,
    started: float,
    permission_invalidation_error: GuardError | None = None,
) -> None:
    """Close both resources and report success only after both are confirmed closed."""

    failures: list[tuple[str, Exception]] = []
    close_evidence: dict[str, object] | None = None
    try:
        close_evidence = target_control.close_session(connection.handle)
    except Exception as exc:  # noqa: BLE001 - runtime cleanup must still run
        failures.append(("worker close", exc))
    try:
        _session_store.close_session(connection.runtime_session)
    except Exception as exc:  # noqa: BLE001 - preserve and report cleanup uncertainty
        failures.append(("runtime close", exc))

    if failures:
        if len(failures) == 1:
            failure = failures[0][1]
        else:
            failure = RuntimeError(
                "Disconnect cleanup reported: "
                + "; ".join(
                    f"{stage}: {type(error).__name__}: {error}" for stage, error in failures
                )
            )
        try:
            _record_event(
                "disconnect_board",
                {"board_id": board_id},
                outcome_kind=ToolOutcome.FAILED,
                error_code=_error_code(failure),
                duration_ms=_duration_ms(started),
                details={"message": str(failure), "worker_close": close_evidence},
                board_id=board_id,
                session=connection.runtime_session,
            )
        except Exception as event_error:  # preserve the resource failure object/type
            raise failure from event_error
        if len(failures) == 1:
            raise failure
        raise failure from failures[0][1]

    _record_event(
        "disconnect_board",
        {"board_id": board_id},
        outcome_kind=(
            ToolOutcome.FAILED if permission_invalidation_error is not None else ToolOutcome.SUCCESS
        ),
        error_code=(permission_invalidation_error.code if permission_invalidation_error else None),
        duration_ms=_duration_ms(started),
        board_id=board_id,
        session=connection.runtime_session,
        details={
            "worker_close": close_evidence,
            **(
                {
                    "permission_invalidation": "uncertain",
                    "message": permission_invalidation_error.message,
                }
                if permission_invalidation_error is not None
                else {}
            ),
        },
    )


def get_board_info(board_id: str) -> str:
    """Return the facts from the board config the session was opened with.

    Reports the project-local profile active for this session —
    pyOCD target, MCU and probe family, recover policy, silicon-id expectation,
    default UART baud, and the smoke-test read address. Returns a notice when
    a connection was opened without a ``board_id`` (raw-target mode), where these
    facts were not loaded.
    """
    with connection_manager.lock_for(board_id):

        def operation() -> str:
            connection = connection_manager.maybe_connection(board_id)
            if connection is None:
                return f"Board '{board_id}' is not connected. Call `connect_board` first."
            handle = connection.handle
            b = handle.board
            if b is None:
                return NO_BOARD_CONFIG_MESSAGE
            capabilities = [
                item.to_record() for item in target_control.recovery_capabilities(handle)
            ]
            return (
                format_board_info(b)
                + "\nLive recovery capabilities: "
                + json.dumps(capabilities, sort_keys=True, separators=(",", ":"))
            )

        return _run_logged_tool(board_id, "get_board_info", {"board_id": board_id}, operation)


def _require_loaded_board(handle: TargetSessionHandle) -> BoardConfig:
    if handle.board is None:
        raise RuntimeError(NO_BOARD_CONFIG_MESSAGE)
    return handle.board


def _resolve_serial_port_for_session(
    handle: TargetSessionHandle,
    *,
    override: str | None,
) -> SerialPortInfo:
    board = _require_loaded_board(handle)
    ports = list_serial_ports()
    if ports is None:
        raise RuntimeError("pyserial is not installed")
    if not ports:
        raise RuntimeError("No serial ports detected")

    probe_uid = session_metadata(handle).probe_uid
    probe = _ProbeHint(probe_uid) if probe_uid else None
    resolution = resolve_serial_port(
        board=cast(BoardLike, board),
        ports=ports,
        probe=cast(ProbeLike | None, probe),
        override=override,
        allow_single_fallback=len(ports) == 1,
        run_cmd=_run_cmd,
        interactive=False,
    )
    if resolution.port is None:
        raise RuntimeError(f"Serial port resolution failed: {resolution.note}")
    return resolution.port


def _guard_serial_identity(board_id: str, override: str | None) -> dict[str, str]:
    """Return only an observed, per-device UART identity for a guarded action.

    A port pathname is a selection, not physical identity.  Serial number is
    strongest; USB location is the next provider-reported stable value.  No
    vendor inference or profile-text fallback is used here.
    """

    connection = connection_manager.maybe_connection(board_id)
    if connection is None:
        raise GuardError(
            "guard/serial-identity-unavailable",
            "The board has no current session; connect, detect the UART, and create a new plan.",
        )
    try:
        resolved = _resolve_serial_port_for_session(connection.handle, override=override)
    except RuntimeError as error:
        raise GuardError(
            "guard/serial-identity-unavailable",
            f"The planned UART cannot be resolved: {error}. Re-detect it and create a new plan.",
        ) from error
    serial_number = resolved.serial_number.strip()
    location = resolved.location.strip()
    if serial_number:
        return {"port": resolved.device, "kind": "serial_number", "value": serial_number}
    if location:
        return {"port": resolved.device, "kind": "location", "value": location}
    raise GuardError(
        "guard/serial-identity-unavailable",
        "The resolved UART reports no stable physical identity; re-detect it and create a new plan.",
    )


def _run_logged_tool(
    board_id: str,
    tool_name: str,
    normalized_args: Mapping[str, object],
    operation: Callable[[], str],
) -> str:
    started = time.monotonic()
    runtime = _runtime_for(board_id)
    try:
        result = operation()
    except Exception as exc:  # noqa: BLE001 - preserve the original tool failure
        _record_event(
            tool_name,
            normalized_args,
            outcome_kind=ToolOutcome.FAILED,
            error_code=_error_code(exc),
            duration_ms=_duration_ms(started),
            details={"message": str(exc)},
            board_id=board_id,
            session=runtime,
        )
        raise

    _record_event(
        tool_name,
        normalized_args,
        outcome_kind=ToolOutcome.SUCCESS,
        error_code=None,
        duration_ms=_duration_ms(started),
        board_id=board_id,
        session=runtime,
    )
    return result


def _complete_effect(effect: Callable[[], None], result: str) -> str:
    effect()
    return result


def get_state(board_id: str) -> str:
    """Return the current core run state (e.g. HALTED, RUNNING, RESET)."""
    with connection_manager.lock_for(board_id):

        def operation() -> str:
            return target_control.get_state(_handle(board_id))

        return _run_logged_tool(board_id, "get_target_state", {"board_id": board_id}, operation)


def halt(board_id: str) -> str:
    """Halt the core."""
    with connection_manager.lock_for(board_id):

        def operation() -> str:
            observed = target_control.halt(_handle(board_id))
            return f"Halt command accepted; observed_state={observed}."

        return _run_logged_tool(board_id, "halt_target", {"board_id": board_id}, operation)


def resume(board_id: str) -> str:
    """Resume execution of the core."""
    with connection_manager.lock_for(board_id):

        def operation() -> str:
            observed = target_control.resume(_handle(board_id))
            return f"Resume command accepted; observed_state={observed}."

        return _run_logged_tool(board_id, "resume_target", {"board_id": board_id}, operation)


def step(board_id: str) -> str:
    """Single-step one instruction and return the new program counter."""
    with connection_manager.lock_for(board_id):

        def operation() -> str:
            observed, pc = target_control.step(_handle(board_id))
            return f"Step command accepted; observed_state={observed}; pc=0x{pc:08X}"

        return _run_logged_tool(board_id, "step_target", {"board_id": board_id}, operation)


def reset(board_id: str, halt_after: bool = True) -> str:
    """Reset the target.

    Args:
        halt_after: If True, request ``halt_after_reset=true`` and require an
            observed HALTED postcondition. If False, request normal reset and
            report the immediately observed state as evidence.
    """
    with connection_manager.lock_for(board_id):

        def operation() -> str:
            observed = target_control.reset(_handle(board_id), halt_after=halt_after)
            return (
                "Reset command accepted; "
                f"halt_after_reset={str(halt_after).lower()}; observed_state={observed}."
            )

        return _run_logged_tool(
            board_id,
            "reset_target",
            {"board_id": board_id, "halt_after_reset": halt_after},
            operation,
        )


def read_core_register(board_id: str, name: str) -> str:
    """Read a core register by name (e.g. "pc", "sp", "r0", "xpsr").

    Returns the value as a hex string.
    """
    with connection_manager.lock_for(board_id):

        def operation() -> str:
            return f"0x{target_control.read_core_register(_handle(board_id), name):08X}"

        return _run_logged_tool(
            board_id,
            "read_cpu_register",
            {"board_id": board_id, "name": name},
            operation,
        )


def write_core_register(board_id: str, name: str, value: str, verify: bool = True) -> str:
    """Write a core register by name. ``value`` may be hex (0x...) or decimal."""
    with connection_manager.lock_for(board_id):

        def operation() -> str:
            parsed = _parse_int(value)
            handle = _handle(board_id)
            target_control.write_core_register(handle, name, parsed)
            if not verify:
                return f"Provider accepted write of 0x{parsed:X} to {name}; verification=not_requested."
            observed = target_control.read_core_register(handle, name)
            if observed != parsed:
                raise TargetStateError(
                    f"CPU register {name} readback mismatch: expected 0x{parsed:X}, observed "
                    f"0x{observed:X}. Reconnect and retry after confirming the register is readable."
                )
            return f"Wrote and verified 0x{parsed:X} in {name}."

        return _run_logged_tool(
            board_id,
            "write_cpu_register",
            {"board_id": board_id, "name": name, "value": value},
            operation,
        )


def read_memory(board_id: str, address: str, word_size: int = 32) -> str:
    """Read a single value from memory.

    Args:
        address: Memory address, hex (0x...) or decimal.
        word_size: Transfer size in bits: 8, 16, or 32.
    """
    with connection_manager.lock_for(board_id):
        started = time.monotonic()
        normalized_args = {
            "board_id": board_id,
            "address": address,
            "word_size": word_size,
        }
        if not _word_size_is_valid(word_size):
            return _report_invalid_argument(
                "read_memory",
                normalized_args,
                code="memory/invalid-word-size",
                message="word_size must be one of: 8, 16, 32.",
                started=started,
                board_id=board_id,
                session=_runtime_for(board_id),
            )

        def operation() -> str:
            value = target_control.read_memory(_handle(board_id), _parse_int(address), word_size)
            return f"0x{value:0{word_size // 4}X}"

        return _run_logged_tool(
            board_id,
            "read_memory",
            normalized_args,
            operation,
        )


def read_memory_block(board_id: str, address: str, length: int) -> str:
    """Read ``length`` bytes from memory starting at ``address``.

    Returns the bytes as a space-separated hex string.
    """
    with connection_manager.lock_for(board_id):
        started = time.monotonic()
        normalized_args = {"board_id": board_id, "address": address, "length": length}
        if length <= 0:
            return _report_invalid_argument(
                "read_memory_block",
                normalized_args,
                code="memory/invalid-length",
                message="length must be > 0.",
                started=started,
                board_id=board_id,
                session=_runtime_for(board_id),
            )

        def operation() -> str:
            values = target_control.read_memory_block(
                _handle(board_id), _parse_int(address), length
            )
            return " ".join(f"{byte:02X}" for byte in values)

        return _run_logged_tool(board_id, "read_memory_block", normalized_args, operation)


def read_symbol_u32(board_id: str, elf_path: str, symbol_name: str) -> str:
    """Resolve ``symbol_name`` in ``elf_path`` and read its 32-bit value from target memory."""
    with connection_manager.lock_for(board_id):
        normalized_args = {
            "board_id": board_id,
            "elf_path": elf_path,
            "symbol_name": symbol_name,
        }

        def operation() -> str:
            resolved = read_symbol_u32_from_elf(_handle(board_id), elf_path, symbol_name)
            if resolved.value_u32 is None:  # pragma: no cover - service always populates this field
                raise RuntimeError(
                    f"Resolved symbol '{symbol_name}' did not produce a 32-bit value."
                )
            resolved_path = Path(elf_path).expanduser().resolve()
            return (
                f"Symbol {resolved.name} from {resolved_path} "
                f"@0x{resolved.address:08X} size={resolved.size} type={resolved.type} "
                f"value_u32=0x{resolved.value_u32:08X}"
            )

        return _run_logged_tool(board_id, "read_symbol_u32", normalized_args, operation)


def write_memory(
    board_id: str,
    symbol_or_address: str | int,
    value: object,
    width: int = 32,
    elf_artifact: str | None = None,
    verify: bool = True,
) -> str:
    """Write an explicit symbol using its ELF, or a direct mapped raw address."""

    return memory_tool_handlers["write_memory"](
        board_id,
        symbol_or_address,
        value,
        width,
        elf_artifact,
        verify,
    )


def _symbol_artifact_for_handle(handle: TargetSessionHandle) -> Path:
    if handle.board is None:
        raise ReferenceArtifactError(
            "Symbol access requires a connected board with canonical firmware metadata."
        )
    binding = _current_symbol_artifacts.get(handle.board.board_id)
    if binding is not None:
        artifact, expected_digest = binding
        try:
            actual_digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        except OSError as exc:
            raise ReferenceArtifactError(
                f"Current symbol artifact is unreadable: {artifact}"
            ) from exc
        if actual_digest != expected_digest:
            raise ReferenceArtifactError(f"Current symbol artifact changed after flash: {artifact}")
        return artifact
    raise ReferenceArtifactError(
        "No current-project ELF/AXF is bound in this Server Run. Pass the project's local symbol "
        "artifact as elf_artifact; no implicit firmware is substituted for project "
        "symbols."
    )


def _prepare_flashed_symbol_artifact(
    tool_name: str, board_id: str, artifact: Path
) -> tuple[Path, str]:
    del tool_name, board_id
    if artifact.suffix.casefold() in {".elf", ".axf"}:
        elf_artifact = artifact
    else:
        companions = tuple(
            path for suffix in (".elf", ".axf") if (path := artifact.with_suffix(suffix)).is_file()
        )
        if len(companions) != 1:
            raise ReferenceArtifactError(
                "Cannot bind symbols for HEX flash without exactly one same-stem ELF or AXF."
            )
        elf_artifact = companions[0]
    try:
        resolved = elf_artifact.expanduser().resolve(strict=True)
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    except OSError as exc:
        raise ReferenceArtifactError(
            f"Cannot prepare the current application ELF before flash: {elf_artifact}"
        ) from exc
    return resolved, digest


def _bind_flashed_symbol_artifact(board_id: str, binding: object) -> None:
    if not isinstance(binding, tuple) or len(binding) != 2:
        raise TypeError("Invalid prepared symbol artifact binding.")
    artifact, digest = binding
    if not isinstance(artifact, Path) or not isinstance(digest, str):
        raise TypeError("Invalid prepared symbol artifact binding.")
    _current_symbol_artifacts[board_id] = (artifact, digest)


def set_breakpoint(board_id: str, address: str) -> str:
    """Set a hardware/software breakpoint at ``address``."""
    with connection_manager.lock_for(board_id):

        def operation() -> str:
            return _complete_effect(
                lambda: target_control.set_breakpoint(_handle(board_id), _parse_int(address)),
                f"Breakpoint set at {address}.",
            )

        return _run_logged_tool(
            board_id,
            "set_breakpoint",
            {"board_id": board_id, "address": address},
            operation,
        )


def remove_breakpoint(board_id: str, address: str) -> str:
    """Remove the breakpoint at ``address``."""
    with connection_manager.lock_for(board_id):

        def operation() -> str:
            return _complete_effect(
                lambda: target_control.remove_breakpoint(_handle(board_id), _parse_int(address)),
                f"Breakpoint removed at {address}.",
            )

        return _run_logged_tool(
            board_id,
            "remove_breakpoint",
            {"board_id": board_id, "address": address},
            operation,
        )


def flash_firmware(
    board_id: str,
    path: str | None = None,
    halt_after_reset: bool = False,
) -> str:
    """Flash firmware through the shared target-control service layer.

    Args:
        path: Optional explicit artifact path. When omitted, resolve the default
            flash artifact for the connected session's loaded board config.
            Returns the resolved path in the success text.
        halt_after_reset: If True, leave the target halted after flashing.
            If False, reset and let it run.
    """
    with connection_manager.lock_for(board_id):
        started = time.monotonic()
        runtime = _runtime_for(board_id)
        normalized_args: dict[str, object] = {
            "board_id": board_id,
            "path": path,
            "halt_after_reset": halt_after_reset,
            "artifact_source": "default" if path is None else "explicit",
            "artifact_path": path,
        }
        handle = connection_manager.maybe_connection(board_id)
        pending_handle = handle.handle if handle is not None else None
        try:
            request = resolve_flash_request(
                pending_handle,
                explicit_path=path,
                action_context=_action_context("flash_firmware", board_id),
            )
            active_handle = _handle(board_id)
            normalized_args.update(request.identity.as_log_fields())
            try:
                profile = _profile_repository.load(board_id)
            except ProfileError:
                profile = None
            _require_current_provider_identity(profile, active_handle)
            flashed = target_control.flash_firmware(
                active_handle,
                request.artifact_path,
                halt_after_reset=halt_after_reset,
            )
        except InvalidRequestError as exc:
            _record_event(
                "flash_firmware",
                normalized_args,
                outcome_kind=ToolOutcome.INVALID,
                error_code=exc.code,
                duration_ms=_duration_ms(started),
                details={"message": exc.message},
                board_id=board_id,
                session=runtime,
            )
            return _format_invalid_request(exc, session_id=_active_session_id(board_id))
        except (FlashFinalResetFailed, FlashFinalResetUncertain) as exc:
            evidence = exc.evidence
            _record_event(
                "flash_firmware",
                normalized_args,
                outcome_kind=ToolOutcome.FAILED,
                error_code=_error_code(exc),
                duration_ms=_duration_ms(started),
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
                },
                board_id=board_id,
                session=runtime,
            )
            raise
        except Exception as exc:  # noqa: BLE001 - preserve backend failure text
            _record_event(
                "flash_firmware",
                normalized_args,
                outcome_kind=ToolOutcome.FAILED,
                error_code=_error_code(exc),
                duration_ms=_duration_ms(started),
                details={"message": str(exc)},
                board_id=board_id,
                session=runtime,
            )
            raise

        state = flashed.final_reset_postcondition
        _record_event(
            "flash_firmware",
            normalized_args,
            outcome_kind=ToolOutcome.SUCCESS,
            error_code=None,
            duration_ms=_duration_ms(started),
            details={
                "target_state": state,
                "byte_count": flashed.byte_count,
                "verified_ranges": flashed.verified_ranges,
                "expected_sha256": flashed.expected_sha256,
                "observed_sha256": flashed.observed_sha256,
                "final_reset_postcondition": flashed.final_reset_postcondition,
            },
            board_id=board_id,
            session=runtime,
        )
        return (
            f"Flashed {flashed.firmware_path} via {session_metadata(active_handle).route_used}; "
            f"byte readback verified {flashed.byte_count} byte(s) across "
            f"{len(flashed.verified_ranges)} range(s); expected_sha256={flashed.expected_sha256}; "
            f"observed_sha256={flashed.observed_sha256}; final_reset_postcondition={state}."
        )


def _connect_board(
    board_id: str,
    probe_id: str | None = None,
    target: str | None = None,
    board_config_path: str | None = None,
    under_reset: bool = False,
) -> str:
    """Establish the one final connection route without mutating a profile."""

    if board_config_path is not None:
        raise ConfigError(
            "connect_board uses the stored board profile; board_config_path is not accepted."
        )
    if under_reset:
        return _connect_with_wired_reset_impl(board_id, probe_id, target, None)
    return _connect_impl(board_id, probe_id, target, None)


session_tool_handlers = build_session_handlers(
    SessionToolServices(
        connect=_connect_board,
        disconnect=disconnect,
        get_board_info=get_board_info,
        get_state=get_state,
    )
)
execution_tool_handlers = build_execution_handlers(
    ExecutionToolServices(
        halt=halt,
        resume=resume,
        step=step,
        reset=lambda board_id, halt_after: reset(board_id, halt_after=halt_after),
    )
)
register_services = RegisterToolServices(
    supported_registers=_supported_registers_for,
    read_register=read_core_register,
    write_register=lambda board_id, name, value, verify: write_core_register(
        board_id, name, str(value), verify
    ),
    masked_register_write=_masked_register_write,
)
register_tool_handlers = build_register_handlers(register_services)


memory_services = MemoryToolServices(
    runtime_for=_runtime_for,
    active_session_id=_active_session_id,
    duration_ms=_duration_ms,
    record_event=_record_event,
    format_invalid=_format_invalid_request,
    handle_for=_handle,
    symbol_artifact_for=_symbol_artifact_for_handle,
    find_symbols=lambda artifact, query: find_symbols(artifact, query),
    resolve_symbol=resolve_symbol,
    read_target_memory=target_control.read_memory,
    read_target_block=target_control.read_memory_block,
    write_target_memory=target_control.write_memory,
    check_memory_read=lambda handle, address, size_bytes: _require_safety_access(
        handle, address, size_bytes, "read", allow_unknown_read=True
    ),
    check_memory_write=lambda handle, address, size_bytes: _require_safety_access(
        handle, address, size_bytes, "write", roles={"ordinary_ram"}
    ),
)
memory_tool_handlers = build_memory_handlers(memory_services)


def _resolve_flash_request_for_session(
    handle: TargetSessionHandle | None,
    firmware_path: str,
    context: ActionContext,
) -> object:
    """Resolve the required caller-selected artifact after proving a live session exists."""

    return resolve_flash_request(handle, explicit_path=firmware_path, action_context=context)


def _stage_flash_snapshot(original: Path, payload: bytes) -> Path:
    directory = _project_root / ".firm" / "runs" / server_run.run_id
    directory.mkdir(parents=True, exist_ok=True)
    staged = directory / f"flash-{secrets.token_hex(12)}{original.suffix.lower()}"
    with staged.open("xb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    return staged


def _cleanup_flash_snapshot(path: Path) -> str | None:
    try:
        path.unlink()
    except OSError as exc:
        return f"server-owned flash snapshot retained at {path}: {type(exc).__name__}: {exc}"
    return None


flash_services = FlashToolServices(
    runtime_for=_runtime_for,
    active_session_id=_active_session_id,
    duration_ms=_duration_ms,
    record_event=_record_event,
    format_invalid=_format_invalid_request,
    action_context=_action_context,
    maybe_handle_for=_maybe_handle,
    handle_for=_handle,
    resolve_request=_resolve_flash_request_for_session,
    flash_target=lambda handle, artifact, halt_after_reset: _flash_target_with_replayed_recipe(
        handle, artifact, halt_after_reset
    ),
    error_code=_error_code,
    validate_flash=None,
    prepare_symbol_artifact=_prepare_flashed_symbol_artifact,
    bind_symbol_artifact=_bind_flashed_symbol_artifact,
    execution_file=lambda name: _guard_core.execution_file(name),
    stage_snapshot=_stage_flash_snapshot,
    cleanup_snapshot=_cleanup_flash_snapshot,
)
flash_tool_handlers = build_flash_handlers(flash_services)


def _reset_serial_target(handle: Any) -> None:
    """Adapt reset evidence to the serial service's command-only callback."""

    target_control.reset(handle, halt_after=False)


serial_services = SerialToolServices(
    runtime_for=_runtime_for,
    active_session_id=_active_session_id,
    duration_ms=_duration_ms,
    record_event=_record_event,
    format_invalid=_format_invalid_request,
    handle_for=_handle,
    resolve_port=_resolve_serial_port_for_session,
    capture_uart=lambda *args, **kwargs: capture_uart_output(*args, **kwargs),
    write_uart=lambda *args, **kwargs: write_uart_output(*args, **kwargs),
    exchange_uart=lambda *args, **kwargs: exchange_uart_output(*args, **kwargs),
    reset_target=_reset_serial_target,
    no_board_config_message=NO_BOARD_CONFIG_MESSAGE,
)
serial_tool_handlers = build_serial_handlers(serial_services)


def _check_breakpoint_safety(
    handle: TargetSessionHandle, address: int, elf_path: str | None
) -> object:
    evidence = _require_safety_access(
        handle, address, 1, "execute", roles={"application", "bootloader", "rom"}
    )
    if elf_path is not None:
        payload = _guard_core.execution_file("elf_path")
        if payload is None:
            raise SafetyAuthorityError(
                "Checked ELF snapshot is unavailable; create a new breakpoint plan."
            )
        try:
            if not any(
                start <= address < end
                for start, end in executable_elf_ranges(Path(elf_path), payload)
            ):
                raise SafetyAuthorityError(
                    "Requested breakpoint address is not covered by a file-backed executable PT_LOAD range in the selected ELF."
                )
        except LinkerEvidenceError as exc:
            raise SafetyAuthorityError(
                f"Selected ELF cannot prove executable breakpoint authority: {exc}"
            ) from exc
    return evidence


breakpoint_services = BreakpointToolServices(
    runtime_for=_runtime_for,
    active_session_id=_active_session_id,
    duration_ms=_duration_ms,
    record_event=_record_event,
    format_invalid=_format_invalid_request,
    handle_for=_handle,
    set_target_breakpoint=target_control.set_breakpoint,
    remove_target_breakpoint=target_control.remove_breakpoint,
    check_breakpoint=_check_breakpoint_safety,
)
breakpoint_tool_handlers = build_breakpoint_handlers(breakpoint_services)

misc_tool_handlers = build_misc_handlers(
    MiscToolServices(
        runtime_for=_runtime_for,
        duration_ms=_duration_ms,
        record_event=_record_event,
    )
)
artifact_tool_handlers = build_artifact_handlers()
build_tool_handlers = build_build_handlers()


def refresh_safety_map(
    board_id: str,
    layout_path: str | None = None,
    application_elf_path: str | None = None,
) -> str:
    """**What** Reconcile current provider facts with selected project evidence into the canonical safety map.

    **When** Use after connecting, or whenever map-bound operations report a missing or stale map.

    **Parameters** `board_id` selects the connected board; optional `layout_path` is JSON/YAML
    schema-v1 project evidence; optional `application_elf_path` supplies exact executable PT_LOAD
    evidence. Example: `refresh_safety_map(board_id="board_a", layout_path="layout.yaml", application_elf_path="build/app.elf", plan_id="...")`.

    **Returns** The canonical map digest, `changed` state, and the exact project-relative profile association.

    **Failures and recovery** Missing live identity, conflicting evidence, or changed selected bytes stop before publication; reconnect/validate or create a new plan with corrected evidence.
    """
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("PyYAML is required to parse a selected safety layout") from exc
    with connection_manager.lock_for(board_id):
        with safety_publication_lock(_project_root, board_id):
            handle = _handle(board_id)
            layout: Mapping[str, Any] | None = None
            if layout_path is not None:
                payload = _guard_core.execution_file("layout_path")
                if payload is None:
                    raise SafetyAuthorityError(
                        "Guarded layout snapshot is unavailable; create a new refresh plan."
                    )
                try:
                    parsed = yaml.safe_load(payload)
                except yaml.YAMLError as exc:
                    raise SafetyAuthorityError(f"Selected layout is malformed: {exc}") from exc
                if not isinstance(parsed, Mapping):
                    raise SafetyAuthorityError("Selected layout must decode to an object.")
                layout = parsed
            application: tuple[Path, bytes] | None = None
            if application_elf_path is not None:
                payload = _guard_core.execution_file("application_elf_path")
                if payload is None:
                    raise SafetyAuthorityError(
                        "Guarded ELF snapshot is unavailable; create a new refresh plan."
                    )
                application = (Path(application_elf_path), payload)
            layout_sources: dict[str, bytes] | None = None
            if layout is not None:
                layout_sources = {}
                regions = layout.get("regions")
                if isinstance(regions, list):
                    for index, fact in enumerate(regions):
                        if isinstance(fact, Mapping) and isinstance(fact.get("source_path"), str):
                            source_payload = _guard_core.execution_file(f"layout_source:{index}")
                            if source_payload is None:
                                raise SafetyAuthorityError(
                                    "Guarded layout evidence snapshot is unavailable; create a new refresh plan."
                                )
                            layout_sources[
                                str(Path(fact["source_path"]).expanduser().resolve())
                            ] = source_payload
            profile = _profile_repository.load(board_id)
            support_geometry: Mapping[str, Any] | None = None
            support_identity: str | None = None
            if (
                profile.device_support is not None
                and profile.device_support.get("kind") != "provider_recipe"
            ):
                candidate_support = _replay_profile_device_support(profile)
                support_geometry = resolve_device_support_geometry(
                    candidate_support, _profile_repository.store
                ).to_document()
                support_identity = str(profile.device_support["support_id"])
            candidate = build_document(
                board_id=board_id,
                handle=handle,
                regions_for=target_control.physical_memory_regions,
                layout=layout,
                layout_source_payloads=layout_sources,
                application_elf=application,
                read_memory=target_control.read_memory,
                support_geometry=support_geometry,
                support_identity=support_identity,
                configured_part_number=profile.mcu_part_number,
            )
            if layout_path is not None:
                layout_bytes = _guard_core.execution_file("layout_path")
                assert layout_bytes is not None
                candidate["sources"].append(
                    {
                        "kind": "layout",
                        "identifier": (
                            "layout-document:" + str(Path(layout_path).expanduser().resolve())
                        ),
                        "sha256": hashlib.sha256(layout_bytes).hexdigest(),
                        "detail": "selected schema-v1 layout",
                    }
                )
                candidate["digest"] = map_digest(candidate)
                candidate = validate_document(candidate, board_id=board_id)
            destination = _safety_authority.path_for(board_id)
            old: dict[str, Any] | None = None
            try:
                old = _safety_authority.load(board_id)
            except SafetyAuthorityError:
                pass
            if old is not None and old["digest"] == candidate["digest"]:
                return json.dumps(
                    {
                        "board_id": board_id,
                        "changed": False,
                        "digest": candidate["digest"],
                        "safety_ref": _profile_repository.load(board_id).safety_ref,
                    },
                    sort_keys=True,
                )
            reference = destination.relative_to(_project_root).as_posix()
            staged = _profile_repository.stage_safety_ref(board_id, reference)
            payload = (
                json.dumps(candidate, sort_keys=True, indent=2, ensure_ascii=False).encode("utf-8")
                + b"\n"
            )
            _profile_repository.publish_safety_map(staged, destination, payload)
            _guard_core.invalidate_board(board_id, "refresh-safety-map-changed")
            return json.dumps(
                {
                    "board_id": board_id,
                    "changed": True,
                    "old_digest": old["digest"] if old else None,
                    "digest": candidate["digest"],
                    "safety_ref": reference,
                },
                sort_keys=True,
            )


_guarded_risks: dict[str, tuple[str, str, bool, bool]] = {
    "refresh_safety_map": ("routine", "connected", False, False),
    "setup_board": ("routine", "inventory", False, False),
    "repair_board_setup": ("routine", "inventory", False, False),
    "continue_board_setup": ("routine", "inventory", False, False),
    "validate_board": ("routine", "profile", False, False),
    "connect_board": ("routine", "profile", False, False),
    "get_target_state": ("routine", "connected", False, False),
    "halt_target": ("routine", "connected", False, False),
    "resume_target": ("routine", "connected", False, False),
    "step_target": ("routine", "connected", False, False),
    "reset_target": ("routine", "connected", False, False),
    "read_cpu_register": ("routine", "connected", False, False),
    "write_cpu_register": ("routine", "connected", False, False),
    "write_peripheral_register": ("routine", "connected-and-safety", False, False),
    "read_memory": ("routine", "connected-and-safety", False, False),
    "write_memory": ("routine", "connected-and-safety", False, False),
    "set_breakpoint": ("routine", "connected-and-safety", False, False),
    "remove_breakpoint": ("routine", "connected-and-safety", False, False),
    "flash_firmware": ("destructive", "connected-and-safety", True, False),
    "read_serial": ("routine", "connected", False, True),
    "write_serial": ("routine", "connected", False, True),
    "exchange_serial": ("routine", "connected", False, True),
    "recover_target": ("destructive", "connected-and-safety", False, False),
}

_guard_file_bindings: dict[str, tuple[str, ...]] = {
    "refresh_safety_map": ("layout_path", "application_elf_path"),
    "set_breakpoint": ("elf_path",),
    "flash_firmware": ("firmware_path", "artifact_target_evidence_path"),
}


def _guard_file_binding_resolver(
    spec: ActionSpec,
    arguments: Mapping[str, object],
    snapshots: Mapping[str, bytes] | None,
) -> Mapping[str, Path]:
    """Discover explicit nested evidence without giving GuardCore tool policy.

    The opaque action registry is the only place that knows the selected
    layout schema.  During execution it parses the GuardCore-captured layout
    bytes, so the handler and map builder never reopen a mutable layout path.
    """

    if spec.name != "refresh_safety_map" or arguments.get("layout_path") is None:
        return {}
    layout_path = cast(str, arguments["layout_path"])
    if snapshots is None:
        payload = Path(layout_path).read_bytes()
    else:
        payload = snapshots.get("layout_path")
        if payload is None:
            return {}
    try:
        import yaml  # type: ignore[import-untyped]

        parsed = yaml.safe_load(payload)
    except Exception as exc:  # noqa: BLE001 - selected trusted evidence is explicit
        raise GuardError(
            "guard/file-binding-invalid", f"Selected layout cannot be parsed: {exc}"
        ) from exc
    if not isinstance(parsed, Mapping) or not isinstance(parsed.get("regions"), list):
        raise GuardError(
            "guard/file-binding-invalid", "Selected layout must contain a regions list."
        )
    nested: dict[str, Path] = {}
    for index, fact in enumerate(parsed["regions"]):
        if (
            not isinstance(fact, Mapping)
            or not isinstance(fact.get("source_path"), str)
            or not fact["source_path"]
        ):
            raise GuardError(
                "guard/file-binding-invalid",
                f"Layout region {index} requires a non-empty source_path.",
            )
        nested[f"layout_source:{index}"] = Path(fact["source_path"])
    return nested


def _guard_evidence(board_id: str) -> dict[str, object]:
    """Current profile/assignment/session evidence, never authority by itself."""

    evidence: dict[str, object] = {"board_id": board_id}
    repository = globals().get("_profile_repository")
    if repository is not None:
        try:
            profile = repository.load(board_id)
            document = profile.to_document()
            evidence["profile_sha256"] = hashlib.sha256(
                json.dumps(document, sort_keys=True, separators=(",", ":"), default=str).encode(
                    "utf-8"
                )
            ).hexdigest()
            evidence["provider_support"] = document.get("device_support")
            evidence["stable_serial"] = document.get("serial_id")
        except (ProfileError, OSError):
            evidence.update(profile_sha256=None, provider_support=None, stable_serial=None)
    else:
        evidence.update(profile_sha256=None, provider_support=None, stable_serial=None)
    evidence["assignment"] = assignment_store.connection_for(board_id)
    connection = connection_manager.maybe_connection(board_id)
    if connection is None:
        evidence.update(connection_id=None, session_id=None, connection_identity=None)
    else:
        evidence.update(
            connection_id=connection.connection_id,
            session_id=connection.runtime_session.session_id,
            connection_identity=stable_connection_identity(connection.handle),
        )
    return evidence


def _guard_safety_binding(board_id: str) -> dict[str, object]:
    """Return a map digest only after replaying it against this live session."""
    authority = globals().get("_safety_authority")
    connection = connection_manager.maybe_connection(board_id)
    if authority is None or connection is None:
        raise GuardError(
            "guard/safety-map-missing",
            "Connect and refresh_safety_map before creating this map-bound plan.",
        )
    try:
        return authority.binding(board_id, connection.handle)
    except SafetyAuthorityError as exc:
        if isinstance(exc.__cause__, LiveIdentityContradiction):
            raise GuardError(
                "guard/live-identity-contradiction",
                "Current live identity contradicts the bound safety map; reconnect and inspect "
                "the configured board before refreshing the map.",
            ) from exc
        if isinstance(exc.__cause__, LiveIdentityObservationError):
            raise GuardError(
                "guard/identity-observation-read-failed",
                "Current configured live identity could not be read before comparing the bound "
                "safety map; reconnect and retry the live identity read.",
            ) from exc
        raise GuardError(
            "guard/safety-map-stale", f"{exc} Remedy: refresh_safety_map for this board."
        ) from exc


def _record_guard_attempt(tool_name: str, attempt: dict[str, object]) -> None:
    board_id = cast(str, attempt["board_id"])
    _record_event(
        tool_name,
        {"board_id": board_id, "plan_id": attempt["plan_id"]},
        outcome_kind=ToolOutcome.ATTEMPT_STARTED,
        error_code=None,
        duration_ms=0,
        details=attempt,
        board_id=board_id,
        session=_runtime_for(board_id),
    )


def _connect_route_classification(
    board_id: str, arguments: Mapping[str, object]
) -> dict[str, object]:
    """Prove a disconnected connect route from current stored evidence.

    A connect plan is deliberately possible before a worker exists, but only
    for the exact replayed profile and run-scoped assignment.  pyOCD probe
    serials retain their established stable-USB comparison; external-provider
    connection IDs remain opaque, case-sensitive namespaced values.
    """

    if arguments.get("board_config_path") is not None:
        raise GuardError(
            "guard/connect-route",
            "connect_board must use the stored profile; board_config_path is not accepted.",
        )
    try:
        profile = _profile_repository.load(board_id)
    except (ProfileError, OSError) as exc:
        raise GuardError(
            "guard/connect-profile-missing",
            "connect_board requires the exact current stored profile; run setup_board first.",
        ) from exc

    evidence = _guard_evidence(board_id)
    digest = evidence.get("profile_sha256")
    support = profile.device_support
    assignment = assignment_store.connection_for(board_id)
    if not isinstance(digest, str) or not digest:
        raise GuardError(
            "guard/connect-profile-missing",
            "connect_board requires a readable current persisted profile digest.",
        )
    if not isinstance(support, Mapping) or not support:
        raise GuardError(
            "guard/connect-support-missing",
            "connect_board requires verified provider support in the stored profile.",
        )
    if not isinstance(assignment, str) or not assignment:
        raise GuardError(
            "guard/connect-assignment-missing",
            "connect_board requires the exact current setup connection assignment.",
        )
    try:
        assignment_store.require(assignment, board_id)
    except Exception as exc:  # the assignment table is correctness evidence
        raise GuardError(
            "guard/connect-assignment-stale",
            "connect_board assignment no longer belongs to this exact board; rerun setup routing.",
        ) from exc

    support_target = support.get("target") or support.get("pyocd_target")
    if not isinstance(support_target, str) or not support_target:
        raise GuardError(
            "guard/connect-support-missing",
            "Stored provider support has no verified target identity; rerun setup_board.",
        )
    if _normalized_target_identity(support_target) != _normalized_target_identity(
        profile.board.target
    ):
        raise GuardError(
            "guard/connect-support-stale",
            "Stored provider support target no longer matches the persisted board profile.",
        )
    target = arguments.get("target")
    if target is not None and (
        not isinstance(target, str)
        or _normalized_target_identity(target) != _normalized_target_identity(support_target)
    ):
        raise GuardError(
            "guard/connect-target-mismatch",
            "An explicit target must match the stored verified support target exactly.",
        )

    requested_probe = arguments.get("probe_id")
    if requested_probe is not None and not isinstance(requested_probe, str):
        raise GuardError("guard/connect-route", "probe_id must be a string or null.")
    provider_id = profile.board.provider_id
    # Only the built-in pyOCD route has a documented stable-USB identity
    # comparison.  Every other provider ID is intentionally opaque: preserve
    # its exact spelling and namespace from the replayed profile.
    if provider_id.casefold() == "pyocd":
        if not assignment.casefold().startswith("probe:") or not assignment.split(":", 1)[1]:
            raise GuardError(
                "guard/connect-assignment-stale",
                "pyOCD connect requires a stored probe:<stable-uid> assignment.",
            )
        stored_uid = assignment.split(":", 1)[1]
        if requested_probe is not None and not _connection_matches_probe(
            requested_probe,
            ValidationProbe(stored_uid, "stored setup assignment", "pyocd", stored_uid),
        ):
            raise GuardError(
                "guard/connect-probe-mismatch",
                "probe_id does not match this board's stored stable probe assignment.",
            )
    else:
        expected_prefix = f"provider:{provider_id}:"
        if not assignment.startswith(expected_prefix):
            raise GuardError(
                "guard/connect-assignment-stale",
                "Provider connect requires the exact stored namespaced connection assignment.",
            )
        if requested_probe is not None and requested_probe != assignment:
            raise GuardError(
                "guard/connect-probe-mismatch",
                "Provider connection IDs are opaque and must exactly match the stored assignment.",
            )

    under_reset = arguments.get("under_reset")
    if not isinstance(under_reset, bool):
        raise GuardError("guard/connect-route", "under_reset must be true or false.")
    return {
        "risk": "routine",
        "effects": {
            "profile_sha256": digest,
            "provider_support": dict(support),
            "connection_assignment": assignment,
            "target": support_target,
            "under_reset": under_reset,
        },
    }


def _canonicalize_connect_plan_actions(board_id: str, actions: list[object]) -> list[object]:
    """Bind a planned connect to its current stored route, not user spelling.

    The public call may use a pyOCD serial spelling that is stably equivalent
    to the setup assignment.  After validating that spelling, persist the
    assignment and verified target in the immutable plan so execution always
    repeats one canonical stored route.  Malformed non-connect actions remain
    for GuardCore's normal exact-schema diagnostics.
    """

    canonical_actions: list[object] = []
    for raw_action in actions:
        if not isinstance(raw_action, Mapping) or raw_action.get("tool") != "connect_board":
            canonical_actions.append(raw_action)
            continue
        raw_arguments = raw_action.get("arguments")
        if not isinstance(raw_arguments, Mapping):
            canonical_actions.append(raw_action)
            continue
        classification = _connect_route_classification(board_id, raw_arguments)
        effects = classification["effects"]
        assert isinstance(effects, Mapping)
        route = dict(raw_arguments)
        route["probe_id"] = effects["connection_assignment"]
        route["target"] = effects["target"]
        route["board_config_path"] = None
        action = dict(raw_action)
        action["arguments"] = route
        canonical_actions.append(action)
    return canonical_actions


def _guard_classification(
    tool: str, board_id: str, arguments: Mapping[str, object], snapshots: Mapping[str, bytes] | None
) -> dict[str, object]:
    """Registry-owned dynamic risk facts; GuardCore deliberately knows no tool names."""

    if tool == "flash_firmware":
        handle = _handle(board_id)
        path = Path(cast(str, arguments["firmware_path"])).expanduser().resolve()
        payload = snapshots.get("firmware_path") if snapshots else None
        if payload is None:
            if snapshots is not None:
                raise GuardError(
                    "guard/file-invalid",
                    "The immutable firmware snapshot is missing; create a fresh plan.",
                )
            payload = path.read_bytes()
        image = parse_flash_image_bytes(path, payload)
        role = arguments.get("flash_role")
        if role not in {"application", "bootloader", "full-device", "sensitive"}:
            raise GuardError(
                "guard/flash-role",
                "flash_role must be application, bootloader, full-device, or sensitive.",
            )
        facts = _safety_authority.validate_flash_role(
            board_id, handle, list(image.ranges), cast(str, role)
        )
        target: dict[str, object] = {
            "status": "unavailable",
            "reason": "ELF/HEX load ranges do not identify an exact MCU part.",
        }
        target_path = arguments.get("artifact_target_evidence_path")
        if target_path is not None:
            raw = snapshots.get("artifact_target_evidence_path") if snapshots else None
            if raw is None:
                if snapshots is not None:
                    raise GuardError(
                        "guard/file-invalid",
                        "The immutable artifact target-evidence snapshot is missing; create a fresh plan.",
                    )
                raw = Path(cast(str, target_path)).read_bytes()
            try:
                evidence = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise GuardError(
                    "guard/artifact-target-evidence",
                    f"Artifact target evidence is malformed: {exc}",
                ) from exc
            if (
                not isinstance(evidence, dict)
                or set(evidence)
                != {"schema_version", "artifact_sha256", "part_number", "provenance"}
                or evidence.get("schema_version") != 1
                or not isinstance(evidence.get("artifact_sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", evidence["artifact_sha256"]) is None
                or not isinstance(evidence.get("part_number"), str)
                or not evidence["part_number"].strip()
                or not isinstance(evidence.get("provenance"), str)
                or not evidence["provenance"].strip()
            ):
                raise GuardError(
                    "guard/artifact-target-evidence",
                    "Artifact target evidence must be the strict version-1 exact-part record.",
                )
            if evidence["artifact_sha256"] != image.sha256:
                raise GuardError(
                    "guard/artifact-target-evidence",
                    "Artifact target evidence digest does not match the captured firmware bytes.",
                )
            try:
                profile = _profile_repository.load(board_id)
                identity = observe_live_identity(
                    handle,
                    read_memory=target_control.read_memory,
                    configured_part_number=profile.mcu_part_number,
                )
            except LiveIdentityContradiction as exc:
                raise GuardError(
                    "guard/live-identity-contradiction",
                    f"Current live identity contradicts this session: {exc}",
                ) from exc
            except LiveIdentityObservationError as exc:
                raise GuardError(
                    "guard/identity-observation-read-failed",
                    "Current configured live identity could not be read; reconnect and retry before "
                    f"this operation: {exc}",
                ) from exc
            if identity.capability == "exact" and normalize_part_number(
                evidence["part_number"]
            ) != normalize_part_number(cast(str, identity.exact_live_part_number)):
                raise GuardError(
                    "guard/artifact-target-mismatch",
                    "Artifact exact target evidence contradicts the current exact live silicon part.",
                )
            target = {
                "status": "matched" if identity.capability == "exact" else "unavailable",
                "classification": "explicit artifact metadata",
                "part_number": evidence["part_number"],
                "provenance": evidence["provenance"],
                "artifact_sha256": image.sha256,
                "live_identity": identity.to_record(),
            }
        return {
            "risk": "routine" if role == "application" else "destructive",
            "effects": {
                "artifact_path": str(path),
                "suffix": path.suffix.lower(),
                "byte_count": len(image.bytes_by_address),
                "sha256": image.sha256,
                "ranges": [list(item) for item in image.ranges],
                "flash_role": role,
                "role_coverage": facts,
                "artifact_target_comparison": target,
                "halt_after_reset": arguments.get("halt_after_reset"),
            },
        }
    if tool in {"write_memory", "write_peripheral_register"}:
        handle = _handle(board_id)
        address = arguments.get("address")
        width = arguments.get("width_bits", 32)
        if not isinstance(address, int) or not isinstance(width, int):
            raise GuardError("guard/write-shape", "Write address and width_bits are invalid.")
        facts = _safety_authority.classify_write(board_id, handle, address, width // 8)
        return {
            "risk": facts["risk"],
            "effects": {
                "span": [address, address + width // 8],
                "width_bits": width,
                "value": arguments.get("value"),
                "regions": facts["regions"],
                "roles": facts["roles"],
                "map_digest": facts["digest"],
            },
        }
    if tool == "recover_target":
        handle = _handle(board_id)
        mechanism = arguments.get("mechanism")
        if not isinstance(mechanism, str) or not mechanism:
            raise GuardError(
                "guard/recovery-mechanism",
                "Recovery requires one non-empty live provider mechanism.",
            )
        cap = next(
            (
                item
                for item in target_control.recovery_capabilities(handle)
                if item.mechanism == mechanism
            ),
            None,
        )
        if cap is None:
            raise GuardError(
                "guard/recovery-capability",
                "Selected recovery mechanism is unavailable; inspect current board capability evidence.",
            )
        try:
            coverage = _safety_authority.resolve_recovery(board_id, handle, cap)
        except SafetyAuthorityError as exc:
            raise GuardError("guard/recovery-coverage", str(exc)) from exc
        return {
            "risk": "destructive",
            "effects": {
                "mechanism": mechanism,
                "capability": cap.to_record(),
                "affected_ranges": coverage,
            },
        }
    if tool == "connect_board":
        return _connect_route_classification(board_id, arguments)
    return {"risk": _guarded_risks[tool][0], "effects": {}}


def _spec_for(name: str, handler: Callable[..., object]) -> ActionSpec:
    risk, lifecycle, artifact_bound, serial_bound = _guarded_risks[name]
    arguments = tuple(inspect.signature(handler).parameters)
    if not arguments or arguments[0] != "board_id" or "plan_id" in arguments:
        raise RuntimeError(f"Guarded tool signature drift for {name}")
    return ActionSpec(
        name,
        cast(Any, risk),
        cast(Any, lifecycle),
        arguments,
        artifact_bound,
        serial_bound,
        _guard_file_bindings.get(name, ()),
        lambda board_id, arguments, snapshots, tool=name: _guard_classification(
            tool, board_id, arguments, snapshots
        ),
    )


def _make_guard_specs(handlers: Mapping[str, Callable[..., object]]) -> dict[str, ActionSpec]:
    return {
        name: _spec_for(name, handler)
        for name, handler in handlers.items()
        if name in _guarded_risks
    }


_composed_handlers = (
    {"refresh_safety_map": refresh_safety_map}
    | session_tool_handlers
    | execution_tool_handlers
    | register_tool_handlers
    | memory_tool_handlers
    | flash_tool_handlers
    | serial_tool_handlers
    | breakpoint_tool_handlers
    | misc_tool_handlers
    | artifact_tool_handlers
    | build_tool_handlers
)
_guard_core = GuardCore(
    project_root=_project_root,
    run_id=server_run.run_id,
    action_specs=_make_guard_specs(_composed_handlers),
    evidence_for=_guard_evidence,
    on_attempt=_record_guard_attempt,
    serial_identity_for=_guard_serial_identity,
    safety_binding_for=_guard_safety_binding,
    file_binding_resolver=_guard_file_binding_resolver,
)


def _guarded_handler(name: str, handler: Callable[..., object]) -> Callable[..., object]:
    """Expose required plan_id while leaving the tool's owned behavior intact."""

    if name not in _guard_core.action_specs:
        return handler
    signature = inspect.signature(handler)
    resolved_annotations = inspect.get_annotations(handler, eval_str=True)
    signature = signature.replace(
        parameters=tuple(
            parameter.replace(
                annotation=resolved_annotations.get(parameter.name, parameter.annotation)
            )
            for parameter in signature.parameters.values()
        ),
        return_annotation=resolved_annotations.get("return", signature.return_annotation),
    )

    def invalidation_uncertainty(error: GuardError, *, raw_outcome: str) -> GuardError:
        """Name a completed/failed raw operation separately from its durable guard state."""

        return GuardError(
            error.code,
            f"{name} {raw_outcome}, but durable permission invalidation is uncertain: "
            f"{error.message} Do not retry blindly; inspect permission status and the operation "
            "result or current session state before choosing the next action.",
        )

    @wraps(handler)
    def guarded(*args: object, plan_id: str, **kwargs: object) -> object:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        board_id = cast(str, bound.arguments.get("board_id", ""))
        # Lock order is board connection -> durable safety publication lock ->
        # GuardCore -> permission store.  Map-bound work keeps the first two
        # locks through the raw side effect; the short guard lock is released
        # by GuardCore before the handler enters a provider worker.
        # Keep the stable board lock through the raw handler and evidence
        # publication so a plan validated for one session cannot act on its
        # replacement.  The guard core releases its own lock before this raw
        # handler can reach a backend.
        with connection_manager.lock_for(board_id):
            safety_context = (
                safety_publication_lock(_project_root, board_id)
                if _guard_core.action_specs[name].lifecycle == "connected-and-safety"
                else nullcontext()
            )
            with safety_context:
                try:
                    _guard_core.execute(tool=name, plan_id=plan_id, arguments=bound.arguments)
                except GuardError as error:
                    _record_event(
                        name,
                        cast(Mapping[str, object], bound.arguments),
                        outcome_kind=ToolOutcome.INVALID,
                        error_code=error.code,
                        duration_ms=0,
                        details={"message": error.message, "plan_id": plan_id},
                        board_id=board_id,
                        session=_runtime_for(board_id),
                    )
                    return _guard_error_result(error)
                evidence_publishing_action = name in {
                    "setup_board",
                    "repair_board_setup",
                    "continue_board_setup",
                    "connect_board",
                }
                try:
                    result = handler(*args, **kwargs)
                except GuardError as operation_error:
                    _record_event(
                        name,
                        cast(Mapping[str, object], bound.arguments),
                        outcome_kind=ToolOutcome.INVALID,
                        error_code=operation_error.code,
                        duration_ms=0,
                        details={"message": operation_error.message, "plan_id": plan_id},
                        board_id=board_id,
                        session=_runtime_for(board_id),
                    )
                    return _guard_error_result(operation_error)
                except BaseException as operation_error:
                    if evidence_publishing_action or name == "validate_board":
                        try:
                            _guard_core.invalidate_board(board_id, f"{name}-changed-evidence")
                        except GuardError as invalidation_error:
                            raise operation_error from invalidation_uncertainty(
                                invalidation_error, raw_outcome="failed"
                            )
                    raise
                finally:
                    _guard_core.clear_execution_files()
                if evidence_publishing_action:
                    try:
                        _guard_core.invalidate_board(board_id, f"{name}-changed-evidence")
                    except GuardError as invalidation_error:
                        return _guard_error_result(
                            invalidation_uncertainty(invalidation_error, raw_outcome="completed")
                        )
                return result

    plan_parameter = inspect.Parameter("plan_id", inspect.Parameter.KEYWORD_ONLY, annotation=str)
    setattr(
        guarded,
        "__signature__",
        signature.replace(parameters=(*signature.parameters.values(), plan_parameter)),
    )
    # Resolve postponed annotations in the owning tool module before copying
    # them onto this server-local wrapper.  FastMCP/Pydantic then sees the
    # concrete Literal schema rather than a forward reference whose namespace
    # was changed by the wrapper.
    guarded.__annotations__ = {**resolved_annotations, "plan_id": str}
    # FastMCP follows ``__wrapped__`` while building a schema.  The public
    # contract is the guard wrapper, so do not let it unwrap back to the raw
    # hardware callable and silently omit the required plan_id.
    del guarded.__wrapped__
    guarded._guarded_raw_handler = handler  # type: ignore[attr-defined]
    guarded.__doc__ = (handler.__doc__ or "") + (
        "\n\n**Guarded call.** Supply the exact `plan_id` returned by `create_hardware_plan`; "
        "visible does not mean authorized."
    )
    return guarded


for _handler_name, _handler in _composed_handlers.items():
    _registered_handler = _guarded_handler(_handler_name, _handler)
    mcp.add_tool(
        _registered_handler,
        name=_handler_name,
        description=_registered_handler.__doc__,
        structured_output=False,
    )


def _built_in_target_names() -> set[str]:
    from pyocd.target.builtin import BUILTIN_TARGETS  # type: ignore[reportMissingImports]

    return {str(name).casefold() for name in BUILTIN_TARGETS}


def _target_names() -> tuple[str, ...]:
    # Use pyOCD's pinned in-process registry.  Parsing its human-formatted CLI
    # table was locale-dependent on Windows and could turn a supported target
    # into an empty inventory when a description contained non-ASCII text.
    names = _built_in_target_names()
    for pack in load_manifest(_firm_store.layout.pack_manifest):
        names.update(target.casefold() for target in pack.provides_targets)
    return tuple(sorted(names))


def _validation_inventory() -> ValidationInventory:
    probes_by_id = {
        probe.uid: ValidationProbe(
            probe.uid,
            probe.description or probe.raw,
            probe.family,
            probe.uid or None,
        )
        for probe in list_connected_probes()
    }
    # pyOCD inventory intentionally omits probes already opened by this process.
    # Validation must still be able to select and stamp the server-owned active
    # connection. A hardware UID remains the stable inventory key; a UID-less
    # provider is represented only by its exact live, session-local connection ID.
    for board_id in connection_manager.assigned_board_ids():
        connection = connection_manager.connection_for(board_id)
        handle = connection.handle
        metadata = session_metadata(handle)
        probe_uid = (metadata.probe_uid or "").strip()
        probe_id = probe_uid or connection.connection_id
        if probe_id in probes_by_id:
            continue
        board = handle.board
        description = str(metadata.probe_description or "").strip()
        if not description:
            description = (
                board.display_name if board is not None else f"Active connection {probe_id}"
            )
        probe_family = str(metadata.probe_family or "unknown")
        probes_by_id[probe_id] = ValidationProbe(
            probe_id,
            description,
            probe_family,
            probe_uid or None,
        )
    probes = tuple(probes_by_id[key] for key in sorted(probes_by_id))
    serial_ports = list_serial_ports() or []
    serial = tuple(
        ValidationSerial(
            port.serial_number or port.device,
            port.device,
            port.description or port.product or "Serial connection",
            port.serial_number or None,
            port.vid,
            port.pid,
        )
        for port in serial_ports
    )
    return ValidationInventory(probes, serial)


def _validation_target_supported(target: str) -> bool | None:
    normalized = target.casefold()
    if normalized in _built_in_target_names():
        return True
    try:
        return (
            verified_pack_for_target(
                normalized,
                manifest_path=_firm_store.layout.pack_manifest,
                packs_dir=_firm_store.layout.pack_files,
            )
            is not None
        )
    except PackProvisionError:
        # A declaration or stale process-global registration is not authority.
        # Missing, changed, or ambiguous pinned bytes must fail closed.
        return False


class _ValidationConnection:
    __slots__ = (
        "handle",
        "owned",
        "board_id",
        "promoted",
        "assignment",
        "transport_lost",
    )

    def __init__(
        self,
        handle: TargetSessionHandle,
        owned: bool,
        *,
        board_id: str | None = None,
        promoted: bool = False,
        assignment: ManagedConnection | None = None,
    ) -> None:
        self.handle = handle
        self.owned = owned
        self.board_id = board_id
        self.promoted = promoted
        self.assignment = assignment
        self.transport_lost = False


def _replay_profile_device_support(profile) -> DeviceSupportAuthority:
    device_support = getattr(profile, "device_support", None)
    part_number = getattr(profile, "mcu_part_number", None)
    if device_support is None or part_number is None:
        raise PackProvisionError("generic profile has no exact device-support authority")
    if device_support.get("kind") == "resolved_builtin_target":
        return resolve_persisted_builtin_target_support(part_number, device_support)
    return resolve_persisted_pack_support(_profile_repository.store, part_number, device_support)


def _verified_pack_for_profile(profile):
    """Return the exact replayed pack for a generic profile, if one is required."""

    device_support = getattr(profile, "device_support", None)
    if device_support is None:
        return None
    candidate = _replay_profile_device_support(profile)
    if candidate.to_authority_document() != dict(device_support):
        raise PackProvisionError("generic profile no longer matches its exact support binding")
    if isinstance(candidate, BuiltInTargetSupportCandidate):
        return None
    return verified_pack_for_candidate(candidate, _profile_repository.store)


def _validation_connect(profile, probe: ValidationProbe) -> object:
    existing = connection_manager.maybe_connection(profile.board_id)
    if existing is not None:
        if not _connection_matches_probe(existing.connection_id, probe):
            raise TargetConnectionError(
                "The selected validation probe does not match the board's exact active "
                "connection assignment."
            )
        return _ValidationConnection(
            existing.handle,
            False,
            board_id=profile.board_id,
            assignment=existing,
        )
    saved_policy = (
        getattr(profile.board, "debug_protocol", None),
        profile.board.debug_connect_mode or "attach",
        profile.board.debug_clock_hz,
    )
    # A successful setup policy is a learned connection fact, not a fallback
    # hint. Replay it exactly rather than first probing a different route.
    protocol, mode, frequency = saved_policy
    selected_pack = _verified_pack_for_profile(profile)
    try:
        handle = target_control.open_session(
            board=profile.board,
            unique_id=probe.usb_serial,
            target=profile.board.target,
            protocol=protocol,
            connect_mode=mode,
            frequency_hz=frequency,
            pack_path=(selected_pack.path if selected_pack is not None else None),
            pack_sha256=(selected_pack.spec.sha256 if selected_pack is not None else None),
            pdsc_device=(
                profile.device_support["pdsc_device"]
                if selected_pack is not None and profile.device_support is not None
                else None
            ),
        )
    except TargetConnectionError:
        raise
    assignment = _promote_open_session(profile.board_id, handle)
    return _ValidationConnection(
        handle,
        False,
        board_id=profile.board_id,
        promoted=True,
        assignment=assignment,
    )


def _validation_read(connection: object, address: int, width: int) -> int:
    validation = cast(_ValidationConnection, connection)
    try:
        return target_control.read_memory(
            validation.handle,
            address,
            width,
        )
    except TargetConnectionError:
        validation.transport_lost = True
        raise


def _close_evicted_validation_connection(
    board_id: str,
    connection: ManagedConnection,
) -> None:
    """Release every resource after exact validation-assignment eviction."""

    try:
        _session_store.close_session(connection.runtime_session)
    finally:
        target_control.close_session(connection.handle)


def _validation_close(connection: object) -> None:
    validation = cast(_ValidationConnection, connection)
    if validation.owned:
        target_control.close_session(validation.handle)
        return
    if validation.transport_lost and validation.board_id is not None:
        captured = validation.assignment
        if captured is None or captured.handle is not validation.handle:
            return
        cleared = connection_manager.clear_if_current(validation.board_id, captured)
        if cleared is None:
            return
        _close_evicted_validation_connection(
            validation.board_id,
            cleared,
        )
        return


_firm_store = FirmStore(_project_root)
_provider_recipe_store = ProviderRecipeStore(_firm_store)
_profile_repository = ProfileRepository(_firm_store)
_safety_authority = SafetyAuthority(
    _firm_store,
    _profile_repository,
    target_control.physical_memory_regions,
    target_control.read_memory,
)
_attachment_cache = AttachmentCache(_firm_store)
_report_writer = ReportWriter(_firm_store)


def _validation_live_identity(profile: BoardProfile, connection: object) -> Mapping[str, object]:
    """Observe validation identity through the same session rule as execution."""

    validation = cast(_ValidationConnection, connection)
    return observe_live_identity(
        validation.handle,
        read_memory=target_control.read_memory,
        configured_part_number=profile.mcu_part_number,
    ).to_record()


_board_validator = BoardValidator(
    _profile_repository,
    _report_writer,
    ValidationBackend(
        _validation_inventory,
        _validation_target_supported,
        _validation_connect,
        _validation_read,
        _validation_close,
        _validation_live_identity,
    ),
    cancellation_checkpoint=cancellation_checkpoint,
    lock_for_board=connection_manager.lock_for,
)


def _normalized_target_identity(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _stable_identity_equal(left: str | None, right: str | None) -> bool:
    """Compare stable USB identifiers without conflating mutable display labels."""

    if not left or not right:
        return False
    left_normalized = left.strip().casefold()
    right_normalized = right.strip().casefold()
    if left_normalized == right_normalized:
        return True
    if left_normalized.isdecimal() and right_normalized.isdecimal():
        return (left_normalized.lstrip("0") or "0") == (right_normalized.lstrip("0") or "0")
    return False


def _replayed_provider_recipe(profile: BoardProfile) -> ProviderRecipe:
    """Load current recipe bytes and require the profile's canonical binding."""

    support = profile.device_support
    if support is None or support.get("kind") != "provider_recipe":
        raise TargetConnectionError("Provider profile has no replayable recipe support binding.")
    try:
        recipe = _provider_recipe_store.load(profile.board.provider_id)
        expected = recipe.support_identity(profile.board.target)
    except ProviderRecipeError as exc:
        raise TargetConnectionError(
            f"Current provider recipe cannot be replayed; rerun setup_board: {exc}"
        ) from exc
    if (
        support.get("provider_id") != recipe.provider_id
        or support.get("target", "").casefold() != profile.board.target.casefold()
    ):
        raise TargetConnectionError("Provider profile route is malformed; rerun setup_board.")
    if support.get("support_id") != expected:
        raise TargetConnectionError(
            "Stored provider recipe changed after setup; rerun setup_board before reconnecting or flashing."
        )
    return recipe


def _resolve_current_connection_for_connect(
    board_id: str,
    board: BoardConfig | None,
    profile: BoardProfile | None,
    requested_connection: str | None,
) -> tuple[str | None, tuple[str, ...] | None]:
    """Resolve one current connection without routing generic boards through pyOCD.

    Built-in boards retain their existing probe resolver.  A generic board is
    always replayed from its profile recipe, then matched exactly once against
    a fresh inventory using the persisted assignment's namespaced identifier.
    """

    if board is None or board.provider_id == "pyocd":
        # ``probe:<uid>`` is a server-owned setup/plan binding, never a pyOCD
        # UID.  A guarded reconnect deliberately replays that immutable
        # assignment, then resolves it again against current inventory at the
        # provider boundary.  This prevents a changed or vanished assignment
        # from reaching the worker, while giving pyOCD only its raw UID.
        if requested_connection is not None and requested_connection.casefold().startswith(
            "probe:"
        ):
            assigned = assignment_store.connection_for(board_id)
            if not isinstance(assigned, str) or assigned != requested_connection:
                raise TargetConnectionError(
                    "The stored pyOCD assignment changed before connect dispatch; "
                    "rerun setup routing and create a fresh plan."
                )
            if not assigned.split(":", 1)[1]:
                raise TargetConnectionError(
                    "The stored pyOCD assignment is malformed; rerun setup routing."
                )
            assigned_uid = _assigned_probe_uid_for_connect(board_id)
            if assigned_uid is None:
                raise TargetConnectionError(
                    "The stored pyOCD assignment disappeared before connect dispatch; "
                    "rerun setup routing and create a fresh plan."
                )
            return assigned_uid, None
        assigned = (
            _assigned_probe_uid_for_connect(board_id) if requested_connection is None else None
        )
        return (
            _resolve_probe_uid_for_connect(
                board,
                assigned or requested_connection,
            ),
            None,
        )
    if profile is None:
        raise TargetConnectionError(
            "Generic provider connection requires its persisted profile; rerun setup_board."
        )
    recipe = _replayed_provider_recipe(profile)
    assigned = assignment_store.connection_for(board_id)
    if not assigned:
        raise TargetConnectionError(
            "No current provider connection is assigned to this board; call get_setup_overview and setup_board."
        )
    if requested_connection is not None and requested_connection != assigned:
        raise TargetConnectionError(
            "Requested provider connection does not match this board's current setup assignment; "
            "call get_setup_overview and setup_board."
        )
    selected_id = assigned
    expected_prefix = f"provider:{recipe.provider_id}:"
    if not isinstance(selected_id, str) or not selected_id.startswith(expected_prefix):
        raise TargetConnectionError(
            "Provider connection must use the exact namespaced inventory connection_id from setup."
        )
    try:
        matches = [
            item for item in run_provider_inventory(recipe) if item.namespaced_id == selected_id
        ]
    except ProviderRecipeError as exc:
        raise TargetConnectionError(
            f"Current provider inventory failed; rerun setup_board: {exc}"
        ) from exc
    if len(matches) != 1:
        raise TargetConnectionError(
            "Assigned provider connection is stale or ambiguous; call get_setup_overview and setup_board."
        )
    return matches[0].connection_id, recipe.worker_argv


def _worker_argv_for_board(board: BoardConfig | None) -> tuple[str, ...] | None:
    """Compatibility helper for callers that do not yet have a profile.

    Connect and flash use `_replayed_provider_recipe`, which binds the argv to
    a stored profile.  This remains only for the temporary internal callers.
    """

    if board is None or board.provider_id == "pyocd":
        return None
    return _provider_recipe_store.load(board.provider_id).worker_argv


def _require_current_provider_identity(
    profile: BoardProfile | None, handle: TargetSessionHandle
) -> None:
    """Re-observe the replay-bound generic-provider identity before use."""

    if profile is None or profile.board.provider_id == "pyocd":
        return
    support = profile.device_support
    _replayed_provider_recipe(profile)
    if support is None or support.get("kind") != "provider_recipe":
        raise TargetConnectionError("Provider profile has no replayable recipe support binding.")
    try:
        observe_live_identity(
            handle,
            read_memory=None,
            configured_part_number=profile.mcu_part_number,
        )
    except TargetStateError as exc:
        raise TargetConnectionError(str(exc)) from exc


def _flash_target_with_replayed_recipe(
    handle: TargetSessionHandle, artifact: Path, halt_after_reset: bool
):
    """Reject a changed generic recipe before the worker can mutate hardware."""

    board_id = None
    for candidate in connection_manager.assigned_board_ids():
        managed = connection_manager.maybe_connection(candidate)
        if managed is not None and managed.handle is handle:
            board_id = candidate
            break
    if board_id is None:
        raise TargetConnectionError("Flash session is no longer assigned to a logical board.")
    try:
        profile = _profile_repository.load(board_id)
    except ProfileError:
        profile = None
    _require_current_provider_identity(profile, handle)
    return target_control.flash_firmware(handle, artifact, halt_after_reset=halt_after_reset)


def _connection_matches_probe(
    connection_id: str,
    probe: ProbeCandidate | ValidationProbe,
) -> bool:
    candidate = connection_id.strip()
    if candidate.casefold().startswith("probe:"):
        candidate = candidate.split(":", 1)[1]
    return _stable_identity_equal(candidate, probe.probe_id) or _stable_identity_equal(
        candidate, probe.usb_serial
    )


_setup_research = ResearchTracker()
_setup_target_overrides: dict[str, str] = {}
_setup_attachment_overrides: dict[str, tuple[str | None, str, int | None]] = {}
_setup_builtin_candidates: dict[str, BuiltInTargetSupportCandidate] = {}
_setup_selections_by_board: dict[str, PreflightSelections] = {}
_setup_pack_pipelines: dict[
    tuple[str, str, tuple[str | None, str, int | None] | None],
    tuple[PackCandidatePipeline, list[tuple[str | None, str, int | None]]],
] = {}


class _ResolvedGenericSetupSupport:
    """Ephemeral generic setup result; intentionally has no persisted authority state."""

    __slots__ = ("candidate", "datasheet_path", "datasheet_sha256")

    def __init__(
        self,
        candidate: DeviceSupportAuthority,
        datasheet_path: Path,
        datasheet_sha256: str,
    ) -> None:
        self.candidate = candidate
        self.datasheet_path = datasheet_path
        self.datasheet_sha256 = datasheet_sha256


def _is_generic_support(value: object) -> bool:
    return isinstance(value, _ResolvedGenericSetupSupport)


def _resolve_setup_support(user_input: SetupUserInput):
    """Resolve registered device support for setup."""

    path = Path(user_input.datasheet_path)
    try:
        digest = sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise PackProvisionError(f"could not read datasheet evidence: {exc}") from exc
    pending_builtin = _setup_builtin_candidates.get(user_input.board_id)
    if pending_builtin is not None:
        if normalize_part_number(pending_builtin.part_number) != normalize_part_number(
            user_input.mcu_part_number
        ):
            raise PackProvisionError("pending built-in target belongs to a different MCU part")
        return _ResolvedGenericSetupSupport(pending_builtin, path, digest)
    try:
        existing_generic = _profile_repository.load(user_input.board_id)
    except ProfileError:
        existing_generic = None
    if existing_generic is not None and existing_generic.device_support is not None:
        if normalize_part_number(existing_generic.mcu_part_number or "") != normalize_part_number(
            user_input.mcu_part_number
        ):
            raise PackProvisionError("existing generic profile belongs to a different MCU part")
        if existing_generic.to_document().get("datasheet_sha256") != digest:
            raise PackProvisionError("setup datasheet does not match the existing generic profile")
        return _ResolvedGenericSetupSupport(
            _replay_profile_device_support(existing_generic), path, digest
        )
    try:
        candidate = resolve_available_pack_support(
            _profile_repository.store, user_input.mcu_part_number
        )
    except PackProvisionError:
        # There is intentionally no reference-board or part catalog fallback.
        # A normal pyOCD setup needs replayable support evidence; a provider
        # recipe can instead report capability-aware live identity through its worker.
        raise
    return _ResolvedGenericSetupSupport(candidate, path, digest)


def _setup_inventory(user_input: SetupUserInput) -> PreflightInventory:
    if user_input.provider_recipe is not None:
        try:
            recipe = ProviderRecipe.from_record(user_input.provider_recipe)
            matches = [
                item
                for item in run_provider_inventory(recipe)
                if item.namespaced_id == user_input.connection_id
            ]
        except ProviderRecipeError as exc:
            return PreflightInventory(
                blocking_error=PreflightBlock("setup/provider-inventory-invalid", str(exc))
            )
        if len(matches) != 1:
            return PreflightInventory(
                blocking_error=PreflightBlock(
                    "setup/provider-connection-not-current",
                    "The selected provider connection was not returned by this current inventory. "
                    "Call get_setup_overview with the recipe and choose one exact connection_id.",
                )
            )
        item = matches[0]
        target = _setup_target_overrides.get(user_input.board_id)
        return PreflightInventory(
            probes=(
                ProbeCandidate(
                    item.namespaced_id,
                    item.description,
                    item.probe_family,
                    item.probe_uid,
                ),
            ),
            exact_detected_targets=(target,) if target else (),
        )
    support: object | None
    try:
        support = _resolve_setup_support(user_input)
    except PackProvisionError as exc:
        return PreflightInventory(
            blocking_error=PreflightBlock(
                "setup/device-support-invalid",
                f"The previously pinned exact device-support record failed replay: {exc}",
            )
        )
    except (OSError, ValueError) as exc:
        return PreflightInventory(
            blocking_error=PreflightBlock(
                "setup/datasheet-evidence-invalid",
                f"The local official datasheet could not be accepted: {exc}",
            )
        )
    generic_support = cast(_ResolvedGenericSetupSupport, support)
    validation_inventory = _validation_inventory()
    probes = tuple(
        ProbeCandidate(
            probe.probe_id,
            probe.description,
            probe.probe_family,
            probe.usb_serial,
        )
        for probe in validation_inventory.probes
    )
    selected_probes = tuple(
        probe for probe in probes if _connection_matches_probe(user_input.connection_id, probe)
    )
    # connection_id is an explicit immutable setup-run identity. Zero matches
    # must remain empty rather than falling back to a different attached probe.
    probes = selected_probes
    serial = tuple(
        SerialCandidate(
            port.serial_id,
            port.port_path,
            port.description,
            port.usb_serial,
            port.vid,
            port.pid,
            external_adapter=not any(
                _stable_identity_equal(port.usb_serial, probe.usb_serial)
                for probe in probes
                if probe.usb_serial is not None
            ),
            provably_mapped=any(
                _stable_identity_equal(port.usb_serial, probe.usb_serial)
                for probe in probes
                if probe.usb_serial is not None
            ),
        )
        for port in validation_inventory.serial_ports
    )
    if user_input.serial_id:
        serial = tuple(
            item for item in serial if _stable_identity_equal(user_input.serial_id, item.serial_id)
        )
    if len(probes) == 1 and probes[0].usb_serial:
        matching_serial = tuple(
            item for item in serial if _stable_identity_equal(probes[0].usb_serial, item.usb_serial)
        )
        if matching_serial:
            serial = matching_serial
    cache_resolution = CacheResolution(False, "no_record")
    if len(probes) == 1 and serial:
        cache_resolution = _attachment_cache.resolve(
            user_input.board_id,
            ProbeIdentity(probes[0].probe_family, probes[0].usb_serial),
            [
                SerialEndpoint(item.port_path, item.usb_serial, item.vid, item.pid)
                for item in serial
            ],
        )
    targets = _target_names()
    manifest_specs = tuple(load_manifest(_firm_store.layout.pack_manifest))
    manifest_targets = tuple(
        sorted({target.casefold() for pack in manifest_specs for target in pack.provides_targets})
    )
    exact: tuple[str, ...] = ()
    reviewed_target = generic_support.candidate.pyocd_target.casefold()
    exact = (reviewed_target,)
    target_override = _setup_target_overrides.get(user_input.board_id)
    if target_override is not None:
        if target_override == reviewed_target:
            exact = (target_override,)
    return PreflightInventory(
        probes=probes,
        serial_ports=serial,
        cache_resolution=cache_resolution,
        built_in_targets=tuple(target for target in targets if target not in manifest_targets),
        manifest_targets=manifest_targets,
        exact_detected_targets=exact,
    )


def _mcu_family(mcu_part_number: str, target: str) -> str:
    normalized = _normalized_target_identity(mcu_part_number)
    return normalized or _normalized_target_identity(target)


def _setup_provider_recipe_connection_phase(context: SetupPhaseContext) -> SetupPhaseOutcome:
    """Commit a generic route only after one worker proves its current facts.

    The generic adapter owns target-specific ID acquisition.  The parent does
    not guess a universal ID register: it binds the exact provider observation,
    current session token, selected inventory connection, and recipe target.
    """

    raw_recipe = context.user_input.provider_recipe
    target = context.preflight.selected_target
    probe = context.preflight.selected_probe
    if raw_recipe is None or target is None or probe is None:
        return SetupPhaseOutcome.stop(
            "setup_connection_failed",
            "setup/provider-input-missing",
            "Provider recipe, selected connection, or target is missing. Restart setup from get_setup_overview.",
        )
    try:
        recipe = ProviderRecipe.from_record(raw_recipe)
        selected = [
            item
            for item in run_provider_inventory(recipe)
            if item.namespaced_id == context.user_input.connection_id
        ]
        if len(selected) != 1:
            raise ProviderRecipeError(
                "selected connection is stale or absent from provider inventory"
            )
        connection = selected[0]
        support_identity = recipe.support_identity(target)
        board = BoardConfig(
            board_id=context.user_input.board_id,
            display_name=context.user_input.display_name,
            mcu_family=_mcu_family(context.user_input.mcu_part_number, target),
            probe_family=connection.probe_family,
            provider_id=recipe.provider_id,
            target=target,
            probe_type=connection.description,
            silicon_id_bound_part_number=context.user_input.mcu_part_number,
            provider_support_identity=support_identity,
            default_baudrate=context.user_input.serial_baudrate or 115200,
        )
        handle = target_control.open_session(
            board=board,
            unique_id=connection.connection_id,
            target=target,
            worker_argv=recipe.worker_argv,
        )
        try:
            metadata = session_metadata(handle)
            try:
                observe_live_identity(
                    handle,
                    read_memory=None,
                    configured_part_number=context.user_input.mcu_part_number,
                )
            except LiveIdentityContradiction as exc:
                raise ProviderRecipeError(
                    f"provider live identity contradicts the selected recipe support: {exc}"
                ) from exc
            except LiveIdentityObservationError as exc:
                raise ProviderRecipeError(
                    "provider live identity could not be observed before comparing the selected "
                    f"recipe support; reconnect and retry: {exc}"
                ) from exc
            identity = metadata.live_identity
            if not isinstance(identity, dict):
                raise ProviderRecipeError("provider worker did not return live identity evidence")
            regions = target_control.physical_memory_regions(handle)
            if any(region.session_token != metadata.runtime_token for region in regions):
                raise ProviderRecipeError(
                    "provider physical-memory evidence belongs to another session"
                )
        finally:
            target_control.close_session(handle)
        support = {
            "kind": "provider_recipe",
            "support_id": support_identity,
            "provider_id": recipe.provider_id,
            "target": target,
            "part_number": context.user_input.mcu_part_number,
        }
        # Inventory and live-worker observation above remain outside this lock.
        # Only the coupled recipe/profile publication must serialize so a failed
        # setup cannot roll back another completed recipe publication.
        with safety_publication_lock(_project_root, board.board_id):
            with provider_recipe_publication_lock(_project_root):
                try:
                    existing = _profile_repository.load(board.board_id)
                except ProfileError:
                    existing = None
                previous_recipes = _provider_recipe_store.snapshot()
                try:
                    # Publish the dependency first: a profile is never made visible
                    # with a support binding whose recipe is absent.
                    _provider_recipe_store.save(recipe)
                except ProviderRecipeError as exc:
                    raise ProviderRecipeError(
                        f"recipe write failed before profile mutation; profile remains unchanged: {exc}"
                    ) from exc
                try:
                    if existing is None:
                        _profile_repository.commit_core(
                            _profile_repository.stage_core(
                                {
                                    "board_id": board.board_id,
                                    "display_name": board.display_name,
                                    "mcu_part_number": context.user_input.mcu_part_number,
                                    "mcu_family": board.mcu_family,
                                    "probe_family": board.probe_family,
                                    "provider_id": board.provider_id,
                                    "target": board.target,
                                    **(
                                        {"serial_baudrate": context.user_input.serial_baudrate}
                                        if context.user_input.requires_uart
                                        else {}
                                    ),
                                }
                            )
                        )
                    committed = _profile_repository.commit_optional(
                        _profile_repository.stage_optional(
                            board.board_id,
                            {
                                "device_support": support,
                                "provider_live_identity": identity,
                            },
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - paired publication must roll back every failure
                    rollback_errors: list[str] = []
                    try:
                        _profile_repository.restore_snapshot(board.board_id, existing)
                    except Exception as rollback_exc:  # noqa: BLE001 - retain primary setup failure
                        rollback_errors.append(
                            f"profile rollback: {type(rollback_exc).__name__}: {rollback_exc}"
                        )
                    try:
                        _provider_recipe_store.restore_snapshot(previous_recipes)
                    except Exception as rollback_exc:  # noqa: BLE001 - retain primary setup failure
                        rollback_errors.append(
                            f"recipe rollback: {type(rollback_exc).__name__}: {rollback_exc}"
                        )
                    residual = (
                        "rollback completed" if not rollback_errors else "; ".join(rollback_errors)
                    )
                    raise ProviderRecipeError(
                        f"profile publication failed after recipe write: {exc}; {residual}"
                    ) from exc
    except (ProviderRecipeError, ProfileError, TargetConnectionError, ValueError) as exc:
        return SetupPhaseOutcome.stop(
            "setup_connection_failed",
            "setup/provider-live-evidence-failed",
            f"Provider recipe setup did not produce a committed profile: {exc}",
        )
    return SetupPhaseOutcome.success(
        "setup/provider-profile-committed-after-live-connect",
        profile=committed.source_path.name,
        provider_id=recipe.provider_id,
        target=target,
        live_identity=identity,
        physical_regions=[region.to_record() for region in regions],
    )


def _setup_connection_phase(context: SetupPhaseContext) -> SetupPhaseOutcome:
    if context.user_input.provider_recipe is not None:
        return _setup_provider_recipe_connection_phase(context)
    try:
        support = _resolve_setup_support(context.user_input)
        generic_support = cast(_ResolvedGenericSetupSupport, support)
        actual_datasheet_hash = support.datasheet_sha256
        generic_geometry = resolve_device_support_geometry(
            generic_support.candidate, _profile_repository.store
        )
        generic_pack = (
            verified_pack_for_candidate(generic_support.candidate, _profile_repository.store)
            if isinstance(generic_support.candidate, DeviceSupportCandidate)
            else None
        )
    except (PackProvisionError, OSError, ValueError) as exc:
        return SetupPhaseOutcome.stop(
            "setup_blocked",
            getattr(exc, "code", "setup/device-support-evidence-mismatch"),
            f"The exact MCU and server-hashed datasheet did not match verified device support: {exc}",
        )
    try:
        existing = _profile_repository.load(context.user_input.board_id)
    except ProfileError:
        existing = None
    target = context.preflight.selected_target
    probe = context.preflight.selected_probe
    if target is None or probe is None:
        return SetupPhaseOutcome.stop(
            "setup_connection_failed",
            "setup/connection-input-missing",
            "Target or probe resolution is incomplete; stop before committing a profile.",
        )
    expected_target = generic_support.candidate.pyocd_target
    if target.casefold() != expected_target.casefold():
        remedy = (
            "The selected target does not match the resolved MCU/device support. "
            "Use the server-derived target before retrying setup."
        )
        return SetupPhaseOutcome.stop(
            "setup_connection_failed",
            "setup/device-support-route-mismatch",
            remedy,
            details={
                "selected_target": target,
                "expected_target": expected_target,
                "selected_probe_family": probe.probe_family,
            },
        )

    if existing is not None and (
        (
            existing.mcu_part_number is not None
            and existing.mcu_part_number != context.user_input.mcu_part_number
        )
        or _profile_name_key(existing.display_name)
        != _profile_name_key(context.user_input.display_name)
        or existing.board.target.casefold() != target.casefold()
    ):
        return SetupPhaseOutcome.stop(
            "setup_connection_failed",
            "setup/existing-profile-identity-mismatch",
            "Repair inputs do not match the established profile identity. Stop rather than "
            "rewriting the profile; restart from get_setup_overview with the recorded board name.",
            details={
                "expected_display_name": existing.display_name,
                "expected_mcu_part_number": existing.mcu_part_number,
                "expected_target": existing.board.target,
            },
        )

    opened: list[TargetSessionHandle] = []
    captured_datasheet_ref: str | None = None
    identity_proof = (
        generic_support.candidate.identity_proof if generic_support is not None else None
    )
    setup_board = BoardConfig(
        board_id=context.user_input.board_id,
        display_name=context.user_input.display_name,
        mcu_family=_mcu_family(context.user_input.mcu_part_number, target),
        probe_family=probe.probe_family,
        target=target,
        probe_type=probe.description,
        probe_hint_terms=(),
        serial_hint_terms=(),
        test_addr=generic_geometry.flash_start,
        silicon_id_addr=identity_proof.address if identity_proof is not None else None,
        silicon_id_expected=identity_proof.expected if identity_proof is not None else None,
        silicon_id_mask=identity_proof.mask if identity_proof is not None else None,
        silicon_id_width_bits=identity_proof.width_bits if identity_proof is not None else 32,
        silicon_id_label=identity_proof.label if identity_proof is not None else "",
        default_baudrate=115200,
        debug_protocol=None,
        debug_connect_mode=None,
        debug_clock_hz=None,
    )

    def connect(candidate_target: str, _pack_path: str | None) -> None:
        nonlocal captured_datasheet_ref, setup_board
        selected_policy = _setup_attachment_overrides.get(context.user_input.board_id)
        attempts = (
            (selected_policy,)
            if selected_policy is not None
            else (
                (None, "attach", None),
                (None, "attach", 1_000_000),
                (None, "under-reset", 1_000_000),
            )
        )
        handle: TargetSessionHandle | None = None
        last_error: TargetConnectionError | None = None
        for protocol, mode, frequency in attempts:
            try:
                handle = target_control.open_session(
                    board=setup_board,
                    unique_id=probe.usb_serial,
                    target=candidate_target,
                    protocol=protocol,
                    connect_mode=mode,
                    frequency_hz=frequency,
                    pack_path=(generic_pack.path if generic_pack is not None else None),
                    pack_sha256=(generic_pack.spec.sha256 if generic_pack is not None else None),
                    pdsc_device=(
                        cast(
                            DeviceSupportCandidate,
                            cast(_ResolvedGenericSetupSupport, support).candidate,
                        ).pdsc_device
                        if isinstance(
                            cast(_ResolvedGenericSetupSupport, support).candidate,
                            DeviceSupportCandidate,
                        )
                        else None
                    ),
                )
            except TargetConnectionError as exc:
                last_error = exc
                continue
            _setup_attachment_overrides[context.user_input.board_id] = (
                protocol,
                mode or "attach",
                frequency,
            )
            break
        if handle is None:
            assert last_error is not None
            raise last_error
        opened.append(handle)
        try:
            selected_probe_uid = probe.usb_serial or probe.probe_id
            if not _stable_identity_equal(selected_probe_uid, session_metadata(handle).probe_uid):
                raise TargetConnectionError(
                    "Live debug connection identity changed during setup; stop before reading "
                    "silicon or committing a profile. Restart get_setup_overview with the current "
                    "friendly connection inventory."
                )
            if generic_support.candidate.identity_proof is None:
                raw_cpuid = target_control.read_memory(handle, 0xE000ED00, 32)
                try:
                    observed_cpuid = validate_identity_observation(raw_cpuid, 32)
                except IdentityObservationError as exc:
                    raise PackProvisionError(
                        "Live CPUID identity observation is malformed; reconnect and retry setup "
                        "before publishing support or profile evidence."
                    ) from exc
                proof = live_cpuid_compatibility_proof(observed_cpuid)
                if isinstance(generic_support.candidate, BuiltInTargetSupportCandidate):
                    generic_support.candidate = generic_support.candidate.with_identity_proof(
                        observed_cpuid
                    )
                    optional_fields["device_support"] = (
                        generic_support.candidate.to_authority_document()
                    )
                setup_board = replace(
                    setup_board,
                    silicon_id_addr=proof.address,
                    silicon_id_expected=proof.expected,
                    silicon_id_mask=proof.mask,
                    silicon_id_width_bits=proof.width_bits,
                    silicon_id_label=proof.label,
                )
                optional_fields.update(
                    {
                        "silicon_id_address": proof.address,
                        "silicon_id_expected": proof.expected,
                        "silicon_id_mask": proof.mask,
                        "silicon_id_width_bits": proof.width_bits,
                        "silicon_id_label": proof.label,
                    }
                )
            if setup_board.silicon_id_addr is not None:
                raw_observed = target_control.read_memory(
                    handle, setup_board.silicon_id_addr, setup_board.silicon_id_width_bits
                )
                try:
                    observed = validate_identity_observation(
                        raw_observed, setup_board.silicon_id_width_bits
                    )
                except IdentityObservationError as exc:
                    raise PackProvisionError(
                        "Live replayed silicon identity observation is malformed; reconnect and "
                        "retry setup before publishing profile evidence."
                    ) from exc
                expected = setup_board.silicon_id_expected
                assert expected is not None
                assert setup_board.silicon_id_mask is not None
                if (observed & setup_board.silicon_id_mask) != (
                    expected & setup_board.silicon_id_mask
                ):
                    raise TargetConnectionError(
                        "Live silicon identity did not match the verified device support "
                        f"(observed 0x{observed:08X}, expected 0x{expected:08X})."
                    )
            assert setup_board.test_addr is not None
            target_control.read_memory(handle, setup_board.test_addr, 32)
            evidence = capture_datasheet_evidence(
                _profile_repository.store, Path(context.user_input.datasheet_path)
            )
            if evidence.sha256 != actual_datasheet_hash:
                raise PackProvisionError("datasheet bytes changed during live setup")
            captured_datasheet_ref = evidence.reference
        finally:
            target_control.close_session(handle)

    optional_fields = {
        "test_read_address": setup_board.test_addr,
        "datasheet_sha256": actual_datasheet_hash,
        "datasheet_ref": (
            _profile_repository.store.layout.datasheet_evidence(actual_datasheet_hash)
            .relative_to(_profile_repository.store.layout.project_root)
            .as_posix()
        ),
        **(
            {"device_support": generic_support.candidate.to_authority_document()}
            if generic_support.candidate.identity_proof is not None
            else {}
        ),
        **(
            {
                "silicon_id_address": setup_board.silicon_id_addr,
                "silicon_id_expected": setup_board.silicon_id_expected,
                "silicon_id_mask": setup_board.silicon_id_mask,
                "silicon_id_width_bits": setup_board.silicon_id_width_bits,
                "silicon_id_label": setup_board.silicon_id_label,
            }
            if setup_board.silicon_id_addr is not None
            else {}
        ),
    }
    coordinator = ProfileCommitCoordinator(
        _profile_repository,
        live_connect=connect,
        before_commit=cancellation_checkpoint,
    )
    try:
        if existing is None:
            committed = coordinator.commit_core(
                {
                    "board_id": context.user_input.board_id,
                    "display_name": context.user_input.display_name,
                    "mcu_part_number": context.user_input.mcu_part_number,
                    "mcu_family": _mcu_family(context.user_input.mcu_part_number, target),
                    "probe_family": probe.probe_family,
                    "provider_id": "pyocd",
                    "target": target,
                    **(
                        {"serial_baudrate": context.user_input.serial_baudrate}
                        if context.user_input.requires_uart
                        else {}
                    ),
                }
            )
        else:
            # A repair never trusts the old partial commit as current hardware proof.
            # Re-run the same bounded live identity/read checks before enriching it.
            connect(target, None)
            committed = existing
        protocol, mode, frequency = _setup_attachment_overrides[context.user_input.board_id]
        if protocol is not None:
            optional_fields["debug_protocol"] = protocol
        optional_fields["debug_connect_mode"] = mode
        if frequency is not None:
            optional_fields["debug_clock_hz"] = frequency
        cancellation_checkpoint()
        committed = _profile_repository.commit_optional(
            _profile_repository.stage_optional(
                context.user_input.board_id,
                optional_fields,
            )
        )
        if captured_datasheet_ref != committed.to_document().get("datasheet_ref"):
            raise ProfileError("captured datasheet evidence was not committed canonically")
    except Exception as exc:  # noqa: BLE001 - workflow records the typed terminal result
        current: BaseException | None = exc
        connection_failure = False
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if isinstance(current, TargetConnectionError):
                connection_failure = True
                break
            current = current.__cause__ or current.__context__
        return SetupPhaseOutcome.stop(
            "setup_connection_failed",
            "setup/live-connect-failed",
            (
                "The previously live-tested target attachment failed before profile commit. "
                f"Check the probe/cabling and restart setup: {exc}"
                if connection_failure
                else f"Live connection failed before the profile could be committed: {exc}"
            ),
        )
    return SetupPhaseOutcome.success(
        "setup/core-profile-committed-after-connect",
        profile=committed.source_path.name,
        live_connections=len(opened),
        datasheet_sha256=actual_datasheet_hash,
        attachment_policy={
            "protocol": committed.board.debug_protocol,
            "connect_mode": committed.board.debug_connect_mode,
            "frequency_hz": committed.board.debug_clock_hz,
        },
    )


def _setup_validation_phase(context: SetupPhaseContext) -> SetupPhaseOutcome:
    selected_probe = context.preflight.selected_probe
    result = _board_validator.validate(
        ValidationRequest(
            context.user_input.board_id,
            selected_probe.probe_id if selected_probe is not None else None,
        )
    )
    configuration_only = (
        result.status == "validation_blocked"
        and result.code == "validation/live-identity-evidence-missing"
        and result.observed.get("capability_level") == "connected_diagnostics_only"
    )
    if result.status == "validation_passed" or configuration_only:
        return SetupPhaseOutcome.success(
            "setup/non-destructive-hardware-validation-passed",
            validation_status=result.status,
            capability_level=(
                "connected_diagnostics_only" if configuration_only else "identity_verified"
            ),
            validation_report=str(result.report_paths.report),
        )
    return SetupPhaseOutcome.stop(
        "setup_validation_failed",
        "setup/validation-failed",
        result.agent_prompt,
        details={"validation_status": result.status, "validation_code": result.code},
    )


def _setup_commit_phase(context: SetupPhaseContext) -> SetupPhaseOutcome:
    board_id = context.user_input.board_id
    try:
        probe = context.preflight.selected_probe
        result = _board_validator.validate(
            ValidationRequest(
                board_id,
                probe.probe_id if probe is not None else None,
            )
        )
    except Exception as exc:  # noqa: BLE001 - setup records the terminal report
        return SetupPhaseOutcome.stop(
            "setup_validation_failed",
            "setup/final-validation-failed",
            f"Final validation failed: {exc}",
        )
    configuration_only = (
        result.status == "validation_blocked"
        and result.code == "validation/live-identity-evidence-missing"
        and result.observed.get("capability_level") == "connected_diagnostics_only"
    )
    if result.status != "validation_passed" and not configuration_only:
        return SetupPhaseOutcome.stop(
            "setup_validation_failed",
            "setup/final-validation-failed",
            result.agent_prompt,
            details={"validation_status": result.status, "validation_code": result.code},
        )
    return SetupPhaseOutcome.success(
        (
            "setup/final-validation-configuration-only"
            if configuration_only
            else "setup/final-validation-passed"
        ),
        validation_status=result.status,
        capability_level=(
            "connected_diagnostics_only" if configuration_only else "identity_verified"
        ),
        validation_report=str(result.report_paths.report),
    )


def _build_firmware_guidance() -> dict[str, object]:
    """Return the sole client-visible build contract used by setup output."""

    # This is the sole public build contract: direct argv and evidence, never a
    # second CLI template with stricter output-discovery rules.
    return {
        "tool": "build_firmware",
        "arguments_template": {
            "project_dir": "<project-root>",
            "build_dir": "<build-output-dir>",
            "command": ["<build-executable>", "<arguments>"],
            "working_dir": None,
            "environment": None,
            "artifacts": None,
            "timeout_seconds": None,
        },
        "guidance": (
            "Call build_firmware with the project's exact direct argv. It runs without a shell and "
            "with closed stdin: builds obtain required input only through argv, cwd, and environment, "
            "never the MCP protocol stream. It "
            "discovers every supported output it finds, and reports a successful zero-exit build "
            "with artifacts=[] when no supported output exists. The optional artifacts mapping is "
            "caller-supplied provenance evidence, not a build-success precondition."
        ),
        "artifact_collection": {
            "tool": "collect_build_artifacts",
            "arguments_template": {
                "output_dir": "<new-or-empty-collection-dir>",
                "elf_path": "<produced-elf-or-null>",
                "hex_path": "<produced-hex-or-null>",
                "bin_path": "<produced-bin-or-null>",
                "map_path": "<produced-map-or-null>",
                "expected_roles": None,
            },
            "purpose": "Optionally normalize explicit existing outputs into a hashed bundle.",
        },
        "flash_boundary": (
            "Pass the selected ELF, AXF, or HEX path to flash_firmware. Flash independently "
            "revalidates artifact bytes, live writable flash containment, and current capability-aware identity evidence."
        ),
    }


def _get_setup_status(board_id: str) -> Mapping[str, object]:
    """Return profile/configuration facts and current connection evidence."""

    configuration_ready = False
    configuration_reason = "no profile is persisted for this board"
    profile: BoardProfile | None = None
    try:
        profile = _profile_repository.load(board_id)
        configuration_ready = True
        configuration_reason = "profile is persisted; direct operations re-check live target facts"
    except ProfileError as exc:
        configuration_reason = str(exc)

    connection = connection_manager.maybe_connection(board_id)
    live_session_ready = connection is not None
    identity_capability: str | None = None
    identity_comparison: str | None = None
    identity_evidence: dict[str, object] | None = None
    ready_for_flash = False
    uart_attachment_ready = False
    uart_reason = "UART attachment has not been resolved for this live board connection"
    resolved_uart: dict[str, object] | None = None
    resolved_probe: dict[str, object] | None = None
    safety_map: dict[str, object] = {
        "state": "missing",
        "digest": None,
        "remedy": (
            "Request a routine plan for refresh_safety_map(board_id=..., "
            "layout_path=None, application_elf_path=None), then call refresh_safety_map "
            "with that plan_id after connecting."
        ),
    }
    if profile is not None and profile.safety_ref:
        if connection is None:
            safety_map.update(
                state="stale",
                remedy="Connect and refresh_safety_map to recheck this profile-associated map against live identity and regions.",
            )
        else:
            try:
                safety_map.update(
                    state="current", **_safety_authority.binding(board_id, connection.handle)
                )
                safety_map["digest"] = safety_map.pop("safety_map_digest")
                safety_map["remedy"] = "Map is current for this live session."
            except SafetyAuthorityError as exc:
                state = "conflicted" if "conflict" in str(exc).casefold() else "stale"
                safety_map.update(state=state, remedy=f"refresh_safety_map: {exc}")
    if profile is not None and connection is not None:
        try:
            identity = observe_live_identity(
                connection.handle,
                read_memory=target_control.read_memory,
                configured_part_number=profile.mcu_part_number,
            ).to_record()
            identity_capability = str(identity["capability"])
            identity_comparison = str(identity["comparison_status"])
            identity_evidence = identity
            ready_for_flash = safety_map.get("state") == "current"
        except LiveIdentityContradiction as exc:
            identity_capability = None
            identity_comparison = "contradicted"
            identity_evidence = {"kind": "contradiction", "reason": str(exc)}
        except (LiveIdentityObservationError, TargetStateError) as exc:
            identity_capability = None
            identity_comparison = "unavailable"
            identity_evidence = {"kind": "observation-failed", "reason": str(exc)}
        except Exception as exc:  # noqa: BLE001 - status is diagnostic only
            identity_capability = None
            identity_comparison = "unavailable"
            identity_evidence = {"kind": "unavailable", "reason": str(exc)}
    elif profile is not None:
        identity_capability = profile.board.silicon_id_capability
        identity_comparison = "unavailable"
        identity_evidence = {"kind": "unavailable", "reason": "no current live session"}
    if profile is not None and connection is not None:
        try:
            inventory = _validation_inventory()
            endpoints = [
                SerialEndpoint(item.port_path, item.usb_serial, item.vid, item.pid)
                for item in inventory.serial_ports
            ]
            probe_uid = getattr(connection.handle, "probe_uid", None)
            if isinstance(probe_uid, str) and probe_uid.strip():
                resolved_probe = {
                    "probe_uid": probe_uid,
                    "connection_id": connection.connection_id,
                    "probe_family": profile.board.probe_family,
                }
            resolution = _attachment_cache.resolve(
                board_id,
                ProbeIdentity(profile.board.probe_family, probe_uid),
                endpoints,
            )
            direct_matches = [
                item
                for item in inventory.serial_ports
                if _stable_identity_equal(probe_uid, item.usb_serial)
            ]
            selected_uart = next(
                (
                    item
                    for item in inventory.serial_ports
                    if resolution.reused
                    and resolution.port_path
                    and item.port_path.casefold() == resolution.port_path.casefold()
                ),
                direct_matches[0] if len(direct_matches) == 1 else None,
            )
            uart_attachment_ready = selected_uart is not None
            if uart_attachment_ready:
                uart_reason = "A stable UART attachment resolves to one current port"
                assert selected_uart is not None
                resolved_uart = {
                    "serial_id": selected_uart.serial_id,
                    "usb_serial": selected_uart.usb_serial,
                    "port_path": selected_uart.port_path,
                    "vid": selected_uart.vid,
                    "pid": selected_uart.pid,
                }
            elif len(inventory.serial_ports) == 0:
                uart_reason = "No UART port is currently visible"
            elif len(inventory.serial_ports) > 1:
                uart_reason = "UART attachment is ambiguous; confirm one friendly choice in setup"
        except Exception as exc:  # noqa: BLE001 - readiness is diagnostic, never authority
            uart_reason = f"UART attachment could not be resolved: {exc}"
    if not configuration_ready:
        remedy = "Call setup_board to persist a profile for this board."
    elif connection is None:
        remedy = "Call connect_board, then validate_board, in the current server run."
    else:
        remedy = "A connection is present. Validation remains diagnostic; each operation checks its live facts."
    return {
        "status": "setup_connected" if live_session_ready else "setup_disconnected",
        "board_id": board_id,
        "configuration_ready": configuration_ready,
        "live_session_ready": live_session_ready,
        "identity_capability": identity_capability,
        "identity_comparison_status": identity_comparison,
        "identity_evidence": identity_evidence,
        "ready_for_flash": ready_for_flash,
        "ready_for_code": configuration_ready and live_session_ready,
        "uart_attachment_ready": uart_attachment_ready,
        "ready_for_uart_work": (
            configuration_ready and live_session_ready and uart_attachment_ready
        ),
        "uart_reason": uart_reason,
        "resolved_uart": resolved_uart,
        "resolved_probe": resolved_probe,
        "configuration_reason": configuration_reason,
        "safety_map": safety_map,
        "remedy": remedy,
        "build_guidance": _build_firmware_guidance(),
    }


def _profile_name_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def _profile_needs_repair(profile: BoardProfile) -> bool:
    """Return whether setup must restore missing durable configuration evidence."""

    digest = profile.to_document().get("datasheet_sha256")
    return not isinstance(digest, str)


def _replace_setup_assignments(
    bindings: Mapping[str, str],
    reason: str,
    *,
    drop_omitted_active: bool = False,
) -> None:
    """Replace provisional routing and cancel stale setup runs."""

    previous = assignment_store.bindings()
    replacement = dict(bindings)

    # get_setup_overview may legitimately be called for one board while other boards are already
    # connected and validated. Omission is not a disconnect request: retain every omitted active
    # one-to-one binding unless the new overview explicitly reuses that board or connection.
    # This keeps independent multi-board sessions alive while the agent works on them in turn.
    if not drop_omitted_active:
        replacement_boards = set(replacement.values())
        for connected_board in connection_manager.assigned_board_ids():
            if connected_board in replacement_boards:
                continue
            previous_connection = next(
                (
                    connection_id
                    for connection_id, board_id in previous.items()
                    if board_id == connected_board
                ),
                None,
            )
            if previous_connection is None or previous_connection in replacement:
                continue
            replacement[previous_connection] = connected_board
    if previous == replacement:
        return
    previous_by_board = {board_id: connection_id for connection_id, board_id in previous.items()}
    replacement_by_board = {
        board_id: connection_id for connection_id, board_id in replacement.items()
    }
    affected = {
        board_id
        for board_id in set(previous_by_board) | set(replacement_by_board)
        if previous_by_board.get(board_id) != replacement_by_board.get(board_id)
    }
    workflow = globals().get("_setup_workflow")
    if isinstance(workflow, SetupWorkflow):
        for board_id in affected:
            workflow.cancel_board(board_id, reason)

    def replacement_owner(connection: ManagedConnection) -> str | None:
        for candidate, board_id in replacement.items():
            if _selected_setup_connection_matches(candidate, connection):
                return board_id
        return None

    # A provisional reassignment must retire any conflicting physical session before
    # publishing the new one-to-one mapping. Otherwise always-visible read/reset tools
    # could still reach the probe through its former logical board.
    for connected_board in connection_manager.assigned_board_ids():
        connection = connection_manager.connection_for(connected_board)
        if replacement_owner(connection) == connected_board:
            continue
        affected.add(connected_board)
        if isinstance(workflow, SetupWorkflow):
            workflow.cancel_board(connected_board, reason)
        with connection_manager.lock_for(connected_board):
            current = connection_manager.maybe_connection(connected_board)
            if current is not connection:
                continue
            connection_manager.clear(connected_board)
            try:
                target_control.close_session(connection.handle)
            finally:
                _session_store.close_session(connection.runtime_session)
    assignment_store.replace(replacement)


def _same_setup_connection(left: str, right: str) -> bool:
    """Compare server-issued setup connection IDs without broad target inference."""

    selected = left.strip()
    observed = right.strip()
    if selected.casefold().startswith("probe:") and observed.casefold().startswith("probe:"):
        return _stable_identity_equal(selected.split(":", 1)[1], observed.split(":", 1)[1])
    # Recipe routes are opaque server-issued identifiers, unlike built-in probe
    # serials. Preserve their exact case after harmless surrounding whitespace.
    return selected == observed


def _setup_connection_key(connection_id: str) -> str:
    """Canonical key for one server-issued setup connection identity."""

    selected = connection_id.strip()
    if not selected.casefold().startswith("probe:"):
        return selected
    probe_identity = selected.split(":", 1)[1].casefold()
    if probe_identity.isdecimal():
        probe_identity = probe_identity.lstrip("0") or "0"
    return f"probe:{probe_identity}"


def _selected_setup_connection_matches(
    selected_connection: str,
    connection: ManagedConnection,
) -> bool:
    """Match a selected inventory row to a live connection's immutable probe identity."""

    if _same_setup_connection(selected_connection, connection.connection_id):
        return True
    probe_uid = (session_metadata(connection.handle).probe_uid or "").strip()
    return bool(probe_uid) and _same_setup_connection(
        selected_connection,
        f"probe:{probe_uid}",
    )


def _proposed_board_id(display_name: str, existing: set[str]) -> str:
    ascii_name = unicodedata.normalize("NFKD", display_name).encode("ascii", "ignore").decode()
    # A board id is a persisted path component, not a policy-sized user input.
    stem = re.sub(r"[^a-z0-9]+", "_", ascii_name.casefold()).strip("_")
    if not stem:
        stem = f"board_{hashlib.sha256(display_name.encode('utf-8')).hexdigest()[:8]}"
    candidate = stem
    counter = 2
    while candidate in existing:
        suffix = f"_{counter}"
        candidate = f"{stem}{suffix}"
        counter += 1
    return candidate


def _setup_overview(
    board_names: list[str] | None,
    connection_assignments: Mapping[str, str] | None = None,
    provider_recipe: dict[str, object] | None = None,
) -> Mapping[str, object]:
    """Give an agent the complete startup route without asking the user for internals."""

    provider_connections: list[dict[str, object]] = []
    provider_inventory_error: str | None = None
    if provider_recipe is not None:
        try:
            recipe = ProviderRecipe.from_record(provider_recipe)
            observed = run_provider_inventory(recipe)
            with provider_recipe_publication_lock(_project_root):
                _provider_recipe_store.save(recipe)
            provider_connections = [connection.to_record() for connection in observed]
        except ProviderRecipeError as exc:
            # Inventory execution/parsing is physical evidence.  A failure must
            # be visible and must not publish a recipe that did not inventory.
            provider_inventory_error = str(exc)

    try:
        profiles = _profile_repository.load_all()
    except ProfileError as exc:
        return {
            "status": "setup_overview_blocked",
            "agent_prompt": (
                f"The board-profile index is invalid: {exc}. Explain the profile problem plainly "
                "and correct it before hardware access."
            ),
            "profiles": [],
            "connections": [],
            "routes": [],
            "build_guidance": _build_firmware_guidance(),
        }
    profile_rows: list[dict[str, object]] = []
    by_name: dict[str, tuple[BoardProfile, str, str]] = {}
    for profile in profiles:
        complete = not _profile_needs_repair(profile)
        route_kind = "validate" if complete else "repair"
        reason = (
            "profile is persisted; validate the currently assigned probe"
            if complete
            else "incomplete profile; run direct setup or repair"
        )
        profile_rows.append(
            {
                "board_id": profile.board_id,
                "display_name": profile.display_name,
                "mcu_part_number": profile.mcu_part_number,
                "configuration_complete": complete,
                "route_reason": reason,
            }
        )
        by_name[_profile_name_key(profile.display_name)] = (profile, route_kind, reason)

    try:
        inventory = _validation_inventory()
        connection_rows_by_identity: dict[str, dict[str, object]] = {}
        for probe in inventory.probes:
            connection_id = (
                f"probe:{probe.usb_serial}" if probe.usb_serial is not None else probe.probe_id
            )
            connection_rows_by_identity.setdefault(
                _setup_connection_key(connection_id),
                {
                    "connection_id": connection_id,
                    "friendly_name": probe.choice().label,
                    "probe_family": probe.probe_family,
                },
            )
        connection_rows = list(connection_rows_by_identity.values()) + provider_connections
        serial_rows = [
            {
                "choice_id": port.serial_id,
                "friendly_name": SerialCandidate(
                    port.serial_id,
                    port.port_path,
                    port.description,
                    port.usb_serial,
                    port.vid,
                    port.pid,
                ).friendly_label(),
                "port_path": port.port_path,
                "stable_usb_identity": port.usb_serial,
            }
            for port in inventory.serial_ports
        ]
    except Exception as exc:  # noqa: BLE001 - overview must remain a safe diagnostic
        connection_rows = []
        serial_rows = []
        inventory_error = str(exc)
    else:
        inventory_error = provider_inventory_error

    routes: list[dict[str, object]] = []
    provisional_bindings: dict[str, str] = {}
    validated_names: list[tuple[str, str]] = []
    no_board_sentinel = False
    if board_names is not None:
        normalized_names: set[str] = set()
        for display_name in board_names:
            if not isinstance(display_name, str) or not display_name.strip():
                raise ValueError("every board name must be non-empty text")
            name = display_name.strip()
            key = _profile_name_key(name)
            if key == _profile_name_key("no board"):
                no_board_sentinel = True
                validated_names.append((name, key))
                continue
            if key in normalized_names:
                raise ValueError("board names must be unique after Unicode normalization")
            normalized_names.add(key)
            validated_names.append((name, key))

    if board_names is not None and validated_names and not no_board_sentinel:
        available_connections = {str(row["connection_id"]) for row in connection_rows}
        assignments = dict(connection_assignments or {})
        expected_names = {name for name, _key in validated_names}
        if assignments and set(assignments) != expected_names:
            raise ValueError("connection_assignments must contain exactly every familiar name")
        if assignments and (
            len({_setup_connection_key(value) for value in assignments.values()})
            != len(assignments)
            or not set(assignments.values()).issubset(available_connections)
        ):
            raise ValueError("connection assignments must be unique current server connection IDs")
        if len(validated_names) > len(connection_rows):
            _replace_setup_assignments({}, "setup overview requires assignment clarification")
            return {
                "status": "setup_assignment_clarification_required",
                "agent_prompt": (
                    "There are more requested board names than visible debug connections. Clarify "
                    "which requested boards are currently attached before setup or validation; do "
                    "not expose machine IDs. Unrelated visible probes do not need board names."
                ),
                "profiles": profile_rows,
                "connections": connection_rows,
                "serial_choices": serial_rows,
                "inventory_error": inventory_error,
                "routes": [],
                "build_guidance": _build_firmware_guidance(),
            }
        if len(connection_rows) > 1 and not assignments:
            _replace_setup_assignments({}, "setup overview requires explicit assignments")
            return {
                "status": "setup_assignment_required",
                "agent_prompt": (
                    "Ask which friendly debug-probe description belongs to each requested board "
                    "name. Then retry get_setup_overview with the same board_names and include the "
                    "selected server connection IDs in connection_assignments. Other visible probes "
                    "may remain unassigned."
                ),
                "profiles": profile_rows,
                "connections": connection_rows,
                "serial_choices": serial_rows,
                "inventory_error": inventory_error,
                "routes": [],
                "assignment_template": {name: None for name, _key in validated_names},
                "build_guidance": _build_firmware_guidance(),
            }
        if not assignments and len(connection_rows) == 1:
            assignments = {validated_names[0][0]: str(connection_rows[0]["connection_id"])}
        existing_ids = {profile.board_id for profile in profiles}
        for name, key in validated_names:
            match = by_name.get(key)
            if match is None:
                board_id = _proposed_board_id(name, existing_ids)
                existing_ids.add(board_id)
                single_connection = assignments.get(name)
                assert single_connection is not None
                provisional_bindings[single_connection] = board_id
                single_serial = serial_rows[0]["choice_id"] if len(serial_rows) == 1 else None
                known_parameters: dict[str, object] = {
                    "board_id": board_id,
                    "connection_id": single_connection,
                    "display_name": name,
                    "mcu_part_number": None,
                    "requires_uart": None,
                    "baud": None,
                    "serial_id": single_serial,
                    "datasheet_path": None,
                    "provider_recipe": None,
                }
                required_user_facts = [
                    "exact package-level MCU part number (full package marking)",
                    "authoritative local datasheet PDF",
                    "whether this firmware workflow uses UART; if yes, its baud rate",
                ]
                if len(connection_rows) == 0:
                    required_user_facts.append("attach and identify one compatible debug probe")
                elif len(connection_rows) > 1:
                    required_user_facts.append(
                        "which friendly debug-probe choice belongs to this board"
                    )
                if len(serial_rows) == 0:
                    required_user_facts.append(
                        "if UART is used, attach and identify the board's UART connection"
                    )
                elif len(serial_rows) > 1:
                    required_user_facts.append(
                        "if UART is used, which friendly UART choice belongs to this board"
                    )
                routes.append(
                    {
                        "display_name": name,
                        "board_id": board_id,
                        "route": "setup",
                        "next_tool": "setup_board",
                        # This intentionally contains unknown required facts and is not a
                        # callable MCP request. Do not invent values just to make it validate.
                        "arguments_template": known_parameters,
                        "template_status": "non_executable",
                        "required_user_facts": required_user_facts,
                    }
                )
                continue
            profile, route_kind, reason = match
            selected_connection = assignments[name]
            provisional_bindings[selected_connection] = profile.board_id
            if route_kind == "repair":
                repair_user_facts = [
                    "authoritative local datasheet PDF",
                    "whether this firmware workflow uses UART; if yes, its baud rate",
                ]
                if profile.mcu_part_number is None:
                    repair_user_facts.insert(
                        0, "exact package-level MCU part number (full package marking)"
                    )
                routes.append(
                    {
                        "display_name": name,
                        "board_id": profile.board_id,
                        "route": "repair",
                        "next_tool": "repair_board_setup",
                        "reason": reason,
                        "next_call": {
                            "tool": "repair_board_setup",
                            "arguments": {"board_id": profile.board_id},
                        },
                        "required_user_facts": repair_user_facts,
                    }
                )
                continue
            routes.append(
                {
                    "display_name": name,
                    "board_id": profile.board_id,
                    "route": "validate",
                    "next_tool": "validate_board",
                    "reason": reason,
                    "next_call": {
                        "tool": "validate_board",
                        "arguments": {"board_id": profile.board_id},
                    },
                }
            )
        _replace_setup_assignments(provisional_bindings, "setup overview assignment replaced")

    if board_names == [] or (no_board_sentinel and len(validated_names) == 1):
        _replace_setup_assignments(
            {},
            "setup overview reported no board",
            drop_omitted_active=True,
        )
        status = "setup_no_board"
        prompt = (
            "The user reported no connected boards using the literal 'no board' sentinel. "
            "Do not begin setup, validation, or hardware access."
        )
    elif no_board_sentinel:
        _replace_setup_assignments({}, "setup overview names are ambiguous")
        status = "setup_names_clarification_required"
        prompt = (
            "The literal 'no board' sentinel was mixed with board names. Ask again in ordinary "
            "language whether no board is connected or, instead, for the familiar name of each "
            "connected board. Do not route or access hardware until the answer is unambiguous."
        )
    elif board_names is None:
        status = "setup_names_required"
        prompt = (
            "Ask the user in ordinary language for a unique familiar name for each board they want "
            "to use in this project now, or the literal sentinel 'no board' by itself. Other "
            "visible probes may remain unassigned. Then call get_setup_overview again with that answer."
        )
    else:
        status = "setup_routes_ready"
        prompt = (
            "Use each route's machine-readable calls without asking the user for their internal "
            "values. Copy a returned next_call exactly only when it is present. Follow a returned "
            "repair route only for its incomplete same-identity profile. For unknown-name setup, use "
            "the non-executable arguments_template to collect required_user_facts and friendly "
            "ambiguous choices, then construct a complete setup_board call. Present only "
            "friendly choices and the returned connection evidence."
        )
    return {
        "status": status,
        "agent_prompt": prompt,
        "profiles": profile_rows,
        "connections": connection_rows,
        "serial_choices": serial_rows,
        "inventory_error": inventory_error,
        "routes": routes,
        "build_guidance": _build_firmware_guidance(),
    }


def _validated_research_prose(response: Mapping[str, object]) -> tuple[list[object], str]:
    evidence = response.get("evidence")
    reasoning = response.get("reasoning_summary")
    if not isinstance(evidence, list) or not evidence:
        raise ResearchError(
            "research/evidence-required",
            "evidence must be a non-empty list of official-source claim records",
        )
    for item in evidence:
        if not isinstance(item, Mapping):
            raise ResearchError("research/evidence-shape", "every evidence item must be an object")
        if set(item) != {"source", "claim"} or not all(
            isinstance(item.get(key), str) and str(item[key]).strip() for key in ("source", "claim")
        ):
            raise ResearchError(
                "research/evidence-shape",
                "each evidence item must contain exactly non-empty source and claim text",
            )
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise ResearchError(
            "research/reasoning-required", "reasoning_summary must be non-empty text"
        )
    return list(evidence), reasoning.strip()


def _enumerate_pack_targets(path: Path, expected_sha256: str) -> tuple[str, ...]:
    from pyocd.target import normalise_target_type_name  # type: ignore[import-untyped]

    payload = read_pack_bytes(path)
    if sha256_bytes(payload) != expected_sha256:
        raise PackProvisionError("quarantined pack changed before target enumeration")
    pack = CmsisPack(io.BytesIO(payload))
    return tuple(
        sorted(
            {
                normalise_target_type_name(device.part_number)
                for device in pack.devices
                if device.part_number.strip()
            }
        )
    )


def _setup_pack_pipeline(
    board_id: str,
    continuation_id: str,
    probe_uid: str,
    requested_policy: tuple[str | None, str, int | None] | None,
) -> tuple[PackCandidatePipeline, list[tuple[str | None, str, int | None]]]:
    key = (board_id, continuation_id, requested_policy)
    current = _setup_pack_pipelines.get(key)
    if current is not None:
        return current
    selected_policies: list[tuple[str | None, str, int | None]] = []

    def live_connect(
        target: str,
        _path: Path,
        expected_sha256: str,
        pdsc_device: str | None,
    ) -> None:
        last_error: TargetConnectionError | None = None
        attempts = (
            (requested_policy,)
            if requested_policy is not None
            else (
                (None, "attach", None),
                (None, "attach", 1_000_000),
                (None, "under-reset", 1_000_000),
            )
        )
        for protocol, mode, frequency in attempts:
            try:
                handle = target_control.open_session(
                    board=None,
                    unique_id=probe_uid,
                    target=target,
                    protocol=protocol,
                    connect_mode=mode,
                    pack_path=_path,
                    pack_sha256=expected_sha256,
                    pdsc_device=pdsc_device,
                    frequency_hz=frequency,
                )
            except TargetConnectionError as exc:
                last_error = exc
                continue
            target_control.close_session(handle)
            selected_policies[:] = [(protocol, mode, frequency)]
            return
        assert last_error is not None
        raise last_error

    current = (
        PackCandidatePipeline(
            _firm_store,
            enumerate_targets=_enumerate_pack_targets,
            live_connect=live_connect,
        ),
        selected_policies,
    )
    _setup_pack_pipelines[key] = current
    return current


def _live_test_builtin_setup_target(
    *,
    probe_uid: str,
    candidate: BuiltInTargetSupportCandidate,
    requested_policy: tuple[str | None, str, int | None] | None,
) -> tuple[BuiltInTargetSupportCandidate, tuple[str | None, str, int | None]]:
    """Record current target and attachment diagnostics for this setup run."""

    attempts = (
        (requested_policy,)
        if requested_policy is not None
        else (
            (None, "attach", None),
            (None, "attach", 1_000_000),
            (None, "under-reset", 1_000_000),
        )
    )
    last_error: TargetControlError | PackProvisionError | None = None
    for protocol, mode, frequency in attempts:
        handle = None
        try:
            handle = target_control.open_session(
                board=None,
                unique_id=probe_uid,
                target=candidate.pyocd_target,
                protocol=protocol,
                connect_mode=mode,
                frequency_hz=frequency,
                pack_path=None,
                pack_sha256=None,
                pdsc_device=None,
            )
            raw_cpuid = target_control.read_memory(handle, 0xE000ED00, 32)
            try:
                observed_cpuid = validate_identity_observation(raw_cpuid, 32)
            except IdentityObservationError as exc:
                raise PackProvisionError(
                    "Live CPUID identity observation is malformed; reconnect and retry setup "
                    "before publishing built-in target support evidence."
                ) from exc
            proven = candidate.with_identity_proof(observed_cpuid)
        except (TargetControlError, PackProvisionError) as exc:
            last_error = exc
            continue
        finally:
            if handle is not None:
                target_control.close_session(handle)
        return proven, (protocol, mode, frequency)
    assert last_error is not None
    raise last_error


def _setup_continue(
    board_id: str,
    continuation_id: str,
    response: Mapping[str, object],
) -> Mapping[str, object]:
    """Validate one setup choice/research response without granting authority."""

    user_input, status, decision = _setup_workflow.continuation_context(continuation_id)
    if user_input.board_id != board_id:
        raise ValueError("continuation_id does not belong to board_id")
    if status not in {"setup_needs_user_input", "setup_research_required"}:
        raise ValueError("this setup continuation is not waiting for a choice or research reply")
    fields = set(response)
    if fields == {"choice_id"}:
        if status != "setup_needs_user_input" or decision is None:
            raise ValueError("the current setup response is not waiting for a friendly choice")
        choice_id = response.get("choice_id")
        if not isinstance(choice_id, str) or choice_id not in {
            choice.choice_id for choice in decision.choices
        }:
            raise ValueError("choice_id must be one of the friendly choices in the last response")
        previous = _setup_selections_by_board.get(board_id, PreflightSelections())
        decision_probe = (
            decision.selected_probe.probe_id if decision.selected_probe is not None else None
        )
        decision_serial = (
            decision.selected_serial.serial_id if decision.selected_serial is not None else None
        )
        if decision.code in {
            "setup/probe-selection-required",
            "setup/probe-selection-invalid",
        }:
            selected = PreflightSelections(
                choice_id,
                previous.serial_id,
                previous.build_configuration_id,
            )
        elif decision.code in {
            "setup/serial-selection-required",
            "setup/serial-selection-invalid",
        }:
            selected = PreflightSelections(
                previous.probe_id or decision_probe,
                choice_id,
                previous.build_configuration_id,
            )
        elif decision.code in {
            "setup/build-selection-required",
            "setup/build-selection-invalid",
        }:
            selected = PreflightSelections(
                previous.probe_id or decision_probe,
                previous.serial_id or decision_serial,
                choice_id,
            )
        else:
            raise ValueError(f"unsupported setup choice route: {decision.code}")
        _setup_selections_by_board[board_id] = selected
        return {
            "status": "setup_continuation_accepted",
            "board_id": board_id,
            "accepted": "friendly_choice",
            "redirect": "Call repair_board_setup now for the current setup run.",
        }

    if status != "setup_research_required":
        raise ValueError(
            "the current setup response is waiting for a friendly choice, not research"
        )

    # Provider recipes use the same provider-neutral target response as pyOCD.
    target_fields = {"target", "evidence", "reasoning_summary"}
    pack_fields = {
        "pack_id",
        "version",
        "filename",
        "url",
        "source_path",
        "official_sha256",
        "evidence",
        "reasoning_summary",
    }
    attachment_fields = {"debug_protocol", "debug_connect_mode", "debug_clock_hz"}
    frozen_fields = frozenset(fields)
    is_target_response = frozen_fields in {
        frozenset(target_fields),
        frozenset(target_fields | attachment_fields),
    }
    is_pack_response = frozen_fields in {
        frozenset(pack_fields),
        frozenset(pack_fields | attachment_fields),
    }
    if not is_target_response and not is_pack_response:
        raise ResearchError(
            "research/field-set-mismatch",
            "response fields must exactly match the requested target or pack schema",
        )
    _validated_research_prose(response)
    target_value = response.get("target")
    target = (
        target_value.strip().casefold()
        if isinstance(target_value, str) and target_value.strip()
        else None
    )
    reviewed_target: str | None = None

    staged_attachment: tuple[str | None, str, int | None] | None = None
    if attachment_fields <= fields:
        protocol = response.get("debug_protocol")
        mode = response.get("debug_connect_mode")
        frequency = response.get("debug_clock_hz")
        if protocol not in {"default", "swd", "jtag"}:
            raise ResearchError(
                "connection/protocol-invalid",
                "debug_protocol must be default, swd, or jtag",
            )
        if mode not in {"attach", "halt", "pre-reset", "under-reset"}:
            raise ResearchError(
                "connection/mode-invalid",
                "debug_connect_mode must be attach, halt, pre-reset, or under-reset",
            )
        if isinstance(frequency, bool) or not isinstance(frequency, int) or frequency <= 0:
            raise ResearchError(
                "connection/clock-invalid", "debug_clock_hz must be a positive integer"
            )
        staged_attachment = (str(protocol), str(mode), frequency)

    if is_target_response:
        if target is None:
            raise ResearchError("research/target-required", "target must be non-empty text")
        if user_input.provider_recipe is not None:
            # The generic worker, not a pyOCD pack or a named-provider branch,
            # proves target identity during the later live connection phase.
            _setup_target_overrides[board_id] = target
            return {
                "status": "setup_continuation_accepted",
                "board_id": board_id,
                "accepted": "provider_target",
                "target": target,
                "redirect": "Call repair_board_setup now for the current setup run.",
            }
        request = make_research_request(
            fact_id="target",
            continuation_token=continuation_id,
            board_id=board_id,
            mcu_part_number=user_input.mcu_part_number,
            unresolved_fact="Resolve the exact pyOCD target for this MCU.",
            requested_fields=tuple(sorted(fields)),
            authoritative_facts={"exact_mcu_part_number": user_input.mcu_part_number},
            acceptable_sources=("official pyOCD documentation", "official vendor CMSIS-Pack"),
            verification_steps=(
                "Check exact MCU consistency.",
                "Confirm built-in or promoted pack support.",
                "Require a live connection before profile commit.",
            ),
        )

        resolved_builtin: BuiltInTargetSupportCandidate | None = None

        def validate(candidate: Mapping[str, object]) -> ValidationOutcome:
            nonlocal resolved_builtin
            try:
                candidate_target = str(candidate["target"]).casefold()
                if reviewed_target is not None:
                    TargetResolver.validate_candidate(
                        candidate_target,
                        expected_target=reviewed_target,
                        built_in_targets=_target_names(),
                        staged_targets=tuple(
                            target_name
                            for pack in load_manifest(_firm_store.layout.pack_manifest)
                            for target_name in pack.provides_targets
                        ),
                    )
                else:
                    resolved_builtin = resolve_builtin_target_support(
                        user_input.mcu_part_number, candidate_target
                    )
            except BuiltInTargetGeometryError as exc:
                return ValidationOutcome(False, str(exc), {"research_route": "official_pack"})
            except (TargetResolutionError, PackProvisionError) as exc:
                return ValidationOutcome(
                    False,
                    str(exc),
                    exc.observed if isinstance(exc, TargetResolutionError) else {},
                )
            return ValidationOutcome(True)

        result = _setup_research.validate_reply(request, response, validate)
        if result.status != "accepted":
            if result.failure is not None and (
                result.failure.observed.get("research_route") == "official_pack"
                or "absent from built-in" in result.failure.reason
                or "CMSIS-Pack" in result.failure.reason
            ):
                return {
                    "status": "setup_research_required",
                    "continuation_id": continuation_id,
                    "agent_prompt": (
                        "The target is plausible but unavailable. Research one official local "
                        "CMSIS-Pack candidate and submit the exact pack response through "
                        "continue_board_setup. Do not ask the user for a target or expose this payload."
                    ),
                    "exact_response_fields": sorted(pack_fields),
                    "optional_response_fields": sorted(attachment_fields),
                    "rejected_candidates": [result.failure.to_document()],
                }
            return {
                "status": result.status,
                "continuation_id": continuation_id,
                "agent_prompt": "The target candidate failed deterministic validation; research a materially different official candidate.",
                "rejected_candidates": (
                    [result.failure.to_document()] if result.failure is not None else []
                ),
            }
        if resolved_builtin is not None:
            try:
                resolved_builtin, selected_policy = _live_test_builtin_setup_target(
                    probe_uid=user_input.connection_id.removeprefix("probe:"),
                    candidate=resolved_builtin,
                    requested_policy=staged_attachment,
                )
            except TargetControlError as exc:
                return {
                    "status": "setup_research_required",
                    "continuation_id": continuation_id,
                    "agent_prompt": (
                        "The installed target is valid but no attempted attachment policy "
                        "connected. Research the exact protocol, connection mode, and clock, "
                        "then resubmit the target and attachment fields together."
                    ),
                    "exact_response_fields": sorted(target_fields | attachment_fields),
                    "rejected_candidates": [{"reason": str(exc), "candidate": {"target": target}}],
                }
            except PackProvisionError as exc:
                return {
                    "status": "setup_research_required",
                    "continuation_id": continuation_id,
                    "agent_prompt": (
                        "The live target did not expose a recognized Arm Cortex-M CPUID. "
                        "Research a materially different exact target or official pack."
                    ),
                    "exact_response_fields": sorted(target_fields),
                    "rejected_candidates": [{"reason": str(exc), "candidate": {"target": target}}],
                }
            _setup_builtin_candidates[board_id] = resolved_builtin
            _setup_attachment_overrides[board_id] = selected_policy
    else:
        for key in ("pack_id", "version", "filename", "url", "source_path"):
            if not isinstance(response.get(key), str) or not str(response[key]).strip():
                raise ResearchError("package/field-required", f"{key} must be non-empty text")
        official_sha = response.get("official_sha256")
        if official_sha is not None and not isinstance(official_sha, str):
            raise ResearchError("package/checksum-shape", "official_sha256 must be text or null")
        probe_uid = user_input.connection_id.removeprefix("probe:")
        candidate = PackCandidate(
            str(response["pack_id"]),
            str(response["version"]),
            str(response["filename"]),
            str(response["url"]),
            Path(str(response["source_path"])).expanduser().resolve(),
            official_sha,
        )
        pipeline, selected_policies = _setup_pack_pipeline(
            board_id, continuation_id, probe_uid, staged_attachment
        )
        try:
            binding = derive_candidate_binding(candidate.source_path, user_input.mcu_part_number)
            target = binding.pyocd_target.casefold()
            if reviewed_target is not None and target != reviewed_target:
                raise PackCandidateError(
                    "package/device-binding-target-mismatch",
                    "The canonical target derived from the exact PDSC leaf does not match the "
                    "reviewed MCU package",
                )
            validated = pipeline.validate_device(
                candidate,
                required_target=target,
                device_binding=binding,
            )
            pipeline.promote(validated, board_id=board_id)
            if not selected_policies:
                raise PackProvisionError(
                    "pack candidate was promoted without a live-tested attachment policy"
                )
            _setup_attachment_overrides[board_id] = selected_policies[-1]
        except (PackCandidateError, PackProvisionError) as exc:
            failure = exc.failure if isinstance(exc, PackCandidateError) else None
            if isinstance(exc, PackCandidateError) and exc.code == "package/live-connect-failed":
                return {
                    "status": "setup_research_required",
                    "continuation_id": continuation_id,
                    "agent_prompt": (
                        "The exact pack and target passed metadata validation, but the default "
                        "attachment policies failed. Keep this same package and research the "
                        "required debug_protocol, debug_connect_mode, and debug_clock_hz; then "
                        "resubmit the complete pack plus attachment response through "
                        "continue_board_setup."
                    ),
                    "connection_failure": (
                        failure.to_document() if failure is not None else {"reason": str(exc)}
                    ),
                    "rejected_candidates": [],
                    "exact_response_fields": sorted(pack_fields | attachment_fields),
                    "optional_response_fields": [],
                }
            return {
                "status": "setup_research_required",
                "continuation_id": continuation_id,
                "agent_prompt": f"The package candidate was rejected: {exc}. Research a materially different official candidate.",
                "rejected_candidates": ([failure.to_document()] if failure is not None else []),
                "exact_response_fields": sorted(pack_fields),
                "optional_response_fields": sorted(attachment_fields),
            }

    assert target is not None
    _setup_target_overrides[board_id] = target
    return {
        "status": "setup_continuation_accepted",
        "board_id": board_id,
        "accepted": "target_and_pack" if is_pack_response else "target",
        "target": target,
        "redirect": "Call repair_board_setup now for the current setup run.",
    }


def _clear_setup_continuation(board_id: str) -> None:
    _setup_target_overrides.pop(board_id, None)
    _setup_attachment_overrides.pop(board_id, None)
    _setup_builtin_candidates.pop(board_id, None)
    _setup_selections_by_board.pop(board_id, None)
    for key in tuple(_setup_pack_pipelines):
        if key[0] == board_id:
            _setup_pack_pipelines.pop(key, None)
    _setup_research.clear(board_id)


_setup_workflow = SetupWorkflow(
    _report_writer,
    _setup_inventory,
    phase_handlers={
        SetupPhase.CONNECTION: _setup_connection_phase,
        SetupPhase.VALIDATION: _setup_validation_phase,
        SetupPhase.COMMIT: _setup_commit_phase,
    },
    cancellation_checkpoint=cancellation_checkpoint,
)
setup_tool_handlers = build_setup_handlers(
    SetupToolServices(
        workflow=_setup_workflow,
        validator=_board_validator,
        setup_status=_get_setup_status,
        setup_overview=_setup_overview,
        setup_continue=_setup_continue,
        setup_selections=lambda board_id: _setup_selections_by_board.get(
            board_id, PreflightSelections()
        ),
        require_assignment=lambda board_id, connection_id: assignment_store.require(
            connection_id, board_id
        ),
        assigned_connection=assignment_store.connection_for,
        safety_map_status=lambda board_id: cast(
            Mapping[str, object], _get_setup_status(board_id)["safety_map"]
        ),
    )
)


for _setup_name, _setup_handler in setup_tool_handlers.items():
    if _setup_name in _guarded_risks:
        _guard_core.action_specs[_setup_name] = _spec_for(_setup_name, _setup_handler)
    _registered_setup_handler = _guarded_handler(_setup_name, _setup_handler)
    mcp.add_tool(
        _registered_setup_handler,
        name=_setup_name,
        description=_registered_setup_handler.__doc__,
        structured_output=False,
    )


def _recovery_cleanup(
    board_id: str,
    connection: ManagedConnection,
) -> dict[str, object]:
    """Detach one dispatched-recovery session without touching a replacement.

    This is deliberately the one post-dispatch cleanup path.  A recovery can
    change target accessibility even when the provider response is malformed,
    so routing is removed before authority invalidation and close attempts.
    """

    cleanup: dict[str, object] = {
        "routing_removal": "uncertain",
        "authority_invalidation": "uncertain",
        "runtime_close": "not_attempted",
        "provider_close": "not_attempted",
        "diagnostics": [],
    }
    diagnostics = cast(list[str], cleanup["diagnostics"])
    detached: ManagedConnection | None = None
    try:
        detached = connection_manager.clear_if_current(board_id, connection)
        cleanup["routing_removal"] = "removed" if detached is not None else "already_detached"
    except Exception as exc:  # noqa: BLE001 - preserve a possibly stale handle as unusable
        diagnostics.append(f"routing removal uncertain: {type(exc).__name__}: {exc}")

    # Do not erase a concurrent replacement's persisted assignment or setup
    # run.  The captured old handle is still closed below regardless.
    if detached is not None:
        try:
            assignment_store.clear_board(board_id)
            workflow = globals().get("_setup_workflow")
            if isinstance(workflow, SetupWorkflow):
                workflow.disconnect(detached.connection_id)
        except Exception as exc:  # noqa: BLE001 - routing is already removed
            diagnostics.append(f"assignment cleanup uncertain: {type(exc).__name__}: {exc}")

    try:
        _guard_core.invalidate_board(board_id, "recovery-post-dispatch")
        cleanup["authority_invalidation"] = "proven"
    except GuardError as exc:
        diagnostics.append(f"authority invalidation uncertain: {exc.code}: {exc.message}")
    except Exception as exc:  # noqa: BLE001 - preserve the primary recovery fact
        diagnostics.append(f"authority invalidation uncertain: {type(exc).__name__}: {exc}")

    # A detached runtime record and its exact provider handle are both owned
    # by the captured assignment.  Close both even if an earlier cleanup fact
    # is uncertain; neither operation may make a replacement routable.
    try:
        _session_store.close_session(connection.runtime_session)
        cleanup["runtime_close"] = "proven"
    except Exception as exc:  # noqa: BLE001 - close the provider too
        cleanup["runtime_close"] = "uncertain"
        diagnostics.append(f"runtime close uncertain: {type(exc).__name__}: {exc}")
    try:
        cleanup["provider_close_evidence"] = target_control.close_session(connection.handle)
        cleanup["provider_close"] = "proven"
    except Exception as exc:  # noqa: BLE001 - stale routing remains removed
        cleanup["provider_close"] = "uncertain"
        diagnostics.append(f"provider close uncertain: {type(exc).__name__}: {exc}")
    return cleanup


def _recovery_cleanup_uncertain(cleanup: Mapping[str, object]) -> bool:
    return any(
        cleanup.get(field) == "uncertain"
        for field in (
            "routing_removal",
            "authority_invalidation",
            "runtime_close",
            "provider_close",
        )
    )


def _recovery_details(
    *,
    selected: RecoveryCapability,
    result: RecoveryResult | None,
    cleanup: Mapping[str, object] | None = None,
    primary: BaseException | None = None,
) -> dict[str, object]:
    details: dict[str, object] = {
        "selected_capability": selected.to_record(),
        "provider_result": result.to_record() if result is not None else None,
        "accepted": result.accepted if result is not None else None,
    }
    if cleanup is not None:
        details["cleanup"] = dict(cleanup)
    if primary is not None:
        details["post_dispatch_error"] = {
            "type": type(primary).__name__,
            "message": str(primary),
        }
    return details


def _recover_target(board_id: str, mechanism: str) -> str:
    """**What** Ask the connected provider to run one supported recovery mechanism.

    **When** Use after a locked or otherwise inaccessible target prevents normal debug access.

    **Parameters** `board_id` is the connected board; `mechanism` is a provider capability label,
    for example `"backend_mass_erase"`.

    **Returns** Provider acceptance and `verification=unavailable` unless the provider exposes an
    observable recovery postcondition.

    **Failures and recovery** Unsupported mechanisms and transport failures are explicit; inspect
    `get_board_info`, reconnect with `connect_board`, or select a provider-supported mechanism.
    """

    started = time.monotonic()
    connection = connection_manager.connection_for(board_id)
    runtime = connection.runtime_session
    try:
        dispatch = target_control.recover_target(connection.handle, mechanism=mechanism)
    except RecoveryPostDispatchError as error:
        cleanup = _recovery_cleanup(board_id, connection)
        evidence = _recovery_details(
            selected=error.selected_capability,
            result=error.result,
            cleanup=cleanup,
            primary=error,
        )
        _record_event(
            "recover_target",
            {"board_id": board_id, "mechanism": mechanism},
            outcome_kind=ToolOutcome.FAILED,
            error_code="recovery/post-dispatch-uncertain",
            duration_ms=_duration_ms(started),
            details=evidence,
            board_id=board_id,
            session=runtime,
        )
        final = RecoverySessionFinalizationError(error, evidence)
        raise final from error

    selected, result = dispatch.selected_capability, dispatch.result
    primary: BaseException | None = None
    retained = False
    # Provider acceptance is distinct from session safety.  A descriptor that
    # declares invalidation wins over a contradictory provider "preserved"
    # result and is never left routable.
    if not result.accepted:
        primary = TargetStateError(
            f"Provider did not accept recovery mechanism '{result.mechanism}'; no recovery effect is claimed."
        )
    elif (
        selected.session_postcondition == "invalidated"
        and result.observed_session_postcondition == "preserved"
    ):
        primary = TargetStateError(
            "Provider result claimed a preserved session, but the selected recovery capability "
            "declares the session invalidated."
        )
    elif (
        selected.session_postcondition in {"preserved", "unknown"}
        and result.observed_session_postcondition == "preserved"
    ):
        try:
            _safety_authority.binding(board_id, connection.handle)
            retained = True
        except Exception as exc:  # noqa: BLE001 - live re-observation is required for retention
            primary = TargetStateError(
                "Provider reported a preserved session, but current identity/regions could not be "
                f"re-observed: {type(exc).__name__}: {exc}"
            )

    if not retained:
        cleanup = _recovery_cleanup(board_id, connection)
        evidence = _recovery_details(
            selected=selected,
            result=result,
            cleanup=cleanup,
            primary=primary,
        )
        evidence["connection_retained"] = False
        evidence["reconnect_required"] = True
        if primary is not None or _recovery_cleanup_uncertain(cleanup):
            _record_event(
                "recover_target",
                {"board_id": board_id, "mechanism": mechanism},
                outcome_kind=ToolOutcome.FAILED,
                error_code="recovery/post-dispatch-uncertain",
                duration_ms=_duration_ms(started),
                details=evidence,
                board_id=board_id,
                session=runtime,
            )
            final = RecoverySessionFinalizationError(primary, evidence)
            if primary is not None:
                raise final from primary
            raise final
    else:
        evidence = _recovery_details(selected=selected, result=result)
        evidence["connection_retained"] = True
        evidence["reconnect_required"] = False
    _record_event(
        "recover_target",
        {"board_id": board_id, "mechanism": mechanism},
        outcome_kind=ToolOutcome.SUCCESS,
        error_code=None,
        duration_ms=_duration_ms(started),
        details=evidence,
        board_id=board_id,
        session=runtime,
    )
    if retained:
        return (
            f"Provider accepted recovery mechanism {result.mechanism}; "
            f"verification={result.verification}; a preserved session was freshly re-observed."
        )
    return (
        f"Provider accepted recovery mechanism {result.mechanism}; verification={result.verification}; "
        "routing removal and authority invalidation were proven after a non-preserved session. "
        "Reconnect, validate, and refresh_safety_map."
    )


_guard_core.action_specs["recover_target"] = _spec_for("recover_target", _recover_target)
_registered_recover_target = _guarded_handler("recover_target", _recover_target)
mcp.add_tool(
    _registered_recover_target,
    name="recover_target",
    description=_registered_recover_target.__doc__,
)


class _HardwarePermissionReply(BaseModel):
    """The cooperative user's direct elicitation response, never an agent authority field."""

    # MCP elicitation accepts only the builtin primitive annotations.  Pydantic
    # strict mode keeps the reply boundary non-coercing without changing those
    # annotations into validator-invisible aliases such as StrictBool.
    model_config = ConfigDict(strict=True)

    approved: bool
    call_budget: int | None = None


def _guard_error_result(error: GuardError) -> str:
    return f"Invalid request [{error.code}]: {error.message}"


def _approval_command(request_id: str) -> tuple[list[str], str]:
    """Return self-contained authoritative argv and a shell-safe rendering."""

    argv = [
        str(Path(sys.executable).resolve()),
        "-m",
        "firmware_mcp.server",
        "approve-hardware",
        "--project",
        str(_project_root),
        "--request",
        request_id,
    ]
    if os.name != "nt":
        return argv, shlex.join(argv)

    # This is deliberately PowerShell syntax, not generic cmd.exe/CRT text.
    # A single-quoted PowerShell literal escapes its quote by doubling it;
    # every token stays an argument to the explicit call operator.
    rendered = "& " + " ".join("'" + token.replace("'", "''") + "'" for token in argv)
    return argv, rendered


async def request_hardware_permission(
    board_id: str,
    scope: Literal["routine-session", "destructive-once"],
    requested_call_budget: int | None = None,
    plan_id: str | None = None,
    ctx: Context | None = None,
) -> dict[str, object] | str:
    """**What** Request a cooperative user's routine grant or exact one-time destructive approval.

    **When** Call before creating a routine hardware plan; the driving agent relays the resulting
    elicitation or external command to the user and never supplies approval itself.

    **Parameters** `board_id` is the logical board; `scope` is `routine-session` or the exact
    one-attempt `destructive-once`; `requested_call_budget` is positive advisory context only; and
    `plan_id` is null for `routine-session` and required for `destructive-once`; a destructive
    request has exactly one call and displays the plan's canonical physical disclosure. Example:
    `request_hardware_permission(board_id="board-a", scope="destructive-once", plan_id="plan-...")`.

    **Returns** Immutable request evidence and either direct elicitation status or the exact
    external approval command. **Failures and recovery** Relay that prompt/command to the user;
    then call `get_hardware_permission`. Agents never run the approval command or set authority.
    """

    try:
        # The request snapshots board evidence under the same stable lock used
        # by guarded execution; elicitation itself intentionally happens after
        # releasing it so a user interaction never blocks board work.
        with (
            connection_manager.lock_for(board_id),
            safety_publication_lock(_project_root, board_id),
        ):
            request = _guard_core.request_permission(
                board_id=board_id,
                scope=scope,
                requested_call_budget=requested_call_budget,
                plan_id=plan_id,
            )
    except GuardError as error:
        return _guard_error_result(error)
    command_argv, command = _approval_command(request.request_id)
    if ctx is not None:
        try:
            elicited = await ctx.elicit(
                (
                    "Approve or decline this exact destructive one-time operation:\n"
                    + json.dumps(request.disclosure, sort_keys=True, separators=(",", ":"))
                    if scope == "destructive-once"
                    else "Approve or decline this routine hardware session. The requested budget is advisory; "
                    "enter the finite number of calls you personally approve."
                ),
                _HardwarePermissionReply,
            )
        except (
            Exception
        ) as error:  # client capability fallback; the immutable request remains usable
            return {
                "request_id": request.request_id,
                "approval": "pending-external-cli",
                "disclosure": request.disclosure,
                "elicitation_diagnostic": f"{type(error).__name__}: {error}",
                "approval_command": command,
                "approval_argv": command_argv,
            }
        if elicited.action == "accept" and elicited.data is not None:
            try:
                with connection_manager.lock_for(board_id):
                    _guard_core.approve_request(
                        request.request_id,
                        approved=elicited.data.approved,
                        call_budget=elicited.data.call_budget,
                    )
            except GuardError as error:
                return _guard_error_result(error)
            return {
                "request_id": request.request_id,
                "approval": "recorded",
                "disclosure": request.disclosure,
                "next_call": f"get_hardware_permission(request_id='{request.request_id}')",
            }
        if elicited.action in {"decline", "cancel"}:
            try:
                with connection_manager.lock_for(board_id):
                    _guard_core.approve_request(
                        request.request_id, approved=False, call_budget=None
                    )
            except GuardError as error:
                return _guard_error_result(error)
            return {"request_id": request.request_id, "approval": "declined"}
    return {
        "request_id": request.request_id,
        "approval": "pending-external-cli",
        "disclosure": request.disclosure,
        "approval_command": command,
        "approval_argv": command_argv,
    }


def get_hardware_permission(request_id: str) -> dict[str, object] | str:
    """**What** Consume one approved user receipt into a run-scoped grant.

    **When** Use after `request_hardware_permission` reports approval.

    **Parameters** `request_id` is the exact immutable request ID, for example
    `get_hardware_permission(request_id="permission-...")`.

    **Returns** Grant binding, initial/remaining calls, invalidation rules, and the exact plan
    shape. **Failures and recovery** Pending/declined/stale receipts are explicit; ask the user
    again with `request_hardware_permission` rather than supplying a budget as an agent argument.
    """

    try:
        # This initial persisted lookup only routes to a board lock; the
        # binding comparison and receipt consumption occur after that lock.
        board_id = _guard_core.request_board_id(request_id)
        with connection_manager.lock_for(board_id):
            return _guard_core.grant_record(_guard_core.get_permission(request_id))
    except GuardError as error:
        return _guard_error_result(error)


def revoke_hardware_permission(grant_id: str) -> dict[str, object]:
    """**What** Revoke one routine grant idempotently and invalidate its plans.

    **When** Use when the cooperative user withdraws approval or the objective is abandoned.

    **Parameters** `grant_id` is the exact value returned by `get_hardware_permission`, for
    example `revoke_hardware_permission(grant_id="grant-...")`.

    **Returns** Whether the grant existed and the exact plan IDs invalidated; retrying is safe.

    **Failures and recovery** A missing grant is an honest idempotent result. Request a new user
    permission and create a new plan if hardware work must resume.
    """

    return _guard_core.revoke(grant_id)


def create_hardware_plan(
    grant_id: str | None,
    board_id: str,
    objective: str,
    expected_result: str,
    actions: list[object],
) -> dict[str, object] | str:
    """**What** Create one immutable plan of exact guarded hardware calls.

    **When** Use after a routine user grant, or create a single destructive plan whose exact
    physical disclosure is approved once through the destructive permission workflow.

    **Parameters** `grant_id` is the exact routine grant (or null only for destructive plans);
    `board_id` is the planned board; `objective` and `expected_result` are nonempty operational
    text; `actions` is a nonempty list of exact `{tool, arguments, max_calls}` records. Each
    `arguments` object contains every public tool parameter except `plan_id`. Example:
    `create_hardware_plan(grant_id="grant-...", board_id="board-a", objective="inspect state",
    expected_result="halt state", actions=[{"tool":"get_target_state","arguments":
    {"board_id":"board-a"},"max_calls":1}])`.

    **Returns** The plan ID, exact canonical actions, evidence binding, remaining budgets, and
    status. Destructive flash/recovery plans return `disclosure-required` in this slice.

    **Failures and recovery** Changed, missing, extra, cross-board, or over-budget arguments fail;
    correct the plan, request user permission when needed, and create a new immutable plan.
    """

    try:
        with (
            connection_manager.lock_for(board_id),
            safety_publication_lock(_project_root, board_id),
        ):
            canonical_actions = _canonicalize_connect_plan_actions(board_id, actions)
            plan = _guard_core.create_plan(
                grant_id=grant_id,
                board_id=board_id,
                objective=objective,
                expected_result=expected_result,
                actions=canonical_actions,
            )
            return _guard_core.plan_record(plan.plan_id)
    except GuardError as error:
        return _guard_error_result(error)


def get_hardware_plan(plan_id: str) -> dict[str, object] | str:
    """**What** Return one immutable plan's actions, binding, budgets, and close reason.

    **When** Use before or after an attempt to inspect whether the exact plan remains usable.

    **Parameters** `plan_id` is the exact returned plan ID, for example
    `get_hardware_plan(plan_id="plan-...")`.

    **Returns** Original canonical arguments, per-action remaining calls, grant remaining calls,
    binding evidence, status, attempts, and any close reason.

    **Failures and recovery** Unknown or invalidated plans are explicit; request a new permission
    if necessary and create a new plan against current board evidence.
    """

    try:
        return _guard_core.plan_record(plan_id)
    except GuardError as error:
        return _guard_error_result(error)


def cancel_hardware_plan(plan_id: str) -> dict[str, object]:
    """**What** Cancel one plan without refunding started user-approved attempts.

    **When** Use when its objective no longer applies or its evidence is no longer wanted.

    **Parameters** `plan_id` is the exact plan ID, for example
    `cancel_hardware_plan(plan_id="plan-...")`.

    **Returns** The final status, existence result, and `refunded=false` evidence.

    **Failures and recovery** Missing plans are an honest idempotent result. Request a new grant
    and create a fresh plan if hardware work is still needed.
    """

    return _guard_core.cancel_plan(plan_id)


# These controls are deliberately always visible: they make or inspect the
# cooperative-user authorization records and never invoke a hardware backend.
for _guard_control in (
    request_hardware_permission,
    get_hardware_permission,
    revoke_hardware_permission,
    create_hardware_plan,
    get_hardware_plan,
    cancel_hardware_plan,
):
    mcp.add_tool(
        _guard_control,
        name=_guard_control.__name__,
        description=_guard_control.__doc__,
        structured_output=False,
    )


def _bind_managed_board_resources(operation: ManagedOperation) -> None:
    """Bind the current connection to interruption cleanup without changing normal state.

    Successful actions preserve the state their documented semantics produce.
    Explicit reset/resume tools preserve the state their documented semantics produce.
    Cancellation and timeout close a debug connection only for operations that use it, because
    backend completion is then uncertain. UART-only, metadata-only, and wait operations do not
    own that independent transport. An ordinary handler error is reported without assuming that
    the healthy connection itself became unsafe.
    """

    board_id = operation.board_id
    if board_id is None or operation.tool_name in {"connect_board", "disconnect_board"}:
        return

    # These tools use profile files, ELF metadata, wall-clock sleep, or UART only. A client
    # cancellation or their own finite timeout says nothing about the independent SWD/JTAG
    # transport, so it must not tear down the validated debug connection. Validation remains
    # live until an actual debug transport failure, explicit disconnect/reassignment, or server
    # termination.
    if operation.tool_name in {
        "get_board_info",
        "find_symbol",
        "read_serial",
        "write_serial",
        "exchange_serial",
        "wait_duration",
    }:
        return
    connection = connection_manager.maybe_connection(board_id)
    if connection is None:
        return
    handle = connection.handle

    def target_connection_failed() -> bool:
        current = operation.error
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if isinstance(current, TargetConnectionError):
                return True
            current = current.__cause__ or current.__context__
        return False

    def evict_captured_connection(reason: str) -> dict[str, object] | None:
        del reason
        cleared = connection_manager.clear_if_current(board_id, connection)
        if cleared is None:
            return None
        try:
            return target_control.close_session(cleared.handle)
        finally:
            _session_store.close_session(cleared.runtime_session)

    def close_failed_connection() -> dict[str, object] | None:
        interrupted = operation.cancellation_requested.is_set()
        # Guard/preflight hooks can legitimately touch the backend before the
        # user handler begins. A typed transport loss there is just as stale as
        # one raised by the handler itself.
        connection_failed = target_connection_failed()
        if not interrupted and not connection_failed:
            return None
        reason = (
            "operation cancelled or timed out"
            if interrupted
            else "target connection failed during operation"
        )
        return evict_captured_connection(reason)

    def release_reset() -> None:
        try:
            target_control.release_reset(handle)
        except TargetConnectionError as release_error:
            try:
                evict_captured_connection("target connection failed while releasing reset")
            except Exception as cleanup_error:  # cleanup still reports the transport loss
                raise TargetConnectionError(
                    "Reset release transport failure: "
                    f"{release_error}. Stale-connection cleanup also failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                ) from cleanup_error
            raise

    operation.resources.close_debug.append(close_failed_connection)
    operation.resources.release_reset.append(release_reset)


mcp.configure_operation_resources(_bind_managed_board_resources)


@mcp.resource(
    "firmware://start-here",
    name="firmware_start_here",
    description="Portable firmware workflow using the current MCP surface.",
    mime_type="text/markdown",
)
def firmware_start_here() -> str:
    """Return the normal setup-to-debug workflow for one or more boards."""

    return """# Firmware MCP start here

Use the normal flow: **detect â†’ configure â†’ build â†’ flash â†’ verify â†’ debug**.

This is `byo-firmware-mcp` version 0.2.0 (Python package `firmware_mcp`). The built-in pyOCD
adapter and a trusted external direct-argv provider recipe share the documented JSON-lines worker
protocol; neither provider changes the visible MCP surface.

This server uses visible guarded actions: a cooperative user supplies a finite call budget, and an
immutable plan binds the exact board, arguments, and current evidence. Permission is not an
attacker-defense or a judgement of project risk; it prevents an honest agent mistake or thrashing.

1. **Detect:** call `get_setup_overview` to inspect available connections and stored boards. If no
   probe or UART port is visible, reconnect hardware, inspect the returned inventory, then retry
   `get_setup_overview`. For an unknown debug provider, pass its direct-argv `provider_recipe`
   (`provider_id`, `inventory_argv`, and `worker_argv`) to that call: the inventory result supplies
   namespaced connection IDs and a successful inventory is stored in `.firm/providers.json`. If it
   fails, no recipe is stored; correct the argv or worker protocol and retry the same call.
2. **Configure and plan target work:** follow an emitted `next_call` only when it exists. For a fresh-board route with
   `template_status=non_executable`, collect every `required_user_facts` item, replace each unknown
   in `arguments_template`, then construct and call the complete `setup_board` request. Do not invent
   values or invoke the partial template. Use `continue_board_setup` for requested research and
   `repair_board_setup` as often as needed. Call `validate_board` and `get_setup_status` for
   diagnostics. Setup runs are bound to the selected board and connection. Before every
   target-affecting setup, validation, connect, debug, memory, breakpoint, or UART action, call
   `request_hardware_permission`. Relay either the MCP elicitation or its exact
   approval command to the user unchanged. Its exact structured `approval_argv` starts with the
   server's absolute Python interpreter and `-m firmware_mcp.server approve-hardware` (prefer it
   when the client can execute argv directly); `approval_command` renders those same tokens for
   POSIX `sh` or Windows PowerShell, never generic `cmd.exe`;
   the agent never runs that command or supplies the authoritative budget. Then call
   `get_hardware_permission`, `create_hardware_plan`, and supply its exact `plan_id` to one matching
   action. `get_hardware_plan`, `revoke_hardware_permission`, and `cancel_hardware_plan` inspect or
   stop this state. Disconnect, changed profile/session/serial evidence, or artifact bytes require
   a new permission and plan.
3. **Connect and map physical evidence:** create an exact plan for `connect_board` from the stored
   returning-board profile and assignment, even while disconnected, then call `get_board_info`.
   Copy the exact `board_id` returned by `get_setup_overview`/`get_setup_status`; an example name is
   never a substitute for that live key. Create a routine plan for `refresh_safety_map` with the selected optional
   JSON/YAML layout and/or ELF, and call it before memory, peripheral, or breakpoint work. Layout
   entries use `{schema_version: 1, board_id, regions:[{name, role, start, end, source_path,
   source_locator}]}`; each role is explicit evidence, while an observed readable `unknown` range
   stays observable but is reported as uncertain. The refresh binds every selected file byte and
   consumes one routine call. Then create a fresh plan for `get_target_state`. Two boards use
   distinct `board_id` values; each connection, lock, setup run, and event stream stays board-local.
4. **Build:** call `build_firmware` with an exact argv, or `collect_build_artifacts` for existing
   outputs. A build failure returns stderr and exact argv; correct the command and retry.
5. **Flash and recovery:** call `flash_firmware` with a required `flash_role`. `application` is a
   routine plan; `bootloader`, `full-device`, and `sensitive` require one single-action
   `disclosure-required` plan, then `request_hardware_permission(scope="destructive-once",
   plan_id=...)`. Relay the exact disclosed JSON to the cooperative user, activate its one-call
   receipt, and invoke only the matching action. Artifact target metadata is compared only when it
   and the current live observation are both exact; otherwise the result is `unavailable`, not
   guessed. For recovery,
   inspect live mechanisms in `get_board_info`, select one exact `mechanism` for `recover_target`,
   and use the same
   one-time disclosure flow. Recovery acceptance is not erase verification; reconnect, validate,
   and refresh the map whenever preservation is not freshly observed.
6. **Observe and debug:** use `read_serial`, `write_serial`, `exchange_serial`, `halt_target`,
   `resume_target`, `step_target`, `reset_target`, `read_cpu_register`, `write_cpu_register`,
   `write_peripheral_register`, `read_memory`, `write_memory`, `find_symbol`, `set_breakpoint`,
   `remove_breakpoint`, and `wait_duration`. UART timeout or expected-text miss is evidence, not a
   fabricated transport result; adjust the expectation or capture window and use `read_serial`.

New board example: `get_setup_overview` â†’ inspect the returned route. Copy its `next_call` only
when present; otherwise complete the non-executable template as described above, then call
`setup_board` â†’ `continue_board_setup` (if requested) â†’ `validate_board` â†’ `connect_board` â†’
`build_firmware` â†’ `flash_firmware` â†’ `read_serial`.

Returning board example: `get_setup_status` â†’ `connect_board` â†’ `get_board_info` â†’
`flash_firmware` â†’ `get_target_state`.

Unknown-provider example: call `get_setup_overview(provider_recipe={"provider_id": "lab-tool",
"inventory_argv": ["lab-tool", "list", "--json"], "worker_argv": ["lab-tool", "worker"]})`,
select one returned `provider:lab-tool:...` connection ID, and pass the same recipe to
`setup_board`. The worker must implement the versioned JSON-lines protocol, report capability-aware
live identity and current physical regions, and bind flash readback evidence to that session. A missing
returning-board recipe is an explicit recovery: call `get_setup_overview` or `setup_board` again
with its recipe; the server never guesses a pyOCD route. Provider IDs and provider-local connection
IDs must not contain `:` because returned IDs use the reversible
`provider:<provider_id>:<connection_id>` namespace.

If a connection drops, call `disconnect_board`, check the physical probe, then call `connect_board`.
Use `disconnect_board` when the board work is complete; it idempotently closes its session.
"""


def _approve_hardware_cli(project: Path, request_id: str) -> int:
    """Run the direct-user fallback without accepting agent-supplied authority."""

    core = GuardCore(
        project_root=project.resolve(),
        run_id="external-approval-cli",
        action_specs={},
        evidence_for=lambda board_id: {"board_id": board_id},
    )
    try:
        status = core.permission_status(request_id)
    except GuardError as error:
        print(f"{error.code}: {error.message}", file=sys.stderr)
        return 2
    request = cast(dict[str, object], status["request"])
    print("Hardware permission request")
    print(f"  board: {request.get('board_id')}")
    print(f"  scope: {request.get('scope')}")
    if request.get("scope") == "destructive-once":
        print("  exact one-time destructive disclosure:")
        print(json.dumps(request.get("disclosure"), sort_keys=True, indent=2))
    else:
        print(f"  requested budget (advisory): {request.get('requested_call_budget')}")
    print(f"  binding evidence: {request.get('binding')}")
    prompt = (
        "Approve this exact one-time destructive operation? [y/N]: "
        if request.get("scope") == "destructive-once"
        else "Approve this routine hardware session? [y/N]: "
    )
    if input(prompt).strip().casefold() not in {
        "y",
        "yes",
    }:
        try:
            core.approve_request(request_id, approved=False, call_budget=None)
        except GuardError as error:
            print(f"{error.code}: {error.message}", file=sys.stderr)
            return 2
        print("Permission declined.")
        return 0
    try:
        if request.get("scope") == "destructive-once":
            core.approve_request(request_id, approved=True, call_budget=None)
        else:
            core.approve_request(
                request_id,
                approved=True,
                call_budget=int(input("Your finite positive call budget: ").strip(), 10),
            )
    except GuardError as error:
        print(f"{error.code}: {error.message}", file=sys.stderr)
        return 2
    except ValueError as error:
        print(f"Approval was not recorded: {error}", file=sys.stderr)
        return 2
    print("Permission approved. Return to the MCP client and call get_hardware_permission.")
    return 0


def main() -> None:
    """Console entry point. Runs the server over stdio transport by default."""

    if len(sys.argv) > 1 and sys.argv[1] in {"-h", "--help"}:
        parser = argparse.ArgumentParser(
            prog="byo-firmware-mcp",
            description="Provider-neutral firmware MCP server over standard input/output.",
        )
        parser.add_argument(
            "approve-hardware",
            nargs="?",
            help="approve a pending hardware-permission request with --project and --request",
        )
        parser.print_help()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "approve-hardware":
        parser = argparse.ArgumentParser(prog="byo-firmware-mcp approve-hardware")
        parser.add_argument("--project", type=Path, required=True)
        parser.add_argument("--request", required=True)
        parsed = parser.parse_args(sys.argv[2:])
        raise SystemExit(_approve_hardware_cli(parsed.project, parsed.request))
    require_clean_startup()
    try:
        mcp.run()
    finally:
        failures: list[str] = []
        for _board_id in connection_manager.assigned_board_ids():
            try:
                disconnect(_board_id)
            except Exception as exc:  # shutdown must report every uncertain cleanup
                diagnostic = f"disconnect_board({_board_id}) failed: {type(exc).__name__}: {exc}"
                failures.append(diagnostic)
                print(diagnostic, file=sys.stderr)
        if failures:
            raise RuntimeError("; ".join(failures))


if __name__ == "__main__":
    main()
