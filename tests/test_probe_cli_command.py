import os
import sys
import unittest
from unittest.mock import patch

from pyocd_debug_mcp.probe_families import configured_probe_cli_commands


class ProbeCliCommandTests(unittest.TestCase):
    def test_default_inventory_uses_the_server_interpreter(self) -> None:
        with patch.dict(os.environ, {"PYOCD_CLI": ""}, clear=False):
            commands = configured_probe_cli_commands()

        self.assertEqual(commands[0], (sys.executable, "-m", "pyocd", "list", "--probes"))

    def test_explicit_operator_override_remains_supported(self) -> None:
        with patch.dict(os.environ, {"PYOCD_CLI": "C:/tools/pyocd.exe"}, clear=False):
            commands = configured_probe_cli_commands()

        self.assertEqual(commands[0], ("C:/tools/pyocd.exe", "list", "--probes"))
