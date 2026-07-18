from __future__ import annotations

import json
from pathlib import Path

import pytest

from pyocd_debug_mcp.firmstore.reports import ReportWriter
from pyocd_debug_mcp.firmstore.store import FirmStore, ImmutableArtifactError
from pyocd_debug_mcp.kernel.run_state import ServerRun
from pyocd_debug_mcp.setup_flow.preflight import (
    NO_INTERNALS_RELAY_INSTRUCTION,
    PreflightInventory,
    PreflightSelections,
    ProbeCandidate,
    SerialCandidate,
    SetupUserInput,
)
from pyocd_debug_mcp.setup_flow.setup import (
    PHASE_ORDER,
    AssignmentRoute,
    PhaseState,
    ProfileRouteView,
    RunAssignmentStore,
    SetupPhase,
    SetupPhaseOutcome,
    SetupWorkflow,
    SetupWorkflowError,
    route_board_name,
)


USER_INPUT = SetupUserInput(
    board_id="bench_board",
    connection_id="probe:001",
    display_name="Bénch Board",
    mcu_part_number="STM32L476RGT6",
    serial_baudrate=115200,
)


def inventory() -> PreflightInventory:
    return PreflightInventory(
        probes=(ProbeCandidate("probe-a", "ST-Link board", "stlink", "PROBE-001"),),
        serial_ports=(
            SerialCandidate("uart-a", "COM3", "ST-Link virtual serial", "UART-001", 0x0483, 0x5740),
        ),
        built_in_targets=("stm32l476rgtx",),
        exact_detected_targets=("stm32l476rgtx",),
    )


def success_handlers() -> dict[SetupPhase, object]:
    return {
        phase: (
            lambda context, selected=phase: SetupPhaseOutcome.success(
                f"test/{selected.value}-verified"
            )
        )
        for phase in PHASE_ORDER
        if phase
        not in {
            SetupPhase.INPUT,
            SetupPhase.PREFLIGHT,
            SetupPhase.SELECTION,
            SetupPhase.TARGET_RESOLUTION,
            SetupPhase.TARGET_SUPPORT,
        }
    }


def workflow(
    tmp_path: Path,
    provider,
    *,
    handlers=None,
    closed=None,
) -> SetupWorkflow:
    close_callback = (
        (lambda board_id, reason: closed.append((board_id, reason))) if closed is not None else None
    )
    return SetupWorkflow(
        ReportWriter(FirmStore(tmp_path)),
        provider,
        phase_handlers=handlers or success_handlers(),  # type: ignore[arg-type]
        on_allowance_closed=close_callback,
    )


def test_failed_setup_gets_one_fix_with_fresh_preflight_and_first_unverified_resume(
    tmp_path: Path,
) -> None:
    inventory_calls = 0
    connection_calls = 0

    def provider(user_input: SetupUserInput) -> PreflightInventory:
        nonlocal inventory_calls
        assert user_input.mcu_part_number == "STM32L476RGT6"
        inventory_calls += 1
        return inventory()

    handlers = success_handlers()

    def connection(context) -> SetupPhaseOutcome:
        nonlocal connection_calls
        connection_calls += 1
        if connection_calls == 1:
            return SetupPhaseOutcome.stop(
                "setup_connection_failed",
                "setup/test-connect-failed",
                "Reconnect the selected board, then use the one permitted repair attempt.",
            )
        return SetupPhaseOutcome.success("setup/test-connect-passed")

    handlers[SetupPhase.CONNECTION] = connection
    setup = workflow(tmp_path, provider, handlers=handlers)
    setup.begin_plan("allowance-1", USER_INPUT, mode="setup")

    first = setup.board_setup("allowance-1", USER_INPUT)
    fixed = setup.board_fix_setup("allowance-1")

    assert first.status == "setup_connection_failed"
    assert first.first_unverified_phase is SetupPhase.CONNECTION
    assert fixed.status == "setup_completed"
    assert fixed.first_unverified_phase is None
    assert inventory_calls == 2
    assert connection_calls == 2
    assert setup.allowance_closed("allowance-1") is True
    with pytest.raises(SetupWorkflowError, match="closed|exactly once"):
        setup.board_fix_setup("allowance-1")

    first_report = json.loads(first.report_paths.report.read_text(encoding="utf-8"))
    fixed_report = json.loads(fixed.report_paths.report.read_text(encoding="utf-8"))
    assert first_report["terminal_status"] == "setup_connection_failed"
    assert fixed_report["terminal_status"] == "setup_completed"
    assert first.report_paths.report != fixed.report_paths.report
    with pytest.raises(ImmutableArtifactError):
        setup.reports.create_setup(first.attempt_id, {"terminal_status": "changed"})


