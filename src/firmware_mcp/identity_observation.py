"""Strict, dependency-neutral validation for identity-register observations.

Provider reads are untyped runtime facts.  Validate their complete register
shape before any caller shifts, masks, compares, formats, or persists them.
Keeping this primitive outside setup and runtime services prevents either path
from becoming the other's identity authority.
"""

from __future__ import annotations


SUPPORTED_IDENTITY_REGISTER_WIDTHS = frozenset({8, 16, 32})


class IdentityObservationError(ValueError):
    """An identity register width or observed value is malformed."""


def validate_identity_register_width(width_bits: object) -> int:
    """Return one supported positive identity-register width."""

    if (
        not isinstance(width_bits, int)
        or isinstance(width_bits, bool)
        or width_bits not in SUPPORTED_IDENTITY_REGISTER_WIDTHS
    ):
        raise IdentityObservationError(
            "identity observation requires a supported positive register width of 8, 16, or 32 bits"
        )
    return width_bits


def validate_identity_observation(observed: object, width_bits: object) -> int:
    """Return a complete unsigned provider observation for ``width_bits``.

    ``bool`` is intentionally excluded even though it is an ``int`` subclass.
    The range test happens before any masking so an oversized provider value
    cannot be truncated into compatible identity evidence.
    """

    width = validate_identity_register_width(width_bits)
    if not isinstance(observed, int) or isinstance(observed, bool):
        raise IdentityObservationError(
            "identity observation must be an unsigned integer returned by the provider"
        )
    if observed < 0 or observed >= 1 << width:
        raise IdentityObservationError(
            f"identity observation is outside the unsigned {width}-bit register range"
        )
    return observed
