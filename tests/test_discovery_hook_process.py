"""Step 1: capped, owned hook execution -- deadlines, caps, descriptors, descendants.

Every test here runs a real child process. The point of the fake hook fixture is that
process ownership, deadlines, output capping, and descriptor handling are genuinely
exercised; mocking them would test nothing that matters.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

import psutil

from pyocd_debug_mcp import discovery_hooks
from pyocd_debug_mcp.discovery_hooks import execute_hook
from pyocd_debug_mcp.kernel.processes import ProcessMarkerStore
from tests.discovery_hook_fixtures import single_spec


class _HookProcessCase(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self._markers = tempfile.TemporaryDirectory()
        self.addCleanup(self._markers.cleanup)
        self.marker_store = ProcessMarkerStore(Path(self._markers.name))


    def spec(
        self,
        mode: str,
        *extra: str,
        kind: str = "probe",
        timeout_seconds: float = 15.0,
    ) -> discovery_hooks.DiscoveryHookSpec:
        return single_spec(self.root, kind, [mode, *extra], timeout_seconds=timeout_seconds)

    def run_hook(self, *args: str, **kwargs: object) -> discovery_hooks.HookExecution:
        spec = self.spec(*args, **kwargs)  # type: ignore[arg-type]
        return execute_hook(spec, marker_store=self.marker_store)


class DeadlineTests(_HookProcessCase):

    def test_timeout_kills_descendants_not_only_the_leader(self) -> None:
        pid_file = self.root / "descendant.pid"

        execution = self.run_hook("spawn_child", str(pid_file), timeout_seconds=2.0)

        self.assertEqual(execution.outcome, "timeout")
        descendant = int(pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if not self._alive(descendant):
                break
            time.sleep(0.05)
        self.assertFalse(
            self._alive(descendant),
            f"descendant {descendant} survived the owned-group termination",
        )

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            process = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return False
        try:
            return process.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False

    def test_a_hook_reading_stdin_gets_eof_instead_of_hanging(self) -> None:
        started = time.monotonic()

        execution = self.run_hook("read_stdin", timeout_seconds=10.0)

        self.assertTrue(execution.ok, execution.failure_detail)
        assert execution.output is not None
        self.assertEqual(execution.output.probes[0].description, "stdin len 0")
        self.assertLess(time.monotonic() - started, 10.0)


class FailureClassTests(_HookProcessCase):

    def test_malformed_json_is_parse_failed_not_hook_failed(self) -> None:
        execution = self.run_hook("bad_json")

        self.assertEqual(execution.outcome, "parse_failed")
        self.assertEqual(execution.failure_code, "discovery/hook-output-invalid")
        self.assertIn("JSON", execution.failure_detail)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
