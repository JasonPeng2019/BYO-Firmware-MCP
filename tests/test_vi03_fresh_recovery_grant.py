"""VI03/M14: a terminal attempt closes its mass-erase grant; a fresh human grant may follow.

Every public scenario drives the real ``target_unlock`` coordinator, the manual
permission repository, the shipped workspace helper, the real ReportWriter and the
recovery finalizer.  Only the recovery backend is simulated; nothing touches hardware.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import unittest
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NamedTuple
from unittest.mock import patch

import anyio

from pyocd_debug_mcp.capabilities.manual_permission_helper import unlock_manual_grant
from pyocd_debug_mcp.capabilities.manual_permissions import (
    ManualPermissionError,
    ManualPermissionRepository,
)

from tests.test_tiered_acceptance_recovery import (
    BOARD,
    DISCLOSURE,
    POLICY,
    _configure_public_native_lite,
    _public_json_reply,
    _unlock_fields,
    _unlock_null_fields,
)
from tests.tiered_acceptance_support import (
    DeterministicTargetBackend,
    isolated_project,
    public_text,
    tiered_test_server,
)
from tests.test_tiered_acceptance_workspace import WORKSPACE

# Raw facts preserved by the evidence runner beside the pass/fail outcomes.
OBSERVATIONS: dict[str, object] = {}

UNLOCK = {"board_id": BOARD, "recovery_mechanism": "backend_mass_erase"}
BACKEND_FAILURE = "simulated backend recovery failure"
FAILED = "unlock_failed_revalidation_required"
COMPLETED = "unlock_completed_revalidation_required"
UNCERTAIN = "unlock_completed_cleanup_uncertain_reconnect_required"


class Attempt(NamedTuple):
    grant_id: str
    claim_id: str
    policy_digest: str
    binding_digest: str


class _CloseFailingBackend(DeterministicTargetBackend):
    """Fail the recovery finalizer's worker close only while ``close_error`` is set."""

    close_error: Exception | None = None

    def close(self, handle: Any) -> None:
        super().close(handle)
        if self.close_error is not None:
            raise self.close_error


class _BlockingRecoveryBackend(DeterministicTargetBackend):
    """Hold the simulated recovery inside the backend until the test releases it."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.entered = threading.Event()
        self.release = threading.Event()

    def recover(self, handle: Any) -> None:
        self.entered.set()
        if not self.release.wait(10):
            raise TimeoutError("the test never released the simulated recovery")
        super().recover(handle)


def _recover_calls(backend: DeterministicTargetBackend) -> int:
    return sum(1 for call in backend.calls if call[0] == "recover")


def _attempt(server: Any) -> Any:
    return asyncio.run(server.mcp.call_tool("target_unlock", dict(UNLOCK)))


def _exception_text(error: BaseException) -> str:
    """Render the public error with its notes and causes, as a client log would."""

    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(f"{type(current).__name__}: {current}")
        parts.extend(getattr(current, "__notes__", ()))
        current = current.__cause__ or current.__context__
    return "\n".join(parts)


def _record_bytes(manual: ManualPermissionRepository, action: str = "mass-erase") -> bytes:
    return manual._action_path(action).read_bytes()  # noqa: SLF001 - durable protocol record


def _invoke_helper(
    project_root: Path, action: str = "mass-erase", **binding: object
) -> dict[str, object]:
    """Run the shipped workspace helper as one separate human skill invocation."""

    arguments: dict[str, object] = {"board_id": BOARD, "policy_digest": POLICY}
    if action == "mass-erase":
        arguments["binding_digest"] = DISCLOSURE
    arguments.update(binding)
    return unlock_manual_grant(project_root=project_root, action=action, **arguments)


def _disclose(testcase: unittest.TestCase, server: Any) -> dict[str, object]:
    public_text(server, "target_unlock-plan", _unlock_null_fields())
    disclosure = _public_json_reply(server, "target_unlock-plan", _unlock_fields(permission=None))
    testcase.assertEqual(disclosure["status"], "unlock_permission_requested", disclosure)
    grant = disclosure["manual_grant"]
    testcase.assertIsInstance(grant, dict, disclosure)
    assert isinstance(grant, dict)
    return grant


def _human_grant(grant: dict[str, object], **override: object) -> dict[str, object]:
    """Invoke the helper with the exact bindings the public disclosure relayed."""

    binding: dict[str, object] = {
        "policy_digest": str(grant["policy_digest"]),
        "binding_digest": str(grant["binding_digest"]),
    }
    binding.update(override)
    return _invoke_helper(Path(str(grant["project_root"])), **binding)


def _workspace_cli_grant(grant: dict[str, object]) -> dict[str, object]:
    """Run the shipped workspace command in a different process on this test project."""

    command = [
        sys.executable,
        "-B",
        str(WORKSPACE / "bin" / "manual-permission"),
        "--project-root",
        str(grant["project_root"]),
        "--action",
        "mass-erase",
        "--board-id",
        BOARD,
        "--policy-digest",
        str(grant["policy_digest"]),
        "--binding-digest",
        str(grant["binding_digest"]),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    OBSERVATIONS.setdefault("workspace_cli", []).append(
        {
            "argv": command,
            "exit": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    )
    if result.returncode != 0:
        raise AssertionError(
            f"workspace helper failed: {result.returncode}: {result.stdout} {result.stderr}"
        )
    return json.loads(result.stdout)


def _approve(server: Any) -> dict[str, object]:
    return _public_json_reply(server, "target_unlock-plan", _unlock_fields(permission="one-time"))


def _grant_and_reserve(
    testcase: unittest.TestCase,
    server: Any,
    manual: ManualPermissionRepository,
    *,
    workspace_cli: bool = False,
) -> Attempt:
    """Disclose, run a real human helper grant, and reserve it with fresh approval."""

    grant = _disclose(testcase, server)
    # A fresh human grant is only ever requested from a clean locked record.
    testcase.assertEqual(grant["state"], "locked", grant)
    testcase.assertEqual(Path(str(grant["project_root"])).resolve(), manual.project_root)
    issued = _workspace_cli_grant(grant) if workspace_cli else _human_grant(grant)
    testcase.assertEqual(issued["status"], "manual_grant_unlocked", issued)
    approved = _approve(server)
    testcase.assertEqual(approved["status"], "unlock_plan_approved", approved)
    reserved = manual.status("mass-erase")
    testcase.assertEqual(reserved.state, "reserved")
    testcase.assertEqual(reserved.grant_id, issued["grant_id"])
    testcase.assertEqual(reserved.server_run_id, manual.server_run_id)
    assert reserved.claim_id is not None
    return Attempt(
        str(issued["grant_id"]),
        reserved.claim_id,
        str(grant["policy_digest"]),
        str(grant["binding_digest"]),
    )


def _reconnect(server: Any, backend: DeterministicTargetBackend) -> None:
    """Model the human reconnect that follows the finalizer's revocation."""

    handle = backend.open(board=None, unique_id="recovery-probe", target="nrf52840")
    server._promote_open_session(BOARD, handle, gate_reason="VI03 reconnect after recovery")