@pytest.mark.parametrize(
    ("phase", "status"),
    [
        (SetupPhase.CONNECTION, "setup_connection_failed"),
        (SetupPhase.VALIDATION, "setup_validation_failed"),
        (SetupPhase.SAFETY_MAP, "setup_safety_incomplete"),
        (SetupPhase.COMMIT, "setup_unresolved"),
    ],
)
def test_later_phase_terminal_statuses_record_the_first_unverified_transition(
    tmp_path: Path,
    phase: SetupPhase,
    status: str,
) -> None:
    handlers = success_handlers()
    handlers[phase] = lambda context: SetupPhaseOutcome.stop(  # type: ignore[assignment]
        status,  # type: ignore[arg-type]
        f"test/{phase.value}-stop",
        "Stop at this phase and follow the stated remedy.",
    )
    setup = workflow(tmp_path, lambda user_input: inventory(), handlers=handlers)
    setup.begin_plan("allowance-1", USER_INPUT, mode="setup")

    result = setup.board_setup("allowance-1", USER_INPUT)

    assert result.status == status
    assert result.first_unverified_phase is phase
    assert NO_INTERNALS_RELAY_INSTRUCTION in result.agent_prompt
    phase_record = next(record for record in result.phase_records if record.phase is phase)
    assert phase_record.state is not PhaseState.VERIFIED


@pytest.mark.parametrize(
    ("current_inventory", "status", "phase"),
    [
        (
            PreflightInventory(),
            "setup_blocked",
            SetupPhase.PREFLIGHT,
        ),
        (
            PreflightInventory(
                probes=(
                    ProbeCandidate("probe-a", "Left probe", "stlink", "PROBE-A"),
                    ProbeCandidate("probe-b", "Right probe", "jlink", "PROBE-B"),
                ),
                serial_ports=(SerialCandidate("uart-a", "COM3", "Board UART", "UART-A", 1, 2),),
                built_in_targets=("stm32l476rgtx",),
                exact_detected_targets=("stm32l476rgtx",),
            ),
            "setup_needs_user_input",
            SetupPhase.SELECTION,
        ),
        (
            PreflightInventory(
                probes=(ProbeCandidate("probe-a", "Left probe", "stlink", "PROBE-A"),),
                serial_ports=(SerialCandidate("uart-a", "COM3", "Board UART", "UART-A", 1, 2),),
            ),
            "setup_research_required",
            SetupPhase.TARGET_RESOLUTION,
        ),
    ],
)
def test_preflight_terminal_transitions_also_produce_immutable_attempt_reports(
    tmp_path: Path,
    current_inventory: PreflightInventory,
    status: str,
    phase: SetupPhase,
) -> None:
    setup = workflow(tmp_path, lambda user_input: current_inventory)
    setup.begin_plan("allowance-1", USER_INPUT, mode="setup")

    result = setup.board_setup("allowance-1", USER_INPUT)

    assert result.status == status
    assert result.first_unverified_phase is phase
    payload = result.to_payload()
    if status == "setup_needs_user_input":
        assert payload["accepted_response"] == {
            "tool": "continue_setup",
            "response": {"choice_id": "one exact choice_id returned above"},
        }
    elif status == "setup_research_required":
        assert payload["accepted_response"]["tool"] == "continue_setup"
        assert set(payload["accepted_response"]["response"]) == {
            "pyocd_target",
            "evidence",
            "reasoning_summary",
        }
    report = json.loads(result.report_paths.report.read_text(encoding="utf-8"))
    assert report["terminal_status"] == status
    assert NO_INTERNALS_RELAY_INSTRUCTION in result.agent_prompt


@pytest.mark.parametrize("closure", ["cancel", "disconnect", "revoke"])
def test_cancellation_disconnect_and_revocation_close_the_paired_allowance(
    tmp_path: Path,
    closure: str,
) -> None:
    handlers = success_handlers()
    handlers[SetupPhase.CONNECTION] = lambda context: SetupPhaseOutcome.stop(  # type: ignore[assignment]
        "setup_connection_failed",
        "test/connect-failed",
        "Reconnect before repairing.",
    )
    closed: list[tuple[str, str]] = []
    setup = SetupWorkflow(
        ReportWriter(FirmStore(tmp_path)),
        lambda user_input: inventory(),
        phase_handlers=handlers,  # type: ignore[arg-type]
        on_allowance_closed=lambda board_id, reason: closed.append((board_id, reason)),
    )
    setup.begin_plan("allowance-1", USER_INPUT, mode="setup")
    first = setup.board_setup("allowance-1", USER_INPUT)

    if closure == "cancel":
        setup.cancel(first.continuation_id)
    elif closure == "disconnect":
        setup.disconnect(USER_INPUT.connection_id)
    else:
        setup.revoke(USER_INPUT.board_id)

    assert setup.allowance_closed("allowance-1") is True
    assert closed and closed[-1][0] == USER_INPUT.board_id
    with pytest.raises(SetupWorkflowError, match="closed"):
        setup.board_fix_setup("allowance-1")


