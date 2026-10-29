"""Controlled negatives for the original LOCAL method assertions; zero requests.

These controls call the actual original assertion bodies with controlled client
and ledger observations. They qualify the oracles, not model/server behavior.
"""

import unittest

from tests import test_monitor_codex_e2e as original
from tests.codex_harness import CodexResult


class RequiredAttemptOracles(unittest.TestCase):
    def invoke(self, cls, name, *, calls=(), counted=(), outcomes=()):
        case = cls(name)
        result = CodexResult(0, "ERROR", "", list(calls))
        case.run_agent = lambda *_args, **_kwargs: result
        case.reports = lambda: []
        case.counted_records = lambda: list(counted)
        case.ledger_records = lambda: list(counted)
        case.outcomes_recorded = lambda: set(outcomes)
        case.call_count_recorded = lambda: max(
            (r["detail"]["total_calls"] for r in counted), default=0
        )
        case.workspace = "controlled-workspace"
        getattr(case, name)()

    def test_no_attempt_cannot_satisfy_batch_refusal(self):
        cls = original.MonitoringToolsAreOutsideTheSafetySurface
        method = "test_monitor_tools_are_refused_inside_a_batch"
        with self.assertRaises(AssertionError):
            self.invoke(cls, method)
        with self.assertRaises(AssertionError):
            self.invoke(cls, method, calls=["mcp: action_batch"])
        self.invoke(cls, method, calls=["mcp: action_batch"], outcomes=["policy_refusal"])

    def test_word_error_without_attempt_cannot_satisfy_malformed_report(self):
        cls = original.AgentAuthoredSubmissions
        method = "test_malformed_report_is_rejected_and_not_stored"
        with self.assertRaises(AssertionError):
            self.invoke(cls, method)
        # A real client/schema rejection need not create a server report.
        self.invoke(cls, method, calls=["mcp: report_agent_issue invalid arguments"])

    def test_empty_counts_cannot_satisfy_cumulative_records(self):
        cls = original.AgentSessionsAreRecorded
        method = "test_records_carry_run_identity_and_cumulative_counts"
        with self.assertRaises(AssertionError):
            self.invoke(cls, method)
        record = {
            "run_id": "controlled-run",
            "workspace": "controlled-workspace",
            "detail": {"cumulative": True, "total_calls": 2},
        }
        self.invoke(cls, method, counted=[record], outcomes=["success"])
        record["detail"]["cumulative"] = False
        with self.assertRaises(AssertionError):
            self.invoke(cls, method, counted=[record], outcomes=["success"])


if __name__ == "__main__":
    unittest.main()
