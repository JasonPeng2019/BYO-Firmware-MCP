from __future__ import annotations

from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from pyocd_debug_mcp.kernel.finalizers import (
    FinalizerValidationError,
    OnExitFinalizer,
    build_finalizer,
    parse_finalizer,
)
from pyocd_debug_mcp.kernel.operations import OperationManager, dispatch, operation_resources


@pytest.mark.parametrize(
    "hostile",
    [
        "rm -rf /",
        {"action": "shell", "command": "whoami"},
        {"action": "uart_write", "text": "x", "command": "whoami"},
        {"action": "reset_and_run", "extra": True},
    ],
)
def test_hostile_or_arbitrary_finalizers_are_rejected(hostile: object) -> None:
    with pytest.raises(FinalizerValidationError):
        parse_finalizer("read_serial", hostile)


def test_finalizer_is_rejected_for_ineligible_tool() -> None:
    with pytest.raises(FinalizerValidationError, match="does not accept"):
        parse_finalizer("flash_application", {"action": "reset_and_run"})


def test_public_finalizer_schema_is_discriminated_and_forbids_extra_fields() -> None:
    schema = TypeAdapter(OnExitFinalizer).json_schema()
    assert schema["discriminator"]["mapping"] == {
        "reset_and_run": "#/$defs/ResetAndRunFinalizer",
        "uart_write": "#/$defs/UARTWriteFinalizer",
    }
    with pytest.raises(ValidationError):
        TypeAdapter(OnExitFinalizer).validate_python(
            {"action": "uart_write", "text": "ok", "shell": "cmd"}
        )


def test_structured_finalizers_bind_only_documented_arguments() -> None:
    calls: list[tuple[Any, ...]] = []
    finalizer = build_finalizer(
        "read_serial",
        "board_a",
        {"action": "uart_write", "text": "status\n", "timeout_seconds": 0.5},
        uart_write=lambda *args: calls.append(args),
        reset_and_run=lambda board: calls.append(("reset", board)),
    )
    assert finalizer is not None
    finalizer()
    assert calls == [("board_a", "status\n", 0.5)]


async def test_failing_finalizer_precedes_and_never_blocks_mandatory_cleanup() -> None:
    order: list[str] = []

    def operation() -> str:
        operation_resources().stop_io.append(lambda: order.append("mandatory-cleanup"))
        return "done"

    def failing_finalizer() -> None:
        order.append("finalizer")
        raise RuntimeError("expected finalizer failure")

    result = await dispatch(
        "read_serial",
        "board_a",
        operation,
        1.0,
        finalizer=failing_finalizer,
        manager=OperationManager(),
    )
    assert result == "done"
    assert order == ["finalizer", "mandatory-cleanup"]


async def test_preexecution_refusal_never_runs_a_finalizer() -> None:
    called = False

    def finalizer() -> None:
        nonlocal called
        called = True

    with pytest.raises(RuntimeError, match="refused"):
        await dispatch(
            "read_serial",
            "board_a",
            lambda: "unused",
            1.0,
            before_execution=lambda: (_ for _ in ()).throw(RuntimeError("refused")),
            finalizer=finalizer,
            manager=OperationManager(),
        )
    assert called is False
