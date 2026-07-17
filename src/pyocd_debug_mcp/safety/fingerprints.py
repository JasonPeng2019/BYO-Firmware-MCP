"""Canonical, domain-separated fingerprints for persisted safety-map inputs."""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from pathlib import PurePath
from typing import Final

FINGERPRINT_SCHEMA_VERSION: Final = 1
FINGERPRINT_ALGORITHM: Final = "sha256"
_DIGEST = re.compile(r"[0-9a-f]{64}")


class FingerprintError(ValueError):
    """Fingerprint input or a persisted fingerprint document is invalid."""


class FingerprintSource(str, Enum):
    PROFILE = "profile"
    PART_TARGET = "part_target"
    PACK = "pack"
    EVIDENCE = "evidence"
    APPLICATION_ARTIFACTS = "application_artifacts"
    BOOTLOADER_ARTIFACTS = "bootloader_artifacts"
    GEOMETRY = "geometry"
    SCHEMA = "schema"


def canonicalize(value: object, *, location: str = "value") -> object:
    """Return a strict JSON-compatible value with mapping keys in canonical order."""

    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise FingerprintError(f"{location} mapping keys must be strings")
        return {
            key: canonicalize(value[key], location=f"{location}.{key}")
            for key in sorted(value)
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            canonicalize(item, location=f"{location}[{index}]")
            for index, item in enumerate(value)
        ]
    raise FingerprintError(
        f"{location} must contain only JSON null, booleans, integers, strings, arrays, or objects"
    )


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        canonicalize(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _normalized_artifact_path(value: object, location: str) -> str:
    if isinstance(value, PurePath):
        text = value.as_posix()
    elif isinstance(value, str) and value.strip():
        text = value.strip().replace("\\", "/")
    else:
        raise FingerprintError(f"{location} must be a non-empty path string")
    normalized = posixpath.normpath(text)
    if normalized == "." and text not in {".", "./"}:
        raise FingerprintError(f"{location} does not identify an artifact path")
    return normalized


def canonicalize_artifacts(
    value: object, *, location: str = "artifacts", collection_key: str | None = None
) -> object:
    """Canonicalize artifact manifests, file ordering, and portable path spelling."""

    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise FingerprintError(f"{location} mapping keys must be strings")
        output: dict[str, object] = {}
        for key in sorted(value):
            item = value[key]
            if key.casefold() == "path" or key.casefold().endswith("_path"):
                output[key] = _normalized_artifact_path(item, f"{location}.{key}")
            else:
                output[key] = canonicalize_artifacts(
                    item,
                    location=f"{location}.{key}",
                    collection_key=key.casefold(),
                )
        return output
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        path_collection = collection_key in {"artifacts", "files"}
        items = [
            (
                _normalized_artifact_path(item, f"{location}[{index}]")
                if path_collection and isinstance(item, (str, PurePath))
                else canonicalize_artifacts(item, location=f"{location}[{index}]")
            )
            for index, item in enumerate(value)
        ]
        path_records = bool(items) and all(
            isinstance(item, Mapping)
            and any(key == "path" or key.endswith("_path") for key in item)
            for item in items
        )
        if path_collection or path_records:
            items.sort(key=canonical_bytes)
        return items
    if isinstance(value, PurePath):
        return _normalized_artifact_path(value, location)
    return canonicalize(value, location=location)


def _source_digest(source: FingerprintSource, value: object) -> str:
    payload = b"firm-safety-source-v1\0" + source.value.encode("ascii") + b"\0"
    return sha256(payload + canonical_bytes(value)).hexdigest()


def _aggregate_digest(sub_fingerprints: Mapping[str, str]) -> str:
    payload = {
        "algorithm": FINGERPRINT_ALGORITHM,
        "schema_version": FINGERPRINT_SCHEMA_VERSION,
        "sub_fingerprints": dict(sorted(sub_fingerprints.items())),
    }
    return sha256(b"firm-safety-aggregate-v1\0" + canonical_bytes(payload)).hexdigest()


@dataclass(frozen=True, slots=True)
class FingerprintInputs:
    profile: object
    part_target: object
    pack: object
    evidence: object
    application_artifacts: object
    bootloader_artifacts: object
    geometry: object
    schema: object

    def values(self) -> dict[FingerprintSource, object]:
        return {
            FingerprintSource.PROFILE: self.profile,
            FingerprintSource.PART_TARGET: self.part_target,
            FingerprintSource.PACK: self.pack,
            FingerprintSource.EVIDENCE: self.evidence,
            FingerprintSource.APPLICATION_ARTIFACTS: canonicalize_artifacts(
                self.application_artifacts,
                location=FingerprintSource.APPLICATION_ARTIFACTS.value,
            ),
            FingerprintSource.BOOTLOADER_ARTIFACTS: canonicalize_artifacts(
                self.bootloader_artifacts,
                location=FingerprintSource.BOOTLOADER_ARTIFACTS.value,
            ),
            FingerprintSource.GEOMETRY: self.geometry,
            FingerprintSource.SCHEMA: self.schema,
        }

    def canonical_documents(self) -> dict[str, object]:
        return {
            source.value: canonicalize(value, location=source.value)
            for source, value in self.values().items()
        }


@dataclass(frozen=True, slots=True)
class FingerprintSet:
    sub_fingerprints: tuple[tuple[FingerprintSource, str], ...]
    aggregate: str

    @classmethod
    def build(cls, inputs: FingerprintInputs) -> FingerprintSet:
        items = tuple(
            (source, _source_digest(source, value))
            for source, value in inputs.values().items()
        )
        aggregate = _aggregate_digest({source.value: digest for source, digest in items})
        return cls(items, aggregate)

    @classmethod
    def from_document(cls, document: object) -> FingerprintSet:
        if not isinstance(document, Mapping) or set(document) != {
            "algorithm",
            "schema_version",
            "sub_fingerprints",
            "aggregate",
        }:
            raise FingerprintError("fingerprint document fields do not match schema v1")
        if (
            document["algorithm"] != FINGERPRINT_ALGORITHM
            or document["schema_version"] != FINGERPRINT_SCHEMA_VERSION
        ):
            raise FingerprintError("unsupported fingerprint algorithm or schema version")
        raw_sub = document["sub_fingerprints"]
        if not isinstance(raw_sub, Mapping) or set(raw_sub) != {
            source.value for source in FingerprintSource
        }:
            raise FingerprintError("sub_fingerprints must contain every exact source group")
        items: list[tuple[FingerprintSource, str]] = []
        for source in FingerprintSource:
            digest = raw_sub[source.value]
            if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
                raise FingerprintError(f"invalid {source.value} sub-fingerprint")
            items.append((source, digest))
        aggregate = document["aggregate"]
        if not isinstance(aggregate, str) or _DIGEST.fullmatch(aggregate) is None:
            raise FingerprintError("invalid aggregate fingerprint")
        expected = _aggregate_digest({source.value: digest for source, digest in items})
        if aggregate != expected:
            raise FingerprintError("aggregate fingerprint does not match its sub-fingerprints")
        return cls(tuple(items), aggregate)

    def as_mapping(self) -> dict[FingerprintSource, str]:
        return dict(self.sub_fingerprints)

    def to_document(self) -> dict[str, object]:
        return {
            "schema_version": FINGERPRINT_SCHEMA_VERSION,
            "algorithm": FINGERPRINT_ALGORITHM,
            "sub_fingerprints": {
                source.value: digest for source, digest in self.sub_fingerprints
            },
            "aggregate": self.aggregate,
        }

    def changed_sources(self, newer: FingerprintSet) -> tuple[FingerprintSource, ...]:
        current = self.as_mapping()
        candidate = newer.as_mapping()
        return tuple(source for source in FingerprintSource if current[source] != candidate[source])
