"""One deadline per operation, cancellation of abandoned requests, late replies.

Contract "Adapter/service interface and lifecycle" items 7-8: a single deadline
starts at operation entry and covers resolution, initialization, sync and the
query (no per-phase reset); a timeout is ``incomplete`` with ``$/cancelRequest``
and late replies are discarded.  The outer resolver adds the existing 5-second
argument grace to the valid requested/default budget.
"""

from __future__ import annotations

import math
import time
import unittest
from typing import Any

from pyocd_debug_mcp.kernel import operations

from tests.code_analysis_support import (
    ANALYSIS_TOOLS,
    ARGUMENT_TIMEOUT_GRACE_SECONDS,
    FAKE_CLANGD,
    OWNED_CLEANUP_SECONDS,
    SCHEDULING_SLACK_SECONDS,
    Call,
    FakeServiceCase,
    assert_envelope,
    assert_failure,
    first_location,
    make_program,
    make_service,
)

ECHO = {"uri": "$request_uri", "range": {"start": "$request_position", "end": "$request_position"}}
QUERY_TOOLS = tuple(name for name in ANALYSIS_TOOLS if name != "get_code_analysis_status")


class OuterTimeoutResolver(unittest.TestCase):
    def test_query_tools_use_requested_or_default_budget_plus_grace(self) -> None:
        self.assertEqual(operations.ARGUMENT_TIMEOUT_GRACE_SECONDS, ARGUMENT_TIMEOUT_GRACE_SECONDS)
        for tool in QUERY_TOOLS:
            with self.subTest(tool=tool):
                self.assertEqual(operations.operation_timeout_seconds(tool, {}), 30.0 + 5.0)
                self.assertEqual(
                    operations.operation_timeout_seconds(tool, {"timeout_seconds": 1}), 6.0
                )
                self.assertEqual(
                    operations.operation_timeout_seconds(tool, {"timeout_seconds": 2.5}), 7.5
                )
                self.assertEqual(
                    operations.operation_timeout_seconds(tool, {"timeout_seconds": 600}), 605.0
                )

    def test_invalid_requested_timeouts_still_get_a_finite_budget(self) -> None:
        # Invalid input must still reach the handler's strict validation, so
        # the resolver may neither raise nor return an unbounded budget.
        for tool in QUERY_TOOLS:
            for value in (True, False, 0, -1, math.nan, math.inf, "10", None, [1]):
                with self.subTest(tool=tool, value=repr(value)):
                    budget = operations.operation_timeout_seconds(tool, {"timeout_seconds": value})
                    self.assertTrue(math.isfinite(budget) and budget > 0, budget)

    def test_status_budget_covers_its_bounded_probe_and_cleanup(self) -> None:
        budget = operations.operation_timeout_seconds("get_code_analysis_status", {})
        self.assertTrue(math.isfinite(budget))
        self.assertGreaterEqual(budget, 5.0 + OWNED_CLEANUP_SECONDS)


class RequestTimeouts(FakeServiceCase):
    def test_unanswered_request_is_incomplete_and_cancelled(self) -> None:
        self.fake.configure(responses={"textDocument/definition": {"never": True}})
        call = Call(self.definition, timeout_seconds=1.0)
        envelope = call.join(1.0 + ARGUMENT_TIMEOUT_GRACE_SECONDS + SCHEDULING_SLACK_SECONDS)
        assert_failure(self, envelope, "definition", "incomplete", "analysis/timeout")
        self.assertLess(call.elapsed, 1.0 + SCHEDULING_SLACK_SECONDS)
        (request,) = self.fake.received("textDocument/definition")
        cancels = self.fake.wait_received("$/cancelRequest", timeout=5.0)
        self.assertIn(request["id"], [message["params"]["id"] for message in cancels])
        self.assertTrue(all("id" not in message for message in cancels), "cancel is a notification")

    def test_late_reply_is_discarded_and_never_answers_a_later_call(self) -> None:
        self.fake.configure(
            responses={
                "textDocument/references": [{"delay": 2.0, "result": [ECHO]}, {"result": [ECHO]}]
            }
        )
        first_position = self.position_of("helper", occurrence=0)
        second_position = self.position_of("helper", occurrence=1)
        late = self.service.references(
            file_path=str(self.source()),
            line=first_position[0],
            column=first_position[1],
            timeout_seconds=1.0,
        )
        assert_failure(self, late, "references", "incomplete", "analysis/timeout")
        time.sleep(1.5)  # the late reply for the abandoned request arrives now
        envelope = self.service.references(
            file_path=str(self.source()),
            line=second_position[0],
            column=second_position[1],
            timeout_seconds=10.0,
        )
        assert_envelope(self, envelope, "references", status="ok")
        self.assertEqual(
            first_location(self, envelope)["range"]["start"],
            {"line": second_position[0], "column": second_position[1]},
            "a late reply was routed to a later caller",
        )
        self.assertEqual(envelope["data"]["observed_count"], 1)


class SingleDeadlineAcrossPhases(FakeServiceCase):
    def test_initialization_time_counts_against_the_query_budget(self) -> None:
        self.fake.configure(
            responses={
                "initialize": {"delay": 0.7},
                "textDocument/definition": {
                    "delay": 0.7,
                    "result": {
                        "uri": "$request_uri",
                        "range": {"start": "$request_position", "end": "$request_position"},
                    },
                },
            }
        )
        call = Call(self.definition, timeout_seconds=1.0)
        envelope = call.join(1.0 + ARGUMENT_TIMEOUT_GRACE_SECONDS + SCHEDULING_SLACK_SECONDS)
        assert_failure(self, envelope, "definition", "incomplete", "analysis/timeout")
        self.assertLess(call.elapsed, 1.0 + SCHEDULING_SLACK_SECONDS)

    def test_program_resolution_receives_and_spends_the_same_absolute_deadline(self) -> None:
        calls: list[tuple[float, float]] = []

        def slow_resolver(context: Any, name: str, deadline: float) -> Any:
            calls.append((time.monotonic(), deadline))
            time.sleep(0.6)
            return make_program(FAKE_CLANGD)

        self.fake.configure(responses={"initialize": {"delay": 0.6}})
        service = make_service(
            self.project.root, fake=self.fake, program_resolver=slow_resolver, testcase=self
        )
        started = time.monotonic()
        call = Call(
            service.definition, file_path=str(self.source()), line=3, column=5, timeout_seconds=1.0
        )
        envelope = call.join(1.0 + ARGUMENT_TIMEOUT_GRACE_SECONDS + SCHEDULING_SLACK_SECONDS)
        assert_failure(self, envelope, "definition", "incomplete", "analysis/timeout")
        self.assertTrue(calls, "the program resolver was not consulted")
        _entered, deadline = calls[0]
        self.assertAlmostEqual(deadline, started + 1.0, delta=0.5)

    def test_silent_diagnostics_are_bounded_by_the_requested_timeout(self) -> None:
        self.fake.configure(diagnostics_mode="silent", responses={"initialize": {"delay": 0.3}})
        call = Call(self.service.diagnostics, file_path=str(self.source()), timeout_seconds=1.5)
        envelope = call.join(1.5 + ARGUMENT_TIMEOUT_GRACE_SECONDS + SCHEDULING_SLACK_SECONDS)
        assert_envelope(self, envelope, "diagnostics", status="incomplete")
        self.assertLess(call.elapsed, 1.5 + SCHEDULING_SLACK_SECONDS)


if __name__ == "__main__":
    unittest.main()
