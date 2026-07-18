"""Pinned, fail-closed evidence loading for automatic first-time setup.

Automatic setup is intentionally narrower than the general research workflow:
it is available only when repository-reviewed documents and the installed
device-support implementation all match their recorded SHA-256 identities.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Mapping

from pyocd_debug_mcp.safety.verify2 import (
    EvidenceError,
    HardwareEvidence,
    ReconciliationResult,
    reconcile_hardware_evidence,
)
from pyocd_debug_mcp.setup_flow.board_catalog import BoardCatalogError, CatalogBoard


@dataclass(frozen=True, slots=True)
class ReviewedEvidenceBundle:
    """Verified source documents and their accepted reconciliation."""

    device_support_document: Mapping[str, Any]
    official_document: Mapping[str, Any]
    device_support_asset_sha256: str
    official_asset_sha256: str
    datasheet_sha256: str
    pyocd_version: str
    pyocd_target_module_sha256: str
    pyocd_svd_bundle_sha256: str
    reconciliation: ReconciliationResult

    def source_record(self) -> dict[str, object]:
        """Return the complete authority record covered by the map's semantic digest."""

        return {
            "device_support": {
                "asset_sha256": self.device_support_asset_sha256,
                "document": dict(self.device_support_document),
                "runtime": {
                    "pyocd_version": self.pyocd_version,
                    "target_module_sha256": self.pyocd_target_module_sha256,
                    "svd_bundle_sha256": self.pyocd_svd_bundle_sha256,
                },
            },
            "official_document": {
                "asset_sha256": self.official_asset_sha256,
                "datasheet_sha256": self.datasheet_sha256,
                "document": dict(self.official_document),
            },
            "reconciliation": {
                "status": self.reconciliation.status,
                "erase_geometry": (
                    {
                        "erase_origin": self.reconciliation.erase_geometry.erase_origin,
                        "erase_size": self.reconciliation.erase_geometry.erase_size,
                    }
                    if self.reconciliation.erase_geometry is not None
                    else None
                ),
                "facts": [
                    {
                        "fact_id": item.fact_id,
                        "kind": item.kind.value,
                        "start": item.address_range.start,
                        "end": item.address_range.end,
                        "source_ids": list(item.source_ids),
                        "reconciliations": list(item.reconciliations),
                    }
                    for item in self.reconciliation.regions
                ],
            },
        }


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_asset(
    resource: str | None, expected_digest: str | None, label: str
) -> tuple[dict[str, Any], str]:
    if not resource or not expected_digest:
        raise BoardCatalogError(f"{label} evidence is not reviewed for automatic setup")
    root = (Path(__file__).resolve().parent / "evidence").resolve()
    path = (Path(__file__).resolve().parent / resource).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise BoardCatalogError(
            f"{label} evidence resource escapes the reviewed evidence directory"
        ) from exc
    if path.is_symlink() or not path.is_file():
        raise BoardCatalogError(f"{label} evidence resource is missing or not a regular file")
    digest = _sha256_file(path)
    if digest != expected_digest:
        raise BoardCatalogError(f"{label} evidence resource failed its pinned SHA-256 check")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BoardCatalogError(f"{label} evidence resource is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise BoardCatalogError(f"{label} evidence resource must contain one JSON object")
    return document, digest


def _runtime_pyocd_identity(catalog: CatalogBoard) -> tuple[str, str, str]:
    if not (
        catalog.pyocd_version
        and catalog.pyocd_target_module
        and catalog.pyocd_target_module_sha256
        and catalog.pyocd_svd_bundle_sha256
    ):
        raise BoardCatalogError("installed device-support identity is not pinned")
    try:
        installed_version = version("pyocd")
    except PackageNotFoundError as exc:
        raise BoardCatalogError("pyOCD is not installed in the server environment") from exc
    if installed_version != catalog.pyocd_version:
        raise BoardCatalogError(
            f"installed pyOCD {installed_version} does not match reviewed version {catalog.pyocd_version}"
        )
    try:
        target_module = import_module(catalog.pyocd_target_module)
        target_path = Path(str(target_module.__file__)).resolve()
        svd_loader = import_module("pyocd.debug.svd.loader")
        svd_path = Path(str(svd_loader.__file__)).resolve().parent / "svd_data.zip"
    except (ImportError, TypeError) as exc:
        raise BoardCatalogError("reviewed pyOCD target or SVD support cannot be located") from exc
    target_digest = _sha256_file(target_path)
    svd_digest = _sha256_file(svd_path)
    if target_digest != catalog.pyocd_target_module_sha256:
        raise BoardCatalogError(
            "installed pyOCD target implementation failed its pinned SHA-256 check"
        )
    if svd_digest != catalog.pyocd_svd_bundle_sha256:
        raise BoardCatalogError("installed pyOCD SVD bundle failed its pinned SHA-256 check")
    return installed_version, target_digest, svd_digest


def load_pinned_reviewed_evidence(
    catalog: CatalogBoard,
    datasheet_sha256: str,
) -> ReviewedEvidenceBundle:
    """Resolve current pinned authorities for an already-reviewed datasheet digest."""

    if not catalog.automatic_setup_reviewed:
        raise BoardCatalogError(
            f"{catalog.board_type} lacks complete reviewed automatic-setup evidence"
        )
    if datasheet_sha256 not in catalog.datasheet_sha256:
        raise BoardCatalogError("datasheet SHA-256 is not a reviewed catalog anchor")
    support_document, support_hash = _load_asset(
        catalog.device_support_evidence_resource,
        catalog.device_support_evidence_sha256,
        "device-support",
    )
    official_document, official_hash = _load_asset(
        catalog.official_evidence_resource,
        catalog.official_evidence_sha256,
        "official-document",
    )
    runtime_version, target_hash, svd_hash = _runtime_pyocd_identity(catalog)
    try:
        support = HardwareEvidence.from_document(support_document)
        official = HardwareEvidence.from_document(official_document)
        expected_official_revision = f"sha256:{datasheet_sha256}"
        if not any(
            source.kind.value == "datasheet" and source.revision == expected_official_revision
            for source in official.sources
        ):
            raise BoardCatalogError(
                "official evidence is not bound to the server-computed datasheet SHA-256"
            )
        expected_runtime_revisions = {
            "target": f"sha256:{target_hash}",
            "svd": f"bundle-sha256:{svd_hash}",
        }
        observed_runtime_revisions = {
            source.kind.value: source.revision
            for source in support.sources
            if source.kind.value in expected_runtime_revisions
        }
        if observed_runtime_revisions != expected_runtime_revisions or any(
            source.version != runtime_version for source in support.sources
        ):
            raise BoardCatalogError(
                "device-support evidence is not bound to the installed pyOCD target and SVD"
            )
        reconciliation = reconcile_hardware_evidence(
            expected_mcu_part_number=catalog.package_part_number,
            expected_target=catalog.pyocd_target,
            device_support=support,
            official_document=official,
        )
    except EvidenceError as exc:
        raise BoardCatalogError(f"reviewed hardware evidence is invalid: {exc}") from exc
    if not reconciliation.accepted:
        summary = "; ".join(
            f"{conflict.code}: {conflict.message}" for conflict in reconciliation.conflicts
        )
        raise BoardCatalogError(f"reviewed hardware evidence conflicts: {summary}")

    facts = {item.fact_id: item for item in reconciliation.regions}
    expected_ranges = {
        "physical_flash": (catalog.flash_start, catalog.flash_end),
        "physical_ram": (catalog.ram_start, catalog.ram_end),
        "writable_ram": (catalog.ram_start, catalog.ram_end),
    }
    for fact_id, endpoints in expected_ranges.items():
        fact = facts.get(fact_id)
        if fact is None or (fact.address_range.start, fact.address_range.end) != endpoints:
            raise BoardCatalogError(
                f"reconciled {fact_id} does not match the reviewed deployment geometry"
            )
    geometry = reconciliation.erase_geometry
    if geometry is None or (
        geometry.erase_origin != catalog.flash_start or geometry.erase_size != catalog.erase_size
    ):
        raise BoardCatalogError(
            "reconciled erase geometry does not match the reviewed catalog deployment geometry"
        )
    return ReviewedEvidenceBundle(
        support_document,
        official_document,
        support_hash,
        official_hash,
        datasheet_sha256,
        runtime_version,
        target_hash,
        svd_hash,
        reconciliation,
    )


def load_reviewed_evidence(
    catalog: CatalogBoard,
    datasheet_path: Path,
) -> ReviewedEvidenceBundle:
    """Verify server-read datasheet bytes and current pinned independent authorities."""

    datasheet_sha256 = catalog.validate_datasheet(datasheet_path)
    return load_pinned_reviewed_evidence(catalog, datasheet_sha256)


def verify_persisted_reviewed_evidence(
    catalog: CatalogBoard,
    pack_record: Mapping[str, object],
    authority_record: Mapping[str, object],
) -> ReviewedEvidenceBundle:
    """Re-resolve every persisted authority anchor before a map can authorize I/O."""

    if not catalog.automatic_setup_reviewed:
        raise BoardCatalogError(
            f"{catalog.board_type} has no server-resolvable reviewed safety authority"
        )
    support_document, support_hash = _load_asset(
        catalog.device_support_evidence_resource,
        catalog.device_support_evidence_sha256,
        "device-support",
    )
    official_document, official_hash = _load_asset(
        catalog.official_evidence_resource,
        catalog.official_evidence_sha256,
        "official-document",
    )
    runtime_version, target_hash, svd_hash = _runtime_pyocd_identity(catalog)
    persisted_support = pack_record.get("document")
    persisted_runtime = pack_record.get("runtime")
    official_record = authority_record.get("official_document")
    if not isinstance(official_record, Mapping):
        raise BoardCatalogError("persisted official-document authority record is missing")
    persisted_official = official_record.get("document")
    datasheet_hash = official_record.get("datasheet_sha256")
    if pack_record.get("asset_sha256") != support_hash or persisted_support != support_document:
        raise BoardCatalogError(
            "persisted device-support evidence does not match its pinned repository asset"
        )
    if (
        official_record.get("asset_sha256") != official_hash
        or persisted_official != official_document
    ):
        raise BoardCatalogError(
            "persisted official evidence does not match its pinned repository asset"
        )
    if not isinstance(datasheet_hash, str) or datasheet_hash not in catalog.datasheet_sha256:
        raise BoardCatalogError("persisted official evidence has no reviewed datasheet anchor")
    expected_runtime = {
        "pyocd_version": runtime_version,
        "target_module_sha256": target_hash,
        "svd_bundle_sha256": svd_hash,
    }
    if persisted_runtime != expected_runtime:
        raise BoardCatalogError(
            "persisted device-support identity does not match the installed reviewed runtime"
        )

    try:
        support = HardwareEvidence.from_document(persisted_support)
        official = HardwareEvidence.from_document(persisted_official)
        reconciliation = reconcile_hardware_evidence(
            expected_mcu_part_number=catalog.package_part_number,
            expected_target=catalog.pyocd_target,
            device_support=support,
            official_document=official,
        )
    except EvidenceError as exc:
        raise BoardCatalogError(f"persisted reviewed evidence is invalid: {exc}") from exc
    if not reconciliation.accepted:
        raise BoardCatalogError("persisted reviewed evidence no longer reconciles")
    facts = {item.fact_id: item for item in reconciliation.regions}
    expected_ranges = {
        "physical_flash": (catalog.flash_start, catalog.flash_end),
        "physical_ram": (catalog.ram_start, catalog.ram_end),
        "writable_ram": (catalog.ram_start, catalog.ram_end),
    }
    if any(
        fact_id not in facts
        or (
            facts[fact_id].address_range.start,
            facts[fact_id].address_range.end,
        )
        != endpoints
        for fact_id, endpoints in expected_ranges.items()
    ):
        raise BoardCatalogError("persisted reviewed evidence has unexpected deployment geometry")
    geometry = reconciliation.erase_geometry
    if geometry is None or (
        geometry.erase_origin != catalog.flash_start or geometry.erase_size != catalog.erase_size
    ):
        raise BoardCatalogError("persisted reviewed erase geometry is not the catalog geometry")
    return ReviewedEvidenceBundle(
        support_document,
        official_document,
        support_hash,
        official_hash,
        datasheet_hash,
        runtime_version,
        target_hash,
        svd_hash,
        reconciliation,
    )
