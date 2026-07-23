"""Process-isolated parent debug-provider adapter.

Each live debug provider is owned by one persistent child process. The
parent holds only a proxy and frozen serializable metadata, so terminating the
child is a real cancellation and fault-containment boundary.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, NoReturn, Sequence, cast

from firmware_mcp.adapters.debug_interface import (
    FlashVerification,
    PhysicalMemoryRegion,
    RecoveryCapability,
    RecoveryResult,
    DebugInterface,
    TargetSessionHandle,
    TargetSessionMetadata,
)
from firmware_mcp.board_config import BoardConfig
from firmware_mcp.kernel.operations import current_operation
from firmware_mcp.kernel.processes import (
    ProcessMarkerStore,
    popen_owned,
    terminate_process_group,
)
from firmware_mcp.target_errors import (
    CleanupDiagnostic,
    LockedTargetError,
    FlashFinalResetFailed,
    FlashFinalResetUncertain,
    ProbeNotFoundError,
    ResetLineUnavailableError,
    TargetConnectionCleanupError,
    TargetConnectionError,
    TargetControlError,
    TargetStateError,
    UnsupportedArtifactError,
)

# Version 4 adds strict, provider-neutral cleanup diagnostics to typed errors.
_VERSION = 4
_ERROR_TYPES: dict[str, type[TargetControlError]] = {
    "target_connection_cleanup": TargetConnectionCleanupError,
    "target_connection": TargetConnectionError,
    "target_control": TargetControlError,
    "locked_target": LockedTargetError,
    "probe_not_found": ProbeNotFoundError,
    "reset_line_unavailable": ResetLineUnavailableError,
    "target_state": TargetStateError,
    "unsupported_artifact": UnsupportedArtifactError,
}


def _cleanup_diagnostics_from_worker(value: object) -> tuple[CleanupDiagnostic, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("cleanup diagnostics were missing")
    diagnostics: list[CleanupDiagnostic] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "stage",
            "status",
            "error_type",
            "error_message",
            "recovery",
        }:
            raise ValueError("cleanup diagnostic had an invalid schema")
        stage = item.get("stage")
        status = item.get("status")
        error_type = item.get("error_type")
        error_message = item.get("error_message")
        recovery = item.get("recovery")
        if (
            stage not in {"reset_release", "session_close"}
            or status != "unconfirmed"
            or not all(
                isinstance(part, str) and part for part in (error_type, error_message, recovery)
            )
        ):
            raise ValueError("cleanup diagnostic had invalid values")
        diagnostics.append(
            CleanupDiagnostic(
                stage=cast(str, stage),
                error_type=cast(str, error_type),
                error_message=cast(str, error_message),
                recovery=cast(str, recovery),
            )
        )
    return tuple(diagnostics)


def _strict_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"worker {name} result was not an integer")
    return value


def _memory_arguments(arguments: dict[str, Any], *, write: bool) -> tuple[int, int]:
    width_bits = arguments.get("width_bits")
    if (
        not isinstance(width_bits, int)
        or isinstance(width_bits, bool)
        or width_bits not in {8, 16, 32}
    ):
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
        "set_breakpoint",
        "remove_breakpoint",
        "release_reset",
    }
    if operation in void_operations:
        if value is not None:
            raise ValueError("worker void operation returned data")
        return None
    if operation == "flash":
        return FlashVerification.from_record(value, allow_uncertain_final_reset=True)
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
    if operation == "physical_memory_regions":
        if not isinstance(value, list):
            raise ValueError("worker physical-memory regions result was invalid")
        regions = tuple(PhysicalMemoryRegion.from_record(item) for item in value)
        if not regions:
            raise ValueError("worker physical-memory regions result was empty")
        session_tokens = {region.session_token for region in regions}
        if len(session_tokens) != 1:
            raise ValueError("worker physical-memory regions mixed target sessions")
        previous_end = -1
        for region in sorted(regions, key=lambda item: (item.start, item.end)):
            if region.start < previous_end:
                raise ValueError("worker physical-memory regions overlapped ambiguously")
            previous_end = region.end
        return regions
    if operation == "supported_core_registers":
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError("worker register-list result was invalid")
        return value
    if operation == "recovery_capabilities":
        if not isinstance(value, list):
            raise ValueError("worker recovery capabilities result was invalid")
        result = tuple(RecoveryCapability.from_record(item) for item in value)
        if len({item.mechanism for item in result}) != len(result):
            raise ValueError("worker recovery capabilities have duplicate mechanisms")
        return result
    if operation == "recover":
        return RecoveryResult.from_record(value)
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
            "live_identity",
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
        identity = value["live_identity"]
        required_identity = {
            "capability",
            "part_number",
            "provenance",
            "support_identity",
            "evidence",
        }
        if not isinstance(identity, dict) or set(identity) != required_identity:
            raise ValueError("worker live identity schema was invalid")
        if identity["capability"] not in {"exact", "compatible", "unavailable"}:
            raise ValueError("worker live identity capability was invalid")
        if identity["part_number"] is not None and not isinstance(identity["part_number"], str):
            raise ValueError("worker live identity part_number was invalid")
        if identity["capability"] != "exact" and identity["part_number"] is not None:
            raise ValueError("non-exact worker live identity must not claim a part_number")
        if any(
            not isinstance(identity[key], str) or not identity[key].strip()
            for key in {"provenance", "support_identity"}
        ):
            raise ValueError("worker live identity provenance was invalid")
        try:
            json.dumps(identity["evidence"], allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("worker live identity evidence was not JSON") from exc
        if identity["capability"] == "unavailable":
            evidence = identity["evidence"]
            if (
                not isinstance(evidence, dict)
                or not isinstance(evidence.get("reason"), str)
                or not evidence["reason"].strip()
            ):
                raise ValueError("unavailable worker live identity requires a reason")
        return value
    raise ValueError("worker returned an unallowlisted operation")


class _WorkerClient:
    """One serial, owned worker protocol connection."""

    def __init__(
        self,
        *,
        worker_argv: Sequence[str] | None = None,
    ) -> None:
        self._guard = threading.RLock()
        self._request_id = 0
        self._closed = False
        self._cleanup_confirmed = False
        self._cleanup_diagnostics: list[str] = []
        self._responses: queue.Queue[object] = queue.Queue()
        # A spawned worker is request-owned before it can emit its ready frame.
        # Promotion explicitly detaches this callback only after the board
        # connection transaction commits; a hanging bootstrap is never left
        # mutating after its MCP request is cancelled.
        self._bootstrap_owner = current_operation()
        self._bootstrap_callback: Callable[[], object] | None = None
        self._bootstrap_detached = False
        self._bootstrap_cleanup_started = False
        self._bootstrap_cleanup_evidence: dict[str, object] | None = None
        argv = tuple(worker_argv or (sys.executable, "-m", "firmware_mcp.adapters.provider_worker"))
        self._process, self._marker = popen_owned(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            bufsize=0,
        )
        if self._bootstrap_owner is not None:
            self._bootstrap_callback = self._cancel_bootstrap
            self._bootstrap_owner.add_cancellation_callback(self._bootstrap_callback)
        try:
            self._start_reader(self._process.stdout, self._responses)
            ready = self._read()
            if not isinstance(ready, dict) or ready != {"version": _VERSION, "ready": True}:
                raise ValueError("worker did not provide the required ready handshake")
            self._raise_if_bootstrap_cancelled()
        except BaseException as exc:
            if (
                self._bootstrap_owner is not None
                and self._bootstrap_owner.cancellation_requested.is_set()
            ):
                self._finish_cancelled_bootstrap()
                self._bootstrap_owner.checkpoint()
            self._invalidate(f"Worker startup failed: {type(exc).__name__}: {exc}.")

    def _cancel_bootstrap(self) -> dict[str, object] | None:
        """Terminate a not-yet-promoted worker when its request is cancelled."""

        with self._guard:
            if self._bootstrap_detached:
                return
        # Return the cached transaction evidence even when promotion rollback
        # won the race.  The cancellation owner needs the same marker-removal
        # diagnostic as the rollback closer, not a silent ``None``.
        return self._finish_cancelled_bootstrap()

    def _raise_if_bootstrap_cancelled(self) -> None:
        owner = self._bootstrap_owner
        if owner is not None and owner.cancellation_requested.is_set():
            self._finish_cancelled_bootstrap()
            owner.checkpoint()

    def _finish_cancelled_bootstrap(self) -> dict[str, object] | None:
        """Close a cancelled bootstrap once and publish one shared outcome.

        The worker lock deliberately spans termination and marker handling.  A
        promotion rollback that calls :meth:`close` therefore either performs
        this transaction or observes this exact completed evidence; it can
        never issue a second termination or marker decision for this worker.
        """

        with self._guard:
            if self._bootstrap_detached:
                return None
            existing = getattr(self, "_bootstrap_cleanup_evidence", None)
            if self._bootstrap_cleanup_started:
                return dict(existing) if existing is not None else None
            if self._closed:
                return None

            self._bootstrap_cleanup_started = True
            cleaned = self._terminate()
            self._closed = True
            self._cleanup_confirmed = cleaned
            owner = self._bootstrap_owner
            if cleaned:
                try:
                    self._remove_confirmed_marker("Cancelled worker bootstrap was terminated.")
                except TargetConnectionError as exc:
                    message = str(exc)
                    self._cleanup_diagnostics.append(message)
                    if owner is not None:
                        owner.resources.cleanup_errors.append(message)
                        owner.resources.fatal_cleanup_errors.append(message)
            else:
                message = (
                    "worker bootstrap cancellation could not be confirmed; recovery marker retained"
                )
                self._cleanup_diagnostics.append(message)
                if owner is not None:
                    owner.resources.cleanup_errors.append(message)
                    owner.resources.fatal_cleanup_errors.append(message)

            evidence = {
                "closed": cleaned,
                "graceful": False,
                "marker_retained": self._marker is not None,
                "diagnostic": "; ".join(self._cleanup_diagnostics) or None,
            }
            self._bootstrap_cleanup_evidence = evidence
            return dict(evidence)

    def promote_to_session(self) -> None:
        """Detach bootstrap cancellation only after the board session commits."""

        owner = self._bootstrap_owner
        callback = self._bootstrap_callback
        if owner is None or callback is None:
            return
        with self._guard:
            owner.checkpoint()
            self._bootstrap_detached = True
        owner.remove_cancellation_callback(callback)

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
        if not hasattr(self, "_cleanup_diagnostics"):
            self._cleanup_diagnostics = []
        for stream in (self._process.stdin, self._process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError) as exc:
                    self._cleanup_diagnostics.append(
                        f"pipe close failed: {type(exc).__name__}: {exc}"
                    )

    def _terminate(self) -> bool:
        if not hasattr(self, "_cleanup_diagnostics"):
            self._cleanup_diagnostics = []
        try:
            cleaned = terminate_process_group(self._process)
        except Exception as exc:
            self._cleanup_diagnostics.append(
                f"worker termination failed: {type(exc).__name__}: {exc}"
            )
            return False
        finally:
            self._close_pipes()
        if cleaned:
            try:
                self._process.poll()
            except OSError as exc:
                self._cleanup_diagnostics.append(
                    f"worker poll after termination failed: {type(exc).__name__}: {exc}"
                )
        return cleaned

    def _remove_confirmed_marker(self, context: str) -> None:
        """Remove a dead worker's marker or report a typed retained-marker failure."""

        marker = self._marker
        if marker is None:
            return
        try:
            ProcessMarkerStore.remove(marker)
        except OSError as exc:
            raise TargetConnectionError(
                f"{context} Recovery marker removal failed: "
                f"{type(exc).__name__}: {exc}. The marker remains retained."
            ) from exc
        self._marker = None

    def _read(self) -> object:
        # EOF or request-owned cancellation terminates the worker and wakes the
        # reader; no server deadline or polling quantum frames this protocol read.
        item = self._responses.get()
        if isinstance(item, BaseException):
            raise item
        return item

    def _write(self, frame: bytes) -> None:
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
        completed.wait()
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
    ) -> Any:
        with self._guard:
            if self._closed:
                raise TargetConnectionError(
                    "The worker session is no longer live; reconnect and validate."
                )
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
            raw = (json.dumps(payload, separators=(",", ":"), allow_nan=False) + "\n").encode(
                "utf-8"
            )
            operation_owner = current_operation()
            cancellation_callback: Callable[[], object] | None = None
            if operation_owner is not None and self._bootstrap_detached:
                cancellation_callback = self._terminate
                operation_owner.add_cancellation_callback(cancellation_callback)
            try:
                self._write(raw)
                reply = self._read()
            except BaseException as exc:
                if (
                    operation_owner is self._bootstrap_owner
                    and operation_owner is not None
                    and operation_owner.cancellation_requested.is_set()
                ):
                    self._finish_cancelled_bootstrap()
                    operation_owner.checkpoint()
                self._invalidate(f"Worker {operation} failed: {type(exc).__name__}: {exc}.")
            finally:
                if operation_owner is not None and cancellation_callback is not None:
                    operation_owner.remove_cancellation_callback(cancellation_callback)
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
            if reply.get("ok") is not False or set(reply) != {
                "version",
                "request_id",
                "ok",
                "error",
            }:
                self._invalidate("Worker returned an invalid error envelope.")
            error = reply.get("error")
            if not isinstance(error, dict):
                self._invalidate("Worker returned an invalid typed error envelope.")
            kind = error.get("kind")
            if kind not in {*_ERROR_TYPES, "provider_failure"}:
                self._invalidate("Worker returned an unallowlisted error kind.")
            message = error.get("message")
            if not isinstance(message, str) or not message:
                self._invalidate("Worker returned an invalid error message.")
            if kind == "target_connection_cleanup":
                if set(error) != {"kind", "message", "primary", "cleanup_diagnostics"}:
                    self._invalidate("Worker returned an invalid cleanup error envelope.")
                primary = error.get("primary")
                if (
                    not isinstance(primary, dict)
                    or set(primary) != {"type", "message"}
                    or not isinstance(primary.get("type"), str)
                    or not primary["type"]
                    or not isinstance(primary.get("message"), str)
                    or not primary["message"]
                ):
                    self._invalidate("Worker returned an invalid cleanup primary error.")
                try:
                    diagnostics = _cleanup_diagnostics_from_worker(error.get("cleanup_diagnostics"))
                except ValueError as exc:
                    self._invalidate(f"Worker returned malformed cleanup diagnostics: {exc}.")
                raise TargetConnectionCleanupError(
                    cast(str, primary["type"]),
                    cast(str, primary["message"]),
                    diagnostics,
                )
            if set(error) != {"kind", "message"}:
                self._invalidate("Worker returned an invalid typed error envelope.")
            if kind == "provider_failure":
                raise TargetConnectionError(f"Worker {operation} failed: {kind}: {message}")
            error_type = _ERROR_TYPES.get(cast(str, kind))
            if error_type is None:
                self._invalidate("Worker returned an unallowlisted error kind.")
            raise error_type(f"Worker {operation} failed: {message}")

    def close(self) -> dict[str, object]:
        if not hasattr(self, "_cleanup_diagnostics"):
            self._cleanup_diagnostics = []
        with self._guard:
            if getattr(self, "_bootstrap_owner", None) is not None and not getattr(
                self, "_bootstrap_detached", False
            ):
                # Before promotion this worker belongs to the bootstrap
                # operation.  Rollback close and cancellation must therefore
                # share one termination/marker decision regardless of which
                # arrives first.  ``RLock`` keeps the complete transaction
                # linearized without a wait loop or a second closer.
                evidence = self._finish_cancelled_bootstrap()
                if evidence is not None:
                    return evidence
            bootstrap_evidence = getattr(self, "_bootstrap_cleanup_evidence", None)
            if bootstrap_evidence is not None:
                # Bootstrap cancellation owns the sole termination and marker
                # decision.  A promotion rollback receives that same evidence
                # instead of issuing any second close attempt.
                return dict(bootstrap_evidence)
            if self._closed:
                if self._cleanup_confirmed:
                    self._remove_confirmed_marker("Worker process cleanup was confirmed.")
                    return {
                        "closed": True,
                        "graceful": False,
                        "diagnostic": "; ".join(self._cleanup_diagnostics) or None,
                    }
                raise TargetConnectionError(
                    "Worker cleanup could not be confirmed; recovery marker retained."
                )
            graceful_error: BaseException | None = None
            try:
                try:
                    self.call("close", {})
                except Exception as exc:
                    # Process termination is the ownership authority. Graceful-close
                    # failures are diagnostics when forced termination is proven.
                    graceful_error = exc
            finally:
                if not self._closed:
                    self._closed = True
                    self._cleanup_confirmed = self._terminate()
                    if self._cleanup_confirmed:
                        self._remove_confirmed_marker("Worker process cleanup was confirmed.")
            if self._cleanup_confirmed:
                diagnostic = "; ".join(
                    item
                    for item in (
                        (
                            f"graceful close failed: {type(graceful_error).__name__}: {graceful_error}"
                            if graceful_error is not None
                            else ""
                        ),
                        *self._cleanup_diagnostics,
                    )
                    if item
                )
                return {
                    "closed": True,
                    "graceful": graceful_error is None,
                    "diagnostic": diagnostic or None,
                }
            raise TargetConnectionError(
                "Worker cleanup could not be confirmed; recovery marker retained. "
                + "; ".join(self._cleanup_diagnostics)
            )


