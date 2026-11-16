"""VI02: interruption cleanup touches only debug resources the operation owned.

These tests run the production ``dispatch`` lifecycle, the real operation and
connection managers, the real gate and runtime session store, the server's
``_bind_managed_board_resources`` binder and, for preflight, the production
register guard. Batch controls use a real ``RegistryFastMCP`` with the
production batch handler. Only backend effects are simulated: the recording
backend observes which live handle each read/close/reset used. Threading
events (never sleeps) decide when an operation holds or waits for a board, so
queued, preflight and running interruptions are distinguished by lifecycle
state rather than by timing.
"""

from __future__ import annotations
from tests.operation_fixture_support import BOARD, HANG_GUARD_SECONDS, _OwnershipFixture

import threading
import unittest
from functools import partial

import anyio

from pyocd_debug_mcp.kernel.operations import (
    BoardBusyError,
    OperationTimeoutError,
    current_operation,
)
from pyocd_debug_mcp.services import target_control
from pyocd_debug_mcp.services.connections import ManagedConnection


"""Upper bound that only converts a hang into a failure; never an oracle."""

"""A queued request's own finite A-11 bound while another operation holds the board."""

RUNNING_DEADLINE_SECONDS = 1.0
"""A started request's own finite A-11 bound; it waits on an event, not this value."""

EVICTED_BY_INTERRUPTION = "operation cancelled or timed out"


def _wait_until_interrupted(entered: threading.Event) -> None:
    """Keep backend use in flight until this operation's own interruption arrives."""

    entered.set()
    operation = current_operation()
    assert operation is not None
    if not operation.cancellation_requested.wait(HANG_GUARD_SECONDS):
        raise AssertionError("operation was never interrupted")
    operation.checkpoint()


class RunningInterruptionControlTests(_OwnershipFixture):
    """Controls that already-owned interruption cleanup still runs."""

    def test_running_timeout_evicts_the_owned_connection(self) -> None:
        owner = self.connect("probe-a")
        entered = threading.Event()
        outcome: dict[str, object] = {}

        def uncertain_backend_wait() -> None:
            self.read_current()
            _wait_until_interrupted(entered)

        async def scenario() -> None:
            try:
                await self.call("read_memory", uncertain_backend_wait, RUNNING_DEADLINE_SECONDS)
            except OperationTimeoutError as exc:
                outcome["timeout"] = exc

        anyio.run(scenario)

        self.assertEqual(self.join_workers(), [])
        self.assertTrue(entered.is_set())
        self.assertIsInstance(outcome.get("timeout"), OperationTimeoutError)
        self.assertNotIsInstance(outcome.get("timeout"), BoardBusyError)
        self.assertEqual(
            {**self.board_state(), "runtime_closed": owner.runtime_session.closed_at is not None},
            {
                **self.evicted(
                    EVICTED_BY_INTERRUPTION,
                    [
                        ("read_memory", "probe-a"),
                        ("finalizer", "read_memory"),
                        ("close", "probe-a"),
                        ("release_reset", "probe-a"),
                    ],
                ),
                "runtime_closed": True,
            },
        )

    def test_running_cancellation_evicts_the_owned_connection(self) -> None:
        owner = self.connect("probe-a")
        entered = threading.Event()

        def uncertain_backend_wait() -> None:
            self.read_current()
            _wait_until_interrupted(entered)

        outcome = anyio.run(
            self.cancel_when,
            entered.is_set,
            partial(self.call, "read_memory", uncertain_backend_wait, HANG_GUARD_SECONDS),
        )

        self.assertEqual(self.join_workers(), [])
        self.assertEqual(outcome, {"cancelled": True})
        self.assertEqual(
            {**self.board_state(), "runtime_closed": owner.runtime_session.closed_at is not None},
            {
                **self.evicted(
                    EVICTED_BY_INTERRUPTION,
                    [
                        ("read_memory", "probe-a"),
                        ("finalizer", "read_memory"),
                        ("close", "probe-a"),
                        ("release_reset", "probe-a"),
                    ],
                ),
                "runtime_closed": True,
            },
        )

    def test_stale_running_cleanup_never_closes_or_resets_a_replacement(self) -> None:
        # Identity control: another owner replaces the connection this running
        # operation captured. Compare-and-clear must leave the replacement alone.
        original = self.connect("probe-c1")
        entered = threading.Event()
        replacement: dict[str, ManagedConnection] = {}

        def replaced_while_running() -> None:
            self.read_current()
            old = self.connections.clear_if_current(BOARD, original)
            assert old is original
            target_control.close_session(old.handle)
            self.sessions.close_session(old.runtime_session)
            replacement["c2"] = self.connect("probe-c2")
            _wait_until_interrupted(entered)

        outcome = anyio.run(
            self.cancel_when,
            entered.is_set,
            partial(self.call, "read_memory", replaced_while_running, HANG_GUARD_SECONDS),
        )

        self.assertEqual(self.join_workers(), [])
        self.assertEqual(outcome, {"cancelled": True})
        self.assertEqual(
            {
                **self.board_state(),
                "original_runtime_closed": original.runtime_session.closed_at is not None,
                "replacement_runtime_closed": (
                    replacement["c2"].runtime_session.closed_at is not None
                ),
            },
            {
                # Only the captured, already replaced handle receives the reset release.
                **self.preserved(
                    replacement["c2"],
                    [
                        ("read_memory", "probe-c1"),
                        ("close", "probe-c1"),
                        ("finalizer", "read_memory"),
                        ("release_reset", "probe-c1"),
                    ],
                ),
                "original_runtime_closed": True,
                "replacement_runtime_closed": False,
            },
        )


if __name__ == "__main__":
    unittest.main()
