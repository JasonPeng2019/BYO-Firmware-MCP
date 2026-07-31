"""The three agent-facing actions and the two model-authored forms.

Model output is untrusted, so the forms are fixed schemas rather than free prose.
The three actions must never be conflated: a routine check-in is not an issue
report, and the health check is not a submission.
"""

from __future__ import annotations

import json
import unittest

from pyocd_debug_mcp.monitor.narrative import (
    validate_checkin_form,
    validate_issue_form,
)
from pyocd_debug_mcp.monitor.tools import (
    CHECKIN_TOOL_NAME,
    HEALTH_TOOL_NAME,
    MONITOR_TOOL_NAMES,
    REPORT_TOOL_NAME,
    build_monitor_tools,
)
from tests.monitor_support import MonitorTestCase


def issue_form(**overrides):
    form = {
        "signal_type": "S-4",
        "codebase_objective": "A motor controller firmware; adding closed-loop speed control.",
        "hypothesis": "The plan envelope rejected my nested action_parameters object.",
        "goal": "Flash the new application image to the bench board.",
        "plan": "Collect artifacts, submit the flash plan, then flash.",
        "failure_point": {
            "action_taken": "Submitted flash_application-plan with the populated plan.",
            "observed_result": "Rejected for an unexpected field.",
            "named_step": "flash_application-plan",
        },
        "recent_actions": [
            {
                "action": f"step {index}",
                "result": "ok",
                "code_context": "Building the speed-control loop in src/control.c.",
            }
            for index in range(5)
        ],
        "earlier_phases": ["Set up the board", "Built the firmware"],
        "session_start": "Started by asking which board to use.",
    }
    form.update(overrides)
    return form


def checkin_form(**overrides):
    form = {
        "codebase_summary": "Motor controller firmware; currently adding speed control.",
        "work_summary": "Built the firmware, flashed it twice, and read back status.",
        "tools_used": [{"tool": "flash", "purpose": "wrote firmware twice"}],
        "effectiveness_observed": (
            "Flashed successfully on the second attempt; spent three tries finding "
            "the linker map path."
        ),
    }
    form.update(overrides)
    return form


class IssueForm(unittest.TestCase):
    def test_valid_form_is_accepted(self) -> None:
        validated, signal = validate_issue_form(issue_form())
        self.assertEqual(signal.value, "S-4")
        self.assertEqual(len(validated["recent_actions"]), 5)

    def test_more_than_five_recent_actions_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_issue_form(
                issue_form(
                    recent_actions=[
                        {"action": "a", "result": "b", "code_context": "c"}
                        for _ in range(6)
                    ]
                )
            )

    def test_missing_required_field_is_rejected(self) -> None:
        form = issue_form()
        del form["hypothesis"]
        with self.assertRaises(ValueError):
            validate_issue_form(form)

    def test_extra_field_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_issue_form(issue_form(sneaky="value"))

    def test_freeform_submission_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_issue_form("something went wrong")

    def test_server_detected_signal_cannot_be_filed_by_the_model(self) -> None:
        with self.assertRaises(ValueError) as caught:
            validate_issue_form(issue_form(signal_type="S-1"))
        self.assertIn("server-detected", str(caught.exception))

    def test_subcase_required_for_guidance_signal(self) -> None:
        with self.assertRaises(ValueError):
            validate_issue_form(issue_form(signal_type="S-6"))
        validated, _ = validate_issue_form(
            issue_form(signal_type="S-6", signal_subcase="guidance_was_unusable")
        )
        self.assertEqual(validated["signal_subcase"], "guidance_was_unusable")

    def test_subcase_required_for_remedy_dead_end(self) -> None:
        with self.assertRaises(ValueError):
            validate_issue_form(issue_form(signal_type="S-7", signal_subcase="wrong"))
        validate_issue_form(issue_form(signal_type="S-7", signal_subcase="no_remedy"))

    def test_payload_in_narrative_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_issue_form(issue_form(hypothesis="dump: " + "DE AD BE EF " * 40))

    def test_real_code_names_are_allowed_in_narrative(self) -> None:
        validate_issue_form(
            issue_form(hypothesis="pwm_set_duty() in src/pwm.c clamps the value early.")
        )


class CheckInForm(unittest.TestCase):
    def test_valid_form_is_accepted(self) -> None:
        validated = validate_checkin_form(checkin_form())
        self.assertIn("work_summary", validated)

    def test_self_rating_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_checkin_form(
                checkin_form(effectiveness_observed="I did well and my performance was good.")
            )

    def test_missing_codebase_summary_is_rejected(self) -> None:
        form = checkin_form()
        del form["codebase_summary"]
        with self.assertRaises(ValueError):
            validate_checkin_form(form)

    def test_extra_field_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_checkin_form(checkin_form(severity="high"))

    def test_check_in_carries_no_issue_fields(self) -> None:
        """A health record must never be shaped like an issue."""

        validated = validate_checkin_form(checkin_form())
        for banned in ("severity", "signal_type", "grouping_key"):
            self.assertNotIn(banned, validated)


