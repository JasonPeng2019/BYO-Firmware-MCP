"""Independent C01 regression: genuine committed-policy recovery inside public batches."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pyocd_debug_mcp.capabilities.policy import Tier
from tests.m15_independent_evidence import capture_imports
from tests.tiered_acceptance_support import isolated_project, public_text, tiered_test_server


BOARD = "m03_c01_independent"
DAMAGED = b'{"damaged_selected_generation":true}'


class HostOnlyBoundary:
    """Any accidental target operation fails, rather than reaching a physical probe."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, operation):
        def forbidden(*args, **kwargs):
            self.calls.append(operation)
            raise AssertionError(f"Host-only restoration reached target operation {operation}")

        return forbidden


def child(name, **arguments):
    return {"tool_name": name, "arguments": {"board_id": BOARD, **arguments}}


@contextmanager
def damaged_policy(test):
    with isolated_project() as root, tiered_test_server(root) as server:
        backend = HostOnlyBoundary()
        server.configure_tiered_test_seams(backend=backend)
        # A real no-setup commit suffices to reproduce C01 and creates no live
        # assignment, profile, validation stamp, target, or privileged authority.
        committed = server._capability_policies.commit(BOARD, Tier.NO_SETUP)
        pointer = server._capability_policies.pointer_path(BOARD)
        pointer_bytes = pointer.read_bytes()
        reference = json.loads(pointer_bytes)
        generation = pointer.parent / "generations" / (reference["generation_id"] + ".json")
        original = generation.read_bytes()
        test.assertEqual(hashlib.sha256(original).hexdigest(), reference["generation_digest"])
        generation.write_bytes(DAMAGED)
        test.assertEqual(server._capability_policies.resolve(BOARD).status, "corrupt")
        events = []

        def call(name, arguments):
            row = {"tool": name, "arguments": arguments}
            events.append(row)
            row["reply"] = public_text(server, name, arguments)
            return row["reply"]

        try:
            # Discover and load the actual advertised recovery route. No loader,
            # restoration handler, dispatch normalizer, or outcome is replaced.
            capabilities = json.loads(call("get_capabilities", {"board_id": BOARD}))
            test.assertEqual(capabilities["policy_status"], "corrupt")
            recovery = capabilities["recovery"]
            test.assertEqual(recovery["generation_id"], committed.generation_id)
            test.assertEqual(recovery["generation_digest"], reference["generation_digest"])
            route = recovery["load_call"]
            loaded = json.loads(call(route["tool"], route["arguments"]))
            test.assertEqual(loaded["status"], "setup_tool_loaded")
            test.assertEqual(loaded["recovery"], recovery)
            test.assertEqual(loaded["next_call"]["tool"], "board_policy_restore")
            yield SimpleNamespace(
                root=root,
                server=server,
                backend=backend,
                call=call,
                events=events,
                pointer=pointer,
                pointer_bytes=pointer_bytes,
                generation=generation,
                original=original,
                committed=committed,
            )
        finally:
            # Capture before tiered_test_server unloads its disposable server.
            capture_imports()
            if output := os.environ.get("BYO_MANAGED_TEST_EVIDENCE"):
                destination = Path(output) / test.id()
                destination.mkdir(parents=True, exist_ok=True)
                for name, data in (
                    ("pointer-original.json", pointer_bytes),
                    ("pointer-final.json", pointer.read_bytes()),
                    ("generation-original.json", original),
                    ("generation-final.json", generation.read_bytes()),
                ):
                    (destination / name).write_bytes(data)
                (destination / "public-calls.json").write_text(
                    json.dumps(events, indent=2), encoding="utf-8"
                )
                (destination / "target-calls.json").write_text(
                    json.dumps(backend.calls), encoding="utf-8"
                )


def run_batch(fixture, actions):
    # Observe every dispatch attempt, including children stopped by route guards;
    # counting backend calls alone would miss a wrongly executed ordinary wait.
    with patch.object(
        fixture.server.mcp, "_call_tool_inner", wraps=fixture.server.mcp._call_tool_inner
    ) as dispatch:
        text = fixture.call("action_batch", {"board_id": BOARD, "actions": actions})
    attempted = [(entry.args[0], entry.args[1]) for entry in dispatch.call_args_list]
    fixture.events[-1]["dispatch_attempts"] = attempted
    return json.loads(text.splitlines()[0]), attempted


