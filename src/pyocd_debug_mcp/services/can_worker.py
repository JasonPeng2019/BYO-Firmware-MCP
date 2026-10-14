# SPDX-License-Identifier: Apache-2.0
# Derived from Firmware-CLI commit 0e8a45d; modified for BYO-Firmware-MCP.
"""Spawned owner for blocking CAN backend operations.

Caller deadlines include worker creation and readiness. A daemon launch owner
retains a stalled OS spawn and retires any late child; cleanup remains unknown
until that owner has proved retirement. The OS spawn itself cannot be interrupted.
"""

from __future__ import annotations

import asyncio
import ctypes
import math
import multiprocessing
import os
import signal
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from multiprocessing.process import BaseProcess
from typing import Any, Protocol, TypeVar, cast

from pyocd_debug_mcp.adapters.can_interface import (
    CANInterface,
    CanBackendCapabilities,
    CanBusHandle,
    CanFrame,
)
from pyocd_debug_mcp.can_config import LoadedCanFixtureConfig
from pyocd_debug_mcp.can_errors import (
    CAN_ERROR_CODES,
    CanCleanupError,
    CanConnectionError,
    CanError,
    CanOperationError,
    CanPolicyError,
    CanTimeoutError,
    CanWorkerError,
)


MAX_WORKER_OPERATION_SECONDS = 30.0


def _spawn_context():
    if os.name == "nt":
        from pyocd_debug_mcp.services.can_windows_spawn import CanSpawnContext

        return CanSpawnContext()
    return multiprocessing.get_context("spawn")


# One caller-visible budget covers OS spawn and child readiness. A pending
# spawn remains owned and explicitly uncertain after the caller times out.
MAX_WORKER_READINESS_SECONDS = 5.0
WORKER_POLL_SLICE_SECONDS = 0.05
MAX_CANCEL_ACK_SECONDS = 0.1
MAX_TERMINATE_GRACE_SECONDS = 0.25
MAX_WORKER_EXIT_SECONDS = 1.5
MAX_ERROR_MESSAGE_CHARACTERS = 1_024

_CleanupResult = TypeVar("_CleanupResult")


class _Connection(Protocol):
    """Portable structural type for multiprocessing pipe endpoints.

    Typeshed exposes Windows pipe endpoints as ``PipeConnection`` rather than
    the POSIX ``Connection`` class even though this shared subset is identical.
    Depending on the concrete class made otherwise-portable code fail native
    Windows type checking.
    """

    def close(self) -> None: ...

    def poll(self, timeout: float = 0.0) -> bool: ...

    def recv(self) -> Any: ...

    def recv_bytes(self) -> bytes: ...

    def send(self, obj: Any) -> None: ...

    def send_bytes(self, buf: bytes) -> None: ...


@dataclass(frozen=True)
class _WorkerControlChannels:
    """Spawn payload containing child-owned ends of two simplex channels."""

    cancellation_receiver: _Connection
    cleanup_sender: _Connection


@dataclass(eq=False)
class _RequestOwnership:
    """Linearization token for one synchronous caller that owns parent IPC."""

    generation: int
    cancellation_epoch: int
    done: threading.Event = field(default_factory=threading.Event)


async def await_cleanup_uninterruptibly(
    awaitable: Awaitable[_CleanupResult],
) -> tuple[_CleanupResult, bool]:
    """Finish one cleanup task despite repeated cancellation requests.

    The caller receives whether cancellation was observed while cleanup ran and
    can re-raise it only after the owned resource is retired or uncertainty is
    durably latched.
    """

    cleanup_task = asyncio.ensure_future(awaitable)
    interrupted = False
    while True:
        try:
            return await asyncio.shield(cleanup_task), interrupted
        except asyncio.CancelledError:
            interrupted = True
            if cleanup_task.done():
                return cleanup_task.result(), interrupted


def _strict_deadline(value: object, name: str, maximum: float) -> float:
    if type(value) not in {int, float}:
        raise ValueError(f"{name} must be a finite positive number")
    try:
        result = float(cast("int | float", value))
    except OverflowError as exc:
        raise ValueError(f"{name} must be a finite positive number") from exc
    if not math.isfinite(result) or not 0 < result <= maximum:
        raise ValueError(f"{name} must be greater than 0 and at most {maximum}")
    return result


def _install_linux_parent_death_signal(
    expected_parent_pid: int,
    *,
    stage_hook: Callable[[str], None] | None = None,
) -> bool:
    """Install PDEATHSIG and close the race by rechecking the parent PID."""

    if not sys_platform_linux():
        return True
    try:
        if stage_hook is not None:
            stage_hook("before_prctl")
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGTERM) != 0:  # PR_SET_PDEATHSIG
            return False
        if stage_hook is not None:
            stage_hook("after_prctl")
    except (AttributeError, OSError):
        return False
    parent_matches = os.getppid() == expected_parent_pid
    if stage_hook is not None:
        stage_hook("after_recheck")
    return parent_matches


def sys_platform_linux() -> bool:
    return os.name == "posix" and os.uname().sysname == "Linux"


def _supports_posix_worker_signals() -> bool:
    """Whether SIGTERM is a cooperative Python unwind on this host."""

    return os.name == "posix" and hasattr(signal, "SIGTERM")


def _process_terminate_is_hard() -> bool:
    """Whether ``multiprocessing.Process.terminate`` skips child cleanup."""

    return not _supports_posix_worker_signals()


def _request_worker_cancellation(cancel_requested: threading.Event) -> None:
    """Notify the worker main thread without using Windows hard termination."""

    cancel_requested.set()
    if _supports_posix_worker_signals():
        os.kill(os.getpid(), signal.SIGTERM)


class _WorkerCancelled(BaseException):
    """Internal signal used to unwind a blocking backend call into cleanup."""


def _build_python_can_adapter() -> CANInterface:
    # Keep python-can and its plugin/config surface out of the parent MCP
    # process. This function is called only after the child death guards exist.
    from pyocd_debug_mcp.adapters.can_pythoncan import PythonCanAdapter

    return PythonCanAdapter()


