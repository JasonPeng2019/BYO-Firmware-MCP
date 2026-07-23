"""Managed cancellation-aware execution for blocking server operations.

The MCP SDK cancels the AnyIO scope that is running a request when it receives
``notifications/cancelled``.  This module turns that scope cancellation into a
cooperative thread cancellation request while keeping the board worker and its
resources owned until the worker has actually stopped.
"""

from __future__ import annotations

import inspect
import math
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from contextlib import ContextDecorator, nullcontext
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field
from enum import Enum
from itertools import count
from pathlib import Path
from typing import Any, ContextManager, TypeVar, cast

import anyio
from anyio.to_thread import run_sync as run_sync_in_thread
from firmware_mcp.kernel.processes import (
    ProcessMarkerStore,
    terminate_process_group,
)

_INTENTIONAL_HALT_TOOLS = frozenset(
    {"halt_target", "reset_target", "connect_board", "set_breakpoint"}
)

T = TypeVar("T")
BeforeExecution = Callable[[], None]
Cleanup = Callable[[], object]
ResourceBinder = Callable[["ManagedOperation"], None]


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


def _run_cleanup(callbacks: list[Cleanup], errors: list[str]) -> None:
    for callback in tuple(callbacks):
        try:
            callback()
        except Exception as exc:  # noqa: BLE001 - cleanup must continue through every resource
            errors.append(f"{type(exc).__name__}: {exc}")


