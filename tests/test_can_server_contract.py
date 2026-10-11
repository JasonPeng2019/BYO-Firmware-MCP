"""Public MCP, timeout, monitoring, and teardown contracts for CAN."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import call, patch

from mcp.server.fastmcp.exceptions import ToolError

from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier
from pyocd_debug_mcp.kernel.operations import operation_timeout_seconds
from pyocd_debug_mcp.monitor.classify import Outcome, classify_result
from pyocd_debug_mcp.tools.can import CanToolServices, build_can_handlers
from tests.tiered_acceptance_support import known_good_full_policy


def _text(result: object) -> str:
    if not isinstance(result, list) or not result:
        raise AssertionError(f"unexpected MCP result: {result!r}")
    text = getattr(result[0], "text", None)
    if not isinstance(text, str):
        raise AssertionError(f"unexpected MCP content: {result!r}")
    return text


class CanServerContractTests(unittest.TestCase):
    server: Any

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="byo-can-server-"))
        cls.environment = patch.dict(
            os.environ,
            {
                "BYO_MCP_ARTIFACT_ROOT": str(cls.root),
                "BYO_MCP_MONITOR_ROOT": str(cls.root / "monitor"),
                "PYOCD_CAN_MODE": "off",
                "PYOCD_CAN_CONFIG": "",
                "PYOCD_CAN_CONFIG_SHA256": "",
                "PYOCD_CAN_RUNTIME_SHA256": "",
                "PYOCD_CAN_READ_SECONDS": "",
            },
            clear=False,
        )
        cls.environment.start()
        sys.modules.pop("pyocd_debug_mcp.server", None)
        cls.server = importlib.import_module("pyocd_debug_mcp.server")

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.server._can_manager.shutdown_sync()
            cls.server._monitor.closeout("test")
        finally:
            sys.modules.pop("pyocd_debug_mcp.server", None)
            cls.environment.stop()
            shutil.rmtree(cls.root, ignore_errors=True)

    def call(self, name: str, arguments: dict[str, object]) -> object:
        return asyncio.run(self.server.mcp.call_tool(name, arguments))

    def test_static_inventory_and_non_layer2_binding(self) -> None:
        expected = {
            "get_can_status",
            "connect_can",
            "disconnect_can",
            "read_can-plan",
            "read_can",
            "send_can-plan",
            "send_can",
        }
        self.assertTrue(expected.issubset(self.server.mcp._tool_manager._tools))
        self.assertTrue(expected.isdisjoint(self.server.mcp._layer2_tools))
        for name in expected - {"read_can-plan", "send_can-plan"}:
            schema = self.server.mcp._tool_manager._tools[name].parameters
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(
                schema["properties"]["board_id"]["pattern"],
                "^[a-z0-9][a-z0-9_]{0,63}$",
            )

    def test_off_status_disconnect_and_connect_are_canonical_json(self) -> None:
        status_text = _text(self.call("get_can_status", {"board_id": "board"}))
        self.assertEqual(
            status_text, json.dumps(json.loads(status_text), sort_keys=True, separators=(",", ":"))
        )
        status = json.loads(status_text)
        self.assertTrue(status["ok"])
        self.assertFalse(status["can"]["configured"])
        self.assertEqual(status["can"]["phase"], "closed")

        disconnected = json.loads(_text(self.call("disconnect_can", {"board_id": "board"})))
        self.assertEqual(disconnected["status"], "disconnected")
        refused = json.loads(_text(self.call("connect_can", {"board_id": "board"})))
        self.assertEqual(refused["status"], "refused")
        self.assertEqual(refused["code"], "can/not-configured")

    def test_locked_action_uses_generic_structured_refusal(self) -> None:
        with self.assertRaises(ToolError) as caught:
            self.call(
                "read_can",
                {"board_id": "board", "read_seconds": None, "max_frames": None},
            )
        payload = json.loads(str(caught.exception))
        self.assertEqual(payload["status"], "refused")
        self.assertEqual(payload["code"], "plan/tool-locked")
        self.assertEqual(payload["operation"], "read_can")

    def test_off_mode_planned_actions_consume_plan_and_return_not_configured(self) -> None:
        common_plan = {
            "board_id": "board",
            "hypothesis": "No CAN profile was staged for this server run.",
            "strategy": "Attempt the exact bounded CAN operation once.",
            "hypothesis_made": True,
            "strategy_evaluated": True,
            "expected_fail_return": "The server reports that CAN is not configured.",
            "expected_success_return": "The exact bounded CAN operation completes.",
            "max_calls": 1,
            "max_calls_buffer": 0,
        }
        cases = (
            (
                "read_can",
                {"read_seconds": None, "max_frames": None},
            ),
            (
                "send_can",
                {
                    "arbitration_id": 0x123,
                    "data_hex": "CAFE",
                    "extended": False,
                    "timeout_seconds": None,
                },
            ),
        )
        for action_name, action_parameters in cases:
            with self.subTest(action_name=action_name):
                plan_name = f"{action_name}-plan"
                null_plan: dict[str, object] = {
                    "board_id": None,
                    "hypothesis": None,
                    "strategy": None,
                    "hypothesis_made": None,
                    "strategy_evaluated": None,
                    "expected_fail_return": None,
                    "expected_success_return": None,
                    "max_calls": None,
                    "max_calls_buffer": None,
                    "action_parameters": None,
                    "user_permission": None,
                }
                self.call(plan_name, null_plan)
                self.call(
                    plan_name,
                    {**common_plan, "action_parameters": action_parameters},
                )

                with patch.object(
                    self.server.connection_manager,
                    "maybe_connection",
                    side_effect=AssertionError("off-mode action performed a Layer-0 lookup"),
                ):
                    refusal = json.loads(
                        _text(self.call(action_name, {"board_id": "board", **action_parameters}))
                    )
                self.assertEqual(refusal["status"], "refused")
                self.assertEqual(refusal["code"], "can/not-configured")
                self.assertIsNone(self.server.plan_engine.active_plan(action_name, "board"))

                with self.assertRaises(ToolError) as caught:
                    self.call(action_name, {"board_id": "board", **action_parameters})
                exhausted = json.loads(str(caught.exception))
                self.assertEqual(exhausted["code"], "plan/tool-locked")

    def test_huge_integer_can_timeout_is_rejected_as_a_plan_validation_error(self) -> None:
        null_plan: dict[str, object] = {
            "board_id": None,
            "hypothesis": None,
            "strategy": None,
            "hypothesis_made": None,
            "strategy_evaluated": None,
            "expected_fail_return": None,
            "expected_success_return": None,
            "max_calls": None,
            "max_calls_buffer": None,
            "action_parameters": None,
            "user_permission": None,
        }
        self.call("read_can-plan", null_plan)
        with self.assertRaises(ToolError) as caught:
            self.call(
                "read_can-plan",
                {
                    "board_id": "board",
                    "hypothesis": "A bounded CAN observation may produce useful evidence.",
                    "strategy": "Attempt the exact bounded CAN observation once.",
                    "hypothesis_made": True,
                    "strategy_evaluated": True,
                    "expected_fail_return": "The observation is refused.",
                    "expected_success_return": "The observation completes.",
                    "max_calls": 1,
                    "max_calls_buffer": 0,
                    "action_parameters": {
                        "read_seconds": 10**400,
                        "max_frames": 1,
                    },
                },
            )
        self.assertIn("Invalid action_parameters fields", str(caught.exception))
        self.assertIn("must be a finite number", str(caught.exception))

    def test_can_null_plan_guidance_publishes_every_global_bound(self) -> None:
        null_plan: dict[str, object] = {
            "board_id": None,
            "hypothesis": None,
            "strategy": None,
            "hypothesis_made": None,
            "strategy_evaluated": None,
            "expected_fail_return": None,
            "expected_success_return": None,
            "max_calls": None,
            "max_calls_buffer": None,
            "action_parameters": None,
            "user_permission": None,
        }
        read_guidance = _text(self.call("read_can-plan", null_plan))
        self.assertIn("read_seconds (number; NULL allowed; > 0; <= 10)", read_guidance)
        self.assertIn("startup-effective default", read_guidance)
        self.assertIn("max_frames (integer; NULL allowed; >= 1; <= 128)", read_guidance)

        send_guidance = _text(self.call("send_can-plan", null_plan))
        self.assertIn("arbitration_id (integer; >= 0; <= 536870911)", send_guidance)
        self.assertIn("timeout_seconds (number; NULL allowed; > 0; <= 0.5)", send_guidance)

    def test_can_plans_enforce_global_and_frame_semantic_bounds(self) -> None:
        null_plan: dict[str, object] = {
            "board_id": None,
            "hypothesis": None,
            "strategy": None,
            "hypothesis_made": None,
            "strategy_evaluated": None,
            "expected_fail_return": None,
            "expected_success_return": None,
            "max_calls": None,
            "max_calls_buffer": None,
            "action_parameters": None,
            "user_permission": None,
        }
        common_plan = {
            "board_id": "board",
            "hypothesis": "The bounded CAN operation can produce evidence.",
            "strategy": "Attempt the exact bounded CAN operation once.",
            "hypothesis_made": True,
            "strategy_evaluated": True,
            "expected_fail_return": "The operation is refused.",
            "expected_success_return": "The operation completes.",
            "max_calls": 1,
            "max_calls_buffer": 0,
        }
        cases = (
            ("read_can-plan", {"read_seconds": 10.1, "max_frames": 1}, "at most 10"),
            ("read_can-plan", {"read_seconds": 0.1, "max_frames": 129}, "at most 128"),
            (
                "send_can-plan",
                {
                    "arbitration_id": 0x123,
                    "data_hex": "",
                    "extended": False,
                    "timeout_seconds": 1e308,
                },
                "at most 0.5",
            ),
            (
                "send_can-plan",
                {
                    "arbitration_id": 0x800,
                    "data_hex": "",
                    "extended": False,
                    "timeout_seconds": 0.1,
                },
                "standard 11-bit",
            ),
            (
                "send_can-plan",
                {
                    "arbitration_id": 0x123,
                    "data_hex": "001122334455667788",
                    "extended": False,
                    "timeout_seconds": 0.1,
                },
                "at most 8 bytes",
            ),
        )
        for plan_name, parameters, expected in cases:
            with self.subTest(plan_name=plan_name, parameters=parameters):
                self.call(plan_name, null_plan)
                with self.assertRaises(ToolError) as caught:
                    self.call(
                        plan_name,
                        {**common_plan, "action_parameters": parameters},
                    )
                self.assertIn(expected, str(caught.exception))

    def test_schema_validation_precedes_route_lock_and_handler(self) -> None:
        invalid_arguments: tuple[dict[str, object], ...] = (
            {"board_id": 1},
            {"board_id": " Bad "},
            {"board_id": "board", "extra": True},
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments), self.assertRaises(ToolError) as caught:
                self.call("get_can_status", arguments)
            message = str(caught.exception)
            self.assertIn("validation error", message)
            self.assertFalse(message.startswith("{"), message)
        with self.assertRaises(ToolError) as caught:
            self.call("read_can-plan", {"board_id": 1})
        self.assertIn("validation error", str(caught.exception))

    def test_exact_json_response_preserves_json_and_delivers_checkin_in_second_block(self) -> None:
        outer = self

        class Observation:
            def completed(self, result: object) -> None:
                del result

            def failed(self, exc: BaseException) -> None:
                del exc

        class DueMonitor:
            consumed = 0

            def begin(self, tool: str, arguments: object, board: object) -> Observation:
                del tool, arguments, board
                return Observation()

            def consume_checkin_prompt(self) -> str:
                self.consumed += 1
                return "\nCHECKIN-DUE"

        due = DueMonitor()
        original = self.server.mcp._monitor
        self.server.mcp.configure_monitor(due)
        try:
            status_result = self.call("get_can_status", {"board_id": "board"})
            self.assertIsInstance(status_result, list)
            assert isinstance(status_result, list)
            self.assertEqual(len(status_result), 2)
            json.loads(status_result[0].text)
            self.assertEqual(status_result[1].text, "\nCHECKIN-DUE")

            connect_result = self.call("connect_can", {"board_id": "board"})
            self.assertIsInstance(connect_result, list)
            assert isinstance(connect_result, list)
            self.assertEqual(len(connect_result), 2)
            json.loads(connect_result[0].text)
            self.assertEqual(connect_result[1].text, "\nCHECKIN-DUE")
        finally:
            self.server.mcp.configure_monitor(original)
        outer.assertEqual(due.consumed, 2)

    def test_real_checkin_cadence_is_delivered_by_can_only_traffic(self) -> None:
        monitor = self.server._monitor
        with monitor._guard:
            original_every = monitor._checkin_every
            original_due = monitor._checkin_due
            current_total = monitor._counters.snapshot().total
            monitor._checkin_every = current_total + 2
            monitor._checkin_due = False
        try:
            before = self.call("get_can_status", {"board_id": "board"})
            self.assertIsInstance(before, list)
            assert isinstance(before, list)
            self.assertEqual(len(before), 1)

            boundary = self.call("get_can_status", {"board_id": "board"})
            self.assertIsInstance(boundary, list)
            assert isinstance(boundary, list)
            self.assertEqual(len(boundary), 2)
            json.loads(boundary[0].text)
            self.assertIn("[routine check-in due]", boundary[1].text)

            after = self.call("get_can_status", {"board_id": "board"})
            self.assertIsInstance(after, list)
            assert isinstance(after, list)
            self.assertEqual(len(after), 1)
        finally:
            with monitor._guard:
                monitor._checkin_every = original_every
                monitor._checkin_due = original_due

    def test_debug_disconnect_does_not_mutate_authority_when_can_cleanup_fails(self) -> None:
        calls: list[str] = []

        class FailingCanManager:
            owner_board_id = "board"

            def disconnect_sync(self, board_id: str) -> None:
                calls.append(f"can:{board_id}")
                raise RuntimeError("cleanup failed")

        original = self.server._can_manager
        self.server._can_manager = FailingCanManager()
        try:
            with (
                patch.object(self.server.assignment_store, "clear_board") as clear_assignment,
                patch.object(self.server.gate_manager, "clear") as clear_gate,
                patch.object(self.server.plan_engine, "invalidate_board") as invalidate,
                self.assertRaisesRegex(RuntimeError, "cleanup failed"),
            ):
                self.server.disconnect("board")
            self.assertEqual(calls, ["can:board"])
            clear_assignment.assert_not_called()
            clear_gate.assert_not_called()
            invalidate.assert_not_called()

            class SuccessfulCanManager:
                owner_board_id = "board"

                def disconnect_sync(self, board_id: str) -> None:
                    calls.append(f"normalized-can:{board_id}")

            self.server._can_manager = SuccessfulCanManager()
            with patch.object(
                self.server,
                "_disconnect_debug_only",
                return_value="Disconnected board 'board'.",
            ) as debug_disconnect:
                self.server.disconnect(" board ")
            self.assertIn("normalized-can:board", calls)
            debug_disconnect.assert_called_once_with("board")
        finally:
            self.server._can_manager = original

    def test_shutdown_cleanup_failure_records_closes_monitor_and_exits_nonzero(self) -> None:
        events: list[str] = []

        class FailingCanManager:
            owner_reads = 0

            @property
            def owner_board_id(self) -> str | None:
                self.owner_reads += 1
                return None if self.owner_reads == 1 else "owner"

            def shutdown_sync(self) -> bool:
                events.append("can-cleanup")
                return False

        class Monitor:
            def boot(self) -> None:
                events.append("monitor-boot")

            def closeout(self, reason: str) -> None:
                events.append(f"monitor-closeout:{reason}")

        original_manager = self.server._can_manager
        original_monitor = self.server._monitor
        self.server._can_manager = FailingCanManager()
        self.server._monitor = Monitor()

        def close_debug(board: str) -> None:
            events.append(f"debug-close:{board}")
            if board == "owner":
                raise KeyboardInterrupt("repeated shutdown signal")

        try:
            with (
                patch.object(self.server, "require_clean_startup"),
                patch.object(self.server.signal, "signal"),
                patch.object(self.server.mcp, "run", side_effect=lambda: events.append("run")),
                patch.object(
                    self.server.connection_manager,
                    "assigned_board_ids",
                    return_value=("owner", "other"),
                ),
                patch.object(
                    self.server,
                    "_disconnect_debug_only",
                    side_effect=close_debug,
                ) as debug_disconnect,
                patch.object(
                    self.server,
                    "_record_event",
                    side_effect=lambda *args, **kwargs: events.append("cleanup-event"),
                ),
                patch.object(self.server.plan_engine, "close_run"),
                patch.object(self.server.tool_registry, "reset"),
                self.assertRaises(SystemExit) as raised,
            ):
                self.server.main()
            self.assertEqual(raised.exception.code, 1)
            self.assertEqual(
                debug_disconnect.call_args_list,
                [call("owner"), call("other")],
            )
            self.assertEqual(events.count("debug-close:owner"), 1)
            self.assertLess(events.index("cleanup-event"), events.index("monitor-closeout:eof"))
        finally:
            self.server._can_manager = original_manager
            self.server._monitor = original_monitor

    def test_repeated_signal_during_ignore_install_cannot_skip_cleanup(self) -> None:
        events: list[str] = []
        installed_handler = None

        class CleanCanManager:
            owner_board_id = None
            configured = True

            def require_profile_board(self, board_id: str) -> None:
                self.assert_valid_board = board_id

            async def disconnect(self, board_id: str) -> None:
                del board_id
                self.assert_handler_ready()
                installed_handler(self.server_signal, None)

            def status(self, board_id: str) -> dict[str, object]:
                return {"board_id": board_id}

            def shutdown_sync(self) -> bool:
                events.append("can-cleanup")
                return True

        class Monitor:
            def boot(self) -> None:
                events.append("monitor-boot")

            def closeout(self, reason: str) -> None:
                events.append(f"monitor-closeout:{reason}")

        def install_signal(signum: int, handler: object) -> None:
            nonlocal installed_handler
            if handler is not self.server.signal.SIG_IGN:
                if installed_handler is None:
                    installed_handler = handler
                return
            if signum == self.server.signal.SIGINT:
                self.assertIsNotNone(installed_handler)
                installed_handler(signum, None)

        manager = CleanCanManager()
        manager.assert_handler_ready = lambda: self.assertIsNotNone(installed_handler)
        manager.server_signal = self.server.signal.SIGINT
        handlers = build_can_handlers(
            CanToolServices(manager=manager, operational_identity=lambda _board: None)
        )

        def interrupt_run() -> None:
            asyncio.run(handlers["disconnect_can"]("owner"))  # type: ignore[misc]

        original_manager = self.server._can_manager
        original_monitor = self.server._monitor
        self.server._can_manager = manager
        self.server._monitor = Monitor()
        try:
            with (
                patch.object(self.server, "require_clean_startup"),
                patch.object(self.server.signal, "signal", side_effect=install_signal),
                patch.object(self.server.mcp, "run", side_effect=interrupt_run),
                patch.object(
                    self.server.connection_manager,
                    "assigned_board_ids",
                    return_value=("owner", "other"),
                ),
                patch.object(
                    self.server,
                    "_disconnect_debug_only",
                    side_effect=lambda board: events.append(f"debug-close:{board}"),
                ),
                patch.object(self.server.plan_engine, "close_run"),
                patch.object(self.server.tool_registry, "reset"),
            ):
                self.server.main()

            self.assertEqual(
                events,
                [
                    "monitor-boot",
                    "can-cleanup",
                    "debug-close:owner",
                    "debug-close:other",
                    "monitor-closeout:signal",
                ],
            )
        finally:
            self.server._can_manager = original_manager
            self.server._monitor = original_monitor

    def test_signal_from_monitor_begin_reaches_terminal_cleanup(self) -> None:
        events: list[str] = []
        installed_handler = None

        class CleanCanManager:
            owner_board_id = None

            def shutdown_sync(self) -> bool:
                events.append("can-cleanup")
                return True

        class TerminalMonitor:
            def boot(self) -> None:
                events.append("monitor-boot")

            def closeout(self, reason: str) -> None:
                events.append(f"monitor-closeout:{reason}")

        class DispatchMonitor:
            def begin(self, name, arguments, board_id):  # type: ignore[no-untyped-def]
                del name, arguments, board_id
                self.assert_handler_ready()
                installed_handler(self.server_signal, None)

        def install_signal(signum: int, handler: object) -> None:
            nonlocal installed_handler
            if handler is not self.server.signal.SIG_IGN and installed_handler is None:
                installed_handler = handler

        dispatch_monitor = DispatchMonitor()
        dispatch_monitor.assert_handler_ready = lambda: self.assertIsNotNone(installed_handler)
        dispatch_monitor.server_signal = self.server.signal.SIGTERM

        def run_one_dispatch() -> None:
            asyncio.run(self.server.mcp.call_tool("get_can_status", {"board_id": "board"}))

        original_manager = self.server._can_manager
        original_monitor = self.server._monitor
        original_dispatch_monitor = self.server.mcp._monitor
        self.server._can_manager = CleanCanManager()
        self.server._monitor = TerminalMonitor()
        self.server.mcp._monitor = dispatch_monitor
        try:
            with (
                patch.object(self.server, "require_clean_startup"),
                patch.object(self.server.signal, "signal", side_effect=install_signal),
                patch.object(self.server.mcp, "run", side_effect=run_one_dispatch),
                patch.object(self.server.mcp, "_call_tool_inner") as inner,
                patch.object(
                    self.server.connection_manager,
                    "assigned_board_ids",
                    return_value=("owner", "other"),
                ),
                patch.object(
                    self.server,
                    "_disconnect_debug_only",
                    side_effect=lambda board: events.append(f"debug-close:{board}"),
                ),
                patch.object(self.server.plan_engine, "close_run"),
                patch.object(self.server.tool_registry, "reset"),
            ):
                self.server.main()

            inner.assert_not_awaited()
            self.assertEqual(
                events,
                [
                    "monitor-boot",
                    "can-cleanup",
                    "debug-close:owner",
                    "debug-close:other",
                    "monitor-closeout:signal",
                ],
            )
        finally:
            self.server.mcp._monitor = original_dispatch_monitor
            self.server._can_manager = original_manager
            self.server._monitor = original_monitor


class CanAvailableIntegrationTests(unittest.TestCase):
    def test_setup_full_validated_board_can_attach_and_close_real_virtual_worker(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="byo-can-available-"))
        profile = root / "can.yaml"
        source = (
            Path(__file__).resolve().parents[1] / "fixtures" / "can" / "virtual_tx_allowed.yaml"
        )
        profile.write_text(
            source.read_text(encoding="utf-8").replace("nucleo_l476rg", "legacy_full"),
            encoding="utf-8",
        )
        environment = patch.dict(
            os.environ,
            {
                "BYO_MCP_ARTIFACT_ROOT": str(root),
                "BYO_MCP_MONITOR_ROOT": str(root / "monitor"),
                "BYO_MCP_TEST_SEAMS": "1",
                "PYOCD_CAN_MODE": "available",
                "PYOCD_CAN_CONFIG": str(profile),
                "PYOCD_CAN_CONFIG_SHA256": "",
                "PYOCD_CAN_RUNTIME_SHA256": "",
                "PYOCD_CAN_READ_SECONDS": "",
            },
            clear=False,
        )
        environment.start()
        sys.modules.pop("pyocd_debug_mcp.server", None)
        server = importlib.import_module("pyocd_debug_mcp.server")
        try:
            board_id = "legacy_full"
            self.assertAlmostEqual(
                server.mcp._timeout_resolver(
                    "read_can", {"read_seconds": None, "max_frames": None}
                ),
                5.25,
            )
            self.assertAlmostEqual(
                server.mcp._timeout_resolver("send_can", {"timeout_seconds": None}),
                5.2,
            )
            self.assertAlmostEqual(
                server.mcp._timeout_resolver("read_can", {"read_seconds": 2.0}),
                7.0,
            )
            self.assertAlmostEqual(
                server.mcp._timeout_resolver("send_can", {"timeout_seconds": 0.5}),
                6.0,
            )
            wrong_disconnect = json.loads(
                _text(
                    asyncio.run(server.mcp.call_tool("disconnect_can", {"board_id": "other_board"}))
                )
            )
            self.assertEqual(wrong_disconnect["status"], "refused")
            self.assertEqual(wrong_disconnect["code"], "can/board-mismatch")
            self.assertIsNone(server._can_manager.owner_board_id)

            with self.assertRaises(ToolError) as wrong_tier:
                asyncio.run(server.mcp.call_tool("connect_can", {"board_id": board_id}))
            wrong_tier_payload = json.loads(str(wrong_tier.exception))
            self.assertEqual(wrong_tier_payload["status"], "refused")
            self.assertEqual(wrong_tier_payload["code"], "tier/wrong-route")
            self.assertIsNone(server._can_manager.owner_board_id)

            policies = CapabilityPolicyRepository(root)
            profile_snapshot, map_snapshot = known_good_full_policy()
            policies.commit(
                board_id,
                Tier.SETUP_FULL,
                profile_snapshot=profile_snapshot,
                map_snapshot=map_snapshot,
            )
            server.configure_tiered_test_seams(policy_repository=policies)
            connection_id = "probe:test:probe-1"
            handle = TargetSessionHandle(
                session=object(),
                board=None,
                probe_uid="probe-1",
                route_used="test",
                target_override="nrf52840",
            )
            runtime = server._session_store.start_session(
                board_id=board_id,
                connection_id=connection_id,
                probe_uid="probe-1",
                route_used="test",
            )
            server.connection_manager.assign(
                board_id,
                handle,
                runtime,
                connection_id=connection_id,
            )
            server.gate_manager.stamp_validation(
                board_id=board_id,
                connection_id=connection_id,
                probe_identity="probe-1",
                observed_mcu="nRF52840",
                validation_run="validation-1",
                map_digest="test-map-digest",
            )

            common_plan = {
                "board_id": board_id,
                "hypothesis": "The isolated virtual fixture is responsive.",
                "strategy": "Perform one bounded exact operation.",
                "hypothesis_made": True,
                "strategy_evaluated": True,
                "expected_fail_return": "The operation is refused or times out.",
                "expected_success_return": "The bounded operation completes.",
                "max_calls": 1,
                "max_calls_buffer": 0,
            }
            null_plan = {
                "board_id": None,
                "hypothesis": None,
                "strategy": None,
                "hypothesis_made": None,
                "strategy_evaluated": None,
                "expected_fail_return": None,
                "expected_success_return": None,
                "max_calls": None,
                "max_calls_buffer": None,
                "action_parameters": None,
                "user_permission": None,
            }
            read_parameters = {"read_seconds": 0.02, "max_frames": 1}
            asyncio.run(server.mcp.call_tool("read_can-plan", null_plan))
            with self.assertRaisesRegex(ToolError, "CAN is not connected"):
                asyncio.run(
                    server.mcp.call_tool(
                        "read_can-plan",
                        {**common_plan, "action_parameters": read_parameters},
                    )
                )

            connected = json.loads(
                _text(asyncio.run(server.mcp.call_tool("connect_can", {"board_id": board_id})))
            )
            self.assertEqual(connected["status"], "connected")
            self.assertEqual(connected["can"]["phase"], "open")
            self.assertEqual(connected["can"]["owner_board_id"], board_id)
            self.assertEqual(connected["can"]["capabilities"]["driver"], "virtual")
            self.assertAlmostEqual(connected["can"]["profile"]["default_read_seconds"], 0.25)
            self.assertEqual(connected["can"]["profile"]["default_max_frames"], 8)
            self.assertAlmostEqual(connected["can"]["profile"]["default_send_timeout_seconds"], 0.1)

            asyncio.run(
                server.mcp.call_tool(
                    "read_can-plan",
                    {**common_plan, "action_parameters": read_parameters},
                )
            )
            captured = json.loads(
                _text(
                    asyncio.run(
                        server.mcp.call_tool("read_can", {"board_id": board_id, **read_parameters})
                    )
                )
            )
            self.assertEqual(captured["status"], "captured")
            self.assertEqual(captured["returned_frames"], 0)

            send_parameters = {
                "arbitration_id": 0x123,
                "data_hex": "CAFE",
                "extended": False,
                "timeout_seconds": None,
            }
            asyncio.run(server.mcp.call_tool("send_can-plan", null_plan))
            asyncio.run(
                server.mcp.call_tool(
                    "send_can-plan",
                    {**common_plan, "action_parameters": send_parameters},
                )
            )
            with patch.object(server, "_active_snapshot_aggregate", return_value="test-map-digest"):
                sent = json.loads(
                    _text(
                        asyncio.run(
                            server.mcp.call_tool(
                                "send_can", {"board_id": board_id, **send_parameters}
                            )
                        )
                    )
                )
            self.assertEqual(sent["status"], "sent")
            self.assertEqual(sent["frame"]["arbitration_id"], 0x123)
            self.assertNotIn("data_hex", sent["frame"])
            self.assertEqual(sent["can"]["budget"]["can_tool_calls"], 3)
            self.assertEqual(sent["can"]["budget"]["read_calls"], 1)
            self.assertEqual(sent["can"]["budget"]["tx_attempts"], 1)

            asyncio.run(
                server.mcp.call_tool(
                    "read_can-plan",
                    {
                        **common_plan,
                        "max_calls": 2,
                        "action_parameters": read_parameters,
                    },
                )
            )
            active_before_disconnect = server.plan_engine.active_plan("read_can", board_id)
            assert active_before_disconnect is not None
            self.assertEqual(active_before_disconnect.remaining_calls, 2)
            disconnected = json.loads(
                _text(asyncio.run(server.mcp.call_tool("disconnect_can", {"board_id": board_id})))
            )
            self.assertEqual(disconnected["status"], "disconnected")
            self.assertEqual(disconnected["can"]["phase"], "closed")
            self.assertIsNone(disconnected["can"]["owner_board_id"])

            with self.assertRaises(ToolError) as not_connected:
                asyncio.run(
                    server.mcp.call_tool("read_can", {"board_id": board_id, **read_parameters})
                )
            self.assertEqual(json.loads(str(not_connected.exception))["code"], "can/not-connected")
            active_after_refusal = server.plan_engine.active_plan("read_can", board_id)
            assert active_after_refusal is not None
            self.assertEqual(active_after_refusal.remaining_calls, 2)
            with self.assertRaisesRegex(ToolError, "CAN is not connected"):
                asyncio.run(
                    server.mcp.call_tool(
                        "read_can-plan",
                        {**common_plan, "action_parameters": read_parameters},
                    )
                )
            server.gate_manager.clear(board_id, "test stale validation")
        finally:
            server._can_manager.shutdown_sync()
            server._monitor.closeout("test")
            sys.modules.pop("pyocd_debug_mcp.server", None)
            environment.stop()
            shutil.rmtree(root, ignore_errors=True)


class CanCrossCuttingContractTests(unittest.TestCase):
    def test_can_operation_timeouts_include_cleanup_grace(self) -> None:
        self.assertEqual(operation_timeout_seconds("get_can_status"), 5.0)
        self.assertEqual(operation_timeout_seconds("connect_can"), 16.0)
        self.assertEqual(operation_timeout_seconds("disconnect_can"), 6.0)
        self.assertAlmostEqual(
            operation_timeout_seconds("read_can", {"read_seconds": 0.001}),
            5.001,
        )
        self.assertAlmostEqual(
            operation_timeout_seconds("send_can", {"timeout_seconds": 0.1}),
            5.2,
        )
        self.assertAlmostEqual(
            operation_timeout_seconds(
                "read_can",
                {"read_seconds": None},
                can_read_default_seconds=0.25,
            ),
            5.25,
        )
        self.assertAlmostEqual(
            operation_timeout_seconds(
                "send_can",
                {"timeout_seconds": None},
                can_send_default_seconds=0.1,
            ),
            5.2,
        )
        self.assertEqual(
            operation_timeout_seconds(
                "action_batch",
                {
                    "actions": [
                        {
                            "tool_name": "read_can",
                            "arguments": {"read_seconds": None, "max_frames": 1},
                        }
                        for _ in range(4)
                    ]
                },
                can_read_default_seconds=10.0,
            ),
            65.0,
        )
        self.assertEqual(operation_timeout_seconds("read_can", {"read_seconds": 40.0}), 5.0)
        self.assertEqual(operation_timeout_seconds("send_can", {"timeout_seconds": 8.0}), 5.0)

    def test_huge_integer_timeouts_fall_back_without_overflow(self) -> None:
        for value in (10**400, 1e308):
            with self.subTest(value_type=type(value).__name__):
                self.assertEqual(
                    operation_timeout_seconds("read_can", {"read_seconds": value}),
                    5.0,
                )
                self.assertEqual(
                    operation_timeout_seconds("send_can", {"timeout_seconds": value}),
                    5.0,
                )

    def test_monitor_distinguishes_can_refusal_and_failure(self) -> None:
        refusal = json.dumps(
            {"status": "refused", "code": "can/tx-disabled", "operation": "send_can"}
        )
        failure = json.dumps(
            {"status": "failed", "code": "can/worker-failure", "operation": "read_can"}
        )
        self.assertEqual(
            classify_result(refusal),
            (Outcome.POLICY_REFUSAL, "can/tx-disabled", None),
        )
        self.assertEqual(
            classify_result(failure),
            (Outcome.UNEXPECTED_ERROR, "can/worker-failure", None),
        )


if __name__ == "__main__":
    unittest.main()
