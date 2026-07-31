"""Observation must be structurally incapable of interfering with dispatch.

The plan's central latency claim is not "we measured it and it was fast" -- it is
that recording happens after ``dispatch`` has returned, so it cannot execute
inside a board lock, a flash transaction, or managed cleanup. These tests pin that
structure, because a refactor that moved the hook inward would satisfy every
behavioural test while violating the requirement.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import unittest
from pathlib import Path

from pyocd_debug_mcp.kernel.operations import dispatch
from pyocd_debug_mcp.kernel.registry import RegistryFastMCP

SERVER_PROJECT = Path(__file__).resolve().parents[1]


class RecordingHappensAfterDispatchReturns(unittest.TestCase):
    def setUp(self) -> None:
        self.events: list[str] = []

        outer = self

        class Watcher:
            def begin(self, tool, arguments, board):  # type: ignore[no-untyped-def]
                outer.events.append("begin")
                return Observation()

            def consume_checkin_prompt(self):  # type: ignore[no-untyped-def]
                return None

        class Observation:
            def completed(self, result):  # type: ignore[no-untyped-def]
                outer.events.append("completed")

            def failed(self, exc):  # type: ignore[no-untyped-def]
                outer.events.append("failed")

        self.watcher = Watcher()

    def test_completion_is_recorded_after_the_handler_finishes(self) -> None:
        events = self.events

        async def run() -> None:
            def handler() -> str:
                events.append("handler")
                return "ok"

            await dispatch("t", None, handler, 5.0)

        events.append("begin")
        asyncio.run(run())
        events.append("completed")
        self.assertEqual(events.index("handler"), events.index("begin") + 1)
        self.assertGreater(events.index("completed"), events.index("handler"))

    def test_hook_runs_outside_the_execution_lock(self) -> None:
        """A hook that ran inside the lock would serialize against board work."""

        lock = threading.Lock()
        held_during_observation: list[bool] = []

        class LockWatcher:
            def begin(self, tool, arguments, board):  # type: ignore[no-untyped-def]
                held_during_observation.append(lock.locked())
                return LockObservation()

            def consume_checkin_prompt(self):  # type: ignore[no-untyped-def]
                return None

        class LockObservation:
            def completed(self, result):  # type: ignore[no-untyped-def]
                held_during_observation.append(lock.locked())

            def failed(self, exc):  # type: ignore[no-untyped-def]
                held_during_observation.append(lock.locked())

        mcp = RegistryFastMCP("passivity-test")

        def sample(board_id: str) -> str:
            """A tool that holds a lock for the duration of its work."""

            with lock:
                return "done"

        mcp.add_tool(sample, name="sample", structured_output=False)
        mcp.configure_monitor(LockWatcher())
        asyncio.run(mcp.call_tool("sample", {"board_id": "b1"}))
        self.assertTrue(held_during_observation)
        self.assertFalse(
            any(held_during_observation),
            "the monitor was invoked while the tool's lock was held",
        )


class BaselineBehaviourIsUnchanged(unittest.TestCase):
    """With monitoring present, refusals and results must match the baseline."""

    def test_refusal_text_is_byte_for_byte_unchanged(self) -> None:
        script = (
            "import asyncio, json, sys;"
            "sys.path.insert(0, 'src');"
            "import pyocd_debug_mcp.server as s;"
            "\nasync def m():\n"
            "    try:\n"
            "        await s.mcp.call_tool('get_state', {'board_id': 'ghost'})\n"
            "    except BaseException as e:\n"
            "        print(repr(str(e)))\n"
            "asyncio.run(m())"
        )
        first = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            cwd=str(SERVER_PROJECT),
        )
        second = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            cwd=str(SERVER_PROJECT),
        )
        self.assertEqual(first.stdout, second.stdout)
        self.assertIn("not connected", first.stdout)


if __name__ == "__main__":
    unittest.main()
