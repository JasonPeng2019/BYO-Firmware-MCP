"""Exercise actual owned pipes/framing using a temporary scriptable LSP peer."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from pyocd_debug_mcp import code_analysis_runtime as runtime
from pyocd_debug_mcp.kernel.processes import ProcessMarkerStore


class TransportTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX receipt scope; Windows Jobs unchanged")

    def test_spawn_budget_and_late_child_cleanup_before_new_spawn(self):
        store = ProcessMarkerStore(self.root / "late-markers")
        original = runtime.popen_owned
        spawned = []

        def delayed(*args, **kwargs):
            if not spawned:
                time.sleep(0.08)
            result = original(*args, **kwargs)
            spawned.append(result[0])
            return result

        with patch.object(runtime, "popen_owned", side_effect=delayed):
            with self.assertRaises(TimeoutError):
                runtime.spawn_before_deadline(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    time.monotonic() + 0.02,
                    None,
                    store,
                )
            process, marker = runtime.spawn_before_deadline(
                [sys.executable, "-c", "pass"], time.monotonic() + 2, None, store
            )
            process.wait(timeout=2)
            from pyocd_debug_mcp.kernel.processes import terminate_process_group

            self.assertTrue(terminate_process_group(process))
            store.remove(marker)
        self.assertIsNotNone(spawned[0].poll())
        self.assertFalse(list((self.root / "late-markers").glob("*.json")))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="analysis spawn ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()


if __name__ == "__main__":
    unittest.main()
