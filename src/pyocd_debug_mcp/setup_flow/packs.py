"""Deterministic CMSIS-Pack candidate staging and promotion."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.pack_provision import (
    PackProvisionError,
    PackSpec,
    load_manifest_document,
    pack_spec_document,
    sha256_bytes,
    sha256_file,
)
from pyocd_debug_mcp.setup_flow.research import CandidateFailure, candidate_fingerprint

_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}")


class PackCandidateError(RuntimeError):
    """A pack candidate failed deterministic validation."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        failure: CandidateFailure | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.failure = failure


@dataclass(frozen=True, slots=True)
class PackCandidate:
    pack_id: str
    version: str
    filename: str
    url: str
    source_path: Path
    official_sha256: str | None = None

    def __post_init__(self) -> None:
        if not self.pack_id.strip() or not self.version.strip() or not self.url.strip():
            raise PackCandidateError(
                "package/invalid-candidate", "Package id, version, and source URL are required"
            )
        if Path(self.filename).name != self.filename or not self.filename.endswith(".pack"):
            raise PackCandidateError(
                "package/invalid-filename", "Package filename must be a plain .pack filename"
            )
        if self.official_sha256 is not None and _SHA256_PATTERN.fullmatch(
            self.official_sha256
        ) is None:
            raise PackCandidateError(
                "package/invalid-checksum", "Official checksum must be 64 hexadecimal digits"
            )

    def report_document(self) -> dict[str, Any]:
        return {
            "id": self.pack_id,
            "version": self.version,
            "filename": self.filename,
            "url": self.url,
            "official_sha256": self.official_sha256,
        }


@dataclass(frozen=True, slots=True)
class ValidatedPack:
    candidate: PackCandidate
    staged_path: Path
    actual_sha256: str
    enumerated_targets: tuple[str, ...]
    required_target: str


class PackCandidatePipeline:
    """Validate at most three materially distinct candidates before promotion."""

    def __init__(
        self,
        store: FirmStore,
        *,
        enumerate_targets: Callable[[Path], Sequence[str]],
        live_connect: Callable[[str, Path], None],
        record_failure: Callable[[CandidateFailure], None] | None = None,
        max_candidates: int = 3,
    ) -> None:
        if max_candidates < 1:
            raise ValueError("max_candidates must be positive")
        self._store = store
        self._enumerate_targets = enumerate_targets
        self._live_connect = live_connect
        self._record_failure = record_failure
        self._max_candidates = max_candidates
        self._seen_material: set[str] = set()
        self._failures: list[CandidateFailure] = []

    @property
    def failures(self) -> tuple[CandidateFailure, ...]:
        return tuple(self._failures)

    @staticmethod
    def _material_fingerprint(candidate: PackCandidate, actual_sha256: str) -> str:
        # Filename/id changes alone are not materially different research.
        return candidate_fingerprint(
            {
                "source": candidate.url,
                "version": candidate.version,
                "content_sha256": actual_sha256,
            }
        )

    def _reject(
        self,
        code: str,
        candidate: PackCandidate,
        fingerprint: str,
        reason: str,
        observed: Mapping[str, Any],
    ) -> PackCandidateError:
        failure = CandidateFailure(
            fingerprint=fingerprint,
            candidate=candidate.report_document(),
            reason=reason,
            observed=dict(observed),
        )
        self._failures.append(failure)
        if self._record_failure is not None:
            self._record_failure(failure)
        if len(self._seen_material) >= self._max_candidates:
            return PackCandidateError(
                "package/retry-exhausted",
                "Three materially distinct package candidates failed; setup is unresolved",
                failure=failure,
            )
        return PackCandidateError(code, reason, failure=failure)

    def validate(self, candidate: PackCandidate, *, required_target: str) -> ValidatedPack:
        try:
            payload = candidate.source_path.read_bytes()
        except OSError as exc:
            raise PackCandidateError(
                "package/source-unreadable", f"Package source could not be read: {exc}"
            ) from exc
        actual = sha256_bytes(payload)
        fingerprint = self._material_fingerprint(candidate, actual)
        if fingerprint in self._seen_material:
            raise PackCandidateError(
                "package/duplicate-candidate",
                "Candidate is identical or merely renamed; provide a materially different "
                "source, version, or checksum",
            )
        if len(self._seen_material) >= self._max_candidates:
            raise PackCandidateError(
                "package/retry-exhausted",
                "Three materially distinct package candidates have already been attempted",
            )
        self._seen_material.add(fingerprint)

        if candidate.official_sha256 is not None and actual != candidate.official_sha256.lower():
            raise self._reject(
                "package/checksum-mismatch",
                candidate,
                fingerprint,
                "Staged package checksum does not match the official checksum",
                {"actual_sha256": actual, "expected_sha256": candidate.official_sha256.lower()},
            )

        destination = self._store.layout.pack_files / candidate.filename
        existed = destination.exists()
        if existed and sha256_file(destination) != actual:
            raise self._reject(
                "package/filename-collision",
                candidate,
                fingerprint,
                "A different staged package already uses this filename",
                {"staged_path": str(destination)},
            )
        self._store.atomic_write_bytes(destination, payload)
        try:
            targets = tuple(
                dict.fromkeys(
                    target.strip() for target in self._enumerate_targets(destination) if target.strip()
                )
            )
            if required_target not in targets:
                if not existed:
                    self._store.remove_artifact(destination)
                raise self._reject(
                    "package/target-absent",
                    candidate,
                    fingerprint,
                    f"Required target '{required_target}' is absent from the staged package",
                    {"enumerated_targets": list(targets)},
                )
            try:
                self._live_connect(required_target, destination)
            except Exception as exc:
                if not existed:
                    self._store.remove_artifact(destination)
                raise self._reject(
                    "package/live-connect-failed",
                    candidate,
                    fingerprint,
                    f"Live connection with the staged package failed: {exc}",
                    {"enumerated_targets": list(targets)},
                ) from exc
        except PackCandidateError:
            raise
        except Exception as exc:
            if not existed:
                self._store.remove_artifact(destination)
            raise self._reject(
                "package/enumeration-failed",
                candidate,
                fingerprint,
                f"Staged package target enumeration failed: {exc}",
                {},
            ) from exc

        return ValidatedPack(candidate, destination, actual, targets, required_target)

    def promote(self, validated: ValidatedPack, *, board_id: str) -> Path:
        """Add metadata to the authoritative manifest only after all validation."""

        try:
            manifest = load_manifest_document(self._store.layout.pack_manifest)
        except PackProvisionError as exc:
            raise PackCandidateError("package/invalid-manifest", str(exc)) from exc
        packs = manifest.setdefault("packs", [])
        if not isinstance(packs, list):
            raise PackCandidateError(
                "package/invalid-manifest", "Pack manifest 'packs' field must be a list"
            )
        entry = pack_spec_document(
            PackSpec(
                id=validated.candidate.pack_id,
                version=validated.candidate.version,
                filename=validated.candidate.filename,
                url=validated.candidate.url,
                sha256=validated.actual_sha256,
                provides_targets=validated.enumerated_targets,
                needed_by_boards=(board_id,),
            )
        )
        for existing in packs:
            if not isinstance(existing, Mapping):
                continue
            if existing.get("id") == entry["id"] or existing.get("filename") == entry["filename"]:
                raise PackCandidateError(
                    "package/manifest-conflict",
                    "Package id or filename already exists in the authoritative manifest",
                )
        packs.append(entry)
        return self._store.atomic_write_pack_manifest(manifest)
