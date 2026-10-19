"""Independent C02 proof at the registered loader/batch composition boundary."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
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
)
from tests.tiered_acceptance_support import (
    _content_text,
    isolated_project,
    public_text,
    tiered_test_server,
)


def observed_batch(fixture, actions):
    """Delegate before route guards; retain actual replies, including exceptions."""
    trace = []
    original = fixture.server.mcp._call_tool_inner

    async def observe(name, arguments, board_id):
        entry = {"tool_name": name, "arguments": arguments}
        trace.append(entry)
        try:
            reply = await original(name, arguments, board_id)
        except Exception as exc:
            entry["exception"] = {"type": type(exc).__name__, "message": str(exc)}
            raise
        entry["reply"] = _content_text(reply)
        return reply

    arguments = {"board_id": BOARD, "actions": actions}
    record = {"arguments": arguments, "trace": trace}
    try:
        with patch.object(fixture.server.mcp, "_call_tool_inner", new=observe):
            text = public_text(fixture.server, "action_batch", arguments)
        record["reply"] = text
        return json.loads(text.splitlines()[0]), trace[1:]
    finally:
        # Record even the counterfactual's failed assertions. No import hashing
        # runs on a short-lived worker's import path.
        record["target_calls"] = list(fixture.backend.calls)
        capture_imports()
        if selected := os.environ.get("BYO_MANAGED_TEST_EVIDENCE"):
            directory = Path(selected) / "loader-batch-independent"
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / "workflows.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record) + "\n")


class IndependentLoaderBatchTests(unittest.TestCase):
    def assert_stopped(self, result, trace, actions, index):
        # Observe both successors before testing the envelope: a later guard
        # refusing an attempted wait would not establish stop-on-failure.
        self.assertEqual(
            [(row["tool_name"], row["arguments"]) for row in trace],
            [(item["tool_name"], item["arguments"]) for item in actions[: index + 1]],
            "The refused loader must prevent BOTH ordinary successor dispatch attempts",
        )
        self.assertEqual(result["status"], "batch_failed")
        self.assertEqual(result["board_id"], BOARD)
        self.assertEqual(
            result["completed"],
            [
                {"index": i, "tool_name": row["tool_name"], "result": row["reply"]}
                for i, row in enumerate(trace[:index])
            ],
        )
        failure = result["failure"]
        self.assertEqual((failure["index"], failure["tool_name"]), (index, "load_setup_tool"))
        if "reply" in trace[index]:
            self.assertEqual(failure["result"], trace[index]["reply"])
        else:
            self.assertEqual(failure["error_type"], trace[index]["exception"]["type"])
            self.assertEqual(failure["result"], trace[index]["exception"]["message"])
        return failure

    def recovery_case(self, fixture, route, durations):
        actions = [
            *(child("wait", ms=ms) for ms in durations),
            child("load_setup_tool", tool_name=route),
            child("wait", ms=7),
            child("wait", ms=9),
        ]
        result, trace = observed_batch(fixture, actions)
        stopped = trace[len(durations)]
        refusal = json.loads(stopped["reply"])
        self.assertEqual(refusal["status"], "setup_policy_recovery_required")
        self.assertEqual(refusal["board_id"], BOARD)
        self.assertEqual(refusal["recovery"]["generation_id"], fixture.committed.generation_id)
        self.assertEqual(
            refusal["recovery"]["generation_digest"],
            json.loads(fixture.pointer_bytes)["generation_digest"],
        )
        self.assertEqual(
            refusal["recovery"]["load_call"]["arguments"]["tool_name"], "board_policy_restore"
        )
        # Establish the real prefix, policy preservation and host boundary even
        # when the old batch will fail the dispatch assertion below.
        for row, ms in zip(trace, durations):
            self.assertIn(f"Waited {ms} ms", row["reply"])
        self.assertEqual(fixture.pointer.read_bytes(), fixture.pointer_bytes)
        self.assertEqual(fixture.generation.read_bytes(), DAMAGED)
        self.assertEqual(fixture.server._capability_policies.resolve(BOARD).status, "corrupt")
        self.assertIsNone(fixture.server.connection_manager.maybe_connection(BOARD))
        self.assertEqual(fixture.backend.calls, [])
        failure = self.assert_stopped(result, trace, actions, len(durations))
        self.assertEqual(failure["error_type"], "ChildResultError")
        self.assertEqual(failure["message"], "Child returned setup_policy_recovery_required.")

    def test_real_recovery_refusal_at_zero_stops_both_waits_on_each_load_route(self):
        with damaged_policy(self) as fixture:
            for route in ("board_validate", "board_setup-plan", "board_safety_refresh"):
                with self.subTest(route=route):
                    self.recovery_case(fixture, route, [])

    def test_real_recovery_refusal_preserves_two_completed_waits_on_each_load_route(self):
        with damaged_policy(self) as fixture:
            for route in ("board_safety_refresh", "board_validate", "board_setup-plan"):
                with self.subTest(route=route):
                    self.recovery_case(fixture, route, [1, 2])

    def test_healthy_loader_metadata_and_waits_all_complete(self):
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = HostOnlyBoundary()
            server.configure_tiered_test_seams(backend=backend)
            server._capability_policies.commit(BOARD, Tier.NO_SETUP)
            pointer = server._capability_policies.pointer_path(BOARD)
            before = pointer.read_bytes()
            fixture = SimpleNamespace(server=server, backend=backend)
            actions = [child("wait", ms=1)]
            for route in ("board_setup-plan", "board_safety_refresh", "board_policy_restore"):
                actions.extend(
                    [
                        child("load_setup_tool", tool_name=route),
                        child("get_capabilities"),
                        child("wait", ms=2),
                    ]
                )
            result, trace = observed_batch(fixture, actions)
            self.assertEqual(result["status"], "batch_completed")
            self.assertIsNone(result["failure"])
            self.assertEqual(len(trace), len(actions))
            self.assertEqual(
                [(row["index"], row["tool_name"]) for row in result["completed"]],
                list(enumerate(action["tool_name"] for action in actions)),
            )
            for action, row, completed in zip(actions, trace, result["completed"]):
                self.assertEqual(row["tool_name"], action["tool_name"])
                self.assertEqual(row["arguments"], action["arguments"])
                if row["tool_name"] == "load_setup_tool":
                    self.assertEqual(completed["result"], row["reply"])
                    loaded = json.loads(row["reply"])
                    self.assertEqual(loaded["status"], "setup_tool_loaded")
                    self.assertEqual(loaded["tool_name"], action["arguments"]["tool_name"])
                    self.assertEqual(loaded["next_call"]["tool"], loaded["tool_name"])
                elif row["tool_name"] == "get_capabilities":
                    self.assertEqual(json.loads(row["reply"])["policy_status"], "committed")
                else:
                    self.assertEqual(completed["result"], row["reply"])
                    self.assertIn(f"Waited {action['arguments']['ms']} ms", row["reply"])
            self.assertEqual(pointer.read_bytes(), before)
            self.assertEqual(backend.calls, [])
            capture_imports()

    def test_real_missing_assignment_and_invalid_load_name_stop_after_wait(self):
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = HostOnlyBoundary()
            server.configure_tiered_test_seams(backend=backend)
            fixture = SimpleNamespace(server=server, backend=backend)
            for route in ("board_validate", "not-a-loadable-tool"):
                with self.subTest(route=route):
                    actions = [
                        child("wait", ms=1),
                        child("load_setup_tool", tool_name=route),
                        child("wait", ms=7),
                        child("wait", ms=9),
                    ]
                    result, trace = observed_batch(fixture, actions)
                    failure = self.assert_stopped(result, trace, actions, 1)
                    if route == "board_validate":
                        self.assertEqual(
                            json.loads(failure["result"])["status"], "setup_assignment_required"
                        )
                        self.assertEqual(failure["error_type"], "ChildResultError")
                    else:
                        self.assertEqual(failure["error_type"], "ToolError")
                        self.assertIn("tool_name must be one of", failure["message"])
                    self.assertEqual(backend.calls, [])

    def test_declared_loader_incomplete_and_future_results_are_not_success(self):
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = HostOnlyBoundary()
            server.configure_tiered_test_seams(backend=backend)
            fixture = SimpleNamespace(server=server, backend=backend)
            producer = server.mcp._tool_manager.get_tool("load_setup_tool")
            # Only this producer seam is substituted; dispatch, normalization,
            # batch orchestration and both later ordinary tools stay real.
            for reply in ('{"tool_name":"board_setup-plan"}', '{"status":"future_load_result"}'):
                with self.subTest(reply=reply), patch.object(producer, "fn", return_value=reply):
                    actions = [
                        child("load_setup_tool", tool_name="board_setup-plan"),
                        child("wait", ms=7),
                        child("wait", ms=9),
                    ]
                    result, trace = observed_batch(fixture, actions)
                    failure = self.assert_stopped(result, trace, actions, 0)
                    self.assertEqual(failure["error_type"], "MalformedChildResult")
                    self.assertEqual(failure["result"], reply)
                    self.assertEqual(backend.calls, [])


if __name__ == "__main__":
    unittest.main()
