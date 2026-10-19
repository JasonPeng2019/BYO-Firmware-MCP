"""Fresh independent public/worker regressions for the real M14-S-01 failure."""

from __future__ import annotations

import json
import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import psutil
from pyocd_debug_mcp.adapters.swd_process import ProcessIsolatedSWDInterface
from pyocd_debug_mcp.adapters.swd_pyocd import PyOCDSWDInterface
from pyocd_debug_mcp.target_errors import TargetConnectionError, TargetControlError

from tests.m14_independent_support import (
    advertised_validation,
    approve,
    attempt,
    connect,
    disclose,
    ordinary_full,
    safe_read,
    safe_value,
)
from tests.m14_return_boundary import ReturnBoundary
from tests.m05_public_support import PublicSetup
from tests.test_tiered_acceptance_lite import _setup_inventory
from tests.tiered_acceptance_support import fresh_server, public_text


class InstalledReturnIndependentTests(unittest.TestCase):
    def test_installed_timeout_false_and_supported_true_contract(self):
        for outcome, expected in (("timeout", False), ("ready", True)):
            with self.subTest(outcome=outcome):
                boundary = ReturnBoundary(outcome)
                with boundary.controlled_clock():
                    self.assertIs(boundary.target.mass_erase(), expected)
                self.assertEqual(boundary.attempts, 1)
                self.assertEqual(sum(row[0] == "poll" for row in boundary.events), 1)
                resets = [row for row in boundary.events if row[:2] == ["write", 0]]
                self.assertEqual(
                    resets, [] if expected is False else [["write", 0, 1], ["write", 0, 0]]
                )

    def test_public_false_cannot_claim_completed_erase(self):
        self.exercise_public("timeout")

    def test_public_installed_true_remains_successful(self):
        self.exercise_public("ready")

    def test_public_installed_soc_void_chip_erase_remains_successful(self):
        self.exercise_public("void")

    def test_public_backend_exception_remains_a_failure(self):
        self.exercise_public("exception")

    def test_public_unaffected_full_read_control(self):
        with ordinary_full(self) as f:
            self.assertEqual(safe_value(f), 0x6D143A72)
            self.assertEqual(f.backend.at_entry, [])
            self.assertFalse(any(row[0] == "erase" for row in f.backend.calls))

    def exercise_public(self, outcome):
        with ordinary_full(self) as f:
            adapter = PyOCDSWDInterface()
            entries = []
            backend_errors = []
            grant_path = f.root / ".agent-workspace/runtime/manual-permissions/mass-erase.json"
            proposed = None
            current_grant = None

            def at_primitive():
                state_bytes = grant_path.read_bytes()
                state = json.loads(state_bytes)
                self.assertEqual(state["state"], "consumed")
                self.assertEqual(state["grant_id"], current_grant)
                self.assertEqual(state["server_run_id"], f.server.server_run.run_id)
                self.assertEqual(state["board_id"], f.board)
                self.assertEqual(state["policy_digest"], proposed["manual_grant"]["policy_digest"])
                self.assertEqual(
                    state["binding_digest"], proposed["manual_grant"]["binding_digest"]
                )
                self.assertTrue(state["claim_id"])
                self.assertFalse(grant_path.with_suffix(".claim").exists())
                self.assertIsNone(f.server.gate_manager.snapshot(f.board))
                entries.append(state_bytes)

            boundary = ReturnBoundary(outcome, at_primitive)

            def session(handle):
                return replace(handle, session=SimpleNamespace(target=boundary.target))

            def recover(handle):
                with boundary.controlled_clock():
                    try:
                        adapter.recover(session(handle))
                    except Exception as exc:
                        backend_errors.append(exc)
                        raise

            # Reuse only the original independent T onboarding/read boundary.
            # EraseProbe.recover is replaced; no repair-I fixture/assertion runs.
            f.backend.recover = recover
            f.backend.supports_recovery = lambda h, m: adapter.supports_recovery(session(h), m)

            def authorize(serial):
                nonlocal proposed, current_grant
                proposed = disclose(f)
                self.assertEqual(boundary.attempts, serial - 1)
                current_grant = f"independent-return-grant-{serial}"
                f.server._manual_permissions.write_unlocked_grant(
                    action="mass-erase",
                    grant_id=current_grant,
                    board_id=f.board,
                    policy_digest=proposed["manual_grant"]["policy_digest"],
                    binding_digest=proposed["manual_grant"]["binding_digest"],
                )
                self.assertEqual(approve(f)["status"], "unlock_plan_approved")
                self.assertEqual(json.loads(grant_path.read_bytes())["state"], "reserved")
                self.assertEqual(boundary.attempts, serial - 1)

            def statuses():
                return [
                    json.loads(p.read_text(encoding="utf-8"))["terminal_status"]
                    for p in sorted(f.root.rglob("target-unlock-*/report.json"))
                    if json.loads(p.read_text(encoding="utf-8"))["terminal_status"].startswith(
                        ("unlock_completed", "unlock_failed")
                    )
                ]

            authorize(1)
            self.assertIsNotNone(f.server.gate_manager.snapshot(f.board))
            reply, failure = None, None
            try:
                reply = attempt(f)
            except Exception as exc:
                failure = exc

            try:
                if outcome in {"timeout", "exception"}:
                    self.assertIsNotNone(
                        failure,
                        f"Backend {outcome} must not claim a completed/performed erase: {reply}",
                    )
                    self.assertEqual(len(backend_errors), 1)
                    self.assertIsInstance(backend_errors[0], TargetControlError)
                    chain = []
                    cause = failure
                    while cause is not None:
                        chain.append(cause)
                        cause = cause.__cause__
                    self.assertIn(backend_errors[0], chain)
                    self.assertNotIn("Target unlock completed", str(failure))
                    self.assertNotIn("performed mass erase", str(failure))
                    if outcome == "exception":
                        self.assertIsInstance(backend_errors[0], TargetConnectionError)
                        self.assertIn("independent CTRL-AP transport loss", str(failure))
                    self.assertEqual(statuses(), ["unlock_failed_revalidation_required"])
                else:
                    self.assertIsNone(failure)
                    self.assertIn("Target unlock completed", reply)
                    self.assertIn("performed mass erase", reply)
                    self.assertEqual(statuses(), ["unlock_completed_revalidation_required"])
                self.assertEqual(boundary.attempts, 1)
                self.assertEqual(len(entries), 1)
                self.assertEqual(grant_path.read_bytes(), entries[0])
                self.assertIsNone(f.server.gate_manager.snapshot(f.board))
                self.assertIsNone(f.server.connection_manager.maybe_connection(f.board))

                def protected_read_refused():
                    before = list(f.backend.calls)
                    try:
                        response = safe_read(f)
                    except Exception as exc:
                        self.assertRegex(str(exc).lower(), "validat|connect|ready|locked")
                    else:
                        self.assertEqual(response["status"], "batch_failed")
                    self.assertEqual(
                        [r for r in f.backend.calls if r[0] == "read"],
                        [r for r in before if r[0] == "read"],
                    )

                with self.assertRaises(Exception):
                    attempt(f)
                protected_read_refused()
                self.assertEqual(boundary.attempts, 1)
                connect(f)
                protected_read_refused()
                self.assertEqual(
                    advertised_validation(f.flow, f.board)["status"], "validation_passed"
                )
                self.assertEqual(safe_value(f), 0x6D143A72)
                disclose(f)
                with self.assertRaises(Exception):
                    approve(f)
                with self.assertRaises(Exception):
                    attempt(f)
                self.assertEqual(boundary.attempts, 1)
                self.assertEqual(grant_path.read_bytes(), entries[0])
                # Consumed authority cannot be refreshed in the same run. A
                # real server restart relocks it; fresh validation/disclosure
                # and a new exact durable grant then permit recovery again.
                f.flow.call("disconnect", {"board_id": f.board})
                old_run = f.server.server_run.run_id
                restarted = fresh_server(f.root)
                restarted.configure_tiered_test_seams(backend=f.backend)
                f.server = restarted

                def restarted_call(name, args):
                    row = {"tool": name, "arguments": args, "phase": "restarted"}
                    f.wire.append(row)
                    try:
                        row["reply"] = public_text(restarted, name, args)
                        return row["reply"]
                    except Exception as exc:
                        row["error"] = str(exc)
                        raise

                f.flow = PublicSetup(restarted, call=restarted_call)
                self.assertNotEqual(restarted.server_run.run_id, old_run)
                self.assertEqual(restarted._manual_permissions.status("mass-erase").state, "locked")
                if outcome != "void":
                    boundary.outcome = "ready"
                with patch.object(restarted, "_hardware_inventory", _setup_inventory()):
                    try:
                        connect(f)
                        protected_read_refused()
                        self.assertEqual(
                            advertised_validation(f.flow, f.board)["status"], "validation_passed"
                        )
                        self.assertEqual(safe_value(f), 0x6D143A72)
                        with self.assertRaises(Exception):
                            attempt(f)
                        authorize(2)
                        self.assertIn("Target unlock completed", attempt(f))
                        self.assertEqual(boundary.attempts, 2)
                        self.assertEqual(len(entries), 2)
                        self.assertEqual(grant_path.read_bytes(), entries[1])
                        self.assertIsNone(f.server.gate_manager.snapshot(f.board))
                        self.assertIsNone(f.server.connection_manager.maybe_connection(f.board))
                    finally:
                        f.flow.call("disconnect", {"board_id": f.board})
            finally:
                destination = Path(os.environ["BYO_MANAGED_TEST_EVIDENCE"]) / self.id()
                destination.mkdir(parents=True, exist_ok=True)
                (destination / "return-oracle.json").write_text(
                    json.dumps(
                        {
                            "outcome": outcome,
                            "reply": reply,
                            "failure": str(failure),
                            "events": boundary.events,
                            "consumed_at_entry": [json.loads(e) for e in entries],
                            "report_statuses": statuses(),
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )

    def test_actual_worker_transports_timeout_as_typed_failure(self):
        output = Path(os.environ["BYO_MANAGED_TEST_EVIDENCE"]) / self.id()
        output.mkdir(parents=True, exist_ok=True)
        proxy = ProcessIsolatedSWDInterface(
            worker_argv=[sys.executable, "-B", "-m", "tests.m14_return_worker"]
        )
        with patch.dict(os.environ, {"M14_RETURN_WORKER_OUTPUT": str(output)}):
            handle = proxy.open(board=None, unique_id="synthetic-m14", target="nrf52840")
            worker = handle.worker
            process = worker._process
            identity = psutil.Process(process.pid)
            created = identity.create_time()
            record = {
                "pid": process.pid,
                "create_time_epoch": created,
                "argv": identity.cmdline(),
                "marker": str(worker._marker),
            }
            try:
                with self.assertRaises(TargetControlError) as caught:
                    proxy.recover(handle)
                self.assertIn("Worker recover failed", str(caught.exception))
                self.assertNotIn("performed mass erase", str(caught.exception))
                self.assertFalse(worker._closed)
            finally:
                proxy.close(handle)
                record.update(
                    raw_exit=process.poll(),
                    cleanup_confirmed=worker._cleanup_confirmed,
                    marker_removed=worker._marker is None,
                )
                try:
                    record["original_absent"] = psutil.Process(process.pid).create_time() != created
                except psutil.NoSuchProcess:
                    record["original_absent"] = True
                (output / "cleanup.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
            # Production close receives its reply then terminates the owned job;
            # the exact exit is captured, not assumed to be a graceful exit 0.
            self.assertIsInstance(record["raw_exit"], int)
            self.assertTrue(record["cleanup_confirmed"])
            self.assertTrue(record["marker_removed"])
            self.assertTrue(record["original_absent"])
            events = json.loads((output / "boundary.json").read_text(encoding="utf-8"))["events"]
            self.assertEqual(sum(row == ["write", 4, 1] for row in events), 1)
            self.assertEqual(sum(row[0] == "poll" for row in events), 1)
            self.assertEqual([r for r in events if r[:2] == ["write", 0]], [])


if __name__ == "__main__":
    unittest.main()
