"""Regression coverage for allowance-identity cleanup compatibility edges."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from pyocd_debug_mcp import server
from pyocd_debug_mcp.guardrails.plan_defs import definition_for_action
from pyocd_debug_mcp.guardrails.plan_engine import PlanEngine, _PlanState
from pyocd_debug_mcp.kernel.run_state import ServerRun
from pyocd_debug_mcp.tools.setup import SetupToolLoadState


class _Locks:
    def is_registered(self, name: str) -> bool:
        return False

    def unlock(self, name: str, board_id: str) -> None:
        pass

    def relock(self, name: str, board_id: str) -> None:
        pass

    def is_unlocked(self, name: str, board_id: str | None) -> bool:
        return True


def _active_state(plan_id: str) -> _PlanState:
    return _PlanState(
        plan_id=plan_id,
        run_id="regression-run",
        definition=definition_for_action("board_setup"),
        board_id="board",
        session_id=None,
        max_calls=1,
        max_calls_buffer=0,
        remaining_calls=1,
        paired_remaining={"board_fix_setup": 1},
        canonical_parameters="{}",
        canonical_plan_fields="{}",
        authorization=None,
        artifact_binding=None,
    )


class StaleAllowanceRegressionTests(unittest.TestCase):
    def test_legacy_paired_plan_closure_still_closes_current_plan(self) -> None:
        """Broad lifecycle callers without an identity retain their old semantics."""
        run = ServerRun(run_id="regression-run")
        engine = PlanEngine(run, _Locks())  # type: ignore[arg-type]
        run.plans[("board_setup", "board")] = _active_state("P2")

        engine.complete_paired_plan("board_setup", "board", "disconnect")

        self.assertIsNone(engine.active_plan("board_setup", "board"))

    def test_identity_aware_continuation_cleanup_preserves_replacement_then_clears_match(
        self,
    ) -> None:
        loader = SetupToolLoadState(ServerRun(run_id="regression-run"))
        loader.bind_allowance("board", "P2")
        target_overrides = {"board": "p2-target"}
        selections = {"board": object()}
        research = Mock()

        with (
            patch.object(server, "setup_tool_loader", loader),
            patch.object(server, "_setup_target_overrides", target_overrides),
            patch.object(server, "_setup_selections_by_board", selections),
            patch.object(server, "_setup_research", research),
        ):
            server._clear_setup_continuation("board", expected_allowance_id="P1")
            self.assertEqual(target_overrides, {"board": "p2-target"})
            self.assertIn("board", selections)
            research.clear.assert_not_called()

            server._clear_setup_continuation("board", expected_allowance_id="P2")

        self.assertFalse(target_overrides)
        self.assertFalse(selections)
        research.clear.assert_called_once_with("board")

    def test_loader_broad_clear_remains_available_for_disconnect_and_revoke(self) -> None:
        loader = SetupToolLoadState(ServerRun(run_id="regression-run"))
        loader.bind_allowance("board", "P2")

        loader.clear_allowance("board", expected_allowance_id="P1")
        self.assertEqual(loader.allowance_for("board"), "P2")

        loader.clear_allowance("board")
        self.assertIsNone(loader.allowance_for("board"))


if __name__ == "__main__":
    unittest.main()
