from __future__ import annotations

import json
from pathlib import Path

from pyocd_debug_mcp.firmstore.profiles import ProfileRepository
from pyocd_debug_mcp.firmstore.reports import ReportWriter
from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.guardrails.permissions import PermissionStore
from pyocd_debug_mcp.guardrails.plan_engine import PlanEngine
from pyocd_debug_mcp.kernel.registry import ToolRegistry
from pyocd_debug_mcp.kernel.run_state import ServerRun
from pyocd_debug_mcp.setup_flow.preflight import (
    PreflightInventory,
    ProbeCandidate,
    SerialCandidate,
)
from pyocd_debug_mcp.setup_flow.setup import PHASE_ORDER, SetupPhase, SetupPhaseOutcome, SetupWorkflow
from pyocd_debug_mcp.setup_flow.validate import BoardValidator, ValidationBackend, ValidationInventory
from pyocd_debug_mcp.tools.setup import SetupToolLoadState, SetupToolServices, build_setup_handlers


PARAMETERS = {
    "mode": "setup",
    "connection_id": "probe:001",
    "display_name": "Bench Board",
    "mcu_part_number": "STM32L476RGT6-Exact",
    "serial_baudrate": 115200,
}


def populated_plan() -> dict[str, object]:
    return {
        "board_id": "bench_board",
        "hypothesis": "The selected hardware can complete deterministic setup.",
        "hypothesis_made": True,
        "strategy": "Run one setup attempt and retain the paired repair only if needed.",
        "strategy_evaluated": True,
        "expected_fail_return": "A structured continuation with a bounded remedy.",
        "expected_success_return": "A completed setup report and validation redirect.",
        "max_calls": 1,
        "max_calls_buffer": 0,
        "user_permission": "one-time",
        "action_parameters": PARAMETERS,
    }


def services(tmp_path: Path):
    run = ServerRun(run_id="setup-tools-run")
    registry = ToolRegistry()
    for name in ("board_setup", "board_fix_setup"):
        registry.register(name, hidden=True, locked=True, prerequisite="board_setup-plan")
    permissions = PermissionStore(run)
    engine = PlanEngine(run, registry, permission_provider=permissions)
    permissions.set_revocation_handler(engine.invalidate)
    reports = ReportWriter(FirmStore(tmp_path))
    inventory = PreflightInventory(
        probes=(ProbeCandidate("probe-a", "ST-Link", "stlink", "P1"),),
        serial_ports=(SerialCandidate("serial-a", "COM1", "UART", "U1", 1, 2),),
        built_in_targets=("stm32l476rgtx",),
        exact_detected_targets=("stm32l476rgtx",),
    )
    handlers = {
        phase: (lambda _context, selected=phase: SetupPhaseOutcome.success(f"test/{selected.value}"))
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
    workflow = SetupWorkflow(reports, lambda _input: inventory, phase_handlers=handlers)
    legacy = tmp_path / "boards"
    legacy.mkdir()
    validator = BoardValidator(
        ProfileRepository(FirmStore(tmp_path), legacy_board_dir=legacy),
        reports,
        ValidationBackend(
            lambda: ValidationInventory(),
            lambda _target: False,
            lambda _profile, _probe, _timeout: object(),
            lambda _connection, _address, _width, _timeout: 0,
            lambda _serial, _baud, _duration, _bytes: "",
            lambda _connection: None,
        ),
    )
    loader = SetupToolLoadState(run)
    built = build_setup_handlers(
        SetupToolServices(
            loader,
            engine,
            workflow,
            validator,
            lambda board_id: {
                "status": "safety_setup_completed",
                "board_id": board_id,
            },
            lambda board_id: {
                "status": "safety_refresh_completed",
                "board_id": board_id,
            },
        )
    )
    return run, registry, engine, loader, built


def test_a20_redirects_are_per_board_tool_and_server_run(tmp_path: Path) -> None:
    run, _, _, loader, handlers = services(tmp_path)

    initial = json.loads(handlers["board_setup-plan"]())
    assert initial["status"] == "setup_tool_not_loaded"

    loaded = json.loads(handlers["load_setup_tool"]("bench_board", "board_setup-plan"))
    assert loaded["status"] == "setup_tool_loaded"
    assert "purpose:" in handlers["board_setup-plan"]().lower()

    other_board = json.loads(
        handlers["board_setup-plan"](**(populated_plan() | {"board_id": "other_board"}))
    )
    assert other_board["status"] == "setup_tool_not_loaded"
    assert not loader.is_loaded("bench_board", "board_validate")

    fresh = SetupToolLoadState(ServerRun(run_id=run.run_id + "-restart"))
    assert not fresh.is_loaded("bench_board", "board_setup-plan")


def test_setup_action_returns_complete_structured_continuation_payload(tmp_path: Path) -> None:
    _, registry, engine, _, handlers = services(tmp_path)
    handlers["load_setup_tool"]("bench_board", "board_setup-plan")
    handlers["board_setup-plan"]()
    response = handlers["board_setup-plan"](**populated_plan())
    assert "Accepted plan plan-" in response

    engine.enforce("board_setup", "bench_board", PARAMETERS, session_id=None)
    payload = json.loads(handlers["board_setup"]("bench_board", **PARAMETERS))

    assert payload["status"] == "setup_completed"
    assert payload["continuation_id"].startswith("setup-continuation-")
    assert {
        "agent_prompt",
        "choices",
        "observed",
        "constraints",
        "rejected_candidates",
        "accepted_response",
        "validation_plan",
    }.issubset(payload)
    assert registry.is_unlocked("board_setup", "bench_board") is False


def test_board_validate_redirect_then_structured_incomplete_report(tmp_path: Path) -> None:
    _, _, _, _, handlers = services(tmp_path)
    redirect = json.loads(handlers["board_validate"]("bench_board"))
    assert redirect["status"] == "setup_tool_not_loaded"

    handlers["load_setup_tool"]("bench_board", "board_validate")
    result = json.loads(handlers["board_validate"]("bench_board"))

    assert result["status"] == "validation_incomplete"
    assert result["continuation_id"].startswith("validation-")
    assert result["constraints"]


def test_a20_safety_tools_redirect_then_invoke_their_scoped_engines(tmp_path: Path) -> None:
    _, _, _, _, handlers = services(tmp_path)

    for tool_name, expected_status in (
        ("board_safety_setup", "safety_setup_completed"),
        ("board_safety_refresh", "safety_refresh_completed"),
    ):
        redirect = json.loads(handlers[tool_name]("bench_board"))
        assert redirect["status"] == "setup_tool_not_loaded"
        assert redirect["tool_name"] == tool_name

        handlers["load_setup_tool"]("bench_board", tool_name)
        result = json.loads(handlers[tool_name]("bench_board"))
        assert result == {
            "board_id": "bench_board",
            "status": expected_status,
        }
