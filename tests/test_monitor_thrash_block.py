"""Repetition detection and the remote-logging staleness backstop.

The detector's risk is the mirror of its purpose: this server has many correct
repetition patterns, so a naive detector becomes a false-positive engine.
Repetition alone is not thrashing -- repetition with an identical outcome and no
state transition is.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from pyocd_debug_mcp.monitor.block import (
    STALENESS_THRESHOLD,
    Anchor,
    BlockState,
    StalenessBlock,
    evaluate,
)
from pyocd_debug_mcp.monitor.thrash import ThrashDetector
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
        self.assertIn("network", message.lower())
        self.assertIn("deliver", message.lower())

    def test_describe_is_side_effect_free(self) -> None:
        block = StalenessBlock(self.server_data)
        self.assertEqual(block.describe(), block.describe())


if __name__ == "__main__":
    unittest.main()
