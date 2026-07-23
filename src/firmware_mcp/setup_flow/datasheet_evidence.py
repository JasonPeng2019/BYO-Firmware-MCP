"""Exact, immutable local datasheet evidence capture and replay."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from firmware_mcp.firmstore.store import FirmStore, ImmutableArtifactError

PARSER_VERSION = "pdf-exact-v2"


class DatasheetEvidenceError(ValueError):
    """The local PDF cannot serve as deterministic setup evidence."""


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
        raise DatasheetEvidenceError(
            f"captured datasheet evidence could not be read: {exc}"
        ) from exc
    reference = PurePosixPath(
        destination.relative_to(store.layout.project_root).as_posix()
    ).as_posix()
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
