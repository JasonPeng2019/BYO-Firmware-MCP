"""Repetition detection and the remote-logging staleness backstop.

The detector's risk is the mirror of its purpose: this server has many correct
repetition patterns, so a naive detector becomes a false-positive engine.
Repetition alone is not thrashing -- repetition with an identical outcome and no
state transition is.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from pyocd_debug_mcp.monitor.block import (
    STALENESS_THRESHOLD,
    Anchor,
    BlockState,
    StalenessBlock,
    evaluate,
)
from pyocd_debug_mcp.monitor.thrash import ThrashDetector
from pyocd_debug_mcp.monitor.transport import (
    DeliveryState,
    NullTransport,
    SimulatedRemoteTransport,
    TestTransport,
)
from pyocd_debug_mcp.services.session_runtime import PolicyRefusal
from tests.monitor_support import MonitorTestCase


class Thrashing(unittest.TestCase):
    def setUp(self) -> None:
        self.detector = ThrashDetector()

    def observe(self, **kwargs):
        base = dict(
            board="b1", tool="read_memory", args_fp="fp-1",
            outcome="policy_refusal", error_class="plan/gate-closed",
            guard_fp="guard-1",
        )
        base.update(kwargs)
        return self.detector.observe(**base)

    def test_a_genuine_loop_fires_once(self) -> None:
        fired = [self.observe() for _ in range(8)]
        self.assertEqual(fired.count(True), 1)

    def test_below_threshold_does_not_fire(self) -> None:
        self.assertFalse(any(self.observe() for _ in range(3)))

    def test_changing_outcome_is_progress_not_a_loop(self) -> None:
        self.observe()
        self.observe()
        self.observe()
        self.assertFalse(self.observe(outcome="success", error_class=None))

    def test_state_transition_is_progress_not_a_loop(self) -> None:
        self.observe()
        self.observe()
        self.observe()
        self.assertFalse(self.observe(guard_fp="guard-2"))

    def test_polling_loops_never_fire(self) -> None:
        for tool in ("get_state", "read_execution_state", "get_setup_status", "wait"):
            with self.subTest(tool=tool):
                detector = ThrashDetector()
                results = [
                    detector.observe(
                        board="b", tool=tool, args_fp="fp", outcome="success",
                        error_class=None, guard_fp="g",
                    )
                    for _ in range(12)
                ]
                self.assertFalse(any(results))

    def test_discovery_and_remote_probe_polling_loops_never_fire(self) -> None:
        """refresh_discovery_hooks / register_remote_probe repeat while the agent
        iterates externally -- repairing a hook file, or waiting for a `pyocd
        server` process to come up on another machine -- so a same-arguments
        repeat of either must never read as a loop, exactly like the other
        polling tools above.
        """

        for tool in ("refresh_discovery_hooks", "register_remote_probe"):
            with self.subTest(tool=tool):
                detector = ThrashDetector()
                results = [
                    detector.observe(
                        board="b", tool=tool, args_fp="retry_id=None",
                        outcome="success", error_class=None, guard_fp="g",
                    )
                    for _ in range(12)
                ]
                self.assertFalse(any(results))

    def test_a_genuinely_thrashing_tool_still_fires(self) -> None:
        """The exclusion list must stay narrow: a real board tool looping on an
        identical outcome with no state transition must still trip the detector,
        confirming this change did not disable thrash detection wholesale.
        """

        detector = ThrashDetector()
        results = [
            detector.observe(
                board="b", tool="connect", args_fp="fp-1",
                outcome="policy_refusal", error_class="plan/gate-closed",
                guard_fp="guard-1",
            )
            for _ in range(8)
        ]
        self.assertEqual(results.count(True), 1)

    def test_all_null_then_populated_plan_never_fires(self) -> None:
        detector = ThrashDetector()
        results = [
            detector.observe(
                board="b", tool="flash_application-plan", args_fp=f"fp{i}",
                outcome="success", error_class=None, guard_fp="g",
            )
            for i in range(10)
        ]
        self.assertFalse(any(results))

    def test_retry_after_board_busy_never_fires(self) -> None:
        detector = ThrashDetector()
        results = [
            detector.observe(
                board="b", tool="read_memory", args_fp="fp",
                outcome="policy_refusal", error_class="runtime/BoardBusyError",
                guard_fp="g",
            )
            for _ in range(10)
        ]
        self.assertFalse(any(results))

    def test_validation_retry_with_accepted_response_never_fires(self) -> None:
        detector = ThrashDetector()
        detector.note_accepted_response("b", "fp-accepted")
        results = [
            detector.observe(
                board="b", tool="board_validate", args_fp="fp-accepted",
                outcome="policy_refusal", error_class="setup/pending", guard_fp="g",
            )
            for _ in range(10)
        ]
        self.assertFalse(any(results))

    def test_refresh_then_validate_pair_never_fires(self) -> None:
        detector = ThrashDetector()
        fired = False
        for _ in range(8):
            detector.observe(
                board="b", tool="board_safety_refresh", args_fp="fp",
                outcome="success", error_class=None, guard_fp="g",
            )
            fired = fired or detector.observe(
                board="b", tool="board_validate", args_fp="fp",
                outcome="policy_refusal", error_class="x", guard_fp="g",
            )
        self.assertFalse(fired)

    def test_paginated_reads_do_not_collide(self) -> None:
        detector = ThrashDetector()
        results = [
            detector.observe(
                board="b", tool="read_memory_block", args_fp=f"fp-{addr}",
                outcome="success", error_class=None, guard_fp="g",
            )
            for addr in range(12)
        ]
        self.assertFalse(any(results))

    def test_boards_are_tracked_separately(self) -> None:
        for _ in range(3):
            self.observe(board="b1")
        self.assertFalse(self.observe(board="b2"))


class BlockEvaluation(unittest.TestCase):
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def test_no_anchor_is_dormant_not_stale(self) -> None:
        """The bootstrap guard: a fresh install must not brick on first use.

        Reading a missing anchor as infinitely stale would refuse the very first
        operation, before any delivery could possibly have happened.
        """

        self.assertIs(evaluate(None, self.now()), BlockState.DORMANT)

    def test_recent_anchor_is_armed_not_tripped(self) -> None:
        anchor = Anchor(self.now() - timedelta(days=1), "filler", "filler")
        self.assertIs(evaluate(anchor, self.now()), BlockState.ARMED)

    def test_stale_anchor_trips(self) -> None:
        anchor = Anchor(self.now() - timedelta(days=15), "filler", "filler")
        self.assertIs(evaluate(anchor, self.now()), BlockState.TRIPPED)

    def test_threshold_is_exactly_two_weeks(self) -> None:
        self.assertEqual(STALENESS_THRESHOLD, timedelta(days=14))
        anchor = Anchor(self.now() - timedelta(days=14), "f", "f")
        self.assertIs(evaluate(anchor, self.now()), BlockState.TRIPPED)
        anchor = Anchor(self.now() - timedelta(days=13, hours=23), "f", "f")
        self.assertIs(evaluate(anchor, self.now()), BlockState.ARMED)

    def test_unreadable_clock_never_trips(self) -> None:
        """A dead real-time clock must not brick a bench machine."""

        anchor = Anchor(self.now() - timedelta(days=99), "f", "f")
        self.assertIs(evaluate(anchor, None), BlockState.CLOCK_UNUSABLE)

    def test_clock_behind_the_anchor_is_unusable_not_stale(self) -> None:
        anchor = Anchor(self.now() + timedelta(days=5), "f", "f")
        self.assertIs(evaluate(anchor, self.now()), BlockState.CLOCK_UNUSABLE)


class BlockLifecycle(MonitorTestCase):
    def test_delivery_reanchors_and_clears(self) -> None:
        block = StalenessBlock(self.server_data)
        block.inject(Anchor(datetime.now(timezone.utc) - timedelta(days=20), "f", "filler"))
        self.assertIs(block.state(), BlockState.TRIPPED)
        block.refresh("simulated_remote", "filler", datetime.now(timezone.utc))
        self.assertIs(block.state(), BlockState.ARMED)

    def test_anchor_survives_across_runs(self) -> None:
        stamped = datetime.now(timezone.utc)
        StalenessBlock(self.server_data).refresh("simulated_remote", "filler", stamped)
        reloaded = StalenessBlock(self.server_data)
        anchor = reloaded.anchor
        self.assertIsNotNone(anchor)
        assert anchor is not None
        self.assertEqual(anchor.origin, "filler")

    def test_filler_origin_is_reported_distinctly(self) -> None:
        """Nobody may conclude a real off-box copy exists."""

        block = StalenessBlock(self.server_data)
        block.refresh("simulated_remote", "filler", datetime.now(timezone.utc))
        described = block.describe()
        self.assertEqual(described["anchor_origin"], "filler")
        self.assertNotEqual(described["anchor_origin"], "real")

    def test_refusal_message_names_its_remedy(self) -> None:
        """Making it a remedy-naming refusal is what keeps it correct behaviour."""

        block = StalenessBlock(self.server_data)
        block.inject(Anchor(datetime.now(timezone.utc) - timedelta(days=30), "f", "filler"))
        message = block.refusal_message()
        self.assertIn("server_health_check", message)
        self.assertIn("confirmed delivery clears", message)
        self.assertNotIn("Restore network", message)

    def test_describe_is_side_effect_free(self) -> None:
        block = StalenessBlock(self.server_data)
        self.assertEqual(block.describe(), block.describe())


class BlockDiagnostics(MonitorTestCase):
    """The age policy is unchanged, even when no delivery was attempted."""

    def setUp(self) -> None:
        super().setUp()
        self.now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        self.clock = self.enterContext(
            patch("pyocd_debug_mcp.monitor.block.datetime", wraps=datetime)
        )
        self.clock.now.return_value = self.now
        transport_clock = self.enterContext(
            patch("pyocd_debug_mcp.monitor.transport.datetime", wraps=datetime)
        )
        transport_clock.now.return_value = self.now

    def test_idle_local_anchors_report_age_and_provenance_without_network_diagnosis(self) -> None:
        # The currently configured null transport differs from the old anchor.
        monitor = self.make_monitor(transport=NullTransport())
        for transport, origin in (("simulated_remote", "filler"), ("test", "test")):
            with self.subTest(anchor_transport=transport):
                anchor = Anchor(self.now - timedelta(days=30), transport, origin)
                monitor._block.inject(anchor)
                with self.assertRaises(PolicyRefusal) as caught:
                    monitor.check_block()
                self.assertEqual(caught.exception.code, "monitor/logging-stale")
                message = str(caught.exception)
                for expected in (
                    "30 days old", "limit: 14 days", "2026-09-01T00:00:00Z",
                    f"transport: {transport}", f"origin: {origin}",
                    "server_health_check", "local/simulated acknowledgement",
                    "unconfigured transport cannot refresh", "Reporting, check-in",
                ):
                    self.assertIn(expected, message)
                self.assertNotIn("Restore network", message)
                self.assertNotIn("has not been able to", message)
                self.assertNotIn("pending log", message)
                self.assertIs(monitor._block.anchor, anchor)
                self.assertEqual(monitor._delivery.describe()["state"], "not_configured")

    def test_dispatch_preserves_the_exact_age_boundary(self) -> None:
        monitor = self.make_monitor(transport=NullTransport())
        for age, state in (
            (timedelta(days=1), BlockState.ARMED),
            (timedelta(days=14) - timedelta(microseconds=1), BlockState.ARMED),
            (timedelta(days=14), BlockState.TRIPPED),
            (timedelta(days=30), BlockState.TRIPPED),
        ):
            with self.subTest(age=age):
                anchor = Anchor(self.now - age, "simulated_remote", "filler")
                monitor._block.inject(anchor)
                self.assertIs(evaluate(anchor, self.now), state)
                self.assertIs(monitor._block.state(), state)
                if state is BlockState.TRIPPED:
                    with self.assertRaises(PolicyRefusal):
                        monitor.check_block()
                else:
                    self.assertIsNone(monitor.check_block())

    def test_absent_future_and_unusable_clock_still_allow_dispatch(self) -> None:
        monitor = self.make_monitor(transport=NullTransport())
        cases = (
            (None, self.now, BlockState.DORMANT),
            (Anchor(self.now + timedelta(days=1), "test", "test"),
             self.now, BlockState.CLOCK_UNUSABLE),
            (Anchor(self.now - timedelta(days=30), "test", "test"),
             None, BlockState.CLOCK_UNUSABLE),
        )
        for anchor, now, state in cases:
            with self.subTest(state=state, now=now):
                monitor._block.inject(anchor)
                self.assertIs(evaluate(anchor, now), state)
                self.clock.now.side_effect = OSError("clock unavailable") if now is None else None
                self.assertIs(monitor._block.state(), state)
                self.assertIsNone(monitor.check_block())
        self.clock.now.side_effect = None

    def _deliver_sealed_segment(self, transport):
        monitor = self.make_monitor(transport=transport)
        anchor = Anchor(self.now - timedelta(days=30), "simulated_remote", "filler")
        monitor._block.inject(anchor)
        from tests.test_monitor_ledger import add_snapshot

        add_snapshot(monitor._ledger)
        sealed = monitor._ledger.seal()
        assert sealed is not None
        monitor._delivery.drain_for_closeout([sealed], budget=2.0)
        return monitor, anchor, sealed

    def test_confirmed_delivery_refreshes_anchor_and_allows_dispatch(self) -> None:
        monitor, _, sealed = self._deliver_sealed_segment(TestTransport())
        self.assertFalse(sealed.path.exists())
        self.assertEqual(monitor._delivery.describe()["state"], DeliveryState.SENT.value)
        self.assertEqual(monitor._block.anchor, Anchor(self.now, "test", "test"))
        self.assertIs(monitor._block.state(), BlockState.ARMED)
        self.assertIsNone(monitor.check_block())

    def test_simulated_acknowledgement_still_refreshes_anchor(self) -> None:
        transport = SimulatedRemoteTransport(self.simulated_remote, "ws")
        self.addCleanup(transport.close)
        monitor, _, sealed = self._deliver_sealed_segment(transport)
        self.assertFalse(sealed.path.exists())
        self.assertTrue(list((self.simulated_remote / "ws" / "ledger").glob("*.jsonl")))
        self.assertEqual(monitor._delivery.describe()["state"], "filler_simulated")
        self.assertFalse(monitor._delivery.describe()["durable_off_box"])
        self.assertEqual(monitor._block.anchor, Anchor(self.now, "simulated_remote", "filler"))
        self.assertIsNone(monitor.check_block())

    def test_actual_failed_delivery_leaves_the_old_anchor_and_gate(self) -> None:
        monitor, anchor, sealed = self._deliver_sealed_segment(TestTransport(fail_always=True))
        self.assertTrue(sealed.path.exists())
        self.assertEqual(monitor._delivery.describe()["state"], "failed")
        self.assertIs(monitor._block.anchor, anchor)
        with self.assertRaises(PolicyRefusal) as caught:
            monitor.check_block()
        self.assertIn("transport: simulated_remote", str(caught.exception))
        self.assertIn("server_health_check", str(caught.exception))
        self.assertNotIn("Restore network", str(caught.exception))

    def test_null_transport_cannot_refresh_a_previous_anchor(self) -> None:
        monitor, anchor, sealed = self._deliver_sealed_segment(NullTransport())
        self.assertTrue(sealed.path.exists())
        self.assertEqual(monitor._delivery.describe()["state"], "not_configured")
        self.assertIs(monitor._block.anchor, anchor)
        with self.assertRaises(PolicyRefusal):
            monitor.check_block()

    def test_refusal_keeps_file_io_off_the_dispatch_path(self) -> None:
        monitor = self.make_monitor(transport=NullTransport())
        monitor._block.inject(Anchor(self.now - timedelta(days=30), "test", "test"))
        with patch("pathlib.Path.open", side_effect=AssertionError("dispatch did file I/O")):
            with self.assertRaises(PolicyRefusal):
                monitor.check_block()


if __name__ == "__main__":
    unittest.main()
