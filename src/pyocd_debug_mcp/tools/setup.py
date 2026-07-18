"""MCP-facing setup, repair, and validation handlers."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pyocd_debug_mcp.guardrails.plan_engine import PlanEngine
from pyocd_debug_mcp.kernel.run_state import ServerRun
from pyocd_debug_mcp.setup_flow.preflight import PreflightSelections, SetupUserInput
from pyocd_debug_mcp.setup_flow.setup import SetupWorkflow
from pyocd_debug_mcp.setup_flow.validate import BoardValidator, ValidationRequest

SETUP_LOADABLE_TOOLS = frozenset(
    {"board_setup-plan", "board_safety_setup", "board_safety_refresh", "board_validate"}
)


def _json(document: Mapping[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, sort_keys=True)


class SetupToolLoadState:
    """A-20 run-scoped, per-board setup-tool disclosure state."""

    def __init__(self, server_run: ServerRun) -> None:
        self._run = server_run
        self._loaded: set[tuple[str, str, str]] = set()
        self._allowance_by_board: dict[str, str] = {}
        self._guard = threading.RLock()

    def load(self, board_id: str, tool_name: str) -> dict[str, Any]:
        board = board_id.strip()
        if not board:
            raise ValueError("board_id must be non-empty")
        if tool_name not in SETUP_LOADABLE_TOOLS:
            raise ValueError(f"tool_name must be one of: {', '.join(sorted(SETUP_LOADABLE_TOOLS))}")
        with self._guard:
            self._loaded.add((self._run.run_id, board, tool_name))
        return {
            "status": "setup_tool_loaded",
            "board_id": board,
            "tool_name": tool_name,
            "redirect": f"Continue by calling {tool_name} for board '{board}'.",
        }

    def is_loaded(self, board_id: str, tool_name: str) -> bool:
        with self._guard:
            return (self._run.run_id, board_id, tool_name) in self._loaded

    def any_loaded(self, tool_name: str) -> bool:
        with self._guard:
            return any(
                run == self._run.run_id and tool == tool_name for run, _, tool in self._loaded
            )

    def redirect(self, board_id: str | None, tool_name: str) -> str:
        target = board_id or "the intended board"
        return _json(
            {
                "status": "setup_tool_not_loaded",
                "board_id": board_id,
                "tool_name": tool_name,
                "redirect": (
                    f"Call load_setup_tool for {target} and tool '{tool_name}', then retry."
                ),
            }
        )

    def bind_allowance(self, board_id: str, allowance_id: str) -> None:
        with self._guard:
            self._allowance_by_board[board_id] = allowance_id

    def allowance_for(self, board_id: str) -> str | None:
        with self._guard:
            return self._allowance_by_board.get(board_id)

    def clear_allowance(self, board_id: str) -> None:
        with self._guard:
            self._allowance_by_board.pop(board_id, None)


@dataclass(frozen=True, slots=True)
class SetupToolServices:
    loader: SetupToolLoadState
    plan_engine: PlanEngine
    workflow: SetupWorkflow
    validator: BoardValidator
    safety_setup: Callable[[str], Mapping[str, Any]]
    safety_refresh: Callable[..., Mapping[str, Any]]
    setup_status: Callable[[str], Mapping[str, Any]] | None = None
    setup_overview: Callable[[list[str] | None], Mapping[str, Any]] | None = None
    setup_continue: Callable[[str, str, Mapping[str, object]], Mapping[str, Any]] | None = None
    setup_selections: Callable[[str], PreflightSelections] | None = None
    clear_setup_continuation: Callable[[str], None] | None = None


def build_setup_handlers(services: SetupToolServices) -> dict[str, Callable[..., str]]:
    """Build the Task-11 tool surface with A-20 redirects."""

    def load_setup_tool(board_id: str, tool_name: str) -> str:
        """Load detailed setup/validation guidance for one server-generated board_id.

        Call setup_overview first so the server, rather than the user, supplies the profile route,
        board_id, and friendly physical choices. Then load exactly the setup tool named by that
        route before calling it. Never ask the user for board_id, connection IDs, or JSON.
        """

        return _json(services.loader.load(board_id, tool_name))

    def setup_overview(board_names: list[str] | None = None) -> str:
        """Inventory profiles/connections and route user-provided familiar board names.

        Call after initialization_handshake and after asking which boards are connected. Pass the
        ordinary familiar names here; pass NULL only to inspect inventory before the user answers.
        Every matching stored name, including an incomplete profile, routes to board_validate
        first. Unknown names receive a server-generated board_id plus setup questions. Use setup,
        repair, safety, attachment, or retry only when validation names that exact remedy. Relay
        only agent_prompt and friendly choices, never raw identifiers or this JSON.
        """

        if services.setup_overview is None:
            return _json(
                {
                    "status": "setup_overview_unavailable",
                    "agent_prompt": "Setup inventory is unavailable; stop before hardware access.",
                }
            )
        return _json(services.setup_overview(board_names))

    def board_setup_plan(
        board_id: str | None = None,
        hypothesis: str | None = None,
        strategy: str | None = None,
        hypothesis_made: bool | None = None,
        strategy_evaluated: bool | None = None,
        expected_fail_return: str | None = None,
        expected_success_return: str | None = None,
        max_calls: int | None = None,
        max_calls_buffer: int | None = None,
        action_parameters: dict[str, object] | None = None,
        user_permission: str | None = None,
    ) -> str:
        """Run this setup-routing plan first before any other *-plan hardware tool.

        Use the all-NULL call before attempting hardware access. It explains how to ask for the
        board's familiar name and exact board/MCU identity, route an existing matching YAML profile
        to board_validate, and load the hidden setup workflow only when the profile is absent or
        validation fails. Do not populate a setup plan for a healthy profile that validates.
        """

        fields: dict[str, object] = {
            "board_id": board_id,
            "hypothesis": hypothesis,
            "hypothesis_made": hypothesis_made,
            "strategy": strategy,
            "strategy_evaluated": strategy_evaluated,
            "expected_fail_return": expected_fail_return,
            "expected_success_return": expected_success_return,
            "max_calls": max_calls,
            "max_calls_buffer": max_calls_buffer,
            "action_parameters": action_parameters,
            "user_permission": user_permission,
        }
        all_null = all(value is None for value in fields.values())
        # The NULL response is the universal hardware-entry routing guide. It must be available
        # before the hidden setup workflow is loaded; populated setup plans remain A-20 scoped.
        if all_null:
            return services.plan_engine.submit("board_setup-plan", fields, session_id=None).message
        if board_id is not None and not services.loader.is_loaded(board_id, "board_setup-plan"):
            return services.loader.redirect(board_id, "board_setup-plan")
        return services.plan_engine.submit("board_setup-plan", fields, session_id=None).message

    def _user_input(
        board_id: str,
        connection_id: str,
        display_name: str,
        board_type: str,
        mcu_part_number: str,
        serial_baudrate: int,
        serial_id: str,
        serial_port: str,
        datasheet_path: str,
        datasheet_sha256: str,
    ) -> SetupUserInput:
        return SetupUserInput(
            board_id,
            connection_id,
            display_name,
            mcu_part_number,
            serial_baudrate,
            True,
            board_type,
            datasheet_path,
            datasheet_sha256,
            serial_id,
            serial_port,
        )

    def board_setup(
        board_id: str,
        mode: str,
        connection_id: str,
        display_name: str,
        board_type: str,
        mcu_part_number: str,
        serial_baudrate: int,
        serial_id: str,
        serial_port: str,
        datasheet_path: str,
        datasheet_sha256: str,
    ) -> str:
        """Run the first setup attempt covered by the active setup plan."""

        active = services.plan_engine.active_plan("board_setup", board_id)
        if active is None:
            return _json(
                {
                    "status": "setup_blocked",
                    "redirect": "Call board_setup-plan with a valid permitted plan first.",
                }
            )
        if mode not in {"setup", "repair"}:
            raise ValueError("mode must be setup or repair")
        user_input = _user_input(
            board_id,
            connection_id,
            display_name,
            board_type,
            mcu_part_number,
            serial_baudrate,
            serial_id,
            serial_port,
            datasheet_path,
            datasheet_sha256,
        )
        services.loader.bind_allowance(board_id, active.plan_id)
        services.workflow.begin_plan(active.plan_id, user_input, mode=mode)  # type: ignore[arg-type]
        response = services.workflow.board_setup(active.plan_id, user_input)
        if response.status == "setup_completed":
            services.loader.clear_allowance(board_id)
            services.plan_engine.complete_paired_plan("board_setup", board_id, "setup completed")
            if services.clear_setup_continuation is not None:
                services.clear_setup_continuation(board_id)
        return _json(response.to_payload())

    def board_fix_setup(
        board_id: str,
        mode: str,
        connection_id: str,
        display_name: str,
        board_type: str,
        mcu_part_number: str,
        serial_baudrate: int,
        serial_id: str,
        serial_port: str,
        datasheet_path: str,
        datasheet_sha256: str,
    ) -> str:
        """Use the setup plan's single paired repair allowance."""

        del (
            mode,
            connection_id,
            display_name,
            board_type,
            mcu_part_number,
            serial_baudrate,
            serial_id,
            serial_port,
            datasheet_path,
            datasheet_sha256,
        )
        allowance_id = services.loader.allowance_for(board_id)
        if allowance_id is None:
            return _json(
                {
                    "status": "setup_blocked",
                    "redirect": "Submit a replacement board_setup-plan before another repair.",
                }
            )
        selections = (
            services.setup_selections(board_id)
            if services.setup_selections is not None
            else PreflightSelections()
        )
        response = services.workflow.board_fix_setup(allowance_id, selections=selections)
        services.loader.clear_allowance(board_id)
        services.plan_engine.complete_paired_plan(
            "board_setup", board_id, f"repair ended with {response.status}"
        )
        if services.clear_setup_continuation is not None:
            services.clear_setup_continuation(board_id)
        return _json(response.to_payload())

    def continue_setup(
        board_id: str,
        continuation_id: str,
        response: dict[str, object],
    ) -> str:
        """Continue an incomplete setup with one server-requested choice or research reply.

        Call this only when board_setup or board_fix_setup returns setup_needs_user_input or
        setup_research_required. Relay the server's friendly question to the user when required,
        or perform the requested official-source research, then submit exactly the response
        object described by accepted_response/exact_response_fields. This tool validates and
        stages evidence but grants no permission, opens no gate, and performs no target write.
        After acceptance, call board_fix_setup under the still-active paired setup allowance.
        """

        if services.setup_continue is None:
            return _json(
                {
                    "status": "setup_continuation_unavailable",
                    "agent_prompt": "Setup cannot accept this continuation; stop before hardware access.",
                }
            )
        if not isinstance(response, dict):
            raise ValueError("response must be one JSON object")
        return _json(services.setup_continue(board_id, continuation_id, response))

    def board_validate(
        board_id: str,
        probe_id: str | None = None,
        serial_id: str | None = None,
    ) -> str:
        """Validate a matching board YAML first, or validate after setup, repair, or reconnect.

        Trigger this instead of board_setup when the user's familiar board name already matches a
        healthy profile. A passing validation stamps only the current board/connection gate. If it
        fails because setup or safety evidence is incomplete, follow its exact setup or safety
        remedy; never treat the profile's presence alone as permission to access hardware.
        """

        if not services.loader.is_loaded(board_id, "board_validate"):
            return services.loader.redirect(board_id, "board_validate")
        result = services.validator.validate(ValidationRequest(board_id, probe_id, serial_id))
        return _json(result.to_payload())

    def board_safety_setup(board_id: str) -> str:
        """Create the first authoritative safety map, or rebuild one after structural failure.

        Trigger this when board setup/validation reports that no safety map exists, authoritative
        sources are incomplete, regions conflict, or an anchor change requires a full rebuild. It
        is not the routine source-drift path and never opens the hardware gate by itself; call
        board_validate after it succeeds.
        """

        if not services.loader.is_loaded(board_id, "board_safety_setup"):
            return services.loader.redirect(board_id, "board_safety_setup")
        return _json(services.safety_setup(board_id))

    def board_safety_refresh(
        board_id: str,
        application_elf: str | None = None,
        application_hex: str | None = None,
        application_map: str | None = None,
    ) -> str:
        """Refresh an existing valid safety map after safely scoped source or build drift.

        Trigger this only when a current map already exists and validation or a guarded action names
        refreshable fingerprint drift as the remedy, such as a rebuilt application with unchanged
        board/target anchors. Use board_safety_setup for a missing/conflicting map and full setup for
        board, MCU, target, or probe-anchor changes. Refresh never reopens a disconnected gate.
        """

        if not services.loader.is_loaded(board_id, "board_safety_refresh"):
            return services.loader.redirect(board_id, "board_safety_refresh")
        return _json(
            services.safety_refresh(
                board_id,
                application_elf=application_elf,
                application_hex=application_hex,
                application_map=application_map,
            )
        )

    def get_setup_status(board_id: str) -> str:
        """Report whether durable setup and this run's live validated session are ready.

        Call this as the final barrier before an external coding workflow begins. It never opens
        a connection or gate and never treats persisted reports as authority. UART readiness is
        reported separately so a project that does not need a console is not blocked, while a
        console-dependent workflow can require ready_for_uart_work before it starts. For a known
        reviewed MCU, build_guidance returns the exact Zephyr board target and recommends the
        cross-platform pyocd-zephyr-build helper, which uses short scratch paths when necessary on
        Windows. That guidance is advisory only: inspect the resulting ELF/map with
        board_safety_refresh before flashing.
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
        "setup_overview": setup_overview,
        "load_setup_tool": load_setup_tool,
        "board_setup-plan": board_setup_plan,
        "board_setup": board_setup,
        "board_fix_setup": board_fix_setup,
        "continue_setup": continue_setup,
        "board_safety_setup": board_safety_setup,
        "board_safety_refresh": board_safety_refresh,
        "board_validate": board_validate,
        "get_setup_status": get_setup_status,
    }
