from __future__ import annotations

from dataclasses import replace

import pytest

from pyocd_debug_mcp.firmstore.cache import CacheResolution
from pyocd_debug_mcp.setup_flow.preflight import (
    NO_INTERNALS_RELAY_INSTRUCTION,
    BuildConfiguration,
    PreflightEngine,
    PreflightInventory,
    PreflightSelections,
    ProbeCandidate,
    SerialCandidate,
    SetupUserInput,
)


USER_INPUT = SetupUserInput(
    board_id="bench_board",
    connection_id="probe:001",
    display_name="Bench Board",
    mcu_part_number="STM32L476RGT6",
    serial_baudrate=115200,
)
PROBE_A = ProbeCandidate("probe-a", "ST-Link on the left board", "stlink", "PROBE-001")
PROBE_B = ProbeCandidate("probe-b", "J-Link on the right board", "jlink", "PROBE-002")
UART_A = SerialCandidate(
    "uart-a", "COM3", "ST-Link virtual serial port", "UART-001", 0x0483, 0x5740
)
UART_B = SerialCandidate(
    "uart-b", "COM9", "USB serial adapter", "UART-002", 0x10C4, 0xEA60
)
BUILD_A = BuildConfiguration("debug", "Debug firmware", ("build/debug/app.elf",))
BUILD_B = BuildConfiguration("release", "Release firmware", ("build/release/app.elf",))


def complete_inventory() -> PreflightInventory:
    return PreflightInventory(
        probes=(PROBE_A,),
        serial_ports=(UART_A,),
        cache_resolution=CacheResolution(False, "no_record"),
        built_in_targets=("stm32l476rgtx",),
        exact_detected_targets=("stm32l476rgtx",),
        build_configurations=(BUILD_A,),
    )


@pytest.mark.parametrize(
    ("inventory", "expected_status", "expected_code", "research"),
    [
        (
            replace(complete_inventory(), probes=()),
            "setup_blocked",
            "setup/no-probe",
            False,
        ),
        (
            replace(complete_inventory(), serial_ports=()),
            "setup_blocked",
            "setup/no-uart",
            False,
        ),
        (
            replace(complete_inventory(), probes=(PROBE_B, PROBE_A)),
            "setup_needs_user_input",
            "setup/probe-selection-required",
            False,
        ),
        (
            replace(complete_inventory(), serial_ports=(UART_B, UART_A)),
            "setup_needs_user_input",
            "setup/serial-selection-required",
            False,
        ),
        (
            replace(complete_inventory(), build_configurations=(BUILD_B, BUILD_A)),
            "setup_needs_user_input",
            "setup/build-selection-required",
            False,
        ),
        (
            replace(complete_inventory(), exact_detected_targets=()),
            "setup_research_required",
            "setup/no-exact-target",
            True,
        ),
        (
            replace(
                complete_inventory(),
                exact_detected_targets=("stm32l476rg", "stm32l476rgtx"),
            ),
            "setup_research_required",
            "setup/ambiguous-exact-target",
            True,
        ),
    ],
)
def test_every_preflight_terminal_row_is_deterministic(
    inventory: PreflightInventory,
    expected_status: str,
    expected_code: str,
    research: bool,
) -> None:
    result = PreflightEngine().evaluate(USER_INPUT, inventory)

    assert result.status == expected_status
    assert result.code == expected_code
    assert result.research_required is research
    assert NO_INTERNALS_RELAY_INSTRUCTION in result.agent_prompt
    if expected_code in {"setup/no-probe", "setup/no-uart"}:
        assert "research" in result.agent_prompt.casefold()


def test_multiple_probe_choice_is_friendly_sorted_and_never_guessed() -> None:
    inventory = replace(complete_inventory(), probes=(PROBE_B, PROBE_A))

    waiting = PreflightEngine().evaluate(USER_INPUT, inventory)
    selected = PreflightEngine().evaluate(
        USER_INPUT,
        inventory,
        PreflightSelections(probe_id="probe-b"),
    )

    assert [choice.choice_id for choice in waiting.choices] == ["probe-a", "probe-b"]
    assert all("identifier ending" in choice.label for choice in waiting.choices)
    assert selected.status == "preflight_ready"
    assert selected.selected_probe == PROBE_B


def test_multiple_serial_ports_reuse_only_one_exact_cache_match() -> None:
    inventory = replace(
        complete_inventory(),
        serial_ports=(UART_A, UART_B),
        cache_resolution=CacheResolution(True, "exact_match", "COM9"),
    )

    result = PreflightEngine().evaluate(USER_INPUT, inventory)

    assert result.status == "preflight_ready"
    assert result.selected_serial == UART_B


def test_external_adapter_requires_confirmation_then_requests_cache_recording() -> None:
    external = replace(UART_B, external_adapter=True, provably_mapped=False)
    inventory = replace(complete_inventory(), serial_ports=(external,))

    waiting = PreflightEngine().evaluate(USER_INPUT, inventory)
    confirmed = PreflightEngine().evaluate(
        USER_INPUT,
        inventory,
        PreflightSelections(external_adapter_confirmed=True),
    )

    assert waiting.status == "setup_needs_user_input"
    assert waiting.code == "setup/external-adapter-confirmation-required"
    assert confirmed.status == "preflight_ready"
    assert confirmed.cache_confirmation_required is True


def test_build_selection_precedes_target_research() -> None:
    inventory = replace(
        complete_inventory(),
        build_configurations=(BUILD_B, BUILD_A),
        exact_detected_targets=(),
    )

    choice = PreflightEngine().evaluate(USER_INPUT, inventory)
    research = PreflightEngine().evaluate(
        USER_INPUT,
        inventory,
        PreflightSelections(build_configuration_id="release"),
    )

    assert choice.code == "setup/build-selection-required"
    assert choice.research_required is False
    assert research.code == "setup/no-exact-target"
    assert research.selected_build == BUILD_B


def test_exact_user_part_number_is_preserved_in_preflight_evidence() -> None:
    exact = SetupUserInput(
        board_id="bench_board",
        connection_id="probe:001",
        display_name="Bénch Board",
        mcu_part_number="  STM32L476RGT6-rev A  ",
        serial_baudrate=115200,
    )

    result = PreflightEngine().evaluate(exact, complete_inventory())

    assert result.observed["user_input"]["mcu_part_number"] == "  STM32L476RGT6-rev A  "


def test_every_preflight_prompt_and_choice_is_plain_prose_with_relay_guard() -> None:
    external = replace(UART_B, external_adapter=True, provably_mapped=False)
    inventories = (
        replace(complete_inventory(), probes=()),
        replace(complete_inventory(), serial_ports=()),
        replace(complete_inventory(), probes=(PROBE_A, PROBE_B)),
        replace(complete_inventory(), serial_ports=(UART_A, UART_B)),
        replace(complete_inventory(), serial_ports=(external,)),
        replace(complete_inventory(), build_configurations=(BUILD_A, BUILD_B)),
        replace(complete_inventory(), exact_detected_targets=()),
        complete_inventory(),
    )

    for current in inventories:
        result = PreflightEngine().evaluate(USER_INPUT, current)
        prompt = result.agent_prompt
        assert NO_INTERNALS_RELAY_INSTRUCTION in prompt
        assert prompt.strip() == prompt
        assert not prompt.startswith(("{", "["))
        assert "continuation_id" not in prompt
        assert "choice_id" not in prompt
        assert "\\n{" not in prompt
        for choice in result.choices:
            assert choice.label.strip() == choice.label
            assert choice.description.strip() == choice.description
            assert not choice.label.startswith(("{", "["))
            assert choice.choice_id not in choice.label
