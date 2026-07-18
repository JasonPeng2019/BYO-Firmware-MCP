from __future__ import annotations

import pytest

from pyocd_debug_mcp.services.uart_exchange_schema import (
    validate_serial_exchange_parameters,
)


def parameters() -> dict[str, object]:
    return {
        "steps": [{"text": "status", "expected_text": "READY", "line_ending": "lf"}],
        "read_seconds": 3.0,
        "baudrate": 115200,
        "port": None,
        "ready_text": "prompt>",
        "ready_seconds": 5.0,
        "ready_probe_text": "",
        "ready_probe_line_ending": "lf",
        "ready_probe_delay_seconds": 1.0,
        "clear_input": False,
    }


def test_newline_only_readiness_probe_is_valid() -> None:
    assert validate_serial_exchange_parameters(parameters()) is None


@pytest.mark.parametrize(
    "mutation",
    [
        {"steps": [{"text": "x", "expected_text": "y", "line_ending": "lf"}] * 9},
        {"steps": [{"text": "x", "expected_text": "y", "invented": "lf"}]},
        {"ready_probe_text": None, "ready_probe_line_ending": "none", "ready_probe_delay_seconds": 1.0},
        {"ready_text": None, "ready_seconds": 1.0},
        {"ready_probe_text": None, "ready_probe_line_ending": "lf", "ready_probe_delay_seconds": 0.0},
    ],
)
def test_malformed_or_internally_inconsistent_exchange_is_rejected(
    mutation: dict[str, object],
) -> None:
    assert validate_serial_exchange_parameters({**parameters(), **mutation}) is not None


def test_hostile_nested_json_types_return_refusals_instead_of_raising() -> None:
    nested = parameters()
    nested["steps"] = [
        {"text": "status", "expected_text": "READY", "line_ending": []}
    ]
    assert "line_ending" in (validate_serial_exchange_parameters(nested) or "")

    probe = parameters()
    probe["ready_probe_line_ending"] = []
    assert "ready_probe_line_ending" in (
        validate_serial_exchange_parameters(probe) or ""
    )
