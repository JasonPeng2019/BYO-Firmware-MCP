"""Typed half-open memory regions and fail-closed action containment."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Final

MAX_ADDRESS_EXCLUSIVE: Final = 1 << 64


class RegionError(ValueError):
    """A region or classification request violates the safety-map contract."""


class RegionKind(str, Enum):
    UNKNOWN = "unknown"
    PROHIBITED = "prohibited"
    APPLICATION_FLASH = "application_flash"
    BOOTLOADER_FLASH = "bootloader_flash"
    RAM = "ram"
    PERIPHERAL = "peripheral"
    CPU_SYSTEM = "cpu_system"
    ROM_BOOTLOADER = "rom_bootloader"
    PHYSICAL_FLASH = "physical_flash"
    PHYSICAL_RAM = "physical_ram"
    ROM = "rom"


class ActionCategory(str, Enum):
    MEMORY_READ = "memory_read"
    MEMORY_WRITE = "memory_write"
    REGISTER_WRITE = "register_write"
    FLASH_APPLICATION = "flash_application"
    FLASH_BOOTLOADER = "flash_bootloader"
    BREAKPOINT = "breakpoint"


class SourceAuthority(str, Enum):
    BUILD = "build"
    DEVICE_SUPPORT = "device_support"
    OFFICIAL_DOCUMENT = "official_document"
    RECONCILED = "reconciled"
    DERIVED = "derived"


@dataclass(frozen=True, slots=True, order=True)
class AddressRange:
    """A non-empty unsigned 64-bit half-open interval ``[start, end)``."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if isinstance(self.start, bool) or isinstance(self.end, bool):
            raise RegionError("range endpoints must be integers, not booleans")
        if not isinstance(self.start, int) or not isinstance(self.end, int):
            raise RegionError("range endpoints must be integers")
        if not 0 <= self.start < self.end <= MAX_ADDRESS_EXCLUSIVE:
            raise RegionError("range must be non-empty and satisfy 0 <= start < end <= 2**64")

    @classmethod
    def from_start_size(cls, start: int, size: int) -> AddressRange:
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise RegionError("range size must be a positive integer")
        return cls(start, start + size)

    @property
    def size(self) -> int:
        return self.end - self.start

    def contains_address(self, address: int) -> bool:
        return self.start <= address < self.end

    def contains(self, other: AddressRange) -> bool:
        return self.start <= other.start and other.end <= self.end

    def overlaps(self, other: AddressRange) -> bool:
        return self.start < other.end and other.start < self.end

    def intersection(self, other: AddressRange) -> AddressRange | None:
        start = max(self.start, other.start)
        end = min(self.end, other.end)
        return AddressRange(start, end) if start < end else None

    def to_document(self) -> dict[str, int]:
        return {"start": self.start, "end": self.end}


