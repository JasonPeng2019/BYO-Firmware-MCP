from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pyocd_debug_mcp import sidecar


class SidecarConfigurationTests(unittest.TestCase):
    def test_project_root_requires_absolute_existing_directory(self) -> None:
        with self.assertRaises(sidecar.ConfigurationError):
            sidecar.validate_project_root("relative")

    def test_project_root_rejects_home_and_filesystem_root(self) -> None:
        with self.assertRaises(sidecar.ConfigurationError):
            sidecar.validate_project_root(str(Path.home()))
        with self.assertRaises(sidecar.ConfigurationError):
            sidecar.validate_project_root(Path.cwd().anchor)

    def test_configure_discards_untrusted_ambient_authority(self) -> None:
        with patch.dict(
            os.environ,
            {
                "BYO_MCP_ARTIFACT_ROOT": "/untrusted",
                "BYO_RUNTIME_ROOT": "/untrusted",
                "BYO_SIDECAR_EXECUTABLE": "/untrusted",
                "BYO_PROVIDER_WORKER_ARGV": '["/untrusted"]',
                "PYOCD_MCP_RUNS_ROOT": "/untrusted",
            },
            clear=False,
        ):
            sidecar._configure_serve_environment()
            for name in (
                "BYO_MCP_ARTIFACT_ROOT",
                "BYO_RUNTIME_ROOT",
                "BYO_SIDECAR_EXECUTABLE",
                "BYO_PROVIDER_WORKER_ARGV",
                "PYOCD_MCP_RUNS_ROOT",
            ):
                self.assertNotIn(name, os.environ)

    def test_running_sidecar_prefers_invoked_compiled_binary(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            binary = Path(raw_root) / "byo-mcp-sidecar"
            binary.write_bytes(b"test")
            missing_synthetic = Path(raw_root) / "python"
            with (
                patch.object(sidecar.sys, "argv", [str(binary)]),
                patch.object(sidecar.sys, "executable", str(missing_synthetic)),
            ):
                self.assertEqual(sidecar._running_sidecar(), binary.resolve())

    def test_self_test_reports_protocol_versions(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = sidecar.main(["self-test"])
        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "passed")
        self.assertEqual(payload["sidecar_protocol"], 1)
        self.assertEqual(payload["worker_protocol"], 1)
        self.assertEqual(payload["capsule_schema"], 1)
        self.assertFalse(payload["runtime_manifest_verified"])

    def test_serve_rejects_unsupported_workflow_protocol_before_import(self) -> None:
        with (
            tempfile.TemporaryDirectory() as raw_project,
            tempfile.TemporaryDirectory() as raw_runtime,
        ):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = sidecar.main(
                    [
                        "serve",
                        "--project-root",
                        raw_project,
                        "--runtime-root",
                        raw_runtime,
                        "--launcher-version",
                        "0.1.0",
                        "--workflow-protocol",
                        "99",
                    ]
                )
        self.assertEqual(result, 2)
        self.assertIn("unsupported", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
