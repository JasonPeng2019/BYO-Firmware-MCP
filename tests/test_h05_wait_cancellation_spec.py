from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
import unittest

import anyio

from pyocd_debug_mcp.kernel import operations
from pyocd_debug_mcp.kernel.operations import (
    CANCELLATION_CLEANUP_GRACE_SECONDS,
    OperationCancelledError,
    OperationManager,
    dispatch,
    wrap_layer2_response,
)
from pyocd_debug_mcp.services.session_runtime import ToolOutcome
from pyocd_debug_mcp.tools.misc import MiscToolServices, build_misc_handlers


class WaitCancellationSpecTests(unittest.TestCase):
    def _handler(self, events: list[dict[str, object]], sleeps: list[float] | None = None):
        return build_misc_handlers(
            MiscToolServices(
                runtime_for=lambda _board_id: None,
                duration_ms=lambda _started: 0,
                record_event=lambda _tool, _args, **event: events.append(event),
                sleep=(sleeps.append if sleeps is not None else time.sleep),
            )
        )["wait"]

    def test_cl001_direct_wait_preserves_units_success_and_invalid_refusals(self) -> None:
        events: list[dict[str, object]] = []
        sleeps: list[float] = []
        wait = self._handler(events, sleeps)

        self.assertEqual(
            wait("logical-board", 37),
            wrap_layer2_response("Waited 37 ms for board 'logical-board'."),
        )
        self.assertEqual(sleeps, [0.037])
        self.assertEqual([event["outcome_kind"] for event in events], [ToolOutcome.SUCCESS])

        for invalid in (True, "50", 0, -1):
            with self.subTest(ms=invalid):
                before = len(events)
                self.assertEqual(
                    wait("logical-board", invalid),  # type: ignore[arg-type]
                    wrap_layer2_response(
                        "Refused [wait/out-of-range]: ms must be a positive integer."
                    ),
                )
                self.assertEqual(events[-1]["outcome_kind"], ToolOutcome.REFUSED)
                self.assertEqual(events[-1]["error_code"], "wait/out-of-range")
                self.assertEqual(len(events), before + 1)
        self.assertEqual(sleeps, [0.037])

    def test_cl001_cancelled_dispatch_has_no_success_and_unblocks_same_board(self) -> None:
        async def exercise() -> None:
            events: list[dict[str, object]] = []
            manager = OperationManager()
            wait = self._handler(events)
            finished = anyio.Event()
            failure: list[BaseException] = []

            async def cancel_when_running() -> None:
                while not manager.snapshots() or manager.snapshots()[0].state.value != "running":
                    await anyio.sleep(0.001)
                self.assertEqual(manager.cancel_request("cancelled-wait"), 1)

            async def invoke_cancelled_wait() -> None:
                try:
                    await dispatch(
                        "wait",
                        "logical-board",
                        lambda: wait("logical-board", 5_000),
                        6.0,
                        request_id="cancelled-wait",
                        manager=manager,
                    )
                except Exception as exc:  # noqa: BLE001 - exact type is asserted below
                    failure.append(exc)
                finally:
                    finished.set()

            async with anyio.create_task_group() as tasks:
                tasks.start_soon(cancel_when_running)
                tasks.start_soon(invoke_cancelled_wait)
                await finished.wait()

            self.assertEqual(len(failure), 1)
            self.assertIsInstance(failure[0], OperationCancelledError)
            self.assertEqual(events, [], "a cancelled wait must not record false success")
            self.assertEqual(manager.snapshots(), ())
            started = time.monotonic()
            result = await dispatch(
                "wait",
                "logical-board",
                lambda: wait("logical-board", 50),
                1.0,
                request_id="follow-up-wait",
                manager=manager,
            )
            self.assertEqual(
                result, wrap_layer2_response("Waited 50 ms for board 'logical-board'.")
            )
            self.assertLess(
                time.monotonic() - started,
                CANCELLATION_CLEANUP_GRACE_SECONDS + 0.05 + 0.25,
            )
            self.assertEqual([event["outcome_kind"] for event in events], [ToolOutcome.SUCCESS])
            self.assertEqual(manager.snapshots(), ())

        anyio.run(exercise)

    def test_cl001_commit_boundary_has_no_false_success_and_keeps_completed_success(self) -> None:
        events: list[dict[str, object]] = []
        manager = OperationManager()
        wait = self._handler(events)

        cancelled = manager.create("before-commit", "wait", "logical-board", 1.0)
        cancelled.request_cancel("cancel before commit")
        token = operations._current_operation.set(cancelled)
        try:
            with self.assertRaises(OperationCancelledError):
                wait("logical-board", 1)
        finally:
            operations._current_operation.reset(token)
            manager.finish(cancelled)
        self.assertEqual(events, [])

        completed = manager.create("after-commit", "wait", "logical-board", 1.0)
        token = operations._current_operation.set(completed)
        try:
            result = wait("logical-board", 1)
        finally:
            operations._current_operation.reset(token)
        completed.request_cancel("too late")
        manager.finish(completed)

        self.assertTrue(completed.completion_committed)
        self.assertEqual(result, wrap_layer2_response("Waited 1 ms for board 'logical-board'."))
        self.assertEqual([event["outcome_kind"] for event in events], [ToolOutcome.SUCCESS])

    def test_cl001_public_stdio_cancellation_never_reports_wait_success(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "from pyocd_debug_mcp.server import main; main()"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.assertIsNotNone(process.stdin)
        self.assertIsNotNone(process.stdout)
        received: queue.Queue[str | None] = queue.Queue()
        raw_lines: list[str] = []

        def read_stdout() -> None:
            assert process.stdout is not None
            while line := process.stdout.readline():
                raw_lines.append(line)
                received.put(line)
            received.put(None)

        reader = threading.Thread(target=read_stdout, daemon=True)
        reader.start()
        stderr = ""

        def send(message: dict[str, object]) -> None:
            assert process.stdin is not None
            process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            process.stdin.flush()

        def receive(request_id: int, timeout: float) -> dict[str, object]:
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                self.assertGreater(remaining, 0, f"timed out waiting for request {request_id}")
                line = received.get(timeout=remaining)
                self.assertIsNotNone(line, "server closed stdout")
                payload = json.loads(line)
                self.assertEqual(payload.get("jsonrpc"), "2.0")
                if payload.get("id") == request_id:
                    return payload

        try:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 400,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "h05-spec", "version": "1"},
                    },
                }
            )
            self.assertIn("result", receive(400, 5.0))
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 402,
                    "method": "tools/call",
                    "params": {"name": "initialization_handshake", "arguments": {}},
                }
            )
            handshake = receive(402, 5.0)
            self.assertFalse(handshake["result"].get("isError", False))
            send({"jsonrpc": "2.0", "id": 401, "method": "tools/list", "params": {}})
            listed = receive(401, 5.0)
            self.assertIn("wait", {tool["name"] for tool in listed["result"]["tools"]})

            send(
                {
                    "jsonrpc": "2.0",
                    "id": 410,
                    "method": "tools/call",
                    "params": {
                        "name": "wait",
                        "arguments": {"board_id": "logical-board", "ms": 5_000},
                    },
                }
            )
            send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": 410, "reason": "H05 cancellation spec"},
                }
            )
            started = time.monotonic()
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 420,
                    "method": "tools/call",
                    "params": {
                        "name": "wait",
                        "arguments": {"board_id": "logical-board", "ms": 50},
                    },
                }
            )
            follow_up = receive(420, CANCELLATION_CLEANUP_GRACE_SECONDS + 0.05 + 0.25)
            self.assertLess(
                time.monotonic() - started,
                CANCELLATION_CLEANUP_GRACE_SECONDS + 0.05 + 0.25,
            )
            self.assertFalse(follow_up["result"].get("isError", False))
            self.assertIn("Waited 50 ms for board 'logical-board'.", str(follow_up["result"]))

            cancelled_messages = [
                json.loads(line) for line in raw_lines if json.loads(line).get("id") == 410
            ]
            self.assertTrue(
                not cancelled_messages
                or all(
                    message.get("error") == {"code": 0, "message": "Request cancelled"}
                    for message in cancelled_messages
                ),
                f"request 410 must have no response or the SDK cancellation response: {cancelled_messages}",
            )
        finally:
            assert process.stdin is not None
            process.stdin.close()
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5.0)
            reader.join(timeout=2.0)
            assert process.stdout is not None
            process.stdout.close()
            assert process.stderr is not None
            stderr = process.stderr.read()
            process.stderr.close()
        self.assertEqual(process.returncode, 0, stderr)
        for line in raw_lines:
            self.assertIsInstance(json.loads(line), dict, f"stdout is not JSON-RPC: {line!r}")
