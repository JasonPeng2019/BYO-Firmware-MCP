# SPDX-License-Identifier: Apache-2.0
# Derived from Firmware-CLI commit 0e8a45d; modified for BYO-Firmware-MCP.
"""Typed CAN errors with stable machine-readable codes."""

from __future__ import annotations


CAN_ERROR_CODES = frozenset(
    {
        "can/not-configured",
        "can/not-connected",
        "can/board-busy",
        "can/config-invalid",
        "can/board-mismatch",
        "can/probe-mismatch",
        "can/backend-unsupported",
        "can/connection-failure",
        "can/capability-unproven",
        "can/tx-disabled",
        "can/id-forbidden",
        "can/invalid-argument",
        "can/rate-limited",
        "can/process-call-limit",
        "can/process-rx-limit",
        "can/process-artifact-limit",
        "can/no-matching-response",
        "can/operation-timeout",
        "can/worker-failure",
        "can/backend-failure",
        "can/cleanup-failed",
        "can/state-uncertain",
        "can/target-mutation-forbidden",
        "can/fixture-sequence-forbidden",
        "can/terminal-sealed",
        "can/terminal-argument-invalid",
        "can/provider-host-isolation-required",
    }
)

# Canonical coded outcomes that mean an admitted operation or live capability
# check failed, rather than the caller requesting something outside policy.
# Server events and turnkey/provider memory both consume this one vocabulary.
CAN_OPERATIONAL_FAILURE_CODES = frozenset(
    {
        "can/backend-failure",
        "can/backend-unsupported",
        "can/capability-unproven",
        "can/cleanup-failed",
        "can/connection-failure",
        "can/no-matching-response",
        "can/operation-timeout",
        "can/state-uncertain",
        "can/worker-failure",
    }
)


class CanError(RuntimeError):
    """Base class for every CAN-facing failure."""

    def __init__(self, code: str, message: str) -> None:
        if code not in CAN_ERROR_CODES:
            raise ValueError(f"Unknown CAN error code: {code!r}")
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


class CanConfigError(CanError):
    """The immutable startup profile is malformed or unsafe."""

    def __init__(self, message: str) -> None:
        super().__init__("can/config-invalid", message)


class CanNotConfiguredError(CanError):
    """No immutable CAN profile was staged for this process."""

    def __init__(self, message: str = "No CAN fixture profile was staged at startup.") -> None:
        super().__init__("can/not-configured", message)


class CanBackendUnsupportedError(CanError):
    """The selected backend discriminant is not supported by CAN v1."""

    def __init__(self, message: str) -> None:
        super().__init__("can/backend-unsupported", message)


class CanConnectionError(CanError):
    """A qualified CAN attachment could not be opened."""

    def __init__(self, message: str, *, code: str = "can/connection-failure") -> None:
        super().__init__(code, message)


class CanOperationError(CanError):
    """The backend failed an otherwise admitted CAN operation."""

    def __init__(self, message: str, *, code: str = "can/backend-failure") -> None:
        super().__init__(code, message)


class CanTimeoutError(CanOperationError):
    """A bounded CAN operation reached its product deadline."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="can/operation-timeout")


class CanWorkerError(CanOperationError):
    """The isolated CAN worker failed or its protocol became invalid."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="can/worker-failure")


class CanPolicyError(CanError):
    """A request is outside the frozen operator policy."""


class CanCleanupError(CanError):
    """Resource cleanup is uncertain after bounded attempts."""

    def __init__(self, message: str) -> None:
        super().__init__("can/cleanup-failed", message)
