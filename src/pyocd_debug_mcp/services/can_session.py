# SPDX-License-Identifier: Apache-2.0
# Adapted from Firmware-CLI commit 0e8a45d for BYO-Firmware-MCP.
"""Bounded CAN service and one-process BYO attachment manager.

The lower worker owns every ``python-can`` object in a spawned process.  This
module deliberately contains no MCP imports: it implements immutable profile
policy, process-lifetime accounting, attachment ownership, and cleanup truth.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable, Coroutine
from dataclasses import asdict, dataclass, replace
from typing import Any, Generic, NoReturn, Protocol, TypeVar, cast

from pyocd_debug_mcp.adapters.can_interface import (
    CanBackendCapabilities,
    CanBusHandle,
    CanDirection,
    CanFrame,
)
from pyocd_debug_mcp.can_capabilities import (
    validate_can_capabilities,
    validate_can_capability_continuity,
)
from pyocd_debug_mcp.can_config import CanProcessLimits, LoadedCanFixtureConfig
from pyocd_debug_mcp.can_errors import (
    CanCleanupError,
    CanConnectionError,
    CanError,
    CanNotConfiguredError,
    CanOperationError,
    CanPolicyError,
    CanTimeoutError,
    CanWorkerError,
)
from pyocd_debug_mcp.can_startup import LoadedCanStartup
from pyocd_debug_mcp.can_secure_files import ensure_private_directory, write_immutable_private_file
from pyocd_debug_mcp.guardrails.can_gate import CanGate
from pyocd_debug_mcp.services.can_worker import (
    AsyncCanWorkerClient,
    CanWorkerClient,
    await_cleanup_uninterruptibly,
)

MAX_CLOSE_ATTEMPTS_PER_GENERATION = 3
_T = TypeVar("_T")


class AsyncCanWorker(Protocol):
    @property
    def cleanup_uncertain(self) -> bool: ...

    @property
    def last_escalation_stage(self) -> str: ...

    @property
    def last_cleanup_outcome(self) -> str: ...

    async def inspect(self, *, timeout_seconds: float) -> CanBackendCapabilities: ...

    async def open(self, *, timeout_seconds: float) -> CanBusHandle: ...

    async def send(
        self,
        frame: CanFrame,
        *,
        timeout_seconds: float,
        operation_timeout_seconds: float,
        not_after_monotonic: float | None = None,
    ) -> CanFrame: ...

    async def receive(
        self, *, timeout_seconds: float, operation_timeout_seconds: float
    ) -> CanFrame | None: ...

    async def close(self, *, timeout_seconds: float) -> None: ...

    async def cancel(self) -> None: ...


@dataclass(frozen=True, slots=True)
class CanBudgetSnapshot:
    budget_id: str
    can_tool_calls: int
    target_policy_refusals: int
    read_calls: int
    received_frames: int
    tx_attempts: int
    recent_tx_attempts: int
    capture_artifacts: int
    capture_bytes: int


class CanProcessBudget:
    """Non-resettable counters owned by one server process/profile snapshot."""

    def __init__(
        self,
        limits: CanProcessLimits | None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limits = limits
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._budget_id = secrets.token_hex(16)
        self._can_tool_calls = 0
        self._target_policy_refusals = 0
        self._read_calls = 0
        self._received_frames = 0
        self._tx_attempts = 0
        self._capture_artifacts = 0
        self._capture_bytes = 0
        self._tx_timestamps: deque[float] = deque()

    def _require_fixture(self) -> CanProcessLimits:
        if self._limits is None:
            raise CanNotConfiguredError()
        return self._limits

    def charge_tool_call(self) -> None:
        limits = self._require_fixture()
        with self._lock:
            if self._can_tool_calls >= limits.max_can_tool_calls:
                raise CanPolicyError(
                    "can/process-call-limit", "The process CAN tool-call limit is exhausted."
                )
            self._can_tool_calls += 1

    def charge_read(self) -> None:
        """Atomically charge the ordinary call and read counters."""

        limits = self._require_fixture()
        with self._lock:
            if self._can_tool_calls >= limits.max_can_tool_calls:
                raise CanPolicyError(
                    "can/process-call-limit", "The process CAN tool-call limit is exhausted."
                )
            if self._read_calls >= limits.max_read_calls:
                raise CanPolicyError(
                    "can/process-call-limit", "The process CAN read-call limit is exhausted."
                )
            self._can_tool_calls += 1
            self._read_calls += 1

    def charge_target_policy_refusal(self) -> None:
        """Charge one immutable-profile refusal without consuming an ordinary call."""

        limits = self._require_fixture()
        with self._lock:
            if self._target_policy_refusals >= limits.max_target_policy_refusals:
                raise CanPolicyError(
                    "can/process-call-limit",
                    "The process CAN target-policy refusal limit is exhausted.",
                )
            self._target_policy_refusals += 1

    def ensure_received_capacity(self) -> None:
        limits = self._require_fixture()
        with self._lock:
            if self._received_frames >= limits.max_received_frames:
                raise CanPolicyError(
                    "can/process-rx-limit", "The process CAN receive-frame limit is exhausted."
                )

    def charge_received_frame(self) -> None:
        limits = self._require_fixture()
        with self._lock:
            if self._received_frames >= limits.max_received_frames:
                raise CanPolicyError(
                    "can/process-rx-limit", "The process CAN receive-frame limit is exhausted."
                )
            self._received_frames += 1

    def charge_capture(self, size: int) -> None:
        limits = self._require_fixture()
        with self._lock:
            if (
                self._capture_artifacts >= limits.max_capture_artifacts
                or self._capture_bytes + size > limits.max_capture_bytes
            ):
                raise CanPolicyError(
                    "can/process-artifact-limit", "CAN capture retention budget is exhausted."
                )
            # Charge before publication. Failed writes cannot reset a process budget.
            self._capture_artifacts += 1
            self._capture_bytes += size

    def charge_tx(self, *, per_second: int, process_maximum: int) -> float:
        now = self._monotonic()
        with self._lock:
            while self._tx_timestamps and now - self._tx_timestamps[0] >= 1.0:
                self._tx_timestamps.popleft()
            if self._tx_attempts >= process_maximum:
                raise CanPolicyError(
                    "can/process-call-limit", "The process CAN transmit-attempt limit is exhausted."
                )
            if len(self._tx_timestamps) >= per_second:
                raise CanPolicyError(
                    "can/rate-limited", "The one-second CAN transmit rate limit is exhausted."
                )
            self._tx_attempts += 1
            self._tx_timestamps.append(now)
        return now

    def snapshot(self) -> CanBudgetSnapshot:
        now = self._monotonic()
        with self._lock:
            recent = sum(1 for stamp in self._tx_timestamps if now - stamp < 1.0)
            return CanBudgetSnapshot(
                budget_id=self._budget_id,
                can_tool_calls=self._can_tool_calls,
                target_policy_refusals=self._target_policy_refusals,
                read_calls=self._read_calls,
                received_frames=self._received_frames,
                tx_attempts=self._tx_attempts,
                recent_tx_attempts=recent,
                capture_artifacts=self._capture_artifacts,
                capture_bytes=self._capture_bytes,
            )


@dataclass(frozen=True, slots=True)
class CanCleanupState:
    resource_generation: int = 0
    attempt_count: int = 0
    first_error: str | None = None
    latest_error: str | None = None
    last_escalation_stage: str = "none"
    last_cleanup_outcome: str = "none"


@dataclass(frozen=True, slots=True)
class CanCaptureResult:
    frames: tuple[CanFrame, ...]
    observed_frames: int
    elapsed_seconds: float
    stop_reason: str
    truncated: bool
    artifact: dict[str, object] | None = None


@dataclass(slots=True)
class _SourceOutcome(Generic[_T]):
    """Loop-thread outcome independent of the caller-facing bridge future."""

    succeeded: bool = False
    value: _T | None = None
    error: BaseException | None = None


_ESCALATION_STAGES = frozenset(
    {
        "none",
        "unreported",
        "graceful-shutdown",
        "cancel-requested",
        "cleanup-acknowledged",
        "cleanup-failed",
        "cleanup-unknown",
        "invalid-cleanup-ack",
        "dead-without-cleanup-ack",
        "request-retirement-timeout",
        "startup-pending",
        "term",
        "term-after-cleanup",
        "term-after-cleanup-failure",
        "term-cleanup-acknowledged",
        "term-cleanup-failed",
        "term-cleanup-unknown",
        "term-after-cleanup-cleanup-acknowledged",
        "term-after-cleanup-cleanup-failed",
        "term-after-cleanup-cleanup-unknown",
        "term-after-cleanup-failure-cleanup-acknowledged",
        "term-after-cleanup-failure-cleanup-failed",
        "term-after-cleanup-failure-cleanup-unknown",
        "kill",
        "kill-after-cleanup",
        "kill-after-cleanup-failure",
        "kill-cleanup-acknowledged",
        "kill-cleanup-failed",
        "kill-cleanup-unknown",
        "kill-timeout",
    }
)
_CLEANUP_OUTCOMES = frozenset({"none", "unreported", "not-running", "closed", "failed", "unknown"})


def can_frame_record(frame: CanFrame, *, include_payload: bool) -> dict[str, object]:
    record: dict[str, object] = {
        "arbitration_id": frame.arbitration_id,
        "arbitration_id_text": frame.identifier.display(),
        "extended": frame.extended,
        "dlc": frame.dlc,
        "timestamp": frame.timestamp,
        "direction": frame.direction.value,
        "channel": frame.channel,
        "is_error": frame.is_error,
        "is_remote": frame.is_remote,
        "is_fd": frame.is_fd,
    }
    if include_payload:
        record["data_hex"] = frame.data.hex()
        record["payload_sha256"] = hashlib.sha256(frame.data).hexdigest()
    return record


class CanSessionService:
    """One serialized, isolated CAN worker with process-lifetime quotas."""

    def __init__(
        self,
        loaded: LoadedCanFixtureConfig,
        *,
        budget: CanProcessBudget,
        worker_factory: Callable[[], AsyncCanWorker],
        default_read_seconds: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.loaded = loaded
        self.gate = CanGate(loaded.config)
        self.budget = budget
        self._worker_factory = worker_factory
        self._default_read_seconds = default_read_seconds
        self._monotonic = monotonic
        self._operation_lock = asyncio.Lock()
        self._worker: AsyncCanWorker | None = None
        self._opening_worker: AsyncCanWorker | None = None
        self._handle: CanBusHandle | None = None
        self._capabilities: CanBackendCapabilities | None = None
        self._cleanup = CanCleanupState()

    @property
    def connected(self) -> bool:
        return self._worker is not None and self._handle is not None

    @property
    def cleanup_needed(self) -> bool:
        return self._worker is not None or self._opening_worker is not None

    @property
    def cleanup_uncertain(self) -> bool:
        return self.cleanup_needed and self._cleanup.latest_error is not None

    @property
    def cleanup_retry_exhausted(self) -> bool:
        return self._cleanup.attempt_count >= MAX_CLOSE_ATTEMPTS_PER_GENERATION

    @property
    def cleanup_state(self) -> CanCleanupState:
        return self._cleanup

    @property
    def capabilities(self) -> CanBackendCapabilities | None:
        return self._capabilities

    def _start_generation(self) -> None:
        self._cleanup = CanCleanupState(resource_generation=self._cleanup.resource_generation + 1)

    @staticmethod
    def _worker_label(worker: AsyncCanWorker, name: str, allowed: frozenset[str]) -> str:
        value = getattr(worker, name, "unreported")
        return value if isinstance(value, str) and value in allowed else "unreported"

    def _record_worker_cleanup(self, worker: AsyncCanWorker) -> None:
        self._cleanup = CanCleanupState(
            resource_generation=self._cleanup.resource_generation,
            attempt_count=self._cleanup.attempt_count,
            first_error=self._cleanup.first_error,
            latest_error=self._cleanup.latest_error,
            last_escalation_stage=self._worker_label(
                worker, "last_escalation_stage", _ESCALATION_STAGES
            ),
            last_cleanup_outcome=self._worker_label(
                worker, "last_cleanup_outcome", _CLEANUP_OUTCOMES
            ),
        )

    def _begin_cleanup(self) -> None:
        if self.cleanup_retry_exhausted:
            raise CanCleanupError("CAN cleanup retry ceiling is exhausted.")
        self._cleanup = CanCleanupState(
            resource_generation=self._cleanup.resource_generation,
            attempt_count=self._cleanup.attempt_count + 1,
            first_error=self._cleanup.first_error,
            latest_error=self._cleanup.latest_error,
            last_escalation_stage=self._cleanup.last_escalation_stage,
            last_cleanup_outcome=self._cleanup.last_cleanup_outcome,
        )

    def _record_cleanup_error(self) -> None:
        self._cleanup = CanCleanupState(
            resource_generation=self._cleanup.resource_generation,
            attempt_count=self._cleanup.attempt_count,
            first_error=self._cleanup.first_error or "can/cleanup-failed",
            latest_error="can/cleanup-failed",
            last_escalation_stage=self._cleanup.last_escalation_stage,
            last_cleanup_outcome=self._cleanup.last_cleanup_outcome,
        )

    def _retire(self, worker: AsyncCanWorker) -> bool:
        if bool(getattr(worker, "cleanup_uncertain", False)):
            self._record_cleanup_error()
            return False
        if self._worker is worker:
            self._worker = None
            self._handle = None
            self._capabilities = None
        if self._opening_worker is worker:
            self._opening_worker = None
        return True

    async def _cancel(self, worker: AsyncCanWorker) -> None:
        self._begin_cleanup()
        try:
            _, interrupted = await await_cleanup_uninterruptibly(worker.cancel())
        except BaseException:
            self._record_worker_cleanup(worker)
            self._record_cleanup_error()
            raise
        self._record_worker_cleanup(worker)
        if not self._retire(worker):
            raise CanCleanupError("CAN cancellation cleanup is uncertain.")
        if interrupted:
            raise asyncio.CancelledError

    def _reconcile_terminal_error(self, worker: AsyncCanWorker) -> None:
        self._record_worker_cleanup(worker)
        proven = self._cleanup.last_cleanup_outcome in {"closed", "not-running"} and not bool(
            getattr(worker, "cleanup_uncertain", False)
        )
        if proven:
            self._retire(worker)
        else:
            self._record_cleanup_error()

    async def connect(self, *, board_id: object, probe_uid: object) -> CanBackendCapabilities:
        try:
            self.gate.require_board(board_id)
            self.gate.require_probe(probe_uid)
        except CanPolicyError:
            self.budget.charge_target_policy_refusal()
            raise
        self.budget.charge_tool_call()
        async with self._operation_lock:
            if self.cleanup_uncertain:
                raise CanCleanupError("Prior CAN cleanup is uncertain; reopen is forbidden.")
            if self.connected:
                assert self._capabilities is not None
                return self._capabilities
            worker = self._worker_factory()
            self._start_generation()
            self._opening_worker = worker
            try:
                capabilities = await worker.inspect(timeout_seconds=3.0)
                validate_can_capabilities(self.loaded, capabilities)
                handle = await worker.open(timeout_seconds=3.0)
            except BaseException as primary:
                try:
                    await self._cancel(worker)
                except asyncio.CancelledError:
                    raise
                except BaseException as cleanup:
                    self._worker = worker
                    self._opening_worker = None
                    self._record_cleanup_error()
                    raise CanCleanupError(
                        "CAN opening failed and cleanup is uncertain."
                    ) from cleanup
                raise primary
            self._opening_worker = None
            if handle.profile_sha256 != self.loaded.canonical_sha256:
                self._worker = worker
                self._handle = handle
                self._capabilities = capabilities
                try:
                    await self.disconnect()
                except BaseException as cleanup:
                    raise CanCleanupError("CAN profile mismatch cleanup is uncertain.") from cleanup
                raise CanConnectionError("CAN worker opened a different immutable profile.")
            self._worker = worker
            self._handle = handle
            self._capabilities = capabilities
            return capabilities

    async def disconnect(self) -> None:
        worker = self._worker or self._opening_worker
        if worker is None:
            return
        if self._operation_lock.locked():
            try:
                await self._cancel(worker)
            except BaseException as exc:
                self._record_cleanup_error()
                raise CanCleanupError("CAN cleanup is uncertain.") from exc
            return
        async with self._operation_lock:
            worker = self._worker or self._opening_worker
            if worker is None:
                return
            self._begin_cleanup()
            try:
                _, interrupted = await await_cleanup_uninterruptibly(
                    worker.close(timeout_seconds=1.0)
                )
            except BaseException as exc:
                self._record_worker_cleanup(worker)
                self._record_cleanup_error()
                raise CanCleanupError("CAN cleanup is uncertain.") from exc
            self._record_worker_cleanup(worker)
            if not self._retire(worker):
                raise CanCleanupError("CAN cleanup is uncertain.")
            if interrupted:
                raise asyncio.CancelledError

    def _require_connected(self) -> AsyncCanWorker:
        if self.cleanup_uncertain:
            raise CanCleanupError("Prior CAN cleanup is uncertain; operation is forbidden.")
        if self._worker is None or self._handle is None:
            raise CanPolicyError("can/not-connected", "CAN is not connected.")
        return self._worker

    async def send(
        self,
        arbitration_id: object,
        data_hex: object,
        *,
        extended: object,
        timeout_seconds: object,
    ) -> CanFrame:
        try:
            validated = self.gate.validate_transmit_request(
                arbitration_id, extended, data_hex, timeout_seconds=timeout_seconds
            )
        except CanPolicyError as exc:
            if exc.code in {"can/tx-disabled", "can/id-forbidden"}:
                self.budget.charge_target_policy_refusal()
            raise
        self.budget.charge_tool_call()
        async with self._operation_lock:
            worker = self._require_connected()
            try:
                current = await worker.inspect(timeout_seconds=validated.timeout_seconds)
                validate_can_capabilities(self.loaded, current)
                if self._capabilities is not None:
                    validate_can_capability_continuity(self._capabilities, current)
                self._capabilities = current
                submitted_at = self.budget.charge_tx(
                    per_second=self.loaded.config.transmit.max_frames_per_second,
                    process_maximum=self.loaded.config.transmit.max_tx_attempts_per_process,
                )
                frame = CanFrame(
                    validated.identifier.arbitration_id,
                    validated.identifier.extended,
                    validated.data,
                    direction=CanDirection.TX,
                )
                receipt = await worker.send(
                    frame,
                    timeout_seconds=validated.timeout_seconds,
                    operation_timeout_seconds=validated.timeout_seconds + 0.5,
                    not_after_monotonic=submitted_at + validated.timeout_seconds,
                )
                if (
                    receipt.is_error
                    or receipt.is_remote
                    or receipt.is_fd
                    or receipt.direction is not CanDirection.TX
                    or receipt.identifier != frame.identifier
                    or receipt.data != frame.data
                ):
                    try:
                        await self._cancel(worker)
                    except asyncio.CancelledError:
                        raise
                    except BaseException as cleanup:
                        raise CanCleanupError(
                            "Invalid CAN send receipt cleanup is uncertain."
                        ) from cleanup
                    raise CanWorkerError("CAN worker returned an invalid send receipt")
                return receipt
            except (CanWorkerError, CanTimeoutError):
                self._reconcile_terminal_error(worker)
                raise
            except asyncio.CancelledError:
                await self._cancel(worker)
                raise

    async def read(
        self,
        *,
        read_seconds: object,
        max_frames: object,
    ) -> CanCaptureResult:
        validated = self.gate.validate_read_request(
            read_seconds=(self._default_read_seconds if read_seconds is None else read_seconds),
            max_frames=max_frames,
        )
        self.budget.charge_read()
        async with self._operation_lock:
            worker = self._require_connected()
            started = self._monotonic()
            deadline = started + validated.read_seconds
            allowed = frozenset(self.loaded.config.receive.exact_ids)
            frames: list[CanFrame] = []
            observed = 0
            stop_reason = "deadline"
            truncated = False
            while len(frames) < validated.max_frames:
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    break
                try:
                    self.budget.ensure_received_capacity()
                    frame = await worker.receive(
                        timeout_seconds=remaining,
                        operation_timeout_seconds=remaining + 0.5,
                    )
                except CanPolicyError as exc:
                    if exc.code != "can/process-rx-limit":
                        raise
                    stop_reason = exc.code
                    truncated = True
                    break
                except (CanWorkerError, CanTimeoutError):
                    self._reconcile_terminal_error(worker)
                    raise
                except asyncio.CancelledError:
                    await self._cancel(worker)
                    raise
                completed = self._monotonic()
                if frame is not None:
                    self.budget.charge_received_frame()
                    observed += 1
                if completed >= deadline or frame is None:
                    break
                # The v1 tool surface is deliberately Classical CAN data-only.
                # Backend filters may still surface RTR, error, or FD traffic
                # with an admitted identifier, so reject those frame classes at
                # the service boundary rather than serializing forbidden flags.
                if (
                    not frame.is_error
                    and not frame.is_remote
                    and not frame.is_fd
                    and frame.identifier in allowed
                ):
                    frames.append(frame)
            if len(frames) >= validated.max_frames:
                stop_reason = "frame_cap"
                truncated = True
            return CanCaptureResult(
                frames=tuple(frames),
                observed_frames=observed,
                elapsed_seconds=max(0.0, self._monotonic() - started),
                stop_reason=stop_reason,
                truncated=truncated,
            )


class CanSessionManager:
    """Thread-safe owner/state bridge around the async CAN service."""

    def __init__(
        self,
        startup: LoadedCanStartup,
        *,
        worker_factory: Callable[[], AsyncCanWorker] | None = None,
    ) -> None:
        self.startup = startup
        fixture = startup.fixture
        self._capture_directory = (
            fixture.snapshot_path.parent.parent / "can-captures"
            if fixture is not None and fixture.snapshot_path is not None
            else None
        )
        self.budget = CanProcessBudget(fixture.config.process_limits if fixture else None)
        self._worker_factory = worker_factory
        self._service = (
            CanSessionService(
                fixture,
                budget=self.budget,
                default_read_seconds=startup.runtime.effective_read_seconds,
                worker_factory=worker_factory
                or (
                    lambda: AsyncCanWorkerClient(
                        CanWorkerClient(cast(LoadedCanFixtureConfig, fixture))
                    )
                ),
            )
            if fixture is not None
            else None
        )
        self._state_lock = threading.RLock()
        self._owner_board_id: str | None = None
        self._phase = "closed"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._loop_ready = threading.Event()
        self._loop_start_error: BaseException | None = None
        self._shutdown_started = False
        self._stopped = False
        self._shutdown_lock = threading.RLock()
        self._shutdown_clean: bool | None = None
        self._active_submissions: dict[
            object,
            tuple[concurrent.futures.Future[Any], threading.Event, threading.Event],
        ] = {}

    @property
    def configured(self) -> bool:
        return self.startup.fixture is not None

    @property
    def owner_board_id(self) -> str | None:
        with self._state_lock:
            return self._owner_board_id

    @property
    def phase(self) -> str:
        with self._state_lock:
            return self._phase

    def _set_state(self, owner: str | None, phase: str) -> None:
        with self._state_lock:
            self._owner_board_id = owner
            self._phase = phase

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._state_lock:
            if self._stopped:
                raise RuntimeError("CAN manager is shut down")
            if self._loop is not None:
                return self._loop
            # ``_loop_thread`` is the start-in-progress sentinel. Without this
            # guard, another caller can enter before the child publishes
            # ``_loop`` and create a second event-loop thread.
            if self._loop_thread is None:
                self._loop_ready.clear()
                self._loop_start_error = None

                def run_loop() -> None:
                    try:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        with self._state_lock:
                            self._loop = loop
                            should_run = not self._stopped
                        self._loop_ready.set()
                        if should_run:
                            loop.run_forever()
                        loop.run_until_complete(loop.shutdown_asyncgens())
                        loop.run_until_complete(loop.shutdown_default_executor())
                        loop.close()
                    except BaseException as exc:
                        with self._state_lock:
                            self._loop_start_error = exc
                        self._loop_ready.set()

                thread = threading.Thread(target=run_loop, name="byo-can-loop", daemon=True)
                self._loop_thread = thread
                try:
                    thread.start()
                except BaseException:
                    self._loop_thread = None
                    raise
        self._loop_ready.wait()
        with self._state_lock:
            if self._stopped:
                raise RuntimeError("CAN manager is shut down")
            if self._loop is None:
                raise RuntimeError("CAN event loop failed to start") from self._loop_start_error
            return self._loop

    def _schedule(
        self,
        factory: Callable[[], Coroutine[Any, Any, _T]],
        *,
        allow_shutdown: bool = False,
    ) -> tuple[concurrent.futures.Future[_T], threading.Event, threading.Event, _SourceOutcome[_T]]:
        if threading.current_thread() is self._loop_thread:
            raise RuntimeError("CAN manager cannot submit from its own loop thread")
        loop = self._ensure_loop()
        source_started = threading.Event()
        completed = threading.Event()
        suppress_after_caller_cancel = threading.Event()
        source_outcome = _SourceOutcome[_T]()
        submission_token = object()

        async def runner() -> _T:
            # Admission does not return until this source coroutine has entered
            # its try/finally. Consequently cancellation can never destroy an
            # unstarted coroutine without setting ``completed``.
            source_started.set()
            try:
                result = await factory()
            except BaseException as exc:
                source_outcome.error = exc
                if suppress_after_caller_cancel.is_set():
                    # The caller-facing task already committed to cancellation.
                    # Service/manager reconciliation above retained every cleanup
                    # fact; consuming the terminal exception here prevents an
                    # orphaned event-loop Task warning from leaking diagnostics.
                    return cast(_T, None)
                raise
            else:
                source_outcome.succeeded = True
                source_outcome.value = result
                return result
            finally:
                completed.set()
                with self._state_lock:
                    self._active_submissions.pop(submission_token, None)

        # Admission and enqueueing are atomic with shutdown's transition to
        # ``_shutdown_started``. Construct the coroutine only after admission,
        # and close it explicitly if the loop rejects scheduling.
        with self._state_lock:
            if self._shutdown_started and not allow_shutdown:
                raise RuntimeError("CAN manager is shutting down")
            coroutine = runner()
            try:
                future = asyncio.run_coroutine_threadsafe(coroutine, loop)
            except BaseException:
                coroutine.close()
                raise
            self._active_submissions[submission_token] = (future, source_started, completed)
        # Never wait while holding ``_state_lock``: an older loop operation may
        # need that lock before this newly queued runner can enter and publish
        # ``source_started``. Shutdown sees the event in the active tuple and
        # likewise waits for entry before cancellation, preserving the
        # no-unentered-coroutine cancellation invariant.
        source_started.wait()
        return future, completed, suppress_after_caller_cancel, source_outcome

    @staticmethod
    def _source_result(outcome: _SourceOutcome[_T]) -> _T:
        if outcome.succeeded:
            return cast(_T, outcome.value)
        if outcome.error is None:
            raise RuntimeError("CAN loop operation completed without a recorded outcome")
        raise outcome.error

    async def _submit(self, factory: Callable[[], Coroutine[Any, Any, _T]]) -> _T:
        future, completed, suppress_after_caller_cancel, source_outcome = self._schedule(factory)
        try:
            return await asyncio.shield(asyncio.wrap_future(future))
        except asyncio.CancelledError:
            # A loop-thread result can be committed while its delivery to this
            # caller event loop is pending. Completion wins that race: reporting
            # cancellation would otherwise leave an unreconciled open resource.
            if completed.is_set():
                return self._source_result(source_outcome)
            suppress_after_caller_cancel.set()
            cancellation_requested = future.cancel()
            await await_cleanup_uninterruptibly(asyncio.to_thread(completed.wait))
            if source_outcome.succeeded:
                return cast(_T, source_outcome.value)
            if not cancellation_requested:
                return self._source_result(source_outcome)
            raise

    def _submit_sync(
        self,
        factory: Callable[[], Coroutine[Any, Any, _T]],
        *,
        allow_shutdown: bool = False,
    ) -> _T:
        if threading.current_thread() is self._loop_thread:
            raise RuntimeError("CAN manager cannot synchronously join its own loop thread")
        future, completed, _suppress_after_caller_cancel, _source_outcome = self._schedule(
            factory, allow_shutdown=allow_shutdown
        )
        try:
            return future.result()
        finally:
            completed.wait()

    def _require_service(self) -> CanSessionService:
        if self._service is None:
            raise CanNotConfiguredError()
        return self._service

    def require_profile_board(self, board_id: str) -> None:
        """Validate a status/connect board against the immutable fixture."""

        try:
            self._require_service().gate.require_board(board_id)
        except CanPolicyError:
            self.budget.charge_target_policy_refusal()
            raise

    def _raise_owner_board_mismatch(self, message: str) -> NoReturn:
        """Charge and report a request targeting a different live owner."""

        self.budget.charge_target_policy_refusal()
        raise CanPolicyError("can/board-mismatch", message)

    def _require_owner(self, board_id: str) -> CanSessionService:
        service = self._require_service()
        with self._state_lock:
            owner = self._owner_board_id
            if owner is None:
                raise CanPolicyError("can/not-connected", "CAN is not connected.")
            owner_mismatch = owner != board_id
            if not owner_mismatch:
                if self._phase in {"closing", "uncertain"}:
                    raise CanCleanupError("CAN cleanup is uncertain; only disconnect is allowed.")
                if self._phase != "open":
                    raise CanPolicyError("can/not-connected", "CAN is not connected.")
        if owner_mismatch:
            self._raise_owner_board_mismatch(
                f"CAN is attached to board {owner!r}, not {board_id!r}."
            )
        return service

    def require_open_owner(self, board_id: str) -> None:
        """Fail before plan consumption unless this board owns an open CAN session."""

        self._require_owner(board_id)

    async def connect(self, board_id: str, probe_uid: str | None) -> CanBackendCapabilities:
        async def operation() -> CanBackendCapabilities:
            service = self._require_service()
            with self._state_lock:
                owner = self._owner_board_id
                owner_mismatch = owner is not None and owner != board_id
                if not owner_mismatch:
                    if self._phase == "opening":
                        raise CanPolicyError(
                            "can/not-connected", "CAN connection is already opening."
                        )
                    if self._phase in {"closing", "uncertain"}:
                        raise CanCleanupError("CAN cleanup is incomplete; reconnect is forbidden.")
                    self._owner_board_id = board_id
                    self._phase = "opening" if not service.connected else "open"
            if owner_mismatch:
                assert owner is not None
                self._raise_owner_board_mismatch(f"CAN is already attached to board {owner!r}.")
            try:
                capabilities = await service.connect(board_id=board_id, probe_uid=probe_uid)
            except BaseException:
                # Admission refusal can occur before any I/O. Preserve an
                # already-open generation unless cleanup itself is uncertain.
                self._reconcile_operation_end(board_id, service)
                raise
            self._set_state(board_id, "open")
            return capabilities

        return await self._submit(operation)

    async def send(
        self,
        board_id: str,
        arbitration_id: object,
        data_hex: object,
        *,
        extended: object,
        timeout_seconds: object,
    ) -> CanFrame:
        async def operation() -> CanFrame:
            service = self._require_owner(board_id)
            try:
                return await service.send(
                    arbitration_id,
                    data_hex,
                    extended=extended,
                    timeout_seconds=timeout_seconds,
                )
            except BaseException:
                self._reconcile_operation_end(board_id, service)
                raise

        return await self._submit(operation)

    def _retain_capture(self, board_id: str, result: CanCaptureResult) -> CanCaptureResult:
        fixture = self.startup.fixture
        directory = self._capture_directory
        if directory is None or fixture is None:
            return result
        frame_records = [can_frame_record(frame, include_payload=True) for frame in result.frames]
        if fixture.config.evidence.artifact_payloads == "hash_only":
            for record in frame_records:
                record.pop("data_hex", None)
        document = {
            "schema_version": 1,
            "board_id": board_id,
            "profile_sha256": fixture.canonical_sha256,
            "runtime_sha256": self.startup.runtime.canonical_sha256,
            "backend": fixture.config.adapter.kind,
            "software_simulation": not fixture.config.adapter.physical,
            "observed_frames": result.observed_frames,
            "stop_reason": result.stop_reason,
            "truncated": result.truncated,
            "frames": frame_records,
        }
        data = (canonical_can_json(document) + "\n").encode("utf-8")
        self.budget.charge_capture(len(data))
        digest = hashlib.sha256(data).hexdigest()
        name = f"capture-{secrets.token_hex(16)}.json"
        try:
            ensure_private_directory(directory, parents=True)
            write_immutable_private_file(directory / name, data, maximum_bytes=len(data))
        except OSError as exc:
            raise CanOperationError("Unable to retain CAN capture artifact") from exc
        return replace(
            result,
            artifact={
                "name": name,
                "sha256": digest,
                "bytes": len(data),
                "format": "json",
                "payloads": fixture.config.evidence.artifact_payloads,
            },
        )

    async def read(
        self,
        board_id: str,
        *,
        read_seconds: object,
        max_frames: object,
    ) -> CanCaptureResult:
        async def operation() -> CanCaptureResult:
            service = self._require_owner(board_id)
            try:
                result = await service.read(read_seconds=read_seconds, max_frames=max_frames)
                retained, interrupted = await await_cleanup_uninterruptibly(
                    asyncio.to_thread(self._retain_capture, board_id, result)
                )
                if interrupted:
                    raise asyncio.CancelledError
                return retained
            except BaseException:
                self._reconcile_operation_end(board_id, service)
                raise

        return await self._submit(operation)

    def _reconcile_operation_end(self, board_id: str, service: CanSessionService) -> None:
        if service.cleanup_needed:
            self._set_state(board_id, "uncertain" if service.cleanup_uncertain else "open")
        else:
            self._set_state(None, "closed")

    async def disconnect(self, board_id: str) -> None:
        if not self.configured:
            return

        async def operation() -> None:
            service = self._require_service()
            with self._state_lock:
                owner = self._owner_board_id
                if owner is None:
                    return
                owner_mismatch = owner != board_id
                if not owner_mismatch:
                    self._phase = "closing"
            if owner_mismatch:
                self._raise_owner_board_mismatch(
                    f"CAN is attached to board {owner!r}, not {board_id!r}."
                )
            try:
                await service.disconnect()
            except BaseException:
                self._reconcile_operation_end(board_id, service)
                raise
            self._set_state(None, "closed")

        await self._submit(operation)

    def disconnect_sync(self, board_id: str, *, _allow_shutdown: bool = False) -> None:
        if not self.configured:
            return

        async def operation() -> None:
            service = self._require_service()
            with self._state_lock:
                owner = self._owner_board_id
                if owner is None:
                    return
                owner_mismatch = owner != board_id
                if not owner_mismatch:
                    self._phase = "closing"
            if owner_mismatch:
                self._raise_owner_board_mismatch(
                    f"CAN is attached to board {owner!r}, not {board_id!r}."
                )
            try:
                await service.disconnect()
            except BaseException:
                self._reconcile_operation_end(board_id, service)
                raise
            self._set_state(None, "closed")

        self._submit_sync(operation, allow_shutdown=_allow_shutdown)

    def disconnect_owner_sync(self) -> bool:
        owner = self.owner_board_id
        if owner is None:
            return True
        service = self._require_service()
        while not service.cleanup_retry_exhausted:
            try:
                self.disconnect_sync(owner, _allow_shutdown=True)
                return True
            except CanError:
                continue
        return False

    def shutdown_sync(self) -> bool:
        # Framework and finalizer paths may both request shutdown. Serialize
        # them and preserve the first terminal cleanup result.
        if threading.current_thread() is self._loop_thread:
            raise RuntimeError("CAN manager cannot shut down from its own loop thread")
        with self._shutdown_lock:
            with self._state_lock:
                if self._stopped:
                    return self._shutdown_clean is not False
                self._shutdown_started = True
                active = tuple(self._active_submissions.values())

            # Retire every already-admitted cross-thread operation before the
            # terminal owner cleanup and loop stop. The runner's completion
            # event tracks the source coroutine, not the caller-facing future,
            # so cancellation races cannot make this wait return prematurely.
            for _future, source_started, _completed in active:
                source_started.wait()
            for future, _source_started, _completed in active:
                future.cancel()
            for _future, _source_started, completed in active:
                completed.wait()

            clean = self.disconnect_owner_sync()
            with self._state_lock:
                loop = self._loop
                thread = self._loop_thread
                self._stopped = True
                self._shutdown_clean = clean
            if loop is not None and not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(loop.stop)
                except RuntimeError:
                    # A failed loop has already reached a terminal state; join
                    # below still proves that no manager loop thread survives.
                    pass
            if thread is not None and thread is not threading.current_thread():
                thread.join()
            with self._state_lock:
                self._loop = None
                self._loop_thread = None
            return clean

    def status(self, board_id: str) -> dict[str, object]:
        fixture = self.startup.fixture
        service = self._service
        with self._state_lock:
            owner = self._owner_board_id
            phase = self._phase
        capabilities = service.capabilities if service is not None else None
        cleanup = service.cleanup_state if service is not None else CanCleanupState()
        runtime = self.startup.runtime
        budget = self.budget.snapshot()
        config = fixture.config if fixture is not None else None
        budget_limits = (
            {
                "max_can_tool_calls": config.process_limits.max_can_tool_calls,
                "max_target_policy_refusals": (config.process_limits.max_target_policy_refusals),
                "max_read_calls": config.process_limits.max_read_calls,
                "max_received_frames": config.process_limits.max_received_frames,
                "max_capture_artifacts": config.process_limits.max_capture_artifacts,
                "max_capture_bytes": config.process_limits.max_capture_bytes,
                "max_tx_attempts_per_process": config.transmit.max_tx_attempts_per_process,
                "max_frames_per_second": config.transmit.max_frames_per_second,
            }
            if config is not None
            else None
        )
        budget_remaining = (
            {
                "can_tool_calls": max(
                    0, config.process_limits.max_can_tool_calls - budget.can_tool_calls
                ),
                "target_policy_refusals": max(
                    0,
                    config.process_limits.max_target_policy_refusals
                    - budget.target_policy_refusals,
                ),
                "capture_artifacts": max(
                    0, config.process_limits.max_capture_artifacts - budget.capture_artifacts
                ),
                "capture_bytes": max(
                    0, config.process_limits.max_capture_bytes - budget.capture_bytes
                ),
                "read_calls": max(0, config.process_limits.max_read_calls - budget.read_calls),
                "received_frames": max(
                    0, config.process_limits.max_received_frames - budget.received_frames
                ),
                "tx_attempts": max(
                    0, config.transmit.max_tx_attempts_per_process - budget.tx_attempts
                ),
                "tx_attempts_in_current_window": max(
                    0, config.transmit.max_frames_per_second - budget.recent_tx_attempts
                ),
            }
            if config is not None
            else None
        )
        return {
            "configured": fixture is not None,
            "mode": runtime.mode.value,
            "phase": phase,
            "owner_board_id": owner,
            "profile_id": fixture.config.profile_id if fixture is not None else None,
            "source_sha256": runtime.source_sha256,
            "profile_sha256": runtime.profile_sha256,
            "runtime_sha256": runtime.canonical_sha256,
            "effective_read_seconds": runtime.effective_read_seconds,
            "profile": fixture.summary.as_dict() if fixture is not None else None,
            "capabilities": (
                {
                    **asdict(capabilities),
                    "bounded_open": capabilities.bounded_open.value,
                    "bounded_send": capabilities.bounded_send.value,
                    "bounded_receive": capabilities.bounded_receive.value,
                    "bounded_close": capabilities.bounded_close.value,
                    "pending_tx_quiescence": capabilities.pending_tx_quiescence.value,
                    "device_identity": list(capabilities.device_identity),
                }
                if capabilities is not None
                else None
            ),
            "cleanup": asdict(cleanup),
            "budget": asdict(budget),
            "budget_limits": budget_limits,
            "budget_remaining": budget_remaining,
        }


def canonical_can_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


__all__ = [
    "CanBudgetSnapshot",
    "CanCaptureResult",
    "CanCleanupState",
    "CanProcessBudget",
    "CanSessionManager",
    "CanSessionService",
    "canonical_can_json",
    "can_frame_record",
]
