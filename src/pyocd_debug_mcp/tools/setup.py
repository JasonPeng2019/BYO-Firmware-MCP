"""MCP-facing setup, repair, and validation handlers."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pyocd_debug_mcp.guardrails.plan_engine import PlanEngine
from pyocd_debug_mcp.kernel.run_state import ServerRun
from pyocd_debug_mcp.setup_flow.preflight import SetupUserInput
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
            raise ValueError(
                f"tool_name must be one of: {', '.join(sorted(SETUP_LOADABLE_TOOLS))}"
            )
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
            return any(run == self._run.run_id and tool == tool_name for run, _, tool in self._loaded)

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
    safety_refresh: Callable[[str], Mapping[str, Any]]


def build_setup_handlers(services: SetupToolServices) -> dict[str, Callable[..., str]]:
    """Build the Task-11 tool surface with A-20 redirects."""

    def load_setup_tool(board_id: str, tool_name: str) -> str:
        """Load one setup or validation tool for one board in this Server Run."""

        return _json(services.loader.load(board_id, tool_name))

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
        """Initialize or submit one exact JSON board-setup plan envelope."""

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
        if all_null and not services.loader.any_loaded("board_setup-plan"):
            return services.loader.redirect(None, "board_setup-plan")
        if board_id is not None and not services.loader.is_loaded(board_id, "board_setup-plan"):
            return services.loader.redirect(board_id, "board_setup-plan")
        return services.plan_engine.submit(
            "board_setup-plan", fields, session_id=None
        ).message

    def _user_input(
        board_id: str,
        connection_id: str,
        display_name: str,
        mcu_part_number: str,
        serial_baudrate: int,
    ) -> SetupUserInput:
        return SetupUserInput(
            board_id,
            connection_id,
            display_name,
            mcu_part_number,
            serial_baudrate,
        )

    def board_setup(
        board_id: str,
        mode: str,
        connection_id: str,
        display_name: str,
        mcu_part_number: str,
        serial_baudrate: int,
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
            board_id, connection_id, display_name, mcu_part_number, serial_baudrate
        )
        services.loader.bind_allowance(board_id, active.plan_id)
        services.workflow.begin_plan(active.plan_id, user_input, mode=mode)  # type: ignore[arg-type]
        response = services.workflow.board_setup(active.plan_id, user_input)
        if response.status == "setup_completed":
            services.loader.clear_allowance(board_id)
            services.plan_engine.complete_paired_plan(
                "board_setup", board_id, "setup completed"
            )
        return _json(response.to_payload())

    def board_fix_setup(
        board_id: str,
        mode: str,
        connection_id: str,
        display_name: str,
        mcu_part_number: str,
        serial_baudrate: int,
    ) -> str:
        """Use the setup plan's single paired repair allowance."""

        del mode, connection_id, display_name, mcu_part_number, serial_baudrate
        allowance_id = services.loader.allowance_for(board_id)
        if allowance_id is None:
            return _json(
                {
                    "status": "setup_blocked",
                    "redirect": "Submit a replacement board_setup-plan before another repair.",
                }
            )
        response = services.workflow.board_fix_setup(allowance_id)
        services.loader.clear_allowance(board_id)
        services.plan_engine.complete_paired_plan(
            "board_setup", board_id, f"repair ended with {response.status}"
        )
        return _json(response.to_payload())

    def board_validate(
        board_id: str,
        probe_id: str | None = None,
        serial_id: str | None = None,
    ) -> str:
        """Run bounded, non-destructive validation for one profile and connection."""

        if not services.loader.is_loaded(board_id, "board_validate"):
            return services.loader.redirect(board_id, "board_validate")
        result = services.validator.validate(ValidationRequest(board_id, probe_id, serial_id))
        return _json(result.to_payload())

    def board_safety_setup(board_id: str) -> str:
        """Build or safely route the authoritative safety map for one board."""

        if not services.loader.is_loaded(board_id, "board_safety_setup"):
            return services.loader.redirect(board_id, "board_safety_setup")
        return _json(services.safety_setup(board_id))

    def board_safety_refresh(board_id: str) -> str:
        """Refresh changed authoritative safety sources for one board."""

        if not services.loader.is_loaded(board_id, "board_safety_refresh"):
            return services.loader.redirect(board_id, "board_safety_refresh")
        return _json(services.safety_refresh(board_id))

    return {
        "load_setup_tool": load_setup_tool,
        "board_setup-plan": board_setup_plan,
        "board_setup": board_setup,
        "board_fix_setup": board_fix_setup,
        "board_safety_setup": board_safety_setup,
        "board_safety_refresh": board_safety_refresh,
        "board_validate": board_validate,
    }
