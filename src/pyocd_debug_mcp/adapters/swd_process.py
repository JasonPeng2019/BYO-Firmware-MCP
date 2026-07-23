"""Process-isolated parent SWD adapter.

Each live native debug provider is owned by one persistent child process. The
parent holds only a proxy and frozen serializable metadata, so terminating the
child is a real cancellation and fault-containment boundary.
"""

from __future__ import annotations

import json
import math
import queue
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, NoReturn, Sequence, cast

from pyocd_debug_mcp.adapters.swd_interface import (
    SWDInterface,
    TargetSessionHandle,
    TargetSessionMetadata,
)
from pyocd_debug_mcp.board_config import BoardConfig
from pyocd_debug_mcp.kernel.operations import (
    CANCELLATION_CLEANUP_GRACE_SECONDS,
    current_operation,
)
from pyocd_debug_mcp.kernel.processes import ProcessMarkerStore, popen_owned, terminate_process_group
from pyocd_debug_mcp.target_errors import (
    LockedTargetError,
    ProbeNotFoundError,
    ResetLineUnavailableError,
    TargetConnectionError,
    TargetControlError,
    TargetStateError,
    UnsupportedArtifactError,
)
from pyocd_debug_mcp.timeouts import (
    DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS,
    ServerTimeoutConfig,
    default_server_timeout_config,
)

_VERSION = 1
_ERROR_TYPES: dict[str, type[TargetControlError]] = {
    "target_connection": TargetConnectionError,
    "target_control": TargetControlError,
    "locked_target": LockedTargetError,
    "probe_not_found": ProbeNotFoundError,
    "reset_line_unavailable": ResetLineUnavailableError,
    "target_state": TargetStateError,
    "unsupported_artifact": UnsupportedArtifactError,
}


def _strict_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"worker {name} result was not an integer")
    return value


def _memory_arguments(arguments: dict[str, Any], *, write: bool) -> tuple[int, int]:
    width_bits = arguments.get("width_bits")
    if not isinstance(width_bits, int) or isinstance(width_bits, bool) or width_bits not in {8, 16, 32}:
        raise ValueError("worker memory width was invalid")
    address = arguments.get("address")
    if not isinstance(address, int) or isinstance(address, bool) or address < 0:
        raise ValueError("worker memory address was invalid")
    if write:
        value = arguments.get("value")
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            or value > (1 << width_bits) - 1
        ):
            raise ValueError("worker memory write value was invalid for its width")
    return address, width_bits