@dataclass(frozen=True, slots=True)
class Provenance:
    authority: SourceAuthority
    source_id: str
    detail: str

    def __post_init__(self) -> None:
        if not self.source_id.strip() or not self.detail.strip():
            raise RegionError("region provenance requires non-empty source_id and detail")

    def to_document(self) -> dict[str, str]:
        return {
            "authority": self.authority.value,
            "source_id": self.source_id,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class SafetyRegion:
    name: str
    kind: RegionKind
    address_range: AddressRange
    provenance: tuple[Provenance, ...]
    executable: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise RegionError("region name must be non-empty")
        if self.kind is RegionKind.UNKNOWN:
            raise RegionError("UNKNOWN is a classification result, not a persisted region kind")
        if not self.provenance:
            raise RegionError("every region requires explicit provenance")
        canonical_provenance = tuple(
            sorted(
                set(self.provenance),
                key=lambda item: (item.authority.value, item.source_id, item.detail),
            )
        )
        object.__setattr__(self, "provenance", canonical_provenance)
        if self.executable and self.kind not in {
            RegionKind.APPLICATION_FLASH,
            RegionKind.BOOTLOADER_FLASH,
            RegionKind.ROM,
        }:
            raise RegionError("only application, bootloader, or ROM regions may be executable")

    def to_document(self) -> dict[str, object]:
        """Return a stable, persistence-ready record including usable provenance."""

        return {
            "name": self.name,
            "kind": self.kind.value,
            **self.address_range.to_document(),
            "executable": self.executable,
            "provenance": [item.to_document() for item in self.provenance],
        }


@dataclass(frozen=True, slots=True)
class Allowed:
    action: ActionCategory
    ranges: tuple[AddressRange, ...]
    classifications: tuple[RegionKind, ...]


@dataclass(frozen=True, slots=True)
class Refusal:
    action: ActionCategory
    code: str
    reason: str
    offending_range: AddressRange
    classification: RegionKind
    remedy: str


CheckResult = Allowed | Refusal


_SPECIFICITY: Final[dict[RegionKind, int]] = {
    RegionKind.PROHIBITED: 100,
    RegionKind.APPLICATION_FLASH: 50,
    RegionKind.BOOTLOADER_FLASH: 50,
    RegionKind.RAM: 50,
    RegionKind.PERIPHERAL: 50,
    RegionKind.CPU_SYSTEM: 50,
    RegionKind.ROM_BOOTLOADER: 50,
    RegionKind.PHYSICAL_FLASH: 10,
    RegionKind.PHYSICAL_RAM: 10,
    RegionKind.ROM: 10,
    RegionKind.UNKNOWN: 0,
}

_ALLOWED_KINDS: Final[dict[ActionCategory, frozenset[RegionKind]]] = {
    ActionCategory.MEMORY_READ: frozenset(RegionKind)
    - {
        RegionKind.UNKNOWN,
        RegionKind.PROHIBITED,
    },
    ActionCategory.MEMORY_WRITE: frozenset({RegionKind.RAM}),
    ActionCategory.REGISTER_WRITE: frozenset({RegionKind.PERIPHERAL}),
    ActionCategory.FLASH_APPLICATION: frozenset({RegionKind.APPLICATION_FLASH}),
    ActionCategory.FLASH_BOOTLOADER: frozenset({RegionKind.BOOTLOADER_FLASH}),
    ActionCategory.BREAKPOINT: frozenset(
        {RegionKind.APPLICATION_FLASH, RegionKind.BOOTLOADER_FLASH, RegionKind.ROM}
    ),
}


def _covered(target: AddressRange, ranges: list[AddressRange]) -> bool:
    """Return whether the union of ranges fully covers target without a gap."""

    cursor = target.start
    for current in sorted(ranges):
        if current.end <= cursor or current.start >= target.end:
            continue
        if current.start > cursor:
            return False
        cursor = max(cursor, current.end)
        if cursor >= target.end:
            return True
    return False


class SafetyMap:
    """Immutable server-owned region collection used for every address decision."""

    def __init__(self, regions: tuple[SafetyRegion, ...] | list[SafetyRegion]) -> None:
        self._regions = tuple(
            sorted(
                regions,
                key=lambda region: (
                    region.address_range.start,
                    region.address_range.end,
                    region.kind.value,
                    region.name,
                    region.executable,
                    tuple(
                        (item.authority.value, item.source_id, item.detail)
                        for item in region.provenance
                    ),
                ),
            )
        )
        if not self._regions:
            raise RegionError("a safety map requires at least one region")

    @property
    def regions(self) -> tuple[SafetyRegion, ...]:
        return self._regions

    def classify(self, requested: AddressRange) -> RegionKind:
        overlapping = [
            region for region in self._regions if region.address_range.overlaps(requested)
        ]
        if any(region.kind is RegionKind.PROHIBITED for region in overlapping):
            return RegionKind.PROHIBITED

        covering_kinds: list[RegionKind] = []
        for kind in RegionKind:
            if kind in {RegionKind.UNKNOWN, RegionKind.PROHIBITED}:
                continue
            ranges = [region.address_range for region in overlapping if region.kind is kind]
            if _covered(requested, ranges):
                covering_kinds.append(kind)
        if not covering_kinds:
            return RegionKind.UNKNOWN
        highest = max(_SPECIFICITY[kind] for kind in covering_kinds)
        most_specific = [kind for kind in covering_kinds if _SPECIFICITY[kind] == highest]
        return most_specific[0] if len(most_specific) == 1 else RegionKind.UNKNOWN

    def _executable_contains(self, requested: AddressRange) -> bool:
        executable_ranges = [
            region.address_range
            for region in self._regions
            if region.executable and region.kind is not RegionKind.PROHIBITED
        ]
        return _covered(requested, executable_ranges)

    def check(
        self,
        action: ActionCategory,
        ranges: tuple[AddressRange, ...] | list[AddressRange],
    ) -> CheckResult:
        requested_ranges = tuple(sorted(set(ranges)))
        if not requested_ranges:
            raise RegionError("an action check requires at least one range")
        classifications: list[RegionKind] = []
        for requested in requested_ranges:
            classification = self.classify(requested)
            classifications.append(classification)
            if classification is RegionKind.PROHIBITED:
                return Refusal(
                    action,
                    "safety/prohibited",
                    "The complete request touches a prohibited security or provisioning region.",
                    requested,
                    classification,
                    "Choose an operation that does not touch prohibited memory.",
                )
            if classification is RegionKind.UNKNOWN:
                return Refusal(
                    action,
                    "safety/unknown",
                    "The complete request is not covered by one authoritative mapped region.",
                    requested,
                    classification,
                    "Complete or refresh board safety setup before retrying.",
                )
            if classification not in _ALLOWED_KINDS[action]:
                return Refusal(
                    action,
                    "safety/wrong-region-kind",
                    f"{action.value} is unavailable in a {classification.value} region.",
                    requested,
                    classification,
                    "Use the operation associated with the mapped region kind.",
                )
            if action is ActionCategory.BREAKPOINT and not self._executable_contains(requested):
                return Refusal(
                    action,
                    "safety/not-executable",
                    "The complete breakpoint range is not build-derived executable memory.",
                    requested,
                    classification,
                    "Select a symbol or address inside a loadable executable segment.",
                )
        return Allowed(action, requested_ranges, tuple(classifications))

    def partition_prohibited_conflicts(self) -> tuple[tuple[SafetyRegion, SafetyRegion], ...]:
        partitions = [
            region
            for region in self._regions
            if region.kind
            in {
                RegionKind.APPLICATION_FLASH,
                RegionKind.BOOTLOADER_FLASH,
                RegionKind.RAM,
            }
        ]
        prohibited = [region for region in self._regions if region.kind is RegionKind.PROHIBITED]
        return tuple(
            (partition, blocked)
            for partition in partitions
            for blocked in prohibited
            if partition.address_range.overlaps(blocked.address_range)
        )


@dataclass(frozen=True, slots=True)
class RecoveryEraseSpan:
    """One map-derived nonvolatile range plus its applicable erase geometry."""

    address_range: AddressRange
    bank: str
    first_sector: int
    last_sector: int

    def to_document(self) -> dict[str, object]:
        return {
            **self.address_range.to_document(),
            "bank": self.bank,
            "first_sector": self.first_sector,
            "last_sector": self.last_sector,
            "sectors": list(range(self.first_sector, self.last_sector + 1)),
        }


@dataclass(frozen=True, slots=True)
class RecoveryEraseDisclosure:
    """Complete conservative disclosure for a typed destructive recovery primitive."""

    spans: tuple[RecoveryEraseSpan, ...]
    affected_regions: tuple[SafetyRegion, ...]
    all_nonvolatile_erased: bool
    expected_losses: tuple[str, ...]

    def to_document(self) -> dict[str, object]:
        return {
            "erased_ranges": [item.to_document() for item in self.spans],
            "affected_regions": [item.to_document() for item in self.affected_regions],
            "all_nonvolatile_erased": self.all_nonvolatile_erased,
            "expected_losses": list(self.expected_losses),
        }


def _uniform_span(
    requested: AddressRange,
    geometry: Mapping[str, object],
    bank: str,
) -> RecoveryEraseSpan:
    erase_size = geometry.get("erase_size")
    origin = geometry.get("erase_origin")
    if (
        isinstance(erase_size, bool)
        or not isinstance(erase_size, int)
        or erase_size <= 0
        or isinstance(origin, bool)
        or not isinstance(origin, int)
        or origin < 0
        or requested.start < origin
        or (requested.start - origin) % erase_size
        or (requested.end - origin) % erase_size
    ):
        raise RegionError(
            "recovery disclosure requires complete aligned erase geometry for every flash range"
        )
    return RecoveryEraseSpan(
        requested,
        bank,
        (requested.start - origin) // erase_size,
        (requested.end - origin) // erase_size - 1,
    )


def _explicit_span(
    requested: AddressRange,
    rows: list[object],
) -> RecoveryEraseSpan:
    sectors: list[tuple[int, AddressRange, str]] = []
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise RegionError("explicit recovery erase sectors must be objects")
        try:
            sector = AddressRange(raw["start"], raw["end"])  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError) as exc:
            raise RegionError(f"invalid recovery erase sector: {exc}") from exc
        bank = str(raw.get("bank", "unspecified-bank")).strip() or "unspecified-bank"
        sectors.append((index, sector, bank))
    covering = [item for item in sectors if item[1].overlaps(requested)]
    if not covering or not _covered(requested, [item[1] for item in covering]):
        raise RegionError("explicit erase geometry does not cover a physical flash range")
    if any(not requested.contains(item[1]) for item in covering):
        raise RegionError("an erase sector extends outside the physical flash range")
    banks = sorted({item[2] for item in covering})
    return RecoveryEraseSpan(
        requested,
        ", ".join(banks),
        min(item[0] for item in covering),
        max(item[0] for item in covering),
    )