def _build_tcp_sim_adapter() -> CANInterface:
    from pyocd_debug_mcp.adapters.can_sim import TcpSimAdapter

    return TcpSimAdapter()


def _error_record(exc: BaseException) -> dict[str, str]:
    def bounded(message: str) -> str:
        if len(message) <= MAX_ERROR_MESSAGE_CHARACTERS:
            return message
        return f"{message[: MAX_ERROR_MESSAGE_CHARACTERS - 1]}…"

    if isinstance(exc, CanError):
        return {"code": exc.code, "message": bounded(exc.message)}
    return {
        "code": "can/worker-failure",
        "message": bounded(f"{type(exc).__name__}: {exc}"),
    }


def _run_worker(
    connection: _Connection,
    control_channels: _WorkerControlChannels,
    readiness_connection: _Connection,
    loaded: LoadedCanFixtureConfig,
    expected_parent_pid: int,
    adapter_factory: Callable[[], CANInterface],
    parent_guard_stage_hook: Callable[[str], None] | None = None,
) -> None:
    adapter: CANInterface | None = None
    handle: CanBusHandle | None = None
    active_operation: str | None = None
    open_failed_without_handle = False
    shutdown_close_failed = False
    watchdog_stop = threading.Event()
    cancel_requested = threading.Event()

    def cancel_signal(_signum: int, _frame: object) -> None:
        # The watchdog normally marks cancellation before signalling this
        # thread.  A loaded child can instead receive the parent's bounded TERM
        # escalation before the watchdog is scheduled.  Mark that path too so
        # the child still reports its definitive close outcome from ``finally``.
        cancel_requested.set()
        raise _WorkerCancelled

    if _supports_posix_worker_signals():
        signal.signal(signal.SIGTERM, cancel_signal)

    def watch_parent() -> None:
        try:
            while not watchdog_stop.wait(WORKER_POLL_SLICE_SECONDS):
                try:
                    if not control_channels.cancellation_receiver.poll(0):
                        continue
                    # Any message (including a malformed one), EOF, or broken
                    # parent endpoint revokes this worker generation.
                    control_channels.cancellation_receiver.recv_bytes()
                except (EOFError, OSError):
                    _request_worker_cancellation(cancel_requested)
                    return
                _request_worker_cancellation(cancel_requested)
                return
        finally:
            control_channels.cancellation_receiver.close()

    def raise_if_cancelled() -> None:
        if cancel_requested.is_set():
            raise _WorkerCancelled

    watchdog_thread: threading.Thread | None = None
    try:
        watchdog_thread = threading.Thread(
            target=watch_parent,
            name="byo-can-parent-watchdog",
            daemon=True,
        )
        watchdog_thread.start()
        if not _install_linux_parent_death_signal(
            expected_parent_pid,
            stage_hook=parent_guard_stage_hook,
        ):
            return
        adapter = adapter_factory()
        raise_if_cancelled()
        readiness_connection.send_bytes(b"ready")
        readiness_connection.close()
        while True:
            raise_if_cancelled()
            try:
                if not connection.poll(WORKER_POLL_SLICE_SECONDS):
                    continue
                request = connection.recv()
            except (EOFError, OSError):
                break
            raise_if_cancelled()
            if not isinstance(request, dict):
                connection.send(
                    {
                        "id": None,
                        "ok": False,
                        "error": _error_record(CanWorkerError("Invalid IPC request")),
                    }
                )
                continue
            request_id = request.get("id")
            operation = request.get("operation")
            arguments = request.get("arguments")
            if (
                type(request_id) is not int
                or not isinstance(operation, str)
                or not isinstance(arguments, dict)
            ):
                connection.send(
                    {
                        "id": request_id,
                        "ok": False,
                        "error": _error_record(CanWorkerError("Invalid IPC request fields")),
                    }
                )
                continue
            try:
                active_operation = operation
                if operation == "inspect":
                    if arguments:
                        raise CanWorkerError("Invalid inspect arguments")
                    result: object = adapter.inspect(loaded)
                elif operation == "open":
                    if set(arguments) != {"timeout_seconds"}:
                        raise CanWorkerError("Invalid open arguments")
                    if handle is not None:
                        raise CanWorkerError("CAN backend is already open")
                    backend_timeout = _strict_deadline(
                        arguments["timeout_seconds"],
                        "CAN open timeout",
                        MAX_WORKER_OPERATION_SECONDS,
                    )
                    handle = adapter.open(loaded, backend_timeout)
                    result = replace(handle, native_handle=None)
                elif operation == "send":
                    if set(arguments) != {
                        "frame",
                        "timeout_seconds",
                        "not_after_monotonic",
                    }:
                        raise CanWorkerError("Invalid send arguments")
                    if handle is None:
                        raise CanWorkerError("CAN backend is not open")
                    frame = arguments["frame"]
                    if not isinstance(frame, CanFrame):
                        raise CanWorkerError("Invalid CAN frame in send request")
                    backend_timeout = _strict_deadline(
                        arguments["timeout_seconds"],
                        "CAN send timeout",
                        MAX_WORKER_OPERATION_SECONDS,
                    )
                    not_after = arguments["not_after_monotonic"]
                    if not_after is not None:
                        if type(not_after) not in {int, float}:
                            raise CanWorkerError("Invalid absolute CAN send deadline")
                        try:
                            not_after = float(cast("int | float", not_after))
                        except OverflowError as exc:
                            raise CanWorkerError("Invalid absolute CAN send deadline") from exc
                        if not math.isfinite(not_after):
                            raise CanWorkerError("Invalid absolute CAN send deadline")
                        remaining = not_after - time.monotonic()
                        if remaining <= 0:
                            raise CanTimeoutError(
                                "CAN send deadline expired before backend submission"
                            )
                        backend_timeout = min(backend_timeout, remaining)
                    if not loaded.config.transmit.enabled:
                        raise CanPolicyError(
                            "can/tx-disabled", "CAN transmit is disabled by operator policy."
                        )
                    if frame.identifier not in loaded.config.transmit.exact_ids:
                        raise CanPolicyError(
                            "can/id-forbidden",
                            "Outgoing CAN identifier is absent from the exact transmit allowlist.",
                        )
                    result = adapter.send(
                        handle,
                        frame,
                        backend_timeout,
                        not_after_monotonic=not_after,
                    )
                elif operation == "receive":
                    if set(arguments) != {"timeout_seconds"}:
                        raise CanWorkerError("Invalid receive arguments")
                    if handle is None:
                        raise CanWorkerError("CAN backend is not open")
                    backend_timeout = _strict_deadline(
                        arguments["timeout_seconds"],
                        "CAN receive timeout",
                        MAX_WORKER_OPERATION_SECONDS,
                    )
                    receive_deadline = time.monotonic() + backend_timeout
                    result = None
                    while result is None:
                        raise_if_cancelled()
                        remaining = receive_deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        result = adapter.receive(
                            handle,
                            min(WORKER_POLL_SLICE_SECONDS, remaining),
                        )
                elif operation == "state":
                    if arguments:
                        raise CanWorkerError("Invalid state arguments")
                    if handle is None:
                        raise CanWorkerError("CAN backend is not open")
                    result = adapter.state(handle)
                elif operation == "shutdown":
                    if arguments:
                        raise CanWorkerError("Invalid shutdown arguments")
                    if handle is not None:
                        # Transfer ownership out of the finalizer before the
                        # potentially ambiguous close call. A raised close may
                        # already have released/recycled native resources and
                        # must never be replayed implicitly by ``finally``.
                        closing_handle = handle
                        handle = None
                        try:
                            adapter.close(closing_handle)
                        except BaseException:
                            shutdown_close_failed = True
                            raise
                    raise_if_cancelled()
                    connection.send({"id": request_id, "ok": True, "result": None})
                    return
                else:
                    raise CanWorkerError(f"Unknown CAN worker operation: {operation}")
            except Exception as exc:  # child serializes ordinary backend failures
                if operation == "open" and handle is None:
                    # CANInterface cannot prove that an open exception rolled
                    # back every native allocation.  Latch uncertainty until
                    # this generation is retired; process exit alone is not a
                    # cleanup receipt.
                    open_failed_without_handle = True
                active_operation = None
                connection.send({"id": request_id, "ok": False, "error": _error_record(exc)})
            else:
                active_operation = None
                raise_if_cancelled()
                connection.send({"id": request_id, "ok": True, "result": result})
    except _WorkerCancelled:
        pass
    finally:
        watchdog_stop.set()
        if watchdog_thread is not None and watchdog_thread is not threading.current_thread():
            watchdog_thread.join(WORKER_POLL_SLICE_SECONDS * 2)
        readiness_connection.close()
        cleanup_outcome = b"cleanup-failed" if shutdown_close_failed else b"closed"
        # An asynchronous cancellation can interrupt adapter.open() after its
        # backend has allocated a native resource but before a CanBusHandle is
        # returned to this owner.  With no registered handle there is no proof
        # that the resource was closed, so never emit a positive cleanup ACK.
        if (active_operation == "open" and handle is None) or open_failed_without_handle:
            cleanup_outcome = b"cleanup-unknown"
        if adapter is not None and handle is not None:
            try:
                adapter.close(handle)
            except BaseException:
                cleanup_outcome = b"cleanup-failed"
        if cancel_requested.is_set():
            try:
                control_channels.cleanup_sender.send_bytes(cleanup_outcome)
            except (BrokenPipeError, EOFError, OSError):
                pass
        control_channels.cleanup_sender.close()
        connection.close()


