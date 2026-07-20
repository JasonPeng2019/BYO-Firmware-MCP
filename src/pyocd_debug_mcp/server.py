"""Minimal MCP server exposing pyOCD debug control to an LLM client.

Design notes
------------
* Debug sessions are *stateful* (halt state, breakpoints, and live target
  connections persist across calls), so each logical board owns one explicit
  connection until it is disconnected.
* pyOCD's target access is blocking and **not thread-safe**. FastMCP may invoke
  tools concurrently, so accesses to the same board share a serialization lock.
* pyOCD calls block; for fast operations (register/memory reads) that is fine.
  Long operations such as flashing should be offloaded (e.g. ``anyio.to_thread``)
  so they don't stall the event loop — left out here to keep the starter small.
"""

from __future__ import annotations

import io
import os
import hashlib
import re
import secrets
import subprocess
import sys
import time
import unicodedata
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from pyocd.target.pack.cmsis_pack import CmsisPack  # type: ignore[import-untyped]

from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.board_config import (
    DEFAULT_BOARD_CONFIG_DIR,
    BoardConfig,
    ConfigError,
    load_selected_board_configs,
)
from pyocd_debug_mcp.guardrails.flash_gate import resolve_flash_request
from pyocd_debug_mcp.guardrails.gate import GateManager, GateRefusal
from pyocd_debug_mcp.guardrails.permissions import PermissionStore
from pyocd_debug_mcp.guardrails.plan_defs import PLAN_DEFINITIONS, PlanDefinition
from pyocd_debug_mcp.guardrails.plan_engine import PlanEngine, PlanRefusal
from pyocd_debug_mcp.firmstore.cache import (
    AttachmentCache,
    CacheResolution,
    ProbeIdentity,
    SerialEndpoint,
)
from pyocd_debug_mcp.firmstore.profiles import BoardProfile, ProfileError, ProfileRepository
from pyocd_debug_mcp.firmstore.reports import ReportWriter
from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.local_env import load_local_env
from pyocd_debug_mcp.kernel.registry import RegistryFastMCP
from pyocd_debug_mcp.kernel.operations import (
    ManagedOperation,
    OperationState,
    cancellation_checkpoint,
    run_if_not_cancelled,
)
from pyocd_debug_mcp.kernel.finalizers import build_finalizer
from pyocd_debug_mcp.kernel.hygiene import require_clean_startup
from pyocd_debug_mcp.kernel.processes import run_owned
from pyocd_debug_mcp.kernel.run_state import create_server_run
from pyocd_debug_mcp.pack_provision import (
    PackProvisionError,
    load_manifest,
    read_bounded_pack_bytes,
    sha256_bytes,
    verified_pack_for_target,
)
from pyocd_debug_mcp.probe_inventory import (
    list_connected_probes,
    probe_family_from_pyocd_probe,
    resolve_probe_for_board,
)
from pyocd_debug_mcp.reference_artifacts import resolve_reference_artifacts
from pyocd_debug_mcp.serial_resolver import (
    BoardLike,
    ProbeLike,
    SerialPortInfo,
    list_serial_ports,
    resolve_serial_port,
)
from pyocd_debug_mcp.services.convergence_watcher import (
    ConvergenceWatcher,
    FLASH_TOOL,
    UART_TOOL,
)
from pyocd_debug_mcp.services.session_runtime import (
    ActionContext,
    InMemorySessionStore,
    PolicyRefusal,
    SessionRecord,
    ToolEvent,
    ToolOutcome,
    WatcherBlocked,
    utc_now_text,
)
from pyocd_debug_mcp.services import target_control
from pyocd_debug_mcp.services.symbols import (
    find_symbols,
    read_symbol_u32 as read_symbol_u32_from_elf,
    resolve_symbol,
)
from pyocd_debug_mcp.services.uart_capture import (
    capture_uart_output,
    exchange_uart_output,
    write_uart_output,
)
from pyocd_debug_mcp.setup_flow.preflight import (
    PreflightBlock,
    PreflightSelections,
    PreflightInventory,
    ProbeCandidate,
    SerialCandidate,
    SetupUserInput,
)
from pyocd_debug_mcp.setup_flow.packs import (
    PackCandidate,
    PackCandidateError,
    PackCandidatePipeline,
)
from pyocd_debug_mcp.setup_flow.research import (
    ResearchError,
    ResearchTracker,
    ValidationOutcome,
    make_research_request,
)
from pyocd_debug_mcp.setup_flow.board_catalog import (
    BoardCatalogError,
    ReviewedSupportAmbiguityError,
    ReviewedSupportNotFoundError,
    hash_local_datasheet,
    resolve_reviewed_support,
    resolve_reviewed_support_from_datasheet,
)
from pyocd_debug_mcp.setup_flow.reviewed_evidence import (
    load_pinned_reviewed_evidence,
    pack_matches_reviewed_catalog,
)
from pyocd_debug_mcp.setup_flow.device_support import (
    DeviceSupportCandidate,
    PackAddressRegion,
    derive_candidate_binding,
    resolve_available_pack_support,
    resolve_persisted_pack_support,
    resolve_registered_pack_geometry,
    has_available_pack_binding,
    verified_pack_for_candidate,
)
from pyocd_debug_mcp.setup_flow.datasheet_evidence import (
    capture_datasheet_evidence,
    replay_datasheet_evidence,
)
from pyocd_debug_mcp.setup_flow.setup import (
    RunAssignmentStore,
    SetupPhase,
    SetupPhaseContext,
    SetupPhaseOutcome,
    SetupWorkflow,
    SetupWorkflowError,
)
from pyocd_debug_mcp.setup_flow.targets import (
    ProfileCommitCoordinator,
    TargetResolutionError,
    TargetResolver,
)
from pyocd_debug_mcp.setup_flow.validate import (
    BoardValidator,
    SafetyMapSnapshot,
    ValidationBackend,
    ValidationHooks,
    ValidationInventory,
    ValidationProbe,
    ValidationRequest,
    ValidationSerial,
)
from pyocd_debug_mcp.safety.enforce import SafetyPolicy, SafetyPolicyError
from pyocd_debug_mcp.safety.linker import BuildRole
from pyocd_debug_mcp.safety.map_build import (
    GenericMapIdentity,
    GenericMapGeometry,
    GenericSafetyMapDocument,
    GenericSourceDigests,
    EraseSector,
    MapGeometry,
    MapIdentity,
    MapPartitions,
    RegionContribution,
    RegionSource,
    SafetyMapBuildRequest,
    SafetyMapBuilder,
    SafetyMapError,
    SafetyMapRepository,
    reviewed_map_source_documents,
    build_artifact_application_allocation,
    generic_map_with_allocation,
    require_reconciled_authority,
    region_conflicts,
)
from pyocd_debug_mcp.safety.refresh import SafetyRefreshRequest, SafetyRefresher
from pyocd_debug_mcp.safety.regions import (
    AddressRange,
    Provenance,
    RegionKind,
    SafetyRegion,
    SourceAuthority,
)
from pyocd_debug_mcp.target_errors import (
    LockedTargetError,
    ProbeNotFoundError,
    ReferenceArtifactError,
    SymbolLookupError,
    TargetConnectionError,
    UnsupportedArtifactError,
)
from pyocd_debug_mcp.timeouts import (
    DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS,
    default_server_timeout_config,
    subprocess_timeout_stream_text,
)
from pyocd_debug_mcp.services.connections import (
    BoardNotConnectedError,
    ConnectionAssignmentError,
    ConnectionManager,
    ManagedConnection,
    stable_connection_identity,
)
from pyocd_debug_mcp.tools.handshake import register_initialization_handshake
from pyocd_debug_mcp.tools.artifacts import build_artifact_handlers
from pyocd_debug_mcp.tools.breakpoints import (
    BreakpointToolServices,
    build_breakpoint_handlers,
)
from pyocd_debug_mcp.tools.batch import build_batch_handlers
from pyocd_debug_mcp.tools.execution import ExecutionToolServices, build_execution_handlers
from pyocd_debug_mcp.tools.flash import FlashToolServices, build_flash_handlers
from pyocd_debug_mcp.tools.memory import MemoryToolServices, build_memory_handlers
from pyocd_debug_mcp.tools.misc import MiscToolServices, build_misc_handlers
from pyocd_debug_mcp.tools.plans import (
    forbid_unknown_tool_arguments,
    register_plan_tools,
)
from pyocd_debug_mcp.tools.registers import (
    RegisterPreconditionError,
    RegisterToolServices,
    build_register_handlers,
    validate_guarded_register_call,
)
from pyocd_debug_mcp.tools.session import SessionToolServices, build_session_handlers
from pyocd_debug_mcp.tools.serial import (
    SerialToolServices,
    build_serial_handlers,
    read_serial as read_serial_action,
    write_serial as write_serial_action,
)
from pyocd_debug_mcp.tools.setup import (
    SetupToolLoadState,
    SetupToolServices,
    build_setup_handlers,
)
from pyocd_debug_mcp.tools.unlock import (
    UnlockCoordinator,
    UnlockToolServices,
    build_unlock_handlers,
)

load_local_env()

mcp = RegistryFastMCP("pyocd-debug")
tool_registry = mcp.registry
server_run = create_server_run()
assignment_store = RunAssignmentStore(server_run.assignments)

connection_manager = ConnectionManager()
gate_manager = GateManager(server_run.gates)
permission_store = PermissionStore(server_run)
_current_symbol_artifacts: dict[str, tuple[Path, str]] = {}
_session_store = InMemorySessionStore()
_watcher = ConvergenceWatcher()
_staged_server_timeouts = default_server_timeout_config()
NO_BOARD_CONFIG_MESSAGE = (
    "No board config loaded for this session. Pass `board_id` to `connect` "
    "(or set PYOCD_BOARD_ID) to load boards/<board>.yaml facts."
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


def _validate_plan_scope(
    definition: PlanDefinition,
    board_id: str,
    session_id: str | None,
) -> None:
    connection = connection_manager.maybe_connection(board_id)
    if definition.action_name == "board_setup":
        try:
            profile = _profile_repository.load(board_id, include_legacy=True)
        except ProfileError:
            profile = None
        existing_profile_paths = tuple(
            _profile_repository.store.layout.board_profile(board_id, suffix=suffix)
            for suffix in (".yaml", ".yml", ".json")
        )
        if profile is not None or any(path.exists() for path in existing_profile_paths):
            active = plan_engine.active_plan("board_setup", board_id)
            mode = active.action_parameters.get("mode") if active is not None else None
            if mode != "repair" or profile is None or not _profile_needs_repair(profile):
                raise PlanRefusal(
                    "plan/setup-established-profile",
                    f"Board '{board_id}' already has a profile. Only an incomplete, parseable "
                    "same-identity profile can use mode=repair; established identity cannot be "
                    "rewritten.",
                    session_id=session_id,
                )
        if session_id is not None or connection is not None:
            raise PlanRefusal(
                "plan/setup-session-active",
                f"Board '{board_id}' already has an active debug session; use validation or "
                "disconnect before first-time setup.",
                session_id=session_id,
            )
        return
    if definition.action_name in {"connect_override", "connect_under_reset"}:
        if connection is not None:
            raise PlanRefusal(
                "plan/session-already-active",
                f"Board '{board_id}' is already connected; disconnect it before exceptional "
                "connection setup.",
                session_id=session_id,
            )
        if session_id is not None:
            raise PlanRefusal(
                "plan/session-invalid",
                f"Board '{board_id}' has a stale session identity; submit a new plan.",
                session_id=session_id,
            )
        return
    if connection is None:
        raise PlanRefusal(
            "plan/session-invalid",
            f"Board '{board_id}' has no active connection; reconnect and submit a new plan.",
            session_id=session_id,
        )
    runtime = connection.runtime_session
    if runtime.closed_at is not None or runtime.session_id != session_id:
        raise PlanRefusal(
            "plan/session-invalid",
            f"Board '{board_id}' no longer has the session bound by this plan.",
            session_id=session_id,
        )


plan_engine = PlanEngine(
    server_run,
    tool_registry,
    permission_provider=permission_store,
    scope_validator=_validate_plan_scope,
)
permission_store.set_revocation_handler(plan_engine.invalidate)

_GUARDED_READ_ACTIONS = frozenset({"read_memory_address", "read_serial"})
_WRITE_CAPABLE_ACTIONS = frozenset(
    {
        "write_memory",
        "write_cpu_register",
        "set_execution_state",
        "register_write",
        "set_breakpoint",
        "flash_application",
        "flash_bootloader",
        "write_serial",
        "serial_exchange",
    }
)
_safety_policy: SafetyPolicy
_unlock_coordinator: UnlockCoordinator


def _current_target(board_id: str) -> str:
    handle = _handle(board_id)
    if handle.board is not None:
        return handle.board.pyocd_target
    return (handle.target_override or "").strip()


def _check_memory_safety(board_id: str, address: int, width: int) -> None:
    _safety_policy.check_memory_write(board_id, address, width)


def _check_memory_read_safety(board_id: str, address: int, size_bytes: int) -> None:
    _safety_policy.check_memory_read(board_id, address, size_bytes)


def _check_register_safety(board_id: str, address: int) -> None:
    _safety_policy.check_register_write(board_id, address)


def _check_breakpoint_safety(board_id: str, address: int, elf_path: Path) -> None:
    _safety_policy.check_breakpoint(board_id, address, elf_path)


def _check_flash_safety(tool_name: str, board_id: str, artifact: Path) -> None:
    role = BuildRole.APPLICATION if tool_name == "flash_application" else BuildRole.BOOTLOADER
    current = _safety_repository.load_current(board_id)
    if role is BuildRole.APPLICATION and isinstance(current, GenericSafetyMapDocument):
        _safety_policy.check_generic_application_candidate(
            board_id,
            artifact,
            current_target=_current_target(board_id),
        )
        return
    try:
        _safety_policy.check_flash(
            board_id,
            role,
            artifact,
            current_target=_current_target(board_id),
        )
    except SafetyPolicyError as exc:
        if role is not BuildRole.APPLICATION or exc.code != "safety/partition-authority-unavailable":
            raise
        _safety_policy.check_generic_application_candidate(
            board_id,
            artifact,
            current_target=_current_target(board_id),
        )


def _require_layer0(tool_name: str, board_id: str) -> None:
    if tool_name not in _GUARDED_READ_ACTIONS | _WRITE_CAPABLE_ACTIONS:
        return
    connection = connection_manager.connection_for(board_id)
    try:
        if tool_name in _GUARDED_READ_ACTIONS:
            gate_manager.require_validated(board_id, connection.connection_id)
        else:
            aggregate = _safety_policy.current_aggregate(board_id)
            stamp = gate_manager.require_write(board_id, connection.connection_id, aggregate)
            if tool_name == "flash_bootloader" and stamp.identity_capability != "exact":
                raise GateRefusal(
                    "gate/exact-identity-required",
                    "Bootloader flash requires an exact live silicon proof; compatible identity "
                    "is sufficient only for artifact-contained application programming.",
                    remedy=("establish_exact_identity_evidence", "board_validate"),
                )
    except (GateRefusal, SafetyPolicyError) as exc:
        code = exc.code
        raise PlanRefusal(
            code,
            str(exc),
            session_id=_active_session_id(board_id),
        ) from exc


def _parse_action_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must not be boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 0)
    raise ValueError(f"{field_name} must be an integer or numeric string")


def _register_write_is_write_only(board_id: str, address: int, mask: int) -> bool:
    allowed = _safety_policy.check_register_write(board_id, address)
    write_only = RegionKind.PERIPHERAL_WRITE_ONLY in allowed.classifications
    if write_only and mask != 0xFFFFFFFF:
        raise ValueError(
            "write-only SVD registers require a full 32-bit mask because masked "
            "read-modify-write would perform a prohibited read"
        )
    return write_only


def _enforce_action_containment(
    tool_name: str,
    board_id: str,
    parameters: Mapping[str, object],
) -> None:
    try:
        if tool_name == "register_write":
            _register_write_is_write_only(
                board_id,
                _parse_action_integer(parameters["address"], "address"),
                _parse_action_integer(parameters["mask"], "mask"),
            )
        elif tool_name == "read_memory_address":
            address = _parse_action_integer(parameters["address"], "address")
            width = _parse_action_integer(parameters["width"], "width")
            if width not in {8, 16, 32}:
                raise ValueError("width must be one of: 8, 16, 32")
            length_value = parameters.get("length")
            size_bytes = (
                width // 8
                if length_value is None
                else _parse_action_integer(length_value, "length")
            )
            _safety_policy.check_memory_read(board_id, address, size_bytes)
        elif tool_name == "write_memory":
            target = parameters["symbol_or_address"]
            try:
                address = _parse_action_integer(target, "symbol_or_address")
            except (TypeError, ValueError):
                if not isinstance(target, str) or not target.strip():
                    raise ValueError("symbol_or_address must be a symbol or address")
                handle = _handle(board_id)
                address = resolve_symbol(_symbol_artifact_for_handle(handle), target).address
            width = _parse_action_integer(parameters["width"], "width")
            _safety_policy.check_memory_write(board_id, address, width)
        elif tool_name == "set_breakpoint":
            target = parameters["symbol_or_address"]
            elf_path = Path(cast(str, parameters["elf_artifact"])).expanduser().resolve()
            try:
                address = _parse_action_integer(target, "symbol_or_address")
            except (TypeError, ValueError):
                if not isinstance(target, str) or not target.strip():
                    raise ValueError("symbol_or_address must be a symbol or address")
                address = resolve_symbol(elf_path, target).address
            _safety_policy.check_breakpoint(board_id, address, elf_path)
        elif tool_name in {"flash_application", "flash_bootloader"}:
            handle = _maybe_handle(board_id)
            context = _action_context(tool_name, board_id)
            request = resolve_flash_request(
                handle,
                explicit_path=cast(str, parameters["artifact"]),
                action_context=context,
            )
            _check_flash_safety(tool_name, board_id, request.artifact_path)
            if tool_name == "flash_application":
                _stage_generic_allocation(board_id, request.artifact_path, handle)
    except (SafetyPolicyError, ValueError, KeyError) as exc:
        code = exc.code if isinstance(exc, SafetyPolicyError) else "safety/invalid-request"
        raise PlanRefusal(
            code,
            str(exc),
            session_id=_active_session_id(board_id),
        ) from exc


