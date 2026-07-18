"""Run-scoped immutable plans with exact binding and atomic budget consumption."""

from __future__ import annotations

import json
import math
import re
import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from pyocd_debug_mcp.guardrails.plan_defs import (
    BudgetMode,
    FieldDefinition,
    FieldType,
    PermissionMode,
    PlanDefinition,
    definition_for_action,
    definition_for_plan_tool,
)
from pyocd_debug_mcp.kernel.run_state import ServerRun
from pyocd_debug_mcp.services.session_runtime import PolicyRefusal

MAX_FLEXIBLE_CALLS = 20
MAX_FLEXIBLE_BUFFER = 10
_BOARD_ID = re.compile(r"[a-z0-9_]{1,64}")
_PLACEHOLDERS = frozenset(
    {"n/a", "na", "none", "null", "placeholder", "tbd", "todo", "unknown"}
)


class PlanRefusal(PolicyRefusal):
    """A plan or planned execution failed deterministic validation."""


class PlanStatus(str, Enum):
    ACTIVE = "active"
    EXHAUSTED = "exhausted"
    REPLACED = "replaced"
    INVALIDATED = "invalidated"
    RUN_CLOSED = "run-closed"


class PlanLockRegistry(Protocol):
    def is_registered(self, name: str) -> bool: ...

    def unlock(self, name: str, board_id: str) -> None: ...

    def relock(self, name: str, board_id: str) -> None: ...

    def is_unlocked(self, name: str, board_id: str | None) -> bool: ...


class PermissionProvider(Protocol):
    """Task 6 extension point; implementations own permission grant semantics."""

    def null_disclosure(self, definition: PlanDefinition) -> str | None: ...

    def authorize_plan(
        self,
        definition: PlanDefinition,
        board_id: str,
        user_permission: object,
        max_calls: int,
        max_calls_buffer: int,
    ) -> object: ...

    def validate_execution(
        self,
        definition: PlanDefinition,
        board_id: str,
        authorization: object,
    ) -> None: ...

    def consume_execution(
        self,
        definition: PlanDefinition,
        board_id: str,
        authorization: object,
    ) -> None: ...

    def reset(self) -> None: ...


class UnavailablePermissionProvider:
    """Fail closed until Task 6 injects a real run-scoped permission store."""

    def null_disclosure(self, definition: PlanDefinition) -> str | None:
        del definition
        return "No full-session permission provider is active yet."

    def authorize_plan(
        self,
        definition: PlanDefinition,
        board_id: str,
        user_permission: object,
        max_calls: int,
        max_calls_buffer: int,
    ) -> object:
        del board_id, user_permission, max_calls, max_calls_buffer
        raise PlanRefusal(
            "permission/provider-unavailable",
            f"{definition.plan_tool_name} requires the injected permission provider from Task 6.",
        )

    def validate_execution(
        self,
        definition: PlanDefinition,
        board_id: str,
        authorization: object,
    ) -> None:
        del definition, board_id, authorization

    def consume_execution(
        self,
        definition: PlanDefinition,
        board_id: str,
        authorization: object,
    ) -> None:
        del definition, board_id, authorization

    def reset(self) -> None:
        return


ExecutionScopeValidator = Callable[[PlanDefinition, str, str | None], None]
PreExecutionCheck = Callable[[], None]


@dataclass(frozen=True, slots=True)
class ActivePlan:
    plan_id: str
    run_id: str
    action_name: str
    plan_tool_name: str
    board_id: str
    session_id: str | None
    max_calls: int
    max_calls_buffer: int
    remaining_calls: int
    paired_remaining: tuple[tuple[str, int], ...]
    status: PlanStatus
    canonical_parameters: str
    canonical_plan_fields: str
    close_reason: str | None = None

    @property
    def total_calls(self) -> int:
        return self.max_calls + self.max_calls_buffer + sum(
            remaining for _, remaining in self.paired_remaining
        )

    def remaining_for(self, action_name: str) -> int:
        """Return the remaining allowance for the primary or one paired action."""

        if action_name == self.action_name:
            return self.remaining_calls
        return dict(self.paired_remaining).get(action_name, 0)

    @property
    def action_parameters(self) -> dict[str, object]:
        value = json.loads(self.canonical_parameters)
        assert isinstance(value, dict)
        return value

    @property
    def submitted_fields(self) -> dict[str, object]:
        value = json.loads(self.canonical_plan_fields)
        assert isinstance(value, dict)
        return value


