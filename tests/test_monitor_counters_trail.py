"""Live counters and the board-scoped trail.

Counts are live server state, not something re-derived by reading logs back. That
direction is what keeps counting correct when the store is unbound and what keeps
the health check honest when delivery is broken.
"""

from __future__ import annotations

import os
import threading
import unittest
from unittest.mock import patch

from pyocd_debug_mcp.monitor.counters import (
    CHECKIN_CADENCE,
    CHECKIN_CADENCE_ENV,
    SNAPSHOT_CADENCE_ENV,
    USAGE_SNAPSHOT_CADENCE,
    RunCounters,
    resolve_checkin_cadence,
    resolve_snapshot_cadence,
)
from pyocd_debug_mcp.monitor.trail import TRAIL_MAX_EVENTS, BoardTrail


class CadenceResolution(unittest.TestCase):
    """The shipped values are the constants; the override exists only for tests."""

    def test_defaults_are_the_constants(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(SNAPSHOT_CADENCE_ENV, None)
            os.environ.pop(CHECKIN_CADENCE_ENV, None)
            self.assertEqual(resolve_snapshot_cadence(), USAGE_SNAPSHOT_CADENCE)
            self.assertEqual(resolve_checkin_cadence(), CHECKIN_CADENCE)

    def test_an_override_is_honoured(self) -> None:
        with patch.dict(
            os.environ,
            {SNAPSHOT_CADENCE_ENV: "7", CHECKIN_CADENCE_ENV: "21"},
        ):
            self.assertEqual(resolve_snapshot_cadence(), 7)
            self.assertEqual(resolve_checkin_cadence(), 21)

    def test_a_nonsense_override_changes_nothing(self) -> None:
        """A misconfigured value must not silently disable recording."""

        for bad in ("", "abc", "0", "-5", "1.5"):
            with patch.dict(os.environ, {SNAPSHOT_CADENCE_ENV: bad}):
                self.assertEqual(
                    resolve_snapshot_cadence(),
                    USAGE_SNAPSHOT_CADENCE,
                    f"override {bad!r} was accepted",
                )

    def test_the_two_cadences_override_independently(self) -> None:
        with patch.dict(os.environ, {SNAPSHOT_CADENCE_ENV: "3"}):
            os.environ.pop(CHECKIN_CADENCE_ENV, None)
            self.assertEqual(resolve_snapshot_cadence(), 3)
            self.assertEqual(resolve_checkin_cadence(), CHECKIN_CADENCE)


class Counting(unittest.TestCase):
    def setUp(self) -> None:
        self.counters = RunCounters()

    def test_counts_by_tool_outcome_and_error_class(self) -> None:
        self.counters.record("get_state", "success", None)
        self.counters.record("get_state", "success", None)
        self.counters.record("flash", "policy_refusal", "plan/gate-closed")
        snapshot = self.counters.snapshot()
        self.assertEqual(snapshot.total, 3)
        self.assertEqual(snapshot.per_tool["get_state"], 2)
        self.assertEqual(snapshot.per_outcome["policy_refusal"], 1)
        self.assertEqual(snapshot.per_error_class["plan/gate-closed"], 1)

    def test_first_and_last_activity_recorded(self) -> None:
        self.counters.record("a", "success", None)
        snapshot = self.counters.snapshot()
        self.assertIsNotNone(snapshot.first_at)
        self.assertIsNotNone(snapshot.last_at)

    def test_coverage_reports_unexercised_tools(self) -> None:
        self.counters.set_advertised(("a", "b", "c"))
        self.counters.record("a", "success", None)
        snapshot = self.counters.snapshot()
        self.assertEqual(snapshot.exercised, ("a",))
        self.assertEqual(snapshot.never_exercised, ("b", "c"))

    def test_no_reset_method_exists(self) -> None:
        """Nothing may clear the counter: the activity still happened.

        Disconnect, gate closure, plan expiry, and a run-scoped authority reset
        must all leave counts intact, so the absence of a reset is the
        enforcement rather than a convention.
        """

        self.assertFalse(hasattr(self.counters, "reset"))
        self.assertFalse(hasattr(self.counters, "clear"))

    def test_snapshot_is_a_copy(self) -> None:
        self.counters.record("a", "success", None)
        snapshot = self.counters.snapshot()
        snapshot.per_tool["a"] = 999
        self.assertEqual(self.counters.snapshot().per_tool["a"], 1)

    def test_appended_total_is_separate_from_call_total(self) -> None:
        # Delivered files delete themselves, so reconciliation keys off this
        # monotonic total rather than off how many files are resident.
        self.counters.record("a", "success", None)
        self.counters.record("b", "success", None)
        self.counters.note_appended()
        snapshot = self.counters.snapshot()
        self.assertEqual(snapshot.total, 2)
        self.assertEqual(snapshot.total_appended, 1)

    def test_write_failures_are_visible(self) -> None:
        self.counters.note_write_failure("PermissionError: denied")
        snapshot = self.counters.snapshot()
        self.assertEqual(snapshot.write_failures, 1)
        self.assertIn("PermissionError", snapshot.last_write_error or "")

    def test_counting_is_thread_safe(self) -> None:
        # Different boards dispatch concurrently by design.
        def worker() -> None:
            for _ in range(200):
                self.counters.record("t", "success", None)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(self.counters.snapshot().total, 1600)


class Trail(unittest.TestCase):
    def setUp(self) -> None:
        self.trail = BoardTrail()

    def test_board_scoping_keeps_boards_apart(self) -> None:
        """A global trail would make two concurrent boards' reports untriageable."""

        self.trail.append(
            tool="read_memory", board="alpha", connection="c1",
            args_fp="fp", outcome="success",
        )
        self.trail.append(
            tool="flash", board="beta", connection="c2",
            args_fp="fp", outcome="unexpected_error",
        )
        alpha = self.trail.for_board("alpha")
        self.assertEqual(len(alpha), 1)
        self.assertEqual(alpha[0].tool, "read_memory")
        self.assertTrue(all(entry.board == "alpha" for entry in alpha))

    def test_buffer_is_bounded(self) -> None:
        for index in range(TRAIL_MAX_EVENTS + 50):
            self.trail.append(
                tool=f"t{index}", board="b", connection=None,
                args_fp="fp", outcome="success",
            )
        self.assertEqual(len(self.trail.for_board("b")), TRAIL_MAX_EVENTS)

    def test_buffer_size_is_a_separate_constant_from_the_snapshot_cadence(self) -> None:
        """Same value today, different knobs -- and they must stay different.

        The trail buffer and the usage-snapshot cadence are both 100 right now,
        which is exactly the situation in which someone collapses them into one
        literal. They answer different questions, so raising the snapshot cadence
        must not silently resize the trail.
        """

        from pyocd_debug_mcp.monitor.counters import USAGE_SNAPSHOT_CADENCE

        self.assertEqual(TRAIL_MAX_EVENTS, 100)
        self.assertEqual(USAGE_SNAPSHOT_CADENCE, 100)
        # Each is defined in its own module, so neither can be an alias of the
        # other and no single edit can move both.
        import pyocd_debug_mcp.monitor.counters as counters_module
        import pyocd_debug_mcp.monitor.trail as trail_module

        self.assertIn("TRAIL_MAX_EVENTS", vars(trail_module))
        self.assertNotIn("TRAIL_MAX_EVENTS", vars(counters_module))
        self.assertIn("USAGE_SNAPSHOT_CADENCE", vars(counters_module))
        self.assertNotIn("USAGE_SNAPSHOT_CADENCE", vars(trail_module))

    def test_the_trail_buffer_ignores_a_changed_snapshot_cadence(self) -> None:
        """Raising the snapshot cadence leaves this buffer at its own size."""

        import pyocd_debug_mcp.monitor.counters as counters_module

        original = counters_module.USAGE_SNAPSHOT_CADENCE
        counters_module.USAGE_SNAPSHOT_CADENCE = 5000
        self.addCleanup(
            setattr, counters_module, "USAGE_SNAPSHOT_CADENCE", original
        )
        trail = BoardTrail()
        for index in range(TRAIL_MAX_EVENTS + 10):
            trail.append(
                tool=f"t{index}", board="b", connection=None,
                args_fp="fp", outcome="success",
            )
        self.assertEqual(len(trail.for_board("b")), TRAIL_MAX_EVENTS)

    def test_entries_record_the_named_remedy(self) -> None:
        self.trail.append(
            tool="flash", board="b", connection="c",
            args_fp="fp", outcome="policy_refusal",
            error_class="plan/gate-closed", remedy="call board_validate",
        )
        entry = self.trail.for_board("b")[0]
        self.assertEqual(entry.remedy, "call board_validate")
        self.assertEqual(entry.outcome, "policy_refusal")

    def test_no_board_bucket_is_separate(self) -> None:
        self.trail.append(
            tool="server_health_check", board=None, connection=None,
            args_fp="fp", outcome="success",
        )
        self.assertEqual(len(self.trail.for_board(None)), 1)
        self.assertEqual(len(self.trail.for_board("b")), 0)

    def test_records_are_jsonable(self) -> None:
        import json

        self.trail.append(
            tool="a", board="b", connection="c", args_fp="fp", outcome="success"
        )
        json.dumps(self.trail.records_for("b"))


if __name__ == "__main__":
    unittest.main()
