"""Contracts for the shipped, tool-agnostic issue-monitor client skill."""

from __future__ import annotations

import inspect
import json
import unittest
from pathlib import Path

from pyocd_debug_mcp.monitor.narrative import (
    validate_checkin_form,
    validate_issue_form,
)
from pyocd_debug_mcp.monitor.tools import build_monitor_tools


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "client-workspace" / "skills-src" / "firmware" / "byo-issue-report"
REPOSITORY_SKILL_ROOT = ROOT / "skills" / "byo-issue-report"
SKILL = SKILL_ROOT / "SKILL.md"
ISSUE_TEMPLATE = SKILL_ROOT / "templates" / "issue-report.json"
CHECKIN_TEMPLATE = SKILL_ROOT / "templates" / "routine-checkin.json"


class MonitorClientSkillTests(unittest.TestCase):
    def test_repository_and_attached_workspace_skill_copies_are_identical(self) -> None:
        relative_files = {
            "SKILL.md",
            "agents/openai.yaml",
            "templates/issue-report.json",
            "templates/routine-checkin.json",
        }
        for relative in relative_files:
            with self.subTest(relative=relative):
                self.assertEqual(
                    (SKILL_ROOT / relative).read_bytes(),
                    (REPOSITORY_SKILL_ROOT / relative).read_bytes(),
                )

    def test_one_tool_agnostic_skill_enumerates_every_model_signal(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        for number in range(4, 15):
            heading = f"### S-{number} ·"
            with self.subTest(signal=heading):
                start = text.index(heading)
                later = text.find("\n### S-", start + len(heading))
                section = text[start : later if later >= 0 else len(text)]
                self.assertIn("**Report when:**", section)
                self.assertIn("**Do not report when:**", section)
                self.assertIn(f"| S-{number} |", text)
        self.assertIn("tool-agnostic", text.lower())
        self.assertNotIn("Codex-specific", text)
        self.assertNotIn("Claude-specific", text)

    def test_skill_makes_workable_remedies_and_checkins_explicit_non_issues(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        prose = " ".join(text.split())
        self.assertIn("A refusal that names a workable remedy is not an issue", text)
        self.assertIn(
            "Do not submit a routine check-in until a server response says it is due", prose
        )
        self.assertIn("one collapsed report", prose)

    def test_issue_template_exactly_matches_intake_and_validates(self) -> None:
        template = json.loads(ISSUE_TEMPLATE.read_text(encoding="utf-8"))
        expected = set(inspect.signature(build_monitor_tools).parameters)
        self.assertEqual(expected, {"monitor"})  # sanity: inspect the returned handler below

        class _Monitor:
            pass

        # ``build_monitor_tools`` only closes over the monitor at construction.
        handler = build_monitor_tools(_Monitor())["report_agent_issue"]
        self.assertEqual(set(template), set(inspect.signature(handler).parameters))
        validated, signal = validate_issue_form(template)
        self.assertEqual(signal.value, "S-4")
        self.assertEqual(validated, template)

    def test_checkin_template_is_distinct_exact_and_valid(self) -> None:
        template = json.loads(CHECKIN_TEMPLATE.read_text(encoding="utf-8"))

        class _Monitor:
            pass

        handler = build_monitor_tools(_Monitor())["submit_routine_checkin"]
        self.assertEqual(set(template), set(inspect.signature(handler).parameters))
        self.assertEqual(validate_checkin_form(template), template)
        issue_keys = set(json.loads(ISSUE_TEMPLATE.read_text(encoding="utf-8")))
        self.assertTrue(issue_keys.isdisjoint(template))

    def test_subcase_values_in_skill_match_server_validator(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        for value in (
            "ignored_usable_guidance",
            "guidance_was_unusable",
            "no_remedy",
            "remedy_repeated_same_refusal",
            "cycle_never_converged",
        ):
            self.assertIn(f"`{value}`", text)


if __name__ == "__main__":
    unittest.main()
