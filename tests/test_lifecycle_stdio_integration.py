from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import pytest
from mcp.types import LATEST_PROTOCOL_VERSION

PROJECT_ROOT = Path(__file__).parents[1]
FAKE_SERVER = PROJECT_ROOT / "tests" / "fixtures" / "fake_lifecycle_stdio_server.py"
CLEANUP_EVENTS = (
    "stop-io",
    "close-uart",
    "close-session",
    "release-reset",
    "reset-and-run",
)


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _wait_for_row(path: Path, event: str, label: str, timeout: float = 3.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for row in _rows(path):
            if row.get("event") == event and row.get("label") == label:
                return row
        time.sleep(0.01)
    raise AssertionError(f"did not observe {event!r} for {label!r}")


def _events(path: Path, label: str) -> list[str]:
    return [str(row["event"]) for row in _rows(path) if row.get("label") == label]


def _budget_count(path: Path, label: str) -> int:
    return sum(
        row.get("event") == "budget-consumed" and row.get("label") == label
        for row in _rows(path)
    )


def _assert_tool_error(response: dict[str, Any], text: str) -> None:
    result = response.get("result")
    assert isinstance(result, dict)
    assert result.get("isError") is True
    assert text in json.dumps(result)


class StdioMCPProcess(AbstractContextManager["StdioMCPProcess"]):
    def __init__(self, log_path: Path) -> None:
        environment = dict(os.environ)
        environment["LIFECYCLE_FAKE_LOG"] = str(log_path)
        self.log_path = log_path
        self.process = subprocess.Popen(
            [sys.executable, str(FAKE_SERVER)],
            cwd=PROJECT_ROOT,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self._write = self.process.stdin
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._pending: dict[int, dict[str, Any]] = {}
        self._send_lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        self.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": LATEST_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "task17-stdio-test", "version": "1"},
                },
            }
        )
        initialized = self.response(1)
        assert "result" in initialized
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                self._messages.put(json.loads(line))
            except json.JSONDecodeError:
                continue

    def send(self, message: dict[str, Any]) -> None:
        with self._send_lock:
            self._write.write(json.dumps(message, separators=(",", ":")) + "\n")
            self._write.flush()

    def call(self, request_id: int, tool_name: str, arguments: dict[str, Any]) -> None:
        self.send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            }
        )

    def cancel(self, request_id: int) -> None:
        self.send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": request_id, "reason": "Task 17 integration test"},
            }
        )

    def response(self, request_id: int, timeout: float = 4.0) -> dict[str, Any]:
        existing = self._pending.pop(request_id, None)
        if existing is not None:
            return existing
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                message = self._messages.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            message_id = message.get("id")
            if isinstance(message_id, int):
                if message_id == request_id:
                    return message
                self._pending[message_id] = message
        stderr = ""
        if self.process.poll() is not None and self.process.stderr is not None:
            stderr = self.process.stderr.read()
        raise AssertionError(f"no response for request {request_id}; stderr={stderr}")

    def close_stdin(self) -> None:
        if not self._write.closed:
            self._write.close()

    def __exit__(self, *exc_info: object) -> None:
        self.close_stdin()
        try:
            self.process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2.0)


@pytest.fixture
def stdio_server(tmp_path: Path) -> Iterator[StdioMCPProcess]:
    with StdioMCPProcess(tmp_path / "lifecycle.jsonl") as server:
        yield server


def _slow_args(
    label: str,
    *,
    board_id: str = "board_a",
    duration: float = 0.5,
    timeout: float = 2.0,
    fail: bool = False,
) -> dict[str, Any]:
    return {
        "board_id": board_id,
        "label": label,
        "duration_seconds": duration,
        "operation_timeout_seconds": timeout,
        "fail": fail,
    }


def test_client_eof_mid_operation_cleans_every_resource_and_releases_lock(
    tmp_path: Path,
) -> None:
    server = StdioMCPProcess(tmp_path / "eof.jsonl")
    server.call(10, "slow_read", _slow_args("eof", duration=5.0))
    _wait_for_row(server.log_path, "handler-start", "eof")
    server.close_stdin()
    assert server.process.wait(timeout=5.0) == 0

    events = _events(server.log_path, "eof")
    assert list(event for event in events if event in CLEANUP_EVENTS) == list(CLEANUP_EVENTS)
    assert _budget_count(server.log_path, "eof") == 1
    assert "handler-complete" not in events


def test_real_mcp_cancellation_cleans_then_same_board_resources_are_reusable(
    stdio_server: StdioMCPProcess,
) -> None:
    stdio_server.call(10, "slow_read", _slow_args("cancel"))
    _wait_for_row(stdio_server.log_path, "handler-start", "cancel")
    stdio_server.cancel(10)
    cancelled = stdio_server.response(10)
    assert cancelled["error"]["message"] == "Request cancelled"
    _wait_for_row(stdio_server.log_path, "reset-and-run", "cancel")

    stdio_server.call(
        11,
        "quick",
        {"board_id": "board_a", "label": "reuse", "operation_timeout_seconds": 1.0},
    )
    reused = stdio_server.response(11)
    assert "resources-reused:board_a:reuse" in json.dumps(reused)
    _wait_for_row(stdio_server.log_path, "reset-and-run", "cancel")
    _wait_for_row(stdio_server.log_path, "handler-start", "reuse")
    all_rows = _rows(stdio_server.log_path)
    cancel_cleanup_index = next(
        index
        for index, row in enumerate(all_rows)
        if row.get("event") == "reset-and-run" and row.get("label") == "cancel"
    )
    reuse_start_index = next(
        index
        for index, row in enumerate(all_rows)
        if row.get("event") == "handler-start" and row.get("label") == "reuse"
    )
    assert cancel_cleanup_index < reuse_start_index
    cancel_events = [
        event for event in _events(stdio_server.log_path, "cancel") if event in CLEANUP_EVENTS
    ]
    assert cancel_events == list(CLEANUP_EVENTS)
    assert _budget_count(stdio_server.log_path, "cancel") == 1
    assert _budget_count(stdio_server.log_path, "reuse") == 1


