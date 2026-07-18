from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from pyocd_debug_mcp.guardrails.plan_defs import PermissionMode, PlanDefinition
from pyocd_debug_mcp.guardrails.plan_engine import (
    PlanEngine,
    PlanRefusal,
    PlanStatus,
)
from pyocd_debug_mcp.kernel.registry import ToolRegistry
from pyocd_debug_mcp.kernel.run_state import ServerRun
from pyocd_debug_mcp.services.session_runtime import PolicyRefusal


SESSION = "session-a"


def common_fields(board_id: str = "board_a", *, max_calls: int = 2) -> dict[str, object]:
    return {
        "board_id": board_id,
        "hypothesis": "The observed state will change in the predicted way.",
        "hypothesis_made": True,
        "strategy": "Use one bounded action and compare its documented result.",
        "strategy_evaluated": True,
        "expected_fail_return": "A deterministic policy or backend failure.",
        "expected_success_return": "The requested bounded operation result.",
        "max_calls": max_calls,
        "max_calls_buffer": 0,
    }


def read_serial_fields(
    *,
    board_id: str = "board_a",
    expected_text: str | None = "boot ok",
    max_calls: int = 2,
) -> dict[str, object]:
    return common_fields(board_id, max_calls=max_calls) | {
        "action_parameters": read_serial_parameters(expected_text),
    }


def read_serial_parameters(expected_text: str | None = "boot ok") -> dict[str, object]:
    return {
        "expected_text": expected_text,
        "read_seconds": 3.0,
        "baudrate": 115200,
        "port": "COM7",
        "reset_on_open": False,
    }


def write_memory_fields(value: object = 1) -> dict[str, object]:
    return common_fields(max_calls=1) | {
        "action_parameters": write_memory_parameters(value),
    }


def mutate_plan(
    fields: dict[str, object],
    mutation: dict[str, object],
) -> dict[str, object]:
    updated = dict(fields)
    raw_parameters = updated["action_parameters"]
    assert isinstance(raw_parameters, dict)
    parameters: dict[str, object] = dict(raw_parameters)
    for name, value in mutation.items():
        if name in parameters:
            parameters[name] = value
        else:
            updated[name] = value
    updated["action_parameters"] = parameters
    return updated


def write_memory_parameters(value: object = 1) -> dict[str, object]:
    return {
        "symbol_or_address": "counter",
        "value": value,
        "width": 32,
        "allow_address_fallback": False,
        "reason": None,
    }


def engine_for(
    action_name: str = "read_serial",
    *,
    server_run: ServerRun | None = None,
    permission_provider=None,
    scope_validator=None,
    registry: ToolRegistry | None = None,
) -> tuple[PlanEngine, ToolRegistry]:
    registry = registry or ToolRegistry()
    registry.register(
        action_name,
        hidden=True,
        locked=True,
        prerequisite=f"{action_name}-plan",
    )
    engine = PlanEngine(
        server_run or ServerRun(run_id="run-test"),
        registry,
        permission_provider=permission_provider,
        scope_validator=scope_validator,
    )
    return engine, registry


def initialize(engine: PlanEngine, plan_tool: str = "read_serial-plan") -> None:
    result = engine.null_response(plan_tool)
    assert result.status == "initialized"


def test_ac_4_1_populated_plan_requires_all_null_initialization() -> None:
    engine, _ = engine_for()

    with pytest.raises(PlanRefusal, match="every parameter NULL") as caught:
        engine.submit("read_serial-plan", read_serial_fields(), session_id=SESSION)

    assert caught.value.code == "plan/not-initialized"


def test_ac_4_1_exact_all_null_call_initializes_the_plan_tool() -> None:
    engine, _ = engine_for()
    from pyocd_debug_mcp.guardrails.plan_defs import definition_for_plan_tool

    definition = definition_for_plan_tool("read_serial-plan")
    null_fields = {name: None for name in definition.null_field_names}
    result = engine.submit("read_serial-plan", null_fields)

    assert result.status == "initialized"
    accepted = engine.submit("read_serial-plan", read_serial_fields(), session_id=SESSION)
    assert accepted.status == "accepted"


