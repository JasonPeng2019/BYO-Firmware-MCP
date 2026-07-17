from __future__ import annotations

import pytest

from pyocd_debug_mcp.guardrails.permissions import GrantMode, PermissionStore
from pyocd_debug_mcp.guardrails.plan_defs import PLAN_DEFINITIONS
from pyocd_debug_mcp.guardrails.plan_engine import PlanEngine, PlanRefusal
from pyocd_debug_mcp.kernel.registry import ToolRegistry
from pyocd_debug_mcp.kernel.run_state import ServerRun
from pyocd_debug_mcp.services.session_runtime import PolicyRefusal


SESSION = "session-a"


def execution_state_fields(
    *,
    board_id: str = "board_a",
    user_permission: object = "one-time",
) -> dict[str, object]:
    return {
        "board_id": board_id,
        "hypothesis": "The program counter will change to the requested mapped value.",
        "hypothesis_made": True,
        "strategy": "Perform one bounded execution-state write and inspect the result.",
        "strategy_evaluated": True,
        "expected_fail_return": "A deterministic permission or policy refusal.",
        "expected_success_return": "The execution-state register is updated.",
        "max_calls": 1,
        "max_calls_buffer": 0,
        "user_permission": user_permission,
        "action_parameters": {"name": "pc", "value": "0x08000101"},
    }


def permission_engine() -> tuple[PlanEngine, PermissionStore, ToolRegistry, ServerRun]:
    server_run = ServerRun(run_id="permission-run")
    registry = ToolRegistry()
    registry.register(
        "set_execution_state",
        hidden=True,
        locked=True,
        prerequisite="set_execution_state-plan",
    )
    store = PermissionStore(server_run)
    engine = PlanEngine(server_run, registry, permission_provider=store)
    store.set_revocation_handler(engine.invalidate)
    engine.null_response("set_execution_state-plan")
    return engine, store, registry, server_run


def test_ac_5_1_missing_or_invalid_structured_permission_is_rejected() -> None:
    definition = PLAN_DEFINITIONS["set_execution_state"]
    store = PermissionStore(ServerRun(run_id="permission-run"))

    for supplied in (None, "yes", True, "ONE-TIME"):
        with pytest.raises(PolicyRefusal) as caught:
            store.authorize_plan(definition, "board_a", supplied, 1, 0)
        assert caught.value.code == "permission/required"
        assert "ordinary language" in caught.value.message
        assert "structured user_permission" in caught.value.message
        assert "conversational assent is not authorization" in caught.value.message


def test_ac_5_2_one_time_permission_requires_exactly_one_zero_budget() -> None:
    definition = PLAN_DEFINITIONS["set_execution_state"]
    store = PermissionStore(ServerRun(run_id="permission-run"))

    for budget in ((2, 0), (1, 1), (2, 1)):
        with pytest.raises(PolicyRefusal) as caught:
            store.authorize_plan(definition, "board_a", "one-time", *budget)
        assert caught.value.code == "permission/one-time-budget"
    assert store.active_grant("set_execution_state", "board_a") is None


def test_ac_5_3_one_time_is_consumed_at_execution_start_and_plan_relocks() -> None:
    engine, store, registry, _ = permission_engine()
    engine.submit(
        "set_execution_state-plan",
        execution_state_fields(),
        session_id=SESSION,
    )

    permit = engine.enforce(
        "set_execution_state",
        "board_a",
        {"name": "pc", "value": "0x08000101"},
        session_id=SESSION,
    )

    assert permit.remaining_calls == 0
    assert store.active_grant("set_execution_state", "board_a") is None
    assert registry.is_unlocked("set_execution_state", "board_a") is False
    with pytest.raises(PlanRefusal) as caught:
        engine.enforce(
            "set_execution_state",
            "board_a",
            {"name": "pc", "value": "0x08000101"},
            session_id=SESSION,
        )
    assert caught.value.code == "plan/no-active-plan"


def test_ac_5_4_full_session_allows_null_replans_and_is_disclosed() -> None:
    engine, store, _, _ = permission_engine()
    first = engine.submit(
        "set_execution_state-plan",
        execution_state_fields(user_permission="full-session"),
        session_id=SESSION,
    )
    assert first.status == "accepted"
    grant = store.active_grant("set_execution_state", "board_a")
    assert grant is not None and grant.mode is GrantMode.FULL_SESSION

    response = engine.null_response("set_execution_state-plan").message
    assert "Full-session permission is active for board(s): board_a" in response
    assert "user_permission may be NULL only for a listed board" in response

    replacement = engine.submit(
        "set_execution_state-plan",
        execution_state_fields(user_permission=None)
        | {"action_parameters": {"name": "pc", "value": "0x08000105"}},
        session_id=SESSION,
    )
    assert replacement.status == "accepted"