def _release(server: Any) -> None:
    if server.connection_manager.maybe_connection(BOARD) is not None:
        server.disconnect(BOARD)


def _report_statuses(server: Any) -> Counter[str]:
    """Read the attempt outcomes the real ReportWriter persisted for this project."""

    validation = server._firm_store.layout.validation
    if not validation.is_dir():
        return Counter()
    statuses = (
        json.loads(path.read_text(encoding="utf-8"))["terminal_status"]
        for path in validation.glob("target-unlock-*/report.json")
    )
    # Plan disclosures are reported too; count only recovery attempt outcomes.
    return Counter(status for status in statuses if status in {FAILED, COMPLETED, UNCERTAIN})


@contextmanager
def _unwritable_reports(testcase: unittest.TestCase, server: Any) -> Iterator[None]:
    """Make the real ReportWriter's validation directory unusable for one attempt."""

    validation = server._firm_store.layout.validation
    aside = validation.with_name(validation.name + ".vi03-aside")
    testcase.assertFalse(aside.exists())
    if validation.exists():
        validation.rename(aside)
    validation.parent.mkdir(parents=True, exist_ok=True)
    validation.write_bytes(b"VI03 report obstruction\n")
    try:
        yield
    finally:
        validation.unlink()
        if aside.exists():
            aside.rename(validation)


def _assert_no_replay(
    testcase: unittest.TestCase, manual: ManualPermissionRepository, spent: Attempt
) -> None:
    """Old grant/claim replays and stale cleanup never change the current record."""

    before = _record_bytes(manual)
    with testcase.assertRaises(ManualPermissionError):
        manual.consume_mass_erase(BOARD, spent.policy_digest, spent.binding_digest, spent.claim_id)
    with testcase.assertRaises(ManualPermissionError):
        manual.reserve_mass_erase(BOARD, spent.grant_id, spent.policy_digest, spent.binding_digest)
    manual.abandon_mass_erase(BOARD, spent.claim_id)
    manual.retire_mass_erase(BOARD, spent.policy_digest, spent.binding_digest, spent.claim_id)
    testcase.assertEqual(_record_bytes(manual), before)


def _assert_closed(
    testcase: unittest.TestCase, manual: ManualPermissionRepository, spent: Attempt
) -> None:
    """A terminal grant leaves a clean locked current-run record and refuses replay."""

    record = manual.status("mass-erase")
    testcase.assertEqual(record.state, "locked", record)
    testcase.assertEqual(record.server_run_id, manual.server_run_id)
    testcase.assertIsNone(record.grant_id)
    testcase.assertIsNone(record.claim_id)
    _assert_no_replay(testcase, manual, spent)


def _consume_next(testcase: unittest.TestCase, repository: ManualPermissionRepository) -> Attempt:
    """Spend one real helper-issued grant through reserve and durable consumption."""

    if repository.status("mass-erase").state == "locked":
        issued = _invoke_helper(repository.project_root)
        testcase.assertEqual(issued["status"], "manual_grant_unlocked", issued)
    grant = repository.status("mass-erase")
    testcase.assertEqual(grant.state, "unlocked")
    assert grant.grant_id is not None
    claim = repository.reserve_mass_erase(BOARD, grant.grant_id, POLICY, DISCLOSURE)
    repository.consume_mass_erase(BOARD, POLICY, DISCLOSURE, claim)
    return Attempt(grant.grant_id, claim, POLICY, DISCLOSURE)


def _run_together(*actions: Any) -> None:
    barrier = threading.Barrier(len(actions))

    def start(action: Any) -> None:
        barrier.wait()
        action()

    workers = [threading.Thread(target=start, args=(action,)) for action in actions]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=15)
    if any(worker.is_alive() for worker in workers):
        raise AssertionError("a racing permission worker did not finish")


