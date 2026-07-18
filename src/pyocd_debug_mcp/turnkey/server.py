"""Server A stdio MCP composition.

Server A exposes only turnkey load/agentic tools. It owns a fresh middleman process for each
agentic call and delegates physical-board access to the separately supervised Server B.
"""

from __future__ import annotations

import os
import re
import threading
import weakref
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from anyio.from_thread import run as run_from_thread
from anyio.to_thread import run_sync
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase
from pydantic import BaseModel, ConfigDict

from pyocd_debug_mcp.turnkey.contracts import (
    CallOwnedScript,
    TurnkeyContext,
    TurnkeyContractError,
    custom_steps,
)
from pyocd_debug_mcp.turnkey.controller import TurnkeyController
from pyocd_debug_mcp.turnkey.prompts import MEMORY_GUIDE, load_guide
from pyocd_debug_mcp.turnkey.provider import (
    EnvironmentMiddlemanFactory,
    MiddlemanFactory,
    MiddlemanSession,
)
from pyocd_debug_mcp.turnkey.schema_adapter import (
    register_dynamic_tool,
    replace_tool_parameters,
    tool_parameters,
)

_COMMON_NAMES = (
    "tool_summary",
    "task",
    "memory_tier1_turn1",
    "memory_tier1_turn2",
    "memory_tier1_turn3",
    "memory_tier1_turn4",
    "memory_tier2",
    "memory_tier3",
    "relevant_files",
    "board_facts",
    "reference_artifacts",
    "build_context",
    "iteration_max",
    "green_check_guide",
    "green_check_script",
    "green_check_expected_outputs",
)
_STEP_NAME = re.compile(r"step_[1-9][0-9]*\Z")
_MAX_CONTINUATIONS_PER_SESSION = 64


@dataclass(slots=True)
class _Continuation:
    context: TurnkeyContext
    workflow: tuple[str, ...] | None
    step_index: int = 0
    workflow_complete: bool = False
    last_result: str = ""
    failed_strategies: tuple[str, ...] = ()
    carry_forward_warnings: tuple[str, ...] = ()


@dataclass(slots=True, weakref_slot=True)
class _ClientState:
    unlocked: set[str]
    continuations: OrderedDict[tuple[str, str], _Continuation]
    execution_lock: threading.RLock

    @classmethod
    def create(cls) -> _ClientState:
        return cls(set(), OrderedDict(), threading.RLock())


class _ComplexTaskArguments(ArgModelBase):
    """Validation model that permits runtime-validated top-level ``step_n`` fields."""

    model_config = ConfigDict(extra="allow")

    tool_summary: str
    task: str
    memory_tier1_turn1: dict[str, str] | None = None
    memory_tier1_turn2: dict[str, str] | None = None
    memory_tier1_turn3: dict[str, str] | None = None
    memory_tier1_turn4: dict[str, str] | None = None
    memory_tier2: str | None = None
    memory_tier3: str | None = None
    relevant_files: str | None = None
    board_facts: str | None = None
    reference_artifacts: str | None = None
    build_context: str | None = None
    iteration_max: int | None = None
    green_check_guide: str | None = None
    green_check_script: CallOwnedScript | None = None
    green_check_expected_outputs: list[str] | None = None
    continue_instruction: str | None = None

    def model_dump_one_level(self) -> dict[str, Any]:
        values = super().model_dump_one_level()
        values.update(self.model_extra or {})
        return values


class _RelayAcknowledgement(BaseModel):
    continue_task: bool = True