def test_ac_5_5_full_session_grant_is_scoped_to_exact_tool_and_board() -> None:
    definition = PLAN_DEFINITIONS["set_execution_state"]
    other = PLAN_DEFINITIONS["board_setup"]
    store = PermissionStore(ServerRun(run_id="permission-run"))
    store.authorize_plan(definition, "board_a", "full-session", 1, 0)

    assert store.active_grant("set_execution_state", "board_a") is not None
    assert store.active_grant("set_execution_state", "board_b") is None
    assert store.active_grant("board_setup", "board_a") is None
    with pytest.raises(PolicyRefusal) as wrong_board:
        store.authorize_plan(definition, "board_b", None, 1, 0)
    assert wrong_board.value.code == "permission/required"
    with pytest.raises(PolicyRefusal) as wrong_tool:
        store.authorize_plan(other, "board_a", None, 1, 0)
    assert wrong_tool.value.code == "permission/required"


def test_ac_5_6_reset_clears_every_grant_and_relocks_matching_plans() -> None:
    engine, store, registry, server_run = permission_engine()
    engine.submit(
        "set_execution_state-plan",
        execution_state_fields(user_permission="full-session"),
        session_id=SESSION,
    )
    assert registry.is_unlocked("set_execution_state", "board_a") is True

    engine.close_run()

    assert server_run.permissions == {}
    assert engine.active_plan("set_execution_state", "board_a") is None
    assert registry.is_unlocked("set_execution_state", "board_a") is False


def test_structured_revocation_reports_result_and_invalidates_only_its_scope() -> None:
    engine, store, registry, _ = permission_engine()
    engine.submit(
        "set_execution_state-plan",
        execution_state_fields(user_permission="full-session"),
        session_id=SESSION,
    )

    result = store.revoke(
        "set_execution_state",
        "board_a",
        reason="User withdrew approval",
    )

    assert result.revoked is True
    assert result.grant_id is not None
    assert result.reason == "User withdrew approval"
    assert result.revoked_at.endswith("Z")
    assert engine.active_plan("set_execution_state", "board_a") is None
    assert registry.is_unlocked("set_execution_state", "board_a") is False


def test_revocation_preserves_other_tool_and_board_grants() -> None:
    server_run = ServerRun(run_id="permission-run")
    callbacks: list[tuple[str, str, str]] = []
    store = PermissionStore(
        server_run,
        on_revoke=lambda tool, board, reason: callbacks.append((tool, board, reason)),
    )
    execution = PLAN_DEFINITIONS["set_execution_state"]
    setup = PLAN_DEFINITIONS["board_setup"]
    store.authorize_plan(execution, "board_a", "full-session", 1, 0)
    store.authorize_plan(execution, "board_b", "full-session", 1, 0)
    store.authorize_plan(setup, "board_a", "full-session", 1, 0)

    result = store.revoke(
        "set_execution_state",
        "board_a",
        reason="User withdrew only this approval",
    )

    assert result.revoked is True
    assert store.active_grant("set_execution_state", "board_a") is None
    assert store.active_grant("set_execution_state", "board_b") is not None
    assert store.active_grant("board_setup", "board_a") is not None
    assert callbacks == [
        ("set_execution_state", "board_a", "User withdrew only this approval")
    ]


def test_ac_5_7_fresh_one_time_never_accepts_full_session_authority() -> None:
    definition = PLAN_DEFINITIONS["target_unlock"]
    store = PermissionStore(ServerRun(run_id="permission-run"))

    with pytest.raises(PolicyRefusal) as caught:
        store.authorize_plan(definition, "board_a", "full-session", 1, 0)
    assert caught.value.code == "permission/fresh-one-time-required"

    authorization = store.authorize_plan(definition, "board_a", "one-time", 1, 0)
    store.consume_execution(definition, "board_a", authorization)
    assert store.active_grant("target_unlock", "board_a") is None
    with pytest.raises(PolicyRefusal) as missing_fresh:
        store.authorize_plan(definition, "board_a", None, 1, 0)
    assert missing_fresh.value.code == "permission/required"
