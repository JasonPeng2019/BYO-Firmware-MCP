"""V03D-01/S03D-01: expose retirement uncertainty after consumption errors before recovery."""

from __future__ import annotations

import asyncio
import json
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

from pyocd_debug_mcp.capabilities.manual_permissions import ManualPermissionError
from tests.test_tiered_acceptance_recovery import BOARD
from tests.test_vi03_fresh_recovery_grant import (
    UNLOCK,
    _assert_closed,
    _invoke_helper,
    _record_bytes,
    _recover_calls,
)
from tests.test_vi03_unlock_diagnostics import (
    PERSISTENCE_ERROR,
    _configured,
    _mcp_response,
    _retirement_write_fault,
)

OBSERVATIONS: dict[str, Any] = {}
CONSUME_ERROR = "consumption durably published but completion failed"
UNLINK_ERROR = "consumed claim unlink denied"


@contextmanager
def _prebackend_case():
    with _configured(RuntimeError("backend must never be entered")) as case:
        backend = case[2]
        # Count entry before the existing backend's durable-authority assertions.
        with patch.object(backend, "recover", wraps=backend.recover) as recovery_entry:
            backend.recovery_entry = recovery_entry
            yield case


@contextmanager
def _consumption_write_fault(manual: Any, primary: BaseException, *, after_commit: bool = True):
    """Fault actual consumption publication; leave the real cleanup protocol in place."""

    original = manual._atomic_write
    writes = []

    def write(path: Path, document: dict[str, Any]) -> None:
        if path == manual._action_path("mass-erase") and document["state"] == "consumed":
            writes.append("consumed")
            if after_commit:
                original(path, document)
            raise primary
        original(path, document)

    with patch.object(manual, "_atomic_write", side_effect=write):
        yield writes


@contextmanager
def _consumption_unlink_fault(manual: Any, primary: BaseException):
    original = manual._unlink_claim_if_matches
    faults = []

    def unlink(path: Path, expected: dict[str, Any]) -> None:
        if path.name == "mass-erase.claim" and manual.status("mass-erase").state == "consumed":
            faults.append("consumed claim unlink")
            raise primary
        original(path, expected)

    with patch.object(manual, "_unlink_claim_if_matches", side_effect=unlink):
        yield faults


