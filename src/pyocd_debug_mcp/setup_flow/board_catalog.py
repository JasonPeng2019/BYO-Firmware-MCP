"""Reviewed board identities and deployment envelopes used by fresh setup.

These records are server-owned evidence.  They deliberately contain no probe
assignment, gate, plan, or permission state and callers cannot supply memory
ranges through the MCP schema.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from pyocd_debug_mcp.safety.regions import RegionKind


class BoardCatalogError(ValueError):
    """A requested supported-board identity does not match reviewed evidence."""


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
    flash_start: int
    flash_end: int
    ram_start: int
    ram_end: int
    erase_size: int
    application_start: int
    application_end: int
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
    debug_connect_mode: str | None = None
    debug_clock_hz: int | None = None

    def accepts_mcu(self, value: str) -> bool:
        normalized = value.strip().casefold().replace("-", "").replace("_", "")
        return any(
            normalized == item.casefold().replace("-", "").replace("_", "")
            for item in self.mcu_names
        )

    def validate_datasheet(self, path: Path, digest: str) -> None:
        if not self.datasheet_sha256:
            raise BoardCatalogError(
                f"No reviewed datasheet is configured for {self.board_type}; "
                "automatic setup remains unavailable."
            )
        if path.is_symlink():
            raise BoardCatalogError("datasheet_path must not be a symbolic link")
        try:
            resolved = path.expanduser().resolve(strict=True)
        except OSError as exc:
            raise BoardCatalogError("datasheet_path must name an existing local PDF") from exc
        if resolved.suffix.casefold() != ".pdf" or not resolved.is_file():
            raise BoardCatalogError("datasheet_path must name an existing local PDF")
        size = resolved.stat().st_size
        if not 5 <= size <= 64 * 1024 * 1024:
            raise BoardCatalogError("datasheet PDF size must be between 5 bytes and 64 MiB")
        with resolved.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise BoardCatalogError("datasheet_path does not contain a PDF document")
            stream.seek(0)
            actual = sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                actual.update(chunk)
        actual_digest = actual.hexdigest()
        normalized = digest.strip().casefold()
        if len(normalized) != 64 or any(ch not in "0123456789abcdef" for ch in normalized):
            raise BoardCatalogError("datasheet_sha256 must be exactly 64 hexadecimal characters")
        if normalized != actual_digest:
            raise BoardCatalogError("The supplied datasheet SHA-256 does not match the PDF bytes.")
        if normalized not in self.datasheet_sha256:
            raise BoardCatalogError(
                f"The supplied datasheet hash is not reviewed for {self.board_type}."
            )

    @property
    def automatic_setup_reviewed(self) -> bool:
        return bool(
            self.datasheet_sha256
            and self.device_support_evidence_resource
            and self.device_support_evidence_sha256
            and self.official_evidence_resource
            and self.official_evidence_sha256
            and self.pyocd_version
            and self.pyocd_target_module
            and self.pyocd_target_module_sha256
            and self.pyocd_svd_bundle_sha256
        )


_CATALOG_RESOURCE = Path(__file__).with_name("reviewed_boards.json")


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


def _load_catalog(path: Path = _CATALOG_RESOURCE) -> dict[str, CatalogBoard]:
    """Load reviewed board facts from the packaged, server-owned data document."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BoardCatalogError(f"Reviewed board catalog is unreadable: {path}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise BoardCatalogError("Reviewed board catalog must use schema_version 1")
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
        erase_size = _required_int(raw, "erase_size")
        if not (0 <= flash_start < flash_end and 0 <= ram_start < ram_end):
            raise BoardCatalogError(f"{board_type}: flash and RAM ranges must be non-empty")
        if not (flash_start <= application_start < application_end <= flash_end):
            raise BoardCatalogError(f"{board_type}: application range must be inside flash")
        if erase_size <= 0:
            raise BoardCatalogError(f"{board_type}: erase_size must be positive")

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
            flash_start=flash_start,
            flash_end=flash_end,
            ram_start=ram_start,
            ram_end=ram_end,
            erase_size=erase_size,
            application_start=application_start,
            application_end=application_end,
            hardware_regions=tuple(regions),
            datasheet_sha256=_string_tuple(raw, "datasheet_sha256"),
            document_revision=_required_string(raw, "document_revision"),
            zephyr_board_target=_optional_string(raw, "zephyr_board_target"),
            device_support_evidence_resource=_optional_string(
                raw, "device_support_evidence_resource"
            ),
            device_support_evidence_sha256=_optional_string(
                raw, "device_support_evidence_sha256"
            ),
            official_evidence_resource=_optional_string(raw, "official_evidence_resource"),
            official_evidence_sha256=_optional_string(raw, "official_evidence_sha256"),
            pyocd_version=_optional_string(raw, "pyocd_version"),
            pyocd_target_module=_optional_string(raw, "pyocd_target_module"),
            pyocd_target_module_sha256=_optional_string(raw, "pyocd_target_module_sha256"),
            pyocd_svd_bundle_sha256=_optional_string(raw, "pyocd_svd_bundle_sha256"),
            debug_connect_mode=_optional_string(raw, "debug_connect_mode"),
            debug_clock_hz=_optional_int(raw, "debug_clock_hz"),
        )
    return result


_CATALOG = _load_catalog()


def catalog_board(board_type: str) -> CatalogBoard:
    """Return one reviewed catalog entry by its exact public board type."""

    key = board_type.strip().casefold()
    try:
        return _CATALOG[key]
    except KeyError as exc:
        raise BoardCatalogError(
            f"Unsupported board type '{board_type}'. Setup requires reviewed board evidence."
        ) from exc


def catalog_board_types() -> tuple[str, ...]:
    return tuple(sorted(_CATALOG))


def reviewed_setup_board_types() -> tuple[str, ...]:
    """Return only identities with complete pinned two-source setup evidence."""

    return tuple(sorted(key for key, board in _CATALOG.items() if board.automatic_setup_reviewed))


def catalog_board_for_mcu(mcu_part_number: str) -> CatalogBoard | None:
    """Resolve one unambiguous reviewed catalog entry for non-authoritative build guidance."""

    matches = [board for board in _CATALOG.values() if board.accepts_mcu(mcu_part_number)]
    return matches[0] if len(matches) == 1 else None
