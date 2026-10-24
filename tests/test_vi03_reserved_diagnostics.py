"""S03PB-01: failed reservation closure must reach the real MCP error content."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import traceback
import unittest
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import patch

from pyocd_debug_mcp.capabilities.manual_permissions import (
    ManualPermissionError,
    ManualPermissionRepository,
)
from pyocd_debug_mcp.guardrails.plan_engine import PlanRefusal
from tests.test_tiered_acceptance_recovery import BOARD, _configure_public_native_lite
from tests.test_tiered_acceptance_workspace import WORKSPACE
from tests.test_vi03_fresh_recovery_grant import (
    UNLOCK,
    _disclose,
    _grant_and_reserve,
    _invoke_helper,
    _record_bytes,
    _recover_calls,
    _release,
)
from tests.test_vi03_unlock_diagnostics import _mcp_response, _ObservedBackend
from tests.tiered_acceptance_support import isolated_project, tiered_test_server

OBSERVATIONS: dict[str, Any] = {}
PRIMARY = "consumption storage replacement denied before publication"
PRIVATE = "private abandonment replacement detail"


@contextmanager
def _reserved_case():
    with isolated_project() as root, tiered_test_server(root) as server:
        backend = _ObservedBackend(recovery_supported=True)
        manual = _configure_public_native_lite(server, root, backend)
        backend.manual, backend.server, backend.boundary = manual, server, []
        try:
            old = _grant_and_reserve(unittest.TestCase(), server, manual, workspace_cli=True)
            backend.spent = old
            connection = server.connection_manager.connection_for(BOARD)
            server.gate_manager.stamp_validation(
                board_id=BOARD,
                connection_id=connection.connection_id,
                probe_identity=connection.handle.probe_uid,
                observed_mcu="nrf52840",
                validation_run="reserved-diagnostic-readiness-control",
                map_digest=server._active_snapshot_aggregate(BOARD),
            )
            with patch.object(backend, "recover", wraps=backend.recover) as entry:
                backend.recovery_entry = entry
                yield root, server, backend, manual, old
        finally:
            _release(server)


class _ReplacementFault:
    """Deny real record replacements after temp JSON, flush and fsync have run."""

    def __init__(self, manual, *, primary=None, abandon="deny"):
        self.target = manual._action_path("mass-erase")
        self.primary = primary if primary is not None else PermissionError(PRIMARY)
        self.abandon = abandon
        self.attempts = []
        self.real_replace = os.replace

    def replace(self, source, destination):
        if Path(destination) != self.target:
            return self.real_replace(source, destination)
        document = json.loads(Path(source).read_text(encoding="utf-8"))
        self.attempts.append(
            {
                "state": document["state"],
                "source": str(source),
                "destination": str(destination),
                "temporary_document": document,
                "old_record": json.loads(self.target.read_bytes()),
            }
        )
        if document["state"] == "consumed":
            raise self.primary
        if self.abandon == "deny":
            raise PermissionError(PRIVATE)
        result = self.real_replace(source, destination)
        if self.abandon == "postcommit":
            raise PermissionError(PRIVATE)
        return result

    @contextmanager
    def active(self):
        with patch.object(os, "replace", side_effect=self.replace):
            yield self


def _helper(root, old):
    command = [
        sys.executable,
        "-B",
        str(WORKSPACE / "bin/manual-permission"),
        "--project-root",
        str(root),
        "--action",
        "mass-erase",
        "--board-id",
        BOARD,
        "--policy-digest",
        str(old["policy_digest"]),
        "--binding-digest",
        str(old["binding_digest"]),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    observation = {
        "argv": command,
        "exit": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "payload": json.loads(result.stdout),
    }
    OBSERVATIONS.setdefault("helpers", []).append(observation)
    return observation


class ReservedDiagnosticTests(unittest.TestCase):
    def assert_remedy(self, text, primary, *, envelope=False):
        prefix = "Error executing tool target_unlock: " if envelope else ""
        self.assertEqual(text.splitlines()[0], prefix + str(primary))
        for fragment in (
            "Closure of the manual mass-erase grant could not be confirmed",
            "old grant remains unusable",
            "server restart may be required",
            "new disclosure",
            "distinct human $mass-erase invocation",
        ):
            self.assertIn(fragment, text)
        self.assertNotIn("spent", text.lower())
        self.assertNotIn("consumed", text.lower())
        self.assertNotIn(PRIVATE, text)

    def assert_no_attempt(self, server, backend):
        backend.recovery_entry.assert_not_called()
        self.assertEqual(_recover_calls(backend), 0)
        self.assertEqual(backend.boundary, [])
        self.assertIsNotNone(server.connection_manager.maybe_connection(BOARD))
        self.assertIsNotNone(server.gate_manager.snapshot(BOARD))

    def assert_public_dead(self, server, backend, manual):
        before = _record_bytes(manual)
        with self.assertRaises(PlanRefusal) as caught:
            server._unlock_coordinator.execute(**UNLOCK)
        self.assertEqual(caught.exception.code, "unlock/approval-inactive")
        replay, _ = _mcp_response(server)
        self.assertTrue(replay.root.isError)
        self.assertEqual(_record_bytes(manual), before)
        self.assert_no_attempt(server, backend)
        return replay.model_dump(mode="json")

    def assert_reserved_after_fault(self, root, server, backend, manual, old):
        # Re-read through a separate repository, without startup reset or consumption.
        observer = ManualPermissionRepository(root, manual.server_run_id)
        record = observer.status("mass-erase")
        self.assertEqual(record.state, "reserved")
        self.assertEqual(record.grant_id, old.grant_id)
        self.assertEqual(record.claim_id, old.claim_id)
        self.assertFalse((manual.root / "mass-erase.claim").exists())
        before = _record_bytes(manual)
        with self.assertRaises(ManualPermissionError):
            manual.reserve_mass_erase(BOARD, old.grant_id, old.policy_digest, old.binding_digest)
        helper = _helper(root, old._asdict())
        self.assertEqual(helper["exit"], 1)
        self.assertEqual(helper["payload"]["code"], "manual/already-reserved")
        replay = self.assert_public_dead(server, backend, manual)
        fresh = _disclose(self, server)
        self.assertEqual(fresh["state"], "reserved")
        self.assertEqual(fresh["grant_id"], old.grant_id)
        second_helper = _helper(root, fresh)
        self.assertEqual(second_helper["exit"], 1)
        self.assertEqual(second_helper["payload"]["code"], "manual/already-reserved")
        self.assertEqual(_record_bytes(manual), before)
        self.assert_public_dead(server, backend, manual)
        return {
            "record_after_fault_removal": asdict(record),
            "public_replay": replay,
            "helper": helper,
            "fresh_disclosure": fresh,
            "second_helper": second_helper,
            "backend_entries": 0,
        }

    def test_actual_mcp_persistent_replace_denial_exposes_reserved_closure_remedy(self):
        with _reserved_case() as (root, server, backend, manual, old):
            before = _record_bytes(manual)
            downgrade = _invoke_helper(root, action="downgrade", policy_digest=old.policy_digest)
            token = manual.issue_downgrade_token(
                BOARD, str(downgrade["grant_id"]), old.policy_digest
            )
            downgrade_before = _record_bytes(manual, "downgrade")
            fault = _ReplacementFault(manual)
            real_retire = manual.retire_mass_erase
            retirements = []

            def retire(*args):
                prior = _record_bytes(manual)
                result = real_retire(*args)
                retirements.append(
                    {
                        "returned_normally": True,
                        "return": result,
                        "unchanged": prior == _record_bytes(manual),
                    }
                )
                return result

            with fault.active(), patch.object(manual, "retire_mass_erase", side_effect=retire):
                response, text = _mcp_response(server)
            facts = self.assert_reserved_after_fault(root, server, backend, manual, old)
            OBSERVATIONS["persistent_denial"] = {
                "response": response.model_dump(mode="json"),
                "replacements": fault.attempts,
                "retirements": retirements,
                "primary_notes": fault.primary.__notes__,
                **facts,
            }
            self.assertTrue(response.root.isError)
            self.assert_remedy(text, fault.primary, envelope=True)
            self.assertEqual([item["state"] for item in fault.attempts], ["consumed", "locked"])
            self.assertEqual(
                retirements, [{"returned_normally": True, "return": None, "unchanged": True}]
            )
            self.assertEqual(_record_bytes(manual), before)
            self.assertEqual(_record_bytes(manual, "downgrade"), downgrade_before)
            manual.consume_downgrade(BOARD, token, old.policy_digest)
            with self.assertRaises(ManualPermissionError):
                manual.consume_downgrade(BOARD, token, old.policy_digest)
            self.assert_no_attempt(server, backend)

    def test_direct_reserved_failure_preserves_original_cause_and_traceback(self):
        with _reserved_case() as (root, server, backend, manual, old):
            fault = _ReplacementFault(manual)
            with fault.active(), self.assertRaises(RuntimeError) as caught:
                server._unlock_coordinator.execute(**UNLOCK)
            self.assertIs(caught.exception.__cause__, fault.primary)
            self.assert_remedy(str(caught.exception), fault.primary)
            frames = traceback.extract_tb(fault.primary.__traceback__)
            self.assertTrue(any(frame.name == "_atomic_write" for frame in frames))
            self.assertTrue(any(PRIVATE in note for note in fault.primary.__notes__))
            OBSERVATIONS["causal_failure"] = {
                "message": str(caught.exception),
                "same_primary_cause": True,
                "primary_traceback": traceback.format_exception(fault.primary),
                **self.assert_reserved_after_fault(root, server, backend, manual, old),
            }

    def test_prepublication_cancellation_identity_survives_failed_abandonment(self):
        primary = asyncio.CancelledError("prepublication cancellation")
        with _reserved_case() as (root, server, backend, manual, old):
            fault = _ReplacementFault(manual, primary=primary)
            with fault.active(), self.assertRaises(asyncio.CancelledError) as caught:
                server._unlock_coordinator.execute(**UNLOCK)
            self.assertIs(caught.exception, primary)
            self.assertIsNone(primary.__cause__)
            self.assertTrue(any(PRIVATE in note for note in primary.__notes__))
            OBSERVATIONS["reserved_cancellation"] = {
                "same_baseexception": True,
                **self.assert_reserved_after_fault(root, server, backend, manual, old),
            }

    def test_successful_abandonment_has_no_spurious_public_warning(self):
        with _reserved_case() as (root, server, backend, manual, old):
            fault = _ReplacementFault(manual, abandon="normal")
            with fault.active():
                response, text = _mcp_response(server)
            self.assertTrue(response.root.isError)
            self.assertEqual(text.splitlines()[0], "Error executing tool target_unlock: " + PRIMARY)
            self.assertNotIn("Closure", text)
            self.assertNotIn("retirement", text.lower())
            self.assertNotIn("restart", text.lower())
            self.assertEqual(manual.status("mass-erase").state, "locked")
            self.assert_public_dead(server, backend, manual)
            fresh = _disclose(self, server)
            helper = _helper(root, fresh)
            self.assertEqual(helper["exit"], 0)
            self.assertNotEqual(helper["payload"]["grant_id"], old.grant_id)
            self.assertEqual(manual.server_run_id, helper["payload"]["server_run_id"])
            OBSERVATIONS["clean_abandonment"] = {
                "response": response.model_dump(mode="json"),
                "fresh_helper": helper,
                "backend_entries": 0,
            }
            self.assert_public_dead(server, backend, manual)

    def test_abandonment_postcommit_error_reports_only_conditional_uncertainty(self):
        with _reserved_case() as (root, server, backend, manual, old):
            fault = _ReplacementFault(manual, abandon="postcommit")
            with fault.active():
                response, text = _mcp_response(server)
            self.assertTrue(response.root.isError)
            self.assert_remedy(text, fault.primary, envelope=True)
            self.assertEqual(manual.status("mass-erase").state, "locked")
            self.assert_public_dead(server, backend, manual)
            fresh = _disclose(self, server)
            helper = _helper(root, fresh)
            self.assertEqual(helper["exit"], 0)
            self.assertNotEqual(helper["payload"]["grant_id"], old.grant_id)
            self.assertEqual(helper["payload"]["server_run_id"], manual.server_run_id)
            OBSERVATIONS["abandonment_postcommit_uncertainty"] = {
                "response": response.model_dump(mode="json"),
                "state_before_new_helper": "locked",
                "fresh_helper": helper,
                "backend_entries": 0,
            }
            self.assert_public_dead(server, backend, manual)

    def test_reserved_grant_is_not_called_spent_when_retirement_also_raises(self):
        with _reserved_case() as (root, server, backend, manual, old):
            fault = _ReplacementFault(manual)
            real_retire = manual.retire_mass_erase

            def retire(*args):
                real_retire(*args)
                raise OSError("private retirement reporting failure")

            with fault.active(), patch.object(manual, "retire_mass_erase", side_effect=retire):
                response, text = _mcp_response(server)
            self.assertTrue(response.root.isError)
            self.assert_remedy(text, fault.primary, envelope=True)
            self.assertNotIn("private retirement reporting failure", text)
            OBSERVATIONS["reserved_retirement_exception"] = {
                "response": response.model_dump(mode="json"),
                **self.assert_reserved_after_fault(root, server, backend, manual, old),
            }


if __name__ == "__main__":
    unittest.main()
