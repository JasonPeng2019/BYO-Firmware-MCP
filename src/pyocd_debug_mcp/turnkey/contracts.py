"""Strict data contracts for Server A and its middleman process."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re
from typing import Final


class TurnkeyContractError(ValueError):
    """A Client A input or middleman decision violates the declared contract."""


MEMORY_FIELDS: Final = frozenset({"action", "reasoning", "codebase_changes", "result"})
DECISION_FIELDS: Final = frozenset(
    {
        "action",
        "action_params",
        "observation_summary",
        "problem_hypotheses",
        "current_strategy",
        "failed_strategies",
        "carry_forward_warnings",
    }
)
DECISION_ACTION_PARAMS: Final[Mapping[str, frozenset[str]]] = {
    "next_step": frozenset(),
    "continue_step": frozenset(),
    "return_text_to_user": frozenset({"text"}),
    "request_green_check": frozenset(),
    "validate_green_check": frozenset({"script_args", "preparation_summary"}),
    "finish_task": frozenset({"task_result"}),
    "fail_task": frozenset({"failure_reason"}),
    "finalize_needs_user_permission": frozenset({"permission_request"}),
}


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TurnkeyContractError(f"{label} must be non-empty text")
    return value.strip()


def _string_list(value: object, label: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise TurnkeyContractError(f"{label} must be an array of text values")
    result = tuple(_text(item, f"{label} item") for item in value)
    if not allow_empty and not result:
        raise TurnkeyContractError(f"{label} must not be empty")
    return result


def _literal_string_list(value: object, label: str) -> tuple[str, ...]:
    """Keep green-check literals byte-for-byte instead of normalizing them as prose."""

    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise TurnkeyContractError(f"{label} must be an array of text values")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise TurnkeyContractError(f"{label} item must be non-empty text")
        result.append(item)
    if not result:
        raise TurnkeyContractError(f"{label} must not be empty")
    return tuple(result)


@dataclass(frozen=True, slots=True)
class MemoryTurn:
    action: str
    reasoning: str
    codebase_changes: str
    result: str

    @classmethod
    def parse(cls, value: object, label: str) -> MemoryTurn:
        if not isinstance(value, Mapping) or set(value) != MEMORY_FIELDS:
            raise TurnkeyContractError(
                f"{label} must contain exactly {sorted(MEMORY_FIELDS)}"
            )
        return cls(*(_text(value[name], f"{label}.{name}") for name in (
            "action", "reasoning", "codebase_changes", "result"
        )))

    def render(self) -> str:
        return "\n".join(
            (
                f"action: {self.action}",
                f"reasoning: {self.reasoning}",
                f"codebase_changes: {self.codebase_changes}",
                f"result: {self.result}",
            )
        )


@dataclass(frozen=True, slots=True)
class CallOwnedScript:
    """Text script materialized only inside one call's disposable artifact root."""

    filename: str
    content: str
    command: tuple[str, ...]

    @classmethod
    def parse(cls, value: object) -> CallOwnedScript:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping) or set(value) != {"filename", "content", "command"}:
            raise TurnkeyContractError(
                "green_check_script must contain exactly filename, content, and command"
            )
        filename = _text(value.get("filename"), "green_check_script.filename")
        content = _text(value.get("content"), "green_check_script.content")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", filename) is None:
            raise TurnkeyContractError(
                "green_check_script.filename must be one safe basename"
            )
        if len(content.encode("utf-8")) > 256 * 1024:
            raise TurnkeyContractError("green_check_script.content exceeds 256 KiB")
        command = _string_list(
            value.get("command"), "green_check_script.command", allow_empty=False
        )
        if command.count("{script}") != 1:
            raise TurnkeyContractError(
                "green_check_script.command must contain the exact {script} token once"
            )
        unknown = [item for item in command if "{" in item and item not in {"{script}", "{python}"}]
        if unknown:
            raise TurnkeyContractError(
                "green_check_script.command supports only {script} and {python} placeholders"
            )
        return cls(filename, content, command)