def _worker_main(
    connection: _Connection,
    control_channels: _WorkerControlChannels,
    readiness_connection: _Connection,
    loaded: LoadedCanFixtureConfig,
    expected_parent_pid: int,
) -> None:
    """Fixed production entrypoint; its spawn payload contains no code selector."""

    _run_worker(
        connection,
        control_channels,
        readiness_connection,
        loaded,
        expected_parent_pid,
        _build_tcp_sim_adapter
        if loaded.config.adapter.kind == "tcp_sim"
        else _build_python_can_adapter,
    )


def _remote_error(error: object) -> CanError:
    if not isinstance(error, dict) or set(error) != {"code", "message"}:
        return CanWorkerError("CAN worker returned a malformed error")
    code = error.get("code")
    message = error.get("message")
    if (
        not isinstance(code, str)
        or not isinstance(message, str)
        or not message
        or len(message) > MAX_ERROR_MESSAGE_CHARACTERS
    ):
        return CanWorkerError("CAN worker returned a malformed error")
    if code not in CAN_ERROR_CODES:
        return CanWorkerError("CAN worker returned an unknown error code")
    if code == "can/operation-timeout":
        return CanTimeoutError(message)
    if code == "can/worker-failure":
        return CanWorkerError(message)
    if code == "can/cleanup-failed":
        return CanCleanupError(message)
    if code in {"can/connection-failure", "can/capability-unproven", "can/backend-unsupported"}:
        return CanConnectionError(message, code=code)
    if code in {
        "can/tx-disabled",
        "can/id-forbidden",
        "can/invalid-argument",
        "can/rate-limited",
        "can/process-call-limit",
        "can/process-rx-limit",
        "can/process-artifact-limit",
        "can/target-mutation-forbidden",
        "can/fixture-sequence-forbidden",
        "can/terminal-sealed",
        "can/terminal-argument-invalid",
        "can/provider-host-isolation-required",
        "can/board-mismatch",
        "can/probe-mismatch",
    }:
        return CanPolicyError(code, message)
    return CanOperationError(message, code=code)


