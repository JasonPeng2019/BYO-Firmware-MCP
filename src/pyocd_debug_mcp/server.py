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
import secrets
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

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
from pyocd_debug_mcp.firmstore.profiles import ProfileError, ProfileRepository
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
from pyocd_debug_mcp.services.uart_capture import capture_uart_output, write_uart_output
from pyocd_debug_mcp.setup_flow.preflight import (
    PreflightInventory,
    ProbeCandidate,
    SerialCandidate,
    SetupUserInput,
)
from pyocd_debug_mcp.setup_flow.setup import (
    SetupPhase,
    SetupPhaseContext,
    SetupPhaseOutcome,
    SetupWorkflow,
)
from pyocd_debug_mcp.setup_flow.targets import ProfileCommitCoordinator
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
    SafetyIssue,
    SafetyMapBuilder,
    SafetySetupRequest,
    region_conflicts,
)
from pyocd_debug_mcp.safety.refresh import SafetyRefreshRequest, SafetyRefresher
from pyocd_debug_mcp.safety.regions import (
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


def _check_register_safety(board_id: str, address: int) -> None:
    _safety_policy.check_register_write(board_id, address)


def _check_breakpoint_safety(board_id: str, address: int) -> None:
    _safety_policy.check_breakpoint(board_id, address)


def _check_flash_safety(tool_name: str, board_id: str, artifact: Path) -> None:
    role = (
        BuildRole.APPLICATION if tool_name == "flash_application" else BuildRole.BOOTLOADER
    )
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
                BuildRole.APPLICATION
                if tool_name == "flash_application"
                else BuildRole.BOOTLOADER
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
        name: value
        for name, value in arguments.items()
        if name not in {"board_id", "on_exit"}
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


def resolve_board_config(board_id: str | None, board_config: str | None) -> BoardConfig | None:
    """Load one board definition through the shared loader, or None if unselected.

    This is the server's single path to ``boards/<board>.yaml`` — the same loader
    the Stage 0 CLI uses — so a custom ST/nRF board's facts (pyOCD target, recover
    policy, silicon id, baud) reach the MCP tools, not just the CLI.

    ``board_id``/``board_config`` fall back to the ``PYOCD_BOARD_ID`` /
    ``PYOCD_BOARD_CONFIG`` environment variables (the stdio-launch config
    channel). Returns ``None`` when no board is named, so ``connect`` still works
    with a raw target. Raises ``ConfigError`` if a named board cannot be found or
    a config file is malformed.
    """
    bid = (board_id or os.environ.get("PYOCD_BOARD_ID") or "").strip()
    if not bid:
        return None
    extra = board_config or os.environ.get("PYOCD_BOARD_CONFIG") or None
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
        f"test_read_address: 0x{b.test_addr:08X}",
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
) -> str | None:
    if unique_id is not None:
        return unique_id
    env_uid = os.environ.get("PYOCD_PROBE_UID") or None
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
        raise RuntimeError(
            f"Probe resolution for {board.display_name} did not yield a unique id. "
            "Rerun with unique_id=... or set PYOCD_PROBE_UID."
        )
    return resolution.probe.uid


def _handle(board_id: str) -> TargetSessionHandle:
    """Return the named board's live session handle or raise if disconnected."""

    return connection_manager.handle_for(board_id)


def _maybe_handle(board_id: str) -> TargetSessionHandle | None:
    connection = connection_manager.maybe_connection(board_id)
    return connection.handle if connection is not None else None


