"""Monitor-store launch-time selection contracts."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
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

    def test_identity_persists_without_optional_fchmod(self) -> None:
        paths._reset_cache(self.root)
        with patch.object(paths.os, "fchmod", None, create=True):
            salt = paths.deployment_salt()
            store = paths.resolve_store_root()
            token = paths.workspace_token(store, "workspace")
            self.assertEqual((self.root / paths.SALT_FILE).read_bytes(), salt)
            self.assertEqual(paths.read_workspace_token(store, "workspace"), token)
            paths._reset_cache(self.root)
            self.assertEqual(paths.deployment_salt(), salt)
            self.assertEqual(paths.workspace_token(store, "workspace"), token)

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

        token_path = explicit / paths.SERVER_DATA / "workspace" / paths.TOKEN_FILE
        token_path.write_bytes(b"\xff")
        self.assertIsNone(paths.read_workspace_token(store, "workspace"))
        replacement = paths.workspace_token(store, "workspace")
        self.assertEqual(paths.read_workspace_token(store, "workspace"), replacement)

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


class MonitorIdentityRaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="byo-monitor-identity-race-")
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)

    def _race(self, expression: str, *, workers: int = 12) -> list[str]:
        shared = self.root / "shared"
        rendezvous = self.root / "rendezvous"
        rendezvous.mkdir()
        start = rendezvous / "start"
        script = ";".join(
            (
                "import os,sys,time",
                "from pathlib import Path",
                "sys.path.insert(0, 'src')",
                "from pyocd_debug_mcp.monitor import paths",
                f"root=Path({str(shared)!r})",
                f"rendezvous=Path({str(rendezvous)!r})",
                "paths._reset_cache(root)",
                "(rendezvous / f'ready-{os.getpid()}').write_text('')",
                f"start=Path({str(start)!r})",
                'exec("while not start.exists():\\n time.sleep(0.001)")',
                f"print({expression}, flush=True)",
            )
        )
        repository = Path(__file__).resolve().parents[1]
        children = [
            subprocess.Popen(
                [sys.executable, "-c", script],
                cwd=repository,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(workers)
        ]
        try:
            deadline = time.monotonic() + 20.0
            while len(list(rendezvous.glob("ready-*"))) != workers:
                if time.monotonic() >= deadline:
                    self.fail("identity-race workers did not reach the start barrier")
                time.sleep(0.01)
            start.write_text("go", encoding="ascii")
            outputs: list[str] = []
            for child in children:
                stdout, stderr = child.communicate(timeout=30)
                self.assertEqual(child.returncode, 0, stderr)
                outputs.append(stdout.strip())
            return outputs
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)

    def test_deployment_salt_first_use_is_one_cross_process_identity(self) -> None:
        outputs = self._race("paths.deployment_salt().hex()")
        self.assertEqual(len(set(outputs)), 1)
        salt = self.root / "shared" / paths.SALT_FILE
        self.assertEqual(salt.read_bytes().hex(), outputs[0])
        if os.name == "posix":
            self.assertEqual(salt.stat().st_mode & 0o777, 0o600)

    def test_workspace_token_first_use_is_one_cross_process_identity(self) -> None:
        outputs = self._race("paths.workspace_token(paths.resolve_store_root(), 'workspace')")
        self.assertEqual(len(set(outputs)), 1)
        token = self.root / "shared" / paths.SERVER_DATA / "workspace" / paths.TOKEN_FILE
        self.assertEqual(token.read_text(encoding="ascii"), outputs[0])
        if os.name == "posix":
            self.assertEqual(token.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
