"""Subprocess-only MCP server used by Task 17 lifecycle integration tests."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Mapping
from pathlib import Path

from pyocd_debug_mcp.kernel.operations import (
    cancellation_checkpoint,
    operation_resources,
)
from pyocd_debug_mcp.kernel.registry import RegistryFastMCP

LOG_PATH = Path(os.environ["LIFECYCLE_FAKE_LOG"])
_LOG_LOCK = threading.Lock()
_BOARD_LOCKS: dict[str, threading.RLock] = {}


def record(event: str, **details: object) -> None:
    row = {"event": event, "monotonic": time.monotonic(), **details}
    with _LOG_LOCK:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, sort_keys=True) + "\n")


def timeout_resolver(_name: str, arguments: Mapping[str, object] | None) -> float:
    value = (arguments or {}).get("operation_timeout_seconds", 2.0)
    return float(value) if isinstance(value, int | float) else 2.0


server = RegistryFastMCP("task17-fake-lifecycle", timeout_resolver=timeout_resolver)


def bind_resources(board_id: str, label: str) -> None:
    resources = operation_resources()
    record("resources-opened", board_id=board_id, label=label)
    resources.stop_io.append(lambda: record("stop-io", board_id=board_id, label=label))
    resources.close_uart.append(lambda: record("close-uart", board_id=board_id, label=label))
    resources.close_debug.append(
        lambda: record("close-session", board_id=board_id, label=label)
    )
    resources.release_reset.append(
        lambda: record("release-reset", board_id=board_id, label=label)
    )
    resources.restore_final_state.append(
        lambda: record("reset-and-run", board_id=board_id, label=label)
    )


def run_for(board_id: str, label: str, duration_seconds: float, *, fail: bool = False) -> str:
    bind_resources(board_id, label)
    record("handler-start", board_id=board_id, label=label)
    deadline = time.monotonic() + duration_seconds
    while time.monotonic() < deadline:
        cancellation_checkpoint()
        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
    if fail:
        record("handler-failure", board_id=board_id, label=label)
        raise RuntimeError(f"fake backend failure: {label}")
    record("handler-complete", board_id=board_id, label=label)
    return f"completed:{board_id}:{label}"


@server.tool()
def slow_read(
    board_id: str,
    label: str,
    duration_seconds: float,
    operation_timeout_seconds: float = 2.0,
    fail: bool = False,
) -> str:
    """Run interruptible fake board I/O."""

    del operation_timeout_seconds
    return run_for(board_id, label, duration_seconds, fail=fail)


@server.tool(name="flash_application")
def flash_application(
    board_id: str,
    label: str,
    duration_seconds: float,
    operation_timeout_seconds: float = 2.0,
) -> str:
    """Run a fake non-interruptible flash transaction."""

    del operation_timeout_seconds
    bind_resources(board_id, label)
    record("handler-start", board_id=board_id, label=label)
    deadline = time.monotonic() + duration_seconds
    chunks = 0
    while time.monotonic() < deadline:
        cancellation_checkpoint()
        chunks += 1
        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
    record("flash-complete", board_id=board_id, label=label, chunks=chunks)
    return f"flashed:{board_id}:{label}:{chunks}"


@server.tool()
def quick(
    board_id: str,
    label: str,
    operation_timeout_seconds: float = 2.0,
) -> str:
    """Prove that board resources and the lock are reusable."""

    del operation_timeout_seconds
    bind_resources(board_id, label)
    record("handler-start", board_id=board_id, label=label)
    record("handler-complete", board_id=board_id, label=label)
    return f"resources-reused:{board_id}:{label}"


@server.tool()
def halt(
    board_id: str,
    label: str,
    operation_timeout_seconds: float = 2.0,
) -> str:
    """Intentionally leave the fake board halted."""

    del operation_timeout_seconds
    bind_resources(board_id, label)
    record("handler-start", board_id=board_id, label=label)
    record("intentional-halt", board_id=board_id, label=label)
    return f"halted:{board_id}:{label}"


@server.tool()
def cleanup_twice(
    board_id: str,
    label: str,
    operation_timeout_seconds: float = 2.0,
) -> str:
    """Invoke cleanup repeatedly to prove finalization is idempotent."""

    del operation_timeout_seconds
    bind_resources(board_id, label)
    resources = operation_resources()
    resources.cleanup(preserve_halt=False)
    resources.cleanup(preserve_halt=False)
    return f"cleaned:{board_id}:{label}"


def budget_guard(name: str, board_id: str, arguments: Mapping[str, object]) -> None:
    record(
        "budget-consumed",
        tool_name=name,
        board_id=board_id,
        label=str(arguments.get("label", "")),
    )


for guarded_name in ("slow_read", "flash_application", "quick", "halt", "cleanup_twice"):
    server.configure_guarded_dispatch(
        guarded_name,
        guard=budget_guard,
        lock_for_board=lambda board_id: _BOARD_LOCKS.setdefault(board_id, threading.RLock()),
    )


if __name__ == "__main__":
    record("server-start")
    try:
        server.run()
    finally:
        record("server-stop")
