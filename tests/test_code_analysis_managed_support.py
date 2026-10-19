"""Native controls for the test observer's retained child identity."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
import uuid
from unittest import mock

from tests.code_analysis_managed_support import (
    EVIDENCE,
    OwnedSpawns,
    native_start_token,
)


@unittest.skipUnless(os.name == "nt", "Windows retained process handle")
class WindowsOwnedObserverTests(unittest.TestCase):
    def test_terminal_child_keeps_original_birth_identity_and_requires_marker_cleanup(self):
        directory = EVIDENCE / "observer" / uuid.uuid4().hex[:6]
        observed = OwnedSpawns(directory)
        original = observed.original
        owned = []

        def finish_before_observation(*args, **kwargs):
            process, marker = original(*args, **kwargs)
            owned.append((process, marker))
            process.wait(timeout=5)
            return process, marker

        with observed, mock.patch.object(observed, "original", finish_before_observation):
            try:
                process, marker = observed.spawn(
                    [sys.executable, "-c", "raise SystemExit(0)"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                record = observed.children[-1][2]
                self.assertEqual(process.returncode, 0)
                self.assertIsNone(native_start_token(process.pid))
                self.assertEqual(record["native_creation"], record["marker"]["start_token"])
                self.assertIsNotNone(record["native_creation"])
                self.assertTrue(marker.is_file())
                # An exited child still needs its owned Job and marker retired.
                with self.assertRaises(AssertionError):
                    observed.check_cleanup()
            finally:
                for child, child_marker in owned:
                    self.assertTrue(observed.processes.terminate_process_group(child))
                    observed.store.remove(child_marker)
            observed.check_cleanup()


if __name__ == "__main__":
    unittest.main()