@dataclass(frozen=True, slots=True)
class PlanResult:
    status: str
    message: str
    plan: ActivePlan | None = None


def accepted_plan_payload(plan: ActivePlan) -> dict[str, object]:
    """Return exact direct and static-client calls derived from an accepted snapshot."""

    parameters = plan.action_parameters
    if "board_id" in parameters:
        raise RuntimeError("plan action_parameters must not contain board_id")

    def execution_call(action_name: str) -> dict[str, object]:
        return {
            "tool_name": action_name,
            "arguments": {"board_id": plan.board_id, **parameters},
        }

    def batch_call(action_name: str) -> dict[str, object]:
        fallback: dict[str, object] = {
            "tool_name": "action_batch",
            "arguments": {
                "board_id": plan.board_id,
                "actions": [execution_call(action_name)],
            },
        }
        arguments = fallback["arguments"]
        actions = arguments.get("actions") if isinstance(arguments, dict) else None
        if not isinstance(actions, list) or len(actions) != 1:
            raise RuntimeError("generated plan fallback must contain exactly one child")
        return fallback

    definition = definition_for_action(plan.action_name)
    return {
        "status": "plan_accepted",
        "message": (
            f"Accepted plan {plan.plan_id} for {plan.action_name} on {plan.board_id}. "
            f"Total permitted calls: {plan.total_calls}. Prefer the direct call when the client "
            "exposes it. If callable bindings remain static, submit only the exact returned "
            "single-child action_batch fallback unchanged; never invent a hidden tool call."
        ),
        "plan_id": plan.plan_id,
        "underlying_action": plan.action_name,
        "total_calls": plan.total_calls,
        "preferred_call": execution_call(plan.action_name),
        "stable_client_fallback": batch_call(plan.action_name),
        "paired_action_fallbacks": [
            {
                "action": action_name,
                "use_only_when": (
                    "Use only after the primary action returns an eligible paired-action redirect; "
                    "never execute it optimistically with the primary action."
                ),
                "call": batch_call(action_name),
            }
            for action_name in definition.paired_actions
        ],
    }


@dataclass(frozen=True, slots=True)
class ValidatedPlanSubmission:
    """A complete schema-checked submission that grants no execution authority."""

    definition: PlanDefinition
    board_id: str
    max_calls: int
    max_calls_buffer: int
    canonical_parameters: str
    canonical_plan_fields: str


@dataclass(slots=True)
class _PlanState:
    plan_id: str
    run_id: str
    definition: PlanDefinition
    board_id: str
    session_id: str | None
    max_calls: int
    max_calls_buffer: int
    remaining_calls: int
    paired_remaining: dict[str, int]
    canonical_parameters: str
    canonical_plan_fields: str
    authorization: object | None
    status: PlanStatus = PlanStatus.ACTIVE
    close_reason: str | None = None

    def snapshot(self) -> ActivePlan:
        return ActivePlan(
            plan_id=self.plan_id,
            run_id=self.run_id,
            action_name=self.definition.action_name,
            plan_tool_name=self.definition.plan_tool_name,
            board_id=self.board_id,
            session_id=self.session_id,
            max_calls=self.max_calls,
            max_calls_buffer=self.max_calls_buffer,
            remaining_calls=self.remaining_calls,
            paired_remaining=tuple(sorted(self.paired_remaining.items())),
            status=self.status,
            canonical_parameters=self.canonical_parameters,
            canonical_plan_fields=self.canonical_plan_fields,
            close_reason=self.close_reason,
        )


def _refuse(code: str, message: str, *, session_id: str | None = None) -> PlanRefusal:
    return PlanRefusal(code, message, session_id=session_id)


