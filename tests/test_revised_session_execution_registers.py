from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pytest

from pyocd_debug_mcp import server
from pyocd_debug_mcp.adapters import swd_pyocd
from pyocd_debug_mcp.guardrails.plan_defs import (
    BudgetMode,
    PermissionMode,
    PLAN_DEFINITIONS,
)
from pyocd_debug_mcp.guardrails.plan_engine import PlanEngine, PlanStatus
from pyocd_debug_mcp.kernel.operations import SAFE_EXIT_REMINDER
from pyocd_debug_mcp.kernel.registry import ToolRegistry
from pyocd_debug_mcp.kernel.run_state import ServerRun
from pyocd_debug_mcp.target_errors import ResetLineUnavailableError
from pyocd_debug_mcp.tools.execution import ExecutionToolServices, build_execution_handlers
from pyocd_debug_mcp.tools.registers import (
    RegisterPreconditionError,
    RegisterToolServices,
    build_register_handlers,
    validate_guarded_register_call,
)
from pyocd_debug_mcp.tools.session import SessionToolServices, build_session_handlers


def test_task7_runtime_surface_replaces_legacy_names_and_preserves_guarded_defaults() -> None:
    advertised = set(server.tool_registry.advertised())
    registered = {tool.name for tool in server.mcp._tool_manager.list_tools()}
    always = {
        "connect",
        "disconnect",
        "get_board_info",
        "get_state",
        "halt",
        "resume",
        "step",
        "reset_and_run",
        "read_cpu_register",
        "read_execution_state",
    }
    guarded = set(server.TASK7_GUARDED_ACTIONS)

    assert always <= advertised
    assert guarded.isdisjoint(advertised)
    assert guarded <= registered
    assert {f"{name}-plan" for name in guarded} <= advertised
    assert {"reset", "read_core_register", "write_core_register"}.isdisjoint(registered)


def test_task7_plan_budget_and_permission_metadata_matches_spec() -> None:
    flexible = {"connect_override", "reset_and_halt", "connect_under_reset"}
    fixed = {"write_cpu_register", "set_execution_state", "register_write"}

    assert all(
        PLAN_DEFINITIONS[name].budget_mode is BudgetMode.FLEXIBLE for name in flexible
    )
    assert all(PLAN_DEFINITIONS[name].budget_mode is BudgetMode.FIXED for name in fixed)
    assert PLAN_DEFINITIONS["set_execution_state"].permission_mode is PermissionMode.REQUIRED
    for name in (flexible | fixed) - {"set_execution_state"}:
        assert PLAN_DEFINITIONS[name].permission_mode is PermissionMode.NONE


def test_task7_runtime_schemas_match_the_spec_table() -> None:
    expected_action_fields = {
        "connect": {"board_id"},
        "disconnect": {"board_id"},
        "get_board_info": {"board_id"},
        "get_state": {"board_id"},
        "connect_override": {
            "board_id",
            "probe_uid",
            "target_override",
            "external_board_config",
        },
        "halt": {"board_id"},
        "resume": {"board_id"},
        "step": {"board_id"},
        "reset_and_run": {"board_id"},
        "reset_and_halt": {"board_id"},
        "connect_under_reset": {"board_id", "probe_uid", "target_override"},
        "read_cpu_register": {"board_id", "name"},
        "read_execution_state": {"board_id", "name"},
        "write_cpu_register": {"board_id", "name", "value"},
        "set_execution_state": {"board_id", "name", "value"},
        "register_write": {"board_id", "address", "mask", "value"},
    }
    tools = {tool.name: tool for tool in server.mcp._tool_manager.list_tools()}

    for name, expected in expected_action_fields.items():
        schema = tools[name].parameters
        assert set(schema["properties"]) == expected, name
        assert "board_id" in schema["required"], name

    common_plan_fields = {
        "board_id",
        "hypothesis",
        "hypothesis_made",
        "strategy",
        "strategy_evaluated",
        "expected_fail_return",
        "expected_success_return",
        "max_calls",
        "max_calls_buffer",
    }
    for action_name in server.TASK7_GUARDED_ACTIONS:
        expected = common_plan_fields | {"action_parameters", "user_permission"}
        plan_schema = tools[f"{action_name}-plan"].parameters
        assert set(plan_schema["properties"]) == expected, action_name
        assert plan_schema.get("required", []) == [], action_name


