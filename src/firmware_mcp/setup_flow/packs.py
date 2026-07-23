"""Deterministic CMSIS-Pack candidate staging and promotion."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from firmware_mcp.firmstore.store import FirmStore
from firmware_mcp.pack_provision import (
    DeviceBinding,
    PackProvisionError,
    PackSpec,
    load_manifest,
    load_manifest_document,
    pack_spec_document,
    read_pack_bytes,
    sha256_bytes,
    sha256_file,
)
from firmware_mcp.setup_flow.research import CandidateFailure, candidate_fingerprint

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
        if Path(self.filename).name != self.filename or not self.filename.casefold().endswith(
            ".pack"
        ):
            raise PackCandidateError(
                "package/invalid-filename", "Package filename must be a plain .pack filename"
            )
        if (
            self.official_sha256 is not None
            and _SHA256_PATTERN.fullmatch(self.official_sha256) is None
        ):
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
    validated_payload: bytes
    enumerated_targets: tuple[str, ...]
    required_target: str
    device_binding: DeviceBinding | None = None
    created_staged_file: bool = False


class PackCandidatePipeline:
    """Validate every materially distinct candidate before promotion."""

    def __init__(
        self,
        store: FirmStore,
        *,
        enumerate_targets: Callable[[Path, str], Sequence[str]],
        live_connect: Callable[[str, Path, str, str | None], None],
        record_failure: Callable[[CandidateFailure], None] | None = None,
    ) -> None:
        self._store = store
        self._enumerate_targets = enumerate_targets
        self._live_connect = live_connect
        self._record_failure = record_failure
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
        return PackCandidateError(code, reason, failure=failure)

    def validate(self, candidate: PackCandidate, *, required_target: str) -> ValidatedPack:
        return self.validate_device(candidate, required_target=required_target)

    def validate_device(
        self,
        candidate: PackCandidate,
        *,
        required_target: str,
        device_binding: DeviceBinding | None = None,
    ) -> ValidatedPack:
        """Validate bytes, derived binding, target exposure, and live attach before promotion."""
        try:
            payload = read_pack_bytes(candidate.source_path)
        except PackProvisionError as exc:
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
        self._seen_material.add(fingerprint)

        if candidate.official_sha256 is not None and actual != candidate.official_sha256.lower():
            raise self._reject(
                "package/checksum-mismatch",
                candidate,
                fingerprint,
                "Staged package checksum does not match the official checksum",
                {"actual_sha256": actual, "expected_sha256": candidate.official_sha256.lower()},
            )

        if device_binding is not None and (
            device_binding.pyocd_target.casefold() != required_target.casefold()
        ):
            raise self._reject(
                "package/device-binding-target-mismatch",
                candidate,
                fingerprint,
                "The server-derived PDSC binding does not match the proposed target",
                {
                    "derived_target": device_binding.pyocd_target,
                    "proposed_target": required_target,
                },
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
            if device_binding is not None:
                # Replay the exact-leaf proof from the staged bytes. The source
                # path is agent-controlled and may change between the earlier
                # research parse and quarantine, so the earlier object alone is
                # never accepted as authority.
                from firmware_mcp.setup_flow.device_support import (
                    derive_candidate_binding,
                )

                replayed_binding = derive_candidate_binding(destination, device_binding.part_number)
                if replayed_binding != device_binding:
                    if not existed:
                        self._store.remove_artifact(destination)
                    raise self._reject(
                        "package/device-binding-drift",
                        candidate,
                        fingerprint,
                        "The quarantined package no longer matches the server-derived device binding",
                        {
                            "expected_binding": {
                                "part_number": device_binding.part_number,
                                "pdsc_device": device_binding.pdsc_device,
                                "pyocd_target": device_binding.pyocd_target,
                            },
                            "observed_binding": {
                                "part_number": replayed_binding.part_number,
                                "pdsc_device": replayed_binding.pdsc_device,
                                "pyocd_target": replayed_binding.pyocd_target,
                            },
                        },
                    )
            targets = tuple(
                dict.fromkeys(
                    target.strip()
                    for target in self._enumerate_targets(destination, actual)
                    if target.strip()
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
                self._live_connect(
                    required_target,
                    destination,
                    actual,
                    device_binding.pdsc_device if device_binding is not None else None,
                )
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

        return ValidatedPack(
            candidate,
            destination,
            actual,
            payload,
            targets,
            required_target,
            device_binding,
            not existed,
        )

    def promote(self, validated: ValidatedPack, *, board_id: str) -> Path:
        """Add metadata to the authoritative manifest only after all validation."""
        try:
            if sha256_file(validated.staged_path) != validated.actual_sha256:
                raise PackCandidateError(
                    "package/staged-bytes-changed",
                    "The quarantined package changed after validation and cannot be promoted",
                )
            if validated.device_binding is not None:
                from firmware_mcp.setup_flow.device_support import (
                    derive_candidate_binding,
                )

                replayed = derive_candidate_binding(
                    validated.staged_path, validated.device_binding.part_number
                )
                if replayed != validated.device_binding:
                    raise PackCandidateError(
                        "package/device-binding-drift",
                        "The quarantined package binding changed after validation",
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
                    device_bindings=(
                        (validated.device_binding,) if validated.device_binding is not None else ()
                    ),
                )
            )

            def merge(manifest_path: Path) -> Mapping[str, Any]:
                # Rebind the final file to the exact bounded payload used by
                # validation/live attach while the manifest merge is locked.
                if sha256_file(validated.staged_path) != validated.actual_sha256:
                    raise PackCandidateError(
                        "package/staged-bytes-changed",
                        "The quarantined package changed before promotion",
                    )
                self._store.atomic_write_bytes(validated.staged_path, validated.validated_payload)
                if sha256_file(validated.staged_path) != validated.actual_sha256:
                    raise PackCandidateError(
                        "package/staged-bytes-changed",
                        "The promoted package bytes could not be rebound exactly",
                    )
                try:
                    manifest = load_manifest_document(manifest_path)
                except PackProvisionError as exc:
                    raise PackCandidateError("package/invalid-manifest", str(exc)) from exc
                packs = manifest.setdefault("packs", [])
                if not isinstance(packs, list):
                    raise PackCandidateError(
                        "package/invalid-manifest", "Pack manifest 'packs' field must be a list"
                    )
                existing_specs = load_manifest(manifest_path)
                for index, existing in enumerate(packs):
                    if not isinstance(existing, Mapping):
                        continue
                    if (
                        existing.get("id") != entry["id"]
                        and existing.get("filename") != entry["filename"]
                    ):
                        continue
                    prior = next(
                        (
                            spec
                            for spec in existing_specs
                            if spec.id == existing.get("id")
                            and spec.filename == existing.get("filename")
                        ),
                        None,
                    )
                    if (
                        prior is None
                        or prior.id != validated.candidate.pack_id
                        or prior.filename != validated.candidate.filename
                        or prior.version != validated.candidate.version
                        or prior.url != validated.candidate.url
                        or prior.sha256 != validated.actual_sha256
                        or prior.provides_targets != validated.enumerated_targets
                    ):
                        raise PackCandidateError(
                            "package/manifest-conflict",
                            "Package id or filename conflicts with a different pinned package",
                        )
                    binding = validated.device_binding
                    bindings = list(prior.device_bindings)
                    if binding is not None:
                        same_part = [
                            item
                            for item in bindings
                            if item.part_number.casefold() == binding.part_number.casefold()
                        ]
                        if same_part and same_part != [binding]:
                            raise PackCandidateError(
                                "package/manifest-conflict",
                                "The existing package binds this exact part differently",
                            )
                        if not same_part:
                            bindings.append(binding)
                    merged = PackSpec(
                        prior.id,
                        prior.version,
                        prior.filename,
                        prior.url,
                        prior.sha256,
                        prior.provides_targets,
                        tuple(sorted(set((*prior.needed_by_boards, board_id)))),
                        tuple(bindings),
                    )
                    packs[index] = pack_spec_document(merged)
                    return manifest
                packs.append(entry)
                return manifest

            return self._store.update_pack_manifest(merge)
        except Exception:
            if validated.created_staged_file:
                self._store.remove_artifact(validated.staged_path)
            raise
