"""AT04/AT07: durable manual locks, binding checks, and one-time consumption."""

from __future__ import annotations

import threading
import unittest

from pyocd_debug_mcp.capabilities.manual_permissions import (
    ManualPermissionError,
    ManualPermissionRepository,
)
from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier

from tests.tiered_acceptance_support import DeterministicTargetBackend, confirmed_lite_policy, fresh_server, isolated_project, public_call, tiered_test_server


BOARD = "left_controller"
POLICY = "a" * 64
BINDING = "b" * 64


class TieredManualPermissionAcceptanceTests(unittest.TestCase):
    """Use durable state, never a test-generated in-memory permission token."""


    def test_public_restart_relocks_a_prior_manual_grant_and_refuses_its_old_identifier(
        self,
    ) -> None:
        """AT07: a second public server run cannot consume a first-run manual grant."""

        with isolated_project() as root, tiered_test_server(root) as server:
            policies = CapabilityPolicyRepository(root)
            committed = policies.commit(
                BOARD,
                Tier.SETUP_LITE,
                map_snapshot=confirmed_lite_policy(BOARD),
                evidence={"confirmed": True},
            )
            manual = ManualPermissionRepository(root, server.server_run.run_id)
            manual.reset_startup()
            manual.write_unlocked_grant(
                action="downgrade",
                grant_id="first-run-grant",
                board_id=BOARD,
                policy_digest=str(committed.policy_digest),
            )
            server.configure_tiered_test_seams(
                policy_repository=policies,
                manual_permission_repository=manual,
            )
            issued = public_call(
                server,
                "unlock_operator",
                {"board_id": BOARD, "action": "downgrade", "grant_id": "first-run-grant"},
            )
            self.assertEqual(issued["status"], "operator_permission_issued")

            restarted = fresh_server(root)
            capabilities = public_call(restarted, "get_capabilities", {"board_id": BOARD})
            self.assertEqual(capabilities["manual_permissions"]["downgrade"]["state"], "locked")
            refusal = public_call(
                restarted,
                "unlock_operator",
                {"board_id": BOARD, "action": "downgrade", "grant_id": "first-run-grant"},
            )
            self.assertEqual(refusal["status"], "refused")
            self.assertIn(refusal["code"], {"manual/locked", "manual/stale-epoch"})

    def test_wrong_action_board_policy_and_disclosure_each_refuse_before_consumption(self) -> None:
        """AT07: grants do not interchange or loosen their exact live binding."""

        with isolated_project() as root:
            repository = ManualPermissionRepository(root, "run-one")
            repository.reset_startup()
            repository.write_unlocked_grant(
                action="mass-erase",
                grant_id="mass-grant",
                board_id=BOARD,
                policy_digest=POLICY,
                binding_digest=BINDING,
            )
            for kwargs, code in (
                (
                    {
                        "board_id": "other_board",
                        "grant_id": "mass-grant",
                        "policy_digest": POLICY,
                        "binding_digest": BINDING,
                    },
                    "wrong-board",
                ),
                (
                    {
                        "board_id": BOARD,
                        "grant_id": "mass-grant",
                        "policy_digest": "c" * 64,
                        "binding_digest": BINDING,
                    },
                    "stale-policy",
                ),
                (
                    {
                        "board_id": BOARD,
                        "grant_id": "mass-grant",
                        "policy_digest": POLICY,
                        "binding_digest": "d" * 64,
                    },
                    "binding-mismatch",
                ),
            ):
                with (
                    self.subTest(code=code),
                    self.assertRaisesRegex(ManualPermissionError, rf"manual/{code}"),
                ):
                    repository.reserve_mass_erase(**kwargs)
                self.assertEqual(repository.status("mass-erase").state, "unlocked")

    def test_duplicate_downgrade_reservations_are_at_most_once_and_second_call_is_already_reserved(
        self,
    ) -> None:
        """AT07: competing calls must not publish two operator permissions."""

        with isolated_project() as root:
            repository = ManualPermissionRepository(root, "run-one")
            repository.reset_startup()
            repository.write_unlocked_grant(
                action="downgrade", grant_id="grant-one", board_id=BOARD, policy_digest=POLICY
            )
            barrier = threading.Barrier(2)
            successes: list[str] = []
            failures: list[str] = []

            def reserve() -> None:
                barrier.wait()
                try:
                    successes.append(repository.issue_downgrade_token(BOARD, "grant-one", POLICY))
                except ManualPermissionError as exc:
                    failures.append(exc.code)

            left = threading.Thread(target=reserve)
            right = threading.Thread(target=reserve)
            left.start()
            right.start()
            left.join(timeout=5)
            right.join(timeout=5)
            self.assertFalse(
                left.is_alive() or right.is_alive(), "reservation threads did not finish"
            )
            self.assertEqual(len(successes), 1)
            self.assertEqual(failures, ["already-reserved"])

            repository.consume_downgrade(BOARD, successes[0], POLICY)
            self.assertEqual(repository.status("downgrade").state, "consumed")
            with self.assertRaisesRegex(ManualPermissionError, r"manual/already-consumed"):
                repository.consume_downgrade(BOARD, successes[0], POLICY)


    def test_public_unlock_and_downgrade_consume_one_current_manual_grant_before_policy_transition(
        self,
    ) -> None:
        """AT07: public calls cannot substitute a string/old permission for a real grant."""

        with isolated_project() as root, tiered_test_server(root) as server:
            policies = CapabilityPolicyRepository(root)
            committed = policies.commit(
                BOARD,
                Tier.SETUP_LITE,
                map_snapshot=confirmed_lite_policy(BOARD),
                evidence={"confirmed": True},
            )
            manual = ManualPermissionRepository(root, server.server_run.run_id)
            manual.reset_startup()
            manual.write_unlocked_grant(
                action="downgrade",
                grant_id="grant-one",
                board_id=BOARD,
                policy_digest=str(committed.policy_digest),
            )
            backend = DeterministicTargetBackend()
            server.configure_tiered_test_seams(
                backend=backend,
                policy_repository=policies,
                manual_permission_repository=manual,
            )
            issued = public_call(
                server,
                "unlock_operator",
                {"board_id": BOARD, "action": "downgrade", "grant_id": "grant-one"},
            )
            self.assertEqual(issued["status"], "operator_permission_issued")
            self.assertEqual(issued["policy_digest"], committed.policy_digest)
            downgraded = public_call(
                server,
                "downgrade",
                {"board_id": BOARD, "permission": issued["permission"]},
            )
            self.assertEqual(downgraded["status"], "downgraded")
            self.assertEqual(downgraded["tier"], "no-setup")
            self.assertTrue(downgraded["setup_incomplete"])
            self.assertEqual(manual.status("downgrade").state, "consumed")
            self.assertEqual(backend.calls, [], "downgrade must not mutate or validate a target")

            # A consumed token cannot be retried even if the prior transition
            # subsequently reports an error; the disk grant is already spent.
            retry = public_call(
                server,
                "downgrade",
                {"board_id": BOARD, "permission": issued["permission"]},
            )
            self.assertEqual(retry["status"], "refused")
            self.assertEqual(retry["code"], "tier/wrong-route")


if __name__ == "__main__":
    unittest.main()