def test_register_write_fixed_plan_unlocks_then_relocks_at_exhaustion() -> None:
    registry = ToolRegistry()
    registry.register(
        "register_write",
        hidden=True,
        locked=True,
        prerequisite="register_write-plan",
    )
    engine = PlanEngine(ServerRun(run_id="task7-checkpoint"), registry)
    engine.null_response("register_write-plan")
    fields: dict[str, object] = {
        "board_id": "board_a",
        "hypothesis": "The documented peripheral field controls the observed behavior.",
        "hypothesis_made": True,
        "strategy": "Apply one masked write and inspect the documented result.",
        "strategy_evaluated": True,
        "expected_fail_return": "A deterministic register policy refusal.",
        "expected_success_return": "One masked peripheral write.",
        "max_calls": 1,
        "max_calls_buffer": 0,
        "action_parameters": {
            "address": "0x40000000",
            "mask": "0xff",
            "value": "18",
        },
    }

    accepted = engine.submit("register_write-plan", fields, session_id="session-a")
    assert accepted.plan is not None
    assert registry.is_unlocked("register_write", "board_a") is True
    exhausted = engine.enforce(
        "register_write",
        "board_a",
        {"address": "0x40000000", "mask": "0xff", "value": "18"},
        session_id="session-a",
    )
    assert exhausted.status is PlanStatus.EXHAUSTED
    assert registry.is_unlocked("register_write", "board_a") is False


def test_session_override_forwards_manual_values_without_profile_writes() -> None:
    calls: list[dict[str, object]] = []

    def connect(board_id: str, **values: object) -> str:
        calls.append({"board_id": board_id, **values})
        return "connected"

    handlers = build_session_handlers(
        SessionToolServices(
            connect=connect,
            connect_override=connect,
            disconnect=lambda board: f"disconnected:{board}",
            get_board_info=lambda board: f"info:{board}",
            get_state=lambda board: f"state:{board}",
        )
    )

    result = handlers["connect_override"](
        "board_a",
        probe_uid="probe-7",
        target_override="stm32l476rgtx",
        external_board_config="external.yaml",
    )

    assert calls == [
        {
            "board_id": "board_a",
            "unique_id": "probe-7",
            "target": "stm32l476rgtx",
            "board_config": "external.yaml",
        }
    ]
    assert "did not rewrite a profile" in result
    assert SAFE_EXIT_REMINDER in result


def test_every_session_result_has_safe_exit_and_routes_the_named_board() -> None:
    calls: list[tuple[str, str]] = []

    def result(action: str, board_id: str) -> str:
        calls.append((action, board_id))
        return action

    handlers = build_session_handlers(
        SessionToolServices(
            connect=lambda board: result("connect", board),
            connect_override=lambda board, **values: result("connect_override", board),
            disconnect=lambda board: result("disconnect", board),
            get_board_info=lambda board: result("get_board_info", board),
            get_state=lambda board: result("get_state", board),
        )
    )

    assert SAFE_EXIT_REMINDER in handlers["connect"]("board_b")
    for name in ("disconnect", "get_board_info", "get_state"):
        assert SAFE_EXIT_REMINDER in handlers[name]("board_b")
    assert calls == [
        ("connect", "board_b"),
        ("disconnect", "board_b"),
        ("get_board_info", "board_b"),
        ("get_state", "board_b"),
    ]


