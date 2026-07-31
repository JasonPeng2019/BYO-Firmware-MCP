"""End-to-end: a real gpt-5.4-mini agent against a real server process.

Each test drives the agent through a genuine MCP session, then asserts on what
the monitor durably recorded about that session. The agent's own answer is
secondary -- the durable record is the product.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.codex_harness import (
    REQUIRED_MODEL,
    CodexAgentTestCase,
    _known_model_migration,
)
from tests.store_cleanup import restore as store_restore, snapshot as store_snapshot


_STORE_BEFORE: "set[str] | None" = None


def setUpModule() -> None:
    global _STORE_BEFORE
    _STORE_BEFORE = store_snapshot()


def tearDownModule() -> None:
    """Restore the real store to exactly what was there before this module ran."""

    store_restore(_STORE_BEFORE)



class AgentSessionsAreRecorded(CodexAgentTestCase):
    def test_real_agent_traffic_reaches_the_ledger(self) -> None:
        result = self.run_agent(
            "call server_health_check once. Reply with only the word DONE."
        )
        self.assertEqual(result.returncode, 0, result.combined[-2000:])
        self.assertTrue(result.tool_calls, "the agent made no MCP tool calls")

        recorded = self.tools_recorded()
        self.assertIn("initialization_handshake", recorded)
        self.assertIn("server_health_check", recorded)

    def test_boot_record_survives_a_real_client_teardown(self) -> None:
        """Durability comes from the append, not from any shutdown path.

        A close record is deliberately *not* required here. This client tears the
        server down by killing the process rather than closing stdin, and on
        Windows that arrives with no notification at all -- so closeout is
        best-effort by construction. The boot record must still be on disk,
        because it was durable the moment it was appended.
        ``test_stdio_lifecycle`` covers the clean-EOF path where a close record
        *is* guaranteed.
        """

        self.run_agent("call server_health_check once. Reply with only DONE.")
        self.assertIn("boot", self.record_kinds())

    def test_no_per_call_record_is_written_for_a_real_session(self) -> None:
        """A real agent session produces occasion records, never one per call.

        This is the shape the spec requires, checked against a live server rather
        than in-process: whatever the agent did, the ledger holds boot, snapshots,
        check-ins, reports, and close -- and nothing keyed to an individual call.
        """

        self.run_agent(
            "call server_health_check three times. Reply with only DONE."
        )
        allowed = {"boot", "usage_snapshot", "checkin", "report", "close"}
        self.assertTrue(self.record_kinds())
        self.assertEqual(self.record_kinds() - allowed, set())
        for record in self.ledger_records():
            # No per-call columns survive at the record's top level.
            for column in ("tool", "args_fp", "outcome", "error_class", "duration_ms"):
                self.assertNotIn(column, record)

    def test_records_carry_run_identity_and_cumulative_counts(self) -> None:
        self.run_agent("call server_health_check once. Reply with only DONE.")
        for record in self.ledger_records():
            self.assertTrue(record.get("run_id"))
            self.assertEqual(record.get("workspace"), self.workspace)
        counted = self.counted_records()
        if counted:
            # Every counted record reports a running total, never a window delta.
            for record in counted:
                self.assertTrue(record["detail"].get("cumulative"))
            self.assertGreater(self.call_count_recorded(), 0)
            self.assertTrue(
                self.outcomes_recorded()
                <= {"success", "policy_refusal", "unexpected_error"}
            )


class WorkspaceBindingThroughARealHandshake(CodexAgentTestCase):
    def test_agent_supplied_workspace_is_bound_and_anonymised(self) -> None:
        result = self.run_agent(
            "call server_health_check. Reply with only DONE."
        )
        self.assertEqual(result.returncode, 0, result.combined[-2000:])
        self.assertTrue(
            self.workspace_dir.exists() or self.delivered_dir.exists(),
            "no workspace folder was created for the bound project",
        )
        # The folder name is a digest, so the project path never lands on disk.
        self.assertNotIn(self.project.name, str(self.workspace_dir))

    def test_no_plaintext_project_path_is_stored(self) -> None:
        self.run_agent("call server_health_check. Reply with only DONE.")
        blob = self.store_text()
        self.assertNotIn(str(self.project), blob)
        self.assertNotIn(self.project.name, blob)

    def test_nothing_is_written_into_the_project_directory(self) -> None:
        self.run_agent("call server_health_check. Reply with only DONE.")
        stray = sorted(
            path.name
            for path in self.project.rglob("*")
            if path.is_file() and path.name != "README.md"
        )
        self.assertEqual(stray, [], f"monitor wrote into the project: {stray}")


class HealthCheckIsAUsableOracle(CodexAgentTestCase):
    def test_agent_can_read_live_counts_back(self) -> None:
        result = self.run_agent(
            "call the MCP tool server_health_check twice, "
            "with no arguments both times. Reply with ONLY the two values of "
            "counters.total from the two responses, separated by a comma, like: 0,1"
        )
        self.assertEqual(result.returncode, 0, result.combined[-2000:])
        answer = result.stdout.strip().splitlines()[-1].strip()
        self.assertRegex(
            answer,
            r"^\d+\s*,\s*\d+$",
            f"agent did not report two counts; got {answer!r}",
        )
        first, second = (int(part) for part in answer.split(","))
        self.assertLess(
            first, second, "counts did not advance between the two health checks"
        )
        # The model's reply alone could be a hallucination, so corroborate that a
        # real server really served this workspace. Note what corroboration is
        # *not* available: there is no per-call ledger record to count, by design.
        # The health check is the oracle here -- it reads the live counter, which
        # is the authoritative source -- and the ledger's job is only to show the
        # run existed. A monotonic pair of counts is what proves the counter moved.
        self.assertIn("boot", self.record_kinds())
        self.assertTrue(
            self.workspace_dir.exists() or self.delivered_dir.exists(),
            "no workspace folder was created, so no server actually ran",
        )

    def test_health_readout_declares_transport_and_build_state(self) -> None:
        result = self.run_agent(
            "call server_health_check once and reply with "
            "ONLY the raw JSON it returned, nothing else."
        )
        start = result.stdout.find("{")
        end = result.stdout.rfind("}")
        self.assertGreater(end, start, f"no JSON in agent reply: {result.stdout[-800:]}")
        payload = json.loads(result.stdout[start : end + 1])
        self.assertIn(payload["narrative_logging"], ("enabled", "not_built"))
        # The filler must never claim a real send.
        self.assertNotEqual(payload["delivery"]["state"], "sent")
        self.assertFalse(payload["delivery"]["durable_off_box"])
        self.assertIn(
            payload["block"]["state"], ("dormant", "armed", "clock_unusable")
        )


class CorrectRefusalsProduceNoDefectReports(CodexAgentTestCase):
    """The primary gate, exercised by a real agent making real mistakes."""

    def test_a_session_of_correct_refusals_files_no_server_defect(self) -> None:
        result = self.run_agent(
            "deliberately do these "
            "four things and do not stop when they fail: (1) call get_state with "
            "board_id 'ghost-board'; (2) call flash_application with board_id "
            "'ghost-board' and artifact_path 'nope.elf'; (3) call the tool named "
            "flash_application-plan with every parameter set to null; (4) call "
            "server_health_check. Then reply with only DONE."
        )
        self.assertEqual(result.returncode, 0, result.combined[-2000:])

        # Guard against passing vacuously: if the agent never actually got
        # refused, "no defects" would hold trivially and prove nothing. The
        # evidence is the snapshot's cumulative outcome distribution, since
        # individual calls are not recorded.
        self.assertIn(
            "policy_refusal",
            self.outcomes_recorded(),
            "the session produced no recorded refusals, so this proves nothing",
        )

        defects = [r for r in self.reports() if r["triage_class"] == "server_defect"]
        self.assertEqual(
            defects,
            [],
            "correct guarded refusals produced server-defect reports: "
            + json.dumps([d["title"] for d in defects]),
        )

    def test_refusals_are_counted_even_though_they_are_not_reported(self) -> None:
        """A refusal is normal output: counted, never filed as an issue.

        It earns no durable record of its own -- that is the point of dropping the
        per-call ledger -- but it must still be *countable*, which is what the
        snapshot's cumulative outcome distribution provides.
        """

        self.run_agent(
            "call get_state with board_id 'ghost-board', then call "
            "server_health_check, then call server_health_check again. "
            "Reply with only DONE."
        )
        self.assertIn(
            "policy_refusal",
            self.outcomes_recorded(),
            "a refused call was not counted as a policy refusal",
        )
        defects = [r for r in self.reports() if r["triage_class"] == "server_defect"]
        self.assertEqual(defects, [], "a correct refusal was filed as a defect")


class AgentAuthoredSubmissions(CodexAgentTestCase):
    def test_agent_can_file_an_issue_report(self) -> None:
        result = self.run_agent(
            "call the MCP tool report_agent_issue with: "
            "signal_type 'S-4'; codebase_objective 'Toy firmware project used for "
            "integration testing'; hypothesis 'The plan envelope rejected my "
            "submission'; goal 'Flash the test image'; plan 'Submit the plan then "
            "flash'; failure_point as an object with action_taken 'submitted the "
            "plan', observed_result 'it was rejected', named_step "
            "'flash_application-plan'; recent_actions as a list of exactly 3 "
            "objects each with action, result and code_context strings; "
            "earlier_phases as a list with one string 'set up the board'; "
            "session_start 'asked which board to use'. Then reply with only the "
            "status field from its JSON response."
        )
        self.assertEqual(result.returncode, 0, result.combined[-2000:])
        self.assertTrue(
            result.called("report_agent_issue"),
            f"agent never called the report tool: {result.tool_calls}",
        )
        reports = self.reports()
        self.assertTrue(reports, "no report was persisted")
        report = reports[-1]
        self.assertEqual(report["signal_type"], "S-4")
        self.assertEqual(report["triage_class"], "agent_behavior")
        self.assertEqual(report["origin"], "model-skill")
        self.assertIn("narrative", report)
        # The server owns these; the model never supplies them.
        self.assertTrue(report["grouping_key"])
        self.assertIn("trail", report)
        self.assertIn("environment", report)

    def test_agent_can_submit_a_routine_checkin(self) -> None:
        result = self.run_agent(
            "call the MCP tool submit_routine_checkin with: "
            "codebase_summary 'Toy firmware project for integration testing'; "
            "work_summary 'Connected to the server and inspected its health'; "
            "tools_used as a list with one object {tool: 'health-check', purpose: "
            "'inspected server state'}; effectiveness_observed 'The health check "
            "returned counts; nothing was blocked'. Reply with only the status "
            "field from its JSON response."
        )
        self.assertEqual(result.returncode, 0, result.combined[-2000:])
        self.assertTrue(
            result.called("submit_routine_checkin"),
            f"agent never called the check-in tool: {result.tool_calls}",
        )
        # A check-in is a health record, so it must not appear as an issue.
        self.assertEqual(
            [r for r in self.reports() if r.get("signal_type")],
            [],
            "a routine check-in was filed as an issue report",
        )
        # It gets its own record kind, distinct from a usage snapshot and a report.
        self.assertIn("checkin", self.record_kinds())
        checkins = [r for r in self.ledger_records() if r.get("kind") == "checkin"]
        self.assertTrue(checkins[-1]["detail"].get("cumulative"))

    def test_malformed_report_is_rejected_and_not_stored(self) -> None:
        result = self.run_agent(
            "call the MCP tool report_agent_issue with "
            "signal_type 'S-4' and hypothesis 'something broke' and nothing else "
            "- deliberately omit every other parameter. Reply with only the "
            "status field from its JSON response, or the word ERROR if the call "
            "could not be made."
        )
        self.assertEqual(result.returncode, 0, result.combined[-2000:])
        recorded = [r for r in self.reports() if r.get("signal_type")]
        self.assertEqual(
            recorded, [], "an invalid model-authored report was recorded anyway"
        )


class MonitoringToolsAreOutsideTheSafetySurface(CodexAgentTestCase):
    def test_all_three_actions_are_discoverable(self) -> None:
        result = self.run_agent(
            "list the tool names available from the MCP "
            "server that contain the word 'health', 'report' or 'checkin'. Reply "
            "with only those names, comma separated."
        )
        self.assertEqual(result.returncode, 0, result.combined[-2000:])
        answer = result.stdout.lower()
        self.assertIn("server_health_check", answer)
        self.assertIn("report_agent_issue", answer)
        self.assertIn("submit_routine_checkin", answer)

    def test_monitor_tools_are_refused_inside_a_batch(self) -> None:
        result = self.run_agent(
            "call the MCP tool action_batch with board_id "
            "'b1' and actions set to a list containing one object with tool_name "
            "'server_health_check' and arguments {board_id: 'b1'}. Report only "
            "whether it succeeded or was refused, in one word."
        )
        self.assertEqual(result.returncode, 0, result.combined[-2000:])
        # Whatever the agent reports, the batch attempt must not have been recorded
        # as a server defect: refusing a monitor tool as a batch child is correct
        # behaviour, so it is a refusal and never an unexpected error.
        defects = [r for r in self.reports() if r["triage_class"] == "server_defect"]
        self.assertEqual(
            defects,
            [],
            "refusing a monitor tool inside a batch was filed as a defect",
        )
        self.assertNotIn("unexpected_error", self.outcomes_recorded())


class StdoutStaysClean(CodexAgentTestCase):
    """Stdout is the wire: any stray byte breaks framing intermittently."""

    def test_a_full_session_keeps_the_protocol_intact(self) -> None:
        result = self.run_agent(
            "call server_health_check, then "
            "initialization_handshake again, then server_health_check again. "
            "Reply with only DONE."
        )
        self.assertEqual(result.returncode, 0, result.combined[-2000:])
        self.assertNotIn("JSONDecodeError", result.combined)
        self.assertNotIn("invalid json", result.combined.lower())
        self.assertNotIn("failed to parse", result.combined.lower())
        # Three tool calls all completed, which they could not if framing broke.
        self.assertGreaterEqual(len(result.tool_calls), 3, result.tool_calls)


class ModelMigrationLookupIsHermetic(unittest.TestCase):
    """`_known_model_migration` reads codex's own `[notice.model_migrations]`
    table rather than a hard-coded replacement name that would itself go
    stale at the next server-side rename. These never spawn codex, so they
    run unconditionally, independent of whether codex is installed here.
    """

    def _with_fake_home(self, config_text: str | None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        if config_text is not None:
            codex_dir = home / ".codex"
            codex_dir.mkdir(parents=True)
            (codex_dir / "config.toml").write_text(config_text, encoding="utf-8")
        return mock.patch("tests.codex_harness.Path.home", return_value=home)

    def test_a_migrated_model_resolves_to_its_replacement(self) -> None:
        config = (
            "[notice.model_migrations]\n"
            f'"{REQUIRED_MODEL}" = "some-future-model"\n'
        )
        with self._with_fake_home(config):
            self.assertEqual(_known_model_migration(REQUIRED_MODEL), "some-future-model")

    def test_no_config_file_resolves_to_no_known_migration(self) -> None:
        with self._with_fake_home(None):
            self.assertIsNone(_known_model_migration(REQUIRED_MODEL))

    def test_config_without_the_section_resolves_to_no_known_migration(self) -> None:
        with self._with_fake_home('model = "something-else"\n'):
            self.assertIsNone(_known_model_migration(REQUIRED_MODEL))

    def test_a_different_models_migration_does_not_leak_in(self) -> None:
        config = "[notice.model_migrations]\n\"some-other-model\" = \"some-future-model\"\n"
        with self._with_fake_home(config):
            self.assertIsNone(_known_model_migration(REQUIRED_MODEL))


class ModelPinningIsEnforced(CodexAgentTestCase):
    def test_the_harness_refuses_a_run_on_another_model(self) -> None:
        """A silently downgraded run would test something other than the claim."""

        from tests.codex_harness import CodexResult

        with self.assertRaises(AssertionError):
            self._assert_model_was_honoured(
                CodexResult(0, "model: gpt-4o-mini\n", "")
            )
        self._assert_model_was_honoured(
            CodexResult(0, f"model: {REQUIRED_MODEL}\n", "")
        )


if __name__ == "__main__":
    unittest.main()
