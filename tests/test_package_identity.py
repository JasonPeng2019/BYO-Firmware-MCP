"""Negative and positive public-identity checks for the 0.2.0 package migration."""

from __future__ import annotations

import importlib
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch


class PackageIdentityTests(unittest.TestCase):
    """Only this negative test may mention the removed public identities literally."""

    def test_current_package_imports_and_removed_package_does_not(self) -> None:
        self.assertIsNotNone(importlib.import_module("firmware_mcp.server"))
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("pyocd_debug_mcp")

    def test_removed_console_identity_is_not_declared_by_project_metadata(self) -> None:
        metadata = importlib.import_module("importlib.metadata")
        distribution = metadata.distribution("byo-firmware-mcp")
        entry_points = {
            entry.name for entry in distribution.entry_points if entry.group == "console_scripts"
        }
        self.assertIn("byo-firmware-mcp", entry_points)
        self.assertNotIn("pyocd-debug-mcp", entry_points)

    def test_current_console_help_is_explicit_and_does_not_start_stdio(self) -> None:
        server = importlib.import_module("firmware_mcp.server")
        output = StringIO()
        with (
            patch.object(server.sys, "argv", ["byo-firmware-mcp", "--help"]),
            patch.object(server, "require_clean_startup") as startup,
            patch.object(server.mcp, "run") as run,
            redirect_stdout(output),
        ):
            server.main()
        self.assertIn("Provider-neutral firmware MCP server", output.getvalue())
        startup.assert_not_called()
        run.assert_not_called()