def _enforce_guarded_invocation(
    tool_name: str,
    board_id: str,
    arguments: Mapping[str, object],
) -> None:
    parameters = {name: value for name, value in arguments.items() if name != "board_id"}

    def validate_layer0_and_action() -> None:
        if tool_name in {"board_setup", "board_fix_setup"}:
            connection_id = parameters.get("connection_id")
            if not isinstance(connection_id, str):
                raise PlanRefusal(
                    "plan/setup-assignment-invalid",
                    "Setup requires the exact connection_id returned by setup_overview.",
                    session_id=_active_session_id(board_id),
                )
            try:
                assignment_store.require(connection_id, board_id)
            except SetupWorkflowError as exc:
                raise PlanRefusal(
                    "plan/setup-assignment-invalid",
                    str(exc),
                    session_id=_active_session_id(board_id),
                ) from exc
        _require_layer0(tool_name, board_id)
        if tool_name == "target_unlock":
            _unlock_coordinator.validate_execution(board_id, parameters)
        if tool_name in {"write_cpu_register", "set_execution_state", "register_write"}:
            try:
                validate_guarded_register_call(
                    register_services,
                    tool_name,
                    board_id,
                    parameters,
                )
            except RegisterPreconditionError as exc:
                raise PlanRefusal(
                    "plan/layer2-precondition",
                    str(exc),
                    session_id=_active_session_id(board_id),
                ) from exc
        _enforce_action_containment(tool_name, board_id, parameters)

    plan_engine.enforce(
        tool_name,
        board_id,
        parameters,
        session_id=_active_session_id(board_id),
        preconditions=validate_layer0_and_action,
    )


def _supported_registers_for(board_id: str) -> tuple[str, ...]:
    with connection_manager.lock_for(board_id):
        return target_control.supported_core_registers(_handle(board_id))


