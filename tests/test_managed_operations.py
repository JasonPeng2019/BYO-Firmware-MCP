from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from pyocd_debug_mcp.kernel.operations import (
    BoardBusyError,
    OperationCancelledError,
    OperationManager,
    OperationTimeoutError,
    cancellation_checkpoint,
    current_operation,
    dispatch,
    operation_resources,
    start_owned_subprocess,
)


def _wait_until(predicate, timeout: float = 1.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true before its deadline")


async def test_managed_operation_tracks_request_and_cooperatively_cancels() -> None:
    manager = OperationManager()
    started = threading.Event()
    cleaned = threading.Event()

    def slow_read() -> str:
        operation_resources().stop_io.append(cleaned.set)
        started.set()
        while True:
            cancellation_checkpoint()
            time.sleep(0.01)

    task = asyncio.create_task(
        dispatch(
            "read_memory_address",
            "board_a",
            slow_read,
            2.0,
            request_id="request-7",
            manager=manager,
        )
    )
    await asyncio.to_thread(started.wait, 1.0)
    snapshots = manager.snapshots()
    assert len(snapshots) == 1
    assert snapshots[0].request_id == "request-7"
    assert snapshots[0].board_id == "board_a"
    assert snapshots[0].owned_resource_count == 1

    assert manager.cancel_request("request-7") == 1
    with pytest.raises(OperationCancelledError):
        await task
    assert cleaned.wait(1.0)
    assert manager.snapshots() == ()


async def test_async_task_cancellation_uses_the_same_cleanup_path() -> None:
    manager = OperationManager()
    started = threading.Event()
    cleanup_order: list[str] = []

    def slow_read() -> str:
        resources = operation_resources()
        resources.stop_io.append(lambda: cleanup_order.append("stop-io"))
        resources.close_uart.append(lambda: cleanup_order.append("close-uart"))
        resources.close_debug.append(lambda: cleanup_order.append("close-pyocd"))
        resources.release_reset.append(lambda: cleanup_order.append("release-reset"))
        resources.restore_final_state.append(lambda: cleanup_order.append("reset-and-run"))
        started.set()
        while True:
            cancellation_checkpoint()
            time.sleep(0.01)

    task = asyncio.create_task(
        dispatch("read_memory_address", "board_a", slow_read, 2.0, manager=manager)
    )
    await asyncio.to_thread(started.wait, 1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    _wait_until(lambda: len(cleanup_order) == 5)
    assert cleanup_order == [
        "stop-io",
        "close-uart",
        "close-pyocd",
        "release-reset",
        "reset-and-run",
    ]
    assert manager.snapshots() == ()


async def test_timeout_cancels_cooperatively_and_cleans_once() -> None:
    manager = OperationManager()
    cleanup_calls = 0

    def slow_operation() -> str:
        nonlocal cleanup_calls

        def clean() -> None:
            nonlocal cleanup_calls
            cleanup_calls += 1

        resources = operation_resources()
        resources.close_debug.extend((clean,))
        # Repeating the same cleanup call is harmless at the operation level.
        started = time.monotonic()
        while time.monotonic() - started < 1.0:
            cancellation_checkpoint()
            time.sleep(0.005)
        return "late"

    with pytest.raises(OperationTimeoutError):
        await dispatch("read_memory_address", "board_a", slow_operation, 0.05, manager=manager)
    _wait_until(lambda: cleanup_calls == 1)
    assert manager.snapshots() == ()


async def test_cleanup_is_idempotent_and_continues_after_a_callback_failure() -> None:
    manager = OperationManager()
    calls: list[str] = []

    def operation() -> str:
        resources = operation_resources()

        def fail() -> None:
            calls.append("failed-close")
            raise RuntimeError("close failed")

        resources.stop_io.append(fail)
        resources.close_uart.append(lambda: calls.append("uart-closed"))
        resources.release_reset.append(lambda: calls.append("reset-released"))
        resources.restore_final_state.append(lambda: calls.append("reset-and-run"))
        resources.cleanup(preserve_halt=False)
        resources.cleanup(preserve_halt=False)
        return "done"

    assert await dispatch("get_state", "board_a", operation, 1.0, manager=manager) == "done"
    assert calls == ["failed-close", "uart-closed", "reset-released", "reset-and-run"]


async def test_handler_failure_cleans_and_releases_board_for_the_next_call() -> None:
    manager = OperationManager()
    cleaned = threading.Event()

    def failing_operation() -> str:
        operation_resources().close_debug.append(cleaned.set)
        raise RuntimeError("fake backend failure")

    with pytest.raises(RuntimeError, match="fake backend failure"):
        await dispatch("read_cpu_register", "board_a", failing_operation, 1.0, manager=manager)
    assert cleaned.is_set()
    assert (
        await dispatch(
            "read_cpu_register", "board_a", lambda: "reused", 1.0, manager=manager
        )
        == "reused"
    )


async def test_cancelled_flash_finishes_before_cleanup_and_release() -> None:
    manager = OperationManager()
    started = threading.Event()
    allow_finish = threading.Event()
    events: list[str] = []

    def flash() -> str:
        operation_resources().close_debug.append(lambda: events.append("cleanup"))
        operation = current_operation()
        assert operation is not None
        operation.begin_non_interruptible()
        started.set()
        while not allow_finish.wait(0.01):
            # A flash checkpoint deliberately does not interrupt the transaction.
            cancellation_checkpoint()
        events.append("flash-complete")
        return "flashed"

    task = asyncio.create_task(
        dispatch("flash_application", "board_a", flash, 1.0, manager=manager)
    )
    await asyncio.to_thread(started.wait, 1.0)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()
    assert events == []
    allow_finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == ["flash-complete", "cleanup"]
    assert manager.snapshots() == ()


async def test_timed_out_flash_waits_for_non_interruptible_completion() -> None:
    manager = OperationManager()
    events: list[str] = []

    def flash() -> str:
        operation_resources().close_debug.append(lambda: events.append("cleanup"))
        operation = current_operation()
        assert operation is not None
        operation.begin_non_interruptible()
        time.sleep(0.075)
        events.append("flash-complete")
        return "flashed"

    started = time.monotonic()
    with pytest.raises(OperationTimeoutError):
        await dispatch("flash_application", "board_a", flash, 0.05, manager=manager)

    assert time.monotonic() - started >= 0.07
    assert events == ["flash-complete", "cleanup"]
    assert manager.snapshots() == ()


async def test_linearized_authority_commit_completes_instead_of_reporting_timeout() -> None:
    manager = OperationManager()
    authority_committed = threading.Event()

    def validation() -> str:
        operation = current_operation()
        assert operation is not None
        operation.run_if_not_cancelled(authority_committed.set)
        time.sleep(0.075)
        return "validated"

    result = await dispatch("board_validate", "board_a", validation, 0.05, manager=manager)

    assert result == "validated"
    assert authority_committed.is_set()
    assert manager.snapshots() == ()


async def test_linearized_authority_commit_completes_after_client_cancellation() -> None:
    manager = OperationManager()
    authority_committed = threading.Event()
    allow_finish = threading.Event()

    def validation() -> str:
        operation = current_operation()
        assert operation is not None
        operation.run_if_not_cancelled(authority_committed.set)
        assert allow_finish.wait(timeout=1.0)
        return "validated"

    task = asyncio.create_task(
        dispatch("board_validate", "board_a", validation, 1.0, manager=manager)
    )
    assert await asyncio.to_thread(authority_committed.wait, 1.0)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()
    allow_finish.set()

    assert await task == "validated"
    assert manager.snapshots() == ()


async def test_same_board_reports_busy_without_interleaving_and_cross_board_runs() -> None:
    manager = OperationManager()
    board_a_started = threading.Event()
    release_board_a = threading.Event()
    board_b_started = threading.Event()

    def board_a_operation() -> str:
        board_a_started.set()
        release_board_a.wait(1.0)
        return "a"

    first = asyncio.create_task(
        dispatch("read_cpu_register", "board_a", board_a_operation, 1.0, manager=manager)
    )
    await asyncio.to_thread(board_a_started.wait, 1.0)

    def board_b_operation() -> str:
        board_b_started.set()
        return "b"

    assert (
        await dispatch("read_cpu_register", "board_b", board_b_operation, 0.5, manager=manager)
        == "b"
    )
    assert board_b_started.is_set()

    with pytest.raises(BoardBusyError, match="Board 'board_a' is busy"):
        await dispatch(
            "read_cpu_register", "board_a", lambda: "must-not-run", 0.05, manager=manager
        )
    release_board_a.set()
    assert await first == "a"


@pytest.mark.parametrize("tool_name,preserves_halt", [("halt", True), ("get_state", False)])
async def test_a15_final_state_respects_intentional_halt(
    tool_name: str, preserves_halt: bool
) -> None:
    manager = OperationManager()
    reset_and_run = threading.Event()

    def operation() -> str:
        operation_resources().restore_final_state.append(reset_and_run.set)
        return "done"

    assert await dispatch(tool_name, "board_a", operation, 1.0, manager=manager) == "done"
    assert reset_and_run.is_set() is not preserves_halt


async def test_owned_subprocess_group_is_terminated_on_cancellation(tmp_path: Path) -> None:
    manager = OperationManager()
    pid_path = tmp_path / "fake-backend.pid"
    started = threading.Event()
    process_holder: list[subprocess.Popen[bytes]] = []

    def subprocess_backend() -> str:
        process = start_owned_subprocess(
            [
                sys.executable,
                "-c",
                (
                    "import os,pathlib,time; "
                    f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid())); "
                    "time.sleep(30)"
                ),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        process_holder.append(process)
        started.set()
        while True:
            cancellation_checkpoint()
            time.sleep(0.01)

    task = asyncio.create_task(
        dispatch(
            "read_memory_address",
            "board_a",
            subprocess_backend,
            2.0,
            manager=manager,
        )
    )
    await asyncio.to_thread(started.wait, 1.0)
    _wait_until(pid_path.exists)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    _wait_until(lambda: process_holder[0].poll() is not None)
    assert process_holder[0].returncode is not None
