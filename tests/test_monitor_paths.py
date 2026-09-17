"""Monitor-store launch-time selection contracts."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pyocd_debug_mcp.monitor import paths


class MonitorStoreRootTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="byo-monitor-paths-")
        self.root = Path(self._temporary.name)
        paths._reset_cache(None)
        self.addCleanup(paths._reset_cache)
        self.addCleanup(self._temporary.cleanup)

    def test_explicit_monitor_root_precedes_app_data_and_artifact_root_and_holds_writes(
        self,
    ) -> None:
        explicit = self.root / "isolated-monitor"
        app_data = self.root / "app-data"
        artifact = self.root / "artifact-root"
        environment = {
            "BYO_MCP_MONITOR_ROOT": str(explicit),
            "BYO_MCP_ARTIFACT_ROOT": str(artifact),
        }

        with (
            patch.dict(os.environ, environment, clear=False),
            patch.object(paths, "_app_data_candidate", return_value=app_data),
        ):
            store = paths.resolve_store_root()
            token = paths.workspace_token(store, "workspace")

        self.assertEqual(store.state, paths.StoreState.MONITOR_ROOT)
        self.assertEqual(store.root, explicit.resolve())
        self.assertTrue((explicit / paths.SERVER_DATA / "workspace" / paths.TOKEN_FILE).is_file())
        self.assertTrue(token)
        self.assertFalse(app_data.exists())
        self.assertFalse((artifact / paths.OPERATOR_SUBDIR).exists())

    def test_explicit_monitor_root_is_buffering_only_when_unusable(self) -> None:
        unusable = self.root / "not-a-directory"
        unusable.write_text("not a monitor directory", encoding="utf-8")
        app_data = self.root / "would-be-app-data"
        artifact = self.root / "would-be-artifact-root"
        environment = {
            "BYO_MCP_MONITOR_ROOT": str(unusable),
            "BYO_MCP_ARTIFACT_ROOT": str(artifact),
        }

        with (
            patch.dict(os.environ, environment, clear=False),
            patch.object(paths, "_app_data_candidate", return_value=app_data),
        ):
            store = paths.resolve_store_root()

        self.assertEqual(store.state, paths.StoreState.BUFFERING)
        self.assertIsNone(store.root)
        self.assertFalse(app_data.exists())
        self.assertFalse((artifact / paths.OPERATOR_SUBDIR).exists())

    def test_explicit_monitor_root_resolution_failure_is_buffering_only(self) -> None:
        app_data = self.root / "would-be-app-data"
        artifact = self.root / "would-be-artifact-root"
        with (
            patch.dict(
                os.environ,
                {
                    "BYO_MCP_MONITOR_ROOT": "unresolvable-monitor-root",
                    "BYO_MCP_ARTIFACT_ROOT": str(artifact),
                },
                clear=False,
            ),
            patch.object(paths, "_monitor_root_candidate", return_value=None),
            patch.object(paths, "_app_data_candidate", return_value=app_data),
        ):
            store = paths.resolve_store_root()

        self.assertEqual(store.state, paths.StoreState.BUFFERING)
        self.assertIsNone(store.root)
        self.assertFalse(app_data.exists())
        self.assertFalse((artifact / paths.OPERATOR_SUBDIR).exists())

    def test_blank_or_unset_monitor_root_keeps_app_data_first_behavior(self) -> None:
        for configured in (None, "   "):
            with self.subTest(configured=configured):
                paths._reset_cache(None)
                app_data = self.root / f"app-data-{configured is None}"
                artifact = self.root / f"artifact-{configured is None}"
                environment = {"BYO_MCP_ARTIFACT_ROOT": str(artifact)}
                if configured is not None:
                    environment["BYO_MCP_MONITOR_ROOT"] = configured
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch.object(paths, "_app_data_candidate", return_value=app_data),
                ):
                    store = paths.resolve_store_root()

                self.assertEqual(store.state, paths.StoreState.APP_DATA)
                self.assertEqual(store.root, app_data)
                self.assertFalse((artifact / paths.OPERATOR_SUBDIR).exists())

    def test_artifact_root_remains_the_baseline_fallback_without_monitor_root(self) -> None:
        artifact = self.root / "artifact-root"
        with (
            patch.dict(
                os.environ,
                {"BYO_MCP_ARTIFACT_ROOT": str(artifact)},
                clear=True,
            ),
            patch.object(paths, "_app_data_candidate", return_value=None),
        ):
            store = paths.resolve_store_root()

        self.assertEqual(store.state, paths.StoreState.OPERATOR_ROOT)
        self.assertEqual(store.root, artifact.resolve() / paths.OPERATOR_SUBDIR)

    def test_test_override_still_precedes_explicit_monitor_root(self) -> None:
        override = self.root / "test-override"
        explicit = self.root / "isolated-monitor"
        paths._reset_cache(override)
        with patch.dict(os.environ, {"BYO_MCP_MONITOR_ROOT": str(explicit)}, clear=False):
            store = paths.resolve_store_root()

        self.assertEqual(store.root, override)
        self.assertFalse(explicit.exists())


if __name__ == "__main__":
    unittest.main()