def _masked_register_write(
    board_id: str,
    address: int,
    mask: int,
    value: int,
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
            if _register_write_is_write_only(board_id, address, mask):
                target_control.write_memory(handle, address, value, 32)
                return (
                    f"Write-only peripheral register 0x{address:08X}: wrote "
                    f"0x{value:08X} with a full mask and no preceding read."
                )
            prior = target_control.read_memory(handle, address, 32)
            updated = (prior & ~mask) | (value & mask)
            target_control.write_memory(handle, address, updated, 32)
            return (
                f"Peripheral register 0x{address:08X}: 0x{prior:08X} -> "
                f"0x{updated:08X} with mask 0x{mask:08X}."
            )

        return _run_logged_tool(board_id, "register_write", normalized_args, operation)


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


def _format_refusal(refusal: PolicyRefusal, *, session_id: str | None) -> str:
    return f"Refused [{refusal.code}]: {refusal.message} session_id={session_id or '(none)'}"


def _format_block(blocked: WatcherBlocked, *, session_id: str | None) -> str:
    return f"Blocked [{blocked.code}]: {blocked.message} session_id={session_id or '(none)'}"


def _refuse_invalid_argument(
    tool_name: str,
    normalized_args: Mapping[str, object],
    *,
    code: str,
    message: str,
    started: float,
    board_id: str,
    session: SessionRecord | None,
) -> str:
    refusal = PolicyRefusal(code, message)
    _record_event(
        tool_name,
        normalized_args,
        outcome_kind=ToolOutcome.REFUSED,
        error_code=refusal.code,
        duration_ms=_duration_ms(started),
        details={"message": refusal.message},
        board_id=board_id,
        session=session,
    )
    return _format_refusal(refusal, session_id=_active_session_id(board_id))


def _record_blocked_event(
    tool_name: str,
    normalized_args: Mapping[str, object],
    blocked: WatcherBlocked,
    *,
    started: float,
    board_id: str,
    session: SessionRecord | None,
) -> ToolEvent:
    return _record_event(
        tool_name,
        normalized_args,
        outcome_kind=ToolOutcome.BLOCKED,
        error_code=blocked.code,
        duration_ms=_duration_ms(started),
        details={"message": blocked.message},
        board_id=board_id,
        session=session,
    )


def _parse_int(text: str) -> int:
    """Parse an int from a string, accepting hex (0x...), binary, or decimal."""
    return int(text, 0)


def _word_size_is_valid(word_size: int) -> bool:
    return word_size in {8, 16, 32}


def _run_cmd(
    cmd: list[str],
    timeout_seconds: float = DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
    try:
        result = run_owned(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
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
    *,
    allow_environment_overrides: bool = True,
) -> BoardConfig | None:
    """Load one board definition through the shared loader, or None if unselected.

    This is the server's single path to ``boards/<board>.yaml`` — the same loader
    the Stage 0 CLI uses — so a custom ST/nRF board's facts (pyOCD target, recover
    policy, silicon id, baud) reach the MCP tools, not just the CLI.

    When ``allow_environment_overrides`` is true, ``board_id``/``board_config``
    fall back to the ``PYOCD_BOARD_ID`` / ``PYOCD_BOARD_CONFIG`` environment
    variables (the stdio-launch config channel). Public profile-only ``connect``
    disables that fallback. Returns ``None`` when no board is named. Raises
    ``ConfigError`` if a named board cannot be found or a config file is malformed.
    """
    environment_board = os.environ.get("PYOCD_BOARD_ID") if allow_environment_overrides else None
    bid = (board_id or environment_board or "").strip()
    if not bid:
        return None
    environment_config = (
        os.environ.get("PYOCD_BOARD_CONFIG") if allow_environment_overrides else None
    )
    extra = board_config or environment_config or None
    if extra is None:
        # Schema-v2 project-local profiles are the authoritative result of the
        # setup workflow.  Resolve them before the checkout's legacy boards/
        # compatibility directory so a clean artifact root can subsequently
        # connect by its new logical board id.
        repository = globals().get("_profile_repository")
        if isinstance(repository, ProfileRepository):
            try:
                return repository.load(bid, include_legacy=False).board
            except ProfileError:
                pass
    extra_paths = [Path(extra)] if extra else []
    boards = load_selected_board_configs(
        DEFAULT_BOARD_CONFIG_DIR,
        extra_paths=extra_paths,
        requested_ids=[bid],
    )
    return boards[0]


def format_board_info(b: BoardConfig) -> str:
    """Render a loaded board definition's facts as a stable text block."""
    lines = [
        f"board_id: {b.board_id}",
        f"display_name: {b.display_name}",
        f"mcu_family: {b.mcu_family}",
        f"probe_family: {b.probe_family}",
        f"pyocd_target: {b.pyocd_target}",
        f"default_baudrate: {b.default_baudrate}",
        (
            f"test_read_address: 0x{b.test_addr:08X}"
            if b.test_addr is not None
            else "test_read_address: (not configured)"
        ),
        f"requires_recover_validation: {b.requires_recover_validation}",
        f"recover_mode: {b.recover_mode or '(none)'}",
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


def build_session_options(
    board: BoardConfig | None, target: str | None
) -> dict[str, object] | None:
    """Compatibility wrapper around the shared target-control option builder."""
    return target_control.build_session_options(board, target)


def _should_bypass_jlink_probe_resolution(
    board: BoardConfig | None,
    *,
    platform_name: str | None = None,
) -> bool:
    if board is None or board.probe_family != "jlink":
        return False
    current_platform = platform_name or sys.platform
    return current_platform.startswith("win")


def _resolve_probe_uid_for_connect(
    board: BoardConfig | None,
    unique_id: str | None,
    *,
    allow_environment_override: bool = True,
) -> str | None:
    if unique_id is not None:
        return unique_id
    env_uid = os.environ.get("PYOCD_PROBE_UID") or None if allow_environment_override else None
    if env_uid is not None:
        return env_uid
    if board is None:
        return None

    allow_subprocess_fallback = True
    if _should_bypass_jlink_probe_resolution(board):
        # On this Windows host, the risky path is the subprocess fallback
        # behind probe resolution, not the direct pyOCD API enumeration. Keep
        # using API-derived UIDs when available so J-Link stdio attaches still
        # work on boards like nrf52840dk, but never pre-run the subprocess
        # probe-listing path for implicit J-Link selection.
        allow_subprocess_fallback = False

    resolution = resolve_probe_for_board(
        board,
        run_cmd=_run_cmd,
        allow_single_fallback=True,
        allow_subprocess_fallback=allow_subprocess_fallback,
    )
    if resolution.probe is None:
        if not allow_subprocess_fallback:
            return None
        raise RuntimeError(f"Probe resolution failed for {board.display_name}: {resolution.note}")
    if not resolution.probe.uid:
        remedy = (
            "Initialize connect_override-plan for a deliberate exceptional manual probe "
            "selection; do not add an override to normal connect."
            if not allow_environment_override
            else "Supply the planned probe UID through guarded connect_override."
        )
        raise RuntimeError(
            f"Probe resolution for {board.display_name} did not yield a unique id. {remedy}"
        )
    return resolution.probe.uid


def _handle(board_id: str) -> TargetSessionHandle:
    """Return the named board's live session handle or raise if disconnected."""

    return connection_manager.handle_for(board_id)


def _maybe_handle(board_id: str) -> TargetSessionHandle | None:
    connection = connection_manager.maybe_connection(board_id)
    return connection.handle if connection is not None else None


def _connect_impl(
    board_id: str,
    unique_id: str | None = None,
    target: str | None = None,
    board_config: str | None = None,
    *,
    allow_environment_overrides: bool,
    allow_missing_profile: bool = False,
) -> str:
    """Assign one connected probe session to the required logical board.

    Args:
        board_id: Required logical board identity. It also selects facts from
            ``boards/<board_id>.yaml`` through the shared board-config loader.
        unique_id: Whole or partial probe serial/unique ID for the guarded override path.
        target: Target type override, e.g. "stm32f407vg" or "nrf52833". Takes
            precedence over a board config. Omit to use the selected board's
            target (when ``board_id`` is given), else the ``PYOCD_TARGET``
            environment variable, else pyOCD auto-detection.
        board_config: Path to an extra board-config file for the guarded override path.
        allow_environment_overrides: Whether guarded/manual launch-time override variables may
            participate. Public normal connect always passes false.
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
                "connect",
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
        uid = None
        tgt = None
        try:
            try:
                board = resolve_board_config(
                    board_id,
                    board_config,
                    allow_environment_overrides=allow_environment_overrides,
                )
            except ConfigError:
                if not allow_missing_profile or not target:
                    raise
                board = None
            uid = _resolve_probe_uid_for_connect(
                board,
                unique_id,
                allow_environment_override=allow_environment_overrides,
            )
            tgt = (
                target
                or (board.pyocd_target if board else None)
                or (os.environ.get("PYOCD_TARGET") if allow_environment_overrides else None)
                or None
            )
            try:
                stored_profile = _profile_repository.load(board_id, include_legacy=False)
            except ProfileError:
                if _profile_repository.store.layout.board_profile(board_id).is_file():
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
                        and target.casefold() != stored_profile.board.pyocd_target.casefold()
                    ):
                        raise ValueError(
                            "generic profile target override must match its exact PDSC target"
                        )
                    tgt = stored_profile.board.pyocd_target
            handle = target_control.open_session(
                board=board,
                unique_id=uid,
                target=tgt,
                server_timeouts=_staged_server_timeouts,
                pack_path=(selected_pack.path if selected_pack is not None else None),
                pack_sha256=(selected_pack.spec.sha256 if selected_pack is not None else None),
                pdsc_device=selected_pdsc_device,
            )
        except Exception as exc:  # noqa: BLE001 - preserve the original connect error
            _record_event(
                "connect",
                normalized_args,
                outcome_kind=ToolOutcome.FAILED,
                error_code=_error_code(exc),
                duration_ms=_duration_ms(started),
                details={"message": str(exc)[:300]},
                board_id=board_id,
                probe_uid=uid,
                route_used=None,
            )
            raise

        connection_id = stable_connection_identity(handle)
        runtime_session = _session_store.start_session(
            board_id=board_id,
            connection_id=connection_id,
            probe_uid=handle.probe_uid,
            route_used=handle.route_used,
        )
        try:
            connection_manager.assign(
                board_id,
                handle,
                runtime_session,
                connection_id=connection_id,
            )
            gate_manager.clear(board_id, "new connection requires board_validate")
        except ConnectionAssignmentError as exc:
            _record_event(
                "connect",
                normalized_args,
                outcome_kind=ToolOutcome.FAILED,
                error_code="connection/already-assigned",
                duration_ms=_duration_ms(started),
                details={"message": str(exc)[:300]},
                board_id=board_id,
                session=runtime_session,
            )
            _session_store.close_session(runtime_session)
            target_control.close_session(handle)
            raise
        suffix = f" [board config: {board.board_id}]" if board else ""
        board_name = handle.session.board.name if handle.session.board is not None else "<unknown>"
        result = (
            f"Connected to board '{board_name}' via probe "
            f"{handle.probe_uid or '(unknown)'} via {handle.route_used}.{suffix} "
            f"session_id={runtime_session.session_id}"
        )
        _record_event(
            "connect",
            normalized_args,
            outcome_kind=ToolOutcome.SUCCESS,
            error_code=None,
            duration_ms=_duration_ms(started),
            details={"board_name": board_name},
            board_id=board_id,
            session=runtime_session,
        )
        return result


@mcp.tool()
def connect(board_id: str) -> str:
    """Connect using only the named schema-v2 project profile.

    Normal connection accepts no manual probe, target, or external board-config override.
    Initialize connect_override-plan for a deliberate exceptional manual connection.
    """

    return _connect_impl(board_id, allow_environment_overrides=False)


def _connect_override_impl(
    board_id: str,
    unique_id: str | None = None,
    target: str | None = None,
    board_config: str | None = None,
) -> str:
    """Connect diagnostically with an explicit target before a profile exists."""

    return _connect_impl(
        board_id,
        unique_id,
        target,
        board_config,
        allow_environment_overrides=True,
        allow_missing_profile=True,
    )


def _connect_under_reset_impl(
    board_id: str,
    probe_uid: str | None,
    target_override: str | None,
) -> str:
    """Open and assign one session using physical reset-line attach."""

    with connection_manager.lock_for(board_id):
        started = time.monotonic()
        normalized_args: dict[str, object] = {
            "board_id": board_id,
            "probe_uid": probe_uid,
            "target_override": target_override,
        }
        if connection_manager.maybe_connection(board_id) is not None:
            return _refuse_invalid_argument(
                "connect_under_reset",
                normalized_args,
                code="connection/already-active",
                message=f"Board '{board_id}' is already connected; disconnect it first.",
                started=started,
                board_id=board_id,
                session=_runtime_for(board_id),
            )
        board = resolve_board_config(board_id, None)
        resolved_uid = _resolve_probe_uid_for_connect(board, probe_uid)
        resolved_target = (
            target_override
            or (board.pyocd_target if board else None)
            or os.environ.get("PYOCD_TARGET")
            or None
        )
        try:
            stored_profile = _profile_repository.load(board_id, include_legacy=False)
        except ProfileError:
            if _profile_repository.store.layout.board_profile(board_id).is_file():
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
                    target_override is not None
                    and target_override.casefold()
                    != stored_profile.board.pyocd_target.casefold()
                ):
                    raise ValueError(
                        "generic profile target override must match its exact PDSC target"
                    )
                resolved_target = stored_profile.board.pyocd_target
        try:
            handle = target_control.connect_under_reset(
                board=board,
                unique_id=resolved_uid,
                target=resolved_target,
                server_timeouts=_staged_server_timeouts,
                pack_path=(selected_pack.path if selected_pack is not None else None),
                pack_sha256=(selected_pack.spec.sha256 if selected_pack is not None else None),
                pdsc_device=selected_pdsc_device,
            )
            connection_id = stable_connection_identity(handle)
            runtime = _session_store.start_session(
                board_id=board_id,
                connection_id=connection_id,
                probe_uid=handle.probe_uid,
                route_used=handle.route_used,
            )
            connection_manager.assign(
                board_id,
                handle,
                runtime,
                connection_id=connection_id,
            )
            gate_manager.clear(board_id, "new connection requires board_validate")
        except Exception as exc:  # noqa: BLE001 - preserve typed backend failure
            _record_event(
                "connect_under_reset",
                normalized_args,
                outcome_kind=ToolOutcome.FAILED,
                error_code=_error_code(exc),
                duration_ms=_duration_ms(started),
                details={"message": str(exc)[:300]},
                board_id=board_id,
                probe_uid=resolved_uid,
            )
            raise
        _record_event(
            "connect_under_reset",
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
            f"{handle.route_used}; target halted and reset line released."
        )


@mcp.tool()
def disconnect(board_id: str) -> str:
    """Close only the named board's debug session and release its probe."""

    with connection_manager.lock_for(board_id):
        connection = connection_manager.maybe_connection(board_id)
        if connection is None:
            assignment_store.clear_board(board_id)
            gate_manager.clear(board_id, "disconnect requested")
            plan_engine.invalidate_board(board_id, "board disconnected")
            unlock = globals().get("_unlock_coordinator")
            if isinstance(unlock, UnlockCoordinator):
                unlock.invalidate_board(board_id)
            workflow = globals().get("_setup_workflow")
            if isinstance(workflow, SetupWorkflow):
                workflow.revoke(board_id)
            loader = globals().get("setup_tool_loader")
            if isinstance(loader, SetupToolLoadState):
                loader.clear_allowance(board_id)
            started = time.monotonic()
            result = "Not connected."
            _record_event(
                "disconnect",
                {"board_id": board_id},
                outcome_kind=ToolOutcome.SUCCESS,
                error_code=None,
                duration_ms=_duration_ms(started),
                details={"status": "not-connected"},
                board_id=board_id,
            )
            return result

        started = time.monotonic()
        handle = connection.handle
        runtime_session = connection.runtime_session
        gate_manager.clear(board_id, "board disconnected")
        assignment_store.clear_board(board_id)
        plan_engine.invalidate_board(board_id, "board disconnected")
        unlock = globals().get("_unlock_coordinator")
        if isinstance(unlock, UnlockCoordinator):
            unlock.invalidate_board(board_id)
        workflow = globals().get("_setup_workflow")
        if isinstance(workflow, SetupWorkflow):
            workflow.disconnect(connection.connection_id)
        loader = globals().get("setup_tool_loader")
        if isinstance(loader, SetupToolLoadState):
            loader.clear_allowance(board_id)
        connection_manager.clear(board_id)
        try:
            target_control.close_session(handle)
        except Exception as exc:  # noqa: BLE001 - preserve the original disconnect error
            _record_event(
                "disconnect",
                {"board_id": board_id},
                outcome_kind=ToolOutcome.FAILED,
                error_code=_error_code(exc),
                duration_ms=_duration_ms(started),
                details={"message": str(exc)[:300]},
                board_id=board_id,
                session=runtime_session,
            )
            raise

        _record_event(
            "disconnect",
            {"board_id": board_id},
            outcome_kind=ToolOutcome.SUCCESS,
            error_code=None,
            duration_ms=_duration_ms(started),
            board_id=board_id,
            session=runtime_session,
        )
        _session_store.close_session(runtime_session)
        return f"Disconnected board '{board_id}'."


@mcp.tool()
def get_board_info(board_id: str) -> str:
    """Return the facts from the board config the session was opened with.

    Reports the ``boards/<board>.yaml`` definition active for this session —
    pyOCD target, MCU and probe family, recover policy, silicon-id expectation,
    default UART baud, and the smoke-test read address. Returns a notice when
    ``connect`` was called without a ``board_id`` (raw-target mode), where these
    facts were not loaded.
    """
    with connection_manager.lock_for(board_id):

        def operation() -> str:
            connection = connection_manager.maybe_connection(board_id)
            if connection is None:
                return f"Board '{board_id}' is not connected. Call `connect` first."
            handle = connection.handle
            b = handle.board
            if b is None:
                return NO_BOARD_CONFIG_MESSAGE
            return format_board_info(b)

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

    probe = _ProbeHint(handle.probe_uid) if handle.probe_uid else None
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


def _handle_mutation_event(board_id: str, event: ToolEvent) -> None:
    runtime = _runtime_for(board_id)
    if runtime is None:
        return
    decision = _watcher.observe_event(runtime, event)
    if decision is not None:
        _session_store.set_block(
            runtime,
            decision.action_family,
            decision.code,
            decision.message,
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
            details={"message": str(exc)[:300]},
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


@mcp.tool()
def get_state(board_id: str) -> str:
    """Return the current core run state (e.g. HALTED, RUNNING, RESET)."""
    with connection_manager.lock_for(board_id):

        def operation() -> str:
            return target_control.get_state(_handle(board_id))

        return _run_logged_tool(board_id, "get_state", {"board_id": board_id}, operation)


@mcp.tool()
def halt(board_id: str) -> str:
    """Halt the core."""
    with connection_manager.lock_for(board_id):

        def operation() -> str:
            return _complete_effect(lambda: target_control.halt(_handle(board_id)), "Halted.")

        return _run_logged_tool(board_id, "halt", {"board_id": board_id}, operation)


@mcp.tool()
def resume(board_id: str) -> str:
    """Resume execution of the core."""
    with connection_manager.lock_for(board_id):

        def operation() -> str:
            return _complete_effect(lambda: target_control.resume(_handle(board_id)), "Resumed.")

        return _run_logged_tool(board_id, "resume", {"board_id": board_id}, operation)


@mcp.tool()
def step(board_id: str) -> str:
    """Single-step one instruction and return the new program counter."""
    with connection_manager.lock_for(board_id):

        def operation() -> str:
            return f"Stepped. pc=0x{target_control.step(_handle(board_id)):08X}"

        return _run_logged_tool(board_id, "step", {"board_id": board_id}, operation)


@mcp.tool()
def reset(board_id: str, halt_after: bool = True) -> str:
    """Reset the target.

    Args:
        halt_after: If True, halt at the reset vector (reset-and-halt).
            If False, reset and let the target run.
    """
    with connection_manager.lock_for(board_id):

        def operation() -> str:
            return _complete_effect(
                lambda: target_control.reset(_handle(board_id), halt_after=halt_after),
                "Reset and halted." if halt_after else "Reset and running.",
            )

        return _run_logged_tool(
            board_id,
            "reset",
            {"board_id": board_id, "halt_after": halt_after},
            operation,
        )


@mcp.tool()
def read_core_register(board_id: str, name: str) -> str:
    """Read a core register by name (e.g. "pc", "sp", "r0", "xpsr").

    Returns the value as a hex string.
    """
    with connection_manager.lock_for(board_id):

        def operation() -> str:
            return f"0x{target_control.read_core_register(_handle(board_id), name):08X}"

        return _run_logged_tool(
            board_id,
            "read_core_register",
            {"board_id": board_id, "name": name},
            operation,
        )


@mcp.tool()
def write_core_register(board_id: str, name: str, value: str) -> str:
    """Write a core register by name. ``value`` may be hex (0x...) or decimal."""
    with connection_manager.lock_for(board_id):

        def operation() -> str:
            return _complete_effect(
                lambda: target_control.write_core_register(
                    _handle(board_id), name, _parse_int(value)
                ),
                f"Wrote {value} to {name}.",
            )

        return _run_logged_tool(
            board_id,
            "write_core_register",
            {"board_id": board_id, "name": name, "value": value},
            operation,
        )


@mcp.tool()
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
            return _refuse_invalid_argument(
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


@mcp.tool()
def read_memory_block(board_id: str, address: str, length: int) -> str:
    """Read ``length`` bytes from memory starting at ``address``.

    Returns the bytes as a space-separated hex string.
    """
    with connection_manager.lock_for(board_id):
        started = time.monotonic()
        normalized_args = {"board_id": board_id, "address": address, "length": length}
        if length <= 0:
            return _refuse_invalid_argument(
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


@mcp.tool()
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


@mcp.tool()
def write_memory(
    board_id: str,
    symbol_or_address: str | int,
    value: object,
    width: int = 32,
    allow_address_fallback: bool = False,
    reason: str | None = None,
) -> str:
    """Write a planned symbol or justified raw address; prefer symbol access."""

    return memory_tool_handlers["write_memory"](
        board_id,
        symbol_or_address,
        value,
        width,
        allow_address_fallback,
        reason,
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
            raise ReferenceArtifactError(
                f"Current symbol artifact changed after flash: {artifact}"
            )
        return artifact
    return resolve_reference_artifacts(handle.board).symbol_artifact


def _prepare_flashed_symbol_artifact(
    tool_name: str, board_id: str, artifact: Path
) -> tuple[Path, str]:
    del tool_name, board_id
    elf_artifact = artifact if artifact.suffix.casefold() == ".elf" else artifact.with_suffix(".elf")
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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
        if runtime is not None:
            try:
                _watcher.ensure_allowed(runtime, FLASH_TOOL)
            except WatcherBlocked as blocked:
                _record_blocked_event(
                    "flash_firmware",
                    normalized_args,
                    blocked,
                    started=started,
                    board_id=board_id,
                    session=runtime,
                )
                return _format_block(blocked, session_id=runtime.session_id)

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
            flashed = target_control.flash_firmware(
                active_handle,
                request.artifact_path,
                halt_after_reset=halt_after_reset,
            )
        except PolicyRefusal as exc:
            event = _record_event(
                "flash_firmware",
                normalized_args,
                outcome_kind=ToolOutcome.REFUSED,
                error_code=exc.code,
                duration_ms=_duration_ms(started),
                details={"message": exc.message},
                board_id=board_id,
                session=runtime,
            )
            if runtime is not None:
                _handle_mutation_event(board_id, event)
            return _format_refusal(exc, session_id=_active_session_id(board_id))
        except Exception as exc:  # noqa: BLE001 - preserve backend failure text
            event = _record_event(
                "flash_firmware",
                normalized_args,
                outcome_kind=ToolOutcome.FAILED,
                error_code=_error_code(exc),
                duration_ms=_duration_ms(started),
                details={"message": str(exc)[:300]},
                board_id=board_id,
                session=runtime,
            )
            if runtime is not None:
                _handle_mutation_event(board_id, event)
            raise

        state = "halted" if halt_after_reset else "running"
        event = _record_event(
            "flash_firmware",
            normalized_args,
            outcome_kind=ToolOutcome.SUCCESS,
            error_code=None,
            duration_ms=_duration_ms(started),
            details={"target_state": state},
            board_id=board_id,
            session=runtime,
        )
        if runtime is not None:
            _handle_mutation_event(board_id, event)
        return f"Flashed {flashed} via {active_handle.route_used}; target left {state}."


@mcp.tool()
def read_serial(
    board_id: str,
    expected_text: str | None = None,
    read_seconds: float = 3.0,
    baudrate: int | None = None,
    port: str | None = None,
    reset_on_open: bool = False,
) -> str:
    """Capture bounded UART output through the planned board-scoped UART service."""

    services = SerialToolServices(
        runtime_for=_runtime_for,
        active_session_id=_active_session_id,
        duration_ms=_duration_ms,
        record_event=_record_event,
        record_blocked_event=_record_blocked_event,
        format_refusal=_format_refusal,
        format_block=_format_block,
        ensure_uart_allowed=lambda runtime: _watcher.ensure_allowed(runtime, UART_TOOL),
        handle_for=_handle,
        resolve_port=_resolve_serial_port_for_session,
        capture_uart=lambda *args, **kwargs: capture_uart_output(*args, **kwargs),
        write_uart=lambda *args, **kwargs: write_uart_output(*args, **kwargs),
        exchange_uart=lambda *args, **kwargs: exchange_uart_output(*args, **kwargs),
        reset_target=lambda handle: target_control.reset(handle, halt_after=False),
        handle_mutation_event=lambda selected_board, event: _handle_mutation_event(
            selected_board, cast(ToolEvent, event)
        ),
        no_board_config_message=NO_BOARD_CONFIG_MESSAGE,
    )
    return read_serial_action(
        services,
        board_id,
        expected_text,
        read_seconds,
        baudrate,
        port,
        reset_on_open,
    )


@mcp.tool()
def write_serial(
    board_id: str,
    text: str,
    baudrate: int | None = None,
    port: str | None = None,
    append_newline: bool = False,
    timeout_seconds: float = 1.0,
) -> str:
    """Write bounded UTF-8 text through the planned board-scoped UART service."""

    services = SerialToolServices(
        runtime_for=_runtime_for,
        active_session_id=_active_session_id,
        duration_ms=_duration_ms,
        record_event=_record_event,
        record_blocked_event=_record_blocked_event,
        format_refusal=_format_refusal,
        format_block=_format_block,
        ensure_uart_allowed=lambda runtime: _watcher.ensure_allowed(runtime, UART_TOOL),
        handle_for=_handle,
        resolve_port=_resolve_serial_port_for_session,
        capture_uart=lambda *args, **kwargs: capture_uart_output(*args, **kwargs),
        write_uart=lambda *args, **kwargs: write_uart_output(*args, **kwargs),
        exchange_uart=lambda *args, **kwargs: exchange_uart_output(*args, **kwargs),
        reset_target=lambda handle: target_control.reset(handle, halt_after=False),
        handle_mutation_event=lambda selected_board, event: _handle_mutation_event(
            selected_board, cast(ToolEvent, event)
        ),
        no_board_config_message=NO_BOARD_CONFIG_MESSAGE,
    )
    return write_serial_action(
        services,
        board_id,
        text,
        baudrate,
        port,
        append_newline,
        timeout_seconds,
    )


_legacy_connect = connect
_legacy_disconnect = disconnect
_legacy_get_board_info = get_board_info
_legacy_get_state = get_state
_legacy_halt = halt
_legacy_resume = resume
_legacy_step = step
_legacy_reset = reset
_legacy_read_core_register = read_core_register
_legacy_write_core_register = write_core_register

session_tool_handlers = build_session_handlers(
    SessionToolServices(
        connect=_legacy_connect,
        connect_override=_connect_override_impl,
        disconnect=_legacy_disconnect,
        get_board_info=_legacy_get_board_info,
        get_state=_legacy_get_state,
    )
)
execution_tool_handlers = build_execution_handlers(
    ExecutionToolServices(
        halt=_legacy_halt,
        resume=_legacy_resume,
        step=_legacy_step,
        reset=lambda board_id, halt_after: _legacy_reset(board_id, halt_after=halt_after),
        connect_under_reset=_connect_under_reset_impl,
    )
)
register_services = RegisterToolServices(
    supported_registers=_supported_registers_for,
    read_register=_legacy_read_core_register,
    write_register=lambda board_id, name, value: _legacy_write_core_register(
        board_id, name, str(value)
    ),
    masked_register_write=_masked_register_write,
    check_register_write=_check_register_safety,
)
register_tool_handlers = build_register_handlers(register_services)

memory_services = MemoryToolServices(
    runtime_for=_runtime_for,
    active_session_id=_active_session_id,
    duration_ms=_duration_ms,
    record_event=_record_event,
    format_refusal=_format_refusal,
    handle_for=_handle,
    symbol_artifact_for=_symbol_artifact_for_handle,
    find_symbols=lambda artifact, query: find_symbols(artifact, query),
    resolve_symbol=resolve_symbol,
    read_target_memory=target_control.read_memory,
    read_target_block=target_control.read_memory_block,
    write_target_memory=target_control.write_memory,
    check_memory_read=_check_memory_read_safety,
    check_memory_write=_check_memory_safety,
)
memory_tool_handlers = build_memory_handlers(memory_services)


class _PendingGenericAllocation:
    __slots__ = ("expected_map_digest", "artifact_digest", "connection_id", "document")

    def __init__(
        self,
        expected_map_digest: str,
        artifact_digest: str,
        connection_id: str,
        document: GenericSafetyMapDocument,
    ) -> None:
        self.expected_map_digest = expected_map_digest
        self.artifact_digest = artifact_digest
        self.connection_id = connection_id
        self.document = document


_pending_generic_allocations: dict[str, _PendingGenericAllocation] = {}


def _stage_generic_allocation(
    board_id: str,
    artifact: Path,
    pending_handle: object,
) -> None:
    _pending_generic_allocations.pop(board_id, None)
    current = _safety_repository.load_current(board_id)
    if not isinstance(current, GenericSafetyMapDocument):
        return
    if not isinstance(pending_handle, TargetSessionHandle):
        raise SafetyPolicyError(
            "safety/session-missing",
            "Generic application allocation requires the active validated target session.",
            remedy=("connect", "board_validate"),
        )
    _, artifact_sectors = _safety_policy.check_generic_application_candidate(
        board_id,
        artifact,
        current_target=_current_target(board_id),
    )
    profile = _profile_repository.load(board_id, include_legacy=False)
    if profile.mcu_part_number is None:
        raise SafetyMapError("generic profile has no exact MCU part number")
    if profile.device_support is None:
        raise SafetyMapError("generic profile has no persisted device-support authority")
    support = resolve_persisted_pack_support(
        _profile_repository.store, profile.mcu_part_number, profile.device_support
    )
    geometry = resolve_registered_pack_geometry(support, _profile_repository.store)
    if geometry.driver_proof_digest is None:
        raise SafetyPolicyError(
            "safety/driver-unbounded",
            "The selected pack does not prove bounded sector erase/program behavior.",
            remedy=("board_safety_refresh",),
        )
    physical_sectors = tuple(item.address_range for item in current.geometry.erase_sectors)
    selected = set(artifact_sectors)
    prior_digest: str | None = None
    if current.deployment_policy != {"kind": "none"}:
        selected.update(
            AddressRange(int(item["start"]), int(item["end"]))
            for item in cast(list[dict[str, int]], current.deployment_policy["sectors"])
        )
        prior_digest = cast(str, current.deployment_policy["allocation_digest"])
    first = min(physical_sectors.index(item) for item in selected)
    last = max(physical_sectors.index(item) for item in selected)
    allocation_sectors = physical_sectors[first : last + 1]
    allocation_range = AddressRange(allocation_sectors[0].start, allocation_sectors[-1].end)
    if current.partitions.application is not None and current.partitions.application.contains(
        allocation_range
    ):
        return
    artifact_digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    policy = build_artifact_application_allocation(
        allocation_sectors,
        driver_proof_digest=geometry.driver_proof_digest,
        creation_map_digest=current.canonical_digest,
        creation_artifact_digest=artifact_digest,
        parent_allocation_digest=prior_digest,
    )
    allocated = generic_map_with_allocation(current, policy)
    connection = connection_manager.connection_for(board_id)
    _pending_generic_allocations[board_id] = _PendingGenericAllocation(
        current.canonical_digest,
        artifact_digest,
        connection.connection_id,
        allocated,
    )


def _prepare_generic_allocation(
    tool_name: str,
    board_id: str,
    artifact: Path,
    pending_handle: object,
) -> _PendingGenericAllocation | None:
    del pending_handle
    if tool_name != "flash_application":
        return None
    current = _safety_repository.load_current(board_id)
    if not isinstance(current, GenericSafetyMapDocument):
        return None
    pending = _pending_generic_allocations.pop(board_id, None)
    if pending is None:
        if current.partitions.application is not None:
            return None
        raise SafetyPolicyError(
            "safety/allocation-proof-missing",
            "The pre-execution artifact allocation proof is unavailable.",
            remedy=("replace_flash_plan",),
        )
    connection = connection_manager.connection_for(board_id)
    if (
        pending.expected_map_digest != current.canonical_digest
        or pending.connection_id != connection.connection_id
        or pending.artifact_digest != hashlib.sha256(artifact.read_bytes()).hexdigest()
    ):
        raise SafetyPolicyError(
            "safety/allocation-proof-stale",
            "The board, map, or artifact changed after allocation validation.",
            remedy=("replace_flash_plan",),
        )
    return pending


def _commit_generic_allocation(board_id: str, pending: object) -> None:
    if not isinstance(pending, _PendingGenericAllocation):
        raise TypeError("invalid generic deployment allocation")
    _safety_repository.commit_if_current(
        board_id, pending.expected_map_digest, pending.document
    )
    connection = connection_manager.maybe_connection(board_id)
    if connection is not None:
        gate_manager.refresh_map_stamp(
            board_id, connection.connection_id, pending.document.canonical_digest
        )


def _clear_generic_allocation(board_id: str) -> None:
    _pending_generic_allocations.pop(board_id, None)


flash_services = FlashToolServices(
    runtime_for=_runtime_for,
    active_session_id=_active_session_id,
    duration_ms=_duration_ms,
    record_event=_record_event,
    record_blocked_event=_record_blocked_event,
    format_refusal=_format_refusal,
    format_block=_format_block,
    ensure_flash_allowed=lambda runtime: _watcher.ensure_allowed(runtime, FLASH_TOOL),
    action_context=_action_context,
    maybe_handle_for=_maybe_handle,
    handle_for=_handle,
    resolve_request=lambda handle, artifact, context: resolve_flash_request(
        handle,
        explicit_path=artifact,
        action_context=context,
    ),
    flash_target=lambda handle, artifact: target_control.flash_firmware(
        handle,
        artifact,
        halt_after_reset=False,
    ),
    handle_mutation_event=lambda selected_board, event: _handle_mutation_event(
        selected_board, cast(ToolEvent, event)
    ),
    error_code=_error_code,
    validate_flash=_check_flash_safety,
    prepare_symbol_artifact=_prepare_flashed_symbol_artifact,
    bind_symbol_artifact=_bind_flashed_symbol_artifact,
    prepare_generic_allocation=_prepare_generic_allocation,
    commit_generic_allocation=_commit_generic_allocation,
    clear_generic_allocation=_clear_generic_allocation,
)
flash_tool_handlers = build_flash_handlers(flash_services)

serial_services = SerialToolServices(
    runtime_for=_runtime_for,
    active_session_id=_active_session_id,
    duration_ms=_duration_ms,
    record_event=_record_event,
    record_blocked_event=_record_blocked_event,
    format_refusal=_format_refusal,
    format_block=_format_block,
    ensure_uart_allowed=lambda runtime: _watcher.ensure_allowed(runtime, UART_TOOL),
    handle_for=_handle,
    resolve_port=_resolve_serial_port_for_session,
    capture_uart=lambda *args, **kwargs: capture_uart_output(*args, **kwargs),
    write_uart=lambda *args, **kwargs: write_uart_output(*args, **kwargs),
    exchange_uart=lambda *args, **kwargs: exchange_uart_output(*args, **kwargs),
    reset_target=lambda handle: target_control.reset(handle, halt_after=False),
    handle_mutation_event=lambda selected_board, event: _handle_mutation_event(
        selected_board, cast(ToolEvent, event)
    ),
    no_board_config_message=NO_BOARD_CONFIG_MESSAGE,
)
serial_tool_handlers = build_serial_handlers(serial_services)

breakpoint_services = BreakpointToolServices(
    runtime_for=_runtime_for,
    active_session_id=_active_session_id,
    duration_ms=_duration_ms,
    record_event=_record_event,
    format_refusal=_format_refusal,
    handle_for=_handle,
    resolve_symbol=resolve_symbol,
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

for _legacy_name in (
    "connect",
    "disconnect",
    "get_board_info",
    "get_state",
    "halt",
    "resume",
    "step",
    "reset",
    "read_core_register",
    "write_core_register",
    "read_memory",
    "read_memory_block",
    "read_symbol_u32",
    "write_memory",
    "set_breakpoint",
    "remove_breakpoint",
    "flash_firmware",
    "read_serial",
    "write_serial",
):
    mcp.remove_tool(_legacy_name)

for _handler_name, _handler in (
    session_tool_handlers
    | execution_tool_handlers
    | register_tool_handlers
    | memory_tool_handlers
    | flash_tool_handlers
    | serial_tool_handlers
    | breakpoint_tool_handlers
    | misc_tool_handlers
    | artifact_tool_handlers
).items():
    mcp.add_tool(
        _handler,
        name=_handler_name,
        description=_handler.__doc__,
        structured_output=False,
    )

# FastMCP ignores unknown fields by default. Normal connect must fail closed rather than silently
# dropping manual override fields, including when it is dispatched as an action_batch child.
forbid_unknown_tool_arguments(mcp, "connect")
forbid_unknown_tool_arguments(mcp, "collect_build_artifacts")

M5_LAYER2_ACTIONS = tuple(
    session_tool_handlers
    | execution_tool_handlers
    | register_tool_handlers
    | memory_tool_handlers
    | flash_tool_handlers
    | serial_tool_handlers
    | breakpoint_tool_handlers
    | misc_tool_handlers
)
for _layer2_action in M5_LAYER2_ACTIONS:
    mcp.configure_layer2(_layer2_action)

PILOT_PLAN_ACTIONS = ("read_serial", "write_serial", "write_memory")
TASK7_GUARDED_ACTIONS = (
    "connect_override",
    "reset_and_halt",
    "connect_under_reset",
    "write_cpu_register",
    "set_execution_state",
    "register_write",
)
TASK8_GUARDED_ACTIONS = (
    "read_memory_address",
    "write_memory",
    "set_breakpoint",
    "flash_application",
    "flash_bootloader",
    "read_serial",
    "write_serial",
    "serial_exchange",
)
M5_GUARDED_ACTIONS = TASK7_GUARDED_ACTIONS + TASK8_GUARDED_ACTIONS
for _guarded_action in M5_GUARDED_ACTIONS:
    tool_registry.configure(
        _guarded_action,
        hidden=True,
        locked=True,
        prerequisite=f"{_guarded_action}-plan",
    )
    mcp.configure_guarded_dispatch(
        _guarded_action,
        guard=_enforce_guarded_invocation,
        lock_for_board=lambda board_id: connection_manager.lock_for(board_id),
    )

plan_tool_handlers = register_plan_tools(
    mcp,
    plan_engine,
    (PLAN_DEFINITIONS[action] for action in M5_GUARDED_ACTIONS),
    _active_session_id,
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
        for probe in list_connected_probes(_run_cmd)
    }
    # pyOCD inventory intentionally omits probes already opened by this process.
    # Validation must still be able to select and stamp the server-owned active
    # connection, so merge those stable identities without reopening a probe.
    for board_id in connection_manager.assigned_board_ids():
        connection = connection_manager.connection_for(board_id)
        handle = connection.handle
        probe_uid = (handle.probe_uid or "").strip()
        if not probe_uid or probe_uid in probes_by_id:
            continue
        board = handle.board
        probe = getattr(handle.session, "probe", None)
        description = str(getattr(probe, "description", "") or "").strip()
        if not description:
            description = board.display_name if board is not None else f"Active probe {probe_uid}"
        probe_family = probe_family_from_pyocd_probe(probe)
        probes_by_id[probe_uid] = ValidationProbe(
            probe_uid,
            description,
            probe_family,
            probe_uid,
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
        return verified_pack_for_target(normalized) is not None
    except PackProvisionError:
        # A declaration or stale process-global registration is not authority.
        # Missing, changed, or ambiguous pinned bytes must fail closed.
        return False


class _ValidationConnection:
    __slots__ = ("handle", "owned", "board_id", "promoted")

    def __init__(
        self,
        handle: TargetSessionHandle,
        owned: bool,
        *,
        board_id: str | None = None,
        promoted: bool = False,
    ) -> None:
        self.handle = handle
        self.owned = owned
        self.board_id = board_id
        self.promoted = promoted


def _verified_pack_for_profile(profile):
    """Return the exact replayed pack for a generic profile, or ``None`` for legacy support."""

    device_support = getattr(profile, "device_support", None)
    if device_support is None:
        return None
    part_number = getattr(profile, "mcu_part_number", None)
    if part_number is None:
        raise PackProvisionError("generic profile has no exact MCU part number")
    candidate = resolve_persisted_pack_support(
        _profile_repository.store, part_number, device_support
    )
    if candidate.to_authority_document() != dict(device_support):
        raise PackProvisionError("generic profile no longer matches its exact pack binding")
    return verified_pack_for_candidate(candidate, _profile_repository.store)


def _validation_connect(profile, probe: ValidationProbe, timeout: float) -> object:
    del timeout
    existing = connection_manager.maybe_connection(profile.board_id)
    if existing is not None:
        return _ValidationConnection(existing.handle, False, board_id=profile.board_id)
    saved_policy = (
        profile.board.debug_connect_mode or "attach",
        profile.board.debug_clock_hz,
    )
    attempts = [("attach", None)]
    if saved_policy not in attempts:
        attempts.append(saved_policy)
    last_error: TargetConnectionError | None = None
    handle: TargetSessionHandle | None = None
    selected_pack = _verified_pack_for_profile(profile)
    for mode, frequency in attempts:
        try:
            handle = target_control.open_session(
                board=profile.board,
                unique_id=probe.usb_serial,
                target=profile.board.pyocd_target,
                server_timeouts=_staged_server_timeouts,
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
        except TargetConnectionError as exc:
            last_error = exc
            continue
        break
    if handle is None:
        assert last_error is not None
        raise last_error
    connection_id = stable_connection_identity(handle)
    runtime = _session_store.start_session(
        board_id=profile.board_id,
        connection_id=connection_id,
        probe_uid=handle.probe_uid,
        route_used=handle.route_used,
    )
    try:
        connection_manager.assign(
            profile.board_id,
            handle,
            runtime,
            connection_id=connection_id,
        )
        gate_manager.clear(profile.board_id, "validation connection not yet stamped")
    except Exception:
        _session_store.close_session(runtime)
        target_control.close_session(handle)
        raise
    return _ValidationConnection(
        handle,
        False,
        board_id=profile.board_id,
        promoted=True,
    )


def _validation_read(connection: object, address: int, width: int, timeout: float) -> int:
    del timeout
    return target_control.read_memory(
        cast(_ValidationConnection, connection).handle, address, width
    )


def _validation_close(connection: object) -> None:
    validation = cast(_ValidationConnection, connection)
    if validation.owned:
        target_control.close_session(validation.handle)
        return
    if validation.promoted and validation.board_id is not None:
        # Keep only a connection whose successful validation stamped this exact
        # assignment.  A failed bootstrap validation releases the probe and
        # session immediately so repair can retry without host intervention.
        assigned = connection_manager.maybe_connection(validation.board_id)
        stamp = gate_manager.snapshot(validation.board_id)
        mismatch = None
        if assigned is not None:
            probe_identity = assigned.handle.probe_uid or assigned.connection_id
            mismatch = gate_manager.current_mismatch(
                validation.board_id,
                assigned.connection_id,
                probe_identity,
            )
        if assigned is not None and (
            stamp is None or stamp.connection_id != assigned.connection_id
        ) and mismatch is None:
            connection_manager.clear(validation.board_id)
            _session_store.close_session(assigned.runtime_session)
            target_control.close_session(assigned.handle)


def _validation_capture(
    serial: ValidationSerial,
    baudrate: int,
    duration: float,
    max_bytes: int,
) -> str:
    return capture_uart_output(
        serial.port_path,
        baudrate,
        duration,
        None,
        reopen_attempts=0,
        max_bytes=max_bytes,
    ).text


_checkout_root = Path(__file__).resolve().parents[2]
_artifact_root_value = os.environ.get("BYO_MCP_ARTIFACT_ROOT", "").strip()
_project_root = (
    Path(_artifact_root_value).expanduser().resolve() if _artifact_root_value else _checkout_root
)
_firm_store = FirmStore(_project_root)
_profile_repository = ProfileRepository(_firm_store, legacy_board_dir=_checkout_root / "boards")
_attachment_cache = AttachmentCache(_firm_store)
_report_writer = ReportWriter(_firm_store)
_safety_repository = SafetyMapRepository(_firm_store)
_safety_builder = SafetyMapBuilder(_safety_repository)


def _safety_continuation(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(8)}"


def _derive_reviewed_safety_map(board_id: str):
    """Reproduce one complete candidate from profile plus packaged reviewed evidence."""

    profile = _profile_repository.load(board_id, include_legacy=False)
    profile_document = profile.to_document()
    datasheet_digest = profile_document.get("datasheet_sha256")
    if not isinstance(datasheet_digest, str) or not datasheet_digest.strip():
        raise SafetyMapError("the profile has no reviewed datasheet SHA-256 anchor")
    try:
        catalog = resolve_reviewed_support(
            profile.mcu_part_number or "",
            datasheet_digest,
        )
    except BoardCatalogError as exc:
        raise SafetyMapError(
            "the profile lacks one unambiguous reviewed MCU/datasheet authority"
        ) from exc
    bundle = load_pinned_reviewed_evidence(catalog, datasheet_digest)
    erase = bundle.reconciliation.erase_geometry
    if erase is None:
        raise SafetyMapError("reviewed evidence has no reconciled erase geometry")
    device_support, reviewed_official = reviewed_map_source_documents(catalog, bundle)
    contributions = tuple(
        RegionContribution(
            region.to_safety_region(),
            (
                RegionSource.REVIEWED_DEVICE_SUPPORT,
                RegionSource.REVIEWED_OFFICIAL_EVIDENCE,
                RegionSource.GEOMETRY,
            ),
        )
        for region in bundle.reconciliation.regions
    )
    return _safety_builder.derive(
        SafetyMapBuildRequest(
            board_id,
            MapIdentity(
                profile.mcu_part_number or "",
                profile.board.pyocd_target,
                catalog.board_type,
            ),
            profile_document,
            device_support,
            reviewed_official,
            MapGeometry(
                AddressRange(catalog.flash_start, catalog.flash_end),
                AddressRange(catalog.ram_start, catalog.ram_end),
                erase_origin=erase.erase_origin,
                erase_size=erase.erase_size,
            ),
            MapPartitions(catalog.application_partition, catalog.bootloader_partition),
            contributions,
        )
    )


def _derive_generic_safety_map(board_id: str) -> GenericSafetyMapDocument:
    """Build a read/debug-only schema-v3 map from a replayed pack binding."""

    profile = _profile_repository.load(board_id, include_legacy=False)
    if profile.mcu_part_number is None or profile.device_support is None:
        raise SafetyMapError("profile lacks a resolved generic device-support source")
    candidate = resolve_persisted_pack_support(
        _profile_repository.store, profile.mcu_part_number, profile.device_support
    )
    if profile.device_support != candidate.to_authority_document():
        raise SafetyMapError("profile generic device-support source no longer matches the registry")
    if profile.board.pyocd_target.casefold() != candidate.pyocd_target.casefold():
        raise SafetyMapError("profile target no longer matches the generic device-support source")
    geometry = resolve_registered_pack_geometry(candidate, _profile_repository.store)
    profile_document = profile.to_document()
    datasheet_digest = profile_document.get("datasheet_sha256")
    if not isinstance(datasheet_digest, str) or not datasheet_digest:
        raise SafetyMapError("profile has no server-computed local datasheet digest")
    datasheet_ref = profile_document.get("datasheet_ref")
    if not isinstance(datasheet_ref, str) or not datasheet_ref:
        raise SafetyMapError("generic profile has no captured datasheet evidence reference")
    try:
        datasheet_evidence = replay_datasheet_evidence(
            _profile_repository.store, datasheet_ref, datasheet_digest
        )
    except ValueError as exc:
        raise SafetyMapError(f"generic datasheet evidence is not current: {exc}") from exc
    flash_regions = geometry.flash_regions or (
        PackAddressRegion("application flash", geometry.flash_start, geometry.flash_end, "rx"),
    )
    ram_regions = geometry.ram_regions or (
        PackAddressRegion("default RAM", geometry.ram_start, geometry.ram_end, "rwx"),
    )
    map_geometry = GenericMapGeometry(
        tuple(AddressRange(item.start, item.end) for item in flash_regions),
        tuple(AddressRange(item.start, item.end) for item in ram_regions),
        erase_sectors=tuple(
            EraseSector(AddressRange(start, end), "internal")
            for start, end in geometry.erase_sectors
        )
        if geometry.driver_proof_digest is not None
        else (),
        erase_available=bool(geometry.erase_sectors and geometry.driver_proof_digest),
    )
    source = candidate.to_authority_document()
    provenance = (
        Provenance(
            SourceAuthority.DEVICE_SUPPORT,
            f"pack:{candidate.pack_sha256}",
            f"verified PDSC leaf {candidate.pdsc_device}",
        ),
    )
    deployment_policy: Mapping[str, object] = {"kind": "none"}
    partitions = MapPartitions(None)
    deployment_regions: tuple[SafetyRegion, ...] = ()
    map_path = _safety_repository.path(board_id)
    try:
        prior = _safety_repository.load_current(board_id)
    except SafetyMapError as exc:
        if map_path.is_file():
            raise SafetyMapError(
                "existing generic map is unreadable; refusing to discard possible one-way "
                "deployment ownership"
            ) from exc
        prior = None
    if (
        isinstance(prior, GenericSafetyMapDocument)
        and prior.deployment_policy.get("kind") == "artifact_application_allocation"
    ):
        if prior.identity.support_id != candidate.support_id:
            raise SafetyMapError(
                "existing generic deployment ownership is bound to different device support"
            )
        if (
            geometry.driver_proof_digest is None
            or prior.deployment_policy.get("driver_proof_digest")
            != geometry.driver_proof_digest
        ):
            raise SafetyMapError(
                "existing generic deployment ownership no longer matches the bounded flash driver"
            )
        # Construction revalidates allocation digests and exact-sector containment
        # against the newly derived geometry; refresh may preserve but never widen it.
        deployment_policy = dict(prior.deployment_policy)
        partitions = prior.partitions
        assert partitions.application is not None
        deployment_regions = (
            SafetyRegion(
                "artifact-defined application allocation",
                RegionKind.APPLICATION_FLASH,
                partitions.application,
                (
                    Provenance(
                        SourceAuthority.DERIVED,
                        str(deployment_policy["allocation_digest"]),
                        "server-derived artifact erase-sector allocation",
                    ),
                ),
            ),
        )
    regions = (
        *(
            SafetyRegion(
                f"pack flash: {item.name}",
                RegionKind.PHYSICAL_FLASH,
                AddressRange(item.start, item.end),
                provenance,
            )
            for item in flash_regions
        ),
        *(
            SafetyRegion(
                f"pack RAM: {item.name}",
                RegionKind.PHYSICAL_RAM,
                AddressRange(item.start, item.end),
                provenance,
            )
            for item in ram_regions
        ),
        *(
            SafetyRegion(
                f"writable pack RAM: {item.name}",
                RegionKind.RAM,
                AddressRange(item.start, item.end),
                provenance,
            )
            for item in ram_regions
        ),
        *(
            SafetyRegion(
                f"pack ROM: {item.name}",
                RegionKind.ROM,
                AddressRange(item.start, item.end),
                provenance,
                executable=True,
            )
            for item in geometry.rom_regions
        ),
        *(
            SafetyRegion(
                f"pack SVD peripheral: {item.name}",
                (
                    RegionKind.PERIPHERAL
                    if item.readable and item.writable
                    else RegionKind.PERIPHERAL_READ_ONLY
                    if item.readable
                    else RegionKind.PERIPHERAL_WRITE_ONLY
                ),
                AddressRange(item.start, item.end),
                provenance,
            )
            for item in geometry.peripheral_regions
        ),
        *(
            SafetyRegion(
                item.name,
                RegionKind.CPU_SYSTEM,
                AddressRange(item.start, item.end),
                provenance,
            )
            for item in geometry.cpu_system_regions
        ),
        *deployment_regions,
    )
    return GenericSafetyMapDocument(
        board_id,
        GenericMapIdentity(profile.mcu_part_number, candidate.pyocd_target, candidate.support_id),
        {
            "kind": "resolved_pack",
            "support_id": candidate.support_id,
            "pack_id": candidate.pack_id,
            "pack_filename": candidate.pack_filename,
            "pack_sha256": candidate.pack_sha256,
            "pdsc_device": candidate.pdsc_device,
            "pyocd_target": candidate.pyocd_target,
        },
        GenericSourceDigests.build(
            profile=profile_document,
            device_support=source,
            datasheet_evidence=datasheet_evidence.source_document(),
            deployment_policy=deployment_policy,
        ),
        map_geometry,
        partitions,
        deployment_policy,
        regions,
    )


def _require_map_authority(document) -> None:
    """Replay one map's exact reviewed or active-project device authority."""

    if isinstance(document, GenericSafetyMapDocument):
        require_reconciled_authority(
            document,
            generic_support_resolver=lambda part: resolve_persisted_pack_support(
                _profile_repository.store, part, document.authority_source
            ),
        )
    else:
        require_reconciled_authority(document)


def _require_current_reviewed_map(document) -> None:
    """Reject a valid but stale map without consulting any build output."""

    _require_map_authority(document)
    candidate = (
        _derive_generic_safety_map(document.board_id)
        if isinstance(document, GenericSafetyMapDocument)
        else _derive_reviewed_safety_map(document.board_id)
    )
    if document != candidate:
        raise SafetyMapError(
            "the stable map no longer matches the semantic profile or current reviewed evidence; "
            "run board_safety_refresh"
        )


def _derive_safety_map(board_id: str):
    """Re-derive the sole map using the profile's recorded authority kind."""

    profile = _profile_repository.load(board_id, include_legacy=False)
    return (
        _derive_generic_safety_map(board_id)
        if profile.device_support is not None
        else _derive_reviewed_safety_map(board_id)
    )


_safety_policy = SafetyPolicy(
    _safety_repository,
    authority_verifier=_require_current_reviewed_map,
)


def _restamp_after_refresh(board_id: str, map_digest: str, identity_changed: bool) -> None:
    expected_ref = (
        _firm_store.layout.safety_reference_prefix(board_id) / "memory_map.yaml"
    ).as_posix()
    profile = _profile_repository.load(board_id, include_legacy=False)
    if profile.safety_ref != expected_ref:
        _profile_repository.commit_safety_ref(
            _profile_repository.stage_safety_ref(board_id, expected_ref)
        )
    if identity_changed:
        gate_manager.clear(board_id, "stable map identity changed during safety refresh")
        return
    connection = connection_manager.maybe_connection(board_id)
    if connection is not None:
        gate_manager.refresh_map_stamp(board_id, connection.connection_id, map_digest)


def _has_current_live_identity(board_id: str) -> bool:
    connection = connection_manager.maybe_connection(board_id)
    identity = gate_manager.live_identity(board_id)
    return bool(
        connection is not None
        and identity is not None
        and identity.connection_id == connection.connection_id
    )


_safety_refresher = SafetyRefresher(
    _firm_store,
    derive=_derive_safety_map,
    has_live_identity=_has_current_live_identity,
    on_commit=_restamp_after_refresh,
)


def _run_board_safety_refresh(board_id: str) -> Mapping[str, object]:
    """Public v2 safety maintenance: deterministic rebuild with no artifact inputs."""

    return _safety_refresher.refresh(
        SafetyRefreshRequest(board_id, _safety_continuation("safety-refresh"))
    ).to_payload()


_REQUIRED_BASE_SAFETY_KINDS = frozenset(
    {
        RegionKind.PROHIBITED,
        RegionKind.PERIPHERAL,
        RegionKind.CPU_SYSTEM,
        RegionKind.PHYSICAL_FLASH,
        RegionKind.PHYSICAL_RAM,
        RegionKind.RAM,
    }
)


def _missing_base_safety_kinds(
    regions: tuple[SafetyRegion, ...], *, generic: bool = False
) -> tuple[str, ...]:
    present = {item.kind for item in regions}
    required = (
        {RegionKind.PHYSICAL_FLASH, RegionKind.PHYSICAL_RAM, RegionKind.RAM}
        if generic
        else _REQUIRED_BASE_SAFETY_KINDS
    )
    return tuple(sorted(kind.value for kind in required - present))


def _load_validation_safety_map(profile) -> SafetyMapSnapshot:
    try:
        document = _safety_repository.load_current(profile.board_id)
        _require_map_authority(document)
        missing_kinds = _missing_base_safety_kinds(
            document.regions, generic=isinstance(document, GenericSafetyMapDocument)
        )
        if missing_kinds:
            return SafetyMapSnapshot(
                True,
                False,
                reason=(
                    "The safety map lacks required base classifications "
                    f"{', '.join(missing_kinds)}. Run board_safety_refresh."
                ),
            )
        if (
            document.identity.mcu_part_number != profile.mcu_part_number
            or document.identity.pyocd_target != profile.board.pyocd_target
        ):
            return SafetyMapSnapshot(
                True,
                False,
                document.canonical_digest,
                "The safety-map identity does not match the profile. Run board_safety_refresh.",
            )
        map_digest = _safety_policy.current_aggregate(profile.board_id)
    except (SafetyMapError, SafetyPolicyError, ValueError) as exc:
        return SafetyMapSnapshot(
            False,
            False,
            reason=f"Safety map is missing or inconsistent; run board_safety_refresh: {exc}",
        )
    return SafetyMapSnapshot(True, True, map_digest, "Profile and current safety map agree.")


def _stamp_validation_session(
    board_id: str,
    validation_run: str,
    probe_id: str,
    probe_uid: str | None,
    observed_mcu: str,
    map_digest: str,
) -> bool:
    connection = connection_manager.maybe_connection(board_id)
    stable_probe = (probe_uid or probe_id).strip()
    if connection is None:
        return False
    if (
        connection.handle.probe_uid
        and connection.handle.probe_uid.casefold() != stable_probe.casefold()
    ):
        return False
    provisional_connection_id = f"probe:{probe_uid or probe_id}"
    try:
        profile = _profile_repository.load(board_id, include_legacy=False)
        capability = "exact"
        if profile.device_support is not None and profile.mcu_part_number is not None:
            proof = resolve_persisted_pack_support(
                _profile_repository.store,
                profile.mcu_part_number,
                profile.device_support,
            ).identity_proof
            if proof is None:
                return False
            capability = proof.capability
    except (ProfileError, PackProvisionError):
        return False
    try:
        assignment_store.run_if_current(
            provisional_connection_id,
            board_id,
            lambda: run_if_not_cancelled(
                lambda: gate_manager.stamp_validation(
                    board_id=board_id,
                    connection_id=connection.connection_id,
                    probe_identity=stable_probe,
                    observed_mcu=observed_mcu,
                    identity_capability=capability,
                    validation_run=validation_run,
                    map_digest=map_digest,
                )
            ),
        )
    except SetupWorkflowError:
        return False
    return True


def _record_validation_mismatch(
    board_id: str,
    validation_run: str,
    probe_id: str,
    probe_uid: str | None,
    expected_mcu: str,
    observed_mcu: str,
) -> bool:
    def commit_mismatch() -> None:
        assignment_store.clear_board(board_id)
        connection = connection_manager.maybe_connection(board_id)
        stable_probe = (probe_uid or probe_id).strip()
        connection_id = (
            connection.connection_id
            if connection is not None
            else f"probe:{stable_probe.casefold()}"
        )
        gate_manager.record_mismatch(
            board_id=board_id,
            connection_id=connection_id,
            probe_identity=stable_probe,
            expected_mcu=expected_mcu,
            observed_mcu=observed_mcu,
            validation_run=validation_run,
        )

    provisional_connection_id = f"probe:{probe_uid or probe_id}"
    try:
        assignment_store.run_if_current(
            provisional_connection_id,
            board_id,
            lambda: run_if_not_cancelled(commit_mismatch),
        )
    except SetupWorkflowError:
        return False
    return True


def _rollback_validation_session(board_id: str, validation_run: str) -> None:
    """Remove only the failed validation's newly created live authority and connection."""

    with connection_manager.lock_for(board_id):
        if not gate_manager.rollback_validation(board_id, validation_run):
            return
        connection = connection_manager.maybe_connection(board_id)
        if connection is None:
            return
        connection_manager.clear(board_id)
        try:
            target_control.close_session(connection.handle)
        finally:
            _session_store.close_session(connection.runtime_session)


_board_validator = BoardValidator(
    _profile_repository,
    _report_writer,
    ValidationBackend(
        _validation_inventory,
        _validation_target_supported,
        _validation_connect,
        _validation_read,
        _validation_capture,
        _validation_close,
    ),
    hooks=ValidationHooks(
        _load_validation_safety_map,
        _stamp_validation_session,
        _record_validation_mismatch,
        _rollback_validation_session,
    ),
    cancellation_checkpoint=cancellation_checkpoint,
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


def _connection_matches_probe(connection_id: str, probe: ProbeCandidate) -> bool:
    candidate = connection_id.strip()
    if candidate.casefold().startswith("probe:"):
        candidate = candidate.split(":", 1)[1]
    return _stable_identity_equal(candidate, probe.probe_id) or _stable_identity_equal(
        candidate, probe.usb_serial
    )


_setup_research = ResearchTracker()
_setup_target_overrides: dict[str, str] = {}
_setup_attachment_overrides: dict[str, tuple[str, int | None]] = {}
_setup_selections_by_board: dict[str, PreflightSelections] = {}
_setup_pack_pipelines: dict[tuple[str, str], PackCandidatePipeline] = {}


class _ResolvedGenericSetupSupport:
    """Ephemeral generic setup result; intentionally has no persisted authority state."""

    __slots__ = ("candidate", "datasheet_path", "datasheet_sha256")

    def __init__(
        self,
        candidate: DeviceSupportCandidate,
        datasheet_path: Path,
        datasheet_sha256: str,
    ) -> None:
        self.candidate = candidate
        self.datasheet_path = datasheet_path
        self.datasheet_sha256 = datasheet_sha256


def _is_generic_support(value: object) -> bool:
    return isinstance(value, _ResolvedGenericSetupSupport)


def _resolve_setup_support(user_input: SetupUserInput):
    """Resolve generic registered support before legacy catalog compatibility."""

    path, digest = hash_local_datasheet(Path(user_input.datasheet_path))
    try:
        candidate = resolve_available_pack_support(
            _profile_repository.store, user_input.mcu_part_number
        )
    except PackProvisionError:
        if has_available_pack_binding(_profile_repository.store, user_input.mcu_part_number):
            # A registered generic binding whose bytes, PDSC leaf, or target
            # proof no longer verify is an authority failure, not a reason to
            # inherit a reference-board catalog policy.
            raise
        # Catalog authority is compatibility-only. A fresh logical board must
        # never inherit a development-board partition, wiring, or attach policy
        # merely because its MCU and datasheet happen to match. Existing
        # catalog-backed profiles can still be repaired through their original
        # authority path.
        try:
            existing = _profile_repository.load(user_input.board_id, include_legacy=True)
        except ProfileError:
            existing = None
        if existing is None or existing.device_support is not None:
            raise ReviewedSupportNotFoundError(
                "Fresh setup requires an exact verified generic device-support binding"
            )
        return resolve_reviewed_support_from_datasheet(user_input.mcu_part_number, path)
    return _ResolvedGenericSetupSupport(candidate, path, digest)


def _setup_inventory(user_input: SetupUserInput) -> PreflightInventory:
    support: object | None
    try:
        support = _resolve_setup_support(user_input)
    except (ReviewedSupportNotFoundError, ReviewedSupportAmbiguityError):
        # A valid PDF with no pre-existing support is the normal generic
        # onboarding path. Preflight will issue bounded agent research after
        # physical probe/UART choices are settled.
        support = None
    except PackProvisionError as exc:
        return PreflightInventory(
            blocking_error=PreflightBlock(
                "setup/device-support-invalid",
                f"The previously pinned exact device-support record failed replay: {exc}",
            )
        )
    except (BoardCatalogError, OSError, ValueError) as exc:
        return PreflightInventory(
            blocking_error=PreflightBlock(
                "setup/datasheet-evidence-invalid",
                f"The local official datasheet could not be accepted: {exc}",
            )
        )
    generic = _is_generic_support(support)
    generic_support = cast(_ResolvedGenericSetupSupport, support) if generic else None
    catalog = None if generic or support is None else cast(Any, support).catalog
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
    # connection_id is an explicit immutable setup-plan identity. Zero matches
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
    manifest_specs = (*load_manifest(), *load_manifest(_firm_store.layout.pack_manifest))
    manifest_targets = tuple(
        sorted({target.casefold() for pack in manifest_specs for target in pack.provides_targets})
    )
    exact: tuple[str, ...] = ()
    # A fresh profile has no board YAML yet, but a complete reviewed catalog entry is itself
    # authoritative for the exact pyOCD target. Package suffixes such as ``-QIAA`` must not
    # force an unnecessary agent research round trip when the exact built-in target is present.
    if generic_support is not None:
        reviewed_target = generic_support.candidate.pyocd_target.casefold()
        pack_backed = False
        support_present = True
    elif catalog is not None:
        reviewed_target = catalog.pyocd_target.casefold()
        pack_backed = bool(catalog.pyocd_pack_filename and catalog.pyocd_pack_sha256)
        support_present = reviewed_target in targets if not pack_backed else False
    else:
        reviewed_target = ""
        pack_backed = False
        support_present = False
    if catalog is not None and pack_backed:
        try:
            selected_pack = verified_pack_for_target(reviewed_target)
            support_present = selected_pack is not None and pack_matches_reviewed_catalog(
                selected_pack, cast(Any, catalog)
            )
        except PackProvisionError as exc:
            return PreflightInventory(
                probes=probes,
                serial_ports=serial,
                cache_resolution=cache_resolution,
                built_in_targets=tuple(
                    target for target in targets if target not in manifest_targets
                ),
                manifest_targets=manifest_targets,
                blocking_error=PreflightBlock(
                    "setup/reviewed-pack-unavailable",
                    "The reviewed local device-support pack is missing, conflicting, or failed "
                    f"integrity verification: {exc}",
                ),
            )
        if not support_present:
            return PreflightInventory(
                probes=probes,
                serial_ports=serial,
                cache_resolution=cache_resolution,
                built_in_targets=tuple(
                    target for target in targets if target not in manifest_targets
                ),
                manifest_targets=manifest_targets,
                blocking_error=PreflightBlock(
                    "setup/reviewed-pack-unavailable",
                    "The reviewed target has no provider in the local pinned pack manifests. "
                    "Run the documented host pack provisioning step; do not research or download "
                    "a replacement during board setup.",
                ),
            )
    if generic_support is not None and support_present:
        exact = (reviewed_target,)
    elif catalog is not None and user_input.mcu_part_number == catalog.package_part_number and support_present:
        exact = (reviewed_target,)
    target_override = _setup_target_overrides.get(user_input.board_id)
    if target_override is not None and support_present:
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


def _setup_connection_phase(context: SetupPhaseContext) -> SetupPhaseOutcome:
    try:
        support = _resolve_setup_support(context.user_input)
        generic = _is_generic_support(support)
        generic_support = cast(_ResolvedGenericSetupSupport, support) if generic else None
        catalog = None if generic else cast(Any, support).catalog
        legacy_catalog = cast(Any, catalog)
        actual_datasheet_hash = support.datasheet_sha256
        generic_geometry = (
            resolve_registered_pack_geometry(
                generic_support.candidate, _profile_repository.store
            )
            if generic_support is not None
            else None
        )
        generic_pack = (
            verified_pack_for_candidate(
                generic_support.candidate, _profile_repository.store
            )
            if generic_support is not None
            else None
        )
    except (BoardCatalogError, PackProvisionError, OSError, ValueError) as exc:
        return SetupPhaseOutcome.stop(
            "setup_blocked",
            getattr(exc, "code", "setup/catalog-evidence-mismatch"),
            f"The exact MCU and server-hashed datasheet did not match verified device support: {exc}",
        )
    try:
        existing = _profile_repository.load(context.user_input.board_id, include_legacy=True)
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
    expected_target = (
        generic_support.candidate.pyocd_target
        if generic_support is not None
        else legacy_catalog.pyocd_target
    )
    if target.casefold() != expected_target.casefold() or probe.probe_family == "unknown":
        if probe.probe_family == "unknown":
            remedy = (
                "The debug probe provider could not be identified. Choose a recognized pyOCD "
                "probe provider before retrying setup."
            )
        else:
            remedy = (
                "The selected target does not match the resolved MCU/device support. "
                "Use the server-derived target before retrying setup."
            )
        return SetupPhaseOutcome.stop(
            "setup_connection_failed",
            "setup/catalog-route-mismatch",
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
        or existing.board.pyocd_target.casefold() != target.casefold()
    ):
        return SetupPhaseOutcome.stop(
            "setup_connection_failed",
            "setup/existing-profile-identity-mismatch",
            "Repair inputs do not match the established profile identity. Stop rather than "
            "rewriting the profile; restart from setup_overview with the recorded board name.",
            details={
                "expected_display_name": existing.display_name,
                "expected_mcu_part_number": existing.mcu_part_number,
                "expected_target": existing.board.pyocd_target,
            },
        )

    opened: list[TargetSessionHandle] = []
    captured_datasheet_ref: str | None = None
    identity_proof = generic_support.candidate.identity_proof if generic_support is not None else None
    setup_board = BoardConfig(
        board_id=context.user_input.board_id,
        display_name=context.user_input.display_name,
        mcu_family=_mcu_family(context.user_input.mcu_part_number, target),
        probe_family=probe.probe_family,
        pyocd_target=target,
        probe_type=probe.description,
        probe_hint_terms=(),
        serial_hint_terms=(),
        test_addr=(generic_geometry.flash_start if generic_geometry is not None else legacy_catalog.test_read_address),
        silicon_id_addr=(
            identity_proof.address
            if identity_proof is not None
            else (None if generic else legacy_catalog.silicon_id_address)
        ),
        silicon_id_expected=(
            identity_proof.expected
            if identity_proof is not None
            else (None if generic else legacy_catalog.silicon_id_expected)
        ),
        silicon_id_mask=(
            identity_proof.mask
            if identity_proof is not None
            else (None if generic else legacy_catalog.silicon_id_mask)
        ),
        silicon_id_width_bits=(
            identity_proof.width_bits
            if identity_proof is not None
            else (32 if generic else legacy_catalog.silicon_id_width_bits)
        ),
        silicon_id_label=(
            identity_proof.label
            if identity_proof is not None
            else ("" if generic else legacy_catalog.silicon_id_label or "")
        ),
        default_baudrate=(115200 if generic else legacy_catalog.default_baudrate),
        debug_connect_mode=(None if generic else legacy_catalog.debug_connect_mode),
        debug_clock_hz=(None if generic else legacy_catalog.debug_clock_hz),
    )

    def connect(candidate_target: str, _pack_path: str | None) -> None:
        nonlocal captured_datasheet_ref
        if generic:
            selected_policy = _setup_attachment_overrides.get(context.user_input.board_id)
            attempts = (
                (selected_policy,)
                if selected_policy is not None
                else (("attach", None), ("attach", 1_000_000), ("under-reset", 1_000_000))
            )
        else:
            attempts = ((legacy_catalog.debug_connect_mode, legacy_catalog.debug_clock_hz),)
        handle: TargetSessionHandle | None = None
        last_error: TargetConnectionError | None = None
        for mode, frequency in attempts:
            try:
                handle = target_control.open_session(
                    board=setup_board,
                    unique_id=probe.usb_serial,
                    target=candidate_target,
                    server_timeouts=_staged_server_timeouts,
                    connect_mode=mode,
                    frequency_hz=frequency,
                    pack_path=(generic_pack.path if generic_pack is not None else None),
                    pack_sha256=(
                        generic_pack.spec.sha256 if generic_pack is not None else None
                    ),
                    pdsc_device=(
                        cast(_ResolvedGenericSetupSupport, support).candidate.pdsc_device
                        if generic
                        else None
                    ),
                )
            except TargetConnectionError as exc:
                last_error = exc
                continue
            if generic:
                _setup_attachment_overrides[context.user_input.board_id] = (
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
            if not _stable_identity_equal(selected_probe_uid, handle.probe_uid):
                raise BoardCatalogError(
                    "Live debug connection identity changed during setup; stop before reading "
                    "silicon or committing a profile. Restart setup_overview with the current "
                    "friendly connection inventory."
                )
            if setup_board.silicon_id_addr is not None:
                observed = target_control.read_memory(
                    handle, setup_board.silicon_id_addr, setup_board.silicon_id_width_bits
                )
                expected = setup_board.silicon_id_expected
                assert expected is not None
                assert setup_board.silicon_id_mask is not None
                if (observed & setup_board.silicon_id_mask) != (
                    expected & setup_board.silicon_id_mask
                ):
                    raise BoardCatalogError(
                        "Live silicon identity did not match the reviewed board catalog "
                        f"(observed 0x{observed:08X}, expected 0x{expected:08X})."
                    )
            assert setup_board.test_addr is not None
            target_control.read_memory(handle, setup_board.test_addr, 32)
            evidence = capture_datasheet_evidence(
                _profile_repository.store, Path(context.user_input.datasheet_path)
            )
            if evidence.sha256 != actual_datasheet_hash:
                raise BoardCatalogError("datasheet bytes changed during live setup")
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
            if generic_support is not None
            else {}
        ),
        **(
            {"debug_connect_mode": legacy_catalog.debug_connect_mode}
            if not generic and legacy_catalog.debug_connect_mode is not None
            else {}
        ),
        **(
            {"debug_clock_hz": legacy_catalog.debug_clock_hz}
            if not generic and legacy_catalog.debug_clock_hz is not None
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
                    "pyocd_target": target,
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
            if existing.read_only:
                cancellation_checkpoint()
                committed = _profile_repository.commit_legacy_migration(
                    _profile_repository.stage_legacy_migration(
                        context.user_input.board_id,
                        context.user_input.mcu_part_number,
                    )
                )
            else:
                committed = existing
        if generic:
            mode, frequency = _setup_attachment_overrides[context.user_input.board_id]
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
            raise BoardCatalogError("captured datasheet evidence was not committed canonically")
    except Exception as exc:  # noqa: BLE001 - workflow records the typed terminal result
        return SetupPhaseOutcome.stop(
            "setup_connection_failed",
            "setup/live-connect-failed",
            f"Live connection failed before the profile could be committed: {exc}",
        )
    return SetupPhaseOutcome.success(
        "setup/core-profile-committed-after-connect",
        profile=committed.source_path.name,
        live_connections=len(opened),
        datasheet_sha256=actual_datasheet_hash,
        attachment_policy={
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
    if result.status == "validation_passed" or configuration_only or (
        result.status == "validation_incomplete"
        and result.code == "validation/safety-missing"
        and "silicon_actual" in result.observed
    ):
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


def _build_automatic_catalog_safety(context: SetupPhaseContext):
    """Build the first v2 map only after pinned independent authorities agree."""

    profile = _profile_repository.load(context.user_input.board_id, include_legacy=False)
    if getattr(profile, "device_support", None) is not None:
        _datasheet_path, current_digest = hash_local_datasheet(
            Path(context.user_input.datasheet_path)
        )
        profile_digest = profile.to_document().get("datasheet_sha256")
        if current_digest != profile_digest:
            raise SafetyMapError(
                "datasheet bytes changed after profile acceptance; restart setup with one stable local PDF"
            )
        candidate = _derive_generic_safety_map(context.user_input.board_id)
        cancellation_checkpoint()
        _safety_repository.commit(context.user_input.board_id, candidate)
        return candidate
    profile_document = profile.to_document()
    datasheet_digest = profile_document.get("datasheet_sha256")
    if not isinstance(datasheet_digest, str) or not datasheet_digest.strip():
        raise BoardCatalogError("profile has no server-computed datasheet digest")
    catalog = resolve_reviewed_support(profile.mcu_part_number or "", datasheet_digest)
    if profile.mcu_part_number != catalog.package_part_number:
        raise BoardCatalogError(
            "profile MCU part number is not the exact reviewed package variant; repair the "
            "profile from the user's exact package marking before safety setup"
        )
    _datasheet_path, current_digest = hash_local_datasheet(
        Path(context.user_input.datasheet_path)
    )
    if current_digest != datasheet_digest:
        raise BoardCatalogError(
            "datasheet bytes changed after profile acceptance; restart setup with one stable "
            "local PDF"
        )
    load_pinned_reviewed_evidence(catalog, datasheet_digest)
    candidate = _derive_reviewed_safety_map(context.user_input.board_id)
    cancellation_checkpoint()
    _safety_repository.commit(context.user_input.board_id, candidate)
    return candidate


def _setup_safety_research_phase(context: SetupPhaseContext) -> SetupPhaseOutcome:
    try:
        artifacts = _safety_repository.load_current(context.user_input.board_id)
        _require_current_reviewed_map(artifacts)
    except (SafetyMapError, ValueError) as exc:
        try:
            artifacts = _build_automatic_catalog_safety(context)
        except (
            BoardCatalogError,
            ProfileError,
            SafetyMapError,
            OSError,
            ValueError,
        ) as build_exc:
            return SetupPhaseOutcome.stop(
                "setup_safety_incomplete",
                "setup/safety-setup-required",
                "Authoritative safety evidence could not be built automatically. Resolve the "
                "reported evidence issue before continuing setup.",
                details={"reason": str(build_exc), "prior_reason": str(exc)},
            )
    return SetupPhaseOutcome.success(
        "setup/safety-sources-verified",
        map_digest=artifacts.canonical_digest,
        source_groups=list(artifacts.source_digests.to_document()),
    )


def _setup_safety_map_phase(context: SetupPhaseContext) -> SetupPhaseOutcome:
    try:
        artifacts = _safety_repository.load_current(context.user_input.board_id)
        _require_current_reviewed_map(artifacts)
        conflicts = region_conflicts(artifacts.regions)
    except (SafetyMapError, ValueError) as exc:
        return SetupPhaseOutcome.stop(
            "setup_safety_incomplete",
            "setup/safety-map-incomplete",
            "The safety map is incomplete. Run board_safety_refresh before validation.",
            details={"reason": str(exc)},
        )
    if conflicts:
        return SetupPhaseOutcome.stop(
            "setup_safety_incomplete",
            "setup/safety-map-conflict",
            "The safety map has a region conflict. Resolve the reported safety sources and "
            "rerun board_safety_refresh.",
            details={"conflicts": conflicts},
        )
    missing_kinds = _missing_base_safety_kinds(
        artifacts.regions, generic=isinstance(artifacts, GenericSafetyMapDocument)
    )
    if missing_kinds:
        return SetupPhaseOutcome.stop(
            "setup_safety_incomplete",
            "setup/safety-map-kinds-missing",
            "The safety map lacks required prohibited, CPU/system, peripheral, flash, or RAM "
            "classifications. Resolve the authoritative evidence and rerun board_safety_refresh.",
            details={"missing_kinds": list(missing_kinds)},
        )
    return SetupPhaseOutcome.success(
        "setup/safety-map-consistent",
        map_digest=artifacts.canonical_digest,
        region_count=len(artifacts.regions),
    )


def _setup_commit_phase(context: SetupPhaseContext) -> SetupPhaseOutcome:
    board_id = context.user_input.board_id
    expected_ref = (
        _firm_store.layout.safety_reference_prefix(board_id) / "memory_map.yaml"
    ).as_posix()
    try:
        profile = _profile_repository.load(board_id, include_legacy=False)
        if profile.safety_ref != expected_ref:
            cancellation_checkpoint()
            profile = _profile_repository.commit_safety_ref(
                _profile_repository.stage_safety_ref(board_id, expected_ref)
            )
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
            "setup/safety-reference-commit-failed",
            f"Safety reference or final validation failed: {exc}",
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
            "setup/safety-reference-committed-configuration-only"
            if configuration_only
            else "setup/safety-reference-committed-and-validated"
        ),
        safety_ref=profile.safety_ref,
        validation_status=result.status,
        capability_level=(
            "connected_diagnostics_only" if configuration_only else "identity_verified"
        ),
        validation_report=str(result.report_paths.report),
    )


def _confirm_setup_cache(user_input: SetupUserInput, decision) -> None:
    probe = decision.selected_probe
    serial = decision.selected_serial
    if probe is None or serial is None:
        return
    probe_identity = ProbeIdentity(probe.probe_family, probe.usb_serial)
    endpoint = SerialEndpoint(serial.port_path, serial.usb_serial, serial.vid, serial.pid)
    if probe_identity.is_stable and endpoint.has_stable_identity:
        _attachment_cache.confirm(user_input.board_id, probe_identity, endpoint)


def _get_setup_status(board_id: str) -> Mapping[str, object]:
    """Return a non-authoritative setup barrier for external orchestration."""

    configuration_ready = False
    configuration_reason = "schema-v2 profile or current safety evidence is missing"
    aggregate: str | None = None
    profile: BoardProfile | None = None
    try:
        profile = _profile_repository.load(board_id, include_legacy=False)
        artifacts = _safety_repository.load_current(board_id)
        if (
            profile.safety_ref
            != (_firm_store.layout.safety_reference_prefix(board_id) / "memory_map.yaml").as_posix()
        ):
            configuration_reason = "profile does not reference the current safety map"
        elif region_conflicts(artifacts.regions):
            configuration_reason = "current safety map has unresolved region conflicts"
        elif missing_kinds := (
            _missing_base_safety_kinds(artifacts.regions, generic=True)
            if isinstance(artifacts, GenericSafetyMapDocument)
            else _missing_base_safety_kinds(artifacts.regions)
        ):
            configuration_reason = (
                "current safety map lacks required base classifications: "
                + ", ".join(missing_kinds)
            )
        else:
            aggregate = _safety_policy.current_aggregate(board_id)
            configuration_ready = True
            configuration_reason = "profile and safety evidence are current"
    except (ProfileError, SafetyMapError, SafetyPolicyError, ValueError) as exc:
        configuration_reason = str(exc)

    connection = connection_manager.maybe_connection(board_id)
    stamp = gate_manager.snapshot(board_id)
    live_session_ready = bool(
        configuration_ready
        and aggregate is not None
        and connection is not None
        and stamp is not None
        and stamp.connection_id == connection.connection_id
        and stamp.map_digest == aggregate
    )
    identity_capability = (
        stamp.identity_capability
        if live_session_ready and stamp is not None
        else None
    )
    ready_for_flash_planning = bool(
        live_session_ready and identity_capability in {"exact", "compatible"}
    )
    uart_attachment_ready = False
    uart_reason = "UART attachment has not been resolved for this live board connection"
    resolved_uart: dict[str, object] | None = None
    resolved_probe: dict[str, object] | None = None
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
        remedy = (
            "Complete board_setup if no profile exists. If the profile exists but stable safety "
            "evidence is missing, invalid, old, or changed, run board_safety_refresh."
        )
    elif connection is None:
        remedy = "Connect this board and run board_validate in the current Server Run."
    elif not live_session_ready:
        remedy = "Run board_validate for the current board connection."
    elif not ready_for_flash_planning:
        remedy = (
            "Setup is ready for bounded diagnostics, but application flash remains unavailable "
            "until validation obtains replayable exact or processor-compatible live identity."
        )
    else:
        remedy = "Setup is ready; normal guarded plans may now be used."
    build_guidance: dict[str, object] | None = None
    if profile is not None and profile.mcu_part_number:
        from pyocd_debug_mcp.native_build import command_template

        build_guidance = {
            "authority": "advisory_only",
            "primary_workflow": "general_native_build_helper",
            "guidance": (
                "First inspect the project's build files and discover its real build executable, "
                "SDK/toolchain, arguments, working directory, and outputs. Prefer a compatible "
                "local installation; if none exists, normal network acquisition is allowed. Then "
                "replace the generic template values and put the exact build argv after '--'. "
                "The helper executes it directly without a shell, inherits the environment and "
                "network by default, accepts repeatable --env KEY=VALUE overrides, verifies "
                "ELF/HEX formats, and reports map provenance without claiming unprovable ELF/map "
                "coherence. Use --offline only when intentionally requested; "
                "it applies best-effort common-client environment guards, not a network sandbox. "
                "Zephyr/west and GNU Make detection are optional conveniences, never the ceiling."
            ),
            "general_build_helper": command_template(),
            "artifact_collection": {
                "tool": "collect_build_artifacts",
                "arguments_template": {
                    "output_dir": "<new-or-empty-collection-dir>",
                    "elf_path": "<native-build ELF when produced>",
                    "hex_path": None,
                    "bin_path": None,
                    "map_path": "<matching native-build linker map>",
                    "expected_roles": ["elf", "map"],
                },
                "purpose": (
                    "Normalize explicit outputs from any build system into canonical hashed "
                    "artifacts. Collection does not build, search, validate, or authorize."
                ),
            },
            "safety_boundary": (
                "Build guidance is advisory and not safety authority. Optionally collect the "
                "selected output, then submit the matching flash plan, which binds that artifact. "
                "The flash action revalidates its bytes and complete containment before target "
                "mutation and requires exact or processor-compatible live identity for an "
                "application (bootloader/recovery authority remains separate). Use "
                "board_safety_refresh only for a stable-map problem."
            ),
            "toolchain_fallback": (
                "If bounded local discovery finds no compatible build environment, the agent may "
                "use the normal installation or network acquisition path for the project's actual "
                "toolchain, then pass that exact command/environment to the generic helper."
            ),
        }
    return {
        "status": "setup_ready" if live_session_ready else "setup_not_ready",
        "board_id": board_id,
        "configuration_ready": configuration_ready,
        "live_session_ready": live_session_ready,
        "identity_capability": identity_capability,
        "ready_for_flash_planning": ready_for_flash_planning,
        "ready_for_code": configuration_ready and live_session_ready,
        "uart_attachment_ready": uart_attachment_ready,
        "ready_for_uart_work": (
            configuration_ready and live_session_ready and uart_attachment_ready
        ),
        "uart_reason": uart_reason,
        "resolved_uart": resolved_uart,
        "resolved_probe": resolved_probe,
        "configuration_reason": configuration_reason,
        "remedy": remedy,
        "build_guidance": build_guidance,
    }


def _profile_name_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def _profile_needs_repair(profile: BoardProfile) -> bool:
    """Return whether setup must restore missing durable reviewed evidence."""

    digest = profile.to_document().get("datasheet_sha256")
    expected_ref = (
        _firm_store.layout.safety_reference_prefix(profile.board_id) / "memory_map.yaml"
    ).as_posix()
    return (
        profile.read_only
        or profile.safety_ref != expected_ref
        or not isinstance(digest, str)
    )


def _replace_setup_assignments(bindings: Mapping[str, str], reason: str) -> None:
    """Replace provisional routing and revoke authority whose binding changed."""

    previous = assignment_store.bindings()
    replacement = dict(bindings)
    if previous == replacement:
        return
    affected = set(previous.values()) | set(replacement.values())
    workflow = globals().get("_setup_workflow")
    if isinstance(workflow, SetupWorkflow):
        for board_id in affected:
            workflow.revoke(board_id)

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
            workflow.revoke(connected_board)
        with connection_manager.lock_for(connected_board):
            current = connection_manager.maybe_connection(connected_board)
            if current is not connection:
                continue
            connection_manager.clear(connected_board)
            gate_manager.clear(connected_board, reason)
            try:
                target_control.close_session(connection.handle)
            finally:
                _session_store.close_session(connection.runtime_session)
    assignment_store.replace(replacement)
    for board_id in affected:
        plan_engine.invalidate_board(board_id, reason)
        gate_manager.clear(board_id, reason)
        loader = globals().get("setup_tool_loader")
        if isinstance(loader, SetupToolLoadState):
            loader.clear_allowance(board_id)


def _same_setup_connection(left: str, right: str) -> bool:
    """Compare server-issued setup connection IDs without broad target inference."""

    if left.casefold() == right.casefold():
        return True
    if left.casefold().startswith("probe:") and right.casefold().startswith("probe:"):
        return _stable_identity_equal(left.split(":", 1)[1], right.split(":", 1)[1])
    return False


def _setup_connection_key(connection_id: str) -> str:
    """Canonical key for one server-issued setup connection identity."""

    normalized = connection_id.strip().casefold()
    if not normalized.startswith("probe:"):
        return normalized
    probe_identity = normalized.split(":", 1)[1]
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
    probe_uid = (connection.handle.probe_uid or "").strip()
    return bool(probe_uid) and _same_setup_connection(
        selected_connection,
        f"probe:{probe_uid}",
    )


def _proposed_board_id(display_name: str, existing: set[str]) -> str:
    ascii_name = unicodedata.normalize("NFKD", display_name).encode("ascii", "ignore").decode()
    stem = re.sub(r"[^a-z0-9]+", "_", ascii_name.casefold()).strip("_")[:48]
    if not stem:
        stem = f"board_{hashlib.sha256(display_name.encode('utf-8')).hexdigest()[:8]}"
    candidate = stem
    counter = 2
    while candidate in existing:
        suffix = f"_{counter}"
        candidate = f"{stem[: 64 - len(suffix)]}{suffix}"
        counter += 1
    return candidate


def _setup_overview(
    board_names: list[str] | None,
    connection_assignments: Mapping[str, str] | None = None,
) -> Mapping[str, object]:
    """Give an agent the complete startup route without asking the user for internals."""

    try:
        profiles = _profile_repository.load_all(include_legacy=True)
    except ProfileError as exc:
        return {
            "status": "setup_overview_blocked",
            "agent_prompt": (
                f"The board-profile index is invalid: {exc}. Explain the profile problem plainly "
                "and stop before hardware access. Do not expose this payload or internal paths."
            ),
            "profiles": [],
            "connections": [],
            "routes": [],
        }
    profile_rows: list[dict[str, object]] = []
    by_name: dict[str, tuple[BoardProfile, str, str]] = {}
    for profile in profiles:
        complete = False
        reason = "legacy or incomplete profile; repair is required"
        route_kind = "repair"
        if not _profile_needs_repair(profile):
            route_kind = "refresh"
            try:
                artifacts = _safety_repository.load_current(profile.board_id)
                _require_current_reviewed_map(artifacts)
                complete = not region_conflicts(artifacts.regions)
                route_kind = "validate" if complete else "refresh"
                reason = (
                    "profile and safety map are present; validate this run"
                    if complete
                    else "safety map has conflicts; board_safety_refresh is required"
                )
            except (SafetyMapError, ValueError) as exc:
                reason = f"safety evidence is incomplete: {exc}"
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
            connection_id = f"probe:{probe.usb_serial or probe.probe_id}"
            connection_rows_by_identity.setdefault(
                _setup_connection_key(connection_id),
                {
                    "connection_id": connection_id,
                    "friendly_name": ProbeCandidate(
                        probe.probe_id,
                        probe.description,
                        probe.probe_family,
                        probe.usb_serial,
                    ).friendly_label(),
                    "probe_family": probe.probe_family,
                },
            )
        connection_rows = list(connection_rows_by_identity.values())
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
        inventory_error = None

    routes: list[dict[str, object]] = []
    provisional_bindings: dict[str, str] = {}
    validated_names: list[tuple[str, str]] = []
    no_board_sentinel = False
    if board_names is not None:
        if len(board_names) > 8:
            raise ValueError("board_names is bounded to eight names")
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
        available_connections = {
            str(row["connection_id"]) for row in connection_rows
        }
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
            }
        if len(connection_rows) > 1 and not assignments:
            _replace_setup_assignments({}, "setup overview requires explicit assignments")
            return {
                "status": "setup_assignment_required",
                "agent_prompt": (
                    "Ask which friendly debug-probe description belongs to each requested board "
                    "name. Then retry setup_overview with the same board_names and copy only the "
                    "selected server connection IDs into connection_assignments; never show those "
                    "IDs. Other visible probes may remain unassigned."
                ),
                "profiles": profile_rows,
                "connections": connection_rows,
                "serial_choices": serial_rows,
                "inventory_error": inventory_error,
                "routes": [],
                "assignment_template": {name: None for name, _key in validated_names},
            }
        if not assignments and len(connection_rows) == 1:
            assignments = {validated_names[0][0]: str(connection_rows[0]["connection_id"])}
        existing_ids = {profile.board_id for profile in profiles}
        setup_definition = PLAN_DEFINITIONS["board_setup"]
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
                    "mode": "setup",
                    "connection_id": single_connection,
                    "display_name": name,
                    "mcu_part_number": None,
                    "requires_uart": None,
                    "serial_baudrate": None,
                    "serial_id": single_serial,
                    "datasheet_path": None,
                }
                parameter_template = {
                    field.name: known_parameters.get(field.name)
                    for field in setup_definition.action_fields
                }
                required_user_facts = [
                    "exact package-level MCU part number (full package marking)",
                    "authoritative local datasheet PDF",
                    "whether this firmware workflow uses UART; if yes, its baud rate",
                    "explicit ordinary-language authorization to run bounded, non-destructive setup",
                ]
                accepted_response: dict[str, object] = {
                    "copy_into": "plan_action_parameters_template"
                }
                if len(connection_rows) == 0:
                    required_user_facts.append("attach and identify one compatible debug probe")
                elif len(connection_rows) > 1:
                    required_user_facts.append(
                        "which friendly debug-probe choice belongs to this board"
                    )
                    accepted_response["connection_id"] = (
                        "<one connections[].connection_id selected from its friendly_name>"
                    )
                if len(serial_rows) == 0:
                    required_user_facts.append(
                        "if UART is used, attach and identify the board's UART connection"
                    )
                elif len(serial_rows) > 1:
                    required_user_facts.append(
                        "if UART is used, which friendly UART choice belongs to this board"
                    )
                    accepted_response["serial_id"] = (
                        "<one serial_choices[].choice_id selected from its friendly_name>"
                    )
                routes.append(
                    {
                        "display_name": name,
                        "board_id": board_id,
                        "route": "setup",
                        "next_tool": "board_setup-plan",
                        "load_call": {
                            "tool": "load_setup_tool",
                            "arguments": {
                                "board_id": board_id,
                                "tool_name": "board_setup-plan",
                            },
                        },
                        "plan_initialization_call": {
                            "tool": "board_setup-plan",
                            "arguments": {
                                field: None for field in setup_definition.null_field_names
                            },
                        },
                        "plan_action_parameters_template": parameter_template,
                        "required_user_facts": required_user_facts,
                        "accepted_response": (
                            accepted_response if len(accepted_response) > 1 else None
                        ),
                    }
                )
                continue
            profile, route_kind, reason = match
            selected_connection = assignments[name]
            connection = connection_manager.maybe_connection(profile.board_id)
            mismatch = None
            if connection is not None and _selected_setup_connection_matches(
                selected_connection, connection
            ):
                mismatch = gate_manager.current_mismatch(
                    profile.board_id,
                    connection.connection_id,
                    connection.handle.probe_uid or connection.connection_id,
                )
            if mismatch is not None:
                routes.append(
                    {
                        "display_name": name,
                        "board_id": profile.board_id,
                        "route": "mismatch",
                        "next_tool": None,
                        "expected_mcu": mismatch.expected_mcu,
                        "observed_mcu": mismatch.observed_mcu,
                        "reason": (
                            "The attached hardware does not match this established profile. "
                            "Tell the user the expected and observed MCU identities and ask what "
                            "they want to do. If they keep the different hardware, call "
                            "setup_overview with a new familiar name to create a new logical "
                            "board; never rewrite this profile in place."
                        ),
                    }
                )
                continue
            provisional_bindings[selected_connection] = profile.board_id
            if route_kind == "repair":
                setup_definition = PLAN_DEFINITIONS["board_setup"]
                single_serial = serial_rows[0]["choice_id"] if len(serial_rows) == 1 else None
                known_parameters: dict[str, object] = {
                    "mode": "repair",
                    "connection_id": selected_connection,
                    "display_name": profile.display_name,
                    "mcu_part_number": profile.mcu_part_number,
                    "requires_uart": None,
                    "serial_baudrate": None,
                    "serial_id": single_serial,
                    "datasheet_path": None,
                }
                repair_user_facts = [
                    "authoritative local datasheet PDF",
                    "whether this firmware workflow uses UART; if yes, its baud rate",
                    "explicit ordinary-language authorization to run bounded, non-destructive repair",
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
                        "next_tool": "board_setup-plan",
                        "reason": reason,
                        "load_call": {
                            "tool": "load_setup_tool",
                            "arguments": {
                                "board_id": profile.board_id,
                                "tool_name": "board_setup-plan",
                            },
                        },
                        "plan_initialization_call": {
                            "tool": "board_setup-plan",
                            "arguments": {
                                field: None for field in setup_definition.null_field_names
                            },
                        },
                        "plan_action_parameters_template": {
                            field.name: known_parameters.get(field.name)
                            for field in setup_definition.action_fields
                        },
                        "required_user_facts": repair_user_facts,
                    }
                )
                continue
            if route_kind == "refresh":
                routes.append(
                    {
                        "display_name": name,
                        "board_id": profile.board_id,
                        "route": "refresh",
                        "next_tool": "board_safety_refresh",
                        "reason": reason,
                        "load_call": {
                            "tool": "load_setup_tool",
                            "arguments": {
                                "board_id": profile.board_id,
                                "tool_name": "board_safety_refresh",
                            },
                        },
                        "next_call": {
                            "tool": "board_safety_refresh",
                            "arguments": {"board_id": profile.board_id},
                        },
                    }
                )
                continue
            routes.append(
                {
                    "display_name": name,
                    "board_id": profile.board_id,
                    "route": "validate",
                    "next_tool": "board_validate",
                    "reason": reason,
                    "load_call": {
                        "tool": "load_setup_tool",
                        "arguments": {
                            "board_id": profile.board_id,
                            "tool_name": "board_validate",
                        },
                    },
                    "next_call": {
                        "tool": "board_validate",
                        "arguments": {
                            "board_id": profile.board_id,
                            "probe_id": selected_connection.removeprefix("probe:"),
                        },
                    },
                }
            )
        _replace_setup_assignments(provisional_bindings, "setup overview assignment replaced")

    if board_names == [] or (no_board_sentinel and len(validated_names) == 1):
        _replace_setup_assignments({}, "setup overview reported no board")
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
            "visible probes may remain unassigned. Then call setup_overview again with that answer. "
            "Do not show this JSON, board IDs, connection IDs, or machine identifiers."
        )
    else:
        status = "setup_routes_ready"
        prompt = (
            "Use each route's machine-readable calls without asking the user for their internal "
            "values. For validate or refresh, copy load_call and next_call. Follow a returned "
            "repair route only for its incomplete same-identity profile. For unknown-name setup, copy "
            "load_call and plan_initialization_call, ask only for required_user_facts and friendly "
            "ambiguous choices, then copy them into plan_action_parameters_template. Present only "
            "friendly choices to the user; do not expose this JSON or internal IDs."
        )
    return {
        "status": status,
        "agent_prompt": prompt,
        "profiles": profile_rows,
        "connections": connection_rows,
        "serial_choices": serial_rows,
        "inventory_error": inventory_error,
        "routes": routes,
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

    payload = read_bounded_pack_bytes(path)
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
    board_id: str, continuation_id: str, probe_uid: str
) -> PackCandidatePipeline:
    key = (board_id, continuation_id)
    current = _setup_pack_pipelines.get(key)
    if current is not None:
        return current

    def live_connect(
        target: str,
        _path: Path,
        expected_sha256: str,
        pdsc_device: str | None,
    ) -> None:
        last_error: TargetConnectionError | None = None
        for mode, frequency in (("attach", None), ("attach", 1_000_000), ("under-reset", 1_000_000)):
            try:
                handle = target_control.open_session(
                    board=None,
                    unique_id=probe_uid,
                    target=target,
                    server_timeouts=_staged_server_timeouts,
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
            _setup_attachment_overrides[board_id] = (mode, frequency)
            return
        assert last_error is not None
        raise last_error

    current = PackCandidatePipeline(
        _firm_store,
        enumerate_targets=_enumerate_pack_targets,
        live_connect=live_connect,
    )
    _setup_pack_pipelines[key] = current
    return current


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
                previous.external_adapter_confirmed,
            )
        elif decision.code in {
            "setup/serial-selection-required",
            "setup/serial-selection-invalid",
        }:
            selected = PreflightSelections(
                previous.probe_id or decision_probe,
                choice_id,
                previous.build_configuration_id,
                previous.external_adapter_confirmed,
            )
        elif decision.code == "setup/external-adapter-confirmation-required":
            selected = PreflightSelections(
                previous.probe_id or decision_probe,
                previous.serial_id or decision_serial,
                previous.build_configuration_id,
                True,
            )
        elif decision.code in {
            "setup/build-selection-required",
            "setup/build-selection-invalid",
        }:
            selected = PreflightSelections(
                previous.probe_id or decision_probe,
                previous.serial_id or decision_serial,
                choice_id,
                previous.external_adapter_confirmed,
            )
        else:
            raise ValueError(f"unsupported setup choice route: {decision.code}")
        _setup_selections_by_board[board_id] = selected
        return {
            "status": "setup_continuation_accepted",
            "board_id": board_id,
            "accepted": "friendly_choice",
            "redirect": "Call board_fix_setup now under the active paired setup allowance.",
        }

    if status != "setup_research_required":
        raise ValueError("the current setup response is waiting for a friendly choice, not research")

    target_fields = {"pyocd_target", "evidence", "reasoning_summary"}
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
    if fields != target_fields and fields != pack_fields:
        raise ResearchError(
            "research/field-set-mismatch",
            "response fields must exactly match the requested choice, target, or pack schema",
        )
    _validated_research_prose(response)
    target_value = response.get("pyocd_target")
    target = (
        target_value.strip().casefold()
        if isinstance(target_value, str) and target_value.strip()
        else None
    )
    reviewed_target: str | None = None
    try:
        current_support = _resolve_setup_support(user_input)
    except (BoardCatalogError, PackProvisionError):
        current_support = None
    if current_support is not None and not _is_generic_support(current_support):
        reviewed_board = cast(Any, current_support).catalog
        if user_input.mcu_part_number != reviewed_board.package_part_number:
            raise ResearchError(
                "target/reviewed-part-mismatch",
                "The exact MCU part does not match the reviewed compatibility mapping.",
            )
        reviewed_target = reviewed_board.pyocd_target.casefold()

    if fields == target_fields:
        if target is None:
            raise ResearchError("research/target-required", "pyocd_target must be non-empty text")
        request = make_research_request(
            fact_id="pyocd_target",
            continuation_token=continuation_id,
            board_id=board_id,
            mcu_part_number=user_input.mcu_part_number,
            unresolved_fact="Resolve the exact pyOCD target for this MCU.",
            requested_fields=("pyocd_target", "evidence", "reasoning_summary"),
            authoritative_facts={"exact_mcu_part_number": user_input.mcu_part_number},
            acceptable_sources=("official pyOCD documentation", "official vendor CMSIS-Pack"),
            validation_plan=(
                "Check exact MCU consistency.",
                "Confirm built-in or promoted pack support.",
                "Require a live connection before profile commit.",
            ),
        )

        def validate(candidate: Mapping[str, object]) -> ValidationOutcome:
            try:
                candidate_target = str(candidate["pyocd_target"]).casefold()
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
                    # A raw target name is only a research lead. Generic setup
                    # still requires a pack leaf to derive and replay physical
                    # device facts, so direct target acceptance is withheld.
                    raise TargetResolutionError(
                        "target/pack-proof-required",
                        "Generic target research requires the matching official CMSIS-Pack "
                        "candidate before setup can establish device authority",
                    )
            except TargetResolutionError as exc:
                return ValidationOutcome(False, str(exc), exc.observed)
            return ValidationOutcome(True)

        result = _setup_research.validate_reply(request, response, validate)
        if result.status != "accepted":
            if result.failure is not None and (
                "absent from built-in" in result.failure.reason
                or "CMSIS-Pack" in result.failure.reason
            ):
                return {
                    "status": "setup_research_required",
                    "continuation_id": continuation_id,
                    "agent_prompt": (
                        "The target is plausible but unavailable. Research one official local "
                        "CMSIS-Pack candidate and submit the exact pack response through "
                        "continue_setup. Do not ask the user for a target or expose this payload."
                    ),
                    "exact_response_fields": sorted(pack_fields),
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
        pipeline = _setup_pack_pipeline(board_id, continuation_id, probe_uid)
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
        except (PackCandidateError, PackProvisionError) as exc:
            failure = exc.failure if isinstance(exc, PackCandidateError) else None
            return {
                "status": (
                    "setup_unresolved"
                    if isinstance(exc, PackCandidateError)
                    and exc.code == "package/retry-exhausted"
                    else "setup_research_required"
                ),
                "continuation_id": continuation_id,
                "agent_prompt": f"The package candidate was rejected: {exc}. Research a materially different official candidate; do not expose this payload.",
                "rejected_candidates": (
                    [failure.to_document()] if failure is not None else []
                ),
                "exact_response_fields": sorted(pack_fields),
            }

    assert target is not None
    _setup_target_overrides[board_id] = target
    return {
        "status": "setup_continuation_accepted",
        "board_id": board_id,
        "accepted": "target_and_pack" if fields == pack_fields else "target",
        "pyocd_target": target,
        "redirect": "Call board_fix_setup now under the active paired setup allowance.",
    }


def _clear_setup_continuation(board_id: str) -> None:
    _setup_target_overrides.pop(board_id, None)
    _setup_attachment_overrides.pop(board_id, None)
    _setup_selections_by_board.pop(board_id, None)
    for key in tuple(_setup_pack_pipelines):
        if key[0] == board_id:
            _setup_pack_pipelines.pop(key, None)


def _setup_plan_eligibility(board_id: str) -> tuple[bool, str]:
    """Expose populated setup only for first setup or an exact live mismatch route."""

    try:
        profile = _profile_repository.load(board_id, include_legacy=True)
    except ProfileError as exc:
        existing_profile_paths = tuple(
            _profile_repository.store.layout.board_profile(board_id, suffix=suffix)
            for suffix in (".yaml", ".yml", ".json")
        )
        if any(path.exists() for path in existing_profile_paths):
            return (
                False,
                f"An existing profile is malformed or incomplete: {exc}. Route through "
                "board_validate and its exact remedy; setup remains locked against rewrite.",
            )
        return True, "No established profile exists; first-time setup is eligible."
    if _profile_needs_repair(profile):
        return (
            True,
            "The existing same-identity profile is incomplete. A fresh authorized mode=repair "
            "plan may rerun deterministic preflight and resume current setup phases.",
        )
    connection = connection_manager.maybe_connection(board_id)
    if connection is not None:
        mismatch = gate_manager.current_mismatch(
            board_id,
            connection.connection_id,
            connection.handle.probe_uid or connection.connection_id,
        )
        if mismatch is not None:
            return (
                True,
                "Validation recorded a live identity mismatch. Ask what the user wants; if they "
                "keep the different hardware, use setup_overview with a new familiar name and "
                "new logical board ID. The established profile remains locked against rewrite.",
            )
    return (
        False,
        "This established profile has no exact live mismatch allowance. Use board_validate or "
        "board_safety_refresh only when its reported remedy requires it; setup remains locked.",
    )


_setup_workflow = SetupWorkflow(
    _report_writer,
    _setup_inventory,
    phase_handlers={
        SetupPhase.CONNECTION: _setup_connection_phase,
        SetupPhase.VALIDATION: _setup_validation_phase,
        SetupPhase.SAFETY_RESEARCH: _setup_safety_research_phase,
        SetupPhase.SAFETY_MAP: _setup_safety_map_phase,
        SetupPhase.COMMIT: _setup_commit_phase,
    },
    on_allowance_closed=lambda board_id, reason: plan_engine.complete_paired_plan(
        "board_setup", board_id, reason
    ),
    on_cache_confirmation=_confirm_setup_cache,
    cancellation_checkpoint=cancellation_checkpoint,
)
setup_tool_loader = SetupToolLoadState(server_run)
setup_tool_handlers = build_setup_handlers(
    SetupToolServices(
        loader=setup_tool_loader,
        plan_engine=plan_engine,
        workflow=_setup_workflow,
        validator=_board_validator,
        safety_setup=_run_board_safety_refresh,
        safety_refresh=_run_board_safety_refresh,
        setup_status=_get_setup_status,
        setup_overview=_setup_overview,
        setup_continue=_setup_continue,
        setup_selections=lambda board_id: _setup_selections_by_board.get(
            board_id, PreflightSelections()
        ),
        clear_setup_continuation=_clear_setup_continuation,
        setup_plan_eligible=_setup_plan_eligibility,
        require_assignment=lambda board_id, connection_id: assignment_store.require(
            connection_id, board_id
        ),
        assigned_connection=assignment_store.connection_for,
    )
)


def _revoke_with_setup_closure(action_name: str, board_id: str, reason: str) -> None:
    plan_engine.invalidate(action_name, board_id, reason)
    if action_name == "board_setup":
        _setup_workflow.revoke(board_id)
        setup_tool_loader.clear_allowance(board_id)
        _clear_setup_continuation(board_id)


permission_store.set_revocation_handler(_revoke_with_setup_closure)
M6_GUARDED_ACTIONS = ("board_setup", "board_fix_setup")
for _setup_name, _setup_handler in setup_tool_handlers.items():
    mcp.add_tool(
        _setup_handler,
        name=_setup_name,
        description=_setup_handler.__doc__,
        structured_output=False,
    )
    if _setup_name.endswith("-plan"):
        forbid_unknown_tool_arguments(mcp, _setup_name)

for _setup_action in M6_GUARDED_ACTIONS:
    tool_registry.configure(
        _setup_action,
        hidden=True,
        locked=True,
        prerequisite="board_setup-plan",
    )
    mcp.configure_guarded_dispatch(
        _setup_action,
        guard=_enforce_guarded_invocation,
        lock_for_board=lambda board_id: connection_manager.lock_for(board_id),
    )
    forbid_unknown_tool_arguments(mcp, _setup_action)


def _mark_unlock_completed(board_id: str) -> None:
    runtime = _runtime_for(board_id)
    if runtime is not None:
        _session_store.mark_recover_completed(runtime)


def _revoke_unlock_permission(board_id: str, reason: str) -> None:
    permission_store.revoke("target_unlock", board_id, reason=reason)


_unlock_coordinator = UnlockCoordinator(
    UnlockToolServices(
        server_run=server_run,
        plan_engine=plan_engine,
        profiles=_profile_repository,
        safety_repository=_safety_repository,
        reports=_report_writer,
        gate_manager=gate_manager,
        handle_for=_handle,
        connection_id_for=lambda board_id: _connection(board_id).connection_id,
        session_id_for=_active_session_id,
        current_map_digest=_safety_policy.current_aggregate,
        supports_recovery=target_control.supports_recovery,
        recover_target=lambda handle, mechanism: target_control.recover_target(
            handle, recover_mode=mechanism
        ),
        mark_recover_completed=_mark_unlock_completed,
        revoke_permission=_revoke_unlock_permission,
    )
)
unlock_tool_handlers = build_unlock_handlers(_unlock_coordinator)
for _unlock_name, _unlock_handler in unlock_tool_handlers.items():
    mcp.add_tool(
        _unlock_handler,
        name=_unlock_name,
        description=_unlock_handler.__doc__,
        structured_output=False,
    )
    if _unlock_name.endswith("-plan"):
        forbid_unknown_tool_arguments(mcp, _unlock_name)
mcp.configure_layer2("target_unlock")
tool_registry.configure(
    "target_unlock",
    hidden=True,
    locked=True,
    prerequisite="target_unlock-plan",
)
mcp.configure_guarded_dispatch(
    "target_unlock",
    guard=_enforce_guarded_invocation,
    lock_for_board=lambda board_id: connection_manager.lock_for(board_id),
)
M8_GUARDED_ACTIONS = ("target_unlock",)

batch_tool_handlers = build_batch_handlers(
    mcp.call_tool,
    tool_exists=tool_registry.is_registered,
)
for _batch_name, _batch_handler in batch_tool_handlers.items():
    mcp.add_tool(
        _batch_handler,
        name=_batch_name,
        description=_batch_handler.__doc__,
        structured_output=False,
    )
    mcp.configure_layer2(_batch_name)
M9_BATCH_ACTIONS = tuple(batch_tool_handlers)


def _bind_managed_board_resources(operation: ManagedOperation) -> None:
    """Bind the current connection to abnormal cleanup without changing successful state.

    A prior implementation appended an unconditional reset-and-run final-state callback to
    every ordinary operation.  That made a successful UART command, memory read, or register
    read reboot the application during cleanup and destroyed volatile state before the next
    call.  Successful actions now preserve the state their documented semantics produce.
    Explicit reset/resume tools and structured ``on_exit.reset_and_run`` remain available;
    cancellation, timeout, and started failures still close the unsafe connection below.
    """

    board_id = operation.board_id
    if board_id is None or operation.tool_name in {"connect", "disconnect", "action_batch"}:
        return
    connection = connection_manager.maybe_connection(board_id)
    if connection is None:
        return
    handle = connection.handle

    def close_failed_connection() -> None:
        failed_after_start = (
            operation.state is OperationState.FAILED and operation.handler_started_at is not None
        )
        if not operation.cancellation_requested.is_set() and not failed_after_start:
            return
        current = connection_manager.maybe_connection(board_id)
        if current is not connection:
            return
        connection_manager.clear(board_id)
        gate_manager.clear(board_id, "operation cancelled, timed out, or failed")
        try:
            target_control.close_session(handle)
        finally:
            _session_store.close_session(connection.runtime_session)

    def release_reset() -> None:
        probe = getattr(handle.session, "probe", None)
        assert_reset = getattr(probe, "assert_reset", None)
        if callable(assert_reset):
            assert_reset(False)

    operation.resources.close_debug.append(close_failed_connection)
    operation.resources.release_reset.append(release_reset)


mcp.configure_operation_resources(_bind_managed_board_resources)


def _finalizer_uart_write(board_id: str, text: str, timeout_seconds: float) -> None:
    handle = _handle(board_id)
    if handle.board is None:
        raise RuntimeError(NO_BOARD_CONFIG_MESSAGE)
    resolved_port = _resolve_serial_port_for_session(handle, override=None)
    write_uart_output(
        resolved_port.device,
        handle.board.default_baudrate,
        text.encode("utf-8"),
        timeout_seconds=timeout_seconds,
    )


def _resolve_operation_finalizer(
    tool_name: str,
    board_id: str,
    arguments: Mapping[str, object],
) -> Callable[[], None] | None:
    return build_finalizer(
        tool_name,
        board_id,
        arguments.get("on_exit"),
        uart_write=_finalizer_uart_write,
        reset_and_run=lambda selected_board: target_control.reset(
            _handle(selected_board), halt_after=False
        ),
    )


mcp.configure_finalizers(_resolve_operation_finalizer)

initialization_handshake = register_initialization_handshake(mcp, tool_registry, server_run)


def main() -> None:
    """Console entry point. Runs the server over stdio transport by default."""
    require_clean_startup()
    try:
        mcp.run()
    finally:
        for _board_id in connection_manager.assigned_board_ids():
            try:
                disconnect(_board_id)
            except Exception:
                pass
        plan_engine.close_run()
        tool_registry.reset()


if __name__ == "__main__":
    main()