def _definition(tool_name: str) -> PlanDefinition:
    try:
        if tool_name.endswith("-plan"):
            return definition_for_plan_tool(tool_name)
        return definition_for_action(tool_name)
    except KeyError as exc:
        raise _refuse("plan/unknown-tool", str(exc)) from exc


def _validate_json(value: object, location: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _refuse("plan/invalid-json", f"{location} must not contain NaN or infinity")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json(item, f"{location}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise _refuse("plan/invalid-json", f"{location} object keys must be strings")
            _validate_json(item, f"{location}.{key}")
        return
    raise _refuse(
        "plan/invalid-json",
        f"{location} must be JSON-representable, not {type(value).__name__}",
    )


def canonical_json(value: object) -> str:
    """Return a type-preserving, stable binding for an MCP JSON value."""

    _validate_json(value, "plan value")
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _field_error(field: FieldDefinition, value: object) -> str | None:
    if value is None:
        return None if field.nullable else "must not be NULL"
    field_type = field.field_type
    type_error: str | None = None
    if field_type is FieldType.TEXT:
        type_error = (
            None
            if isinstance(value, str) and (field.allow_empty or value.strip())
            else "must be non-empty text"
        )
    elif field_type is FieldType.INTEGER:
        type_error = (
            None if isinstance(value, int) and not isinstance(value, bool) else "must be an integer"
        )
    elif field_type is FieldType.NUMBER:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "must be a finite number"
        type_error = None if math.isfinite(float(value)) else "must be a finite number"
    elif field_type is FieldType.BOOLEAN:
        type_error = None if isinstance(value, bool) else "must be a boolean"
    elif field_type is FieldType.ARRAY:
        type_error = None if isinstance(value, list) else "must be an array"
    elif field_type is FieldType.OBJECT:
        type_error = None if isinstance(value, dict) else "must be an object"
    elif field_type is FieldType.TEXT_OR_INTEGER:
        if isinstance(value, int) and not isinstance(value, bool):
            type_error = None
        else:
            type_error = (
                None if isinstance(value, str) and value.strip() else "must be text or an integer"
            )
    elif field_type is FieldType.JSON:
        try:
            _validate_json(value, field.name)
        except PlanRefusal as exc:
            return exc.message
    else:  # pragma: no cover
        return "has an unsupported field type"
    if type_error is not None:
        return type_error
    if field.choices and not any(
        type(value) is type(choice) and value == choice for choice in field.choices
    ):
        return f"must be one of {field.choices}"
    if field.minimum is not None and isinstance(value, (int, float)) and not isinstance(value, bool):
        if field.exclusive_minimum and value <= field.minimum:
            return f"must be greater than {field.minimum:g}"
        if not field.exclusive_minimum and value < field.minimum:
            return f"must be at least {field.minimum:g}"
    if field.maximum is not None and isinstance(value, (int, float)) and value > field.maximum:
        return f"must be at most {field.maximum:g}"
    if field.min_items is not None and isinstance(value, list) and len(value) < field.min_items:
        return f"must contain at least {field.min_items} item(s)"
    if field.max_items is not None and isinstance(value, list) and len(value) > field.max_items:
        return f"must contain at most {field.max_items} item(s)"
    return None


class PlanEngine:
    """Own one Server Run's plan initialization, binding, and call budgets."""

    def __init__(
        self,
        server_run: ServerRun,
        registry: PlanLockRegistry,
        *,
        permission_provider: PermissionProvider | None = None,
        scope_validator: ExecutionScopeValidator | None = None,
    ) -> None:
        self.server_run = server_run
        self.registry = registry
        self.permission_provider = permission_provider or UnavailablePermissionProvider()
        self.scope_validator = scope_validator or (lambda definition, board_id, session_id: None)
        self._initialized_plan_tools: set[str] = set()
        self._accepted_cycles: dict[tuple[str, str], int] = {}
        self._guard = threading.RLock()

    def null_response(self, tool_name: str) -> PlanResult:
        definition = _definition(tool_name)
        disclosure = (
            self.permission_provider.null_disclosure(definition)
            if definition.permission_mode is not PermissionMode.NONE
            else None
        )
        with self._guard:
            self._initialized_plan_tools.add(definition.plan_tool_name)
        return PlanResult(
            status="initialized",
            message=definition.render_null_response(disclosure),
        )

    def submit(
        self,
        tool_name: str,
        fields: Mapping[str, object],
        *,
        session_id: str | None = None,
        plan_id_override: str | None = None,
    ) -> PlanResult:
        definition = _definition(tool_name)
        expected_names = set(definition.plan_field_names)
        null_names = set(definition.null_field_names)
        supplied_names = set(fields)
        if supplied_names == null_names and all(value is None for value in fields.values()):
            return self.null_response(definition.plan_tool_name)
        if fields and all(value is None for value in fields.values()):
            missing = sorted(null_names - supplied_names)
            unknown = sorted(supplied_names - null_names)
            raise _refuse(
                "plan/invalid-null-call",
                f"The all-NULL call must include every plan field. Missing={missing}; unknown={unknown}.",
                session_id=session_id,
            )

        with self._guard:
            if definition.plan_tool_name not in self._initialized_plan_tools:
                raise _refuse(
                    "plan/not-initialized",
                    f"Call {definition.plan_tool_name} once with every parameter NULL before "
                    "submitting a populated plan.",
                    session_id=session_id,
                )

        missing = sorted(expected_names - supplied_names)
        unknown = sorted(supplied_names - expected_names)
        if missing or unknown:
            raise _refuse(
                "plan/incomplete",
                f"Complete plan required. Missing={missing}; unknown={unknown}.",
                session_id=session_id,
            )
        errors = {
            field.name: error
            for field in definition.plan_fields
            if (error := _field_error(field, fields[field.name])) is not None
        }
        if errors:
            raise _refuse(
                "plan/invalid-fields",
                f"Invalid plan fields: {errors}",
                session_id=session_id,
            )
        self._validate_reasoning(fields, session_id=session_id)
        board_id = self._validate_board_id(fields["board_id"], session_id=session_id)
        max_calls, max_calls_buffer = self._validate_budget(
            definition,
            fields["max_calls"],
            fields["max_calls_buffer"],
            session_id=session_id,
        )
        action_parameters = self._validate_action_parameters(
            definition,
            fields["action_parameters"],
            session_id=session_id,
        )
        canonical_parameters = canonical_json(action_parameters)
        canonical_plan_fields = canonical_json(dict(fields))

        with self._guard:
            key = (definition.action_name, board_id)
            cycles = self._accepted_cycles.get(key, 0)
            if (
                definition.max_plan_cycles_per_board is not None
                and cycles >= definition.max_plan_cycles_per_board
            ):
                self._invalidate_locked(key, "deterministic plan-cycle budget exhausted")
                raise _refuse(
                    "plan/retry-exhausted",
                    f"{definition.plan_tool_name} reached its per-board limit of "
                    f"{definition.max_plan_cycles_per_board} plan cycles for this Server Run.",
                    session_id=session_id,
                )

            authorization: object | None = None
            if definition.permission_mode is not PermissionMode.NONE:
                authorization = self.permission_provider.authorize_plan(
                    definition,
                    board_id,
                    fields["user_permission"],
                    max_calls,
                    max_calls_buffer,
                )
            state = _PlanState(
                plan_id=self._plan_id(plan_id_override),
                run_id=self.server_run.run_id,
                definition=definition,
                board_id=board_id,
                session_id=session_id,
                max_calls=max_calls,
                max_calls_buffer=max_calls_buffer,
                remaining_calls=max_calls + max_calls_buffer,
                paired_remaining={action: 1 for action in definition.paired_actions},
                canonical_parameters=canonical_parameters,
                canonical_plan_fields=canonical_plan_fields,
                authorization=authorization,
            )
            previous = self.server_run.plans.get(key)
            self.server_run.plans[key] = state
            try:
                self._unlock_definition(definition, board_id)
            except BaseException:
                if previous is None:
                    self.server_run.plans.pop(key, None)
                else:
                    self.server_run.plans[key] = previous
                raise
            if isinstance(previous, _PlanState) and previous.status is PlanStatus.ACTIVE:
                previous.status = PlanStatus.REPLACED
                previous.close_reason = f"replaced by {state.plan_id}"
            self._accepted_cycles[key] = cycles + 1
            snapshot = state.snapshot()

        payload = accepted_plan_payload(snapshot)
        return PlanResult(
            status="accepted",
            message=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            plan=snapshot,
        )

    def preview_submission(
        self,
        tool_name: str,
        fields: Mapping[str, object],
        *,
        session_id: str | None = None,
    ) -> ValidatedPlanSubmission:
        """Validate a populated plan without permission, activation, or visibility changes.

        Destructive recovery uses this to render a plan-id-bound disclosure before
        fresh approval exists. The ordinary :meth:`submit` path remains the only
        operation that creates an active plan.
        """

        definition = _definition(tool_name)
        expected_names = set(definition.plan_field_names)
        supplied_names = set(fields)
        with self._guard:
            if definition.plan_tool_name not in self._initialized_plan_tools:
                raise _refuse(
                    "plan/not-initialized",
                    f"Call {definition.plan_tool_name} once with every parameter NULL before "
                    "submitting a populated plan.",
                    session_id=session_id,
                )
        missing = sorted(expected_names - supplied_names)
        unknown = sorted(supplied_names - expected_names)
        if missing or unknown:
            raise _refuse(
                "plan/incomplete",
                f"Complete plan required. Missing={missing}; unknown={unknown}.",
                session_id=session_id,
            )
        if all(value is None for value in fields.values()):
            raise _refuse(
                "plan/preview-null",
                "A disclosure preview requires a populated complete plan.",
                session_id=session_id,
            )
        errors = {
            field.name: error
            for field in definition.plan_fields
            if (error := _field_error(field, fields[field.name])) is not None
        }
        if errors:
            raise _refuse(
                "plan/invalid-fields",
                f"Invalid plan fields: {errors}",
                session_id=session_id,
            )
        self._validate_reasoning(fields, session_id=session_id)
        board_id = self._validate_board_id(fields["board_id"], session_id=session_id)
        max_calls, max_calls_buffer = self._validate_budget(
            definition,
            fields["max_calls"],
            fields["max_calls_buffer"],
            session_id=session_id,
        )
        action_parameters = self._validate_action_parameters(
            definition,
            fields["action_parameters"],
            session_id=session_id,
        )
        return ValidatedPlanSubmission(
            definition,
            board_id,
            max_calls,
            max_calls_buffer,
            canonical_json(action_parameters),
            canonical_json(dict(fields)),
        )

    @staticmethod
    def _validate_action_parameters(
        definition: PlanDefinition,
        value: object,
        *,
        session_id: str | None,
    ) -> dict[str, object]:
        if not isinstance(value, dict):
            raise _refuse(
                "plan/invalid-action-parameters",
                "action_parameters must be one nested JSON object, not prose, a wrapper, or "
                "flattened action fields.",
                session_id=session_id,
            )
        expected = {field.name for field in definition.action_fields}
        supplied = set(value)
        missing = sorted(expected - supplied)
        unknown = sorted(supplied - expected)
        if missing or unknown:
            raise _refuse(
                "plan/invalid-action-parameters",
                f"action_parameters must match the underlying action exactly. Missing={missing}; "
                f"unknown={unknown}.",
                session_id=session_id,
            )
        errors = {
            field.name: error
            for field in definition.action_fields
            if (error := _field_error(field, value[field.name])) is not None
        }
        if errors:
            raise _refuse(
                "plan/invalid-action-parameters",
                f"Invalid action_parameters fields: {errors}",
                session_id=session_id,
            )
        if definition.action_validator is not None:
            semantic_error = definition.action_validator(value)
            if semantic_error is not None:
                raise _refuse(
                    "plan/invalid-action-parameters",
                    f"Invalid action_parameters: {semantic_error}.",
                    session_id=session_id,
                )
        _validate_json(value, "action_parameters")
        return dict(value)

    def _plan_id(self, override: str | None) -> str:
        if override is None:
            return f"plan-{secrets.token_hex(8)}"
        if re.fullmatch(r"plan-[0-9a-f]{16}", override) is None:
            raise _refuse("plan/invalid-id", "Internal plan identifier is malformed.")
        if any(
            isinstance(state, _PlanState) and state.plan_id == override
            for state in self.server_run.plans.values()
        ):
            raise _refuse("plan/duplicate-id", "Internal plan identifier is already in use.")
        return override

    @staticmethod
    def _validate_reasoning(
        fields: Mapping[str, object],
        *,
        session_id: str | None,
    ) -> None:
        if fields["hypothesis_made"] is not True or fields["strategy_evaluated"] is not True:
            raise _refuse(
                "plan/reasoning-flags",
                "hypothesis_made and strategy_evaluated must both be true.",
                session_id=session_id,
            )
        for name in (
            "hypothesis",
            "strategy",
            "expected_fail_return",
            "expected_success_return",
        ):
            value = fields[name]
            assert isinstance(value, str)
            if value.strip().casefold() in _PLACEHOLDERS:
                raise _refuse(
                    "plan/placeholder-reasoning",
                    f"{name} must be concrete, not placeholder text.",
                    session_id=session_id,
                )

    @staticmethod
    def _validate_board_id(value: object, *, session_id: str | None) -> str:
        assert isinstance(value, str)
        if not _BOARD_ID.fullmatch(value):
            raise _refuse(
                "plan/invalid-board",
                "board_id must be 1-64 lowercase letters, numbers, or underscores.",
                session_id=session_id,
            )
        return value

    @staticmethod
    def _validate_budget(
        definition: PlanDefinition,
        max_calls_value: object,
        buffer_value: object,
        *,
        session_id: str | None,
    ) -> tuple[int, int]:
        assert isinstance(max_calls_value, int) and not isinstance(max_calls_value, bool)
        assert isinstance(buffer_value, int) and not isinstance(buffer_value, bool)
        max_calls = max_calls_value
        max_calls_buffer = buffer_value
        if max_calls < 1 or max_calls_buffer < 0:
            raise _refuse(
                "plan/invalid-budget",
                "max_calls must be at least 1 and max_calls_buffer must be at least 0.",
                session_id=session_id,
            )
        if definition.budget_mode is BudgetMode.FIXED:
            if (max_calls, max_calls_buffer) != (1, 0):
                raise _refuse(
                    "plan/fixed-budget",
                    f"{definition.plan_tool_name} requires max_calls=1 and "
                    "max_calls_buffer=0.",
                    session_id=session_id,
                )
        elif max_calls > MAX_FLEXIBLE_CALLS or max_calls_buffer > MAX_FLEXIBLE_BUFFER:
            raise _refuse(
                "plan/budget-cap",
                f"Flexible budgets are capped at max_calls<={MAX_FLEXIBLE_CALLS} and "
                f"max_calls_buffer<={MAX_FLEXIBLE_BUFFER}; submit a smaller plan.",
                session_id=session_id,
            )
        return max_calls, max_calls_buffer

    def enforce(
        self,
        action_name: str,
        board_id: str,
        parameters: Mapping[str, object],
        *,
        session_id: str | None = None,
        preconditions: PreExecutionCheck | None = None,
    ) -> ActivePlan:
        """Validate every precondition, then consume once at execution start."""

        definition = _definition(action_name)
        normalized_board = self._validate_board_id(board_id, session_id=session_id)
        expected_names = {field.name for field in definition.action_fields}
        supplied_names = set(parameters)
        if supplied_names != expected_names:
            raise _refuse(
                "plan/parameter-mismatch",
                f"Action parameters differ from the plan schema. Missing="
                f"{sorted(expected_names - supplied_names)}; unknown="
                f"{sorted(supplied_names - expected_names)}.",
                session_id=session_id,
            )
        errors = {
            field.name: error
            for field in definition.action_fields
            if (error := _field_error(field, parameters[field.name])) is not None
        }
        if errors:
            raise _refuse(
                "plan/parameter-mismatch",
                f"Action parameters are invalid: {errors}",
                session_id=session_id,
            )
        canonical_parameters = canonical_json(dict(parameters))
        key = (definition.action_name, normalized_board)
        with self._guard:
            state = self._precheck_locked(
                key,
                definition,
                canonical_parameters,
                session_id,
                invoked_action=action_name,
            )
            plan_id = state.plan_id

        try:
            self.scope_validator(definition, normalized_board, session_id)
        except PolicyRefusal:
            with self._guard:
                current = self.server_run.plans.get(key)
                if isinstance(current, _PlanState) and current.plan_id == plan_id:
                    self._invalidate_locked(key, "board assignment or session is no longer valid")
            raise
        with self._guard:
            state = self._precheck_locked(
                key,
                definition,
                canonical_parameters,
                session_id,
                invoked_action=action_name,
                expected_plan_id=plan_id,
            )
            if definition.permission_mode is not PermissionMode.NONE:
                assert state.authorization is not None
                self.permission_provider.validate_execution(
                    definition,
                    normalized_board,
                    state.authorization,
                )
        if preconditions is not None:
            preconditions()

        with self._guard:
            state = self._precheck_locked(
                key,
                definition,
                canonical_parameters,
                session_id,
                invoked_action=action_name,
                expected_plan_id=plan_id,
            )
            if definition.permission_mode is not PermissionMode.NONE:
                assert state.authorization is not None
                self.permission_provider.validate_execution(
                    definition,
                    normalized_board,
                    state.authorization,
                )
            if action_name == definition.action_name:
                state.remaining_calls -= 1
            else:
                state.paired_remaining[action_name] -= 1
            exhausted = state.remaining_calls == 0 and not any(
                state.paired_remaining.values()
            )
            if exhausted:
                if definition.permission_mode is not PermissionMode.NONE:
                    assert state.authorization is not None
                    self.permission_provider.consume_execution(
                        definition,
                        normalized_board,
                        state.authorization,
                    )
                state.status = PlanStatus.EXHAUSTED
                state.close_reason = "primary and paired call allowances exhausted at execution start"
                self._relock_definition(definition, normalized_board)
            return state.snapshot()

    def _precheck_locked(
        self,
        key: tuple[str, str],
        definition: PlanDefinition,
        canonical_parameters: str,
        session_id: str | None,
        *,
        invoked_action: str,
        expected_plan_id: str | None = None,
    ) -> _PlanState:
        state = self.server_run.plans.get(key)
        if not isinstance(state, _PlanState) or state.status is not PlanStatus.ACTIVE:
            raise _refuse(
                "plan/no-active-plan",
                f"No active plan covers {definition.action_name} for board '{key[1]}'. Call "
                f"{definition.plan_tool_name} first.",
                session_id=session_id,
            )
        if expected_plan_id is not None and state.plan_id != expected_plan_id:
            raise _refuse(
                "plan/replaced",
                f"Plan {expected_plan_id} was replaced before execution began.",
                session_id=session_id,
            )
        if state.canonical_parameters != canonical_parameters:
            raise _refuse(
                "plan/parameter-mismatch",
                "The underlying action parameters differ from the immutable plan binding.",
                session_id=session_id,
            )
        if state.run_id != self.server_run.run_id:
            self._invalidate_locked(key, "plan belongs to a different Server Run")
            raise _refuse(
                "plan/run-mismatch",
                "The active plan belongs to a different Server Run; submit a new plan.",
                session_id=session_id,
            )
        if invoked_action == definition.action_name:
            action_remaining = state.remaining_calls
        elif invoked_action in definition.paired_actions:
            action_remaining = state.paired_remaining.get(invoked_action, 0)
        else:  # pragma: no cover - definition lookup already constrains this branch
            action_remaining = 0
        if action_remaining <= 0:
            raise _refuse(
                "plan/action-exhausted",
                f"Plan {state.plan_id} has no remaining {invoked_action} allowance; submit a "
                "replacement plan if more work is required.",
                session_id=session_id,
            )
        if state.session_id != session_id:
            self._invalidate_locked(key, "session changed")
            raise _refuse(
                "plan/session-mismatch",
                "The board session changed; the plan was invalidated and must be replaced.",
                session_id=session_id,
            )
        lock_name = (
            invoked_action
            if self.registry.is_registered(invoked_action)
            else definition.action_name
        )
        if not self.registry.is_unlocked(lock_name, key[1]):
            raise _refuse(
                "plan/tool-locked",
                f"{invoked_action} is physically locked; call "
                f"{definition.plan_tool_name} again.",
                session_id=session_id,
            )
        return state

    def complete_paired_plan(self, action_name: str, board_id: str, reason: str) -> None:
        """Close a paired workflow early and consume any one-time authorization.

        Setup calls this when it completes, is cancelled, or reaches a terminal
        stop before both statically granted calls are used. Full-session grants
        remain reusable because their provider consumption is intentionally a no-op.
        """

        definition = _definition(action_name)
        key = (definition.action_name, board_id)
        with self._guard:
            state = self.server_run.plans.get(key)
            if not isinstance(state, _PlanState) or state.status is not PlanStatus.ACTIVE:
                return
            authorization = state.authorization
        if definition.permission_mode is not PermissionMode.NONE and authorization is not None:
            try:
                self.permission_provider.consume_execution(
                    definition,
                    board_id,
                    authorization,
                )
            except PolicyRefusal:
                # Revocation may already have removed the grant; closure still relocks.
                pass
        with self._guard:
            self._invalidate_locked(key, reason)

    def active_plan(self, action_name: str, board_id: str) -> ActivePlan | None:
        definition = _definition(action_name)
        key = (definition.action_name, board_id)
        with self._guard:
            state = self.server_run.plans.get(key)
            if not isinstance(state, _PlanState) or state.status is not PlanStatus.ACTIVE:
                return None
            return state.snapshot()

    def invalidate(self, action_name: str, board_id: str, reason: str) -> None:
        definition = _definition(action_name)
        with self._guard:
            self._invalidate_locked((definition.action_name, board_id), reason)

    def invalidate_board(self, board_id: str, reason: str) -> None:
        with self._guard:
            keys = [
                key
                for key, state in self.server_run.plans.items()
                if isinstance(key, tuple)
                and len(key) == 2
                and key[1] == board_id
                and isinstance(state, _PlanState)
            ]
            for key in keys:
                self._invalidate_locked(key, reason)

    def _invalidate_locked(self, key: tuple[str, str], reason: str) -> None:
        state = self.server_run.plans.get(key)
        if not isinstance(state, _PlanState) or state.status is not PlanStatus.ACTIVE:
            return
        state.status = PlanStatus.INVALIDATED
        state.close_reason = reason
        self._relock_definition(state.definition, state.board_id)

    def _unlock_definition(self, definition: PlanDefinition, board_id: str) -> None:
        unlocked: list[str] = []
        try:
            for action in (definition.action_name, *definition.paired_actions):
                if action != definition.action_name and not self.registry.is_registered(action):
                    continue
                self.registry.unlock(action, board_id)
                unlocked.append(action)
        except BaseException:
            for action in reversed(unlocked):
                self.registry.relock(action, board_id)
            raise

    def _relock_definition(self, definition: PlanDefinition, board_id: str) -> None:
        for action in (definition.action_name, *definition.paired_actions):
            if action == definition.action_name or self.registry.is_registered(action):
                self.registry.relock(action, board_id)

    def close_run(self) -> None:
        with self._guard:
            for state in self.server_run.plans.values():
                if isinstance(state, _PlanState) and state.status is PlanStatus.ACTIVE:
                    state.status = PlanStatus.RUN_CLOSED
                    state.close_reason = "Server Run ended"
                    self._relock_definition(state.definition, state.board_id)
            self.server_run.plans.clear()
            self._initialized_plan_tools.clear()
            self._accepted_cycles.clear()
        self.permission_provider.reset()
