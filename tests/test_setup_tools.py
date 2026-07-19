from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from collections.abc import Callable
from typing import Any, cast

import pytest

from pyocd_debug_mcp import server
from pyocd_debug_mcp.firmstore.profiles import ProfileRepository
from pyocd_debug_mcp.firmstore.reports import ReportWriter
from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.guardrails.permissions import PermissionStore
from pyocd_debug_mcp.guardrails.gate import GateManager
from pyocd_debug_mcp.guardrails.plan_engine import PlanEngine, PlanRefusal
from pyocd_debug_mcp.kernel.registry import ToolRegistry
from pyocd_debug_mcp.kernel.run_state import ServerRun
from pyocd_debug_mcp.setup_flow.preflight import (
    PreflightInventory,
    ProbeCandidate,
    SerialCandidate,
)
from pyocd_debug_mcp.setup_flow.setup import (
    PHASE_ORDER,
    RunAssignmentStore,
    SetupPhase,
    SetupPhaseOutcome,
    SetupWorkflow,
)
from pyocd_debug_mcp.setup_flow.validate import (
    BoardValidator,
    ValidationBackend,
    ValidationInventory,
    ValidationProbe,
    ValidationSerial,
)
from pyocd_debug_mcp.tools.setup import SetupToolLoadState, SetupToolServices, build_setup_handlers


PARAMETERS = {
    "mode": "setup",
    "connection_id": "probe:001",
    "display_name": "Bench Board",
    "mcu_part_number": "STM32L476RGT6-Exact",
    "requires_uart": True,
    "serial_baudrate": 115200,
    "serial_id": "UART-001",
    "datasheet_path": "board-datasheet.pdf",
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


def services(
    tmp_path: Path,
    *,
    assigned_connection: Callable[[str], str | None] | None = None,
    require_assignment: Callable[[str, str], None] | None = None,
):
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
        phase: (
            lambda _context, selected=phase: SetupPhaseOutcome.success(f"test/{selected.value}")
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
                "status": "internal_compatibility_alias_must_not_be_public",
                "board_id": board_id,
            },
            lambda board_id: {
                "status": "safety_refresh_completed",
                "board_id": board_id,
            },
            setup_overview=lambda names: {
                "status": "setup_routes_ready" if names else "setup_names_required",
                "agent_prompt": "Ask conversationally and do not expose JSON.",
                "routes": [
                    {"display_name": name, "board_id": "bench_board", "route": "validate"}
                    for name in (names or [])
                ],
            },
            setup_continue=lambda board_id, continuation_id, response: {
                "status": "setup_continuation_accepted",
                "board_id": board_id,
                "continuation_id": continuation_id,
                "response": dict(response),
            },
            require_assignment=require_assignment,
            assigned_connection=assigned_connection
            or (lambda board_id: "probe:probe-a" if board_id == "bench_board" else None),
        )
    )
    return run, registry, engine, loader, built


def test_a20_redirects_are_per_board_tool_and_server_run(tmp_path: Path) -> None:
    run, _, _, loader, handlers = services(tmp_path)

    initial = handlers["board_setup-plan"]()
    assert "first routing plan" in initial.lower()
    assert "board_validate" in initial

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


def test_setup_tool_index_descriptions_explain_trigger_and_routing(tmp_path: Path) -> None:
    _, _, _, _, handlers = services(tmp_path)

    setup_description = handlers["board_setup-plan"].__doc__ or ""
    assert "first before any other *-plan" in setup_description
    assert "matching YAML profile" in setup_description
    assert "board_validate" in setup_description

    assert "board_safety_setup" not in handlers

    refresh_description = handlers["board_safety_refresh"].__doc__ or ""
    assert "complete stable safety map" in refresh_description
    assert "ordinary firmware builds" in refresh_description
    assert "accepts no artifact" in refresh_description

    validate_description = handlers["board_validate"].__doc__ or ""
    assert "matching board YAML first" in validate_description
    assert "passing validation stamps" in validate_description

    overview_description = handlers["setup_overview"].__doc__ or ""
    assert "familiar board names" in overview_description
    assert "server-generated board_id" in overview_description

    continue_description = handlers["continue_setup"].__doc__ or ""
    assert "setup_research_required" in continue_description
    assert "grants no permission" in continue_description


