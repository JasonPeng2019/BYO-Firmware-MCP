"""The packaged manual-permission helper preserves the server protocol."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pyocd_debug_mcp.capabilities import manual_permission_helper


class ManualPermissionHelperRuntimeTests(unittest.TestCase):
    def test_unlocks_only_a_current_locked_downgrade_record(self) -> None:
        with tempfile.TemporaryDirectory() as raw_project:
            project = Path(raw_project)
            root = project / ".agent-workspace" / "runtime" / "manual-permissions"
            root.mkdir(parents=True)
            (root / "epoch.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "server_run_id": "server-run-1",
                        "created_at": "2026-09-17T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            for action in ("downgrade", "mass-erase"):
                (root / f"{action}.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "action": action,
                            "state": "locked",
                            "server_run_id": "server-run-1",
                            "grant_id": None,
                            "board_id": None,
                            "policy_digest": None,
                            "binding_digest": None,
                            "claim_id": None,
                            "created_at": "2026-09-17T00:00:00Z",
                        }
                    ),
                    encoding="utf-8",
                )

            result = manual_permission_helper.unlock_manual_grant(
                project_root=project,
                action="downgrade",
                board_id="board-1",
                policy_digest="a" * 64,
            )

            self.assertEqual(result["status"], "manual_grant_unlocked")
            self.assertEqual(result["action"], "downgrade")
            self.assertEqual(result["board_id"], "board-1")
            self.assertEqual(result["policy_digest"], "a" * 64)
            record = json.loads((root / "downgrade.json").read_text(encoding="utf-8"))
            self.assertEqual(record["state"], "unlocked")
            self.assertEqual(record["server_run_id"], "server-run-1")
            self.assertEqual(record["claim_id"], None)

    def test_partial_claim_write_still_releases_a_valid_claim(self) -> None:
        with tempfile.TemporaryDirectory() as raw_project:
            project = Path(raw_project)
            root = project / ".agent-workspace" / "runtime" / "manual-permissions"
            root.mkdir(parents=True)
            (root / "epoch.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "server_run_id": "server-run-1",
                        "created_at": "2026-09-17T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            for action in ("downgrade", "mass-erase"):
                (root / f"{action}.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "action": action,
                            "state": "locked",
                            "server_run_id": "server-run-1",
                            "grant_id": None,
                            "board_id": None,
                            "policy_digest": None,
                            "binding_digest": None,
                            "claim_id": None,
                            "created_at": "2026-09-17T00:00:00Z",
                        }
                    ),
                    encoding="utf-8",
                )
            original_write = os.write

            def short_write(descriptor: int, payload: bytes) -> int:
                chunk = payload[: max(1, len(payload) // 2)]
                return original_write(descriptor, chunk)

            with patch.object(manual_permission_helper.os, "write", side_effect=short_write):
                result = manual_permission_helper.unlock_manual_grant(
                    project_root=project,
                    action="downgrade",
                    board_id="board-1",
                    policy_digest="a" * 64,
                )

            self.assertEqual(result["status"], "manual_grant_unlocked")
            self.assertFalse((root / "downgrade.claim").exists())


if __name__ == "__main__":
    unittest.main()
