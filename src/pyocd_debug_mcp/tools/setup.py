"""MCP-facing setup, repair, and validation handlers."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pyocd_debug_mcp.guardrails.plan_defs import PLAN_DEFINITIONS
from pyocd_debug_mcp.guardrails.plan_engine import PlanEngine
from pyocd_debug_mcp.kernel.run_state import ServerRun
from pyocd_debug_mcp.setup_flow.preflight import (
    NO_INTERNALS_RELAY_INSTRUCTION,
    PreflightSelections,
    SetupUserInput,
)
from pyocd_debug_mcp.setup_flow.setup import SetupWorkflow
from pyocd_debug_mcp.setup_flow.validate import (
    VALIDATION_STATUSES,
    BoardValidator,
    ValidationRequest,
)

SETUP_LOADABLE_TOOLS = frozenset(
    {"board_setup-plan", "board_safety_refresh", "board_validate"}
)


def _json(document: Mapping[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, sort_keys=True)


def _load_guidance(
    board_id: str,
    tool_name: str,
    *,
    validation_probe_id: str | None = None,
) -> dict[str, Any]:
    """Return one bounded, tool-specific route rather than the whole setup manual."""

    relay_rule = (
        f"{NO_INTERNALS_RELAY_INSTRUCTION} Ask only the ordinary-language question named by "
        "the returned status; copy machine fields into the next MCP call without showing them."
    )
    if tool_name == "board_setup-plan":
        definition = PLAN_DEFINITIONS["board_setup"]
        return {
            "next_call": {
                "tool": definition.plan_tool_name,
                "arguments": {name: None for name in definition.null_field_names},
            },
            "guidance": {
                "purpose": "Plan one first-time setup or one validation-directed profile repair.",
                "when_to_use": (
                    "Use only for an unknown familiar board name, or after board_validate records "
                    "a live MCU mismatch and the user elects to keep it under a new logical board."
                ),
                "when_not_to_use": (
                    "Do not use when a matching profile has not yet been validated, or as a "
                    "shortcut around validation or a stable-map safety failure. Never rewrite an "
                    "established profile's MCU identity in place."
                ),
                "expected_statuses": [
                    "plan initialization guidance",
                    "plan accepted",
                    "setup_completed",
                    "setup_needs_user_input",
                    "setup_research_required",
                    "setup_blocked",
                    "setup_unresolved",
                    "setup_connection_failed",
                ],
                "accepted_response_shape": (
                    "First make the all-NULL next_call above. Then submit only one complete "
                    "board_setup-plan JSON object. Copy board_id and server-known action fields "
                    "from setup_overview.plan_action_parameters_template; obtain only its listed "
                    "user facts conversationally. For a setup continuation, copy the exact "
                    "accepted_response returned by board_setup into continue_setup."
                ),
                "common_remedies": [
                    "For friendly probe or UART ambiguity, ask which friendly label belongs to the board.",
                    "For official-source research, return only the fields requested by the continuation.",
                    "After completion, load and call board_validate; setup alone does not open the gate.",
                ],
                "relay_rule": relay_rule,
            },
        }
    if tool_name == "board_validate":
        arguments = {"board_id": board_id}
        if validation_probe_id is not None:
            arguments["probe_id"] = validation_probe_id
        return {
            "next_call": {"tool": tool_name, "arguments": arguments},
            "guidance": {
                "purpose": "Non-destructively prove live MCU identity and bind the current safety map.",
                "when_to_use": (
                    "Use only when there is no live proof (restart, initial setup, or not yet "
                    "validated), when connection identity changes (disconnect, reconnect, probe "
                    "or target override), or when hardware identity may have changed (identity "
                    "repair or destructive recovery)."
                ),
                "when_not_to_use": (
                    "Do not validate merely because of a build, flash, reset/halt, UART operation, "
                    "safety refresh, full map reconstruction, or bookkeeping change."
                ),
                "expected_statuses": list(VALIDATION_STATUSES),
                "accepted_response_shape": (
                    "If validation_needs_user_input is returned, ask the one friendly choice "
                    "question and copy its accepted_response object as the exact board_validate "
                    "retry. Terminal statuses have accepted_response null."
                ),
                "common_remedies": [
                    "Choose a returned friendly probe label without exposing its choice_id.",
                    "Use board_safety_refresh only for a stable-map problem.",
                    "For a mismatch, report expected and observed MCU identity and ask the user what to do.",
                ],
                "relay_rule": relay_rule,
            },
        }
    return {
        "next_call": {
            "tool": tool_name,
            "arguments": {"board_id": board_id},
        },
        "guidance": {
            "purpose": (
                "Deterministically rebuild the complete stable safety map from reviewed sources."
            ),
            "when_to_use": (
                "Use for a missing, malformed, old, or inconsistent map, or when reviewed MCU, "
                "target, geometry, partition policy, pack/SVD/target, datasheet, or schema evidence changes."
            ),
            "when_not_to_use": (
                "Do not use for an ordinary rebuild, artifact path/timestamp/size change, reset, "
                "flash, or UART use. It accepts no build artifacts or caller ranges and cannot "
                "create live identity authority."
            ),
            "expected_statuses": [
                "safety_refresh_completed",
                "safety_refresh_blocked",
            ],
            "accepted_response_shape": (
                "Call with only the server-generated board_id; follow validation_required exactly."
            ),
            "common_remedies": [
                "Resolve unavailable reviewed evidence and retry refresh.",
                "A missing reviewed partition is a maintainer evidence task, never a caller-range prompt.",
                "Run board_validate only when validation_required is true.",
            ],
            "relay_rule": relay_rule,
        },
    }


class SetupToolLoadState:
    """A-20 run-scoped, per-board setup-tool disclosure state."""

    def __init__(self, server_run: ServerRun) -> None:
        self._run = server_run
        self._loaded: set[tuple[str, str, str]] = set()
        self._allowance_by_board: dict[str, str] = {}
        self._guard = threading.RLock()

    def load(
        self,
        board_id: str,
        tool_name: str,
        *,
        validation_probe_id: str | None = None,
    ) -> dict[str, Any]:
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
        } | _load_guidance(board, tool_name, validation_probe_id=validation_probe_id)

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
    setup_overview: Callable[..., Mapping[str, Any]] | None = None
    setup_continue: Callable[[str, str, Mapping[str, object]], Mapping[str, Any]] | None = None
    setup_selections: Callable[[str], PreflightSelections] | None = None
    clear_setup_continuation: Callable[[str], None] | None = None
    setup_plan_eligible: Callable[[str], tuple[bool, str]] | None = None
    require_assignment: Callable[[str, str], None] | None = None
    assigned_connection: Callable[[str], str | None] | None = None


def build_setup_handlers(services: SetupToolServices) -> dict[str, Callable[..., str]]:
    """Build the Task-11 tool surface with A-20 redirects."""

    def load_setup_tool(board_id: str, tool_name: str) -> str:
        """Load detailed setup/validation guidance for one server-generated board_id.

        Call setup_overview first so the server, rather than the user, supplies the profile route,
        board_id, and friendly physical choices. Then load exactly the setup tool named by that
        route before calling it. Never ask the user for board_id, connection IDs, or JSON.
        """

        if tool_name == "board_setup-plan" and services.setup_plan_eligible is not None:
            eligible, reason = services.setup_plan_eligible(board_id)
            if not eligible:
                return _json(
                    {
                        "status": "setup_plan_ineligible",
                        "board_id": board_id,
                        "tool_name": tool_name,
                        "agent_prompt": reason,
                    }
                )
        validation_probe_id: str | None = None
        if tool_name == "board_validate":
            connection_id = (
                services.assigned_connection(board_id)
                if services.assigned_connection is not None
                else None
            )
            if connection_id is None or not connection_id.startswith("probe:"):
                return _json(
                    {
                        "status": "setup_assignment_required",
                        "board_id": board_id,
                        "tool_name": tool_name,
                        "agent_prompt": (
                            "Call setup_overview again for the user's familiar board names and "
                            "copy its exact load_call and next_call. Validation cannot proceed "
                            "without this run's board-to-probe assignment."
                        ),
                    }
                )
            validation_probe_id = connection_id.removeprefix("probe:")
        return _json(
            services.loader.load(
                board_id,
                tool_name,
                validation_probe_id=validation_probe_id,
            )
        )

    def setup_overview(
        board_names: list[str] | None = None,
        connection_assignments: dict[str, str] | None = None,
    ) -> str:
        """Inventory profiles/connections and route user-provided familiar board names.

        Call after initialization_handshake and after asking which boards the user wants to work
        with now. Pass those ordinary familiar names here; pass NULL only to inspect inventory
        before the user answers. Other visible debug probes may remain unassigned.
        The normalized literal sentinel "no board" must be passed by itself and is never a board
        name candidate. If it is mixed with names, re-ask the user conversationally.
        A complete matching profile routes to validation, an incomplete same-identity profile to
        repair, and a stable-map problem to safety refresh. Unknown names receive a
        server-generated board_id plus setup questions. Follow only the exact route returned here.
        Relay only agent_prompt and friendly choices, never raw identifiers or this JSON.
        """

        if services.setup_overview is None:
            return _json(
                {
                    "status": "setup_overview_unavailable",
                    "agent_prompt": "Setup inventory is unavailable; stop before hardware access.",
                }
            )
        if connection_assignments is None:
            return _json(services.setup_overview(board_names))
        return _json(services.setup_overview(board_names, connection_assignments))

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
        if (
            services.require_assignment is not None
            and board_id is not None
            and isinstance(action_parameters, dict)
            and isinstance(action_parameters.get("connection_id"), str)
        ):
            connection_id = action_parameters["connection_id"]
            assert isinstance(connection_id, str)
            services.require_assignment(board_id, connection_id)
        return services.plan_engine.submit("board_setup-plan", fields, session_id=None).message

    def _user_input(
        board_id: str,
        connection_id: str,
        display_name: str,
        mcu_part_number: str,
        requires_uart: bool,
        serial_baudrate: int | None,
        serial_id: str | None,
        datasheet_path: str,
    ) -> SetupUserInput:
        return SetupUserInput(
            board_id=board_id,
            connection_id=connection_id,
            display_name=display_name,
            mcu_part_number=mcu_part_number,
            requires_uart=requires_uart,
            serial_baudrate=serial_baudrate,
            datasheet_path=datasheet_path,
            serial_id=serial_id or "",
        )

    def board_setup(
        board_id: str,
        mode: str,
        connection_id: str,
        display_name: str,
        mcu_part_number: str,
        requires_uart: bool,
        serial_baudrate: int | None,
        serial_id: str | None,
        datasheet_path: str,
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
        if services.require_assignment is not None:
            services.require_assignment(board_id, connection_id)
        user_input = _user_input(
            board_id,
            connection_id,
            display_name,
            mcu_part_number,
            requires_uart,
            serial_baudrate,
            serial_id,
            datasheet_path,
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
        mcu_part_number: str,
        requires_uart: bool,
        serial_baudrate: int | None,
        serial_id: str | None,
        datasheet_path: str,
    ) -> str:
        """Use the setup plan's single paired repair allowance."""

        del (
            mode,
            connection_id,
            display_name,
            mcu_part_number,
            requires_uart,
            serial_baudrate,
            serial_id,
            datasheet_path,
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

    def board_validate(board_id: str, probe_id: str | None = None) -> str:
        """Validate a matching board YAML first, or validate after setup, repair, or reconnect.

        Trigger this instead of board_setup when the user's familiar board name already matches a
        healthy profile. A passing validation stamps only the current board/connection gate. If it
        fails because setup or safety evidence is incomplete, follow its exact setup or safety
        remedy; never treat the profile's presence alone as permission to access hardware.
        """

        if not services.loader.is_loaded(board_id, "board_validate"):
            return services.loader.redirect(board_id, "board_validate")
        if services.require_assignment is not None:
            if probe_id is None:
                raise ValueError(
                    "board_validate requires the probe_id returned by setup_overview for this "
                    "run-scoped board assignment"
                )
            services.require_assignment(board_id, f"probe:{probe_id}")
        result = services.validator.validate(ValidationRequest(board_id, probe_id))
        return _json(result.to_payload())

    def board_safety_refresh(board_id: str) -> str:
        """Rebuild the complete stable safety map from current replayed verified sources.

        Use this for any missing/corrupt/stale map or stable-authority change. Do not use it
        after ordinary firmware builds. It accepts no artifact, geometry, partition, or caller range.
        """

        if not services.loader.is_loaded(board_id, "board_safety_refresh"):
            return services.loader.redirect(board_id, "board_safety_refresh")
        return _json(services.safety_refresh(board_id))

    def get_setup_status(board_id: str) -> str:
        """Report whether durable setup and this run's live validated session are ready.

        Call this as the final barrier before an external coding workflow begins. It never opens
        a connection or gate and never treats persisted reports as authority. UART readiness is
        reported separately so a project that does not need a console is not blocked, while a
        console-dependent workflow can require ready_for_uart_work before it starts. For a known
        MCU, build_guidance returns the parameterized general native-build helper and
        collect_build_artifacts workflow. Inspect the project and supply its exact executable,
        argv, cwd, environment overrides, and outputs after preferring a compatible local toolchain.
        The helper accepts any build system, inherits network access when acquisition is needed, and
        applies best-effort common-client guards only when `--offline` is explicitly selected; it
        does not claim an OS network sandbox. All build guidance is
        advisory only: inspect the resulting ELF/HEX through the flash plan; refresh is only for
        stable-map problems.
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
        "board_safety_refresh": board_safety_refresh,
        "board_validate": board_validate,
        "get_setup_status": get_setup_status,
    }
