from __future__ import annotations

import pytest

from pyocd_debug_mcp.guardrails.permissions import PermissionStore
from pyocd_debug_mcp.guardrails.plan_engine import PlanEngine, PlanRefusal, PlanStatus
from pyocd_debug_mcp.kernel.registry import ToolRegistry
from pyocd_debug_mcp.kernel.run_state import ServerRun


PARAMETERS = {
    "mode": "setup",
    "connection_id": "probe:001",
    "display_name": "Bench Board",
    "board_type": "nucleo_l476rg",
    "mcu_part_number": "STM32L476RGT6",
    "serial_baudrate": 115200,
    "serial_id": "UART-001",
    "serial_port": "COM1",
    "datasheet_path": "board-datasheet.pdf",
    "datasheet_sha256": "0" * 64,
}


def fields() -> dict[str, object]:
    return {
        "board_id": "board_a",
        "hypothesis": "The selected board can complete deterministic setup.",
        "hypothesis_made": True,
        "strategy": "Run setup once and use its one repair allowance only if required.",
        "strategy_evaluated": True,
        "expected_fail_return": "A deterministic setup status and continuation remedy.",
        "expected_success_return": "A completed and validated board profile.",
        "max_calls": 1,
        "max_calls_buffer": 0,
        "user_permission": "one-time",
        "action_parameters": PARAMETERS,
    }


def engine() -> tuple[PlanEngine, ToolRegistry, PermissionStore]:
    run = ServerRun(run_id="setup-plan-run")
    registry = ToolRegistry()
    registry.register(
        "board_setup",
        hidden=True,
        locked=True,
        prerequisite="board_setup-plan",
    )
    registry.register(
        "board_fix_setup",
        hidden=True,
        locked=True,
        prerequisite="board_setup-plan",
    )
    permissions = PermissionStore(run)
    plan_engine = PlanEngine(run, registry, permission_provider=permissions)
    permissions.set_revocation_handler(
        lambda tool, board, reason: plan_engine.invalidate(tool, board, reason)
    )
    plan_engine.null_response("board_setup-plan")
    plan_engine.submit("board_setup-plan", fields())
    return plan_engine, registry, permissions


def test_one_setup_and_one_fix_share_one_time_permission_then_exhaust_together() -> None:
    plan_engine, registry, permissions = engine()

    setup_call = plan_engine.enforce("board_setup", "board_a", PARAMETERS)

    assert setup_call.status is PlanStatus.ACTIVE
    assert setup_call.remaining_for("board_setup") == 0
    assert setup_call.remaining_for("board_fix_setup") == 1
    assert permissions.active_grant("board_setup", "board_a") is not None
    assert registry.is_unlocked("board_setup", "board_a") is True
    assert registry.is_unlocked("board_fix_setup", "board_a") is True
    with pytest.raises(PlanRefusal) as duplicate:
        plan_engine.enforce("board_setup", "board_a", PARAMETERS)
    assert duplicate.value.code == "plan/action-exhausted"

    fix_call = plan_engine.enforce("board_fix_setup", "board_a", PARAMETERS)

    assert fix_call.status is PlanStatus.EXHAUSTED
    assert fix_call.remaining_for("board_fix_setup") == 0
    assert permissions.active_grant("board_setup", "board_a") is None
    assert registry.is_unlocked("board_setup", "board_a") is False
    assert registry.is_unlocked("board_fix_setup", "board_a") is False


def test_completion_before_fix_consumes_one_time_permission_and_relocks() -> None:
    plan_engine, registry, permissions = engine()
    plan_engine.enforce("board_setup", "board_a", PARAMETERS)

    plan_engine.complete_paired_plan("board_setup", "board_a", "setup completed")

    assert plan_engine.active_plan("board_setup", "board_a") is None
    assert permissions.active_grant("board_setup", "board_a") is None
    assert registry.is_unlocked("board_setup", "board_a") is False
    assert registry.is_unlocked("board_fix_setup", "board_a") is False


def test_third_plan_engine_call_requires_and_accepts_only_a_replacement_plan() -> None:
    plan_engine, registry, _ = engine()
    plan_engine.enforce("board_setup", "board_a", PARAMETERS)
    plan_engine.enforce("board_fix_setup", "board_a", PARAMETERS)

    with pytest.raises(PlanRefusal) as stale:
        plan_engine.enforce("board_fix_setup", "board_a", PARAMETERS)
    assert stale.value.code == "plan/no-active-plan"

    replacement = plan_engine.submit("board_setup-plan", fields())
    assert replacement.plan is not None
    assert replacement.plan.total_calls == 2
    assert registry.is_unlocked("board_setup", "board_a") is True
    assert registry.is_unlocked("board_fix_setup", "board_a") is True
    third = plan_engine.enforce("board_setup", "board_a", PARAMETERS)
    assert third.status is PlanStatus.ACTIVE