def _run_debug_cleanup(
    callbacks: list[Cleanup], errors: list[str], fatal_errors: list[str]
) -> None:
    """Retain forced-close diagnostics but fail on unproven debug closure.

    A process worker that was force-terminated is still closed; its graceful
    failure is useful evidence, not an invented open-resource failure.  A
    close callback that cannot prove closure is different: cancellation must
    surface its retained recovery marker to the client.
    """

    for callback in tuple(callbacks):
        try:
            evidence = callback()
        except Exception as exc:  # noqa: BLE001 - every owned cleanup still runs
            message = f"debug session cleanup was not confirmed: {type(exc).__name__}: {exc}"
            errors.append(message)
            fatal_errors.append(message)
            continue
        if not isinstance(evidence, dict):
            continue
        if evidence.get("closed") is not True:
            message = f"debug session cleanup was not confirmed: {evidence!r}"
            errors.append(message)
            fatal_errors.append(message)
            continue
        if evidence.get("graceful") is False and evidence.get("diagnostic"):
            errors.append(
                "debug session was force-closed after graceful close failed: "
                f"{evidence['diagnostic']}"
            )


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
        _run_debug_cleanup(
            self.close_debug,
            self.cleanup_errors,
            self.fatal_cleanup_errors,
        )
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
    timeout_seconds: float | None
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
    cancellation_callbacks: list[Cleanup] = field(default_factory=list)
    done: threading.Event = field(default_factory=threading.Event)
    result: object | None = None
    error: BaseException | None = None
    _guard: threading.RLock = field(default_factory=threading.RLock)

    def request_cancel(self, reason: str) -> None:
        with self._guard:
            if self.cancellation_reason is None:
                self.cancellation_reason = reason
            self.cancellation_requested.set()
            callbacks = tuple(self.cancellation_callbacks)
            self.cancellation_callbacks.clear()
        # Cancellation is an ownership action, not a timer poll.  Each callback
        # must be idempotent because EOF and an MCP cancellation can race.
        _run_cleanup(list(callbacks), self.resources.cleanup_errors)

    def add_cancellation_callback(self, callback: Cleanup) -> None:
        with self._guard:
            if self.cancellation_requested.is_set():
                invoke_now = True
            else:
                self.cancellation_callbacks.append(callback)
                invoke_now = False
        if invoke_now:
            _run_cleanup([callback], self.resources.cleanup_errors)

    def remove_cancellation_callback(self, callback: Cleanup) -> None:
        """Release request-bound ownership after a resource becomes a session."""

        with self._guard:
            self.cancellation_callbacks = [
                candidate for candidate in self.cancellation_callbacks if candidate is not callback
            ]

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

    def commit_completion(self, action: Callable[[], T]) -> T:
        """Make one publication outcome indivisible from request cancellation.

        ``action`` may stage a small publication while this state lock is held.
        The second checkpoint is intentional: a cancellation raised re-entrantly
        by that action is still before the completion decision, so its caller can
        roll the publication back.  A cancellation from another thread instead
        waits for this lock and therefore observes either the pre-commit rollback
        path or the completed result, never a half-published session.
        """

        with self._guard:
            self.checkpoint()
            result = action()
            self.checkpoint()
            self.completion_committed = True
            return result

    def run_if_not_cancelled(self, action: Callable[[], T]) -> T:
        """Backward-compatible name for the general short commit helper."""

        return self.commit_completion(action)

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

    def create(
        self,
        request_id: str | None,
        tool_name: str,
        board_id: str | None,
        timeout_seconds: float | None,
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


def current_operation() -> ManagedOperation | None:
    """Return the operation owning the current async task or worker thread."""

    return _current_operation.get()


def cancellation_checkpoint() -> None:
    """Stop interruptible work when its MCP request was cancelled or timed out."""

    operation = current_operation()
    if operation is not None:
        operation.checkpoint()


def wrap_layer2_response(value: str) -> str:
    """Return an operation result without adding policy instructions.

    The retained presentation seam intentionally does not append a mandatory
    shutdown reminder: the cooperative caller receives the actual result.
    """

    return value


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


async def _wait_for_done(operation: ManagedOperation) -> None:
    """Wait on the ownership event; no server polling interval is involved."""

    # ``threading.Event.wait`` is a blocking synchronization primitive, not an
    # arbitrary server deadline. Wrap it so Pyright sees the zero-argument
    # callable rather than the interpreter's overloaded event method.
    def wait_for_completion() -> None:
        operation.done.wait()

    await run_sync_in_thread(wait_for_completion, abandon_on_cancel=False)


def _acquire_worker_lock(
    lock: threading.Lock | ContextManager[object], operation: ManagedOperation
) -> ContextManager[object]:
    if not isinstance(lock, type(threading.Lock())):
        return cast(ContextManager[object], lock)

    class _CooperativeLock(ContextDecorator):
        def __enter__(self) -> object:
            # ``threading.Lock`` cannot wait for both its own acquisition and an
            # MCP cancellation. Race the two OS events instead of polling a
            # project-defined interval. A cancelled waiter releases a later lock
            # acquisition without ever entering the operation body.
            outcome: queue.Queue[bool] = queue.Queue(maxsize=2)

            def acquire_lock() -> None:
                lock.acquire()
                if operation.cancellation_requested.is_set():
                    lock.release()
                    outcome.put(False)
                else:
                    outcome.put(True)

            def await_cancellation() -> None:
                operation.cancellation_requested.wait()
                outcome.put(False)

            threading.Thread(target=acquire_lock, daemon=True).start()
            threading.Thread(target=await_cancellation, daemon=True).start()
            if not outcome.get():
                operation.checkpoint()
                raise OperationCancelledError(
                    operation.cancellation_reason or "operation cancelled while waiting for board"
                )
            try:
                operation.checkpoint()
            except BaseException:
                lock.release()
                raise
            return lock

        def __exit__(self, *exc_info: object) -> None:
            lock.release()

    return _CooperativeLock()


async def dispatch(
    tool_name: str,
    board_id: str | None,
    operation: Callable[[], T] | Callable[[], Awaitable[T]],
    timeout: float | None = None,
    *,
    before_execution: BeforeExecution | None = None,
    execution_lock: ContextManager[object] | None = None,
    request_id: str | None = None,
    serialize_board: bool = True,
    resource_binder: ResourceBinder | None = None,
    manager: OperationManager = operation_manager,
) -> T:
    """Execute one request until completion, cancellation, or caller semantic time."""

    if timeout is not None and (timeout <= 0 or not math.isfinite(timeout)):
        raise ValueError("timeout must be a positive finite number when supplied")

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
        try:
            managed.mark_running()
            if before_execution is not None:
                before_execution()
            managed.mark_handler_started()
            timeout_scope = anyio.fail_after(timeout) if timeout is not None else nullcontext()
            with timeout_scope:
                async_operation = cast(Callable[[], Awaitable[T]], operation)
                result = await async_operation()
            managed.result = result
            managed.finish(OperationState.COMPLETED)
            return result
        except TimeoutError as exc:
            assert timeout is not None
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
            was_completed = managed.state is OperationState.COMPLETED
            managed.cleanup()
            managed.done.set()
            manager.finish(managed)
            _current_operation.reset(token)
            if managed.resources.fatal_cleanup_errors and (
                was_completed or active_error is not None
            ):
                raise _operation_cleanup_error(managed, active_error) from active_error

    sync_operation = cast(Callable[[], T], operation)
    worker_lock = manager.worker_lock(board_id) if serialize_board else nullcontext()

    def run_synchronous() -> None:
        worker_token = _current_operation.set(managed)
        try:
            with (
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
        timeout_scope = anyio.fail_after(timeout) if timeout is not None else nullcontext()
        with timeout_scope:
            await _wait_for_done(managed)
    except TimeoutError as exc:
        assert timeout is not None
        managed.request_cancel("operation timeout")
        await _wait_for_done(managed)
        if managed.done.is_set() and managed.completion_committed and managed.error is None:
            if managed.resources.fatal_cleanup_errors:
                raise _operation_cleanup_error(managed)
            return cast(T, managed.result)
        timeout_error = OperationTimeoutError(tool_name, timeout, board_id=board_id)
        if managed.resources.fatal_cleanup_errors:
            raise _operation_cleanup_error(managed, timeout_error) from timeout_error
        raise timeout_error from exc
    except anyio.get_cancelled_exc_class() as exc:
        managed.request_cancel("MCP request cancelled or client disconnected")
        await _wait_for_done(managed)
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