class PublicRecoveryGrantLifecycleTests(unittest.TestCase):
    def test_workspace_cli_g1_failure_then_distinct_g2_uses_actual_server_protocol(self) -> None:
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = DeterministicTargetBackend(
                recovery_supported=True, recover_error=RuntimeError(BACKEND_FAILURE)
            )
            manual = _configure_public_native_lite(server, root, backend)
            try:
                g1 = _grant_and_reserve(self, server, manual, workspace_cli=True)
                with self.assertRaisesRegex(Exception, BACKEND_FAILURE):
                    _attempt(server)
                _assert_closed(self, manual, g1)
                self.assertEqual(_recover_calls(backend), 1)
                _reconnect(server, backend)
                backend.recover_error = None
                g2 = _grant_and_reserve(self, server, manual, workspace_cli=True)
                self.assertNotEqual(g1.grant_id, g2.grant_id)
                self.assertNotEqual(g1.claim_id, g2.claim_id)
                _assert_no_replay(self, manual, g1)
                self.assertIn(
                    "Target unlock completed", public_text(server, "target_unlock", UNLOCK)
                )
                _assert_closed(self, manual, g2)
                self.assertEqual(_recover_calls(backend), 2)
                self.assertEqual(_report_statuses(server), Counter({FAILED: 1, COMPLETED: 1}))
            finally:
                _release(server)

    def test_failed_recovery_closes_g1_and_fresh_human_g2_recovers_in_the_same_run(self) -> None:
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = DeterministicTargetBackend(
                recovery_supported=True, recover_error=RuntimeError(BACKEND_FAILURE)
            )
            manual = _configure_public_native_lite(server, root, backend)
            run_id = server.server_run.run_id
            try:
                g1 = _grant_and_reserve(self, server, manual)
                with self.assertRaisesRegex(Exception, BACKEND_FAILURE):
                    _attempt(server)
                self.assertEqual(_recover_calls(backend), 1)
                self.assertIsNone(server.connection_manager.maybe_connection(BOARD))
                self.assertIsNone(server.gate_manager.snapshot(BOARD))
                _assert_closed(self, manual, g1)
                with self.assertRaisesRegex(Exception, "target_unlock.*locked"):
                    _attempt(server)
                self.assertEqual(_recover_calls(backend), 1)

                backend.recover_error = None
                _reconnect(server, backend)
                g2 = _grant_and_reserve(self, server, manual)
                self.assertNotEqual(g2.grant_id, g1.grant_id)
                self.assertNotEqual(g2.claim_id, g1.claim_id)
                _assert_no_replay(self, manual, g1)
                self.assertEqual(manual.status("mass-erase").claim_id, g2.claim_id)
                completed = public_text(server, "target_unlock", UNLOCK)
                self.assertIn("Target unlock completed", completed)
                self.assertEqual(_recover_calls(backend), 2)
                self.assertEqual(server.server_run.run_id, run_id)
                _assert_closed(self, manual, g2)
                _assert_no_replay(self, manual, g1)
                self.assertEqual(_report_statuses(server), Counter({FAILED: 1, COMPLETED: 1}))
                OBSERVATIONS["failed_then_fresh_binding_changed_on_reconnect"] = (
                    g1.binding_digest != g2.binding_digest
                )
            finally:
                _release(server)

    def test_successful_recovery_closes_g1_and_later_fresh_grants_stay_distinct(self) -> None:
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = DeterministicTargetBackend(recovery_supported=True)
            manual = _configure_public_native_lite(server, root, backend)
            try:
                g1 = _grant_and_reserve(self, server, manual)
                completed = public_text(server, "target_unlock", UNLOCK)
                self.assertIn("Target unlock completed", completed)
                self.assertEqual(_recover_calls(backend), 1)
                self.assertIsNone(server.connection_manager.maybe_connection(BOARD))
                _assert_closed(self, manual, g1)

                backend.recover_error = RuntimeError(BACKEND_FAILURE)
                _reconnect(server, backend)
                g2 = _grant_and_reserve(self, server, manual)
                self.assertNotEqual(g2.grant_id, g1.grant_id)
                with self.assertRaisesRegex(Exception, BACKEND_FAILURE):
                    _attempt(server)
                self.assertEqual(_recover_calls(backend), 2)
                _assert_closed(self, manual, g2)
                _assert_no_replay(self, manual, g1)

                _reconnect(server, backend)
                issued = _human_grant(_disclose(self, server))
                self.assertEqual(issued["status"], "manual_grant_unlocked", issued)
                self.assertNotIn(issued["grant_id"], {g1.grant_id, g2.grant_id})
                _assert_no_replay(self, manual, g1)
                _assert_no_replay(self, manual, g2)
                self.assertEqual(_recover_calls(backend), 2)
                self.assertEqual(_report_statuses(server), Counter({COMPLETED: 1, FAILED: 1}))
            finally:
                _release(server)

    def test_failed_backend_with_unwritable_report_still_finalizes_and_closes_g1(self) -> None:
        """ROOT clarification: the failure report cannot skip finalization or mask the error."""

        with isolated_project() as root, tiered_test_server(root) as server:
            backend = DeterministicTargetBackend(
                recovery_supported=True, recover_error=RuntimeError(BACKEND_FAILURE)
            )
            manual = _configure_public_native_lite(server, root, backend)
            try:
                g1 = _grant_and_reserve(self, server, manual)
                with _unwritable_reports(self, server):
                    with self.assertRaises(Exception) as raised:
                        _attempt(server)
                text = _exception_text(raised.exception)
                OBSERVATIONS["failed_backend_report_failure"] = text
                self.assertIn(BACKEND_FAILURE, str(raised.exception))
                self.assertIn("report could not be written", text)
                self.assertEqual(_recover_calls(backend), 1)
                self.assertIsNone(server.connection_manager.maybe_connection(BOARD))
                self.assertIsNone(server.gate_manager.snapshot(BOARD))
                _assert_closed(self, manual, g1)

                backend.recover_error = None
                _reconnect(server, backend)
                g2 = _grant_and_reserve(self, server, manual)
                self.assertIn(
                    "Target unlock completed", public_text(server, "target_unlock", UNLOCK)
                )
                self.assertEqual(_recover_calls(backend), 2)
                _assert_closed(self, manual, g2)
                _assert_no_replay(self, manual, g1)
            finally:
                _release(server)

    def test_completed_recovery_with_unwritable_report_is_truthful_and_closes_g1(self) -> None:
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = DeterministicTargetBackend(recovery_supported=True)
            manual = _configure_public_native_lite(server, root, backend)
            try:
                g1 = _grant_and_reserve(self, server, manual)
                with _unwritable_reports(self, server):
                    with self.assertRaises(Exception) as raised:
                        _attempt(server)
                message = str(raised.exception)
                OBSERVATIONS["completed_report_failure"] = _exception_text(raised.exception)
                self.assertIn("Target unlock completed", message)
                self.assertIn("completion report could not be written", message)
                self.assertIn("board_validate", message)
                self.assertEqual(_recover_calls(backend), 1)
                self.assertIsNone(server.connection_manager.maybe_connection(BOARD))
                self.assertIsNone(server.gate_manager.snapshot(BOARD))
                _assert_closed(self, manual, g1)

                _reconnect(server, backend)
                g2 = _grant_and_reserve(self, server, manual)
                self.assertIn(
                    "Target unlock completed", public_text(server, "target_unlock", UNLOCK)
                )
                self.assertEqual(_recover_calls(backend), 2)
                _assert_closed(self, manual, g2)
            finally:
                _release(server)

    def test_completed_recovery_with_finalizer_failure_is_truthful_and_closes_g1(self) -> None:
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = _CloseFailingBackend(recovery_supported=True)
            manual = _configure_public_native_lite(server, root, backend)
            try:
                g1 = _grant_and_reserve(self, server, manual)
                backend.close_error = RuntimeError("simulated worker close failure")
                try:
                    with self.assertRaises(Exception) as raised:
                        _attempt(server)
                finally:
                    backend.close_error = None
                message = str(raised.exception)
                OBSERVATIONS["completed_finalizer_failure"] = _exception_text(raised.exception)
                self.assertIn(
                    "Target recovery completed, but connection cleanup was uncertain", message
                )
                self.assertEqual(_recover_calls(backend), 1)
                self.assertIsNone(server.connection_manager.maybe_connection(BOARD))
                self.assertIsNone(server.gate_manager.snapshot(BOARD))
                self.assertEqual(_report_statuses(server), Counter({UNCERTAIN: 1}))
                _assert_closed(self, manual, g1)

                _reconnect(server, backend)
                g2 = _grant_and_reserve(self, server, manual)
                self.assertIn(
                    "Target unlock completed", public_text(server, "target_unlock", UNLOCK)
                )
                self.assertEqual(_recover_calls(backend), 2)
                _assert_closed(self, manual, g2)
            finally:
                _release(server)

    def test_completed_recovery_with_finalizer_and_report_failure_closes_g1(self) -> None:
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = _CloseFailingBackend(recovery_supported=True)
            manual = _configure_public_native_lite(server, root, backend)
            try:
                g1 = _grant_and_reserve(self, server, manual)
                backend.close_error = RuntimeError("simulated worker close failure")
                try:
                    with _unwritable_reports(self, server):
                        with self.assertRaises(Exception) as raised:
                            _attempt(server)
                finally:
                    backend.close_error = None
                text = _exception_text(raised.exception)
                OBSERVATIONS["completed_finalizer_and_report_failure"] = text
                self.assertIn(
                    "Target recovery completed, but connection cleanup was uncertain",
                    str(raised.exception),
                )
                self.assertIn("report could not be written", text)
                self.assertEqual(_recover_calls(backend), 1)
                self.assertIsNone(server.connection_manager.maybe_connection(BOARD))
                _assert_closed(self, manual, g1)

                _reconnect(server, backend)
                g2 = _grant_and_reserve(self, server, manual)
                self.assertIn(
                    "Target unlock completed", public_text(server, "target_unlock", UNLOCK)
                )
                _assert_closed(self, manual, g2)
            finally:
                _release(server)

    def test_failed_backend_with_finalizer_failure_keeps_backend_primary_and_closes_g1(
        self,
    ) -> None:
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = _CloseFailingBackend(
                recovery_supported=True, recover_error=RuntimeError(BACKEND_FAILURE)
            )
            manual = _configure_public_native_lite(server, root, backend)
            try:
                g1 = _grant_and_reserve(self, server, manual)
                backend.close_error = RuntimeError("simulated worker close failure")
                try:
                    with self.assertRaises(Exception) as raised:
                        _attempt(server)
                finally:
                    backend.close_error = None
                text = _exception_text(raised.exception)
                OBSERVATIONS["failed_backend_finalizer_failure"] = text
                self.assertIn(BACKEND_FAILURE, str(raised.exception))
                self.assertIn("recovery cleanup after failed backend attempt also failed", text)
                self.assertEqual(_recover_calls(backend), 1)
                self.assertIsNone(server.connection_manager.maybe_connection(BOARD))
                _assert_closed(self, manual, g1)

                backend.recover_error = None
                _reconnect(server, backend)
                g2 = _grant_and_reserve(self, server, manual)
                self.assertIn(
                    "Target unlock completed", public_text(server, "target_unlock", UNLOCK)
                )
                _assert_closed(self, manual, g2)
            finally:
                _release(server)

    def test_failure_before_durable_consumption_releases_g1_without_backend_io(self) -> None:
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = DeterministicTargetBackend(recovery_supported=True)
            manual = _configure_public_native_lite(server, root, backend)
            try:
                g1 = _grant_and_reserve(self, server, manual)
                failure = ManualPermissionError(
                    "malformed", "simulated failure before durable consumption"
                )
                with patch.object(manual, "consume_mass_erase", side_effect=failure):
                    with self.assertRaisesRegex(Exception, "before durable consumption"):
                        _attempt(server)
                self.assertEqual(_recover_calls(backend), 0)
                # No recovery was attempted, so the validated connection is not revoked.
                self.assertIsNotNone(server.connection_manager.maybe_connection(BOARD))
                _assert_closed(self, manual, g1)

                g2 = _grant_and_reserve(self, server, manual)
                self.assertNotEqual(g2.grant_id, g1.grant_id)
                self.assertIn(
                    "Target unlock completed", public_text(server, "target_unlock", UNLOCK)
                )
                self.assertEqual(_recover_calls(backend), 1)
                _assert_closed(self, manual, g2)
                _assert_no_replay(self, manual, g1)
            finally:
                _release(server)

    def test_failure_after_durable_consumption_closes_g1_without_backend_io(self) -> None:
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = DeterministicTargetBackend(recovery_supported=True)
            manual = _configure_public_native_lite(server, root, backend)
            try:
                g1 = _grant_and_reserve(self, server, manual)
                durable_consume = manual.consume_mass_erase

                def consume_then_fail(*arguments: Any) -> None:
                    durable_consume(*arguments)
                    raise OSError("simulated failure after durable consumption")

                with patch.object(manual, "consume_mass_erase", side_effect=consume_then_fail):
                    with self.assertRaisesRegex(Exception, "after durable consumption"):
                        _attempt(server)
                self.assertEqual(_recover_calls(backend), 0)
                self.assertIsNotNone(server.connection_manager.maybe_connection(BOARD))
                _assert_closed(self, manual, g1)

                g2 = _grant_and_reserve(self, server, manual)
                self.assertIn(
                    "Target unlock completed", public_text(server, "target_unlock", UNLOCK)
                )
                self.assertEqual(_recover_calls(backend), 1)
                _assert_closed(self, manual, g2)
                _assert_no_replay(self, manual, g1)
            finally:
                _release(server)

    def test_request_cancellation_during_backend_recovery_still_closes_g1(self) -> None:
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = _BlockingRecoveryBackend(recovery_supported=True)
            manual = _configure_public_native_lite(server, root, backend)
            try:
                g1 = _grant_and_reserve(self, server, manual)

                async def cancel_mid_recovery() -> tuple[bool, str]:
                    task = asyncio.create_task(server.mcp.call_tool("target_unlock", dict(UNLOCK)))
                    try:
                        entered = await asyncio.to_thread(backend.entered.wait, 10)
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            return entered, "CancelledError"
                        except Exception as exc:  # noqa: BLE001 - preserved as raw evidence
                            return entered, f"{type(exc).__name__}: {exc}"
                        return entered, "completed"
                    finally:
                        backend.release.set()

                entered, outcome = asyncio.run(cancel_mid_recovery())
                workers = [
                    thread
                    for thread in threading.enumerate()
                    if thread.name.startswith("mcp-") and thread.name.endswith("-target_unlock")
                ]
                for worker in workers:
                    worker.join(10)
                OBSERVATIONS["cancellation"] = {
                    "entered_backend": entered,
                    "client_outcome": outcome,
                    "workers": [worker.name for worker in workers],
                    "workers_alive": [worker.name for worker in workers if worker.is_alive()],
                    "record_state": manual.status("mass-erase").state,
                    "reports": dict(_report_statuses(server)),
                }
                self.assertTrue(entered)
                self.assertEqual(outcome, "CancelledError")
                self.assertFalse(any(worker.is_alive() for worker in workers))
                self.assertEqual(_recover_calls(backend), 1)
                self.assertIsNone(server.connection_manager.maybe_connection(BOARD))
                self.assertIsNone(server.gate_manager.snapshot(BOARD))
                _assert_closed(self, manual, g1)

                _reconnect(server, backend)
                g2 = _grant_and_reserve(self, server, manual)
                self.assertIn(
                    "Target unlock completed", public_text(server, "target_unlock", UNLOCK)
                )
                self.assertEqual(_recover_calls(backend), 2)
                _assert_closed(self, manual, g2)
            finally:
                backend.release.set()
                _release(server)

    def test_retirement_failure_fails_closed_and_is_reported_truthfully(self) -> None:
        for label, recover_error in (
            ("failed", RuntimeError(BACKEND_FAILURE)),
            ("completed", None),
        ):
            with (
                self.subTest(label),
                isolated_project() as root,
                tiered_test_server(root) as server,
            ):
                backend = DeterministicTargetBackend(
                    recovery_supported=True, recover_error=recover_error
                )
                manual = _configure_public_native_lite(server, root, backend)
                try:
                    g1 = _grant_and_reserve(self, server, manual)
                    with patch.object(
                        manual,
                        "retire_mass_erase",
                        side_effect=OSError("simulated retirement failure"),
                    ):
                        with self.assertRaises(Exception) as raised:
                            _attempt(server)
                    text = _exception_text(raised.exception)
                    OBSERVATIONS[f"retirement_failure_{label}"] = text
                    if recover_error is not None:
                        self.assertIn(BACKEND_FAILURE, str(raised.exception))
                        self.assertIn("grant retirement failed", text)
                    else:
                        message = str(raised.exception)
                        self.assertIn("Target unlock completed", message)
                        self.assertIn("could not be confirmed", message)
                        self.assertIn("restart", message)
                    self.assertEqual(_recover_calls(backend), 1)
                    self.assertIsNone(server.connection_manager.maybe_connection(BOARD))
                    # Fail closed: the spent grant stays consumed, never reusable.
                    self.assertEqual(manual.status("mass-erase").state, "consumed")
                    refused = _invoke_helper(root)
                    self.assertEqual(refused["code"], "manual/already-consumed", refused)
                    with self.assertRaisesRegex(ManualPermissionError, "already-consumed"):
                        manual.consume_mass_erase(
                            BOARD, g1.policy_digest, g1.binding_digest, g1.claim_id
                        )
                finally:
                    _release(server)

    def test_real_anyio_cancellation_shields_terminal_cleanup_and_blocks_early_g2(self) -> None:
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = _BlockingRecoveryBackend(recovery_supported=True)
            manual = _configure_public_native_lite(server, root, backend)
            try:
                g1 = _grant_and_reserve(self, server, manual)

                async def cancel_in_backend() -> dict[str, object]:
                    done = anyio.Event()
                    scopes: list[anyio.CancelScope] = []

                    async def request() -> None:
                        try:
                            with anyio.CancelScope() as scope:
                                scopes.append(scope)
                                await server.mcp.call_tool("target_unlock", dict(UNLOCK))
                        finally:
                            done.set()

                    async with anyio.create_task_group() as group:
                        group.start_soon(request)
                        try:
                            with anyio.fail_after(8):
                                while not backend.entered.is_set():
                                    await anyio.sleep(0.01)
                            # The irreversible boundary sees durable consumption;
                            # a helper invocation cannot publish G2 while G1 is live.
                            self.assertEqual(manual.status("mass-erase").state, "consumed")
                            early = _invoke_helper(root)
                            self.assertEqual(early["code"], "manual/already-consumed", early)
                            scopes[0].cancel()
                            await anyio.sleep(0.05)
                            self.assertFalse(
                                done.is_set(), "cancellation returned before shielded cleanup"
                            )
                            self.assertEqual(manual.status("mass-erase").state, "consumed")
                        finally:
                            backend.release.set()
                        with anyio.fail_after(8):
                            await done.wait()
                    return {
                        "scope_class": type(scopes[0]).__module__ + "." + type(scopes[0]).__name__,
                        "cancel_called": scopes[0].cancel_called,
                        "cancelled_caught": scopes[0].cancelled_caught,
                        "early_helper": early,
                    }

                proof = anyio.run(cancel_in_backend)
                OBSERVATIONS["anyio_cancellation"] = proof
                self.assertEqual(proof["scope_class"], "anyio._backends._asyncio.CancelScope")
                self.assertTrue(proof["cancel_called"])
                self.assertTrue(proof["cancelled_caught"])
                self.assertEqual(_recover_calls(backend), 1)
                self.assertIsNone(server.connection_manager.maybe_connection(BOARD))
                self.assertIsNone(server.gate_manager.snapshot(BOARD))
                _assert_closed(self, manual, g1)
                _reconnect(server, backend)
                g2 = _grant_and_reserve(self, server, manual)
                self.assertIn(
                    "Target unlock completed", public_text(server, "target_unlock", UNLOCK)
                )
                _assert_closed(self, manual, g2)
                self.assertEqual(_recover_calls(backend), 2)
            finally:
                backend.release.set()
                _release(server)

    def test_retirement_cleanup_error_after_lock_commit_does_not_claim_still_consumed(self) -> None:
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = DeterministicTargetBackend(recovery_supported=True)
            manual = _configure_public_native_lite(server, root, backend)
            try:
                g1 = _grant_and_reserve(self, server, manual)
                retire = manual.retire_mass_erase

                def retire_then_fail(*arguments: Any) -> None:
                    retire(*arguments)
                    raise OSError("simulated retirement cleanup error after lock commit")

                with patch.object(manual, "retire_mass_erase", side_effect=retire_then_fail):
                    with self.assertRaises(Exception) as raised:
                        _attempt(server)
                message = str(raised.exception)
                self.assertIn("Target unlock completed", message)
                self.assertIn("could not be confirmed", message)
                self.assertNotIn("stays consumed", message)
                _assert_closed(self, manual, g1)
                self.assertEqual(_recover_calls(backend), 1)
                self.assertIsNone(server.connection_manager.maybe_connection(BOARD))
                _reconnect(server, backend)
                g2 = _grant_and_reserve(self, server, manual)
                self.assertIn(
                    "Target unlock completed", public_text(server, "target_unlock", UNLOCK)
                )
                _assert_closed(self, manual, g2)
            finally:
                _release(server)

    def test_mismatched_fresh_grants_are_refused_without_backend_io(self) -> None:
        cases: tuple[tuple[str, dict[str, object] | None, str], ...] = (
            ("wrong-board", {"board_id": "other_board"}, "manual/wrong-board"),
            ("wrong-disclosure", {"binding_digest": "c" * 64}, "manual/binding-mismatch"),
            ("stale-policy", {"policy_digest": "d" * 64}, "manual/stale-policy"),
            ("previous-startup", None, "manual/stale-epoch"),
        )
        refusals: dict[str, object] = {}
        for label, override, code in cases:
            with (
                self.subTest(label),
                isolated_project() as root,
                tiered_test_server(root) as server,
            ):
                backend = DeterministicTargetBackend(recovery_supported=True)
                manual = _configure_public_native_lite(server, root, backend)
                try:
                    g1 = _grant_and_reserve(self, server, manual)
                    public_text(server, "target_unlock", UNLOCK)
                    _assert_closed(self, manual, g1)
                    _reconnect(server, backend)
                    grant = _disclose(self, server)
                    if override is None:
                        previous = ManualPermissionRepository(root, "previous-startup")
                        previous._atomic_write(  # noqa: SLF001 - forge another run's record
                            manual._action_path("mass-erase"),  # noqa: SLF001
                            previous._document(  # noqa: SLF001
                                action="mass-erase",
                                state="unlocked",
                                grant_id="grant-previous-startup",
                                board_id=BOARD,
                                policy_digest=str(grant["policy_digest"]),
                                binding_digest=str(grant["binding_digest"]),
                                claim_id=None,
                            ),
                        )
                    else:
                        issued = _human_grant(grant, **override)
                        self.assertEqual(issued["status"], "manual_grant_unlocked", issued)
                    stray = _record_bytes(manual)
                    with self.assertRaisesRegex(Exception, code) as raised:
                        _approve(server)
                    self.assertEqual(_record_bytes(manual), stray)
                    with self.assertRaisesRegex(Exception, "target_unlock.*locked"):
                        _attempt(server)
                    self.assertEqual(_recover_calls(backend), 1)
                    _assert_no_replay(self, manual, g1)
                    later = _human_grant(grant)
                    refusals[label] = {
                        "approval_refusal": str(raised.exception),
                        "record_state_after_refusal": manual.status("mass-erase").state,
                        "later_helper_invocation": later.get("code", later.get("status")),
                    }
                finally:
                    _release(server)
        OBSERVATIONS["mismatched_fresh_grant_refusals"] = refusals


