from __future__ import annotations

import json
from pathlib import Path

import pytest

from pyocd_debug_mcp.guardrails.plan_contract import render_plan_contract_markdown
from pyocd_debug_mcp.guardrails.plan_defs import PLAN_DEFINITIONS, PermissionMode
from pyocd_debug_mcp.guardrails.plan_engine import PlanEngine, PlanRefusal
from pyocd_debug_mcp.kernel.registry import ToolRegistry
from pyocd_debug_mcp.kernel.run_state import ServerRun


SECTIONS = (
    "[MECHANISM]",
    "[PURPOSE]",
    "[SETUP-FIRST ROUTING]",
    "[LOCAL-FIRST DEPENDENCIES]",
    "[USE-WHEN / NOT-WHEN]",
    "[PLAN-FIELDS]",
    "[ACTION-PARAMETERS]",
    "[VALIDATION]",
    "[BUDGET]",
    "[PERMISSION]",
    "[PRECONDITIONS]",
    "[WARNINGS]",
    "[SOFT-GUARDRAILS",
    "[EXIT]",
    "[EXAMPLE-PLAN]",
    "[AFTER-ACCEPTANCE]",
)


@pytest.mark.parametrize("action_name", sorted(PLAN_DEFINITIONS))
def test_every_null_prompt_is_self_contained_and_ordered(action_name: str) -> None:
    definition = PLAN_DEFINITIONS[action_name]
    response = definition.render_null_response("No reusable grant is active.")

    offsets = [response.index(section) for section in SECTIONS]
    assert offsets == sorted(offsets)
    for required in (
        definition.action_name,
        definition.plan_tool_name,
        "action_parameters",
        "hypothesis_made",
        "strategy_evaluated",
        "expected_fail_return",
        "expected_success_return",
        "max_calls",
        "max_calls_buffer",
        "Submit only the plan JSON object",
        "no prose, Markdown, wrapper key, flattened action fields, or extra fields",
        "Pre-execution refusal consumes nothing",
        "complete replacement plan",
        "exact server-returned single-child action_batch fallback unchanged",
        "STM32CubeIDE/STM32Cube/ThreadX",
        "Download only as a fallback",
        "recursively scan the whole disk",
    ):
        assert required.casefold() in response.casefold()
    for field in definition.action_fields:
        assert field.name in response

    if action_name == "board_setup":
        assert "Every matching YAML goes to board_validate first" in response
        assert "before loading the setup tool" in response
    else:
        assert "Every matching YAML must pass board_validate first" in response
        assert "ready_for_code=true" in response

    if definition.permission_mode is PermissionMode.NONE:
        assert "Omit user_permission entirely from a populated plan" in response
    elif definition.permission_mode is PermissionMode.REQUIRED:
        assert "Conversation is never permission" in response
        assert "one-time" in response and "full-session" in response
    else:
        assert "TWO-PHASE APPROVAL" in response
        assert "Full-session and prior approval never cover mass erase" in response


@pytest.mark.parametrize(
    ("action_name", "required_phrases"),
    [
        (
            "board_setup",
            (
                "paired board_fix_setup",
                "first routing plan",
                "before any hardware attempt",
                "matching board-name YAML",
                "board_validate only",
                "hardware gate is stamped",
                "hidden setup tools",
                "authoritative datasheet",
                "exact MCU part number",
            ),
        ),
        ("connect_override", ("normal connect", "never rewrite a profile", "probe unique ID")),
        ("write_cpu_register", ("R0-R12", "read_cpu_register", "ordinary-register class")),
        ("set_execution_state", ("PRIMASK", "reset_and_run", "Ask the user plainly")),
        ("read_memory_address", ("Prefer symbol access", "64 KiB", "clear-on-read")),
        ("write_memory", ("Try a symbol first", "RAM-only", "allow_address_fallback")),
        ("set_breakpoint", ("escalation, not the first move", "remove_breakpoint", "tagged UART")),
        ("flash_application", ("erase sector", "vector table", "board_safety_refresh")),
        ("flash_bootloader", ("bootloader partition", "recoverable over SWD", "permission")),
        ("register_write", ("reference-manual", "SVD", "write-1-to-clear")),
        ("reset_and_halt", ("reset_and_run is always available", "not an unlock", "silently halted")),
        ("connect_under_reset", ("wired, supported reset line", "not an unlock", "normal-attach failure")),
        ("target_unlock", ("server-confirmed locked target", "all ranges/banks/sectors", "gate stays closed")),
        ("read_serial", ("You cannot see the board. Prints are your eyes.", "uart_debug_prints", "zero hits")),
        (
            "serial_exchange",
            (
                "one or more ordered console commands",
                "one handle",
                "Stop on the first missing or mismatched response",
            ),
        ),
        ("write_serial", ("You cannot see the board. Prints are your eyes.", "different command requires", "zero hits")),
    ],
)
def test_each_null_prompt_contains_its_required_product_guidance(
    action_name: str,
    required_phrases: tuple[str, ...],
) -> None:
    response = PLAN_DEFINITIONS[action_name].render_null_response()
    for phrase in required_phrases:
        assert phrase.casefold() in response.casefold()


def _read_serial_plan() -> dict[str, object]:
    return {
        "board_id": "board_a",
        "hypothesis": "The captured UART window will distinguish the two suspected paths.",
        "strategy": "Capture once, compare the tagged output, and stop or revise on mismatch.",
        "hypothesis_made": True,
        "strategy_evaluated": True,
        "expected_fail_return": "No tagged output appears in the bounded capture.",
        "expected_success_return": "The expected tagged path appears in order.",
        "max_calls": 1,
        "max_calls_buffer": 0,
        "action_parameters": {
            "expected_text": None,
            "read_seconds": 3.0,
            "baudrate": None,
            "port": None,
            "reset_on_open": False,
            "on_exit": None,
        },
    }


