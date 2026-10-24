"""S03-01: the actual MCP error envelope must relay terminal retirement uncertainty."""

from __future__ import annotations

import asyncio
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

from mcp import types as mcp_types

from pyocd_debug_mcp.capabilities.manual_permissions import ManualPermissionError
from tests.test_tiered_acceptance_recovery import BOARD, _configure_public_native_lite
from tests.test_vi03_fresh_recovery_grant import (
    UNLOCK,
    _assert_closed,
    _grant_and_reserve,
    _invoke_helper,
    _record_bytes,
    _recover_calls,
    _release,
)
from tests.tiered_acceptance_support import (
    DeterministicTargetBackend,
    isolated_project,
    tiered_test_server,
)

OBSERVATIONS: dict[str, Any] = {}
BACKEND_ERROR = "diagnostic backend recovery failed"
PERSISTENCE_ERROR = "private retirement persistence detail that must not reach the client"


class _ObservedBackend(DeterministicTargetBackend):
    """Record the actual durable authority and readiness at simulated backend entry."""

    def recover(self, handle: Any) -> None:
        record = self.manual.status("mass-erase")
        assert record.state == "consumed"
        assert record.claim_id == self.spent.claim_id
        assert record.grant_id == self.spent.grant_id
        assert self.server.gate_manager.snapshot(BOARD) is None
        self.boundary.append({"state": record.state, "claim": record.claim_id})
        super().recover(handle)


@contextmanager
def _configured(error: BaseException):
    with isolated_project() as root, tiered_test_server(root) as server:
        backend = _ObservedBackend(recovery_supported=True, recover_error=error)
        manual = _configure_public_native_lite(server, root, backend)
        backend.manual, backend.server, backend.boundary = manual, server, []
        try:
            spent = _grant_and_reserve(unittest.TestCase(), server, manual)
            backend.spent = spent
            connection = server.connection_manager.connection_for(BOARD)
            server.gate_manager.stamp_validation(
                board_id=BOARD,
                connection_id=connection.connection_id,
                probe_identity=connection.handle.probe_uid,
                observed_mcu="nrf52840",
                validation_run="diagnostic-readiness-control",
                map_digest=server._active_snapshot_aggregate(BOARD),
            )
            assert server.gate_manager.snapshot(BOARD) is not None
            yield root, server, backend, manual, spent
        finally:
            _release(server)


def _mcp_response(server: Any):
    request = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name="target_unlock", arguments=dict(UNLOCK)),
    )
    handler = server.mcp._mcp_server.request_handlers[mcp_types.CallToolRequest]
    response = asyncio.run(handler(request))
    text = "\n".join(getattr(block, "text", "") for block in response.root.content)
    return response, text


@contextmanager
def _retirement_write_fault(manual: Any, *, after_commit: bool = False):
    """Reach the real retirement protocol, faulting only its locked-record write."""

    original = manual._atomic_write
    attempts = []

    def write(path: Path, document: dict[str, Any]) -> None:
        if path == manual._action_path("mass-erase") and document["state"] == "locked":
            attempts.append(document["state"])
            if after_commit:
                original(path, document)
            raise OSError(PERSISTENCE_ERROR)
        original(path, document)

    with patch.object(manual, "_atomic_write", side_effect=write):
        yield attempts