class CanWorkerClient:
    """Synchronous parent-side IPC client with TERM/KILL escalation."""

    def __init__(
        self,
        loaded: LoadedCanFixtureConfig,
        *,
        terminate_grace_seconds: float = 0.25,
    ) -> None:
        terminate_grace_seconds = _strict_deadline(
            terminate_grace_seconds,
            "terminate_grace_seconds",
            MAX_TERMINATE_GRACE_SECONDS,
        )
        self._loaded = loaded
        self._terminate_grace_seconds = terminate_grace_seconds
        self._launch_done: threading.Event | None = None
        self._launch_error: BaseException | None = None
        self._process: BaseProcess | None = None
        self._connection: _Connection | None = None
        self._cancellation_sender: _Connection | None = None
        self._cleanup_receiver: _Connection | None = None
        self._readiness_receiver: _Connection | None = None
        self._next_request_id = 1
        self._request_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._termination_lock = threading.Lock()
        self._request_owner: _RequestOwnership | None = None
        self._generation = 0
        self._cancellation_epoch = 0
        self._failure: str | None = None
        self._last_escalation_stage = "none"
        self._last_cleanup_outcome = "none"
        self._last_cleanup_generation: int | None = None

    @property
    def is_alive(self) -> bool:
        return self._process is not None and self._process.is_alive()

    @property
    def last_escalation_stage(self) -> str:
        return self._last_escalation_stage

    @property
    def last_cleanup_outcome(self) -> str:
        return self._last_cleanup_outcome

    def _worker_process_target(self) -> Callable[..., None]:
        """Return the fixed spawn target; test subclasses may replace the process."""

        return _worker_main

    def _worker_process_extra_arguments(self) -> tuple[object, ...]:
        return ()

    def _owner_was_cancelled(self, owner: _RequestOwnership) -> bool:
        with self._lifecycle_lock:
            return (
                self._request_owner is not owner
                or owner.cancellation_epoch != self._cancellation_epoch
                or owner.generation != self._generation
                or self._failure is not None
            )

    def _raise_if_owner_cancelled(self, owner: _RequestOwnership) -> None:
        with self._lifecycle_lock:
            if (
                self._request_owner is owner
                and owner.cancellation_epoch == self._cancellation_epoch
                and owner.generation == self._generation
                and self._failure is None
            ):
                return
            message = self._failure or "CAN worker generation was cancelled"
        raise CanWorkerError(message)

    def _linearize_response(self, owner: _RequestOwnership) -> None:
        """Accept one response atomically before a later cancellation epoch."""

        self._raise_if_owner_cancelled(owner)

    def _finish_request_owner(self, owner: _RequestOwnership) -> None:
        """Release borrowed IPC only after this caller can no longer touch it."""

        close_after_cancel: list[_Connection] = []
        try:
            with self._lifecycle_lock:
                if self._request_owner is owner:
                    self._request_owner = None
                if owner.cancellation_epoch != self._cancellation_epoch and (
                    self._launch_done is None or self._launch_done.is_set()
                ):
                    if self._connection is not None:
                        close_after_cancel.append(self._connection)
                        self._connection = None
                    if self._readiness_receiver is not None:
                        close_after_cancel.append(self._readiness_receiver)
                        self._readiness_receiver = None
            for endpoint in close_after_cancel:
                try:
                    endpoint.close()
                except OSError:
                    pass
        finally:
            owner.done.set()

    def _start(self, owner: _RequestOwnership) -> bool:
        """Bound the caller's wait while retaining ownership of a late OS spawn.

        No backend command is submitted until startup succeeds. If cancellation
        wins, the launch owner closes/reaps the late child and cannot revive the
        cancelled generation. Pending launch cleanup is never reported clean.
        """

        startup_deadline = time.monotonic() + MAX_WORKER_READINESS_SECONDS
        with self._lifecycle_lock:
            self._raise_if_owner_cancelled(owner)
            if self._process is not None:
                if not self._process.is_alive():
                    self._failure = "CAN worker exited unexpectedly"
                    raise CanWorkerError(self._failure)
                return False
            context = _spawn_context()
            allocated: list[_Connection] = []

            def new_pipe(*, duplex: bool) -> tuple[_Connection, _Connection]:
                pair = context.Pipe(duplex=duplex)
                allocated.extend(pair)
                return pair

            try:
                parent_connection, child_connection = new_pipe(duplex=True)
                cancellation_receiver, cancellation_sender = new_pipe(duplex=False)
                cleanup_receiver, cleanup_sender = new_pipe(duplex=False)
                readiness_receiver, readiness_sender = new_pipe(duplex=False)
                control_channels = _WorkerControlChannels(cancellation_receiver, cleanup_sender)
                process = context.Process(
                    target=self._worker_process_target(),
                    args=(
                        child_connection,
                        control_channels,
                        readiness_sender,
                        self._loaded,
                        os.getpid(),
                        *self._worker_process_extra_arguments(),
                    ),
                    name="byo-can-worker",
                    daemon=True,
                )
                self._generation += 1
                owner.generation = self._generation
                self._last_cleanup_outcome = "none"
                self._last_escalation_stage = "none"
                self._last_cleanup_generation = None
                self._connection = parent_connection
                self._cancellation_sender = cancellation_sender
                self._cleanup_receiver = cleanup_receiver
                self._readiness_receiver = readiness_receiver
                launch_done = threading.Event()
                self._launch_done = launch_done
                self._launch_error = None

                def launch() -> None:
                    # Thread.start can raise after creating its OS thread. The
                    # creator holds this lock through the start/exception path;
                    # only a still-authorized launcher may take endpoint ownership.
                    with self._lifecycle_lock:
                        revoked = (
                            owner.cancellation_epoch != self._cancellation_epoch
                            or self._failure is not None
                        )
                        if revoked:
                            for endpoint in (
                                child_connection,
                                cancellation_receiver,
                                cleanup_sender,
                                readiness_sender,
                            ):
                                endpoint.close()
                            self._last_cleanup_outcome = "not-running"
                            launch_done.set()
                    if revoked:
                        self._terminate(self._failure or "CAN startup cancelled before OS launch")
                        return
                    error: BaseException | None = None
                    try:
                        process.start()
                    except BaseException as exc:
                        error = exc
                    finally:
                        # These endpoints must remain valid throughout spawn's
                        # serialization/handle duplication, even after cancellation.
                        for endpoint in (
                            child_connection,
                            cancellation_receiver,
                            cleanup_sender,
                            readiness_sender,
                        ):
                            endpoint.close()
                    with self._lifecycle_lock:
                        self._launch_error = error
                        if process.pid is not None:
                            self._process = process
                        cancelled = owner.cancellation_epoch != self._cancellation_epoch
                        if error is not None:
                            self._failure = f"CAN worker process failed to start: {type(error).__name__}: {error}"
                        launch_done.set()
                    if cancelled or error is not None:
                        self._terminate(self._failure or "CAN late startup was cancelled")

                threading.Thread(target=launch, name="byo-can-launch-owner", daemon=True).start()

            except BaseException as exc:
                self._cancellation_epoch += 1
                for endpoint in allocated:
                    endpoint.close()
                if self._launch_done is not None:
                    self._launch_done.set()
                self._connection = None
                self._cancellation_sender = None
                self._cleanup_receiver = None
                self._readiness_receiver = None
                self._failure = f"CAN worker launch allocation failed: {type(exc).__name__}: {exc}"
                self._last_cleanup_outcome = "not-running"
                raise CanWorkerError(self._failure) from exc

        while not launch_done.wait(WORKER_POLL_SLICE_SECONDS):
            self._raise_if_owner_cancelled(owner)
            if time.monotonic() >= startup_deadline:
                self._terminate("CAN worker startup deadline expired", initiating_owner=owner)
                raise CanTimeoutError(
                    f"CAN worker startup exceeded {MAX_WORKER_READINESS_SECONDS} seconds; "
                    "late launch remains owned and cleanup is pending"
                )
        self._raise_if_owner_cancelled(owner)
        assert readiness_receiver is not None
        try:
            readiness_deadline = startup_deadline
            while True:
                self._raise_if_owner_cancelled(owner)
                remaining = readiness_deadline - time.monotonic()
                if remaining <= 0:
                    self._terminate("CAN worker readiness deadline expired", initiating_owner=owner)
                    raise CanTimeoutError(
                        f"CAN worker readiness exceeded {MAX_WORKER_READINESS_SECONDS} seconds"
                    )
                if readiness_receiver.poll(min(WORKER_POLL_SLICE_SECONDS, remaining)):
                    break
            acknowledgement = readiness_receiver.recv_bytes()
        except (EOFError, OSError) as exc:
            if self._owner_was_cancelled(owner):
                self._raise_if_owner_cancelled(owner)
            self._terminate("CAN worker exited before startup readiness", initiating_owner=owner)
            raise CanWorkerError(self._failure or str(exc)) from exc
        finally:
            readiness_receiver.close()
            with self._lifecycle_lock:
                if self._readiness_receiver is readiness_receiver:
                    self._readiness_receiver = None
        if acknowledgement != b"ready":
            self._terminate("CAN worker returned invalid startup readiness", initiating_owner=owner)
            raise CanWorkerError("CAN worker returned invalid startup readiness")
        self._raise_if_owner_cancelled(owner)
        return True

    def _terminate(
        self,
        reason: str,
        *,
        initiating_owner: _RequestOwnership | None = None,
    ) -> str:
        """Cancel, reap, and classify one generation without stealing owner IPC."""

        with self._termination_lock:
            termination_deadline = time.monotonic() + MAX_WORKER_EXIT_SECONDS
            with self._lifecycle_lock:
                # This is the cancellation linearization point. A request may
                # accept a response only if it observed the prior epoch first.
                self._cancellation_epoch += 1
                process = self._process
                connection = self._connection
                cancellation_sender = self._cancellation_sender
                cleanup_receiver = self._cleanup_receiver
                readiness = self._readiness_receiver
                request_owner = self._request_owner
                self._failure = reason
                if self._launch_done is not None and not self._launch_done.is_set():
                    # Do not close handles that Process.start may still be
                    # duplicating. No command can be sent by this revoked owner;
                    # the daemon launcher retains all handles and reaps later.
                    self._last_cleanup_outcome = "unknown"
                    self._last_escalation_stage = "startup-pending"
                    self._last_cleanup_generation = self._generation
                    return "unknown"

            cleanup_outcome = "not-running"
            receipt_valid = False
            hard_termination_invoked = False

            def process_is_alive() -> bool:
                if process is None:
                    return False
                try:
                    return process.is_alive()
                except (AssertionError, ValueError):
                    return False

            def receive_cleanup_ack(timeout: float = 0.0) -> None:
                """Consume at most one child-owned result from its simplex pipe."""

                nonlocal cleanup_outcome, receipt_valid
                if cleanup_receiver is None or hard_termination_invoked:
                    return
                try:
                    if not cleanup_receiver.poll(timeout):
                        return
                    acknowledgement = cleanup_receiver.recv_bytes()
                except (EOFError, OSError):
                    return
                receipt_valid = True
                if acknowledgement == b"closed":
                    cleanup_outcome = "closed"
                    self._last_escalation_stage = "cleanup-acknowledged"
                elif acknowledgement == b"cleanup-failed":
                    cleanup_outcome = "failed"
                    self._last_escalation_stage = "cleanup-failed"
                elif acknowledgement == b"cleanup-unknown":
                    cleanup_outcome = "unknown"
                    self._last_escalation_stage = "cleanup-unknown"
                else:
                    receipt_valid = False
                    cleanup_outcome = "unknown"
                    self._last_escalation_stage = "invalid-cleanup-ack"

            if process is not None:
                if process_is_alive():
                    self._last_escalation_stage = "cancel-requested"
                    cleanup_outcome = "unknown"
                    if cancellation_sender is not None:
                        try:
                            cancellation_sender.send_bytes(b"cancel")
                        except (BrokenPipeError, EOFError, OSError):
                            pass
                    cooperative_wait = (
                        MAX_TERMINATE_GRACE_SECONDS
                        if _process_terminate_is_hard()
                        else MAX_CANCEL_ACK_SECONDS
                    )
                    receive_cleanup_ack(cooperative_wait)
                    # A receipt means the child completed its own finalizer.
                    # Give it the ordinary grace window to exit before signals.
                    process.join(self._terminate_grace_seconds if receipt_valid else 0)
                else:
                    receive_cleanup_ack()
                    if not receipt_valid:
                        if self._last_cleanup_generation == self._generation and (
                            self._last_cleanup_outcome in {"closed", "failed", "unknown"}
                        ):
                            cleanup_outcome = self._last_cleanup_outcome
                        else:
                            cleanup_outcome = "unknown"
                            self._last_escalation_stage = "dead-without-cleanup-ack"

                if process_is_alive():
                    if cleanup_outcome == "closed":
                        self._last_escalation_stage = "term-after-cleanup"
                    elif cleanup_outcome == "failed":
                        self._last_escalation_stage = "term-after-cleanup-failure"
                    else:
                        self._last_escalation_stage = "term"
                    process.terminate()
                    if _process_terminate_is_hard():
                        # Windows TerminateProcess cannot run Python finally.
                        # A receipt buffered before this point is not sufficient
                        # to prove the process did not reacquire native state.
                        hard_termination_invoked = True
                        cleanup_outcome = "unknown"
                    process.join(self._terminate_grace_seconds)
                    receive_cleanup_ack()

                if process_is_alive():
                    if cleanup_outcome == "closed":
                        self._last_escalation_stage = "kill-after-cleanup"
                    elif cleanup_outcome == "failed":
                        self._last_escalation_stage = "kill-after-cleanup-failure"
                    else:
                        self._last_escalation_stage = "kill"
                    process.kill()
                    hard_termination_invoked = True
                    cleanup_outcome = "unknown"
                    process.join(max(0.0, termination_deadline - time.monotonic()))

                if process_is_alive():
                    self._last_escalation_stage = "kill-timeout"
                    self._failure = f"{reason}; worker remained alive after kill"
                    cleanup_outcome = "unknown"
                else:
                    process.join(timeout=0)
                    receive_cleanup_ack()
                    if hard_termination_invoked:
                        cleanup_outcome = "unknown"
            elif self._last_cleanup_generation == self._generation and (
                self._last_cleanup_outcome in {"closed", "failed", "unknown"}
            ):
                cleanup_outcome = self._last_cleanup_outcome

            # The current request thread exclusively owns request/readiness IPC.
            # A different cancelling thread waits for proof that it has left all
            # poll/recv calls before those endpoints can be closed.
            if request_owner is not None and request_owner is not initiating_owner:
                request_owner.done.wait(max(0.0, termination_deadline - time.monotonic()))
                if not request_owner.done.is_set():
                    cleanup_outcome = "unknown"
                    self._last_escalation_stage = "request-retirement-timeout"

            close_parent_ipc = request_owner is None or request_owner.done.is_set()
            with self._lifecycle_lock:
                self._last_cleanup_outcome = cleanup_outcome
                self._last_cleanup_generation = self._generation
                if self._cancellation_sender is cancellation_sender:
                    self._cancellation_sender = None
                if self._cleanup_receiver is cleanup_receiver:
                    self._cleanup_receiver = None
                if close_parent_ipc and self._connection is connection:
                    self._connection = None
                if close_parent_ipc and self._readiness_receiver is readiness:
                    self._readiness_receiver = None

            for endpoint in (cancellation_sender, cleanup_receiver):
                if endpoint is not None:
                    try:
                        endpoint.close()
                    except OSError:
                        pass
            if close_parent_ipc:
                for endpoint in (connection, readiness):
                    if endpoint is not None:
                        try:
                            endpoint.close()
                        except OSError:
                            pass
            return cleanup_outcome

    def cancel(self) -> None:
        """Cancel any command by terminating its isolated resource-owning process."""

        cleanup_outcome = self._terminate("CAN worker operation was cancelled")
        if cleanup_outcome not in {"closed", "not-running"}:
            raise CanCleanupError(
                "CAN worker cancellation could not prove successful backend cleanup"
            )

    def _request(
        self, operation: str, arguments: dict[str, object], *, timeout_seconds: float
    ) -> object:
        timeout = _strict_deadline(
            timeout_seconds, "worker request timeout", MAX_WORKER_OPERATION_SECONDS
        )
        deadline = time.monotonic() + timeout
        if not self._request_lock.acquire(timeout=timeout):
            raise CanTimeoutError(
                f"CAN worker operation {operation!r} timed out waiting for the active command"
            )
        with self._lifecycle_lock:
            if self._request_owner is not None:
                self._request_lock.release()
                raise CanWorkerError("CAN worker request ownership invariant was violated")
            owner = _RequestOwnership(
                generation=self._generation,
                cancellation_epoch=self._cancellation_epoch,
            )
            self._request_owner = owner
        try:
            return self._request_locked(
                operation,
                arguments,
                deadline=deadline,
                timeout=timeout,
                owner=owner,
            )
        finally:
            self._finish_request_owner(owner)
            self._request_lock.release()

    def _request_locked(
        self,
        operation: str,
        arguments: dict[str, object],
        *,
        deadline: float,
        timeout: float,
        owner: _RequestOwnership,
    ) -> object:
        newly_started = self._start(owner)
        if newly_started:
            deadline = time.monotonic() + timeout
        with self._lifecycle_lock:
            connection = self._connection
        self._raise_if_owner_cancelled(owner)
        if connection is None:
            raise CanWorkerError("CAN worker IPC connection is unavailable")
        request_id = self._next_request_id
        self._next_request_id += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            self._terminate(
                f"CAN worker operation {operation!r} timed out during startup",
                initiating_owner=owner,
            )
            raise CanTimeoutError(f"CAN worker operation {operation!r} exceeded {timeout} seconds")
        try:
            connection.send({"id": request_id, "operation": operation, "arguments": arguments})
        except (BrokenPipeError, EOFError, OSError) as exc:
            if self._owner_was_cancelled(owner):
                self._raise_if_owner_cancelled(owner)
            self._terminate("CAN worker IPC send failed", initiating_owner=owner)
            raise CanWorkerError(self._failure or str(exc)) from exc
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            self._terminate(
                f"CAN worker operation {operation!r} timed out after submission",
                initiating_owner=owner,
            )
            raise CanTimeoutError(f"CAN worker operation {operation!r} exceeded {timeout} seconds")
        try:
            response_ready = False
            while remaining > 0:
                self._raise_if_owner_cancelled(owner)
                if connection.poll(min(WORKER_POLL_SLICE_SECONDS, remaining)):
                    response_ready = True
                    break
                remaining = deadline - time.monotonic()
        except (EOFError, OSError) as exc:
            if self._owner_was_cancelled(owner):
                self._raise_if_owner_cancelled(owner)
            self._terminate("CAN worker IPC wait failed", initiating_owner=owner)
            raise CanWorkerError(self._failure or str(exc)) from exc
        if not response_ready:
            self._terminate(f"CAN worker operation {operation!r} timed out", initiating_owner=owner)
            raise CanTimeoutError(f"CAN worker operation {operation!r} exceeded {timeout} seconds")
        try:
            response = connection.recv()
        except (EOFError, OSError) as exc:
            if self._owner_was_cancelled(owner):
                self._raise_if_owner_cancelled(owner)
            self._terminate("CAN worker exited without a response", initiating_owner=owner)
            raise CanWorkerError(self._failure or str(exc)) from exc
        # This check is the response/cancellation linearization point. If
        # cancellation incremented the epoch first, even a buffered success is
        # stale and may not escape to the caller.
        self._linearize_response(owner)
        if not isinstance(response, dict) or response.get("id") != request_id:
            self._terminate(
                "CAN worker returned an invalid or out-of-order response",
                initiating_owner=owner,
            )
            raise CanWorkerError("CAN worker returned an invalid or out-of-order response")
        if response.get("ok") is True:
            if set(response) != {"id", "ok", "result"}:
                self._terminate(
                    "CAN worker returned a malformed success response", initiating_owner=owner
                )
                raise CanWorkerError("CAN worker returned a malformed success response")
            return response["result"]
        if response.get("ok") is False and set(response) == {"id", "ok", "error"}:
            remote = _remote_error(response.get("error"))
            if isinstance(remote, CanWorkerError):
                self._terminate(remote.message, initiating_owner=owner)
            raise remote
        self._terminate("CAN worker returned a malformed error response", initiating_owner=owner)
        raise CanWorkerError("CAN worker returned a malformed error response")

    def inspect(self, *, timeout_seconds: float) -> CanBackendCapabilities:
        result = self._request("inspect", {}, timeout_seconds=timeout_seconds)
        if not isinstance(result, CanBackendCapabilities):
            self._terminate("CAN worker returned invalid capability data")
            raise CanWorkerError("CAN worker returned invalid capability data")
        return result

    def open(self, *, timeout_seconds: float) -> CanBusHandle:
        result = self._request(
            "open",
            {"timeout_seconds": timeout_seconds},
            timeout_seconds=timeout_seconds,
        )
        if not isinstance(result, CanBusHandle):
            self._terminate("CAN worker returned an invalid handle")
            raise CanWorkerError("CAN worker returned an invalid handle")
        return result

    def send(
        self,
        frame: CanFrame,
        *,
        timeout_seconds: float,
        operation_timeout_seconds: float,
        not_after_monotonic: float | None = None,
    ) -> CanFrame:
        if not isinstance(frame, CanFrame):
            raise TypeError("frame must be a CanFrame")
        backend_timeout = _strict_deadline(
            timeout_seconds,
            "CAN send timeout",
            MAX_WORKER_OPERATION_SECONDS,
        )
        result = self._request(
            "send",
            {
                "frame": frame,
                "timeout_seconds": backend_timeout,
                "not_after_monotonic": not_after_monotonic,
            },
            timeout_seconds=operation_timeout_seconds,
        )
        if not isinstance(result, CanFrame):
            self._terminate("CAN worker returned an invalid send receipt")
            raise CanWorkerError("CAN worker returned an invalid send receipt")
        return result

    def receive(
        self, *, timeout_seconds: float, operation_timeout_seconds: float
    ) -> CanFrame | None:
        backend_timeout = _strict_deadline(
            timeout_seconds,
            "CAN receive timeout",
            MAX_WORKER_OPERATION_SECONDS,
        )
        result = self._request(
            "receive",
            {"timeout_seconds": backend_timeout},
            timeout_seconds=operation_timeout_seconds,
        )
        if result is not None and not isinstance(result, CanFrame):
            self._terminate("CAN worker returned an invalid frame")
            raise CanWorkerError("CAN worker returned an invalid frame")
        return result

    def state(self, *, timeout_seconds: float) -> str:
        result = self._request("state", {}, timeout_seconds=timeout_seconds)
        if not isinstance(result, str):
            self._terminate("CAN worker returned invalid state data")
            raise CanWorkerError("CAN worker returned invalid state data")
        return result

    def close(self, *, timeout_seconds: float) -> None:
        timeout = _strict_deadline(
            timeout_seconds,
            "worker close timeout",
            MAX_WORKER_OPERATION_SECONDS,
        )
        with self._lifecycle_lock:
            pending_launch = self._launch_done is not None and not self._launch_done.is_set()
            if self._process is None and not pending_launch:
                return
            terminate_reason = (
                "CAN worker close cancelled pending startup"
                if pending_launch
                else (
                    self._failure or "CAN worker was not alive during close"
                    if self._failure is not None
                    or self._process is None
                    or not self._process.is_alive()
                    else None
                )
            )
        if terminate_reason is not None:
            cleanup_outcome = self._terminate(terminate_reason)
            if cleanup_outcome not in {"closed", "not-running"}:
                raise CanCleanupError("CAN worker close could not prove successful backend cleanup")
            return
        deadline = time.monotonic() + timeout
        if not self._request_lock.acquire(blocking=False):
            self._terminate("CAN worker close cancelled an active operation")
            raise CanCleanupError(
                "CAN worker required forced cleanup because another operation was active"
            )
        with self._lifecycle_lock:
            if self._request_owner is not None:
                self._request_lock.release()
                raise CanWorkerError("CAN worker request ownership invariant was violated")
            owner = _RequestOwnership(
                generation=self._generation,
                cancellation_epoch=self._cancellation_epoch,
            )
            self._request_owner = owner
        try:
            self._request_locked("shutdown", {}, deadline=deadline, timeout=timeout, owner=owner)
        except CanError:
            # The shutdown response and the independent cleanup
            # acknowledgement race under host load. If escalation received the
            # explicit child-owned ``closed`` acknowledgement and reaped the
            # process, cleanup succeeded even though the ordinary response was
            # lost at its deadline. Unknown/failed cleanup still propagates.
            if not self.is_alive and self._last_cleanup_outcome == "closed":
                return
            if self.is_alive:
                self._terminate("CAN worker shutdown failed", initiating_owner=owner)
            raise
        finally:
            self._finish_request_owner(owner)
            self._request_lock.release()
        process_needs_termination = False
        with self._lifecycle_lock:
            process = self._process
            if process is None:
                return
            remaining = max(0.0, deadline - time.monotonic())
            # The backend is already closed when the shutdown response arrives.
            # Give interpreter/process teardown the caller's entire remaining
            # deadline; the ordinary TERM grace is for unacknowledged cleanup,
            # not for this proven-clean shutdown path. Nested spawn/stdio loads
            # can legitimately take longer than 250 ms to reap on Windows.
            process.join(remaining)
            if process.is_alive():
                process_needs_termination = True
            else:
                # The shutdown response proves that the child-owned backend close
                # completed, and the join proves this generation exited. Retire the
                # generation atomically so a second close is a true no-op; do not
                # route the dead, successfully closed process through crash cleanup.
                self._last_cleanup_outcome = "closed"
                self._last_escalation_stage = "graceful-shutdown"
                self._last_cleanup_generation = self._generation
                self._process = None
                if self._connection is not None:
                    self._connection.close()
                self._connection = None
                if self._cancellation_sender is not None:
                    self._cancellation_sender.close()
                self._cancellation_sender = None
                if self._cleanup_receiver is not None:
                    self._cleanup_receiver.close()
                self._cleanup_receiver = None
                if self._readiness_receiver is not None:
                    self._readiness_receiver.close()
                self._readiness_receiver = None
                try:
                    process.close()
                except (AttributeError, OSError, ValueError):
                    pass
        if process_needs_termination:
            # Never acquire the termination lock while holding lifecycle state;
            # a concurrent canceller takes those locks in the opposite phase.
            self._terminate("CAN worker did not exit after shutdown acknowledgment")
            raise CanWorkerError(
                self._failure or "CAN worker did not exit after shutdown acknowledgment"
            )