class TurnkeyApplication:
    def __init__(self, controller: TurnkeyController) -> None:
        self.controller = controller
        self._states: weakref.WeakKeyDictionary[object, _ClientState] = (
            weakref.WeakKeyDictionary()
        )
        self._states_lock = threading.RLock()
        self.workspace = Path(os.environ.get("BYO_WORKSPACE_ROOT", Path.cwd())).resolve()
        self.server_b_url = os.environ.get("BYO_SERVER_B_URL", "http://127.0.0.1:8765/mcp")

    def state_for(self, session: object) -> _ClientState:
        with self._states_lock:
            state = self._states.get(session)
            if state is None:
                state = _ClientState.create()
                self._states[session] = state
            return state

    def load(self, state: _ClientState, action: str) -> dict[str, object]:
        state.unlocked.add(action)
        return {
            "status": "agentic_tool_unlocked",
            "action": action,
            "guidance": load_guide(action),
            "memory_construction": MEMORY_GUIDE,
            "next_call": action,
        }

    def _context(
        self,
        state: _ClientState,
        action: str,
        values: Mapping[str, object],
        *,
        specific_name: str | None = None,
    ) -> tuple[TurnkeyContext, tuple[str, str], bool]:
        if action not in state.unlocked:
            raise TurnkeyContractError(f"{action} is locked; call load_{action} first")
        summary = values.get("tool_summary")
        task = values.get("task")
        if not isinstance(summary, str) or not summary.strip():
            raise TurnkeyContractError("tool_summary must be non-empty text")
        if not isinstance(task, str) or not task.strip():
            raise TurnkeyContractError("task must be non-empty text")
        key = (action, task.strip())
        continuation = values.get("continue_instruction")
        supplied_context = [values.get(name) for name in _COMMON_NAMES[2:]]
        if specific_name is not None:
            supplied_context.append(values.get(specific_name))
        if continuation is not None:
            if any(item is not None for item in supplied_context):
                raise TurnkeyContractError(
                    "delta calls accept only tool_summary, task, and continue_instruction"
                )
            if not isinstance(continuation, str) or not continuation.strip():
                raise TurnkeyContractError("continue_instruction must be non-empty text")
            previous = state.continuations.get(key)
            if previous is None:
                raise TurnkeyContractError(
                    "no in-memory full context exists for this tool/task; submit the complete call"
                )
            return (
                replace(
                    previous.context,
                    tool_summary=summary.strip(),
                    task=task.strip(),
                ),
                key,
                True,
            )
        specific = values.get(specific_name) if specific_name else None
        if specific_name is not None and (
            not isinstance(specific, str) or not specific.strip()
        ):
            raise TurnkeyContractError(f"{specific_name} must be non-empty text")
        document = {name: values.get(name) for name in _COMMON_NAMES}
        context = TurnkeyContext.parse(document)
        if specific_name is not None:
            specific_text = cast(str, specific)
            context = replace(
                context,
                task_detail_label=specific_name.upper(),
                task_detail=specific_text.strip(),
            )
        return context, key, False

    def run(
        self,
        state: _ClientState,
        action: str,
        values: Mapping[str, object],
        *,
        specific_name: str | None = None,
        steps: tuple[str, ...] | None = None,
        relay_text: Callable[[str], bool] | None = None,
        session_observer: Callable[[MiddlemanSession | None], None] | None = None,
    ) -> dict[str, object]:
        try:
            with state.execution_lock:
                context, key, is_delta = self._context(
                    state, action, values, specific_name=specific_name
                )
                prior = state.continuations.get(key)
                workflow = steps
                if is_delta:
                    workflow = prior.workflow if prior is not None else None
                elif action == "complex_task" and workflow is None:
                    raise TurnkeyContractError(
                        "complex_task requires contiguous top-level step_1 through step_n fields"
                    )
                continuation = values.get("continue_instruction")
                result = self.controller.run(
                    tool_name=action,
                    context=context,
                    workspace=self.workspace,
                    server_b_url=self.server_b_url,
                    steps=workflow,
                    start_step_index=prior.step_index if is_delta and prior is not None else 0,
                    continue_instruction=(
                        str(continuation).strip() if continuation is not None else None
                    ),
                    prior_last_result=prior.last_result if is_delta and prior is not None else None,
                    prior_failed_strategies=(
                        prior.failed_strategies if is_delta and prior is not None else ()
                    ),
                    prior_carry_forward_warnings=(
                        prior.carry_forward_warnings if is_delta and prior is not None else ()
                    ),
                    prior_workflow_complete=(
                        prior.workflow_complete if is_delta and prior is not None else False
                    ),
                    relay_text=relay_text,
                    session_observer=session_observer,
                )
                if result.status == "completed":
                    state.continuations.pop(key, None)
                else:
                    state.continuations[key] = _Continuation(
                        context=context,
                        workflow=workflow,
                        step_index=result.step_index,
                        workflow_complete=result.workflow_complete,
                        last_result=result.last_result,
                        failed_strategies=result.failed_strategies,
                        carry_forward_warnings=result.carry_forward_warnings,
                    )
                    state.continuations.move_to_end(key)
                    while len(state.continuations) > _MAX_CONTINUATIONS_PER_SESSION:
                        state.continuations.popitem(last=False)
                return result.to_document()
        except TurnkeyContractError as exc:
            return {
                "status": "agentic_tool_refused",
                "message": str(exc),
                "remedy": f"call load_{action}, then submit the exact documented input",
            }
        except Exception as exc:
            return {
                "status": "agentic_tool_error",
                "message": (
                    f"agentic tool did not finish: {exc}; diagnose the issue and try again."
                ),
                "remedy": f"Inspect the operational failure, then retry {action} with the same task.",
            }