def test_load_setup_tool_returns_distinct_bounded_next_step_guidance(tmp_path: Path) -> None:
    _, _, _, _, handlers = services(tmp_path)
    payloads = {
        tool_name: json.loads(handlers["load_setup_tool"]("bench_board", tool_name))
        for tool_name in (
            "board_setup-plan",
            "board_validate",
            "board_safety_refresh",
        )
    }

    assert {payload["next_call"]["tool"] for payload in payloads.values()} == set(payloads)
    assert len({payload["guidance"]["purpose"] for payload in payloads.values()}) == 3
    for tool_name, payload in payloads.items():
        guidance = payload["guidance"]
        assert payload["tool_name"] == tool_name
        assert {
            "purpose",
            "when_to_use",
            "when_not_to_use",
            "expected_statuses",
            "accepted_response_shape",
            "common_remedies",
            "relay_rule",
        } <= set(guidance)
        assert "do not expose" in guidance["relay_rule"].lower()
        assert len(json.dumps(guidance, ensure_ascii=False)) < 5000

    setup_call = payloads["board_setup-plan"]["next_call"]
    assert setup_call["tool"] == "board_setup-plan"
    assert setup_call["arguments"]
    assert set(setup_call["arguments"].values()) == {None}
    assert payloads["board_validate"]["next_call"] == {
        "tool": "board_validate",
        "arguments": {"board_id": "bench_board", "probe_id": "probe-a"},
    }
    assert payloads["board_safety_refresh"]["next_call"] == {
        "tool": "board_safety_refresh",
        "arguments": {"board_id": "bench_board"},
    }

    validation_use = payloads["board_validate"]["guidance"]["when_to_use"].lower()
    assert "no live proof" in validation_use
    assert "connection identity changes" in validation_use
    assert "hardware identity may have changed" in validation_use
    validation_nontriggers = payloads["board_validate"]["guidance"]["when_not_to_use"].lower()
    for nontrigger in (
        "build",
        "flash",
        "reset/halt",
        "uart",
        "safety refresh",
        "full map reconstruction",
        "bookkeeping",
    ):
        assert nontrigger in validation_nontriggers

    refresh_guidance = payloads["board_safety_refresh"]["guidance"]
    assert "missing, malformed, old, or inconsistent map" in refresh_guidance["when_to_use"]
    assert "no build artifacts or caller ranges" in refresh_guidance["when_not_to_use"]


def test_loaded_validation_call_copies_run_scoped_probe_and_executes(tmp_path: Path) -> None:
    assignment_checks: list[tuple[str, str]] = []
    _, _, _, _, handlers = services(
        tmp_path,
        assigned_connection=lambda board_id: (
            "probe:probe-a" if board_id == "bench_board" else None
        ),
        require_assignment=lambda board_id, connection_id: assignment_checks.append(
            (board_id, connection_id)
        ),
    )

    loaded = json.loads(handlers["load_setup_tool"]("bench_board", "board_validate"))

    assert loaded["next_call"] == {
        "tool": "board_validate",
        "arguments": {"board_id": "bench_board", "probe_id": "probe-a"},
    }
    result = json.loads(handlers["board_validate"](**loaded["next_call"]["arguments"]))
    assert result["status"] == "validation_incomplete"
    assert assignment_checks == [("bench_board", "probe:probe-a")]


def test_loading_validation_without_run_assignment_fails_closed(tmp_path: Path) -> None:
    _, _, _, loader, handlers = services(
        tmp_path,
        assigned_connection=lambda _board_id: None,
    )

    loaded = json.loads(handlers["load_setup_tool"]("bench_board", "board_validate"))

    assert loaded["status"] == "setup_assignment_required"
    assert "next_call" not in loaded
    assert "setup_overview" in loaded["agent_prompt"]
    assert not loader.is_loaded("bench_board", "board_validate")


def test_removed_safety_setup_cannot_be_loaded_or_called(tmp_path: Path) -> None:
    _, _, _, _, handlers = services(tmp_path)

    assert "board_safety_setup" not in handlers
    with pytest.raises(ValueError, match="tool_name must be one of"):
        handlers["load_setup_tool"]("bench_board", "board_safety_setup")


def test_v2_validation_and_refresh_handlers_have_minimal_public_schemas(tmp_path: Path) -> None:
    _, _, _, _, handlers = services(tmp_path)

    assert tuple(inspect.signature(handlers["board_validate"]).parameters) == (
        "board_id",
        "probe_id",
    )
    assert tuple(inspect.signature(handlers["board_safety_refresh"]).parameters) == ("board_id",)


