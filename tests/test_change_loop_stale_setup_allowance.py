"""Adversarial regressions for stale setup-allowance retirement (CL-001..004)."""

from __future__ import annotations

import json
import unittest
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

from pyocd_debug_mcp import server
from pyocd_debug_mcp.guardrails.plan_defs import definition_for_action
from pyocd_debug_mcp.guardrails.plan_engine import PlanEngine, PlanStatus, _PlanState
from pyocd_debug_mcp.firmstore.reports import ReportWriter
from pyocd_debug_mcp.kernel.run_state import ServerRun
from pyocd_debug_mcp.services.session_runtime import PolicyRefusal
from pyocd_debug_mcp.setup_flow.preflight import PreflightInventory, SetupUserInput
from pyocd_debug_mcp.setup_flow.setup import SetupWorkflow
from pyocd_debug_mcp.setup_flow.validate import BoardValidator
from pyocd_debug_mcp.tools.setup import (
    SetupToolLoadState,
    SetupToolServices,
    build_setup_handlers,
)


class _Registry:
    def is_registered(self, name: str) -> bool:
        return False

    def unlock(self, name: str, board_id: str) -> None:
        pass

    def relock(self, name: str, board_id: str) -> None:
        pass

    def is_unlocked(self, name: str, board_id: str | None) -> bool:
        return True


class _PermissionProvider:
    def __init__(self, on_consume: Mock | None = None) -> None:
        self.consume = Mock(side_effect=on_consume)

    def null_disclosure(self, definition: object) -> None:
        return None

    def authorize_plan(self, *args: object) -> object:
        return object()

    def validate_execution(self, *args: object) -> None:
        pass

    def consume_execution(self, *args: object) -> None:
        self.consume(*args)

    def reset(self) -> None:
        pass


class _UnusedReportWriter(ReportWriter):
    def __init__(self) -> None:
        """Avoid filesystem setup; these allowance tests never write a report."""


