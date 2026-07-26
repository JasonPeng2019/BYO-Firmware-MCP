"""Run-scoped, resumable board Setup orchestration."""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Literal

from pyocd_debug_mcp.firmstore.reports import ReportPaths, ReportWriter
from pyocd_debug_mcp.kernel.operations import OperationCancelledError
from pyocd_debug_mcp.setup_flow.preflight import (
    NO_INTERNALS_RELAY_INSTRUCTION,
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
    "setup_safety_incomplete",
)
SetupTerminalStatus = Literal[
    "setup_completed",
    "setup_needs_user_input",
    "setup_research_required",
    "setup_blocked",
    "setup_unresolved",
    "setup_connection_failed",
    "setup_validation_failed",
    "setup_safety_incomplete",
]
SetupMode = Literal["setup", "repair"]


class SetupWorkflowError(RuntimeError):
    """The setup workflow or its run-scoped allowance cannot continue."""


class SetupPhase(str, Enum):
    INPUT = "input"
    PREFLIGHT = "preflight"
    SELECTION = "selection"
    TARGET_RESOLUTION = "target_resolution"
    TARGET_SUPPORT = "target_support"
    CONNECTION = "connection"
    VALIDATION = "validation"
    SAFETY_RESEARCH = "safety_research"
    SAFETY_MAP = "safety_map"
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
    """Result contract implemented by later target, validation, and safety tasks."""

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
            _relay_prompt(agent_prompt),
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
AllowanceClosed = Callable[[str, str, str], None]
CacheConfirmationHandler = Callable[[SetupUserInput, PreflightDecision], None]
CancellationCheckpoint = Callable[[], None]


def _relay_prompt(message: str) -> str:
    text = message.strip()
    if NO_INTERNALS_RELAY_INSTRUCTION not in text:
        text = f"{text} {NO_INTERNALS_RELAY_INSTRUCTION}"
    return text


def _token(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(8)}"


@dataclass(slots=True)
class _SetupAllowance:
    allowance_id: str
    board_id: str
    connection_id: str
    mode: SetupMode
    setup_called: bool = False
    fix_called: bool = False
    closed: bool = False
    close_reason: str | None = None


@dataclass(slots=True)
class _SetupState:
    continuation_id: str
    allowance_id: str
    mode: SetupMode
    user_input: SetupUserInput
    phase_records: dict[SetupPhase, PhaseRecord]
    attempts: list[str] = field(default_factory=list)
    last_status: SetupTerminalStatus | None = None
    last_preflight: PreflightDecision | None = None