def test_setup_overview_routes_names_without_user_facing_internal_fields(tmp_path: Path) -> None:
    _, _, _, _, handlers = services(tmp_path)

    payload = json.loads(handlers["setup_overview"](["Bench Board"]))

    assert payload["status"] == "setup_routes_ready"
    assert payload["routes"] == [
        {"display_name": "Bench Board", "board_id": "bench_board", "route": "validate"}
    ]
    assert "do not expose JSON" in payload["agent_prompt"]


@pytest.mark.parametrize(
    "board_names",
    ([], ["no board"], ["No Board"], ["  NO BOARD  "], ["Ｎｏ　Ｂｏａｒｄ"]),
)
def test_real_setup_overview_treats_no_board_as_literal_normalized_sentinel(
    monkeypatch: pytest.MonkeyPatch,
    board_names: list[str],
) -> None:
    monkeypatch.setattr(server._profile_repository, "load_all", lambda **_kwargs: ())
    monkeypatch.setattr(server, "_validation_inventory", lambda: ValidationInventory())

    payload = server._setup_overview(board_names)

    assert payload["status"] == "setup_no_board"
    assert payload["routes"] == []


def test_real_setup_overview_rejects_mixed_no_board_sentinel_without_a_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server._profile_repository, "load_all", lambda **_kwargs: ())
    monkeypatch.setattr(server, "_validation_inventory", lambda: ValidationInventory())

    payload = server._setup_overview(["left", "No Board"])

    assert payload["status"] == "setup_names_clarification_required"
    assert payload["routes"] == []
    assert "ask" in str(payload["agent_prompt"]).lower()
    assert "ordinary language" in str(payload["agent_prompt"]).lower()


def test_real_unknown_setup_route_supplies_exact_machine_call_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server._profile_repository, "load_all", lambda **_kwargs: ())
    monkeypatch.setattr(
        server,
        "_validation_inventory",
        lambda: ValidationInventory(
            probes=(ValidationProbe("probe-a", "J-Link", "jlink", "PROBE-001"),),
            serial_ports=(
                ValidationSerial(
                    "UART-001",
                    "COM11",
                    "J-Link CDC UART",
                    "UART-001",
                    0x1366,
                    0x1015,
                ),
            ),
        ),
    )

    payload = cast(dict[str, Any], server._setup_overview(["Brand New Board"]))
    route = payload["routes"][0]

    assert route["load_call"] == {
        "tool": "load_setup_tool",
        "arguments": {"board_id": route["board_id"], "tool_name": "board_setup-plan"},
    }
    assert route["plan_initialization_call"]["tool"] == "board_setup-plan"
    assert set(route["plan_initialization_call"]["arguments"].values()) == {None}
    template = route["plan_action_parameters_template"]
    assert template == {
        "mode": "setup",
        "connection_id": "probe:PROBE-001",
            "display_name": "Brand New Board",
            "mcu_part_number": None,
            "requires_uart": None,
            "serial_baudrate": None,
        "serial_id": "UART-001",
        "datasheet_path": None,
    }
    facts = " ".join(route["required_user_facts"]).lower()
    assert "baud" in facts
    assert "authorization" in facts
    assert "board type" not in facts
    assert "digest" not in facts and "sha" not in facts
    assert "known_board_types" not in payload
    assert "supported_reviewed_board_types" not in payload
    assert "serial_port" not in json.dumps(route)
    assert route["accepted_response"] is None
    assert payload["serial_choices"] == [
        {
            "choice_id": "UART-001",
            "friendly_name": payload["serial_choices"][0]["friendly_name"],
            "port_path": "COM11",
            "stable_usb_identity": "UART-001",
        }
    ]