class AsyncCanWorkerClient:
    """Async cancellation-safe proxy; blocking IPC never runs on the event loop."""

    def __init__(self, worker: CanWorkerClient) -> None:
        self._worker = worker
        self._cleanup_error: str | None = None
        self._cancel_task: asyncio.Task[None] | None = None

    @property
    def cleanup_uncertain(self) -> bool:
        """Whether cancellation failed to prove resource cleanup."""

        return self._cleanup_error is not None

    @property
    def last_escalation_stage(self) -> str:
        return self._worker.last_escalation_stage

    @property
    def last_cleanup_outcome(self) -> str:
        return self._worker.last_cleanup_outcome

    async def _cancel_owned_worker(self) -> None:
        task = self._cancel_task
        if task is None:
            task = asyncio.create_task(asyncio.to_thread(self._worker.cancel))
            self._cancel_task = task
        try:
            await task
        except CanCleanupError as exc:
            self._cleanup_error = str(exc)
            if self._cancel_task is task:
                self._cancel_task = None
            raise
        except BaseException:
            if self._cancel_task is task:
                self._cancel_task = None
            raise

    async def _call(self, function: Callable[..., Any], /, *args: object, **kwargs: object) -> Any:
        call_task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(call_task)
        except asyncio.CancelledError:
            # Preserve asyncio's cancellation contract even when cleanup is
            # explicitly reported failed/uncertain. The synchronous worker
            # retains that outcome for health/evidence reporting. The shielded
            # to_thread task must also finish so no executor thread remains
            # blocked in worker IPC after this coroutine reports cancellation.
            try:
                try:
                    await await_cleanup_uninterruptibly(self._cancel_owned_worker())
                except CanCleanupError as exc:
                    self._cleanup_error = str(exc)
                except BaseException as exc:
                    message = f"CAN worker cancellation raised {type(exc).__name__}: {exc}"
                    self._cleanup_error = message[:MAX_ERROR_MESSAGE_CHARACTERS]
            finally:
                try:
                    await await_cleanup_uninterruptibly(call_task)
                except BaseException:
                    # Cancellation normally makes the synchronous request fail
                    # after the worker is reaped. Its error must not replace the
                    # caller-visible asyncio cancellation.
                    pass
            raise

    async def inspect(self, *, timeout_seconds: float) -> CanBackendCapabilities:
        return cast(
            CanBackendCapabilities,
            await self._call(self._worker.inspect, timeout_seconds=timeout_seconds),
        )

    async def open(self, *, timeout_seconds: float) -> CanBusHandle:
        return cast(
            CanBusHandle, await self._call(self._worker.open, timeout_seconds=timeout_seconds)
        )

    async def send(
        self,
        frame: CanFrame,
        *,
        timeout_seconds: float,
        operation_timeout_seconds: float,
        not_after_monotonic: float | None = None,
    ) -> CanFrame:
        return cast(
            CanFrame,
            await self._call(
                self._worker.send,
                frame,
                timeout_seconds=timeout_seconds,
                operation_timeout_seconds=operation_timeout_seconds,
                not_after_monotonic=not_after_monotonic,
            ),
        )

    async def receive(
        self, *, timeout_seconds: float, operation_timeout_seconds: float
    ) -> CanFrame | None:
        return cast(
            CanFrame | None,
            await self._call(
                self._worker.receive,
                timeout_seconds=timeout_seconds,
                operation_timeout_seconds=operation_timeout_seconds,
            ),
        )

    async def state(self, *, timeout_seconds: float) -> str:
        return cast(str, await self._call(self._worker.state, timeout_seconds=timeout_seconds))

    async def close(self, *, timeout_seconds: float) -> None:
        try:
            await self._call(self._worker.close, timeout_seconds=timeout_seconds)
        except CanCleanupError as exc:
            self._cleanup_error = str(exc)
            raise
        if self._cleanup_error is not None:
            raise CanCleanupError(self._cleanup_error)

    async def cancel(self) -> None:
        try:
            _, interrupted = await await_cleanup_uninterruptibly(self._cancel_owned_worker())
        except CanCleanupError as exc:
            self._cleanup_error = str(exc)
            raise
        if interrupted:
            raise asyncio.CancelledError
