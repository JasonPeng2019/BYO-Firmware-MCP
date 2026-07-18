"""Reviewed board identities and deployment envelopes used by fresh setup.

These records are server-owned evidence.  They deliberately contain no probe
assignment, gate, plan, or permission state and callers cannot supply memory
ranges through the MCP schema.
"""

from __future__ import annotations

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


_CATALOG = {
    "nrf52840dk": CatalogBoard(
        board_type="nrf52840dk",
        mcu_names=("nRF52840", "nRF52840-QIAA"),
        package_part_number="nRF52840-QIAA",
        pyocd_target="nrf52840",
        probe_family="jlink",
        default_baudrate=115200,
        test_read_address=0x10000000,
        silicon_id_address=0x10000100,
        silicon_id_expected=0x00052840,
        silicon_id_mask=0xFFFFFFFF,
        flash_start=0x00000000,
        flash_end=0x00100000,
        ram_start=0x20000000,
        ram_end=0x20040000,
        erase_size=0x1000,
        application_start=0x00000000,
        application_end=0x00100000,
        hardware_regions=(
            CatalogHardwareRegion(
                "UICR and persistent configuration", RegionKind.PROHIBITED, 0x10001000, 0x10002000
            ),
            CatalogHardwareRegion("factory information", RegionKind.ROM, 0x10000000, 0x10001000),
            CatalogHardwareRegion(
                "volatile GPIO registers", RegionKind.PERIPHERAL, 0x50000500, 0x50000900
            ),
            CatalogHardwareRegion(
                "nonvolatile memory and access control registers",
                RegionKind.PROHIBITED,
                0x4001E000,
                0x4001F000,
            ),
            CatalogHardwareRegion(
                "Cortex-M system control", RegionKind.CPU_SYSTEM, 0xE0000000, 0xE0100000
            ),
        ),
        datasheet_sha256=("c619e336b9c0610663273041f057f2537a65fd408ce0c5b8214a26de2aa88422",),
        document_revision="nRF52840 PS v1.1 plus nRF52840 DK reviewed deployment policy v1",
        zephyr_board_target="nrf52840dk/nrf52840",
        device_support_evidence_resource="evidence/nrf52840_device_support.json",
        device_support_evidence_sha256="8ffa48e16fb03d491660c93faaa1a34f9654873e7c27b7113cc23216fe47a8e7",
        official_evidence_resource="evidence/nrf52840_official_document.json",
        official_evidence_sha256="c159d4eec5af036fcca72c06ea620db370750b88a086de965040235317f74daa",
        pyocd_version="0.45.0",
        pyocd_target_module="pyocd.target.builtin.target_nRF52840_xxAA",
        pyocd_target_module_sha256="c4713dea41facd880a92eb3b4a12924305290b36753e1fe592a463b856b9b29f",
        pyocd_svd_bundle_sha256="e452ca593edadbb0d6f960c19f761977e35e2a33e027d7b84fbd4e82b2608d8c",
    ),
    "nrf52833dk": CatalogBoard(
        board_type="nrf52833dk",
        mcu_names=("nRF52833", "nRF52833-QIAA"),
        package_part_number="nRF52833-QIAA",
        pyocd_target="nrf52833",
        probe_family="jlink",
        default_baudrate=115200,
        test_read_address=0x10000000,
        silicon_id_address=0x10000100,
        silicon_id_expected=0x00052833,
        silicon_id_mask=0xFFFFFFFF,
        flash_start=0x00000000,
        flash_end=0x00080000,
        ram_start=0x20000000,
        ram_end=0x20020000,
        erase_size=0x1000,
        application_start=0x00000000,
        application_end=0x00080000,
        hardware_regions=(
            CatalogHardwareRegion(
                "UICR and persistent configuration", RegionKind.PROHIBITED, 0x10001000, 0x10002000
            ),
            CatalogHardwareRegion("factory information", RegionKind.ROM, 0x10000000, 0x10001000),
            CatalogHardwareRegion(
                "peripheral registers", RegionKind.PERIPHERAL, 0x40000000, 0x60000000
            ),
            CatalogHardwareRegion(
                "Cortex-M system control", RegionKind.CPU_SYSTEM, 0xE0000000, 0xE0100000
            ),
        ),
        zephyr_board_target="nrf52833dk/nrf52833",
    ),
    "nucleo_l476rg": CatalogBoard(
        board_type="nucleo_l476rg",
        mcu_names=("STM32L476RG", "STM32L476RGT6"),
        package_part_number="STM32L476RGT6",
        pyocd_target="stm32l476rgtx",
        probe_family="stlink",
        default_baudrate=115200,
        test_read_address=0x08000000,
        silicon_id_address=None,
        silicon_id_expected=None,
        silicon_id_mask=0xFFFFFFFF,
        flash_start=0x08000000,
        flash_end=0x08100000,
        ram_start=0x20000000,
        ram_end=0x20018000,
        erase_size=0x800,
        application_start=0x08000000,
        application_end=0x08100000,
        hardware_regions=(
            CatalogHardwareRegion(
                "system-memory ROM bootloader", RegionKind.ROM_BOOTLOADER, 0x1FFF0000, 0x1FFF7000
            ),
            CatalogHardwareRegion(
                "OTP and persistent configuration", RegionKind.PROHIBITED, 0x1FFF7000, 0x1FFF7800
            ),
            CatalogHardwareRegion("option bytes", RegionKind.PROHIBITED, 0x1FFF7800, 0x1FFF8000),
            CatalogHardwareRegion(
                "peripheral registers", RegionKind.PERIPHERAL, 0x40000000, 0x60000000
            ),
            CatalogHardwareRegion(
                "Cortex-M system control", RegionKind.CPU_SYSTEM, 0xE0000000, 0xE0100000
            ),
        ),
        zephyr_board_target="nucleo_l476rg",
    ),
}


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