def test_real_unknown_setup_route_maps_ambiguous_friendly_hardware_choices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assignments = RunAssignmentStore({})
    assignments.assign("probe:STALE", "stale_board")
    monkeypatch.setattr(server, "assignment_store", assignments)
    monkeypatch.setattr(server._profile_repository, "load_all", lambda **_kwargs: ())
    monkeypatch.setattr(
        server,
        "_validation_inventory",
        lambda: ValidationInventory(
            probes=(
                ValidationProbe("probe-a", "First", "jlink", "PROBE-A"),
                ValidationProbe("probe-b", "Second", "jlink", "PROBE-B"),
            ),
            serial_ports=(
                ValidationSerial("uart-a", "COM7", "First UART", "UART-A"),
                ValidationSerial("uart-b", "COM8", "Second UART", "UART-B"),
            ),
        ),
    )

    payload = cast(dict[str, Any], server._setup_overview(["Brand New Board"]))
    assert payload["status"] == "setup_assignment_required"
    assert payload["routes"] == []
    assert assignments.bindings() == {}

    selected = cast(
        dict[str, Any],
        server._setup_overview(
            ["Brand New Board"],
            {"Brand New Board": "probe:PROBE-B"},
        ),
    )
    assert selected["status"] == "setup_routes_ready"
    assert selected["routes"][0]["plan_action_parameters_template"]["connection_id"] == (
        "probe:PROBE-B"
    )
    assert assignments.bindings() == {"probe:PROBE-B": selected["routes"][0]["board_id"]}


def test_setup_overview_rejects_more_named_boards_than_visible_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server._profile_repository, "load_all", lambda **_kwargs: ())
    monkeypatch.setattr(
        server,
        "_validation_inventory",
        lambda: ValidationInventory(
            probes=(ValidationProbe("probe-a", "Only", "cmsis-dap", "PROBE-A"),),
        ),
    )

    payload = cast(dict[str, Any], server._setup_overview(["First", "Second"]))

    assert payload["status"] == "setup_assignment_clarification_required"
    assert payload["routes"] == []


def test_setup_overview_deduplicates_case_equivalent_probe_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server._profile_repository, "load_all", lambda **_kwargs: ())
    monkeypatch.setattr(
        server,
        "_validation_inventory",
        lambda: ValidationInventory(
            probes=(
                ValidationProbe("PROBE-A", "First source", "cmsis-dap", "PROBE-A"),
                ValidationProbe("probe-a", "Second source", "cmsis-dap", "probe-a"),
            ),
        ),
    )

    payload = cast(dict[str, Any], server._setup_overview(["First", "Second"]))

    assert len(payload["connections"]) == 1
    assert payload["status"] == "setup_assignment_clarification_required"
    assert payload["routes"] == []


def test_setup_continuation_accepts_one_exact_response_object(tmp_path: Path) -> None:
    _, _, _, _, handlers = services(tmp_path)

    payload = json.loads(
        handlers["continue_setup"]("bench_board", "continue-1", {"choice_id": "probe-a"})
    )

    assert payload == {
        "board_id": "bench_board",
        "continuation_id": "continue-1",
        "response": {"choice_id": "probe-a"},
        "status": "setup_continuation_accepted",
    }


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


@pytest.mark.parametrize("obsolete", ["board_type", "datasheet_sha256"])
def test_setup_plan_rejects_obsolete_public_evidence_fields(
    tmp_path: Path,
    obsolete: str,
) -> None:
    _, _, _, _, handlers = services(tmp_path)
    handlers["load_setup_tool"]("bench_board", "board_setup-plan")
    handlers["board_setup-plan"]()
    plan = populated_plan()
    action_parameters = dict(PARAMETERS)
    action_parameters[obsolete] = "obsolete-caller-value"
    plan["action_parameters"] = action_parameters

    with pytest.raises(PlanRefusal, match=rf"unknown=\['{obsolete}'\]"):
        handlers["board_setup-plan"](**plan)


def test_board_validate_redirect_then_structured_incomplete_report(tmp_path: Path) -> None:
    _, _, _, _, handlers = services(tmp_path)
    redirect = json.loads(handlers["board_validate"]("bench_board"))
    assert redirect["status"] == "setup_tool_not_loaded"

    handlers["load_setup_tool"]("bench_board", "board_validate")
    result = json.loads(handlers["board_validate"]("bench_board"))

    assert result["status"] == "validation_incomplete"
    assert result["continuation_id"].startswith("validation-")
    assert result["constraints"]