class PrebackendDiagnosticTests(unittest.TestCase):
    def assert_remedy(self, text: str, primary: BaseException, *, envelope: bool = False) -> None:
        prefix = "Error executing tool target_unlock: " if envelope else ""
        self.assertTrue(text.startswith(prefix + str(primary) + "\n"), text)
        self.assertIn("Closure of the manual mass-erase grant could not be confirmed", text)
        self.assertIn("old grant remains unusable", text)
        self.assertIn("server restart may be required", text)
        self.assertIn("new disclosure", text)
        self.assertIn("distinct human $mass-erase invocation", text)
        self.assertNotIn(PERSISTENCE_ERROR, text)

    def assert_no_backend(self, server: Any, backend: Any) -> None:
        backend.recovery_entry.assert_not_called()
        self.assertEqual(_recover_calls(backend), 0)
        self.assertEqual(backend.boundary, [])
        # A refused pre-attempt operation has not finalized otherwise healthy hardware.
        self.assertIsNotNone(server.connection_manager.maybe_connection(BOARD))
        self.assertIsNotNone(server.gate_manager.snapshot(BOARD))

    def assert_fail_closed(self, root: Path, server: Any, backend: Any, manual: Any, spent: Any):
        self.assert_no_backend(server, backend)
        record = manual.status("mass-erase")
        self.assertEqual(record.state, "consumed")
        self.assertEqual(record.grant_id, spent.grant_id)
        self.assertEqual(record.claim_id, spent.claim_id)
        before = _record_bytes(manual)
        claim_path = manual.root / "mass-erase.claim"
        claim_before = claim_path.read_bytes() if claim_path.exists() else None
        with self.assertRaises(ManualPermissionError):
            manual.consume_mass_erase(
                BOARD, spent.policy_digest, spent.binding_digest, spent.claim_id
            )
        with self.assertRaises(ManualPermissionError):
            manual.reserve_mass_erase(
                BOARD, spent.grant_id, spent.policy_digest, spent.binding_digest
            )
        helper = _invoke_helper(root)
        self.assertEqual(helper["status"], "refused")
        self.assertEqual(
            helper["code"], "manual/already-reserved" if claim_before else "manual/already-consumed"
        )
        with self.assertRaises(Exception):
            server._unlock_coordinator.execute(**UNLOCK)
        replay, _ = _mcp_response(server)
        self.assertTrue(replay.root.isError)
        self.assertEqual(_record_bytes(manual), before)
        self.assertEqual(claim_path.read_bytes() if claim_path.exists() else None, claim_before)
        self.assert_no_backend(server, backend)
        return {
            "helper": helper,
            "replay_response": replay.model_dump(mode="json"),
            "state": record.state,
            "backend_calls": 0,
            "transient_claim": json.loads(claim_before) if claim_before else None,
        }

    def test_actual_mcp_postwrite_failure_and_failed_retirement_relay_remedy(self):
        primary = OSError(CONSUME_ERROR)
        with _prebackend_case() as (
            root,
            server,
            backend,
            manual,
            spent,
        ):
            downgrade = _invoke_helper(root, action="downgrade", policy_digest=spent.policy_digest)
            self.assertEqual(downgrade["status"], "manual_grant_unlocked")
            manual.issue_downgrade_token(BOARD, str(downgrade["grant_id"]), spent.policy_digest)
            downgrade_before = _record_bytes(manual, "downgrade")
            with _retirement_write_fault(manual) as retire_writes:
                with _consumption_write_fault(manual, primary) as consume_writes:
                    response, text = _mcp_response(server)
            self.assertTrue(response.root.isError)
            self.assert_remedy(text, primary, envelope=True)
            self.assertEqual(consume_writes, ["consumed"])
            self.assertEqual(retire_writes, ["locked"])
            facts = self.assert_fail_closed(root, server, backend, manual, spent)
            self.assertEqual(_record_bytes(manual, "downgrade"), downgrade_before)
            OBSERVATIONS["postwrite_double_failure"] = {
                "response": response.model_dump(mode="json"),
                **facts,
                "consumption_writes": consume_writes,
                "retirement_writes": retire_writes,
                "downgrade_record_unchanged": True,
            }

    def test_actual_mcp_claim_unlink_failure_relays_remedy_without_second_injected_fault(self):
        primary = PermissionError(UNLINK_ERROR)
        with _prebackend_case() as (
            root,
            server,
            backend,
            manual,
            spent,
        ):
            with _consumption_unlink_fault(manual, primary) as faults:
                response, text = _mcp_response(server)
            self.assertTrue(response.root.isError)
            self.assert_remedy(text, primary, envelope=True)
            self.assertEqual(faults, ["consumed claim unlink"])
            facts = self.assert_fail_closed(root, server, backend, manual, spent)
            self.assertIsNotNone(facts["transient_claim"])
            OBSERVATIONS["claim_unlink_failure"] = {
                "response": response.model_dump(mode="json"),
                **facts,
                "faults": faults,
            }

    def test_direct_postwrite_double_failure_preserves_original_cause(self):
        primary = ValueError(CONSUME_ERROR)
        with _prebackend_case() as (
            root,
            server,
            backend,
            manual,
            spent,
        ):
            with _retirement_write_fault(manual):
                with _consumption_write_fault(manual, primary):
                    with self.assertRaises(RuntimeError) as raised:
                        server._unlock_coordinator.execute(**UNLOCK)
            self.assert_remedy(str(raised.exception), primary)
            self.assertIs(raised.exception.__cause__, primary)
            self.assertIsNotNone(primary.__traceback__)
            self.assertTrue(any(PERSISTENCE_ERROR in note for note in primary.__notes__))
            self.assert_fail_closed(root, server, backend, manual, spent)
            OBSERVATIONS["direct_cause"] = {
                "message": str(raised.exception),
                "original_cause_identity": True,
                "original_traceback_present": True,
                "backend_calls": 0,
            }

    def test_direct_claim_unlink_failure_preserves_original_cause(self):
        primary = PermissionError(UNLINK_ERROR)
        with _prebackend_case() as (
            root,
            server,
            backend,
            manual,
            spent,
        ):
            with _consumption_unlink_fault(manual, primary):
                with self.assertRaises(RuntimeError) as raised:
                    server._unlock_coordinator.execute(**UNLOCK)
            self.assertIs(raised.exception.__cause__, primary)
            self.assert_remedy(str(raised.exception), primary)
            self.assert_fail_closed(root, server, backend, manual, spent)

    def test_actual_mcp_successful_prebackend_retirement_has_no_spurious_guidance(self):
        primary = OSError(CONSUME_ERROR)
        with _prebackend_case() as (
            _,
            server,
            backend,
            manual,
            spent,
        ):
            with _consumption_write_fault(manual, primary):
                response, text = _mcp_response(server)
            self.assertTrue(response.root.isError)
            self.assertTrue(text.startswith("Error executing tool target_unlock: " + str(primary)))
            self.assertNotIn("Retirement", text)
            self.assertNotIn("restart", text)
            self.assert_no_backend(server, backend)
            _assert_closed(self, manual, spent)
            OBSERVATIONS["normal_retirement"] = {
                "response": response.model_dump(mode="json"),
                "state": "locked",
                "backend_calls": 0,
            }

    def test_direct_successful_retirement_reraises_same_consumption_exception(self):
        primary = ValueError(CONSUME_ERROR)
        with _prebackend_case() as (
            _,
            server,
            backend,
            manual,
            spent,
        ):
            with _consumption_write_fault(manual, primary):
                with self.assertRaises(ValueError) as raised:
                    server._unlock_coordinator.execute(**UNLOCK)
            self.assertIs(raised.exception, primary)
            self.assertIsNone(primary.__cause__)
            self.assertEqual(str(primary), CONSUME_ERROR)
            self.assert_no_backend(server, backend)
            _assert_closed(self, manual, spent)

    def test_prepublication_failure_cleans_reservation_without_spurious_guidance(self):
        primary = OSError("consumption publication refused before write")
        with _prebackend_case() as (
            _,
            server,
            backend,
            manual,
            spent,
        ):
            with _consumption_write_fault(manual, primary, after_commit=False):
                response, text = _mcp_response(server)
            self.assertTrue(response.root.isError)
            self.assertTrue(text.startswith("Error executing tool target_unlock: " + str(primary)))
            self.assertNotIn("Retirement", text)
            self.assertNotIn("restart", text)
            self.assert_no_backend(server, backend)
            _assert_closed(self, manual, spent)

    def test_prebackend_cancellation_identity_survives_failed_retirement(self):
        primary = asyncio.CancelledError("prebackend cancellation after consumption publication")
        with _prebackend_case() as (
            root,
            server,
            backend,
            manual,
            spent,
        ):
            with _retirement_write_fault(manual):
                with _consumption_write_fault(manual, primary):
                    with self.assertRaises(asyncio.CancelledError) as raised:
                        server._unlock_coordinator.execute(**UNLOCK)
            self.assertIs(raised.exception, primary)
            self.assertIsNone(primary.__cause__)
            self.assert_fail_closed(root, server, backend, manual, spent)
            OBSERVATIONS["cancellation"] = {"same_baseexception": True, "backend_calls": 0}

    def test_retirement_postcommit_failure_reports_uncertainty_even_when_locked(self):
        primary = OSError(CONSUME_ERROR)
        with _prebackend_case() as (
            root,
            server,
            backend,
            manual,
            spent,
        ):
            with _retirement_write_fault(manual, after_commit=True):
                with _consumption_write_fault(manual, primary):
                    response, text = _mcp_response(server)
            self.assertTrue(response.root.isError)
            self.assert_remedy(text, primary, envelope=True)
            self.assertNotIn("stays consumed", text)
            self.assert_no_backend(server, backend)
            _assert_closed(self, manual, spent)
            fresh = _invoke_helper(
                root, policy_digest=spent.policy_digest, binding_digest=spent.binding_digest
            )
            self.assertEqual(fresh["status"], "manual_grant_unlocked")
            self.assertNotEqual(fresh["grant_id"], spent.grant_id)
            OBSERVATIONS["postcommit_uncertainty"] = {
                "response": response.model_dump(mode="json"),
                "fresh_helper": fresh,
                "backend_calls": 0,
            }


if __name__ == "__main__":
    unittest.main()