def test_ac_4_1_partially_null_call_never_substitutes_for_initialization() -> None:
    engine, _ = engine_for()
    partial = mutate_plan(
        read_serial_fields(expected_text=None),
        {"hypothesis": None, "baudrate": None, "port": None},
    )

    with pytest.raises(PlanRefusal) as before_null:
        engine.submit("read_serial-plan", partial, session_id=SESSION)
    assert before_null.value.code == "plan/not-initialized"

    # The rejected partial call must not initialize the plan tool as a side effect.
    with pytest.raises(PlanRefusal) as still_uninitialized:
        engine.submit("read_serial-plan", read_serial_fields(), session_id=SESSION)
    assert still_uninitialized.value.code == "plan/not-initialized"

    initialize(engine)
    with pytest.raises(PlanRefusal) as invalid_populated:
        engine.submit("read_serial-plan", partial, session_id=SESSION)
    assert invalid_populated.value.code == "plan/invalid-fields"


def test_ac_4_1_nullable_action_parameters_are_valid_only_after_initialization() -> None:
    engine, _ = engine_for()
    fields = mutate_plan(
        read_serial_fields(expected_text=None), {"baudrate": None, "port": None}
    )

    with pytest.raises(PlanRefusal) as before_null:
        engine.submit("read_serial-plan", fields, session_id=SESSION)
    assert before_null.value.code == "plan/not-initialized"

    initialize(engine)
    accepted = engine.submit("read_serial-plan", fields, session_id=SESSION)
    assert accepted.status == "accepted"


def test_ac_4_2_null_response_contains_complete_guidance() -> None:
    engine, _ = engine_for()
    response = engine.null_response("read_serial-plan").message

    for required in (
        "Purpose:",
        "Required plan fields:",
        "Underlying action parameters:",
        "Budget: flexible",
        "Permission:",
        "Extra instructions:",
        "expected_text",
        "max_calls_buffer",
    ):
        assert required in response


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ({"hypothesis_made": False}, "plan/reasoning-flags"),
        ({"strategy_evaluated": False}, "plan/reasoning-flags"),
        ({"hypothesis": ""}, "plan/invalid-fields"),
        ({"strategy": "TBD"}, "plan/placeholder-reasoning"),
        ({"read_seconds": 0}, "plan/invalid-action-parameters"),
        ({"baudrate": -1}, "plan/invalid-action-parameters"),
    ],
)
def test_ac_4_3_complete_populated_plan_validation(
    mutation: dict[str, object],
    code: str,
) -> None:
    engine, _ = engine_for()
    initialize(engine)

    with pytest.raises(PlanRefusal) as caught:
        engine.submit(
            "read_serial-plan", mutate_plan(read_serial_fields(), mutation), session_id=SESSION
        )

    assert caught.value.code == code


def test_ac_4_3_missing_and_unknown_fields_are_reported_together() -> None:
    engine, _ = engine_for()
    initialize(engine)
    fields = read_serial_fields()
    fields.pop("strategy")
    fields["invented"] = True

    with pytest.raises(PlanRefusal, match="strategy.*invented") as caught:
        engine.submit("read_serial-plan", fields, session_id=SESSION)

    assert caught.value.code == "plan/incomplete"


@pytest.mark.parametrize(
    ("field_name", "placeholder"),
    [
        ("hypothesis", "unknown"),
        ("strategy", " placeholder "),
        ("expected_fail_return", "N/A"),
        ("expected_success_return", "ToDo"),
    ],
)
def test_ac_4_3_every_reasoning_output_rejects_placeholder_text(
    field_name: str,
    placeholder: str,
) -> None:
    engine, _ = engine_for()
    initialize(engine)

    with pytest.raises(PlanRefusal) as caught:
        engine.submit(
            "read_serial-plan",
            read_serial_fields() | {field_name: placeholder},
            session_id=SESSION,
        )

    assert caught.value.code == "plan/placeholder-reasoning"


@pytest.mark.parametrize("flag_name", ["hypothesis_made", "strategy_evaluated"])
def test_ac_4_3_false_reasoning_flags_are_never_truthy_coerced(flag_name: str) -> None:
    engine, _ = engine_for()
    initialize(engine)

    with pytest.raises(PlanRefusal) as caught:
        engine.submit(
            "read_serial-plan",
            read_serial_fields() | {flag_name: False},
            session_id=SESSION,
        )

    assert caught.value.code == "plan/reasoning-flags"


