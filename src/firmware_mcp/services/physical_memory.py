"""Live-provider physical-memory evidence for direct target operations.

This module deliberately has no persisted map, ownership model, or role names.
Every request obtains regions from the current provider session and rechecks
the capability-aware live identity observation bound to that session before it
classifies an address.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from firmware_mcp.adapters.debug_interface import PhysicalMemoryRegion, TargetSessionHandle
from firmware_mcp.target_errors import TargetControlError
from firmware_mcp.services.live_identity import LiveIdentityObservation, observe_live_identity


class PhysicalMemoryFactsUnavailable(TargetControlError):
    """The current provider cannot establish physical-memory facts."""


class PhysicalMemoryAccessError(TargetControlError):
    """A requested span is not fully covered with the requested provider access."""


@dataclass(frozen=True, slots=True)
class PhysicalSpanEvidence:
    """Invocation-scoped proof of one completely covered physical span."""

    start: int
    end: int
    access: str
    regions: tuple[PhysicalMemoryRegion, ...]

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(region.kind for region in self.regions)


def _validate_regions(
    regions: tuple[PhysicalMemoryRegion, ...], session_token: str
) -> tuple[PhysicalMemoryRegion, ...]:
    if not regions:
        raise PhysicalMemoryFactsUnavailable(
            "Live provider memory-map facts are unavailable. Reconnect and validate the target."
        )
    ordered = tuple(sorted(regions, key=lambda region: (region.start, region.end)))
    previous_end = -1
    for region in ordered:
        try:
            # Re-parse even in-process records so malformed adapter facts cannot become authority.
            checked = PhysicalMemoryRegion.from_record(region.to_record())
        except ValueError as exc:
            raise PhysicalMemoryFactsUnavailable(
                f"Live provider memory-map facts are malformed: {exc}. Reconnect and validate the target."
            ) from exc
        if checked.session_token != session_token:
            raise PhysicalMemoryFactsUnavailable(
                "Live provider memory-map facts belong to another target session. Reconnect and retry."
            )
        if checked.start < previous_end:
            raise PhysicalMemoryFactsUnavailable(
                "Live provider memory-map regions overlap ambiguously. Reconnect and validate the target."
            )
        previous_end = checked.end
    return ordered


def observe_identity(
    handle: TargetSessionHandle,
    read_memory: Callable[[TargetSessionHandle, int, int], int],
) -> LiveIdentityObservation:
    """Observe identity without promoting compatible/unavailable evidence to exact."""

    return observe_live_identity(handle, read_memory=read_memory)


def require_live_identity(
    handle: TargetSessionHandle,
    *,
    read_memory: Callable[[TargetSessionHandle, int, int], int],
) -> LiveIdentityObservation:
    """Re-read the configured proof and return its honest capability."""

    return observe_identity(handle, read_memory)


def require_live_physical_access(
    handle: TargetSessionHandle,
    start: int,
    length: int,
    access: str,
    *,
    regions_for: Callable[[TargetSessionHandle], tuple[PhysicalMemoryRegion, ...]],
    read_memory: Callable[[TargetSessionHandle, int, int], int],
) -> PhysicalSpanEvidence:
    """Return current-session evidence that the complete span has ``access``.

    Adjacent compatible regions are valid.  Gaps, wrong access, malformed maps,
    and stale session records are all explicit failures before target mutation.
    """

    if isinstance(start, bool) or not isinstance(start, int) or start < 0:
        raise PhysicalMemoryAccessError("Physical-memory address must be a non-negative integer.")
    if isinstance(length, bool) or not isinstance(length, int) or length <= 0:
        raise PhysicalMemoryAccessError("Physical-memory span length must be a positive integer.")
    end = start + length
    if end > 1 << 64:
        raise PhysicalMemoryAccessError(
            "Physical-memory span exceeds the unsigned 64-bit address space."
        )
    if access not in {"read", "write", "execute"}:
        raise ValueError("physical access must be read, write, or execute")

    observe_identity(handle, read_memory)
    metadata = handle.metadata
    if metadata is None:
        raise PhysicalMemoryFactsUnavailable(
            "Live target session has no immutable identity token. Reconnect and retry."
        )
    regions = _validate_regions(regions_for(handle), metadata.runtime_token)
    flag = {"read": "readable", "write": "writable", "execute": "executable"}[access]
    cursor = start
    covered: list[PhysicalMemoryRegion] = []
    for region in regions:
        if region.end <= cursor:
            continue
        if region.start > cursor:
            break
        if not getattr(region, flag):
            raise PhysicalMemoryAccessError(
                f"Live provider region '{region.name}' at 0x{cursor:016X} is not {access}able."
            )
        covered.append(region)
        cursor = min(end, region.end)
        if cursor == end:
            return PhysicalSpanEvidence(start, end, access, tuple(covered))
    raise PhysicalMemoryAccessError(
        f"Live provider facts do not fully cover 0x{cursor:016X} in requested "
        f"range 0x{start:016X}-0x{end:016X} for {access} access."
    )