def test_safety_refresh_redirect_then_invokes_single_board_rebuild(tmp_path: Path) -> None:
    _, _, _, _, handlers = services(tmp_path)

    redirect = json.loads(handlers["board_safety_refresh"]("bench_board"))
    assert redirect["status"] == "setup_tool_not_loaded"
    assert redirect["tool_name"] == "board_safety_refresh"

    handlers["load_setup_tool"]("bench_board", "board_safety_refresh")
    result = json.loads(handlers["board_safety_refresh"]("bench_board"))
    assert result == {
        "board_id": "bench_board",
        "status": "safety_refresh_completed",
    }
    with pytest.raises(TypeError, match="unexpected keyword"):
        handlers["board_safety_refresh"]("bench_board", application_elf="firmware.elf")


def test_real_setup_overview_routes_recorded_mismatch_neutrally_to_new_logical_board(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profiles = ProfileRepository(FirmStore(tmp_path), legacy_board_dir=tmp_path / "legacy")
    profiles.commit_core(
        profiles.stage_core(
            {
                "board_id": "bench_board",
                "display_name": "Bench Board",
                "mcu_part_number": "STM32L476RGT6",
                "mcu_family": "stm32l4",
                "probe_family": "stlink",
                "pyocd_target": "stm32l476rgtx",
            }
        )
    )
    connection = SimpleNamespace(
        connection_id="connection-a",
        handle=SimpleNamespace(probe_uid="probe-a"),
    )
    gates = GateManager()
    gates.record_mismatch(
        board_id="bench_board",
        connection_id="connection-a",
        probe_identity="probe-a",
        expected_mcu="STM32L476RGT6",
        observed_mcu="STM32F407VGT6",
        validation_run="validation-mismatch",
    )
    monkeypatch.setattr(server, "_profile_repository", profiles)
    monkeypatch.setattr(server.connection_manager, "maybe_connection", lambda _board: connection)
    monkeypatch.setattr(server, "gate_manager", gates)
    monkeypatch.setattr(
        server,
        "_validation_inventory",
        lambda: ValidationInventory(
            probes=(ValidationProbe("probe-a", "ST-Link", "stlink", "probe-a"),)
        ),
    )

    assert server._setup_plan_eligibility("bench_board")[0] is True
    mismatch = cast(dict[str, Any], server._setup_overview(["Bench Board"]))["routes"][0]

    assert mismatch["route"] == "mismatch"
    assert mismatch["next_tool"] is None
    assert mismatch["expected_mcu"] == "STM32L476RGT6"
    assert mismatch["observed_mcu"] == "STM32F407VGT6"
    assert "ask what they want" in mismatch["reason"]
    assert "new logical" in mismatch["reason"]
    assert "load_call" not in mismatch

    adopted = cast(dict[str, Any], server._setup_overview(["Replacement Board"]))["routes"][0]
    assert adopted["route"] == "setup"
    assert adopted["board_id"] != "bench_board"
    assert server._setup_plan_eligibility(str(adopted["board_id"]))[0] is True

    gates.clear_mismatch("bench_board")
    eligible, reason = server._setup_plan_eligibility("bench_board")
    assert eligible is True
    assert "mode=repair" in reason

    malformed = profiles.store.layout.board_profile("malformed_board")
    malformed.parent.mkdir(parents=True, exist_ok=True)
    malformed.write_text("schema_version: 2\nboard_id: malformed_board\n", encoding="utf-8")
    eligible, reason = server._setup_plan_eligibility("malformed_board")
    assert eligible is False
    assert "malformed or incomplete" in reason


def test_setup_overview_does_not_apply_stale_mismatch_from_unselected_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profiles = ProfileRepository(FirmStore(tmp_path), legacy_board_dir=tmp_path / "legacy")
    profiles.commit_core(
        profiles.stage_core(
            {
                "board_id": "bench_board",
                "display_name": "Bench Board",
                "mcu_part_number": "STM32L476RGT6",
                "mcu_family": "stm32l4",
                "probe_family": "stlink",
                "pyocd_target": "stm32l476rgtx",
            }
        )
    )
    stale_connection = SimpleNamespace(
        connection_id="probe:PROBE-A",
        handle=SimpleNamespace(probe_uid="PROBE-A"),
    )
    gates = GateManager()
    gates.record_mismatch(
        board_id="bench_board",
        connection_id="probe:PROBE-A",
        probe_identity="PROBE-A",
        expected_mcu="STM32L476RGT6",
        observed_mcu="STM32F407VGT6",
        validation_run="validation-mismatch",
    )
    replacements: list[dict[str, str]] = []
    monkeypatch.setattr(server, "_profile_repository", profiles)
    monkeypatch.setattr(
        server.connection_manager,
        "maybe_connection",
        lambda _board: stale_connection,
    )
    monkeypatch.setattr(server, "gate_manager", gates)
    monkeypatch.setattr(
        server,
        "_replace_setup_assignments",
        lambda bindings, _reason: replacements.append(dict(bindings)),
    )
    monkeypatch.setattr(
        server,
        "_validation_inventory",
        lambda: ValidationInventory(
            probes=(
                ValidationProbe("probe-a", "First", "stlink", "PROBE-A"),
                ValidationProbe("probe-b", "Second", "stlink", "PROBE-B"),
            ),
        ),
    )

    payload = cast(
        dict[str, Any],
        server._setup_overview(["Bench Board"], {"Bench Board": "probe:PROBE-B"}),
    )

    assert payload["routes"][0]["route"] == "repair"
    assert payload["routes"][0]["plan_action_parameters_template"]["connection_id"] == (
        "probe:PROBE-B"
    )
    assert replacements == [{"probe:PROBE-B": "bench_board"}]


def test_real_setup_overview_routes_parseable_incomplete_profile_to_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assignment_store = RunAssignmentStore({})
    monkeypatch.setattr(server, "assignment_store", assignment_store)
    profiles = ProfileRepository(FirmStore(tmp_path), legacy_board_dir=tmp_path / "legacy")
    profiles.commit_core(
        profiles.stage_core(
            {
                "board_id": "repair_board",
                "display_name": "Repair Board",
                "mcu_part_number": "nRF52840-QIAA",
                "mcu_family": "nrf52840",
                "probe_family": "jlink",
                "pyocd_target": "nrf52840",
            }
        )
    )
    monkeypatch.setattr(server, "_profile_repository", profiles)
    monkeypatch.setattr(
        server,
        "_validation_inventory",
        lambda: ValidationInventory(
            probes=(ValidationProbe("probe-a", "CMSIS-DAP Probe", "cmsisdap", "probe-a"),)
        ),
    )
    monkeypatch.setattr(server.connection_manager, "maybe_connection", lambda _board: None)

    route = cast(dict[str, Any], server._setup_overview(["Repair Board"]))["routes"][0]

    assert route["route"] == "repair"
    assert route["next_tool"] == "board_setup-plan"
    template = cast(dict[str, object], route["plan_action_parameters_template"])
    assert template["mode"] == "repair"
    assert template["mcu_part_number"] == "nRF52840-QIAA"
    assert template["connection_id"] == "probe:probe-a"
    assert template["datasheet_path"] is None
    assignment_store.require("probe:probe-a", "repair_board")


def test_noncanonical_safety_reference_routes_to_repair_not_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assignments = RunAssignmentStore({})
    profiles = ProfileRepository(FirmStore(tmp_path), legacy_board_dir=tmp_path / "legacy")
    profiles.commit_core(
        profiles.stage_core(
            {
                "board_id": "wrong_ref_board",
                "display_name": "Wrong Ref Board",
                "mcu_part_number": "nRF52840-QIAA",
                "mcu_family": "nrf52840",
                "probe_family": "jlink",
                "pyocd_target": "nrf52840",
            }
        )
    )
    profiles.commit_optional(
        profiles.stage_optional("wrong_ref_board", {"datasheet_sha256": "a" * 64})
    )
    wrong_ref = (
        profiles.store.layout.safety_reference_prefix("wrong_ref_board") / "other.yaml"
    ).as_posix()
    profiles.commit_safety_ref(profiles.stage_safety_ref("wrong_ref_board", wrong_ref))
    monkeypatch.setattr(server, "assignment_store", assignments)
    monkeypatch.setattr(server, "_profile_repository", profiles)
    monkeypatch.setattr(
        server,
        "_validation_inventory",
        lambda: ValidationInventory(
            probes=(ValidationProbe("probe-a", "J-Link Probe", "jlink", "probe-a"),)
        ),
    )
    monkeypatch.setattr(server.connection_manager, "maybe_connection", lambda _board: None)

    overview = cast(dict[str, Any], server._setup_overview(["Wrong Ref Board"]))

    assert overview["routes"][0]["route"] == "repair"
    assert overview["routes"][0]["next_tool"] == "board_setup-plan"
    assert server._setup_plan_eligibility("wrong_ref_board")[0] is True
