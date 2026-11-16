"""Setup allowance identity, replacement races and broad lifecycle cleanup."""

from __future__ import annotations
import unittest
from unittest.mock import Mock
from pyocd_debug_mcp.guardrails.plan_defs import definition_for_action
from pyocd_debug_mcp.guardrails.plan_engine import PlanEngine, _PlanState
from pyocd_debug_mcp.kernel.run_state import ServerRun


class _Registry:
    def is_registered(self, name: str) -> bool:
        return False


class _PermissionProvider:
    def __init__(self, on_consume: Mock | None = None) -> None:
        self.consume = Mock(side_effect=on_consume)


    def reset(self) -> None:
        pass


def _state(plan_id: str, authorization: object) -> _PlanState:
    definition = definition_for_action("board_setup")
    return _PlanState(
        plan_id=plan_id,
        run_id="run-test",
        definition=definition,
        board_id="board",
        session_id=None,
        max_calls=1,
        max_calls_buffer=0,
        remaining_calls=1,
        paired_remaining={"board_fix_setup": 1},
        canonical_parameters="{}",
        canonical_plan_fields="{}",
        authorization=authorization,
        artifact_binding=None,
    )


class StaleSetupAllowanceSpecTests(unittest.TestCase):

    def test_cl002_cleanup_race_cannot_invalidate_plan_installed_during_consumption(self) -> None:
        run = ServerRun(run_id="run-test")
        engine: PlanEngine
        p2_auth = object()

        def install_p2(*_args: object) -> None:
            with engine._guard:  # the implementation must have released this lock to get here
                run.plans[("board_setup", "board")] = _state("P2", p2_auth)

        provider = _PermissionProvider(Mock(side_effect=install_p2))
        engine = PlanEngine(run, _Registry(), permission_provider=provider)  # type: ignore[arg-type]
        run.plans[("board_setup", "board")] = _state("P1", object())

        engine.complete_paired_plan("board_setup", "board", "P1 done", expected_plan_id="P1")

        active = engine.active_plan("board_setup", "board")
        self.assertIsNotNone(active)
        self.assertEqual(active.plan_id, "P2")  # type: ignore[union-attr]

