"""Step 1: manifest loading, strict validation, path containment, and hash drift."""

from __future__ import annotations

import tempfile
import unittest
import unittest.mock
from pathlib import Path

from pyocd_debug_mcp.discovery_hooks import DiscoveryHookError, parse_manifest_document


class _TempRoot(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)


class DocumentValidationTests(_TempRoot):
    def _expect_document_refusal(self, document: object, fragment: str) -> None:
        with self.assertRaises(DiscoveryHookError) as caught:
            parse_manifest_document(document)
        self.assertIn(fragment, str(caught.exception))


    def test_unknown_manifest_field_is_refused(self) -> None:
        self._expect_document_refusal(
            {"schema_version": 1, "hooks": [], "executable": "evil"},
            "unknown field(s): executable",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
