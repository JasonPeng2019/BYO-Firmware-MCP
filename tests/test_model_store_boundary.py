"""Deterministic controls for local model preparation; zero model requests."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest import mock

from tests import codex_harness, store_cleanup


class ModelStoreBoundary(unittest.TestCase):
    def test_supported_scoped_store_is_shared_by_child_assertions_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "monitor"
            with mock.patch.dict(os.environ, {"BYO_MCP_MONITOR_ROOT": str(root)}):
                self.assertEqual(store_cleanup.store_root(), root)
                self.assertEqual(codex_harness._store_root(), root)
                overrides = codex_harness.codex_overrides()
                pair = next(p for p in overrides if ".env.BYO_MCP_MONITOR_ROOT=" in p)
                self.assertEqual(
                    tomllib.loads("value=" + pair.split("=", 1)[1])["value"], str(root)
                )
                self.assertFalse(root.exists(), "metadata selection must not create the store")
                before = store_cleanup.snapshot()
                self.assertIsNone(before)
                root.mkdir()
                (root / "new-record").write_bytes(b"owned run")
                store_cleanup.restore(before)
                self.assertFalse(root.exists())

    def test_scoped_store_restoration_keeps_prior_bytes_and_unrelated_store(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "monitor"
            shared = base / "unrelated-shared-store"
            shared.mkdir()
            sentinel = shared / "prior"
            sentinel.write_bytes(b"unrelated prior bytes")
            root.mkdir()
            (root / "old").write_bytes(b"old bytes")
            with mock.patch.dict(os.environ, {"BYO_MCP_MONITOR_ROOT": str(root)}):
                before = store_cleanup.snapshot()
                (root / "old").write_bytes(b"changed")
                (root / "new").write_bytes(b"new")
                store_cleanup.restore(before)
            self.assertEqual((root / "old").read_bytes(), b"old bytes")
            self.assertFalse((root / "new").exists())
            self.assertEqual(sentinel.read_bytes(), b"unrelated prior bytes")

    def test_relative_monitor_root_is_refused_before_store_io(self):
        with mock.patch.dict(os.environ, {"BYO_MCP_MONITOR_ROOT": "relative-monitor"}):
            with self.assertRaisesRegex(ValueError, "absolute"):
                store_cleanup.snapshot()


if __name__ == "__main__":
    unittest.main()
