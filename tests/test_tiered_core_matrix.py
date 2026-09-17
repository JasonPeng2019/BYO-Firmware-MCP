"""Tier route inventory must remain in lockstep with public registration."""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pyocd_debug_mcp.capabilities.matrix import (
    ROUTE_BY_NAME,
    applicable_route_names,
    route_names,
)


class TierRouteMatrixTests(unittest.TestCase):
    def test_raw_and_safe_pairs_are_single_source(self) -> None:
        for name in route_names("raw"):
            route = ROUTE_BY_NAME[name]
            self.assertEqual(route.raw_tool, name)
            self.assertIsNotNone(route.safe_tool)
            self.assertIn(route.safe_tool, ROUTE_BY_NAME)
        for name in route_names("safe"):
            route = ROUTE_BY_NAME[name]
            self.assertTrue(route.requires_plan)
            if route.safe_tool is not None:
                self.assertEqual(route.safe_tool, name)

    def test_inventory_covers_every_registered_public_tool(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                os.environ,
                {"BYO_MCP_ARTIFACT_ROOT": str(Path(temporary)), "BYO_MCP_TEST_SEAMS": "1"},
                clear=False,
            ),
        ):
            sys.modules.pop("pyocd_debug_mcp.server", None)
            server = importlib.import_module("pyocd_debug_mcp.server")
            registered = set(server.mcp._tool_manager._tools)
        self.assertSetEqual(set(applicable_route_names(narrative_logging=True)), registered)

    def test_professional_profile_omits_only_personal_matrix_rows(self) -> None:
        import pyocd_debug_mcp.monitor.tools as monitor_tools

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                os.environ,
                {"BYO_MCP_ARTIFACT_ROOT": str(Path(temporary)), "BYO_MCP_TEST_SEAMS": "1"},
                clear=False,
            ),
            patch.object(monitor_tools, "NARRATIVE_LOGGING", False),
        ):
            sys.modules.pop("pyocd_debug_mcp.server", None)
            server = importlib.import_module("pyocd_debug_mcp.server")
            registered = set(server.mcp._tool_manager._tools)
        self.assertSetEqual(set(applicable_route_names(narrative_logging=False)), registered)


if __name__ == "__main__":
    unittest.main()
