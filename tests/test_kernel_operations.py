from __future__ import annotations

import threading
from contextlib import contextmanager

import pytest

from pyocd_debug_mcp.kernel.operations import (
    BATCH_TIMEOUT_GRACE_SECONDS,
    DEFAULT_OPERATION_TIMEOUT_SECONDS,
    FLASH_OPERATION_TIMEOUT_SECONDS,
    VALIDATION_OPERATION_TIMEOUT_SECONDS,
    OperationTimeoutError,
    dispatch,
    operation_timeout_seconds,
)


async def test_dispatch_runs_synchronous_operation_off_event_loop_thread() -> None:
    event_loop_thread = threading.get_ident()

    worker_thread = await dispatch(
        "read_memory",
        None,
        threading.get_ident,
        timeout=1.0,
    )

    assert worker_thread != event_loop_thread


async def test_dispatch_bounds_synchronous_operation() -> None:
    started = threading.Event()
    release = threading.Event()

    def blocked_operation() -> str:
        started.set()
        release.wait(timeout=2.0)
        return "late"

    try:
        with pytest.raises(OperationTimeoutError, match="slow_tool.*0.05s"):
            await dispatch("slow_tool", "board_a", blocked_operation, timeout=0.05)
        assert started.is_set()
    finally:
        release.set()


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("inf"), float("nan")])
async def test_dispatch_rejects_non_finite_or_non_positive_timeout(timeout: float) -> None:
    with pytest.raises(ValueError, match="positive finite"):
        await dispatch("tool", None, lambda: "unused", timeout=timeout)


def test_operation_timeout_budgets_are_finite_and_preserve_serial_parameters() -> None:
    assert operation_timeout_seconds("get_state") == DEFAULT_OPERATION_TIMEOUT_SECONDS
    assert operation_timeout_seconds("flash_firmware") == FLASH_OPERATION_TIMEOUT_SECONDS
    assert operation_timeout_seconds("flash_application") == FLASH_OPERATION_TIMEOUT_SECONDS
    assert operation_timeout_seconds("flash_bootloader") == FLASH_OPERATION_TIMEOUT_SECONDS
    assert operation_timeout_seconds("board_validate") == VALIDATION_OPERATION_TIMEOUT_SECONDS
    assert operation_timeout_seconds("read_serial", {"read_seconds": 40.0}) == 45.0
    assert operation_timeout_seconds("write_serial", {"timeout_seconds": 50.0}) == 55.0
    assert operation_timeout_seconds("read_serial", {"read_seconds": -1.0}) == 30.0


def test_batch_outer_bound_allows_each_child_its_direct_timeout() -> None:
    timeout = operation_timeout_seconds(
        "action_batch",
        {
            "actions": [
                {"tool_name": "get_state", "arguments": {"board_id": "board_a"}},
                {
                    "tool_name": "flash_application",
                    "arguments": {"board_id": "board_a", "artifact": "firmware.hex"},
                },
            ]
        },
    )

    assert timeout == (
        DEFAULT_OPERATION_TIMEOUT_SECONDS
        + FLASH_OPERATION_TIMEOUT_SECONDS
        + BATCH_TIMEOUT_GRACE_SECONDS
    )


async def test_guard_and_handler_share_the_execution_lock_in_order() -> None:
    order: list[str] = []

    @contextmanager
    def execution_lock():
        order.append("lock-enter")
        try:
            yield
        finally:
            order.append("lock-exit")

    def before_execution() -> None:
        order.append("checklist-and-budget")

    def operation() -> str:
        order.append("handler")
        return "done"

    result = await dispatch(
        "guarded",
        "board_a",
        operation,
        timeout=1.0,
        before_execution=before_execution,
        execution_lock=execution_lock(),
    )

    assert result == "done"
    assert order == ["lock-enter", "checklist-and-budget", "handler", "lock-exit"]


async def test_pre_execution_refusal_never_starts_handler() -> None:
    started = False

    def refuse() -> None:
        raise RuntimeError("policy refusal")

    def operation() -> str:
        nonlocal started
        started = True
        return "unexpected"

    with pytest.raises(RuntimeError, match="policy refusal"):
        await dispatch(
            "guarded",
            "board_a",
            operation,
            timeout=1.0,
            before_execution=refuse,
        )
    assert started is False