@pytest.mark.parametrize(
    ("max_calls", "max_calls_buffer"),
    [(2, 0), (1, 1), (2, 1)],
)
def test_ac_4_4_every_fixed_budget_drift_is_rejected(
    max_calls: int,
    max_calls_buffer: int,
) -> None:
    fixed_engine, _ = engine_for("write_memory")
    initialize(fixed_engine, "write_memory-plan")
    with pytest.raises(PlanRefusal) as fixed:
        fixed_engine.submit(
            "write_memory-plan",
            write_memory_fields()
            | {"max_calls": max_calls, "max_calls_buffer": max_calls_buffer},
            session_id=SESSION,
        )
    assert fixed.value.code == "plan/fixed-budget"


@pytest.mark.parametrize(
    ("max_calls", "max_calls_buffer"),
    [(21, 10), (20, 11), (21, 11)],
)
def test_a9_flexible_budget_ceilings_are_independently_enforced(
    max_calls: int,
    max_calls_buffer: int,
) -> None:
    flexible_engine, _ = engine_for()
    initialize(flexible_engine)
    with pytest.raises(PlanRefusal) as capped:
        flexible_engine.submit(
            "read_serial-plan",
            read_serial_fields(max_calls=max_calls)
            | {"max_calls_buffer": max_calls_buffer},
            session_id=SESSION,
        )
    assert capped.value.code == "plan/budget-cap"


def test_a9_flexible_budget_accepts_both_exact_ceilings() -> None:
    engine, _ = engine_for()
    initialize(engine)

    accepted = engine.submit(
        "read_serial-plan",
        read_serial_fields(max_calls=20) | {"max_calls_buffer": 10},
        session_id=SESSION,
    )

    assert accepted.plan is not None
    assert accepted.plan.total_calls == 30


def test_ac_4_5_valid_plan_unlocks_one_tool_for_exactly_one_board() -> None:
    engine, registry = engine_for()
    initialize(engine)

    result = engine.submit("read_serial-plan", read_serial_fields(), session_id=SESSION)

    assert result.plan is not None
    assert result.plan.plan_id in result.message
    assert "read_serial" in result.message
    assert "Total permitted calls: 2" in result.message
    assert registry.is_unlocked("read_serial", "board_a") is True
    assert registry.is_unlocked("read_serial", "board_b") is False


def test_ac_4_6_parameter_drift_is_exact_and_consumes_no_budget() -> None:
    engine, _ = engine_for()
    initialize(engine)
    engine.submit("read_serial-plan", read_serial_fields(), session_id=SESSION)

    with pytest.raises(PlanRefusal) as caught:
        engine.enforce(
            "read_serial",
            "board_a",
            read_serial_parameters(expected_text="different"),
            session_id=SESSION,
        )

    assert caught.value.code == "plan/parameter-mismatch"
    active = engine.active_plan("read_serial", "board_a")
    assert active is not None and active.remaining_calls == 2


def test_ac_4_6_canonical_binding_preserves_types_and_copies_nested_values() -> None:
    engine, _ = engine_for("write_memory")
    initialize(engine, "write_memory-plan")
    nested = {"b": 2, "a": [1]}
    engine.submit("write_memory-plan", write_memory_fields(nested), session_id=SESSION)
    nested["a"].append(99)

    permit = engine.enforce(
        "write_memory",
        "board_a",
        write_memory_parameters({"a": [1], "b": 2}),
        session_id=SESSION,
    )
    assert permit.status is PlanStatus.EXHAUSTED

    second, _ = engine_for("write_memory")
    initialize(second, "write_memory-plan")
    second.submit("write_memory-plan", write_memory_fields(1), session_id=SESSION)
    with pytest.raises(PlanRefusal, match="immutable plan binding"):
        second.enforce(
            "write_memory",
            "board_a",
            write_memory_parameters(1.0),
            session_id=SESSION,
        )


@pytest.mark.parametrize(
    ("planned", "supplied"),
    [
        ([1, 2], [2, 1]),
        ("caf\u00e9", "cafe\u0301"),
        (True, 1),
    ],
)
def test_ac_4_6_semantically_distinct_json_values_never_share_a_binding(
    planned: object,
    supplied: object,
) -> None:
    engine, _ = engine_for("write_memory")
    initialize(engine, "write_memory-plan")
    engine.submit("write_memory-plan", write_memory_fields(planned), session_id=SESSION)

    with pytest.raises(PlanRefusal) as caught:
        engine.enforce(
            "write_memory",
            "board_a",
            write_memory_parameters(supplied),
            session_id=SESSION,
        )

    assert caught.value.code == "plan/parameter-mismatch"
    active = engine.active_plan("write_memory", "board_a")
    assert active is not None and active.remaining_calls == 1