def test_plan_engine_accepts_only_exact_nested_plan_json_and_preserves_active_plan() -> None:
    registry = ToolRegistry()
    registry.register("read_serial", hidden=True, locked=True, prerequisite="read_serial-plan")
    engine = PlanEngine(ServerRun(run_id="nested-plan-json"), registry)
    definition = PLAN_DEFINITIONS["read_serial"]
    engine.submit(
        definition.plan_tool_name,
        {name: None for name in definition.null_field_names},
    )
    accepted = engine.submit(definition.plan_tool_name, _read_serial_plan())
    assert accepted.plan is not None
    original_id = accepted.plan.plan_id

    flattened = dict(_read_serial_plan())
    parameters = flattened.pop("action_parameters")
    assert isinstance(parameters, dict)
    flattened.update(parameters)
    with pytest.raises(PlanRefusal) as flat_error:
        engine.submit(definition.plan_tool_name, flattened)
    assert flat_error.value.code == "plan/incomplete"

    for invalid in (
        _read_serial_plan() | {"plan_json": _read_serial_plan()},
        _read_serial_plan() | {"action_parameters": "describe the read in prose"},
        _read_serial_plan() | {"action_parameters": {**parameters, "invented": True}},
        _read_serial_plan() | {"user_permission": None},
    ):
        with pytest.raises(PlanRefusal):
            engine.submit(definition.plan_tool_name, invalid)
        active = engine.active_plan("read_serial", "board_a")
        assert active is not None and active.plan_id == original_id


def test_universal_null_envelope_and_populated_permission_shape_are_distinct() -> None:
    for definition in PLAN_DEFINITIONS.values():
        assert definition.null_field_names == (
            "board_id",
            "hypothesis",
            "strategy",
            "hypothesis_made",
            "strategy_evaluated",
            "expected_fail_return",
            "expected_success_return",
            "max_calls",
            "max_calls_buffer",
            "action_parameters",
            "user_permission",
        )
        assert "action_parameters" in definition.plan_field_names
        assert not ({field.name for field in definition.action_fields} & set(definition.plan_field_names))
        assert ("user_permission" in definition.plan_field_names) is (
            definition.permission_mode is not PermissionMode.NONE
        )


def test_current_human_plan_contract_is_generated_from_every_live_definition() -> None:
    contract = Path("docs/plan-tool-contract.md").read_text(encoding="utf-8")

    assert contract == render_plan_contract_markdown()
    for definition in PLAN_DEFINITIONS.values():
        assert f"## `{definition.plan_tool_name}`" in contract
        assert f"- Action: `{definition.action_name}`" in contract
        assert f"- Budget mode: `{definition.budget_mode.value}`" in contract
        assert f"- Permission mode: `{definition.permission_mode.value}`" in contract
        for field in definition.action_fields:
            assert f"`{field.name}`" in contract


def test_rendered_serial_exchange_example_round_trips_and_invalid_replacements_are_atomic() -> None:
    definition = PLAN_DEFINITIONS["serial_exchange"]
    rendered = definition.render_null_response()
    example_start = rendered.index("{", rendered.index("[EXAMPLE-PLAN]"))
    example, _ = json.JSONDecoder().raw_decode(rendered[example_start:])
    assert example["max_calls"] == 1
    assert example["max_calls_buffer"] == 0
    assert [step["text"] for step in example["action_parameters"]["steps"]] == [
        "blink on",
        "blink status",
        "blink off",
    ]
    registry = ToolRegistry()
    registry.register(
        "serial_exchange",
        hidden=True,
        locked=True,
        prerequisite="serial_exchange-plan",
    )
    engine = PlanEngine(ServerRun(run_id="serial-schema-run"), registry)
    engine.submit(
        definition.plan_tool_name,
        {name: None for name in definition.null_field_names},
    )

    accepted = engine.submit(definition.plan_tool_name, example)
    assert accepted.plan is not None
    accepted_id = accepted.plan.plan_id
    action = example["action_parameters"]
    assert isinstance(action, dict)
    steps = action["steps"]
    assert isinstance(steps, list)

    invalid_actions = (
        {**action, "steps": [*steps, *([steps[0]] * 7)]},
        {**action, "steps": [{"text": "on", "invented": True}]},
        {
            **action,
            "ready_probe_text": None,
            "ready_probe_line_ending": "none",
            "ready_probe_delay_seconds": 1.0,
        },
        {**action, "ready_text": None, "ready_seconds": 5.0},
    )
    for invalid_action in invalid_actions:
        with pytest.raises(PlanRefusal, match="Invalid action_parameters"):
            engine.submit(
                definition.plan_tool_name,
                {**example, "action_parameters": invalid_action},
            )
        active = engine.active_plan("serial_exchange", "left_controller")
        assert active is not None and active.plan_id == accepted_id


def test_rendered_setup_example_uses_current_reviewed_automatic_board() -> None:
    definition = PLAN_DEFINITIONS["board_setup"]
    rendered = definition.render_null_response()
    example_start = rendered.index("{", rendered.index("[EXAMPLE-PLAN]"))
    example, _ = json.JSONDecoder().raw_decode(rendered[example_start:])
    action = example["action_parameters"]

    assert action == {
        "mode": "setup",
        "connection_id": "connection_1",
        "display_name": "left controller",
            "mcu_part_number": "nRF52840-QIAA",
            "requires_uart": True,
            "serial_baudrate": 115200,
        "serial_id": "683377322",
        "datasheet_path": "C:/firmware/docs/nRF52840_PS_v1.1.pdf",
    }
    assert example["max_calls"] == 1
    assert example["max_calls_buffer"] == 0
