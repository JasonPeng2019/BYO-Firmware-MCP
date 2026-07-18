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
from pathlib import Path
from typing import cast

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
from pyocd_debug_mcp.kernel.operations import ManagedOperation, OperationState
from pyocd_debug_mcp.kernel.finalizers import build_finalizer
from pyocd_debug_mcp.kernel.hygiene import cleanup_stale_owned_processes
from pyocd_debug_mcp.kernel.processes import run_owned
from pyocd_debug_mcp.kernel.run_state import create_server_run
from pyocd_debug_mcp.pack_provision import load_manifest, pack_spec_document, sha256_file
from pyocd_debug_mcp.probe_inventory import list_connected_probes, resolve_probe_for_board
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
    catalog_board,
    catalog_board_for_mcu,
    catalog_board_types,
    reviewed_setup_board_types,
)
from pyocd_debug_mcp.setup_flow.reviewed_evidence import (
    load_pinned_reviewed_evidence,
    load_reviewed_evidence,
)
from pyocd_debug_mcp.setup_flow.setup import (
    SetupPhase,
    SetupPhaseContext,
    SetupPhaseOutcome,
    SetupWorkflow,
)
from pyocd_debug_mcp.setup_flow.targets import (
    ProfileCommitCoordinator,
    TargetResolutionError,
    TargetResolver,
)
from pyocd_debug_mcp.setup_flow.validate import (
    BoardValidator,
    Layer0Snapshot,
    ValidationBackend,
    ValidationHooks,
    ValidationInventory,
    ValidationProbe,
    ValidationRequest,
    ValidationSerial,
)
from pyocd_debug_mcp.safety.enforce import SafetyPolicy, SafetyPolicyError
from pyocd_debug_mcp.safety.fingerprints import (
    FingerprintInputs,
    FingerprintSet,
    FingerprintSource,
)
from pyocd_debug_mcp.safety.linker import (
    BuildArtifactSelection,
    BuildRole,
    LinkerEvidenceError,
    extract_build_evidence,
)
from pyocd_debug_mcp.safety.map_build import (
    RegionContribution,
    SafetyArtifactError,
    SafetyArtifactRepository,
    SafetyArtifacts,
    SafetyIssue,
    SafetyMapBuilder,
    SafetySetupRequest,
    build_documents,
    require_reconciled_authority,
    region_conflicts,
)
from pyocd_debug_mcp.safety.refresh import SafetyRefreshRequest, SafetyRefresher
from pyocd_debug_mcp.safety.regions import (
    ActionCategory,
    AddressRange,
    Provenance,
    Refusal,
    RegionKind,
    SafetyMap,
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

connection_manager = ConnectionManager()
gate_manager = GateManager(server_run.gates)
permission_store = PermissionStore(server_run)
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


def _check_breakpoint_safety(board_id: str, address: int) -> None:
    _safety_policy.check_breakpoint(board_id, address)


def _check_flash_safety(tool_name: str, board_id: str, artifact: Path) -> None:
    role = BuildRole.APPLICATION if tool_name == "flash_application" else BuildRole.BOOTLOADER
    _safety_policy.check_flash(
        board_id,
        role,
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
            gate_manager.require_write(board_id, connection.connection_id, aggregate)
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


def _enforce_action_containment(
    tool_name: str,
    board_id: str,
    parameters: Mapping[str, object],
) -> None:
    try:
        if tool_name == "register_write":
            _safety_policy.check_register_write(
                board_id, _parse_action_integer(parameters["address"], "address")
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
            try:
                address = _parse_action_integer(target, "symbol_or_address")
            except (TypeError, ValueError):
                if not isinstance(target, str) or not target.strip():
                    raise ValueError("symbol_or_address must be a symbol or address")
                handle = _handle(board_id)
                address = resolve_symbol(_symbol_artifact_for_handle(handle), target).address
            _safety_policy.check_breakpoint(board_id, address)
        elif tool_name in {"flash_application", "flash_bootloader"}:
            handle = _maybe_handle(board_id)
            context = _action_context(tool_name, board_id)
            request = resolve_flash_request(
                handle,
                explicit_path=cast(str, parameters["artifact"]),
                action_context=context,
            )
            role = (
                BuildRole.APPLICATION if tool_name == "flash_application" else BuildRole.BOOTLOADER
            )
            _safety_policy.check_flash(
                board_id,
                role,
                request.artifact_path,
                current_target=_current_target(board_id),
            )
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
    parameters = {
        name: value for name, value in arguments.items() if name not in {"board_id", "on_exit"}
    }

    def validate_layer0_and_action() -> None:
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
            handle = target_control.open_session(
                board=board,
                unique_id=uid,
                target=tgt,
                server_timeouts=_staged_server_timeouts,
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
            handle = target_control.connect_under_reset(
                board=board,
                unique_id=resolved_uid,
                target=resolved_target,
                server_timeouts=_staged_server_timeouts,
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
    return resolve_reference_artifacts(handle.board).symbol_artifact


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
    symbol_artifact_for=_symbol_artifact_for_handle,
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


def _infer_probe_family(text: str) -> str:
    normalized = text.casefold().replace("-", "")
    if "stlink" in normalized:
        return "stlink"
    if "jlink" in normalized:
        return "jlink"
    return "cmsis-dap"


def _target_names() -> tuple[str, ...]:
    # Use pyOCD's pinned in-process registry.  Parsing its human-formatted CLI
    # table was locale-dependent on Windows and could turn a supported target
    # into an empty inventory when a description contained non-ASCII text.
    from pyocd.target.builtin import BUILTIN_TARGETS

    names: set[str] = {str(name).casefold() for name in BUILTIN_TARGETS}
    for pack in load_manifest(_firm_store.layout.pack_manifest):
        names.update(target.casefold() for target in pack.provides_targets)
    return tuple(sorted(names))


def _validation_inventory() -> ValidationInventory:
    probes_by_id = {
        probe.uid: ValidationProbe(
            probe.uid,
            probe.description or probe.raw,
            _infer_probe_family(f"{probe.description} {probe.raw}"),
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
        probe_family = board.probe_family if board is not None else _infer_probe_family(description)
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
    return target.casefold() in set(_target_names())


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


def _validation_connect(profile, probe: ValidationProbe, timeout: float) -> object:
    del timeout
    existing = connection_manager.maybe_connection(profile.board_id)
    if existing is not None:
        return _ValidationConnection(existing.handle, False, board_id=profile.board_id)
    handle = target_control.open_session(
        board=profile.board,
        unique_id=probe.usb_serial,
        target=profile.board.pyocd_target,
        server_timeouts=_staged_server_timeouts,
        connect_mode="attach",
    )
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
        if assigned is not None and (
            stamp is None or stamp.connection_id != assigned.connection_id
        ):
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
_safety_repository = SafetyArtifactRepository(_firm_store)


def _refresh_tracked_artifact_hashes(stored: object) -> object:
    """Re-hash only paths already selected by authoritative setup evidence."""

    if not isinstance(stored, Mapping):
        return stored

    def refreshed_artifact(raw: object) -> object:
        if not isinstance(raw, Mapping):
            return raw
        updated = dict(raw)
        path_value = raw.get("path")
        if isinstance(path_value, str) and path_value.strip():
            path = Path(path_value).expanduser()
            updated["sha256"] = sha256_file(path) if path.is_file() else "missing"
        return updated

    updated = dict(stored)
    nested = stored.get("artifact")
    if nested is not None:
        updated["artifact"] = refreshed_artifact(nested)
    artifacts = stored.get("artifacts")
    if isinstance(artifacts, list):
        updated["artifacts"] = [refreshed_artifact(item) for item in artifacts]
    return updated


def _live_safety_inputs(board_id: str, artifacts) -> FingerprintInputs:
    sources = artifacts.source_manifest.get("sources")
    if not isinstance(sources, Mapping):
        raise SafetyPolicyError(
            "safety/source-manifest-invalid",
            "Safety source manifest has no source table.",
            remedy=("board_safety_setup",),
        )

    def evidence(source: FingerprintSource) -> object:
        row = sources.get(source.value)
        if not isinstance(row, Mapping) or "evidence" not in row:
            raise SafetyPolicyError(
                "safety/source-manifest-invalid",
                f"Safety source manifest has no {source.value} evidence.",
                remedy=("board_safety_setup",),
            )
        return row["evidence"]

    def update_tracked(
        stored: object,
        live: Mapping[str, object],
        *,
        preserve: frozenset[str] = frozenset(),
    ) -> object:
        if not isinstance(stored, Mapping):
            return stored
        updated = dict(stored)
        for key in stored:
            if key not in preserve and key in live:
                updated[str(key)] = live[str(key)]
        return updated

    try:
        profile = _profile_repository.load(board_id, include_legacy=False)
    except ProfileError as exc:
        raise SafetyPolicyError(
            "safety/profile-stale",
            f"The current schema-v2 profile cannot be loaded: {exc}",
            remedy=("board_setup", "board_validate"),
        ) from exc
    profile_document = profile.to_document()
    live_profile = update_tracked(
        evidence(FingerprintSource.PROFILE),
        profile_document,
        preserve=frozenset({"created_at", "updated_at", "safety_ref"}),
    )
    live_part_target = update_tracked(
        evidence(FingerprintSource.PART_TARGET),
        {
            "mcu_part_number": profile.mcu_part_number or "",
            "target": profile.board.pyocd_target,
        },
    )
    stored_pack = _refresh_tracked_artifact_hashes(evidence(FingerprintSource.PACK))
    pack = next(
        (
            candidate
            for candidate in load_manifest(_firm_store.layout.pack_manifest)
            if profile.board.pyocd_target in candidate.provides_targets
        ),
        None,
    )
    if isinstance(stored_pack, Mapping) and "id" in stored_pack:
        live_pack = (
            update_tracked(stored_pack, pack_spec_document(pack))
            if pack is not None
            else {"missing_for_target": profile.board.pyocd_target}
        )
    else:
        live_pack = stored_pack
    return FingerprintInputs(
        live_profile,
        live_part_target,
        live_pack,
        evidence(FingerprintSource.EVIDENCE),
        _refresh_tracked_artifact_hashes(evidence(FingerprintSource.APPLICATION_ARTIFACTS)),
        _refresh_tracked_artifact_hashes(evidence(FingerprintSource.BOOTLOADER_ARTIFACTS)),
        evidence(FingerprintSource.GEOMETRY),
        evidence(FingerprintSource.SCHEMA),
    )


_safety_policy = SafetyPolicy(_safety_repository, live_inputs=_live_safety_inputs)


def _restamp_after_refresh(board_id: str, aggregate: str) -> None:
    connection = connection_manager.maybe_connection(board_id)
    if connection is not None:
        gate_manager.refresh_fingerprint(board_id, connection.connection_id, aggregate)


_safety_refresher = SafetyRefresher(_firm_store, on_commit=_restamp_after_refresh)
_safety_builder = SafetyMapBuilder(_firm_store)


def _safety_continuation(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(8)}"


def _bootstrap_safety_inputs(board_id: str) -> FingerprintInputs:
    profile = _profile_repository.load(board_id, include_legacy=False)
    pack = next(
        (
            candidate
            for candidate in load_manifest(_firm_store.layout.pack_manifest)
            if profile.board.pyocd_target in candidate.provides_targets
        ),
        None,
    )
    return FingerprintInputs(
        profile.to_document(),
        {
            "mcu_part_number": profile.mcu_part_number or "",
            "target": profile.board.pyocd_target,
        },
        pack_spec_document(pack)
        if pack is not None
        else {"missing_for_target": profile.board.pyocd_target},
        {},
        {"configuration": None, "artifacts": []},
        {"configuration": None, "artifacts": []},
        {},
        {"memory_map": 1, "fingerprints": 1, "evidence": 1},
    )


def _tracked_build_selection(document: object, role: BuildRole) -> BuildArtifactSelection | None:
    if not isinstance(document, Mapping):
        raise LinkerEvidenceError(
            "build/tracked-evidence-invalid",
            f"Tracked {role.value} artifact evidence must be an object.",
        )
    rows = document.get("artifacts")
    if not isinstance(rows, list):
        raise LinkerEvidenceError(
            "build/tracked-evidence-invalid",
            f"Tracked {role.value} artifact evidence has no artifact list.",
        )
    paths: dict[str, Path] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise LinkerEvidenceError(
                "build/tracked-evidence-invalid", "Tracked artifact entries must be objects."
            )
        kind = row.get("kind")
        path = row.get("path")
        if isinstance(kind, str) and isinstance(path, str) and path.strip():
            paths[kind.casefold()] = Path(path).expanduser()
    if not paths:
        return None
    elf = paths.get("elf")
    if elf is None:
        raise LinkerEvidenceError(
            "build/tracked-elf-missing",
            f"Tracked {role.value} evidence does not include an ELF artifact.",
        )
    configuration = document.get("configuration")
    if not isinstance(configuration, str) or not configuration.strip():
        configuration = f"tracked_{role.value}"
    return BuildArtifactSelection(
        configuration,
        role,
        elf,
        linker_map_path=paths.get("map") or paths.get("linker_map"),
        hex_path=paths.get("hex"),
    )


def _build_region_replacements(
    document: object,
    role: BuildRole,
) -> tuple[RegionContribution, ...]:
    selection = _tracked_build_selection(document, role)
    evidence = extract_build_evidence(selection)
    if selection is None or not evidence.artifact_present:
        return ()
    source = (
        FingerprintSource.APPLICATION_ARTIFACTS
        if role is BuildRole.APPLICATION
        else FingerprintSource.BOOTLOADER_ARTIFACTS
    )
    provenance = tuple(
        Provenance(
            SourceAuthority.BUILD,
            item.artifact_kind,
            f"{item.path} sha256 {item.sha256}",
        )
        for item in evidence.provenance
    )
    if not provenance:
        raise LinkerEvidenceError(
            "build/tracked-provenance-missing",
            f"Tracked {role.value} evidence produced no content-addressed provenance.",
        )
    replacements: list[RegionContribution] = []
    if evidence.flash_partition is not None:
        kind = (
            RegionKind.APPLICATION_FLASH
            if role is BuildRole.APPLICATION
            else RegionKind.BOOTLOADER_FLASH
        )
        replacements.append(
            RegionContribution(
                SafetyRegion(
                    f"{role.value} flash",
                    kind,
                    evidence.flash_partition,
                    provenance,
                    executable=False,
                ),
                (source,),
            )
        )
        replacements.extend(
            RegionContribution(
                SafetyRegion(
                    f"{role.value} executable segment {segment.index}",
                    kind,
                    segment.runtime_range,
                    provenance,
                    executable=True,
                ),
                (source,),
            )
            for segment in evidence.loadable_segments
            if segment.executable and evidence.flash_partition.contains(segment.runtime_range)
        )
    # RAM authority is hardware-owned. Linker RAM remains fingerprint evidence
    # only and can never widen the reconciled writable-RAM boundary.
    return tuple(replacements)


def _run_board_safety_setup(board_id: str) -> Mapping[str, object]:
    continuation = _safety_continuation("safety-setup")
    try:
        current = _safety_repository.load_current(board_id)
        require_reconciled_authority(current)
    except (SafetyArtifactError, ValueError):
        try:
            inputs = _bootstrap_safety_inputs(board_id)
            profile = _profile_repository.load(board_id, include_legacy=False)
        except (ProfileError, ValueError) as exc:
            return {
                "status": "safety_setup_blocked",
                "agent_prompt": (
                    f"The schema-v2 board profile is unavailable: {exc}. Complete board_setup "
                    "before safety setup. Relay this guidance conversationally and do not "
                    "expose structured internals."
                ),
                "choices": [],
                "observed": {"board_id": board_id},
                "constraints": ["No gate or authority state was created."],
                "rejected_candidates": [],
                "accepted_response": None,
                "validation_plan": [
                    "board_setup-plan",
                    "board_setup",
                    "board_safety_setup",
                    "board_validate",
                ],
            }
        catalog = catalog_board_for_mcu(profile.mcu_part_number or "")
        if catalog is None or not catalog.automatic_setup_reviewed:
            reviewed = reviewed_setup_board_types()
            reviewed_text = ", ".join(reviewed) if reviewed else "none"
            result = _safety_builder.build(
                SafetySetupRequest(
                    board_id,
                    continuation,
                    inputs,
                    (),
                    (
                        SafetyIssue(
                            "safety_setup_unsupported_board",
                            "safety/unsupported-board",
                            "Automatic safety setup is unavailable for this board type. "
                            f"The reviewed automatic-safety board types are: {reviewed_text}. "
                            "Extending this list requires maintainers to add pinned device-support "
                            "and official-document evidence, exact runtime identities, deterministic "
                            "two-source reconciliation, and reviewed catalog geometry. Caller-supplied "
                            "allowed ranges are never accepted.",
                            details={
                                "board_type": (catalog.board_type if catalog is not None else None),
                                "mcu_part_number": profile.mcu_part_number,
                                "reviewed_board_types": list(reviewed),
                            },
                        ),
                    ),
                )
            )
            return result.to_payload()
        result = _safety_builder.build(
            SafetySetupRequest(
                board_id,
                continuation,
                inputs,
                (),
                (
                    SafetyIssue(
                        "safety_setup_blocked",
                        "safety/reviewed-setup-required",
                        "This reviewed board has no current authoritative safety map. Run "
                        "board_setup-plan and board_setup with the reviewed datasheet so the "
                        "server can rebuild pinned two-source evidence, then retry "
                        "board_safety_setup and board_validate. No caller-defined ranges are accepted.",
                        details={
                            "board_type": catalog.board_type,
                            "remedy": [
                                "board_setup-plan",
                                "board_setup",
                                "board_safety_setup",
                                "board_validate",
                            ],
                        },
                    ),
                ),
            )
        )
        return result.to_payload()

    live_inputs = _live_safety_inputs(board_id, current)
    changed = current.fingerprints.changed_sources(FingerprintSet.build(live_inputs))
    if changed:
        return _run_board_safety_refresh(board_id)

    result = _safety_builder.build(
        SafetySetupRequest(board_id, continuation, live_inputs, current.regions)
    )
    expected_ref = (
        _firm_store.layout.safety_reference_prefix(board_id) / "memory_map.yaml"
    ).as_posix()
    profile = _profile_repository.load(board_id, include_legacy=False)
    if result.status == "safety_setup_completed" and profile.safety_ref != expected_ref:
        _profile_repository.commit_safety_ref(
            _profile_repository.stage_safety_ref(board_id, expected_ref)
        )
    return result.to_payload()


def _artifact_refresh_selection(
    role: BuildRole,
    elf: str | None,
    hex_file: str | None,
    linker_map: str | None,
) -> BuildArtifactSelection:
    if elf is None:
        raise LinkerEvidenceError(
            f"build/{role.value}-elf-required",
            f"{role.value.title()} refresh requires an ELF path.",
        )
    return BuildArtifactSelection(
        f"runtime_{role.value}",
        role,
        Path(elf).expanduser().resolve(),
        Path(linker_map).expanduser().resolve() if linker_map else None,
        Path(hex_file).expanduser().resolve() if hex_file else None,
    )


def _artifact_requested_ranges(evidence: object) -> tuple[AddressRange, ...]:
    loadable_segments = getattr(evidence, "loadable_segments")
    requested = [
        segment.load_range for segment in loadable_segments if segment.load_range is not None
    ]
    requested.extend(segment.runtime_range for segment in loadable_segments if segment.executable)
    requested.extend(getattr(evidence, "hex_ranges"))
    flash_partition = getattr(evidence, "flash_partition")
    if flash_partition is not None:
        requested.append(flash_partition)
    for value in (getattr(evidence, "entry_point"), getattr(evidence, "vector_table")):
        if value is not None:
            requested.append(AddressRange.from_start_size(value, 1))
    return tuple(requested)


def _artifact_refresh_contributions(
    selection: BuildArtifactSelection,
    evidence: object,
) -> tuple[RegionContribution, ...]:
    source = (
        FingerprintSource.APPLICATION_ARTIFACTS
        if selection.role is BuildRole.APPLICATION
        else FingerprintSource.BOOTLOADER_ARTIFACTS
    )
    kind = (
        RegionKind.APPLICATION_FLASH
        if selection.role is BuildRole.APPLICATION
        else RegionKind.BOOTLOADER_FLASH
    )
    flash_partition = getattr(evidence, "flash_partition")
    if flash_partition is None:
        raise LinkerEvidenceError(
            "build/flash-partition-missing",
            f"The selected {selection.role.value} build has no linker-owned flash partition.",
        )
    provenance = tuple(
        Provenance(
            SourceAuthority.BUILD,
            item.artifact_kind,
            f"{item.path} sha256 {item.sha256}",
        )
        for item in getattr(evidence, "provenance")
    )
    if not provenance:
        raise LinkerEvidenceError(
            "build/provenance-missing",
            f"The selected {selection.role.value} build has no content-addressed provenance.",
        )
    partition = RegionContribution(
        SafetyRegion(
            f"{selection.role.value} flash",
            kind,
            flash_partition,
            provenance,
            executable=False,
        ),
        (source,),
    )
    executable = tuple(
        RegionContribution(
            SafetyRegion(
                f"{selection.role.value} executable segment {segment.index}",
                kind,
                segment.runtime_range,
                provenance,
                executable=True,
            ),
            (source,),
        )
        for segment in getattr(evidence, "loadable_segments")
        if segment.executable and flash_partition.contains(segment.runtime_range)
    )
    return (partition,) + executable


def _validated_artifact_refresh(
    current,
    inputs: FingerprintInputs,
    selection: BuildArtifactSelection,
) -> tuple[FingerprintSource, object, tuple[RegionContribution, ...]]:
    evidence = extract_build_evidence(selection)
    requested = _artifact_requested_ranges(evidence)
    if not requested:
        raise LinkerEvidenceError(
            "build/no-loadable-ranges",
            f"The selected {selection.role.value} build has no loadable ranges.",
        )
    if selection.role is BuildRole.APPLICATION:
        source_record = inputs.evidence
        deployment = (
            source_record.get("deployment_policy") if isinstance(source_record, Mapping) else None
        )
        if not isinstance(deployment, Mapping):
            raise LinkerEvidenceError(
                "build/deployment-policy-missing",
                "The current source manifest has no reviewed application deployment ceiling.",
            )
        envelope = AddressRange(
            int(deployment["application_start"]),
            int(deployment["application_end"]),
        )
        if any(not envelope.contains(item) for item in requested):
            raise LinkerEvidenceError(
                "build/deployment-policy-exceeded",
                "The application artifact would widen or exit the reviewed deployment envelope.",
            )
        envelope_document: object = envelope.to_document()
        source = FingerprintSource.APPLICATION_ARTIFACTS
    else:
        bootloader_regions = tuple(
            item.region
            for item in current.regions
            if item.region.kind is RegionKind.BOOTLOADER_FLASH
            and FingerprintSource.BOOTLOADER_ARTIFACTS not in item.source_groups
            and set(item.source_groups).intersection(
                {FingerprintSource.PACK, FingerprintSource.EVIDENCE}
            )
            and all(
                provenance.authority is SourceAuthority.RECONCILED
                for provenance in item.region.provenance
            )
        )
        if not bootloader_regions:
            raise LinkerEvidenceError(
                "build/bootloader-envelope-missing",
                "The current map has no server-owned reviewed bootloader partition. Refresh "
                "cannot create or widen bootloader authority; maintainers must add reviewed "
                "bootloader evidence before a full safety setup can establish it.",
            )
        prohibited = tuple(
            item.region for item in current.regions if item.region.kind is RegionKind.PROHIBITED
        )
        containment = SafetyMap([*bootloader_regions, *prohibited]).check(
            ActionCategory.FLASH_BOOTLOADER,
            requested,
        )
        if isinstance(containment, Refusal):
            raise LinkerEvidenceError(
                "build/bootloader-envelope-exceeded",
                "The bootloader artifact would widen, exit, or cross a prohibited part of the "
                f"existing reviewed bootloader partition: {containment.reason}",
            )
        envelope_document = [item.address_range.to_document() for item in bootloader_regions]
        source = FingerprintSource.BOOTLOADER_ARTIFACTS
    artifact_document = {
        "configuration": selection.configuration_id,
        "role": selection.role.value,
        "artifacts": [
            {
                "kind": item.artifact_kind,
                "path": str(item.path),
                "sha256": item.sha256,
            }
            for item in getattr(evidence, "provenance")
        ],
        "deployment_envelope": envelope_document,
    }
    return source, artifact_document, _artifact_refresh_contributions(selection, evidence)


def _blocked_artifact_refresh(
    board_id: str,
    role: BuildRole,
    exc: Exception,
    continuation: str,
    inputs: FingerprintInputs,
) -> Mapping[str, object]:
    missing_authority = getattr(exc, "code", None) == "build/bootloader-envelope-missing"
    source = (
        FingerprintSource.APPLICATION_ARTIFACTS
        if role is BuildRole.APPLICATION
        else FingerprintSource.BOOTLOADER_ARTIFACTS
    )
    return _safety_refresher.blocked(
        SafetyRefreshRequest(board_id, continuation, inputs),
        message=(
            f"The {role.value} build could not be proven inside existing server-owned safety "
            f"authority: {exc} No safety state changed. Relay this plainly and do not expose "
            "structured internals."
        ),
        classification=(
            "bootloader_authority_missing" if missing_authority else f"{role.value}_change"
        ),
        changed=(source,),
        remedy=(
            () if missing_authority else ("select_valid_build_artifact", "board_safety_refresh")
        ),
        details={"reason": str(exc), "terminal": missing_authority},
    ).to_payload()


def _manifest_source_evidence(current, source: FingerprintSource) -> object:
    sources = current.source_manifest.get("sources")
    row = sources.get(source.value) if isinstance(sources, Mapping) else None
    if not isinstance(row, Mapping) or "evidence" not in row:
        raise SafetyArtifactError(f"{source.value} source evidence is missing")
    return row["evidence"]


def _contribution_documents(
    contributions: tuple[RegionContribution, ...],
) -> tuple[Mapping[str, object], ...]:
    documents = [cast(Mapping[str, object], item.to_document()) for item in contributions]
    return tuple(
        sorted(
            documents,
            key=lambda item: (
                str(item["kind"]),
                str(item["start"]),
                str(item["end"]),
                str(item["name"]),
            ),
        )
    )


def _server_owned_hardware_authority_refresh(
    board_id: str,
    continuation: str,
    current,
) -> Mapping[str, object]:
    """Migrate stale pinned pack/evidence only after current server sources reconcile."""

    current_values = _live_safety_inputs(board_id, current).values()
    part_target = current_values[FingerprintSource.PART_TARGET]
    evidence_record = current_values[FingerprintSource.EVIDENCE]
    if not isinstance(part_target, Mapping) or not isinstance(evidence_record, Mapping):
        raise SafetyArtifactError("persisted safety anchors are malformed")
    board_type = part_target.get("board_type")
    if not isinstance(board_type, str):
        raise SafetyArtifactError("persisted safety authority has no reviewed board type")
    catalog = catalog_board(board_type)
    profile = _profile_repository.load(board_id, include_legacy=False)
    if (
        profile.mcu_part_number != catalog.package_part_number
        or profile.board.pyocd_target != catalog.pyocd_target
        or part_target.get("mcu_part_number") != catalog.package_part_number
        or part_target.get("target") != catalog.pyocd_target
    ):
        raise SafetyArtifactError(
            "board, MCU, or target anchors changed; full safety setup and validation are required"
        )
    schema = current_values[FingerprintSource.SCHEMA]
    if not isinstance(schema, Mapping) or schema.get("evidence") != 2 or schema.get("catalog") != 2:
        raise SafetyArtifactError(
            "legacy or changed safety schema requires full safety setup and validation"
        )
    official = evidence_record.get("official_document")
    digest = official.get("datasheet_sha256") if isinstance(official, Mapping) else None
    if not isinstance(digest, str):
        raise SafetyArtifactError("persisted official evidence has no reviewed datasheet digest")
    bundle = load_pinned_reviewed_evidence(catalog, digest)
    source_record = bundle.source_record()
    geometry = bundle.reconciliation.erase_geometry
    if geometry is None:
        raise SafetyArtifactError("current pinned evidence has no reconciled erase geometry")
    current_values[FingerprintSource.PACK] = source_record["device_support"]
    current_values[FingerprintSource.EVIDENCE] = {
        "official_document": source_record["official_document"],
        "reconciliation": source_record["reconciliation"],
        "deployment_policy": {
            "application_start": catalog.application_start,
            "application_end": catalog.application_end,
        },
    }
    current_values[FingerprintSource.GEOMETRY] = {
        "flash_start": catalog.flash_start,
        "flash_end": catalog.flash_end,
        "ram_start": catalog.ram_start,
        "ram_end": catalog.ram_end,
        "erase_origin": geometry.erase_origin,
        "erase_size": geometry.erase_size,
    }
    candidate = FingerprintInputs(
        current_values[FingerprintSource.PROFILE],
        current_values[FingerprintSource.PART_TARGET],
        current_values[FingerprintSource.PACK],
        current_values[FingerprintSource.EVIDENCE],
        current_values[FingerprintSource.APPLICATION_ARTIFACTS],
        current_values[FingerprintSource.BOOTLOADER_ARTIFACTS],
        current_values[FingerprintSource.GEOMETRY],
        current_values[FingerprintSource.SCHEMA],
    )
    changed = current.fingerprints.changed_sources(FingerprintSet.build(candidate))
    changed_set = set(changed)
    if changed_set.intersection(
        {
            FingerprintSource.PART_TARGET,
            FingerprintSource.GEOMETRY,
            FingerprintSource.SCHEMA,
        }
    ):
        return _safety_refresher.blocked(
            SafetyRefreshRequest(board_id, continuation, candidate),
            message=(
                "Current pinned evidence changes a board anchor, erase geometry, or schema. "
                "Run full board_safety_setup and board_validate; scoped refresh did not commit."
            ),
            classification=(
                "anchor_change"
                if FingerprintSource.PART_TARGET in changed_set
                else "geometry_change"
                if FingerprintSource.GEOMETRY in changed_set
                else "schema_change"
            ),
            changed=changed,
            remedy=("board_safety_setup", "board_validate"),
        ).to_payload()
    if not changed_set or not changed_set.issubset(
        {FingerprintSource.PACK, FingerprintSource.EVIDENCE}
    ):
        raise SafetyArtifactError("stale authority cannot be safely scoped to pack/evidence drift")

    authority_groups = {
        FingerprintSource.PACK,
        FingerprintSource.EVIDENCE,
        FingerprintSource.GEOMETRY,
    }
    retained = tuple(
        item for item in current.regions if not authority_groups.intersection(item.source_groups)
    )
    for source, role in (
        (FingerprintSource.APPLICATION_ARTIFACTS, BuildRole.APPLICATION),
        (FingerprintSource.BOOTLOADER_ARTIFACTS, BuildRole.BOOTLOADER),
    ):
        document = current_values[source]
        rows = document.get("artifacts") if isinstance(document, Mapping) else None
        expected = () if rows == [] else _build_region_replacements(document, role)
        observed = tuple(item for item in retained if source in item.source_groups)
        if _contribution_documents(observed) != _contribution_documents(expected):
            raise SafetyArtifactError(
                f"persisted {role.value} regions cannot be reproduced from tracked artifacts"
            )
    if any(
        not set(item.source_groups).intersection(
            {
                FingerprintSource.APPLICATION_ARTIFACTS,
                FingerprintSource.BOOTLOADER_ARTIFACTS,
            }
        )
        for item in retained
    ):
        raise SafetyArtifactError("persisted non-hardware regions have unclear ownership")

    replacements = tuple(
        RegionContribution(
            region.to_safety_region(),
            (
                FingerprintSource.PACK,
                FingerprintSource.EVIDENCE,
                FingerprintSource.GEOMETRY,
            ),
        )
        for region in bundle.reconciliation.regions
    )
    retained_bootloader = tuple(
        item.region
        for item in retained
        if FingerprintSource.BOOTLOADER_ARTIFACTS in item.source_groups
    )
    if retained_bootloader:
        reviewed_bootloader = tuple(
            item.region for item in replacements if item.region.kind is RegionKind.BOOTLOADER_FLASH
        )
        prohibited = tuple(
            item.region for item in replacements if item.region.kind is RegionKind.PROHIBITED
        )
        if not reviewed_bootloader:
            raise SafetyArtifactError(
                "current pinned evidence has no independent bootloader envelope for the retained build"
            )
        bootloader_map = SafetyMap([*reviewed_bootloader, *prohibited])
        for region in retained_bootloader:
            containment = bootloader_map.check(
                ActionCategory.FLASH_BOOTLOADER,
                (region.address_range,),
            )
            if isinstance(containment, Refusal):
                raise SafetyArtifactError(
                    "retained bootloader build exceeds the newly reconciled bootloader envelope"
                )
    final_regions = tuple(
        sorted(
            (*retained, *replacements),
            key=lambda item: (
                item.region.address_range.start,
                item.region.address_range.end,
                item.region.kind.value,
                item.region.name,
            ),
        )
    )
    conflicts = region_conflicts(final_regions)
    if conflicts:
        raise SafetyArtifactError("current pinned evidence conflicts with retained build regions")
    candidate_fingerprints = FingerprintSet.build(candidate)
    preflight_request = SafetySetupRequest(
        board_id,
        continuation,
        candidate,
        final_regions,
    )
    memory, manifest, _report = build_documents(
        preflight_request,
        candidate_fingerprints,
        status="safety_refresh_preflight",
        prompt="Server-owned candidate authority preflight.",
    )
    require_reconciled_authority(
        SafetyArtifacts(
            board_id,
            candidate_fingerprints,
            final_regions,
            memory,
            manifest,
        )
    )
    migration_refresher = SafetyRefresher(
        _firm_store,
        on_commit=_restamp_after_refresh,
        authority_verifier=lambda _artifacts: None,
    )
    result = migration_refresher.refresh(
        SafetyRefreshRequest(
            board_id,
            continuation,
            candidate,
            (FingerprintSource.PACK, FingerprintSource.EVIDENCE),
            replacements,
        )
    )
    return result.to_payload()


def _run_board_safety_refresh(
    board_id: str,
    *,
    application_elf: str | None = None,
    application_hex: str | None = None,
    application_map: str | None = None,
    bootloader_elf: str | None = None,
    bootloader_hex: str | None = None,
    bootloader_map: str | None = None,
) -> Mapping[str, object]:
    continuation = _safety_continuation("safety-refresh")
    try:
        current = _safety_repository.load_current(board_id)
    except (SafetyArtifactError, ValueError):
        inputs = _bootstrap_safety_inputs(board_id)
        return _safety_refresher.refresh(
            SafetyRefreshRequest(board_id, continuation, inputs)
        ).to_payload()
    try:
        require_reconciled_authority(current)
    except SafetyArtifactError as stale_exc:
        try:
            return _server_owned_hardware_authority_refresh(
                board_id,
                continuation,
                current,
            )
        except (
            BoardCatalogError,
            LinkerEvidenceError,
            ProfileError,
            SafetyArtifactError,
            ValueError,
        ) as exc:
            values = {
                source: _manifest_source_evidence(current, source) for source in FingerprintSource
            }
            inputs = FingerprintInputs(
                values[FingerprintSource.PROFILE],
                values[FingerprintSource.PART_TARGET],
                values[FingerprintSource.PACK],
                values[FingerprintSource.EVIDENCE],
                values[FingerprintSource.APPLICATION_ARTIFACTS],
                values[FingerprintSource.BOOTLOADER_ARTIFACTS],
                values[FingerprintSource.GEOMETRY],
                values[FingerprintSource.SCHEMA],
            )
            return _safety_refresher.blocked(
                SafetyRefreshRequest(board_id, continuation, inputs),
                message=(
                    "Pack or official-evidence authority could not be migrated from current "
                    "server-owned reviewed sources. The old map remains closed; resolve the "
                    "maintainer evidence issue, then run board_safety_refresh again."
                ),
                classification="pack_or_official_evidence_blocked",
                changed=(FingerprintSource.PACK, FingerprintSource.EVIDENCE),
                remedy=("resolve_reviewed_safety_sources", "board_safety_refresh"),
                details={"reason": str(exc), "stale_reason": str(stale_exc)},
            ).to_payload()
    try:
        live_inputs = _live_safety_inputs(board_id, current)
    except (SafetyPolicyError, ProfileError, ValueError):
        inputs = _bootstrap_safety_inputs(board_id)
        return _safety_refresher.refresh(
            SafetyRefreshRequest(board_id, continuation, inputs)
        ).to_payload()

    provided = {
        BuildRole.APPLICATION: (application_elf, application_hex, application_map),
        BuildRole.BOOTLOADER: (bootloader_elf, bootloader_hex, bootloader_map),
    }
    selected_roles = [role for role, paths in provided.items() if any(paths)]
    if selected_roles:
        values = live_inputs.values()
        replacements_by_source: dict[FingerprintSource, tuple[RegionContribution, ...]] = {}
        failing_role = selected_roles[0]
        try:
            for role in selected_roles:
                failing_role = role
                elf, hex_file, linker_map = provided[role]
                selection = _artifact_refresh_selection(role, elf, hex_file, linker_map)
                source, document, replacements = _validated_artifact_refresh(
                    current, live_inputs, selection
                )
                values[source] = document
                replacements_by_source[source] = replacements
        except (LinkerEvidenceError, OSError, ValueError) as exc:
            return _blocked_artifact_refresh(
                board_id,
                failing_role,
                exc,
                continuation,
                live_inputs,
            )
        updated_inputs = FingerprintInputs(
            values[FingerprintSource.PROFILE],
            values[FingerprintSource.PART_TARGET],
            values[FingerprintSource.PACK],
            values[FingerprintSource.EVIDENCE],
            values[FingerprintSource.APPLICATION_ARTIFACTS],
            values[FingerprintSource.BOOTLOADER_ARTIFACTS],
            values[FingerprintSource.GEOMETRY],
            values[FingerprintSource.SCHEMA],
        )
        changed = set(current.fingerprints.changed_sources(FingerprintSet.build(updated_inputs)))
        rebuilt = tuple(source for source in replacements_by_source if source in changed)
        replacements = tuple(
            contribution
            for source, contributions in replacements_by_source.items()
            if source in changed
            for contribution in contributions
        )
        return _safety_refresher.refresh(
            SafetyRefreshRequest(
                board_id,
                continuation,
                updated_inputs,
                rebuilt,
                replacements,
            )
        ).to_payload()

    changed = current.fingerprints.changed_sources(FingerprintSet.build(live_inputs))
    changed_set = set(changed)
    build_sources = {
        FingerprintSource.APPLICATION_ARTIFACTS,
        FingerprintSource.BOOTLOADER_ARTIFACTS,
    }
    rebuilt: tuple[FingerprintSource, ...] = ()
    replacements: tuple[RegionContribution, ...] = ()
    if changed_set and changed_set.issubset(build_sources):
        rebuilt = changed
        values = live_inputs.values()
        try:
            if FingerprintSource.APPLICATION_ARTIFACTS in changed_set:
                replacements += _build_region_replacements(
                    values[FingerprintSource.APPLICATION_ARTIFACTS], BuildRole.APPLICATION
                )
            if FingerprintSource.BOOTLOADER_ARTIFACTS in changed_set:
                replacements += _build_region_replacements(
                    values[FingerprintSource.BOOTLOADER_ARTIFACTS], BuildRole.BOOTLOADER
                )
        except LinkerEvidenceError:
            rebuilt = ()
            replacements = ()
    return _safety_refresher.refresh(
        SafetyRefreshRequest(
            board_id,
            continuation,
            live_inputs,
            rebuilt,
            replacements,
        )
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


def _missing_base_safety_kinds(regions: tuple[RegionContribution, ...]) -> tuple[str, ...]:
    present = {item.region.kind for item in regions}
    return tuple(sorted(kind.value for kind in _REQUIRED_BASE_SAFETY_KINDS - present))


def _load_validation_layer0(profile) -> Layer0Snapshot:
    expected_ref = (
        _firm_store.layout.safety_reference_prefix(profile.board_id) / "memory_map.yaml"
    ).as_posix()
    if profile.safety_ref != expected_ref:
        return Layer0Snapshot(
            False,
            False,
            reason=(
                "The profile has no current safety-map reference. Run board_safety_setup, "
                "complete the safety-reference commit, then run board_validate."
            ),
        )
    try:
        artifacts = _safety_repository.load_current(profile.board_id)
        missing_kinds = _missing_base_safety_kinds(artifacts.regions)
        if missing_kinds:
            return Layer0Snapshot(
                True,
                False,
                reason=(
                    "The safety map lacks required base classifications "
                    f"{', '.join(missing_kinds)}. Run board_safety_setup with complete "
                    "authoritative evidence."
                ),
            )
        conflicts = region_conflicts(artifacts.regions)
        if conflicts:
            return Layer0Snapshot(
                True,
                False,
                artifacts.fingerprints.aggregate,
                "The current safety map contains overlapping prohibited or ambiguous regions. "
                "Resolve the evidence and run board_safety_setup.",
            )
        aggregate = _safety_policy.current_aggregate(profile.board_id)
    except (SafetyArtifactError, SafetyPolicyError, ValueError) as exc:
        return Layer0Snapshot(
            False,
            False,
            reason=f"Safety map is missing, stale, or inconsistent: {exc}",
        )
    return Layer0Snapshot(True, True, aggregate, "Safety map and source fingerprints agree.")


def _stamp_validation_session(
    board_id: str,
    hardware_result: str,
    probe_id: str,
    probe_uid: str | None,
    aggregate_fingerprint: str,
) -> bool:
    connection = connection_manager.maybe_connection(board_id)
    stable_probe = (probe_uid or probe_id).strip()
    if (
        connection is not None
        and connection.handle.probe_uid
        and connection.handle.probe_uid.casefold() != stable_probe.casefold()
    ):
        return False
    connection_id = (
        connection.connection_id if connection is not None else f"probe:{stable_probe.casefold()}"
    )
    gate_manager.stamp_validation(
        board_id=board_id,
        connection_id=connection_id,
        hardware_result=hardware_result,
        probe_identity=stable_probe,
        aggregate_fingerprint=aggregate_fingerprint,
    )
    return True


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
    cache=_attachment_cache,
    hooks=ValidationHooks(_load_validation_layer0, _stamp_validation_session),
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


def _part_matches_target(mcu_part_number: str, target: str) -> bool:
    """Accept only exact target identity or documented target wildcard/prefix forms."""

    part = _normalized_target_identity(mcu_part_number)
    normalized_target = _normalized_target_identity(target)
    if part == normalized_target:
        return True
    if part.startswith(normalized_target):
        return True
    return (
        normalized_target.endswith("x")
        and len(part) == len(normalized_target)
        and part[:-1] == normalized_target[:-1]
    )


_setup_research = ResearchTracker()
_setup_target_overrides: dict[str, str] = {}
_setup_selections_by_board: dict[str, PreflightSelections] = {}
_setup_pack_pipelines: dict[tuple[str, str], PackCandidatePipeline] = {}


def _setup_inventory(user_input: SetupUserInput) -> PreflightInventory:
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
    part_identity = _normalized_target_identity(user_input.mcu_part_number)
    exact: tuple[str, ...] = tuple(
        target for target in targets if _normalized_target_identity(target) == part_identity
    )
    # A fresh profile has no board YAML yet, but a complete reviewed catalog entry is itself
    # authoritative for the exact pyOCD target. Package suffixes such as ``-QIAA`` must not
    # force an unnecessary agent research round trip when the exact built-in target is present.
    try:
        catalog = catalog_board(user_input.board_type)
    except BoardCatalogError:
        catalog = None
    if (
        catalog is not None
        and user_input.mcu_part_number == catalog.package_part_number
        and catalog.pyocd_target.casefold() in targets
    ):
        exact = (catalog.pyocd_target.casefold(),)
    try:
        official_board = resolve_board_config(user_input.board_id, None)
    except ConfigError:
        official_board = None
    if official_board is not None:
        mapped_target = official_board.pyocd_target.casefold()
        if mapped_target in targets and _part_matches_target(
            user_input.mcu_part_number, mapped_target
        ):
            exact = (mapped_target,)
    manifest_targets = tuple(
        sorted(
            {
                target
                for pack in load_manifest(_firm_store.layout.pack_manifest)
                for target in pack.provides_targets
            }
        )
    )
    target_override = _setup_target_overrides.get(user_input.board_id)
    if target_override is not None:
        supported = set(targets) | set(manifest_targets)
        if target_override in supported and _part_matches_target(
            user_input.mcu_part_number, target_override
        ):
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
        catalog = catalog_board(context.user_input.board_type)
        if context.user_input.mcu_part_number != catalog.package_part_number:
            raise BoardCatalogError(
                f"MCU part '{context.user_input.mcu_part_number}' is not the exact reviewed "
                f"package '{catalog.package_part_number}' for {catalog.board_type}. Ask for "
                "the package marking; do not guess or rewrite it."
            )
        datasheet = Path(context.user_input.datasheet_path).expanduser().resolve()
        actual_datasheet_hash = sha256_file(datasheet)
        supplied_datasheet_hash = context.user_input.datasheet_sha256.strip()
        if supplied_datasheet_hash and (
            actual_datasheet_hash.casefold() != supplied_datasheet_hash.casefold()
        ):
            raise BoardCatalogError("The supplied datasheet SHA-256 does not match the PDF.")
        catalog.validate_datasheet(datasheet, actual_datasheet_hash)
    except (BoardCatalogError, OSError, ValueError) as exc:
        return SetupPhaseOutcome.stop(
            "setup_blocked",
            "setup/catalog-evidence-mismatch",
            f"Board, MCU, or datasheet evidence did not match reviewed setup support: {exc}",
        )
    try:
        existing = _profile_repository.load(context.user_input.board_id, include_legacy=False)
        return SetupPhaseOutcome.success(
            "setup/core-profile-already-committed", profile=existing.source_path.name
        )
    except ProfileError:
        pass
    target = context.preflight.selected_target
    probe = context.preflight.selected_probe
    if target is None or probe is None:
        return SetupPhaseOutcome.stop(
            "setup_connection_failed",
            "setup/connection-input-missing",
            "Target or probe resolution is incomplete; stop before committing a profile.",
        )
    if target.casefold() != catalog.pyocd_target or probe.probe_family != catalog.probe_family:
        return SetupPhaseOutcome.stop(
            "setup_connection_failed",
            "setup/catalog-route-mismatch",
            "The selected target or probe family does not match the reviewed board type; "
            "stop before committing a profile.",
            details={
                "selected_target": target,
                "expected_target": catalog.pyocd_target,
                "selected_probe_family": probe.probe_family,
                "expected_probe_family": catalog.probe_family,
            },
        )

    opened: list[TargetSessionHandle] = []

    def connect(candidate_target: str, _pack_path: str | None) -> None:
        handle = target_control.open_session(
            board=None,
            unique_id=probe.usb_serial,
            target=candidate_target,
            server_timeouts=_staged_server_timeouts,
        )
        opened.append(handle)
        try:
            if catalog.silicon_id_address is not None:
                observed = target_control.read_memory(handle, catalog.silicon_id_address, 32)
                expected = catalog.silicon_id_expected
                assert expected is not None
                if (observed & catalog.silicon_id_mask) != (expected & catalog.silicon_id_mask):
                    raise BoardCatalogError(
                        "Live silicon identity did not match the reviewed board catalog "
                        f"(observed 0x{observed:08X}, expected 0x{expected:08X})."
                    )
            target_control.read_memory(handle, catalog.test_read_address, 32)
        finally:
            target_control.close_session(handle)

    coordinator = ProfileCommitCoordinator(_profile_repository, live_connect=connect)
    try:
        committed = coordinator.commit_core(
            {
                "board_id": context.user_input.board_id,
                "display_name": context.user_input.display_name,
                "mcu_part_number": context.user_input.mcu_part_number,
                "mcu_family": _mcu_family(context.user_input.mcu_part_number, target),
                "probe_family": probe.probe_family,
                "pyocd_target": target,
                "serial_baudrate": context.user_input.serial_baudrate,
            }
        )
        committed = _profile_repository.commit_optional(
            _profile_repository.stage_optional(
                context.user_input.board_id,
                {
                    "test_read_address": catalog.test_read_address,
                    **(
                        {"debug_connect_mode": catalog.debug_connect_mode}
                        if catalog.debug_connect_mode is not None
                        else {}
                    ),
                    **(
                        {"debug_clock_hz": catalog.debug_clock_hz}
                        if catalog.debug_clock_hz is not None
                        else {}
                    ),
                    **(
                        {
                            "silicon_id_address": catalog.silicon_id_address,
                            "silicon_id_expected": catalog.silicon_id_expected,
                            "silicon_id_mask": catalog.silicon_id_mask,
                            "silicon_id_label": "FICR.INFO.PART",
                        }
                        if catalog.silicon_id_address is not None
                        else {}
                    ),
                },
            )
        )
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
        board_type=catalog.board_type,
        datasheet_sha256=actual_datasheet_hash,
    )


def _setup_validation_phase(context: SetupPhaseContext) -> SetupPhaseOutcome:
    result = _board_validator.validate(ValidationRequest(context.user_input.board_id))
    if result.status in {
        "validation_passed",
        "validation_passed_uart_not_configured",
    } or (
        result.status == "validation_incomplete"
        and result.code == "validation/safety-missing"
        and str(result.observed.get("hardware_result", "")).startswith("validation_passed")
    ):
        return SetupPhaseOutcome.success(
            "setup/non-destructive-hardware-validation-passed",
            validation_status=result.status,
            validation_report=str(result.report_paths.report),
        )
    return SetupPhaseOutcome.stop(
        "setup_validation_failed",
        "setup/validation-failed",
        result.agent_prompt,
        details={"validation_status": result.status, "validation_code": result.code},
    )


def _build_automatic_catalog_safety(context: SetupPhaseContext):
    """Build a base map only after pinned independent authorities agree."""

    catalog = catalog_board(context.user_input.board_type)
    profile = _profile_repository.load(context.user_input.board_id, include_legacy=False)
    datasheet = Path(context.user_input.datasheet_path).expanduser().resolve()
    digest = sha256_file(datasheet)
    if profile.mcu_part_number != catalog.package_part_number:
        raise BoardCatalogError(
            "profile MCU part number is not the exact reviewed package variant; repair the "
            "profile from the user's exact package marking before safety setup"
        )
    evidence = load_reviewed_evidence(catalog, datasheet, digest)
    erase_geometry = evidence.reconciliation.erase_geometry
    if erase_geometry is None:  # fail closed even if a future reconciler changes accepted semantics
        raise BoardCatalogError("reviewed erase geometry did not reconcile")
    source_record = evidence.source_record()
    inputs = FingerprintInputs(
        profile.to_document(),
        {
            "board_type": catalog.board_type,
            "mcu_part_number": profile.mcu_part_number or "",
            "package_part_number": catalog.package_part_number,
            "target": catalog.pyocd_target,
        },
        source_record["device_support"],
        {
            "official_document": source_record["official_document"],
            "reconciliation": source_record["reconciliation"],
            "deployment_policy": {
                "application_start": catalog.application_start,
                "application_end": catalog.application_end,
            },
        },
        {"configuration": None, "artifacts": []},
        {"configuration": None, "artifacts": []},
        {
            "flash_start": catalog.flash_start,
            "flash_end": catalog.flash_end,
            "ram_start": catalog.ram_start,
            "ram_end": catalog.ram_end,
            "erase_origin": erase_geometry.erase_origin,
            "erase_size": erase_geometry.erase_size,
        },
        {"memory_map": 1, "fingerprints": 1, "evidence": 2, "catalog": 2},
    )
    # The catalog deployment envelope is a ceiling used to validate a later
    # selected linker/ELF.  It is intentionally not persisted as an application
    # partition: New Brain makes firmware partitions build-owned, and flash must
    # remain unavailable until board_safety_refresh selects real build evidence.
    regions = tuple(
        RegionContribution(
            region.to_safety_region(),
            (FingerprintSource.EVIDENCE, FingerprintSource.GEOMETRY),
        )
        for region in evidence.reconciliation.regions
    )
    result = _safety_builder.build(
        SafetySetupRequest(
            context.user_input.board_id,
            _safety_continuation("automatic-safety-setup"),
            inputs,
            regions,
        )
    )
    if result.status != "safety_setup_completed":
        raise SafetyArtifactError(result.agent_prompt)
    return _safety_repository.load_current(context.user_input.board_id)


def _setup_safety_research_phase(context: SetupPhaseContext) -> SetupPhaseOutcome:
    try:
        artifacts = _safety_repository.load_current(context.user_input.board_id)
        require_reconciled_authority(artifacts)
    except (SafetyArtifactError, ValueError) as exc:
        try:
            artifacts = _build_automatic_catalog_safety(context)
        except (
            BoardCatalogError,
            ProfileError,
            SafetyArtifactError,
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
    sources = artifacts.source_manifest.get("sources")
    source_groups = sorted(str(source) for source in sources) if isinstance(sources, dict) else []
    return SetupPhaseOutcome.success(
        "setup/safety-sources-verified",
        aggregate_fingerprint=artifacts.fingerprints.aggregate,
        source_groups=source_groups,
    )


def _setup_safety_map_phase(context: SetupPhaseContext) -> SetupPhaseOutcome:
    try:
        artifacts = _safety_repository.load_current(context.user_input.board_id)
        require_reconciled_authority(artifacts)
        conflicts = region_conflicts(artifacts.regions)
    except (SafetyArtifactError, ValueError) as exc:
        return SetupPhaseOutcome.stop(
            "setup_safety_incomplete",
            "setup/safety-map-incomplete",
            "The safety map is incomplete. Run board_safety_setup before validation.",
            details={"reason": str(exc)},
        )
    if conflicts:
        return SetupPhaseOutcome.stop(
            "setup_safety_incomplete",
            "setup/safety-map-conflict",
            "The safety map has a region conflict. Resolve the reported safety sources and "
            "rerun board_safety_setup.",
            details={"conflicts": conflicts},
        )
    missing_kinds = _missing_base_safety_kinds(artifacts.regions)
    if missing_kinds:
        return SetupPhaseOutcome.stop(
            "setup_safety_incomplete",
            "setup/safety-map-kinds-missing",
            "The safety map lacks required prohibited, CPU/system, peripheral, flash, or RAM "
            "classifications. Resolve the authoritative evidence and rerun board_safety_setup.",
            details={"missing_kinds": list(missing_kinds)},
        )
    return SetupPhaseOutcome.success(
        "setup/safety-map-consistent",
        aggregate_fingerprint=artifacts.fingerprints.aggregate,
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
            profile = _profile_repository.commit_safety_ref(
                _profile_repository.stage_safety_ref(board_id, expected_ref)
            )
        probe = context.preflight.selected_probe
        serial = context.preflight.selected_serial
        result = _board_validator.validate(
            ValidationRequest(
                board_id,
                probe.probe_id if probe is not None else None,
                serial.serial_id if serial is not None else None,
            )
        )
    except Exception as exc:  # noqa: BLE001 - setup records the terminal report
        return SetupPhaseOutcome.stop(
            "setup_validation_failed",
            "setup/safety-reference-commit-failed",
            f"Safety reference or final validation failed: {exc}",
        )
    if result.status not in {
        "validation_passed",
        "validation_passed_uart_not_configured",
    }:
        return SetupPhaseOutcome.stop(
            "setup_validation_failed",
            "setup/final-validation-failed",
            result.agent_prompt,
            details={"validation_status": result.status, "validation_code": result.code},
        )
    return SetupPhaseOutcome.success(
        "setup/safety-reference-committed-and-validated",
        safety_ref=profile.safety_ref,
        validation_status=result.status,
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
        elif missing_kinds := _missing_base_safety_kinds(artifacts.regions):
            configuration_reason = (
                "current safety map lacks required base classifications: "
                + ", ".join(missing_kinds)
            )
        else:
            aggregate = _safety_policy.current_aggregate(board_id)
            configuration_ready = True
            configuration_reason = "profile and safety evidence are current"
    except (ProfileError, SafetyArtifactError, SafetyPolicyError, ValueError) as exc:
        configuration_reason = str(exc)

    connection = connection_manager.maybe_connection(board_id)
    stamp = gate_manager.snapshot(board_id)
    live_session_ready = bool(
        configuration_ready
        and aggregate is not None
        and connection is not None
        and stamp is not None
        and stamp.connection_id == connection.connection_id
        and stamp.aggregate_fingerprint == aggregate
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
        remedy = "Complete board_setup and authoritative safety setup."
    elif connection is None:
        remedy = "Connect this board and run board_validate in the current Server Run."
    elif not live_session_ready:
        remedy = "Run board_validate for the current board connection."
    else:
        remedy = "Setup is ready; normal guarded plans may now be used."
    build_guidance: dict[str, object] | None = None
    if profile is not None and profile.mcu_part_number:
        build_guidance = {
            "authority": "advisory_only",
            "primary_workflow": "native_project_build",
            "guidance": (
                "Reuse the project's validated local IDE or CLI build and its existing SDK. "
                "Do not download or change toolchains merely to match this server."
            ),
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
                "Build guidance is not safety authority; board_safety_refresh must inspect "
                "the resulting ELF and map before flash_application."
            ),
            "toolchain_fallback": None,
        }
        catalog = catalog_board_for_mcu(profile.mcu_part_number)
        if catalog is not None and catalog.zephyr_board_target:
            target = catalog.zephyr_board_target
            build_argv = [
                sys.executable,
                "-m",
                "pyocd_debug_mcp.zephyr_build",
                "--app-dir",
                "<app-dir>",
                "--build-dir",
                "<build-dir>",
                "--board",
                target,
            ]
            powershell_command = "& " + " ".join(
                "'" + item.replace("'", "''") + "'" for item in build_argv
            )
            build_guidance["toolchain_fallback"] = {
                "provider": "zephyr_west",
                "use_when": (
                    "Use only when this project is a Zephyr application and no compatible "
                    "local project build command is already available."
                ),
                "zephyr_board_target": target,
                "recommended_argv": build_argv,
                "recommended_command": (
                    powershell_command if sys.platform == "win32" else shlex.join(build_argv)
                ),
                "recommended_powershell": powershell_command,
                "reason": (
                    "This optional parameterized fallback reuses or bootstraps a Zephyr workspace; "
                    "it is not the generic build route and is not an MCP hardware action."
                ),
            }
    return {
        "status": "setup_ready" if live_session_ready else "setup_not_ready",
        "board_id": board_id,
        "configuration_ready": configuration_ready,
        "live_session_ready": live_session_ready,
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


def _setup_overview(board_names: list[str] | None) -> Mapping[str, object]:
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
    by_name: dict[str, tuple[BoardProfile, bool, str]] = {}
    for profile in profiles:
        complete = False
        reason = "legacy or incomplete profile; repair is required"
        if not profile.read_only and profile.safety_ref:
            try:
                artifacts = _safety_repository.load_current(profile.board_id)
                require_reconciled_authority(artifacts)
                complete = not region_conflicts(artifacts.regions)
                reason = (
                    "profile and safety map are present; validate this run"
                    if complete
                    else "safety map has conflicts; repair or full safety setup is required"
                )
            except (SafetyArtifactError, ValueError) as exc:
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
        by_name[_profile_name_key(profile.display_name)] = (profile, complete, reason)

    try:
        inventory = _validation_inventory()
        connection_rows = [
            {
                "connection_id": f"probe:{probe.usb_serial or probe.probe_id}",
                "friendly_name": ProbeCandidate(
                    probe.probe_id,
                    probe.description,
                    probe.probe_family,
                    probe.usb_serial,
                ).friendly_label(),
                "probe_family": probe.probe_family,
            }
            for probe in inventory.probes
        ]
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

    if board_names is not None and not no_board_sentinel:
        existing_ids = {profile.board_id for profile in profiles}
        setup_definition = PLAN_DEFINITIONS["board_setup"]
        for name, key in validated_names:
            match = by_name.get(key)
            if match is None:
                board_id = _proposed_board_id(name, existing_ids)
                existing_ids.add(board_id)
                single_connection = (
                    connection_rows[0]["connection_id"] if len(connection_rows) == 1 else None
                )
                single_serial = serial_rows[0]["choice_id"] if len(serial_rows) == 1 else None
                known_parameters: dict[str, object] = {
                    "mode": "setup",
                    "connection_id": single_connection,
                    "display_name": name,
                    "board_type": None,
                    "mcu_part_number": None,
                    "serial_baudrate": None,
                    "serial_id": single_serial,
                    "datasheet_path": None,
                    "datasheet_sha256": None,
                }
                parameter_template = {
                    field.name: known_parameters.get(field.name)
                    for field in setup_definition.action_fields
                }
                required_user_facts = [
                    "exact board type",
                    "exact package-level MCU part number (full package marking)",
                    "authoritative local datasheet PDF",
                    "UART baud rate used by this firmware",
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
                    required_user_facts.append("attach and identify the board's UART connection")
                elif len(serial_rows) > 1:
                    required_user_facts.append("which friendly UART choice belongs to this board")
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
            profile, _complete, reason = match
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
                        "arguments": {"board_id": profile.board_id},
                    },
                }
            )

    if board_names == [] or (no_board_sentinel and len(validated_names) == 1):
        status = "setup_no_board"
        prompt = (
            "The user reported no connected boards using the literal 'no board' sentinel. "
            "Do not begin setup, validation, or hardware access."
        )
    elif no_board_sentinel:
        status = "setup_names_clarification_required"
        prompt = (
            "The literal 'no board' sentinel was mixed with board names. Ask again in ordinary "
            "language whether no board is connected or, instead, for the familiar name of each "
            "connected board. Do not route or access hardware until the answer is unambiguous."
        )
    elif board_names is None:
        status = "setup_names_required"
        prompt = (
            "Ask the user in ordinary language for one unique familiar name for every connected "
            "board, or the literal sentinel 'no board' by itself. Then call setup_overview again "
            "with that answer. Do not show this JSON, board IDs, connection IDs, or machine "
            "identifiers."
        )
    else:
        status = "setup_routes_ready"
        prompt = (
            "Use each route's machine-readable calls without asking the user for their internal "
            "values. For validate, copy load_call and next_call. For any matching profile, always "
            "validate first and follow only its exact remedy. For unknown-name setup, copy "
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
        "known_board_types": list(catalog_board_types()),
        "supported_reviewed_board_types": list(reviewed_setup_board_types()),
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


def _enumerate_pack_targets(path: Path) -> tuple[str, ...]:
    from pyocd.target import normalise_target_type_name  # type: ignore[import-untyped]

    pack = CmsisPack(path)
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

    def live_connect(target: str, _path: Path) -> None:
        handle = target_control.open_session(
            board=None,
            unique_id=probe_uid,
            target=target,
            server_timeouts=_staged_server_timeouts,
        )
        target_control.close_session(handle)

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
        if decision.code == "setup/ambiguous-probe":
            selected = PreflightSelections(
                choice_id,
                previous.serial_id,
                previous.build_configuration_id,
                previous.external_adapter_confirmed,
            )
        elif decision.code in {"setup/ambiguous-uart", "setup/external-uart-confirmation"}:
            selected = PreflightSelections(
                previous.probe_id,
                choice_id,
                previous.build_configuration_id,
                decision.code == "setup/external-uart-confirmation"
                or previous.external_adapter_confirmed,
            )
        elif decision.code == "setup/ambiguous-build":
            selected = PreflightSelections(
                previous.probe_id,
                previous.serial_id,
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

    target_fields = {"pyocd_target", "evidence", "reasoning_summary"}
    pack_fields = {
        "pack_id",
        "version",
        "filename",
        "url",
        "source_path",
        "official_sha256",
        "pyocd_target",
        "evidence",
        "reasoning_summary",
    }
    if fields != target_fields and fields != pack_fields:
        raise ResearchError(
            "research/field-set-mismatch",
            "response fields must exactly match the requested choice, target, or pack schema",
        )
    _validated_research_prose(response)
    target = response.get("pyocd_target")
    if not isinstance(target, str) or not target.strip():
        raise ResearchError("research/target-required", "pyocd_target must be non-empty text")
    target = target.strip().casefold()

    if fields == target_fields:
        request = make_research_request(
            fact_id="pyocd_target",
            continuation_token=continuation_id,
            board_id=board_id,
            mcu_part_number=user_input.mcu_part_number,
            unresolved_fact="Resolve the exact pyOCD target for this MCU.",
            requested_fields=("pyocd_target", "evidence", "reasoning_summary"),
            authoritative_facts={"board_type": user_input.board_type},
            acceptable_sources=("official pyOCD documentation", "official vendor CMSIS-Pack"),
            validation_plan=(
                "Check exact MCU consistency.",
                "Confirm built-in or promoted pack support.",
                "Require a live connection before profile commit.",
            ),
        )

        def validate(candidate: Mapping[str, object]) -> ValidationOutcome:
            try:
                TargetResolver.validate_candidate(
                    str(candidate["pyocd_target"]).casefold(),
                    mcu_part_number=user_input.mcu_part_number,
                    part_consistent=_part_matches_target,
                    built_in_targets=_target_names(),
                    staged_targets=tuple(
                        target_name
                        for pack in load_manifest(_firm_store.layout.pack_manifest)
                        for target_name in pack.provides_targets
                    ),
                )
            except TargetResolutionError as exc:
                return ValidationOutcome(False, str(exc), exc.observed)
            return ValidationOutcome(True)

        result = _setup_research.validate_reply(request, response, validate)
        if result.status != "accepted":
            if result.failure is not None and "absent from built-in" in result.failure.reason:
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
        if not _part_matches_target(user_input.mcu_part_number, target):
            raise ResearchError(
                "target/part-mismatch", "pack target does not match the exact MCU part number"
            )
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
            validated = pipeline.validate(candidate, required_target=target)
            pipeline.promote(validated, board_id=board_id)
        except PackCandidateError as exc:
            return {
                "status": (
                    "setup_unresolved"
                    if exc.code == "package/retry-exhausted"
                    else "setup_research_required"
                ),
                "continuation_id": continuation_id,
                "agent_prompt": f"The package candidate was rejected: {exc}. Research a materially different official candidate; do not expose this payload.",
                "rejected_candidates": (
                    [exc.failure.to_document()] if exc.failure is not None else []
                ),
                "exact_response_fields": sorted(pack_fields),
            }

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
    _setup_selections_by_board.pop(board_id, None)
    for key in tuple(_setup_pack_pipelines):
        if key[0] == board_id:
            _setup_pack_pipelines.pop(key, None)


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
)
setup_tool_loader = SetupToolLoadState(server_run)
setup_tool_handlers = build_setup_handlers(
    SetupToolServices(
        loader=setup_tool_loader,
        plan_engine=plan_engine,
        workflow=_setup_workflow,
        validator=_board_validator,
        safety_setup=_run_board_safety_setup,
        safety_refresh=_run_board_safety_refresh,
        setup_status=_get_setup_status,
        setup_overview=_setup_overview,
        setup_continue=_setup_continue,
        setup_selections=lambda board_id: _setup_selections_by_board.get(
            board_id, PreflightSelections()
        ),
        clear_setup_continuation=_clear_setup_continuation,
    )
)


def _revoke_with_setup_closure(action_name: str, board_id: str, reason: str) -> None:
    plan_engine.invalidate(action_name, board_id, reason)
    if action_name == "board_setup":
        _setup_workflow.revoke(board_id)
        setup_tool_loader.clear_allowance(board_id)
        _clear_setup_continuation(board_id)


permission_store.set_revocation_handler(_revoke_with_setup_closure)
for _setup_name, _setup_handler in setup_tool_handlers.items():
    mcp.add_tool(
        _setup_handler,
        name=_setup_name,
        description=_setup_handler.__doc__,
        structured_output=False,
    )
    if _setup_name.endswith("-plan"):
        forbid_unknown_tool_arguments(mcp, _setup_name)

M6_GUARDED_ACTIONS = ("board_setup", "board_fix_setup")
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
        current_fingerprint=_safety_policy.current_aggregate,
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
    cleanup_stale_owned_processes()
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