def test_ac_4_7_started_failure_or_cancellation_consumes_but_precheck_does_not() -> None:
    engine, _ = engine_for()
    initialize(engine)
    engine.submit("read_serial-plan", read_serial_fields(), session_id=SESSION)

    def reject_before_start() -> None:
        raise PolicyRefusal("safety/not-ready", "Layer 0 rejected before execution")

    with pytest.raises(PolicyRefusal):
        engine.enforce(
            "read_serial",
            "board_a",
            read_serial_parameters(),
            session_id=SESSION,
            preconditions=reject_before_start,
        )
    active = engine.active_plan("read_serial", "board_a")
    assert active is not None and active.remaining_calls == 2

    engine.enforce("read_serial", "board_a", read_serial_parameters(), session_id=SESSION)
    # A handler failure, timeout, or cancellation occurs after this start boundary; no refund API exists.
    active = engine.active_plan("read_serial", "board_a")
    assert active is not None and active.remaining_calls == 1


@pytest.mark.parametrize(
    "post_start_error",
    [
        RuntimeError("handler failed"),
        TimeoutError("operation timed out"),
        asyncio.CancelledError("request cancelled"),
    ],
)
def test_ac_4_7_each_terminal_path_after_start_has_no_budget_refund(
    post_start_error: BaseException,
) -> None:
    engine, _ = engine_for()
    initialize(engine)
    engine.submit("read_serial-plan", read_serial_fields(), session_id=SESSION)

    with pytest.raises(type(post_start_error)):
        engine.enforce("read_serial", "board_a", read_serial_parameters(), session_id=SESSION)
        raise post_start_error

    active = engine.active_plan("read_serial", "board_a")
    assert active is not None and active.remaining_calls == 1


def test_ac_4_8_exhaustion_relocks_and_requires_replacement() -> None:
    engine, registry = engine_for()
    initialize(engine)
    engine.submit(
        "read_serial-plan",
        read_serial_fields(max_calls=1),
        session_id=SESSION,
    )

    permit = engine.enforce(
        "read_serial", "board_a", read_serial_parameters(), session_id=SESSION
    )

    assert permit.status is PlanStatus.EXHAUSTED
    assert permit.remaining_calls == 0
    assert registry.is_unlocked("read_serial", "board_a") is False
    with pytest.raises(PlanRefusal) as caught:
        engine.enforce("read_serial", "board_a", read_serial_parameters(), session_id=SESSION)
    assert caught.value.code == "plan/no-active-plan"


def test_ac_4_9_replacement_atomically_closes_old_parameter_binding() -> None:
    engine, registry = engine_for()
    initialize(engine)
    first = engine.submit(
        "read_serial-plan",
        read_serial_fields(expected_text="first"),
        session_id=SESSION,
    )
    second = engine.submit(
        "read_serial-plan",
        read_serial_fields(expected_text="second"),
        session_id=SESSION,
    )

    assert first.plan is not None and second.plan is not None
    assert first.plan.plan_id != second.plan.plan_id
    assert registry.is_unlocked("read_serial", "board_a") is True
    with pytest.raises(PlanRefusal):
        engine.enforce(
            "read_serial",
            "board_a",
            read_serial_parameters("first"),
            session_id=SESSION,
        )
    active = engine.active_plan("read_serial", "board_a")
    assert active is not None and active.plan_id == second.plan.plan_id
    assert active.remaining_calls == 2


def test_ac_4_10_restart_has_no_initialized_or_active_plan_state() -> None:
    first_run = ServerRun(run_id="run-first")
    engine, registry = engine_for(server_run=first_run)
    initialize(engine)
    engine.submit("read_serial-plan", read_serial_fields(), session_id=SESSION)
    engine.close_run()

    restarted, restarted_registry = engine_for(server_run=ServerRun(run_id="run-second"))

    assert first_run.plans == {}
    assert registry.is_unlocked("read_serial", "board_a") is False
    assert restarted_registry.is_unlocked("read_serial", "board_a") is False
    with pytest.raises(PlanRefusal) as caught:
        restarted.submit("read_serial-plan", read_serial_fields(), session_id=SESSION)
    assert caught.value.code == "plan/not-initialized"