class MonitorToolSurface(MonitorTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.monitor = self.make_monitor()
        self.tools = build_monitor_tools(self.monitor)

    def test_exactly_three_actions_in_a_personal_build(self) -> None:
        self.assertEqual(
            set(self.tools), {REPORT_TOOL_NAME, HEALTH_TOOL_NAME, CHECKIN_TOOL_NAME}
        )

    def test_none_of_them_take_a_board_id(self) -> None:
        """No board_id is what keeps them off per-board serialization."""

        import inspect

        for name, handler in self.tools.items():
            with self.subTest(tool=name):
                self.assertNotIn("board_id", inspect.signature(handler).parameters)

    def test_all_carry_usable_descriptions(self) -> None:
        for name, handler in self.tools.items():
            with self.subTest(tool=name):
                doc = handler.__doc__ or ""
                self.assertIn("When to reach for it", doc)
                self.assertGreater(len(doc), 200)

    def test_report_tool_states_the_classification_rule(self) -> None:
        doc = self.tools[REPORT_TOOL_NAME].__doc__ or ""
        self.assertIn("NOT reportable", doc)

    def test_health_check_is_side_effect_free(self) -> None:
        first = json.loads(self.tools[HEALTH_TOOL_NAME]())
        second = json.loads(self.tools[HEALTH_TOOL_NAME]())
        first.pop("uptime_seconds", None)
        second.pop("uptime_seconds", None)
        self.assertEqual(first, second)

    def test_health_check_emits_no_record_and_no_report(self) -> None:
        before = len(self.ledger_files()) + len(self.report_files())
        self.tools[HEALTH_TOOL_NAME]()
        self.tools[HEALTH_TOOL_NAME]()
        after = len(self.ledger_files()) + len(self.report_files())
        self.assertEqual(before, after)

    def test_health_check_declares_the_build_profile(self) -> None:
        payload = json.loads(self.tools[HEALTH_TOOL_NAME]())
        self.assertIn(payload["narrative_logging"], ("enabled", "not_built"))

    def test_report_submission_records_an_issue(self) -> None:
        self.monitor.bind_workspace(None)
        payload = json.loads(self.tools[REPORT_TOOL_NAME](**issue_form()))
        self.assertEqual(payload["status"], "report_recorded")
        self.assertTrue(self.report_files())

    def test_malformed_report_is_rejected_not_recorded(self) -> None:
        payload = json.loads(
            self.tools[REPORT_TOOL_NAME](
                signal_type="S-4",
                codebase_objective="x",
                hypothesis="y",
                goal="z",
                plan="p",
                failure_point={"action_taken": "a"},
                recent_actions=[],
                session_start="s",
            )
        )
        self.assertEqual(payload["status"], "report_rejected")
        self.assertFalse(self.report_files())

    def test_checkin_records_a_summary_not_an_issue(self) -> None:
        self.monitor.bind_workspace(None)
        payload = json.loads(self.tools[CHECKIN_TOOL_NAME](**checkin_form()))
        self.assertEqual(payload["status"], "checkin_recorded")
        self.assertIn("summary_id", payload)
        self.assertFalse(self.report_files())

    def test_monitor_tool_names_cover_the_surface(self) -> None:
        self.assertEqual(
            MONITOR_TOOL_NAMES,
            frozenset({REPORT_TOOL_NAME, HEALTH_TOOL_NAME, CHECKIN_TOOL_NAME}),
        )


class ProfessionalBuild(MonitorTestCase):
    """A build cut without narrative logging strips the model-authored layer."""

    def setUp(self) -> None:
        super().setUp()
        import pyocd_debug_mcp.monitor.tools as tools_module

        self._original = tools_module.NARRATIVE_LOGGING
        tools_module.NARRATIVE_LOGGING = False
        self.addCleanup(setattr, tools_module, "NARRATIVE_LOGGING", self._original)
        self.monitor = self.make_monitor()
        self.tools = build_monitor_tools(self.monitor)

    def test_check_in_is_absent_not_disabled(self) -> None:
        # Absent, because it is server-prompted: with no prompt the agent never
        # expects it, so a missing tool cannot be misfiled as a discovery failure.
        self.assertNotIn(CHECKIN_TOOL_NAME, self.tools)

    def test_report_tool_is_present_and_explains(self) -> None:
        self.assertIn(REPORT_TOOL_NAME, self.tools)
        payload = json.loads(self.tools[REPORT_TOOL_NAME](**issue_form()))
        self.assertEqual(payload["status"], "reporting_disabled")
        self.assertIn("professional license", payload["message"])

    def test_refused_report_stores_no_narrative(self) -> None:
        self.monitor.bind_workspace(None)
        self.tools[REPORT_TOOL_NAME](**issue_form())
        self.assertFalse(self.report_files())
        self.assertNotIn("closed-loop speed control", self.all_store_text())

    def test_health_check_still_available(self) -> None:
        self.assertIn(HEALTH_TOOL_NAME, self.tools)


if __name__ == "__main__":
    unittest.main()
