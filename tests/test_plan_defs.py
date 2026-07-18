from __future__ import annotations

from pyocd_debug_mcp.guardrails.plan_defs import (
    BudgetMode,
    PermissionMode,
    PLAN_DEFINITIONS,
    SafetyMode,
    definition_for_action,
)
from pyocd_debug_mcp.guardrails.plan_engine import PlanEngine
from pyocd_debug_mcp.kernel.registry import ToolRegistry
from pyocd_debug_mcp.kernel.run_state import ServerRun


def test_plan_definitions_cover_the_complete_guarded_surface() -> None:
    expected = {
        "board_setup",
        "board_fix_setup",
        "connect_override",
        "write_cpu_register",
        "set_execution_state",
        "read_memory_address",
        "write_memory",
        "set_breakpoint",
        "flash_application",
        "flash_bootloader",
        "register_write",
        "reset_and_halt",
        "connect_under_reset",
        "target_unlock",
        "read_serial",
        "write_serial",
        "serial_exchange",
    }

    assert set(PLAN_DEFINITIONS) | {"board_fix_setup"} == expected
    assert definition_for_action("board_fix_setup") is PLAN_DEFINITIONS["board_setup"]


def test_budget_permission_safety_and_timeout_policy_is_declarative() -> None:
    fixed = {
        name
        for name, definition in PLAN_DEFINITIONS.items()
        if definition.budget_mode is BudgetMode.FIXED
    }
    assert fixed == {
        "board_setup",
        "write_cpu_register",
        "set_execution_state",
        "write_memory",
        "set_breakpoint",
        "flash_application",
        "flash_bootloader",
        "register_write",
        "target_unlock",
    }
    assert {
        name
        for name, definition in PLAN_DEFINITIONS.items()
        if definition.permission_mode is not PermissionMode.NONE
    } == {"board_setup", "set_execution_state", "flash_bootloader", "target_unlock"}
    assert PLAN_DEFINITIONS["target_unlock"].permission_mode is PermissionMode.FRESH_ONE_TIME

    for read_action in ("read_memory_address", "read_serial"):
        assert PLAN_DEFINITIONS[read_action].safety_mode is SafetyMode.VALIDATED_READ
    for definition in PLAN_DEFINITIONS.values():
        if definition.action_name.startswith("flash_"):
            assert definition.timeout_seconds == 120.0
        elif definition.action_name == "board_setup":
            assert definition.timeout_seconds == 300.0
        elif definition.action_name == "target_unlock":
            assert definition.timeout_seconds == 300.0
        else:
            assert definition.timeout_seconds == 30.0


def test_null_response_text_is_derived_from_each_complete_definition() -> None:
    for definition in PLAN_DEFINITIONS.values():
        response = definition.render_null_response("Injected disclosure.")
        assert definition.purpose in response
        assert definition.plan_tool_name in response
        assert definition.extra_instructions in response
        assert definition.budget_mode.value in response
        assert definition.safety_mode.value in response
        for field in definition.plan_fields:
            assert field.name in response


def test_engine_null_responses_equal_definition_output_and_include_all_instructions() -> None:
    engine = PlanEngine(ServerRun(run_id="null-guidance-run"), ToolRegistry())
    unavailable_disclosure = "No full-session permission provider is active yet."

    for definition in PLAN_DEFINITIONS.values():
        disclosure = (
            unavailable_disclosure
            if definition.permission_mode is not PermissionMode.NONE
            else None
        )
        result = engine.null_response(definition.plan_tool_name)

        assert result.message == definition.render_null_response(disclosure)
        assert f"Plan initialization for {definition.plan_tool_name}." in result.message
        assert f"Purpose: {definition.purpose}" in result.message
        assert "Required plan fields:" in result.message
        assert "Underlying action parameters:" in result.message
        assert "Budget:" in result.message
        assert "Permission:" in result.message
        assert definition.safety_mode.value in result.message
        assert f"default timeout {definition.timeout_seconds:g}s" in result.message
        assert f"Extra instructions: {definition.extra_instructions}" in result.message
        for field in definition.plan_fields:
            assert f"- {field.name}: {field.description}" in result.message


def test_action_schema_carries_format_constraints() -> None:
    read_fields = {
        field.name: field for field in PLAN_DEFINITIONS["read_memory_address"].action_fields
    }
    assert read_fields["width"].choices == (8, 16, 32)
    assert read_fields["length"].maximum == 65536
    serial_fields = {
        field.name: field for field in PLAN_DEFINITIONS["read_serial"].action_fields
    }
    assert serial_fields["read_seconds"].exclusive_minimum is True
    assert serial_fields["baudrate"].minimum == 1

    setup_fields = {
        field.name: field for field in PLAN_DEFINITIONS["board_setup"].action_fields
    }
    assert "serial_port" not in setup_fields
    assert setup_fields["serial_id"].nullable is False
    assert setup_fields["datasheet_sha256"].nullable is True

    for action_name in ("flash_application", "flash_bootloader"):
        flash_fields = {
            field.name: field for field in PLAN_DEFINITIONS[action_name].action_fields
        }
        assert set(flash_fields) == {"artifact"}
