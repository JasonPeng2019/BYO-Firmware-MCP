"""Reviewed board identities and deployment envelopes used by fresh setup.

These records are server-owned evidence.  They deliberately contain no probe
assignment, gate, plan, or permission state and callers cannot supply memory
ranges through the MCP schema.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from pyocd_debug_mcp.safety.regions import AddressRange, RegionKind


class BoardCatalogError(ValueError):
    """A requested supported-board identity does not match reviewed evidence."""


class ReviewedSupportNotFoundError(BoardCatalogError):
    """No reviewed record accepts the exact MCU and server-computed datasheet digest."""

    code = "setup/reviewed-support-not-found"


class ReviewedSupportAmbiguityError(BoardCatalogError):
    """More than one reviewed record accepts the same exact setup evidence."""

    code = "setup/reviewed-support-ambiguous"


@dataclass(frozen=True, slots=True)
class CatalogHardwareRegion:
    """One range independently reviewed against device support and official documentation."""

    name: str
    kind: RegionKind
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class CatalogBoard:
    board_type: str
    mcu_names: tuple[str, ...]
    package_part_number: str
    pyocd_target: str
    probe_family: str
    default_baudrate: int
    test_read_address: int
    silicon_id_address: int | None
    silicon_id_expected: int | None
    silicon_id_mask: int
    silicon_id_width_bits: int
    silicon_id_label: str
    silicon_id_limitation: str
    flash_start: int
    flash_end: int
    ram_start: int
    ram_end: int
    erase_size: int
    application_start: int
    application_end: int
    application_partition_authoritative: bool
    bootloader_start: int | None
    bootloader_end: int | None
    bootloader_partition_authoritative: bool
    no_protected_resident_bootloader: bool
    hardware_regions: tuple[CatalogHardwareRegion, ...]
    datasheet_sha256: tuple[str, ...] = ()
    document_revision: str = "repository-reviewed board support v1"
    zephyr_board_target: str | None = None
    device_support_evidence_resource: str | None = None
    device_support_evidence_sha256: str | None = None
    official_evidence_resource: str | None = None
    official_evidence_sha256: str | None = None
    pyocd_version: str | None = None
    pyocd_target_module: str | None = None
    pyocd_target_module_sha256: str | None = None
    pyocd_svd_bundle_sha256: str | None = None
    pyocd_pack_filename: str | None = None
    pyocd_pack_sha256: str | None = None
    debug_connect_mode: str | None = None
    debug_clock_hz: int | None = None

    def accepts_mcu(self, value: str) -> bool:
        normalized = value.strip().casefold().replace("-", "").replace("_", "")
        return any(
            normalized == item.casefold().replace("-", "").replace("_", "")
            for item in self.mcu_names
        )

    def validate_datasheet(self, path: Path) -> str:
        """Validate and hash one local PDF using only server-read bytes."""

        if not self.datasheet_sha256:
            raise BoardCatalogError(
                f"No reviewed datasheet is configured for {self.board_type}; "
                "automatic setup remains unavailable."
            )
        _, digest = hash_local_datasheet(path)
        if digest not in self.datasheet_sha256:
            raise BoardCatalogError(
                f"The server-computed datasheet hash is not reviewed for {self.board_type}."
            )
        return digest

    @property
    def automatic_setup_reviewed(self) -> bool:
        module_runtime = bool(
            self.pyocd_target_module
            and self.pyocd_target_module_sha256
            and self.pyocd_svd_bundle_sha256
            and not self.pyocd_pack_filename
            and not self.pyocd_pack_sha256
        )
        pack_runtime = bool(
            self.pyocd_pack_filename
            and self.pyocd_pack_sha256
            and not self.pyocd_target_module
            and not self.pyocd_target_module_sha256
            and not self.pyocd_svd_bundle_sha256
        )
        return bool(
            self.datasheet_sha256
            and self.device_support_evidence_resource
            and self.device_support_evidence_sha256
            and self.official_evidence_resource
            and self.official_evidence_sha256
            and self.pyocd_version
            and (module_runtime or pack_runtime)
            and self.live_identity_reviewed
            and self.application_partition is not None
        )

    @property
    def live_identity_reviewed(self) -> bool:
        """Whether validation has an explicit reviewed electronic identity proof."""

        return bool(
            self.silicon_id_address is not None
            and self.silicon_id_expected is not None
            and self.silicon_id_mask > 0
            and self.silicon_id_width_bits in {8, 16, 32, 64}
            and self.silicon_id_label
            and self.silicon_id_limitation
        )

    @property
    def application_partition(self) -> AddressRange | None:
        """Return application authority only when the reviewed policy explicitly grants it."""

        if not self.application_partition_authoritative:
            return None
        if (
            self.application_start == self.flash_start
            and self.application_end == self.flash_end
            and not self.no_protected_resident_bootloader
        ):
            return None
        return AddressRange(self.application_start, self.application_end)

    @property
    def bootloader_partition(self) -> AddressRange | None:
        if not self.bootloader_partition_authoritative:
            return None
        if self.bootloader_start is None or self.bootloader_end is None:
            return None
        return AddressRange(self.bootloader_start, self.bootloader_end)

    def deployment_partition_policy_document(self) -> dict[str, object]:
        """Return the explicit reviewed partition policy for map-source hashing."""

        return {
            "application": {
                "authoritative": self.application_partition_authoritative,
                "start": self.application_start,
                "end": self.application_end,
            },
            "bootloader": {
                "authoritative": self.bootloader_partition_authoritative,
                "start": self.bootloader_start,
                "end": self.bootloader_end,
            },
            "no_protected_resident_bootloader": self.no_protected_resident_bootloader,
        }



def _required_string(raw: dict[str, object], name: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not value.strip():
        raise BoardCatalogError(f"Catalog field '{name}' must be a non-empty string")
    return value.strip()


def _required_int(raw: dict[str, object], name: str) -> int:
    value = raw.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise BoardCatalogError(f"Catalog field '{name}' must be an integer")
    return value


def _optional_int(raw: dict[str, object], name: str) -> int | None:
    value = raw.get(name)
    if value is None:
        return None
    return _required_int(raw, name)


def _optional_string(raw: dict[str, object], name: str) -> str | None:
    value = raw.get(name)
    if value is None:
        return None
    return _required_string(raw, name)


def _required_bool(raw: dict[str, object], name: str) -> bool:
    value = raw.get(name)
    if not isinstance(value, bool):
        raise BoardCatalogError(f"Catalog field '{name}' must be a boolean")
    return value


def _string_tuple(raw: dict[str, object], name: str, *, required: bool = False) -> tuple[str, ...]:
    value = raw.get(name)
    if value is None and not required:
        return ()
    if not isinstance(value, list) or (required and not value):
        raise BoardCatalogError(f"Catalog field '{name}' must be a non-empty string list")
    result = tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
    if len(result) != len(value):
        raise BoardCatalogError(f"Catalog field '{name}' must contain only non-empty strings")
    return result


def _load_catalog(path: Path | None = None) -> dict[str, CatalogBoard]:
    """Load reviewed board facts from an explicitly configured external document."""

    try:
        if path is None:
            return {}
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BoardCatalogError(f"Reviewed board catalog is unreadable: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        source = str(path) if path is not None else "configured catalog"
        raise BoardCatalogError(f"Reviewed board catalog is unreadable: {source}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 2:
        raise BoardCatalogError("Reviewed board catalog must use schema_version 2")
    rows = document.get("boards")
    if not isinstance(rows, list):
        raise BoardCatalogError("Reviewed board catalog 'boards' must be a list")

    result: dict[str, CatalogBoard] = {}
    for raw_row in rows:
        if not isinstance(raw_row, dict):
            raise BoardCatalogError("Each reviewed board entry must be an object")
        raw = dict(raw_row)
        board_type = _required_string(raw, "board_type")
        key = board_type.casefold()
        if key in result:
            raise BoardCatalogError(f"Duplicate reviewed board type: {board_type}")

        regions_value = raw.get("hardware_regions")
        if not isinstance(regions_value, list):
            raise BoardCatalogError(f"{board_type}: hardware_regions must be a list")
        regions: list[CatalogHardwareRegion] = []
        for raw_region in regions_value:
            if not isinstance(raw_region, dict):
                raise BoardCatalogError(f"{board_type}: hardware region must be an object")
            region_raw = dict(raw_region)
            try:
                kind = RegionKind(_required_string(region_raw, "kind"))
            except ValueError as exc:
                raise BoardCatalogError(f"{board_type}: unsupported hardware region kind") from exc
            start = _required_int(region_raw, "start")
            end = _required_int(region_raw, "end")
            if start < 0 or end <= start:
                raise BoardCatalogError(f"{board_type}: hardware region must be a non-empty range")
            regions.append(
                CatalogHardwareRegion(_required_string(region_raw, "name"), kind, start, end)
            )

        flash_start = _required_int(raw, "flash_start")
        flash_end = _required_int(raw, "flash_end")
        ram_start = _required_int(raw, "ram_start")
        ram_end = _required_int(raw, "ram_end")
        application_start = _required_int(raw, "application_start")
        application_end = _required_int(raw, "application_end")
        application_authoritative = _required_bool(raw, "application_partition_authoritative")
        bootloader_start = _optional_int(raw, "bootloader_start")
        bootloader_end = _optional_int(raw, "bootloader_end")
        bootloader_authoritative = _required_bool(raw, "bootloader_partition_authoritative")
        no_resident_bootloader = _required_bool(raw, "no_protected_resident_bootloader")
        erase_size = _required_int(raw, "erase_size")
        if not (0 <= flash_start < flash_end and 0 <= ram_start < ram_end):
            raise BoardCatalogError(f"{board_type}: flash and RAM ranges must be non-empty")
        if not (flash_start <= application_start < application_end <= flash_end):
            raise BoardCatalogError(f"{board_type}: application range must be inside flash")
        if erase_size <= 0:
            raise BoardCatalogError(f"{board_type}: erase_size must be positive")
        if application_authoritative and (
            application_start == flash_start
            and application_end == flash_end
            and not no_resident_bootloader
        ):
            raise BoardCatalogError(
                f"{board_type}: full-flash application authority requires an explicit "
                "no-protected-resident-bootloader assertion"
            )
        if bootloader_authoritative != (
            bootloader_start is not None and bootloader_end is not None
        ):
            raise BoardCatalogError(
                f"{board_type}: authoritative bootloader policy requires one explicit range"
            )
        if bootloader_start is not None and bootloader_end is not None:
            if not (flash_start <= bootloader_start < bootloader_end <= flash_end):
                raise BoardCatalogError(f"{board_type}: bootloader range must be inside flash")
            if application_start < bootloader_end and bootloader_start < application_end:
                raise BoardCatalogError(
                    f"{board_type}: application and bootloader ranges must not overlap"
                )

        result[key] = CatalogBoard(
            board_type=board_type,
            mcu_names=_string_tuple(raw, "mcu_names", required=True),
            package_part_number=_required_string(raw, "package_part_number"),
            pyocd_target=_required_string(raw, "pyocd_target"),
            probe_family=_required_string(raw, "probe_family"),
            default_baudrate=_required_int(raw, "default_baudrate"),
            test_read_address=_required_int(raw, "test_read_address"),
            silicon_id_address=_optional_int(raw, "silicon_id_address"),
            silicon_id_expected=_optional_int(raw, "silicon_id_expected"),
            silicon_id_mask=_required_int(raw, "silicon_id_mask"),
            silicon_id_width_bits=_required_int(raw, "silicon_id_width_bits"),
            silicon_id_label=_required_string(raw, "silicon_id_label"),
            silicon_id_limitation=_required_string(raw, "silicon_id_limitation"),
            flash_start=flash_start,
            flash_end=flash_end,
            ram_start=ram_start,
            ram_end=ram_end,
            erase_size=erase_size,
            application_start=application_start,
            application_end=application_end,
            application_partition_authoritative=application_authoritative,
            bootloader_start=bootloader_start,
            bootloader_end=bootloader_end,
            bootloader_partition_authoritative=bootloader_authoritative,
            no_protected_resident_bootloader=no_resident_bootloader,
            hardware_regions=tuple(regions),
            datasheet_sha256=_string_tuple(raw, "datasheet_sha256"),
            document_revision=_required_string(raw, "document_revision"),
            zephyr_board_target=_optional_string(raw, "zephyr_board_target"),
            device_support_evidence_resource=_optional_string(
                raw, "device_support_evidence_resource"
            ),
            device_support_evidence_sha256=_optional_string(raw, "device_support_evidence_sha256"),
            official_evidence_resource=_optional_string(raw, "official_evidence_resource"),
            official_evidence_sha256=_optional_string(raw, "official_evidence_sha256"),
            pyocd_version=_optional_string(raw, "pyocd_version"),
            pyocd_target_module=_optional_string(raw, "pyocd_target_module"),
            pyocd_target_module_sha256=_optional_string(raw, "pyocd_target_module_sha256"),
            pyocd_svd_bundle_sha256=_optional_string(raw, "pyocd_svd_bundle_sha256"),
            pyocd_pack_filename=_optional_string(raw, "pyocd_pack_filename"),
            pyocd_pack_sha256=_optional_string(raw, "pyocd_pack_sha256"),
            debug_connect_mode=_optional_string(raw, "debug_connect_mode"),
            debug_clock_hz=_optional_int(raw, "debug_clock_hz"),
        )
    return result


_configured_catalog = os.environ.get("PYOCD_REVIEWED_BOARD_CATALOG", "").strip()
_CATALOG = (
    _load_catalog(Path(_configured_catalog).expanduser().resolve())
    if _configured_catalog
    else {}
)


@dataclass(frozen=True, slots=True)
class ResolvedReviewedSupport:
    """Internal setup support selected without a caller-owned catalog identifier."""

    catalog: CatalogBoard
    datasheet_path: Path
    datasheet_sha256: str


def hash_local_datasheet(path: Path) -> tuple[Path, str]:
    """Resolve, validate, and hash a bounded local PDF from its actual bytes."""

    expanded = path.expanduser()
    if expanded.is_symlink():
        raise BoardCatalogError("datasheet_path must not be a symbolic link")
    try:
        resolved = expanded.resolve(strict=True)
    except OSError as exc:
        raise BoardCatalogError("datasheet_path must name an existing local PDF") from exc
    if resolved.suffix.casefold() != ".pdf" or not resolved.is_file():
        raise BoardCatalogError("datasheet_path must name an existing local PDF")
    size = resolved.stat().st_size
    if not 5 <= size <= 64 * 1024 * 1024:
        raise BoardCatalogError("datasheet PDF size must be between 5 bytes and 64 MiB")
    digest = sha256()
    with resolved.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise BoardCatalogError("datasheet_path does not contain a PDF document")
        stream.seek(0)
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return resolved, digest.hexdigest()


def resolve_reviewed_support(
    mcu_part_number: str,
    datasheet_sha256: str,
    *,
    candidates: tuple[CatalogBoard, ...] | None = None,
) -> CatalogBoard:
    """Resolve exactly one complete reviewed record from server-owned evidence."""

    records = candidates if candidates is not None else tuple(_CATALOG.values())
    matches = tuple(
        record
        for record in records
        if record.automatic_setup_reviewed
        and record.package_part_number == mcu_part_number
        and datasheet_sha256 in record.datasheet_sha256
    )
    if not matches:
        raise ReviewedSupportNotFoundError(
            "No reviewed support accepts the exact MCU part number and server-computed "
            "datasheet digest."
        )
    if len(matches) != 1:
        raise ReviewedSupportAmbiguityError(
            "The exact MCU part number and server-computed datasheet digest match multiple "
            "reviewed support records; setup remains unavailable until the catalog is unambiguous."
        )
    return matches[0]


def resolve_reviewed_support_from_datasheet(
    mcu_part_number: str,
    datasheet_path: Path,
    *,
    candidates: tuple[CatalogBoard, ...] | None = None,
) -> ResolvedReviewedSupport:
    """Hash a local datasheet and resolve one internal reviewed support record."""

    resolved_path, digest = hash_local_datasheet(datasheet_path)
    catalog = resolve_reviewed_support(
        mcu_part_number,
        digest,
        candidates=candidates,
    )
    return ResolvedReviewedSupport(catalog, resolved_path, digest)


def catalog_board(board_type: str) -> CatalogBoard:
    """Return one reviewed catalog entry by its internal repository identifier."""

    key = board_type.strip().casefold()
    try:
        return _CATALOG[key]
    except KeyError as exc:
        raise BoardCatalogError(
            f"Unsupported board type '{board_type}'. Setup requires reviewed board evidence."
        ) from exc