def _board_record(board: BoardConfig | None) -> dict[str, Any] | None:
    if board is None:
        return None
    record = asdict(board)
    record["source_path"] = str(board.source_path) if board.source_path is not None else None
    return record


class ProcessIsolatedDebugInterface(DebugInterface):
    """SWD proxy whose worker, not the MCP process, owns native providers."""

    def _open(
        self,
        operation: str,
        *,
        board: BoardConfig | None,
        unique_id: str | None,
        target: str | None,
        protocol: str | None = None,
        connect_mode: str | None = None,
        pack_path: Path | None = None,
        pack_sha256: str | None = None,
        pdsc_device: str | None = None,
        frequency_hz: int | None = None,
        worker_argv: Sequence[str] | None = None,
    ) -> TargetSessionHandle:
        worker = _WorkerClient(worker_argv=worker_argv)
        arguments = {
            "board": _board_record(board),
            "unique_id": unique_id,
            "target": target,
            "protocol": protocol,
            "connect_mode": connect_mode,
            "pack_path": str(pack_path) if pack_path is not None else None,
            "pack_sha256": pack_sha256,
            "pdsc_device": pdsc_device,
            "frequency_hz": frequency_hz,
        }
        try:
            raw_metadata = worker.call(operation, arguments)
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
                worker.close()
            except BaseException as cleanup:
                raise primary from cleanup
            raise

    def open(
        self,
        *,
        board: BoardConfig | None,
        unique_id: str | None,
        target: str | None,
        protocol: str | None = None,
        connect_mode: str | None = None,
        pack_path: Path | None = None,
        pack_sha256: str | None = None,
        pdsc_device: str | None = None,
        frequency_hz: int | None = None,
        worker_argv: Sequence[str] | None = None,
    ) -> TargetSessionHandle:
        return self._open(
            "open",
            board=board,
            unique_id=unique_id,
            target=target,
            protocol=protocol,
            connect_mode=connect_mode,
            pack_path=pack_path,
            pack_sha256=pack_sha256,
            pdsc_device=pdsc_device,
            frequency_hz=frequency_hz,
            worker_argv=worker_argv,
        )

    def connect_under_reset(
        self,
        *,
        board: BoardConfig | None,
        unique_id: str | None,
        target: str | None,
        pack_path: Path | None = None,
        pack_sha256: str | None = None,
        pdsc_device: str | None = None,
        worker_argv: Sequence[str] | None = None,
    ) -> TargetSessionHandle:
        return self._open(
            "connect_under_reset",
            board=board,
            unique_id=unique_id,
            target=target,
            pack_path=pack_path,
            pack_sha256=pack_sha256,
            pdsc_device=pdsc_device,
            worker_argv=worker_argv,
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
    ) -> Any:
        return self._worker(handle).call(operation, arguments or {})

    def close(self, handle: TargetSessionHandle) -> dict[str, object]:
        return self._worker(handle).close()

    def get_state(self, handle: TargetSessionHandle) -> str:
        return cast(str, self._call(handle, "get_state"))

    def read_memory(
        self,
        handle: TargetSessionHandle,
        address: int,
        width_bits: int,
    ) -> int:
        return cast(
            int,
            self._call(
                handle,
                "read_memory",
                {"address": address, "width_bits": width_bits},
            ),
        )

    def read_memory_block(
        self, handle: TargetSessionHandle, address: int, length: int
    ) -> list[int]:
        return cast(
            list[int],
            self._call(handle, "read_memory_block", {"address": address, "length": length}),
        )

    def physical_memory_regions(
        self, handle: TargetSessionHandle
    ) -> tuple[PhysicalMemoryRegion, ...]:
        regions = cast(
            tuple[PhysicalMemoryRegion, ...], self._call(handle, "physical_memory_regions")
        )
        metadata = handle.metadata
        if metadata is None or any(
            region.session_token != metadata.runtime_token for region in regions
        ):
            raise TargetConnectionError(
                "Worker physical-memory facts did not belong to the current target session."
            )
        return regions

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
    ) -> FlashVerification:
        result = cast(
            FlashVerification,
            self._call(
                handle,
                "flash",
                {"path": str(firmware), "halt_after_reset": halt_after_reset},
            ),
        )
        if handle.board is not None and handle.board.provider_id != "pyocd":
            metadata = handle.metadata
            identity = metadata.live_identity if metadata is not None else None
            if (
                metadata is None
                or not isinstance(identity, dict)
                or result.session_token != metadata.runtime_token
                or result.support_identity != identity.get("support_identity")
            ):
                raise TargetConnectionError(
                    "Provider flash evidence was not bound to the current live session and support identity."
                )
        if result.final_reset_postcondition.casefold() == "failed":
            raise FlashFinalResetFailed(result)
        if result.final_reset_postcondition.casefold() == "unknown":
            raise FlashFinalResetUncertain(
                result,
                TargetConnectionError(
                    f"{result.final_reset_error_type}: {result.final_reset_error_message}"
                ),
            )
        return result

    def recovery_capabilities(self, handle: TargetSessionHandle) -> tuple[RecoveryCapability, ...]:
        return cast(tuple[RecoveryCapability, ...], self._call(handle, "recovery_capabilities"))

    def recover(self, handle: TargetSessionHandle, mechanism: str) -> RecoveryResult:
        return cast(RecoveryResult, self._call(handle, "recover", {"mechanism": mechanism}))

    def set_breakpoint(self, handle: TargetSessionHandle, address: int) -> None:
        self._call(handle, "set_breakpoint", {"address": address})

    def remove_breakpoint(self, handle: TargetSessionHandle, address: int) -> None:
        self._call(handle, "remove_breakpoint", {"address": address})
