"""Regression coverage for stale board-setup datasheet plan bindings."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from pyocd_debug_mcp.guardrails.plan_engine import PlanEngine, PlanRefusal
from pyocd_debug_mcp.kernel.run_state import ServerRun


class _Registry:
    def __init__(self) -> None:
        self.relocked: list[tuple[str, str]] = []

    def is_registered(self, name: str) -> bool:
        return True

    def unlock(self, name: str, board_id: str) -> None:
        pass

    def relock(self, name: str, board_id: str) -> None:
        self.relocked.append((name, board_id))

    def is_unlocked(self, name: str, board_id: str | None) -> bool:
        return True


class _Permissions:
    def __init__(self) -> None:
        self.validate_execution = Mock()
        self.consume_execution = Mock()

    def null_disclosure(self, definition: object) -> None:
        pass

    def authorize_plan(self, *args: object) -> object:
        return object()

    def reset(self) -> None:
        pass


class StaleDatasheetPlanRegressionTests(unittest.TestCase):
    @staticmethod
    def _parameters(pdf: Path) -> dict[str, object]:
        return {
            "mode": "setup",
            "connection_id": "discovered-connection",
            "display_name": "General Development Board",
            "mcu_part_number": "Generic-MCU-7",
            "requires_uart": False,
            "serial_baudrate": None,
            "serial_id": None,
            "datasheet_path": str(pdf),
        }

    def test_removed_datasheet_relocks_primary_and_paired_before_setup_preconditions(self) -> None:
        registry = _Registry()
        permissions = _Permissions()
        engine = PlanEngine(
            ServerRun(run_id="h04-regression"), registry, permission_provider=permissions  # type: ignore[arg-type]
        )
        engine.null_response("board_setup-plan")

        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "selected.PDF"
            pdf.write_bytes(b"%PDF-stable-at-acceptance")
            parameters = self._parameters(pdf)
            engine.submit(
                "board_setup-plan",
                {
                    "board_id": "board_under_test",
                    "hypothesis": "the selected PDF identifies the requested target",
                    "strategy": "bind its bytes before setup",
                    "hypothesis_made": True,
                    "strategy_evaluated": True,
                    "expected_fail_return": "a missing PDF invalidates the plan",
                    "expected_success_return": "the same PDF permits setup",
                    "max_calls": 1,
                    "max_calls_buffer": 0,
                    "action_parameters": parameters,
                    "user_permission": "full-session-setup-permission",
                },
                plan_id_override="plan-0000000000000010",
            )
            pdf.unlink()
            setup_preconditions = Mock()

            with self.assertRaises(PlanRefusal) as raised:
                engine.enforce("board_setup", "board_under_test", parameters, preconditions=setup_preconditions)

        self.assertEqual(raised.exception.code, "plan/artifact-changed")
        self.assertIsNone(engine.active_plan("board_setup", "board_under_test"))
        self.assertIsNone(engine.active_plan("board_fix_setup", "board_under_test"))
        self.assertEqual(
            registry.relocked[-2:],
            [("board_setup", "board_under_test"), ("board_fix_setup", "board_under_test")],
        )
        setup_preconditions.assert_not_called()
        permissions.validate_execution.assert_not_called()
        permissions.consume_execution.assert_not_called()


if __name__ == "__main__":
    unittest.main()
