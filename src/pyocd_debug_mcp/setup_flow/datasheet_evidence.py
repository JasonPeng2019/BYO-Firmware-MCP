"""Exact, immutable local datasheet evidence capture and replay."""

from __future__ import annotations

import hashlib
import io
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pyocd_debug_mcp.firmstore.store import FirmStore, ImmutableArtifactError

PARSER_VERSION = "pypdf"


class DatasheetEvidenceError(ValueError):
    """The local PDF cannot serve as deterministic setup evidence."""


class DatasheetApplicabilityError(DatasheetEvidenceError):
    """A readable PDF does not establish that it covers the requested MCU."""


class DatasheetIdentityTerm(str):
    """A server-derived identity term with its narrowly scoped document convention."""

    __slots__ = ("permits_family_placeholder",)

    def __new__(cls, text: str, permits_family_placeholder: bool = False):
        term = super().__new__(cls, text)
        term.permits_family_placeholder = permits_family_placeholder
        return term


@dataclass(frozen=True, slots=True)
class DatasheetEvidence:
    sha256: str
    reference: str
    parser_version: str = PARSER_VERSION

    def source_document(self) -> dict[str, str]:
        return {
            "sha256": self.sha256,
            "reference": self.reference,
            "parser_version": self.parser_version,
        }


@dataclass(frozen=True, slots=True)
class DatasheetApplicabilityProof:
    """Server-generated association between one PDF byte snapshot and MCU authority."""

    requested_identity: str
    matched_term: str
    evidence_locus: str
    pdf_sha256: str
    parser_version: str

    def to_document(self) -> dict[str, str]:
        return {
            "requested_identity": self.requested_identity,
            "matched_term": self.matched_term,
            "evidence_locus": self.evidence_locus,
            "pdf_sha256": self.pdf_sha256,
            "parser_version": self.parser_version,
        }


def normalize_identity_text(value: str) -> str:
    """Normalize a document identity without deriving a family from caller text."""

    return "".join(
        character.casefold()
        for character in unicodedata.normalize("NFKC", value)
        if character.isascii() and character.isalnum()
    )


def _identity_tokens(value: str) -> tuple[str, ...]:
    """Keep document token boundaries so one SKU cannot satisfy another's prefix."""

    normalized = unicodedata.normalize("NFKC", value)
    return tuple(match.group(0).casefold() for match in re.finditer(r"[A-Za-z0-9]+", normalized))


def _contains_identity_term(
    source: str, term: str, *, permits_family_placeholder: bool = False
) -> bool:
    """Match an exact contiguous or separator-delimited server-derived identity."""

    term_tokens = _identity_tokens(term)
    source_tokens = _identity_tokens(source)
    if not term_tokens:
        return False
    collapsed = normalize_identity_text(term)
    if collapsed in source_tokens:
        return True
    # Official family PDFs commonly use a trailing ``xx`` placeholder for a
    # verified PDSC family/subfamily terms. This is a token convention, not
    # a general prefix match: concrete suffixes such as ``...1440`` still fail.
    if permits_family_placeholder and any(
        re.fullmatch(re.escape(collapsed) + r"x+", token) is not None
        for token in source_tokens
    ):
        return True
    width = len(term_tokens)
    return any(source_tokens[index : index + width] == term_tokens for index in range(len(source_tokens)))