@mcp.tool()
def connect(
    board_id: str,
    unique_id: str | None = None,
    target: str | None = None,
    board_config: str | None = None,
) -> str:
    """Assign one connected probe session to the required logical board.

    Args:
        board_id: Required logical board identity. It also selects facts from
            ``boards/<board_id>.yaml`` through the shared board-config loader.
        unique_id: Whole or partial probe serial/unique ID to select a specific
            probe. Omit when exactly one probe is attached. Defaults to the
            ``PYOCD_PROBE_UID`` environment variable if unset.
        target: Target type override, e.g. "stm32f407vg" or "nrf52833". Takes
            precedence over a board config. Omit to use the selected board's
            target (when ``board_id`` is given), else the ``PYOCD_TARGET``
            environment variable, else pyOCD auto-detection.
        board_config: Path to an extra board-config file outside the tracked
            ``boards/`` directory, for a custom board. Defaults to the
            ``PYOCD_BOARD_CONFIG`` environment variable.
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
            board = resolve_board_config(board_id, board_config)
            uid = _resolve_probe_uid_for_connect(board, unique_id)
            tgt = (
                target
                or (board.pyocd_target if board else None)
                or os.environ.get("PYOCD_TARGET")
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
).items():
    mcp.add_tool(
        _handler,
        name=_handler_name,
        description=_handler.__doc__,
        structured_output=False,
    )

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
    rc, stdout, _ = _run_cmd(["pyocd", "list", "--targets"])
    names: set[str] = set()
    if rc == 0:
        for line in stdout.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and not set(stripped) == {"-"}:
                names.add(stripped.split()[0].casefold())
    for pack in load_manifest():
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
        probe_family = (
            board.probe_family
            if board is not None
            else _infer_probe_family(description)
        )
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
            port.device,
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
    __slots__ = ("handle", "owned")

    def __init__(self, handle: TargetSessionHandle, owned: bool) -> None:
        self.handle = handle
        self.owned = owned


def _validation_connect(profile, probe: ValidationProbe, timeout: float) -> object:
    del timeout
    existing = connection_manager.maybe_connection(profile.board_id)
    if existing is not None:
        return _ValidationConnection(existing.handle, False)
    return _ValidationConnection(
        target_control.open_session(
            board=profile.board,
            unique_id=probe.usb_serial,
            target=profile.board.pyocd_target,
            server_timeouts=_staged_server_timeouts,
            connect_mode="attach",
        ),
        True,
    )


def _validation_read(connection: object, address: int, width: int, timeout: float) -> int:
    del timeout
    return target_control.read_memory(cast(_ValidationConnection, connection).handle, address, width)


def _validation_close(connection: object) -> None:
    validation = cast(_ValidationConnection, connection)
    if validation.owned:
        target_control.close_session(validation.handle)


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
            for candidate in load_manifest()
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
            for candidate in load_manifest()
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
        pack_spec_document(pack) if pack is not None else {"missing_for_target": profile.board.pyocd_target},
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
                    executable=True,
                ),
                (source,),
            )
        )
    replacements.extend(
        RegionContribution(
            SafetyRegion(
                f"{role.value} RAM {index}",
                RegionKind.RAM,
                address_range,
                provenance,
            ),
            (source,),
        )
        for index, address_range in enumerate(evidence.ram_partitions)
    )
    return tuple(replacements)


def _run_board_safety_setup(board_id: str) -> Mapping[str, object]:
    continuation = _safety_continuation("safety-setup")
    try:
        current = _safety_repository.load_current(board_id)
    except (SafetyArtifactError, ValueError):
        try:
            inputs = _bootstrap_safety_inputs(board_id)
        except (ProfileError, ValueError) as exc:
            return {
                "status": "safety_setup_blocked",
                "continuation_id": continuation,
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
                "validation_plan": ["board_setup", "board_safety_setup"],
            }
        result = _safety_builder.build(
            SafetySetupRequest(
                board_id,
                continuation,
                inputs,
                (),
                (
                    SafetyIssue(
                        "safety_setup_research_required",
                        "safety/authoritative-sources-required",
                        "No current safety map exists. Resolve server-loaded device support, "
                        "official-document evidence, and selected build artifacts before retrying "
                        "safety setup; never supply caller-defined allowed ranges.",
                    ),
                ),
            )
        )
        return result.to_payload()

    live_inputs = _live_safety_inputs(board_id, current)
    changed = current.fingerprints.changed_sources(FingerprintSet.build(live_inputs))
    if changed:
        result = _safety_builder.build(
            SafetySetupRequest(
                board_id,
                continuation,
                live_inputs,
                current.regions,
                (
                    SafetyIssue(
                        "safety_setup_research_required",
                        "safety/source-rebuild-required",
                        "Authoritative safety inputs changed. Rebuild and re-verify the named "
                        "sources before replacing the current map.",
                        details={"changed_sources": [item.value for item in changed]},
                    ),
                ),
            )
        )
        return result.to_payload()

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


def _run_board_safety_refresh(board_id: str) -> Mapping[str, object]:
    continuation = _safety_continuation("safety-refresh")
    try:
        current = _safety_repository.load_current(board_id)
        live_inputs = _live_safety_inputs(board_id, current)
    except (SafetyArtifactError, SafetyPolicyError, ProfileError, ValueError):
        inputs = _bootstrap_safety_inputs(board_id)
        return _safety_refresher.refresh(
            SafetyRefreshRequest(board_id, continuation, inputs)
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
        connection.connection_id
        if connection is not None
        else f"probe:{stable_probe.casefold()}"
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
    if len(selected_probes) == 1:
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
        sorted({target for pack in load_manifest() for target in pack.provides_targets})
    )
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
    if normalized.startswith("stm32") and len(normalized) >= 7:
        return normalized[:7]
    if normalized.startswith("nrf") and len(normalized) >= 8:
        return normalized[:8]
    return target.casefold()


def _setup_connection_phase(context: SetupPhaseContext) -> SetupPhaseOutcome:
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

    opened: list[TargetSessionHandle] = []

    def connect(candidate_target: str, _pack_path: str | None) -> None:
        handle = target_control.open_session(
            board=None,
            unique_id=probe.usb_serial,
            target=candidate_target,
            server_timeouts=_staged_server_timeouts,
        )
        opened.append(handle)
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


def _setup_safety_research_phase(context: SetupPhaseContext) -> SetupPhaseOutcome:
    try:
        artifacts = _safety_repository.load_current(context.user_input.board_id)
    except (SafetyArtifactError, ValueError) as exc:
        return SetupPhaseOutcome.stop(
            "setup_safety_incomplete",
            "setup/safety-setup-required",
            "Authoritative safety evidence is not complete. Run board_safety_setup, resolve "
            "any research request it returns, then continue the paired setup repair.",
            details={"reason": str(exc)},
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
        setup_tool_loader,
        plan_engine,
        _setup_workflow,
        _board_validator,
        _run_board_safety_setup,
        _run_board_safety_refresh,
    )
)


def _revoke_with_setup_closure(action_name: str, board_id: str, reason: str) -> None:
    plan_engine.invalidate(action_name, board_id, reason)
    if action_name == "board_setup":
        _setup_workflow.revoke(board_id)
        setup_tool_loader.clear_allowance(board_id)


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
    """Bind the current persistent connection to abnormal cleanup and A-15 restoration."""

    board_id = operation.board_id
    if board_id is None or operation.tool_name in {"connect", "disconnect", "action_batch"}:
        return
    connection = connection_manager.maybe_connection(board_id)
    if connection is None:
        return
    handle = connection.handle

    def close_failed_connection() -> None:
        failed_after_start = (
            operation.state is OperationState.FAILED
            and operation.handler_started_at is not None
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
    operation.resources.restore_final_state.append(
        lambda: target_control.reset(handle, halt_after=False)
    )


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

initialization_handshake = register_initialization_handshake(mcp, tool_registry)


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