def test_three_replacement_plan_cycles_are_allowed_and_the_fourth_stops(
    tmp_path: Path,
) -> None:
    setup = workflow(tmp_path, lambda user_input: inventory())

    for index in range(3):
        setup.begin_plan(f"allowance-{index}", USER_INPUT, mode="setup")

    with pytest.raises(SetupWorkflowError, match="retry limit"):
        setup.begin_plan("allowance-3", USER_INPUT, mode="setup")


def test_third_workflow_attempt_is_impossible_until_a_replacement_plan_exists(
    tmp_path: Path,
) -> None:
    handlers = success_handlers()
    handlers[SetupPhase.CONNECTION] = lambda context: SetupPhaseOutcome.stop(  # type: ignore[assignment]
        "setup_connection_failed",
        "test/connect-still-failed",
        "Reconnect the intended board before another bounded attempt.",
    )
    setup = workflow(tmp_path, lambda user_input: inventory(), handlers=handlers)
    setup.begin_plan("allowance-1", USER_INPUT, mode="setup")

    first = setup.board_setup("allowance-1", USER_INPUT)
    second = setup.board_fix_setup("allowance-1")
    with pytest.raises(SetupWorkflowError, match="closed|exactly once"):
        setup.board_fix_setup("allowance-1")

    setup.begin_plan("allowance-2", USER_INPUT, mode="repair")
    third = setup.board_setup("allowance-2", USER_INPUT)

    assert first.status == "setup_connection_failed"
    assert second.status == "setup_connection_failed"
    assert third.status == "setup_connection_failed"
    assert third.attempt_id not in {first.attempt_id, second.attempt_id}


def test_known_unknown_incomplete_and_mismatch_name_routes_do_not_mutate_profiles() -> None:
    profiles = [
        ProfileRouteView("bench_board", "Bénch Board", "setup_completed"),
        ProfileRouteView("repair_board", "Repair Me", "setup_validation_failed"),
    ]
    original = list(profiles)

    known = route_board_name("Be\u0301nch Board", profiles)
    unknown = route_board_name("New Board", profiles)
    repair = route_board_name("Repair Me", profiles)
    mismatch = route_board_name("Bénch Board", profiles, hardware_mismatch=True)
    no_board = route_board_name("no board", profiles)

    assert (known.kind, known.board_id) == ("validate", "bench_board")
    assert unknown.kind == "setup"
    assert (repair.kind, repair.board_id) == ("validate", "repair_board")
    assert "Validate" in repair.agent_prompt
    assert mismatch.kind == "correct_assignment"
    assert "do not rewrite" in mismatch.agent_prompt.casefold()
    assert no_board.kind == "no_board"
    assert profiles == original
    for route in (known, unknown, repair, mismatch, no_board):
        assert NO_INTERNALS_RELAY_INSTRUCTION in route.agent_prompt
        assert route.agent_prompt.strip() == route.agent_prompt
        assert not route.agent_prompt.startswith(("{", "["))
        assert "continuation_id" not in route.agent_prompt


def test_run_assignments_are_one_to_one_and_mismatch_only_clears_memory() -> None:
    run = ServerRun(run_id="assignment-run")
    assignments = RunAssignmentStore(run.assignments)
    assignments.assign("probe:1", "board_a")

    with pytest.raises(SetupWorkflowError, match="another connection"):
        assignments.assign("probe:2", "board_a")
    with pytest.raises(SetupWorkflowError, match="another profile"):
        assignments.assign("probe:1", "board_b")

    route: AssignmentRoute = assignments.mismatch("probe:1", "board_a")
    assert route.kind == "correct_assignment"
    assert run.assignments == {}


def test_external_confirmation_callback_runs_only_after_explicit_confirmation(
    tmp_path: Path,
) -> None:
    external = SerialCandidate(
        "uart-x",
        "COM8",
        "External USB serial",
        "UART-X",
        0x10C4,
        0xEA60,
        external_adapter=True,
        provably_mapped=False,
    )
    current = PreflightInventory(
        probes=(ProbeCandidate("probe-a", "ST-Link board", "stlink", "PROBE-001"),),
        serial_ports=(external,),
        built_in_targets=("stm32l476rgtx",),
        exact_detected_targets=("stm32l476rgtx",),
    )
    confirmations: list[str] = []
    setup = SetupWorkflow(
        ReportWriter(FirmStore(tmp_path)),
        lambda user_input: current,
        phase_handlers=success_handlers(),  # type: ignore[arg-type]
        on_cache_confirmation=lambda user_input, decision: confirmations.append(
            decision.selected_serial.serial_id  # type: ignore[union-attr]
        ),
    )
    setup.begin_plan("allowance-1", USER_INPUT, mode="setup")

    waiting = setup.board_setup("allowance-1", USER_INPUT)
    fixed = setup.board_fix_setup(
        "allowance-1",
        selections=PreflightSelections(external_adapter_confirmed=True),
    )

    assert waiting.status == "setup_needs_user_input"
    assert fixed.status == "setup_completed"
    assert confirmations == ["uart-x"]
