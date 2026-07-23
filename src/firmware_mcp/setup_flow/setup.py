"""Run-scoped, resumable board Setup orchestration."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Literal

from firmware_mcp.firmstore.reports import ReportPaths, ReportWriter
from firmware_mcp.kernel.operations import OperationCancelledError
from firmware_mcp.setup_flow.preflight import (
    FriendlyChoice,
    PreflightDecision,
    PreflightEngine,
    PreflightInventory,
    PreflightSelections,
    SetupUserInput,
)


TERMINAL_SETUP_STATUSES = (
    "setup_completed",
    "setup_needs_user_input",
    "setup_research_required",
    "setup_blocked",
    "setup_unresolved",
    "setup_connection_failed",
    "setup_validation_failed",
)
SetupTerminalStatus = Literal[
    "setup_completed",
    "setup_needs_user_input",
    "setup_research_required",
    "setup_blocked",
    "setup_unresolved",
    "setup_connection_failed",
    "setup_validation_failed",
]
SetupMode = Literal["setup", "repair"]


class SetupWorkflowError(RuntimeError):
    """The setup workflow cannot continue with the current setup run."""


class SetupPhase(str, Enum):
    INPUT = "input"
    PREFLIGHT = "preflight"
    SELECTION = "selection"
    TARGET_RESOLUTION = "target_resolution"
    TARGET_SUPPORT = "target_support"
    CONNECTION = "connection"
    VALIDATION = "validation"
    COMMIT = "commit"


PHASE_ORDER = tuple(SetupPhase)


class PhaseState(str, Enum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PhaseRecord:
    phase: SetupPhase
    state: PhaseState
    code: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SetupPhaseOutcome:
    """Result contract implemented by target and validation tasks."""

    verified: bool
    code: str
    details: dict[str, Any] = field(default_factory=dict)
    terminal_status: SetupTerminalStatus | None = None
    agent_prompt: str = ""
    choices: tuple[FriendlyChoice, ...] = ()

    @classmethod
    def success(cls, code: str, **details: Any) -> SetupPhaseOutcome:
        return cls(True, code, details)

    @classmethod
    def stop(
        cls,
        status: SetupTerminalStatus,
        code: str,
        agent_prompt: str,
        *,
        choices: tuple[FriendlyChoice, ...] = (),
        details: Mapping[str, Any] | None = None,
    ) -> SetupPhaseOutcome:
        return cls(
            False,
            code,
            dict(details or {}),
            status,
            _plain_prompt(agent_prompt),
            choices,
        )


@dataclass(frozen=True, slots=True)
class SetupPhaseContext:
    continuation_id: str
    attempt_id: str
    mode: SetupMode
    user_input: SetupUserInput
    preflight: PreflightDecision
    phase_records: Mapping[SetupPhase, PhaseRecord]


SetupPhaseHandler = Callable[[SetupPhaseContext], SetupPhaseOutcome]
AssignmentAction = Callable[[], object]
InventoryProvider = Callable[[SetupUserInput], PreflightInventory]
SetupRunCancelled = Callable[[str, str], None]
CancellationCheckpoint = Callable[[], None]


def _token(prefix: str) -> str:
    """Return a server-created identifier; callers never supply run identifiers."""

    return f"{prefix}-{uuid.uuid4()}"


def _plain_prompt(message: str) -> str:
    """Keep diagnostics usable without hiding structured setup evidence."""

    return message.strip()


@dataclass(slots=True)
class SetupRun:
    """In-memory state for one board/connection-bound setup investigation.

    A setup run records only the facts needed to reject stale or cross-board
    continuation.  It does not grant authority and deliberately has no call cap.
    """

    setup_run_id: str
    board_id: str
    connection_id: str
    mode: SetupMode
    user_input: SetupUserInput
    closed: bool = False
    close_reason: str | None = None


@dataclass(slots=True)
class _SetupState:
    continuation_id: str
    setup_run_id: str
    mode: SetupMode
    user_input: SetupUserInput
    phase_records: dict[SetupPhase, PhaseRecord]
    attempts: list[str] = field(default_factory=list)
    last_status: SetupTerminalStatus | None = None
    last_preflight: PreflightDecision | None = None


@dataclass(frozen=True, slots=True)
class SetupResponse:
    status: SetupTerminalStatus
    setup_run_id: str
    continuation_id: str
    attempt_id: str
    agent_prompt: str
    choices: tuple[FriendlyChoice, ...]
    first_unverified_phase: SetupPhase | None
    phase_records: tuple[PhaseRecord, ...]
    report_paths: ReportPaths

    def to_payload(self) -> dict[str, Any]:
        accepted_response: dict[str, Any] | None = None
        if self.status == "setup_needs_user_input" and self.choices:
            accepted_response = {
                "tool": "continue_board_setup",
                "response": {"choice_id": "one exact choice_id returned above"},
            }
        elif self.status == "setup_research_required":
            exact_fields = next(
                (
                    record.details.get("exact_response_fields")
                    for record in reversed(self.phase_records)
                    if isinstance(record.details.get("exact_response_fields"), list)
                ),
                None,
            )
            placeholders: dict[str, Any] = {
                "pack_id": "official vendor pack identifier",
                "version": "official pack version",
                "filename": "official.pack",
                "url": "official vendor source URL",
                "source_path": "local path to the acquired official .pack bytes",
                "official_sha256": None,
                "target": "exact provider target name",
                "debug_protocol": "default, swd, or jtag",
                "debug_connect_mode": "attach, halt, pre-reset, or under-reset",
                "debug_clock_hz": 1_000_000,
                "evidence": [{"source": "official source", "claim": "resolved fact"}],
                "reasoning_summary": "why the evidence resolves the exact requested fact",
            }
            response_fields = (
                tuple(str(field) for field in exact_fields)
                if exact_fields is not None
                else (
                    "pack_id",
                    "version",
                    "filename",
                    "url",
                    "source_path",
                    "official_sha256",
                    "evidence",
                    "reasoning_summary",
                )
            )
            accepted_response = {
                "tool": "continue_board_setup",
                "response": {
                    field: placeholders.get(field, "required value") for field in response_fields
                },
            }
        return {
            "status": self.status,
            "continuation_id": self.continuation_id,
            "agent_prompt": self.agent_prompt,
            "choices": [asdict(choice) for choice in self.choices],
            "observed": {
                "setup_run_id": self.setup_run_id,
                "attempt_id": self.attempt_id,
                "first_unverified_phase": (
                    self.first_unverified_phase.value if self.first_unverified_phase else None
                ),
                "phase_records": [
                    {
                        "phase": record.phase.value,
                        "state": record.state.value,
                        "code": record.code,
                        "details": record.details,
                    }
                    for record in self.phase_records
                ],
                "report": str(self.report_paths.report),
                "events": str(self.report_paths.events),
            },
            "evidence_note": (
                "Target, package, hardware, and physical-memory facts include provenance; unresolved "
                "facts remain explicit."
            ),
            "rejected_candidates": [],
            "accepted_response": accepted_response,
            "phase_progress": [record.phase.value for record in self.phase_records],
            "first_unverified_phase": (
                self.first_unverified_phase.value if self.first_unverified_phase else None
            ),
        }


def first_unverified_phase(
    records: Mapping[SetupPhase, PhaseRecord],
) -> SetupPhase | None:
    """Return the first phase that current evidence has not verified."""

    for phase in PHASE_ORDER:
        record = records.get(phase)
        if record is None or record.state is not PhaseState.VERIFIED:
            return phase
    return None


def _default_phase_handler(phase: SetupPhase) -> SetupPhaseHandler:
    def pending(context: SetupPhaseContext) -> SetupPhaseOutcome:
        del context
        if phase in {SetupPhase.TARGET_RESOLUTION, SetupPhase.TARGET_SUPPORT}:
            return SetupPhaseOutcome.stop(
                "setup_research_required",
                f"setup/{phase.value}-interface-pending",
                "Use continue_board_setup with exactly the official-source package response schema "
                "returned here; the server derives the target. Do not invent or ask the user "
                "for a package or debug-target identifier.",
            )
        return SetupPhaseOutcome.stop(
            "setup_unresolved",
            f"setup/{phase.value}-interface-pending",
            f"The server interface for {phase.value.replace('_', ' ')} is not installed yet. "
            "Stop rather than guessing or repeating the operation.",
        )

    return pending


class SetupWorkflow:
    """Own resumable setup runs and their immutable diagnostic reports.

    Runs are deliberately in-memory and bound to one board and connection.  A
    replacement assignment or disconnect cancels the affected run so a stale
    continuation cannot apply its result to different hardware.
    """

    def __init__(
        self,
        reports: ReportWriter,
        inventory_provider: InventoryProvider,
        *,
        preflight: PreflightEngine | None = None,
        phase_handlers: Mapping[SetupPhase, SetupPhaseHandler] | None = None,
        on_run_cancelled: SetupRunCancelled | None = None,
        cancellation_checkpoint: CancellationCheckpoint | None = None,
    ) -> None:
        self.reports = reports
        self.inventory_provider = inventory_provider
        self.preflight = preflight or PreflightEngine()
        supplied = dict(phase_handlers or {})
        self.phase_handlers = {
            phase: supplied.get(phase, _default_phase_handler(phase))
            for phase in PHASE_ORDER
            if phase
            not in {
                SetupPhase.INPUT,
                SetupPhase.PREFLIGHT,
                SetupPhase.SELECTION,
                SetupPhase.TARGET_RESOLUTION,
            }
        }
        self.on_run_cancelled = on_run_cancelled or (lambda board_id, reason: None)
        self.cancellation_checkpoint = cancellation_checkpoint or (lambda: None)
        self._runs: dict[str, SetupRun] = {}
        self._current_run_by_board: dict[str, str] = {}
        self._states: dict[str, _SetupState] = {}
        self._continuation_by_run: dict[str, str] = {}
        self._guard = threading.RLock()

    def start_setup(
        self,
        user_input: SetupUserInput,
        *,
        mode: SetupMode = "setup",
        selections: PreflightSelections | None = None,
    ) -> SetupResponse:
        """Start a new server-created setup run, replacing any current board run."""

        run, state = self._create_run(user_input, mode)
        return self._run_attempt(state, run, selections or PreflightSelections())

    def _create_run(
        self,
        user_input: SetupUserInput,
        mode: SetupMode,
    ) -> tuple[SetupRun, _SetupState]:
        if mode not in {"setup", "repair"}:
            raise SetupWorkflowError("mode must be setup or repair")
        with self._guard:
            previous_id = self._current_run_by_board.get(user_input.board_id)
            if previous_id is not None:
                self._cancel_run_locked(previous_id, "replaced by a newer setup run")
            run = SetupRun(
                _token("setup-run"),
                user_input.board_id,
                user_input.connection_id,
                mode,
                user_input,
            )
            continuation_id = _token("setup-continuation")
            state = _SetupState(
                continuation_id,
                run.setup_run_id,
                mode,
                user_input,
                {
                    SetupPhase.INPUT: PhaseRecord(
                        SetupPhase.INPUT,
                        PhaseState.VERIFIED,
                        "setup/input-verified",
                        {
                            "display_name": user_input.display_name,
                            "mcu_part_number": user_input.mcu_part_number,
                            "serial_baudrate": user_input.serial_baudrate,
                        },
                    )
                },
            )
            self._runs[run.setup_run_id] = run
            self._current_run_by_board[user_input.board_id] = run.setup_run_id
            self._states[continuation_id] = state
            self._continuation_by_run[run.setup_run_id] = continuation_id
            return run, state

    def repair_setup(
        self,
        board_id: str,
        *,
        selections: PreflightSelections | None = None,
    ) -> SetupResponse:
        """Retry the current board run; repeat repairs are intentionally unlimited."""

        with self._guard:
            run_id = self._current_run_by_board.get(board_id)
            if run_id is None:
                raise SetupWorkflowError("No current setup run is available to repair")
            run = self._require_open_run_locked(run_id)
            continuation_id = self._continuation_by_run.get(run_id)
            if continuation_id is None:
                raise SetupWorkflowError("No incomplete setup attempt is available to repair")
            state = self._states[continuation_id]
        return self._run_attempt(state, run, selections or PreflightSelections(), repair=True)

    def _run_attempt(
        self,
        state: _SetupState,
        run: SetupRun,
        selections: PreflightSelections,
        *,
        repair: bool = False,
    ) -> SetupResponse:
        attempt_id = _token("setup-attempt")
        state.attempts.append(attempt_id)
        terminal: SetupTerminalStatus
        prompt: str
        choices: tuple[FriendlyChoice, ...] = ()
        decision: PreflightDecision | None = None
        self.cancellation_checkpoint()
        try:
            inventory = self.inventory_provider(state.user_input)
            self.cancellation_checkpoint()
            decision = self.preflight.evaluate(state.user_input, inventory, selections)
            state.last_preflight = decision
            self._apply_preflight_records(state, decision)

            if decision.status != "preflight_ready":
                terminal = decision.status
                prompt = decision.agent_prompt
                choices = decision.choices
            else:
                terminal, prompt, choices = self._run_remaining_phases(
                    state,
                    run,
                    attempt_id,
                    decision,
                    repair=repair,
                )
        except OperationCancelledError:
            with self._guard:
                self._cancel_run_locked(run.setup_run_id, "managed setup operation cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - every attempt must produce a report
            failed_phase = first_unverified_phase(state.phase_records) or SetupPhase.PREFLIGHT
            state.phase_records[failed_phase] = PhaseRecord(
                failed_phase,
                PhaseState.FAILED,
                "setup/unexpected-error",
                {"error_type": type(exc).__name__, "message": str(exc)},
            )
            terminal = "setup_unresolved"
            prompt = _plain_prompt(
                "Setup stopped because the server encountered an unexpected deterministic "
                "workflow error. Report the plain-language failure and stop rather than loop."
            )

        self.cancellation_checkpoint()
        state.last_status = terminal
        report_paths = self._write_report(
            state,
            attempt_id,
            terminal,
            decision,
            repair=repair,
        )
        resume_phase = first_unverified_phase(state.phase_records)
        response = SetupResponse(
            terminal,
            state.setup_run_id,
            state.continuation_id,
            attempt_id,
            _plain_prompt(prompt),
            choices,
            resume_phase,
            tuple(
                state.phase_records[phase] for phase in PHASE_ORDER if phase in state.phase_records
            ),
            report_paths,
        )
        return response

    @staticmethod
    def _apply_preflight_records(
        state: _SetupState,
        decision: PreflightDecision,
    ) -> None:
        if decision.status == "setup_blocked":
            state.phase_records[SetupPhase.PREFLIGHT] = PhaseRecord(
                SetupPhase.PREFLIGHT,
                PhaseState.FAILED,
                decision.code,
                decision.observed,
            )
            return
        state.phase_records[SetupPhase.PREFLIGHT] = PhaseRecord(
            SetupPhase.PREFLIGHT,
            PhaseState.VERIFIED,
            "setup/live-preflight-complete",
            decision.observed,
        )
        if decision.status == "setup_needs_user_input":
            state.phase_records[SetupPhase.SELECTION] = PhaseRecord(
                SetupPhase.SELECTION,
                PhaseState.UNVERIFIED,
                decision.code,
                {"friendly_choices": [choice.label for choice in decision.choices]},
            )
            return
        state.phase_records[SetupPhase.SELECTION] = PhaseRecord(
            SetupPhase.SELECTION,
            PhaseState.VERIFIED,
            "setup/selections-resolved",
            {
                "probe": decision.selected_probe.friendly_label()
                if decision.selected_probe
                else None,
                "serial": decision.selected_serial.friendly_label()
                if decision.selected_serial
                else None,
                "build": decision.selected_build.friendly_label()
                if decision.selected_build
                else None,
            },
        )
        if decision.status == "setup_research_required":
            state.phase_records[SetupPhase.TARGET_RESOLUTION] = PhaseRecord(
                SetupPhase.TARGET_RESOLUTION,
                PhaseState.UNVERIFIED,
                decision.code,
                {"detected_targets": decision.observed.get("exact_detected_targets", [])},
            )
            return
        state.phase_records[SetupPhase.TARGET_RESOLUTION] = PhaseRecord(
            SetupPhase.TARGET_RESOLUTION,
            PhaseState.VERIFIED,
            "setup/exact-target-detected",
            {"target": decision.selected_target},
        )

    def _run_remaining_phases(
        self,
        state: _SetupState,
        run: SetupRun,
        attempt_id: str,
        decision: PreflightDecision,
        *,
        repair: bool,
    ) -> tuple[SetupTerminalStatus, str, tuple[FriendlyChoice, ...]]:
        del repair  # the verified-record routing below is the repair behavior
        for phase in PHASE_ORDER:
            if phase in {
                SetupPhase.INPUT,
                SetupPhase.PREFLIGHT,
                SetupPhase.SELECTION,
                SetupPhase.TARGET_RESOLUTION,
            }:
                continue
            previous = state.phase_records.get(phase)
            if previous is not None and previous.state is PhaseState.VERIFIED:
                continue

            # Serialize each phase with run cancellation. A replacement assignment may
            # wait for an in-flight phase, but the next phase must not use stale facts.
            with self._guard:
                self.cancellation_checkpoint()
                if run.closed or self._current_run_by_board.get(run.board_id) != run.setup_run_id:
                    return (
                        "setup_blocked",
                        (
                            "The setup board or connection changed while setup was running. "
                            "Stop and start a fresh setup run for the current hardware."
                        ),
                        (),
                    )

                # Built-in or pinned support is a deterministic target-support success.
                if phase is SetupPhase.TARGET_SUPPORT and decision.selected_target is not None:
                    target_sets = set(decision.observed.get("built_in_targets", [])) | set(
                        decision.observed.get("manifest_targets", [])
                    )
                    if decision.selected_target in target_sets:
                        state.phase_records[phase] = PhaseRecord(
                            phase,
                            PhaseState.VERIFIED,
                            "setup/target-support-present",
                            {"target": decision.selected_target},
                        )
                        continue

                context = SetupPhaseContext(
                    state.continuation_id,
                    attempt_id,
                    state.mode,
                    state.user_input,
                    decision,
                    dict(state.phase_records),
                )
                outcome = self.phase_handlers[phase](context)
                self.cancellation_checkpoint()
                if outcome.verified:
                    state.phase_records[phase] = PhaseRecord(
                        phase,
                        PhaseState.VERIFIED,
                        outcome.code,
                        dict(outcome.details),
                    )
                    continue
                terminal = outcome.terminal_status or "setup_unresolved"
                record_state = (
                    PhaseState.UNVERIFIED
                    if terminal
                    in {
                        "setup_needs_user_input",
                        "setup_research_required",
                    }
                    else PhaseState.FAILED
                )
                state.phase_records[phase] = PhaseRecord(
                    phase,
                    record_state,
                    outcome.code,
                    dict(outcome.details),
                )
                return terminal, outcome.agent_prompt, outcome.choices

        return (
            "setup_completed",
            (
                "Setup and its automatic validation completed. The setup actions are now "
                "closed; continue only with the newly validated board assignment."
            ),
            (),
        )

    def _write_report(
        self,
        state: _SetupState,
        attempt_id: str,
        status: SetupTerminalStatus,
        decision: PreflightDecision | None,
        *,
        repair: bool,
    ) -> ReportPaths:
        selected_hardware = {
            "probe": asdict(decision.selected_probe)
            if decision and decision.selected_probe
            else None,
            "serial": (
                asdict(decision.selected_serial) if decision and decision.selected_serial else None
            ),
            "build_configuration": (
                asdict(decision.selected_build) if decision and decision.selected_build else None
            ),
        }
        fields: dict[str, Any] = {
            "board_id": state.user_input.board_id,
            "connection_id": state.user_input.connection_id,
            "continuation_id": state.continuation_id,
            "mode": "repair" if repair else state.mode,
            "terminal_status": status,
            "inventories": decision.observed if decision else {},
            "selected_hardware": selected_hardware,
            "cache_outcome": (decision.observed.get("cache", {}) if decision is not None else {}),
            "target_resolution": {
                "selected_target": decision.selected_target if decision else None,
                "research_required": decision.research_required if decision else False,
            },
            "package_resolution": {},
            "research_exchanges": [],
            "candidate_validation_results": [],
            "connection_results": self._phase_details(state, SetupPhase.CONNECTION),
            "phase_records": [
                {
                    "phase": record.phase.value,
                    "state": record.state.value,
                    "code": record.code,
                    "details": record.details,
                }
                for phase in PHASE_ORDER
                if (record := state.phase_records.get(phase)) is not None
            ],
        }
        paths = self.reports.create_setup(attempt_id, fields)
        for record in fields["phase_records"]:
            assert isinstance(record, dict)
            self.reports.append_setup_event(
                attempt_id,
                {
                    "phase": record["phase"],
                    "state": record["state"],
                    "code": record["code"],
                },
            )
        return paths

    @staticmethod
    def _phase_details(state: _SetupState, phase: SetupPhase) -> dict[str, Any]:
        record = state.phase_records.get(phase)
        return dict(record.details) if record is not None else {}

    def cancel(self, continuation_id: str) -> None:
        with self._guard:
            state = self._states.get(continuation_id)
            if state is None:
                raise SetupWorkflowError(f"Unknown continuation '{continuation_id}'")
            self._cancel_run_locked(state.setup_run_id, "setup workflow cancelled")

    def continuation_context(
        self, continuation_id: str
    ) -> tuple[SetupUserInput, SetupTerminalStatus | None, PreflightDecision | None]:
        """Return the immutable input and last routing result for one live continuation.

        The public setup continuation tool uses this read-only view to validate a friendly
        selection or research reply. It deliberately does not mutate phase records;
        its board and connection checks prevent stale continuation from crossing runs.
        """

        with self._guard:
            state = self._states.get(continuation_id)
            if state is None:
                raise SetupWorkflowError(f"Unknown continuation '{continuation_id}'")
            run = self._require_open_run_locked(state.setup_run_id)
            if (
                run.board_id != state.user_input.board_id
                or run.connection_id != state.user_input.connection_id
            ):
                raise SetupWorkflowError("Setup continuation board scope is inconsistent")
            return state.user_input, state.last_status, state.last_preflight

    def disconnect(self, connection_id: str) -> None:
        with self._guard:
            matching = [
                run.setup_run_id
                for run in self._runs.values()
                if not run.closed and run.connection_id == connection_id
            ]
            for run_id in matching:
                self._cancel_run_locked(run_id, "scoped connection disconnected")

    def cancel_board(self, board_id: str, reason: str = "board setup run cancelled") -> None:
        """Cancel the current run when its board is reassigned or removed."""

        with self._guard:
            matching = [
                run.setup_run_id
                for run in self._runs.values()
                if not run.closed and run.board_id == board_id
            ]
            for run_id in matching:
                self._cancel_run_locked(run_id, reason)

    def _require_open_run_locked(self, setup_run_id: str) -> SetupRun:
        run = self._runs.get(setup_run_id)
        if run is None:
            raise SetupWorkflowError(f"Unknown setup run '{setup_run_id}'")
        if run.closed:
            raise SetupWorkflowError(f"Setup run '{setup_run_id}' is cancelled: {run.close_reason}")
        return run

    def _cancel_run_locked(self, setup_run_id: str, reason: str) -> None:
        run = self._runs.get(setup_run_id)
        if run is None or run.closed:
            return
        run.closed = True
        run.close_reason = reason
        if self._current_run_by_board.get(run.board_id) == setup_run_id:
            self._current_run_by_board.pop(run.board_id, None)
        self.on_run_cancelled(run.board_id, reason)


@dataclass(frozen=True, slots=True)
class ProfileRouteView:
    board_id: str
    display_name: str
    setup_status: str | None = None


AssignmentRouteKind = Literal[
    "no_board",
    "validate",
    "setup",
    "repair",
    "mismatch",
    "conflict",
]


@dataclass(frozen=True, slots=True)
class AssignmentRoute:
    kind: AssignmentRouteKind
    board_id: str | None
    agent_prompt: str


class RunAssignmentStore:
    """One-to-one, in-memory connection/profile assignment bindings."""

    def __init__(self, assignments: dict[object, Any]) -> None:
        self._assignments = assignments
        self._guard = threading.RLock()

    def assign(self, connection_id: str, board_id: str) -> None:
        connection = connection_id.strip()
        board = board_id.strip()
        if not connection or not board:
            raise SetupWorkflowError("connection_id and board_id must be non-empty")
        with self._guard:
            current_board = self._assignments.get(("connection", connection))
            current_connection = self._assignments.get(("board", board))
            if current_board not in {None, board}:
                raise SetupWorkflowError(
                    f"Connection '{connection}' is already assigned to another profile"
                )
            if current_connection not in {None, connection}:
                raise SetupWorkflowError(
                    f"Board '{board}' is already assigned to another connection"
                )
            self._assignments[("connection", connection)] = board
            self._assignments[("board", board)] = connection

    def replace(self, bindings: Mapping[str, str]) -> None:
        """Atomically replace the run's provisional one-to-one assignments."""

        normalized = {connection.strip(): board.strip() for connection, board in bindings.items()}
        if any(not connection or not board for connection, board in normalized.items()):
            raise SetupWorkflowError("connection_id and board_id must be non-empty")
        if len(set(normalized.values())) != len(normalized):
            raise SetupWorkflowError("A board cannot be assigned to two connections")
        with self._guard:
            self._assignments.clear()
            for connection, board in normalized.items():
                self._assignments[("connection", connection)] = board
                self._assignments[("board", board)] = connection

    def require(self, connection_id: str, board_id: str) -> None:
        """Require an exact assignment without creating or changing one."""

        connection = connection_id.strip()
        board = board_id.strip()
        with self._guard:
            if (
                self._assignments.get(("connection", connection)) != board
                or self._assignments.get(("board", board)) != connection
            ):
                raise SetupWorkflowError(
                    "The board and debug connection do not match the current get_setup_overview "
                    "assignment. Call get_setup_overview again before hardware access."
                )

    def bindings(self) -> dict[str, str]:
        """Return a detached connection-to-board snapshot."""

        with self._guard:
            return {
                str(key[1]): str(value)
                for key, value in self._assignments.items()
                if isinstance(key, tuple)
                and len(key) == 2
                and key[0] == "connection"
                and isinstance(value, str)
            }

    def connection_for(self, board_id: str) -> str | None:
        """Return the current run-scoped connection assigned to one board, if any."""

        board = board_id.strip()
        if not board:
            return None
        with self._guard:
            connection = self._assignments.get(("board", board))
            return connection if isinstance(connection, str) else None

    def run_if_current(
        self,
        connection_id: str,
        board_id: str,
        action: AssignmentAction,
    ) -> None:
        """Run one stamp action atomically with exact assignment verification."""

        connection = connection_id.strip()
        board = board_id.strip()
        with self._guard:
            if (
                self._assignments.get(("connection", connection)) != board
                or self._assignments.get(("board", board)) != connection
            ):
                raise SetupWorkflowError(
                    "The board assignment changed before live validation could stamp it."
                )
            action()

    def clear_connection(self, connection_id: str) -> None:
        connection = connection_id.strip()
        with self._guard:
            board = self._assignments.pop(("connection", connection), None)
            if isinstance(board, str):
                self._assignments.pop(("board", board), None)

    def clear_board(self, board_id: str) -> None:
        board = board_id.strip()
        with self._guard:
            connection = self._assignments.pop(("board", board), None)
            if isinstance(connection, str):
                self._assignments.pop(("connection", connection), None)

    def mismatch(self, connection_id: str, board_id: str) -> AssignmentRoute:
        self.clear_connection(connection_id)
        return AssignmentRoute(
            "mismatch",
            board_id,
            _plain_prompt(
                "The live hardware does not match the selected profile. Tell the user and ask "
                "what they want to do. Keeping the different hardware requires a new logical "
                "board/profile; the established profile has not been changed."
            ),
        )