class IndependentRestoreBatchTests(unittest.TestCase):
    def assert_blocked_batch(self, fixture, prefix, source):
        restore = child("board_policy_restore", generation_backup_path=source)
        original = fixture.call(restore["tool_name"], restore["arguments"])
        blocked = json.loads(original)
        self.assertEqual(blocked["status"], "policy_restore_blocked")
        self.assertTrue(blocked["validation_required"])
        self.assertFalse(blocked["ready_for_code"])
        expected_prefix = [
            {
                "index": index,
                "tool_name": item["tool_name"],
                "result": fixture.call(item["tool_name"], item["arguments"]),
            }
            for index, item in enumerate(prefix)
        ]
        successors = [child("wait", ms=3), child("wait", ms=4)]
        actions = [*prefix, restore, *successors]
        result, attempted = run_batch(fixture, actions)
        self.assertEqual(
            attempted,
            [("action_batch", {"board_id": BOARD, "actions": actions})]
            + [(item["tool_name"], item["arguments"]) for item in [*prefix, restore]],
            "Blocked restoration must stop before BOTH actual successor dispatches",
        )
        self.assertEqual(result["status"], "batch_failed")
        self.assertEqual(result["board_id"], BOARD)
        self.assertEqual(result["completed"], expected_prefix)
        self.assertEqual(
            result["failure"],
            {
                "index": len(prefix),
                "tool_name": "board_policy_restore",
                "result": original,
                "error_type": "ChildResultError",
                "message": "Child returned policy_restore_blocked.",
            },
        )
        self.assertEqual(fixture.pointer.read_bytes(), fixture.pointer_bytes)
        self.assertEqual(fixture.generation.read_bytes(), DAMAGED)
        self.assertEqual(fixture.server._capability_policies.resolve(BOARD).status, "corrupt")
        self.assertEqual(fixture.backend.calls, [])

    def test_absent_backup_stops_at_first_child_without_successor_dispatch(self):
        with damaged_policy(self) as fixture:
            self.assert_blocked_batch(fixture, [], None)

    def test_missing_backup_keeps_two_successful_children_and_exact_failure(self):
        with damaged_policy(self) as fixture:
            source = "trusted-backups/missing-selected-generation.json"
            self.assertFalse((fixture.root / source).exists())
            self.assert_blocked_batch(fixture, [child("wait", ms=1), child("wait", ms=2)], source)

    def test_exact_backup_restores_selected_bytes_and_allows_ordinary_successor(self):
        with damaged_policy(self) as fixture:
            backup = fixture.root / "trusted-selected-generation.json"
            backup.write_bytes(fixture.original)
            actions = [
                child("board_policy_restore", generation_backup_path=backup.name),
                child("wait", ms=3),
            ]
            result, attempted = run_batch(fixture, actions)
            self.assertEqual(
                attempted,
                [("action_batch", {"board_id": BOARD, "actions": actions})]
                + [(item["tool_name"], item["arguments"]) for item in actions],
            )
            self.assertEqual(result["status"], "batch_completed")
            self.assertIsNone(result["failure"])
            self.assertEqual(
                [(item["index"], item["tool_name"]) for item in result["completed"]],
                [(0, "board_policy_restore"), (1, "wait")],
            )
            restored = json.loads(result["completed"][0]["result"])
            self.assertEqual(restored["status"], "policy_restore_completed")
            self.assertEqual(restored["generation_id"], fixture.committed.generation_id)
            self.assertTrue(restored["validation_required"])
            self.assertFalse(restored["ready_for_code"])
            self.assertEqual(
                result["completed"][1]["result"],
                fixture.call("wait", actions[1]["arguments"]),
            )
            self.assertEqual(fixture.pointer.read_bytes(), fixture.pointer_bytes)
            self.assertEqual(fixture.generation.read_bytes(), fixture.original)
            self.assertEqual(backup.read_bytes(), fixture.original)
            self.assertEqual(fixture.server._capability_policies.resolve(BOARD), fixture.committed)
            status = json.loads(fixture.call("get_setup_status", {"board_id": BOARD}))
            self.assertFalse(status["ready_for_code"])
            self.assertIsNone(fixture.server.connection_manager.maybe_connection(BOARD))
            self.assertEqual(fixture.backend.calls, [])


if __name__ == "__main__":
    unittest.main()