def test_cancellation_during_flash_finishes_transaction_before_cleanup(
    stdio_server: StdioMCPProcess,
) -> None:
    stdio_server.call(
        20,
        "flash_application",
        {
            "board_id": "board_a",
            "label": "flash-cancel",
            "duration_seconds": 0.3,
            "operation_timeout_seconds": 2.0,
        },
    )
    _wait_for_row(stdio_server.log_path, "handler-start", "flash-cancel")
    stdio_server.cancel(20)
    cancelled = stdio_server.response(20)
    assert cancelled["error"]["message"] == "Request cancelled"
    _wait_for_row(stdio_server.log_path, "close-session", "flash-cancel")
    events = _events(stdio_server.log_path, "flash-cancel")
    assert events.index("flash-complete") < events.index("stop-io")
    assert _budget_count(stdio_server.log_path, "flash-cancel") == 1


def test_timeout_failure_and_repeated_cleanup_have_one_start_budget_and_parity(
    stdio_server: StdioMCPProcess,
) -> None:
    stdio_server.call(30, "slow_read", _slow_args("timeout", duration=1.0, timeout=0.08))
    timeout_response = stdio_server.response(30)
    _assert_tool_error(timeout_response, "operation timeout")
    _wait_for_row(stdio_server.log_path, "reset-and-run", "timeout")

    stdio_server.call(31, "slow_read", _slow_args("failure", duration=0.01, fail=True))
    failure_response = stdio_server.response(31)
    _assert_tool_error(failure_response, "fake backend failure")
    _wait_for_row(stdio_server.log_path, "reset-and-run", "failure")

    stdio_server.call(
        32,
        "cleanup_twice",
        {"board_id": "board_a", "label": "repeat", "operation_timeout_seconds": 1.0},
    )
    assert "result" in stdio_server.response(32)
    _wait_for_row(stdio_server.log_path, "reset-and-run", "repeat")

    for label in ("timeout", "failure", "repeat"):
        assert _budget_count(stdio_server.log_path, label) == 1
        cleanup = [event for event in _events(stdio_server.log_path, label) if event in CLEANUP_EVENTS]
        assert cleanup == list(CLEANUP_EVENTS)


def test_same_board_busy_does_not_burn_budget_while_other_board_is_independent(
    stdio_server: StdioMCPProcess,
) -> None:
    stdio_server.call(40, "slow_read", _slow_args("board-a-active", duration=0.35))
    _wait_for_row(stdio_server.log_path, "handler-start", "board-a-active")
    stdio_server.call(
        41,
        "slow_read",
        _slow_args("board-a-busy", duration=0.01, timeout=0.06),
    )
    stdio_server.call(
        42,
        "slow_read",
        _slow_args("board-b", board_id="board_b", duration=0.02, timeout=1.0),
    )

    board_b = stdio_server.response(42)
    assert "completed:board_b:board-b" in json.dumps(board_b)
    busy = stdio_server.response(41)
    _assert_tool_error(busy, "Board 'board_a' is busy")
    assert "result" in stdio_server.response(40)
    board_b_complete = _wait_for_row(stdio_server.log_path, "handler-complete", "board-b")
    board_a_complete = _wait_for_row(
        stdio_server.log_path, "handler-complete", "board-a-active"
    )
    assert board_b_complete["monotonic"] < board_a_complete["monotonic"]
    assert _budget_count(stdio_server.log_path, "board-a-active") == 1
    assert _budget_count(stdio_server.log_path, "board-a-busy") == 0
    assert _budget_count(stdio_server.log_path, "board-b") == 1


def test_intentional_halt_suppresses_a15_reset_while_ordinary_success_restores_it(
    stdio_server: StdioMCPProcess,
) -> None:
    stdio_server.call(
        50,
        "halt",
        {"board_id": "board_a", "label": "halted", "operation_timeout_seconds": 1.0},
    )
    assert "result" in stdio_server.response(50)
    _wait_for_row(stdio_server.log_path, "release-reset", "halted")
    assert "reset-and-run" not in _events(stdio_server.log_path, "halted")
    assert _budget_count(stdio_server.log_path, "halted") == 1

    stdio_server.call(
        51,
        "quick",
        {"board_id": "board_a", "label": "ordinary", "operation_timeout_seconds": 1.0},
    )
    assert "result" in stdio_server.response(51)
    _wait_for_row(stdio_server.log_path, "reset-and-run", "ordinary")
    assert _budget_count(stdio_server.log_path, "ordinary") == 1
    assert _events(stdio_server.log_path, "ordinary").index("release-reset") < _events(
        stdio_server.log_path, "ordinary"
    ).index("reset-and-run")
