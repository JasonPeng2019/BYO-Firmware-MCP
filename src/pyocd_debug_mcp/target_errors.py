"""Typed shared errors for SWD, UART, and symbol-resolution flows."""

from __future__ import annotations


class TargetControlError(RuntimeError):
    """Base class for shared target-control failures."""


class ProbeNotFoundError(TargetControlError):
    """Raised when no matching debug probe can be opened."""


class TargetConnectionError(TargetControlError):
    """Raised when a probe is visible but target access still fails."""


class LockedTargetError(TargetConnectionError):
    """Raised when the target appears locked or access-protected."""


class UnsupportedArtifactError(TargetControlError):
    """Raised when a flash artifact type is not supported."""


class SymbolLookupError(TargetControlError):
    """Raised when a required symbol cannot be resolved from an ELF."""


class ReferenceArtifactError(TargetControlError):
    """Raised when the canonical reference artifacts cannot be resolved."""
