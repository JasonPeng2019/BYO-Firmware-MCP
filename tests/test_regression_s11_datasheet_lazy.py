"""Regression coverage for completion paths of lazy datasheet evidence parsing."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pyocd_debug_mcp.setup_flow.datasheet_evidence import prove_datasheet_applicability


class _Page:
    def __init__(self, text: str | None, calls: list[int], index: int):
        self._text = text
        self._calls = calls
        self._index = index

    def extract_text(self) -> str | None:
        self._calls.append(self._index)
        return self._text


class _Reader:
    def __init__(self, metadata: dict[str, object] | None, pages: list[_Page]):
        self.metadata = metadata
        self.pages = pages


class DatasheetLazyRegressionTests(unittest.TestCase):
    """Ensure lazy traversal remains complete whenever the proof requires it."""

    def _prove(self, reader: _Reader):
        with patch("pypdf.PdfReader", return_value=reader):
            return prove_datasheet_applicability(
                b"%PDF-regression-evidence",
                requested_part="GENERIC-17",
                authority_terms=("GENERIC-17",),
            )

    def test_final_page_proof_reads_each_required_page_once(self) -> None:
        calls: list[int] = []
        proof = self._prove(
            _Reader(
                {"title": "unrelated"},
                [
                    _Page("other device", calls, 1),
                    _Page("GENERIC-17", calls, 2),
                ],
            )
        )

        self.assertEqual(calls, [1, 2])
        self.assertEqual(proof.evidence_locus, "page:2")
        self.assertEqual(proof.matched_term, "generic17")

    def test_none_metadata_and_page_text_are_empty_evidence_before_later_proof(self) -> None:
        calls: list[int] = []
        proof = self._prove(
            _Reader(
                None,
                [
                    _Page(None, calls, 1),
                    _Page("GENERIC-17", calls, 2),
                ],
            )
        )

        self.assertEqual(calls, [1, 2])
        self.assertEqual(proof.evidence_locus, "page:2")


if __name__ == "__main__":
    unittest.main()