def build_recovery_erase_disclosure(
    regions: tuple[SafetyRegion, ...] | list[SafetyRegion],
    geometry: Mapping[str, object],
    *,
    mass_erase: bool,
) -> RecoveryEraseDisclosure:
    """Derive recovery loss from the map; callers cannot inject allowed ranges."""

    mapped = tuple(regions)
    physical_flash = tuple(
        region for region in mapped if region.kind is RegionKind.PHYSICAL_FLASH
    )
    if not physical_flash:
        raise RegionError("the current safety map has no physical flash range to disclose")
    explicit = geometry.get("sectors")
    spans = tuple(
        _explicit_span(region.address_range, explicit)
        if isinstance(explicit, list)
        else _uniform_span(region.address_range, geometry, region.name)
        for region in physical_flash
    )
    affected = tuple(
        sorted(
            {
                region
                for region in mapped
                if region.kind
                in {
                    RegionKind.PHYSICAL_FLASH,
                    RegionKind.APPLICATION_FLASH,
                    RegionKind.BOOTLOADER_FLASH,
                    RegionKind.PROHIBITED,
                }
                and (
                    region.kind is RegionKind.PROHIBITED
                    or any(
                        flash.address_range.overlaps(region.address_range)
                        for flash in physical_flash
                    )
                )
            },
            key=lambda item: (
                item.address_range.start,
                item.address_range.end,
                item.kind.value,
                item.name,
            ),
        )
    )
    kinds = {item.kind for item in affected}
    losses = ["all user data in addressable nonvolatile memory"]
    if mass_erase or RegionKind.APPLICATION_FLASH in kinds:
        losses.append("application firmware")
    if mass_erase or RegionKind.BOOTLOADER_FLASH in kinds:
        losses.append("user bootloader firmware")
    if mass_erase or RegionKind.PROHIBITED in kinds:
        losses.append("nonvolatile configuration, protection, provisioning, and user settings")
    return RecoveryEraseDisclosure(
        spans,
        affected,
        mass_erase,
        tuple(losses),
    )
