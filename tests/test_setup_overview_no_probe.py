"""Step 0 precursor fixes: the no-probe status and the single connection_id mint."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

from pyocd_debug_mcp import server
from pyocd_debug_mcp.hardware_inventory import snapshot_from_validation_inventory
from pyocd_debug_mcp.probe_inventory import registered_provider_ids

import pytest


def setUpModule() -> None:
    required = {"cmsisdap", "picoprobe"}
    actual = set(registered_provider_ids())
    if not required <= actual:
        raise RuntimeError(f"Real pyOCD registry lacks fixture prerequisites: {required - actual}")


class _OverviewHarness:
    """Drive `_setup_overview` with a synthetic inventory and no profile store.

    Patches the inventory *service*, not just `_validation_inventory`. There is real
    debug hardware on the developer bench this suite runs on, so a harness that only
    patched the legacy shape would still take a live snapshot and report real probes in
    the no-probe payload's diagnostics.
    """

    def __init__(self, probes: tuple[SimpleNamespace, ...], serial: tuple[SimpleNamespace, ...]):
        self.inventory = SimpleNamespace(probes=probes, serial_ports=serial)
        self.snapshot = snapshot_from_validation_inventory(
            cast(Any, self.inventory),
        )
        self.replace = Mock()

    def run(self, names: list[str] | None, assignments: dict[str, str] | None = None):
        service = SimpleNamespace(
            snapshot=lambda: self.snapshot,
            validation_inventory=lambda: self.inventory,
        )
        with (
            patch.object(server, "_profile_repository", SimpleNamespace(load_all=lambda: ())),
            patch.object(server, "_hardware_inventory", service),
            patch.object(server, "_validation_inventory", return_value=self.inventory),
            patch.object(server, "_replace_setup_assignments", self.replace),
        ):
            return server._setup_overview(names, assignments)


class SetupOverviewNoProbeTests(unittest.TestCase):
    @pytest.mark.mvp
    def test_one_name_zero_connections_reports_missing_probe_not_naming_ambiguity(self) -> None:
        harness = _OverviewHarness((), ())

        overview = harness.run(["Nucleo"])

        self.assertEqual(overview["status"], "setup_no_probe")
        self.assertNotEqual(overview["status"], "setup_assignment_clarification_required")
        self.assertEqual(overview["routes"], [])
        self.assertEqual(overview["connections"], [])
        prompt = str(overview["agent_prompt"])
        self.assertIn("No debug probe is visible to the server", prompt)
        self.assertIn("not a board-naming ambiguity", prompt)

    def test_zero_names_and_zero_connections_still_reports_no_board(self) -> None:
        harness = _OverviewHarness((), ())

        overview = harness.run([])

        self.assertEqual(overview["status"], "setup_no_board")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