def test_execution_matrix_and_reset_never_unlocks() -> None:
    calls: list[tuple[str, object]] = []
    target_locked = True

    def reset(board_id: str, halt_after: bool) -> str:
        calls.append(("reset", (board_id, halt_after)))
        assert target_locked is True
        return "reset complete"

    handlers = build_execution_handlers(
        ExecutionToolServices(
            halt=lambda board: calls.append(("halt", board)) or "halted",
            resume=lambda board: calls.append(("resume", board)) or "resumed",
            step=lambda board: calls.append(("step", board)) or "pc=0x08000100",
            reset=reset,
            connect_under_reset=lambda board, probe, target: (
                calls.append(("connect-under-reset", (board, probe, target)))
                or "attached and halted"
            ),
        )
    )

    for name in ("halt", "resume", "step", "reset_and_run", "reset_and_halt"):
        assert SAFE_EXIT_REMINDER in handlers[name]("board_a")
        assert target_locked is True
    result = handlers["connect_under_reset"]("board_a", "probe-7", "nrf52833")

    assert SAFE_EXIT_REMINDER in result
    assert ("reset", ("board_a", False)) in calls
    assert ("reset", ("board_a", True)) in calls
    assert ("connect-under-reset", ("board_a", "probe-7", "nrf52833")) in calls
    assert target_locked is True


def test_execution_actions_produce_expected_state_transitions() -> None:
    states = {"board_b": "running"}

    def set_state(board_id: str, state: str) -> str:
        states[board_id] = state
        return state

    handlers = build_execution_handlers(
        ExecutionToolServices(
            halt=lambda board: set_state(board, "halted"),
            resume=lambda board: set_state(board, "running"),
            step=lambda board: set_state(board, "halted"),
            reset=lambda board, halt_after: set_state(
                board, "halted" if halt_after else "running"
            ),
            connect_under_reset=lambda board, probe, target: set_state(board, "halted"),
        )
    )

    handlers["halt"]("board_b")
    assert states["board_b"] == "halted"
    handlers["resume"]("board_b")
    assert states["board_b"] == "running"
    handlers["step"]("board_b")
    assert states["board_b"] == "halted"
    handlers["reset_and_run"]("board_b")
    assert states["board_b"] == "running"
    handlers["reset_and_halt"]("board_b")
    assert states["board_b"] == "halted"
    handlers["connect_under_reset"]("board_b", None, None)
    assert states["board_b"] == "halted"


@dataclass
class FakeProbe:
    calls: list[object]
    unique_id: str = "probe-7"

    def assert_reset(self, asserted: bool) -> None:
        self.calls.append(("reset-line", asserted))


class FakeTarget:
    def __init__(self, calls: list[object]) -> None:
        self.calls = calls
        self.core_registers = type(
            "CoreRegisters",
            (),
            {"by_name": {"PC": object(), "r0": object(), "CONTROL": object()}},
        )()

    def halt(self) -> None:
        self.calls.append("halt")

    def get_state(self) -> object:
        self.calls.append("get-state")
        return type("State", (), {"name": "HALTED"})()


class FakeSession:
    def __init__(self, calls: list[object], probe: object) -> None:
        self.calls = calls
        self.probe = probe
        self.target = FakeTarget(calls)

    def open(self) -> None:
        assert_reset = getattr(self.probe, "assert_reset")
        assert_reset(True)
        self.calls.append("open")
        self.target.halt()
        assert_reset(False)

    def close(self) -> None:
        self.calls.append("close")


def test_swd_connect_under_reset_orders_line_attach_halt_release(monkeypatch) -> None:
    calls: list[object] = []
    session = FakeSession(calls, FakeProbe(calls))
    adapter = swd_pyocd.PyOCDSWDInterface()
    monkeypatch.setattr(adapter, "_choose_session", lambda **kwargs: session)
    monkeypatch.setattr(swd_pyocd, "discover_local_packs", lambda: [])

    handle = adapter.connect_under_reset(
        board=None,
        unique_id="probe-7",
        target="nrf52833",
    )

    assert calls == [
        ("reset-line", True),
        "open",
        "halt",
        ("reset-line", False),
        "halt",
        "get-state",
    ]
    assert handle.session is session
    assert handle.probe_uid == "probe-7"
    assert adapter.supported_core_registers(handle) == ("control", "pc", "r0")


