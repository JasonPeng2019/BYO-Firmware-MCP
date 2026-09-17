"""Focused durable manual-grant lifecycle contracts."""

from __future__ import annotations

import tempfile
import unittest
import json
import os
from pathlib import Path
import subprocess
import sys

from pyocd_debug_mcp.capabilities.manual_permissions import ManualPermissionRepository


class ManualPermissionRepositoryTests(unittest.TestCase):
    def test_protocol_lock_refuses_another_process_within_its_bounded_wait(self) -> None:
        """The filesystem lock, rather than an in-process RLock, serializes a grant root."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = ManualPermissionRepository(root, "run-one")
            first.reset_startup()
            script = "\n".join(
                (
                    "from pathlib import Path",
                    "import sys, time",
                    "from pyocd_debug_mcp.capabilities.manual_permissions import ManualPermissionRepository",
                    "repo = ManualPermissionRepository(Path(sys.argv[1]), 'run-one')",
                    "with repo._protocol_lock():",
                    "    print('locked', flush=True)",
                    "    time.sleep(1)",
                )
            )
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(root),
                ],
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(child.stdout.readline().strip(), "locked")  # type: ignore[union-attr]
                second = ManualPermissionRepository(root, "run-two")
                second._protocol_timeout_seconds = 0.1
                with self.assertRaisesRegex(RuntimeError, "already-reserved"):
                    second.reset_startup()
            finally:
                child.wait(timeout=5)
                if child.stdout is not None:
                    child.stdout.close()

    def test_startup_prunes_only_old_epoch_claims_and_preserves_current_helper_claim(self) -> None:
        """A current helper claim is contention, never startup garbage."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = ManualPermissionRepository(root, "run-current")
            claim_root = root / ".agent-workspace" / "runtime" / "manual-permissions"
            claim_root.mkdir(parents=True)
            current = {
                "schema_version": 1,
                "owner": "helper",
                "claim_id": "a" * 32,
                "server_run_id": "run-current",
                "action": "downgrade",
                "created_at": "2026-01-01T00:00:00Z",
            }
            old = {
                **current,
                "claim_id": "b" * 32,
                "server_run_id": "run-old",
                "action": "mass-erase",
            }
            (claim_root / "downgrade.claim").write_text(json.dumps(current), encoding="utf-8")
            (claim_root / "mass-erase.claim").write_text(json.dumps(old), encoding="utf-8")

            repository.reset_startup()

            self.assertTrue((claim_root / "downgrade.claim").is_file())
            self.assertFalse((claim_root / "mass-erase.claim").exists())
            with self.assertRaisesRegex(RuntimeError, "already-reserved"):
                repository.issue_downgrade_token("controller", "grant-1", "policy-1")

    def test_startup_reset_invalidates_old_grants_and_consumption_is_single_use(self) -> None:
        """A manual grant is project-local, run-bound, and consumed before an action."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = ManualPermissionRepository(root, "run-one")
            first.reset_startup()
            first.write_unlocked_grant(
                action="downgrade",
                grant_id="grant-1",
                board_id="controller",
                policy_digest="policy-1",
            )
            token = first.issue_downgrade_token("controller", "grant-1", "policy-1")
            first.consume_downgrade("controller", token, "policy-1")
            with self.assertRaisesRegex(RuntimeError, "already-consumed"):
                first.consume_downgrade("controller", token, "policy-1")

            restarted = ManualPermissionRepository(root, "run-two")
            restarted.reset_startup()
            with self.assertRaisesRegex(RuntimeError, "locked"):
                restarted.issue_downgrade_token("controller", "grant-1", "policy-1")

    def test_stale_downgrade_and_abandoned_mass_erase_require_fresh_same_run_grants(self) -> None:
        """A failed reservation cannot strand the action or replay its old claim."""

        with tempfile.TemporaryDirectory() as temporary:
            repository = ManualPermissionRepository(Path(temporary), "run-one")
            repository.reset_startup()
            repository.write_unlocked_grant(
                action="downgrade", grant_id="grant-1", board_id="controller", policy_digest="old"
            )
            token = repository.issue_downgrade_token("controller", "grant-1", "old")
            with self.assertRaisesRegex(RuntimeError, "stale-policy"):
                repository.consume_downgrade("controller", token, "new")
            self.assertEqual(repository.status("downgrade").state, "locked")
            repository.write_unlocked_grant(
                action="downgrade", grant_id="grant-2", board_id="controller", policy_digest="new"
            )

            repository.write_unlocked_grant(
                action="mass-erase",
                grant_id="mass-1",
                board_id="controller",
                policy_digest="new",
                binding_digest="binding",
            )
            claim = repository.reserve_mass_erase("controller", "mass-1", "new", "binding")
            repository.abandon_mass_erase("controller", claim)
            self.assertEqual(repository.status("mass-erase").state, "locked")
            with self.assertRaisesRegex(RuntimeError, "locked"):
                repository.consume_mass_erase("controller", "new", "binding", claim)
            repository.write_unlocked_grant(
                action="mass-erase",
                grant_id="mass-2",
                board_id="controller",
                policy_digest="new",
                binding_digest="binding",
            )


if __name__ == "__main__":
    unittest.main()
