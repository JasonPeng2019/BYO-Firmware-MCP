"""Provider-neutral observation of the identity facts available on a live session.

The configured profile/support route is replayed elsewhere.  This module only
answers the narrower runtime question: what (if anything) did the current
session prove about that configured route?  In particular, a compatible core
register is useful evidence but is never promoted to a part-number proof.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from firmware_mcp.adapters.debug_interface import TargetSessionHandle
from firmware_mcp.identity_observation import (
    IdentityObservationError,
    validate_identity_observation,
    validate_identity_register_width,
)
from firmware_mcp.setup_flow.device_support import normalize_part_number
from firmware_mcp.target_errors import TargetStateError


class LiveIdentityError(TargetStateError):
    """A configured live-identity observation could not establish safe agreement."""


class LiveIdentityContradiction(LiveIdentityError):
    """A current provider fact disagrees with replayed configured evidence."""

    code = "identity-contradiction"


class LiveIdentityObservationError(LiveIdentityError):
    """Configured current identity evidence could not be observed or validated."""

    code = "identity-observation/read-failed"


@dataclass(frozen=True, slots=True)
class LiveIdentityObservation:
    configured_part_number: str
    provider_id: str
    target: str
    provider_support_identity: str
    capability: str
    comparison_status: str
    exact_live_part_number: str | None
    evidence: dict[str, Any]

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


def _record_proof(board: Any, observed: int) -> dict[str, Any]:
    provenance = str(getattr(board, "silicon_id_provenance", "")).strip()
    # A compatible proof never authorizes an exact part.  It can nevertheless
    # be a reproducible observation when it was reconstructed from the
    # replayed support document (for example the canonical Arm CPUID proof).
    # Keep that source explicit rather than inventing a part-level authority.
    if not provenance:
        provenance = "replayed compatible support proof"
    return {
        "kind": "register",
        "address": int(getattr(board, "silicon_id_addr", 0)),
        "expected": int(getattr(board, "silicon_id_expected", 0)),
        "mask": int(getattr(board, "silicon_id_mask", 0)),
        "width_bits": int(getattr(board, "silicon_id_width_bits", 32)),
        "label": str(getattr(board, "silicon_id_label", "silicon identity")),
        "provenance": provenance,
        "masked_observed": observed & int(getattr(board, "silicon_id_mask", 0)),
    }


def _unavailable(board: Any, configured_part_number: str, reason: str) -> LiveIdentityObservation:
    return LiveIdentityObservation(
        configured_part_number=configured_part_number,
        provider_id=str(getattr(board, "provider_id", "")),
        target=str(getattr(board, "target", "")),
        provider_support_identity=str(getattr(board, "provider_support_identity", "")),
        capability="unavailable",
        comparison_status="unavailable",
        exact_live_part_number=None,
        evidence={"kind": "unavailable", "reason": reason},
    )


def observe_live_identity(
    handle: TargetSessionHandle,
    *,
    read_memory: Callable[[TargetSessionHandle, int, int], int] | None,
    configured_part_number: str | None = None,
) -> LiveIdentityObservation:
    """Return one canonical observation, or raise for a blocking identity failure.

    A missing proof is deliberately distinct from a proof that could not be
    reread: the former is ``unavailable``; the latter is a current-session
    failure and cannot become authority.
    """

    board = handle.board
    if board is None:
        raise LiveIdentityObservationError(
            "Live identity cannot be observed without a bound board profile."
        )
    configured = str(
        configured_part_number
        or getattr(board, "silicon_id_bound_part_number", "")
        or getattr(board, "target", "")
    ).strip()
    if not configured:
        return _unavailable(board, "", "configured profile part number is unavailable")

    metadata_identity = getattr(getattr(handle, "metadata", None), "live_identity", None)
    if str(getattr(board, "provider_id", "")) != "pyocd" and metadata_identity is not None:
        if not isinstance(metadata_identity, dict):
            raise LiveIdentityObservationError(
                "Provider live identity evidence is malformed for this session."
            )
        capability = metadata_identity.get("capability")
        if capability not in {"exact", "compatible", "unavailable"}:
            raise LiveIdentityObservationError(
                "Provider live identity capability is malformed for this session."
            )
        support = metadata_identity.get("support_identity")
        if support != getattr(board, "provider_support_identity", ""):
            raise LiveIdentityObservationError(
                "Provider live support identity is bound to a different replayed support route."
            )
        provenance = metadata_identity.get("provenance")
        evidence = metadata_identity.get("evidence")
        if (
            not isinstance(provenance, str)
            or not provenance.strip()
            or not isinstance(evidence, dict)
        ):
            raise LiveIdentityObservationError(
                "Provider live identity evidence is incomplete for this session."
            )
        part = metadata_identity.get("part_number")
        if capability == "exact":
            if not isinstance(part, str) or not part.strip():
                raise LiveIdentityObservationError(
                    "Exact provider live identity has no observed part number."
                )
            if normalize_part_number(part) != normalize_part_number(configured):
                raise LiveIdentityContradiction(
                    "Exact provider live identity contradicts the configured MCU part."
                )
            return LiveIdentityObservation(
                configured,
                str(getattr(board, "provider_id", "pyocd")),
                str(getattr(board, "target", "")),
                str(getattr(board, "provider_support_identity", "")),
                "exact",
                "matched",
                part,
                {
                    "kind": "provider",
                    "provenance": provenance,
                    "support_identity": support,
                    "evidence": evidence,
                },
            )
        if capability == "compatible":
            return LiveIdentityObservation(
                configured,
                str(getattr(board, "provider_id", "pyocd")),
                str(getattr(board, "target", "")),
                str(getattr(board, "provider_support_identity", "")),
                "compatible",
                "compatible",
                None,
                {
                    "kind": "provider",
                    "provenance": provenance,
                    "support_identity": support,
                    "evidence": evidence,
                },
            )
        reason = metadata_identity.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            reason = evidence.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise LiveIdentityObservationError(
                "Unavailable provider live identity requires a narrow reason."
            )
        return _unavailable(board, configured, reason)

    address = getattr(board, "silicon_id_addr", None)
    expected = getattr(board, "silicon_id_expected", None)
    mask = getattr(board, "silicon_id_mask", None)
    width_bits = getattr(board, "silicon_id_width_bits", None)
    if read_memory is None or not all(
        isinstance(value, int) and not isinstance(value, bool)
        for value in (address, expected, mask, width_bits)
    ):
        return _unavailable(
            board, configured, "no complete configured live identity register proof"
        )
    assert isinstance(address, int) and not isinstance(address, bool)
    assert isinstance(expected, int) and not isinstance(expected, bool)
    assert isinstance(mask, int) and not isinstance(mask, bool)
    assert isinstance(width_bits, int) and not isinstance(width_bits, bool)
    try:
        width_bits = validate_identity_register_width(width_bits)
    except IdentityObservationError as exc:
        raise LiveIdentityObservationError(
            "Configured live identity proof has an unsupported register width."
        ) from exc
    capability = str(getattr(board, "silicon_id_capability", "compatible"))
    if capability not in {"exact", "compatible"}:
        raise LiveIdentityObservationError("Configured live identity capability is malformed.")
    if capability == "exact":
        if (
            not str(getattr(board, "silicon_id_provenance", "")).strip()
            or not str(getattr(board, "silicon_id_support_identity", "")).strip()
            or getattr(board, "silicon_id_support_identity", "")
            != getattr(board, "provider_support_identity", "")
            or normalize_part_number(str(getattr(board, "silicon_id_bound_part_number", "")))
            != normalize_part_number(configured)
        ):
            raise LiveIdentityObservationError(
                "Exact configured identity proof is not bound to the replayed part/support route."
            )
    try:
        actual = read_memory(handle, address, width_bits)
    except Exception as exc:  # noqa: BLE001 - provider read is the operation fact
        raise LiveIdentityObservationError(
            "Configured live identity proof could not be read from this session."
        ) from exc
    try:
        actual = validate_identity_observation(actual, width_bits)
    except IdentityObservationError as exc:
        raise LiveIdentityObservationError(
            "Configured live identity proof returned a malformed observed register value."
        ) from exc
    if (actual & mask) != (expected & mask):
        if capability == "exact":
            raise LiveIdentityContradiction(
                "Direct pyOCD flash refused because live silicon-ID evidence does not match the configured target."
            )
        raise LiveIdentityContradiction(
            "Compatible live identity evidence contradicts this configured session."
        )
    if capability == "exact":
        return LiveIdentityObservation(
            configured,
            str(getattr(board, "provider_id", "pyocd")),
            str(getattr(board, "target", "")),
            str(getattr(board, "provider_support_identity", "")),
            "exact",
            "matched",
            str(getattr(board, "silicon_id_bound_part_number", configured)),
            _record_proof(board, actual),
        )
    return LiveIdentityObservation(
        configured,
        str(getattr(board, "provider_id", "pyocd")),
        str(getattr(board, "target", "")),
        str(getattr(board, "provider_support_identity", "")),
        "compatible",
        "compatible",
        None,
        _record_proof(board, actual),
    )
