"""VI03/M14: a terminal attempt closes its mass-erase grant; a fresh human grant may follow.

Every public scenario drives the real ``target_unlock`` coordinator, the manual
permission repository, the shipped workspace helper, the real ReportWriter and the
recovery finalizer.  Only the recovery backend is simulated; nothing touches hardware.
"""

from __future__ import annotations
from tests.recovery_fixture_support import Attempt, OBSERVATIONS, UNLOCK, _assert_closed, _assert_no_replay, _grant_and_reserve, _invoke_helper, _recover_calls, _release

import asyncio
import json
import threading
import unittest
from collections import Counter
from typing import Any

import anyio

from pyocd_debug_mcp.capabilities.manual_permissions import ManualPermissionRepository

from tests.recovery_fixture_support import BOARD, DISCLOSURE, POLICY, _configure_public_native_lite
from tests.tiered_acceptance_support import (
    DeterministicTargetBackend,
    isolated_project,
    public_text,
    tiered_test_server,
)


BACKEND_FAILURE = "simulated backend recovery failure"
FAILED = "unlock_failed_revalidation_required"
COMPLETED = "unlock_completed_revalidation_required"
UNCERTAIN = "unlock_completed_cleanup_uncertain_reconnect_required"


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


def _attempt(server: Any) -> Any:
    return asyncio.run(server.mcp.call_tool("target_unlock", dict(UNLOCK)))


def _reconnect(server: Any, backend: DeterministicTargetBackend) -> None:
    """Model the human reconnect that follows the finalizer's revocation."""

    handle = backend.open(board=None, unique_id="recovery-probe", target="nrf52840")
    server._promote_open_session(BOARD, handle, gate_reason="VI03 reconnect after recovery")


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


class RetirementProtocolTests(unittest.TestCase):

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


if __name__ == "__main__":
    unittest.main()