def _complex_task_schema() -> dict[str, object]:
    schema = _ComplexTaskArguments.model_json_schema()
    schema.pop("required", None)
    schema["patternProperties"] = {"^step_[1-9][0-9]*$": {"type": "string", "minLength": 1}}
    schema["additionalProperties"] = False
    full_context = list(_COMMON_NAMES) + ["step_1"]
    delta_forbidden = [
        {"required": [name]} for name in _COMMON_NAMES[2:]
    ]
    schema["oneOf"] = [
        {
            "required": full_context,
            "not": {"required": ["continue_instruction"]},
        },
        {
            "required": ["tool_summary", "task", "continue_instruction"],
            "not": {"anyOf": delta_forbidden},
            "propertyNames": {"not": {"pattern": "^step_[1-9][0-9]*$"}},
        },
    ]
    return schema


def _agentic_schema(base: Mapping[str, object], specific_name: str) -> dict[str, object]:
    """Describe the exact mutually-exclusive full and delta call forms."""

    schema = dict(base)
    schema.pop("required", None)
    forbidden = list(_COMMON_NAMES[2:]) + [specific_name]
    schema["oneOf"] = [
        {
            "required": [*_COMMON_NAMES, specific_name],
            "not": {"required": ["continue_instruction"]},
        },
        {
            "required": ["tool_summary", "task", "continue_instruction"],
            "not": {"anyOf": [{"required": [name]} for name in forbidden]},
        },
    ]
    return schema