class RetirementProtocolTests(unittest.TestCase):
    def test_retire_binds_claim_board_disclosure_and_current_policy(self) -> None:
        with isolated_project() as root:
            repository = ManualPermissionRepository(root, "run-one")
            repository.reset_startup()
            g1 = _consume_next(self, repository)
            consumed = _record_bytes(repository)
            repository.retire_mass_erase(BOARD, POLICY, DISCLOSURE, "0" * 32)
            self.assertEqual(_record_bytes(repository), consumed)
            for label, binding, code in (
                ("wrong-board", ("other_board", POLICY, DISCLOSURE), "manual/wrong-board"),
                ("stale-policy", (BOARD, "d" * 64, DISCLOSURE), "manual/stale-policy"),
                ("wrong-disclosure", (BOARD, POLICY, "c" * 64), "manual/binding-mismatch"),
            ):
                with self.subTest(label):
                    with self.assertRaisesRegex(ManualPermissionError, code):
                        repository.retire_mass_erase(*binding, g1.claim_id)
                    self.assertEqual(_record_bytes(repository), consumed)
            refused = _invoke_helper(root)
            self.assertEqual(refused["code"], "manual/already-consumed", refused)

            repository.retire_mass_erase(BOARD, POLICY, DISCLOSURE, g1.claim_id)
            _assert_closed(self, repository, g1)
            issued = _invoke_helper(root)
            self.assertEqual(issued["status"], "manual_grant_unlocked", issued)
            self.assertNotEqual(issued["grant_id"], g1.grant_id)
            _assert_no_replay(self, repository, g1)

    def test_retire_refuses_previous_startup_and_wrong_action_records(self) -> None:
        with isolated_project() as root:
            current = ManualPermissionRepository(root, "run-two")
            current.reset_startup()
            previous = ManualPermissionRepository(root, "run-one")
            claim = "1" * 32
            for label, writer, action, code in (
                ("previous-startup", previous, "mass-erase", "manual/stale-epoch"),
                ("wrong-action", current, "downgrade", "manual/wrong-action"),
            ):
                with self.subTest(label):
                    current._atomic_write(  # noqa: SLF001 - forge one protocol record
                        current._action_path("mass-erase"),  # noqa: SLF001
                        writer._document(  # noqa: SLF001
                            action=action,
                            state="consumed",
                            grant_id="grant-forged",
                            board_id=BOARD,
                            policy_digest=POLICY,
                            binding_digest=DISCLOSURE,
                            claim_id=claim,
                        ),
                    )
                    forged = _record_bytes(current)
                    with self.assertRaisesRegex(ManualPermissionError, code):
                        current.retire_mass_erase(BOARD, POLICY, DISCLOSURE, claim)
                    self.assertEqual(_record_bytes(current), forged)
            not_ready = ManualPermissionRepository(root, "never-reset")
            with self.assertRaisesRegex(ManualPermissionError, "manual/reset-failed"):
                not_ready.retire_mass_erase(BOARD, POLICY, DISCLOSURE, claim)

    def test_stale_retire_never_touches_a_fresh_unlocked_reserved_or_consumed_g2(self) -> None:
        with isolated_project() as root:
            repository = ManualPermissionRepository(root, "run-one")
            repository.reset_startup()
            g1 = _consume_next(self, repository)
            repository.retire_mass_erase(BOARD, POLICY, DISCLOSURE, g1.claim_id)
            issued = _invoke_helper(root)
            self.assertEqual(issued["status"], "manual_grant_unlocked", issued)
            g2_claim = ""
            for stage in ("unlocked", "reserved", "consumed"):
                if stage == "reserved":
                    g2_claim = repository.reserve_mass_erase(
                        BOARD, str(issued["grant_id"]), POLICY, DISCLOSURE
                    )
                if stage == "consumed":
                    repository.consume_mass_erase(BOARD, POLICY, DISCLOSURE, g2_claim)
                with self.subTest(stage):
                    self.assertEqual(repository.status("mass-erase").state, stage)
                    self.assertEqual(repository.status("mass-erase").grant_id, issued["grant_id"])
                    _assert_no_replay(self, repository, g1)

    def test_racing_fresh_human_grants_after_retirement_produce_one_grant(self) -> None:
        with isolated_project() as root:
            repository = ManualPermissionRepository(root, "run-one")
            repository.reset_startup()
            g1 = _consume_next(self, repository)
            repository.retire_mass_erase(BOARD, POLICY, DISCLOSURE, g1.claim_id)
            results: list[dict[str, object]] = []
            _run_together(*(lambda: results.append(_invoke_helper(root)) for _ in range(2)))
            statuses = Counter(str(result["status"]) for result in results)
            self.assertEqual(statuses, Counter({"manual_grant_unlocked": 1, "refused": 1}))
            granted = next(r for r in results if r["status"] == "manual_grant_unlocked")
            refused = next(r for r in results if r["status"] == "refused")
            self.assertEqual(refused["code"], "manual/already-reserved", refused)
            self.assertEqual(repository.status("mass-erase").grant_id, granted["grant_id"])
            _assert_no_replay(self, repository, g1)

    def test_racing_reservations_of_a_fresh_g2_produce_one_owner(self) -> None:
        with isolated_project() as root:
            repository = ManualPermissionRepository(root, "run-one")
            repository.reset_startup()
            g1 = _consume_next(self, repository)
            repository.retire_mass_erase(BOARD, POLICY, DISCLOSURE, g1.claim_id)
            issued = _invoke_helper(root)
            self.assertEqual(issued["status"], "manual_grant_unlocked", issued)
            claims: list[str] = []
            refusals: list[str] = []

            def reserve() -> None:
                try:
                    claims.append(
                        repository.reserve_mass_erase(
                            BOARD, str(issued["grant_id"]), POLICY, DISCLOSURE
                        )
                    )
                except ManualPermissionError as exc:
                    refusals.append(exc.code)

            _run_together(reserve, reserve)
            self.assertEqual(len(claims), 1)
            self.assertEqual(refusals, ["already-reserved"])
            self.assertEqual(repository.status("mass-erase").claim_id, claims[0])
            _assert_no_replay(self, repository, g1)

    def test_stale_retire_racing_a_fresh_human_grant_never_overwrites_it(self) -> None:
        orders: Counter[str] = Counter()
        with isolated_project() as root:
            repository = ManualPermissionRepository(root, "run-one")
            repository.reset_startup()
            for iteration in range(20):
                spent = _consume_next(self, repository)
                retire_errors: list[BaseException] = []
                grants: list[dict[str, object]] = []

                def retire(spent: Attempt = spent) -> None:
                    try:
                        repository.retire_mass_erase(BOARD, POLICY, DISCLOSURE, spent.claim_id)
                    except BaseException as exc:  # noqa: BLE001 - asserted below
                        retire_errors.append(exc)

                _run_together(retire, lambda: grants.append(_invoke_helper(root)))
                with self.subTest(iteration=iteration):
                    self.assertEqual(retire_errors, [])
                    record = repository.status("mass-erase")
                    if grants[0]["status"] == "manual_grant_unlocked":
                        orders["retire-then-fresh-grant"] += 1
                        self.assertEqual(record.state, "unlocked")
                        self.assertEqual(record.grant_id, grants[0]["grant_id"])
                    else:
                        orders["fresh-grant-refused-before-retire"] += 1
                        self.assertEqual(grants[0]["code"], "manual/already-consumed")
                        self.assertEqual(record.state, "locked")
                    _assert_no_replay(self, repository, spent)
        OBSERVATIONS["stale_retire_vs_fresh_grant_orders"] = dict(orders)

    def test_downgrade_grants_are_unchanged_by_mass_erase_retirement(self) -> None:
        with isolated_project() as root:
            repository = ManualPermissionRepository(root, "run-one")
            repository.reset_startup()
            downgrade = _invoke_helper(root, action="downgrade")
            self.assertEqual(downgrade["status"], "manual_grant_unlocked", downgrade)
            token = repository.issue_downgrade_token(BOARD, str(downgrade["grant_id"]), POLICY)
            repository.consume_downgrade(BOARD, token, POLICY)
            consumed_downgrade = _record_bytes(repository, "downgrade")

            g1 = _consume_next(self, repository)
            repository.retire_mass_erase(BOARD, POLICY, DISCLOSURE, g1.claim_id)
            self.assertEqual(_record_bytes(repository, "downgrade"), consumed_downgrade)
            self.assertEqual(repository.status("downgrade").state, "consumed")
            refused = _invoke_helper(root, action="downgrade")
            self.assertEqual(refused["code"], "manual/already-consumed", refused)
            with self.assertRaisesRegex(ManualPermissionError, "already-consumed"):
                repository.consume_downgrade(BOARD, token, POLICY)
            fresh = _invoke_helper(root)
            self.assertEqual(fresh["status"], "manual_grant_unlocked", fresh)
            self.assertEqual(_record_bytes(repository, "downgrade"), consumed_downgrade)


if __name__ == "__main__":
    unittest.main()
