"""MCP-facing direct setup, repair, continuation, and validation handlers."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from firmware_mcp.setup_flow.preflight import PreflightSelections, SetupUserInput
from firmware_mcp.setup_flow.setup import SetupWorkflow
from firmware_mcp.setup_flow.validate import BoardValidator, ValidationRequest


def _json(document: Mapping[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True, slots=True)
class SetupToolServices:
    """Direct setup dependencies; assignment checks prevent cross-board reuse."""

    workflow: SetupWorkflow
    validator: BoardValidator
    setup_status: Callable[[str], Mapping[str, Any]] | None = None
    setup_overview: Callable[..., Mapping[str, Any]] | None = None
    setup_continue: Callable[[str, str, Mapping[str, object]], Mapping[str, Any]] | None = None
    setup_selections: Callable[[str], PreflightSelections] | None = None
    require_assignment: Callable[[str, str], None] | None = None
    assigned_connection: Callable[[str], str | None] | None = None
    safety_map_status: Callable[[str], Mapping[str, Any]] | None = None


def build_setup_handlers(services: SetupToolServices) -> dict[str, Callable[..., str]]:
    """Build the normally visible setup workflow tool surface."""

    def setup_overview(
        board_names: list[str] | None = None,
        connection_assignments: dict[str, str] | None = None,
        provider_recipe: dict[str, object] | None = None,
    ) -> str:
        """**What** Inventory setup profiles and current board connections.

        **When** Use first to discover configuration and routing state.

        **Parameters** optional `board_names` filters boards (for example `[\"board-a\"]`);
        optional `connection_assignments` maps each familiar name appearing in `board_names` to one
        current connection ID (for example, `board_names=["left"]` with
        `connection_assignments={"left": "probe:probe-1"}`); optional `provider_recipe` is
        exactly `{\"provider_id\": \"lab-tool\", \"inventory_argv\": [\"tool\", \"list\"],
        \"worker_argv\": [\"tool\", \"worker\"]}` and runs direct argv without a shell.

        **Returns** Inventory and diagnostic routing evidence.

        **Failures and recovery** Unavailable inventory is explicit; inspect the project store then
        use `setup_board`.
        """

        if services.setup_overview is None:
            return _json(
                {
                    "status": "setup_overview_unavailable",
                    "agent_prompt": "Setup inventory is unavailable; inspect the current connection and retry.",
                }
            )
        return _json(services.setup_overview(board_names, connection_assignments, provider_recipe))

    def _user_input(
        board_id: str,
        connection_id: str,
        display_name: str,
        mcu_part_number: str,
        requires_uart: bool,
        baud: int | None,
        serial_id: str | None,
        datasheet_path: str,
        provider_recipe: dict[str, object] | None = None,
    ) -> SetupUserInput:
        return SetupUserInput(
            board_id=board_id,
            connection_id=connection_id,
            display_name=display_name,
            mcu_part_number=mcu_part_number,
            requires_uart=requires_uart,
            serial_baudrate=baud,
            datasheet_path=datasheet_path,
            serial_id=serial_id or "",
            provider_recipe=provider_recipe,
        )

    def setup_board(
        board_id: str,
        connection_id: str,
        display_name: str,
        mcu_part_number: str,
        requires_uart: bool,
        baud: int | None,
        serial_id: str | None,
        datasheet_path: str,
        provider_recipe: dict[str, object] | None = None,
    ) -> str:
        """**What** Start a fresh board-bound setup run.

        **When** Use after selecting a physical connection for a board.

        **Parameters** `board_id`, `connection_id`, `display_name`, `mcu_part_number`,
        `requires_uart`, optional UART `baud` in baud (for example `115200`), `serial_id`, and
        `datasheet_path` describe the selected hardware. Optional `provider_recipe` is exactly
        `{"provider_id": "lab-tool", "inventory_argv": ["tool", "list"], "worker_argv":
        ["tool", "worker"]}`; it is run as direct argv without a shell and lets an unknown
        provider supply its own inventory and isolated worker protocol.

        **Returns** Setup-run, continuation, profile, and live-evidence diagnostics.

        **Failures and recovery** Incomplete research returns a continuation; call
        `continue_board_setup` or `repair_board_setup` for the current board.
        """

        # A recipe inventories the exact provider-local connection inside this
        # setup attempt. Requiring a prior built-in assignment would make the
        # documented direct onboarding route impossible.
        if services.require_assignment is not None and provider_recipe is None:
            services.require_assignment(board_id, connection_id)
        user_input = _user_input(
            board_id,
            connection_id,
            display_name,
            mcu_part_number,
            requires_uart,
            baud,
            serial_id,
            datasheet_path,
            provider_recipe,
        )
        response = services.workflow.start_setup(user_input)
        return _json(response.to_payload())

    def repair_board_setup(board_id: str) -> str:
        """**What** Retry the current board-bound setup run.

        **When** Use after retaining or updating the current setup selection.

        **Parameters** `board_id` is the existing setup board, for example `"board-a"`.

        **Returns** Updated diagnostics or the next continuation.

        **Failures and recovery** Stale/disconnected runs are explicit; call `setup_board` to
        create a fresh run or `continue_board_setup` with the requested response.
        """
        selections = (
            services.setup_selections(board_id)
            if services.setup_selections is not None
            else PreflightSelections()
        )
        response = services.workflow.repair_setup(board_id, selections=selections)
        return _json(response.to_payload())

    def continue_board_setup(
        board_id: str,
        continuation_id: str,
        response: dict[str, object],
    ) -> str:
        """**What** Submit one requested current setup continuation response.

        **When** Use only after setup returns its continuation identifier.

        **Parameters** `board_id` is the board; `continuation_id` is the returned UUID; `response`
        is one JSON object, for example `{\"answer\": \"...\"}`.

        **Returns** Updated setup diagnostics and any next continuation.

        **Failures and recovery** Stale/wrong-board continuation is rejected; use
        `get_setup_status` then restart with `setup_board` if needed.
        """

        if services.setup_continue is None:
            return _json(
                {
                    "status": "setup_continuation_unavailable",
                    "agent_prompt": "Setup cannot accept this continuation; inspect current setup status and retry.",
                }
            )
        if not isinstance(response, dict):
            raise ValueError("response must be one JSON object")
        return _json(services.setup_continue(board_id, continuation_id, response))

    def validate_board(board_id: str) -> str:
        """**What** Validate the current assigned probe and live identity evidence.

        **When** Use after setup or reconnecting to diagnose identity.

        **Parameters** `board_id` is the assigned board, for example `"board-a"`.

        **Returns** Exact observed validation diagnostics; it grants no separate authority.

        **Failures and recovery** Missing assignment or evidence is explicit; use `setup_board` or
        `connect_board` then retry.
        """

        probe_id: str | None = None
        if services.require_assignment is not None:
            assigned_connection = (
                services.assigned_connection(board_id)
                if services.assigned_connection is not None
                else None
            )
            if assigned_connection is None or not assigned_connection.strip():
                raise ValueError("validate_board requires the exact current board assignment")
            expected_probe_id = (
                assigned_connection.removeprefix("probe:")
                if assigned_connection.startswith("probe:")
                else assigned_connection
            )
            probe_id = expected_probe_id
            services.require_assignment(board_id, assigned_connection)
        result = services.validator.validate(ValidationRequest(board_id, probe_id))
        payload = result.to_payload()
        # Validation remains diagnostic.  The map state is separately reported
        # so a valid probe/identity result never implies map authority for a
        # semantic-range operation.
        if services.safety_map_status is not None:
            payload["safety_map"] = dict(services.safety_map_status(board_id))
        return _json(payload)

    def get_setup_status(board_id: str) -> str:
        """**What** Report board setup/profile and current connection diagnostics.

        **When** Use to inspect readiness without changing state.

        **Parameters** `board_id` is the board, for example `\"board-a\".

        **Returns** Configuration and live-session evidence, including uncertainty when unknown.

        **Failures and recovery** Missing configuration is reported; start `setup_board` or use
        `connect_board` as indicated.
        """

        if services.setup_status is None:
            return _json(
                {
                    "status": "setup_status_unavailable",
                    "board_id": board_id,
                    "configuration_ready": False,
                    "live_session_ready": False,
                    "ready_for_code": False,
                    "uart_attachment_ready": False,
                    "ready_for_uart_work": False,
                }
            )
        return _json(services.setup_status(board_id))

    return {
        "get_setup_overview": setup_overview,
        "setup_board": setup_board,
        "repair_board_setup": repair_board_setup,
        "continue_board_setup": continue_board_setup,
        "validate_board": validate_board,
        "get_setup_status": get_setup_status,
    }