def prove_datasheet_applicability(
    payload: bytes,
    *,
    requested_part: str,
    authority_terms: tuple[str | DatasheetIdentityTerm, ...],
) -> DatasheetApplicabilityProof:
    """Require parser-extracted PDF text or metadata to name server-derived MCU authority.

    ``authority_terms`` must be the exact requested part plus exact terms replayed from
    verified support; callers must not infer a prefix or family from user text.
    """

    digest = hashlib.sha256(payload).hexdigest()
    requested = normalize_identity_text(requested_part)
    terms = tuple(
        (normalized, str(value), value.permits_family_placeholder)
        if isinstance(value, DatasheetIdentityTerm)
        else (normalized, value, False)
        for value in authority_terms
        if (normalized := normalize_identity_text(
            str(value) if isinstance(value, DatasheetIdentityTerm) else value
        ))
    )
    if not requested or requested not in (normalized for normalized, _value, _placeholder in terms):
        raise DatasheetApplicabilityError("requested MCU identity is not server-derived")
    try:
        from pypdf import PdfReader
        from pypdf import __version__ as pypdf_version

        reader = PdfReader(io.BytesIO(payload))
        metadata = " ".join(str(value) for value in (reader.metadata or {}).values())
        sources = [("metadata", metadata)] + [
            (f"page:{index + 1}", page.extract_text() or "")
            for index, page in enumerate(reader.pages)
        ]
    except Exception as exc:  # pypdf exposes several malformed/encrypted PDF exceptions
        raise DatasheetApplicabilityError(
            "datasheet PDF could not be read for MCU applicability; provide verifiable official "
            f"datasheet evidence for {requested_part}"
        ) from exc
    for locus, source in sources:
        for normalized_term, original_term, permits_placeholder in terms:
            if _contains_identity_term(
                source, original_term, permits_family_placeholder=permits_placeholder
            ):
                return DatasheetApplicabilityProof(
                    requested, normalized_term, locus, digest, f"{PARSER_VERSION}-{pypdf_version}"
                )
    raise DatasheetApplicabilityError(
        "datasheet applicability to requested MCU "
        f"{requested_part} was not established; provide verifiable official datasheet evidence"
    )


def read_datasheet_pdf(path: Path) -> bytes:
    """Read exact bytes from a non-empty local PDF without an arbitrary size policy."""

    try:
        size = path.stat().st_size
        if size < 5 or not path.is_file():
            raise DatasheetEvidenceError("datasheet evidence must be a non-empty PDF file")
        payload = path.read_bytes()
    except OSError as exc:
        raise DatasheetEvidenceError(f"datasheet PDF could not be read: {exc}") from exc
    if len(payload) != size:
        raise DatasheetEvidenceError("datasheet PDF changed while it was being read")
    if not payload.startswith(b"%PDF-"):
        raise DatasheetEvidenceError("datasheet evidence must be a PDF file")
    return payload


def capture_datasheet_evidence(store: FirmStore, source: Path) -> DatasheetEvidence:
    """Copy exact PDF bytes into immutable project evidence after server hashing."""

    payload = read_datasheet_pdf(source.expanduser().resolve())
    digest = hashlib.sha256(payload).hexdigest()
    destination = store.layout.datasheet_evidence(digest)
    if not destination.is_file():
        try:
            store.atomic_create_bytes(destination, payload)
        except ImmutableArtifactError:
            # Another server instance may have won the content-addressed
            # create. The bytes still have to replay exactly.
            pass
    try:
        if destination.read_bytes() != payload:
            raise DatasheetEvidenceError("datasheet evidence digest collision")
    except OSError as exc:
        raise DatasheetEvidenceError(f"captured datasheet evidence could not be read: {exc}") from exc
    reference = PurePosixPath(destination.relative_to(store.layout.project_root).as_posix()).as_posix()
    return DatasheetEvidence(digest, reference)


def replay_datasheet_evidence(
    store: FirmStore, reference: str, expected_sha256: str
) -> DatasheetEvidence:
    """Re-hash a captured PDF and reject path escape, loss, or mutation."""

    relative = PurePosixPath(reference)
    if relative.is_absolute() or ".." in relative.parts:
        raise DatasheetEvidenceError("datasheet evidence reference must be project-relative")
    path = (store.layout.project_root / Path(*relative.parts)).resolve()
    root = store.layout.project_root.resolve()
    if not path.is_relative_to(root):
        raise DatasheetEvidenceError("datasheet evidence reference escapes the project")
    payload = read_datasheet_pdf(path)
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected_sha256:
        raise DatasheetEvidenceError("captured datasheet evidence changed")
    if path != store.layout.datasheet_evidence(actual).resolve():
        raise DatasheetEvidenceError("datasheet evidence reference is not canonical")
    return DatasheetEvidence(actual, reference)