def _workflow(*, on_allowance_closed: object | None = None) -> SetupWorkflow:
    callback = (
        cast(Callable[[str, str, str], None], on_allowance_closed)
        if on_allowance_closed is not None
        else None
    )
    return SetupWorkflow(
        _UnusedReportWriter(),
        lambda _input: PreflightInventory(),
        on_allowance_closed=callback,
    )


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
    def test_cl001_replacement_and_terminal_callbacks_carry_closed_identity_once(self) -> None:
        closed: list[tuple[str, str, str]] = []
        workflow = _workflow(
            on_allowance_closed=lambda board, allowance, reason: closed.append(
                (board, allowance, reason)
            )
        )
        input_ = SetupUserInput("board", "connection", "Board", "MCU", None, requires_uart=False)

        workflow.begin_plan("P1", input_, mode="setup")
        workflow.begin_plan("P2", input_, mode="setup")
        workflow.revoke("board")
        workflow.revoke("board")

        self.assertEqual(closed[0], ("board", "P1", "replaced by a new setup plan"))
        self.assertEqual(closed[1], ("board", "P2", "user revoked setup authorization"))
        self.assertEqual(len(closed), 2)
        self.assertTrue(workflow.allowance_closed("P1"))

    def test_cl001_unknown_or_already_closed_allowance_never_notifies(self) -> None:
        closed = Mock()
        workflow = _workflow(on_allowance_closed=closed)
        input_ = SetupUserInput("board", "connection", "Board", "MCU", None, requires_uart=False)

        workflow._close_allowance_locked("unknown", "ignored")  # type: ignore[attr-defined]
        workflow.begin_plan("P1", input_, mode="setup")
        workflow.revoke("board")
        workflow._close_allowance_locked("P1", "second close")  # type: ignore[attr-defined]

        closed.assert_called_once_with("board", "P1", "user revoked setup authorization")

    def test_cl002_expected_identity_never_consumes_or_closes_replacement(self) -> None:
        provider = _PermissionProvider()
        run = ServerRun(run_id="run-test")
        engine = PlanEngine(run, _Registry(), permission_provider=provider)  # type: ignore[arg-type]
        p1_auth, p2_auth = object(), object()
        run.plans[("board_setup", "board")] = _state("P2", p2_auth)

        engine.complete_paired_plan("board_setup", "board", "late P1", expected_plan_id="P1")

        self.assertEqual(engine.active_plan("board_setup", "board").plan_id, "P2")  # type: ignore[union-attr]
        provider.consume.assert_not_called()

        run.plans[("board_setup", "board")] = _state("P1", p1_auth)
        engine.complete_paired_plan("board_setup", "board", "P1 done", expected_plan_id="P1")
        self.assertIsNone(engine.active_plan("board_setup", "board"))
        provider.consume.assert_called_once()
        self.assertIs(provider.consume.call_args.args[2], p1_auth)

    def test_cl002_legacy_close_and_policy_refusal_still_relock_current_plan(self) -> None:
        provider = _PermissionProvider()
        provider.consume.side_effect = PolicyRefusal("permission/inactive", "already revoked")
        run = ServerRun(run_id="run-test")
        engine = PlanEngine(run, _Registry(), permission_provider=provider)  # type: ignore[arg-type]
        current = _state("P1", object())
        run.plans[("board_setup", "board")] = current

        engine.complete_paired_plan("board_setup", "board", "legacy close")

        self.assertIsNone(engine.active_plan("board_setup", "board"))
        self.assertEqual(current.status, PlanStatus.INVALIDATED)
        provider.consume.assert_called_once()

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

    def test_cl003_stale_and_matching_closures_scope_loader_and_continuation_cleanup(self) -> None:
        loader = SetupToolLoadState(ServerRun(run_id="run-test"))
        loader.bind_allowance("board", "P2")
        target = {"board": "target-p2"}
        attachment = {"board": (None, "attach", None)}
        builtin = {"board": object()}
        selections = {"board": object()}
        pipelines = {("board", "P2"): object()}
        research = Mock()
        engine = Mock()
        with (
            patch.object(server, "setup_tool_loader", loader),
            patch.object(server, "plan_engine", engine),
            patch.object(server, "_setup_target_overrides", target),
            patch.object(server, "_setup_attachment_overrides", attachment),
            patch.object(server, "_setup_builtin_candidates", builtin),
            patch.object(server, "_setup_selections_by_board", selections),
            patch.object(server, "_setup_pack_pipelines", pipelines),
            patch.object(server, "_setup_research", research),
        ):
            server._close_setup_allowance("board", "P1", "late")
            self.assertEqual(loader.allowance_for("board"), "P2")
            self.assertEqual(target, {"board": "target-p2"})
            self.assertEqual(attachment, {"board": (None, "attach", None)})
            self.assertIn("board", builtin)
            self.assertIn("board", selections)
            self.assertIn(("board", "P2"), pipelines)
            engine.complete_paired_plan.assert_called_once_with(
                "board_setup", "board", "late", expected_plan_id="P1"
            )
            research.clear.assert_not_called()

            server._close_setup_allowance("board", "P2", "terminal")
            self.assertIsNone(loader.allowance_for("board"))
            self.assertFalse(target)
            self.assertFalse(attachment)
            self.assertFalse(builtin)
            self.assertFalse(selections)
            self.assertFalse(pipelines)
            research.clear.assert_called_once_with("board")

    def test_cl004_replacement_callback_runs_before_new_binding_and_repair_uses_p2(self) -> None:
        loader = SetupToolLoadState(ServerRun(run_id="run-test"))
        loader.bind_allowance("board", "P1")
        active = SimpleNamespace(plan_id="P2")
        engine = SimpleNamespace(active_plan=Mock(return_value=active), complete_paired_plan=Mock())
        continuation = Mock(return_value={"status": "setup_continued"})
        selections = Mock(return_value=SimpleNamespace())

        def begin_plan(plan_id: str, *_args: object, **_kwargs: object) -> None:
            self.assertEqual(plan_id, "P2")
            # This models P1's synchronous retirement callback during P2 installation.
            self.assertEqual(loader.allowance_for("board"), "P1")
            loader.clear_allowance("board", "P1")

        workflow = SimpleNamespace(
            begin_plan=Mock(side_effect=begin_plan),
            board_setup=Mock(
                return_value=SimpleNamespace(
                    status="setup_needs_user_input",
                    to_payload=lambda: {"status": "setup_needs_user_input"},
                )
            ),
            board_fix_setup=Mock(
                return_value=SimpleNamespace(
                    status="setup_completed", to_payload=lambda: {"status": "setup_completed"}
                )
            ),
        )
        clear_setup_continuation = Mock()
        services = SetupToolServices(
            loader=loader,
            plan_engine=cast(PlanEngine, engine),
            workflow=cast(SetupWorkflow, workflow),
            validator=cast(BoardValidator, Mock(spec=BoardValidator)),
            safety_setup=lambda _board: {},
            safety_refresh=lambda **_kwargs: {},
            setup_selections=selections,
            clear_setup_continuation=clear_setup_continuation,
            setup_continue=continuation,
            require_assignment=None,
        )
        handlers = build_setup_handlers(services)
        arguments = (
            "board",
            "setup",
            "connection",
            "Board",
            "MCU",
            True,
            115200,
            "uart",
            "data.pdf",
        )

        first = json.loads(handlers["board_setup"](*arguments))
        continued = json.loads(
            handlers["continue_setup"]("board", "external-confirm", {"confirmed": True})
        )
        repaired = json.loads(handlers["board_fix_setup"](*arguments))

        self.assertEqual(first["status"], "setup_needs_user_input")
        self.assertEqual(continued["status"], "setup_continued")
        self.assertEqual(repaired["status"], "setup_completed")
        workflow.board_fix_setup.assert_called_once_with("P2", selections=selections.return_value)
        engine.complete_paired_plan.assert_called_once_with(
            "board_setup", "board", "repair ended with setup_completed", expected_plan_id="P2"
        )
        clear_setup_continuation.assert_called_once_with("board", "P2")


if __name__ == "__main__":
    unittest.main()
