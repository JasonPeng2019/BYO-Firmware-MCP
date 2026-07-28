"""Adversarial specifications for lazy generic datasheet applicability evidence."""

from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pyocd_debug_mcp.setup_flow.datasheet_evidence import (
    DatasheetApplicabilityError,
    prove_datasheet_applicability,
)


class _Page:
    def __init__(self, name: str, text: str | None, calls: list[str], error: Exception | None = None):
        self.name = name
        self.text = text
        self.calls = calls
        self.error = error

    def extract_text(self) -> str | None:
        self.calls.append(self.name)
        if self.error is not None:
            raise self.error
        return self.text


class _Reader:
    def __init__(
        self,
        metadata: dict[str, object] | None,
        pages: object,
        *,
        metadata_error: Exception | None = None,
    ):
        self._metadata = metadata
        self._pages = pages
        self._metadata_error = metadata_error

    @property
    def metadata(self) -> dict[str, object] | None:
        if self._metadata_error is not None:
            raise self._metadata_error
        return self._metadata

    @property
    def pages(self) -> object:
        return self._pages


class _ExplodingPages:
    def __init__(self, error: Exception):
        self.error = error

    def __iter__(self):
        raise self.error


class LazyDatasheetEvidenceSpecTests(unittest.TestCase):
    """CL-001: only evidence needed for the first valid authority proof is parsed."""

    payload = b"%PDF-lazy-spec-payload"

    def _prove(self, reader: _Reader):
        with (
            patch("pypdf.PdfReader", return_value=reader),
            patch("pypdf.__version__", "spec-version"),
        ):
            return prove_datasheet_applicability(
                self.payload,
                requested_part="ACME-42",
                authority_terms=("ACME-42", "ACME FAMILY"),
            )

    def test_metadata_proof_preserves_fields_term_order_and_never_extracts_pages(self) -> None:
        calls: list[str] = []
        reader = _Reader(
            {"subject": "ACME FAMILY official reference"},
            [_Page("later", None, calls, RuntimeError("must not be extracted"))],
        )

        proof = self._prove(reader)

        self.assertEqual(calls, [])
        self.assertEqual(proof.requested_identity, "acme42")
        self.assertEqual(proof.matched_term, "acmefamily")
        self.assertEqual(proof.evidence_locus, "metadata")
        self.assertEqual(proof.pdf_sha256, hashlib.sha256(self.payload).hexdigest())
        self.assertEqual(proof.parser_version, "pypdf-spec-version")

    def test_first_page_proof_does_not_touch_a_later_failing_page(self) -> None:
        calls: list[str] = []
        reader = _Reader(
            None,
            [
                _Page("page-1", "ACME-42", calls),
                _Page("page-2", None, calls, RuntimeError("later page must remain unread")),
            ],
        )

        proof = self._prove(reader)

        self.assertEqual(proof.evidence_locus, "page:1")
        self.assertEqual(calls, ["page-1"])

    def test_later_match_extracts_each_preceding_page_once_in_order_then_stops(self) -> None:
        calls: list[str] = []
        reader = _Reader(
            {},
            [
                _Page("page-1", None, calls),
                _Page("page-2", "unrelated", calls),
                _Page("page-3", "ACME-42", calls),
                _Page("page-4", None, calls, RuntimeError("must not be extracted")),
            ],
        )

        proof = self._prove(reader)

        self.assertEqual(proof.evidence_locus, "page:3")
        self.assertEqual(calls, ["page-1", "page-2", "page-3"])

    def test_final_page_proof_extracts_every_page_once_in_order(self) -> None:
        calls: list[str] = []
        reader = _Reader(
            {},
            [
                _Page("page-1", None, calls),
                _Page("page-2", "unrelated", calls),
                _Page("page-3", "ACME-42", calls),
            ],
        )

        proof = self._prove(reader)

        self.assertEqual(proof.evidence_locus, "page:3")
        self.assertEqual(calls, ["page-1", "page-2", "page-3"])

    def test_no_match_extracts_every_page_once_and_keeps_actionable_refusal(self) -> None:
        calls: list[str] = []
        reader = _Reader(
            None,
            [
                _Page("page-1", None, calls),
                _Page("page-2", "another part", calls),
                _Page("page-3", "still unrelated", calls),
            ],
        )

        with self.assertRaisesRegex(
            DatasheetApplicabilityError,
            "ACME-42 was not established; provide verifiable official datasheet evidence",
        ):
            self._prove(reader)
        self.assertEqual(calls, ["page-1", "page-2", "page-3"])

    def test_required_page_failure_is_typed_unreadable_error_with_original_cause(self) -> None:
        calls: list[str] = []
        failure = RuntimeError("broken required page")
        reader = _Reader(None, [_Page("page-1", "unrelated", calls), _Page("page-2", None, calls, failure)])

        with self.assertRaisesRegex(
            DatasheetApplicabilityError,
            "datasheet PDF could not be read for MCU applicability; provide verifiable official "
            "datasheet evidence for ACME-42",
        ) as raised:
            self._prove(reader)
        self.assertIs(raised.exception.__cause__, failure)
        self.assertEqual(calls, ["page-1", "page-2"])

    def test_required_metadata_and_page_iteration_failures_keep_their_causes(self) -> None:
        for reader, failure in (
            (_Reader(None, [], metadata_error=ValueError("metadata failed")), ValueError("metadata failed")),
            (_Reader(None, _ExplodingPages(ValueError("page iteration failed"))), ValueError("page iteration failed")),
        ):
            with self.subTest(failure=failure), self.assertRaisesRegex(
                DatasheetApplicabilityError, "datasheet PDF could not be read for MCU applicability"
            ) as raised:
                self._prove(reader)
            self.assertEqual(str(raised.exception.__cause__), str(failure))

    def test_reader_construction_failure_is_typed_and_retains_its_cause(self) -> None:
        failure = RuntimeError("reader construction failed")

        with (
            patch("pypdf.PdfReader", side_effect=failure),
            self.assertRaisesRegex(
                DatasheetApplicabilityError,
                "datasheet PDF could not be read for MCU applicability; provide verifiable official "
                "datasheet evidence for ACME-42",
            ) as raised,
        ):
            prove_datasheet_applicability(
                self.payload,
                requested_part="ACME-42",
                authority_terms=("ACME-42",),
            )
        self.assertIs(raised.exception.__cause__, failure)
