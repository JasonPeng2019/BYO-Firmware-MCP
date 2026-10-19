"""Raw response failures retain their contract and release only stale ownership.

The independent provider-budget regression covers real worker termination. These
host controls exercise readiness, runtime ownership, warnings and batch dispatch.
"""

from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pyocd_debug_mcp.capabilities.policy import Tier
from pyocd_debug_mcp.hardware_inventory import snapshot_from_validation_inventory
from pyocd_debug_mcp.kernel.operations import current_operation, operation_manager
from pyocd_debug_mcp.setup_flow.validate import ValidationInventory, ValidationProbe
from pyocd_debug_mcp.target_errors import (
    FlashTargetSupportError,
    FlashVerificationError,
    TargetConnectionError,
    TargetInputError,
    UnsupportedArtifactError,
)
from tests.tiered_acceptance_support import (
    DeterministicTargetBackend,
    assert_lite_raw_warning,
    confirmed_lite_policy,
    isolated_project,
    public_text,
    tiered_test_server,
)


BOARD = "raw-cleanup"


class RawConnectionCleanupTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = self.stack.enter_context(isolated_project())
        self.server = self.stack.enter_context(tiered_test_server(self.root))
        self.backend = DeterministicTargetBackend()
        self.server.configure_tiered_test_seams(backend=self.backend)
        inventory = ValidationInventory(
            probes=tuple(
                ValidationProbe(uid, "Controlled", "jlink", uid)
                for uid in ("cleanup-probe", "other-probe")
            )
        )
        self.stack.enter_context(
            patch.object(
                self.server,
                "_hardware_inventory",
                SimpleNamespace(
                    snapshot=lambda: snapshot_from_validation_inventory(inventory),
                    validation_inventory=lambda: inventory,
                ),
            )
        )
        self.stack.enter_context(
            patch.object(self.server, "_validation_inventory", return_value=inventory)
        )
        self.connect()
        self.connection = self.server.connection_manager.connection_for(BOARD)
        self.server._capability_policies.commit(
            BOARD, Tier.SETUP_LITE, map_snapshot=confirmed_lite_policy(BOARD)
        )
        self.stamp(BOARD)
        self.image = self.root / "image.bin"
        self.image.write_bytes(bytes(range(128)))
        self.operations = []
        self.cause = OSError("probe dropped during programming")
        self.failure = TargetConnectionError("programming lost target access")

    def tearDown(self):
        for board_id in self.server.connection_manager.assigned_board_ids():
            self.call("disconnect", board_id=board_id)
        self.server._monitor.closeout("raw cleanup controls finished")

    def call(self, tool, *, board_id=BOARD, **arguments):
        return public_text(self.server, tool, {"board_id": board_id, **arguments})

    def connect(self, board_id=BOARD, uid="cleanup-probe"):
        self.assertIn(
            "Connected", self.call("connect", board_id=board_id, probe_uid=uid, target="cortex_m")
        )

    def stamp(self, board_id):
        connection = self.server.connection_manager.connection_for(board_id)
        return self.server.gate_manager.stamp_validation(
            board_id=board_id,
            connection_id=connection.connection_id,
            probe_identity=connection.handle.probe_uid,
            observed_mcu="controlled-device",
            validation_run="controlled-validation",
            map_digest="controlled-map",
        )

    def fail_flash(self, *_args, **_kwargs):
        self.operations.append(current_operation())
        raise self.failure from self.cause

    def flash(self, **arguments):
        return json.loads(
            self.call("flash_raw", artifact=str(self.image), base_address=0x08000000, **arguments)
        )

    def assert_failure_response(self, result):
        self.assertEqual((result["status"], result["code"]), ("error", "backend/flash-failed"))
        self.assertEqual(result["message"], str(self.failure))
        self.assertEqual((result["board_id"], result["operation"]), (BOARD, "flash_raw"))
        self.assertEqual(result["schema_version"], 1)
        self.assertTrue(result["raw"])
        assert_lite_raw_warning(self, result, "flash_raw")

    def test_typed_failure_preserves_response_cause_and_releases_only_owned_state(self):
        self.connect("other", "other-probe")
        other = self.server.connection_manager.connection_for("other")
        other_stamp = self.stamp("other")
        close_lock_states = []
        original_close = self.backend.close

        def close(handle):
            close_lock_states.append(operation_manager.worker_lock(BOARD).locked())
            original_close(handle)

        with (
            patch.object(self.backend, "flash", side_effect=self.fail_flash) as program,
            patch.object(self.backend, "close", side_effect=close),
        ):
            result = self.flash()
        self.assert_failure_response(result)
        program.assert_called_once()
        self.assertEqual(close_lock_states, [True])
        self.assertIsNone(self.server.connection_manager.maybe_connection(BOARD))
        self.assertIsNone(self.server.gate_manager.snapshot(BOARD))
        self.assertIsNotNone(self.connection.runtime_session.closed_at)
        self.assertIs(self.server.connection_manager.connection_for("other"), other)
        self.assertEqual(self.server.gate_manager.snapshot("other"), other_stamp)
        self.assertIsNone(other.runtime_session.closed_at)
        self.assertEqual(operation_manager.snapshots(), ())
        operation = self.operations[0]
        self.assertIs(operation.response_error, self.failure)
        self.assertIs(operation.response_error.__cause__, self.cause)
        self.assertIsNone(operation.error)
        self.assertEqual(
            json.loads(self.call("read_memory_raw", board_id="other", address=0x20000000))[
                "status"
            ],
            "ok",
        )

    def test_input_support_and_verification_errors_keep_the_healthy_connection(self):
        stamp = self.server.gate_manager.snapshot(BOARD)
        for error, status, code in (
            (TargetInputError("bad argument"), "error", "input/invalid-argument"),
            (ValueError("bad base address"), "error", "backend/flash-failed"),
            (FlashTargetSupportError("no algorithm"), "refused", "flash/unsupported-target"),
            (UnsupportedArtifactError("bad image"), "refused", "flash/unsupported-artifact"),
            (FlashVerificationError("byte mismatch"), "error", "flash/verification-failed"),
        ):
            with self.subTest(error=type(error).__name__):
                with patch.object(self.backend, "flash", side_effect=error) as program:
                    result = self.flash()
                self.assertEqual((result["status"], result["code"]), (status, code))
                assert_lite_raw_warning(self, result, "flash_raw")
                program.assert_called_once()
                self.assertIs(self.server.connection_manager.connection_for(BOARD), self.connection)
                self.assertIsNone(self.connection.runtime_session.closed_at)
                self.assertEqual(self.server.gate_manager.snapshot(BOARD), stamp)
                self.assertEqual(
                    json.loads(self.call("read_memory_raw", address=0x20000000))["status"], "ok"
                )
        self.assertFalse(any(call[0] == "close" for call in self.backend.calls))

    def test_batch_stops_at_the_original_raw_failure_and_cleans_the_child(self):
        with patch.object(self.backend, "flash", side_effect=self.fail_flash) as program:
            result = json.loads(
                self.call(
                    "action_batch",
                    actions=[
                        {
                            "tool_name": "write_memory_raw",
                            "arguments": {"board_id": BOARD, "address": 0x20000000, "value": 1},
                        },
                        {
                            "tool_name": "flash_raw",
                            "arguments": {
                                "board_id": BOARD,
                                "artifact": str(self.image),
                                "base_address": 0x08000000,
                            },
                        },
                        {
                            "tool_name": "read_memory_raw",
                            "arguments": {"board_id": BOARD, "address": 0x20000000},
                        },
                    ],
                ).split("\n", 1)[0]
            )
        program.assert_called_once()
        self.assertEqual(result["status"], "batch_failed")
        self.assertEqual(result["failure"]["index"], 1)
        self.assertEqual([row["tool_name"] for row in result["completed"]], ["write_memory_raw"])
        failed = result["failure"]["result"]
        self.assert_failure_response(json.loads(failed) if isinstance(failed, str) else failed)
        self.assertFalse(any(call[0] == "read_memory" for call in self.backend.calls))
        self.assertIsNone(self.server.connection_manager.maybe_connection(BOARD))
        self.assertIsNotNone(self.connection.runtime_session.closed_at)
        self.assertEqual(operation_manager.snapshots(), ())

    def test_unconfirmed_cleanup_keeps_diagnostics_and_original_raw_error(self):
        with (
            patch.object(self.backend, "flash", side_effect=self.fail_flash),
            patch.object(
                self.backend,
                "close",
                side_effect=TargetConnectionError(
                    "worker cleanup unconfirmed; recovery marker retained"
                ),
            ),
        ):
            result = self.flash()
        self.assert_failure_response(result)
        self.assertIsNone(self.server.connection_manager.maybe_connection(BOARD))
        self.assertIsNone(self.server.gate_manager.snapshot(BOARD))
        self.assertIsNotNone(self.connection.runtime_session.closed_at)
        self.assertTrue(
            any(
                "recovery marker retained" in item
                for item in self.operations[0].resources.cleanup_errors
            )
        )
        self.assertIs(self.operations[0].response_error, self.failure)

    def test_captured_cleanup_cannot_clear_a_successor_assignment_or_readiness(self):
        replacements = []

        def replace_then_fail(*args, **kwargs):
            # Deterministically model reassignment after the resource binder captured
            # the old object. Cleanup must compare that exact object before clearing.
            manager = self.server.connection_manager
            manager.clear(BOARD)
            replacement = manager.assign(
                BOARD, self.connection.handle, self.connection.runtime_session
            )
            replacements.append((replacement, self.stamp(BOARD)))
            self.fail_flash(*args, **kwargs)

        with (
            patch.object(self.backend, "flash", side_effect=replace_then_fail),
            patch.object(self.backend, "close", wraps=self.backend.close) as close,
        ):
            result = self.flash()
        self.assert_failure_response(result)
        close.assert_not_called()
        replacement, stamp = replacements[0]
        self.assertIs(self.server.connection_manager.connection_for(BOARD), replacement)
        self.assertEqual(self.server.gate_manager.snapshot(BOARD), stamp)
        self.assertIsNone(replacement.runtime_session.closed_at)


if __name__ == "__main__":
    unittest.main()