def test_concurrent_execution_decrements_exactly_once_and_never_overspends() -> None:
    engine, registry = engine_for()
    initialize(engine)
    engine.submit(
        "read_serial-plan",
        read_serial_fields(max_calls=1),
        session_id=SESSION,
    )
    barrier = threading.Barrier(8)

    def attempt() -> str:
        barrier.wait()
        try:
            engine.enforce(
                "read_serial", "board_a", read_serial_parameters(), session_id=SESSION
            )
        except PlanRefusal as exc:
            return exc.code
        return "started"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _: attempt(), range(8)))

    assert outcomes.count("started") == 1
    assert outcomes.count("plan/no-active-plan") == 7
    assert registry.is_unlocked("read_serial", "board_a") is False


def test_atomic_concurrent_replacement_leaves_one_complete_active_plan() -> None:
    engine, _ = engine_for()
    initialize(engine)
    barrier = threading.Barrier(2)

    def submit(expected: str) -> None:
        barrier.wait()
        engine.submit(
            "read_serial-plan",
            read_serial_fields(expected_text=expected),
            session_id=SESSION,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(submit, ("alpha", "beta")))

    active = engine.active_plan("read_serial", "board_a")
    assert active is not None
    winner = active.action_parameters["expected_text"]
    loser = "alpha" if winner == "beta" else "beta"
    with pytest.raises(PlanRefusal):
        engine.enforce(
            "read_serial",
            "board_a",
            read_serial_parameters(loser),
            session_id=SESSION,
        )
    still_active = engine.active_plan("read_serial", "board_a")
    assert still_active is not None and still_active.remaining_calls == 2