def test_swd_connect_under_reset_fails_when_probe_has_no_reset_line(monkeypatch) -> None:
    calls: list[object] = []
    probe = type("NoResetProbe", (), {"unique_id": "probe-7"})()
    session = FakeSession(calls, probe)
    adapter = swd_pyocd.PyOCDSWDInterface()
    monkeypatch.setattr(adapter, "_choose_session", lambda **kwargs: session)
    monkeypatch.setattr(swd_pyocd, "discover_local_packs", lambda: [])

    with pytest.raises(ResetLineUnavailableError, match="does not expose wired reset-line"):
        adapter.connect_under_reset(board=None, unique_id="probe-7", target="nrf52833")

    assert calls == ["close"]


def test_swd_connect_under_reset_maps_unimplemented_reset_control(monkeypatch) -> None:
    calls: list[object] = []

    class UnsupportedResetProbe:
        unique_id = "probe-7"

        def assert_reset(self, asserted: bool) -> None:
            raise NotImplementedError

    session = FakeSession(calls, UnsupportedResetProbe())
    adapter = swd_pyocd.PyOCDSWDInterface()
    monkeypatch.setattr(adapter, "_choose_session", lambda **kwargs: session)
    monkeypatch.setattr(swd_pyocd, "discover_local_packs", lambda: [])

    with pytest.raises(ResetLineUnavailableError, match="does not support wired reset-line"):
        adapter.connect_under_reset(board=None, unique_id="probe-7", target="nrf52833")

    assert calls == ["close"]


def register_handlers() -> tuple[dict[str, Callable[..., str]], list[tuple[str, object]]]:
    calls: list[tuple[str, object]] = []
    supported = (
        "r0",
        "r12",
        "s0",
        "d3",
        "q1",
        "pc",
        "msp",
        "control",
        "msp_ns",
    )
    handlers = build_register_handlers(
        RegisterToolServices(
            supported_registers=lambda board: supported,
            read_register=lambda board, name: (
                calls.append(("read", (board, name))) or f"{name}=0x1"
            ),
            write_register=lambda board, name, value: (
                calls.append(("write", (board, name, value))) or "written"
            ),
            masked_register_write=lambda board, address, mask, value: (
                calls.append(("register-write", (board, address, mask, value))) or "masked"
            ),
        )
    )
    return handlers, calls


@pytest.mark.parametrize(
    ("tool_name", "register_name"),
    [
        ("read_cpu_register", "r0"),
        ("read_cpu_register", "S0"),
        ("write_cpu_register", "r12"),
        ("write_cpu_register", "D3"),
        ("read_execution_state", "pc"),
        ("read_execution_state", "CONTROL"),
        ("set_execution_state", "msp"),
    ],
)
def test_supported_register_class_matrix_accepts_exact_runtime_registers(
    tool_name: str,
    register_name: str,
) -> None:
    handlers, calls = register_handlers()

    if tool_name in {"write_cpu_register", "set_execution_state"}:
        result = handlers[tool_name]("board_a", register_name, "0x20")  # type: ignore[operator]
    else:
        result = handlers[tool_name]("board_a", register_name)  # type: ignore[operator]

    assert "Refused" not in result
    assert SAFE_EXIT_REMINDER in result
    assert calls


@pytest.mark.parametrize(
    ("tool_name", "register_name", "code"),
    [
        ("write_cpu_register", "pc", "register/wrong-class"),
        ("set_execution_state", "r0", "register/wrong-class"),
        ("write_cpu_register", "r11", "register/unsupported"),
        ("set_execution_state", "msp_ns", "register/prohibited"),
        ("write_cpu_register", "secure_key", "register/prohibited"),
    ],
)
def test_register_matrix_rejects_wrong_unsupported_and_security_classes(
    tool_name: str,
    register_name: str,
    code: str,
) -> None:
    handlers, calls = register_handlers()

    result = handlers[tool_name]("board_a", register_name, 1)  # type: ignore[operator]

    assert f"Refused [{code}]" in result
    assert SAFE_EXIT_REMINDER in result
    assert calls == []