@dataclass(frozen=True, slots=True)
class SetupResponse:
    status: SetupTerminalStatus
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
                "tool": "continue_setup",
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
                "pyocd_target": "exact installed pyOCD target name",
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
                "tool": "continue_setup",
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
            "constraints": [
                "Do not expose continuation tokens or internal fields to the user.",
                "Do not guess target, package, hardware, or safety facts.",
            ],
            "rejected_candidates": [],
            "accepted_response": accepted_response,
            "validation_plan": [record.phase.value for record in self.phase_records],
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
        if phase in {SetupPhase.SAFETY_RESEARCH, SetupPhase.SAFETY_MAP}:
            return SetupPhaseOutcome.stop(
                "setup_safety_incomplete",
                f"setup/{phase.value}-interface-pending",
                "Safety establishment is not available yet. Stop here and keep write-capable "
                "actions closed until the safety implementation is installed.",
            )
        if phase in {SetupPhase.TARGET_RESOLUTION, SetupPhase.TARGET_SUPPORT}:
            return SetupPhaseOutcome.stop(
                "setup_research_required",
                f"setup/{phase.value}-interface-pending",
                "Use continue_setup with exactly the official-source package response schema "
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
    """Own setup attempts, paired setup/fix allowances, and immutable reports.

    This object is intentionally run-scoped. Durable reports contain evidence but
    never restore an allowance, permission, assignment, or unlocked tool.
    """

    def __init__(
        self,
        reports: ReportWriter,
        inventory_provider: InventoryProvider,
        *,
        preflight: PreflightEngine | None = None,
        phase_handlers: Mapping[SetupPhase, SetupPhaseHandler] | None = None,
        on_allowance_closed: AllowanceClosed | None = None,
        on_cache_confirmation: CacheConfirmationHandler | None = None,
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
        self.on_allowance_closed = on_allowance_closed or (
            lambda board_id, allowance_id, reason: None
        )
        self.on_cache_confirmation = on_cache_confirmation or (lambda user_input, decision: None)
        self.cancellation_checkpoint = cancellation_checkpoint or (lambda: None)
        self._allowances: dict[str, _SetupAllowance] = {}
        self._current_allowance_by_board: dict[str, str] = {}
        self._states: dict[str, _SetupState] = {}
        self._continuation_by_allowance: dict[str, str] = {}
        self._guard = threading.RLock()

    def begin_plan(
        self,
        allowance_id: str,
        user_input: SetupUserInput,
        *,
        mode: SetupMode,
    ) -> None:
        """Register one externally validated board_setup plan allowance."""

        if mode not in {"setup", "repair"}:
            raise SetupWorkflowError("mode must be setup or repair")
        normalized_id = allowance_id.strip()
        if not normalized_id:
            raise SetupWorkflowError("allowance_id must be non-empty")
        with self._guard:
            if normalized_id in self._allowances:
                raise SetupWorkflowError(f"Setup allowance '{normalized_id}' already exists")
            previous_id = self._current_allowance_by_board.get(user_input.board_id)
            if previous_id is not None:
                self._close_allowance_locked(previous_id, "replaced by a new setup plan")
            self._allowances[normalized_id] = _SetupAllowance(
                normalized_id,
                user_input.board_id,
                user_input.connection_id,
                mode,
            )
            self._current_allowance_by_board[user_input.board_id] = normalized_id

    def board_setup(
        self,
        allowance_id: str,
        user_input: SetupUserInput,
        *,
        selections: PreflightSelections | None = None,
    ) -> SetupResponse:
        with self._guard:
            allowance = self._require_allowance_locked(allowance_id, user_input)
            if allowance.setup_called:
                raise SetupWorkflowError("board_setup is allowed exactly once by this setup plan")
            allowance.setup_called = True
            continuation_id = _token("setup-continuation")
            state = _SetupState(
                continuation_id,
                allowance.allowance_id,
                allowance.mode,
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
            self._states[continuation_id] = state
            self._continuation_by_allowance[allowance_id] = continuation_id
        return self._run_attempt(state, allowance, selections or PreflightSelections())

    def board_fix_setup(
        self,
        allowance_id: str,
        *,
        selections: PreflightSelections | None = None,
    ) -> SetupResponse:
        with self._guard:
            allowance = self._require_open_allowance_locked(allowance_id)
            if not allowance.setup_called:
                raise SetupWorkflowError("board_fix_setup cannot run before board_setup")
            if allowance.fix_called:
                raise SetupWorkflowError(
                    "board_fix_setup is allowed exactly once by this setup plan; replace the plan"
                )
            continuation_id = self._continuation_by_allowance.get(allowance_id)
            if continuation_id is None:
                raise SetupWorkflowError("No incomplete setup attempt is available to repair")
            state = self._states[continuation_id]
            allowance.fix_called = True
        response = self._run_attempt(
            state,
            allowance,
            selections or PreflightSelections(),
            repair=True,
        )
        with self._guard:
            if not allowance.closed:
                self._close_allowance_locked(
                    allowance.allowance_id,
                    f"paired board_fix_setup ended with {response.status}",
                )
        return response

    def _run_attempt(
        self,
        state: _SetupState,
        allowance: _SetupAllowance,
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
                if decision.selected_serial is not None:
                    self.cancellation_checkpoint()
                    self.on_cache_confirmation(state.user_input, decision)
                terminal, prompt, choices = self._run_remaining_phases(
                    state,
                    allowance,
                    attempt_id,
                    decision,
                    repair=repair,
                )
        except OperationCancelledError:
            with self._guard:
                self._close_allowance_locked(
                    allowance.allowance_id,
                    "managed setup operation cancelled",
                )
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
            prompt = _relay_prompt(
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
            state.continuation_id,
            attempt_id,
            _relay_prompt(prompt),
            choices,
            resume_phase,
            tuple(
                state.phase_records[phase] for phase in PHASE_ORDER if phase in state.phase_records
            ),
            report_paths,
        )
        with self._guard:
            if terminal in {"setup_completed", "setup_blocked", "setup_unresolved"}:
                self._close_allowance_locked(
                    allowance.allowance_id,
                    f"workflow ended with {terminal}",
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
        allowance: _SetupAllowance,
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

            # Serialize each phase with revocation. A replacement assignment may wait for
            # an in-flight phase, but it closes the allowance before another phase or the
            # commit phase can begin.
            with self._guard:
                self.cancellation_checkpoint()
                if (
                    allowance.closed
                    or self._current_allowance_by_board.get(allowance.board_id)
                    != allowance.allowance_id
                ):
                    return (
                        "setup_blocked",
                        _relay_prompt(
                            "The setup assignment or authorization changed while setup was "
                            "running. Stop and request a fresh setup overview and plan."
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
                        "setup_safety_incomplete",
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
            _relay_prompt(
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
            "safety_sources": self._phase_details(state, SetupPhase.SAFETY_RESEARCH),
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
            self._close_allowance_locked(state.allowance_id, "setup workflow cancelled")

    def continuation_context(
        self, continuation_id: str
    ) -> tuple[SetupUserInput, SetupTerminalStatus | None, PreflightDecision | None]:
        """Return the immutable input and last routing result for one live continuation.

        The public setup continuation tool uses this read-only view to validate a friendly
        selection or research reply.  It deliberately does not expose the allowance, mutate
        phase records, or turn a continuation token into authorization.
        """

        with self._guard:
            state = self._states.get(continuation_id)
            if state is None:
                raise SetupWorkflowError(f"Unknown continuation '{continuation_id}'")
            allowance = self._require_open_allowance_locked(state.allowance_id)
            if allowance.board_id != state.user_input.board_id:
                raise SetupWorkflowError("Setup continuation board scope is inconsistent")
            return state.user_input, state.last_status, state.last_preflight

    def disconnect(self, connection_id: str) -> None:
        with self._guard:
            matching = [
                allowance.allowance_id
                for allowance in self._allowances.values()
                if not allowance.closed and allowance.connection_id == connection_id
            ]
            for allowance_id in matching:
                self._close_allowance_locked(allowance_id, "scoped connection disconnected")

    def revoke(self, board_id: str) -> None:
        with self._guard:
            matching = [
                allowance.allowance_id
                for allowance in self._allowances.values()
                if not allowance.closed and allowance.board_id == board_id
            ]
            for allowance_id in matching:
                self._close_allowance_locked(allowance_id, "user revoked setup authorization")

    def allowance_closed(self, allowance_id: str) -> bool:
        with self._guard:
            allowance = self._allowances.get(allowance_id)
            return allowance is None or allowance.closed

    def _require_allowance_locked(
        self,
        allowance_id: str,
        user_input: SetupUserInput,
    ) -> _SetupAllowance:
        allowance = self._require_open_allowance_locked(allowance_id)
        if (
            allowance.board_id != user_input.board_id
            or allowance.connection_id != user_input.connection_id
        ):
            raise SetupWorkflowError(
                "Setup input differs from the board and connection bound by the setup plan"
            )
        return allowance

    def _require_open_allowance_locked(self, allowance_id: str) -> _SetupAllowance:
        allowance = self._allowances.get(allowance_id)
        if allowance is None:
            raise SetupWorkflowError(f"Unknown setup allowance '{allowance_id}'")
        if allowance.closed:
            raise SetupWorkflowError(
                f"Setup allowance '{allowance_id}' is closed: {allowance.close_reason}"
            )
        return allowance

    def _close_allowance_locked(self, allowance_id: str, reason: str) -> None:
        allowance = self._allowances.get(allowance_id)
        if allowance is None or allowance.closed:
            return
        allowance.closed = True
        allowance.close_reason = reason
        if self._current_allowance_by_board.get(allowance.board_id) == allowance_id:
            self._current_allowance_by_board.pop(allowance.board_id, None)
        self.on_allowance_closed(allowance.board_id, allowance.allowance_id, reason)


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
                    "The board and debug connection do not match the current setup_overview "
                    "assignment. Call setup_overview again before hardware access."
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
            _relay_prompt(
                "The live hardware does not match the selected profile. Tell the user and ask "
                "what they want to do. Keeping the different hardware requires a new logical "
                "board/profile; the established profile has not been changed."
            ),
        )
