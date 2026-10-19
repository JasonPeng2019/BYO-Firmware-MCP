"""Implementation checks for public batch ordering and retained child outcomes."""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from pyocd_debug_mcp.capabilities.policy import Tier
from tests.m15_independent_evidence import capture_imports
from tests.test_m03_restore_batch_independent import (
    BOARD,
    DAMAGED,
    HostOnlyBoundary,
    child,
    damaged_policy,
    run_batch,
)
from tests.test_tiered_acceptance_lite import _setup_inventory
from tests.tiered_acceptance_support import (
    DeterministicTargetBackend,
    isolated_project,
    public_text,
    tiered_test_server,
)


class BatchFailureTests(unittest.TestCase):
    def test_loader_recovery_refusal_retains_exact_prefix_and_stops_both_successors(self) -> None:
        with damaged_policy(self) as fixture:
            for tool in ("board_validate", "board_setup-plan", "board_safety_refresh"):
                for prefix in ([], [child("wait", ms=1), child("wait", ms=2)]):
                    with self.subTest(tool=tool, prefix=len(prefix)):
                        request = child("load_setup_tool", tool_name=tool)
                        original = fixture.call(request["tool_name"], request["arguments"])
                        self.assertEqual(
                            json.loads(original)["status"], "setup_policy_recovery_required"
                        )
                        expected_prefix = [
                            {
                                "index": index,
                                "tool_name": item["tool_name"],
                                "result": fixture.call(item["tool_name"], item["arguments"]),
                            }
                            for index, item in enumerate(prefix)
                        ]
                        actions = [*prefix, request, child("wait", ms=3), child("wait", ms=4)]
                        result, attempts = run_batch(fixture, actions)
                        self.assertEqual(
                            attempts,
                            [("action_batch", {"board_id": BOARD, "actions": actions})]
                            + [
                                (item["tool_name"], item["arguments"])
                                for item in [*prefix, request]
                            ],
                        )
                        self.assertEqual(result["status"], "batch_failed")
                        self.assertEqual(result["completed"], expected_prefix)
                        self.assertEqual(
                            result["failure"],
                            {
                                "index": len(prefix),
                                "tool_name": "load_setup_tool",
                                "result": original,
                                "error_type": "ChildResultError",
                                "message": "Child returned setup_policy_recovery_required.",
                            },
                        )
                        self.assertFalse(fixture.server.setup_tool_loader.is_loaded(BOARD, tool))
                        self.assertEqual(fixture.pointer.read_bytes(), fixture.pointer_bytes)
                        self.assertEqual(fixture.generation.read_bytes(), DAMAGED)
                        self.assertEqual(fixture.backend.calls, [])

    def test_healthy_loader_and_metadata_complete_through_public_dispatch(self) -> None:
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = HostOnlyBoundary()
            server.configure_tiered_test_seams(backend=backend)
            server._capability_policies.commit("board", Tier.NO_SETUP)
            actions = [
                {"tool_name": "get_capabilities", "arguments": {"board_id": "board"}},
                *[
                    {
                        "tool_name": "load_setup_tool",
                        "arguments": {"board_id": "board", "tool_name": tool},
                    }
                    for tool in ("board_setup-plan", "board_safety_refresh", "board_policy_restore")
                ],
                {"tool_name": "wait", "arguments": {"board_id": "board", "ms": 1}},
            ]
            with patch.object(
                server.mcp, "_call_tool_inner", wraps=server.mcp._call_tool_inner
            ) as dispatch:
                result = json.loads(
                    public_text(
                        server, "action_batch", {"board_id": "board", "actions": actions}
                    ).splitlines()[0]
                )
            self.assertEqual(result["status"], "batch_completed")
            self.assertIsNone(result["failure"])
            self.assertEqual(
                [(call.args[0], call.args[1]) for call in dispatch.call_args_list[1:]],
                [(item["tool_name"], item["arguments"]) for item in actions],
            )
            self.assertEqual([item["index"] for item in result["completed"]], list(range(5)))
            for entry, action in zip(result["completed"][1:4], actions[1:4]):
                self.assertEqual(json.loads(entry["result"])["status"], "setup_tool_loaded")
                self.assertTrue(
                    server.setup_tool_loader.is_loaded("board", action["arguments"]["tool_name"])
                )
            self.assertIn("Waited 1 ms", result["completed"][-1]["result"])
            self.assertEqual(backend.calls, [])
            capture_imports()

    def test_loader_assignment_refusal_and_real_dispatch_exception_stop(self) -> None:
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = HostOnlyBoundary()
            server.configure_tiered_test_seams(backend=backend)
            for tool, error in (("board_validate", "ChildResultError"), ("invalid", "ToolError")):
                with self.subTest(tool=tool):
                    request = {"board_id": "board", "tool_name": tool}
                    wait = {"tool_name": "wait", "arguments": {"board_id": "board", "ms": 1}}
                    original = (
                        public_text(server, "load_setup_tool", request)
                        if tool == "board_validate"
                        else None
                    )
                    actions = [
                        wait,
                        {"tool_name": "load_setup_tool", "arguments": request},
                        wait,
                        wait,
                    ]
                    with patch.object(
                        server.mcp, "_call_tool_inner", wraps=server.mcp._call_tool_inner
                    ) as dispatch:
                        result = json.loads(
                            public_text(
                                server, "action_batch", {"board_id": "board", "actions": actions}
                            ).splitlines()[0]
                        )
                    self.assertEqual(
                        [call.args[0] for call in dispatch.call_args_list],
                        ["action_batch", "wait", "load_setup_tool"],
                    )
                    self.assertEqual(result["status"], "batch_failed")
                    self.assertEqual(len(result["completed"]), 1)
                    self.assertEqual(result["failure"]["index"], 1)
                    self.assertEqual(result["failure"]["error_type"], error)
                    if original is not None:
                        self.assertEqual(
                            json.loads(original)["status"], "setup_assignment_required"
                        )
                        self.assertEqual(result["failure"]["result"], original)
                    else:
                        self.assertIn("tool_name must be one of", result["failure"]["result"])
                    self.assertEqual(backend.calls, [])
            capture_imports()

    def test_loader_declared_refusal_and_malformed_results_stop_unchanged(self) -> None:
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = HostOnlyBoundary()
            server.configure_tiered_test_seams(backend=backend)
            tool = server.mcp._tool_manager.get_tool("load_setup_tool")
            self.assertIsNotNone(tool)
            # Only the child producer is substituted to check its declared contract.
            for reply in (
                '{"status":"setup_plan_ineligible"}',
                "{}",
                '{"status":"unknown"}',
                '{"status":null}',
                '{"status":"ok"}',
                "[]",
                "legacy success",
                '{"status":',
            ):
                with self.subTest(reply=reply), patch.object(tool, "fn", return_value=reply):
                    wait = {"tool_name": "wait", "arguments": {"board_id": "board", "ms": 1}}
                    actions = [
                        wait,
                        {
                            "tool_name": "load_setup_tool",
                            "arguments": {"board_id": "board", "tool_name": "board_setup-plan"},
                        },
                        wait,
                        wait,
                    ]
                    with patch.object(
                        server.mcp, "_call_tool_inner", wraps=server.mcp._call_tool_inner
                    ) as dispatch:
                        result = json.loads(
                            public_text(
                                server, "action_batch", {"board_id": "board", "actions": actions}
                            ).splitlines()[0]
                        )
                    self.assertEqual(
                        [call.args[0] for call in dispatch.call_args_list],
                        ["action_batch", "wait", "load_setup_tool"],
                    )
                    self.assertEqual(result["status"], "batch_failed")
                    self.assertEqual(len(result["completed"]), 1)
                    self.assertEqual(result["failure"]["index"], 1)
                    self.assertEqual(result["failure"]["result"], reply)
                    self.assertEqual(
                        result["failure"]["error_type"],
                        "ChildResultError"
                        if "setup_plan_ineligible" in reply
                        else "MalformedChildResult",
                    )
                    self.assertEqual(backend.calls, [])
            capture_imports()

    def test_blocked_policy_restore_keeps_prefix_and_stops_successor_dispatch(self) -> None:
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = DeterministicTargetBackend()
            server.configure_tiered_test_seams(backend=backend)
            policy = server._capability_policies.commit("board", Tier.NO_SETUP)
            pointer = server._capability_policies.pointer_path("board")
            pointer_bytes = pointer.read_bytes()
            generation = pointer.parent / "generations" / f"{policy.generation_id}.json"
            generation.write_bytes(b"{}")
            public_text(
                server,
                "load_setup_tool",
                {"board_id": "board", "tool_name": "board_policy_restore"},
            )
            wait = {"tool_name": "wait", "arguments": {"board_id": "board", "ms": 1}}
            for source, prefix in ((None, []), ("missing-backup.json", [wait])):
                with self.subTest(source=source):
                    arguments = {"board_id": "board", "generation_backup_path": source}
                    original = public_text(server, "board_policy_restore", arguments)
                    self.assertEqual(json.loads(original)["status"], "policy_restore_blocked")
                    actions = [
                        *prefix,
                        {"tool_name": "board_policy_restore", "arguments": arguments},
                        wait,
                    ]
                    with patch.object(
                        server.mcp, "_call_tool_inner", wraps=server.mcp._call_tool_inner
                    ) as dispatch:
                        payload = json.loads(
                            public_text(
                                server, "action_batch", {"board_id": "board", "actions": actions}
                            ).split("\n", 1)[0]
                        )
                    self.assertEqual(payload["status"], "batch_failed")
                    self.assertEqual(
                        [(call.args[0], call.args[1]) for call in dispatch.call_args_list[1:]],
                        [(entry["tool_name"], entry["arguments"]) for entry in actions[:-1]],
                    )
                    self.assertEqual(len(payload["completed"]), len(prefix))
                    if prefix:
                        self.assertEqual(payload["completed"][0]["index"], 0)
                        self.assertEqual(payload["completed"][0]["tool_name"], "wait")
                        self.assertIn("Waited 1 ms", payload["completed"][0]["result"])
                    self.assertEqual(
                        {key: payload["failure"][key] for key in ("index", "tool_name", "result")},
                        {
                            "index": len(prefix),
                            "tool_name": "board_policy_restore",
                            "result": original,
                        },
                    )
                    self.assertEqual(payload["failure"]["error_type"], "ChildResultError")
                    self.assertEqual(pointer.read_bytes(), pointer_bytes)
                    self.assertEqual(generation.read_bytes(), b"{}")
                    self.assertEqual(backend.calls, [])

    def test_completed_policy_restore_allows_wait_without_claiming_live_readiness(self) -> None:
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = DeterministicTargetBackend()
            server.configure_tiered_test_seams(backend=backend)
            policy = server._capability_policies.commit("board", Tier.NO_SETUP)
            pointer = server._capability_policies.pointer_path("board")
            pointer_bytes = pointer.read_bytes()
            generation = pointer.parent / "generations" / f"{policy.generation_id}.json"
            original = generation.read_bytes()
            backup = root / "selected-generation.json"
            backup.write_bytes(original)
            generation.write_bytes(b"{}")
            public_text(
                server,
                "load_setup_tool",
                {"board_id": "board", "tool_name": "board_policy_restore"},
            )
            actions = [
                {
                    "tool_name": "board_policy_restore",
                    "arguments": {"board_id": "board", "generation_backup_path": str(backup)},
                },
                {"tool_name": "wait", "arguments": {"board_id": "board", "ms": 1}},
            ]
            with patch.object(
                server.mcp, "_call_tool_inner", wraps=server.mcp._call_tool_inner
            ) as dispatch:
                payload = json.loads(
                    public_text(
                        server, "action_batch", {"board_id": "board", "actions": actions}
                    ).split("\n", 1)[0]
                )
            self.assertEqual(payload["status"], "batch_completed")
            self.assertIsNone(payload["failure"])
            self.assertEqual([entry["index"] for entry in payload["completed"]], [0, 1])
            self.assertEqual(
                [call.args[0] for call in dispatch.call_args_list],
                ["action_batch", "board_policy_restore", "wait"],
            )
            restored = json.loads(payload["completed"][0]["result"])
            self.assertEqual(restored["status"], "policy_restore_completed")
            self.assertTrue(restored["validation_required"])
            self.assertFalse(restored["ready_for_code"])
            self.assertIn("Waited 1 ms", payload["completed"][1]["result"])
            self.assertEqual(pointer.read_bytes(), pointer_bytes)
            self.assertEqual(generation.read_bytes(), original)
            self.assertEqual(backup.read_bytes(), original)
            self.assertEqual(backend.calls, [])

    def test_unloaded_or_malformed_policy_restore_stops_successor_dispatch(self) -> None:
        with isolated_project() as root, tiered_test_server(root) as server:
            tool = server.mcp._tool_manager.get_tool("board_policy_restore")
            self.assertIsNotNone(tool)
            actions = [
                {"tool_name": "board_policy_restore", "arguments": {"board_id": "board"}},
                {"tool_name": "wait", "arguments": {"board_id": "board", "ms": 1}},
            ]

            def run():
                with patch.object(
                    server.mcp, "_call_tool_inner", wraps=server.mcp._call_tool_inner
                ) as dispatch:
                    payload = json.loads(
                        public_text(
                            server, "action_batch", {"board_id": "board", "actions": actions}
                        ).split("\n", 1)[0]
                    )
                self.assertEqual(
                    [call.args[0] for call in dispatch.call_args_list],
                    ["action_batch", "board_policy_restore"],
                )
                self.assertEqual(payload["status"], "batch_failed")
                self.assertEqual(payload["completed"], [])
                self.assertEqual(payload["failure"]["index"], 0)
                return payload["failure"]

            unloaded = run()
            self.assertEqual(json.loads(unloaded["result"])["status"], "setup_tool_not_loaded")
            self.assertEqual(unloaded["error_type"], "ChildResultError")
            for result in ("{}", '{"status":"unknown"}', "legacy success", "[]"):
                with self.subTest(result=result), patch.object(tool, "fn", return_value=result):
                    failure = run()
                    self.assertEqual(failure["result"], result)
                    self.assertEqual(failure["error_type"], "MalformedChildResult")

    def test_malformed_raw_result_stops_before_next_public_child(self) -> None:
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = DeterministicTargetBackend()
            server.configure_tiered_test_seams(
                backend=backend, auto_target_resolver=lambda _board: "nrf52840"
            )
            with patch.object(server, "_hardware_inventory", _setup_inventory()):
                asyncio.run(
                    server.mcp.call_tool(
                        "connect",
                        {
                            "board_id": "board",
                            "probe_uid": "LITE-ACCEPTANCE-PROBE",
                            "target": "nrf52840",
                        },
                    )
                )
            tool = server.mcp._tool_manager.get_tool("read_memory_raw")
            self.assertIsNotNone(tool)
            for result in ("not a raw envelope", "{}", '{"status":"unknown"}', "[]"):
                with self.subTest(result=result), patch.object(tool, "fn", return_value=result):
                    backend.calls.clear()
                    payload = json.loads(
                        public_text(
                            server,
                            "action_batch",
                            {
                                "board_id": "board",
                                "actions": [
                                    {
                                        "tool_name": "read_memory_raw",
                                        "arguments": {
                                            "board_id": "board",
                                            "address": 0x20000000,
                                        },
                                    },
                                    {
                                        "tool_name": "write_memory_raw",
                                        "arguments": {
                                            "board_id": "board",
                                            "address": 0x20000000,
                                            "value": 7,
                                        },
                                    },
                                ],
                            },
                        ).split("\n", 1)[0]
                    )
                    self.assertEqual(payload["status"], "batch_failed")
                    self.assertEqual(payload["completed"], [])
                    self.assertEqual(payload["failure"]["index"], 0)
                    self.assertEqual(payload["failure"]["result"], result)
                    self.assertEqual(payload["failure"]["error_type"], "MalformedChildResult")
                    self.assertFalse(any(call[0] == "write_memory" for call in backend.calls))

    def test_returned_outcomes_stop_before_dependent_child(self) -> None:
        with isolated_project() as root, tiered_test_server(root) as server:
            seen: list[str] = []

            async def batch_fixture(board_id: str, result: str) -> str:
                del board_id
                seen.append(result)
                if result == "raise":
                    raise RuntimeError("fixture child exception")
                return result

            server.mcp.add_tool(batch_fixture, structured_output=False)
            for result, status in (
                ('{"status":"error","code":"fixture/error"}', "batch_failed"),
                ('{"status":"refused","code":"fixture/refused"}', "batch_failed"),
                ("Refused [fixture/refused]: stopped", "batch_failed"),
                ('{"status":null}', "batch_failed"),
                ('{"status":', "batch_failed"),
                ('{"status":"setup_blocked"}', "batch_failed"),
                ('{"status":"setup_unresolved"}', "batch_failed"),
                ('{"status":"setup_research_required","continuation_id":"c"}', "batch_pending"),
                ('{"status":"setup_needs_user_input","continuation_id":"c"}', "batch_pending"),
                ('{"status":"setup_continuation_accepted"}', "batch_pending"),
                ('{"status":"validation_incomplete"}', "batch_failed"),
                ("raise", "batch_failed"),
            ):
                with self.subTest(result=result):
                    seen.clear()
                    actions = [
                        {
                            "tool_name": "batch_fixture",
                            "arguments": {"board_id": "board", "result": value},
                        }
                        for value in ('{"status":"ok","effect":"retained"}', result, "later")
                    ]
                    payload = json.loads(
                        public_text(
                            server, "action_batch", {"board_id": "board", "actions": actions}
                        ).split("\n", 1)[0]
                    )
                    self.assertEqual(seen, [actions[0]["arguments"]["result"], result])
                    self.assertEqual(payload["status"], status)
                    self.assertEqual(
                        payload["completed"],
                        [{"index": 0, "tool_name": "batch_fixture", "result": seen[0]}],
                    )
                    self.assertEqual(payload["failure"]["index"], 1)
                    self.assertEqual(payload["failure"]["tool_name"], "batch_fixture")
                    if result != "raise":
                        self.assertEqual(payload["failure"]["result"], result)
                    else:
                        self.assertIn("fixture child exception", payload["failure"]["message"])

    def test_success_preserves_legacy_text_and_metadata_without_status(self) -> None:
        with isolated_project() as root, tiered_test_server(root) as server:

            async def batch_fixture(board_id: str, result: str) -> str:
                del board_id
                return result

            server.mcp.add_tool(batch_fixture, structured_output=False)
            results = [
                "legacy result",
                '{"capabilities":["read"]}',
                '{"status":"ok"}',
                '{"status":"unrecognized_metadata_status"}',
            ]
            payload = json.loads(
                public_text(
                    server,
                    "action_batch",
                    {
                        "board_id": "board",
                        "actions": [
                            {
                                "tool_name": "batch_fixture",
                                "arguments": {"board_id": "board", "result": r},
                            }
                            for r in results
                        ],
                    },
                ).split("\n", 1)[0]
            )
            self.assertEqual(payload["status"], "batch_completed")
            self.assertIsNone(payload["failure"])
            self.assertEqual([entry["result"] for entry in payload["completed"]], results)

    def test_raw_failure_retains_write_prefix_and_never_attempts_later_write(self) -> None:
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = DeterministicTargetBackend()
            server.configure_tiered_test_seams(
                backend=backend, auto_target_resolver=lambda _board: "nrf52840"
            )
            with patch.object(server, "_hardware_inventory", _setup_inventory()):
                asyncio.run(
                    server.mcp.call_tool(
                        "connect",
                        {
                            "board_id": "board",
                            "probe_uid": "LITE-ACCEPTANCE-PROBE",
                            "target": "nrf52840",
                        },
                    )
                )
            backend.calls.clear()
            actions = [
                {
                    "tool_name": "write_memory_raw",
                    "arguments": {"board_id": "board", "address": 0x20000000, "value": 7},
                },
                {
                    "tool_name": "read_memory_raw",
                    "arguments": {"board_id": "board", "address": -1},
                },
                {
                    "tool_name": "write_memory_raw",
                    "arguments": {"board_id": "board", "address": 0x20000004, "value": 9},
                },
            ]
            payload = json.loads(
                public_text(
                    server,
                    "action_batch",
                    {
                        "board_id": "board",
                        "actions": actions,
                    },
                ).split("\n", 1)[0]
            )
            self.assertEqual(payload["status"], "batch_failed")
            self.assertEqual(payload["failure"]["index"], 1)
            self.assertEqual(json.loads(payload["failure"]["result"])["status"], "error")
            self.assertEqual([entry["index"] for entry in payload["completed"]], [0])
            self.assertEqual(json.loads(payload["completed"][0]["result"])["status"], "ok")
            writes = [call for call in backend.calls if call[0] == "write_memory"]
            self.assertEqual(len(writes), 1)
            self.assertEqual(writes[0][2:], (0x20000000, 7, 32))