@dataclass(frozen=True, slots=True)
class TurnkeyContext:
    tool_summary: str
    task: str
    tier1: tuple[MemoryTurn, MemoryTurn, MemoryTurn, MemoryTurn]
    memory_tier2: str
    memory_tier3: str
    relevant_files: str
    board_facts: str
    reference_artifacts: str
    build_context: str
    iteration_max: int
    green_check_guide: str
    green_check_script: CallOwnedScript
    green_check_expected_outputs: tuple[str, ...]
    task_detail_label: str | None = None
    task_detail: str | None = None

    @classmethod
    def parse(cls, values: Mapping[str, object]) -> TurnkeyContext:
        raw_max = values.get("iteration_max")
        if isinstance(raw_max, bool) or not isinstance(raw_max, int) or not 1 <= raw_max <= 100:
            raise TurnkeyContractError("iteration_max must be an integer from 1 through 100")
        return cls(
            _text(values.get("tool_summary"), "tool_summary"),
            _text(values.get("task"), "task"),
            tuple(  # type: ignore[arg-type]
                MemoryTurn.parse(values.get(f"memory_tier1_turn{index}"), f"memory_tier1_turn{index}")
                for index in range(1, 5)
            ),
            _text(values.get("memory_tier2"), "memory_tier2"),
            _text(values.get("memory_tier3"), "memory_tier3"),
            _text(values.get("relevant_files"), "relevant_files"),
            _text(values.get("board_facts"), "board_facts"),
            _text(values.get("reference_artifacts"), "reference_artifacts"),
            _text(values.get("build_context"), "build_context"),
            raw_max,
            _text(values.get("green_check_guide"), "green_check_guide"),
            CallOwnedScript.parse(values.get("green_check_script")),
            _literal_string_list(
                values.get("green_check_expected_outputs"),
                "green_check_expected_outputs",
            ),
        )


@dataclass(frozen=True, slots=True)
class MiddlemanDecision:
    action: str
    action_params: Mapping[str, object]
    observation_summary: str
    problem_hypotheses: tuple[str, ...]
    current_strategy: str
    failed_strategies: tuple[str, ...]
    carry_forward_warnings: tuple[str, ...]

    @classmethod
    def parse(cls, value: object) -> MiddlemanDecision:
        if not isinstance(value, Mapping):
            raise TurnkeyContractError("middleman reply must be one JSON object")
        unknown = set(value) - DECISION_FIELDS
        required = DECISION_FIELDS - {"problem_hypotheses"}
        missing = required - set(value)
        if unknown or missing:
            raise TurnkeyContractError(
                f"decision fields mismatch; missing={sorted(missing)} unknown={sorted(unknown)}"
            )
        action = _text(value.get("action"), "action")
        expected = DECISION_ACTION_PARAMS.get(action)
        if expected is None:
            raise TurnkeyContractError(f"unsupported middleman action: {action}")
        params = value.get("action_params")
        if not isinstance(params, Mapping) or set(params) != expected:
            raise TurnkeyContractError(
                f"{action}.action_params must contain exactly {sorted(expected)}"
            )
        if action == "validate_green_check":
            _string_list(params.get("script_args"), "script_args")
            _text(params.get("preparation_summary"), "preparation_summary")
        elif expected:
            for name in expected:
                _text(params.get(name), f"{action}.{name}")
        return cls(
            action,
            dict(params),
            _text(value.get("observation_summary"), "observation_summary"),
            _string_list(value.get("problem_hypotheses", ()), "problem_hypotheses"),
            _text(value.get("current_strategy"), "current_strategy"),
            _string_list(value.get("failed_strategies"), "failed_strategies"),
            _string_list(value.get("carry_forward_warnings"), "carry_forward_warnings"),
        )


def custom_steps(value: object) -> tuple[str, ...]:
    """Validate one unbounded, contiguous ``step_1`` through ``step_n`` plan object."""

    if not isinstance(value, Mapping) or not value:
        raise TurnkeyContractError("complex_task plan must be a non-empty JSON object")
    expected = {f"step_{index}" for index in range(1, len(value) + 1)}
    if set(value) != expected:
        raise TurnkeyContractError(
            "complex_task plan keys must be contiguous from step_1 through step_n"
        )
    return tuple(_text(value[f"step_{index}"], f"step_{index}") for index in range(1, len(value) + 1))
