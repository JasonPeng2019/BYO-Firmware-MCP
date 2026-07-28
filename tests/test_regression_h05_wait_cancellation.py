"""Adversarial regressions for the managed wait cancellation repair."""

from __future__ import annotations

import unittest

import anyio

from pyocd_debug_mcp import kernel
from pyocd_debug_mcp.kernel.operations import (
    OperationCancelledError,
    OperationManager,
    dispatch,
    wrap_layer2_response,
)
from pyocd_debug_mcp.services.session_runtime import ToolOutcome
from pyocd_debug_mcp.tools.misc import MiscToolServices, build_misc_handlers


class WaitCancellationRegressionTests(unittest.TestCase):
    def _wait(self, events: list[dict[str, object]], sleeps: list[float]):
        return build_misc_handlers(
            MiscToolServices(
                runtime_for=lambda _board_id: None,
                duration_ms=lambda _started: 0,
                record_event=lambda _tool, _args, **event: events.append(event),
                sleep=sleeps.append,
            )
        )["wait"]

    def test_cancelled_managed_wait_does_not_poison_the_direct_sleep_seam(self) -> None:
        async def exercise() -> None:
            events: list[dict[str, object]] = []
            sleeps: list[float] = []
            wait = self._wait(events, sleeps)
            manager = OperationManager()
            failure: list[BaseException] = []

            async def cancel_when_active() -> None:
                while not manager.snapshots():
                    await anyio.sleep(0.001)
                self.assertEqual(manager.cancel_request("cancelled-request"), 1)

            async def invoke() -> None:
                try:
                    await dispatch(
                        "wait",
                        "logical-board",
                        lambda: wait("logical-board", 1_000),
                        2.0,
                        request_id="cancelled-request",
                        manager=manager,
                    )
                except Exception as exc:  # noqa: BLE001 - assert the cancellation type below
                    failure.append(exc)

            async with anyio.create_task_group() as tasks:
                tasks.start_soon(cancel_when_active)
                tasks.start_soon(invoke)

            self.assertEqual(len(failure), 1)
            self.assertIsInstance(failure[0], OperationCancelledError)
            self.assertEqual(events, [])
            self.assertEqual(manager.snapshots(), ())
            self.assertEqual(
                wait("logical-board", 7),
                wrap_layer2_response("Waited 7 ms for board 'logical-board'."),
            )
            self.assertEqual(sleeps, [0.007])
            self.assertEqual([event["outcome_kind"] for event in events], [ToolOutcome.SUCCESS])

        anyio.run(exercise)

    def test_cancellation_after_the_atomic_commit_keeps_the_success_result(self) -> None:
        events: list[dict[str, object]] = []
        sleeps: list[float] = []
        wait = self._wait(events, sleeps)
        manager = OperationManager()
        operation = manager.create("completed-request", "wait", "logical-board", 1.0)
        token = kernel.operations._current_operation.set(operation)
        try:
            result = wait("logical-board", 1)
        finally:
            kernel.operations._current_operation.reset(token)

        self.assertTrue(operation.completion_committed)
        self.assertEqual(manager.cancel_request("completed-request"), 1)
        manager.finish(operation)
        self.assertEqual(result, wrap_layer2_response("Waited 1 ms for board 'logical-board'."))
        self.assertEqual(sleeps, [])
        self.assertEqual([event["outcome_kind"] for event in events], [ToolOutcome.SUCCESS])


if __name__ == "__main__":
    unittest.main()