def test_floating_point_register_accepts_a_runtime_supported_wide_value() -> None:
    handlers, calls = register_handlers()

    result = handlers["write_cpu_register"]("board_a", "d3", "0x100000000")  # type: ignore[operator]

    assert "Refused" not in result
    assert calls == [("write", ("board_a", "d3", 0x100000000))]


@pytest.mark.parametrize(("value", "expected"), [("32", 32), ("0x20", 0x20)])
def test_cpu_register_values_accept_decimal_and_hexadecimal(
    value: str,
    expected: int,
) -> None:
    handlers, calls = register_handlers()

    result = handlers["write_cpu_register"]("board_b", "r0", value)  # type: ignore[operator]

    assert "Refused" not in result
    assert calls == [("write", ("board_b", "r0", expected))]


@pytest.mark.parametrize(
    ("tool_name", "register_name", "value"),
    [
        ("write_cpu_register", "r0", "0x100000000"),
        ("write_cpu_register", "s0", str(1 << 32)),
        ("write_cpu_register", "d3", hex(1 << 64)),
        ("write_cpu_register", "q1", str(1 << 128)),
        ("set_execution_state", "pc", "0x100000000"),
    ],
)
def test_register_width_overflow_is_refused_before_backend(
    tool_name: str, register_name: str, value: str
) -> None:
    handlers, calls = register_handlers()

    result = handlers[tool_name]("board_a", register_name, value)  # type: ignore[operator]

    assert "Refused [register/invalid-value]" in result
    assert calls == []


@pytest.mark.parametrize(
    ("register_name", "value"),
    [
        ("r0", (1 << 32) - 1),
        ("s0", (1 << 32) - 1),
        ("d3", (1 << 64) - 1),
        ("q1", (1 << 128) - 1),
    ],
)
def test_register_width_maximum_is_accepted(register_name: str, value: int) -> None:
    handlers, calls = register_handlers()

    result = handlers["write_cpu_register"]("board_a", register_name, value)  # type: ignore[operator]

    assert "Refused" not in result
    assert calls == [("write", ("board_a", register_name, value))]


@pytest.mark.parametrize(
    ("address", "mask", "value"),
    [
        ("1073741824", "255", "18"),
        ("0x40000000", "0xff", "0x12"),
    ],
)
def test_register_write_accepts_decimal_and_hexadecimal(
    address: str,
    mask: str,
    value: str,
) -> None:
    handlers, calls = register_handlers()

    result = handlers["register_write"]("board_b", address, mask, value)  # type: ignore[operator]

    assert "Safety map" in result
    assert calls == [("register-write", ("board_b", 0x40000000, 0xFF, 0x12))]


def test_register_write_uses_fixed_plan_and_safety_map_policy() -> None:
    handlers, calls = register_handlers()

    result = handlers["register_write"]("board_a", "0x40000000", "0xff", "0x12")  # type: ignore[operator]

    assert calls == [("register-write", ("board_a", 0x40000000, 0xFF, 0x12))]
    assert "Safety map" in result
    assert SAFE_EXIT_REMINDER in result

    calls.clear()
    assert "Refused [register/unaligned]" in handlers["register_write"](  # type: ignore[operator]
        "board_a", "0x40000002", "0xff", "0x12"
    )
    assert "Refused [register/empty-mask]" in handlers["register_write"](  # type: ignore[operator]
        "board_a", "0x40000000", 0, "0x12"
    )
    assert calls == []


def test_guarded_register_preconditions_reject_before_execution() -> None:
    services = RegisterToolServices(
        supported_registers=lambda board: ("r0", "pc"),
        read_register=lambda board, name: "unreachable",
        write_register=lambda board, name, value: "unreachable",
        masked_register_write=lambda board, address, mask, value: "unreachable",
    )

    with pytest.raises(RegisterPreconditionError, match="wrong-class"):
        validate_guarded_register_call(
            services,
            "write_cpu_register",
            "board_a",
            {"name": "pc", "value": "0x10"},
        )
    with pytest.raises(RegisterPreconditionError, match="empty-mask"):
        validate_guarded_register_call(
            services,
            "register_write",
            "board_a",
            {"address": "0x40000000", "mask": 0, "value": 1},
        )
