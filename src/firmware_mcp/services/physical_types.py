"""Neutral physical-region primitives shared by artifact and setup evidence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final


MAX_ADDRESS_EXCLUSIVE: Final = 1 << 64


class RegionError(ValueError):
    """A physical address range or provider-kind record is malformed."""


class RegionKind(str, Enum):
    """Diagnostic physical kinds only; access is decided by provider flags."""

    UNKNOWN = "unknown"
    PHYSICAL_FLASH = "physical_flash"
    PHYSICAL_RAM = "physical_ram"
    ROM = "rom"
    PERIPHERAL = "peripheral"


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
    def from_start_size(cls, start: int, size: int) -> "AddressRange":
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise RegionError("range size must be a positive integer")
        return cls(start, start + size)

    @property
    def size(self) -> int:
        return self.end - self.start

    def to_document(self) -> dict[str, int]:
        return {"start": self.start, "end": self.end}