class UnlockDiagnosticTests(unittest.TestCase):
    def assert_diagnostic(self, text: str, *, envelope: bool = False) -> None:
        prefix = "Error executing tool target_unlock: " if envelope else ""
        self.assertTrue(text.startswith(prefix + BACKEND_ERROR + "\n"), text)
        self.assertIn(
            "Retirement of the spent manual mass-erase grant could not be confirmed", text
        )
        self.assertIn("old grant remains unusable", text)
        self.assertIn("server restart may be required", text)
        self.assertIn("new disclosure", text)
        self.assertIn("distinct human $mass-erase invocation", text)
        self.assertNotIn(PERSISTENCE_ERROR, text)

    def assert_fail_closed(self, root: Path, server: Any, backend: Any, manual: Any, spent: Any):
        self.assertEqual(_recover_calls(backend), 1)
        self.assertEqual(len(backend.boundary), 1)
        record = manual.status("mass-erase")
        self.assertEqual(record.state, "consumed")
        self.assertEqual(record.grant_id, spent.grant_id)
        self.assertEqual(record.claim_id, spent.claim_id)
        before = _record_bytes(manual)
        with self.assertRaises(ManualPermissionError):
            manual.consume_mass_erase(
                BOARD, spent.policy_digest, spent.binding_digest, spent.claim_id
            )
        with self.assertRaises(ManualPermissionError):
            manual.reserve_mass_erase(
                BOARD, spent.grant_id, spent.policy_digest, spent.binding_digest
            )
        refused = _invoke_helper(root)
        self.assertEqual(refused["code"], "manual/already-consumed")
        self.assertEqual(_record_bytes(manual), before)
        self.assertIsNone(server.connection_manager.maybe_connection(BOARD))
        self.assertIsNone(server.gate_manager.snapshot(BOARD))
        with self.assertRaises(Exception):
            server._unlock_coordinator.execute(**UNLOCK)
        self.assertEqual(_record_bytes(manual), before)
        self.assertEqual(_recover_calls(backend), 1)
        return refused

    def test_actual_mcp_envelope_exposes_double_failure_remedy_and_keeps_grant_consumed(self):
        with _configured(RuntimeError(BACKEND_ERROR)) as (root, server, backend, manual, spent):
            # A reserved downgrade token is unrelated authority and must survive byte-exact.
            downgrade = _invoke_helper(root, action="downgrade", policy_digest=spent.policy_digest)
            self.assertEqual(downgrade["status"], "manual_grant_unlocked")
            manual.issue_downgrade_token(BOARD, str(downgrade["grant_id"]), spent.policy_digest)
            downgrade_before = _record_bytes(manual, "downgrade")
            with _retirement_write_fault(manual) as writes:
                response, text = _mcp_response(server)
            self.assertTrue(response.root.isError)
            self.assert_diagnostic(text, envelope=True)
            self.assertEqual(writes, ["locked"])
            refused = self.assert_fail_closed(root, server, backend, manual, spent)
            replay, _ = _mcp_response(server)
            self.assertTrue(replay.root.isError)
            self.assertEqual(_recover_calls(backend), 1)
            self.assertEqual(_record_bytes(manual, "downgrade"), downgrade_before)
            OBSERVATIONS["mcp_double_failure"] = {
                "response": response.model_dump(mode="json"),
                "replay_response": replay.model_dump(mode="json"),
                "backend_boundary": backend.boundary,
                "authorized_failed_backend_calls": 1,
                "unauthorized_backend_calls": 0,
                "retirement_write_attempts": len(writes),
                "state": manual.status("mass-erase").state,
                "helper": refused,
                "downgrade_record_unchanged": True,
                "limit": "Actual in-process MCP request-handler envelope; simulated backend.",
            }

    def test_direct_coordinator_keeps_original_backend_as_explicit_cause(self):
        primary = ValueError(BACKEND_ERROR)
        with _configured(primary) as (root, server, backend, manual, spent):
            with _retirement_write_fault(manual) as writes:
                with self.assertRaises(RuntimeError) as raised:
                    server._unlock_coordinator.execute(**UNLOCK)
            self.assert_diagnostic(str(raised.exception))
            self.assertIs(raised.exception.__cause__, primary)
            self.assertIsNotNone(primary.__traceback__)
            self.assertEqual(writes, ["locked"])
            self.assert_fail_closed(root, server, backend, manual, spent)
            OBSERVATIONS["direct_double_failure"] = {
                "message": str(raised.exception),
                "explicit_cause_is_original_backend_error": raised.exception.__cause__ is primary,
                "backend_cause_type": type(primary).__name__,
                "authorized_failed_backend_calls": _recover_calls(backend),
                "unauthorized_backend_calls": 0,
            }

    def test_actual_mcp_normal_backend_failure_has_no_retirement_warning(self):
        with _configured(RuntimeError(BACKEND_ERROR)) as (_, server, backend, manual, spent):
            response, text = _mcp_response(server)
            self.assertTrue(response.root.isError)
            self.assertTrue(text.startswith("Error executing tool target_unlock: " + BACKEND_ERROR))
            self.assertNotIn("Retirement", text)
            self.assertNotIn("restart", text)
            self.assertEqual(_recover_calls(backend), 1)
            _assert_closed(self, manual, spent)
            self.assertIsNone(server.connection_manager.maybe_connection(BOARD))
            self.assertIsNone(server.gate_manager.snapshot(BOARD))
            OBSERVATIONS["mcp_normal_backend_failure"] = {
                "response": response.model_dump(mode="json"),
                "state": manual.status("mass-erase").state,
                "authorized_failed_backend_calls": _recover_calls(backend),
            }

    def test_direct_coordinator_normal_retirement_reraises_same_backend_error(self):
        primary = ValueError(BACKEND_ERROR)
        with _configured(primary) as (_, server, backend, manual, spent):
            with self.assertRaises(ValueError) as raised:
                server._unlock_coordinator.execute(**UNLOCK)
            self.assertIs(raised.exception, primary)
            self.assertEqual(str(primary), BACKEND_ERROR)
            self.assertIsNone(primary.__cause__)
            self.assertEqual(_recover_calls(backend), 1)
            _assert_closed(self, manual, spent)

    def test_cancellation_is_not_converted_to_an_ordinary_error_on_retirement_failure(self):
        primary = asyncio.CancelledError("simulated backend cancellation")
        with _configured(primary) as (root, server, backend, manual, spent):
            with _retirement_write_fault(manual) as writes:
                with self.assertRaises(asyncio.CancelledError) as raised:
                    server._unlock_coordinator.execute(**UNLOCK)
            self.assertIs(raised.exception, primary)
            self.assertEqual(writes, ["locked"])
            self.assert_fail_closed(root, server, backend, manual, spent)

    def test_actual_mcp_postcommit_retirement_fault_does_not_claim_still_consumed(self):
        with _configured(RuntimeError(BACKEND_ERROR)) as (root, server, backend, manual, spent):
            with _retirement_write_fault(manual, after_commit=True) as writes:
                response, text = _mcp_response(server)
            self.assertTrue(response.root.isError)
            self.assert_diagnostic(text, envelope=True)
            self.assertNotIn("stays consumed", text)
            self.assertEqual(writes, ["locked"])
            self.assertEqual(_recover_calls(backend), 1)
            _assert_closed(self, manual, spent)
            fresh = _invoke_helper(
                root, policy_digest=spent.policy_digest, binding_digest=spent.binding_digest
            )
            self.assertEqual(fresh["status"], "manual_grant_unlocked")
            self.assertNotEqual(fresh["grant_id"], spent.grant_id)
            OBSERVATIONS["mcp_postcommit_uncertainty"] = {
                "response": response.model_dump(mode="json"),
                "fresh_helper": fresh,
                "authorized_failed_backend_calls": _recover_calls(backend),
                "unauthorized_backend_calls": 0,
            }


if __name__ == "__main__":
    unittest.main()