def _validate_result(operation: str, value: object, arguments: dict[str, Any] | None = None) -> Any:
    void_operations = {
        "close",
        "write_memory",
        "write_core_register",
        "halt",
        "resume",
        "step",
        "reset",
        "reset_and_halt",
        "recover",
        "set_breakpoint",
        "remove_breakpoint",
        "release_reset",
    }
    if operation in void_operations:
        if value is not None:
            raise ValueError("worker void operation returned data")
        return None
    if operation == "flash":
        if value not in {"running", "halted", "reset_state_unconfirmed"}:
            raise ValueError("worker flash state result was invalid")
        return value
    if operation == "get_state":
        if not isinstance(value, str) or not value:
            raise ValueError("worker state result was invalid")
        return value
    if operation == "read_memory":
        if arguments is None:
            raise ValueError("worker memory result lacked request facts")
        _, width_bits = _memory_arguments(arguments, write=False)
        result = _strict_int(value, operation)
        if result < 0 or result > (1 << width_bits) - 1:
            raise ValueError("worker memory result exceeded its requested width")
        return result
    if operation == "read_core_register":
        return _strict_int(value, operation)
    if operation == "read_memory_block":
        if arguments is None:
            raise ValueError("worker memory block result lacked request facts")
        length = arguments.get("length")
        if not isinstance(length, int) or isinstance(length, bool) or length < 1:
            raise ValueError("worker memory block request length was invalid")
        if not isinstance(value, list) or len(value) != length:
            raise ValueError("worker memory block length did not match the request")
        result = [_strict_int(item, "memory block item") for item in value]
        if any(item < 0 or item > 255 for item in result):
            raise ValueError("worker memory block item exceeded byte range")
        return result
    if operation == "supported_core_registers":
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError("worker register-list result was invalid")
        return value
    if operation == "supports_recovery":
        if not isinstance(value, bool):
            raise ValueError("worker recovery capability result was invalid")
        return value
    if operation in {"open", "connect_under_reset"}:
        required = {
            "board_name",
            "probe_description",
            "probe_family",
            "probe_uid",
            "live_part_number",
            "route_used",
            "target_override",
            "runtime_token",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise ValueError("worker session metadata schema was invalid")
        for key in {"board_name", "probe_description", "probe_family"}:
            if not isinstance(value[key], str):
                raise ValueError(f"worker metadata field {key} was invalid")
        for key in {"route_used", "runtime_token"}:
            if not isinstance(value[key], str) or not value[key]:
                raise ValueError(f"worker metadata field {key} was invalid")
        for key in {"probe_uid", "live_part_number", "target_override"}:
            if value[key] is not None and not isinstance(value[key], str):
                raise ValueError(f"worker metadata field {key} was invalid")
        return value
    raise ValueError("worker returned an unallowlisted operation")


def _positive_timeout(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be a finite positive number")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return numeric


def _timeout(timeout: float | None) -> float:
    return _positive_timeout(
        DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS if timeout is None else timeout,
        "operation timeout",
    )


def _operation_deadline(operation_timeout_seconds: float | None = None) -> float:
    """Return the absolute provider deadline for the current call."""

    now = time.monotonic()
    if operation_timeout_seconds is not None:
        return now + _positive_timeout(operation_timeout_seconds, "operation timeout")
    operation = current_operation()
    if operation is None:
        return now + DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS
    deadline = (
        operation.started_at
        + operation.timeout_seconds
        - CANCELLATION_CLEANUP_GRACE_SECONDS
    )
    if not math.isfinite(deadline) or deadline <= now:
        raise TargetConnectionError(
            "The managed operation has no provider time remaining after reserving cleanup time."
        )
    return deadline


def _remaining(deadline: float) -> float:
    if isinstance(deadline, bool) or not isinstance(deadline, int | float):
        raise ValueError("worker deadline must be a finite number")
    numeric = float(deadline)
    if not math.isfinite(numeric):
        raise ValueError("worker deadline must be a finite number")
    remaining = numeric - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("worker request deadline elapsed")
    return remaining


class _WorkerClient:
    """One serial, owned worker protocol connection."""

    def __init__(
        self,
        *,
        worker_argv: Sequence[str] | None = None,
        deadline: float | None = None,
    ) -> None:
        self._guard = threading.RLock()
        self._request_id = 0
        self._closed = False
        self._cleanup_confirmed = False
        self._responses: queue.Queue[object] = queue.Queue()
        startup_deadline = deadline if deadline is not None else _operation_deadline()
        argv = tuple(worker_argv or (sys.executable, "-m", "pyocd_debug_mcp.adapters.provider_worker"))
        self._process, self._marker = popen_owned(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            bufsize=0,
        )
        try:
            self._start_reader(self._process.stdout, self._responses)
            ready = self._read(startup_deadline)
            if not isinstance(ready, dict) or ready != {"version": _VERSION, "ready": True}:
                raise ValueError("worker did not provide the required ready handshake")
        except BaseException as exc:
            self._invalidate(f"Worker startup failed: {type(exc).__name__}: {exc}.")

    @staticmethod
    def _start_reader(stream: Any, responses: queue.Queue[object]) -> None:
        def read() -> None:
            try:
                pending = bytearray()
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        responses.put(EOFError("worker protocol stream closed"))
                        return
                    pending.extend(chunk)
                    while b"\n" in pending:
                        raw_frame, _, remainder = pending.partition(b"\n")
                        pending = bytearray(remainder)
                        try:
                            responses.put(json.loads(raw_frame.decode("utf-8")))
                        except (UnicodeError, json.JSONDecodeError) as exc:
                            responses.put(ValueError(f"invalid worker frame: {exc}"))
                            return
            except BaseException as exc:  # reader must wake a bounded parent request
                responses.put(exc)

        threading.Thread(target=read, daemon=True).start()

    def _close_pipes(self) -> None:
        for stream in (self._process.stdin, self._process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass

    def _terminate(self) -> bool:
        try:
            cleaned = terminate_process_group(self._process)
        except Exception:
            return False
        finally:
            self._close_pipes()
        if cleaned:
            try:
                self._process.poll()
            except OSError:
                pass
        return cleaned

    def _remove_confirmed_marker(self, context: str) -> None:
        """Remove a dead worker's marker or report typed, retryable cleanup."""

        marker = self._marker
        if marker is None:
            return
        try:
            ProcessMarkerStore.remove(marker)
        except OSError as exc:
            raise TargetConnectionError(
                f"{context} Recovery marker removal failed: "
                f"{type(exc).__name__}: {exc}. The marker remains retained and close may retry it."
            ) from exc
        self._marker = None

    def _read(self, deadline: float) -> object:
        try:
            item = self._responses.get(timeout=_remaining(deadline))
        except queue.Empty as exc:
            raise TimeoutError("worker did not reply before the deadline") from exc
        if isinstance(item, BaseException):
            raise item
        return item

    def _write(self, frame: bytes, deadline: float) -> None:
        stdin = self._process.stdin
        if stdin is None:
            raise EOFError("worker stdin is unavailable")
        completed = threading.Event()
        failure: list[BaseException] = []

        def write() -> None:
            try:
                stdin.write(frame)
                stdin.flush()
            except BaseException as exc:
                failure.append(exc)
            finally:
                completed.set()

        threading.Thread(target=write, daemon=True).start()
        if not completed.wait(_remaining(deadline)):
            raise TimeoutError("worker pipe write exceeded the deadline")
        if failure:
            raise failure[0]

    def _invalidate(self, reason: str) -> NoReturn:
        if self._closed:
            suffix = (
                " Recovery marker remains retained."
                if not self._cleanup_confirmed or self._marker is not None
                else ""
            )
            raise TargetConnectionError(f"{reason}{suffix}")
        self._closed = True
        self._cleanup_confirmed = self._terminate()
        if self._cleanup_confirmed:
            self._remove_confirmed_marker(f"{reason} Worker was terminated.")
            raise TargetConnectionError(f"{reason} Worker was terminated.")
        raise TargetConnectionError(
            f"{reason} Worker termination could not be confirmed; recovery marker retained."
        )

    def call(
        self,
        operation: str,
        arguments: dict[str, Any],
        *,
        deadline: float | None = None,
        timeout: float | None = None,
    ) -> Any:
        if deadline is not None and timeout is not None:
            raise ValueError("pass either deadline or timeout, not both")
        call_deadline = (
            deadline
            if deadline is not None
            else time.monotonic() + _timeout(timeout)
        )
        _remaining(call_deadline)
        with self._guard:
            if self._closed:
                raise TargetConnectionError("The worker session is no longer live; reconnect and validate.")
            self._request_id += 1
            request_id = self._request_id
            payload = {
                "version": _VERSION,
                "request_id": request_id,
                "operation": operation,
                "arguments": arguments,
            }
            if operation == "read_memory":
                _memory_arguments(arguments, write=False)
            elif operation == "write_memory":
                _memory_arguments(arguments, write=True)
            raw = (json.dumps(payload, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
            try:
                self._write(raw, call_deadline)
                reply = self._read(call_deadline)
            except BaseException as exc:
                self._invalidate(f"Worker {operation} failed: {type(exc).__name__}: {exc}.")
            if not isinstance(reply, dict) or reply.get("version") != _VERSION:
                self._invalidate("Worker returned a malformed protocol reply.")
            if reply.get("request_id") != request_id:
                self._invalidate("Worker returned a stale or mismatched protocol reply.")
            if reply.get("ok") is True:
                if set(reply) != {"version", "request_id", "ok", "result"}:
                    self._invalidate("Worker returned an invalid success envelope.")
                try:
                    return _validate_result(operation, reply.get("result"), arguments)
                except ValueError as exc:
                    self._invalidate(f"Worker returned an invalid {operation} result: {exc}.")
            if reply.get("ok") is not False or set(reply) != {"version", "request_id", "ok", "error"}:
                self._invalidate("Worker returned an invalid error envelope.")
            error = reply.get("error")
            if not isinstance(error, dict) or set(error) != {"kind", "message"}:
                self._invalidate("Worker returned an invalid typed error envelope.")
            kind = error.get("kind")
            if kind not in {*_ERROR_TYPES, "provider_failure"}:
                self._invalidate("Worker returned an unallowlisted error kind.")
            message = error.get("message")
            if not isinstance(message, str):
                self._invalidate("Worker returned an invalid error message.")
            if kind == "provider_failure":
                raise TargetConnectionError(f"Worker {operation} failed: {kind}: {message}")
            error_type = _ERROR_TYPES.get(cast(str, kind))
            if error_type is None:
                self._invalidate("Worker returned an unallowlisted error kind.")
            raise error_type(f"Worker {operation} failed: {message}")

    def close(self, *, deadline: float | None = None) -> None:
        with self._guard:
            if self._closed:
                if self._cleanup_confirmed:
                    self._remove_confirmed_marker("Worker process cleanup was confirmed.")
                    return
                raise TargetConnectionError(
                    "Worker cleanup could not be confirmed; recovery marker retained."
                )
            try:
                try:
                    close_deadline = deadline if deadline is not None else _operation_deadline()
                    if close_deadline > time.monotonic():
                        self.call("close", {}, deadline=close_deadline)
                except Exception:
                    # Process termination is the ownership authority. Graceful-close
                    # failures, including a deadline race, are only diagnostics.
                    pass
            finally:
                if not self._closed:
                    self._closed = True
                    self._cleanup_confirmed = self._terminate()
                    if self._cleanup_confirmed:
                        self._remove_confirmed_marker("Worker process cleanup was confirmed.")
            if self._cleanup_confirmed:
                return
            raise TargetConnectionError(
                "Worker cleanup could not be confirmed; recovery marker retained."
            )


def _board_record(board: BoardConfig | None) -> dict[str, Any] | None:
    if board is None:
        return None
    record = asdict(board)
    record["source_path"] = str(board.source_path) if board.source_path is not None else None
    return record


class ProcessIsolatedSWDInterface(SWDInterface):
    """SWD proxy whose worker, not the MCP process, owns native providers."""

    def _open(
        self,
        operation: str,
        *,
        board: BoardConfig | None,
        unique_id: str | None,
        target: str | None,
        server_timeouts: ServerTimeoutConfig | None,
        protocol: str | None = None,
        connect_mode: str | None = None,
        pack_path: Path | None = None,
        pack_sha256: str | None = None,
        pdsc_device: str | None = None,
        frequency_hz: int | None = None,
        operation_timeout_seconds: float | None = None,
    ) -> TargetSessionHandle:
        deadline = _operation_deadline(operation_timeout_seconds)
        worker = _WorkerClient(deadline=deadline)
        arguments = {
            "board": _board_record(board),
            "unique_id": unique_id,
            "target": target,
            "server_timeouts": (server_timeouts or default_server_timeout_config()).to_record(),
            "protocol": protocol,
            "connect_mode": connect_mode,
            "pack_path": str(pack_path) if pack_path is not None else None,
            "pack_sha256": pack_sha256,
            "pdsc_device": pdsc_device,
            "frequency_hz": frequency_hz,
        }
        try:
            raw_metadata = worker.call(operation, arguments, deadline=deadline)
            metadata = TargetSessionMetadata(**cast(dict[str, Any], raw_metadata))
            return TargetSessionHandle(
                session=None,
                board=board,
                probe_uid=metadata.probe_uid,
                route_used=metadata.route_used,
                target_override=metadata.target_override,
                worker=worker,
                metadata=metadata,
            )
        except BaseException as primary:
            try:
                worker.close(deadline=deadline)
            except BaseException as cleanup:
                raise primary from cleanup
            raise

    def open(
        self,
        *,
        board: BoardConfig | None,
        unique_id: str | None,
        target: str | None,
        server_timeouts: ServerTimeoutConfig | None = None,
        protocol: str | None = None,
        connect_mode: str | None = None,
        pack_path: Path | None = None,
        pack_sha256: str | None = None,
        pdsc_device: str | None = None,
        frequency_hz: int | None = None,
        operation_timeout_seconds: float | None = None,
    ) -> TargetSessionHandle:
        return self._open(
            "open",
            board=board,
            unique_id=unique_id,
            target=target,
            server_timeouts=server_timeouts,
            protocol=protocol,
            connect_mode=connect_mode,
            pack_path=pack_path,
            pack_sha256=pack_sha256,
            pdsc_device=pdsc_device,
            frequency_hz=frequency_hz,
            operation_timeout_seconds=operation_timeout_seconds,
        )

    def connect_under_reset(
        self,
        *,
        board: BoardConfig | None,
        unique_id: str | None,
        target: str | None,
        server_timeouts: ServerTimeoutConfig | None = None,
        pack_path: Path | None = None,
        pack_sha256: str | None = None,
        pdsc_device: str | None = None,
        operation_timeout_seconds: float | None = None,
    ) -> TargetSessionHandle:
        return self._open(
            "connect_under_reset",
            board=board,
            unique_id=unique_id,
            target=target,
            server_timeouts=server_timeouts,
            pack_path=pack_path,
            pack_sha256=pack_sha256,
            pdsc_device=pdsc_device,
            operation_timeout_seconds=operation_timeout_seconds,
        )

    @staticmethod
    def _worker(handle: TargetSessionHandle) -> _WorkerClient:
        if not isinstance(handle.worker, _WorkerClient):
            raise TargetConnectionError("Target session has no live process worker.")
        return handle.worker

    def _call(
        self,
        handle: TargetSessionHandle,
        operation: str,
        arguments: dict[str, Any] | None = None,
        *,
        operation_timeout_seconds: float | None = None,
    ) -> Any:
        return self._worker(handle).call(
            operation,
            arguments or {},
            deadline=_operation_deadline(operation_timeout_seconds),
        )

    def close(self, handle: TargetSessionHandle) -> None:
        try:
            deadline = _operation_deadline()
        except TargetConnectionError:
            deadline = time.monotonic()
        self._worker(handle).close(deadline=deadline)

    def get_state(self, handle: TargetSessionHandle) -> str:
        return cast(str, self._call(handle, "get_state"))

    def read_memory(
        self,
        handle: TargetSessionHandle,
        address: int,
        width_bits: int,
        *,
        operation_timeout_seconds: float | None = None,
    ) -> int:
        return cast(
            int,
            self._call(
                handle,
                "read_memory",
                {"address": address, "width_bits": width_bits},
                operation_timeout_seconds=operation_timeout_seconds,
            ),
        )

    def read_memory_block(self, handle: TargetSessionHandle, address: int, length: int) -> list[int]:
        return cast(
            list[int],
            self._call(handle, "read_memory_block", {"address": address, "length": length}),
        )

    def write_memory(
        self, handle: TargetSessionHandle, address: int, value: int, width_bits: int
    ) -> None:
        self._call(
            handle,
            "write_memory",
            {"address": address, "value": value, "width_bits": width_bits},
        )

    def read_core_register(self, handle: TargetSessionHandle, name: str) -> int:
        return cast(int, self._call(handle, "read_core_register", {"name": name}))

    def write_core_register(self, handle: TargetSessionHandle, name: str, value: int) -> None:
        self._call(handle, "write_core_register", {"name": name, "value": value})

    def supported_core_registers(self, handle: TargetSessionHandle) -> tuple[str, ...]:
        return tuple(self._call(handle, "supported_core_registers"))

    def halt(self, handle: TargetSessionHandle) -> None:
        self._call(handle, "halt")

    def resume(self, handle: TargetSessionHandle) -> None:
        self._call(handle, "resume")

    def step(self, handle: TargetSessionHandle) -> None:
        self._call(handle, "step")

    def reset(self, handle: TargetSessionHandle) -> None:
        self._call(handle, "reset")

    def reset_and_halt(self, handle: TargetSessionHandle) -> None:
        self._call(handle, "reset_and_halt")

    def release_reset(self, handle: TargetSessionHandle) -> None:
        self._call(handle, "release_reset")

    def flash(
        self,
        handle: TargetSessionHandle,
        firmware: Path,
        *,
        halt_after_reset: bool,
    ) -> str:
        return cast(str, self._call(
            handle,
            "flash",
            {"path": str(firmware), "halt_after_reset": halt_after_reset},
        ))

    def recover(self, handle: TargetSessionHandle) -> None:
        self._call(handle, "recover")

    def supports_recovery(self, handle: TargetSessionHandle, mechanism: str) -> bool:
        return cast(
            bool,
            self._call(handle, "supports_recovery", {"mechanism": mechanism}),
        )

    def set_breakpoint(self, handle: TargetSessionHandle, address: int) -> None:
        self._call(handle, "set_breakpoint", {"address": address})

    def remove_breakpoint(self, handle: TargetSessionHandle, address: int) -> None:
        self._call(handle, "remove_breakpoint", {"address": address})
