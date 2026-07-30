"""Managed, bounded execution for blocking server operations.

The MCP SDK cancels the AnyIO scope that is running a request when it receives
``notifications/cancelled``.  This module turns that scope cancellation into a
cooperative thread cancellation request while keeping the board worker and its
resources owned until the worker has actually stopped.
"""

from __future__ import annotations

import inspect
import math
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import ContextDecorator, nullcontext
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field
from enum import Enum
from itertools import count
from pathlib import Path
from typing import Any, ContextManager, TypeVar, cast

import anyio
from pyocd_debug_mcp.kernel.processes import (
    MAX_OWNED_PROCESS_CLEANUP_SECONDS,
    ProcessMarkerStore,
    terminate_process_group,
)
from pyocd_debug_mcp.probe_families import configured_probe_cli_commands
from pyocd_debug_mcp.timeouts import (
    DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS,
    MAX_HOOK_TIMEOUT_SECONDS,
)

DEFAULT_OPERATION_TIMEOUT_SECONDS = 30.0
FLASH_OPERATION_TIMEOUT_SECONDS = 120.0
VALIDATION_OPERATION_TIMEOUT_SECONDS = 120.0
RECOVERY_OPERATION_TIMEOUT_SECONDS = 300.0
ARGUMENT_TIMEOUT_GRACE_SECONDS = 5.0
BATCH_TIMEOUT_GRACE_SECONDS = 5.0
CANCELLATION_CLEANUP_GRACE_SECONDS = 1.0
BOARD_LOCK_POLL_SECONDS = 0.02
SAFE_EXIT_REMINDER = (
    "Safe exit: leave the board in the intended run state, then disconnect when hardware "
    "work is complete."
)

_FLASH_TOOLS = frozenset({"flash_application", "flash_bootloader", "flash_firmware"})
_VALIDATION_TOOLS = frozenset({"board_validate"})
_RECOVERY_TOOLS = frozenset({"target_unlock"})
_PROBE_INVENTORY_TOOLS = frozenset(
    {
        "setup_overview",
        "connect",
        "connect_override",
        "get_setup_status",
        "connect_under_reset",
        "board_validate",
        "board_setup",
        "board_fix_setup",
    }
)
_INTENTIONAL_HALT_TOOLS = frozenset(
    {"halt", "reset_and_halt", "connect_under_reset", "set_breakpoint"}
)
# The easy group to miss and the one that breaks the product. These are NOT in
# _PROBE_INVENTORY_TOOLS and their budgets come from their own arguments, but
# `_resolve_serial_port_for_session` runs immediately before every one of them, so each
# can execute a UART hook. `read_serial` with read_seconds=3 resolves to 8s, against a
# hook allowance of up to 60s -- without the addend the read is cancelled before it
# starts whenever a hook actually runs.
_UART_ACTION_TOOLS = frozenset({"read_serial", "write_serial", "serial_exchange"})
# `refresh_discovery_hooks` executes every eligible hook of both kinds.
# `get_discovery_hook_contract` executes nothing and keeps the default budget.
_DISCOVERY_HOOK_TOOLS = frozenset({"refresh_discovery_hooks"})


def _default_eligible_hook_counts() -> Mapping[str, int]:
    return {"probe": 0, "uart": 0}


# A provider callable, never an import of server state: operations.py is imported by
# registry.py which is imported by server.py, so any reverse import is a cycle. The
# eligible-hook count is also run-scoped -- it is only known after a refresh -- so it
# cannot be an import-time constant.
_eligible_hook_counts: Callable[[], Mapping[str, int]] = _default_eligible_hook_counts


def set_eligible_hook_count_provider(provider: Callable[[], Mapping[str, int]]) -> None:
    """Point the timeout budget at the live hook snapshot store."""

    global _eligible_hook_counts
    _eligible_hook_counts = provider


