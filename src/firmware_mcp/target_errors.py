"""Typed shared errors for SWD, UART, and symbol-resolution flows."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from firmware_mcp.adapters.debug_interface import (
        FlashVerification,
        RecoveryCapability,
        RecoveryResult,
    )


class TargetControlError(RuntimeError):
    """Base class for shared target-control failures."""


class ProbeNotFoundError(TargetControlError):
    """Raised when no matching debug probe can be opened."""


class TargetConnectionError(TargetControlError):
    """Raised when a probe is visible but target access still fails."""


@dataclass(frozen=True)
class CleanupDiagnostic:
    """One provider-neutral cleanup fact that could not be confirmed."""

    stage: str
    error_type: str
    error_message: str
    recovery: str

    def to_record(self) -> dict[str, str]:
        return {
            "stage": self.stage,
            "status": "unconfirmed",
            "error_type": self.error_type,
            "error_message": self.error_message,
            "recovery": self.recovery,
        }


class TargetConnectionCleanupError(TargetConnectionError):
    """A connection failure whose owned-provider cleanup is unconfirmed."""

    def __init__(
        self,
        primary_error_type: str,
        primary_error_message: str,
        cleanup_diagnostics: tuple[CleanupDiagnostic, ...],
    ) -> None:
        self.primary_error_type = primary_error_type
        self.primary_error_message = primary_error_message
        self.cleanup_diagnostics = cleanup_diagnostics
        diagnostics = "; ".join(
            f"{item.stage}=unconfirmed ({item.error_type}: {item.error_message}); "
            f"recovery: {item.recovery}"
            for item in cleanup_diagnostics
        )
        super().__init__(
            "Provider connection operation failed: "
            f"primary={primary_error_type}: {primary_error_message}; "
            f"cleanup uncertainty: {diagnostics}"
        )


class FlashFinalResetUncertain(TargetConnectionError):
    """Program/readback succeeded, but final reset could not be observed."""

    def __init__(self, evidence: "FlashVerification", cause: Exception) -> None:
        self.evidence = evidence
        self.cause = cause
        super().__init__(
            "Flash programming and byte readback verified, but final reset postcondition is "
            f"{evidence.final_reset_postcondition}: "
            f"{evidence.final_reset_error_type or type(cause).__name__}: "
            f"{evidence.final_reset_error_message or cause}. Reconnect before further target operations."
        )


class TargetStateError(TargetControlError):
    """Raised when a live target refuses an operation in its current state."""


class RecoveryPostDispatchError(TargetControlError):
    """Recovery was dispatched, but the returned provider fact is unusable.

    The selected descriptor is retained so the server can conservatively finish
    the exact managed session instead of guessing from a later capability read.
    """

    def __init__(
        self,
        selected_capability: "RecoveryCapability",
        result: "RecoveryResult | None",
        cause: BaseException,
    ) -> None:
        self.selected_capability = selected_capability
        self.result = result
        self.cause = cause
        super().__init__(
            "Recovery backend dispatch completed without a usable result for "
            f"mechanism '{selected_capability.mechanism}': "
            f"{type(cause).__name__}: {cause}"
        )


class RecoverySessionFinalizationError(TargetControlError):
    """A dispatched recovery could not prove the stale-session cleanup path."""

    def __init__(
        self,
        primary: BaseException | None,
        evidence: dict[str, object],
    ) -> None:
        self.primary = primary
        self.evidence = evidence
        selected = evidence.get("selected_capability")
        result = evidence.get("provider_result")
        cleanup = evidence.get("cleanup")
        selected_record = selected if isinstance(selected, Mapping) else {}
        result_record = result if isinstance(result, Mapping) else None
        cleanup_record = cleanup if isinstance(cleanup, Mapping) else {}
        if result_record is None:
            provider_text = "provider_result=unavailable"
        else:
            provider_text = (
                "provider_result=("
                f"mechanism={result_record.get('mechanism')!r}, "
                f"effect={selected_record.get('effect')!r}, "
                f"provider_accepted={result_record.get('accepted')!r}, "
                f"effect_verification={result_record.get('verification')!r}, "
                "observed_session_postcondition="
                f"{result_record.get('observed_session_postcondition')!r})"
            )
        cleanup_text = (
            "cleanup=("
            f"routing_removal={cleanup_record.get('routing_removal')!r}, "
            f"authority_invalidation={cleanup_record.get('authority_invalidation')!r}, "
            f"provider_close={cleanup_record.get('provider_close')!r}, "
            f"diagnostics={cleanup_record.get('diagnostics')!r})"
        )
        primary_text = (
            f"primary_finalization={type(primary).__name__}: {primary}"
            if primary is not None
            else "primary_finalization=none"
        )
        super().__init__(
            "Recovery dispatch requires reconnect and inspection. "
            f"{provider_text}; {primary_text}; {cleanup_text}. "
            "Reconnect, inspect the provider outcome, validate the board, and refresh_safety_map."
        )


class FlashFinalResetFailed(TargetStateError):
    """Program/readback succeeded, but a final reset state was observed and failed."""

    def __init__(self, evidence: "FlashVerification") -> None:
        self.evidence = evidence
        super().__init__(
            "Flash programming and byte readback verified, but final reset postcondition was observed "
            f"and failed: {evidence.final_reset_error_type}: "
            f"{evidence.final_reset_error_message}."
        )


class LockedTargetError(TargetConnectionError):
    """Raised when the target appears locked or access-protected."""


class ResetLineUnavailableError(TargetConnectionError):
    """Raised when connect-under-reset cannot control a physical reset line."""


class UnsupportedArtifactError(TargetControlError):
    """Raised when a flash artifact type is not supported."""


class SymbolLookupError(TargetControlError):
    """Raised when a required symbol cannot be resolved from an ELF."""


class ReferenceArtifactError(TargetControlError):
    """Raised when the canonical reference artifacts cannot be resolved."""
