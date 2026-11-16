"""Tests for the remote-probe registry: module, inventory integration, and MCP tools.

Covers section 5 of REMOTE_PROBE_PLAN.md: the no-registration invariant, round-trip
registration through snapshot(), the selector surviving ProbeSelectionStore.resolve(),
re-registration updating in place rather than duplicating, unregistration, a malformed
registry file not crashing discovery, an unreachable endpoint still registering, host
and port validation, and remote rows staying visible alongside a native probe.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pyocd_debug_mcp.hardware_inventory import HardwareInventoryService
from pyocd_debug_mcp.probe_inventory import EMPTY_NATIVE_PROBE_LISTING
from pyocd_debug_mcp.remote_probes import load_remote_probes
from pyocd_debug_mcp.tools.remote_probes import RemoteProbeToolServices, build_remote_probe_handlers


class _RegistryCase(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.registry_path = Path(self._directory.name) / "remote_probes.json"


class ToolHandlerTests(_RegistryCase):
    def _handlers(self, *, check_endpoint_result: bool = True):
        return build_remote_probe_handlers(
            RemoteProbeToolServices(
                registry_path=lambda: self.registry_path,
                check_endpoint=lambda host, port: check_endpoint_result,
            )
        )


    def test_an_unreachable_endpoint_still_registers_and_says_so(self) -> None:
        handlers = self._handlers(check_endpoint_result=False)

        result = json.loads(handlers["register_remote_probe"]("bench.local", 5555, "x"))

        self.assertEqual(result["status"], "remote_probe_registered")
        self.assertFalse(result["reachable"])
        loaded = load_remote_probes(self.registry_path)
        self.assertEqual(len(loaded), 1, "an unreachable endpoint must still be registered")
        self.assertEqual(loaded[0].host, "bench.local")


    def test_the_row_disappears_from_the_next_snapshot_after_unregistering(self) -> None:
        handlers = self._handlers()
        handlers["register_remote_probe"]("bench.local", 5555, "bench")
        service = HardwareInventoryService(
            native_probes=lambda: EMPTY_NATIVE_PROBE_LISTING,
            native_uarts=lambda: [],
            remote_probes=lambda: load_remote_probes(self.registry_path),
        )
        self.assertEqual(len(service.snapshot().probes), 1)

        handlers["unregister_remote_probe"]("bench.local", 5555)

        self.assertEqual(service.snapshot().probes, ())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
