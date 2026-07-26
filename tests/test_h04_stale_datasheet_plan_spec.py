"""CL-001 specifications for setup-plan datasheet byte identity and recovery."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from pyocd_debug_mcp.guardrails.plan_defs import definition_for_action
from pyocd_debug_mcp.guardrails.plan_engine import PlanEngine, PlanRefusal, PlanStatus
from pyocd_debug_mcp.kernel.run_state import ServerRun


class _Registry:
    def __init__(self) -> None:
        self.unlocked: list[tuple[str, str]] = []
        self.relocked: list[tuple[str, str]] = []

    def is_registered(self, name: str) -> bool:
        return True

    def unlock(self, name: str, board_id: str) -> None:
        self.unlocked.append((name, board_id))

    def relock(self, name: str, board_id: str) -> None:
        self.relocked.append((name, board_id))

    def is_unlocked(self, name: str, board_id: str | None) -> bool:
        return (name, board_id or "") not in self.relocked


class _Permissions:
    def __init__(self) -> None:
        self.authorize = Mock(return_value=object())
        self.validate = Mock()
        self.consume = Mock()

    def null_disclosure(self, definition: object) -> None:
        return None

    def authorize_plan(self, *args: object) -> object:
        return self.authorize(*args)

    def validate_execution(self, *args: object) -> None:
        self.validate(*args)

    def consume_execution(self, *args: object) -> None:
        self.consume(*args)

    def reset(self) -> None:
        pass


class StaleDatasheetPlanSpecTests(unittest.TestCase):
    """Attack CL-001's declared binding, ordering, paired action, and recovery behavior."""

    def setUp(self) -> None:
        self.registry = _Registry()
        self.permissions = _Permissions()
        self.engine = PlanEngine(
            ServerRun(run_id="h04-spec-run"),
            self.registry,
            permission_provider=self.permissions,  # type: ignore[arg-type]
        )
        self.engine.null_response("board_setup-plan")

    @staticmethod
    def _parameters(pdf: Path) -> dict[str, object]:
        return {
            "mode": "setup",
            "connection_id": "enumerated-connection",
            "display_name": "Arbitrary Board",
            "mcu_part_number": "Arbitrary-MCU-42",
            "requires_uart": False,
            "serial_baudrate": None,
            "serial_id": None,
            "datasheet_path": str(pdf),
        }

    def _submit(self, pdf: Path, *, plan_id: str) -> object:
        return self.engine.submit(
            "board_setup-plan",
            {
                "board_id": "board_1",
                "hypothesis": "the supplied datasheet identifies the selected setup target",
                "strategy": "bind the selected local datasheet before setup begins",
                "hypothesis_made": True,
                "strategy_evaluated": True,
                "expected_fail_return": "a changed datasheet invalidates the plan",
                "expected_success_return": "the exact selected datasheet starts setup",
                "max_calls": 1,
                "max_calls_buffer": 0,
                "action_parameters": self._parameters(pdf),
                "user_permission": "full-session-setup-permission",
            },
            plan_id_override=plan_id,
        )

    def test_definition_and_accepted_payload_declare_exact_pdf_byte_binding(self) -> None:
        definition = definition_for_action("board_setup")
        self.assertEqual(definition.artifact_binding_field, "datasheet_path")
        self.assertEqual(definition.artifact_binding_suffixes, (".pdf",))

        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "chosen.pdf"
            pdf.write_bytes(b"%PDF-bound-at-acceptance")
            result = self._submit(pdf, plan_id="plan-0000000000000001")

        payload = json.loads(result.message)  # type: ignore[attr-defined]
        self.assertIn("datasheet_path bytes are bound", " ".join(payload["reminders"]))

    def test_changed_primary_pdf_relocks_both_actions_before_work_or_permission_use(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "chosen.pdf"
            pdf.write_bytes(b"%PDF-original")
            accepted = self._submit(pdf, plan_id="plan-0000000000000002")
            payload = json.loads(accepted.message)  # type: ignore[attr-defined]
            self.assertIn("datasheet_path bytes are bound", " ".join(payload["reminders"]))
            pdf.write_bytes(b"%PDF-replaced")

            workflow = Mock()
            with self.assertRaises(PlanRefusal) as raised:
                self.engine.enforce(
                    "board_setup", "board_1", self._parameters(pdf), preconditions=workflow
                )

            self.assertEqual(raised.exception.code, "plan/artifact-changed")
            self.assertIsNone(self.engine.active_plan("board_setup", "board_1"))
            self.assertIsNone(self.engine.active_plan("board_fix_setup", "board_1"))
            self.assertEqual(self.registry.relocked[-2:], [("board_setup", "board_1"), ("board_fix_setup", "board_1")])
            workflow.assert_not_called()
            self.permissions.validate.assert_not_called()
            self.permissions.consume.assert_not_called()

            replacement = self._submit(pdf, plan_id="plan-0000000000000003")
            self.assertEqual(replacement.plan.plan_id, "plan-0000000000000003")  # type: ignore[union-attr]

    def test_changed_pdf_after_primary_refuses_paired_fix_without_completion_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "chosen.pdf"
            pdf.write_bytes(b"%PDF-original")
            self._submit(pdf, plan_id="plan-0000000000000004")
            self.engine.enforce("board_setup", "board_1", self._parameters(pdf))
            active = self.engine.active_plan("board_setup", "board_1")
            self.assertIsNotNone(active)
            self.assertEqual(active.remaining_for("board_setup"), 0)  # type: ignore[union-attr]
            self.assertEqual(active.remaining_for("board_fix_setup"), 1)  # type: ignore[union-attr]

            pdf.write_bytes(b"%PDF-changed-after-primary")
            repair_workflow = Mock()
            with self.assertRaises(PlanRefusal) as raised:
                self.engine.enforce(
                    "board_fix_setup", "board_1", self._parameters(pdf), preconditions=repair_workflow
                )

            self.assertEqual(raised.exception.code, "plan/artifact-changed")
            self.assertIsNone(self.engine.active_plan("board_fix_setup", "board_1"))
            repair_workflow.assert_not_called()
            self.permissions.consume.assert_not_called()

    def test_unchanged_pdf_keeps_primary_and_one_paired_allowance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "chosen.pdf"
            pdf.write_bytes(b"%PDF-stable")
            self._submit(pdf, plan_id="plan-0000000000000005")

            primary = self.engine.enforce("board_setup", "board_1", self._parameters(pdf))
            paired = self.engine.enforce("board_fix_setup", "board_1", self._parameters(pdf))

        self.assertEqual(primary.remaining_for("board_setup"), 0)
        self.assertEqual(primary.remaining_for("board_fix_setup"), 1)
        self.assertEqual(paired.status, PlanStatus.EXHAUSTED)
        self.permissions.consume.assert_called_once()


if __name__ == "__main__":
    unittest.main()