class FailNextUnlockRegistry(ToolRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next_unlock = False

    def unlock(self, name: str, board_id: str) -> None:
        if self.fail_next_unlock:
            self.fail_next_unlock = False
            raise RuntimeError("injected unlock failure")
        super().unlock(name, board_id)


def test_ac_4_9_failed_replacement_rolls_back_to_complete_prior_plan() -> None:
    registry = FailNextUnlockRegistry()
    engine, _ = engine_for(registry=registry)
    initialize(engine)
    first = engine.submit(
        "read_serial-plan",
        read_serial_fields(expected_text="first"),
        session_id=SESSION,
    )
    assert first.plan is not None
    registry.fail_next_unlock = True

    with pytest.raises(RuntimeError, match="injected unlock failure"):
        engine.submit(
            "read_serial-plan",
            read_serial_fields(expected_text="second"),
            session_id=SESSION,
        )

    active = engine.active_plan("read_serial", "board_a")
    assert active is not None
    assert active.plan_id == first.plan.plan_id
    assert active.action_parameters["expected_text"] == "first"
    assert active.status is PlanStatus.ACTIVE
    assert registry.is_unlocked("read_serial", "board_a") is True


def test_board_and_session_scope_never_transfer_and_session_change_invalidates() -> None:
    engine, registry = engine_for()
    initialize(engine)
    engine.submit("read_serial-plan", read_serial_fields(), session_id=SESSION)

    with pytest.raises(PlanRefusal) as wrong_board:
        engine.enforce(
            "read_serial", "board_b", read_serial_parameters(), session_id=SESSION
        )
    assert wrong_board.value.code == "plan/no-active-plan"
    assert registry.is_unlocked("read_serial", "board_a") is True

    with pytest.raises(PlanRefusal) as new_session:
        engine.enforce(
            "read_serial",
            "board_a",
            read_serial_parameters(),
            session_id="session-b",
        )
    assert new_session.value.code == "plan/session-mismatch"
    assert registry.is_unlocked("read_serial", "board_a") is False


def test_run_identity_change_invalidates_and_relocks() -> None:
    server_run = ServerRun(run_id="run-original")
    engine, registry = engine_for(server_run=server_run)
    initialize(engine)
    engine.submit("read_serial-plan", read_serial_fields(), session_id=SESSION)
    server_run.run_id = "run-restarted"

    with pytest.raises(PlanRefusal) as caught:
        engine.enforce("read_serial", "board_a", read_serial_parameters(), session_id=SESSION)

    assert caught.value.code == "plan/run-mismatch"
    assert registry.is_unlocked("read_serial", "board_a") is False


def test_invalid_assignment_scope_relocks_without_consuming() -> None:
    def reject_scope(
        definition: PlanDefinition,
        board_id: str,
        session_id: str | None,
    ) -> None:
        del definition, board_id
        raise PlanRefusal(
            "plan/session-invalid",
            "Assignment disappeared",
            session_id=session_id,
        )

    engine, registry = engine_for(scope_validator=reject_scope)
    initialize(engine)
    engine.submit("read_serial-plan", read_serial_fields(), session_id=SESSION)

    with pytest.raises(PlanRefusal) as caught:
        engine.enforce("read_serial", "board_a", read_serial_parameters(), session_id=SESSION)

    assert caught.value.code == "plan/session-invalid"
    assert engine.active_plan("read_serial", "board_a") is None
    assert registry.is_unlocked("read_serial", "board_a") is False


class AllowPermissionProvider:
    def __init__(self) -> None:
        self.consumed = 0

    def null_disclosure(self, definition: PlanDefinition) -> str | None:
        assert definition.permission_mode is not PermissionMode.NONE
        return "A test permission provider is active."

    def authorize_plan(
        self,
        definition: PlanDefinition,
        board_id: str,
        user_permission: object,
        max_calls: int,
        max_calls_buffer: int,
    ) -> object:
        assert definition.permission_mode is not PermissionMode.NONE
        assert board_id == "board_a"
        assert user_permission == "one-time"
        assert (max_calls, max_calls_buffer) == (1, 0)
        if definition.action_name == "board_setup":
            return "setup-authorization"
        assert definition.action_name == "set_execution_state"
        return "opaque-test-authorization"

    def validate_execution(
        self,
        definition: PlanDefinition,
        board_id: str,
        authorization: object,
    ) -> None:
        assert definition.action_name == "set_execution_state"
        assert board_id == "board_a"
        assert authorization == "opaque-test-authorization"

    def consume_execution(
        self,
        definition: PlanDefinition,
        board_id: str,
        authorization: object,
    ) -> None:
        self.validate_execution(definition, board_id, authorization)
        self.consumed += 1

    def reset(self) -> None:
        self.consumed = 0


def execution_state_fields() -> dict[str, object]:
    return common_fields(max_calls=1) | {
        "user_permission": "one-time",
        "action_parameters": {"name": "pc", "value": "0x08000101"},
    }


def test_permissions_remain_fail_closed_and_injectable_until_task_6() -> None:
    closed, _ = engine_for("set_execution_state")
    initialize(closed, "set_execution_state-plan")
    with pytest.raises(PlanRefusal) as unavailable:
        closed.submit(
            "set_execution_state-plan",
            execution_state_fields(),
            session_id=SESSION,
        )
    assert unavailable.value.code == "permission/provider-unavailable"

    provider = AllowPermissionProvider()
    injected, _ = engine_for("set_execution_state", permission_provider=provider)
    initialize(injected, "set_execution_state-plan")
    injected.submit(
        "set_execution_state-plan",
        execution_state_fields(),
        session_id=SESSION,
    )
    injected.enforce(
        "set_execution_state",
        "board_a",
        {"name": "pc", "value": "0x08000101"},
        session_id=SESSION,
    )
    assert provider.consumed == 1


def test_a10_setup_plan_cycle_limit_relocks_after_three_replacements() -> None:
    provider = AllowPermissionProvider()
    engine, registry = engine_for("board_setup", permission_provider=provider)
    initialize(engine, "board_setup-plan")
    fields = common_fields(max_calls=1) | {
        "user_permission": "one-time",
        "action_parameters": {
            "mode": "setup",
            "connection_id": "connection-1",
            "display_name": "Bench Board",
            "board_type": "nucleo_l476rg",
            "mcu_part_number": "STM32L476RGT6",
            "serial_baudrate": 115200,
            "serial_id": "UART-001",
            "serial_port": "COM1",
            "datasheet_path": "board-datasheet.pdf",
            "datasheet_sha256": "0" * 64,
        },
    }
    for _ in range(3):
        engine.submit("board_setup-plan", fields, session_id=None)

    with pytest.raises(PlanRefusal) as exhausted:
        engine.submit("board_setup-plan", fields, session_id=None)

    assert exhausted.value.code == "plan/retry-exhausted"
    assert registry.is_unlocked("board_setup", "board_a") is False
