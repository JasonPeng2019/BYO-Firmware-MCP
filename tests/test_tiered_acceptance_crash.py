"""AT02: tier publication survives ordinary failures and abrupt process death."""

from __future__ import annotations

import json
import unittest

from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier

from tests.tiered_acceptance_support import (
    confirmed_lite_policy,
    isolated_project,
    known_good_full_policy,
    run_child,
)


BOARD = "legacy_full"
OLD_PROFILE, OLD_MAP = known_good_full_policy()
NEW_MAP = confirmed_lite_policy(BOARD)


def _initial_state(root):  # type: ignore[no-untyped-def]
    return CapabilityPolicyRepository(root).commit(
        BOARD,
        Tier.SETUP_FULL,
        profile_snapshot=OLD_PROFILE,
        map_snapshot=OLD_MAP,
        evidence={"source": "old-confirmed-evidence"},
    )


def _crashing_commit_script() -> str:
    # The fault name and explicit test gate come from Appendix A.  No exception
    # handler can prove these crash semantics: policy.py deliberately os._exit()s.
    return "\n".join(
        (
            "from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier",
            f"board = {BOARD!r}",
            f"new_map = {NEW_MAP!r}",
            "CapabilityPolicyRepository(__import__('pathlib').Path(__import__('os').environ['BYO_MCP_ARTIFACT_ROOT'])).commit(",
            "    board, Tier.SETUP_LITE, map_snapshot=new_map, evidence={'source': 'new-evidence'}",
            ")",
        )
    )


def _crashing_downgrade_script() -> str:
    """Return a child program whose only transition is the policy downgrade."""

    return "\n".join(
        (
            "from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository",
            f"board = {BOARD!r}",
            "CapabilityPolicyRepository(__import__('pathlib').Path(__import__('os').environ['BYO_MCP_ARTIFACT_ROOT'])).downgrade_to_no_setup(board)",
        )
    )


class TieredCrashAcceptanceTests(unittest.TestCase):
    def test_ordinary_pre_commit_failures_leave_the_old_complete_generation_authoritative(
        self,
    ) -> None:
        """AT02: callback failures are a useful non-crash companion to child death."""

        for fault in ("after_generation_write", "before_pointer_replace"):
            with self.subTest(fault=fault), isolated_project() as root:
                old = _initial_state(root)

                def fail_at(name: str) -> None:
                    if name == fault:
                        raise OSError(f"ordinary simulated failure at {name}")

                with self.assertRaisesRegex(OSError, fault):
                    CapabilityPolicyRepository(root, fault_hook=fail_at).commit(
                        BOARD,
                        Tier.SETUP_LITE,
                        map_snapshot=NEW_MAP,
                        evidence={"source": "new-evidence"},
                    )

                restored = CapabilityPolicyRepository(root).resolve(BOARD)
                self.assertEqual(restored.tier, Tier.SETUP_FULL)
                self.assertEqual(restored.policy_digest, old.policy_digest)
                self.assertEqual(restored.map_snapshot, OLD_MAP)
                self.assertEqual(restored.evidence, {"source": "old-confirmed-evidence"})

    def test_abrupt_child_crash_before_pointer_keeps_old_tier_map_and_evidence_after_restart(
        self,
    ) -> None:
        """AT02: a real child termination cannot create a mixed or raw policy window."""

        for fault in ("after_generation_write", "before_pointer_replace"):
            with self.subTest(fault=fault), isolated_project() as root:
                old = _initial_state(root)
                result = run_child(
                    root,
                    _crashing_commit_script(),
                    extra_env={"BYO_MCP_TIER_FAULT": fault},
                )
                self.assertEqual(result.returncode, 86, result)

                restarted = CapabilityPolicyRepository(root).resolve(BOARD)
                self.assertEqual(restarted.tier, Tier.SETUP_FULL)
                self.assertEqual(restarted.policy_digest, old.policy_digest)
                self.assertEqual(restarted.map_snapshot, OLD_MAP)
                self.assertEqual(restarted.evidence, {"source": "old-confirmed-evidence"})

    def test_abrupt_crash_after_pointer_commits_new_generation_before_no_response(self) -> None:
        """AT02: post-commit/pre-response death exposes exactly the new committed policy."""

        with isolated_project() as root:
            old = _initial_state(root)
            result = run_child(
                root,
                _crashing_commit_script(),
                extra_env={"BYO_MCP_TIER_FAULT": "after_pointer_replace"},
            )
            self.assertEqual(result.returncode, 86, result)

            restarted = CapabilityPolicyRepository(root).resolve(BOARD)
            self.assertEqual(restarted.tier, Tier.SETUP_LITE)
            self.assertNotEqual(restarted.policy_digest, old.policy_digest)
            self.assertEqual(restarted.map_snapshot, NEW_MAP)
            self.assertEqual(restarted.evidence, {"source": "new-evidence"})
            self.assertFalse(restarted.setup_incomplete)

            pointer = json.loads(
                (root / ".firm" / "capabilities" / BOARD / "current.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(pointer["board_id"], BOARD)
            self.assertEqual(pointer["generation_id"], restarted.generation_id)

    def test_abrupt_downgrade_crash_preserves_only_the_side_of_the_pointer_that_committed(
        self,
    ) -> None:
        """AT02: the downgrade publication phase has no hybrid or accidental raw window."""

        for fault, expected_tier in (
            ("before_pointer_replace", Tier.SETUP_LITE),
            ("after_pointer_replace", Tier.NO_SETUP),
        ):
            with self.subTest(fault=fault), isolated_project() as root:
                old = CapabilityPolicyRepository(root).commit(
                    BOARD,
                    Tier.SETUP_LITE,
                    map_snapshot=OLD_MAP,
                    evidence={"source": "confirmed-before-downgrade"},
                )
                result = run_child(
                    root,
                    _crashing_downgrade_script(),
                    extra_env={"BYO_MCP_TIER_FAULT": fault},
                )
                self.assertEqual(result.returncode, 86, result)

                restarted = CapabilityPolicyRepository(root).resolve(BOARD)
                self.assertEqual(restarted.tier, expected_tier)
                if expected_tier is Tier.SETUP_LITE:
                    self.assertEqual(restarted.policy_digest, old.policy_digest)
                    self.assertEqual(restarted.map_snapshot, OLD_MAP)
                    self.assertEqual(restarted.evidence, {"source": "confirmed-before-downgrade"})
                else:
                    self.assertTrue(restarted.setup_incomplete)
                    self.assertIsNone(restarted.map_snapshot)
                    self.assertNotEqual(restarted.policy_digest, old.policy_digest)


if __name__ == "__main__":
    unittest.main()
