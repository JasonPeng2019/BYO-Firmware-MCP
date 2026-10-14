"""Thin MCP-facing handlers for the governed Classical CAN service."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from pydantic import StringConstraints

from pyocd_debug_mcp.can_errors import CAN_OPERATIONAL_FAILURE_CODES, CanCleanupError, CanError
from pyocd_debug_mcp.services.can_session import (
    CanSessionManager,
    canonical_can_json,
    can_frame_record,
)

_BOARD_ID = re.compile(r"[a-z0-9][a-z0-9_]{0,63}")
CanBoardId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_]{0,63}$")]
_FAILURE_MESSAGES = {
    "can/backend-failure": "CAN operation failed.",
    "can/backend-unsupported": "The configured CAN backend is unavailable.",
    "can/capability-unproven": "Required CAN backend capabilities were not proven.",
    "can/cleanup-failed": "CAN cleanup failed.",
    "can/connection-failure": "CAN connection failed.",
    "can/no-matching-response": "No matching CAN response was observed.",
    "can/operation-timeout": "CAN operation timed out.",
    "can/state-uncertain": "CAN backend state is uncertain.",
    "can/worker-failure": "The isolated CAN worker failed.",
}


@dataclass(frozen=True, slots=True)
class CanToolServices:
    manager: CanSessionManager
    operational_identity: Callable[[str], str | None]


def validate_can_board_id(board_id: object) -> str:
    if not isinstance(board_id, str) or _BOARD_ID.fullmatch(board_id) is None:
        raise CanError(
            "can/invalid-argument",
            "board_id must match [a-z0-9][a-z0-9_]{0,63}",
        )
    return board_id


def _base(
    services: CanToolServices,
    operation: str,
    board_id: str,
    *,
    ok: bool,
    status: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "ok": ok,
        "status": status,
        "operation": operation,
        "board_id": board_id,
        "can": services.manager.status(board_id),
    }


def _failure(
    services: CanToolServices,
    operation: str,
    board_id: str,
    error: CanError,
) -> str:
    status = (
        "failed"
        if error.code in CAN_OPERATIONAL_FAILURE_CODES or isinstance(error, CanCleanupError)
        else "refused"
    )
    payload = _base(services, operation, board_id, ok=False, status=status)
    message = _FAILURE_MESSAGES.get(error.code, error.message)
    payload.update({"code": error.code, "message": message})
    return canonical_can_json(payload)


def _unexpected(
    services: CanToolServices,
    operation: str,
    board_id: str,
    *,
    cleanup: bool = False,
) -> str:
    code = "can/cleanup-failed" if cleanup else "can/backend-failure"
    payload = _base(services, operation, board_id, ok=False, status="failed")
    payload.update(
        {
            "code": code,
            "message": "CAN cleanup failed." if cleanup else "CAN operation failed.",
        }
    )
    return canonical_can_json(payload)


def build_can_handlers(services: CanToolServices) -> dict[str, Callable[..., object]]:
    """Build the five static CAN handlers over composition-root services."""

    def get_can_status(board_id: CanBoardId) -> str:
        """Return immutable CAN profile, owner, budget, capability, and cleanup state."""

        try:
            board_id = validate_can_board_id(board_id)
            fixture = services.manager.startup.fixture
            if fixture is not None:
                services.manager.require_profile_board(board_id)
            return canonical_can_json(
                _base(services, "get_can_status", board_id, ok=True, status="status_report")
            )
        except CanError as exc:
            return _failure(services, "get_can_status", str(board_id), exc)
        except Exception:
            return _unexpected(services, "get_can_status", str(board_id))

    async def connect_can(board_id: CanBoardId) -> str:
        """Attach the startup-staged software CAN fixture to one validated board."""

        try:
            board_id = validate_can_board_id(board_id)
            if not services.manager.configured:
                services.manager.require_profile_board(board_id)
            probe_uid = services.operational_identity(board_id)
            await services.manager.connect(board_id, probe_uid)
            return canonical_can_json(
                _base(services, "connect_can", board_id, ok=True, status="connected")
            )
        except CanError as exc:
            return _failure(services, "connect_can", str(board_id), exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            return _unexpected(services, "connect_can", str(board_id))

    async def disconnect_can(board_id: CanBoardId) -> str:
        """Idempotently retire this board's CAN generation without requiring live validation."""

        try:
            board_id = validate_can_board_id(board_id)
            if services.manager.configured:
                services.manager.require_profile_board(board_id)
            await services.manager.disconnect(board_id)
            return canonical_can_json(
                _base(services, "disconnect_can", board_id, ok=True, status="disconnected")
            )
        except CanError as exc:
            return _failure(services, "disconnect_can", str(board_id), exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            return _unexpected(services, "disconnect_can", str(board_id), cleanup=True)

    async def send_can(
        board_id: CanBoardId,
        arbitration_id: int,
        data_hex: str,
        extended: bool = False,
        timeout_seconds: float | int | None = None,
    ) -> str:
        """Send one plan- and profile-bound Classical CAN data frame."""

        try:
            board_id = validate_can_board_id(board_id)
            if not services.manager.configured:
                services.manager.require_profile_board(board_id)
            services.operational_identity(board_id)
            frame = await services.manager.send(
                board_id,
                arbitration_id,
                data_hex,
                extended=extended,
                timeout_seconds=timeout_seconds,
            )
            fixture = services.manager.startup.fixture
            assert fixture is not None
            payload = _base(services, "send_can", board_id, ok=True, status="sent")
            payload["frame"] = can_frame_record(
                frame, include_payload=fixture.config.evidence.provider_payloads == "full"
            )
            return canonical_can_json(payload)
        except CanError as exc:
            return _failure(services, "send_can", str(board_id), exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            return _unexpected(services, "send_can", str(board_id))

    async def read_can(
        board_id: CanBoardId,
        read_seconds: float | int | None = None,
        max_frames: int | None = None,
    ) -> str:
        """Capture exact-allowlisted CAN frames within plan/profile/process bounds."""

        try:
            board_id = validate_can_board_id(board_id)
            if not services.manager.configured:
                services.manager.require_profile_board(board_id)
            services.operational_identity(board_id)
            result = await services.manager.read(
                board_id,
                read_seconds=read_seconds,
                max_frames=max_frames,
            )
            fixture = services.manager.startup.fixture
            assert fixture is not None
            include_payload = fixture.config.evidence.provider_payloads == "full"
            payload = _base(services, "read_can", board_id, ok=True, status="captured")
            payload.update(
                {
                    "observed_frames": result.observed_frames,
                    "returned_frames": len(result.frames),
                    "artifact": result.artifact,
                    "elapsed_seconds": result.elapsed_seconds,
                    "stop_reason": result.stop_reason,
                    "truncated": result.truncated,
                    "frames": [
                        can_frame_record(frame, include_payload=include_payload)
                        for frame in result.frames
                    ],
                }
            )
            return canonical_can_json(payload)
        except CanError as exc:
            return _failure(services, "read_can", str(board_id), exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            return _unexpected(services, "read_can", str(board_id))

    return {
        "get_can_status": get_can_status,
        "connect_can": connect_can,
        "disconnect_can": disconnect_can,
        "send_can": send_can,
        "read_can": read_can,
    }


__all__ = ["CanToolServices", "build_can_handlers", "validate_can_board_id"]
