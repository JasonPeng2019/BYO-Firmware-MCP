"""Typed shared errors for SWD, UART, and symbol-resolution flows."""

from __future__ import annotations


class TargetControlError(RuntimeError):
    """Base class for shared target-control failures."""


class TargetInputError(TargetControlError):
    """Invalid operation arguments refused before backend access; session remains live."""


class ProbeNotFoundError(TargetControlError):
    """Raised when no matching debug probe can be opened."""


class TargetConnectionError(TargetControlError):
    """Raised when a probe is visible but target access still fails."""


class TargetStateError(TargetControlError):
    """Raised when a live target refuses an operation in its current state."""


class LockedTargetError(TargetConnectionError):
    """Raised when the target appears locked or access-protected."""


class ResetLineUnavailableError(TargetConnectionError):
    """Raised when connect-under-reset cannot control a physical reset line."""


class UnsupportedArtifactError(TargetControlError):
    """Raised when a flash artifact type is not supported."""


class FlashArtifactChangedError(UnsupportedArtifactError):
    """Raised before any reset or programming when the artifact bytes no longer
    match the digest approved for this flash."""


class FlashTargetSupportError(TargetControlError):
    """Raised before any reset or programming when the live target cannot program
    every addressed artifact byte with a flash algorithm."""


class FlashVerificationError(TargetControlError):
    """Raised when programmed flash bytes cannot be read back or do not match the
    artifact; programming is then unverified rather than successful."""


class FlashReadbackConnectionError(FlashVerificationError, TargetConnectionError):
    """Unverified programming caused by typed native readback transport loss.

    Keep verification reporting and captured-connection cleanup compatible across
    the worker boundary without serializing the native exception chain.
    """


class SymbolLookupError(TargetControlError):
    """Raised when a required symbol cannot be resolved from an ELF."""


class ReferenceArtifactError(TargetControlError):
    """Raised when the canonical reference artifacts cannot be resolved."""
