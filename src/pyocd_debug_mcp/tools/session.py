"""Revised Layer-2 session and connection handlers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pyocd_debug_mcp.kernel.operations import wrap_layer2_response


@dataclass(frozen=True, slots=True)
class SessionToolServices:
    connect: Callable[..., str]
    disconnect: Callable[[str], str]
    get_board_info: Callable[[str], str]
    get_state: Callable[[str], str]


def build_session_handlers(services: SessionToolServices) -> dict[str, Callable[..., str]]:
    """Build the exact revised session surface over composition-root services."""

    def connect(
        board_id: str,
        unique_id: str | None = None,
        target: str | None = None,
        board_config: str | None = None,
    ) -> str:
        """Open a persistent debug session for one named logical board."""

        return wrap_layer2_response(
            services.connect(
                board_id,
                unique_id=unique_id,
                target=target,
                board_config=board_config,
            )
        )

    def disconnect(board_id: str) -> str:
        """Close the named board session and release only its connection."""

        return wrap_layer2_response(services.disconnect(board_id))

    def get_board_info(board_id: str) -> str:
        """Return active profile and routing facts for the named board."""

        return wrap_layer2_response(services.get_board_info(board_id))

    def get_state(board_id: str) -> str:
        """Return the connected core's current observable run state."""

        return wrap_layer2_response(services.get_state(board_id))

    def connect_override(
        board_id: str,
        probe_uid: str | None = None,
        target_override: str | None = None,
        external_board_config: str | None = None,
    ) -> str:
        """Connect with run-scoped manual identifiers without rewriting any profile."""

        result = services.connect(
            board_id,
            unique_id=probe_uid,
            target=target_override,
            board_config=external_board_config,
        )
        return wrap_layer2_response(
            f"{result}\nManual override values are run-scoped and did not rewrite a profile."
        )

    return {
        "connect": connect,
        "disconnect": disconnect,
        "get_board_info": get_board_info,
        "get_state": get_state,
        "connect_override": connect_override,
    }