def reset_eligible_hook_count_provider() -> None:
    """Restore the zero-count default (used by tests and at shutdown)."""

    global _eligible_hook_counts
    _eligible_hook_counts = _default_eligible_hook_counts


def _hook_budget(*kinds: str) -> float:
    """Reserve time for hooks that *may* run during this operation.

    Zero on a healthy machine: counts only go positive once a manifest is loaded, and
    hooks only execute when native discovery for that kind comes back empty. The budget
    must still be reserved, because whether native discovery will be empty is not known
    when the deadline is computed.
    """

    try:
        counts = _eligible_hook_counts()
    except Exception:  # noqa: BLE001 - a deadline must never fail to be computed
        return 0.0
    total = 0
    for kind in kinds:
        try:
            total += int(counts.get(kind, 0) or 0)
        except (TypeError, ValueError):
            continue
    if total <= 0:
        return 0.0
    return total * (MAX_HOOK_TIMEOUT_SECONDS + MAX_OWNED_PROCESS_CLEANUP_SECONDS)

T = TypeVar("T")
BeforeExecution = Callable[[], None]
Cleanup = Callable[[], None]
ResourceBinder = Callable[["ManagedOperation"], None]
Finalizer = Callable[[], None]


class OperationState(str, Enum):
    """Externally inspectable lifecycle states for one invocation."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    CLEANED = "cleaned_up"


class OperationCancelledError(RuntimeError):
    """Cooperative worker cancellation was observed at a safe checkpoint."""


class OperationCleanupError(RuntimeError):
    """A successful operation could not prove owned-process cleanup."""


def _operation_cleanup_error(
    operation: ManagedOperation, original: BaseException | None = None
) -> OperationCleanupError:
    detail = "; ".join(operation.resources.fatal_cleanup_errors)
    if original is not None:
        detail = f"{type(original).__name__}: {original}; cleanup failure: {detail}"
    return OperationCleanupError(detail)


class OperationTimeoutError(TimeoutError):
    """A bounded MCP operation did not return before its configured deadline."""

    def __init__(
        self,
        tool_name: str,
        timeout_seconds: float,
        *,
        board_id: str | None = None,
    ) -> None:
        self.tool_name = tool_name
        self.timeout_seconds = timeout_seconds
        self.board_id = board_id
        board_text = f" for board '{board_id}'" if board_id else ""
        super().__init__(
            f"Tool '{tool_name}'{board_text} exceeded its {timeout_seconds:g}s operation timeout."
        )


class BoardBusyError(TimeoutError):
    """A request used its finite bound waiting for the named board worker."""

    def __init__(self, tool_name: str, board_id: str, timeout_seconds: float) -> None:
        self.tool_name = tool_name
        self.board_id = board_id
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"Board '{board_id}' is busy with another operation; tool '{tool_name}' waited "
            f"{timeout_seconds:g}s without starting. Retry after the active operation finishes."
        )


def _run_cleanup(callbacks: list[Cleanup], errors: list[str]) -> None:
    for callback in tuple(callbacks):
        try:
            callback()
        except Exception as exc:  # noqa: BLE001 - cleanup must continue through every resource
            errors.append(f"{type(exc).__name__}: {exc}")


@dataclass(slots=True)
class _OwnedSubprocess:
    process: subprocess.Popen[Any]
    marker_store: ProcessMarkerStore
    marker: Path | None


@dataclass(slots=True)
class OperationResources:
    """Resources owned by one operation, closed once in deterministic order."""

    stop_io: list[Cleanup] = field(default_factory=list)
    close_uart: list[Cleanup] = field(default_factory=list)
    close_debug: list[Cleanup] = field(default_factory=list)
    subprocesses: list[_OwnedSubprocess] = field(default_factory=list)
    release_reset: list[Cleanup] = field(default_factory=list)
    restore_final_state: list[Cleanup] = field(default_factory=list)
    cleanup_errors: list[str] = field(default_factory=list)
    fatal_cleanup_errors: list[str] = field(default_factory=list)
    _cleaned: bool = False
    _guard: threading.Lock = field(default_factory=threading.Lock)

    @property
    def owned_count(self) -> int:
        return sum(
            (
                len(self.stop_io),
                len(self.close_uart),
                len(self.close_debug),
                len(self.subprocesses),
                len(self.release_reset),
                len(self.restore_final_state),
            )
        )

    def cleanup(self, *, preserve_halt: bool) -> None:
        """Run the mandatory cleanup chain once, continuing after individual errors."""

        with self._guard:
            if self._cleaned:
                return
            self._cleaned = True
        _run_cleanup(self.stop_io, self.cleanup_errors)
        _run_cleanup(self.close_uart, self.cleanup_errors)
        _run_cleanup(self.close_debug, self.cleanup_errors)
        for owned in tuple(self.subprocesses):
            try:
                if terminate_process_group(owned.process):
                    owned.marker_store.remove(owned.marker)
                else:
                    message = (
                        "RuntimeError: owned subprocess cleanup was not confirmed; "
                        "recovery marker retained"
                    )
                    self.cleanup_errors.append(message)
                    self.fatal_cleanup_errors.append(message)
            except Exception as exc:  # noqa: BLE001 - retain cleanup progress
                message = f"{type(exc).__name__}: {exc}"
                self.cleanup_errors.append(message)
                self.fatal_cleanup_errors.append(message)
        _run_cleanup(self.release_reset, self.cleanup_errors)
        if not preserve_halt:
            _run_cleanup(self.restore_final_state, self.cleanup_errors)


@dataclass(slots=True)
class ManagedOperation:
    """One request-bound operation and all resources it owns."""

    operation_id: str
    request_id: str
    tool_name: str
    board_id: str | None
    timeout_seconds: float
    non_interruptible: bool
    preserve_halt: bool
    resources: OperationResources = field(default_factory=OperationResources)
    prepared: dict[str, object] = field(default_factory=dict)
    state: OperationState = OperationState.QUEUED
    cancellation_reason: str | None = None
    completion_committed: bool = False
    started_at: float = field(default_factory=time.monotonic)
    execution_started_at: float | None = None
    handler_started_at: float | None = None
    finished_at: float | None = None
    cancellation_requested: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    result: object | None = None
    error: BaseException | None = None
    _guard: threading.RLock = field(default_factory=threading.RLock)

    def request_cancel(self, reason: str) -> None:
        with self._guard:
            if self.cancellation_reason is None:
                self.cancellation_reason = reason
            self.cancellation_requested.set()

    def checkpoint(self) -> None:
        if (
            self.cancellation_requested.is_set()
            and not self.non_interruptible
            and not self.completion_committed
        ):
            raise OperationCancelledError(self.cancellation_reason or "operation cancelled")

    def begin_non_interruptible(self) -> None:
        """Enter a backend transaction only after honoring any pending cancellation."""

        with self._guard:
            self.checkpoint()
            self.non_interruptible = True

    def run_if_not_cancelled(self, action: Callable[[], T]) -> T:
        """Linearize a short authority commit before any later cancellation request."""

        with self._guard:
            self.checkpoint()
            result = action()
            self.completion_committed = True
            return result

    def mark_running(self) -> None:
        with self._guard:
            self.state = OperationState.RUNNING
            self.execution_started_at = time.monotonic()

    def mark_handler_started(self) -> None:
        with self._guard:
            self.handler_started_at = time.monotonic()

    def finish(self, state: OperationState) -> None:
        with self._guard:
            self.state = state
            self.finished_at = time.monotonic()

    def cleanup(self) -> None:
        self.resources.cleanup(preserve_halt=self.preserve_halt)
        with self._guard:
            self.state = OperationState.CLEANED

    def run_finalizer(self, finalizer: Finalizer | None) -> None:
        if finalizer is None or self.handler_started_at is None:
            return
        try:
            finalizer()
        except Exception as exc:  # noqa: BLE001 - finalizers are best-effort by contract
            self.resources.cleanup_errors.append(f"finalizer: {type(exc).__name__}: {exc}")


@dataclass(frozen=True, slots=True)
class OperationSnapshot:
    operation_id: str
    request_id: str
    tool_name: str
    board_id: str | None
    state: OperationState
    cancellation_requested: bool
    non_interruptible: bool
    owned_resource_count: int


class OperationManager:
    """Track request/operation/resource ownership and one worker boundary per board."""

    def __init__(self) -> None:
        self._guard = threading.RLock()
        self._sequence = count(1)
        self._operations: dict[str, ManagedOperation] = {}
        self._operations_by_request: dict[str, set[str]] = {}
        self._board_workers: dict[str, threading.Lock] = {}
        self._batch_reservations: dict[str, threading.Lock] = {}

    def create(
        self,
        request_id: str | None,
        tool_name: str,
        board_id: str | None,
        timeout_seconds: float,
    ) -> ManagedOperation:
        with self._guard:
            operation_id = f"op-{next(self._sequence)}"
            normalized_request = request_id or operation_id
            operation = ManagedOperation(
                operation_id=operation_id,
                request_id=normalized_request,
                tool_name=tool_name,
                board_id=board_id,
                timeout_seconds=timeout_seconds,
                # Flash is interruptible while queued and while its artifact and
                # containment are checked. The handler marks only backend mutation
                # non-interruptible.
                non_interruptible=False,
                preserve_halt=tool_name in _INTENTIONAL_HALT_TOOLS,
            )
            self._operations[operation_id] = operation
            self._operations_by_request.setdefault(normalized_request, set()).add(operation_id)
            return operation

    def worker_lock(self, board_id: str | None) -> threading.Lock | ContextManager[object]:
        if board_id is None:
            return nullcontext()
        with self._guard:
            return self._board_workers.setdefault(board_id, threading.Lock())

    def batch_lock(self, board_id: str | None) -> threading.Lock | ContextManager[object]:
        if board_id is None:
            return nullcontext()
        with self._guard:
            return self._batch_reservations.setdefault(board_id, threading.Lock())

    def finish(self, operation: ManagedOperation) -> None:
        with self._guard:
            self._operations.pop(operation.operation_id, None)
            request_operations = self._operations_by_request.get(operation.request_id)
            if request_operations is not None:
                request_operations.discard(operation.operation_id)
                if not request_operations:
                    self._operations_by_request.pop(operation.request_id, None)

    def cancel_request(self, request_id: str, reason: str = "MCP request cancelled") -> int:
        with self._guard:
            operations = tuple(
                self._operations[operation_id]
                for operation_id in self._operations_by_request.get(request_id, ())
                if operation_id in self._operations
            )
        for operation in operations:
            operation.request_cancel(reason)
        return len(operations)

    def cancel_all(self, reason: str = "client EOF or server shutdown") -> int:
        with self._guard:
            operations = tuple(self._operations.values())
        for operation in operations:
            operation.request_cancel(reason)
        return len(operations)

    def snapshots(self) -> tuple[OperationSnapshot, ...]:
        with self._guard:
            return tuple(
                OperationSnapshot(
                    operation.operation_id,
                    operation.request_id,
                    operation.tool_name,
                    operation.board_id,
                    operation.state,
                    operation.cancellation_requested.is_set(),
                    operation.non_interruptible,
                    operation.resources.owned_count,
                )
                for operation in self._operations.values()
            )


operation_manager = OperationManager()
_current_operation: ContextVar[ManagedOperation | None] = ContextVar(
    "managed_operation", default=None
)
_reserved_batch_boards: ContextVar[frozenset[str]] = ContextVar(
    "reserved_batch_boards", default=frozenset()
)


def current_operation() -> ManagedOperation | None:
    """Return the operation owning the current async task or worker thread."""

    return _current_operation.get()


def cancellation_checkpoint() -> None:
    """Stop interruptible work when its MCP request was cancelled or timed out."""

    operation = current_operation()
    if operation is not None:
        operation.checkpoint()


def run_if_not_cancelled(action: Callable[[], T]) -> T:
    """Run one short commit only if the owning managed request is still live."""

    operation = current_operation()
    if operation is None:
        return action()
    return operation.run_if_not_cancelled(action)


def operation_resources() -> OperationResources:
    operation = current_operation()
    if operation is None:
        raise RuntimeError("operation resources are available only during managed dispatch")
    return operation.resources


def wrap_layer2_response(result: str) -> str:
    """Append the common Layer-2 safe-exit reminder exactly once."""

    if SAFE_EXIT_REMINDER in result:
        return result
    return f"{result}\n{SAFE_EXIT_REMINDER}"


def _positive_finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number > 0 and math.isfinite(number) else None


def operation_timeout_seconds(
    tool_name: str,
    arguments: Mapping[str, object] | None = None,
) -> float:
    """Return the finite A-11 timeout budget for a tool invocation."""

    values = arguments or {}
    finalizer_timeout: float | None = None
    finalizer_reaches_uart = False
    if "on_exit" in values:
        from pyocd_debug_mcp.kernel.finalizers import (
            FinalizerValidationError,
            UARTWriteFinalizer,
            parse_finalizer,
        )

        try:
            finalizer = parse_finalizer(tool_name, values["on_exit"])
        except FinalizerValidationError:
            raw_finalizer = values["on_exit"]
            # Preserve the established deadline treatment for malformed input
            # that explicitly supplied a positive finite UART timeout. Valid
            # UART finalizers, including ones that omit the field, use the
            # schema-validated value below.
            if (
                isinstance(raw_finalizer, Mapping)
                and raw_finalizer.get("action") == "uart_write"
                and "timeout_seconds" in raw_finalizer
            ):
                finalizer_timeout = _positive_finite_number(raw_finalizer.get("timeout_seconds"))
                finalizer_reaches_uart = True
        else:
            if isinstance(finalizer, UARTWriteFinalizer):
                finalizer_timeout = finalizer.timeout_seconds
                finalizer_reaches_uart = True

    def include_finalizer(timeout: float) -> float:
        if finalizer_timeout is None:
            return timeout
        total = timeout + finalizer_timeout + ARGUMENT_TIMEOUT_GRACE_SECONDS
        if finalizer_reaches_uart:
            # `_finalizer_uart_write` calls `_resolve_serial_port_for_session`, so the
            # finalizer itself can execute a UART hook after the main action finished.
            total += _hook_budget("uart")
        return total

    if tool_name == "action_batch":
        actions = values.get("actions")
        if isinstance(actions, list):
            child_timeouts: list[float] = []
            for child in actions:
                if not isinstance(child, Mapping):
                    continue
                child_name = child.get("tool_name")
                child_arguments = child.get("arguments")
                if not isinstance(child_name, str) or child_name == "action_batch":
                    child_timeouts.append(DEFAULT_OPERATION_TIMEOUT_SECONDS)
                    continue
                child_timeouts.append(
                    operation_timeout_seconds(
                        child_name,
                        child_arguments if isinstance(child_arguments, Mapping) else None,
                    )
                )
            if child_timeouts:
                return include_finalizer(
                    max(
                        DEFAULT_OPERATION_TIMEOUT_SECONDS,
                        sum(child_timeouts) + BATCH_TIMEOUT_GRACE_SECONDS,
                    )
                )
    # Planned actions publish their timeout as part of the immutable plan
    # definition.  Runtime dispatch must use that same value so the guidance
    # shown to an agent cannot drift from the actual operation deadline.
    try:
        from pyocd_debug_mcp.guardrails.plan_defs import definition_for_action

        planned_timeout = definition_for_action(tool_name).timeout_seconds
    except KeyError:
        planned_timeout = None
    if tool_name in _FLASH_TOOLS:
        resolved_timeout = FLASH_OPERATION_TIMEOUT_SECONDS
    elif tool_name in _VALIDATION_TOOLS:
        resolved_timeout = VALIDATION_OPERATION_TIMEOUT_SECONDS
    elif tool_name in _RECOVERY_TOOLS:
        resolved_timeout = RECOVERY_OPERATION_TIMEOUT_SECONDS
    elif planned_timeout is not None:
        resolved_timeout = float(planned_timeout)
    else:
        resolved_timeout = DEFAULT_OPERATION_TIMEOUT_SECONDS
    if tool_name in _PROBE_INVENTORY_TOOLS:
        inventory_timeout = (
            DEFAULT_OPERATION_TIMEOUT_SECONDS
            + len(configured_probe_cli_commands())
            * (DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS + MAX_OWNED_PROCESS_CLEANUP_SECONDS)
            + CANCELLATION_CLEANUP_GRACE_SECONDS
            # Every one of these tools takes an inventory snapshot, which can execute
            # hooks of either kind.
            + _hook_budget("probe", "uart")
        )
        resolved_timeout = max(resolved_timeout, inventory_timeout)
    if tool_name in _DISCOVERY_HOOK_TOOLS:
        resolved_timeout = max(
            resolved_timeout,
            DEFAULT_OPERATION_TIMEOUT_SECONDS
            + _hook_budget("probe", "uart")
            + CANCELLATION_CLEANUP_GRACE_SECONDS,
        )
    if tool_name in _UART_ACTION_TOOLS:
        resolved_timeout += _hook_budget("uart")
    if tool_name == "read_serial":
        requested = _positive_finite_number(values.get("read_seconds"))
        if requested is not None:
            return include_finalizer(
                max(
                    float(planned_timeout or DEFAULT_OPERATION_TIMEOUT_SECONDS),
                    requested + ARGUMENT_TIMEOUT_GRACE_SECONDS,
                )
                + _hook_budget("uart")
            )
    if tool_name == "serial_exchange":
        per_step = _positive_finite_number(values.get("read_seconds"))
        steps = values.get("steps")
        step_count = len(steps) if isinstance(steps, list) and steps else 1
        ready = _positive_finite_number(values.get("ready_seconds")) or 0.0
        if per_step is not None:
            return include_finalizer(
                max(
                    float(planned_timeout or DEFAULT_OPERATION_TIMEOUT_SECONDS),
                    ready + step_count * per_step + ARGUMENT_TIMEOUT_GRACE_SECONDS,
                )
                + _hook_budget("uart")
            )
    if tool_name == "write_serial":
        requested = _positive_finite_number(values.get("timeout_seconds"))
        if requested is not None:
            return include_finalizer(
                max(
                    float(planned_timeout or DEFAULT_OPERATION_TIMEOUT_SECONDS),
                    requested + ARGUMENT_TIMEOUT_GRACE_SECONDS,
                )
                + _hook_budget("uart")
            )
    if tool_name == "wait":
        requested_ms = _positive_finite_number(values.get("ms"))
        if requested_ms is not None:
            return include_finalizer(
                max(
                    DEFAULT_OPERATION_TIMEOUT_SECONDS,
                    requested_ms / 1000.0 + ARGUMENT_TIMEOUT_GRACE_SECONDS,
                )
            )
    return include_finalizer(resolved_timeout)


async def _wait_for_done(operation: ManagedOperation) -> None:
    while not operation.done.is_set():
        await anyio.sleep(BOARD_LOCK_POLL_SECONDS)


async def _wait_for_cleanup(operation: ManagedOperation, seconds: float) -> None:
    with anyio.CancelScope(shield=True):
        with anyio.move_on_after(seconds):
            await _wait_for_done(operation)


def _acquire_worker_lock(
    lock: threading.Lock | ContextManager[object], operation: ManagedOperation
) -> ContextManager[object]:
    if not isinstance(lock, type(threading.Lock())):
        return cast(ContextManager[object], lock)

    class _CooperativeLock(ContextDecorator):
        def __enter__(self) -> object:
            while not lock.acquire(timeout=BOARD_LOCK_POLL_SECONDS):
                operation.checkpoint()
            return lock

        def __exit__(self, *exc_info: object) -> None:
            lock.release()

    return _CooperativeLock()


async def dispatch(
    tool_name: str,
    board_id: str | None,
    operation: Callable[[], T] | Callable[[], Awaitable[T]],
    timeout: float,
    *,
    before_execution: BeforeExecution | None = None,
    execution_lock: ContextManager[object] | None = None,
    request_id: str | None = None,
    serialize_board: bool = True,
    resource_binder: ResourceBinder | None = None,
    finalizer: Finalizer | None = None,
    manager: OperationManager = operation_manager,
) -> T:
    """Execute one request through its managed lifecycle and finite A-11 bound."""

    if timeout <= 0 or not math.isfinite(timeout):
        raise ValueError("timeout must be a positive finite number")

    managed = manager.create(request_id, tool_name, board_id, timeout)
    if resource_binder is not None:
        try:
            resource_binder(managed)
        except BaseException:
            managed.cleanup()
            managed.done.set()
            manager.finish(managed)
            raise
    token = _current_operation.set(managed)
    if inspect.iscoroutinefunction(operation):
        reservation = manager.batch_lock(board_id)
        owns_reservation = board_id is not None and board_id in _reserved_batch_boards.get()
        acquired_reservation = False
        reservation_token = None
        try:
            if not owns_reservation and isinstance(reservation, type(threading.Lock())):
                concrete_reservation = cast(Any, reservation)
                while not concrete_reservation.acquire(blocking=False):
                    managed.checkpoint()
                    await anyio.sleep(BOARD_LOCK_POLL_SECONDS)
                acquired_reservation = True
            if tool_name == "action_batch" and board_id is not None:
                reservation_token = _reserved_batch_boards.set(
                    _reserved_batch_boards.get() | {board_id}
                )
            managed.mark_running()
            if before_execution is not None:
                before_execution()
            managed.mark_handler_started()
            with anyio.fail_after(timeout):
                async_operation = cast(Callable[[], Awaitable[T]], operation)
                result = await async_operation()
            managed.result = result
            managed.finish(OperationState.COMPLETED)
            return result
        except TimeoutError as exc:
            managed.request_cancel("operation timeout")
            managed.finish(OperationState.TIMED_OUT)
            raise OperationTimeoutError(tool_name, timeout, board_id=board_id) from exc
        except anyio.get_cancelled_exc_class():
            managed.request_cancel("MCP request cancelled or client disconnected")
            managed.finish(OperationState.CANCELLED)
            raise
        except BaseException as exc:
            managed.error = exc
            managed.finish(OperationState.FAILED)
            raise
        finally:
            active_error = sys.exception()
            managed.run_finalizer(finalizer)
            was_completed = managed.state is OperationState.COMPLETED
            managed.cleanup()
            managed.done.set()
            manager.finish(managed)
            if reservation_token is not None:
                _reserved_batch_boards.reset(reservation_token)
            if acquired_reservation:
                cast(Any, reservation).release()
            _current_operation.reset(token)
            if managed.resources.fatal_cleanup_errors and (
                was_completed or active_error is not None
            ):
                raise _operation_cleanup_error(managed, active_error) from active_error

    sync_operation = cast(Callable[[], T], operation)
    worker_lock = manager.worker_lock(board_id) if serialize_board else nullcontext()
    reservation_lock = (
        nullcontext()
        if board_id is not None and board_id in _reserved_batch_boards.get()
        else manager.batch_lock(board_id)
    )

    def run_synchronous() -> None:
        worker_token = _current_operation.set(managed)
        try:
            with (
                _acquire_worker_lock(reservation_lock, managed),
                _acquire_worker_lock(worker_lock, managed),
            ):
                try:
                    with execution_lock or nullcontext():
                        managed.checkpoint()
                        managed.mark_running()
                        if before_execution is not None:
                            before_execution()
                        managed.checkpoint()
                        managed.mark_handler_started()
                        managed.result = sync_operation()
                        managed.checkpoint()
                    managed.finish(OperationState.COMPLETED)
                except OperationCancelledError as exc:
                    managed.error = exc
                    managed.finish(OperationState.CANCELLED)
                except BaseException as exc:
                    managed.error = exc
                    managed.finish(OperationState.FAILED)
                finally:
                    managed.run_finalizer(finalizer)
                    # Resource cleanup remains inside the one-board worker boundary.
                    was_completed = managed.state is OperationState.COMPLETED
                    managed.cleanup()
                    if (
                        managed.error is None
                        and was_completed
                        and managed.resources.fatal_cleanup_errors
                    ):
                        managed.error = _operation_cleanup_error(managed)
                        managed.finish(OperationState.FAILED)
        except OperationCancelledError as exc:
            # Cancellation can occur while still queued, before the lock context enters.
            managed.error = exc
            managed.finish(OperationState.CANCELLED)
            managed.cleanup()
        except BaseException as exc:
            managed.error = exc
            managed.finish(OperationState.FAILED)
            managed.cleanup()
        finally:
            managed.done.set()
            manager.finish(managed)
            _current_operation.reset(worker_token)

    worker_context = copy_context()
    worker = threading.Thread(
        target=worker_context.run,
        args=(run_synchronous,),
        name=f"mcp-{managed.operation_id}-{tool_name}",
        daemon=True,
    )
    worker.start()
    try:
        with anyio.fail_after(timeout):
            await _wait_for_done(managed)
    except TimeoutError as exc:
        was_queued = managed.execution_started_at is None
        managed.request_cancel("operation timeout")
        cleanup_wait = (
            timeout
            if managed.non_interruptible or managed.completion_committed
            else CANCELLATION_CLEANUP_GRACE_SECONDS
        )
        await _wait_for_cleanup(managed, cleanup_wait)
        if managed.done.is_set() and managed.completion_committed and managed.error is None:
            if managed.resources.fatal_cleanup_errors:
                raise _operation_cleanup_error(managed)
            return cast(T, managed.result)
        timeout_error = OperationTimeoutError(tool_name, timeout, board_id=board_id)
        if managed.resources.fatal_cleanup_errors:
            raise _operation_cleanup_error(managed, timeout_error) from timeout_error
        if was_queued:
            if board_id is None:  # pragma: no cover - no board lock means it cannot be queued
                raise timeout_error from exc
            raise BoardBusyError(tool_name, board_id, timeout) from exc
        raise timeout_error from exc
    except anyio.get_cancelled_exc_class() as exc:
        managed.request_cancel("MCP request cancelled or client disconnected")
        cleanup_wait = (
            timeout
            if managed.non_interruptible or managed.completion_committed
            else CANCELLATION_CLEANUP_GRACE_SECONDS
        )
        await _wait_for_cleanup(managed, cleanup_wait)
        if managed.done.is_set() and managed.completion_committed and managed.error is None:
            if managed.resources.fatal_cleanup_errors:
                raise _operation_cleanup_error(managed, exc) from exc
            return cast(T, managed.result)
        if managed.resources.fatal_cleanup_errors:
            raise _operation_cleanup_error(managed, exc) from exc
        raise
    finally:
        _current_operation.reset(token)

    if managed.error is not None:
        if managed.resources.fatal_cleanup_errors and not isinstance(
            managed.error, OperationCleanupError
        ):
            raise _operation_cleanup_error(managed, managed.error) from managed.error
        raise managed.error
    return cast(T, managed.result)