def create_turnkey_server(provider_factory: MiddlemanFactory | None = None) -> FastMCP:
    factory = provider_factory or EnvironmentMiddlemanFactory()
    app = TurnkeyApplication(TurnkeyController(factory))
    mcp = FastMCP(
        "turnkey-brain",
        instructions=(
            "Call the matching load_* tool before an agentic tool. Server A owns the middleman "
            "loop; hardware access remains guarded by Server B."
        ),
    )

    @mcp.tool()
    def initialization_handshake() -> dict[str, object]:
        """Learn the exact Server A workflow before invoking a turnkey agent."""

        return {
            "status": "turnkey_ready",
            "workflow": (
                "Call load_bug_fix, load_complex_implementation, or load_complex_task; read "
                "its guide; then call the unlocked action with complete context."
            ),
            "server_b": "The middleman receives the supervised guarded hardware endpoint.",
            "server_b_client_endpoint": app.server_b_url,
            "client_topology": (
                "Client A should register this loopback Server B endpoint alongside Server A "
                "when it needs direct guarded hardware tools. The pyocd-turnkey command starts "
                "or reuses both servers."
            ),
            "success_rule": "finish_task requires Server A to validate the green check.",
        }

    @mcp.tool()
    def load_bug_fix(ctx: Context) -> dict[str, object]:
        """Load the purpose and complete input guide, then unlock bug_fix for this client."""

        return app.load(app.state_for(ctx.session), "bug_fix")

    @mcp.tool()
    def load_complex_implementation(ctx: Context) -> dict[str, object]:
        """Load the input guide and unlock complex_implementation for this client."""

        return app.load(app.state_for(ctx.session), "complex_implementation")

    @mcp.tool()
    def load_complex_task(ctx: Context) -> dict[str, object]:
        """Load the input guide and unlock complex_task for this client."""

        return app.load(app.state_for(ctx.session), "complex_task")

    def common_values(local: Mapping[str, Any]) -> dict[str, object]:
        return {key: value for key, value in local.items() if key not in {"ctx", "app", "mcp"}}

    async def invoke(
        ctx: Context,
        action: str,
        values: Mapping[str, object],
        *,
        specific_name: str | None = None,
        steps: tuple[str, ...] | None = None,
    ) -> dict[str, object]:
        state = app.state_for(ctx.session)
        bridge_lock = threading.Lock()
        active_session: MiddlemanSession | None = None
        cancelled = False

        def observe_session(session: MiddlemanSession | None) -> None:
            nonlocal active_session
            with bridge_lock:
                active_session = session
                session_to_close = session if cancelled else None
            if session_to_close is not None:
                session_to_close.close()

        def cancel_session() -> None:
            nonlocal cancelled
            with bridge_lock:
                cancelled = True
                session = active_session
            if session is not None:
                session.close()

        def relay(text: str) -> bool:
            result = run_from_thread(ctx.elicit, text, _RelayAcknowledgement)
            return result.action == "accept" and result.data.continue_task

        try:
            return await run_sync(
                lambda: app.run(
                    state,
                    action,
                    values,
                    specific_name=specific_name,
                    steps=steps,
                    relay_text=relay,
                    session_observer=observe_session,
                ),
                abandon_on_cancel=True,
            )
        except BaseException:
            cancel_session()
            raise

    @mcp.tool()
    async def bug_fix(
        ctx: Context,
        tool_summary: str,
        task: str,
        bug: str | None = None,
        memory_tier1_turn1: dict[str, str] | None = None,
        memory_tier1_turn2: dict[str, str] | None = None,
        memory_tier1_turn3: dict[str, str] | None = None,
        memory_tier1_turn4: dict[str, str] | None = None,
        memory_tier2: str | None = None,
        memory_tier3: str | None = None,
        relevant_files: str | None = None,
        board_facts: str | None = None,
        reference_artifacts: str | None = None,
        build_context: str | None = None,
        iteration_max: int | None = None,
        green_check_guide: str | None = None,
        green_check_script: CallOwnedScript | None = None,
        green_check_expected_outputs: list[str] | None = None,
        continue_instruction: str | None = None,
    ) -> dict[str, object]:
        """Run the fixed diagnose-to-green-check turnkey bug-fix workflow."""

        return await invoke(ctx, "bug_fix", common_values(locals()), specific_name="bug")

    @mcp.tool()
    async def complex_implementation(
        ctx: Context,
        tool_summary: str,
        task: str,
        feature: str | None = None,
        memory_tier1_turn1: dict[str, str] | None = None,
        memory_tier1_turn2: dict[str, str] | None = None,
        memory_tier1_turn3: dict[str, str] | None = None,
        memory_tier1_turn4: dict[str, str] | None = None,
        memory_tier2: str | None = None,
        memory_tier3: str | None = None,
        relevant_files: str | None = None,
        board_facts: str | None = None,
        reference_artifacts: str | None = None,
        build_context: str | None = None,
        iteration_max: int | None = None,
        green_check_guide: str | None = None,
        green_check_script: CallOwnedScript | None = None,
        green_check_expected_outputs: list[str] | None = None,
        continue_instruction: str | None = None,
    ) -> dict[str, object]:
        """Run the fixed requirement-to-green-check implementation workflow."""

        return await invoke(
            ctx,
            "complex_implementation",
            common_values(locals()),
            specific_name="feature",
        )

    async def complex_task(ctx: Context, **values: Any) -> dict[str, object]:
        """Run caller-authored top-level step_1 through step_n fields in order."""

        unknown = set(values) - set(_COMMON_NAMES) - {"continue_instruction"}
        step_names = {name for name in unknown if _STEP_NAME.fullmatch(name)}
        unsupported = unknown - step_names
        if unsupported:
            return {
                "status": "agentic_tool_refused",
                "message": f"unknown complex_task fields: {sorted(unsupported)}",
                "remedy": "submit only documented common fields and top-level step_n fields",
            }
        try:
            normalized_steps = None
            if values.get("continue_instruction") is None:
                normalized_steps = custom_steps({name: values[name] for name in step_names})
        except TurnkeyContractError as exc:
            return {
                "status": "agentic_tool_refused",
                "message": str(exc),
                "remedy": "submit contiguous top-level step_1 through step_n text fields",
            }
        return await invoke(ctx, "complex_task", values, steps=normalized_steps)

    register_dynamic_tool(
        mcp,
        complex_task,
        name="complex_task",
        description=complex_task.__doc__,
        arg_model=_ComplexTaskArguments,
        parameters=_complex_task_schema(),
    )

    for action, specific_name in (
        ("bug_fix", "bug"),
        ("complex_implementation", "feature"),
    ):
        replace_tool_parameters(
            mcp,
            action,
            _agentic_schema(tool_parameters(mcp, action), specific_name),
        )

    return mcp


def main() -> None:
    create_turnkey_server().run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
